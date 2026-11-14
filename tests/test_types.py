"""A DaemonSet's Sizing per instance type (slice 15.3a): on a cluster that runs an Actuator,
its container is also sized for its pods on nodes of each instance type, from their own
usage and requests, beside the pooled Sizing, which stays the one applied and the one Waste
counts against. Shown by `sizing show`, the API, the console and the Hub MCP."""

from typing import Any

import pytest
from pydantic import SecretStr

from squero import contracts, sizing, usage, webapp
from squero.store import Store
from squero.usage import SHARED_STORE, Target, UsageError
from tests.fake_prometheus import FakePrometheus
from tests.test_sizing_store import NOW, prometheus_contract, store  # noqa: F401
from tests.test_usage import _prom, cpu, deployment, mem, owner

MI = 2**20
N = 1441  # samples: two days at the 7-day window's step, a day more than a Sizing needs
SHOP = Target("test", "http://prometheus", None, {}, ["shop"])
TYPE = "label_node_kubernetes_io_instance_type"


def a_node(fake: FakePrometheus, name: str, kind: str | None, **labels: str) -> None:
    typed = {TYPE: kind} if kind else {}
    fake.add("kube_node_labels", 1, node=name, **typed, **labels)


def agent(
    fake: FakePrometheus,
    pod: str,
    node: str | None,
    cores: float = 0.05,
    peak: float = 64.0 * MI,
    request: float = 0.5,
    ns: str = "shop",
    samples: int = N,
) -> None:
    """A pod of DaemonSet `agent` on `node`, using `cores` and `peak` flat, asking `request`."""
    owner(fake, pod, "DaemonSet", "agent", ns, owner_is_controller="true")
    cpu(fake, pod, [cores] * samples, ns=ns)
    mem(fake, pod, [peak] * samples, ns=ns)
    if node is not None:
        fake.add("kube_pod_info", 1, namespace=ns, pod=pod, node=node)
    who = {"namespace": ns, "pod": pod, "container": "app"}
    fake.add("kube_pod_container_resource_requests", request, resource="cpu", **who)
    fake.add("kube_pod_container_resource_requests", 256.0 * MI, resource="memory", **who)


def fleet() -> FakePrometheus:
    """Two small nodes whose agent idles, one large node whose agent is busy."""
    fake = FakePrometheus()
    for name, kind in (("s1", "m5.large"), ("s2", "m5.large"), ("big", "m5.4xlarge")):
        a_node(fake, name, kind)
    agent(fake, "agent-s1", "s1")
    agent(fake, "agent-s2", "s2")
    agent(fake, "agent-big", "big", cores=1.0, peak=900.0 * MI, request=2.0)
    return fake


def acting(**cluster: Any) -> contracts.Contract:
    """A Contract with a Prometheus whose cluster runs an Actuator."""
    data = prometheus_contract().model_dump()
    data["clusters"][0] |= {"actuator_token_secret": "act", **cluster}
    return contracts.build(data, secrets={"act": SecretStr("a" * 40)})


def collected(fake: FakePrometheus, monkeypatch: pytest.MonkeyPatch, c: Any = None) -> Any:
    monkeypatch.setattr(usage, "Prom", lambda t: _prom(fake, t))
    return usage.collect(c or acting(), [7 * 86400], NOW)


def rows_of(fake: FakePrometheus, monkeypatch: pytest.MonkeyPatch, c: Any = None) -> Any:
    c = c or acting()
    found = collected(fake, monkeypatch, c)
    return sizing.rows(c, found.by_window, NOW, found.scaled, found.rooms)


# --- the read --------------------------------------------------------------------------


def test_each_pod_gets_its_nodes_instance_type_over_the_window() -> None:
    fake = FakePrometheus()
    a_node(fake, "n1", "m5.large")
    a_node(fake, "n2", None)  # the label not allowlisted, or bare metal
    a_node(fake, "far", "c5.large", cluster="b")
    for pod, node, ns in (("a", "n1", "shop"), ("b", "n2", "shop"), ("c", "gone", "shop")):
        fake.add("kube_pod_info", 1, namespace=ns, pod=pod, node=node)
    fake.add("kube_pod_info", 1, namespace="bank", pod="theirs", node="n1")
    got = usage.pod_types(_prom(fake, SHOP), SHOP, 3600, NOW)
    # a pod whose node isn't found is left out: not known is no type
    assert got == {("shop", "a"): "m5.large", ("shop", "b"): ""}
    assert all("[3600s]" in q for q in fake.queries)
    assert 'namespace=~"shop"' in fake.queries[1] and "kube_pod_info" in fake.queries[1]
    b = Target("test", "http://prometheus", None, {"cluster": "b"}, ["shop"])
    fake.add("kube_pod_info", 1, namespace="shop", pod="far-1", node="far", cluster="b")
    a_node(fake, "far", "t3.nano")  # another cluster's node of the same name, in one store
    assert usage.pod_types(_prom(fake, b), b, 3600, NOW) == {("shop", "far-1"): "c5.large"}
    old = FakePrometheus()
    fake_label = "label_beta_kubernetes_io_instance_type"
    old.add("kube_node_labels", 1, node="n1", **{fake_label: "t2.small"})
    old.add("kube_pod_info", 1, namespace="shop", pod="a", node="n1")
    assert usage.pod_types(_prom(old, SHOP), SHOP, 3600, NOW) == {("shop", "a"): "t2.small"}


def test_a_daemonsets_container_is_also_kept_by_instance_type() -> None:
    fake = fleet()
    for p, node in zip(deployment(fake, "api", pods=2), ("s1", "big"), strict=True):
        cpu(fake, p, [0.2] * N)  # a Deployment's pods on nodes of two types
        fake.add("kube_pod_info", 1, namespace="shop", pod=p, node=node)
    types = usage.pod_types(_prom(fake, SHOP), SHOP, 7 * 86400, NOW)
    found = usage.usage(_prom(fake, SHOP), SHOP, 7 * 86400, NOW, types=types)
    by = {(r.workload.kind, r.workload.name): r for r in found.rows}
    assert by["Deployment", "api"].groups == {}  # only a DaemonSet's pods sit one a node
    row = by["DaemonSet", "agent"]
    assert sorted(row.groups) == ["m5.4xlarge", "m5.large"]
    small, big = row.groups["m5.large"], row.groups["m5.4xlarge"]
    assert (small.pods, big.pods) == ({"agent-s1", "agent-s2"}, {"agent-big"})
    assert {v for _, v in small.cpu} == {0.05} and {v for _, v in big.cpu} == {1.0}
    assert len(small.cpu) == 2 * N and len(row.cpu) == 3 * N  # the row stays pooled
    # each group's requests are its own pods'; the row's are the largest of all
    assert (small.requests["cpu"], big.requests["cpu"], row.requests["cpu"]) == (0.5, 2.0, 2.0)
    assert small.live and big.live and small.workload == row.workload
    without = usage.usage(_prom(fake, SHOP), SHOP, 7 * 86400, NOW)
    assert all(r.groups == {} for r in without.rows)


def test_fewer_than_two_types_is_no_group_and_a_pod_of_no_known_node_is_in_none() -> None:
    one = FakePrometheus()
    a_node(one, "s1", "m5.large")
    a_node(one, "s2", "m5.large")
    agent(one, "agent-s1", "s1")
    agent(one, "agent-s2", "s2")
    types = usage.pod_types(_prom(one, SHOP), SHOP, 7 * 86400, NOW)
    (row,) = usage.usage(_prom(one, SHOP), SHOP, 7 * 86400, NOW, types=types).rows
    assert row.groups == {}  # one type: the pooled Sizing is that one's
    bare = FakePrometheus()
    a_node(bare, "s1", None)
    a_node(bare, "s2", None)
    agent(bare, "agent-s1", "s1")
    agent(bare, "agent-s2", "s2")
    types = usage.pod_types(_prom(bare, SHOP), SHOP, 7 * 86400, NOW)
    assert set(types.values()) == {""}
    (row,) = usage.usage(_prom(bare, SHOP), SHOP, 7 * 86400, NOW, types=types).rows
    assert row.groups == {}  # no label anywhere: one group, as before
    mixed = fleet()
    a_node(mixed, "bare", None)
    agent(mixed, "agent-bare", "bare")
    agent(mixed, "agent-lost", None)  # no kube_pod_info of it: its node isn't known
    agent(mixed, "agent-gone", "gone")  # on a node kube_node_labels never had
    types = usage.pod_types(_prom(mixed, SHOP), SHOP, 7 * 86400, NOW)
    (row,) = usage.usage(_prom(mixed, SHOP), SHOP, 7 * 86400, NOW, types=types).rows
    assert sorted(row.groups) == ["", "m5.4xlarge", "m5.large"]
    assert row.groups[""].pods == {"agent-bare"}
    grouped = set().union(*(g.pods for g in row.groups.values()))
    assert row.pods - grouped == {"agent-lost", "agent-gone"}  # pooled, in no group


def test_what_hides_the_containers_demand_hides_every_groups_and_pods_are_counted_alike() -> None:
    fake = fleet()
    periods = {"namespace": "shop", "container": "app"}
    for pod in ("agent-s1", "agent-big"):
        fake.add("container_cpu_cfs_periods_total", 1000.0, pod=pod, **periods)
    fake.add("container_cpu_cfs_throttled_periods_total", 900.0, pod="agent-big", **periods)
    oom = "kube_pod_container_status_last_terminated_reason"
    fake.add(oom, 1, namespace="shop", pod="agent-big", container="app", reason="OOMKilled")
    for pod in ("agent-s1", "agent-s2", "agent-big"):
        fake.add("kube_pod_status_phase", [1.0] * N, namespace="shop", pod=pod, phase="Running")
    types = usage.pod_types(_prom(fake, SHOP), SHOP, 7 * 86400, NOW)
    (row,) = usage.usage(_prom(fake, SHOP), SHOP, 7 * 86400, NOW, types=types).rows
    assert row.throttled == 0.45 and row.oom_killed_pods == 1
    for g in row.groups.values():
        assert (g.throttled, g.oom_killed_pods) == (0.45, 1)
    # from the Running phase, as the row's: the groups' pods add up to the row's
    assert sum(len(g.running) for g in row.groups.values()) == len(row.running) == 3 * N


# --- a run ------------------------------------------------------------------------------


def test_only_a_cluster_that_runs_an_actuator_gets_groups(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only an Actuator can size a DaemonSet's pods per node: elsewhere the read isn't
    even made."""
    fake = fleet()
    (row,) = collected(fake, monkeypatch).by_window[7 * 86400]
    assert sorted(row[1].groups) == ["m5.4xlarge", "m5.large"]
    assert any("kube_node_labels" in q and "last_over_time" in q for q in fake.queries)
    fake.queries.clear()
    (plain,) = collected(fake, monkeypatch, prometheus_contract()).by_window[7 * 86400]
    assert plain[1].groups == {}
    assert not any("last_over_time(kube_node_labels" in q for q in fake.queries)


def test_a_shared_store_without_labels_or_a_failed_read_gives_no_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = fleet()
    c = acting()
    c._shared_stores.add("test")
    found = collected(fake, monkeypatch, c)
    assert found.by_window[7 * 86400][0][1].groups == {}
    assert found.rooms["test"]["problem"] == SHARED_STORE and found.clusters == {"test": None}
    assert not any("last_over_time(kube_node_labels" in q for q in fake.queries)

    def boom(*a: Any) -> Any:
        raise UsageError("HTTP 500 from prometheus")

    monkeypatch.setattr(usage, "pod_types", boom)
    failed = collected(fake, monkeypatch)
    assert failed.clusters == {"test": None}  # the cluster is read all the same
    assert failed.by_window[7 * 86400][0][1].groups == {}


# --- the Sizings ------------------------------------------------------------------------


def test_each_type_is_sized_from_its_own_pods_beside_the_pooled_sizing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (row,) = rows_of(fleet(), monkeypatch)
    small, big = sorted(row["groups"], key=lambda g: g["pods"], reverse=True)
    assert (small["type"], small["pods"], small["state"]) == ("m5.large", 2.0, "ready")
    assert (big["type"], big["pods"], big["state"]) == ("m5.4xlarge", 1.0, "ready")
    # each against the requests of its own pods, not the row's largest
    assert (small["cpu"]["current"], big["cpu"]["current"], row["cpu"]["current"]) == (
        0.5,
        2.0,
        2.0,
    )
    assert small["cpu"]["action"] == big["cpu"]["action"] == "lower"
    assert small["cpu"]["target"] < 0.1 < 1.0 < big["cpu"]["target"]
    assert small["memory"]["target"] < 128 * MI < 900 * MI < big["memory"]["target"]
    # memory is a peak: the pooled Sizing is the largest type's
    assert row["memory"]["target"] == big["memory"]["target"]
    # one busy node of three: the pooled cpu Sizing fits it, and no risk says otherwise
    assert "type_needs_more_cpu" not in [r["kind"] for r in row["risks"]]
    assert row["groups"] == sorted(row["groups"], key=lambda g: g["type"])


def test_waste_stays_on_the_pooled_sizing_until_an_actuator_applies_the_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with_groups = rows_of(fleet(), monkeypatch)[0]
    plain = rows_of(fleet(), monkeypatch, prometheus_contract())[0]
    assert "groups" in with_groups and "groups" not in plain
    for key in ("waste", "cpu", "memory", "state", "pods", "risks", "requests"):
        assert with_groups[key] == plain[key], key


def test_the_pooled_cpu_sizing_can_sit_below_a_busy_minority() -> None:
    """One busy node among thirty: CPU is a percentile over every pod's samples, and a
    thirtieth of them is above the 95th. The pooled Sizing follows the quiet ones; only
    the busy node's own type shows what it needs. Memory, a peak, fits the busiest."""
    fake = FakePrometheus()
    for i in range(29):
        a_node(fake, f"s{i}", "m5.large")
        agent(fake, f"agent-s{i}", f"s{i}")
    a_node(fake, "big", "m5.4xlarge")
    agent(fake, "agent-big", "big", cores=1.0, peak=900.0 * MI)
    types = usage.pod_types(_prom(fake, SHOP), SHOP, 7 * 86400, NOW)
    (u,) = usage.usage(_prom(fake, SHOP), SHOP, 7 * 86400, NOW, types=types).rows
    c = acting()
    (row,) = sizing.rows(c, {7 * 86400: [("test", u)]}, NOW)
    big = next(g for g in row["groups"] if g["type"] == "m5.4xlarge")
    assert row["cpu"]["target"] < 0.1 and big["cpu"]["target"] > 1.0  # an order of magnitude
    assert row["memory"]["target"] == big["memory"]["target"]
    # and the row says so, as a risk: this is the Sizing that gets applied
    assert [r for r in row["risks"] if r["kind"] == "type_needs_more_cpu"] == [
        {
            "kind": "type_needs_more_cpu",
            "text": "its one cpu Sizing of 58m for every node is below what its pods need on "
            "m5.4xlarge (1150m)",
        }
    ]


def typed(kind: str, target: float | None, state: str = "ready") -> dict[str, Any]:
    g: dict[str, Any] = {"type": kind, "state": state, "pods": 1.0}
    return g if target is None else g | {"cpu": {"target": target}}


def test_the_risk_names_the_types_that_need_more_than_a_change_would_make() -> None:
    policy = sizing.PRODUCTION
    least = max(policy.threshold * 1.0, sizing.MIN_CHANGE["cpu"])

    def short(*groups: dict[str, Any], pooled: float | None = 1.0) -> Any:
        row: dict[str, Any] = {"groups": list(groups)}
        if pooled is not None:
            row["cpu"] = {"target": pooled}
        got = sizing._short_of(row, policy)
        return got and got["text"]

    assert short(typed("a", 1.0), typed("b", 0.2)) is None  # none needs more
    assert short(typed("a", 1.0 + least)) is None  # no more than a change it wouldn't make
    assert "on a (1500m)" in short(typed("a", 1.5), typed("b", 0.2))
    assert short(typed("a", 1.5, "learning"), typed("b", None)) is None  # no Sizing of its own
    assert short(typed("a", 1.5), pooled=None) is None  # the row has no cpu Sizing
    assert short(typed("", 1.5)).endswith("on nodes with no instance type (1500m)")
    many = [typed(f"t{i}", 2.0) for i in range(5)]
    assert short(*many).endswith("on t0 (2000m), t1 (2000m), t2 (2000m) and 2 more")
    tiny = sizing._short_of({"cpu": {"target": 0.001}, "groups": [typed("a", 0.010)]}, policy)
    assert tiny is None  # under the smallest change counted, 10m


def test_a_type_is_sized_under_its_containers_own_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    lean = {"policies": {"lean": {"base": "production", "cpu": {"headroom": 1.0}}}}
    data = acting().model_dump()
    data["sizing"] = {**lean, "rules": [{"name": "agent", "policy": "lean"}]}
    c = contracts.build(data, secrets={"act": SecretStr("a" * 40)})
    (row,) = rows_of(fleet(), monkeypatch, c)
    (plain,) = rows_of(fleet(), monkeypatch)
    big = next(g for g in row["groups"] if g["type"] == "m5.4xlarge")
    usual = next(g for g in plain["groups"] if g["type"] == "m5.4xlarge")
    assert row["policy"] == "lean" and big["cpu"]["target"] == 2.0 > usual["cpu"]["target"]


def test_a_type_with_too_little_history_is_still_learning(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = fleet()
    a_node(fake, "new", "c5.large")
    agent(fake, "agent-new", "new", samples=30)  # an hour of it
    (row,) = rows_of(fake, monkeypatch)
    young = next(g for g in row["groups"] if g["type"] == "c5.large")
    assert young["state"] == "learning" and "cpu" not in young and young["history_h"] < 24
    assert row["state"] == "ready"
    assert [g["state"] for g in row["groups"] if g["type"] != "c5.large"] == ["ready", "ready"]


def test_a_throttled_containers_groups_are_not_lowered(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = fleet()
    periods = {"namespace": "shop", "container": "app", "pod": "agent-big"}
    fake.add("container_cpu_cfs_periods_total", 1000.0, **periods)
    fake.add("container_cpu_cfs_throttled_periods_total", 900.0, **periods)
    (row,) = rows_of(fake, monkeypatch)
    assert row["cpu"]["action"] == "keep"
    assert [g["cpu"]["action"] for g in row["groups"]] == ["keep", "keep"]
    # its memory is still sized: 256Mi asked, the busy one uses 900Mi, the others 64Mi
    assert [g["memory"]["action"] for g in row["groups"]] == ["raise", "lower"]


@pytest.mark.anyio
async def test_a_stored_run_keeps_the_groups_and_the_workloads_page_gets_them(
    store: Store,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """From Prometheus to the page: one run stores the row with its groups, and the
    Workloads report and the Hub MCP's container carry them."""
    from squero import hubmcp

    monkeypatch.setattr(usage, "Prom", lambda t: _prom(fleet(), t))
    c = acting()
    assert await sizing.store_run(store, c, NOW) is not None
    shown = await webapp.workloads_of(store, c, NOW)
    (w,) = shown.workloads
    (container,) = w.containers
    assert [(g.type, g.pods) for g in container.groups] == [("m5.4xlarge", 1.0), ("m5.large", 2.0)]
    told = hubmcp._workload(w, {})["containers"][0]
    assert told["more_types"] == 0 and len(told["by_instance_type"]) == 2
    assert told["by_instance_type"][1].startswith("  on m5.large (2 pods): cpu lower 500m -> ")


def test_each_contract_groups_only_its_own_pods(
    two_contracts: dict[str, contracts.Contract],
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both demo Contracts end to end on one Prometheus, each cluster running an Actuator,
    a DaemonSet `agent` in every namespace of each on the same two types of node: a run
    stores, per type, its own Contract's pods and no other's."""
    import asyncio

    from squero.store import connect
    from tests.test_sizing_store import PROM

    fake = FakePrometheus()
    a_node(fake, "s1", "m5.large")
    a_node(fake, "big", "m5.4xlarge")
    spaces = {
        cid: [ns for cl in c.clusters for ns in cl.namespaces] for cid, c in two_contracts.items()
    }
    for i, found in enumerate(spaces.values()):
        for ns in found:
            # the second Contract has two pods of it on the small node, the first one
            for j in range(i + 1):
                agent(fake, f"agent-s{j}", "s1", ns=ns)
            agent(fake, "agent-big", "big", cores=1.0, ns=ns)
    monkeypatch.setattr(usage, "Prom", lambda t: _prom(fake, t))

    async def go() -> dict[str, list[tuple[str, list[tuple[str, float]]]]]:
        async with connect(database_url) as pool:
            s = Store(pool)
            await s.migrate()
            out = {}
            for c in two_contracts.values():
                acts = [cl.model_copy(update={"actuator_token_secret": "act"}) for cl in c.clusters]
                c = c.model_copy(update={"integrations": [*c.integrations, PROM], "clusters": acts})
                c._secrets["act"] = SecretStr("a" * 40)
                assert await sizing.store_run(s, c, NOW) is not None
                rows, _ = await s.latest_sizings(c.id, {cl.name for cl in c.clusters})
                out[c.id] = [
                    (r["namespace"], [(g["type"], g["pods"]) for g in r["groups"]]) for r in rows
                ]
            return out

    stored = asyncio.run(go())
    for i, (cid, found) in enumerate(spaces.items()):
        want = [("m5.4xlarge", 1.0), ("m5.large", float(i + 1))]
        assert sorted(stored[cid]) == [(ns, want) for ns in sorted(found)]


# --- shown ------------------------------------------------------------------------------


def test_the_groups_read_as_lines_under_their_row(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = fleet()
    a_node(fake, "new", None)
    agent(fake, "agent-new", "new", samples=30)
    (row,) = rows_of(fake, monkeypatch)
    lines = sizing.group_lines(row)
    assert lines[0].startswith("  on nodes with no instance type (1 pods): learning (")
    big, small = lines[1], lines[2]
    assert big.startswith("  on m5.4xlarge (1 pods): cpu lower 2000m -> ")
    assert small.startswith("  on m5.large (2 pods): cpu lower 500m -> ")
    assert ", memory lower 256Mi -> " in small
    assert sizing.group_lines({"groups": []}) == [] and sizing.group_lines({}) == []


def test_the_api_and_the_hub_mcp_give_a_containers_groups_capped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from squero import hubmcp

    (row,) = rows_of(fleet(), monkeypatch)
    c = webapp.Container.model_validate(row)
    assert [(g.type, g.pods, g.state) for g in c.groups] == [
        ("m5.4xlarge", 1.0, "ready"),
        ("m5.large", 2.0, "ready"),
    ]
    assert c.groups[1].cpu and c.groups[1].cpu.current == 0.5
    shown = hubmcp._by_type(c)
    assert shown["more_types"] == 0 and shown["by_instance_type"] == sizing.group_lines(row)
    many = [{**row["groups"][0], "type": f"t{i:02}"} for i in range(12)]
    crowded = hubmcp._by_type(webapp.Container.model_validate({**row, "groups": many}))
    assert len(crowded["by_instance_type"]) == 10 and crowded["more_types"] == 2
    assert hubmcp._by_type(webapp.Container.model_validate({**row, "groups": []})) == {}
