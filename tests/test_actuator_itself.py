"""The Actuator's own pod (audit A4): it runs outside the Contract's namespaces, so the
Hub's read of the nodes counted it as a pod it can't see and its node was never clear
(Part A finding 2). It reports that pod's facts; the Hub judges them by the same rules,
counts the pod on its node and moves it with the node's others."""

import json
import math
from typing import Any
from unittest.mock import MagicMock

import httpx
import kubernetes.client as k
import pytest
from fastapi.testclient import TestClient
from kubernetes.client.exceptions import ApiException

from squero import actuator, blockers, sizing
from squero.config import Settings
from tests.test_actuator import A_TOKEN, B_TOKEN, actuated, as_actuator, hub, sql
from tests.test_blockers import EMPTY, a_pod, a_report, apis, kpod
from tests.test_could_go import FRESH, GI, node, pod, read_of
from tests.test_sizing_store import NOW

MI = 2**20
CA, KARPENTER = "cluster-autoscaler", "karpenter"
SAFE_TMP = {blockers.SAFE_VOLUMES: "tmp"}
SAFE_TMP_KW = {"cluster_autoscaler.kubernetes.io__safe_to_evict_local_volumes": "tmp"}  # kpod's


def me(**more: Any) -> dict[str, Any]:
    """Its own pod as the Hub stores it, on node `a`, started an hour before the read."""
    facts = {
        **a_pod("squero-actuator-7c9", "a", "squero-actuator", name="squero-actuator-5d"),
        **{"empty_dir": ["tmp"], "in_memory": [], "host_path": [], "annotations": SAFE_TMP},
        **{"budget": None, "allowed": None, "started": NOW - 3600, "placed": False},
        **{"workload": None, "cpu": 0.02, "memory": 128.0 * MI, "need": None},
    }
    return facts | more


def hosting(n: dict[str, Any], cpu: float = 0.02, gib: float = 0.125) -> dict[str, Any]:
    """Node `n` with its Actuator's pod on it, as the read sees one outside the Contract's
    namespaces: one more pod and its requests, in no one's `own`."""
    asked = {"cpu": n["requests"]["cpu"] + cpu, "memory": n["requests"]["memory"] + gib * GI}
    return n | {"pods": n["pods"] + 1, "requests": asked}


def judged(read: dict[str, Any], itself: dict[str, Any] | None, **cl: Any) -> dict[str, Any]:
    cluster = actuated("acme", A_TOKEN, **cl).clusters[0]
    report = FRESH | ({"itself": itself} if itself else {})
    return blockers.verdicts(read, report, cluster, NOW)


# --- the Hub: its node judged -----------------------------------------------------------


def test_its_own_pod_makes_its_node_known() -> None:
    read = read_of(hosting(node("a", pod("a1", 0.5))), node("b", pod("b1", 1.0)))
    before = judged(read, None)
    assert (before["a"]["state"], before["a"]["why"]) == ("unknown", "1 pod not visible")
    after = judged(read, me(), autoscaler=CA)
    assert after["a"] == {"state": "clear", "why": None, "blockers": []}
    assert after["b"] == before["b"]


@pytest.mark.parametrize(
    ("autoscaler", "annotations", "reasons"),
    [
        (CA, SAFE_TMP, None),  # as shipped: its /tmp named safe
        (CA, {}, ["local storage (tmp)"]),
        (KARPENTER, {}, None),
        (None, SAFE_TMP, ["emptyDir data (tmp)"]),  # kubectl drain reads no annotation
    ],
)
def test_its_own_pod_is_judged_by_the_rules_any_pod_is(
    autoscaler: str | None, annotations: dict[str, str], reasons: list[str] | None
) -> None:
    read = read_of(hosting(node("a")), node("b"))
    got = judged(read, me(annotations=annotations), autoscaler=autoscaler)["a"]
    if reasons is None:
        assert got["state"] == "clear"
    else:
        assert got["state"] == "blocked"
        assert [(b["namespace"], b["reasons"]) for b in got["blockers"]] == [
            ("squero-actuator", reasons)
        ]


@pytest.mark.parametrize(
    "itself",
    [
        me(started=NOW - blockers.SEEN + 1),  # the read may not hold it yet
        me(started=None),
        me(node="b"),  # the read put no pod it can't see on b
        me(namespace="shop"),  # a Contract namespace's pod: the read holds it already
    ],
)
def test_its_own_pod_counts_only_where_the_read_holds_it(itself: dict[str, Any]) -> None:
    read = read_of(hosting(node("a")), node("b"))
    got = judged(read, itself)
    assert (got["a"]["state"], got["a"]["why"]) == ("unknown", "1 pod not visible")
    assert got["b"]["state"] == "clear"


def test_its_own_pod_counts_from_seen_after_it_started() -> None:
    read = read_of(hosting(node("a")))
    got = judged(read, me(started=NOW - blockers.SEEN), autoscaler=CA)
    assert got["a"]["state"] == "clear"


def test_it_never_hides_more_than_its_own_pod() -> None:
    two = hosting(hosting(node("a")))  # it and another pod the Hub can't see
    got = judged(read_of(two), me(), autoscaler=CA)["a"]
    assert (got["state"], got["why"]) == ("unknown", "1 pod not visible")


def test_its_own_pod_is_not_counted_without_a_usable_report() -> None:
    read = read_of(hosting(node("a")))
    old = FRESH | {"itself": me(), "received": NOW - blockers.STALE - 1}
    got = blockers.verdicts(read, old, actuated("acme", A_TOKEN).clusters[0], NOW)
    assert got["a"]["state"] == "unknown"
    assert blockers.known(read, None, actuated("acme", A_TOKEN).clusters[0]) is read


# --- the Hub: its pod moves with its node -------------------------------------------------


def went(*nodes: dict[str, Any], **report: Any) -> Any:
    c = actuated("acme", A_TOKEN, autoscaler=CA)
    (shown, *_) = sizing.nodes_report({"test": read_of(*nodes)}, c, {"test": FRESH | report}, NOW)
    return shown["could_go"]["now"]["nodes"], shown["could_go"]["after"]["nodes"]


def test_its_own_pod_needs_room_on_another_node() -> None:
    a = hosting(node("a", pod("a1", 0.5)), cpu=0.5)
    roomy = node("b", pod("b1", 3.0))  # a core free: both fit
    tight = node("b", pod("b1", 3.4))  # 600m free: a1 fits, it doesn't
    assert went(a, roomy, itself=me(cpu=0.5)) == (["a"], ["a"])
    assert went(a, tight, itself=me(cpu=0.5)) == ([], [])
    assert went(a, tight) == ([], [])  # without it: unknown


def test_its_own_pod_lands_only_where_it_may() -> None:
    a, b = hosting(node("a")), node("b", pod("b1", 2.0))  # a, the lighter, goes first
    ssd = {"selector": {"disk": "ssd"}, "required": [], "preferred": []}
    needs = {"needs": [ssd], "needed": {}}
    plain = {"a": {}, "b": {}}
    assert went(a, b, itself=me(need=0), **needs, labels=plain) == (["b"], ["b"])
    fast = {"a": {}, "b": {"disk": "ssd"}}
    assert went(a, b, itself=me(need=0), **needs, labels=fast) == (["a"], ["a"])
    assert went(a, b, itself=me(), **needs, labels=plain) == (["a"], ["a"])


# --- the Hub: what it takes --------------------------------------------------------------


def test_its_own_pod_is_taken_from_outside_the_clusters_namespaces(
    settings: Settings, database_url: str
) -> None:
    """Its own pod is the one fact outside the Contract's namespaces a report may carry:
    stored with the report; a pod of that namespace among `pods` is still refused."""
    body = a_report(itself=me(), constraints="read", needs=[], needed={}, labels={})
    with TestClient(hub(settings, database_url, actuated("acme", A_TOKEN))) as c:
        post = lambda b: c.post(  # noqa: E731
            "/actuator/blockers", headers=as_actuator(A_TOKEN), json=b
        )
        assert post(body).status_code == 204
        outside = post(a_report(a_pod(ns="squero-actuator")))
    assert outside.status_code == 422
    ((stored,),) = sql(database_url, ("select report from cluster_blockers", ()))
    assert stored["itself"]["pod"] == "squero-actuator-7c9"


@pytest.mark.parametrize(
    "change",
    [
        {"pod": "Squero"},  # no name Kubernetes allows
        {"node": "N1"},
        {"need": 0},  # no such need
        {"started": math.inf},
        {"cpu": math.inf},
        {"memory": math.inf},
        {"workload": "Deployment"},
        {"annotations": {"other": "x"}},
    ],
)
def test_its_own_pod_no_actuator_sends_is_refused_and_named(
    settings: Settings, database_url: str, change: dict[str, Any]
) -> None:
    body = a_report(itself=me(**change), constraints="read", needs=[], needed={}, labels={})
    with TestClient(hub(settings, database_url, actuated("acme", A_TOKEN))) as c:
        r = c.post(
            "/actuator/blockers",
            headers=as_actuator(A_TOKEN) | {"content-type": "application/json"},
            content=json.dumps(body),  # Infinity as Python writes it
        )
    assert r.status_code == 422 and r.json()["detail"] == "not what an Actuator sends: itself"


def test_each_contracts_own_pod_stays_its_own(settings: Settings, database_url: str) -> None:
    acme, bank = actuated("acme", A_TOKEN), actuated("bank", B_TOKEN)
    with TestClient(hub(settings, database_url, acme, bank)) as c:
        for token, pod_ in ((A_TOKEN, "acme-act"), (B_TOKEN, "bank-act")):
            body = a_report(itself=me(pod=pod_))
            r = c.post("/actuator/blockers", headers=as_actuator(token), json=body)
            assert r.status_code == 204
    rows = sql(database_url, ("select contract_id, report from cluster_blockers order by 1", ()))
    assert [(cid, r["itself"]["pod"]) for cid, r in rows] == [
        ("acme", "acme-act"),
        ("bank", "bank-act"),
    ]


def test_its_own_pod_is_sent_only_when_read_so_an_older_hub_takes_the_report() -> None:
    plain = blockers.Report(at=NOW, namespaces=["shop"], pods=[])
    assert "itself" not in blockers.sent(plain)
    full = blockers.Report.model_validate(a_report(itself=me()))
    assert blockers.sent(full)["itself"]["pod"] == "squero-actuator-7c9"


# --- the Actuator: reading it ----------------------------------------------------------


def own(
    *containers: tuple[str, str], selector: dict[str, str] | None = None, **more: Any
) -> k.V1Pod:
    """Its own pod: a ReplicaSet's with a /tmp emptyDir, nothing that blocks under Karpenter."""
    p = kpod("me-1", ns="squero-actuator", volumes=[EMPTY], **more)
    p.spec.containers = [
        k.V1Container(name=f"c{i}", resources=k.V1ResourceRequirements(requests=dict(r)))
        for i, r in enumerate(containers or [])
    ] or [k.V1Container(name="app")]
    p.spec.node_selector = selector
    return p


def reading(p: Any) -> Any:
    core, _ = apis({"shop": [[]]})
    if isinstance(p, Exception):
        core.read_namespaced_pod.side_effect = p
    else:
        core.read_namespaced_pod.return_value = p
    return core


def test_it_reads_its_own_pod_by_name_with_its_requests() -> None:
    p = own((("cpu", "20m"), ("memory", "128Mi")), (("cpu", "1"), ("memory", "1Gi")))
    core = reading(p)
    got = actuator.own_pod(core, "squero-actuator", "me-1")
    core.read_namespaced_pod.assert_called_once_with("me-1", "squero-actuator")
    assert got is not None
    itself, pod_ = got
    assert pod_ is p
    assert (itself.namespace, itself.pod, itself.kind, itself.empty_dir) == (
        "squero-actuator",
        "me-1",
        "ReplicaSet",
        ["scratch"],
    )
    assert (itself.cpu, itself.memory) == pytest.approx((1.02, 1152.0 * MI))
    bare = actuator.own_pod(reading(own()), "squero-actuator", "me-1")
    assert bare is not None and (bare[0].cpu, bare[0].memory) == (0.0, 0.0)


def test_its_own_pod_is_told_with_no_fact_that_may_block() -> None:
    p = kpod("me-1", ns="squero-actuator")  # no volume, a ReplicaSet's: nothing to tell
    assert actuator.pod_facts(p, []) is None
    got = actuator.own_pod(reading(p), "squero-actuator", "me-1")
    assert got is not None and got[0].pod == "me-1"


@pytest.mark.parametrize(
    "failed",
    [ApiException(status=403, reason="Forbidden"), ApiException(status=500, reason="Boom")],
)
def test_its_own_pod_unread_is_none(failed: ApiException) -> None:
    assert actuator.own_pod(reading(failed), "squero-actuator", "me-1") is None


def test_its_own_pod_on_its_way_out_is_none() -> None:
    p = own()
    p.metadata.deletion_timestamp = "2026-11-16T00:00:00Z"
    assert actuator.own_pod(reading(p), "squero-actuator", "me-1") is None


def nodes_listed(core: Any, labels: dict[str, dict[str, str]]) -> None:
    items = [k.V1Node(metadata=k.V1ObjectMeta(name=n, labels=v)) for n, v in labels.items()]
    core.list_node.return_value = k.V1NodeList(items=items, metadata=k.V1ListMeta())


def test_its_report_carries_its_own_pod_and_its_need_by_index() -> None:
    core = reading(own(selector={"disk": "ssd"}))
    nodes_listed(core, {"a": {"disk": "ssd", "zone": "x"}, "b": {}})
    _, policy = apis({"shop": [[]]})
    me_ = ("squero-actuator", "me-1")
    r = actuator.blocking(core, policy, ["shop"], NOW, constraints=True, me=me_)
    assert r.itself is not None and r.itself.pod == "me-1" and r.itself.need == 0
    assert r.needs[0].selector == {"disk": "ssd"} and r.needed == {}  # not a Contract pod's
    assert r.labels == {"a": {"disk": "ssd"}, "b": {}}
    asked = actuator.blocking(core, policy, ["shop"], NOW, constraints=False, me=me_)
    assert asked.itself is None and "itself" not in blockers.sent(asked)
    core.read_namespaced_pod.reset_mock()
    assert actuator.blocking(core, policy, ["shop"], NOW, constraints=True).itself is None
    core.read_namespaced_pod.assert_not_called()


def test_its_own_pod_with_no_need_reads_no_node() -> None:
    core = reading(own())
    _, policy = apis({"shop": [[]]})
    r = actuator.blocking(core, policy, ["shop"], NOW, constraints=True, me=("x", "me-1"))
    assert r.itself is not None and r.itself.need is None and r.constraints == "read"
    core.list_node.assert_not_called()


def test_its_own_need_over_a_reports_limits_cuts_the_needs_not_its_pod() -> None:
    core = reading(own(selector={f"k{i}": "v" for i in range(51)}))  # over 50 keys
    _, policy = apis({"shop": [[]]})
    r = actuator.blocking(core, policy, ["shop"], NOW, constraints=True, me=("x", "me-1"))
    assert r.constraints == "cut" and r.itself is not None and r.itself.need is None


def test_a_report_over_the_size_drops_its_own_need_with_the_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core = reading(own(selector={"disk": "ssd"}))
    nodes_listed(core, {"a": {"disk": "ssd"}})
    _, policy = apis({"shop": [[]]})
    me_ = ("x", "me-1")
    whole = actuator.blocking(core, policy, ["shop"], NOW, constraints=True, me=me_)
    monkeypatch.setattr(blockers, "MAX_BYTES", len(whole.model_dump_json()) - 1)
    r = actuator.blocking(core, policy, ["shop"], NOW, constraints=True, me=me_)
    assert r.constraints == "cut" and r.needs == []
    assert r.itself is not None and r.itself.need is None
    assert blockers.unreal(r) is None  # what the Hub takes


def test_the_actuator_reads_its_own_pod_only_when_it_knows_its_name() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    http = httpx.Client(base_url="http://hub", transport=httpx.MockTransport(handler))
    core = reading(own())
    _, policy = apis({"shop": [[]]})
    a = actuator.Actuator(http, core, MagicMock(), "squero-actuator", pod="me-1")
    a.policy = policy
    a.blocked(["shop"], NOW, constraints=True)
    core.read_namespaced_pod.assert_called_once_with("me-1", "squero-actuator")
    assert json.loads(seen[0].content)["itself"]["pod"] == "me-1"
    nameless = actuator.Actuator(http, core, MagicMock(), "squero-actuator")
    nameless.policy = policy
    nameless.blocked(["shop"], NOW, constraints=True)
    assert core.read_namespaced_pod.call_count == 1
    assert "itself" not in json.loads(seen[1].content)


def test_its_settings_take_its_pods_name() -> None:
    s = actuator.ActuatorSettings(hub="https://hub", token="t" * 32, namespace="n", pod="me-1")  # type: ignore[arg-type]
    assert s.pod == "me-1"
    assert actuator.ActuatorSettings(hub="https://hub", token="t", namespace="n").pod is None  # type: ignore[arg-type]


# --- the manifests -----------------------------------------------------------------------


def chart() -> list[dict[str, Any]]:
    """The Actuator's objects as the chart renders them (Phase 18.1a)."""
    from tests.test_chart import ACTUATOR, render

    return render(relay={"enabled": False}, actuator=ACTUATOR)


def test_its_own_pod_is_read_by_a_role_of_its_own_namespace_getting_pods_only() -> None:
    docs = chart()
    (role,) = [
        d for d in docs if d["kind"] == "Role" and d["metadata"]["name"] == "squero-actuator-self"
    ]
    assert role["metadata"]["namespace"] == "squero-actuator"
    assert role["rules"] == [{"apiGroups": [""], "resources": ["pods"], "verbs": ["get"]}]
    (bound,) = [
        d
        for d in docs
        if d["kind"] == "RoleBinding" and d["metadata"]["name"] == "squero-actuator-self"
    ]
    assert bound["metadata"]["namespace"] == "squero-actuator"
    assert bound["roleRef"] == {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": "Role",
        "name": "squero-actuator-self",
    }
    assert bound["subjects"] == [
        {"kind": "ServiceAccount", "name": "squero-actuator", "namespace": "squero-actuator"}
    ]


def test_its_pod_knows_its_name_and_names_every_emptydir_safe() -> None:
    deploy = next(d for d in chart() if d["kind"] == "Deployment")
    template = deploy["spec"]["template"]
    (ctr,) = template["spec"]["containers"]
    named = {e["name"]: e.get("valueFrom") for e in ctr["env"]}
    assert named["SQUERO_ACTUATOR_POD"] == {"fieldRef": {"fieldPath": "metadata.name"}}
    empty = {v["name"] for v in template["spec"]["volumes"] if "emptyDir" in v}
    safe = template["metadata"]["annotations"][blockers.SAFE_VOLUMES]
    assert {v.strip() for v in safe.split(",")} == empty == {"tmp"}


def test_part_as_finding_end_to_end_its_node_known_from_its_own_pod() -> None:
    """Part A finding 2, from its pod object to the Hub's verdict: the node running the
    Actuator was "unknown (1 pod not visible)"."""
    from datetime import UTC, datetime

    p = own((("cpu", "20m"), ("memory", "128Mi")), node="a", **SAFE_TMP_KW)
    p.status.start_time = datetime.fromtimestamp(NOW - 3600, UTC)
    p.spec.volumes = [k.V1Volume(name="tmp", empty_dir=k.V1EmptyDirVolumeSource())]  # as shipped
    core = reading(p)
    _, policy = apis({"shop": [[]]})
    me_ = ("squero-actuator", "me-1")
    r = actuator.blocking(core, policy, ["shop"], NOW, constraints=True, me=me_)
    taken = blockers.Report.model_validate(blockers.sent(r))
    assert blockers.unreal(taken) is None
    report = taken.model_dump() | {"received": NOW}
    read = read_of(hosting(node("a", pod("a1", 0.5))), node("b", pod("b1", 1.0)))
    cluster = actuated("acme", A_TOKEN, autoscaler=CA).clusters[0]
    assert blockers.verdicts(read, report, cluster, NOW)["a"]["state"] == "clear"
    no_role = actuator.blocking(
        reading(ApiException(status=403)), policy, ["shop"], NOW, constraints=True, me=me_
    )
    unseen = no_role.model_dump() | {"received": NOW}
    got = blockers.verdicts(read, unseen, cluster, NOW)["a"]
    assert (got["state"], got["why"]) == ("unknown", "1 pod not visible")
