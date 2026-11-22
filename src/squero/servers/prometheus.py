"""Prometheus MCP server: PromQL queries and active alerts, read-only.

Range queries are summarized per series (first/last/min/max) instead of
returning raw points, which keeps results small enough for the model.

A Contract with clusters gets each cluster's own Prometheus (slice 9.6b): every tool
takes `cluster`. A cluster whose store is shared with other Contracts (it sets
`prometheus_labels`) gets none: free PromQL there could read another Contract's
series, and nothing here can force the labels into it.
"""

import time
from typing import Annotated, Any

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from squero.servers import Access
from squero.usage import SigV4, refusal

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
MAX_SERIES = 30
ACCESS: dict[str, Access] = {
    "query": "read",
    "query_range": "read",
    "active_alerts": "read",
}  # ADR 0002
QUERY = """Instant PromQL query (current value). Examples:
kube_pod_container_status_restarts_total{namespace="shop"}
sum by (pod) (rate(container_cpu_usage_seconds_total{namespace="shop"}[5m]))"""
QUERY_RANGE = """PromQL over the last N minutes, summarized per series as first/last/min/max.
Use it to see when something started and whether it is getting worse."""
ALERTS = "Alerts currently firing or pending in Prometheus: what else is going on?"


def _num(v: str) -> float | str:
    try:
        return round(float(v), 4)
    except ValueError:
        return v


def build_server(
    stores: dict[str, tuple[str, httpx.Auth | None]],
    clustered: bool,
    http: dict[str, httpx.Client] | None = None,
    shared: list[str] | None = None,
    relayed: list[str] | None = None,
) -> FastMCP:
    """stores: cluster -> (url, auth: `usage.auth`); one under "" when not clustered (a Contract
    without clusters: no `cluster` argument). shared: the clusters left out, for the
    argument's description. relayed: clusters the model may name whose Prometheus the
    Hub reaches through their Relay (slice 3.3a). http: cluster -> client, for tests."""
    clients = http or {
        name: httpx.Client(base_url=url, auth=auth, timeout=15)
        for name, (url, auth) in stores.items()
    }
    mcp = FastMCP("prometheus", instructions="Query metrics and alerts with PromQL.")
    names = sorted([*stores, *(relayed or [])])

    def client(cluster: str) -> httpx.Client:
        if cluster not in clients:
            raise ToolError(f"cluster {cluster!r} has no Prometheus here: one of {names}")
        return clients[cluster]

    def api(cluster: str, path: str, params: dict[str, Any]) -> Any:
        try:
            resp = client(cluster).get(path, params=params)
        except httpx.HTTPError as e:
            raise ToolError(f"Prometheus unreachable: {e}") from None
        body = (
            resp.json()
            if resp.headers.get("content-type", "").startswith("application/json")
            else {}
        )
        if resp.status_code != 200 or body.get("status") != "success":
            # a signed store's refusal (19.1b) by its type only, never AWS's own words: its
            # message can quote the signed request
            signed = isinstance(client(cluster).auth, SigV4)
            said = refusal(resp, body) or ("refused" if signed else resp.text[:300])
            raise ToolError(f"Prometheus error {resp.status_code}: {said}")
        return body["data"]

    def trimmed(series: list[Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {"series": rows[:MAX_SERIES]}
        if len(series) > MAX_SERIES:
            out["note"] = f"{len(series)} series, showing {MAX_SERIES}; aggregate with sum/topk"
        return out

    def query(cluster: str, promql: str) -> dict[str, Any]:
        data = api(cluster, "/api/v1/query", {"query": promql})
        if data["resultType"] != "vector":
            return {"result": data["result"]}
        rows = [{"labels": s["metric"], "value": _num(s["value"][1])} for s in data["result"]]
        return trimmed(data["result"], rows)

    def query_range(cluster: str, promql: str, minutes: int, step: str) -> dict[str, Any]:
        minutes = max(1, min(minutes, 24 * 60))
        end = time.time()
        data = api(
            cluster,
            "/api/v1/query_range",
            {"query": promql, "start": end - minutes * 60, "end": end, "step": step},
        )
        rows = []
        for s in data["result"]:
            values = [float(v) for _, v in s["values"]]
            rows.append(
                {
                    "labels": s["metric"],
                    "first": round(values[0], 4),
                    "last": round(values[-1], 4),
                    "min": round(min(values), 4),
                    "max": round(max(values), 4),
                    "points": len(values),
                }
            )
        return trimmed(data["result"], rows)

    def active_alerts(cluster: str) -> list[dict[str, Any]]:
        alerts = api(cluster, "/api/v1/alerts", {})["alerts"]
        return [
            {
                "name": a["labels"].get("alertname"),
                "state": a["state"],
                "labels": a["labels"],
                "since": a.get("activeAt"),
            }
            for a in alerts[:50]
        ]

    if not clustered:

        @mcp.tool(annotations=READ, name="query", description=QUERY)
        def query_one(promql: str) -> dict[str, Any]:
            return query("", promql)

        @mcp.tool(annotations=READ, name="query_range", description=QUERY_RANGE)
        def query_range_one(promql: str, minutes: int = 60, step: str = "1m") -> dict[str, Any]:
            return query_range("", promql, minutes, step)

        @mcp.tool(annotations=READ, name="active_alerts", description=ALERTS)
        def active_alerts_one() -> list[dict[str, Any]]:
            return active_alerts("")

        return mcp
    if not names:  # every cluster's store is shared: no PromQL at all
        return mcp
    hint = "Which cluster's Prometheus; use the alert's cluster label. One of: " + ", ".join(names)
    if shared:
        hint += f". None for {', '.join(shared)} (a store shared with other Contracts)"
    Cluster = Annotated[str, Field(description=hint, json_schema_extra={"enum": list[Any](names)})]

    @mcp.tool(annotations=READ, name="query", description=QUERY)
    def query_in(cluster: Cluster, promql: str) -> dict[str, Any]:
        return query(cluster, promql)

    @mcp.tool(annotations=READ, name="query_range", description=QUERY_RANGE)
    def query_range_in(
        cluster: Cluster, promql: str, minutes: int = 60, step: str = "1m"
    ) -> dict[str, Any]:
        return query_range(cluster, promql, minutes, step)

    @mcp.tool(annotations=READ, name="active_alerts", description=ALERTS)
    def active_alerts_in(cluster: Cluster) -> list[dict[str, Any]]:
        return active_alerts(cluster)

    return mcp
