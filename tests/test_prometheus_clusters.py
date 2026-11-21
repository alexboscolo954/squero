"""The agent's Prometheus per cluster (slice 9.6b): each cluster's tools go to its own
Prometheus (its URL and token, else the integration's); a cluster on a store shared
with other Contracts (prometheus_labels) gets no PromQL; a Contract without clusters
keeps the tools without `cluster`."""

from typing import Any

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from pydantic import SecretStr

from squero import agent, contracts, usage
from squero.config import Settings
from squero.servers import prometheus
from tests.conftest import make_contract

OK = {"status": "success", "data": {"resultType": "vector", "result": []}}


def fake(seen: list[str], name: str) -> httpx.Client:
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(f"{name} {req.headers.get('authorization', '-')}")
        if req.url.path == "/api/v1/alerts":
            return httpx.Response(200, json={"status": "success", "data": {"alerts": []}})
        return httpx.Response(200, json=OK)

    return httpx.Client(transport=httpx.MockTransport(handler), base_url=f"http://{name}")


def contract(**clusters: dict[str, Any]) -> contracts.Contract:
    data = make_contract(clusters={n: ["shop"] for n in clusters}).model_dump()
    for c in data["clusters"]:
        c.update(clusters[c["name"]])
    data["integrations"].append(
        {"name": "prometheus", "url": "http://central", "token_secret": "central-token"}
    )
    secrets = {"central-token": SecretStr("c-tok"), "eu-token": SecretStr("eu-tok")}
    return contracts.build(data, secrets=secrets)


THREE = {
    "eu": {"prometheus_url": "http://eu", "prometheus_token_secret": "eu-token"},
    "us": {},  # the integration's
    "lab": {"prometheus_url": "http://shared", "prometheus_labels": {"cluster": "lab"}},
}


def header(a: httpx.Auth | None) -> str | None:
    """The Authorization header an auth puts on a request (slice 19.1a)."""
    return (
        next(a.auth_flow(httpx.Request("GET", "http://x"))).headers["authorization"] if a else None
    )


def captured(monkeypatch: pytest.MonkeyPatch, c: contracts.Contract, settings: Settings) -> Any:
    got: dict[str, Any] = {}
    real = prometheus.build_server

    def spy(
        stores: Any, clustered: bool, http: Any = None, shared: Any = None, relayed: Any = None
    ) -> Any:
        got.update(stores=stores, clustered=clustered, shared=shared, relayed=relayed)
        return real(stores, clustered, http, shared, relayed)

    monkeypatch.setattr(prometheus, "build_server", spy)
    k8s = next(i for i in c.integrations if i.name == "prometheus")
    server, _ = agent.integration_server(c, k8s, settings)
    return got, server


def test_each_cluster_gets_its_own_prometheus_and_a_shared_store_none(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    got, _ = captured(monkeypatch, contract(**THREE), settings)
    stores = {k: (url, header(a)) for k, (url, a) in got["stores"].items()}
    assert stores == {
        "eu": ("http://eu", "Bearer eu-tok"),
        "us": ("http://central", "Bearer c-tok"),
    }
    assert got["clustered"] is True and got["shared"] == ["lab"] and got["relayed"] == []


def test_a_contract_without_clusters_keeps_the_integrations_url_and_no_cluster_argument(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    data = {
        "id": "cloud",
        "name": "cloud",
        "integrations": [{"name": "prometheus", "url": "http://central"}],
    }
    got, _ = captured(monkeypatch, contracts.build(data), settings)
    assert got["stores"] == {"": ("http://central", None)} and got["clustered"] is False


@pytest.mark.anyio
async def test_a_query_goes_to_the_clusters_own_prometheus() -> None:
    seen: list[str] = []
    stores = {"eu": ("http://eu", None), "us": ("http://us", None)}
    http = {"eu": fake(seen, "eu"), "us": fake(seen, "us")}
    async with Client(prometheus.build_server(stores, True, http, ["lab"])) as c:
        tools = {t.name: t for t in await c.list_tools()}
        await c.call_tool("query", {"cluster": "us", "promql": "up"})
        await c.call_tool("query_range", {"cluster": "us", "promql": "up"})
        await c.call_tool("active_alerts", {"cluster": "us"})
        with pytest.raises(ToolError, match="has no Prometheus here"):
            await c.call_tool("query", {"cluster": "lab", "promql": "up"})
    assert [s.split()[0] for s in seen] == ["us", "us", "us"]  # never the first, eu
    for name in ("query", "query_range", "active_alerts"):
        cluster = tools[name].input_schema["properties"]["cluster"]
        assert cluster["enum"] == ["eu", "us"] and "cluster" in tools[name].input_schema["required"]
        assert "None for lab (a store shared with other Contracts)" in cluster["description"]


@pytest.mark.anyio
async def test_every_cluster_on_a_shared_store_means_no_promql() -> None:
    async with Client(prometheus.build_server({}, True, shared=["lab"])) as c:
        assert await c.list_tools() == []


@pytest.mark.anyio
async def test_without_clusters_the_tools_take_no_cluster() -> None:
    seen: list[str] = []
    server = prometheus.build_server({"": ("http://central", None)}, False, {"": fake(seen, "c")})
    async with Client(server) as c:
        tools = {t.name: t for t in await c.list_tools()}
        await c.call_tool("query", {"promql": "up"})
    assert all("cluster" not in t.input_schema.get("properties", {}) for t in tools.values())
    assert seen == ["c -"]


def test_the_probes_check_each_cluster_with_promql() -> None:
    checks = [p for p in agent.probes(contract(**THREE)) if p[1] == "prometheus_query"]
    assert checks == [
        ("prometheus eu", "prometheus_query", {"cluster": "eu", "promql": "vector(1)"}),
        ("prometheus us", "prometheus_query", {"cluster": "us", "promql": "vector(1)"}),
    ]
    alone = contracts.build(
        {"id": "x", "name": "x", "integrations": [{"name": "prometheus", "url": "http://p"}]}
    )
    assert [p for p in agent.probes(alone) if p[1]] == [
        ("prometheus", "prometheus_query", {"promql": "vector(1)"})
    ]


@pytest.mark.anyio
async def test_each_clusters_token_goes_only_to_its_own_prometheus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(f"{req.url.host} {req.headers.get('authorization', '-')}")
        return httpx.Response(200, json=OK)

    real = httpx.Client
    monkeypatch.setattr(
        prometheus.httpx,
        "Client",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )
    stores = {"eu": ("http://eu", usage.auth(SecretStr("eu-tok"))), "us": ("http://us", None)}
    async with Client(prometheus.build_server(stores, True)) as c:
        for cluster in ("eu", "us"):
            await c.call_tool("query", {"cluster": cluster, "promql": "up"})
    assert seen == ["eu Bearer eu-tok", "us -"]
