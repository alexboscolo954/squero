"""GPU nodes (slice 17.5a): GPUs a third resource in the nodes that could go, a GPU pod
landing only where as many are free and of the same product where both nodes say theirs;
a node that could go priced with its GPUs; Unrequested GPUs per cluster, priced, in the
digest. GPUs are read apart from cpu and memory, which every reader of those iterates."""

from typing import Any

import pytest

from squero import blockers, contracts, sizing
from squero.usage import POD_REQUESTS, Target
from tests.fake_prometheus import FakePrometheus
from tests.test_could_go import ANYWHERE, go, read_of
from tests.test_could_go import node as box
from tests.test_could_go import pod as one
from tests.test_nodes import node as kube_node
from tests.test_nodes import pod as kube_pod
from tests.test_nodes import read as rooms_of
from tests.test_sizing_store import NOW, prometheus_contract

GI = 2.0**30
T4, A100 = "Tesla-T4", "A100-SXM4-40GB"


def gpu_pod(name: str, gpus: float = 1.0, **kw: Any) -> dict[str, Any]:
    return {**one(name, 0.5, 1.0, **kw), "gpus": gpus}


def gpu_box(name: str, *own: dict[str, Any], gpus: float = 1.0, product: str | None = T4,
            busy: float | None = None, **kw: Any) -> dict[str, Any]:  # fmt: skip
    """A node with `gpus` GPUs, its pods asking theirs (or `busy` of them asked)."""
    asked = sum(p.get("gpus", 0.0) for p in own) if busy is None else busy
    g = {"allocatable": gpus, "capacity": gpus, "requests": asked, "product": product}
    return {**box(name, *own, **kw), "gpus": g}


# --- the fit ---------------------------------------------------------------------------


def test_a_gpu_pod_needs_as_many_gpus_free_where_it_lands() -> None:
    a = gpu_box("a", gpu_pod("a1"))
    assert go(a, gpu_box("b"), b="blocked") == ["a"]
    assert go(a, gpu_box("b", busy=1.0), b="blocked") == []  # its GPU is asked for
    assert go(a, box("b"), b="blocked") == []  # a CPU node: no GPU at all
    assert go(gpu_box("a", gpu_pod("a1", 2.0), gpus=2), gpu_box("b"), b="blocked") == []


def test_a_gpu_taken_by_one_pod_is_not_free_for_the_next() -> None:
    a, c = gpu_box("a", gpu_pod("a1")), gpu_box("c", gpu_pod("c1"))
    assert go(a, gpu_box("b"), c, b="blocked") == ["a"]


@pytest.mark.parametrize(
    ("there", "goes"),
    [(T4, True), (A100, False), (None, True)],  # the same, another, one not saying
)
def test_a_gpu_pod_lands_on_the_same_product_where_both_nodes_say_theirs(
    there: str | None, goes: bool
) -> None:
    got = go(gpu_box("a", gpu_pod("a1")), gpu_box("b", product=there), b="blocked")
    assert got == (["a"] if goes else [])
    unsaid = go(gpu_box("a", gpu_pod("a1"), product=None), gpu_box("b", product=A100), b="blocked")
    assert unsaid == ["a"]


def test_a_pod_on_a_shared_gpu_or_a_mig_profile_never_moves() -> None:
    a = gpu_box("a", {**one("a1", 0.5, 1.0), "gpus_unread": True})
    assert go(a, gpu_box("b", gpus=8), b="blocked") == []


def test_a_gpu_pod_moved_twice_keeps_the_product_of_the_node_it_first_left() -> None:
    """a's T4 pod lands on b, which says no product; b may go only if that pod finds a
    T4 again: c is an A100."""
    a = gpu_box("a", gpu_pod("a1"), product=T4)
    b = gpu_box("b", one("b1", 1.0, 2.0), gpus=1, product=None)
    c = gpu_box("c", gpus=2, product=A100)
    assert go(a, b, c, c="blocked") == ["a"]


def test_without_gpus_a_read_goes_as_before() -> None:  # a read from before 17.5a
    assert go(box("a", one("a1", 0.5)), box("b"), b="blocked") == ["a"]


def test_after_the_sizings_a_gpu_pod_still_asks_its_gpus() -> None:
    a = gpu_box("a", gpu_pod("a1", workload=("Deployment", "api")))
    read = read_of(a, box("b"))
    row = {"cluster": "test", "namespace": "shop", "kind": "Deployment", "name": "api",
           "container": "app", "state": "ready", "cpu": {"target": 0.1},
           "memory": {"target": 0.1 * GI}}  # fmt: skip
    later = blockers.after(read, [row])
    assert later["nodes"][0]["own"][0]["gpus"] == 1.0
    verdict = {"a": {"state": "clear"}, "b": {"state": "blocked"}}
    assert blockers.could_go(later, verdict, ANYWHERE) == []  # b has no GPU


# --- the read --------------------------------------------------------------------------


def gpu_cluster(fake: FakePrometheus, **scope: str) -> None:
    kube_node(fake, "g1", **scope)
    for metric in ("kube_node_status_allocatable", "kube_node_status_capacity"):
        fake.add(metric, 4, node="g1", resource="nvidia_com_gpu", **scope)
    fake.add("kube_node_status_allocatable", 110, node="g1", resource="pods", **scope)
    fake.add("kube_node_labels", 1, node="g1", label_nvidia_com_gpu_product=T4, **scope)


def test_the_read_keeps_gpus_apart_from_cpu_and_memory() -> None:
    fake = FakePrometheus()
    gpu_cluster(fake)
    kube_pod(fake, "trainer", on="g1")
    kube_pod(fake, "sliced", on="g1")
    who = {"node": "g1", "container": "app", "namespace": "shop"}
    fake.add(POD_REQUESTS, 2, resource="nvidia_com_gpu", pod="trainer", uid="uid-shop-trainer",
             **who)  # fmt: skip
    fake.add(POD_REQUESTS, 1, resource="nvidia_com_mig_1g_5gb", pod="sliced",
             uid="uid-shop-sliced", **who)  # fmt: skip
    (n,) = rooms_of(fake)["nodes"]
    assert n["gpus"] == {"allocatable": 4.0, "capacity": 4.0, "requests": 2.0, "product": T4}
    for key in ("allocatable", "requests", "daemonsets"):
        assert set(n[key]) == {"cpu", "memory"}, key
    pods = {p["pod"]: p for p in n["own"]}
    assert pods["trainer"]["gpus"] == 2.0 and "gpus_unread" not in pods["trainer"]
    assert pods["sliced"]["gpus_unread"] is True and "gpus" not in pods["sliced"]
    for p in pods.values():
        assert all(set(c) == {"name", "cpu", "memory"} for c in p["containers"])


def test_a_node_without_gpus_reads_as_before() -> None:
    fake = FakePrometheus()
    kube_node(fake, "n1")
    (n,) = rooms_of(fake)["nodes"]
    assert "gpus" not in n


def test_a_clusters_gpus_are_its_own_on_a_shared_store() -> None:
    """Isolation: two clusters on one store, told apart by their labels."""
    fake = FakePrometheus()
    gpu_cluster(fake, cluster="eu")
    kube_node(fake, "g1", cluster="us")
    fake.add("kube_node_status_allocatable", 8, node="g1", resource="nvidia_com_gpu",
             cluster="us")  # fmt: skip
    eu = Target("eu", "http://prometheus", None, {"cluster": "eu"}, ["shop"])
    (n,) = rooms_of(fake, eu)["nodes"]
    assert n["gpus"]["allocatable"] == 4.0


# --- priced and said ---------------------------------------------------------------------


def priced(gpu: float | None = 0.5) -> contracts.Contract:
    data = prometheus_contract().model_dump()
    data["prices"] = {"cpu": 0.04, "memory": 0.005, **({"gpu": gpu} if gpu is not None else {})}
    data["clusters"][0]["autoscaler"] = "karpenter"
    return contracts.build(data)


def report(c: contracts.Contract, *nodes: dict[str, Any]) -> dict[str, Any]:
    read = {**read_of(*nodes), "placement": None, "spot": None}
    (cluster,) = sizing.nodes_report({"test": read}, c, {}, NOW, [])
    return cluster


def test_a_node_that_could_go_is_priced_with_its_gpus(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(blockers, "verdicts", lambda read, *a: {
        n["node"]: {"state": "clear" if n["node"] == "a" else "blocked"} for n in read["nodes"]
    })  # fmt: skip
    monkeypatch.setattr(blockers, "fit", lambda *a: ANYWHERE)
    nodes = (gpu_box("a", gpu_pod("a1"), gpus=1), gpu_box("b", gpus=2))
    went = report(priced(0.5), *nodes)["could_go"]["now"]
    cpu_mem = sizing.monthly(priced(0.5).prices, cpu=4.1, memory=16.5 * GI)  # type: ignore[arg-type]
    assert went["nodes"] == ["a"] and went["gpus"] == 1.0
    assert went["cost"] == pytest.approx(cpu_mem + 0.5 * 730)
    assert report(priced(None), *nodes)["could_go"]["now"]["cost"] is None  # never a part
    plain = report(priced(None), box("a", one("a1", 0.5)), box("b"))["could_go"]["now"]
    assert plain["cost"] is not None and "gpus" not in plain


def test_gpu_pods_placed_by_count_alone_are_said(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(blockers, "verdicts", lambda read, *a: {
        n["node"]: {"state": "clear" if n["node"] == "a" else "blocked"} for n in read["nodes"]
    })  # fmt: skip
    monkeypatch.setattr(blockers, "fit", lambda *a: ANYWHERE)
    bare = report(priced(), gpu_box("a", gpu_pod("a1"), product=None), gpu_box("b"))
    assert "GPU model not checked: 1 GPU nodes" in bare["could_go"]["gpu_note"]
    said = report(priced(), gpu_box("a", gpu_pod("a1")), gpu_box("b"))
    assert "gpu_note" not in said["could_go"]


def test_unrequested_gpus_are_counted_per_cluster_and_priced() -> None:
    nodes = (gpu_box("a", gpu_pod("a1"), gpus=4), gpu_box("b", gpus=2), gpu_box("c", busy=1.0))
    got = report(priced(0.5), *nodes)["unrequested_gpus"]
    assert got == {"gpus": 5.0, "nodes": 2, "cost": pytest.approx(5 * 0.5 * 730)}
    assert report(priced(None), *nodes)["unrequested_gpus"]["cost"] is None
    assert "unrequested_gpus" not in report(priced(), box("a"))


def test_the_digest_says_unrequested_gpus_only_when_there_are_some() -> None:
    def line(*clusters: dict[str, Any], currency: str | None = "USD") -> str | None:
        return sizing.unrequested_line(list(clusters), currency)

    u = {"unrequested_gpus": {"gpus": 3.0, "nodes": 2, "cost": 1095.0}}
    assert line(u) == "Unrequested GPUs: 3 on 2 nodes, 1,095.00 USD/month."
    unpriced = {"unrequested_gpus": {"gpus": 3.0, "nodes": 2, "cost": None}}
    assert (
        line(u, unpriced)
        == "Unrequested GPUs: 6 on 4 nodes (no GPU price: set `gpu` in its prices)."
    )
    assert line(unpriced, currency=None) == "Unrequested GPUs: 3 on 2 nodes."
    assert line({"unrequested_gpus": {"gpus": 0.0, "nodes": 0, "cost": 0.0}}, {}) is None
    now = {"total": {"cpu": 0.0, "memory": 0.0}, "clusters": {"test": {"at": NOW, "error": None}},
           "workloads": [], "learning": [], "excluded": []}  # fmt: skip
    digest = sizing.digest_text("acme", now, now, nodes=[{**u, "could_go": None}])
    assert "Unrequested GPUs: 3 on 2 nodes." in digest
