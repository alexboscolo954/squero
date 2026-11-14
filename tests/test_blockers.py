"""Blockers (slice 15.1b): the Actuator's hourly facts of the pods that may keep a node
from being removed, the Hub's endpoint for them, the rules of each autoscaler and of
`kubectl drain`, and each node's verdict, which is clear only on positive evidence."""

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient
from kubernetes import client as k
from kubernetes.client.exceptions import ApiException

from squero import actuator, blockers, sizing
from squero.blockers import DO_NOT_DISRUPT, SAFE, SAFE_VOLUMES, STALE
from squero.config import Settings
from tests.test_actuator import A_TOKEN, B_TOKEN, actuated, as_actuator, hub, sql
from tests.test_nodes import a_node, found
from tests.test_webapp import OPERATOR, VIEW, app_dir  # noqa: F401

NOW = 1_000_000.0

# --- the Actuator's side: facts ----------------------------------------------------------


def kpod(
    name: str = "api-1",
    ns: str = "shop",
    node: str | None = "n1",
    by: str | None = "ReplicaSet",
    phase: str = "Running",
    volumes: list[k.V1Volume] | None = None,
    labels: dict[str, str] | None = None,
    **annotations: str,
) -> k.V1Pod:
    owners = by and [
        k.V1OwnerReference(api_version="v1", kind=by, name="owner", uid="u", controller=True)
    ]
    meta = k.V1ObjectMeta(name=name, namespace=ns, labels=labels or {"app": "api"})
    meta.owner_references = owners or None
    meta.annotations = {a.replace("__", "/").replace("_", "-"): v for a, v in annotations.items()}
    spec = k.V1PodSpec(containers=[k.V1Container(name="app")], node_name=node, volumes=volumes)
    return k.V1Pod(metadata=meta, spec=spec, status=k.V1PodStatus(phase=phase))


def budget(name: str, allowed: int | None, **match: str) -> k.V1PodDisruptionBudget:
    selector = k.V1LabelSelector(match_labels=match) if match else None
    counts = {"current_healthy": 1, "desired_healthy": 1, "expected_pods": 1}
    status = (
        None
        if allowed is None
        else k.V1PodDisruptionBudgetStatus(disruptions_allowed=allowed, **counts)
    )
    return k.V1PodDisruptionBudget(
        metadata=k.V1ObjectMeta(name=name),
        spec=k.V1PodDisruptionBudgetSpec(selector=selector),
        status=status,
    )


EMPTY = k.V1Volume(name="scratch", empty_dir=k.V1EmptyDirVolumeSource())
RAM = k.V1Volume(name="shm", empty_dir=k.V1EmptyDirVolumeSource(medium="Memory"))
HOST = k.V1Volume(name="logs", host_path=k.V1HostPathVolumeSource(path="/var/log"))
CONFIG = k.V1Volume(name="cfg", config_map=k.V1ConfigMapVolumeSource(name="cfg"))


def facts(p: k.V1Pod, *budgets: k.V1PodDisruptionBudget) -> dict[str, Any] | None:
    f = actuator.pod_facts(p, list(budgets))
    return f.model_dump() if f else None


def test_a_pod_with_nothing_to_tell_is_not_sent() -> None:
    assert facts(kpod()) is None
    assert facts(kpod(by="StatefulSet", volumes=[CONFIG])) is None
    assert facts(kpod(by="Job"), budget("b", 1, app="api")) is None  # covered, one allowed
    assert facts(kpod(), budget("b", 0, app="web")) is None  # another Workload's budget
    assert facts(kpod(), budget("b", 0)) is None  # a null selector matches nothing


def test_each_fact_is_sent_as_it_is_and_never_a_verdict() -> None:
    plain = {"namespace": "shop", "pod": "api-1", "node": "n1", "kind": "ReplicaSet"}
    plain |= {"name": "owner", "empty_dir": [], "in_memory": [], "host_path": []}
    plain |= {"annotations": {}, "budget": None, "allowed": None, "started": None}
    assert facts(kpod(by=None)) == plain | {"kind": "", "name": ""}
    assert facts(kpod(by="Cluster")) == plain | {"kind": "Cluster"}  # a custom controller's
    owned = kpod()
    owned.metadata.owner_references[0].controller = False  # an owner that controls nothing
    assert facts(owned) == plain | {"kind": "", "name": ""}
    stored = facts(kpod(volumes=[RAM, EMPTY, HOST, CONFIG]))
    assert stored == plain | {
        "empty_dir": ["scratch", "shm"],
        "in_memory": ["shm"],
        "host_path": ["logs"],
    }
    safe = "cluster_autoscaler.kubernetes.io__safe_to_evict"
    noted = kpod(**{safe: "true", "karpenter.sh__do_not_disrupt": "true", "team": "pay"})
    assert facts(noted) == plain | {"annotations": {SAFE: "true", DO_NOT_DISRUPT: "true"}}
    long = facts(kpod(**{safe: "x" * 400}))
    assert long and long["annotations"] == {SAFE: "x" * 253}
    running = kpod(by=None)
    running.status.start_time = datetime.fromtimestamp(NOW, UTC)
    assert facts(running) == plain | {"kind": "", "name": "", "started": NOW}


def test_the_budget_named_is_the_covering_one_that_allows_fewest() -> None:
    loose, tight = budget("loose", 2, app="api"), budget("tight", 0, app="api")
    got = facts(kpod(), loose, tight, budget("other", 0, app="web"))
    assert got and (got["budget"], got["allowed"]) == ("tight", 0)
    # a budget whose status isn't written yet allows none, as far as anyone knows
    unknown = facts(kpod(), budget("new", None, app="api"))
    assert unknown and (unknown["budget"], unknown["allowed"]) == ("new", 0)
    # one that allows a disruption is named only beside another fact
    assert facts(kpod(), loose) is None
    beside = facts(kpod(by=None), loose)
    assert beside and (beside["budget"], beside["allowed"]) == ("loose", 2)


def test_a_kube_system_pod_is_sent_unless_a_budget_covers_it() -> None:
    bare = facts(kpod(ns="kube-system"))
    assert bare and bare["budget"] is None
    assert facts(kpod(ns="kube-system"), budget("dns", 1, app="api")) is None


def test_pods_that_stay_with_their_node_or_hold_nothing_are_never_sent() -> None:
    assert facts(kpod(by=None)) is not None
    assert facts(kpod(by="DaemonSet", volumes=[EMPTY])) is None
    assert facts(kpod(by=None, node=None, phase="Pending")) is None  # on no node yet
    assert facts(kpod(by=None, phase="Succeeded")) is None
    assert facts(kpod(by=None, phase="Failed")) is None
    mirror = kpod(by="Node", **{"kubernetes.io__config.mirror": "abc"})
    mirror.metadata.annotations = {"kubernetes.io/config.mirror": "abc"}
    assert facts(mirror) is None  # the node's own static pod
    leaving = kpod(by=None)
    leaving.metadata.deletion_timestamp = datetime.now(UTC)
    assert facts(leaving) is None


# --- the Actuator's side: the read and the send ------------------------------------------


def apis(
    pods: dict[str, list[Any]], budgets: dict[str, list[Any]] | None = None
) -> tuple[Any, Any]:
    """CoreV1Api and PolicyV1Api mocks: `pods` per namespace, each item a page of pods or
    an exception to raise; `budgets` per namespace."""
    core, policy = MagicMock(), MagicMock()
    tokens = {
        (ns, None if i == 0 else f"{ns}-{i}"): i
        for ns, pages in pods.items()
        for i in range(len(pages))
    }

    def list_pods(ns: str, limit: int, _continue: str | None = None) -> Any:
        assert limit == actuator.PAGE
        if ns not in pods:
            raise ApiException(status=403, reason="Forbidden")
        i = tokens[(ns, _continue)]
        if isinstance(pods[ns][i], Exception):
            raise pods[ns][i]
        more = f"{ns}-{i + 1}" if i + 1 < len(pods[ns]) else None
        return k.V1PodList(items=pods[ns][i], metadata=k.V1ListMeta(_continue=more))

    core.list_namespaced_pod.side_effect = list_pods
    policy.list_namespaced_pod_disruption_budget.side_effect = lambda ns: MagicMock(
        items=(budgets or {}).get(ns, [])
    )
    return core, policy


def test_a_namespace_is_read_a_page_at_a_time_with_its_own_budgets() -> None:
    pods = {
        "shop": [[kpod("b", by=None), kpod("quiet")], [kpod("a", by=None)]],
        "pay": [[kpod("c", ns="pay")]],
    }
    core, policy = apis(pods, {"pay": [budget("tight", 0, app="api")]})
    r = actuator.blocking(core, policy, ["shop", "pay", "shop"], NOW)
    assert (r.at, r.namespaces, r.truncated) == (NOW, ["pay", "shop"], False)
    assert [(p.namespace, p.pod, p.budget) for p in r.pods] == [
        ("pay", "c", "tight"),
        ("shop", "a", None),
        ("shop", "b", None),
    ]
    assert core.list_namespaced_pod.call_count == 3  # shop's two pages, once


def test_a_namespace_it_could_not_read_is_left_out_and_said() -> None:
    """Not bound there, or a read that failed half way: none of its pods are sent, and
    it is not among the namespaces read, so the Hub knows what it wasn't told."""
    pods = {
        "shop": [[kpod("a", by=None)], ApiException(status=500, reason="boom")],
        "pay": [[kpod("c", ns="pay", by=None)]],
    }
    core, policy = apis(pods)
    r = actuator.blocking(core, policy, ["shop", "pay", "secret"], NOW)
    assert r.namespaces == ["pay"] and [p.pod for p in r.pods] == ["c"]
    policy.list_namespaced_pod_disruption_budget.side_effect = ApiException(status=403)
    assert actuator.blocking(core, policy, ["pay"], NOW).namespaces == []


def test_a_report_over_the_cap_is_cut_and_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(blockers, "MAX_PODS", 2)
    core, policy = apis({"shop": [[kpod(n, by=None) for n in ("c", "a", "b")]]})
    r = actuator.blocking(core, policy, ["shop"], NOW)
    assert r.truncated and [p.pod for p in r.pods] == ["a", "b"]
    monkeypatch.setattr(blockers, "MAX_PODS", 3)
    assert not actuator.blocking(core, policy, ["shop"], NOW).truncated


def test_a_report_too_large_for_the_hubs_proxy_is_cut_by_size_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core, policy = apis({"shop": [[kpod(f"pod-{i:03}", by=None) for i in range(40)]]})
    whole = actuator.blocking(core, policy, ["shop"], NOW)
    assert len(whole.pods) == 40 and not whole.truncated
    monkeypatch.setattr(blockers, "MAX_BYTES", len(whole.model_dump_json()))
    assert actuator.blocking(core, policy, ["shop"], NOW) == whole  # at the size: whole
    monkeypatch.setattr(blockers, "MAX_BYTES", len(whole.model_dump_json()) - 1)
    cut = actuator.blocking(core, policy, ["shop"], NOW)
    assert cut.truncated and [p.pod for p in cut.pods] == [f"pod-{i:03}" for i in range(30)]
    monkeypatch.setattr(blockers, "MAX_BYTES", 10)
    assert actuator.blocking(core, policy, ["shop"], NOW).pods == []  # and it ends


def an_actuator(answers: list[Any], seen: list[httpx.Request]) -> actuator.Actuator:
    """An Actuator whose Hub gives `answers` in turn to its Blockers reports."""
    left = iter(answers)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        answer = next(left)
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(answer)

    http = httpx.Client(base_url="http://hub", transport=httpx.MockTransport(handler))
    core, policy = apis({"shop": [[kpod("a", by=None)]]})
    a = actuator.Actuator(http, core, MagicMock(), "x")
    a.policy = policy
    return a


def test_its_blockers_go_once_an_hour_and_again_soon_after_one_that_did_not_arrive() -> None:
    seen: list[httpx.Request] = []
    a = an_actuator([204, 503, httpx.ConnectError("down"), 204, 404, 204], seen)
    a.blocked(["shop"], NOW)
    assert len(seen) == 1 and seen[0].url.path == "/actuator/blockers"
    assert [p["pod"] for p in __import__("json").loads(seen[0].content)["pods"]] == ["a"]
    a.blocked(["shop"], NOW + 3599)  # not due
    assert len(seen) == 1
    a.blocked(["shop"], NOW + 3600)  # 503: the Hub was down
    a.blocked(["shop"], NOW + 3600 + 299)
    assert len(seen) == 2
    a.blocked(["shop"], NOW + 3600 + 300)  # again five minutes on: no connection
    a.blocked(["shop"], NOW + 3600 + 600)  # taken
    assert len(seen) == 4
    a.blocked(["shop"], NOW + 7200 + 600)  # 404: an older Hub has no such route
    a.blocked(["shop"], NOW + 7200 + 900)  # not sent again every five minutes
    assert len(seen) == 5
    a.blocked(["shop"], NOW + 10800 + 600)
    assert len(seen) == 6


def desired(**more: Any) -> actuator.Desired:
    return actuator.Desired(kill_switch=False, sizings=[], **more)


def test_the_minute_reports_blockers_last_and_a_failure_there_costs_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[httpx.Request] = []
    a = an_actuator([204], seen)
    order: list[str] = []
    monkeypatch.setattr(a, "send", lambda: order.append("reports"))
    monkeypatch.setattr(a, "save", lambda: None)
    real = a.blocked
    monkeypatch.setattr(a, "blocked", lambda ns, now: (order.append("blockers"), real(ns, now)))
    monkeypatch.setattr(a, "pull", lambda: desired(blockers=["shop"]))
    a.step()
    assert order == ["reports", "blockers"] and len(seen) == 1
    monkeypatch.setattr(a, "pull", lambda: desired())  # an older Hub: no namespaces given
    a.blockers_due = 0.0
    a.step()
    assert order == ["reports", "blockers", "reports"]
    monkeypatch.setattr(a, "pull", lambda: None)  # the Hub unreachable
    a.step()
    assert order.count("blockers") == 1

    def boom(ns: Any, now: Any) -> None:
        raise RuntimeError("a bug")

    monkeypatch.setattr(a, "pull", lambda: desired(blockers=["shop"]))
    monkeypatch.setattr(a, "blocked", boom)
    a.step()  # said in its log, and the minute ends as any other
    killed = actuator.Desired(kill_switch=True, sizings=[], blockers=["shop"])
    monkeypatch.setattr(a, "pull", lambda: killed)
    monkeypatch.setattr(a, "blocked", lambda ns, now: order.append(f"killed {ns}"))
    a.step()
    assert order[-1] == "killed ['shop']"  # a read is no change: the Kill switch stops none


# --- the Hub's side ----------------------------------------------------------------------


def a_report(*pods: dict[str, Any], **more: Any) -> dict[str, Any]:
    return {"at": NOW, "namespaces": ["shop"], "pods": list(pods), **more}


def a_pod(pod: str = "api-1", node: str = "n1", ns: str = "shop", **facts_: Any) -> dict[str, Any]:
    return {"namespace": ns, "pod": pod, "node": node, "kind": "ReplicaSet", "name": "rs", **facts_}


def test_the_hub_names_the_clusters_namespaces_under_the_kill_switch_too(
    settings: Settings, database_url: str
) -> None:
    app = hub(settings, database_url, actuated("acme", A_TOKEN))
    with TestClient(app) as c:
        got = c.get("/actuator/desired", headers=as_actuator(A_TOKEN)).json()
        assert (got["kill_switch"], got["blockers"]) == (False, ["shop"])
        sql(
            database_url,
            ("insert into kill_switches values ('acme', 't', 1)", ()),
        )
        got = c.get("/actuator/desired", headers=as_actuator(A_TOKEN)).json()
        assert (got["kill_switch"], got["blockers"]) == (True, ["shop"])


def test_a_report_is_stored_as_its_tokens_cluster_and_replaces_the_last(
    settings: Settings, database_url: str
) -> None:
    acme, bank = actuated("acme", A_TOKEN), actuated("bank", B_TOKEN)
    app = hub(settings, database_url, acme, bank)
    with TestClient(app) as c:
        post = lambda body, token=A_TOKEN: c.post(  # noqa: E731
            "/actuator/blockers", headers=as_actuator(token), json=body
        )
        assert post(a_report(a_pod("old", kind="", name=""))).status_code == 204
        assert post(a_report(a_pod("new", kind="", name=""))).status_code == 204
        assert post(a_report(a_pod("theirs", kind="", name="")), B_TOKEN).status_code == 204
        assert post(a_report(), "wrong" * 8).status_code == 401
        rows = sql(
            database_url,
            ("select contract_id, cluster, report, received from cluster_blockers order by 1", ()),
        )
        assert all(r[3] > 1e9 for r in rows)  # the Hub's clock, not the report's `at` of NOW
        assert [(r[0], r[1], [p["pod"] for p in r[2]["pods"]]) for r in rows] == [
            ("acme", "test", ["new"]),
            ("bank", "test", ["theirs"]),
        ]


def test_a_report_the_hub_refuses_is_refused_whole(
    settings: Settings, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = hub(settings, database_url, actuated("acme", A_TOKEN))
    with TestClient(app) as c:

        def post(body: Any) -> Any:
            return c.post("/actuator/blockers", headers=as_actuator(A_TOKEN), json=body)

        outside = post(a_report(a_pod(), a_pod("x", ns="bank")))
        assert outside.status_code == 422
        assert outside.json()["detail"] == "namespaces not in cluster 'test': ['bank']"
        assert post(a_report(namespaces=["shop", "kube-system"])).status_code == 422
        assert post(a_report(a_pod(), at=-1.0)).status_code == 422
        assert post(a_report(a_pod(surprise=1))).status_code == 422  # a field of no Actuator's
        assert post(a_report(a_pod(pod="p" * 254))).status_code == 422
        assert post(a_report(a_pod(allowed=-1, budget="b"))).status_code == 422
        odd = post(a_report(a_pod(annotations={"team": "x" * 300})))
        assert odd.status_code == 422 and odd.json()["detail"] == "not what an Actuator sends: pods"
        assert "xxx" not in odd.text  # named, never quoted
        assert post(a_report(a_pod(annotations={"team": "pay"}))).status_code == 422
        assert post(a_report(a_pod(annotations={SAFE: "x" * 254}))).status_code == 422
        assert post(a_report(a_pod(annotations={SAFE: "x" * 253}))).status_code == 204
        assert post(a_report(a_pod(empty_dir=["v" * 254]))).status_code == 422
        monkeypatch.setattr(blockers, "MAX_PODS", 1)
        assert post(a_report(a_pod("a"), a_pod("b"))).status_code == 413
        assert post(a_report(a_pod("a"))).status_code == 204
        stored = sql(database_url, ("select report from cluster_blockers", ()))
        assert [p["pod"] for p in stored[0][0]["pods"]] == ["a"]


def test_what_an_actuator_builds_is_never_refused(settings: Settings, database_url: str) -> None:
    """A refused report is an hour of nothing: every fact at its longest still passes."""
    safe = "cluster_autoscaler.kubernetes.io__safe_to_evict"
    full = kpod("p" * 253, by=None, volumes=[EMPTY, RAM, HOST], **{safe: "x" * 400})
    full.status.start_time = datetime.fromtimestamp(NOW, UTC)
    core, policy = apis(
        {"shop": [[full, kpod("b", by="Cluster")]]}, {"shop": [budget("b" * 253, 0, app="api")]}
    )
    report = actuator.blocking(core, policy, ["shop"], NOW)
    assert len(report.pods) == 2 and blockers.unreal(report) is None
    app = hub(settings, database_url, actuated("acme", A_TOKEN))
    with TestClient(app) as c:
        sent = c.post("/actuator/blockers", headers=as_actuator(A_TOKEN), json=report.model_dump())
    assert sent.status_code == 204
    odd = report.model_copy(
        update={"pods": [report.pods[0].model_copy(update={"started": float("inf")})]}
    )
    assert blockers.unreal(odd) == "pods"


# --- the rules ---------------------------------------------------------------------------

CA, KARPENTER = "cluster-autoscaler", "karpenter"
TIGHT = {"budget": "tight", "allowed": 0}


def why(autoscaler: str | None, ns: str = "shop", **facts_: Any) -> list[str]:
    return blockers.reasons(a_pod(ns=ns, **facts_), autoscaler, NOW)


@pytest.mark.parametrize("autoscaler", [CA, KARPENTER, None])
def test_a_budget_that_allows_no_disruption_blocks_whoever_removes_nodes(
    autoscaler: str | None,
) -> None:
    said = ["PodDisruptionBudget tight allows no disruption"]
    assert why(autoscaler, **TIGHT) == said
    assert why(autoscaler, budget="loose", allowed=1) == []
    assert why(autoscaler) == []
    safe = {SAFE: "true", DO_NOT_DISRUPT: "false"}
    assert why(autoscaler, annotations=safe, **TIGHT) == said  # no annotation lifts it


def test_cluster_autoscalers_rules() -> None:
    assert why(CA, kind="", name="") == ["no controller"]
    assert why(CA, kind="Cluster") == ["a custom controller (Cluster)"]
    for built_in in ("ReplicaSet", "StatefulSet", "Job", "ReplicationController"):
        assert why(CA, kind=built_in) == []
    disk = {"empty_dir": ["scratch", "shm"], "in_memory": ["shm"], "host_path": ["logs"]}
    assert why(CA, **disk) == ["local storage (logs, scratch)"]  # not the one in memory
    assert why(CA, empty_dir=["shm"], in_memory=["shm"]) == []
    listed = {SAFE_VOLUMES: "scratch, logs"}
    assert why(CA, annotations=listed, **disk) == []  # every local volume named safe
    assert why(CA, annotations={SAFE_VOLUMES: "scratch"}, **disk) == ["local storage (logs)"]
    assert why(CA, annotations={SAFE: "false"}) == [f"annotated {SAFE}: false"]
    # "true" lifts no controller, local storage and kube-system, as its FAQ says
    free = {"annotations": {SAFE: "true"}, "kind": "", "name": "", **disk}
    assert why(CA, **free) == [] and why(CA, ns="kube-system", **free) == []
    assert why(CA, ns="kube-system") == ["a kube-system pod with no PodDisruptionBudget"]
    assert why(CA, ns="kube-system", budget="dns", allowed=1) == []
    assert why(CA, annotations={DO_NOT_DISRUPT: "true"}) == []  # Karpenter's, not its


def test_karpenters_rules() -> None:
    """Its annotation as its docs give it: "true" for good, a duration for that long
    after the pod started, anything else ignored."""
    noted = lambda value: {"annotations": {DO_NOT_DISRUPT: value}}  # noqa: E731
    assert why(KARPENTER, **noted("true")) == [f"annotated {DO_NOT_DISRUPT}: true"]
    lasting = [f"annotated {DO_NOT_DISRUPT}: 30m, for that long after it started"]
    assert why(KARPENTER, started=NOW - 1799, **noted("30m")) == lasting
    assert why(KARPENTER, started=NOW - 1800, **noted("30m")) == []  # over
    assert why(KARPENTER, **noted("30m")) == lasting  # its start not known: still held
    assert why(KARPENTER, started=NOW - 5399, **noted("1h30m")) != []
    assert why(KARPENTER, started=NOW - 5400, **noted("1h30m")) == []
    for ignored in ("false", "soon", "30", "m", "30m!", ""):
        assert why(KARPENTER, **noted(ignored)) == [], ignored
    # nothing in its docs about these: they block Cluster Autoscaler, not Karpenter
    others = {"kind": "", "name": "", "empty_dir": ["scratch"], "host_path": ["logs"]}
    assert why(KARPENTER, annotations={SAFE: "false"}, **others) == []
    assert why(KARPENTER, ns="kube-system") == []
    assert blockers.go_duration("1.5h") == 5400 and blockers.go_duration("90s") == 90
    assert blockers.go_duration("250ms") == 0.25


def test_with_no_autoscaler_named_the_rules_are_kubectl_drains() -> None:
    assert why(None, kind="", name="") == ["no controller"]
    assert why(None, kind="Cluster") == ["a custom controller (Cluster)"]
    assert why(None, empty_dir=["shm", "scratch"], in_memory=["shm"]) == [
        "emptyDir data (scratch, shm)"  # in memory or not: it is gone with the node
    ]
    assert why(None, empty_dir=["shm"], in_memory=["shm"]) == ["emptyDir data (shm)"]
    assert why(None, host_path=["logs"]) == []  # no local data to drain
    assert why(None, annotations={SAFE: "false", DO_NOT_DISRUPT: "true"}) == []
    assert why(CA, annotations={DO_NOT_DISRUPT: "30m"}) == []
    assert why(None, ns="kube-system") == []
    assert why(None, annotations={SAFE: "true"}, kind="", name="") == ["no controller"]


# --- the verdict -------------------------------------------------------------------------

OWN = {"namespace": "shop", "pod": "api-1", "cpu": 0.5, "memory": 1.0, "bound": False}
AGENT = {"namespace": "shop", "pod": "agent-x", "cpu": 0.1, "memory": 1.0, "bound": True}


def read_of(*nodes: dict[str, Any]) -> dict[str, Any]:
    return found(*nodes) | {"at": NOW}


def n1(**kw: Any) -> dict[str, Any]:
    """n1: one pod of the Contract's, its DaemonSet's agent and kube-proxy."""
    return a_node("n1", 4.0, 16.0, **({"pods": 3, "bound": 2, "own": [OWN, AGENT]} | kw))


def got(report: dict[str, Any] | None, *nodes: dict[str, Any], now: float = NOW, **cl: Any) -> Any:
    cluster = actuated("acme", A_TOKEN, **cl).clusters[0]
    stored = None if report is None else {"received": NOW, **report}
    return blockers.verdicts(read_of(*(nodes or (n1(),))), stored, cluster, now)


def test_a_node_is_clear_only_when_every_pod_on_it_is_known_and_none_blocks() -> None:
    assert got(a_report()) == {"n1": {"state": "clear", "why": None, "blockers": []}}
    elsewhere = a_report(a_pod("db-0", node="n2", **TIGHT))
    assert got(elsewhere)["n1"]["state"] == "clear"


def test_a_duration_is_judged_at_the_time_the_verdict_is_given() -> None:
    held = a_report(a_pod(annotations={DO_NOT_DISRUPT: "30m"}, started=NOW - 600))
    assert got(held, autoscaler=KARPENTER)["n1"]["state"] == "blocked"
    assert got(held, autoscaler=KARPENTER, now=NOW + 1200)["n1"]["state"] == "clear"


def test_a_node_with_a_blocker_is_blocked_with_the_pod_and_why() -> None:
    report = a_report(a_pod(**TIGHT), a_pod("job-1", kind="", name=""), a_pod("fine"))
    assert got(report)["n1"] == {
        "state": "blocked",
        "why": None,
        "blockers": [
            {
                "namespace": "shop",
                "pod": "api-1",
                "reasons": ["PodDisruptionBudget tight allows no disruption"],
            },
            {"namespace": "shop", "pod": "job-1", "reasons": ["no controller"]},
        ],
    }
    # by the rules of who removes nodes there: Karpenter evicts a pod with no controller
    assert [b["pod"] for b in got(report, autoscaler=KARPENTER)["n1"]["blockers"]] == ["api-1"]
    lone = a_report(a_pod("job-1", kind="", name=""))
    assert got(lone, autoscaler=KARPENTER)["n1"]["state"] == "clear"
    assert got(lone, autoscaler=CA)["n1"]["state"] == "blocked"


def unknown(why_: str) -> dict[str, Any]:
    return {"state": "unknown", "why": why_, "blockers": []}


def test_without_a_fresh_report_no_node_is_clear() -> None:
    cluster = actuated("acme", A_TOKEN).clusters
    no_actuator = blockers.verdicts(read_of(n1()), {"received": NOW, **a_report()}, cluster[1], NOW)
    assert no_actuator["n1"] == unknown("the cluster runs no Actuator")
    assert got(None)["n1"] == unknown("its Actuator has reported no Blockers yet")
    blocked = a_report(a_pod(**TIGHT))
    assert STALE == 7200
    assert got(blocked, now=NOW + STALE)["n1"]["state"] == "blocked"  # two hours: still good
    assert got(blocked, now=NOW + STALE + 1)["n1"] == unknown(
        "its Actuator's last report is from 1970-01-12 13:46 UTC"
    )
    # by when the Hub got it: the Actuator's own clock says nothing
    future = a_report(a_pod(**TIGHT), at=NOW + 10 * STALE)
    assert got(future, now=NOW + STALE + 1)["n1"]["state"] == "unknown"
    past = a_report(at=0.0)
    assert got(past)["n1"]["state"] == "clear"


def test_what_the_hub_was_not_told_is_unknown_never_clear() -> None:
    cut = a_report(truncated=True)
    assert got(cut)["n1"] == unknown("its Actuator's report was cut at 1000 pods")
    # a Blocker it did name stands: positive evidence
    named = got(a_report(a_pod(**TIGHT), truncated=True))["n1"]
    assert (named["state"], named["why"]) == ("blocked", None)
    unread = a_report(namespaces=[])
    assert got(unread)["n1"] == unknown("its Actuator could not read shop")
    only_bound = n1(own=[AGENT], pods=2)  # its DaemonSet's pod alone: nothing to read there
    assert got(unread, only_bound)["n1"]["state"] == "clear"
    hidden = n1(pods=5)  # two more pods, outside the Contract's namespaces, not DaemonSets'
    assert got(a_report(), hidden)["n1"] == unknown("2 pods not visible")
    assert got(a_report(), n1(pods=4))["n1"] == unknown("1 pod not visible")
    old = n1()
    del old["bound"]  # stored by a run of 15.1a
    assert got(a_report(), old)["n1"] == unknown(
        "its pods were read before the Hub counted the ones that stay with a node"
    )


# --- without a usable report: what Prometheus shows (slice 15.1c) ------------------------


def pod_of(name: str, kind: str | None, ns: str = "shop", bound: bool = False) -> dict[str, Any]:
    p = {"namespace": ns, "pod": name, "cpu": 0.1, "memory": 1.0, "bound": bound}
    return p if kind is None else p | {"kind": kind}


def bare(*own: dict[str, Any], budgets: list[dict[str, str]] | None = None, **cl: Any) -> Any:
    """n1's verdict in a cluster that runs no Actuator (`edge`), from its stored read."""
    cluster = actuated("acme", A_TOKEN).clusters[1].model_copy(update=cl)
    read = read_of(n1(own=list(own), pods=len(own), bound=sum(p["bound"] for p in own)))
    read["budgets"] = budgets or []
    return blockers.verdicts(read, None, cluster, NOW)["n1"]


NO_ACTUATOR = "the cluster runs no Actuator"


def test_without_an_actuator_a_pod_with_no_controller_still_blocks() -> None:
    lone, operated = pod_of("solo", ""), pod_of("pg-0", "Cluster")
    assert bare(pod_of("api-1", "ReplicaSet"), lone, operated) == {
        "state": "blocked",
        "why": None,
        "blockers": [
            {"namespace": "shop", "pod": "solo", "reasons": ["no controller"]},
            {"namespace": "shop", "pod": "pg-0", "reasons": ["a custom controller (Cluster)"]},
        ],
    }
    # under Cluster Autoscaler's rules an annotation could lift it, and only an Actuator
    # sees annotations: blocked, and said
    said = bare(lone, autoscaler=CA)["blockers"][0]["reasons"]
    assert said == [
        "no controller (unless annotated safe-to-evict: true, which only an Actuator can see)"
    ]
    # Karpenter's docs name no such rule
    assert bare(lone, operated, autoscaler=KARPENTER) == unknown(NO_ACTUATOR)


def test_without_an_actuator_a_node_is_never_clear() -> None:
    for kind in ("ReplicaSet", "StatefulSet", "Job", "ReplicationController"):
        assert bare(pod_of("api-1", kind)) == unknown(NO_ACTUATOR)
    assert bare() == unknown(NO_ACTUATOR)  # not even an empty one: its volumes go unseen
    assert bare(pod_of("agent-x", "DaemonSet", bound=True)) == unknown(NO_ACTUATOR)
    assert bare(pod_of("etcd", "", bound=True)) == unknown(NO_ACTUATOR)  # stays with it
    assert bare(pod_of("old", None)) == unknown(NO_ACTUATOR)  # read before 15.1c: not known


def test_a_budget_at_0_is_named_where_its_namespace_has_pods_on_the_node() -> None:
    """Prometheus doesn't say which pods a PodDisruptionBudget covers: named, not blamed."""
    tight = [{"namespace": "shop", "budget": "web"}, {"namespace": "pay", "budget": "api"}]
    got_ = bare(pod_of("api-1", "ReplicaSet"), budgets=tight)
    assert got_ == unknown(f"{NO_ACTUATOR}; PodDisruptionBudget web of shop allows no disruption")
    both = bare(
        pod_of("api-1", "ReplicaSet"), pod_of("pay-1", "ReplicaSet", ns="pay"), budgets=tight
    )
    assert both["why"].endswith("PodDisruptionBudget web of shop, api of pay allows no disruption")
    assert bare(pod_of("pay-1", "ReplicaSet", ns="pay"), budgets=tight[:1]) == unknown(NO_ACTUATOR)
    only_bound = bare(pod_of("agent-x", "DaemonSet", bound=True), budgets=tight)
    assert only_bound == unknown(NO_ACTUATOR)
    blocked = bare(pod_of("solo", ""), budgets=tight)
    assert (blocked["state"], blocked["why"]) == ("blocked", None)


def test_an_actuator_whose_report_is_missing_or_too_old_falls_back_the_same_way() -> None:
    lone = n1(own=[pod_of("solo", ""), AGENT], pods=3, bound=2)
    never = got(None, lone)["n1"]
    assert never["state"] == "blocked" and never["blockers"][0]["pod"] == "solo"
    old = got(a_report(), lone, now=NOW + STALE + 1)["n1"]  # a report that said nothing of it
    assert old["state"] == "blocked"
    quiet = n1(own=[pod_of("api-1", "ReplicaSet"), AGENT])
    assert got(None, quiet)["n1"] == unknown("its Actuator has reported no Blockers yet")
    # a fresh report is believed over Prometheus: it sees the annotation that lifts it
    lifted = a_report(a_pod("solo", kind="", name="", annotations={SAFE: "true"}))
    assert got(lifted, lone, autoscaler=CA)["n1"]["state"] == "clear"


def test_a_pod_in_a_namespace_the_contract_left_is_not_shown() -> None:
    report = a_report(a_pod("x", ns="left", **TIGHT), namespaces=["shop", "left"])
    assert got(report)["n1"]["state"] == "clear"
    assert got(report, namespaces=["shop", "left"])["n1"]["state"] == "blocked"


# --- shown -------------------------------------------------------------------------------


def test_the_report_gives_each_node_its_verdict_by_its_clusters_rules() -> None:
    c = actuated("acme", A_TOKEN, autoscaler=KARPENTER)
    stored = {"test": read_of(n1()), "edge": read_of(n1())}
    reports = {"test": {"received": NOW, **a_report(a_pod(**TIGHT))}}
    test, edge = sizing.nodes_report(stored, c, reports, NOW)
    assert (test["rules"], test["reported"]) == ("Karpenter's rules", NOW)
    assert test["nodes"][0]["verdict"]["state"] == "blocked"
    assert edge["rules"] == "kubectl drain's rules (the Contract names no autoscaler)"
    assert edge["reported"] is None and edge["nodes"][0]["verdict"]["state"] == "unknown"
    (later,) = sizing.nodes_report({"test": stored["test"]}, c, reports, NOW + STALE + 1)
    assert later["nodes"][0]["verdict"]["state"] == "unknown"
    (never,) = sizing.nodes_report({"test": stored["test"]}, c)
    assert never["nodes"][0]["verdict"]["why"] == "its Actuator has reported no Blockers yet"


def test_the_nodes_route_shows_each_contracts_own_actuators_report(
    settings: Settings,
    database_url: str,
    app_dir: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two Contracts, a cluster of the same name each: A's Actuator's report is A's."""
    acme, bank = actuated("acme", A_TOKEN), actuated("bank", B_TOKEN)
    settings.console_operators = {OPERATOR}
    monkeypatch.setattr("squero.api.now", lambda: NOW)
    app = hub(settings, database_url, acme, bank)
    with TestClient(app) as c:
        import json

        for cid in ("acme", "bank"):
            sql(
                database_url,
                (
                    "insert into cluster_rooms values (%s, 'test', %s, %s)",
                    (cid, NOW, json.dumps(found(n1()))),
                ),
            )
        report = a_report(a_pod("pod-of-acme", **TIGHT))
        assert (
            c.post("/actuator/blockers", headers=as_actuator(A_TOKEN), json=report).status_code
            == 204
        )
        mine = c.get("/api/v1/contracts/acme/nodes", headers=VIEW).json()["clusters"][0]
        theirs = c.get("/api/v1/contracts/bank/nodes", headers=VIEW)
    assert mine["nodes"][0]["verdict"]["state"] == "blocked" and mine["reported"] is not None
    assert mine["nodes"][0]["verdict"]["blockers"][0]["pod"] == "pod-of-acme"
    assert mine["nodes"][0]["bound"] == 2
    assert "pod-of-acme" not in theirs.text
    other = theirs.json()["clusters"][0]
    assert other["reported"] is None and other["nodes"][0]["verdict"]["state"] == "unknown"
