"""The Relay's connection (slice 3.1): MCP served by the Relay over a socket it dials,
the token binding it to one Contract's cluster, and a Relay's cluster never reached
Direct."""

import importlib.metadata
import json
import logging
import socket
from collections.abc import Callable
from typing import Any

import anyio
import pytest
import uvicorn
import websockets
from fastmcp import Client, FastMCP
from pydantic import SecretStr, ValidationError

from squero import contracts, relay, usage
from squero.api import create_app
from squero.config import Settings
from squero.servers import kubernetes
from tests.conftest import make_contract, scripted

A_TOKEN = "a" * 40
B_TOKEN = "b" * 40


def relayed(contract_id: str, token: str, **extra: Any) -> contracts.Contract:
    """A Contract with a Direct cluster `test` and a Relay cluster `edge`."""
    data = {
        "id": contract_id,
        "name": contract_id,
        "clusters": [
            {"name": "test", "context": "test", "namespaces": ["shop"]},
            {"name": "edge", "namespaces": ["shop"], "via": "relay", "relay_token_secret": "r"},
        ],
        "integrations": [{"name": "kubernetes"}],
        **extra,
    }
    return contracts.build(data, secrets={"r": SecretStr(token)})


async def until(ready: Callable[[], bool]) -> None:
    """Poll: the registry and uvicorn have nothing to wait on."""
    with anyio.fail_after(10):
        while not ready():  # noqa: ASYNC110
            await anyio.sleep(0.01)


def tools_server(calls: list[str] | None = None) -> FastMCP:
    server = FastMCP("kubernetes")

    @server.tool
    def list_pods(namespace: str) -> str:
        if calls is not None:
            calls.append(namespace)
        return f"pods in {namespace}"

    return server


# --- the Contract: a Relay's cluster -------------------------------------------------------


def test_a_relay_cluster_names_its_token_and_none_of_the_hubs_credentials() -> None:
    base = {"name": "edge", "namespaces": ["shop"], "via": "relay"}
    with pytest.raises(ValidationError, match="via: relay needs relay_token_secret"):
        contracts.Cluster.model_validate(base)
    for field, value in (
        ("context", "edge"),
        ("in_cluster", True),
        ("kubeconfig_secret", "k"),
        ("allow_exec", True),
        ("prometheus_url", "http://p:9090"),
    ):
        with pytest.raises(ValidationError, match="the Relay holds these"):
            contracts.Cluster.model_validate({**base, "relay_token_secret": "t", field: value})
    with pytest.raises(ValidationError, match="relay_token_secret is for via: relay"):
        contracts.Cluster.model_validate(
            {"name": "x", "namespaces": ["a"], "context": "x", "relay_token_secret": "t"}
        )
    ok = contracts.Cluster.model_validate(
        {**base, "relay_token_secret": "t", "prometheus_labels": {"cluster": "edge"}}
    )
    assert ok.via == "relay" and ok.context is None


def test_the_hub_never_reaches_a_relay_cluster_direct_nor_its_prometheus() -> None:
    c = relayed("acme", A_TOKEN, integrations=[
        {"name": "kubernetes"}, {"name": "prometheus", "url": "http://shared:9090"},
    ])  # fmt: skip
    assert [cl.name for cl in c.direct_clusters] == ["test"]
    assert "r" in c.secret_refs()  # checked like every secret
    edge_target = next(t for t in usage.targets(c) if t.cluster == "edge")
    assert (edge_target.url, edge_target.token) == ("", None)  # not even the shared URL
    assert edge_target.relay == ("acme", "edge")  # read through its Relay (3.3b)
    edge = next(cl for cl in c.clusters if cl.name == "edge")
    with pytest.raises(RuntimeError, match="via its Relay"):
        kubernetes.api_client(edge, c)  # it has no context: the default would be the Hub's
    probes = [label for label, _, _ in __import__("squero.agent").agent.probes(c)]
    assert {"kubernetes test", "kubernetes edge", "prometheus edge"} <= set(probes)  # 3.3b
    without = relayed("acme", A_TOKEN)  # no Prometheus: no Sizings, as no PromQL tools
    assert usage.targets(without) == []


@pytest.mark.anyio
async def test_the_hubs_own_kubernetes_tools_refuse_a_relay_cluster() -> None:
    from tests.conftest import make_fake_cluster

    c = relayed("acme", A_TOKEN)
    server = kubernetes.build_server(c, c.integrations[0], 8000, {"test": make_fake_cluster()})  # type: ignore[arg-type]
    async with Client(server) as hub_client:
        r = await hub_client.call_tool("recent_changes", {"cluster": "edge"}, raise_on_error=False)
    assert r.is_error and "reached through its Relay" in r.content[0].text  # type: ignore[union-attr]


# --- tokens -----------------------------------------------------------------------------


def test_each_token_names_one_contracts_cluster() -> None:
    known = relay.tokens({"acme": relayed("acme", A_TOKEN), "bank": relayed("bank", B_TOKEN)})
    assert relay.who(f"Bearer {A_TOKEN}", known) == ("acme", "edge")
    assert relay.who(f"bearer {B_TOKEN}", known) == ("bank", "edge")
    assert relay.who(f"Bearer {A_TOKEN}x", known) is None
    assert relay.who(A_TOKEN, known) is None  # no scheme
    assert relay.who("Bearer ", known) is None
    assert relay.who(f"Basic {A_TOKEN}", known) is None
    with pytest.raises(ValueError, match="another Relay's"):
        relay.tokens({"acme": relayed("acme", A_TOKEN), "bank": relayed("bank", A_TOKEN)})
    with pytest.raises(ValueError, match="too short"):
        relay.tokens({"acme": relayed("acme", "x" * 31)})
    assert relay.tokens({"acme": make_contract()}) == []  # no Relay cluster


# --- the bridge, in memory --------------------------------------------------------------


def pipe() -> tuple[Any, Any, Any, Any]:
    """(hub send, hub receive, relay send, relay receive) over two queues."""
    to_relay_w, to_relay_r = anyio.create_memory_object_stream[str | None](10)
    to_hub_w, to_hub_r = anyio.create_memory_object_stream[str | None](10)
    return to_relay_w.send, to_hub_r.receive, to_hub_w.send, to_relay_r.receive


@pytest.mark.anyio
async def test_mcp_runs_over_the_bridge_and_a_bad_frame_ends_nothing() -> None:
    hub_send, hub_receive, relay_send, relay_receive = pipe()
    calls: list[str] = []
    async with anyio.create_task_group() as tg:
        tg.start_soon(relay.serve, tools_server(calls), relay_send, relay_receive)
        async with Client(relay.RelayTransport(hub_send, hub_receive)) as c:
            assert [t.name for t in await c.list_tools()] == ["list_pods"]
            r = await c.call_tool("list_pods", {"namespace": "shop"})
            assert r.content[0].text == "pods in shop"  # type: ignore[union-attr]
            await hub_send('{"jsonrpc": "2.0", "garbage"')  # not one JSON-RPC message
            huge = "x" * relay.MAX_FRAME  # one valid call, in a frame over the limit
            meta = (  # the envelope mcp's modern protocol wants on every request
                '"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28", '
                '"io.modelcontextprotocol/clientCapabilities": {}}'
            )
            await hub_send(
                '{"jsonrpc": "2.0", "id": 99, "method": "tools/call", "params": '
                f'{{"name": "list_pods", "arguments": {{"namespace": "{huge}"}}, {meta}}}}}'
            )
            await relay_send("not json")  # from the Relay: client-controlled
            r = await c.call_tool("list_pods", {"namespace": "web"})
            assert r.content[0].text == "pods in web"  # type: ignore[union-attr]
        tg.cancel_scope.cancel()
    assert calls == ["shop", "web"]  # the oversized call never ran


@pytest.mark.anyio
async def test_a_new_connection_replaces_the_old_and_a_closed_one_leaves() -> None:
    relays = relay.Relays()
    closed: list[str] = []

    async def connect(name: str, tg: Any) -> Any:
        hub_send, hub_receive, relay_send, relay_receive = pipe()
        tg.start_soon(relay.serve, tools_server(), relay_send, relay_receive)

        async def close() -> None:
            closed.append(name)
            await relay_send(None)  # the socket closes: the Hub's receive sees None

        tg.start_soon(relays.hold, ("acme", "edge"), hub_send, hub_receive, close)
        await until(
            lambda: (x := relays.live.get(("acme", "edge"))) is not None and x.close is close
        )
        return close

    async with anyio.create_task_group() as tg:
        await connect("first", tg)
        assert relays.get("bank", "edge") is None
        second = await connect("second", tg)
        assert closed == ["first"]  # the old one is closed for the new
        r = await relays.get("acme", "edge").call_tool(  # type: ignore[union-attr]
            "list_pods", {"namespace": "shop"}
        )
        assert r.content[0].text == "pods in shop"  # type: ignore[union-attr]
        await second()
        await until(lambda: relays.get("acme", "edge") is None)
        tg.cancel_scope.cancel()


# --- over a real websocket, into the Hub ------------------------------------------------


def hub(
    settings: Settings,
    database_url: str,
    *contracts_: contracts.Contract,
    approver: str = "approver-token",
) -> Any:
    settings.database_url = SecretStr(database_url)
    settings.approver_token = SecretStr(approver)
    by_id = {c.id: c for c in contracts_}
    return create_app(settings, scripted(), lambda c: None, by_id)  # type: ignore[arg-type,return-value]


@pytest.mark.anyio
async def test_a_relay_dials_the_hub_and_serves_only_its_own_cluster(
    settings: Settings, database_url: str, caplog: Any
) -> None:
    caplog.set_level(logging.INFO)
    app = hub(settings, database_url, relayed("acme", A_TOKEN), relayed("bank", B_TOKEN))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    url = f"ws://127.0.0.1:{sock.getsockname()[1]}/relay"
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    relays: relay.Relays = app.state.relays
    async with anyio.create_task_group() as tg:
        tg.start_soon(lambda: server.serve(sockets=[sock]))
        await until(lambda: server.started)
        refused = []
        for headers in ({}, {"Authorization": "Bearer nope"}, {"Authorization": A_TOKEN}):
            with pytest.raises(websockets.InvalidStatus) as e:
                await websockets.connect(url, additional_headers=headers)
            refused.append(e.value.response.status_code)
        assert refused == [403, 403, 403] and relays.live == {}
        tg.start_soon(relay.dial, url, A_TOKEN, tools_server())
        await until(lambda: relays.get("acme", "edge") is not None)
        r = await relays.get("acme", "edge").call_tool(  # type: ignore[union-attr]
            "list_pods", {"namespace": "shop"}
        )
        assert r.content[0].text == "pods in shop"  # type: ignore[union-attr]
        assert list(relays.live) == [("acme", "edge")]  # A's token serves A only
        server.should_exit = True
        tg.cancel_scope.cancel()
    assert A_TOKEN not in caplog.text and "relay acme/edge connected" in caplog.text


def test_a_relay_token_is_no_other_token(settings: Settings, database_url: str) -> None:
    with pytest.raises(RuntimeError, match="a Relay token is the approver token"):
        hub(settings, database_url, relayed("acme", A_TOKEN), approver=A_TOKEN)
    hooked = relayed("acme", A_TOKEN, webhook_token_sha256=contracts.token_hash(B_TOKEN))
    with pytest.raises(RuntimeError, match="bank/edge: its Relay token is 'acme''s webhook"):
        hub(settings, database_url, hooked, relayed("bank", B_TOKEN))
    with pytest.raises(RuntimeError, match="another Relay's"):
        hub(settings, database_url, relayed("acme", A_TOKEN), relayed("bank", A_TOKEN))


# --- the Relay process (slice 3.2) ------------------------------------------------------


def settings_of(**env: Any) -> relay.RelaySettings:
    base = {
        "hub": "ws://hub/relay",
        "token": A_TOKEN,
        "cluster": "edge",
        "namespaces": ["shop"],
        "prometheus_url": "http://prometheus",
    }
    return relay.RelaySettings(**(base | env))


async def names(server: FastMCP) -> list[str]:
    async with Client(server) as c:
        return sorted(t.name for t in await c.list_tools())


@pytest.mark.anyio
async def test_the_relay_serves_its_clusters_reads_with_the_hubs_names_and_no_write() -> None:
    from tests.conftest import make_fake_cluster

    served = await names(relay.relay_server(settings_of(), {"edge": make_fake_cluster()}))
    assert "kubernetes_list_pods" in served and "prometheus_query" in served
    assert {"hub_promql_instant", "hub_promql_range"} <= set(served)
    assert not [n for n in served if "evict" in n or "scale" in n]  # writes are off
    on = await names(relay.relay_server(settings_of(writes=True), {"edge": make_fake_cluster()}))
    assert {"kubernetes_evict_pod", "kubernetes_scale_deployment"} <= set(on)
    bare = await names(
        relay.relay_server(settings_of(prometheus_url=None), {"edge": make_fake_cluster()})
    )
    assert not [n for n in bare if n.startswith(("prometheus_", "hub_"))]


@pytest.mark.anyio
async def test_the_relay_keeps_to_its_own_cluster_and_namespaces() -> None:
    from tests.conftest import make_fake_cluster

    server = relay.relay_server(settings_of(), {"edge": make_fake_cluster()})
    async with Client(server) as c:
        ok = await c.call_tool("kubernetes_list_pods", {"cluster": "edge", "namespace": "shop"})
        other = await c.call_tool(
            "kubernetes_list_pods",
            {"cluster": "edge", "namespace": "kube-system"},
            raise_on_error=False,
        )
        elsewhere = await c.call_tool(
            "kubernetes_list_pods",
            {"cluster": "test", "namespace": "shop"},
            raise_on_error=False,
        )
    assert not ok.is_error and other.is_error and elsewhere.is_error


@pytest.mark.anyio
async def test_the_hubs_own_queries_read_the_relays_prometheus() -> None:
    import httpx

    asked: list[dict[str, str]] = []

    def prometheus(request: httpx.Request) -> httpx.Response:
        asked.append({"path": request.url.path, **request.url.params})
        if request.url.path.endswith("query_range"):
            return httpx.Response(500, json={"status": "error", "error": "down"})
        result = [{"metric": {"namespace": "shop"}, "value": [0, "1"]}]
        return httpx.Response(200, json={"status": "success", "data": {"result": result}})

    http = httpx.Client(base_url="http://prometheus", transport=httpx.MockTransport(prometheus))
    server = relay.relay_server(settings_of(), {"edge": (None, None)}, http)
    async with Client(server) as c:
        r = await c.call_tool("hub_promql_instant", {"promql": "up", "at": 1700000000})
        down = await c.call_tool(
            "hub_promql_range",
            {"promql": "up", "start": 10, "end": 70, "step": 30},
            raise_on_error=False,
        )
    assert r.structured_content["result"][0]["metric"]["namespace"] == "shop"  # type: ignore[index]
    assert down.is_error and "Prometheus error 500" in down.content[0].text  # type: ignore[union-attr]
    assert asked == [
        {"path": "/api/v1/query", "query": "up", "time": "1700000000.0"},
        {
            "path": "/api/v1/query_range",
            "query": "up",
            "start": "10.0",
            "end": "70.0",
            "step": "30",
        },
    ]


@pytest.mark.anyio
async def test_the_relay_dials_again_slower_each_time_and_fast_after_a_held_connection() -> None:
    clock = iter([0, 1, 2, 3, 10, 200, 300, 301])  # each attempt's start and end
    outcomes: list[Any] = [OSError("refused"), OSError("refused"), None, None]
    waits: list[float] = []

    async def connect(url: str, token: str, server: Any) -> None:
        assert (url, token) == ("ws://hub/relay", A_TOKEN)
        outcome = outcomes.pop(0)
        if outcome is not None:
            raise outcome

    async def sleep(seconds: float) -> None:
        waits.append(seconds)
        if not outcomes:
            raise anyio.get_cancelled_exc_class()

    with pytest.raises(anyio.get_cancelled_exc_class()):
        await relay.run(settings_of(), FastMCP("x"), connect, sleep, lambda: next(clock))
    assert waits == [1, 2, 1, 2]  # the third connection held 190 s: back to 1 s


def test_the_relay_manifests_read_only_and_writes_only_in_the_overlay() -> None:
    import yaml

    from tests.test_shared import ROOT

    docs = list(yaml.safe_load_all((ROOT / "deploy/relay/deployment.yaml").read_text()))
    pod = docs[0]["spec"]["template"]["spec"]
    (container,) = pod["containers"]
    assert container["args"] == ["relay"]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert "SQUERO_RELAY_WRITES" not in {e["name"] for e in container["env"]}
    service = docs[1]  # the cluster's Alertmanager posts to its receiver (slice 3.4)
    assert service["spec"]["selector"] == docs[0]["spec"]["selector"]["matchLabels"]
    assert [p["targetPort"] for p in service["spec"]["ports"]] == ["alerts"]
    assert container["ports"] == [{"name": "alerts", "containerPort": 8080}]
    hub = next(yaml.safe_load_all((ROOT / "deploy/base/deployment.yaml").read_text()))
    assert container["image"] == hub["spec"]["template"]["spec"]["containers"][0]["image"]
    overlay = (ROOT / "deploy/relay-actions/kustomization.yaml").read_text()
    assert "SQUERO_RELAY_WRITES" in overlay
    rbac = (ROOT / "deploy/relay/rbac.yaml").read_text()
    assert "secrets" not in rbac and "patch" not in rbac


@pytest.mark.anyio
async def test_a_refused_token_is_said_and_the_wait_stops_at_a_minute(caplog: Any) -> None:
    from websockets.datastructures import Headers
    from websockets.http11 import Response

    refused = websockets.InvalidStatus(Response(403, "Forbidden", Headers()))
    attempts = 9
    waits: list[float] = []

    async def connect(url: str, token: str, server: Any) -> None:
        raise refused

    async def sleep(seconds: float) -> None:
        waits.append(seconds)
        if len(waits) == attempts:
            raise anyio.get_cancelled_exc_class()

    with pytest.raises(anyio.get_cancelled_exc_class()):
        await relay.run(settings_of(), FastMCP("x"), connect, sleep, lambda: 0.0)
    assert waits == [1, 2, 4, 8, 16, 32, 60, 60, 60]
    assert "refused the connection (403): check the token" in caplog.text
    assert A_TOKEN not in caplog.text


@pytest.mark.anyio
async def test_a_hub_that_is_down_is_not_said_to_be_the_token(caplog: Any) -> None:
    from websockets.datastructures import Headers
    from websockets.http11 import Response

    async def connect(url: str, token: str, server: Any) -> None:
        raise websockets.InvalidStatus(Response(502, "Bad Gateway", Headers()))

    async def sleep(seconds: float) -> None:
        raise anyio.get_cancelled_exc_class()

    with pytest.raises(anyio.get_cancelled_exc_class()):
        await relay.run(settings_of(), FastMCP("x"), connect, sleep, lambda: 0.0)
    assert "refused the connection (502); dialing again" in caplog.text
    assert "check the token" not in caplog.text


# --- the Hub's side: a Run's calls through the Relay (slice 3.3a) ---------------------------


async def connected(relays: relay.Relays, named: relay.Who, server: FastMCP, tg: Any) -> Any:
    """`server` served as a Relay, held in `relays`; returns what closes its socket."""
    hub_send, hub_receive, relay_send, relay_receive = pipe()
    tg.start_soon(relay.serve, server, relay_send, relay_receive)

    async def close() -> None:
        await relay_send(None)

    tg.start_soon(relays.hold, named, hub_send, hub_receive, close)
    await until(lambda: relays.get(*named) is not None)
    return close


def local() -> dict[str, Any]:
    """The Direct cluster `test`, faked."""
    from tests.conftest import make_fake_cluster

    return {"test": make_fake_cluster()}


def with_prometheus(contract_id: str, token: str) -> contracts.Contract:
    return relayed(
        contract_id,
        token,
        integrations=[{"name": "kubernetes"}, {"name": "prometheus", "url": "http://prom"}],
    )


def text(result: Any) -> str:
    return " ".join(getattr(b, "text", "") for b in result.content)


@pytest.mark.anyio
async def test_a_relay_clusters_calls_go_through_its_relay_after_the_hubs_own_checks(
    settings: Settings, caplog: Any
) -> None:
    import httpx

    from squero import agent
    from tests.conftest import make_fake_cluster

    caplog.set_level(logging.INFO)

    def prometheus(request: httpx.Request) -> httpx.Response:
        series = [{"metric": {"job": "edge"}, "value": [0, "1"]}]
        data = {"resultType": "vector", "result": series}
        return httpx.Response(200, json={"status": "success", "data": data})

    relays, here, edge = relay.Relays(), make_fake_cluster(), make_fake_cluster()
    toolbox = agent.build_mcp(
        with_prometheus("acme", A_TOKEN), settings, k8s_apis={"test": here}, relays=relays
    )
    http = httpx.Client(base_url="http://prometheus", transport=httpx.MockTransport(prometheus))
    # The Relay allows more than the Contract does: the Hub's check is the one that holds.
    wide = settings_of(namespaces=["shop", "kube-system"], writes=True)
    async with anyio.create_task_group() as tg:
        await connected(
            relays, ("acme", "edge"), relay.relay_server(wide, {"edge": edge}, http), tg
        )

        @toolbox.mcp.tool
        def sizing_like(cluster: str) -> str:  # takes a cluster, isn't the Relay's
            return f"the Hub's own, for {cluster}"

        async with Client(toolbox.mcp) as hub_client:
            tools = {t.name: t for t in await hub_client.list_tools()}
            mine = await hub_client.call_tool("sizing_like", {"cluster": "edge"})
            pods = {"cluster": "edge", "namespace": "shop"}
            via = await hub_client.call_tool("kubernetes_list_pods", pods)
            query = await hub_client.call_tool(
                "prometheus_query", {"cluster": "edge", "promql": "up"}
            )
            barred = await hub_client.call_tool(
                "kubernetes_list_pods",
                {"cluster": "edge", "namespace": "kube-system"},
                raise_on_error=False,
            )
            local = await hub_client.call_tool(
                "kubernetes_list_pods", {"cluster": "test", "namespace": "shop"}
            )
        tg.cancel_scope.cancel()
    for name in ("kubernetes_list_pods", "prometheus_query"):
        assert tools[name].input_schema["properties"]["cluster"]["enum"] == ["edge", "test"]
    assert not [n for n in tools if n.startswith("hub_") or "evict" in n]  # the Hub's list
    assert "checkout-7d9f-abc" in text(via) and '{"job":"edge"}' in text(query)
    assert barred.is_error and "'kube-system' is not allowed in cluster 'edge'" in text(barred)
    assert "checkout-7d9f-abc" in text(local) and text(mine) == "the Hub's own, for edge"
    assert edge[0].list_namespaced_pod.call_count == 1  # kube-system never reached it
    assert here[0].list_namespaced_pod.call_count == 1
    assert caplog.text.count('"tool": "kubernetes_list_pods"') == 3  # audited, forwarded too


def hanging(entered: anyio.Event) -> FastMCP:
    """A Relay whose list_pods never answers."""
    server = FastMCP("relay")

    @server.tool
    async def kubernetes_list_pods(cluster: str, namespace: str) -> str:
        entered.set()
        await anyio.sleep_forever()
        return "never"

    return server


@pytest.mark.anyio
async def test_a_relay_offline_slow_or_gone_mid_call_is_offline(settings: Settings) -> None:
    from squero import agent

    relays = relay.Relays()
    settings.relay_call_seconds = 0.3
    toolbox = agent.build_mcp(relayed("acme", A_TOKEN), settings, k8s_apis=local(), relays=relays)
    pods = {"cluster": "edge", "namespace": "shop"}
    async with Client(toolbox.mcp) as hub_client:
        offline = await hub_client.call_tool("kubernetes_list_pods", pods, raise_on_error=False)
        async with anyio.create_task_group() as tg:
            entered = anyio.Event()
            await connected(relays, ("acme", "edge"), hanging(entered), tg)
            with anyio.fail_after(5):
                slow = await hub_client.call_tool(
                    "kubernetes_list_pods", pods, raise_on_error=False
                )
            settings.relay_call_seconds = 30
            toolbox = agent.build_mcp(
                relayed("acme", A_TOKEN), settings, k8s_apis=local(), relays=relays
            )
            entered = anyio.Event()
            close = await connected(relays, ("acme", "edge"), hanging(entered), tg)
            async with Client(toolbox.mcp) as patient:
                with anyio.fail_after(5):  # well inside the 30 s: the socket closed

                    async def kill() -> None:
                        await entered.wait()
                        await close()

                    tg.start_soon(kill)
                    gone = await patient.call_tool(
                        "kubernetes_list_pods", pods, raise_on_error=False
                    )
            tg.cancel_scope.cancel()
    assert text(offline) == "Relay offline: cluster 'edge' has no Relay connected"
    assert text(slow) == "Relay offline: cluster 'edge''s Relay didn't answer in 0.3 s"
    assert gone.is_error and text(gone).startswith("Relay offline: cluster 'edge''s Relay: ")


@pytest.mark.anyio
async def test_a_run_that_calls_an_offline_relay_fails_after_recording_the_call(
    settings: Settings,
) -> None:
    from squero import agent
    from tests.conftest import REPORT, call
    from tests.test_agent import ALERT

    recorded: list[tuple[str, bool]] = []

    async def sink(tool: str, args: Any, result: str, error: bool) -> None:
        recorded.append((tool, error))

    toolbox = agent.build_mcp(
        relayed("acme", A_TOKEN), settings, k8s_apis=local(), relays=relay.Relays()
    )
    model = scripted(
        call("kubernetes_list_pods", "c1", cluster="edge", namespace="shop"),
        call("TriageReport", "c2", **REPORT),
    )
    async with agent.open_agent(settings, toolbox, model, record=sink) as a:
        with pytest.raises(relay.RelayOffline, match="cluster 'edge' has no Relay connected"):
            await agent.start(a, ALERT, "t1")
    assert recorded == [("kubernetes_list_pods", True)]


@pytest.mark.anyio
async def test_one_contracts_relay_never_answers_anothers_cluster_of_the_same_name(
    settings: Settings,
) -> None:
    from squero import agent
    from tests.conftest import make_fake_cluster

    relays, edge = relay.Relays(), make_fake_cluster()
    bank = agent.build_mcp(relayed("bank", B_TOKEN), settings, k8s_apis=local(), relays=relays)
    async with anyio.create_task_group() as tg:
        await connected(
            relays, ("acme", "edge"), relay.relay_server(settings_of(), {"edge": edge}), tg
        )
        async with Client(bank.mcp) as hub_client:
            r = await hub_client.call_tool(
                "kubernetes_list_pods",
                {"cluster": "edge", "namespace": "shop"},
                raise_on_error=False,
            )
        tg.cancel_scope.cancel()
    assert text(r) == "Relay offline: cluster 'edge' has no Relay connected"
    assert edge[0].list_namespaced_pod.call_count == 0


@pytest.mark.anyio
async def test_a_write_needs_the_relays_own_switch_too(settings: Settings) -> None:
    from squero import agent
    from tests.conftest import make_fake_cluster

    relays, edge = relay.Relays(), make_fake_cluster()
    actions = relayed(
        "acme", A_TOKEN, integrations=[{"name": "kubernetes", "enable_actions": True}]
    )
    toolbox = agent.build_mcp(actions, settings, k8s_apis=local(), relays=relays)
    async with anyio.create_task_group() as tg:
        await connected(
            relays, ("acme", "edge"), relay.relay_server(settings_of(), {"edge": edge}), tg
        )
        async with Client(toolbox.mcp) as hub_client:
            offered = {t.name for t in await hub_client.list_tools()}
            r = await hub_client.call_tool(
                "kubernetes_scale_deployment",
                {"cluster": "edge", "namespace": "shop", "name": "checkout", "replicas": 2},
                raise_on_error=False,
            )
        tg.cancel_scope.cancel()
    assert "kubernetes_scale_deployment" in offered  # the Hub's gate is open
    assert r.is_error and "Relay offline" not in text(r)  # the Relay has no such tool
    assert edge[1].patch_namespaced_deployment_scale.call_count == 0


def changes(rows: list[dict[str, Any]] | None) -> FastMCP:
    """A Relay whose recent_changes answers `rows`, whatever the Contract allows;
    None: text, not a list."""
    server = FastMCP("relay")
    if rows is None:

        @server.tool(name="kubernetes_recent_changes")
        def text_only(cluster: str) -> str:
            return "everything"

        return server

    @server.tool
    def kubernetes_recent_changes(cluster: str) -> list[dict[str, Any]]:  # as the real one
        return rows

    return server


@pytest.mark.anyio
async def test_a_relays_recent_changes_keep_to_the_contracts_namespaces(
    settings: Settings,
) -> None:
    from squero import agent

    clusters = [
        {"name": "test", "context": "test", "namespaces": ["shop"]},
        {
            "name": "edge",
            "namespaces": ["shop"],
            "gitops_namespaces": ["flux-system"],
            "via": "relay",
            "relay_token_secret": "r",
        },
    ]
    c = relayed("acme", A_TOKEN, clusters=clusters)
    rows = [
        {"namespace": "shop", "deployment": "checkout"},
        {"namespace": "kube-system", "deployment": "coredns"},
        {"namespace": "shop, payments", "kustomization": "apps"},  # deploys beyond the Contract
        {"namespace": "flux-system", "gitops": "kustomizations", "error": "403"},
        {"namespace": "flux-system", "deployment": "source-controller"},  # not a notice
        {"deployment": "nameless"},
        {"namespace": ["shop"], "deployment": "listed"},
    ]
    pods = {"cluster": "edge"}
    relays = relay.Relays()
    toolbox = agent.build_mcp(c, settings, k8s_apis=local(), relays=relays)
    async with anyio.create_task_group() as tg:
        close = await connected(relays, ("acme", "edge"), changes(rows), tg)
        async with Client(toolbox.mcp) as hub_client:
            kept = await hub_client.call_tool("kubernetes_recent_changes", pods)
            await close()
            await until(lambda: relays.get("acme", "edge") is None)
            await connected(relays, ("acme", "edge"), changes(None), tg)
            odd = await hub_client.call_tool(
                "kubernetes_recent_changes", pods, raise_on_error=False
            )
        tg.cancel_scope.cancel()
    want = [rows[0], rows[3]]
    assert json.loads(text(kept)) == want and kept.structured_content == {"result": want}
    assert odd.is_error and text(odd) == "cluster 'edge''s Relay sent changes that aren't a list"


# --- the Hub's own reads through the Relay, and its state (slice 3.3b) ----------------------


def promql_relay(asked: list[str], fail: bool = False) -> FastMCP:
    """A Relay whose Prometheus answers one series to any query (or a 500)."""
    import httpx

    def prometheus(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.params["query"])
        if fail:
            return httpx.Response(500, json={"status": "error", "error": "down"})
        if request.url.path.endswith("query_range"):
            series = [{"metric": {"namespace": "shop"}, "values": [[0, "1"]]}]
            data = {"resultType": "matrix", "result": series}
        else:
            series = [{"metric": {"namespace": "shop"}, "value": [0, "1"]}]
            data = {"resultType": "vector", "result": series}
        return httpx.Response(200, json={"status": "success", "data": data})

    http = httpx.Client(base_url="http://prometheus", transport=httpx.MockTransport(prometheus))
    return relay.relay_server(settings_of(), {"edge": (None, None)}, http)


@pytest.mark.anyio
async def test_the_hubs_own_reads_go_through_its_relay_from_a_worker_thread() -> None:
    relays, asked = relay.Relays(), list[str]()
    mine, theirs = relay.Reader(relays, ("acme", "edge")), relay.Reader(relays, ("bank", "edge"))
    with pytest.raises(usage.UsageError, match="Relay offline: cluster 'edge' has no Relay"):
        await anyio.to_thread.run_sync(mine.instant, "up", 1.0)
    async with anyio.create_task_group() as tg:
        close = await connected(relays, ("acme", "edge"), promql_relay(asked), tg)
        rows = await anyio.to_thread.run_sync(mine.instant, "up", 1.0)
        ranged = await anyio.to_thread.run_sync(mine.range, "up", 0.0, 60.0, 60)
        with pytest.raises(usage.UsageError, match="has no Relay connected"):
            await anyio.to_thread.run_sync(theirs.instant, "up", 1.0)  # A's Relay never B's
        await close()
        await until(lambda: relays.get("acme", "edge") is None)
        close = await connected(relays, ("acme", "edge"), promql_relay(asked, fail=True), tg)
        with pytest.raises(usage.UsageError, match=r"through cluster 'edge''s Relay: .*500"):
            await anyio.to_thread.run_sync(mine.instant, "up", 1.0)
        await close()
        await until(lambda: relays.get("acme", "edge") is None)
        await connected(relays, ("acme", "edge"), FastMCP("bare"), tg)
        with pytest.raises(usage.UsageError, match=r"through cluster 'edge''s Relay: .*hub_promql"):
            await anyio.to_thread.run_sync(mine.instant, "up", 1.0)  # none on this Relay
        tg.cancel_scope.cancel()
    assert rows == [{"metric": {"namespace": "shop"}, "value": [0, "1"]}]
    assert ranged == [{"metric": {"namespace": "shop"}, "values": [[0, "1"]]}]
    assert asked == ["up", "up", "up"]


@pytest.mark.anyio
async def test_a_relay_that_answers_no_series_is_an_error() -> None:
    relays = relay.Relays()
    odd = FastMCP("odd")

    @odd.tool
    def hub_promql_instant(promql: str, at: float) -> list[Any]:
        return ["not a series"]

    async with anyio.create_task_group() as tg:
        await connected(relays, ("acme", "edge"), odd, tg)
        with pytest.raises(usage.UsageError, match="something else than series"):
            reader = relay.Reader(relays, ("acme", "edge"))
            await anyio.to_thread.run_sync(reader.instant, "up", 1.0)
        tg.cancel_scope.cancel()


def relay_only(contract_id: str, token: str) -> contracts.Contract:
    """One cluster, `edge`, through its Relay, with Prometheus."""
    data = {
        "id": contract_id,
        "name": contract_id,
        "clusters": [
            {"name": "edge", "namespaces": ["shop"], "via": "relay", "relay_token_secret": "r"}
        ],
        "integrations": [{"name": "kubernetes"}, {"name": "prometheus", "url": "http://prom"}],
    }
    return contracts.build(data, secrets={"r": SecretStr(token)})


@pytest.mark.anyio
async def test_hourly_usage_reads_a_relay_cluster_through_the_hubs_relays() -> None:
    asked: list[str] = []
    c = relay_only("acme", A_TOKEN)
    offline = await anyio.to_thread.run_sync(usage.collect, c, [3600], 7200.0)
    async with anyio.create_task_group() as tg:
        await connected(relay.HUB, ("acme", "edge"), promql_relay(asked), tg)
        read = await anyio.to_thread.run_sync(usage.collect, c, [3600], 7200.0)
        tg.cancel_scope.cancel()
    await until(lambda: relay.HUB.get("acme", "edge") is None)
    assert offline.clusters["edge"].startswith("Relay offline: cluster 'edge'")  # type: ignore[union-attr]
    assert read.clusters == {"edge": None} and asked  # read, through the Relay
    probe = 'count(kube_pod_owner{exported_namespace!=""})'  # how ksm is scraped, as Direct
    wide = [q for q in asked if q != probe and 'namespace=~"shop"' not in q]
    # its namespaces only, but for a node's Room (slice 15.1a): node series, which carry
    # none, and pods outside them only as Prometheus sums and counts them per node
    per_node = ("kube_node_", "sum by (node, exported_node", "count by (node, exported_node")
    assert wide and all(q.startswith(per_node) for q in wide)


@pytest.mark.anyio
async def test_a_relays_state_says_since_when_and_its_version() -> None:
    times = iter([1700000000.0, 1700000600.0, 1700003600.0])
    relays = relay.Relays(clock=lambda: next(times))
    never = relays.state("acme", "edge")
    async with anyio.create_task_group() as tg:
        close = await connected(relays, ("acme", "edge"), promql_relay([]), tg)
        on = relays.state("acme", "edge")
        await close()
        await until(lambda: relays.get("acme", "edge") is None)
        tg.cancel_scope.cancel()
    off = relays.state("acme", "edge")
    version = importlib.metadata.version("squero")
    assert never == (
        False,
        1700000000.0,
        "not connected since the Hub started at 2023-11-14 22:13 UTC",
    )
    assert on == (True, 1700000600.0, f"connected since 2023-11-14 22:23 UTC, version {version}")
    assert off == (False, 1700003600.0, "offline since 2023-11-14 23:13 UTC")
    assert relays.state("bank", "edge")[0] is False  # another Contract's cluster of that name


def test_health_shows_each_relay_and_probes_through_it(
    settings: Settings, database_url: str
) -> None:
    from fastapi.testclient import TestClient

    from squero import agent
    from tests.test_console import OPERATOR, VIEW

    settings.database_url = SecretStr(database_url)
    settings.console_operators = {OPERATOR}
    settings.approver_token = SecretStr("approver-token")
    by_id = {c.id: c for c in (relayed("acme", A_TOKEN), relayed("bank", B_TOKEN))}

    def mcp(c: contracts.Contract) -> agent.Toolbox:
        return agent.build_mcp(c, settings, k8s_apis=local(), relays=relay.HUB)

    async def close() -> None:
        pass

    up = relay.Live(None, close, 1700000600.0, None, "0.31.0")  # type: ignore[arg-type]
    relay.HUB.live[("acme", "edge")] = up
    try:
        with TestClient(create_app(settings, scripted(), mcp, by_id)) as c:
            acme = c.get("/api/v1/contracts/acme/health", headers=VIEW).json()["integrations"]
            bank = c.get("/api/v1/contracts/bank/health", headers=VIEW).json()["integrations"]
    finally:
        del relay.HUB.live[("acme", "edge")]
    rows = {p["name"]: p for p in acme}
    assert rows["relay edge"] == {
        "name": "relay edge",
        "ok": True,
        "ms": 0,
        "detail": "connected since 2023-11-14 22:23 UTC, version 0.31.0",
    }
    assert rows["kubernetes test"]["ok"] is True
    theirs = {p["name"]: p for p in bank}
    assert theirs["relay edge"]["ok"] is False  # A's Relay is never B's
    assert theirs["relay edge"]["detail"].startswith("not connected since the Hub started at")
    assert theirs["kubernetes edge"]["detail"].startswith("Relay offline: cluster 'edge' has no")


@pytest.mark.parametrize(
    ("path", "account"),
    [
        ("deploy/gitops/rbac-gitops.yaml", ("squero", "squero")),
        ("deploy/relay/rbac-gitops.yaml", ("squero-relay", "squero-relay")),
    ],
)
def test_gitops_rbac_grants_every_kind_recent_changes_reads_in_each_namespace(
    path: str, account: tuple[str, str]
) -> None:
    """recent_changes asks every GitOps kind in every GitOps namespace and reports a
    403 as a missing grant: a namespace granted only some kinds reads as misconfigured."""
    import yaml

    from tests.test_shared import ROOT

    docs = list(yaml.safe_load_all((ROOT / path).read_text()))
    subjects = [s for d in docs if d["kind"] == "RoleBinding" for s in d["subjects"]]
    assert subjects and all((s["name"], s["namespace"]) == account for s in subjects)
    wanted = {(group, plural) for group, _, plural in kubernetes.GITOPS}
    roles = [d for d in docs if d["kind"] == "Role"]
    assert {r["metadata"]["namespace"] for r in roles} == {"argocd", "flux-system"}
    for role in roles:
        granted = {
            (g, r) for rule in role["rules"] for g in rule["apiGroups"] for r in rule["resources"]
        }
        verbs = {v for rule in role["rules"] for v in rule["verbs"]}
        assert granted == wanted and verbs == {"get", "list"}, role["metadata"]["namespace"]
    kustomization = (ROOT / path).parent / "kustomization.yaml"  # opt-in: never in a base
    assert not kustomization.exists() or "rbac-gitops" not in kustomization.read_text()


# --- alerts through the Relay (slice 3.4) ----------------------------------------------

GROUP = {
    "status": "firing",
    "groupKey": "g1",
    "commonLabels": {"alertname": "KubePodCrashLooping", "namespace": "shop"},
    "alerts": [{"status": "firing", "labels": {"pod": "orders-7d9f-abc"}, "fingerprint": "f1"}],
}


async def relay_side(uplink: relay.Uplink, send: Any, receive: Any) -> None:
    """A Relay's end of a socket, as `dial` runs it."""
    send = relay.locked(send)
    with uplink.over(send):
        await relay.serve(tools_server(), send, receive, uplink.answered)


async def with_alerts(alerts: Any, tg: Any) -> tuple[relay.Relays, relay.Uplink, Any, Any]:
    """A Relay held by the Hub, `alerts` taking what it forwards; returns what closes
    the socket under the Relay, what sends the Hub a raw frame, and when `hold` ended."""
    relays, uplink = relay.Relays(), relay.Uplink()
    hub_send, hub_receive, relay_send, relay_receive = pipe()
    tg.start_soon(relay_side, uplink, relay_send, relay_receive)
    held: list[bool] = []

    async def close() -> None:
        await relay_send(None)

    async def hold() -> None:
        await relays.hold(("acme", "edge"), hub_send, hub_receive, close, alerts)
        held.append(True)

    tg.start_soon(hold)
    await until(lambda: relays.get("acme", "edge") is not None)

    async def cut() -> None:
        await hub_send(None)
        await relay_send(None)

    return relays, uplink, cut, (relay_send, held)


@pytest.mark.anyio
async def test_an_alert_rides_the_relays_socket_and_the_hubs_answer_comes_back() -> None:
    got: list[Any] = []

    async def alerts(named: relay.Who, params: Any) -> tuple[int, Any]:
        got.append((named, params))
        return 202, {"status": "accepted"}

    async with anyio.create_task_group() as tg:
        relays, uplink, _, (raw, _) = await with_alerts(alerts, tg)
        assert await uplink.forward("hook", GROUP) == (202, {"status": "accepted"})
        await raw('{"jsonrpc": "2.0", "id": 7, "method": "squero/other", "params": {}}')
        r = await relays.get("acme", "edge").call_tool(  # type: ignore[union-attr]
            "list_pods", {"namespace": "shop"}
        )
        assert r.content[0].text == "pods in shop"  # type: ignore[union-attr]
        tg.cancel_scope.cancel()
    assert got == [(("acme", "edge"), {"token": "hook", "group": GROUP})]
    assert uplink.waiting == {}


@pytest.mark.anyio
async def test_an_alert_the_hub_doesnt_take_is_a_5xx_alertmanager_sends_again(
    monkeypatch: Any,
) -> None:
    assert await relay.Uplink().forward("hook", GROUP) == (
        503,
        {"detail": "the Relay has no connection to the Hub"},
    )
    answers: list[Any] = [RuntimeError("store down"), (999, {}), (401, {"detail": "no"})]
    stuck = anyio.Event()

    async def alerts(named: relay.Who, params: Any) -> tuple[int, Any]:
        if not answers:
            await stuck.wait()  # never set: a Hub that doesn't answer
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer  # type: ignore[no-any-return]

    async with anyio.create_task_group() as tg:
        relays, uplink, cut, (_, held) = await with_alerts(alerts, tg)
        assert await uplink.forward("hook", GROUP) == (502, {"detail": "the Hub failed to take it"})
        assert await uplink.forward("hook", GROUP) == (
            502,
            {"detail": "the Hub answered something else"},
        )
        assert await uplink.forward("hook", GROUP) == (401, {"detail": "no"})  # passed on
        monkeypatch.setattr(relay, "ALERT_SECONDS", 0.1)
        assert await uplink.forward("hook", GROUP) == (
            503,
            {"detail": "the Hub didn't answer in 0.1 s"},
        )
        r = await relays.get("acme", "edge").call_tool(  # type: ignore[union-attr]
            "list_pods", {"namespace": "shop"}
        )
        assert r.content[0].text == "pods in shop"  # a failed alert ends nothing
        monkeypatch.setattr(relay, "ALERT_SECONDS", 10)
        async with anyio.create_task_group() as waiting:
            outcome: list[Any] = []

            async def forward() -> None:
                outcome.append(await uplink.forward("hook", GROUP))

            waiting.start_soon(forward)
            await until(lambda: bool(uplink.waiting))
            await until(lambda: not answers)  # the Hub holds it, stuck
            await cut()  # the socket closes under it
        assert outcome == [(503, {"detail": "no connection to the Hub: ConnectionError"})]
        assert uplink.send is None and uplink.waiting == {}
        await until(lambda: bool(held))  # the stuck alert didn't keep the connection
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_the_relays_receiver_wants_a_bearer_and_an_alert_group(monkeypatch: Any) -> None:
    import httpx

    from squero.models import AlertGroup

    uplink = relay.Uplink()
    sent: list[Any] = []

    async def forward(token: str, group: Any) -> tuple[int, Any]:
        sent.append((token, group))
        return 202, {"status": "accepted"}

    monkeypatch.setattr(uplink, "forward", forward)
    monkeypatch.setattr(relay, "MAX_ALERT", 2000)
    app = relay.webhook(uplink)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://r") as c:
        url, hook = "/alertmanager", {"Authorization": "Bearer hook"}
        assert (await c.post(url, json=GROUP)).status_code == 401
        assert (
            await c.post(url, json=GROUP, headers={"Authorization": "Basic x"})
        ).status_code == 401
        assert (await c.post(url, json={"status": 1}, headers=hook)).status_code == 422
        assert (await c.post(url, content=b"{", headers=hook)).status_code == 422
        big = {**GROUP, "commonLabels": {"x": "y" * 2000}}
        assert (await c.post(url, json=big, headers=hook)).status_code == 413
        assert sent == []  # none of those reached the Hub
        r = await c.post(url, json=GROUP, headers=hook)
        assert (r.status_code, r.json()) == (202, {"status": "accepted"})
        assert sent == [("hook", AlertGroup.model_validate(GROUP).model_dump(mode="json"))]
        assert (await c.get(url)).status_code == 405
    app = relay.webhook(relay.Uplink())  # not connected
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://r") as c:
        assert (await c.post(url, json=GROUP, headers=hook)).status_code == 503


@pytest.mark.anyio
async def test_a_relays_alert_is_its_own_contracts_with_that_contracts_webhook_token(
    settings: Settings, database_url: str
) -> None:
    import httpx

    hooks = {"acme": "h" * 40, "bank": "i" * 40}
    acme, bank = (
        relayed(cid, token, webhook_token_sha256=contracts.token_hash(hooks[cid]))
        for cid, token in (("acme", A_TOKEN), ("bank", B_TOKEN))
    )
    app = hub(settings, database_url, acme, bank)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    base = f"127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    relays: relay.Relays = app.state.relays
    uplink = relay.Uplink()
    async with anyio.create_task_group() as tg:
        tg.start_soon(lambda: server.serve(sockets=[sock]))
        await until(lambda: server.started)
        tg.start_soon(relay.dial, f"ws://{base}/relay", A_TOKEN, tools_server(), uplink)
        await until(lambda: relays.get("acme", "edge") is not None)
        assert await uplink.forward(hooks["bank"], GROUP) == (401, {"detail": "invalid token"})
        assert await uplink.forward(A_TOKEN, GROUP) == (401, {"detail": "invalid token"})
        status, body = await uplink.forward(hooks["acme"], GROUP)
        assert (status, body["status"]) == (202, "accepted")
        again = await uplink.forward(hooks["acme"], GROUP)  # Alertmanager's repeat
        assert again == (202, {"status": "duplicate", "investigation": body["investigation"]})
        bad = await uplink.forward(hooks["acme"], {"groupKey": 1})
        assert bad == (422, {"detail": "not an Alertmanager alert group"})
        async with httpx.AsyncClient(base_url=f"http://{base}") as c:
            r = await c.get("/investigations", headers={"Authorization": "Bearer approver-token"})
        stored = [(i["id"], i["contract_id"]) for i in r.json()]
        assert stored == [(body["investigation"], "acme")]  # bank has none
        server.should_exit = True
        tg.cancel_scope.cancel()


def test_the_relays_settings_survive_its_own_services_links(monkeypatch: Any) -> None:
    """Kubernetes gives each pod environment variables for every Service in its
    namespace: for the Relay's own Service (`squero-relay`) they share its prefix, and
    v0.32.0 read SQUERO_RELAY_PORT=tcp://... as its port and failed to start."""
    import yaml

    from tests.test_shared import ROOT

    docs = list(yaml.safe_load_all((ROOT / "deploy/relay/deployment.yaml").read_text()))
    service = next(d for d in docs if d["kind"] == "Service")
    name = service["metadata"]["name"].upper().replace("-", "_")
    port = service["spec"]["ports"][0]["port"]
    links = {
        f"{name}_SERVICE_HOST": "10.96.0.1",
        f"{name}_SERVICE_PORT": str(port),
        f"{name}_SERVICE_PORT_ALERTS": str(port),
        f"{name}_PORT": f"tcp://10.96.0.1:{port}",
        f"{name}_PORT_{port}_TCP": f"tcp://10.96.0.1:{port}",
        f"{name}_PORT_{port}_TCP_PROTO": "tcp",
        f"{name}_PORT_{port}_TCP_PORT": str(port),
        f"{name}_PORT_{port}_TCP_ADDR": "10.96.0.1",
    }
    for k, v in links.items():
        monkeypatch.setenv(k, v)
    assert settings_of().alerts_port == 8080
