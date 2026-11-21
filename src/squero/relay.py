"""The Relay's connection (slice 3.1): MCP over a websocket the Relay dials, reversed.

A Relay runs inside a client's network and dials the Hub; over that one socket the
Relay is the MCP *server* (its cluster's tools) and the Hub the MCP *client*. mcp 2.3
has no websocket transport, so `bridge` turns any socket into the (read, write) stream
pair MCP runs over, framing each JSON-RPC message as one text frame exactly as its
stdio transport frames a line. The socket is two callables, so the Hub's Starlette
socket, the Relay's websockets connection and an in-memory pair all fit.

Everything a Relay sends is client-controlled data: the Hub keeps its own allowlist,
cluster and namespace checks, redaction and caps on it (ADR 0002).

Alerts (slice 3.4) ride the same socket beside MCP: the Relay takes its cluster's
Alertmanager webhook and sends each alert group as one JSON-RPC request (`ALERT`); the
Hub answers it once the alert is taken. The bridge hands those to `intercept`, so the
MCP session never sees them.
"""

import asyncio
import contextlib
import hashlib
import hmac
import importlib.metadata
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from functools import partial
from typing import Any, Literal

import anyio
import httpx
import uvicorn
import websockets
from fastmcp import Client, FastMCP
from fastmcp.client.transports.base import ClientTransport
from fastmcp.client.transports.memory import _enter_server_lifespan, _lowlevel_of
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.tools.base import ToolResult
from mcp import ClientSession, types
from mcp.shared.message import SessionMessage
from mcp.types import TextContent, ToolAnnotations
from pydantic import SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from squero import contracts, protocol, usage
from squero.contracts import Contract
from squero.models import AlertGroup
from squero.servers import kubernetes, prometheus
from squero.sizing import utc

log = logging.getLogger("squero.relay")

Send = Callable[[str], Awaitable[None]]
Receive = Callable[[], Awaitable[str | None]]  # None: the socket closed
MAX_FRAME = 4 * 1024 * 1024  # characters: tool output is capped well below this
ALERT = "squero/alert"  # a Relay's alert group, beside MCP on its socket (slice 3.4)
Intercept = Callable[[Any], bool]  # True: a message beside MCP, taken; the session never sees it


class FrameTooLarge(ValueError):
    pass


@asynccontextmanager
async def bridge(
    send: Send, receive: Receive, intercept: Intercept | None = None
) -> AsyncIterator[tuple[Any, Any]]:
    """MCP's (read, write) streams over a socket. A frame that isn't one JSON-RPC
    message, or is too large, reaches the session as an Exception, as mcp's stdio
    transport does with a bad line: it never ends the connection by itself."""
    read_writer, read = anyio.create_memory_object_stream[SessionMessage | Exception](0)
    write, write_reader = anyio.create_memory_object_stream[SessionMessage](0)

    async def inbound() -> None:
        async with read_writer:
            while (frame := await receive()) is not None:
                if len(frame) > MAX_FRAME:
                    await read_writer.send(FrameTooLarge(f"a frame of {len(frame)} characters"))
                    continue
                try:
                    message = types.jsonrpc_message_adapter.validate_json(frame, by_name=False)
                except ValueError as e:
                    await read_writer.send(e)
                    continue
                if intercept is not None and intercept(message):
                    continue
                await read_writer.send(SessionMessage(message))

    async def outbound() -> None:
        async with write_reader:
            async for m in write_reader:
                await send(m.message.model_dump_json(by_alias=True, exclude_unset=True))

    async with anyio.create_task_group() as tg:
        tg.start_soon(inbound)
        tg.start_soon(outbound)
        try:
            yield read, write
        finally:
            tg.cancel_scope.cancel()


def locked(send: Send) -> Send:
    """One frame at a time: MCP's and an alert's share the socket."""
    lock = anyio.Lock()

    async def one(frame: str) -> None:
        async with lock:
            await send(frame)

    return one


async def serve(
    server: FastMCP, send: Send, receive: Receive, intercept: Intercept | None = None
) -> None:
    """The Relay's side: run `server` over the socket until it closes.
    ponytail: fastmcp 4.1 has no public way to serve over given streams; this is what its
    own in-memory transport does (exact pins; the bridge tests break on a bump)."""
    lowlevel = _lowlevel_of(server)
    async with (
        _enter_server_lifespan(server=server),
        bridge(send, receive, intercept) as (read, write),
    ):
        await lowlevel.run(read, write, lowlevel.create_initialization_options())


class RelayTransport(ClientTransport):
    """The Hub's side: a fastmcp Client over a Relay's socket."""

    def __init__(self, send: Send, receive: Receive, intercept: Intercept | None = None) -> None:
        self.send, self.receive, self.intercept = send, receive, intercept

    @asynccontextmanager
    async def connect_session(self, **session_kwargs: Any) -> AsyncIterator[ClientSession]:
        session_kwargs.pop("transport_options", None)
        async with (
            bridge(self.send, self.receive, self.intercept) as (read, write),
            ClientSession(read, write, **session_kwargs) as session,
        ):
            yield session


# --- the Hub's side: who a Relay is, and its live connection ------------------------------

Who = tuple[str, str]  # (contract id, cluster)
# Takes one alert group a Relay forwards (its Alertmanager's token and the group, as the
# Relay sent them: unchecked): the HTTP status and body the Relay answers Alertmanager with.
Alerts = Callable[[Who, Any], Awaitable[tuple[int, Any]]]


def tokens(
    by_id: dict[str, Contract],
    role: Literal["Relay", "Actuator"] = "Relay",
    taken: list[tuple[bytes, Who]] | None = None,
) -> list[tuple[bytes, Who]]:
    """Each Relay's (or Actuator's, slice 12.1) cluster token, as its sha256 and whom it
    names. ValueError: one that's too short, or names two clusters or roles (`taken`: the
    other role's tokens, so a Relay's token never opens an Actuator's door)."""
    out: list[tuple[bytes, Who]] = []
    for c in by_id.values():
        for cl in c.clusters:
            name = cl.relay_token_secret if role == "Relay" else cl.actuator_token_secret
            if not name:
                continue
            token = c.secret(name).get_secret_value()
            if len(token) < 32:
                raise ValueError(
                    f"{c.id}/{cl.name}: its {role} token is too short (32+ characters)"
                )
            digest = hashlib.sha256(token.encode()).digest()
            if any(hmac.compare_digest(digest, d) for d, _ in [*out, *(taken or [])]):
                raise ValueError(
                    f"{c.id}/{cl.name}: its {role} token is another Relay's or Actuator's"
                )
            out.append((digest, (c.id, cl.name)))
    return out


def who(authorization: str, known: list[tuple[bytes, Who]]) -> Who | None:
    """Whom a `Bearer <token>` names, or None. Every token is compared, in constant time:
    which one matched, or that none did, takes the same time."""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    digest = hashlib.sha256(token.encode()).digest()
    found = None
    for d, named in known:
        if hmac.compare_digest(digest, d):
            found = named
    return found


@dataclass
class Live:
    client: Client[Any]
    close: Callable[[], Awaitable[None]]
    since: float
    loop: asyncio.AbstractEventLoop  # where its session runs: the Hub's reads call into it
    version: str | None = None  # as the Relay says: shown, never trusted


class Relays:
    """The connected Relays, by (contract, cluster): only the token's lookup puts one
    here, never anything the Relay sends. A new connection with the same token
    replaces the old one (a Relay that restarted before the Hub saw it go)."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.live: dict[Who, Live] = {}
        self.clock, self.started = clock, clock()
        self.gone: dict[Who, float] = {}  # when each last went

    def get(self, contract_id: str, cluster: str) -> Client[Any] | None:
        found = self.live.get((contract_id, cluster))
        return found.client if found else None

    def state(self, contract_id: str, cluster: str) -> tuple[bool, float, str]:
        """Connected or not, since when, and that in words (Health, the Agenda)."""
        if live := self.live.get((contract_id, cluster)):
            version = f", version {live.version}" if live.version else ""
            return True, live.since, f"connected since {utc(live.since)}{version}"
        if gone := self.gone.get((contract_id, cluster)):
            return False, gone, f"offline since {utc(gone)}"
        return False, self.started, f"not connected since the Hub started at {utc(self.started)}"

    async def hold(
        self,
        named: Who,
        send: Send,
        receive: Receive,
        close: Callable[[], Awaitable[None]],
        alerts: Alerts | None = None,
    ) -> None:
        """Serve one Relay's connection until it closes; its MCP session opens first.
        `alerts` takes each alert group it forwards: its answer goes back to the Relay."""
        closed = anyio.Event()
        send = locked(send)

        async def until_closed() -> str | None:
            frame = await receive()
            if frame is None:
                closed.set()
            return frame

        async def answer(m: types.JSONRPCRequest) -> None:
            assert alerts is not None
            reply: types.JSONRPCResponse | types.JSONRPCError
            try:
                status, body = await alerts(named, m.params)
                reply = types.JSONRPCResponse(
                    jsonrpc="2.0", id=m.id, result={"status": status, "body": body}
                )
            except Exception:
                log.exception("relay %s/%s: an alert it forwarded failed", *named)
                failed = types.ErrorData(code=-32603, message="the Hub failed to take it")
                reply = types.JSONRPCError(jsonrpc="2.0", id=m.id, error=failed)
            with contextlib.suppress(Exception):  # gone: the Relay answers 503 itself
                await send(reply.model_dump_json(by_alias=True, exclude_unset=True))

        def intercept(m: Any) -> bool:
            if not (isinstance(m, types.JSONRPCRequest) and m.method == ALERT):
                return False
            tg.start_soon(answer, m)  # the next frame is read meanwhile
            return True

        transport = RelayTransport(send, until_closed, intercept if alerts else None)
        async with anyio.create_task_group() as tg:
            try:
                async with Client(transport) as client:
                    info = client.server_info
                    version = info.version[:40] if info and info.version else None
                    me = Live(client, close, self.clock(), asyncio.get_running_loop(), version)
                    old = self.live.get(named)
                    self.live[named] = me
                    log.info("relay %s/%s connected", *named)
                    if old is not None:
                        await old.close()
                    try:
                        await closed.wait()
                    finally:
                        if self.live.get(named) is me:
                            del self.live[named]
                            self.gone[named] = self.clock()
                        log.info("relay %s/%s disconnected", *named)
            finally:
                tg.cancel_scope.cancel()  # an alert still being taken: its Relay has gone


# The Hub's connected Relays: one process, one registry, so its own reads (Sizings,
# bills), Health and the Agenda see the connections its /relay route holds.
HUB = Relays()
READ_SECONDS = 120  # one of the Hub's own reads: a 14-day range takes longer than a tool call


class Reader(usage.Prom):
    """The Hub's own PromQL on a Relay's cluster (slice 3.3b): the Relay's `hub_promql_*`,
    called on the loop that holds its connection, from the worker thread usage reads run
    in (never from that loop: it would wait on itself). Anything but rows is a
    UsageError, as Prometheus' own errors are."""

    def __init__(self, relays: Relays, who: Who) -> None:  # no HTTP client of its own
        self.relays, self.who = relays, who

    def _call(self, tool: str, args: dict[str, Any]) -> list[dict[str, Any]]:
        cluster = self.who[1]
        live = self.relays.live.get(self.who)
        if live is None:
            raise usage.UsageError(f"{OFFLINE}: cluster {cluster!r} has no Relay connected")
        call = asyncio.wait_for(
            live.client.call_tool(tool, args, raise_on_error=False), READ_SECONDS
        )
        try:
            result = asyncio.run_coroutine_threadsafe(call, live.loop).result(READ_SECONDS + 5)
        except TimeoutError:
            raise usage.UsageError(
                f"{OFFLINE}: cluster {cluster!r}'s Relay didn't answer in {READ_SECONDS} s"
            ) from None
        except Exception as e:  # the connection ended under the read
            raise usage.UsageError(
                f"{OFFLINE}: cluster {cluster!r}'s Relay: {type(e).__name__}"
            ) from None
        texts = " ".join(getattr(b, "text", "") for b in result.content)
        if result.is_error:
            raise usage.UsageError(f"through cluster {cluster!r}'s Relay: {texts[:300]}")
        rows = (result.structured_content or {}).get("result")
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            raise usage.UsageError(
                f"cluster {cluster!r}'s Relay answered something else than series"
            )
        return rows

    def instant(self, promql: str, at: float) -> list[dict[str, Any]]:
        return self._call("hub_promql_instant", {"promql": promql, "at": at})

    def range(self, promql: str, start: float, end: float, step: int) -> list[dict[str, Any]]:
        return self._call(
            "hub_promql_range", {"promql": promql, "start": start, "end": end, "step": step}
        )


OFFLINE = "Relay offline"
# The tools a Relay answers for its cluster: the same names on the Hub and the Relay.
FORWARDED = frozenset(
    {f"kubernetes_{t}" for t in kubernetes.ACCESS} | {f"prometheus_{t}" for t in prometheus.ACCESS}
)


class RelayOffline(RuntimeError):
    """A Run called a Relay that isn't connected, or didn't answer in time."""


class Forward(Middleware):
    """The Hub's side of a call to a Relay's cluster (slice 3.3a): the Hub's own cluster
    and namespace check, then the same call on that cluster's live Relay. The tools are
    the Hub's own (its list, schemas, approval gate); nothing a Relay offers is ever
    listed, so `hub_promql_*` is never the agent's. A call to another cluster, or any
    other tool (a Sizing's takes a cluster too), passes by untouched."""

    def __init__(self, contract: Contract, relays: Relays, seconds: float) -> None:
        self.contract_id, self.relays, self.seconds = contract.id, relays, seconds
        self.clusters = {c.name: set(c.namespaces) for c in contract.clusters if c.via == "relay"}
        self.gitops = {c.name: set(c.gitops_namespaces) for c in contract.clusters}

    def within(self, cluster: str, row: Any) -> bool:
        """A change the Contract may see: every namespace it names is one of the
        cluster's (a GitOps notice: one of its GitOps namespaces)."""
        where = row.get("namespace") if isinstance(row, dict) else None
        if not isinstance(where, str):
            return False
        allowed = self.gitops[cluster] if "gitops" in row else self.clusters[cluster]
        return set(where.split(", ")) <= allowed

    async def on_call_tool(self, context: MiddlewareContext[Any], call_next: Any) -> Any:
        name, args = context.message.name, context.message.arguments or {}
        cluster = args.get("cluster")
        if name not in FORWARDED or not isinstance(cluster, str) or cluster not in self.clusters:
            return await call_next(context)
        if "namespace" in args and args["namespace"] not in self.clusters[cluster]:
            raise ToolError(
                f"namespace {args['namespace']!r} is not allowed in cluster {cluster!r}; "
                f"allowed: {sorted(self.clusters[cluster])}"
            )
        client = self.relays.get(self.contract_id, cluster)
        if client is None:
            raise ToolError(f"{OFFLINE}: cluster {cluster!r} has no Relay connected")
        try:
            with anyio.fail_after(self.seconds):
                result = await client.call_tool(name, args, raise_on_error=False)
        except TimeoutError:
            raise ToolError(
                f"{OFFLINE}: cluster {cluster!r}'s Relay didn't answer in {self.seconds:g} s"
            ) from None
        except Exception as e:  # the connection ended under the call
            raise ToolError(f"{OFFLINE}: cluster {cluster!r}'s Relay: {type(e).__name__}") from None
        if name == "kubernetes_recent_changes" and not result.is_error:
            # It takes no namespace: the Relay reads what it allows, which may be more
            # than the Contract does, so the Hub keeps only the Contract's.
            rows = (result.structured_content or {}).get("result")
            if not isinstance(rows, list):
                raise ToolError(f"cluster {cluster!r}'s Relay sent changes that aren't a list")
            kept = [r for r in rows if self.within(cluster, r)]
            return ToolResult([TextContent(type="text", text=json.dumps(kept))], {"result": kept})
        return ToolResult(result.content, result.structured_content, is_error=result.is_error)


# --- the Relay's side ---------------------------------------------------------------------


class Uplink:
    """The Relay's alerts to the Hub (slice 3.4), over whichever connection is live. It
    keeps no queue: an alert it can't hand over, or that the Hub doesn't answer in
    `ALERT_SECONDS`, is a 503 and Alertmanager sends it again."""

    def __init__(self) -> None:
        self.send: Send | None = None
        self.waiting: dict[str, asyncio.Future[Any]] = {}
        self.sent = 0

    @contextmanager
    def over(self, send: Send) -> Iterator[None]:
        """Alerts go through `send` while the connection holds."""
        self.send = send
        try:
            yield
        finally:
            self.send = None
            for answer in self.waiting.values():
                if not answer.done():
                    answer.set_exception(ConnectionError("the connection closed"))

    def answered(self, m: Any) -> bool:
        """The Hub's answer to an alert: taken, never the MCP session's."""
        if not isinstance(m, types.JSONRPCResponse | types.JSONRPCError):
            return False
        if not (isinstance(m.id, str) and m.id.startswith("alert-")):
            return False
        if (answer := self.waiting.get(m.id)) is not None and not answer.done():
            answer.set_result(m)
        return True

    async def forward(self, token: str, group: dict[str, Any]) -> tuple[int, Any]:
        if self.send is None:
            return 503, {"detail": "the Relay has no connection to the Hub"}
        self.sent += 1
        key = f"alert-{self.sent}"
        answer = self.waiting[key] = asyncio.get_running_loop().create_future()
        request = types.JSONRPCRequest(
            jsonrpc="2.0", id=key, method=ALERT, params={"token": token, "group": group}
        )
        try:
            await self.send(request.model_dump_json(by_alias=True, exclude_unset=True))
            m = await asyncio.wait_for(answer, ALERT_SECONDS)
        except TimeoutError:
            return 503, {"detail": f"the Hub didn't answer in {ALERT_SECONDS} s"}
        except Exception as e:  # the connection went under it
            return 503, {"detail": f"no connection to the Hub: {type(e).__name__}"}
        finally:
            del self.waiting[key]
        if isinstance(m, types.JSONRPCError):
            return 502, {"detail": "the Hub failed to take it"}
        result = m.result if isinstance(m.result, dict) else {}
        status = result.get("status")
        if not isinstance(status, int) or not 200 <= status < 600:
            return 502, {"detail": "the Hub answered something else"}
        return status, result.get("body")


ALERT_SECONDS = 10  # the Hub only stores it before answering
MAX_ALERT = 1024 * 1024  # bytes: an alert group is far smaller; a frame holds 4 MiB


def webhook(uplink: Uplink) -> Starlette:
    """The Relay's Alertmanager receiver, `POST /alertmanager` inside the client's
    network: the alert group goes to the Hub with the bearer token Alertmanager sent,
    which the Hub checks against the Contract's own webhook token."""

    async def alertmanager(request: Request) -> JSONResponse:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            return JSONResponse({"detail": "invalid token"}, 401)
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_ALERT:
                return JSONResponse({"detail": "too large"}, 413)
        try:
            group = AlertGroup.model_validate_json(body)
        except ValidationError:
            return JSONResponse({"detail": "not an Alertmanager alert group"}, 422)
        status, answer = await uplink.forward(token, group.model_dump(mode="json"))
        return JSONResponse(answer, status)

    return Starlette(routes=[Route("/alertmanager", alertmanager, methods=["POST"])])


async def dial(url: str, token: str, server: FastMCP, uplink: Uplink | None = None) -> None:
    """Connect to the Hub and serve `server` over the socket until it closes; `uplink`'s
    alerts go over it meanwhile."""
    headers = {"Authorization": f"Bearer {token}", protocol.HEADER: str(protocol.OWN["Relay"])}
    uplink = uplink or Uplink()
    async with websockets.connect(url, additional_headers=headers, max_size=MAX_FRAME) as ws:

        async def receive() -> str | None:
            try:
                frame = await ws.recv()
            except websockets.ConnectionClosed:
                return None
            return frame if isinstance(frame, str) else None  # a binary frame ends it

        send = locked(ws.send)
        with uplink.over(send):
            await serve(server, send, receive, uplink.answered)


class RelaySettings(BaseSettings):
    """The Relay's own configuration, set by the client in its manifest (slice 3.2)."""

    model_config = SettingsConfigDict(env_prefix="SQUERO_RELAY_")

    hub: str  # wss://<the Hub's domain>/relay
    token: SecretStr
    cluster: str  # its name in the Contract
    namespaces: list[str]  # JSON, as its RoleBindings grant
    gitops_namespaces: list[str] = []
    prometheus_url: str | None = None  # inside the client's network
    prometheus_token: SecretStr | None = None
    writes: bool = False  # the client's own switch: no write tool exists without it
    tool_output_limit: int = 8000
    # Its Alertmanager receiver. Not `port`: Kubernetes sets SQUERO_RELAY_PORT=tcp://... in
    # every pod in the namespace of a Service named squero-relay (its service links).
    alerts_port: int = 8080


def relay_server(
    s: RelaySettings,
    apis: dict[str, tuple[Any, ...]] | None = None,
    http: httpx.Client | None = None,
) -> FastMCP:
    """What the Relay serves: its cluster's Kubernetes tools (write tools only when the
    client turns writes on), its Prometheus tools, and the Hub's own PromQL as
    `hub_promql_*`, which the Hub never puts on the agent's list. Tool names are the
    Hub's own (`kubernetes_list_pods`...). apis, http: fakes, for tests."""
    cluster: dict[str, Any] = {"name": s.cluster, "namespaces": s.namespaces, "in_cluster": True}
    cluster["gitops_namespaces"] = s.gitops_namespaces
    k8s = {"name": "kubernetes", "enable_actions": s.writes}
    c = contracts.build(
        {"id": "relay", "name": "relay", "clusters": [cluster], "integrations": [k8s]}
    )
    k8s_integration = c.integrations[0]
    assert isinstance(k8s_integration, contracts.Kubernetes)
    root = FastMCP(f"squero-relay-{s.cluster}", version=importlib.metadata.version("squero"))
    tools = kubernetes.build_server(c, k8s_integration, s.tool_output_limit, apis)
    root.mount(tools, namespace="kubernetes")
    if s.prometheus_url:
        store = {s.cluster: (s.prometheus_url, usage.auth(s.prometheus_token))}
        tested = {s.cluster: http} if http else None
        root.mount(prometheus.build_server(store, True, tested), namespace="prometheus")
        target = usage.Target(s.cluster, s.prometheus_url, s.prometheus_token, {}, s.namespaces)
        prom = usage.Prom(target, http)

        @root.tool(annotations=ToolAnnotations(readOnlyHint=True))
        async def hub_promql_instant(promql: str, at: float) -> list[dict[str, Any]]:
            """The Hub's own query (Sizings, prices, Health): never the agent's."""
            return await asyncio.to_thread(prom.instant, promql, at)

        @root.tool(annotations=ToolAnnotations(readOnlyHint=True))
        async def hub_promql_range(
            promql: str, start: float, end: float, step: int
        ) -> list[dict[str, Any]]:
            """The Hub's own range query: never the agent's."""
            return await asyncio.to_thread(prom.range, promql, start, end, step)

    return root


async def run(
    s: RelaySettings,
    server: FastMCP,
    connect: Callable[[str, str, FastMCP], Awaitable[None]] = dial,
    sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Dial the Hub and serve, forever: after a refusal, a dropped connection or the
    Hub going away, wait and dial again, 1 s doubling to a minute, back to 1 s once
    a connection held (a token the Hub refuses keeps retrying, slowly)."""
    wait = 1.0
    while True:
        started = clock()
        try:
            await connect(s.hub, s.token.get_secret_value(), server)
            why = "the Hub closed the connection"
        except websockets.InvalidStatus as e:
            code = e.response.status_code
            # 401 or 403 is the token; anything else (Caddy's 502 while the Hub is down) isn't
            hint = ": check the token" if code in (401, 403) else ""
            why = f"the Hub refused the connection ({code}){hint}"
            if code == 426:  # slice 18.3: which side to upgrade
                why = protocol.told("Relay", e.response.headers.get(protocol.HEADER))
        except (OSError, websockets.WebSocketException) as e:
            why = f"no connection to the Hub: {type(e).__name__}"
        if clock() - started > 60:
            wait = 1.0
        log.warning("relay %s: %s; dialing again in %.0f s", s.cluster, why, wait)
        await sleep(wait)
        wait = min(wait * 2, 60.0)


async def main(s: RelaySettings) -> None:
    """`squero relay`: the receiver for its cluster's Alertmanager, and the connection."""
    uplink = Uplink()
    everywhere = "0.0.0.0"  # noqa: S104  # its Service, inside the client's cluster
    config = uvicorn.Config(
        webhook(uplink), host=everywhere, port=s.alerts_port, log_level="warning"
    )
    async with anyio.create_task_group() as tg:
        tg.start_soon(uvicorn.Server(config).serve)
        await run(s, relay_server(s), partial(dial, uplink=uplink))
