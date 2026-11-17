"""The agent uses Sizings (slice 9.6a): sizing_for_workload reads a Workload's stored
Sizing by its name or a pod's, and after an OOM kill suggests memory above what it was
killed at; the eval checks a Report recommends memory above a quantity."""

from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from squero import agent, contracts, evals
from squero.config import Settings
from squero.servers import sizing as tool
from squero.store import Store, connect
from squero.usage import ContainerUsage, Workload
from tests.conftest import make_contract
from tests.test_sizing_store import MI, NOW, container, prometheus_contract, rows_of


def oom(limit: float | None = 64 * MI, peak: float = 64 * MI, **kw: Any) -> ContainerUsage:
    u = container(pods=1, peak=peak, oom_killed_pods=2, **kw)
    u.requests = {"cpu": 1.0, "memory": 32.0 * MI}
    u.limits = {} if limit is None else {"memory": limit}
    return u


def named(u: ContainerUsage, name: str, kind: str = "Deployment") -> ContainerUsage:
    u.workload = Workload("shop", kind, name)
    return u


# --- what the tool returns --------------------------------------------------------


def test_rows_store_each_resources_limit() -> None:
    (row,) = rows_of(oom())
    assert row["memory"]["limit"] == 64 * MI and row["cpu"]["limit"] is None


def test_an_oom_kill_at_a_limit_suggests_the_policys_headroom_above_it() -> None:
    (row,) = rows_of(oom())
    assert tool.suggested(row, 0.2) == {
        "memory_request": "77Mi",
        "memory_limit": "77Mi",
        "why": "OOM-killed at its 64Mi limit: its peak is the limit, not its demand, "
        "so 20% above it",
    }


def test_an_oom_kill_without_a_limit_suggests_a_request_above_the_peak() -> None:
    (row,) = rows_of(oom(limit=None, peak=200 * MI))
    s = tool.suggested(row, 0.2)
    assert s is not None and s["memory_request"] == "240Mi" and "memory_limit" not in s
    assert "without a memory limit" in s["why"]


def test_a_suggestion_is_capped_at_the_policys_memory_max() -> None:
    (row,) = rows_of(oom())  # killed at its 64Mi limit: 77Mi uncapped
    s = tool.suggested(row, 0.2, 70 * MI)
    assert s is not None and s["memory_request"] == s["memory_limit"] == "70Mi"
    assert s["why"].endswith("so 20% above it, capped at its policy's 70Mi memory max")
    assert tool.suggested(row, 0.2, 77 * MI) == tool.suggested(row, 0.2)  # at the max: not cut
    (none,) = rows_of(oom(limit=None, peak=200 * MI))
    s = tool.suggested(none, 0.2, 220 * MI)
    assert s is not None and s["memory_request"] == "220Mi" and "capped" in s["why"]


def test_the_tool_caps_its_suggestion_at_the_rows_policys_memory_max() -> None:
    (row,) = rows_of(oom())
    data = prometheus_contract().model_dump()
    data["sizing"]["policies"] = {"tight": {"base": row["policy"], "memory": {"max": "70Mi"}}}
    out = tool.container(row | {"policy": "tight"}, contracts.build(data))
    assert out["suggested"]["memory_request"] == "70Mi"


def test_a_max_at_or_below_what_it_was_killed_at_suggests_no_number() -> None:
    (row,) = rows_of(oom())
    for cap in (64 * MI, 32 * MI):
        assert tool.suggested(row, 0.2, cap) == {
            "why": f"OOM-killed at its 64Mi limit, and its policy's memory max "
            f"({cap // MI}Mi) is at or below it, so no raise fits within it: "
            "raise the max, or look for a leak"
        }
    (none,) = rows_of(oom(limit=None, peak=200 * MI))
    s = tool.suggested(none, 0.2, 200 * MI)
    assert s == {
        "why": "OOM-killed at its 200Mi peak, and its policy's memory max (200Mi) is at "
        "or below it, so no raise fits within it: raise the max, or look for a leak"
    }
    assert tool.suggested(none, 0.2, 201 * MI) is not None


def test_no_suggestion_without_an_oom_kill_or_for_a_row_without_limits() -> None:
    (row,) = rows_of(container(pods=1))
    assert tool.suggested(row, 0.2) is None
    (old,) = rows_of(oom())
    del old["memory"]["limit"]  # stored before 9.6
    assert tool.suggested(old, 0.2) is None


def test_a_container_reads_as_quantities_with_its_risks() -> None:
    (row,) = rows_of(oom())
    out = tool.container(row, prometheus_contract())
    assert out["memory"] == {
        "request": "32Mi",
        "limit": "64Mi",
        "sizing": "64Mi",  # raised to its limit, which is below the 77Mi it needs
        "action": "raise",
        "peak": "64Mi",
    }
    assert out["cpu"]["request"] == "1000m" and out["cpu"]["limit"] is None
    assert out["cpu"]["sizing"] == "115m" and out["cpu"]["p95"] == "100m"
    assert out["waste"] == {"cpu": "885m", "memory": "0Mi"}
    assert any("OOM-killed" in r for r in out["risks"])
    assert out["suggested"]["memory_request"] == "77Mi"
    (learning,) = rows_of(container(hours=2))
    assert tool.container(learning, prometheus_contract())["learning"] == "2 h of usage, needs 24 h"


def test_a_pod_name_finds_its_workload_and_the_longest_name_wins() -> None:
    rows = rows_of(
        named(container(pods=1), "check"),
        named(container(pods=1), "checkout"),
        named(container(pods=1), "checkout", "StatefulSet"),
    )
    assert {r["kind"] for r in tool.matching(rows, "checkout-7d9f-abc", None)} == {
        "Deployment",
        "StatefulSet",
    }  # a clash without kind: both
    assert [r["kind"] for r in tool.matching(rows, "checkout", "StatefulSet")] == ["StatefulSet"]
    assert [r["name"] for r in tool.matching(rows, "check-0", None)] == ["check"]
    assert tool.matching(rows, "checkoutx-1", None) == []
    api = rows_of(named(container(pods=1), "api"), named(container(pods=1), "api-gateway"))
    assert [r["name"] for r in tool.matching(api, "api-gateway-5f6c-x2", None)] == ["api-gateway"]
    assert [r["name"] for r in tool.matching(api, "api-5f6c-x2", None)] == ["api"]
    assert tool.matching(rows, "chec", None) == []


# --- the server -------------------------------------------------------------------


@pytest.fixture
async def store(database_url: str) -> Any:
    async with connect(database_url) as pool:
        s = Store(pool)
        await s.migrate()
        yield s


async def seed(
    store: Store, contract_id: str, *us: ContainerUsage, error: str | None = None
) -> None:
    run = await store.claim_sizing_run(contract_id, NOW, 0)
    assert run is not None
    await store.finish_sizing_run(run, contract_id, {"test": None}, rows_of(*us))
    if error:
        run = await store.claim_sizing_run(contract_id, NOW + 3600, 0)
        assert run is not None
        await store.finish_sizing_run(run, contract_id, {"test": error}, [])


async def ask(server: Any, **args: Any) -> dict[str, Any]:
    async with Client(server) as client:
        r = await client.call_tool("for_workload", {"cluster": "test", "namespace": "shop", **args})
    assert isinstance(r.structured_content, dict)
    return r.structured_content


@pytest.mark.anyio
async def test_the_tool_answers_for_the_alerting_pod(store: Store) -> None:
    await seed(store, "acme", named(oom(), "reports"), named(container(pods=1), "api"))
    out = await ask(tool.build_server(prometheus_contract(), store), name="reports-55b4f-hmq7f")
    (w,) = out["workloads"]
    assert (w["workload"], w["kind"], w["as_of"]) == (
        "test/shop/Deployment/reports",
        "Deployment",
        "1972-09-27 00:00 UTC",
    )
    (c,) = w["containers"]
    assert c["suggested"]["memory_limit"] == "77Mi" and "stale" not in w


@pytest.mark.anyio
async def test_a_stale_cluster_and_an_unknown_workload_say_so(store: Store) -> None:
    await seed(store, "acme", named(oom(), "reports"), error="Prometheus 500")
    server = tool.build_server(prometheus_contract(), store)
    (w,) = (await ask(server, name="reports"))["workloads"]
    assert w["stale"] == "reads failing since 1972-09-27 01:00 UTC: Prometheus 500"
    out = await ask(server, name="nope")
    assert out == {"workloads": [], "note": "No stored Sizing for 'nope' in test/shop"}


@pytest.mark.anyio
async def test_only_the_contracts_clusters_and_namespaces(store: Store) -> None:
    server = tool.build_server(prometheus_contract(), store)
    with pytest.raises(ToolError, match="has no Sizings"):
        await ask(server, cluster="other", name="x")
    with pytest.raises(ToolError, match="not allowed in cluster"):
        await ask(server, namespace="kube-system", name="x")
    out = await ask(server, name="x")
    assert out["note"].endswith("none stored for this cluster yet")


@pytest.mark.anyio
async def test_a_workload_of_the_same_name_in_another_namespace_is_not_it(store: Store) -> None:
    data = make_contract(clusters={"test": ["shop", "web"]}).model_dump()
    data["integrations"].append({"name": "prometheus", "url": "http://prometheus"})
    c = contracts.build(data)
    web = [{**r, "namespace": "web"} for r in rows_of(named(oom(), "reports"))]
    run = await store.claim_sizing_run("acme", NOW, 0)
    assert run is not None
    await store.finish_sizing_run(run, "acme", {"test": None}, web)
    out = await ask(tool.build_server(c, store), name="reports")
    assert out["workloads"] == []


@pytest.mark.anyio
async def test_without_the_hubs_database_the_tool_says_so() -> None:
    with pytest.raises(ToolError, match="Hub's database"):
        await ask(tool.build_server(prometheus_contract(), None), name="x")


# --- the Contract and the agent ---------------------------------------------------


def with_sizing(**kw: Any) -> contracts.Contract:
    data = make_contract(**kw).model_dump()
    data["integrations"].append({"name": "sizing"})
    return contracts.build(data)


def test_the_sizing_integration_needs_a_cluster_with_a_prometheus() -> None:
    with pytest.raises(ValueError, match="needs a cluster with a Prometheus"):
        with_sizing()
    c = make_contract().model_dump()
    c["integrations"].append({"name": "sizing"})
    c["clusters"][0]["prometheus_url"] = "http://prometheus"
    assert any(i.name == "sizing" for i in contracts.build(c).integrations)


@pytest.mark.anyio
async def test_the_agent_gets_the_tool_and_its_guide_only_from_the_hub(
    settings: Settings, store: Store
) -> None:
    data = prometheus_contract().model_dump()
    data["integrations"].append({"name": "sizing"})
    c = contracts.build(data)
    hub = agent.build_mcp(c, settings, {"test": (None, None)}, store=store)
    async with Client(hub.mcp) as client:
        names = {t.name for t in await client.list_tools()}
    assert "sizing_for_workload" in names and hub.access["sizing_for_workload"] == "read"
    assert tool.GUIDE in hub.guides
    cli = agent.build_mcp(c, settings, {"test": (None, None)})
    assert tool.GUIDE not in cli.guides
    plain = agent.build_mcp(prometheus_contract(), settings, {"test": (None, None)}, store=store)
    assert tool.GUIDE not in plain.guides
    assert (
        "sizing",
        "sizing_for_workload",
        {"cluster": "test", "namespace": "shop", "name": "health"},
    ) in agent.probes(c)


# --- the eval ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("steps", "passes"),
    [
        (["Raise the memory limit to 77Mi and the request to 77Mi."], True),
        (["Set memory to 1.5Gi."], True),
        (["Give it 96 MiB."], True),
        (["Give it 80M of memory."], True),
        (["Give it 900000K."], True),
        (["Keep the 64Mi limit; investigate the leak."], False),
        (["Raise CPU to 500m, wait 5 minutes."], False),
        ([], False),
    ],
)
def test_memory_above_reads_quantities_in_the_recommendations(
    steps: list[str], passes: bool
) -> None:
    assert evals.memory_above(steps, "64Mi") is passes


def test_a_scenario_with_sizing_gets_a_prometheus_for_its_clusters() -> None:
    c = evals.scenario_contract("demo", False, Path("none"), integrations=["kubernetes", "sizing"])
    assert [i.name for i in c.integrations] == ["kubernetes", "sizing"]
    assert all(cl.prometheus_url for cl in c.clusters)
