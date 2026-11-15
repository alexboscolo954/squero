"""Placement, the Actuator's side (slice 15.2b): its webhook gives a new pod of a placed
Workload a preference for the cluster's Pinned nodes, and their taint's toleration; it
reports which pods carry the preference and each pod's own Workload; and it says, on a
Workload, when its new pods start and stop being placed."""

import copy
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import httpx
import jsonpatch
from kubernetes import client as k

from squero import actuator, blockers
from squero.actuator import Desired, Placing
from tests.test_actuator import HELD, new_pod
from tests.test_blockers import an_actuator as an_actuator_of_blockers
from tests.test_blockers import apis, kpod

LABEL = "squero.io/pinned=yes"
API = "shop/Deployment/api"  # new_pod()'s Workload
TERM = {
    "weight": 100,
    "preference": {
        "matchExpressions": [{"key": "squero.io/pinned", "operator": "In", "values": ["yes"]}]
    },
}
PREFERRED = "preferredDuringSchedulingIgnoredDuringExecution"


def placing(*workloads: str, taint: str | None = None) -> Placing:
    return Placing(label=LABEL, taint=taint, workloads=list(workloads))


def applied(
    review: dict[str, Any], p: Placing | None, held: Any = None, **kw: Any
) -> dict[str, Any]:
    """The new pod as the webhook's patch leaves it."""
    req = review["request"]
    ops = actuator.mutation(req, {} if held is None else held, "squero-actuator", placing=p, **kw)
    return jsonpatch.apply_patch(copy.deepcopy(req["object"]), ops)  # type: ignore[no-any-return]


def with_spec(**spec: Any) -> dict[str, Any]:
    review = new_pod()
    review["request"]["object"]["spec"] |= spec
    return review


# --- the webhook ------------------------------------------------------------------------


def test_a_placed_workloads_new_pod_prefers_the_pinned_nodes() -> None:
    pod = applied(new_pod(), placing(API))
    assert pod["spec"]["affinity"] == {"nodeAffinity": {PREFERRED: [TERM]}}
    assert "tolerations" not in pod["spec"]  # no taint named
    assert applied(new_pod(), placing("shop/Deployment/web")) == new_pod()["request"]["object"]
    assert applied(new_pod(), None) == new_pod()["request"]["object"]
    unknown = Placing(label=LABEL, workloads=None)
    assert applied(new_pod(), unknown) == new_pod()["request"]["object"]


def test_the_preference_goes_after_the_pods_own_and_never_replaces_one() -> None:
    required = {
        "nodeSelectorTerms": [
            {"matchExpressions": [{"key": "zone", "operator": "In", "values": ["a"]}]}
        ]
    }
    own = {
        "weight": 5,
        "preference": {"matchExpressions": [{"key": "disk", "operator": "In", "values": ["ssd"]}]},
    }
    spread = {"podAntiAffinity": {"preferredDuringSchedulingIgnoredDuringExecution": []}}
    cases = [
        ({"affinity": spread}, spread | {"nodeAffinity": {PREFERRED: [TERM]}}),
        (
            {
                "affinity": {
                    "nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": required}
                }
            },
            {
                "nodeAffinity": {
                    "requiredDuringSchedulingIgnoredDuringExecution": required,
                    PREFERRED: [TERM],
                }
            },
        ),
        (
            {"affinity": {"nodeAffinity": {PREFERRED: [own]}}},
            {"nodeAffinity": {PREFERRED: [own, TERM]}},
        ),
    ]
    for spec, after in cases:
        assert applied(with_spec(**spec), placing(API))["spec"]["affinity"] == after
    # one that prefers that label already, by its own hand or a webhook's earlier answer
    mine = {
        "weight": 1,
        "preference": {"matchExpressions": [TERM["preference"]["matchExpressions"][0]]},
    }
    again = with_spec(affinity={"nodeAffinity": {PREFERRED: [mine]}})
    assert actuator.placed(again["request"]["object"], placing(API)) == []
    other = {
        "weight": 1,
        "preference": {
            "matchExpressions": [{"key": "squero.io/pinned", "operator": "In", "values": ["no"]}]
        },
    }
    elsewhere = with_spec(affinity={"nodeAffinity": {PREFERRED: [other]}})
    assert applied(elsewhere, placing(API))["spec"]["affinity"]["nodeAffinity"][PREFERRED] == [
        other,
        TERM,
    ]


def test_a_named_taint_is_tolerated_by_placed_pods_only() -> None:
    equal = {"key": "squero.io/pinned", "operator": "Equal", "value": "yes", "effect": "NoSchedule"}
    p = placing(API, taint="squero.io/pinned=yes:NoSchedule")
    assert applied(new_pod(), p)["spec"]["tolerations"] == [equal]
    exists = {"key": "pinned", "operator": "Exists", "effect": "NoExecute"}
    assert applied(new_pod(), placing(API, taint="pinned:NoExecute"))["spec"]["tolerations"] == [
        exists
    ]
    gpu = {"key": "gpu", "operator": "Exists", "effect": "NoSchedule"}
    assert applied(with_spec(tolerations=[gpu]), p)["spec"]["tolerations"] == [gpu, equal]
    already = with_spec(tolerations=[equal | {"tolerationSeconds": 30}])
    assert "tolerations" not in str(actuator.placed(already["request"]["object"], p))
    other = placing("shop/Deployment/web", taint="squero.io/pinned=yes:NoSchedule")
    assert "tolerations" not in applied(new_pod(), other)["spec"]


def test_placement_rides_with_the_resize_and_stops_where_the_webhook_does() -> None:
    pod = applied(new_pod(), placing(API), HELD)
    assert pod["spec"]["containers"][1]["resources"]["requests"]["cpu"] == "115m"  # resized too
    assert pod["spec"]["affinity"]["nodeAffinity"][PREFERRED] == [TERM]
    p = placing(API)
    assert (
        actuator.mutation(new_pod()["request"], {}, "x", frozenset({API}), placing=p) == []
    )  # paused
    assert (
        actuator.mutation(
            new_pod("kube-system")["request"],
            {},
            "x",
            placing=placing("kube-system/Deployment/api"),
        )
        == []
    )
    assert actuator.mutation(new_pod(operation="UPDATE")["request"], {}, "x", placing=p) == []
    daemon = new_pod()
    daemon["request"]["object"]["metadata"]["ownerReferences"] = [
        {"kind": "DaemonSet", "name": "agent"}
    ]
    assert (
        actuator.mutation(daemon["request"], {}, "x", placing=placing("shop/DaemonSet/agent")) == []
    )
    killed = actuator.admit(new_pod(), {}, True, "x", placing=p)
    assert "patch" not in killed["response"]
    taken = actuator.admit(new_pod(), {}, False, "x", placing=p)
    assert taken["response"]["patchType"] == "JSONPatch"


# --- what it reports --------------------------------------------------------------------


def preferring(pod: k.V1Pod, key: str = "squero.io/pinned", value: str = "yes") -> k.V1Pod:
    term = k.V1PreferredSchedulingTerm(
        weight=100,
        preference=k.V1NodeSelectorTerm(
            match_expressions=[k.V1NodeSelectorRequirement(key=key, operator="In", values=[value])]
        ),
    )
    pod.spec.affinity = k.V1Affinity(
        node_affinity=k.V1NodeAffinity(preferred_during_scheduling_ignored_during_execution=[term])
    )
    return pod


KEEP = {"cluster_autoscaler.kubernetes.io__safe_to_evict": "false"}


def test_a_pod_says_whether_it_carries_the_preference_and_its_own_workload() -> None:
    labels = {"app": "api", "pod-template-hash": "5d8f"}
    pod = kpod(labels=labels, **KEEP)
    pod.metadata.owner_references[0].name = "api-5d8f"
    f = actuator.pod_facts(preferring(pod), [], LABEL)
    assert f is not None and f.placed and f.workload == "Deployment/api"
    other = actuator.pod_facts(preferring(kpod(labels=labels, **KEEP), value="no"), [], LABEL)
    assert other is not None and not other.placed
    plain = actuator.pod_facts(preferring(pod), [])  # no Placement: neither is sent
    assert plain is not None and not plain.placed and plain.workload is None
    assert (
        "workload"
        not in blockers.sent(blockers.Report(at=0, namespaces=[], pods=[plain]))["pods"][0]
    )
    custom = actuator.pod_facts(kpod(by="Workflow"), [], LABEL)
    assert custom is not None and custom.workload is None  # no Workload the webhook names
    bare = kpod(**KEEP)
    bare.spec.affinity = k.V1Affinity()
    assert actuator.pod_facts(bare, [], LABEL).placed is False  # type: ignore[union-attr]


def test_its_report_says_it_places_only_with_a_placement() -> None:
    core, policy = apis({"shop": [[preferring(kpod(**KEEP))]]})
    told = actuator.blocking(core, policy, ["shop"], 0.0, LABEL)
    assert told.placing and told.pods[0].placed
    plain = actuator.blocking(core, policy, ["shop"], 0.0)
    assert not plain.placing and not plain.pods[0].placed


# --- what it keeps, and says ------------------------------------------------------------


def an_actuator() -> actuator.Actuator:
    http = httpx.Client(
        base_url="http://hub", transport=httpx.MockTransport(lambda r: httpx.Response(204))
    )
    return actuator.Actuator(http, MagicMock(), MagicMock(), "squero-actuator")


def events(a: actuator.Actuator) -> list[tuple[str, str, str, str]]:
    a.say_placed()
    out = []
    for call in a.core.create_namespaced_event.call_args_list:
        ns, body = call.args
        out.append((ns, body.involved_object.kind, body.involved_object.name, body.reason))
    a.core.create_namespaced_event.reset_mock()
    return out


def test_a_workload_joining_or_leaving_the_list_is_said_on_it() -> None:
    a = an_actuator()
    a.place(placing(API, "shop/StatefulSet/db", taint="squero.io/pinned=yes:NoSchedule"))
    assert events(a) == [
        ("shop", "Deployment", "api", "Placed"),
        ("shop", "StatefulSet", "db", "Placed"),
    ]
    body = a.core.create_namespaced_event  # the message names the label and the taint
    a.place(placing(API, "shop/StatefulSet/db"))
    assert events(a) == []  # the same list: nothing new
    a.place(Placing(label=LABEL, workloads=None))  # not known now: the last list stays
    assert events(a) == [] and a.placement and a.placement.workloads == [API, "shop/StatefulSet/db"]
    a.place(placing("shop/StatefulSet/db"))
    assert events(a) == [("shop", "Deployment", "api", "PlacementEnded")]
    a.place(None)  # the Contract's Placement off
    assert events(a) == [("shop", "StatefulSet", "db", "PlacementEnded")] and a.placement is None
    assert body is a.core.create_namespaced_event


def test_the_message_names_the_label_and_the_taint() -> None:
    a = an_actuator()
    a.place(placing(API, taint="squero.io/pinned=yes:NoSchedule"))
    assert a.core.create_namespaced_event.call_args_list == []  # said later in the minute
    a.say_placed()
    (call,) = a.core.create_namespaced_event.call_args_list
    assert call.args[1].message == (
        "Placement: its new pods prefer the nodes labelled squero.io/pinned=yes, "
        "tolerating squero.io/pinned=yes:NoSchedule"
    )
    assert call.args[1].involved_object.api_version == "apps/v1"


def test_a_placed_workloads_pause_is_read_and_its_events_said_in_the_minute() -> None:
    a = an_actuator()
    noted = {actuator.PAUSE: "true"}
    paused = SimpleNamespace(metadata=SimpleNamespace(name="api", annotations=noted))
    a.apps.list_namespaced_deployment.return_value = SimpleNamespace(items=[paused])
    a.pull = lambda: Desired(kill_switch=False, sizings=[], placement=placing(API))  # type: ignore[method-assign]
    a.step()
    assert API in a.paused  # read though nothing else of api is held
    assert actuator.mutation(new_pod()["request"], {}, "x", a.paused, placing=a.placement) == []
    assert events(a) == [("shop", "Deployment", "api", "Placed")]  # said in the minute
    a.core.create_namespaced_event.side_effect = RuntimeError("refused")
    a.pull = lambda: Desired(
        kill_switch=False, sizings=[], placement=placing(API, "shop/StatefulSet/db")
    )  # type: ignore[method-assign]
    a.step()  # an Event that fails costs nothing else
    assert (
        a.placement
        and a.placement.workloads == [API, "shop/StatefulSet/db"]
        and a.placed_news == []
    )


def test_the_webhook_over_tls_places_with_the_actuators_list() -> None:
    import ssl

    cert, key = actuator.certificate("localhost")
    a = actuator.Actuator(MagicMock(), MagicMock(), MagicMock(), "squero-actuator")
    a.placement = placing(API)
    server = actuator.serve(a, cert, key, port=0)
    try:
        trusted = ssl.create_default_context(cadata=cert.decode())
        with httpx.Client(verify=trusted) as http:
            got = http.post(f"https://localhost:{server.server_address[1]}/mutate", json=new_pod())
    finally:
        server.shutdown()
    import base64
    import json

    ops = json.loads(base64.b64decode(got.json()["response"]["patch"]))
    assert ops == [
        {"op": "add", "path": "/spec/affinity", "value": {"nodeAffinity": {PREFERRED: [TERM]}}}
    ]


def test_its_report_says_it_places_unless_the_kill_switch_is_on() -> None:
    seen: list[httpx.Request] = []
    a = an_actuator_of_blockers([204, 204, 204], seen)
    a.blocked(["shop"], 0.0)
    a.placement = placing(API)
    a.blocked(["shop"], 10_000.0)
    a.killed = True
    a.blocked(["shop"], 20_000.0)
    import json

    bodies = [json.loads(r.content) for r in seen]
    assert ["placing" in b for b in bodies] == [False, True, False]


def test_the_kill_switch_keeps_the_list_and_the_webhook_places_nothing() -> None:
    a = an_actuator()
    a.pull = lambda: Desired(kill_switch=False, sizings=[], placement=placing(API))  # type: ignore[method-assign]
    a.step()
    assert a.placement and a.placement.workloads == [API]
    a.pull = lambda: Desired(kill_switch=True, sizings=[])  # type: ignore[method-assign]
    a.step()
    assert a.killed and a.placement and a.placement.workloads == [API]  # kept, not ended
    assert (
        "patch" not in actuator.admit(new_pod(), {}, a.killed, "x", placing=a.placement)["response"]
    )
    a.pull = lambda: None  # type: ignore[method-assign]  # the Hub unreachable
    a.step()
    assert a.placement and a.placement.workloads == [API]


# --- the Hub's side ---------------------------------------------------------------------


def test_the_hub_takes_a_pods_workload_from_the_actuator_and_refuses_a_misshapen_one() -> None:
    from tests.test_placement import facts, node, pod, report, verdict

    # the pod is newer than the Hub's read of the nodes: no Workload of it there
    rep = report(
        facts(
            "api-9",
            "b",
            annotations={"cluster-autoscaler.kubernetes.io/safe-to-evict": "false"},
            workload="Deployment/api",
        )
    )
    got = verdict(rep, node("b", pod("other")))
    assert got["b"]["blockers"][0]["placement"] == API
    assert got["b"]["blockers"][0]["workload"] == "Deployment/api"
    for wrong in ("Deployment", "Deployment/Api", "Deploy ment/api", "/api"):
        r = blockers.Report(
            at=0,
            namespaces=["shop"],
            pods=[blockers.PodFacts(namespace="shop", pod="p", node="n", workload=wrong)],
        )
        assert blockers.unreal(r) == "pods", wrong
    fine = blockers.Report(
        at=0,
        namespaces=["shop"],
        pods=[blockers.PodFacts(namespace="shop", pod="p", node="n", workload="StatefulSet/db")],
    )
    assert blockers.unreal(fine) is None
