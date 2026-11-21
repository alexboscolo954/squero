"""The Hub MCP server (slice 11.1a): the Operator's own MCP endpoint at /mcp/, for an MCP
client such as Claude Code on the Operator's devices.

Two doors, both required: the bearer SQUERO_MCP_TOKEN, and a listed Operator's Tailscale
identity, which `tailscale serve` adds to what it proxies (and strips from what a client
sends). The token alone could come from anything that read the Hub's env; the identity
header alone from anything else that reaches the port. Stateless HTTP: a Hub restart
(every contracts update) leaves no session for a client to lose.

Every tool takes one Contract and answers within it. What it returns reaches the
client's model, so it follows that Contract's Data policy as the Hub's own agent does:
secrets redacted, identifiers masked with the Contract's own key, error text included.
A placeholder in an argument becomes its value again only when this server handed it
out for that same Contract, the `contract` argument included (a client's id is likely
one of its own terms).

The Investigation agent never gets these tools: they live here, not in servers/.
"""

import secrets
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.middleware import MiddlewareContext
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from squero import agenda, billing, masking, proposals, usage, webapp
from squero.console import QUESTION_LIMIT
from squero.contracts import Contract
from squero.models import Investigation
from squero.observability import AuditMiddleware
from squero.servers.sizing import allowed, workload
from squero.sizing import by_type, changes, floor_text, group_lines, profile_lines, show
from squero.store import Store

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
# a Question changes nothing in a cluster, but it starts a run that costs model money
ASK = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)
# approving runs the agent's Proposed actions on the client's systems, or (a Sizing)
# lets the Actuator change them
DECIDE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True)
# a pull request changes nothing until the Operator merges it
CHANGE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)
STOP = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True)
CONTRACT = Field(description="The Contract's id, as list_contracts gives it")
LOGIN = "tailscale-user-login"
KILLED = "The Contract's Kill switch is on: approving is refused. Turn it off in the console first."
KEYS = ("cluster", "namespace", "kind", "name", "container")
RES = ("cpu", "memory")


def guard(app: ASGIApp, token: str, operators: set[str]) -> ASGIApp:
    """The MCP app behind both doors; lifespan events pass through."""
    expected = f"Bearer {token}".encode()

    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = Headers(scope=scope)
            if not secrets.compare_digest(headers.get("authorization", "").encode(), expected):
                await JSONResponse({"error": "invalid token"}, 401)(scope, receive, send)
                return
            if headers.get(LOGIN, "").strip() not in operators:
                error = "not an Operator (connect through tailscale serve)"
                await JSONResponse({"error": error}, 403)(scope, receive, send)
                return
        await app(scope, receive, send)

    return asgi


class Audit(AuditMiddleware):
    """The agent's audit line, saying it came through the Hub MCP server, from whom,
    about which Contract (as the client wrote it: masked arguments stay masked)."""

    def who(self, context: MiddlewareContext[Any]) -> dict[str, Any]:
        contract = (context.message.arguments or {}).get("contract")
        return {"channel": "mcp", "login": _login(), "contract": contract}


def _date(epoch: float | None) -> str | None:
    return (
        None if epoch is None else datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%d %H:%M UTC")
    )


def _resource(res: str, r: webapp.Resource | None) -> dict[str, Any] | None:
    if r is None:
        return None
    now = None if r.current is None else show(res, r.current)
    out = {"request": now, "sizing": show(res, r.target), "action": r.action}
    if r.held:  # slice 14.3
        out["held"] = r.held.change or f"by its HPA {r.held.hpa}"
    return out


MAX_TYPES = 10  # of a DaemonSet's instance types given to the model; the rest counted


def _by_type(c: webapp.Container) -> dict[str, Any]:
    """A DaemonSet container's Sizings per instance type (slice 15.3a), as lines, or
    nothing: the busiest types first would need usage, so by name, capped. `applied`: what
    its Actuator applies, its pooled Sizing then not (slice 15.3c)."""
    if not c.groups:
        return {}
    lines = group_lines({"groups": [g.model_dump() for g in c.groups[:MAX_TYPES]]})
    applied = "by_instance_type" if c.by_type else "the one Sizing, to every pod"
    more = max(0, len(c.groups) - MAX_TYPES)
    return {"by_instance_type": lines, "more_types": more, "applied": applied}


MAX_BLOCKERS = 5  # a node's Blockers given to the model, one per Workload; the rest counted
MAX_PODS = 3  # of one Blocker's pods named
ORDER = {"blocked": 0, "unknown": 1, "clear": 2, "pinned": 3}


def _blockers(found: list[webapp.BlockingPod]) -> list[dict[str, Any]]:
    """A node's blocking pods, one entry for the pods of a Workload that block for the
    same reasons (its replicas would repeat one change), each with the change that
    removes it (slice 15.6): what to change, and a manifest where there is one."""
    grouped: dict[tuple[str, tuple[str, ...]], list[webapp.BlockingPod]] = {}
    for b in found:
        whose = f"{b.namespace}/{b.workload}" if b.workload else f"{b.namespace}/{b.pod}"
        grouped.setdefault((whose, tuple(b.reasons)), []).append(b)
    out = []
    for (whose, said), pods in grouped.items():
        names = [f"{b.namespace}/{b.pod}" for b in pods]
        entry: dict[str, Any] = {"of": whose, "pods": names[:MAX_PODS]}
        entry |= {"more_pods": max(0, len(names) - MAX_PODS), "reasons": [s[:300] for s in said]}
        entry["changes"] = [{"change": c.change, "manifest": c.manifest} for c in pods[0].changes]
        if pods[0].placement:  # slice 15.2a
            entry["placed_by"] = pods[0].placement
            notes = sorted({b.note for b in pods if b.note})
            entry |= {"note": notes[0]} if notes else {}
        out.append(entry)
    return out


MAX_PLACED = 20  # of a cluster's placed Workloads named; the rest counted (slice 15.2a)


def _restart(r: webapp.Restart) -> dict[str, Any]:
    """A node's restart advice, its Workloads capped as a Blocker's pods are."""
    said = r.workloads[:MAX_BLOCKERS]
    more = max(0, len(r.workloads) - MAX_BLOCKERS)
    return {"workloads": said, "more_workloads": more, "why_not": (r.why_not or "")[:300] or None}


def _placement(p: webapp.PlacementShown) -> dict[str, Any]:
    """A cluster's Placement, its placed Workloads capped."""
    more = max(0, len(p.workloads) - MAX_PLACED)
    return p.model_dump() | {"workloads": p.workloads[:MAX_PLACED], "more_workloads": more}


def _spotted(s: webapp.SpotWorkloads) -> dict[str, Any]:
    """A cluster's Workloads under a Spot ratio, capped as placed ones are (slice 16.1c)."""
    more = max(0, len(s.workloads) - MAX_PLACED)
    return s.model_dump() | {
        "workloads": [w.model_dump() for w in s.workloads[:MAX_PLACED]],
        "more_workloads": more,
    }


def _nodes(cl: webapp.ClusterNodes, top: int) -> dict[str, Any]:
    """A cluster's nodes for the `nodes` tool (slice 15.4): blocked first, `top` of them,
    each with its Room and its verdict. The pods' names and the reasons came from the
    cluster's Actuator: capped here, redacted and masked with the rest on the way out."""
    listed = sorted(cl.nodes, key=lambda n: (ORDER[n.verdict.state], n.node))
    nodes = []
    for n in listed[:top]:
        blockers = _blockers(n.verdict.blockers)
        nodes.append(
            {
                **{"node": n.node, "type": n.type, "taints": n.taints, "cordoned": n.cordoned},
                **{"spot": n.spot},
                **{"room": n.room, "pods": n.pods, "own_pods": n.mine.pods},
                **{"not_visible_pods": n.others.pods, "could_be_removed": n.verdict.state},
                **{"why": n.verdict.why, "blockers": blockers[:MAX_BLOCKERS]},
                **{"more_blockers": max(0, len(blockers) - MAX_BLOCKERS)},
            }
            | ({"restart": _restart(n.verdict.restart)} if n.verdict.restart else {})
            | ({"gpus": n.gpus.model_dump()} if n.gpus else {})  # slice 17.5b
        )
    went = cl.could_go.model_dump(mode="json") if cl.could_go else None
    return {
        **{"cluster": cl.cluster, "as_of": _date(cl.at), "problem": cl.problem},
        **{"autoscaler": cl.autoscaler, "rules": cl.rules, "reported": _date(cl.reported)},
        **{"autoscaler_settings_to_check": cl.settings},
        **{"judged": cl.judged, "could_go": went, "largest_room": cl.largest},
        **{"placement": _placement(cl.placement) if cl.placement else None},
        **{"spot": cl.spot.model_dump() if cl.spot else None},
        **{"spot_candidates": cl.candidates.model_dump() if cl.candidates else None},
        **{"spot_workloads": _spotted(cl.spot_workloads) if cl.spot_workloads else None},
        **{"nodes": nodes, "more_nodes": max(0, len(listed) - top)},
    } | ({"unrequested_gpus": cl.unrequested_gpus.model_dump()} if cl.unrequested_gpus else {})


def _workload(w: webapp.Workload, approved: dict[str, Any]) -> dict[str, Any]:
    """A ranked Workload as the client's model reads it: quantities as 115m and 96Mi,
    and who approved a container's Sizing, when one is."""
    return {
        "workload": w.workload,
        "pods": w.pods,
        "waste": {"cpu": show("cpu", w.waste.cpu), "memory": show("memory", w.waste.memory)},
        "cost": None if w.cost is None else w.cost.model_dump(),
        "risks": [r.text for r in w.risks],
        "containers": [
            {
                "container": c.container,
                "cpu": _resource("cpu", c.cpu),
                "memory": _resource("memory", c.memory),
            }
            | ({"approved": approved[c.workload]} if c.workload in approved else {})
            | _by_type(c)
            for c in w.containers
        ],
    } | ({"idle_gpus": w.gpus.text} if w.gpus else {})  # slice 17.2b


def build(
    by_id: dict[str, Contract],
    store: Callable[[], Store],
    now: Callable[[], float],
    asked: Callable[[str, str], Awaitable[str | None]],
    decided: Callable[[Investigation, float, bool, str, str], Awaitable[str | None]],
    stopped: Callable[[str, str], Awaitable[bool]],
    proposed: Callable[[Contract, list[proposals.Edit], str, str, str], Awaitable[str]],
) -> FastMCP:
    """asked(contract id, text): a new Question's id, as the console asks it.
    decided(Investigation, its pause's deadline, approve, reason, login): None when
    decided, else why not. stopped(contract id, by): turns its Kill switch on, False
    when it already was. Nothing here turns a Kill switch off: only the console does.
    proposed(Contract, edits, title, why, login): a pull request's URL, or
    proposals.Refused."""
    maskers = {cid: masking.for_contract(c) for cid, c in by_id.items()}
    # ponytail: in memory, so a restart forgets what was handed out (the client lists
    # again); grows with the distinct identifiers shown, which a Contract bounds
    seen: dict[str, masking.Seen] = {cid: {} for cid in by_id}
    mcp = FastMCP(
        "squero-hub",
        instructions="The Hub's stored view of each Contract. Every tool takes one Contract: "
        "list_contracts names them.",
        mask_error_details=True,  # an unexpected error's text could carry client data
    )
    mcp.add_middleware(Audit(None))

    def out(c: Contract, value: Any) -> Any:
        return masking._walk(value, lambda s: masking.outgoing(maskers[c.id], s, seen[c.id]))

    def arg(c: Contract, value: str) -> str:
        return masking.unmask(value, seen[c.id])

    def resolve(value: str) -> Contract:
        for c in by_id.values():
            if arg(c, value) == c.id:
                return c
        raise ToolError("unknown Contract: list_contracts names them")

    async def approvals(c: Contract) -> dict[str, Any]:
        """The Contract's current Approved Sizings, by the container's full name."""
        return {
            "/".join(a[k] for k in KEYS): {"by": a["approved_by"], "at": _date(a["approved_at"])}
            for a in await store().approved_sizings(c.id)
        }

    async def answer(c: Contract, work: Awaitable[Any]) -> Any:
        try:
            return out(c, await work)
        except ToolError as e:
            raise ToolError(out(c, str(e))) from None

    @mcp.tool(annotations=READ)
    async def list_contracts() -> list[dict[str, Any]]:
        """Every Contract the Hub serves: its id (pass it as `contract` to the other
        tools), name, clusters, and whether its Kill switch is on."""
        listed = []
        for c in sorted(by_id.values(), key=lambda c: c.id):
            on = await store().kill_switch(c.id) is not None
            clusters = [t.cluster for t in usage.targets(c)]
            listed.append(
                out(c, {"id": c.id, "name": c.name, "clusters": clusters, "kill_switch": on})
            )
        return listed

    @mcp.tool(annotations=READ)
    async def sizings(
        contract: Annotated[str, CONTRACT],
        top: Annotated[int, Field(ge=1, le=100)] = 10,
        cluster: str | None = None,
        namespace: str | None = None,
        risky: bool = False,
    ) -> dict[str, Any]:
        """The Contract's Workloads ranked by what their waste costs (by the waste
        itself when the Contract has no prices), from the Hub's latest Sizing run: each
        one's requests now and its Sizing per container, its waste and risks. held: a
        request its HPA divides by, which the Actuator leaves alone, with the manifest
        change that frees it. by_instance_type: a DaemonSet's container sized for its
        pods on nodes of each instance type, beside its one Sizing for all of them,
        which is the one applied. floors: for each Workload with an HPA (or a vLLM
        Workload without one, against its replica count: its saving needs an autoscaler
        holding the Floor), the minimum replicas its stored Needs support, against the
        HPA's minimum today, their GPUs priced with them, and its
        Weekly profile (blocks of hours that need more, KEDA cron triggers) with its
        Backtest: how far off it was last week, or that the load isn't weekly enough
        for one. idle_gpus: GPUs whose busiest hour stays under 5% busy, each counted
        by the share of the window it was held, with their monthly cost beside the
        waste (never in it) and the GPUs not judged (shared, unread, learning).
        risky: only Workloads with a risk (an OOM kill, throttling, restarts)."""
        c = resolve(contract)

        async def work() -> dict[str, Any]:
            names = [t.cluster for t in usage.targets(c)]
            wanted = None if cluster is None else arg(c, cluster)
            if wanted is not None and wanted not in names:
                raise ToolError(f"cluster {wanted!r} has no Sizings: one of {names}")
            space = None if namespace is None else arg(c, namespace)
            w = await webapp.workloads_of(store(), c, now())
            approved = await approvals(c)
            found = [
                x
                for x in w.workloads
                if wanted in (None, x.cluster)
                and space in (None, x.namespace)
                and (x.risks or not risky)
            ]
            return {
                "currency": w.currency,
                # a list, not a dict: masking leaves keys alone, and a cluster's name
                # can be the client's
                "clusters": [
                    {
                        "cluster": k,
                        "as_of": _date(s.at),
                        "failing": s.error,
                        "since": _date(s.since),
                    }
                    for k, s in w.clusters.items()
                ],
                "matching": len(found),
                "workloads": [_workload(x, approved) for x in found[:top]],
                # slice 14.1b: each autoscaled Workload's Floor against its HPA's minimum
                "floors": [
                    "\n".join([floor_text(shown, w.currency), *profile_lines(shown, w.currency)])
                    for f in w.floors
                    for shown in [f.model_dump(exclude_none=True)]
                    if wanted in (None, f.cluster) and space in (None, f.namespace)
                ],
                "learning": len(w.learning),
                "excluded": len(w.excluded),
            } | ({"idle_gpus": w.gpus.text} if w.gpus else {})  # slice 17.2b

        result: dict[str, Any] = await answer(c, work())
        return result

    @mcp.tool(annotations=READ)
    async def sizing(
        contract: Annotated[str, CONTRACT],
        cluster: str,
        namespace: str,
        name: str,
        kind: str | None = None,
    ) -> dict[str, Any]:
        """One Workload's Sizing, per container: requests and limits now, what they
        should be (CPU from its weighted p95, memory from its peak, with headroom), its
        risks and waste, and who approved it when it is. name: the Workload or one of
        its pods. kind (Deployment, StatefulSet, ...) only when two Workloads share a
        name."""
        c = resolve(contract)

        async def work() -> dict[str, Any]:
            where = arg(c, cluster), arg(c, namespace), arg(c, name)
            found = await workload(store(), c, *where, kind)
            approved = await approvals(c)
            for w in found["workloads"]:
                for k in w["containers"]:
                    if (key := f"{w['workload']}/{k['container']}") in approved:
                        k["approved"] = approved[key]
            return found

        result: dict[str, Any] = await answer(c, work())
        return result

    @mcp.tool(annotations=DECIDE)
    async def approve_sizing(
        contract: Annotated[str, CONTRACT],
        cluster: str,
        namespace: str,
        name: str,
        container: str,
        cpu: str | None,
        memory: str | None,
        kind: str | None = None,
    ) -> dict[str, Any]:
        """Approve one container's latest Sizing, for the Actuator to apply (Phase 12):
        nothing changes in a cluster before then. name: the Workload, as `sizings`
        shows it. cpu and memory: its Sizing exactly as `sizing` or `sizings` showed it
        (115m, 96Mi; null where none was shown), so a newer Sizing is never approved
        unseen. Refused while it is learning, when it changes nothing, and while the
        Contract's Kill switch is on. A later Sizing of the container supersedes it. A
        DaemonSet's Sizings by instance type are approved with it, as `sizings` showed
        them (its Actuator gives each pod its node's type's, a pod on a type still
        learning its template's requests), so never one with more types than shown."""
        c = resolve(contract)

        async def work() -> dict[str, Any]:
            by = f"mcp:{_operator()}"
            given = zip(KEYS, (cluster, namespace, kind, name, container), strict=True)
            want = {k: arg(c, v) for k, v in given if v is not None}
            allowed(c, want["cluster"], want["namespace"])
            rows, status = await store().latest_sizings(c.id, {want["cluster"]})
            found = [r for r in rows if all(r[k] == v for k, v in want.items())]
            if not found:
                raise ToolError("no stored Sizing of that container: `sizing` lists them")
            if len(found) > 1:
                raise ToolError("two Workloads share that name: pass kind")
            (r,) = found
            if r["state"] != "ready":
                raise ToolError(f"it is {r['state']}: it has no Sizing to approve")
            shown = {res: show(res, r[res]["target"]) if r.get(res) else None for res in RES}
            if shown != {"cpu": cpu, "memory": memory}:
                raise ToolError(
                    f"its latest Sizing is cpu {shown['cpu']}, memory {shown['memory']}: "
                    "read it again"
                )
            types = by_type(r)
            kept = all(r[res]["action"] == "keep" for res in RES if r.get(res))
            if r.get("by_type") and not changes(None, None, types):  # slice 15.3c
                raise ToolError(
                    "its Sizings by instance type, which its Actuator applies, keep the "
                    "requests as they are: nothing to approve"
                )
            if kept and not changes(None, None, types):
                raise ToolError("its Sizing keeps the requests as they are: nothing to approve")
            if len(r.get("groups") or []) > MAX_TYPES:  # never approved unseen
                raise ToolError(
                    f"it is sized for {len(r['groups'])} instance types, more than the "
                    f"{MAX_TYPES} shown here: not approved unseen"
                )
            run = status[want["cluster"]]["run"]
            done = await store().approve_sizing(c.id, run, r, by, now(), types)
            if done == "killed":
                raise ToolError(KILLED)
            return {
                "workload": r["workload"],
                "approved": "approved" if done == "approved" else "already approved",
                **shown,
                **({"by_instance_type": group_lines(r)} if types else {}),
            }

        result: dict[str, Any] = await answer(c, work())
        return result

    @mcp.tool(annotations=DECIDE)
    async def revert_sizing(
        contract: Annotated[str, CONTRACT],
        cluster: str,
        namespace: str,
        name: str,
        container: str,
        kind: str = "Deployment",
    ) -> dict[str, Any]:
        """Revert what the Actuator holds for one container (Phase 12): within a minute
        its pods go back to their template's requests, in place, and the Actuator stops
        holding it. Automation of that container stops until you approve one of its
        Sizings. Refused for a container the Actuator never reported on, and while the
        Contract's Kill switch is on."""
        c = resolve(contract)

        async def work() -> dict[str, Any]:
            keys = tuple(arg(c, v) for v in (cluster, namespace, kind, name, container))
            try:
                await webapp.revert(store(), c, keys, f"mcp:{_operator()}", now())
            except ValueError as e:
                raise ToolError(str(e)) from None
            return {"workload": "/".join(keys), "revert": "asked"}

        result: dict[str, Any] = await answer(c, work())
        return result

    async def allocation(c: Contract, by: str, compare: int | None) -> webapp.Allocation:
        try:
            return await webapp.allocation_of(store(), c, by, now(), compare)
        except ValueError as e:
            raise ToolError(str(e)) from None

    @mcp.tool(annotations=READ)
    async def cost(
        contract: Annotated[str, CONTRACT],
        by: str = "namespace",
        compare_days: Annotated[int | None, Field(ge=1, le=400)] = None,
    ) -> dict[str, Any]:
        """What the Contract's Workloads cost a month, by namespace, team or
        label:<key> (`groupings` lists what this Contract allows): each group's requests
        and the waste in them (the available Savings), ranked by what the requests cost,
        with each group's change since `compare_days` ago when given. cpu in cores,
        memory in bytes, money a month in `currency`. gpus: the Contract's idle GPUs
        and what they cost, beside the Savings (never in them)."""
        c = resolve(contract)

        async def work() -> dict[str, Any]:
            a = await allocation(c, arg(c, by), compare_days)
            shown = a.model_dump(mode="json", exclude={"realized", "compare"})
            return {**shown, "compare": _compared(a.compare)}

        result: dict[str, Any] = await answer(c, work())
        return result

    @mcp.tool(annotations=READ)
    async def nodes(
        contract: Annotated[str, CONTRACT],
        cluster: str | None = None,
        top: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict[str, Any]:
        """The Contract's nodes as the Hub's latest hourly run read them. Per cluster:
        could_go, the nodes that could be removed `now` and `after` its Sizings are
        applied, with their capacity and monthly cost: an upper bound (spread rules and
        tolerations are not seen; a pod moves only where its node affinity lets it) and a
        figure of its own, never part of Savings or waste; its note says why none is
        counted; None when the read can't say. judged: how many nodes are blocked, clear
        or unknown (or pinned), so "none could go" of nodes nobody judged is no finding.
        spot: where the cluster names spot nodes, their label, the discount they are priced
        at (the share of on-demand they cost) and how many nodes carry it; each node says
        whether it is spot, and a node that could go is priced at its own rate.
        spot_candidates: Deployments and Rollouts that could run on spot (2 or more
        replicas on average, no persistent volume, blocking no node, under no Spot ratio),
        each with a what-if of 70% of its replicas on spot and 2 on-demand at least, and
        where the cluster names spot nodes what that would save a month: an upper bound,
        a figure of its own, never part of Savings. spot_workloads: those under a Spot
        ratio, the replicas intended on-demand and on spot against where they run (a CronJob
        under `spot: true`: its runs on spot and a retry off it, no split; why not, where its
        Actuator skips it; as a candidate, a run entirely on spot), a note
        while its Actuator doesn't place them yet, and once it does a risk where fewer run
        on-demand than meant (restarting the Workload repairs it; nothing here does).
        placement: where the cluster has Placement, its label, how many nodes carry it
        (Pinned nodes: kept for pods that can't move, never removed), the Workloads its
        Actuator is told to place (their new pods prefer the Pinned nodes) and whether it
        places them yet. nodes: the blocked ones first, `top` of them, each with its
        room (allocatable less every pod's requests; cpu in cores, memory in bytes), its
        pods (own_pods are the Contract's; not_visible_pods are outside its namespaces,
        never named) and could_be_removed: blocked, with its blockers by `rules`; clear;
        unknown, with why; or pinned, its blockers expected there. A blocked node's
        restart, once its Actuator places: the Workloads whose restart frees it (their
        new pods land on the Pinned nodes), or why_not; nothing here restarts one. Each
        blocker is one Workload's pods (`of`), why they keep the node, placed_by where
        its Workload is placed, and the changes that would remove it: what to change and
        what it costs, with a manifest to copy where the change adds something (it belongs
        in the client's own manifests: no tool here writes it anywhere; for a Contract that
        masks, the names in it are placeholders here and real on the console's Nodes page);
        words only where it would undo something the client set on purpose.
        autoscaler_settings_to_check: the autoscaler's own settings that decide the same for
        the whole cluster, with their documented defaults; the Hub can't read the cluster's.
        GPUs: a GPU node's gpus (allocatable, capacity, requested, product); a GPU pod moves
        only to as many free GPUs of the same product (could_go's gpu_note when a node
        doesn't say its product), and could_go's cost counts the GPUs at the cluster's GPU
        price (none without one). unrequested_gpus: GPUs no pod asks for, how many nodes
        have some and their monthly cost, apart from idle GPUs and waste.
        Nothing here removes a node or changes a cluster."""
        c = resolve(contract)

        async def work() -> dict[str, Any]:
            wanted = None if cluster is None else arg(c, cluster)
            found = await webapp.nodes_of(store(), c, now())
            names = [cl.cluster for cl in found.clusters]
            if wanted is not None and wanted not in names:
                raise ToolError(f"cluster {wanted!r} has no nodes read: one of {names}")
            shown = [_nodes(cl, top) for cl in found.clusters if wanted in (None, cl.cluster)]
            return {"currency": found.currency, "clusters": shown}

        result: dict[str, Any] = await answer(c, work())
        return result

    @mcp.tool(annotations=READ)
    async def savings(
        contract: Annotated[str, CONTRACT],
        compare_days: Annotated[int | None, Field(ge=1, le=400)] = None,
    ) -> dict[str, Any]:
        """The Contract's Savings: available (the waste in its requests, a month) and
        realized (requests lowered since, and under `floors` the HPA minimums lowered
        toward their Floors: held less now, accrued since each drop, and the last 7
        days' drops, by what they save), and the change since `compare_days`
        ago when given. cpu in cores, memory in bytes, money in `currency`."""
        c = resolve(contract)

        async def work() -> dict[str, Any]:
            a = await allocation(c, "namespace", compare_days)
            return {
                "currency": a.currency,
                "available": a.total.model_dump(mode="json"),
                "realized": a.realized.model_dump(mode="json"),
                "compare": _compared(a.compare),
                "prices": [p.model_dump(mode="json") for p in a.prices],
            }

        result: dict[str, Any] = await answer(c, work())
        return result

    @mcp.tool(annotations=READ)
    async def bill(
        contract: Annotated[str, CONTRACT],
        days: Annotated[int, Field(ge=1, le=billing.KEPT_DAYS)] = 7,
    ) -> dict[str, Any]:
        """Each billed cluster's cloud bill beside the Hub's estimate over the last full
        days the cloud has settled: billed, estimated, the gap and whether it is within
        the tolerance, and how many node-hours the estimate rests on; spot_billed and
        spot_estimated: what its spot lines cost beside what the estimate prices its spot
        hours at, where the cluster names spot nodes (a spot price checked by the bill)."""
        c = resolve(contract)

        async def work() -> dict[str, Any]:
            rows = await billing.report(store(), c, now(), days)
            return webapp.Bills.model_validate(
                {
                    "tolerance": billing.TOLERANCE,
                    "min_coverage": billing.MIN_COVERAGE,
                    "clusters": rows,
                }
            ).model_dump(mode="json")

        result: dict[str, Any] = await answer(c, work())
        return result

    @mcp.tool(name="now", annotations=READ)  # not `def now`: that is the clock here
    async def agenda_now(
        contract: Annotated[str | None, CONTRACT] = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """What to look at now, most urgent first, ranked without a model: the Kill
        switch while it is on, pauses waiting for a decision (soonest deadline first),
        failing Checks, data the Hub can't read, a Relay or Actuator whose Protocol the
        Hub refused (which side to upgrade), Bills outside their tolerance, running
        Investigations, the three Workloads whose waste costs most (one approved where an
        Actuator runs left out, counted on a line). Without `contract`: only how many of
        each per Contract."""
        if contract is not None:
            c = resolve(contract)

            async def work() -> dict[str, Any]:
                return (await agenda.of(store(), c, now())).model_dump(mode="json")

            result: dict[str, Any] = await answer(c, work())
            return result
        listed = []
        for c in sorted(by_id.values(), key=lambda c: c.id):
            a = await agenda.of(store(), c, now())
            listed.append(out(c, {"contract": c.id, "name": c.name, **agenda.counts(a)}))
        return listed

    @mcp.tool(annotations=READ)
    async def investigations(
        contract: Annotated[str, CONTRACT],
        open: bool = False,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> list[dict[str, Any]]:
        """The Contract's Investigations, newest first (Triages, Questions and failing
        Checks): each one's status, and its Category, severity and summary, Answer or
        verdict. open: only those still running or waiting for an Approval decision."""
        c = resolve(contract)

        async def work() -> list[dict[str, Any]]:
            if open:
                found = [t for t in await store().unfinished() if t.contract_id == c.id]
                found.reverse()  # newest first, like recent()
            else:
                found = await store().recent(limit, c.id)
            return [_brief(t, lambda s: out(c, s)) for t in found[:limit]]

        result: list[dict[str, Any]] = await answer(c, work())
        return result

    @mcp.tool(annotations=READ)
    async def investigation(contract: Annotated[str, CONTRACT], id: str) -> dict[str, Any]:
        """One Investigation of this Contract in full: its Report, Answer or Check
        verdict with their Evidence, the Proposed actions waiting for a decision, the
        Approval decisions made and the actions executed."""
        c = resolve(contract)

        async def work() -> dict[str, Any]:
            t = await store().get(id)
            if t is None or t.contract_id != c.id:  # another Contract's is never shown
                raise ToolError("no such Investigation in this Contract")
            full = _brief(t, lambda s: out(c, s))
            full.update(
                t.model_dump(
                    mode="json",
                    include={"report", "answer", "check_result", "pending", "executed_actions"},
                )
            )
            full["deadline"] = _date(t.deadline)
            # the pause's exact deadline, as text: `decide` names the pause with it
            open_pause = t.status == "awaiting_approval" and t.deadline is not None
            full["pause"] = repr(t.deadline) if open_pause else None
            full["decisions"] = [{**d, "at": _date(d["at"])} for d in await store().decisions(t.id)]
            return full

        result: dict[str, Any] = await answer(c, work())
        return result

    @mcp.tool(annotations=READ)
    async def checks(contract: Annotated[str, CONTRACT]) -> list[dict[str, Any]]:
        """The Contract's scheduled Checks: each one's schedule, when it runs next, and
        its last Run (status, passed or not, and its id for `investigation`)."""
        c = resolve(contract)

        async def work() -> list[dict[str, Any]]:
            return [
                {
                    "name": s["name"],
                    "schedule": s["schedule"],
                    "next": _date(s["next_due"]),
                    "last": s["last_id"],
                    "last_status": s["last_status"],
                    "last_passed": s["last_passed"],
                    "last_started": _date(s["last_started"]),
                    "skipped": _date(s["skipped_at"]),
                    "skipped_why": s["skipped_reason"],
                }
                for s in await store().check_schedules(c.id)
            ]

        result: list[dict[str, Any]] = await answer(c, work())
        return result

    @mcp.tool(annotations=ASK)
    async def ask(
        contract: Annotated[str, CONTRACT],
        question: Annotated[str, Field(min_length=1, max_length=QUESTION_LIMIT)],
    ) -> dict[str, Any]:
        """Ask the Hub's agent a Question about this Contract. It answers with Evidence
        from the Contract's read tools, in the background (a minute or more, and it costs
        model money): this returns the Question's id at once; read its Answer with
        `investigation`. Stored like a console Question; never posted to Slack."""
        c = resolve(contract)

        async def work() -> dict[str, Any]:
            qid = await asked(c.id, arg(c, question).strip())
            if qid is None:
                raise ToolError("the Question could not be started")
            return {"id": qid, "status": "running"}

        result: dict[str, Any] = await answer(c, work())
        return result

    @mcp.tool(annotations=DECIDE)
    async def decide(
        contract: Annotated[str, CONTRACT],
        id: str,
        pause: str,
        approve: bool,
        reason: str = "",
    ) -> dict[str, Any]:
        """Approve or reject every Proposed action of one pause of this Contract's
        Investigation, as Slack's buttons do: approving runs them on the Contract's
        systems. pause: the token `investigation` showed with the actions you read; an
        older token never decides a newer pause. reason (a rejection's): goes back to
        the agent, and a Teaching may be drafted from it. Approving is refused while the
        Contract's Kill switch is on; rejecting still works."""
        c = resolve(contract)

        async def work() -> dict[str, Any]:
            t = await store().get(id)
            if t is None or t.contract_id != c.id:  # another Contract's is never decided
                raise ToolError("no such Investigation in this Contract")
            if t.status != "awaiting_approval" or t.deadline is None or pause != repr(t.deadline):
                raise ToolError("that pause is not open: read the Investigation again")
            why = await decided(t, t.deadline, approve, arg(c, reason), _operator())
            if why:
                raise ToolError(why)
            return {"id": t.id, "decided": "approved" if approve else "rejected"}

        result: dict[str, Any] = await answer(c, work())
        return result

    @mcp.tool(annotations=STOP)
    async def turn_on_kill_switch(contract: Annotated[str, CONTRACT]) -> dict[str, Any]:
        """Stop every change to this Contract's systems now: approving a Proposed
        action is refused, and a change already approved is refused where it would
        run. Only the Operator, in the console, turns it off."""
        c = resolve(contract)

        async def work() -> dict[str, Any]:
            newly = await stopped(c.id, f"mcp:{_operator()}")
            on = await store().kill_switch(c.id)
            assert on is not None
            return {"kill_switch": "on" if newly else "already on", "since": _date(on["set_at"])}

        result: dict[str, Any] = await answer(c, work())
        return result

    @mcp.tool(annotations=CHANGE)
    async def propose_change(
        contract: Annotated[str, CONTRACT],
        edits: Annotated[list[proposals.Edit], Field(min_length=1, max_length=20)],
        title: Annotated[str, Field(min_length=1, max_length=120)],
        why: Annotated[str, Field(max_length=2000)] = "",
    ) -> dict[str, Any]:
        """Open a pull request on the contracts repo editing this Contract's Sizing
        policy, for the Operator to review and merge: nothing changes until it is
        merged and the Hub restarts on it. edits, in order: set_rule (cluster,
        namespace, kind, name, container, any of them, and a policy: the rule with
        exactly that match gets it, else a new rule goes first, as the first match
        wins; automate true or false turns Automation on or off for it, the Actuator
        then applying its Sizings unasked), remove_rule (the rule with exactly that
        match), set_default (a policy), set_policy (one of the Contract's own: name,
        base and the settings to change, as contract.yaml writes them: window 7d, cpu
        {headroom: 0.15}, memory {max: 256Mi}). Refused when GitHub's main is not what
        the Hub loaded, or when the Contract would not load after it. why: the pull
        request's description."""
        c = resolve(contract)

        async def work() -> dict[str, Any]:
            by = _operator()
            plain = [
                type(e).model_validate(masking._walk(e.model_dump(), lambda s: arg(c, s)))
                for e in edits
            ]
            try:
                url = await proposed(c, plain, arg(c, title), arg(c, why), by)
            except proposals.Refused as e:
                raise ToolError(str(e)) from None
            return {"pull_request": url}

        result: dict[str, Any] = await answer(c, work())
        return result

    return mcp


def _login() -> str | None:
    return get_http_headers().get(LOGIN, "").strip() or None


def _operator() -> str:
    """Who is changing something: guard() let only a listed Operator in, so a call
    without one came some other way and changes nothing."""
    login = _login()
    if login is None:
        raise ToolError("only the Operator, through tailscale serve, may change anything")
    return login


def _compared(compare: webapp.Comparison | None) -> dict[str, Any] | None:
    """The comparison with its clusters in a list: masking leaves keys alone, and a
    cluster's name can be the client's."""
    if compare is None:
        return None
    shown = compare.model_dump(mode="json")
    shown["clusters"] = [{"cluster": k, **v} for k, v in shown["clusters"].items()]
    return shown


def _brief(t: Investigation, mask: Callable[[str], str]) -> dict[str, Any]:
    """An Investigation as a list shows it. Its title and Answer are cut after `mask`:
    a term cut in two would match nothing (audit A2)."""
    out: dict[str, Any] = {
        "id": t.id,
        "kind": t.kind,
        "title": mask(t.title)[:200],
        "status": t.status,
        "started": _date(t.started),
    }
    if t.report:
        out.update(t.report.model_dump(mode="json", include={"category", "severity", "summary"}))
    if t.answer:
        out["answer"] = mask(t.answer.answer)[:500]
    if t.check_result:
        out.update(t.check_result.model_dump(mode="json", include={"passed", "summary"}))
    if t.pending:
        out["pending_actions"] = len(t.pending)
    if t.error:
        out["error"] = t.error
    return out
