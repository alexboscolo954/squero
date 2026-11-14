"""Nodes (slice 15.1a): each node's Room read from kube-state-metrics, the pods outside the
Contract's namespaces only as a sum and a count; the largest Room after DaemonSets as a
cap on Sizings that never lowers a request; stored per cluster and shown by the API."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from squero import contracts, sizing, usage
from squero.config import Settings
from squero.contracts import Contract
from squero.store import Store, connect
from squero.usage import POD_REQUESTS, SHARED_STORE, Target, UsageError
from tests.conftest import make_contract
from tests.fake_prometheus import FakePrometheus
from tests.test_sizing_policy import lab, run_show
from tests.test_sizing_store import (  # noqa: F401
    NOW,
    PROM,
    container,
    kinds,
    prometheus_contract,
    store,
)
from tests.test_usage import _prom, cpu, deployment, mem
from tests.test_webapp import VIEW, app_dir, hub  # noqa: F401

MI = 2**20
GI = 2**30
SHOP = Target("test", "http://prometheus", None, {}, ["shop"])
PHASES = ("Pending", "Running", "Succeeded", "Failed", "Unknown")


def node(
    fake: FakePrometheus, name: str, cores: float = 4.0, memory: float = 16.0 * GI, **labels: str
) -> None:
    for res, value in (("cpu", cores), ("memory", memory)):
        fake.add("kube_node_status_allocatable", value, node=name, resource=res, **labels)
    fake.add("kube_node_spec_unschedulable", 0, node=name, **labels)


def pod(
    fake: FakePrometheus,
    name: str,
    on: str = "n1",
    ns: str = "shop",
    cores: float | None = 0.5,
    memory: float | None = 1.0 * GI,
    by: str = "ReplicaSet",
    phase: str = "Running",
    **labels: str,
) -> None:
    """A pod as kube-state-metrics shows it: its info, a series per phase (1 for its
    own), and its container's requests, all with its uid."""
    who = {"namespace": ns, "pod": name, "uid": f"uid-{ns}-{name}", **labels}
    fake.add("kube_pod_info", 1, node=on, created_by_kind=by, **who)
    for p in PHASES:
        fake.add("kube_pod_status_phase", 1 if p == phase else 0, phase=p, **who)
    for res, value in (("cpu", cores), ("memory", memory)):
        if value is not None:
            fake.add(POD_REQUESTS, value, node=on, container="app", resource=res, **who)


def read(fake: FakePrometheus, t: Target = SHOP) -> dict[str, Any]:
    return usage.rooms(_prom(fake, t), t, NOW)


def cluster() -> FakePrometheus:
    """Two nodes. n1: the Contract's api and db, another tenant's pod, a DaemonSet's pod
    in kube-system. n2 (larger, tainted): one pod of the Contract's."""
    fake = FakePrometheus()
    node(fake, "n1")
    node(fake, "n2", 8.0, 32.0 * GI)
    fake.add("kube_node_spec_taint", 1, node="n2", key="gpu", value="true", effect="NoSchedule")
    fake.add("kube_node_spec_taint", 1, node="n2", key="spot", effect="PreferNoSchedule")
    fake.add("kube_node_labels", 1, node="n1", label_node_kubernetes_io_instance_type="m5.xlarge")
    pod(fake, "api-1")
    pod(fake, "db-0", cores=1.0, memory=4.0 * GI, by="StatefulSet")
    pod(fake, "ledger-1", ns="bank", cores=0.25, memory=0.5 * GI)
    pod(fake, "proxy-x", ns="kube-system", cores=0.1, memory=64.0 * MI, by="DaemonSet")
    pod(fake, "train-1", on="n2", cores=2.0, memory=8.0 * GI)
    return fake


# --- the read --------------------------------------------------------------------------


def test_each_node_is_read_with_what_its_room_is_made_of() -> None:
    got = read(cluster())
    assert got["problem"] is None
    n1, n2 = got["nodes"]
    assert n1 == {
        "node": "n1",
        "type": "m5.xlarge",
        "taints": [],
        "cordoned": False,
        "allocatable": {"cpu": 4.0, "memory": 16.0 * GI},
        "requests": {"cpu": 1.85, "memory": 5.5 * GI + 64 * MI},  # every pod on it
        "daemonsets": {"cpu": 0.1, "memory": 64.0 * MI},
        "pods": 4,
        "own": [
            {"namespace": "shop", "pod": "api-1", "cpu": 0.5, "memory": 1.0 * GI},
            {"namespace": "shop", "pod": "db-0", "cpu": 1.0, "memory": 4.0 * GI},
        ],
    }
    assert (n2["node"], n2["type"], n2["pods"]) == ("n2", "", 1)
    assert n2["taints"] == ["gpu=true:NoSchedule", "spot:PreferNoSchedule"]
    assert n2["daemonsets"] == {"cpu": 0.0, "memory": 0.0}
    assert n2["requests"] == {"cpu": 2.0, "memory": 8.0 * GI}


def test_a_pod_outside_the_contracts_namespaces_only_reaches_the_hub_in_a_sum() -> None:
    """Every pod series read without the Contract's namespaces is summed or counted by
    Prometheus per node: no name and no namespace of it comes back."""
    fake = cluster()
    seen: list[str] = []
    handler = fake.handler

    def spy(request: Any) -> Any:
        response = handler(request)
        seen.append(response.text)
        return response

    fake.handler = spy  # type: ignore[method-assign]
    got = read(fake)
    assert "ledger" not in json.dumps(got) and "bank" not in json.dumps(got)
    assert "proxy" not in json.dumps(got) and "kube-system" not in json.dumps(got)
    assert not any("ledger" in text or "bank" in text or "proxy" in text for text in seen)
    for q in fake.queries:
        if "kube_pod" in q and 'namespace=~"shop"' not in q:
            assert q.startswith(("sum by (node, exported_node", "count by (node, exported_node")), q
    assert sum('namespace=~"shop"' in q for q in fake.queries) == 2  # its own pods, by name


def test_only_running_and_pending_pods_hold_requests() -> None:
    """A finished pod's requests series stays until it is deleted, and holds nothing."""
    fake = FakePrometheus()
    node(fake, "n1")
    pod(fake, "api-1")
    pod(fake, "report-1-x", phase="Succeeded", by="Job")
    pod(fake, "crash-1", phase="Failed")
    pod(fake, "new-1", phase="Pending", cores=0.25)
    pod(fake, "old-x", ns="kube-system", phase="Succeeded", by="DaemonSet")
    (n1,) = read(fake)["nodes"]
    assert (n1["pods"], n1["requests"]["cpu"]) == (2, 0.75)
    assert [p["pod"] for p in n1["own"]] == ["api-1", "new-1"]
    assert n1["daemonsets"] == {"cpu": 0.0, "memory": 0.0}


def test_a_pod_asking_nothing_is_still_the_contracts_own_and_one_on_no_node_is_nowhere() -> None:
    fake = FakePrometheus()
    node(fake, "n1")
    pod(fake, "bare-1", cores=None, memory=None)
    pod(fake, "waiting-1", on="", phase="Pending")  # not scheduled yet
    (n1,) = read(fake)["nodes"]
    assert n1["own"] == [{"namespace": "shop", "pod": "bare-1", "cpu": 0.0, "memory": 0.0}]
    assert (n1["pods"], n1["requests"]) == (1, {"cpu": 0.0, "memory": 0.0})


def test_the_clusters_labels_scope_every_series_and_a_renamed_node_label_is_read() -> None:
    """A store with two clusters: only the Target's nodes and pods; and kube-state-metrics
    scraped without honor_labels has the node's name in exported_node."""
    fake = FakePrometheus()
    node(fake, "ksm", cluster="a", exported_node="a1")
    node(fake, "b1", 64.0, cluster="b")
    fake.add("kube_node_spec_taint", 1, node="b1", key="x", effect="NoSchedule", cluster="b")
    pod(fake, "api-1", on="ksm", cluster="a", exported_node="a1")
    pod(fake, "api-9", on="b1", cores=9.0, cluster="b")
    pod(fake, "sys-1", on="b1", ns="kube-system", by="DaemonSet", cluster="b")
    a = Target("test", "http://prometheus", None, {"cluster": "a"}, ["shop"])
    (n,) = read(fake, a)["nodes"]
    assert (n["node"], n["allocatable"]["cpu"], n["requests"]["cpu"], n["pods"]) == (
        "a1",
        4.0,
        0.5,
        1,
    )
    assert n["taints"] == [] and n["daemonsets"]["cpu"] == 0.0
    assert all('cluster="a"' in q for q in fake.queries)
    assert all(q.count('cluster="a"') == q.count("{") for q in fake.queries)  # every selector


def test_no_node_read_is_a_problem_and_a_cordoned_node_says_so() -> None:
    assert read(FakePrometheus()) == {
        "nodes": [],
        "problem": "no nodes read: kube_node_status_allocatable is missing",
    }
    fake = FakePrometheus()
    node(fake, "n1")
    fake.add("kube_node_spec_unschedulable", 1, node="n2")
    node(fake, "n2")
    assert [n["cordoned"] for n in read(fake)["nodes"]] == [False, True]
    lost = FakePrometheus()
    pod(lost, "api-1", on="gone")  # a pod on a node whose allocatable was not read
    (n,) = read(lost)["nodes"]
    assert (n["node"], n["allocatable"], n["requests"]["cpu"]) == ("gone", {}, 0.5)
    down = FakePrometheus(status=500)
    with pytest.raises(UsageError):
        read(down)


# --- the largest Room ------------------------------------------------------------------


def a_node(name: str, cores: float, gib: float, daemons: float = 0.0, **kw: Any) -> dict[str, Any]:
    n: dict[str, Any] = {"node": name, "type": "", "taints": [], "cordoned": False, "pods": 0}
    n |= {"allocatable": {"cpu": cores, "memory": gib * GI}, "own": []}
    n |= {"requests": {"cpu": 0.0, "memory": 0.0}}
    n |= {"daemonsets": {"cpu": daemons, "memory": daemons * GI}}
    return n | kw


def found(*nodes: dict[str, Any], problem: str | None = None) -> dict[str, Any]:
    return {"nodes": list(nodes), "problem": problem}


def test_the_largest_room_is_after_each_nodes_daemonsets_per_resource() -> None:
    """Not the largest node: the one with most left once its DaemonSets are in. CPU and
    memory may come from different nodes (generous, never strict), a tainted node
    counts, a cordoned one does not."""
    wide = a_node("wide", 8.0, 16.0, daemons=3.0)  # 5 cores, 13 GiB left
    tall = a_node("tall", 6.0, 32.0, daemons=0.5, taints=["gpu:NoSchedule"])  # 5.5, 31.5
    shut = a_node("shut", 64.0, 256.0, cordoned=True)
    assert sizing.largest_room(found(wide, tall, shut)) == {"cpu": 5.5, "memory": 31.5 * GI}
    assert sizing.largest_room(found(wide)) == {"cpu": 5.0, "memory": 13.0 * GI}


def test_half_a_read_is_no_ceiling() -> None:
    whole = a_node("n1", 4.0, 16.0)
    assert sizing.largest_room(None) is None
    assert sizing.largest_room(found()) is None
    assert sizing.largest_room(found(whole, problem="boom")) is None
    assert sizing.largest_room(found(a_node("shut", 4.0, 16.0, cordoned=True))) is None
    partial = a_node("n2", 64.0, 256.0, allocatable={"cpu": 64.0})  # its memory not read
    assert sizing.largest_room(found(whole, partial)) is None
    unknown = a_node("n3", 0.0, 0.0, allocatable={}, cordoned=True)  # even one cordoned
    assert sizing.largest_room(found(whole, unknown)) is None


# --- the cap ---------------------------------------------------------------------------

BUSY = {"cores": 3.0, "peak": 6.0 * GI, "request": {"cpu": 2.0, "memory": 4.0 * GI}}


def test_a_sizing_asks_no_more_than_the_largest_room() -> None:
    free = sizing.size(container(**BUSY), NOW)
    assert free.cpu and free.memory and free.cpu.target > 3.0 and free.memory.target > 6.0 * GI
    room = {"cpu": 2.5005, "memory": 5.0 * GI + 1000}
    s = sizing.size(container(**BUSY), NOW, room=room)
    assert s.cpu and s.memory
    # rounded down to a millicore and a MiB: the rounding up after it stays within
    assert (s.cpu.target, s.cpu.action) == (2.5, "raise")
    assert (s.memory.target, s.memory.action) == (5.0 * GI, "raise")
    assert [r.kind for r in s.risks] == ["needs_larger_node", "needs_larger_node"]
    assert s.risks[0].text == (
        f"cpu capped at 2500m, the most a node has room for after its DaemonSets: it needs "
        f"{sizing.show('cpu', free.cpu.target)}, so a larger node"
    )
    assert s.risks[1].text.startswith("memory capped at 5120Mi, the most a node has room for")


def test_room_enough_changes_nothing() -> None:
    free = sizing.size(container(**BUSY), NOW)
    assert free.cpu and free.memory
    exact = {"cpu": free.cpu.target, "memory": free.memory.target}
    assert sizing.size(container(**BUSY), NOW, room=exact) == free
    assert sizing.size(container(**BUSY), NOW, room={"cpu": 64.0, "memory": 256.0 * GI}) == free
    assert sizing.size(container(**BUSY), NOW, room={}) == free
    only = sizing.size(container(**BUSY), NOW, room={"cpu": 2.5})  # memory's Room unknown
    assert only.memory == free.memory and only.cpu and only.cpu.target == 2.5
    # needing exactly the Room is no risk: an idle container at the policy's floor of 10m
    idle = container(cores=0.0, request={})
    at_it = sizing.size(idle, NOW, room={"cpu": 0.01, "memory": 256.0 * GI})
    assert at_it.cpu and at_it.cpu.target == 0.01 and at_it.risks == []


def test_the_cap_never_lowers_a_request() -> None:
    """Its node cordoned, or gone from this read: the largest Room left is under what the
    container asks today. It keeps its request, with the risk; a Sizing that lowers
    anyway (it uses less) is not held up by the cap."""
    small = {"cpu": 1.0, "memory": 2.0 * GI}
    free_busy = sizing.size(container(**BUSY), NOW)
    assert free_busy.cpu
    s = sizing.size(container(**BUSY), NOW, room=small)
    assert s.cpu and s.memory
    assert (s.cpu.current, s.cpu.target, s.cpu.action) == (2.0, 2.0, "keep")
    assert (s.memory.target, s.memory.action) == (4.0 * GI, "keep")
    assert [r.kind for r in s.risks] == ["needs_larger_node", "needs_larger_node"]
    assert s.risks[0].text == (  # its request is kept; the Room named is the real one
        "cpu kept at 2000m, though no node has room for more than 1000m after its "
        f"DaemonSets: it needs {sizing.show('cpu', free_busy.cpu.target)}, so a larger node"
    )
    idle = container(request={"cpu": 2.0, "memory": 4.0 * GI})  # uses 100m and 80 Mi
    free = sizing.size(idle, NOW)
    assert free.cpu and free.cpu.action == "lower"
    assert sizing.size(idle, NOW, room=small) == free
    unset = sizing.size(container(**(BUSY | {"request": {}})), NOW, room=small)
    assert unset.cpu and (unset.cpu.target, unset.cpu.action) == (1.0, "set")


def test_a_limit_below_the_room_is_the_one_that_caps() -> None:
    u = container(**BUSY, limits={"cpu": 2.2, "memory": 4.5 * GI})
    s = sizing.size(u, NOW, room={"cpu": 2.5, "memory": 5.0 * GI})
    assert s.cpu and s.cpu.target == 2.2
    assert [r.kind for r in s.risks] == ["limit_below_need", "limit_below_need"]


# --- rows ------------------------------------------------------------------------------

ROOMS = {"test": found(a_node("n1", 4.0, 16.0, daemons=1.0))}  # 3 cores and 15 GiB left


def rows_with(*us: Any, rooms: dict[str, Any] | None = ROOMS) -> list[dict[str, Any]]:
    by_window = {7 * 86400: [("test", u) for u in us]}
    return sizing.rows(prometheus_contract(), by_window, NOW, None, rooms)


def test_a_run_caps_its_sizings_at_its_clusters_largest_room() -> None:
    (row,) = rows_with(container(**BUSY))
    assert row["cpu"]["target"] == 3.0 and "needs_larger_node" in kinds(row)
    assert row["memory"]["target"] > 6.0 * GI  # 15 GiB is room enough
    tight = {"test": found(a_node("n1", 2.0, 16.0, daemons=1.0))}  # 1 core: under its request
    (kept,) = rows_with(container(**BUSY), rooms=tight)
    assert kept["cpu"]["target"] == 2.0
    assert kinds(kept).count("needs_larger_node") == 1  # said once: a pod of one container
    for rooms in (None, {}, {"other": ROOMS["test"]}, {"test": found(problem="boom")}):
        (free,) = rows_with(container(**BUSY), rooms=rooms)
        assert free["cpu"]["target"] > 3.0 and "needs_larger_node" not in kinds(free)


def test_a_daemonsets_own_sizing_is_not_capped() -> None:
    """The Room after DaemonSets already leaves its own requests out."""
    u = container(**BUSY)
    u.workload = usage.Workload("shop", "DaemonSet", "agent")
    (row,) = rows_with(u)
    assert row["cpu"]["target"] > 3.0 and "needs_larger_node" not in kinds(row)


def test_containers_that_ask_more_together_than_a_node_has_say_so() -> None:
    """Two containers of one pod at 2 cores each, in 3 cores of Room: neither is capped,
    both carry the risk. Another Workload, and a pod of one container, do not."""
    half = {"cores": 1.7, "peak": 1.0 * GI, "request": {"cpu": 2.0, "memory": 2.0 * GI}}
    app, sidecar, alone = container(**half), container(**half), container(**half)
    sidecar.container = "sidecar"
    alone.workload = usage.Workload("shop", "Deployment", "web")
    got = {r["workload"].split("/", 2)[2]: r for r in rows_with(app, sidecar, alone)}
    both = (
        got["Deployment/api/app"]["cpu"]["target"] + got["Deployment/api/sidecar"]["cpu"]["target"]
    )
    assert both == 4.0
    text = (
        "its pod's containers ask 4000m of cpu together, more than the 3000m a node has "
        "room for after its DaemonSets: it needs a larger node"
    )
    want = [{"kind": "needs_larger_node", "text": text}]
    assert got["Deployment/api/app"]["risks"] == got["Deployment/api/sidecar"]["risks"] == want
    assert got["Deployment/web/app"]["risks"] == []
    fits = {"test": found(a_node("n1", 4.0, 16.0))}  # 4 cores: exactly enough
    assert all(r["risks"] == [] for r in rows_with(app, sidecar, rooms=fits))
    lazy = {"request": {"cpu": 2.0, "memory": 2.0 * GI}}  # asking 4 cores, sized far lower
    quiet, quieter = container(**lazy), container(**lazy)
    quieter.container = "sidecar"
    assert all("needs_larger_node" not in kinds(r) for r in rows_with(quiet, quieter))
    young = container(**half, hours=2)  # learning: its request counts as it is
    young.container = "sidecar"
    (ready, learning) = rows_with(app, young)
    assert kinds(ready) == ["needs_larger_node"]
    assert (learning["state"], learning["risks"]) == ("learning", [])  # no Sizing, no risk of one


# --- the Contract ----------------------------------------------------------------------


def test_a_cluster_names_its_autoscaler() -> None:
    data = make_contract().model_dump()
    assert contracts.build(data).clusters[0].autoscaler is None
    for name in ("cluster-autoscaler", "karpenter"):
        data["clusters"][0]["autoscaler"] = name
        assert contracts.build(data).clusters[0].autoscaler == name
    data["clusters"][0]["autoscaler"] = "descheduler"
    with pytest.raises(ValidationError):
        contracts.build(data)


# --- a run -----------------------------------------------------------------------------


def test_a_store_shared_with_another_contract_without_labels_reads_no_room(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard list prices have: node series carry no namespace. Not one of the Room's
    queries goes out, and a Contract without list prices is guarded too."""
    fake = cluster()
    lab(fake)
    monkeypatch.setattr(usage, "Prom", lambda t: _prom(fake, t))
    c = prometheus_contract()
    assert not c.list_prices
    alone = usage.collect(c, [86400], NOW)
    assert alone.rooms["test"]["problem"] is None and len(alone.rooms["test"]["nodes"]) == 2
    c._shared_stores.add("test")
    fake.queries.clear()
    shared = usage.collect(c, [86400], NOW)
    assert shared.rooms == {"test": {"nodes": [], "problem": SHARED_STORE}}
    assert shared.clusters == {"test": None}  # its Sizings are read as before
    assert not any("kube_node" in q or "kube_pod_info" in q for q in fake.queries)
    assert not any("on (uid)" in q or q.startswith(("sum by", "count by")) for q in fake.queries)
    assert sizing.largest_room(shared.rooms["test"]) is None
    data = c.model_dump()
    data["clusters"][0]["prometheus_labels"] = {"cluster": "a"}
    labelled = contracts.build(data)
    labelled._shared_stores.add("test")
    fake.queries.clear()
    scoped = usage.collect(labelled, [86400], NOW).rooms["test"]
    assert scoped["problem"] != SHARED_STORE  # its labels pick its own cluster's nodes
    assert any("kube_node_status_allocatable" in q and 'cluster="a"' in q for q in fake.queries)


def test_a_failed_read_of_the_nodes_stops_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakePrometheus()
    lab(fake)
    monkeypatch.setattr(usage, "Prom", lambda t: _prom(fake, t))

    def boom(*a: Any) -> Any:
        raise UsageError("HTTP 500 from prometheus")

    monkeypatch.setattr(usage, "rooms", boom)
    got = usage.collect(prometheus_contract(), [86400], NOW)
    assert got.clusters == {"test": None} and len(got.by_window[86400]) == 4
    assert got.rooms == {"test": {"nodes": [], "problem": "HTTP 500 from prometheus"}}


def test_show_caps_its_sizings_like_a_stored_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakePrometheus()
    (p,) = deployment(fake, "api", pods=1)
    cpu(fake, p, [i * 180.0 for i in range(1441)])  # 3 cores, a counter over two days
    mem(fake, p, [80 * MI] * 1441)
    node(fake, "n1", 2.0)
    out = run_show(monkeypatch, capsys, fake)
    assert "capped at 2000m, the most a node has room for" in out


@pytest.mark.anyio
async def test_a_stored_run_caps_its_sizings_and_keeps_the_nodes(
    store: Store,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakePrometheus()
    (p,) = deployment(fake, "api", pods=1)
    cpu(fake, p, [i * 180.0 for i in range(1441)])  # 3 cores
    mem(fake, p, [80 * MI] * 1441)
    node(fake, "n1", 2.0)
    monkeypatch.setattr(usage, "Prom", lambda t: _prom(fake, t))
    c = prometheus_contract()
    assert await sizing.store_run(store, c, NOW) is not None
    (row,), _ = await store.latest_sizings(c.id, {"test"})
    assert row["cpu"]["target"] == 2.0 and "needs_larger_node" in kinds(row)
    kept = await store.latest_rooms(c.id, {"test"})
    assert [n["node"] for n in kept["test"]["nodes"]] == ["n1"] and kept["test"]["at"] == NOW


@pytest.mark.anyio
async def test_a_clusters_nodes_are_replaced_by_each_run_and_kept_per_contract(
    store: Store,  # noqa: F811
) -> None:
    first, second = found(a_node("n1", 4.0, 16.0)), found(a_node("n2", 8.0, 32.0))
    await store.save_rooms("acme", "test", NOW, first)
    await store.save_rooms("acme", "edge", NOW, first)
    await store.save_rooms("bank", "test", NOW, found(problem="theirs"))
    await store.save_rooms("acme", "test", NOW + 3600, second)
    got = await store.latest_rooms("acme", {"test", "edge"})
    assert got == {"test": {"at": NOW + 3600, **second}, "edge": {"at": NOW, **first}}
    assert await store.latest_rooms("acme", {"edge"}) == {"edge": {"at": NOW, **first}}
    assert await store.latest_rooms("bank", {"test"}) == {
        "test": {"at": NOW, "nodes": [], "problem": "theirs"}
    }
    assert await store.latest_rooms("nobody", {"test"}) == {}


def test_each_demo_contract_stores_its_own_pods_and_the_others_only_in_a_sum(
    two_contracts: dict[str, Contract], database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both demo Contracts end to end on one cluster and one Prometheus, a pod named
    after its namespace in every namespace of each: a run stores its own Contract's pods
    by name and every other pod on the node as part of one sum and one count."""
    fake = FakePrometheus()
    node(fake, "n1", 64.0, 256.0 * GI)
    spaces = {
        cid: [ns for cl in c.clusters for ns in cl.namespaces] for cid, c in two_contracts.items()
    }
    everyone = [ns for found_ in spaces.values() for ns in found_]
    for ns in everyone:
        pod(fake, f"pod-of-{ns}", ns=ns)
    monkeypatch.setattr(usage, "Prom", lambda t: _prom(fake, t))

    async def go() -> dict[str, dict[str, Any]]:
        async with connect(database_url) as pool:
            s = Store(pool)
            await s.migrate()
            out = {}
            for c in two_contracts.values():
                c = c.model_copy(update={"integrations": [*c.integrations, PROM]})
                assert await sizing.store_run(s, c, NOW) is not None
                out[c.id] = await s.latest_rooms(c.id, {cl.name for cl in c.clusters})
            return out

    stored = asyncio.run(go())
    for cid, c in two_contracts.items():
        theirs = [ns for ns in everyone if ns not in spaces[cid]]
        assert theirs and set(stored[cid]) == {cl.name for cl in c.clusters}
        for cl in c.clusters:
            (n,) = stored[cid][cl.name]["nodes"]
            assert [p["namespace"] for p in n["own"]] == sorted(cl.namespaces)
            assert n["pods"] == len(everyone) and n["requests"]["cpu"] == 0.5 * len(everyone)
        text = json.dumps(stored[cid])
        assert not any(f"pod-of-{ns}" in text for ns in theirs)


# --- shown -----------------------------------------------------------------------------


def test_the_report_splits_each_nodes_requests_into_its_own_and_the_rest() -> None:
    data = make_contract(clusters={"test": ["shop"], "edge": ["shop"]}).model_dump()
    data["clusters"][0]["autoscaler"] = "karpenter"
    c = contracts.build(data)
    read_ = {"at": NOW, **read(cluster())}
    gone = {"at": NOW, **found(a_node("old", 1.0, 1.0))}
    (shown,) = sizing.nodes_report({"test": read_, "left": gone}, c)  # edge: never read
    assert (shown["cluster"], shown["at"], shown["autoscaler"]) == ("test", NOW, "karpenter")
    assert shown["problem"] is None
    assert shown["largest"] == {"cpu": 8.0, "memory": 32.0 * GI}  # n2, tainted, no DaemonSet
    n1, n2 = shown["nodes"]
    assert n1["mine"] == {"pods": 2, "cpu": 1.5, "memory": 5.0 * GI}
    assert n1["others"] == {"pods": 2, "cpu": pytest.approx(0.35), "memory": 0.5 * GI + 64 * MI}
    assert n1["room"] == {"cpu": pytest.approx(2.15), "memory": 16.0 * GI - 5.5 * GI - 64 * MI}
    assert n2["others"] == {"pods": 0, "cpu": 0.0, "memory": 0.0}
    refused = {"at": NOW, "nodes": [], "problem": SHARED_STORE}
    (none,) = sizing.nodes_report({"test": refused}, c)
    assert (none["problem"], none["largest"], none["nodes"]) == (SHARED_STORE, None, [])


def test_the_nodes_route_gives_the_contracts_own_clusters(
    settings: Settings,
    fake_cluster: Any,
    database_url: str,
    app_dir: Path,  # noqa: F811
) -> None:
    read_ = read(cluster())

    async def seed() -> None:
        async with connect(database_url) as pool:
            s = Store(pool)
            await s.migrate()
            await s.save_rooms("acme", "test", NOW, read_)
            await s.save_rooms("acme", "left", NOW, read_)  # no longer a cluster of its
            await s.save_rooms("bank", "test", NOW, found(a_node("theirs", 1.0, 1.0)))

    asyncio.run(seed())
    with hub(settings, fake_cluster, database_url) as c:
        body = c.get("/api/v1/contracts/acme/nodes", headers=VIEW).json()
        assert c.get("/api/v1/contracts/nobody/nodes", headers=VIEW).status_code == 404
    (shown,) = body["clusters"]
    assert (shown["cluster"], shown["at"], shown["autoscaler"]) == ("test", NOW, None)
    assert [n["node"] for n in shown["nodes"]] == ["n1", "n2"]
    assert shown["nodes"][0]["own"][0] == {
        "namespace": "shop",
        "pod": "api-1",
        "cpu": 0.5,
        "memory": 1.0 * GI,
    }
    assert shown["nodes"][0]["others"]["pods"] == 2 and "theirs" not in json.dumps(body)
