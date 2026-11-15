"""Spot placing, the Actuator's side (slice 16.2b): its webhook puts each new replica of a
Workload under a Spot ratio on its side, by ReplicaSet: on-demand first, required off
the spot nodes, then a preference for them; the count kept by a label, recounted each
minute and when a ReplicaSet is counted full; the deletion cost; the Events; its report
saying it steers."""

import copy
import json
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import ANY, MagicMock

import httpx
import jsonpatch
from kubernetes.client.exceptions import ApiException

from squero import actuator
from squero.actuator import CAPACITY, DELETION_COST, REQUIRED, Desired, Placing, Spotted, Spotting
from tests.test_actuator import new_pod
from tests.test_blockers import an_actuator as an_actuator_of_blockers

LABEL = "karpenter.sh/capacity-type=spot"
TAINT = "karpenter.sh/capacity-type=spot:NoSchedule"
API = Spotted(namespace="shop", kind="Deployment", name="api", ratio=0.7, on_demand=2)
OFF = {"key": "karpenter.sh/capacity-type", "operator": "NotIn", "values": ["spot"]}
PREFER = {
    "weight": 100,
    "preference": {
        "matchExpressions": [
            {"key": "karpenter.sh/capacity-type", "operator": "In", "values": ["spot"]}
        ]
    },
}
PREFERRED = "preferredDuringSchedulingIgnoredDuringExecution"
ON = {"key": "karpenter.sh/capacity-type", "operator": "In", "values": ["spot"]}
TERM_ON = {"matchExpressions": [ON]}


def spotting(*workloads: Spotted, taint: str | None = None, nodes: Any = ("s1",)) -> Spotting:
    return Spotting(label=LABEL, taint=taint, workloads=list(workloads), nodes=list(nodes))


def an_actuator(spot: Spotting | None = None, replicas: Any = 10) -> actuator.Actuator:
    http = httpx.Client(
        base_url="http://hub", transport=httpx.MockTransport(lambda r: httpx.Response(204))
    )
    a = actuator.Actuator(http, MagicMock(), MagicMock(), "squero-actuator")
    a.spot = spot
    rs = a.apps.read_namespaced_replica_set
    if isinstance(replicas, list):
        rs.side_effect = [SimpleNamespace(spec=SimpleNamespace(replicas=r)) for r in replicas]
    else:
        rs.return_value = SimpleNamespace(spec=SimpleNamespace(replicas=replicas))
    a.core.list_namespaced_pod.return_value = SimpleNamespace(items=[])
    return a


def admitted(
    a: actuator.Actuator, review: dict[str, Any] | None = None, **kw: Any
) -> dict[str, Any]:
    """The new pod as the webhook's patch leaves it."""
    req = (review or new_pod())["request"]
    ops = actuator.mutation(req, {}, "squero-actuator", steering=a.steer_pod, **kw)
    return jsonpatch.apply_patch(copy.deepcopy(req["object"]), ops)  # type: ignore[no-any-return]


def side(pod: dict[str, Any]) -> str | None:
    return (pod["metadata"].get("labels") or {}).get(CAPACITY)


def kpod(name: str, node: str | None = None, label: str | None = None, rs: str = "api-5d8f",
         phase: str = "Running", gone: bool = False) -> Any:  # fmt: skip
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            deletion_timestamp="now" if gone else None,
            labels={"app": "api", "pod-template-hash": "5d8f"}
            | ({CAPACITY: label} if label else {}),
            owner_references=[SimpleNamespace(kind="ReplicaSet", name=rs)],
        ),
        spec=SimpleNamespace(node_name=node),
        status=SimpleNamespace(phase=phase),
    )


# --- the patch --------------------------------------------------------------------------


def test_an_on_demand_replica_is_required_off_spot_nodes_and_deleted_last() -> None:
    a = an_actuator(spotting(API))
    pod = admitted(a)
    assert side(pod) == "on-demand"
    assert pod["spec"]["affinity"] == {
        "nodeAffinity": {REQUIRED: {"nodeSelectorTerms": [{"matchExpressions": [OFF]}]}}
    }
    assert pod["metadata"]["annotations"] == {DELETION_COST: "1"}
    assert "tolerations" not in pod["spec"]


def test_a_spot_replica_prefers_the_spot_nodes_and_tolerates_their_taint() -> None:
    a = an_actuator(spotting(API, taint=TAINT))
    a.spot_counts["shop/api-5d8f"] = {"replicas": 10, "on-demand": 3, "spot": 0}
    pod = admitted(a)
    assert side(pod) == "spot"
    assert pod["spec"]["affinity"] == {"nodeAffinity": {PREFERRED: [PREFER]}}
    assert pod["spec"]["tolerations"] == [
        {
            "key": "karpenter.sh/capacity-type",
            "operator": "Equal",
            "value": "spot",
            "effect": "NoSchedule",
        }
    ]
    assert DELETION_COST not in (pod["metadata"].get("annotations") or {})


def test_the_requirement_goes_into_each_of_the_pods_own_terms() -> None:
    """Required terms are ORed: one beside them would loosen them."""
    zone = {"key": "zone", "operator": "In", "values": ["a"]}
    review = new_pod()
    review["request"]["object"]["spec"]["affinity"] = {
        "nodeAffinity": {REQUIRED: {"nodeSelectorTerms": [
            {"matchExpressions": [zone]},
            {"matchFields": [{"key": "metadata.name", "operator": "In", "values": ["n1"]}]},
        ]}}
    }  # fmt: skip
    pod = admitted(an_actuator(spotting(API)), review)
    terms = pod["spec"]["affinity"]["nodeAffinity"][REQUIRED]["nodeSelectorTerms"]
    assert terms[0]["matchExpressions"] == [zone, OFF]
    assert terms[1]["matchExpressions"] == [OFF] and "matchFields" in terms[1]


def test_a_pods_own_deletion_cost_and_labels_are_kept() -> None:
    review = new_pod()
    review["request"]["object"]["metadata"]["annotations"] = {DELETION_COST: "-5", "x": "y"}
    pod = admitted(an_actuator(spotting(API)), review)
    assert pod["metadata"]["annotations"] == {DELETION_COST: "-5", "x": "y"}
    assert pod["metadata"]["labels"]["app"] == "api" and side(pod) == "on-demand"
    bare = {"metadata": {}, "spec": {}}  # no labels, no annotations of its own
    pod = jsonpatch.apply_patch(bare, actuator.steered(bare, "on-demand", spotting(API)))
    assert pod["metadata"] == {
        "labels": {CAPACITY: "on-demand"},
        "annotations": {DELETION_COST: "1"},
    }
    terms = pod["spec"]["affinity"]["nodeAffinity"][REQUIRED]["nodeSelectorTerms"]
    assert terms == [{"matchExpressions": [OFF]}]


def test_a_pod_whose_spec_settles_its_side_is_left_alone() -> None:
    for spec in (
        {"nodeSelector": {"karpenter.sh/capacity-type": "on-demand"}},
        {"affinity": {"nodeAffinity": {REQUIRED: {"nodeSelectorTerms": [TERM_ON]}}}},
    ):  # fmt: skip
        review = new_pod()
        review["request"]["object"]["spec"] |= spec
        a = an_actuator(spotting(API))
        assert admitted(a, review) == review["request"]["object"]
        assert a.spot_counts == {}  # nor counted


def test_only_a_listed_workloads_pods_are_steered() -> None:
    plain = new_pod()["request"]["object"]
    assert admitted(an_actuator(None)) == plain
    other = Spotted(namespace="shop", kind="Deployment", name="web", ratio=0.7, on_demand=2)
    assert admitted(an_actuator(spotting(other))) == plain
    assert admitted(an_actuator(Spotting(label=LABEL, workloads=None))) == plain


def test_placement_and_spot_on_one_pod_build_on_each_other() -> None:
    """A Workload that blocks a node is placed (15.2b) and kept off spot (ratio 0)."""
    held = API.model_copy(update={"ratio": 0.0})
    a = an_actuator(spotting(held))
    pinned = Placing(label="squero.io/pinned=yes", taint="squero.io/pinned=yes:NoSchedule",
                     workloads=["shop/Deployment/api"])  # fmt: skip
    review = new_pod()
    req = review["request"]
    ops = actuator.mutation(
        req, {}, "x", placing=pinned, steering=a.steer_pod, boosts={"shop/Deployment/api/app": 2.0}
    )
    pod = jsonpatch.apply_patch(copy.deepcopy(req["object"]), ops)
    node = pod["spec"]["affinity"]["nodeAffinity"]
    assert node[PREFERRED][0]["preference"]["matchExpressions"][0]["key"] == "squero.io/pinned"
    assert node[REQUIRED]["nodeSelectorTerms"] == [{"matchExpressions": [OFF]}]
    assert pod["spec"]["tolerations"][0]["key"] == "squero.io/pinned"
    assert DELETION_COST in pod["metadata"]["annotations"]
    assert side(pod) == "on-demand"


# --- the count --------------------------------------------------------------------------


def sides_of(a: actuator.Actuator, n: int) -> list[str | None]:
    return [side(admitted(a)) for _ in range(n)]


def test_a_replica_set_fills_on_demand_first_then_spot() -> None:
    a = an_actuator(spotting(API), replicas=10)
    got = sides_of(a, 10)
    assert got == ["on-demand"] * 3 + ["spot"] * 7
    assert a.spot_counts["shop/api-5d8f"] == {"replicas": 10, "on-demand": 3, "spot": 7}
    a.apps.read_namespaced_replica_set.assert_called_with(
        "api-5d8f", "shop", _request_timeout=actuator.SPOT_BUDGET
    )


def test_a_ratio_of_0_keeps_every_replica_off_spot() -> None:
    a = an_actuator(spotting(API.model_copy(update={"ratio": 0.0})), replicas=4)
    assert sides_of(a, 4) == ["on-demand"] * 4


def test_replicas_growing_within_the_minute_are_read_again() -> None:
    """A rollout grows its new ReplicaSet in seconds: 3 read first, then 10."""
    a = an_actuator(spotting(API), replicas=[3, 10])
    assert sides_of(a, 10) == ["on-demand"] * 2 + ["spot"] + ["on-demand"] + ["spot"] * 6
    assert a.spot_counts["shop/api-5d8f"] == {"replicas": 10, "on-demand": 3, "spot": 7}


def test_a_replacement_in_a_full_replica_set_is_decided_by_a_recount() -> None:
    """One on-demand pod lost since the count: its replacement goes on-demand."""
    a = an_actuator(spotting(API), replicas=10)
    a.spot_counts["shop/api-5d8f"] = {"replicas": 10, "on-demand": 3, "spot": 7}
    left = [kpod(f"o{i}", label="on-demand") for i in range(2)] + [
        kpod(f"s{i}", label="spot") for i in range(7)
    ]
    left.append(kpod("lost", label="on-demand", gone=True))
    left.append(kpod("elsewhere", label="on-demand", rs="api-0000"))  # an older ReplicaSet's
    a.core.list_namespaced_pod.return_value = SimpleNamespace(items=left)
    assert side(admitted(a)) == "on-demand"
    a.core.list_namespaced_pod.assert_called_with(
        "shop", label_selector="pod-template-hash=5d8f", _request_timeout=ANY
    )
    assert a.spot_counts["shop/api-5d8f"] == {"replicas": 10, "on-demand": 3, "spot": 7}
    a.spot_counts["shop/api-5d8f"] = {"replicas": 10, "on-demand": 3, "spot": 6}
    a.core.list_namespaced_pod.reset_mock()
    assert side(admitted(a)) == "spot"  # not full: no list
    a.core.list_namespaced_pod.assert_not_called()


def test_a_read_that_fails_places_and_counts_nothing() -> None:
    a = an_actuator(spotting(API))
    a.apps.read_namespaced_replica_set.side_effect = ApiException(status=500)
    assert admitted(a) == new_pod()["request"]["object"] and a.spot_counts == {}
    a = an_actuator(spotting(API))
    a.core.list_namespaced_pod.side_effect = TimeoutError("slow")
    assert admitted(a) == new_pod()["request"]["object"] and a.spot_counts == {}


def test_a_dry_run_is_decided_but_never_counted() -> None:
    a = an_actuator(spotting(API))
    review = new_pod()
    review["request"]["dryRun"] = True
    assert side(admitted(a, review)) == "on-demand"
    assert a.spot_counts["shop/api-5d8f"] == {"replicas": 10, "on-demand": 0, "spot": 0}


def test_two_admissions_at_once_never_both_take_the_last_on_demand_place() -> None:
    a = an_actuator(spotting(API))
    a.spot_counts["shop/api-5d8f"] = {"replicas": 10, "on-demand": 2, "spot": 0}
    start, got = threading.Barrier(8), []

    def one() -> None:
        start.wait()
        got.append(side(admitted(a)))

    threads = [threading.Thread(target=one) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(got, key=str) == ["on-demand"] + ["spot"] * 7


def test_pods_count_by_their_label_else_their_node() -> None:
    pods = [
        kpod("a", node="s1", label="on-demand"),  # its label wins over its node
        kpod("b", node="s1"),  # placed before: by its node
        kpod("c", node="n1"),
        kpod("d"),  # Pending, no label: neither
        kpod("e", label="spot"),  # Pending, placed: its side
        kpod("f", node="n1", phase="Succeeded"),
        kpod("g", node="n1", gone=True),
    ]
    assert actuator.sides(pods, {"s1"}) == {"on-demand": 2, "spot": 2}
    assert actuator.sides(pods, None) == {"on-demand": 1, "spot": 1}  # nodes unknown


def test_each_minute_the_steered_replica_sets_are_counted_again() -> None:
    web = Spotted(namespace="web", kind="Deployment", name="front", ratio=0.5, on_demand=1)
    a = an_actuator(spotting(API, web))
    shop = [kpod("a", node="s1"), kpod("b", label="on-demand"), kpod("x", rs="other-1")]
    shop[2].metadata.labels = {"pod-template-hash": "1"}  # its Workload `other`: not steered
    a.spot_counts = {"shop/api-5d8f": {"replicas": 10, "on-demand": 9, "spot": 9},
                     "web/front-1": {"replicas": 4, "on-demand": 1, "spot": 1}}  # fmt: skip

    def listed(ns: str, **kw: Any) -> Any:
        if ns == "web":
            raise ApiException(status=403)
        return SimpleNamespace(items=shop)

    a.core.list_namespaced_pod.side_effect = listed
    a.recount()
    assert a.spot_counts == {
        "shop/api-5d8f": {"replicas": None, "on-demand": 1, "spot": 1},
        "web/front-1": {"replicas": 4, "on-demand": 1, "spot": 1},  # not listed: kept
    }
    a.spot = None
    a.recount()
    assert a.spot_counts == {}


# --- the Actuator -----------------------------------------------------------------------


def events(a: actuator.Actuator) -> list[tuple[str, str, str]]:
    a.say_placed()
    out = [(c.args[1].involved_object.name, c.args[1].reason, c.args[1].message)
           for c in a.core.create_namespaced_event.call_args_list]  # fmt: skip
    a.core.create_namespaced_event.reset_mock()
    return out


def test_a_workload_joining_or_leaving_the_list_is_said_on_it() -> None:
    a = an_actuator()
    held = Spotted(namespace="shop", kind="Deployment", name="web", ratio=0.0, on_demand=2)
    a.steer(spotting(API, held))
    assert events(a) == [
        ("api", "SpotPlaced", "Spot: 70% of its new replicas prefer the nodes labelled "
         "karpenter.sh/capacity-type=spot, at least 2 kept off them"),
        ("web", "SpotPlaced", "Spot: none of its new replicas on the nodes labelled "
         "karpenter.sh/capacity-type=spot, its pods blocking a node"),
    ]  # fmt: skip
    a.steer(Spotting(label=LABEL, workloads=None, nodes=None))  # not known now: kept
    assert events(a) == [] and a.spot and a.spot.workloads == [API, held]
    assert a.spot.nodes == ["s1"]
    a.steer(spotting(held, nodes=("s2",)))
    assert events(a) == [
        (
            "api",
            "SpotEnded",
            "Spot ended: its new replicas are placed as before; running ones keep theirs",
        )
    ]
    assert a.spot and a.spot.nodes == ["s2"]
    a.steer(None)
    assert [e[:2] for e in events(a)] == [("web", "SpotEnded")] and a.spot is None


def test_a_steered_workloads_pause_is_read_and_the_kill_switch_steers_nothing() -> None:
    a = an_actuator()
    noted = {actuator.PAUSE: "true"}
    paused = SimpleNamespace(metadata=SimpleNamespace(name="api", annotations=noted))
    a.apps.list_namespaced_deployment.return_value = SimpleNamespace(items=[paused])
    a.pull = lambda: Desired(kill_switch=False, sizings=[], spot=spotting(API))  # type: ignore[method-assign]
    a.step()
    assert "shop/Deployment/api" in a.paused and a.spot and a.spot.workloads == [API]
    assert actuator.mutation(new_pod()["request"], {}, "x", a.paused, steering=a.steer_pod) == []
    a.paused = frozenset()
    a.pull = lambda: Desired(kill_switch=True, sizings=[])  # type: ignore[method-assign]
    a.step()
    assert a.killed and a.spot and a.spot.workloads == [API]  # kept, not ended
    answer = actuator.admit(new_pod(), {}, a.killed, "x", steering=a.steer_pod)
    assert "patch" not in answer["response"]


def test_its_report_says_it_steers_only_with_spot_nodes_and_never_under_the_kill_switch() -> None:
    seen: list[httpx.Request] = []
    a = an_actuator_of_blockers([204, 204, 204, 204], seen)
    a.blocked(["shop"], 0.0)
    a.spot = Spotting(label=LABEL, workloads=None)  # its list not known yet: steers nothing
    a.blocked(["shop"], 10_000.0)
    a.spot = spotting(API)
    a.blocked(["shop"], 20_000.0)
    a.killed = True
    a.blocked(["shop"], 30_000.0)
    bodies = [json.loads(r.content) for r in seen]
    assert ["spotting" in b for b in bodies] == [False, False, True, False]


def test_the_webhook_over_tls_steers() -> None:
    import base64
    import ssl

    cert, key = actuator.certificate("localhost")
    a = an_actuator(spotting(API))
    a.spot_counts["shop/api-5d8f"] = {"replicas": 10, "on-demand": 3, "spot": 0}
    server = actuator.serve(a, cert, key, port=0)
    try:
        trusted = ssl.create_default_context(cadata=cert.decode())
        with httpx.Client(verify=trusted) as http:
            got = http.post(f"https://localhost:{server.server_address[1]}/mutate", json=new_pod())
    finally:
        server.shutdown()
    ops = json.loads(base64.b64decode(got.json()["response"]["patch"]))
    assert {
        "op": "add",
        "path": f"/metadata/labels/{CAPACITY.replace('/', '~1')}",
        "value": "spot",
    } in ops
    assert a.spot_counts["shop/api-5d8f"]["spot"] == 1


# --- review fixes -----------------------------------------------------------------------


def test_the_pod_as_its_own_patch_leaves_it_is_what_json_patch_gives() -> None:
    held = API.model_copy(update={"ratio": 0.0})
    a = an_actuator(spotting(held, taint=TAINT))
    pinned = Placing(label="squero.io/pinned=yes", taint="squero.io/pinned=yes:NoSchedule",
                     workloads=["shop/Deployment/api"])  # fmt: skip
    req = new_pod()["request"]
    ops = actuator.mutation(req, {}, "x", placing=pinned, boosts={"shop/Deployment/api/app": 2.0})
    assert actuator.added(req["object"], ops) == jsonpatch.apply_patch(
        copy.deepcopy(req["object"]), ops
    )
    more = ops + actuator.steered(actuator.added(req["object"], ops), "on-demand", a.spot)  # type: ignore[arg-type]
    assert actuator.added(req["object"], more) == jsonpatch.apply_patch(
        copy.deepcopy(req["object"]), more
    )
    assert (
        actuator.added(req["object"], []) == req["object"]
        and actuator.added(req["object"], []) is not req["object"]
    )


def test_admissions_listing_at_once_never_lay_a_stale_count_over_each_other() -> None:
    """Eight pods of a new ReplicaSet at once, each reading it and listing its pods:
    the first count found is taken, the others decide on the admissions since."""
    a = an_actuator(spotting(API), replicas=10)
    inside = threading.Barrier(8)

    def listed(*args: Any, **kw: Any) -> Any:
        inside.wait()  # every admission is between its two locks
        return SimpleNamespace(items=[])

    a.core.list_namespaced_pod.side_effect = listed
    got: list[str | None] = []
    threads = [threading.Thread(target=lambda: got.append(side(admitted(a)))) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(got, key=str) == ["on-demand"] * 3 + ["spot"] * 5
    assert a.spot_counts["shop/api-5d8f"] == {"replicas": 10, "on-demand": 3, "spot": 5}


def test_a_count_taken_while_the_webhook_counted_is_left_for_the_next_minute() -> None:
    a = an_actuator(spotting(API))
    a.spot_counts = {"shop/api-5d8f": {"replicas": 10, "on-demand": 3, "spot": 4}}

    def listed(ns: str, **kw: Any) -> Any:
        a.spot_seq["shop/api-5d8f"] = 1  # a pod admitted meanwhile
        return SimpleNamespace(items=[kpod("a", label="on-demand")])

    a.core.list_namespaced_pod.side_effect = listed
    a.recount()
    assert a.spot_counts == {"shop/api-5d8f": {"replicas": 10, "on-demand": 3, "spot": 4}}
    a.core.list_namespaced_pod.side_effect = None
    a.core.list_namespaced_pod.return_value = SimpleNamespace(items=[kpod("a", label="on-demand")])
    a.recount()  # nothing admitted this time: the count is taken
    assert a.spot_counts == {"shop/api-5d8f": {"replicas": None, "on-demand": 1, "spot": 0}}


def test_both_reads_share_one_budget_inside_the_webhooks_two_seconds(monkeypatch: Any) -> None:
    assert actuator.SPOT_BUDGET < 2
    clock = iter([0.0, actuator.SPOT_BUDGET - 0.05])
    monkeypatch.setattr(actuator.time, "monotonic", lambda: next(clock))
    a = an_actuator(spotting(API))
    assert admitted(a) == new_pod()["request"]["object"] and a.spot_counts == {}
    a.core.list_namespaced_pod.assert_not_called()  # no time left to list


def test_a_cluster_without_a_list_never_reaches_the_steering(monkeypatch: Any) -> None:
    import ssl

    cert, key = actuator.certificate("localhost")
    a = an_actuator(None)
    a.steer_pod = MagicMock(return_value=[])  # type: ignore[method-assign]
    server = actuator.serve(a, cert, key, port=0)
    try:
        trusted = ssl.create_default_context(cadata=cert.decode())
        with httpx.Client(verify=trusted) as http:
            for spot in (None, Spotting(label=LABEL, workloads=[])):
                a.spot = spot
                http.post(f"https://localhost:{server.server_address[1]}/mutate", json=new_pod())
            a.spot = spotting(API)
            http.post(f"https://localhost:{server.server_address[1]}/mutate", json=new_pod())
    finally:
        server.shutdown()
    assert a.steer_pod.call_count == 1  # type: ignore[attr-defined]
