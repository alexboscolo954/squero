"""Sizings MCP server: a Workload's stored Sizings for the agent (slice 9.6a).

Reads the Hub's latest stored run (as the console and the weekly digest do), never the
client's Prometheus. The server is built for one Contract and puts its id in every
query; the model passes a cluster, a namespace and a name, so another Contract's
Sizings are never reachable.

After an OOM kill a Sizing keeps memory as it is (the peak is the limit, not the
demand), so the tool adds a suggested memory request and limit for the Report.
"""

from datetime import UTC, datetime
from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from squero import usage
from squero.contracts import Contract
from squero.servers import Access
from squero.sizing import _round_up, show
from squero.store import Store

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
ACCESS: dict[str, Access] = {"for_workload": "read"}  # ADR 0002
GUIDE = """\
Sizings (sizing_ tools): for an alert about a container's resources (an OOM kill,
CPU throttling, memory or CPU pressure, restarts from either), call
sizing_for_workload once with the alerting pod or Workload. It returns what the
Hub measured: the requests and limits now, what they should be and from which usage,
and the risks. After an OOM kill it returns `suggested`: recommend that memory
request and limit, and cite the Sizing as Evidence.
"""


def _date(epoch: float | None) -> str | None:
    return (
        None if epoch is None else datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%d %H:%M UTC")
    )


def _q(resource: str, value: float | None) -> str | None:
    return None if value is None else show(resource, value)


def suggested(
    row: dict[str, Any], headroom: float, cap: float | None = None
) -> dict[str, str] | None:
    """For an OOM-killed container, memory `headroom` above what it was killed at:
    its limit, or its peak when it has none (the node's OOM killer did it), never past
    `cap` (its policy's memory max). A cap at or below what it was killed at gives no
    number, only why: a suggestion that raises nothing after an OOM kill is wrong.
    None when it wasn't OOM-killed, or its row predates stored limits."""
    memory = row.get("memory")
    if not memory or "limit" not in memory:
        return None
    if not any(r["kind"] == "oom_killed" for r in row.get("risks", [])):
        return None
    limit = memory["limit"]
    at = limit if limit is not None else memory["basis"]
    killed = f"its {show('memory', at)} {'limit' if limit is not None else 'peak'}"
    if cap is not None and cap <= at:
        return {
            "why": f"OOM-killed at {killed}, and its policy's memory max "
            f"({show('memory', cap)}) is at or below it, so no raise fits within it: "
            "raise the max, or look for a leak"
        }
    to = _round_up("memory", at * (1 + headroom))
    capped = ""
    if cap is not None and to > cap:
        to, capped = cap, f", capped at its policy's {show('memory', cap)} memory max"
    if limit is not None:
        return {
            "memory_request": show("memory", to),
            "memory_limit": show("memory", to),
            "why": f"OOM-killed at {killed}: its peak is the limit, not its demand, so "
            f"{headroom:.0%} above it{capped}",
        }
    return {
        "memory_request": show("memory", to),
        "why": "OOM-killed without a memory limit (the node ran out of memory): "
        f"{headroom:.0%} above {killed}{capped}",
    }


def container(row: dict[str, Any], contract: Contract) -> dict[str, Any]:
    """One stored row as the model sees it: quantities as 115m and 96Mi."""
    out: dict[str, Any] = {
        "container": row["container"],
        "policy": row["policy"],
        "state": row["state"],
    }
    if row["state"] == "learning":
        out["learning"] = f"{row['history_h']:g} h of usage, needs {row['needs_h']:g} h"
    for res, basis in (("cpu", "p95"), ("memory", "peak")):
        r = row.get(res)
        if r is None:
            continue
        out[res] = {
            "request": _q(res, r["current"]),
            "limit": _q(res, r["limit"]) if "limit" in r else "unknown",
            "sizing": show(res, r["target"]),
            "action": r["action"],
            basis: show(res, r["basis"]),
        }
    if "pods" in row:
        out["avg_pods"] = row["pods"]
    if "waste" in row:
        out["waste"] = {
            "cpu": show("cpu", row["waste"].get("cpu", 0.0)),
            "memory": show("memory", row["waste"].get("memory", 0.0)),
        }
    if row.get("risks"):
        out["risks"] = [r["text"] for r in row["risks"]]
    policy = contract.sizing.policy(row["policy"])
    if policy and (s := suggested(row, policy.memory.headroom, policy.memory.max)):
        out["suggested"] = s
    return out


def matching(rows: list[dict[str, Any]], name: str, kind: str | None) -> list[dict[str, Any]]:
    """Rows of the Workload `name` names: itself, or the longest Workload name a pod
    name starts with (checkout for checkout-7d9f-abc, never check)."""
    rows = [r for r in rows if kind is None or r["kind"] == kind]
    names = {r["name"] for r in rows if name == r["name"] or name.startswith(r["name"] + "-")}
    if not names:
        return []
    best = max(names, key=len)
    return [r for r in rows if r["name"] == best]


def build_server(contract: Contract, store: Store | None) -> FastMCP:
    """store None: tool definitions only (replay answers before any call)."""
    spaces = {c.name: sorted(c.namespaces) for c in contract.clusters}
    names = [t.cluster for t in usage.targets(contract)]
    cluster_arg = Field(
        description="Which cluster; use the alert's cluster label. One of: "
        + "; ".join(f"{n} (namespaces: {spaces[n]})" for n in names),
        json_schema_extra={"enum": list[Any](names)},
    )
    mcp = FastMCP("sizing", instructions="This Contract's stored Sizings.")

    @mcp.tool(annotations=READ)
    async def for_workload(
        cluster: Annotated[str, cluster_arg],
        namespace: str,
        name: str,
        kind: str | None = None,
    ) -> dict[str, Any]:
        """The Hub's Sizing of one Workload, per container: requests and limits now,
        what they should be (CPU from its weighted p95, memory from its peak, with
        headroom), its risks and waste, and a suggested memory request and limit
        after an OOM kill. name: the Workload or one of its pods (from the alert).
        kind (Deployment, StatefulSet, ...) only when two Workloads share a name."""
        return await workload(store, contract, cluster, namespace, name, kind)

    return mcp


def allowed(contract: Contract, cluster: str, namespace: str) -> None:
    """Refuse a cluster without Sizings or a namespace the Contract doesn't allow there."""
    spaces = {c.name: sorted(c.namespaces) for c in contract.clusters}
    names = [t.cluster for t in usage.targets(contract)]
    if cluster not in names:
        raise ToolError(f"cluster {cluster!r} has no Sizings: one of {names}")
    if namespace not in spaces[cluster]:
        raise ToolError(
            f"namespace {namespace!r} is not allowed in cluster {cluster!r}; "
            f"allowed: {spaces[cluster]}"
        )


async def workload(
    store: Store | None,
    contract: Contract,
    cluster: str,
    namespace: str,
    name: str,
    kind: str | None,
) -> dict[str, Any]:
    """One Workload's Sizing, within the Contract's clusters and namespaces: the agent's
    tool and the Hub MCP server's (slice 11.1a)."""
    allowed(contract, cluster, namespace)
    if store is None:
        raise ToolError("Sizings need the Hub's database")
    rows, status = await store.latest_sizings(contract.id, {cluster})
    st = status.get(cluster, {"at": None, "error": None, "since": None})
    found = matching([r for r in rows if r["namespace"] == namespace], name, kind)
    if not found:
        return {
            "workloads": [],
            "note": f"No stored Sizing for {name!r} in {cluster}/{namespace}"
            + ("" if st["at"] else ": none stored for this cluster yet"),
        }
    workloads: dict[tuple[str, str], dict[str, Any]] = {}
    for r in found:
        w = workloads.setdefault(
            (r["kind"], r["name"]),
            {
                "workload": r["workload"].rsplit("/", 1)[0],
                "kind": r["kind"],
                "name": r["name"],
                "as_of": _date(st["at"]),
                "containers": [],
            },
        )
        if st["error"]:
            w["stale"] = f"reads failing since {_date(st['since'])}: {st['error']}"
        w["containers"].append(container(r, contract))
    return {"workloads": list(workloads.values())}
