"""The Actuator (slice 12.1): the Hub's two routes for it, bound by its token to one
Contract's cluster, and the Actuator resizing a Deployment's pods in place."""

import asyncio
import base64
import json
import ssl
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call

import httpx
import psycopg
import pytest
import yaml
from fastapi.testclient import TestClient
from kubernetes import client as k
from kubernetes.client.exceptions import ApiException
from pydantic import SecretStr

from squero import actuator, contracts
from squero.api import create_app
from squero.config import Settings
from squero.store import Store, connect
from tests.conftest import scripted

A_TOKEN = "a" * 40
B_TOKEN = "b" * 40
MI = 2**20
ROOT = Path(__file__).resolve().parent.parent


def actuated(cid: str, token: str, **cluster: Any) -> contracts.Contract:
    """A Contract whose cluster `test` (namespace shop) runs an Actuator; `edge` doesn't."""
    data = {
        "id": cid,
        "name": cid,
        "clusters": [
            {
                "name": "test",
                "context": "test",
                "namespaces": ["shop"],
                "actuator_token_secret": "act",
                **cluster,
            },
            {"name": "edge", "context": "edge", "namespaces": ["shop"]},
        ],
        "integrations": [{"name": "kubernetes"}],
    }
    return contracts.build(data, secrets={"act": SecretStr(token), "r": SecretStr("r" * 40)})


def hub(settings: Settings, database_url: str, *cs: contracts.Contract, **kw: Any) -> Any:
    settings.database_url = SecretStr(database_url)
    settings.approver_token = SecretStr(kw.get("approver", "approver-token"))
    return create_app(settings, scripted(), lambda c: None, {c.id: c for c in cs})  # type: ignore[arg-type]


def sql(database_url: str, *statements: tuple[str, tuple[Any, ...]]) -> list[Any]:
    with psycopg.connect(database_url) as conn:
        out = [conn.execute(q, args) for q, args in statements]
        return out[-1].fetchall() if out[-1].description else []


REVERT = (
    "insert into reverts (contract_id, cluster, namespace, kind, name, container, asked_by, "
    "asked_at) values (%s, 'test', %s, %s, %s, 'app', 'mcp:x', %s)",
)


def approve(cid: str, run_id: int, cluster: str = "test", ns: str = "shop", **more: Any) -> Any:
    keys = {"kind": "Deployment", "name": "api", "container": "app", **more}
    return (
        "insert into approved_sizings values (%s, %s, %s, %s, %s, %s, %s, 0.115, null, 't', 1)",
        (cid, run_id, cluster, ns, keys["kind"], keys["name"], keys["container"]),
    )


def run(cid: str, clusters: str = "{}") -> Any:
    return (
        "insert into sizing_runs (contract_id, at, clusters) values (%s, 1, %s)",
        (cid, clusters),
    )


def as_actuator(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- the Hub's side -------------------------------------------------------------------------


def test_an_actuator_gets_only_its_own_clusters_current_approved_deployments(
    settings: Settings, database_url: str
) -> None:
    app = hub(settings, database_url, actuated("acme", A_TOKEN), actuated("bank", B_TOKEN))
    with TestClient(app) as c:
        sql(
            database_url,
            run("acme"),  # run 1, still going: supersedes nothing
            approve("acme", 1),
            approve("acme", 1, name="db", kind="StatefulSet"),  # slice 12.8a
            # db reverted before that approval: approved again (slice 12.9b), still to undo
            (*REVERT, ("acme", "shop", "StatefulSet", "db", 0.5)),
            approve("acme", 1, name="agent", kind="DaemonSet"),
            approve("acme", 1, name="web", kind="Rollout"),  # Argo Rollouts: slice 12.8b
            approve("acme", 1, name="nightly", kind="CronJob"),  # slice 12.8c
            approve("acme", 1, name="once", kind="Job"),  # a Job of no CronJob: never sent
            approve("acme", 1, cluster="edge"),  # another cluster of acme's
            approve("acme", 1, ns="old"),  # no longer one of its namespaces
            approve("bank", 1, name="ledger"),
        )
        kinds = {"kinds": "Deployment,StatefulSet,DaemonSet,Rollout,CronJob,Job"}
        mine = c.get("/actuator/desired", headers=as_actuator(A_TOKEN), params=kinds)
        three = {"kinds": "Deployment,StatefulSet,DaemonSet"}  # 0.44.0: no Rollouts
        before = c.get("/actuator/desired", headers=as_actuator(A_TOKEN), params=three)
        old = c.get("/actuator/desired", headers=as_actuator(A_TOKEN))  # 0.43.0: names none
        theirs = c.get("/actuator/desired", headers=as_actuator(B_TOKEN))
        wrong = c.get("/actuator/desired", headers=as_actuator(A_TOKEN + "x"))
        none = c.get("/actuator/desired")
    wanted = {"run_id": 1, "namespace": "shop", "kind": "Deployment", "container": "app"}
    wanted["again"] = False  # none approved after a Revert (slice 12.9b)
    wanted["types"] = {}  # none sized per instance type (slice 15.3b)
    assert mine.json() == {
        "kill_switch": False,
        "sizings": [
            {**wanted, "kind": "CronJob", "name": "nightly", "cpu": 0.115, "memory": None},
            {**wanted, "kind": "DaemonSet", "name": "agent", "cpu": 0.115, "memory": None},
            {**wanted, "name": "api", "cpu": 0.115, "memory": None},
            {**wanted, "kind": "Rollout", "name": "web", "cpu": 0.115, "memory": None},
            {
                **wanted,
                "kind": "StatefulSet",
                "name": "db",
                "cpu": 0.115,
                "memory": None,
                "again": True,
            },
        ],
        "automated": [],
        "reverts": [
            {"id": 1, "namespace": "shop", "kind": "StatefulSet", "name": "db", "container": "app"}
        ],
        "eviction": "off",
        "oom": [],
        "oom_max": None,
        "oom_expires": None,
        "boosts": [],
        "burst": [],
        "burst_run": None,
        "burst_max": None,
        "burst_expires": None,
        "blockers": ["shop"],  # slice 15.1b: under the Kill switch too
        "node_types": None,  # slice 15.3b: no Sizing here has types
        "placement": None,  # slice 15.2a: the cluster has none
    }
    assert old.json()["sizings"] == [{**wanted, "name": "api", "cpu": 0.115, "memory": None}]
    known = ("Deployment", "StatefulSet", "DaemonSet")  # what 0.44.0 names
    assert before.json()["sizings"] == [x for x in mine.json()["sizings"] if x["kind"] in known]
    assert theirs.json()["sizings"] == [{**wanted, "name": "ledger", "cpu": 0.115, "memory": None}]
    assert wrong.status_code == 401 and none.status_code in (401, 403)


def test_the_kill_switch_and_a_later_run_leave_nothing_to_apply(
    settings: Settings, database_url: str
) -> None:
    app = hub(settings, database_url, actuated("acme", A_TOKEN), actuated("bank", B_TOKEN))
    with TestClient(app) as c:
        sql(database_url, run("acme"), approve("acme", 1), approve("bank", 1))
        sql(database_url, ("insert into kill_switches values ('acme', 't', 1)", ()))
        killed = c.get("/actuator/desired", headers=as_actuator(A_TOKEN)).json()
        # bank's run 2 read its cluster test: run 1's approval was never applied, and won't be
        sql(database_url, run("bank", '{"edge": null}'), run("bank", '{"test": null}'))
        superseded = c.get("/actuator/desired", headers=as_actuator(B_TOKEN)).json()
    assert killed == {
        "kill_switch": True,
        "sizings": [],
        "automated": [],
        "reverts": [],
        "eviction": "off",
        "oom": [],
        "oom_max": None,
        "oom_expires": None,
        "boosts": [],
        "burst": [],
        "burst_run": None,
        "burst_max": None,
        "burst_expires": None,
        "blockers": ["shop"],  # slice 15.1b: under the Kill switch too
        "node_types": None,  # slice 15.3b: no Sizing here has types
        "placement": None,  # slice 15.2a: the cluster has none
    }
    assert superseded == {
        "kill_switch": False,
        "sizings": [],
        "automated": [],
        "reverts": [],
        "eviction": "off",
        "oom": [],
        "oom_max": None,
        "oom_expires": None,
        "boosts": [],
        "burst": [],
        "burst_run": None,
        "burst_max": None,
        "burst_expires": None,
        "blockers": ["shop"],  # slice 15.1b: under the Kill switch too
        "node_types": None,  # slice 15.3b: no Sizing here has types
        "placement": None,  # slice 15.2a: the cluster has none
    }


def report(**more: Any) -> dict[str, Any]:
    base = {
        "namespace": "shop",
        "kind": "Deployment",
        "name": "api",
        "container": "app",
        "run_id": 1,
        "outcome": "applied",
        "cpu": 0.115,
        "memory": None,
        "pods": 2,
        "reason": "2 of 2 pods resized in place",
        "at": 100.0,
    }
    return base | more


def test_reports_are_stored_as_the_tokens_contract_and_cluster_once(
    settings: Settings, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = hub(settings, database_url, actuated("acme", A_TOKEN), actuated("bank", B_TOKEN))
    with TestClient(app) as c:
        ok = c.post("/actuator/reports", json=[report()], headers=as_actuator(A_TOKEN))
        again = c.post("/actuator/reports", json=[report()], headers=as_actuator(A_TOKEN))
        later = report(outcome="handed_back", at=160.0, pods=0)  # slice 12.2
        c.post("/actuator/reports", json=[later], headers=as_actuator(B_TOKEN))
        outside = c.post(
            "/actuator/reports",
            json=[report(at=200.0), report(namespace="kube-system", at=200.0)],
            headers=as_actuator(A_TOKEN),
        )
        wrong = c.post("/actuator/reports", json=[report()], headers=as_actuator("x" * 40))
        unknown = c.post(
            "/actuator/reports", json=[report(outcome="done")], headers=as_actuator(A_TOKEN)
        )
        monkeypatch.setattr(actuator, "MAX_REPORTS", 1)
        many = c.post("/actuator/reports", json=[report()] * 2, headers=as_actuator(A_TOKEN))
    assert (ok.status_code, again.status_code) == (204, 204)
    assert outside.status_code == 422 and "kube-system" in outside.json()["detail"]
    assert (wrong.status_code, unknown.status_code, many.status_code) == (401, 422, 413)
    rows = sql(
        database_url,
        ("select contract_id, cluster, outcome, pods, at from actuations order by at", ()),
    )
    assert rows == [
        ("acme", "test", "applied", 2, 100.0),
        ("bank", "test", "handed_back", 0, 160.0),
    ]


def test_actuations_are_kept_90_days(database_url: str) -> None:
    async def go() -> list[dict[str, Any]]:
        async with connect(database_url) as pool:
            store = Store(pool)
            await store.migrate()
            day = 86400.0
            await store.record_actuations("acme", "test", [report(at=0.0)], 0.0)
            await store.record_actuations("acme", "test", [report(at=10 * day)], 10 * day)
            # dated ten years ahead by its Actuator (slice 12.9c): as old as its arrival
            ahead = [report(at=3650 * day, name="ahead")]
            await store.record_actuations("acme", "test", ahead, 0.0)
            await store.record_actuations("bank", "test", [report(at=0.0)], 0.0)
            await store.prune_sizings("acme", 90 * day + 1)
            return await store.actuations("acme", 0.0) + await store.actuations("bank", 0.0)

    assert [(r["contract_id"], r["name"], r["at"]) for r in asyncio.run(go())] == [
        ("acme", "api", 10 * 86400.0),
        ("bank", "api", 0.0),
    ]


def test_an_actuator_token_is_no_other_token(settings: Settings, database_url: str) -> None:
    def refused(*cs: contracts.Contract, approver: str = "approver-token") -> str:
        with pytest.raises(RuntimeError) as e:
            hub(settings, database_url, *cs, approver=approver)
        return str(e.value)

    relayed = actuated("acme", A_TOKEN, via="relay", context=None, relay_token_secret="act")
    assert "its Actuator token is another Relay's or Actuator's" in refused(relayed)
    assert "another Relay's or Actuator's" in refused(
        actuated("acme", A_TOKEN), actuated("bank", A_TOKEN)
    )
    assert "an Actuator token is the approver token" in refused(
        actuated("acme", A_TOKEN), approver=A_TOKEN
    )
    hooked = actuated("acme", A_TOKEN)
    hooked = hooked.model_copy(update={"webhook_token_sha256": contracts.token_hash(B_TOKEN)})
    assert "bank/test: its Actuator token is 'acme''s webhook token" in refused(
        hooked, actuated("bank", B_TOKEN)
    )
    with pytest.raises(ValueError, match="Actuator token is too short"):
        from squero import relay

        relay.tokens({"acme": actuated("acme", "x" * 31)}, "Actuator")


# --- the Actuator's side ------------------------------------------------------------------


def owner(kind: str, name: str, uid: str = "") -> k.V1OwnerReference:
    return k.V1OwnerReference(api_version="apps/v1", kind=kind, name=name, uid=uid)


def container(
    requests: dict[str, str], limits: dict[str, str] | None = None, policy: Any = None
) -> k.V1Container:
    return k.V1Container(
        name="app",
        resources=k.V1ResourceRequirements(requests=requests, limits=limits),
        resize_policy=policy,
    )


def pod(
    name: str, rs: str, phase: str = "Running", deleting: bool = False, **requests: str
) -> k.V1Pod:
    meta = k.V1ObjectMeta(
        name=name, labels={"app": "api"}, owner_references=[owner("ReplicaSet", rs)]
    )
    meta.deletion_timestamp = datetime.now(UTC) if deleting else None
    return k.V1Pod(
        metadata=meta,
        spec=k.V1PodSpec(containers=[container(requests or {"cpu": "1", "memory": "512Mi"})]),
        status=k.V1PodStatus(phase=phase),
    )


KEY = "shop/Deployment/api/app"


def cluster(
    limits: dict[str, str] | None = None, policy: Any = None, requests: Any = None
) -> tuple[Any, Any]:
    """api (uid u1) with ReplicaSet api-1; another Deployment's api-x shares its labels. A
    resize changes the pod's spec, and the kubelet allocates it at once, Ready."""
    core, apps = MagicMock(), MagicMock()
    template = container({"cpu": "1"} if requests is None else requests, limits, policy)
    apps.read_namespaced_deployment.return_value = k.V1Deployment(
        metadata=k.V1ObjectMeta(name="api", namespace="shop", uid="u1"),
        spec=k.V1DeploymentSpec(
            replicas=2,
            selector=k.V1LabelSelector(match_labels={"app": "api"}),
            template=k.V1PodTemplateSpec(spec=k.V1PodSpec(containers=[template])),
        ),
    )
    apps.list_namespaced_replica_set.return_value = k.V1ReplicaSetList(
        items=[
            k.V1ReplicaSet(
                metadata=k.V1ObjectMeta(name=n, owner_references=[owner("Deployment", d, u)]),
                spec=k.V1ReplicaSetSpec(selector=k.V1LabelSelector()),
            )
            for n, d, u in (("api-1", "api", "u1"), ("api-x", "api-old", "u2"))
        ]
    )
    pods = [
        pod("api-1-a", "api-1"),
        pod("api-1-b", "api-1"),
        pod("api-1-done", "api-1", phase="Succeeded"),
        pod("api-1-gone", "api-1", deleting=True),
        pod("api-x-a", "api-x"),
    ]
    for p in pods:
        p.metadata.namespace = "shop"
    core.list_namespaced_pod.return_value = k.V1PodList(items=pods)

    def patch(name: str, ns: str, body: Any) -> None:
        live = core.list_namespaced_pod.return_value.items  # pods added later included
        c = next(p for p in live if p.metadata.name == name).spec.containers[0]
        asked = body["spec"]["containers"][0]["resources"]
        c.resources.requests = {**(c.resources.requests or {}), **(asked.get("requests") or {})}
        if asked.get("limits"):  # a Step-up's (slice 13.1b)
            c.resources.limits = {**(c.resources.limits or {}), **asked["limits"]}

    def read(name: str, ns: str) -> Any:
        p = next(p for p in core.list_namespaced_pod.return_value.items if p.metadata.name == name)
        allocated = k.V1ResourceRequirements(requests=p.spec.containers[0].resources.requests)
        p.status = k.V1PodStatus(
            phase="Running",
            conditions=[k.V1PodCondition(type="Ready", status="True")],
            container_statuses=[status("app", allocated)],
        )
        return p

    core.patch_namespaced_pod_resize.side_effect = patch
    core.read_namespaced_pod.side_effect = read
    core.list_namespaced_limit_range.return_value = k.V1LimitRangeList(items=[])
    core.list_namespaced_resource_quota.return_value = k.V1ResourceQuotaList(items=[])
    return core, apps


def status(name: str, resources: Any) -> Any:
    return k.V1ContainerStatus(
        name=name, image="app", image_id="", ready=True, restart_count=0, resources=resources
    )


def scalers(hpas: list[Any] | None = None, vpas: Any = None) -> tuple[Any, Any]:
    """(AutoscalingV2Api, CustomObjectsApi) mocks: these HPAs, these VPAs (an exception: raised)."""
    autoscaling, custom = MagicMock(), MagicMock()
    autoscaling.list_namespaced_horizontal_pod_autoscaler.return_value = MagicMock(items=hpas or [])
    if isinstance(vpas, Exception):
        custom.list_namespaced_custom_object.side_effect = vpas
    else:
        custom.list_namespaced_custom_object.return_value = {"items": vpas or []}
    return autoscaling, custom


def budgets(*selectors: Any) -> Any:
    """A PolicyV1Api mock: a PodDisruptionBudget per selector (an exception: raised)."""
    policy = MagicMock()
    if selectors and isinstance(selectors[0], Exception):
        policy.list_namespaced_pod_disruption_budget.side_effect = selectors[0]
        return policy
    items = [
        k.V1PodDisruptionBudget(spec=k.V1PodDisruptionBudgetSpec(selector=s)) for s in selectors
    ]
    policy.list_namespaced_pod_disruption_budget.return_value = MagicMock(items=items)
    return policy


PDB = budgets(k.V1LabelSelector(match_labels={"app": "api"}))  # covers api's pods


def start(core: Any, apps: Any, w: Any, at: float = 0.0, **scaling: Any) -> Any:
    return actuator.apply(core, apps, w, at, *scalers(**scaling))


def wanted(run_id: int = 1, cpu: float | None = 0.115, memory: float | None = 96 * MI) -> Any:
    return actuator.Wanted(
        run_id=run_id,
        namespace="shop",
        kind="Deployment",
        name="api",
        container="app",
        cpu=cpu,
        memory=memory,
    )


def resized(core: Any) -> list[tuple[str, Any]]:
    return [(c.args[0], c.args[2]) for c in core.patch_namespaced_pod_resize.call_args_list]


def body(**requests: str) -> Any:
    return {"spec": {"containers": [{"name": "app", "resources": {"requests": requests}}]}}


def test_a_canary_first_then_one_pod_at_a_time_never_above_the_limit() -> None:
    core, apps = cluster(limits={"memory": "64Mi"})
    a, held = start(core, apps, wanted())
    both = body(cpu="115m", memory="64Mi")
    assert a is None and resized(core) == [("api-1-a", both)]
    assert held == {
        "run_id": 1,
        "outcome": "rolling",
        "requests": {"cpu": "115m", "memory": "64Mi"},
        "template": {"cpu": "1", "memory": None},  # what a person's change is told from
        "at": 0.0,
        "pods": {"api-1-a": {"at": 0.0, "restarts": 0}},  # watched from its resize (12.4)
        "pod": "api-1-a",
        "since": 0.0,
        "canary_until": 600.0,
        "resized": 1,
        "notes": "; capped at its limit: ['memory']",
    }
    assert actuator.advance(core, apps, KEY, held, 599.0) is None  # the canary's 10 minutes
    assert len(resized(core)) == 1
    assert actuator.advance(core, apps, KEY, held, 600.0) is None
    assert resized(core) == [("api-1-a", both), ("api-1-b", both)]
    assert (held["pod"], held["since"], held["resized"]) == ("api-1-b", 600.0, 2)
    done = actuator.advance(core, apps, KEY, held, 660.0)
    assert done is not None and (done.outcome, done.pods, done.cpu, done.memory) == (
        "applied",
        2,
        0.115,
        64.0 * MI,
    )
    assert (
        done.reason
        == "2 of 2 pods resized in place, one at a time; capped at its limit: ['memory']"
    )
    assert held == {
        "run_id": 1,
        "outcome": "applied",
        "requests": {"cpu": "115m", "memory": "64Mi"},
        "template": {"cpu": "1", "memory": None},
        "at": 0.0,
        "pods": {n: {"at": t, "restarts": 0} for n, t in (("api-1-a", 0.0), ("api-1-b", 600.0))},
        "watch_until": 660.0 + 3600,  # an hour for a rollback
    }
    labels = apps.list_namespaced_replica_set.call_args.kwargs["label_selector"]
    assert labels == "app=api" == core.list_namespaced_pod.call_args.kwargs["label_selector"]


def test_a_request_left_as_it_is_and_pods_that_already_match_are_not_touched() -> None:
    core, apps = cluster(limits={"cpu": "2"})
    core.list_namespaced_pod.return_value.items[1].spec.containers[0].resources.requests = {
        "cpu": "115m"
    }
    a, held = start(core, apps, wanted(memory=None))
    done = actuator.advance(core, apps, KEY, held, 600.0)
    assert a is None and resized(core) == [("api-1-a", body(cpu="115m"))]  # b already 115m
    assert done is not None and (done.outcome, done.pods, done.memory) == ("applied", 1, None)
    again, _ = start(core, apps, wanted(memory=None))
    assert (again.outcome, again.reason) == ("skipped", "all 2 pods already have these requests")


def refusal(status: int = 422, message: str = "Pod QoS Class may not change") -> ApiException:
    e = ApiException(status=status, reason="Unprocessable Entity")
    e.body = json.dumps({"message": message})
    return e


def pending(reason: str, message: str) -> Any:
    """A pod whose resize the kubelet holds back (PodResizePending)."""
    p = pod("api-1-a", "api-1")
    p.status = k.V1PodStatus(
        conditions=[
            k.V1PodCondition(type="PodResizePending", status="True", reason=reason, message=message)
        ]
    )
    return p


def test_a_resize_the_kubelet_cannot_make_stops_its_rollout() -> None:
    core, apps = cluster()
    _, held = start(core, apps, wanted())
    core.read_namespaced_pod.side_effect = None
    core.read_namespaced_pod.return_value = pending(
        "Infeasible", "Node didn't have enough capacity"
    )
    infeasible = actuator.advance(core, apps, KEY, dict(held), 60.0)
    assert infeasible is not None and (infeasible.outcome, infeasible.reason) == (
        "failed",
        "api-1-a: Infeasible: Node didn't have enough capacity; 0 other pods resized",
    )
    core.read_namespaced_pod.return_value = pending("Deferred", "no room now")
    deferred = dict(held)
    assert actuator.advance(core, apps, KEY, deferred, 599.0) is None  # it may still fit
    late = actuator.advance(core, apps, KEY, deferred, 601.0)
    assert late is not None and late.reason == "api-1-a: deferred for 10 minutes: no room now"
    for condition, message in (
        (k.V1PodCondition(type="Ready", status="False"), "not Ready"),
        (k.V1PodCondition(type="PodResizeInProgress", status="True"), "in progress"),
        (k.V1PodCondition(type="Ready", status="True"), "not yet allocated"),
    ):
        p = pod("api-1-a", "api-1")
        allocated = held["requests"] if message != "not yet allocated" else {"cpu": "1"}
        p.status = k.V1PodStatus(
            conditions=[condition],
            container_statuses=[status("app", k.V1ResourceRequirements(requests=allocated))],
        )
        core.read_namespaced_pod.return_value = p
        assert actuator.advance(core, apps, KEY, dict(held), 599.0) is None
        stuck = actuator.advance(core, apps, KEY, dict(held), 700.0)
        assert stuck is not None and stuck.reason == f"api-1-a: waiting for 10 minutes: {message}"
    core.read_namespaced_pod.side_effect = ApiException(status=500)
    assert actuator.advance(core, apps, KEY, dict(held), 900.0) is None  # the API's trouble: wait
    assert len(resized(core)) == 1  # nothing else was touched


def test_a_canary_gone_or_refused_and_a_refused_next_pod() -> None:
    core, apps = cluster()
    core.patch_namespaced_pod_resize.side_effect = refusal()
    refused, held = start(core, apps, wanted())
    assert (refused.outcome, refused.pods, held["outcome"]) == ("failed", 0, "failed")
    assert refused.reason == "api-1-a: 422: Pod QoS Class may not change"
    actuator.event(core, refused)
    shown = core.create_namespaced_event.call_args.args[1]
    assert (shown.reason, shown.type) == ("SizingFailed", "Warning")
    core, apps = cluster()
    _, held = start(core, apps, wanted())
    core.read_namespaced_pod.side_effect = ApiException(status=404)  # the canary was replaced
    core.patch_namespaced_pod_resize.side_effect = refusal(403, "forbidden")
    next_refused = actuator.advance(core, apps, KEY, held, 600.0)
    assert (
        next_refused is not None and next_refused.reason == "resizing its next pod: 403: forbidden"
    )
    assert next_refused.pods == 1 and "pod" not in held


def hpa(target: str = "api", metrics: Any = "cpu") -> Any:
    """An HPA of `target`; metrics: a resource it scales on by utilization, or a list."""
    h = MagicMock()
    h.spec.scale_target_ref.kind, h.spec.scale_target_ref.name = "Deployment", target
    if isinstance(metrics, str):
        m = MagicMock(type="Resource")
        m.resource.name, m.resource.target.type = metrics, "Utilization"
        metrics = [m]
    h.spec.metrics = metrics
    return h


def test_an_hpa_keeps_the_request_it_divides_by_and_a_vpa_keeps_the_workload() -> None:
    core, apps = cluster()
    vpa = {"spec": {"targetRef": {"kind": "Deployment", "name": "api"}}}
    auto, _ = start(core, apps, wanted(), vpas=[vpa])  # no updatePolicy: Auto
    off = {"spec": {**vpa["spec"], "updatePolicy": {"updateMode": "Off"}}}
    other = {"spec": {"targetRef": {"kind": "Deployment", "name": "web"}}}
    assert auto.reason == "a VPA in mode Auto sizes it" and not resized(core)
    a, held = start(core, apps, wanted(), vpas=[off, other], hpas=[hpa(), hpa("web", "memory")])
    assert a is None and resized(core) == [("api-1-a", body(memory="96Mi"))]
    assert held["notes"] == "; cpu left to its HPA, which scales on its utilization"
    assert held["template"] == {"memory": None}
    defaults, _ = start(core, apps, wanted(memory=None), hpas=[hpa(metrics=[])])
    assert defaults.reason.startswith("nothing its HPA doesn't scale on")  # no metrics: cpu
    by_value = MagicMock(type="Resource")
    by_value.resource.name, by_value.resource.target.type = "cpu", "AverageValue"
    mine = MagicMock(type="ContainerResource")
    mine.container_resource.container, mine.container_resource.name = "app", "memory"
    mine.container_resource.target.type = "Utilization"
    sidecar = MagicMock(type="ContainerResource")
    sidecar.container_resource.container, sidecar.container_resource.name = "proxy", "cpu"
    sidecar.container_resource.target.type = "Utilization"
    core, apps = cluster()
    _, held = start(core, apps, wanted(), hpas=[hpa(metrics=[by_value, mine, sidecar])])
    assert held["requests"] == {"cpu": "115m"}  # only its own container's memory is the HPA's
    missing, _ = start(core, apps, wanted(), vpas=ApiException(status=404))  # no VPA installed
    forbidden, _ = start(core, apps, wanted(), vpas=refusal(403, "forbidden"))
    assert missing is None  # rolling: a cluster without VPAs has none to fight
    assert (forbidden.outcome, forbidden.reason) == ("failed", "checking it: 403: forbidden")


def test_a_resize_changing_the_pods_qos_class_is_skipped() -> None:
    guaranteed = {"cpu": "1", "memory": "512Mi"}
    core, apps = cluster(limits=guaranteed, requests=guaranteed)
    g, _ = start(core, apps, wanted())
    assert g.reason == "it is Guaranteed: its limits must change too"
    core, apps = cluster(limits={"cpu": "115m", "memory": "96Mi"}, requests={"cpu": "1"})
    up, _ = start(core, apps, wanted())
    assert up.reason == "it would turn its pods from Burstable to Guaranteed"
    core, apps = cluster(requests={})
    apps.read_namespaced_deployment.return_value.spec.template.spec.containers[0].resources = None
    best, _ = start(core, apps, wanted())
    assert best.reason == "it would turn its pods from BestEffort to Burstable"
    assert not resized(core)


def limit_range(**item: Any) -> Any:
    return k.V1LimitRangeList(
        items=[
            k.V1LimitRange(
                spec=k.V1LimitRangeSpec(limits=[k.V1LimitRangeItem(type="Container", **item)])
            )
        ]
    )


def quota(hard: dict[str, str], used: dict[str, str]) -> Any:
    return k.V1ResourceQuotaList(
        items=[
            k.V1ResourceQuota(
                metadata=k.V1ObjectMeta(name="team"),
                status=k.V1ResourceQuotaStatus(hard=hard, used=used),
            )
        ]
    )


def test_a_limit_range_or_a_quota_that_would_refuse_it_skips_it_never_clamped() -> None:
    def tried(**set_up: Any) -> Any:
        core, apps = cluster(limits={"memory": "1Gi"})
        for name, value in set_up.items():
            getattr(core, f"list_namespaced_{name}").return_value = value
        a, _ = start(core, apps, wanted(cpu=2.0, memory=None))
        return a.reason if a else "rolling"

    assert (
        tried(limit_range=limit_range(min={"cpu": "3"}))
        == "cpu 2000m is under its LimitRange's minimum 3"
    )
    assert (
        tried(limit_range=limit_range(max={"cpu": "1500m"}))
        == "cpu 2000m is over its LimitRange's maximum 1500m"
    )
    assert tried(resource_quota=quota({"requests.cpu": "3"}, {"requests.cpu": "2"})) == (
        "2 pods at cpu 2000m would pass ResourceQuota team's requests.cpu 3"
    )
    assert tried(resource_quota=quota({"cpu": "4"}, {"cpu": "2"})) == "rolling"  # 2 + 2 x 1 fits
    assert tried(resource_quota=quota({"cpu": "3"}, {"cpu": "2"})) == (
        "2 pods at cpu 2000m would pass ResourceQuota team's cpu 3"
    )
    pod_wide = limit_range(min={"cpu": "3"})
    pod_wide.items[0].spec.limits[0].type = "Pod"
    assert tried(limit_range=pod_wide) == "rolling"  # a Pod's limits, not a container's
    assert tried(resource_quota=quota({"memory": "1"}, {"memory": "1"})) == "rolling"
    core, apps = cluster(limits={"memory": "1Gi"})
    core.list_namespaced_limit_range.return_value = limit_range(
        max_limit_request_ratio={"memory": "4"}
    )
    ratio, _ = start(core, apps, wanted(cpu=None, memory=128 * MI))
    assert ratio.reason == "memory 128Mi under limit 1Gi passes its LimitRange's ratio 4"
    core, apps = cluster()
    core.list_namespaced_resource_quota.return_value = quota(
        {"requests.cpu": "1"}, {"requests.cpu": "3"}
    )
    lower, _ = start(core, apps, wanted())
    assert lower is None  # a decrease never passes a quota, even one already passed


def test_a_resize_that_would_restart_the_container_or_has_nothing_to_resize_is_skipped() -> None:
    policy = [k.V1ContainerResizePolicy(resource_name="memory", restart_policy="RestartContainer")]
    core, apps = cluster(policy=policy)
    restarts, _ = start(core, apps, wanted())
    assert restarts.reason == "its resizePolicy restarts the container for ['memory']"
    assert not resized(core)
    cpu_only, _ = start(core, apps, wanted(memory=None))
    assert cpu_only is None  # a cpu resize doesn't restart it: rolling
    gone = MagicMock()
    gone.read_namespaced_deployment.side_effect = ApiException(status=404)
    missing, _ = start(MagicMock(), gone, wanted())
    core, apps = cluster()
    apps.read_namespaced_deployment.return_value.spec.template.spec.containers[0].name = "web"
    renamed, _ = start(core, apps, wanted())
    core, apps = cluster()
    core.list_namespaced_pod.return_value.items = []
    idle, _ = start(core, apps, wanted())
    apps.list_namespaced_replica_set.side_effect = refusal(403, "forbidden")
    forbidden, _ = start(core, apps, wanted())
    assert [(x.outcome, x.reason) for x in (missing, renamed, idle, forbidden)] == [
        ("skipped", "no such Deployment"),
        ("skipped", "no container 'app' in its template"),
        ("skipped", "no live pods"),
        ("failed", "checking it: 403: forbidden"),
    ]


def hub_of(answers: list[Any], seen: list[httpx.Request]) -> httpx.Client:
    """The Hub: answers each GET /actuator/desired in turn, 204 to reports (or a status)."""
    pulls = iter(answers)

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/actuator/desired":
            got = next(pulls)
            return got if isinstance(got, httpx.Response) else httpx.Response(200, json=got)
        return reports.pop(0) if reports else httpx.Response(204)

    reports: list[httpx.Response] = []
    c = httpx.Client(
        base_url="https://hub", transport=httpx.MockTransport(handle), headers=as_actuator(A_TOKEN)
    )
    c.reports = reports  # type: ignore[attr-defined]
    return c


def desired(*runs: int, killed: bool = False) -> dict[str, Any]:
    return {"kill_switch": killed, "sizings": [wanted(r).model_dump() for r in runs]}


def make(http: httpx.Client, core: Any, apps: Any, **kw: Any) -> actuator.Actuator:
    autoscaling, custom = scalers()
    kw.setdefault("policy", PDB)
    return actuator.Actuator(
        http, core, apps, "squero-actuator", autoscaling=autoscaling, custom=custom, **kw
    )


def test_a_rollout_goes_on_only_while_the_hub_answers_and_the_kill_switch_is_off(
    monkeypatch: pytest.MonkeyPatch, caplog: Any
) -> None:
    monkeypatch.setattr(actuator, "CANARY_SECONDS", 0)
    core, apps = cluster()
    seen: list[httpx.Request] = []
    answers = [desired(1), httpx.Response(503), desired(1, killed=True), desired(1)]
    a = make(
        hub_of([*answers, desired(1), desired(1), desired(1, 2, killed=True)], seen), core, apps
    )
    a.step()  # run 1: the canary
    a.step()  # the Hub is down: no further
    a.step()  # the Kill switch: no further
    assert [n for n, _ in resized(core)] == ["api-1-a"] and a.held[KEY]["outcome"] == "rolling"
    a.step()  # on: the next pod
    a.step()  # every pod done: applied, reported
    a.step()  # run 1 again: nothing new
    a.step()  # the Kill switch with a newer run: nothing applied
    assert [n for n, _ in resized(core)] == ["api-1-a", "api-1-b"] and a.killed
    posted = [json.loads(r.content) for r in seen if r.method == "POST"]
    assert [[(x["run_id"], x["outcome"], x["pods"]) for x in p] for p in posted] == [
        [(1, "applied", 2)]
    ]
    assert all(r.headers["authorization"] == f"Bearer {A_TOKEN}" for r in seen)
    pulls = [r.url.params.get("kinds") for r in seen if r.method == "GET"]
    assert set(pulls) == {"Deployment,StatefulSet,DaemonSet,Rollout,CronJob"}  # slice 12.8c
    held = json.loads(core.replace_namespaced_config_map.call_args.args[2].data[actuator.KEY])
    assert held[KEY]["outcome"] == "applied"
    # each step while rolling or watched, and once more when its report is taken
    assert core.replace_namespaced_config_map.call_count == 8
    events = [c.args[1] for c in core.create_namespaced_event.call_args_list]
    assert [(e.reason, e.type, e.involved_object.name) for e in events] == [
        ("SizingApplied", "Normal", "api")
    ]
    assert events[0].message.startswith("app: cpu 115m, memory 96Mi (Approved Sizing, run 1)")
    assert "the Hub answered 503" in caplog.text and A_TOKEN not in caplog.text


def actuation(run_id: int) -> actuator.Actuation:
    return actuator.Actuation(
        namespace="shop",
        kind="Deployment",
        name="api",
        container="app",
        run_id=run_id,
        outcome="applied",
        pods=1,
        reason="",
        at=float(run_id),
    )


def test_reports_are_resent_until_the_hub_takes_them_unless_it_refuses_them(caplog: Any) -> None:
    seen: list[httpx.Request] = []
    http = hub_of([], seen)
    http.reports += [httpx.Response(502), httpx.Response(422), httpx.Response(401)]  # type: ignore[attr-defined]
    a = make(http, MagicMock(), MagicMock())
    a.unsent = [actuation(1)]
    a.report()  # 502: kept
    assert [x.run_id for x in a.unsent] == [1]
    a.report()  # 422: the Hub's verdict, dropped
    assert a.unsent == [] and "the Hub refused 1 reports (422): dropped" in caplog.text
    a.unsent = [actuation(2)]
    a.report()  # 401: a refused token may be the Hub mid-update: kept
    assert [x.run_id for x in a.unsent] == [2]
    a.report()
    posted = [json.loads(r.content) for r in seen if r.method == "POST"]
    assert [[x["run_id"] for x in p] for p in posted] == [[1], [1], [2], [2]] and a.unsent == []


def test_what_it_applied_survives_a_restart() -> None:
    core, apps = cluster()
    held = {k: {**v} for k, v in HELD.items()}
    core.read_namespaced_config_map.return_value = k.V1ConfigMap(
        data={actuator.KEY: json.dumps(held)}
    )
    for p in core.list_namespaced_pod.return_value.items:  # as it left them
        p.spec.containers[0].resources.requests = dict(held[KEY]["requests"])
    a = make(hub_of([desired(1)], []), core, apps)
    a.load()
    a.step()
    assert a.held == held and not resized(core)
    fresh = MagicMock()
    fresh.read_namespaced_config_map.side_effect = ApiException(status=404)
    fresh.replace_namespaced_config_map.side_effect = ApiException(status=404)
    b = make(hub_of([desired(1)], []), fresh, apps)
    b.load()
    assert b.held == {}
    for name in ("list_namespaced_pod", "patch_namespaced_pod_resize", "read_namespaced_pod"):
        setattr(fresh, name, getattr(core, name))
    fresh.list_namespaced_limit_range = core.list_namespaced_limit_range
    fresh.list_namespaced_resource_quota = core.list_namespaced_resource_quota
    b.step()  # the first save creates the ConfigMap
    assert fresh.create_namespaced_config_map.call_args.args[0] == "squero-actuator"


@pytest.mark.parametrize(
    ("minor", "ok"), [("32", False), ("33", True), ("33+", True), ("37", True)]
)
def test_it_refuses_a_cluster_without_in_place_resize(minor: str, ok: bool) -> None:
    version = MagicMock()
    version.get_code.return_value = MagicMock(major="1", minor=minor, git_version=f"v1.{minor}.0")
    if ok:
        actuator.check_version(version)
    else:
        with pytest.raises(SystemExit, match=r"v1\.32\.0: the Actuator needs 1\.33"):
            actuator.check_version(version)


def test_the_manifests_resize_pods_and_never_edit_a_deployment() -> None:
    docs = list(yaml.safe_load_all((ROOT / "deploy/actuator/rbac.yaml").read_text()))
    rules = [r for d in docs if d["kind"] in ("ClusterRole", "Role") for r in d["rules"]]
    writes = {
        (res, verb)
        for r in rules
        for res in r["resources"]
        for verb in r["verbs"]
        if verb not in ("get", "list")
    }
    assert writes == {
        ("pods/resize", "patch"),
        ("pods/eviction", "create"),  # slice 12.7, the Eviction API: PDBs hold
        ("events", "create"),
        ("configmaps", "update"),
        ("configmaps", "create"),
        ("mutatingwebhookconfigurations", "patch"),
    }
    assert "secrets" not in str(rules)
    apps_reads = ["deployments", "replicasets", "statefulsets", "daemonsets"]  # slice 12.8a
    assert {"apiGroups": ["apps"], "resources": apps_reads, "verbs": ["get", "list"]} in rules
    assert {
        "apiGroups": ["policy"],
        "resources": ["poddisruptionbudgets"],
        "verbs": ["get", "list"],
    } in rules
    argo = {"apiGroups": ["argoproj.io"], "resources": ["rollouts"], "verbs": ["get", "list"]}
    assert argo in rules  # slice 12.8b: read only
    batch = {"apiGroups": ["batch"], "resources": ["cronjobs", "jobs"], "verbs": ["get", "list"]}
    assert batch in rules  # slice 12.8c: read only
    webhook_rbac = next(r for r in rules if "mutatingwebhookconfigurations" in r["resources"])
    assert webhook_rbac["resourceNames"] == [actuator.WEBHOOK]
    deploy, service = yaml.safe_load_all((ROOT / "deploy/actuator/deployment.yaml").read_text())
    assert deploy["spec"]["template"]["spec"]["enableServiceLinks"] is False
    assert service["metadata"]["name"] == actuator.WEBHOOK
    assert service["spec"]["ports"] == [{"name": "webhook", "port": 443, "targetPort": "webhook"}]
    (hook,) = yaml.safe_load_all((ROOT / "deploy/actuator/webhook.yaml").read_text())
    (w,) = hook["webhooks"]
    assert (w["failurePolicy"], w["timeoutSeconds"], w["sideEffects"]) == ("Ignore", 2, "None")
    assert w["clientConfig"]["service"] == {
        "name": actuator.WEBHOOK,
        "namespace": "squero-actuator",
        "path": "/mutate",
    }
    assert w["rules"][0]["operations"] == ["CREATE"] and w["rules"][0]["resources"] == ["pods"]
    (c,) = deploy["spec"]["template"]["spec"]["containers"]
    hub_image = next(yaml.safe_load_all((ROOT / "deploy/base/deployment.yaml").read_text()))
    assert c["image"] == hub_image["spec"]["template"]["spec"]["containers"][0]["image"]
    assert c["args"] == ["actuator"] and c["securityContext"]["readOnlyRootFilesystem"] is True


# --- the pod-creation webhook (slice 12.2) ------------------------------------------------

HELD = {
    "shop/Deployment/api/app": {
        "run_id": 1,
        "outcome": "applied",
        "requests": {"cpu": "115m", "memory": "64Mi"},
        "template": {"cpu": "1", "memory": None},
    }
}


def new_pod(
    ns: str = "shop", operation: str = "CREATE", sidecar: Any = None, **resources: Any
) -> dict[str, Any]:
    """The AdmissionReview of a new pod of api's ReplicaSet api-5d8f, its container `app`."""
    if sidecar is None:  # its request keeps the pod Burstable, whatever app's are
        sidecar = {"resources": {"requests": {"cpu": "10m"}}}
    c: dict[str, Any] = {"name": "app"}
    if resources != {"none": True}:
        c["resources"] = resources or {"requests": {"cpu": "1"}, "limits": {"memory": "48Mi"}}
    return {
        "apiVersion": "admission.k8s.io/v1",
        "kind": "AdmissionReview",
        "request": {
            "uid": "u-1",
            "operation": operation,
            "namespace": ns,
            "object": {
                "metadata": {
                    "generateName": "api-5d8f-",
                    "labels": {"app": "api", "pod-template-hash": "5d8f"},
                    "ownerReferences": [
                        {"kind": "ReplicaSet", "name": "api-5d8f", "controller": True}
                    ],
                },
                "spec": {"containers": [{"name": "sidecar", **sidecar}, c]},
            },
        },
    }


def patched(review: Any, held: Any = HELD, killed: bool = False) -> list[Any] | None:
    answer = actuator.admit(review, held, killed, "squero-actuator")
    response = answer["response"]
    assert response["allowed"] is True and response["uid"] == (
        review.get("request", {}).get("uid", "") if isinstance(review, dict) else ""
    )
    if "patch" not in response:
        return None
    assert response["patchType"] == "JSONPatch"
    return json.loads(base64.b64decode(response["patch"]))  # type: ignore[no-any-return]


def test_a_new_pod_gets_the_requests_applied_never_above_its_limit() -> None:
    assert patched(new_pod()) == [
        {"op": "add", "path": "/spec/containers/1/resources/requests/cpu", "value": "115m"},
        {"op": "add", "path": "/spec/containers/1/resources/requests/memory", "value": "48Mi"},
    ]
    no_resources = patched(new_pod(none=True), held=untemplated(cpu=None, memory=None))
    assert no_resources == [
        {
            "op": "add",
            "path": "/spec/containers/1/resources",
            "value": {"requests": {"cpu": "115m", "memory": "64Mi"}},
        }
    ]
    no_requests = patched(new_pod(limits={"cpu": "2"}), held=untemplated(cpu=None, memory=None))
    assert no_requests == [
        {
            "op": "add",
            "path": "/spec/containers/1/resources/requests",
            "value": {"cpu": "115m", "memory": "64Mi"},
        }
    ]
    already = new_pod(requests={"cpu": "0.115", "memory": "64Mi"})
    assert patched(already, held=untemplated(cpu="0.115", memory="64Mi")) is None


def untemplated(**template: str | None) -> Any:
    """HELD, with the template's requests as they were when it applied."""
    return {k: {**v, "template": template} for k, v in HELD.items()}


def test_a_new_pod_keeps_its_own_requests_when_the_hold_does_not_apply() -> None:
    changed = new_pod(requests={"cpu": "2"})  # a person changed the template since
    assert patched(changed) is None
    assert patched(new_pod(), killed=True) is None  # the Kill switch: pods start unchanged
    nothing = untemplated(cpu=None, memory=None)  # BestEffort would turn Burstable
    assert patched(new_pod(none=True, sidecar={}), held=nothing) is None
    pinned = {"cpu": "1", "memory": "64Mi"}  # Guaranteed would turn Burstable
    guaranteed = new_pod(requests=pinned, limits=pinned, sidecar={"resources": {"limits": pinned}})
    assert patched(guaranteed, held=untemplated(**pinned)) is None
    for ns in ("squero-actuator", "kube-system"):
        assert (
            patched(new_pod(ns=ns), held={k.replace("shop", ns): v for k, v in HELD.items()})
            is None
        )
    assert patched(new_pod(operation="UPDATE")) is None
    assert patched(new_pod(ns="bank")) is None  # nothing held there
    gone = {k: {**v, "outcome": "handed_back"} for k, v in HELD.items()}
    assert patched(new_pod(), held=gone) is None
    before_12_2 = {k: {kk: vv for kk, vv in v.items() if kk != "template"} for k, v in HELD.items()}
    assert patched(new_pod(), held=before_12_2) is None
    assert patched(new_pod(none=True), held=before_12_2) is None
    bare = new_pod()
    bare["request"]["object"]["metadata"]["labels"].pop("pod-template-hash")  # not a Deployment's
    assert patched(bare) is None
    bare_rs = new_pod()  # a ReplicaSet of its own named api: not one of Deployment api's
    bare_rs["request"]["object"]["metadata"]["ownerReferences"][0]["name"] = "api"
    assert patched(bare_rs) is None
    assert patched(["not", "a", "review"]) is None
    broken = {k: {**v, "requests": ["cpu"]} for k, v in HELD.items()}
    assert patched(new_pod(), held=broken) is None  # whatever goes wrong, the pod is created


def test_the_webhook_answers_over_tls_with_its_own_certificate() -> None:
    cert, key = actuator.certificate("localhost")
    a = actuator.Actuator(MagicMock(), MagicMock(), MagicMock(), "squero-actuator", held=HELD)
    server = actuator.serve(a, cert, key, port=0)
    try:
        url = f"https://localhost:{server.server_address[1]}"
        trusted = ssl.create_default_context(cadata=cert.decode())
        with httpx.Client(verify=trusted) as http:
            ok = http.post(f"{url}/mutate?timeout=2s", json=new_pod())  # as the API server calls
            a.held, a.boosting = {}, {KEY: 2.0}  # its Startup boosts too (slice 13.4)
            boost = new_pod(requests={"cpu": "250m"}, limits={"cpu": "500m"})
            boosted = http.post(f"{url}/mutate", json=boost)
            a.boosting, a.bursts = {}, {KEY: dict(MADE)}  # and its Bursts (slice 13.3b)
            burst = http.post(f"{url}/mutate", json=burst_pod())
            a.killed = True
            killed = http.post(f"{url}/mutate", json=new_pod())
            elsewhere = http.post(f"{url}/other", json=new_pod())
            garbage = http.post(f"{url}/mutate", content=b"{not json")
        with pytest.raises(httpx.ConnectError):
            httpx.post(f"{url}/mutate", json=new_pod())  # an untrusted certificate
    finally:
        server.shutdown()
    assert ok.status_code == 200 and "patch" in ok.json()["response"]
    assert "patch" in boosted.json()["response"] and "patch" in burst.json()["response"]
    assert "patch" not in killed.json()["response"]
    assert (elsewhere.status_code, garbage.status_code) == (404, 400)


def test_it_puts_its_certificate_in_its_own_webhooks_ca_bundle() -> None:
    admission = MagicMock()
    actuator.trust(admission, b"PEM")
    admission.patch_mutating_webhook_configuration.assert_called_once_with(
        "squero-actuator",
        [{"op": "replace", "path": "/webhooks/0/clientConfig/caBundle", "value": "UEVN"}],
    )


def test_a_person_changing_the_template_gets_the_container_back() -> None:
    core, apps = cluster()  # api's template asks cpu 1, as when it applied
    held = {
        **HELD,
        "shop/Deployment/gone/app": {**HELD["shop/Deployment/api/app"]},
        "shop/Deployment/other/app": {**HELD["shop/Deployment/api/app"], "outcome": "failed"},
        "shop/Deployment/old/app": {"run_id": 1, "outcome": "applied", "requests": {"cpu": "1"}},
    }
    held = {k: {**v} for k, v in held.items()}

    def read(name: str, ns: str) -> Any:
        if name == "gone":
            raise ApiException(status=404)
        return apps.read_namespaced_deployment.return_value

    apps.read_namespaced_deployment.side_effect = read
    seen: list[httpx.Request] = []
    a = actuator.Actuator(hub_of([httpx.Response(503)] * 2, seen), core, apps, "x", held=held)
    a.step()  # the Hub is down: hand-backs still happen, and are saved and reported
    assert [(k, v["outcome"]) for k, v in a.held.items()] == [
        ("shop/Deployment/api/app", "applied"),
        ("shop/Deployment/gone/app", "handed_back"),
        ("shop/Deployment/other/app", "failed"),
        ("shop/Deployment/old/app", "handed_back"),
    ]
    template = apps.read_namespaced_deployment.return_value.spec.template.spec.containers[0]
    template.resources.requests = {"cpu": "200m"}
    a.step()
    assert a.held["shop/Deployment/api/app"]["outcome"] == "handed_back"
    reports = [x for r in seen if r.method == "POST" for x in json.loads(r.content)]
    assert [(r["name"], r["outcome"], r["reason"]) for r in reports] == [
        ("gone", "handed_back", "no such Deployment"),
        ("old", "handed_back", "held without its template's requests (before slice 12.2)"),
        ("api", "handed_back", "its template's requests changed: cpu 1 to 200m"),
    ]
    events = [c.args[1] for c in core.create_namespaced_event.call_args_list]
    assert [e.reason for e in events] == ["HandedBack"] * 3
    assert events[2].message.startswith("app: back to its template (Approved Sizing, run 1)")
    assert core.replace_namespaced_config_map.call_count == 4  # with its reports, then without
    template.name = "web"
    a.held["shop/Deployment/api/app"]["outcome"] = "applied"
    (renamed,) = a.hand_back()
    assert renamed.reason == "no container 'app' in its template"
    rolling = {**HELD[KEY], "outcome": "rolling", "pod": "api-1-a", "since": 0.0, "resized": 1}
    apps.read_namespaced_deployment.side_effect = None
    template.name = "app"
    a.held = {KEY: rolling}  # a rollout under way when a person changes the template
    (stopped,) = a.hand_back()
    assert stopped.reason == "its template's requests changed: cpu 1 to 200m"
    assert a.held[KEY] == {**HELD[KEY], "outcome": "handed_back"}
    apps.read_namespaced_deployment.side_effect = ApiException(status=500)
    a.held = {KEY: {**HELD[KEY]}}
    assert a.hand_back() == []  # the API server failing is no change by a person


# --- rollback (slice 12.4) ----------------------------------------------------------------


def unhealthy(
    p: Any, restarts: int = 0, oom: bool = False, ready: bool = True, since: float = 0.0
) -> Any:
    """`p` as the kubelet reports it: its container's restarts, last end, Ready since `since`."""
    ended = k.V1ContainerStateTerminated(exit_code=137, reason="OOMKilled" if oom else "Error")
    st = status("app", None)
    st.restart_count, st.last_state = restarts, k.V1ContainerState(terminated=ended)
    when = datetime.fromtimestamp(since, UTC)
    ready_now = k.V1PodCondition(
        type="Ready", status="True" if ready else "False", last_transition_time=when
    )
    p.status = k.V1PodStatus(phase="Running", conditions=[ready_now], container_statuses=[st])
    return p


def watched(core: Any, apps: Any, at: float = 1000.0) -> dict[str, Any]:
    """api's Sizing applied on both pods at `at`, each with no restart then."""
    _, h = start(core, apps, wanted(memory=None))
    actuator.advance(core, apps, KEY, h, 600.0)
    actuator.advance(core, apps, KEY, h, 660.0)
    assert h["outcome"] == "applied"
    h["pods"] = {n: {"at": at, "restarts": 0} for n in ("api-1-a", "api-1-b")}
    return h


def test_what_makes_a_sizing_unhealthy_and_what_does_not() -> None:
    core, apps = cluster()
    h = watched(core, apps)
    a, b = core.list_namespaced_pod.return_value.items[:2]

    def found(now: float = 2000.0) -> str | None:
        return actuator.trouble(core, apps, KEY, h, now)

    assert found() is None  # healthy
    unhealthy(a, restarts=1, oom=True)
    assert found() == "api-1-a: app was OOM-killed"
    unhealthy(a, restarts=1)
    assert found() == "api-1-a: app restarted"
    h["pods"]["api-1-a"]["restarts"] = 1  # it had restarted once before it was resized
    assert found() is None
    unhealthy(b, ready=False, since=1500.0)
    assert found(1619.0) is None and found(1620.0) == "api-1-b: NotReady for 2 minutes"
    unhealthy(b, ready=True, since=1500.0)  # Ready again since: fine
    assert found() is None
    unhealthy(b, ready=False, since=900.0)  # NotReady since before its resize: not its doing
    assert found() is None
    unhealthy(b)
    new = pod("api-1-c", "api-1", cpu="115m")  # created by the webhook, with its requests
    core.list_namespaced_pod.return_value.items.append(new)
    unhealthy(new, ready=False, since=0.0)
    assert found() is None  # a new pod may take its time to be Ready
    unhealthy(new, restarts=1)
    assert found() == "api-1-c: app restarted"
    other = pod("api-1-d", "api-1")  # the template's requests: not the Actuator's doing
    core.list_namespaced_pod.return_value.items[-1] = unhealthy(other, restarts=5, oom=True)
    assert found() is None
    apps.read_namespaced_deployment.side_effect = ApiException(status=500)
    assert found() is None


def test_a_rollback_puts_its_pods_back_and_backs_off_doubling() -> None:
    core, apps = cluster()
    h = watched(core, apps)
    core.patch_namespaced_pod_resize.reset_mock()
    h["template"] = {"cpu": "1"}
    first = actuator.rollback(core, apps, KEY, h, "api-1-a: app restarted", 7200.0)
    assert resized(core) == [("api-1-a", body(cpu="1")), ("api-1-b", body(cpu="1"))]
    assert (first.outcome, first.pods, first.cpu) == ("rolled_back", 2, 1.0)
    assert first.reason == (
        "api-1-a: app restarted: 2 pods back from cpu 115m to their template's cpu 1; "
        "no new Sizing of it until 1970-01-02 02:00 UTC"
    )
    assert h == {
        "run_id": 1,
        "outcome": "rolled_back",
        "requests": {"cpu": "115m"},
        "template": {"cpu": "1"},
        "at": 0.0,
        "rollbacks": [7200.0],
        "backoff_until": 7200.0 + 86400,
    }
    h["rollbacks"] = [0.0, 7200.0]
    later = 7 * 86400.0 + 100  # the one at 0 is over 7 days old, the one at 7200 not
    again = actuator.rollback(core, apps, KEY, h, "x", later)
    assert h["rollbacks"] == [7200.0, later]
    assert h["backoff_until"] == later + 2 * 86400  # the second in 7 days: two days
    assert again.pods == 0  # already back
    core, apps = cluster()
    h = watched(core, apps)
    h["template"] = {"cpu": None}  # the template asked no cpu: nothing to put back
    core.patch_namespaced_pod_resize.reset_mock()
    unset = actuator.rollback(core, apps, KEY, h, "x", 0.0)
    assert not resized(core)
    assert unset.reason.startswith("x: 0 pods back from cpu 115m (['cpu'] left: the template sets")
    core, apps = cluster()
    h = watched(core, apps)
    core.patch_namespaced_pod_resize.side_effect = refusal(403, "forbidden")
    refused = actuator.rollback(core, apps, KEY, h, "x", 0.0)
    assert refused.pods == 0 and "2 refused, first api-1-a: 403: forbidden" in refused.reason
    actuator.event(core, refused)
    shown = core.create_namespaced_event.call_args.args[1]
    assert (shown.reason, shown.type) == ("SizingRolledBack", "Warning")
    # what it went back to is in the reason; the values never read as the ones approved
    assert shown.message.startswith("app: back to its template (Approved Sizing, run 1): x: 0 pods")


def test_an_unhealthy_rollout_is_rolled_back_and_the_next_approval_waits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(actuator, "CANARY_SECONDS", 0)
    core, apps = cluster()
    seen: list[httpx.Request] = []
    answers = [
        desired(1),  # the canary
        httpx.Response(503),  # the canary restarts: rolled back, the Hub down or not
        desired(1, 2),  # a newer approval during the backoff: skipped
        desired(2, 3),  # after the backoff: applied
    ]
    a = make(hub_of(answers, seen), core, apps)
    a.step()
    canary = core.list_namespaced_pod.return_value.items[0]
    core.read_namespaced_pod.side_effect = None
    core.read_namespaced_pod.return_value = unhealthy(canary, restarts=1, oom=True)
    a.step()
    assert a.held[KEY]["outcome"] == "rolled_back"
    assert [n for n, _ in resized(core)] == ["api-1-a", "api-1-a"]  # resized, then back
    a.step()
    assert a.held[KEY]["outcome"] == "skipped" and a.held[KEY]["rollbacks"]
    a.held[KEY]["backoff_until"] = 0.0
    unhealthy(canary)  # healthy again
    a.step()
    assert a.held[KEY]["run_id"] == 3 and a.held[KEY]["outcome"] == "rolling"
    assert a.held[KEY]["rollbacks"]  # kept for the doubling
    posted = [x for r in seen if r.method == "POST" for x in json.loads(r.content)]
    assert [(x["run_id"], x["outcome"]) for x in posted] == [(1, "rolled_back"), (2, "skipped")]
    assert posted[0]["reason"].startswith("api-1-a: app was OOM-killed: 1 pods back")
    assert posted[1]["reason"].startswith("rolled back 1 times in 7 days: no new Sizing of it")


def test_no_rollback_under_the_kill_switch_and_none_once_its_hour_is_over() -> None:
    core, apps = cluster()
    h = watched(core, apps)
    unhealthy(core.list_namespaced_pod.return_value.items[0], restarts=1)
    a = make(hub_of([desired(1, killed=True), httpx.Response(503)], []), core, apps)
    a.held = {KEY: h}
    h["watch_until"] = 10**12
    a.step()  # the Kill switch: nothing changes, not even a rollback
    assert h["outcome"] == "applied"
    h["watch_until"] = 0.0
    a.killed = False
    a.step()  # its hour is over: no longer watched
    assert h["outcome"] == "applied" and "pods" not in h and "watch_until" not in h


def tell(c: TestClient, cid: str | None = None, at: float | None = None) -> list[Any]:
    """Tell the Rollbacks not told yet, as the Hub does after a report and each tick, and
    wait for it (the one a report started runs under the same lock)."""
    assert c.portal is not None
    c.portal.call(c.app.state.tell, cid, at)  # type: ignore[attr-defined]
    return []


def untold(database_url: str) -> list[Any]:
    return sql(database_url, ("select contract_id, name from actuations where not told", ()))


def test_a_rollback_reaches_the_operators_channel_once(
    settings: Settings, database_url: str
) -> None:
    from tests.test_slack import TOKEN, FakeSlack

    settings.slack_bot_token = SecretStr(TOKEN)
    slack = FakeSlack()
    channel = {"slack": {"operator_channel": "C0123456789"}}
    acme = contracts.build(
        {**actuated("acme", A_TOKEN).model_dump(mode="json", exclude_none=True), **channel},
        secrets={"act": SecretStr(A_TOKEN), "r": SecretStr("r" * 40)},
    )
    settings.database_url = SecretStr(database_url)
    settings.approver_token = SecretStr("approver-token")
    app = create_app(
        settings,
        scripted(),
        lambda c: None,
        {"acme": acme, "bank": actuated("bank", B_TOKEN)},
        slack_http=httpx.AsyncClient(transport=slack.transport()),
    )
    back = report(outcome="rolled_back", reason="api-1-a: app was OOM-killed: 2 pods back")
    with TestClient(app) as c:
        for _ in range(2):  # sent again: posted once
            c.post("/actuator/reports", json=[back, report(at=1.0)], headers=as_actuator(A_TOKEN))
            tell(c)
        c.post("/actuator/reports", json=[back], headers=as_actuator(B_TOKEN))  # no Slack
        tell(c)
        assert tell(c) == [] == untold(database_url)  # bank's: no channel, nothing to tell
    posts = [p for m, p, _ in slack.calls if m == "chat.postMessage"]
    assert [(p["channel"], p["text"]) for p in posts] == [
        (
            "C0123456789",
            ":rewind: acme: the Actuator rolled back test/shop/Deployment/api container app: "
            "api-1-a: app was OOM-killed: 2 pods back",
        )
    ]


def test_pods_that_missed_the_held_requests_get_them_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pod started from its template while the Kill switch was on (or the webhook down)
    gets the Sizing again once the Actuator may change things: the pre-checks, a canary,
    and the pods it already watched stay watched (found live, 2026-11-10)."""
    monkeypatch.setattr(actuator, "CANARY_SECONDS", 0)
    core, apps = cluster()
    h = watched(core, apps)
    h["watch_until"], h["rollbacks"] = 10**12, [5.0]
    seen: list[httpx.Request] = []
    a = make(hub_of([desired(1, killed=True), *[desired(1)] * 6], seen), core, apps)
    a.held = {KEY: h}
    core.patch_namespaced_pod_resize.reset_mock()
    late = pod("api-1-c", "api-1")  # from the template: cpu 1
    late.metadata.namespace = "shop"
    core.list_namespaced_pod.return_value.items.append(late)
    a.step()  # the Kill switch: left as it is
    assert not resized(core) and a.held[KEY]["outcome"] == "applied"
    a.step()  # off: it catches up, as a canary
    assert resized(core) == [("api-1-c", body(cpu="115m"))]
    now = a.held[KEY]
    assert (now["outcome"], now["pod"], now["watch_until"]) == ("rolling", "api-1-c", 10**12)
    assert set(now["pods"]) == {"api-1-a", "api-1-b", "api-1-c"}  # still watched
    assert now["rollbacks"] == [5.0]  # kept for the doubling
    a.step()  # allocated: applied again
    a.step()  # nothing behind: nothing to do
    assert len(resized(core)) == 1 and a.held[KEY]["outcome"] == "applied"
    posted = [x for r in seen if r.method == "POST" for x in json.loads(r.content)]
    assert [(x["run_id"], x["outcome"], x["pods"]) for x in posted] == [(1, "applied", 1)]
    # A pre-check that fails now (a VPA since) ends the hold: reported, never resized.
    behind = pod("api-1-d", "api-1")
    behind.metadata.namespace = "shop"
    core.list_namespaced_pod.return_value.items.append(behind)
    vpa = {"spec": {"targetRef": {"kind": "Deployment", "name": "api"}}}
    a.custom = scalers(vpas=[vpa])[1]
    a.step()
    assert len(resized(core)) == 1 and a.held[KEY]["outcome"] == "skipped"
    last = json.loads([r for r in seen if r.method == "POST"][-1].content)[0]
    assert (last["outcome"], last["reason"]) == ("skipped", "a VPA in mode Auto sizes it")
    a.custom = scalers()[1]
    a.step()  # no longer held: never caught up
    a.held[KEY]["outcome"] = "rolled_back"
    a.step()  # rolled back: never caught up either
    assert len(resized(core)) == 1


# --- Automation (slice 12.5) --------------------------------------------------------------


def automating(cid: str, token: str, *rules: dict[str, Any], **more: Any) -> contracts.Contract:
    """actuated(), with these Sizing rules."""
    data = actuated(cid, token).model_dump(mode="json", exclude_none=True)
    return contracts.build(
        {**data, "sizing": {"rules": list(rules)}, **more},
        secrets={"act": SecretStr(token), "r": SecretStr("r" * 40)},
    )


AUTO = {"namespace": "shop", "container": "app", "policy": "production", "automate": True}


def test_automate_needs_an_actuator_and_a_policy_and_the_first_matching_rule_decides() -> None:
    with pytest.raises(ValueError, match="policy off has no Sizings to apply"):
        automating("acme", A_TOKEN, {**AUTO, "policy": "off"})
    with pytest.raises(ValueError, match="rule 1 automates: cluster 'edge' has no actuator"):
        automating("acme", A_TOKEN, {**AUTO, "cluster": "edge"})
    plain = actuated("acme", A_TOKEN).model_dump(mode="json", exclude_none=True)
    plain["clusters"][0].pop("actuator_token_secret")
    with pytest.raises(ValueError, match="rule 1 automates: no cluster has no actuator"):
        contracts.build({**plain, "sizing": {"rules": [AUTO]}}, secrets={})
    c = automating(
        "acme",
        A_TOKEN,
        {"namespace": "shop", "name": "db", "policy": "production"},  # first: not automated
        {**AUTO, "cluster": "test"},
    )
    assert c.sizing.automated("test", "shop", "Deployment", "api", "app")
    assert not c.sizing.automated("test", "shop", "Deployment", "db", "app")
    assert not c.sizing.automated("test", "shop", "Deployment", "api", "sidecar")
    assert not c.sizing.automated("edge", "shop", "Deployment", "api", "app")
    assert not actuated("acme", A_TOKEN).sizing.automated(
        "test", "shop", "Deployment", "api", "app"
    )


def sized(cid: str, run_id: int, name: str = "api", **row: Any) -> Any:
    """A stored Sizing of test/shop/<kind>/<name>/<container> in run `run_id`."""
    r = {
        "cluster": "test",
        "namespace": "shop",
        "kind": "Deployment",
        "name": name,
        "container": "app",
        "state": "ready",
        "cpu": {"action": "lower", "current": 1.0, "target": 0.115},
        "memory": {"action": "keep", "current": 1.0, "target": 1.0},
    } | row
    keys = [r[k] for k in ("cluster", "namespace", "kind", "name", "container")]
    return ("insert into sizings values (%s, %s, %s, %s, %s, %s, %s, %s)",
            (run_id, cid, *keys, json.dumps(r)))  # fmt: skip


def rolled_back(
    cid: str, at: float, cluster: str = "test", name: str = "api", outcome: str = "rolled_back"
) -> Any:
    return (
        "insert into actuations values (%s, %s, 'shop', 'Deployment', %s, 'app', 1, "
        "%s, null, null, 1, 'x', %s, %s)",
        (cid, cluster, name, outcome, at, at),
    )


def test_an_actuator_gets_the_newest_sizings_of_what_its_contract_automates(
    settings: Settings, database_url: str
) -> None:
    import time

    now = time.time()
    gone = {**AUTO, "namespace": "old"}  # a namespace the cluster no longer has
    acme = automating("acme", A_TOKEN, AUTO, gone)
    bank = automating("bank", B_TOKEN, {**AUTO, "name": "ledger"})
    app = hub(settings, database_url, acme, bank)
    with TestClient(app) as c:
        sql(
            database_url,
            run("acme", '{"test": null}'),  # run 1
            run("acme", '{"test": null}'),  # run 2: the newest
            sized("acme", 1, cpu={"action": "raise", "current": 0.1, "target": 0.5}),
            sized("acme", 2),
            sized("acme", 2, "learning", state="learning"),
            sized("acme", 2, "steady", cpu={"action": "keep", "current": 1.0, "target": 1.0}),
            sized("acme", 2, "db", kind="StatefulSet"),  # slice 12.8a
            sized("acme", 2, "web", kind="Rollout"),  # Argo Rollouts: slice 12.8b
            sized("acme", 2, "side", container="sidecar"),  # no rule automates it
            sized("acme", 2, "approved"),
            approve("acme", 2, name="approved"),  # the approval goes as approved, once
            sized("acme", 2, "broken"),
            sized("acme", 2, "twice"),
            sized("acme", 2, "elsewhere"),
            sized("acme", 2, "moved", namespace="old"),
            *(rolled_back("acme", now - d * 86400, name="broken") for d in (1, 2, 3)),  # ended
            *(rolled_back("acme", now - d * 86400, name="twice") for d in (1, 2, 7.5)),  # 7.5: old
            *(rolled_back("acme", now - d * 86400, "edge", "elsewhere") for d in (1, 2, 3)),
            *(rolled_back("bank", now - d * 86400) for d in (1, 2, 3)),  # bank's api, not acme's
            *(rolled_back("acme", now - d, outcome="applied") for d in (1, 2, 3)),  # no rollback
            run("bank", '{"test": null}'),  # run 3, bank's
            sized("bank", 3, "ledger"),
        )
        kinds = {"kinds": ",".join(actuator.KINDS)}
        mine = c.get("/actuator/desired", headers=as_actuator(A_TOKEN), params=kinds).json()
        old = c.get("/actuator/desired", headers=as_actuator(A_TOKEN)).json()
        theirs = c.get("/actuator/desired", headers=as_actuator(B_TOKEN)).json()
        sql(database_url, ("insert into kill_switches values ('acme', 't', 1)", ()))
        killed = c.get("/actuator/desired", headers=as_actuator(A_TOKEN)).json()
    one = {"run_id": 2, "namespace": "shop", "kind": "Deployment", "container": "app"}
    one["again"] = False
    one["types"] = {}
    assert [w["name"] for w in mine["sizings"]] == ["approved"]
    assert mine["automated"] == [
        {**one, "name": n, "cpu": 0.115, "memory": None} for n in ("api", "elsewhere", "twice")
    ] + [
        {**one, "kind": "Rollout", "name": "web", "cpu": 0.115, "memory": None},
        {**one, "kind": "StatefulSet", "name": "db", "cpu": 0.115, "memory": None},
    ]
    assert old["automated"] == mine["automated"][:-2]  # an Actuator naming no kinds: no db, web
    assert theirs["automated"] == [{**one, "run_id": 3, "name": "ledger", "cpu": 0.115,
                                    "memory": None}]  # fmt: skip
    assert killed == {
        "kill_switch": True,
        "sizings": [],
        "automated": [],
        "reverts": [],
        "eviction": "off",
        "oom": [],
        "oom_max": None,
        "oom_expires": None,
        "boosts": [],
        "burst": [],
        "burst_run": None,
        "burst_max": None,
        "burst_expires": None,
        "blockers": ["shop"],  # slice 15.1b: under the Kill switch too
        "node_types": None,  # slice 15.3b: no Sizing here has types
        "placement": None,  # slice 15.2a: the cluster has none
    }


def test_the_rollback_that_ends_automation_reaches_the_operator_once(
    settings: Settings, database_url: str
) -> None:
    from tests.test_slack import TOKEN, FakeSlack

    settings.slack_bot_token = SecretStr(TOKEN)
    slack = FakeSlack()
    channel = {"slack": {"operator_channel": "C0123456789"}}
    acme = automating("acme", A_TOKEN, AUTO, **channel)
    settings.database_url = SecretStr(database_url)
    settings.approver_token = SecretStr("approver-token")
    app = create_app(
        settings,
        scripted(),
        lambda c: None,
        {"acme": acme, "bank": automating("bank", B_TOKEN, AUTO)},
        slack_http=httpx.AsyncClient(transport=slack.transport()),
    )
    day = 86400.0
    import time

    now = time.time()

    def back(at: float, **more: Any) -> dict[str, Any]:
        return report(outcome="rolled_back", reason="r", at=at, **more)

    with TestClient(app) as c:
        sql(
            database_url,
            run("acme", '{"test": null}'),
            *(sized("acme", 1, n) for n in ("api", "web")),
            sized("acme", 1, "db", container="sidecar"),  # no container of db is automated
        )

        def post(*rs: Any) -> None:
            c.post("/actuator/reports", json=list(rs), headers=as_actuator(A_TOKEN))
            tell(c)  # as its own request starts it: before the next arrives

        post(back(now - 3 * day), back(now - 2 * day))  # two: Automation goes on
        post(back(now - day, container="sidecar"))  # the third, of api's other container
        post(back(now - day + 1))  # the fourth in 7 days: it was already over
        post(back(now - 4 * day, name="web"))
        post(back(now - 2 * day, name="web"), back(now - day, name="web"), back(now, name="web"))
        post(back(now, name="web"))  # sent again: nothing new
        post(*(back(now - d * day, name="db", container="sidecar") for d in (1, 2, 3)))
        c.post("/actuator/reports", json=[back(now - d * day) for d in (1, 2, 3)],
               headers=as_actuator(B_TOKEN))  # bank: no channel  # fmt: skip
        tell(c)
    texts = [p["text"] for m, p, _ in slack.calls if m == "chat.postMessage"]
    assert len(texts) == 11 and all(t.startswith(":rewind: acme: ") for t in texts)
    # said with the Rollback that ends it, in one message (slice 12.9c)
    ended = [t.split("\n")[1] for t in texts if "ended" in t]
    assert [t.split(" container ")[1].split(":")[0] for t in texts if "ended" in t] == [
        "sidecar",
        "app",
    ]
    until = datetime.fromtimestamp(now - 3 * day + 7 * day, UTC)
    web = datetime.fromtimestamp(now - 2 * day + 7 * day, UTC)
    assert ended == [
        f":octagonal_sign: acme: Automation of test/shop/Deployment/api ended: 3 rollbacks in "
        f"7 days. It resumes on {until:%Y-%m-%d %H:%M} UTC unless more follow; remove "
        "`automate` from its rule to end it for good.",
        f":octagonal_sign: acme: Automation of test/shop/Deployment/web ended: 4 rollbacks in "
        f"7 days. It resumes on {web:%Y-%m-%d %H:%M} UTC unless more follow; remove "
        "`automate` from its rule to end it for good.",
    ]


def applied(core: Any, apps: Any, w: Any) -> dict[str, Any]:
    """`w` applied on both of api's pods; its hold."""
    _, h = start(core, apps, w)
    actuator.advance(core, apps, KEY, h, 600.0)
    actuator.advance(core, apps, KEY, h, 660.0)
    assert h["outcome"] == "applied"
    return h


def test_a_sizing_that_leaves_a_resource_alone_keeps_what_is_held_of_it() -> None:
    core, apps = cluster()  # the template asks cpu 1, no memory
    h = applied(core, apps, wanted())  # cpu 115m, memory 96Mi
    _, new = actuator.apply(core, apps, wanted(2, cpu=None, memory=64 * MI), 700.0, *scalers(), h)
    assert new["requests"] == {"cpu": "115m", "memory": "64Mi"}
    assert new["template"] == {"cpu": "1", "memory": None}
    assert new["lowered_at"] == 700.0  # memory went down
    _, up = actuator.apply(core, apps, wanted(3, cpu=0.2, memory=None), 700.0, *scalers(), h)
    assert up["requests"] == {"cpu": "200m", "memory": "96Mi"} and "lowered_at" not in up
    core, apps = cluster()
    _, caught = start(core, apps, wanted())  # catching up (no hold replaced): not a decrease
    assert "lowered_at" not in caught


def test_automation_ignores_small_changes_and_lowers_at_most_once_a_day() -> None:
    core, apps = cluster()
    h = applied(core, apps, wanted(memory=100 * MI))  # cpu 115m, memory 100Mi

    def auto(at: float = 10**6, old: Any = h, **values: float | None) -> Any:
        w = wanted(2, **({"cpu": None, "memory": None} | values))
        return actuator.apply(core, apps, w, at, *scalers(), old, True)

    assert auto(cpu=0.120) == (None, {})  # 4% up
    assert auto(memory=91 * MI) == (None, {})  # 9% down
    _, ten = auto(memory=110 * MI)  # 10% exactly: applied
    assert ten["requests"] == {"cpu": "115m", "memory": "110Mi"}
    _, first = auto(memory=64 * MI)  # the first decrease
    assert first["requests"]["memory"] == "64Mi" and first["lowered_at"] == 10**6
    recent = {**h, "lowered_at": 10**6 - 86399}
    assert auto(old=recent, memory=64 * MI) == (None, {})  # lowered under a day ago
    _, mixed = auto(old=recent, cpu=0.2, memory=64 * MI)  # the increase goes, memory waits
    assert mixed["requests"] == {"cpu": "200m", "memory": "100Mi"} and "lowered_at" not in mixed
    assert mixed["deferred"] == ["memory"] and "deferred" not in ten  # for an approval of it
    _, day = auto(old={**h, "lowered_at": 10**6 - 86400}, memory=64 * MI)
    assert day["requests"]["memory"] == "64Mi"
    core, apps = cluster()  # nothing held: from the template's cpu 1, no memory
    assert auto(old={}, cpu=0.95) == (None, {})
    _, unset = auto(old={}, memory=50 * MI)  # no request now: set
    assert unset["requests"] == {"memory": "50Mi"}
    _, approved = actuator.apply(core, apps, wanted(cpu=0.95, memory=None), 0.0, *scalers(), {})
    assert approved["requests"] == {"cpu": "950m"}  # an approval is never under the rule


def wants(*runs: Any, approved: tuple[Any, ...] = (), killed: bool = False) -> dict[str, Any]:
    """The Hub's answer: these Automation Sizings, and these approved."""
    return {
        "kill_switch": killed,
        "sizings": [w.model_dump() for w in approved],
        "automated": [w.model_dump() for w in runs],
    }


def test_automation_applies_unasked_and_waits_while_its_container_rolls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(actuator, "CANARY_SECONDS", 0)
    core, apps = cluster()
    seen: list[httpx.Request] = []
    up = wanted(3, cpu=0.5)
    a = make(hub_of([wants(wanted(2)), wants(up), wants(up)], seen), core, apps)
    a.step()  # run 2: the canary
    assert a.held[KEY]["automated"] is True
    a.step()  # the next pod; run 3 waits for the rollout
    assert a.held[KEY]["run_id"] == 2
    a.step()  # run 2 applied, then run 3's canary
    assert [(n, b["spec"]["containers"][0]["resources"]["requests"]["cpu"])
            for n, b in resized(core)] == [("api-1-a", "115m"), ("api-1-b", "115m"),
                                           ("api-1-a", "500m")]  # fmt: skip
    events = [c.args[1] for c in core.create_namespaced_event.call_args_list]
    assert events[0].message.startswith("app: cpu 115m, memory 96Mi (Automation, run 2)")
    posted = [x for r in seen if r.method == "POST" for x in json.loads(r.content)]
    assert [(x["run_id"], x["outcome"]) for x in posted] == [(2, "applied")]


def test_a_sizing_automation_no_longer_lists_keeps_what_the_actuator_holds() -> None:
    """Slice 9.7: a Sizing learning again (a gap in Prometheus) leaves the Hub's answer.
    What the Actuator applied stays held: nothing resized, handed back or said."""
    core, apps = cluster()
    h = applied(core, apps, wanted())
    h["automated"] = True
    seen: list[httpx.Request] = []
    a = make(hub_of([wants(), wants()], seen), core, apps)
    a.held = {KEY: h}
    had, done = dict(h["requests"]), len(resized(core))
    a.step()
    a.step()
    assert a.held[KEY]["outcome"] == "applied" and a.held[KEY]["requests"] == had
    assert len(resized(core)) == done
    assert not [r for r in seen if r.method == "POST"]


def test_automation_says_a_skip_once_keeps_its_hold_and_backs_off_unsaid() -> None:
    core, apps = cluster()
    h = applied(core, apps, wanted())
    h["automated"] = True
    vpa = {"spec": {"targetRef": {"kind": "Deployment", "name": "api"}}}
    seen: list[httpx.Request] = []
    answers = [
        wants(wanted(4, cpu=0.5)),  # a VPA since: skipped, said
        wants(wanted(5, cpu=0.6)),  # the same reason: unsaid
        wants(wanted(6, cpu=0.12)),  # under 10%: ignored, unsaid
        wants(wanted(6, cpu=0.12)),  # the same run: not even looked at again
        wants(wanted(7, cpu=0.6)),  # backing off: unsaid
        wants(approved=(wanted(8, cpu=0.6),)),  # an approval backing off: said
    ]
    a = make(hub_of(answers, seen), core, apps)
    a.custom = scalers(vpas=[vpa])[1]
    a.held = {KEY: h}
    a.step()
    a.step()
    assert a.held[KEY]["outcome"] == "applied" and a.held[KEY]["requests"]["cpu"] == "115m"
    assert a.held[KEY]["seen"] == 5 and a.held[KEY]["run_id"] == 1  # still holding run 1
    a.custom = scalers()[1]
    a.step()
    assert a.held[KEY]["seen"] == 6 and not resized(core)[2:]
    reads = apps.read_namespaced_deployment.call_count
    a.step()
    assert apps.read_namespaced_deployment.call_count == reads + 2  # hand_back, catch_up
    a.held[KEY]["backoff_until"] = 4e9  # 2096
    a.step()
    a.step()
    posted = [x for r in seen if r.method == "POST" for x in json.loads(r.content)]
    assert [(x["run_id"], x["outcome"], x["reason"][:20]) for x in posted] == [
        (4, "skipped", "a VPA in mode Auto s"),
        (8, "skipped", "rolled back 0 times "),
    ]
    assert len(resized(core)) == 2  # api's two pods, before


def test_an_approval_of_the_run_automation_applied_is_applied_as_approved() -> None:
    core, apps = cluster()
    h = applied(core, apps, wanted(2))
    a = make(hub_of([wants(approved=(wanted(2, cpu=0.12),)), wants(approved=(wanted(2),))], []),
             core, apps)  # fmt: skip
    a.held = {KEY: {**h, "automated": True, "deferred": ["cpu"]}}  # cpu: under the churn rule
    a.step()  # what the churn rule left out still applies once approved
    assert resized(core)[-1] == ("api-1-a", body(cpu="120m", memory="96Mi"))
    assert "automated" not in a.held[KEY]
    a.step()  # the same approval again: once
    assert len(resized(core)) == 3


def paused(apps: Any, *names: str) -> None:
    deployments = [
        MagicMock(metadata=k.V1ObjectMeta(name=n, annotations={actuator.PAUSE: "true"}))
        for n in names
    ]
    apps.list_namespaced_deployment.return_value = MagicMock(items=deployments)


def test_a_paused_deployment_is_left_alone_as_under_the_kill_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(actuator, "CANARY_SECONDS", 0)
    core, apps = cluster()
    paused(apps, "api")
    apps.list_namespaced_deployment.return_value.items.append(
        MagicMock(metadata=k.V1ObjectMeta(name="web", annotations={actuator.PAUSE: "false"}))
    )
    a = make(hub_of([desired(1)] * 7, []), core, apps)
    a.step()  # paused: not applied, not used up
    assert not resized(core) and KEY not in a.held and a.paused == {"shop/Deployment/api"}
    assert actuator.mutation(new_pod()["request"], HELD, "x", a.paused) == []
    assert actuator.mutation(new_pod()["request"], HELD, "x", frozenset({"shop/Deployment/web"}))
    assert actuator.mutation(new_pod()["request"], HELD, "x", frozenset({"shop/StatefulSet/api"}))
    paused(apps)
    a.step()  # lifted: the approval applies
    assert [n for n, _ in resized(core)] == ["api-1-a"]
    paused(apps, "api")
    a.step()  # paused mid-rollout: it stops there
    assert [n for n, _ in resized(core)] == ["api-1-a"]
    apps.list_namespaced_deployment.side_effect = ApiException(status=500)
    a.step()  # its Deployments unreadable: still paused
    assert a.paused == {"shop/Deployment/api"} and len(resized(core)) == 1
    apps.list_namespaced_deployment.side_effect = None
    paused(apps)
    a.step()
    assert [n for n, _ in resized(core)] == ["api-1-a", "api-1-b"]
    a.step()  # applied
    late = pod("api-1-c", "api-1")  # from its template
    late.metadata.namespace = "shop"
    core.list_namespaced_pod.return_value.items.append(late)
    paused(apps, "api")
    a.step()  # paused: no catching up either
    assert a.held[KEY]["outcome"] == "applied" and len(resized(core)) == 2


def test_the_hub_unanswered_15_minutes_is_said_once_in_an_event() -> None:
    core = MagicMock()
    a = make(hub_of([], []), core, MagicMock())
    for at in (0.0, 899.0):
        a.answered(False, at)
    assert not core.create_namespaced_event.called
    for at in (900.0, 2000.0):
        a.answered(False, at)
    a.answered(True, 2001.0)
    for at in (2002.0, 2901.0, 2902.0):  # once more, after it answered in between
        a.answered(False, at)
    events = [c.args for c in core.create_namespaced_event.call_args_list]
    assert [(ns, e.reason, e.type, e.involved_object.kind, e.involved_object.name)
            for ns, e in events] == [
        ("squero-actuator", "HubUnreachable", "Warning", "ConfigMap", "squero-actuator")
    ] * 2  # fmt: skip
    assert events[0][1].message == (
        "the Hub has not answered for 15 minutes: holding what it applied, changing nothing new"
    )


def test_approving_what_automation_already_applied_changes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(actuator, "CANARY_SECONDS", 0)
    for rolling, deferred in ((False, False), (True, False), (True, True)):
        core, apps = cluster()
        steps = 1 if rolling else 3  # Automation's canary; then the next pod; then applied
        answers = [wants(wanted(2))] * steps + [wants(approved=(wanted(2),))]
        a = make(hub_of(answers, []), core, apps)
        for _ in range(steps):
            a.step()
        assert a.held[KEY]["outcome"] == ("rolling" if rolling else "applied")
        if deferred:  # the churn rule left one out: it waits for this rollout to end
            a.held[KEY]["deferred"] = ["memory"]
        moving = ("automated", "since", "resized", "pod")  # a rollout moves on by itself
        before = {k: v for k, v in a.held[KEY].items() if k not in moving}
        count = len(resized(core))
        a.step()  # the Operator approves the same run
        assert {k: v for k, v in a.held[KEY].items() if k not in moving} == before
        assert ("automated" in a.held[KEY]) == deferred
        assert len(resized(core)) == count + rolling
        if not rolling:
            assert actuator.mutation(new_pod()["request"], a.held, "x")  # still held


def test_a_reason_is_the_same_whatever_its_numbers() -> None:
    quota = "3 pods at cpu 500m would pass ResourceQuota q's requests.cpu 2"
    assert actuator.gist(quota) == actuator.gist(quota.replace("500m", "600m").replace("3", "4"))
    assert actuator.gist(quota) != actuator.gist("a VPA in mode Auto sizes it")


# --- Revert (slice 12.6a) -----------------------------------------------------------------


def revert(
    cid: str, at: float, name: str = "api", ns: str = "shop", kind: str = "Deployment"
) -> Any:
    return (
        "insert into reverts (contract_id, cluster, namespace, kind, name, container, "
        "asked_by, asked_at) values (%s, 'test', %s, %s, %s, 'app', 'mcp:x', %s)",
        (cid, ns, kind, name, at),
    )


def test_an_actuator_gets_its_own_pending_reverts_and_automation_stops_until_an_approval(
    settings: Settings, database_url: str
) -> None:
    import time

    now = time.time()
    app = hub(settings, database_url, automating("acme", A_TOKEN, AUTO), actuated("bank", B_TOKEN))
    with TestClient(app) as c:
        sql(
            database_url,
            run("acme", '{"test": null}'),
            sized("acme", 1),
            sized("acme", 1, "web"),
            revert("acme", now - 60),
            revert("acme", now - 60, "web"),
            revert("acme", now - 60, ns="old"),  # no longer one of the cluster's namespaces
            revert("acme", now - 60, "db", kind="StatefulSet"),  # to an Actuator that names it
            revert("bank", now - 60, "ledger"),
            # none of these closes api's Revert or resumes its Automation: from before it
            rolled_back("acme", now - 120, outcome="reverted"),
            (
                "insert into approved_sizings values ('acme', 0, 'test', 'shop', 'Deployment', "
                "'api', 'app', 1, null, 't', %s)",
                (now - 120,),
            ),
            approve("acme", 1),  # current, but older than api's Revert: never applied now
        )
        applied = report(name="api", reason="applied", at=now)  # nor an Actuation not a Revert
        c.post("/actuator/reports", json=[applied], headers=as_actuator(A_TOKEN))
        first = c.get("/actuator/desired", headers=as_actuator(A_TOKEN)).json()
        kinds = {"kinds": "Deployment,StatefulSet"}
        named = c.get("/actuator/desired", headers=as_actuator(A_TOKEN), params=kinds).json()
        done = report(outcome="reverted", name="web", reason="reverted", at=now)
        c.post("/actuator/reports", json=[done], headers=as_actuator(A_TOKEN))
        # web is approved since its Revert: Automation of it goes on
        sql(database_url, ("insert into approved_sizings values ('acme', 0, 'test', 'shop', "
                           "'Deployment', 'web', 'app', 1, null, 't', %s)", (now,)))  # fmt: skip
        after = c.get("/actuator/desired", headers=as_actuator(A_TOKEN)).json()
        sql(database_url, ("insert into kill_switches values ('acme', 't', 1)", ()))
        killed = c.get("/actuator/desired", headers=as_actuator(A_TOKEN)).json()
    undo = {"namespace": "shop", "kind": "Deployment", "container": "app"}
    assert [{k: r[k] for k in ("namespace", "kind", "name", "container")} for r in
            first["reverts"]] == [{**undo, "name": "api"}, {**undo, "name": "web"}]  # fmt: skip
    assert [r["name"] for r in named["reverts"]] == ["api", "web", "db"]
    assert first["automated"] == [] and first["sizings"] == []  # reverted, unapproved since
    assert [r["name"] for r in after["reverts"]] == ["api"]  # web's is done
    assert [w["name"] for w in after["automated"]] == ["web"]
    assert killed["reverts"] == []


def test_a_revert_puts_the_pods_back_and_holds_them_no_more(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(actuator, "CANARY_SECONDS", 0)
    core, apps = cluster()
    undo = {"id": 7, "namespace": "shop", "kind": "Deployment", "name": "api", "container": "app"}

    def undoing(*ids: int, **more: Any) -> dict[str, Any]:
        return {**desired(1), "reverts": [{**undo, "id": i, **more} for i in ids]}

    seen: list[httpx.Request] = []
    answers = [desired(1), desired(1), desired(1), undoing(7), undoing(7),
               undoing(8, name="web"), undoing(9)]  # fmt: skip
    a = make(hub_of(answers, seen), core, apps)
    for _ in range(3):
        a.step()  # run 1 applied on both pods
    assert a.held[KEY]["outcome"] == "applied"
    core.patch_namespaced_pod_resize.reset_mock()
    a.step()  # Revert 7
    assert resized(core) == [("api-1-a", body(cpu="1")), ("api-1-b", body(cpu="1"))]
    assert a.held[KEY]["outcome"] == "reverted" and a.held[KEY]["revert"] == 7
    assert actuator.mutation(new_pod()["request"], a.held, "x") == []  # held no more
    a.step()  # Revert 7 again, and run 1 again: nothing
    a.step()  # a Revert of what it never held: said so
    paused(apps, "api")
    a.step()  # paused: Revert 9 waits
    assert len(resized(core)) == 2 and a.held[KEY]["revert"] == 7
    posted = [x for r in seen if r.method == "POST" for x in json.loads(r.content)]
    assert [(x["name"], x["outcome"], x["pods"], x["reason"]) for x in posted[1:]] == [
        ("api", "reverted", 2,
         "reverted by the Operator (7): 2 pods back from cpu 115m, memory 96Mi to their "
         "template's cpu 1 (['memory'] left: the template sets none)"),
        ("web", "reverted", 0, "reverted by the Operator (8): nothing held"),
    ]  # fmt: skip
    events = [c.args[1] for c in core.create_namespaced_event.call_args_list]
    assert (events[1].reason, events[1].type) == ("SizingReverted", "Normal")
    assert events[1].message.startswith("app: back to its template (Approved Sizing, run 1)")


def test_approving_after_a_revert_applies_and_a_revert_seen_again_changes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(actuator, "CANARY_SECONDS", 0)
    core, apps = cluster()
    undo = {"id": 7, "namespace": "shop", "kind": "Deployment", "name": "api", "container": "app"}
    answers = [
        *[wants(wanted(2))] * 3,  # Automation applies run 2
        {**wants(), "reverts": [undo]},  # the Operator reverts it
        wants(approved=(wanted(2),)),  # then approves that same run: it applies again
        {**wants(approved=(wanted(2),)), "reverts": [undo]},  # its report not taken yet
    ]
    a = make(hub_of(answers, []), core, apps)
    for _ in range(4):
        a.step()
    assert a.held[KEY]["outcome"] == "reverted"
    count = len(resized(core))
    a.step()
    assert len(resized(core)) == count + 1 and a.held[KEY]["outcome"] == "rolling"
    assert a.held[KEY]["revert"] == 7  # kept across the new hold
    a.step()  # Revert 7 again: done already
    assert a.held[KEY]["outcome"] == "rolling"


def test_a_catch_up_keeps_the_last_revert_it_did() -> None:
    core, apps = cluster()
    h = applied(core, apps, wanted(memory=None))
    late = pod("api-1-c", "api-1")  # from its template: behind
    late.metadata.namespace = "shop"
    core.list_namespaced_pod.return_value.items.append(late)
    a = make(hub_of([], []), core, apps)
    a.held = {KEY: {**h, "revert": 7}}
    a.catch_up()
    assert a.held[KEY]["outcome"] == "rolling" and a.held[KEY]["revert"] == 7


# --- the console's Automation view (slice 12.6b) ------------------------------------------


def test_the_automation_view_shows_the_contracts_own_containers_and_reverts_one(
    settings: Settings, database_url: str
) -> None:
    import time

    from tests.test_console import AS_OPERATOR, OPERATOR, VIEW

    settings.console_operators = {OPERATOR}
    now = time.time()
    app = hub(settings, database_url, automating("acme", A_TOKEN, AUTO), actuated("bank", B_TOKEN))
    path, same = "/api/v1/contracts/acme/automation", {**VIEW, "Sec-Fetch-Site": "same-origin"}
    with TestClient(app) as c:
        sql(
            database_url,
            rolled_back("acme", now - 3000, name="api", outcome="skipped"),
            rolled_back("acme", now - 2000, name="api", outcome="applied"),  # api's last
            *(rolled_back("acme", now - d * 86400, name="web") for d in (1, 2, 3)),
            rolled_back("acme", now - 100, name="db", outcome="applied"),
            revert("acme", now - 50, "db"),  # pending, and stops db's Automation
            rolled_back("acme", now - 900, name="kept", outcome="failed"),
            rolled_back("acme", now - 800, name="kept", outcome="skipped"),  # leaves it held
            rolled_back("acme", now - 800, name="idle", outcome="skipped"),  # nothing held
            rolled_back("acme", now - 100, "edge", "api", "skipped"),  # edge runs no Actuator
            (
                "insert into actuations values ('acme', 'test', 'old', 'Deployment', 'x', 'app', "
                "1, 'applied', null, null, 1, 'x', 1, 1)",
                (),
            ),  # no longer one of its namespaces
            rolled_back("bank", now - 100, name="ledger", outcome="applied"),
            (
                "insert into actuations values ('acme', 'test', 'shop', 'Deployment', 'side', "
                "'sidecar', 1, 'applied', null, null, 1, 'x', 1, 1)",
                (),
            ),  # no rule automates it
        )
        page = c.get(path, headers=VIEW).json()
        what = {"cluster": "test", "namespace": "shop", "name": "api", "container": "app"}
        cross, no_token = {**VIEW, "Sec-Fetch-Site": "cross-site"}, {**AS_OPERATOR, **same}
        no_token.pop("Cookie")
        refused = [
            c.post(f"{path}/revert", json=what, headers=h).status_code for h in (cross, no_token)
        ]
        done = c.post(f"{path}/revert", json=what, headers=same)
        never = c.post(f"{path}/revert", json={**what, "name": "ledger"}, headers=same)
        sql(database_url, ("insert into kill_switches values ('acme', 't', 1)", ()))
        killed = c.post(f"{path}/revert", json=what, headers=same)
        shows_killed = c.get(path, headers=VIEW).json()["kill_switch"]
        theirs = c.get("/api/v1/contracts/bank/automation", headers=VIEW).json()
        no_cookie = c.get(path, headers=AS_OPERATOR).status_code
    assert refused == [403, 401] and no_cookie == 401
    (cluster,) = page["clusters"]
    assert cluster["cluster"] == "test" and not page["kill_switch"]
    shown = {h["name"]: h for h in cluster["containers"]}
    assert list(shown) == ["api", "db", "idle", "kept", "side", "web"] and shows_killed
    assert [n for n, h in shown.items() if h["revertable"]] == ["api", "db", "kept", "side"]
    assert shown["kept"]["outcome"] == "skipped"  # its last Actuation, a Revert still offered
    assert not shown["side"]["automated"]
    assert shown["api"]["outcome"] == "applied" and shown["api"]["automated"]
    assert shown["api"]["resumes"] is None and not shown["api"]["stopped"]
    assert shown["db"]["stopped"] and shown["db"]["reverting"]
    assert shown["web"]["resumes"] == pytest.approx(now - 3 * 86400 + 7 * 86400)
    assert done.status_code == 200
    assert next(h for h in done.json()["clusters"][0]["containers"] if h["name"] == "api")[
        "reverting"
    ]
    assert never.status_code == 409 and "reported nothing" in never.json()["detail"]
    assert killed.status_code == 409 and "Kill switch is on" in killed.json()["detail"]
    assert [h["name"] for h in theirs["clusters"][0]["containers"]] == ["ledger"]
    rows = sql(database_url, ("select name, asked_by from reverts order by id", ()))
    assert rows == [("db", "mcp:x"), ("api", f"console:{OPERATOR}")]


# --- eviction in windows (slice 12.7) --------------------------------------------------------


def evicting(cid: str, token: str, block: dict[str, Any]) -> contracts.Contract:
    data = actuated(cid, token).model_dump(mode="json", exclude_defaults=True)
    data["actuator"] = block
    return contracts.build(data, secrets={"act": SecretStr(token)})


NIGHTS = {"days": ["fri", "sat"], "start": "22:00", "end": "02:00"}


def test_a_window_opens_on_its_days_in_its_timezone_a_night_belonging_to_its_start() -> None:
    rome = contracts.ActuatorConfig(eviction=True, timezone="Europe/Rome", windows=[NIGHTS])
    at = lambda s: datetime.fromisoformat(s)  # noqa: E731
    assert rome.evicting(at("2026-11-13T21:00:00+00:00")) == "open"  # Friday 22:00 in Rome
    assert rome.evicting(at("2026-11-13T20:59:00+00:00")) == "closed"  # 21:59
    assert rome.evicting(at("2026-11-14T00:59:00+00:00")) == "open"  # Saturday 01:59: Friday's
    assert rome.evicting(at("2026-11-14T01:00:00+00:00")) == "closed"  # 02:00: ended
    assert rome.evicting(at("2026-11-15T00:30:00+00:00")) == "open"  # Sunday 01:30: Saturday's
    assert rome.evicting(at("2026-11-16T00:30:00+00:00")) == "closed"  # Monday 01:30: Sunday's
    assert rome.evicting(at("2026-11-12T21:30:00+00:00")) == "closed"  # Thursday 22:30
    day = contracts.Window(days=["sun"], start="00:00", end="24:00")
    assert day.holds(6, 0) and day.holds(6, 1439) and not day.holds(0, 0) and not day.holds(5, 1439)
    off = contracts.ActuatorConfig(windows=[NIGHTS])  # windows alone evict nothing
    assert off.evicting(at("2026-11-13T21:00:00+00:00")) == "off"
    utc = contracts.ActuatorConfig(
        eviction=True, windows=[{"days": ["fri"], "start": "22:00", "end": "23:00"}]
    )
    assert utc.evicting(at("2026-11-13T22:00:00+00:00")) == "open"  # UTC unless it says
    assert utc.evicting(at("2026-11-13T23:00:00+00:00")) == "closed"  # its end: closed


@pytest.mark.parametrize(
    ("block", "error"),
    [
        ({"eviction": True}, "eviction needs at least one window"),
        ({"timezone": "Mars/Olympus"}, "no timezone 'Mars/Olympus'"),
        ({"windows": [{**NIGHTS, "end": "22:00"}]}, "from 22:00 to 22:00 is no window"),
        ({"windows": [{**NIGHTS, "start": "24:00"}]}, "from 24:00 to 02:00 is no window"),
        ({"windows": [{**NIGHTS, "end": "25:00"}]}, "String should match pattern"),
        ({"windows": [{**NIGHTS, "days": []}]}, "at least 1 item"),
        ({"windows": [{**NIGHTS, "days": ["fry"]}]}, "Input should be 'mon'"),
    ],
)
def test_an_actuator_block_that_cannot_work_is_refused(block: dict[str, Any], error: str) -> None:
    with pytest.raises(ValueError, match=error):
        evicting("acme", A_TOKEN, block)


def test_eviction_needs_a_cluster_that_runs_an_actuator() -> None:
    data = actuated("acme", A_TOKEN).model_dump(mode="json", exclude_defaults=True)
    data["clusters"][0].pop("actuator_token_secret")
    data["actuator"] = {"eviction": True, "windows": [NIGHTS]}
    with pytest.raises(ValueError, match="eviction, but no cluster has an actuator_token_secret"):
        contracts.build(data, secrets={})


def test_an_actuator_hears_only_its_own_contracts_eviction_window(
    settings: Settings, database_url: str
) -> None:
    today = datetime.now(UTC).weekday()
    always = {"days": list(contracts.DAYS), "start": "00:00", "end": "24:00"}
    never = {"days": [d for i, d in enumerate(contracts.DAYS) if (i - today) % 7 not in (0, 1, 6)]}
    never |= {"start": "00:00", "end": "24:00"}  # not today, nor a day whose night runs into it
    acme = evicting("acme", A_TOKEN, {"eviction": True, "windows": [always]})
    bank = actuated("bank", B_TOKEN)
    shut = evicting("shut", "c" * 40, {"eviction": True, "windows": [never]})
    app = hub(settings, database_url, acme, bank, shut)
    with TestClient(app) as c:
        seen = [
            c.get("/actuator/desired", headers=as_actuator(t)).json()["eviction"]
            for t in (A_TOKEN, B_TOKEN, "c" * 40)
        ]
        sql(database_url, ("insert into kill_switches values ('acme', 't', 1)", ()))
        killed = c.get("/actuator/desired", headers=as_actuator(A_TOKEN)).json()["eviction"]
    assert seen == ["open", "off", "closed"] and killed == "off"


def infeasible(core: Any) -> None:
    """api-1-a's resize Infeasible from now on."""
    core.read_namespaced_pod.side_effect = None
    core.read_namespaced_pod.return_value = pending("Infeasible", "Node didn't have enough")


def up(name: str, ready: bool = True, phase: str = "Running", **requests: str) -> Any:
    """A new pod of api-1 (a replacement), Ready or not."""
    p = pod(name, "api-1", phase=phase, **requests)
    p.metadata.namespace = "shop"
    p.status.conditions = [k.V1PodCondition(type="Ready", status="True" if ready else "False")]
    allocated = k.V1ResourceRequirements(requests=p.spec.containers[0].resources.requests)
    p.status.container_statuses = [status("app", allocated)]
    return p


def evicted(core: Any) -> list[tuple[str, str, str]]:
    return [
        (c.args[0], c.args[1], c.args[2].metadata.name)
        for c in core.create_namespaced_pod_eviction.call_args_list
    ]


def reasons(core: Any) -> list[str]:
    return [c.args[1].reason for c in core.create_namespaced_event.call_args_list]


def test_an_infeasible_pod_waits_for_the_window_then_is_evicted_and_replaced() -> None:
    core, apps = cluster()
    _, held = start(core, apps, wanted())
    infeasible(core)
    for at in (60.0, 120.0, 5000.0):  # no 10 minutes' limit while it waits
        assert actuator.advance(core, apps, KEY, held, at, "closed", PDB) is None
    assert reasons(core) == ["EvictionWaiting"] and held["waiting"] == "window"
    assert not core.create_namespaced_pod_eviction.called
    message = core.create_namespaced_event.call_args.args[1].message
    assert message.startswith("api-1-a: Infeasible: Node didn't have enough; it waits")
    assert actuator.advance(core, apps, KEY, held, 6000.0, "open", PDB) is None
    assert evicted(core) == [("api-1-a", "shop", "api-1-a")]
    assert held["evicted"] == {"pod": "api-1-a", "at": 6000.0, "before": ["api-1-a", "api-1-b"]}
    assert held["evicted_at"] == 6000.0 and "waiting" not in held
    assert reasons(core) == ["EvictionWaiting", "PodEvicted"]
    assert "its replacement starts with cpu 115m, memory 96Mi" in (
        core.create_namespaced_event.call_args.args[1].message
    )
    pods = core.list_namespaced_pod.return_value.items
    pods[0].metadata.deletion_timestamp = datetime.now(UTC)  # api-1-a goes
    pods[1] = up("api-1-b")  # Ready, but no replacement: it was there before
    pods.append(up("api-1-c", ready=False, cpu="115m", memory="96Mi"))
    assert actuator.advance(core, apps, KEY, held, 6060.0, "open", PDB) is None  # not Ready yet
    assert "evicted" in held and len(evicted(core)) == 1
    pods[-1] = up("api-1-c", cpu="115m", memory="96Mi")  # the webhook gave it the requests
    assert actuator.advance(core, apps, KEY, held, 6120.0, "open", PDB) is None
    assert "evicted" not in held and (held["pod"], held["since"]) == ("api-1-c", 6120.0)
    assert held["canary_until"] == 6120.0 + 600  # the canary's 10 minutes, again
    assert held["pods"]["api-1-c"] == {"at": 6120.0, "restarts": 0}
    core.read_namespaced_pod.side_effect = lambda name, ns: up(name, cpu="115m", memory="96Mi")
    assert actuator.advance(core, apps, KEY, held, 6719.0, "open", PDB) is None
    assert actuator.advance(core, apps, KEY, held, 6720.0, "open", PDB) is None  # the next: b
    assert resized(core)[-1] == ("api-1-b", body(cpu="115m", memory="96Mi"))
    done = actuator.advance(core, apps, KEY, held, 6780.0, "open", PDB)
    assert done is not None and (done.outcome, done.pods) == ("applied", 2)
    assert done.reason == (
        "2 of 2 pods resized in place, one at a time; api-1-a evicted, its resize Infeasible"
    )
    assert held["evicted_at"] == 6000.0  # kept: the next eviction 5 minutes after


def test_a_disruption_budget_holds_an_eviction_and_other_refusals_fail_the_rollout() -> None:
    core, apps = cluster()
    _, held = start(core, apps, wanted())
    infeasible(core)
    core.create_namespaced_pod_eviction.side_effect = refusal(429, "would violate the budget")
    for at in (60.0, 120.0):
        assert actuator.advance(core, apps, KEY, held, at, "open", PDB) is None
        assert "evicted" not in held  # refused: nothing to wait for
    assert held["waiting"] == "budget" and "evicted" not in held and "evicted_at" not in held
    assert reasons(core) == ["EvictionWaiting"]
    assert "a PodDisruptionBudget allows no eviction now" in (
        core.create_namespaced_event.call_args.args[1].message
    )
    core.create_namespaced_pod_eviction.side_effect = refusal(403, "forbidden")
    failed = actuator.advance(core, apps, KEY, held, 180.0, "open", PDB)
    assert failed is not None and failed.outcome == "failed"
    assert failed.reason == "evicting api-1-a: 403: forbidden"
    assert "evicted" not in held and "waiting" not in held
    off = start(core, apps, wanted())[1]
    gone = actuator.advance(core, apps, KEY, off, 60.0, "off")  # as before slice 12.7
    assert gone is not None and gone.outcome == "failed" and "Infeasible" in gone.reason


def test_a_replacement_the_webhook_missed_ends_the_rollout() -> None:
    core, apps = cluster()
    _, held = start(core, apps, wanted())
    infeasible(core)
    actuator.advance(core, apps, KEY, held, 60.0, "open", PDB)
    core.list_namespaced_pod.return_value.items.append(up("api-1-c"))  # its template's cpu 1
    failed = actuator.advance(core, apps, KEY, held, 120.0, "open", PDB)
    assert failed is not None and failed.outcome == "failed"
    assert failed.reason == (
        "api-1-c, replacing evicted api-1-a, started from its template; "
        "api-1-a evicted, its resize Infeasible"
    )
    assert "evicted" not in held and len(evicted(core)) == 1  # never again, in a loop


def test_a_replacement_not_ready_in_10_minutes_is_rolled_back_and_evicted_if_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(actuator.time, "time", lambda: 660.0)
    core, apps = cluster()
    _, held = start(core, apps, wanted())
    infeasible(core)
    actuator.advance(core, apps, KEY, held, 60.0, "open", PDB)
    pods = core.list_namespaced_pod.return_value.items
    pods[0].metadata.deletion_timestamp = datetime.now(UTC)
    pods[1] = up("api-1-b", phase="Pending")  # Ready or not, there before: not a replacement
    pods[1].status.conditions[0].status = "True"
    stuck = up("api-1-c", ready=False, phase="Pending", cpu="115m", memory="96Mi")
    pods.append(stuck)
    assert actuator.trouble(core, apps, KEY, held, 659.0) is None  # 10 minutes not yet
    assert actuator.trouble(core, apps, KEY, held, 660.0) == (
        "api-1-a evicted: no replacement Ready in 10 minutes"
    )
    pods[-1] = up("api-1-c", cpu="115m", memory="96Mi")  # up, though advance() never saw it
    assert actuator.trouble(core, apps, KEY, held, 660.0) is None
    pods[-1] = stuck
    pods.append(up("api-1-d", ready=False, cpu="115m", memory="96Mi"))  # running: in place
    held_then: list[bool] = []
    core.create_namespaced_pod_eviction.side_effect = lambda *_: held_then.append("evicted" in held)
    a = actuator.Actuator(MagicMock(), core, apps, "x", held={KEY: held})
    (back,) = a.watch()
    assert held_then == [False]  # let go first: the webhook gives its replacement nothing
    assert [n for n, _ in resized(core)][-1] == "api-1-d"
    assert back.outcome == "rolled_back"
    assert "; 1 Pending replacement evicted to start from its template" in back.reason
    assert evicted(core)[1:] == [("api-1-c", "shop", "api-1-c")]  # nor api-1-b, Pending
    assert all(n != "api-1-c" for n, _ in resized(core))  # evicted, never resized
    assert "evicted" not in held and held["outcome"] == "rolled_back"


def test_the_webhook_sizes_a_replacement_only_while_its_eviction_is_outstanding() -> None:
    rolling = {k: {**v, "outcome": "rolling"} for k, v in HELD.items()}
    assert patched(new_pod(), held=rolling) is None  # a rollout's new pods: from the template
    out = {
        k: {**v, "evicted": {"pod": "api-1-a", "at": 0.0, "before": []}} for k, v in rolling.items()
    }
    assert patched(new_pod(), held=out) == patched(new_pod())


def test_one_eviction_at_a_time_and_five_minutes_apart(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(actuator.time, "time", lambda: clock[0])
    core, apps = cluster()
    _, first = start(core, apps, wanted())
    other = {**first, "pod": "api-1-b"}
    infeasible(core)
    a = actuator.Actuator(
        MagicMock(),
        core,
        apps,
        "x",
        held={KEY: first, "shop/Deployment/api/side": other},
        policy=PDB,
    )
    a.advance("open")
    assert evicted(core) == [("api-1-a", "shop", "api-1-a")]  # the second waits: one at a time
    assert a.held["shop/Deployment/api/side"]["waiting"] == "window"
    del first["evicted"]  # its replacement up
    clock[0] = 1299.0
    a.advance("open")
    assert len(evicted(core)) == 1  # 5 minutes not yet
    clock[0] = 1300.0
    a.advance("open")
    assert len(evicted(core)) == 2
    a.advance("closed")
    assert len(evicted(core)) == 2
    first["evicted"] = {"pod": "api-1-a", "at": 0.0, "before": ["api-1-a", "api-1-b"]}
    core.list_namespaced_pod.return_value.items[0].metadata.deletion_timestamp = datetime.now(UTC)
    first["evicted_at"] = 0.0
    a.held["shop/Deployment/api/side"].pop("evicted", None)
    a.held["shop/Deployment/api/side"]["evicted_at"] = 0.0
    clock[0] = 9000.0
    a.advance("open")
    assert len(evicted(core)) == 2  # one replacement still awaited: no other eviction


def test_each_minute_passes_the_hubs_eviction_word_to_the_rollouts() -> None:
    core, apps = cluster()
    open_ = {**desired(1), "eviction": "open"}
    a = make(hub_of([open_, open_], []), core, apps)
    a.step()  # the canary
    infeasible(core)
    a.step()
    assert evicted(core) == [("api-1-a", "shop", "api-1-a")]


def test_a_newer_automation_sizing_replaces_a_rollout_waiting_to_evict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(actuator.time, "time", lambda: 100.0)
    core, apps = cluster()
    a = make(MagicMock(), core, apps)
    a.act([], [wanted(1, memory=None)])
    assert a.held[KEY]["outcome"] == "rolling"
    a.act([], [wanted(2, cpu=0.5, memory=None)])
    assert a.held[KEY]["run_id"] == 1  # rolling: waits, unsaid
    a.held[KEY] |= {"waiting": "window", "evicted_at": 50.0}
    a.act([], [wanted(3, cpu=0.5, memory=None)])
    assert a.held[KEY]["run_id"] == 3 and a.held[KEY]["outcome"] == "rolling"
    assert a.held[KEY]["evicted_at"] == 50.0 and "waiting" not in a.held[KEY]
    a.held[KEY] = {**a.held[KEY], "outcome": "applied", "pods": {}}
    core.list_namespaced_pod.return_value.items.append(up("api-1-c"))  # behind its hold
    a.catch_up()
    assert a.held[KEY]["evicted_at"] == 50.0  # kept through a catch-up too


def test_a_deployment_scaled_down_after_an_eviction_owes_no_replacement() -> None:
    core, apps = cluster()
    _, held = start(core, apps, wanted())
    infeasible(core)
    actuator.advance(core, apps, KEY, held, 60.0, "open", PDB)
    core.list_namespaced_pod.return_value.items[0].metadata.deletion_timestamp = datetime.now(UTC)
    assert actuator.trouble(core, apps, KEY, held, 660.0) is not None  # 1 of 2: one owed
    apps.read_namespaced_deployment.return_value.spec.replicas = 1  # its HPA scaled it down
    assert actuator.trouble(core, apps, KEY, held, 660.0) is None  # no Rollback for that
    assert actuator.advance(core, apps, KEY, held, 120.0, "open", PDB) is None
    assert "evicted" not in held  # the rollout goes on with the pods it has


def test_no_sizing_replaces_a_hold_while_its_evicted_pods_replacement_is_awaited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(actuator.time, "time", lambda: 100.0)
    core, apps = cluster()
    a = make(MagicMock(), core, apps)
    a.act([wanted(1)])
    a.held[KEY]["evicted"] = {"pod": "api-1-a", "at": 90.0, "before": ["api-1-a", "api-1-b"]}
    a.act([wanted(2, cpu=0.5)], [wanted(3, cpu=0.6)])
    assert a.held[KEY]["run_id"] == 1 and "evicted" in a.held[KEY]  # both wait
    del a.held[KEY]["evicted"]
    a.act([wanted(2, cpu=0.5)])
    assert a.held[KEY]["run_id"] == 2


ROOMLESS = 'pods "api-1-a" is forbidden: node didn\'t have enough allocatable resources: cpu'


def test_a_resize_the_api_server_refuses_for_want_of_room_is_evicted_as_infeasible() -> None:
    """Kubernetes 1.35+ refuses at once a resize its node can never fit, where 1.33 marks
    the pod's resize Infeasible (found in the live check on 1.37)."""
    core, apps = cluster()
    core.patch_namespaced_pod_resize.side_effect = refusal(403, ROOMLESS)
    off, _ = actuator.apply(core, apps, wanted(), 0.0, *scalers())  # off: failed, as before
    assert off is not None and off.outcome == "failed" and "enough allocatable" in off.reason
    other = refusal(403, "forbidden: something else")
    core.patch_namespaced_pod_resize.side_effect = other
    a, _ = actuator.apply(core, apps, wanted(), 0.0, *scalers(), eviction="open")
    assert a is not None and a.outcome == "failed"  # only want of room is evicted
    core.patch_namespaced_pod_resize.side_effect = refusal(403, ROOMLESS)
    a, held = actuator.apply(core, apps, wanted(), 0.0, *scalers(), eviction="closed")
    assert a is None and held["outcome"] == "rolling" and held["no_room"] == ROOMLESS
    assert actuator.advance(core, apps, KEY, held, 6000.0, "closed", PDB) is None  # no 10 minutes
    assert reasons(core) == ["EvictionWaiting"]
    assert actuator.advance(core, apps, KEY, held, 6060.0, "open", PDB) is None
    assert evicted(core) == [("api-1-a", "shop", "api-1-a")]
    pods = core.list_namespaced_pod.return_value.items
    pods[0].metadata.deletion_timestamp = datetime.now(UTC)
    pods.append(up("api-1-c", cpu="115m", memory="96Mi"))
    assert actuator.advance(core, apps, KEY, held, 6120.0, "open", PDB) is None
    assert "no_room" not in held and held["pod"] == "api-1-c"
    core.patch_namespaced_pod_resize.side_effect = refusal(403, ROOMLESS)  # b: no room either
    core.read_namespaced_pod.side_effect = lambda name, ns: up(name, cpu="115m", memory="96Mi")
    off = json.loads(json.dumps(held))
    assert actuator.advance(core, apps, KEY, held, 6720.0, "open", PDB) is None
    assert (held["pod"], held["no_room"], held["resized"]) == ("api-1-b", ROOMLESS, 2)
    ended = actuator.advance(core, apps, KEY, off, 6720.0, "off")  # off meanwhile: as before
    assert ended is not None and ended.outcome == "failed"
    assert ended.reason.startswith("resizing its next pod: 403: ")


def test_the_actuator_keeps_the_hubs_eviction_word_for_new_rollouts() -> None:
    core, apps = cluster()
    core.patch_namespaced_pod_resize.side_effect = refusal(403, ROOMLESS)
    closed = {**desired(1), "eviction": "closed"}
    a = make(hub_of([closed], []), core, apps)
    a.step()
    assert a.eviction == "closed" and a.held[KEY]["no_room"] == ROOMLESS
    assert a.held[KEY]["outcome"] == "rolling"  # waits for a window, not failed


def test_a_pod_without_a_disruption_budget_or_a_second_replica_is_never_evicted() -> None:
    other = k.V1LabelSelector(match_labels={"app": "web"})
    none = "no PodDisruptionBudget covers it, so it is never evicted"
    one = "it has one replica, so it is never evicted"
    for policy, replicas, eviction, why_ in (
        (budgets(), 2, "closed", none),  # no wait for a window that changes nothing
        (budgets(other, None), 2, "open", none),
        (budgets(refusal(403, "forbidden")), 2, "open", "not evicted: checking it: 403: forbidden"),
        (PDB, 1, "open", one),
        (PDB, None, "closed", one),
    ):
        core, apps = cluster()
        apps.read_namespaced_deployment.return_value.spec.replicas = replicas
        _, held = start(core, apps, wanted())
        infeasible(core)
        failed = actuator.advance(core, apps, KEY, held, 60.0, eviction, policy)
        assert failed is not None and failed.outcome == "failed"
        assert failed.reason == (
            f"api-1-a: Infeasible: Node didn't have enough; 0 other pods resized; {why_}"
        )
        assert "waiting" not in held and not core.create_namespaced_pod_eviction.called
    core, apps = cluster()
    _, held = start(core, apps, wanted())
    infeasible(core)
    off = actuator.advance(core, apps, KEY, held, 60.0, "off", None)  # nothing read
    assert off is not None and off.reason.endswith("0 other pods resized")


@pytest.mark.parametrize(
    ("selector", "matches"),
    [
        (None, False),
        (k.V1LabelSelector(), True),
        (k.V1LabelSelector(match_labels={"app": "api"}), True),
        (k.V1LabelSelector(match_labels={"app": "api", "tier": "x"}), False),
        *[
            (
                k.V1LabelSelector(
                    match_expressions=[k.V1LabelSelectorRequirement(key=key, operator=op, values=v)]
                ),
                m,
            )
            for key, op, v, m in (
                ("app", "In", ["web", "api"], True),
                ("app", "In", ["web"], False),
                ("tier", "In", ["x"], False),
                ("app", "NotIn", ["api"], False),
                ("app", "NotIn", ["web"], True),
                ("tier", "NotIn", ["x"], True),
                ("app", "Exists", None, True),
                ("tier", "Exists", None, False),
                ("tier", "DoesNotExist", None, True),
                ("app", "DoesNotExist", None, False),
                ("app", "Bogus", None, False),
            )
        ],
    ],
)
def test_a_disruption_budgets_selector_matches_as_kubernetes_does(
    selector: Any, matches: bool
) -> None:
    assert actuator.selects(selector, {"app": "api"}) is matches


# --- StatefulSets and DaemonSets (slice 12.8a) ---------------------------------------------


def kinded(kind: str, **kw: Any) -> tuple[Any, Any]:
    """cluster(), api a `kind` (uid u1) owning api-1's pods itself; api-x-a another's."""
    core, apps = cluster(**kw)
    d = apps.read_namespaced_deployment.return_value
    on = {"selector": d.spec.selector, "template": d.spec.template}
    getattr(apps, f"read_namespaced_{actuator.KINDS[kind]}").return_value = (
        k.V1StatefulSet(metadata=d.metadata, spec=k.V1StatefulSetSpec(**on, replicas=2))
        if kind == "StatefulSet"
        else k.V1DaemonSet(metadata=d.metadata, spec=k.V1DaemonSetSpec(**on))  # no replicas
    )
    apps.read_namespaced_deployment.side_effect = ApiException(status=404)
    for p in core.list_namespaced_pod.return_value.items:
        mine = p.metadata.owner_references[0].name == "api-1"
        p.metadata.owner_references = [
            owner(kind, "api" if mine else "api-old", "u1" if mine else "u2")
        ]
    return core, apps


def of(kind: str, **more: Any) -> Any:
    return wanted(**more).model_copy(update={"kind": kind})


@pytest.mark.parametrize("kind", ["StatefulSet", "DaemonSet"])
def test_a_statefulset_or_daemonset_is_resized_on_its_own_pods(kind: str) -> None:
    core, apps = kinded(kind)
    a, held = start(core, apps, of(kind))
    assert a is None and resized(core) == [("api-1-a", body(cpu="115m", memory="96Mi"))]
    key = f"shop/{kind}/api/app"
    assert actuator.advance(core, apps, key, held, 600.0) is None  # the next: b
    done = actuator.advance(core, apps, key, held, 660.0)
    assert done is not None and (done.outcome, done.kind, done.pods) == ("applied", kind, 2)
    assert [n for n, _ in resized(core)] == ["api-1-a", "api-1-b"]  # not api-x-a, another's
    assert not apps.list_namespaced_replica_set.called


def test_an_hpa_or_a_vpa_counts_only_for_its_own_kind() -> None:
    core, apps = kinded("StatefulSet")
    deployments = {"spec": {"targetRef": {"kind": "Deployment", "name": "api"}}}
    a, held = start(core, apps, of("StatefulSet"), vpas=[deployments], hpas=[hpa()])
    assert a is None and held["requests"] == {"cpu": "115m", "memory": "96Mi"}
    own = {"spec": {"targetRef": {"kind": "StatefulSet", "name": "api"}}}
    vpa, _ = start(core, apps, of("StatefulSet"), vpas=[own])
    assert vpa.reason == "a VPA in mode Auto sizes it"
    scaler = hpa()
    scaler.spec.scale_target_ref.kind = "StatefulSet"
    core, apps = kinded("StatefulSet")
    _, held = start(core, apps, of("StatefulSet"), hpas=[scaler])
    assert held["requests"] == {"memory": "96Mi"}


@pytest.mark.parametrize("kind", ["StatefulSet", "Rollout"])  # a DaemonSet's is left as it runs
def test_a_statefulsets_or_rollouts_pod_is_never_evicted(kind: str) -> None:
    core, apps = rolled() if kind == "Rollout" else kinded(kind)
    _, held = start(core, apps, of(kind))
    infeasible(core)
    failed = actuator.advance(core, apps, f"shop/{kind}/api/app", held, 60.0, "open", PDB)
    assert failed is not None and failed.outcome == "failed"
    assert failed.reason.endswith(f"; a {kind}'s pod is never evicted")
    assert not core.create_namespaced_pod_eviction.called


def owned_by(kind: str, name: str, **resources: Any) -> dict[str, Any]:
    review = new_pod(**resources)
    meta = review["request"]["object"]["metadata"]
    meta["labels"] = {"app": name}
    meta["ownerReferences"] = [{"kind": kind, "name": name, "controller": True}]
    return review


def test_the_webhook_sizes_statefulset_pods_and_only_lowers_daemonset_pods() -> None:
    def hold(kind: str, **requests: str) -> Any:
        return {f"shop/{kind}/db/app": {**HELD["shop/Deployment/api/app"], "requests": requests}}

    cpu = "/spec/containers/1/resources/requests/cpu"
    memory = "/spec/containers/1/resources/requests/memory"
    lower = {"cpu": "115m", "memory": "64Mi"}  # cpu under the template's 1, memory unset there
    assert patched(owned_by("StatefulSet", "db"), hold("StatefulSet", **lower)) == [
        {"op": "add", "path": cpu, "value": "115m"},
        {"op": "add", "path": memory, "value": "48Mi"},  # its limit
    ]
    assert patched(owned_by("DaemonSet", "db"), hold("DaemonSet", **lower)) == [
        {"op": "add", "path": cpu, "value": "115m"}  # memory: a raise from none
    ]
    assert patched(owned_by("DaemonSet", "db"), hold("DaemonSet", cpu="2")) is None  # a raise
    assert patched(owned_by("DaemonSet", "db"), hold("DaemonSet", cpu="1")) is None  # the same
    assert patched(owned_by("StatefulSet", "db"), hold("DaemonSet", **lower)) is None
    assert patched(owned_by("StatefulSet", "db"), hold("Deployment", **lower)) is None
    held = hold("StatefulSet", **lower)
    req = owned_by("StatefulSet", "db")["request"]
    assert actuator.mutation(req, held, "x", frozenset({"shop/StatefulSet/db"})) == []
    assert actuator.mutation(req, held, "x", frozenset({"shop/Deployment/db"}))
    lone = owned_by("ReplicaSet", "db-5d8f")  # a ReplicaSet of no Deployment: no hash label
    assert patched(lone, {"shop/Deployment/db/app": HELD["shop/Deployment/api/app"]}) is None


def test_pauses_are_read_for_each_kind_it_holds() -> None:
    apps = MagicMock()
    note = {actuator.PAUSE: "true"}
    apps.list_namespaced_deployment.return_value = MagicMock(
        items=[MagicMock(metadata=k.V1ObjectMeta(name="db", annotations=note))]
    )
    apps.list_namespaced_stateful_set.return_value = MagicMock(
        items=[MagicMock(metadata=k.V1ObjectMeta(name="api", annotations=note))]
    )
    apps.list_namespaced_rollout.return_value = MagicMock(
        items=[MagicMock(metadata=k.V1ObjectMeta(name="web", annotations=note))]
    )
    a = make(hub_of([], []), MagicMock(), apps)
    keys = {"shop/StatefulSet/api/app", "shop/Deployment/api/app", "shop/Rollout/web/app"}
    a.pauses(keys)
    assert a.paused == {"shop/Deployment/db", "shop/StatefulSet/api", "shop/Rollout/web"}
    assert a.is_paused("shop/StatefulSet/api/app") and not a.is_paused("shop/Deployment/api/app")
    assert not apps.list_namespaced_daemon_set.called
    apps.list_namespaced_stateful_set.side_effect = ApiException(status=500)
    apps.list_namespaced_deployment.return_value = MagicMock(items=[])
    apps.list_namespaced_rollout.side_effect = ApiException(status=404)  # Argo uninstalled
    a.pauses(keys)  # its StatefulSets and Rollouts unreadable: they keep what it knew
    assert a.paused == {"shop/StatefulSet/api", "shop/Rollout/web"}


def test_a_statefulset_is_read_as_one_for_its_hand_back_catch_up_and_a_kind_it_skips() -> None:
    core, apps = kinded("DaemonSet")
    a, held = start(core, apps, of("DaemonSet"))
    for at in (600.0, 660.0):
        a = actuator.advance(core, apps, "shop/DaemonSet/api/app", held, at)
    assert a is not None and a.outcome == "applied"
    act = make(hub_of([], []), core, apps, held={"shop/DaemonSet/api/app": held})
    late = pod("api-1-c", "api-1")  # a new node's pod, from its template
    late.metadata.namespace = "shop"
    late.metadata.owner_references = [owner("DaemonSet", "api", "u1")]
    core.list_namespaced_pod.return_value.items.append(late)
    act.catch_up()
    assert resized(core)[-1] == ("api-1-c", body(cpu="115m", memory="96Mi"))  # in place
    assert act.changed("shop/DaemonSet/api/app", held) is None
    apps.read_namespaced_daemon_set.side_effect = ApiException(status=404)
    assert act.changed("shop/DaemonSet/api/app", held) == "no such DaemonSet"
    gone, _ = start(core, apps, of("DaemonSet"))
    assert (gone.outcome, gone.reason) == ("skipped", "no such DaemonSet")
    apps.read_namespaced_daemon_set.side_effect = ApiException(status=500, reason="boom")
    broken, _ = start(core, apps, of("DaemonSet"))
    assert (broken.outcome, broken.reason) == ("failed", "reading the DaemonSet: 500: boom")
    lone, _ = start(core, apps, of("Job"))
    assert (lone.outcome, lone.reason) == ("skipped", "a Job is not resized")


def stuck(core: Any, *names: str, reason: str = "Infeasible") -> None:
    """These pods' resizes held back by the kubelet: allocated as they started."""
    normal = core.read_namespaced_pod.side_effect

    def read(name: str, ns: str) -> Any:
        p = normal(name, ns)
        if name in names:
            p.status.conditions = [
                k.V1PodCondition(type="PodResizePending", status="True", reason=reason,
                                 message="Node didn't have enough")
            ]  # fmt: skip
            was = k.V1ResourceRequirements(requests={"cpu": "1", "memory": "512Mi"})
            p.status.container_statuses = [status("app", was)]
        return p

    core.read_namespaced_pod.side_effect = read


DS = "shop/DaemonSet/api/app"
NONE = "left without its raise: its node has no room"
FULL = body(cpu="4000m", memory="96Mi")  # a raise of cpu (from 1), memory lowered (512Mi)
TEMPLATE = {"cpu": "1", "memory": "512Mi"}


def raised() -> Any:
    return of("DaemonSet", cpu=4.0)


def test_a_daemonsets_pod_its_node_has_no_room_for_is_left_and_the_canary_moves_on() -> None:
    core, apps = kinded("DaemonSet")
    _, held = start(core, apps, raised())  # the canary: api-1-a
    stuck(core, "api-1-a")
    assert actuator.advance(core, apps, DS, held, 60.0, "open", PDB) is None  # never evicted
    assert not core.create_namespaced_pod_eviction.called
    assert held["left"] == ["api-1-a"] and "api-1-a" not in held["pods"]
    assert (held["pod"], held["resized"], held["canary_until"]) == ("api-1-b", 1, 660.0)
    assert resized(core) == [
        ("api-1-a", FULL),
        ("api-1-a", body(cpu="1", memory="96Mi")),  # its raise undone, its lowering kept
        ("api-1-b", FULL),  # the canary now
    ]
    event = core.create_namespaced_event.call_args.args[1]
    assert (event.reason, event.type) == ("PodLeft", "Normal")
    assert event.message == (
        f"api-1-a {NONE} for cpu 4000m, memory 96Mi (Node didn't have enough); "
        "lowered in place to memory 96Mi"
    )
    assert actuator.advance(core, apps, DS, held, 659.0) is None  # its 10 minutes
    done = actuator.advance(core, apps, DS, held, 660.0)
    assert done is not None and (done.outcome, done.pods) == ("applied", 1)
    assert done.reason == f"1 of 2 pods resized in place, one at a time; api-1-a {NONE}"
    assert held["left"] == ["api-1-a"]  # kept: never caught up


def test_a_daemonset_whose_nodes_all_lack_room_fails_and_deferred_or_refused_pods_are_left(
) -> None:  # fmt: skip
    core, apps = kinded("DaemonSet")
    _, held = start(core, apps, raised())
    stuck(core, "api-1-a", "api-1-b")
    assert actuator.advance(core, apps, DS, held, 60.0) is None  # a left, b resized
    failed = actuator.advance(core, apps, DS, held, 120.0)
    assert failed is not None and (failed.outcome, failed.pods) == ("failed", 0)
    assert failed.reason == (
        f"no node of its 2 pods has room for them; api-1-a {NONE}; api-1-b {NONE}"
    )
    core, apps = kinded("DaemonSet")
    _, held = start(core, apps, raised())
    stuck(core, "api-1-a", reason="Deferred")  # full now: 10 minutes, then left
    assert actuator.advance(core, apps, DS, held, 599.0) is None and "left" not in held
    assert actuator.advance(core, apps, DS, held, 600.0) is None and held["left"] == ["api-1-a"]
    core, apps = kinded("DaemonSet")
    patch = core.patch_namespaced_pod_resize.side_effect
    core.patch_namespaced_pod_resize.side_effect = refusal(403, ROOMLESS)  # Kubernetes 1.35+
    a, held = start(core, apps, raised())  # eviction off: left all the same
    core.patch_namespaced_pod_resize.side_effect = patch  # b's node has room
    assert actuator.advance(core, apps, DS, held, 60.0) is None
    assert held["left"] == ["api-1-a"] and "no_room" not in held  # b's resize is its own
    assert resized(core) == [
        ("api-1-a", FULL),  # refused: nothing pending to undo
        ("api-1-a", body(memory="96Mi")),  # its lowering alone, which fits
        ("api-1-b", FULL),
    ]
    done = actuator.advance(core, apps, DS, held, 660.0)
    assert done is not None and (done.outcome, done.pods) == ("applied", 1)
    core, apps = kinded("DaemonSet")
    core.patch_namespaced_pod_resize.side_effect = refusal(403, ROOMLESS)
    a, held = start(core, apps, raised())
    assert a is None and "enough allocatable" in held["no_room"]
    assert actuator.advance(core, apps, DS, held, 60.0) is None  # a left, b refused too
    assert held["left"] == ["api-1-a"] and (held["pod"], held["resized"]) == ("api-1-b", 1)
    assert "enough allocatable" in held["no_room"]
    refused = actuator.advance(core, apps, DS, held, 120.0)
    assert refused is not None and refused.outcome == "failed" and "no_room" not in held


def test_a_new_nodes_pod_with_no_room_is_left_and_the_daemonset_keeps_its_requests() -> None:
    core, apps = kinded("DaemonSet", requests=TEMPLATE)
    _, held = start(core, apps, raised())
    for at in (600.0, 660.0):
        actuator.advance(core, apps, DS, held, at)
    act = make(hub_of([], []), core, apps, held={DS: held})
    late = pod("api-1-c", "api-1")  # a new node's pod, from its template
    late.metadata.namespace = "shop"
    late.metadata.owner_references = [owner("DaemonSet", "api", "u1")]
    core.list_namespaced_pod.return_value.items.append(late)
    act.catch_up()
    assert resized(core)[-1] == ("api-1-c", FULL)
    stuck(core, "api-1-c")
    (done,) = act.advance("off")
    assert (done.outcome, done.pods) == ("applied", 0)
    assert done.reason == f"0 of 3 pods resized in place, one at a time; api-1-c {NONE}"
    assert act.held[DS]["outcome"] == "applied" and act.held[DS]["left"] == ["api-1-c"]
    assert resized(core)[-1] == ("api-1-c", body(cpu="1", memory="96Mi"))
    new = owned_by("DaemonSet", "api", requests=TEMPLATE)["request"]
    assert actuator.mutation(new, act.held, "x") == [  # still held: new pods lowered
        {"op": "add", "path": "/spec/containers/1/resources/requests/memory", "value": "96Mi"}
    ]
    count = len(resized(core))
    assert act.catch_up() == [] and len(resized(core)) == count  # api-1-c not tried again
    later = pod("api-1-d", "api-1")  # another new node's: caught up, api-1-c still left
    later.metadata.namespace = "shop"
    later.metadata.owner_references = [owner("DaemonSet", "api", "u1")]
    core.list_namespaced_pod.return_value.items.append(later)
    act.catch_up()
    assert [n for n, _ in resized(core)[count:]] == ["api-1-d"]
    assert act.held[DS]["left"] == ["api-1-c"] and act.held[DS]["pod"] == "api-1-d"


# --- Argo Rollouts (slice 12.8b) -----------------------------------------------------------


def rolled(**kw: Any) -> tuple[Any, Any]:
    """cluster(), api an Argo Rollout (uid u1) owning ReplicaSet api-1; api-x another's."""
    core, apps = cluster(**kw)
    apps.read_namespaced_rollout.return_value = apps.read_namespaced_deployment.return_value
    apps.read_namespaced_deployment.side_effect = ApiException(status=404)
    for rs in apps.list_namespaced_replica_set.return_value.items:
        rs.metadata.owner_references[0].kind = "Rollout"
    return core, apps


def test_an_argo_rollout_is_resized_on_its_own_replicasets_pods() -> None:
    core, apps = rolled()
    a, held = start(core, apps, of("Rollout"))
    assert a is None and resized(core) == [("api-1-a", body(cpu="115m", memory="96Mi"))]
    key = "shop/Rollout/api/app"
    assert actuator.advance(core, apps, key, held, 600.0) is None  # the next: b
    done = actuator.advance(core, apps, key, held, 660.0)
    assert done is not None and (done.outcome, done.kind, done.pods) == ("applied", "Rollout", 2)
    assert [n for n, _ in resized(core)] == ["api-1-a", "api-1-b"]  # not api-x-a, another's
    actuator.event(core, done)
    on = core.create_namespaced_event.call_args.args[1].involved_object
    assert (on.api_version, on.kind, on.name) == ("argoproj.io/v1alpha1", "Rollout", "api")
    gone, _ = start(*rolled(), of("Deployment"))  # a Deployment of its name: none
    assert (gone.outcome, gone.reason) == ("skipped", "no such Deployment")


def test_the_webhook_tells_an_argo_rollouts_pod_by_its_hash_label() -> None:
    review = new_pod()
    labels = review["request"]["object"]["metadata"]["labels"]
    del labels["pod-template-hash"]
    labels["rollouts-pod-template-hash"] = "5d8f"
    held = {"shop/Rollout/api/app": HELD["shop/Deployment/api/app"]}
    assert patched(review, held) == patched(new_pod()) is not None  # as a Deployment's
    assert patched(review) is None  # a Deployment of its name holds nothing for it
    assert patched(new_pod(), held) is None  # nor it for a Deployment's pod
    labels["rollouts-pod-template-hash"] = "9999"  # not its ReplicaSet's hash
    assert patched(review, held) is None
    db = owned_by("StatefulSet", "db")  # a StatefulSet's pod, whatever its labels say
    db["request"]["object"]["metadata"]["labels"]["rollouts-pod-template-hash"] = "5d8f"
    assert patched(db, {"shop/StatefulSet/db/app": HELD["shop/Deployment/api/app"]})


def test_a_rollout_is_read_as_a_deployment_its_template_its_own_or_its_workload_refs() -> None:
    custom, apps = MagicMock(), MagicMock()
    template = {"metadata": {"labels": {"app": "web"}}, "spec": {"containers": [
        {"name": "app", "image": "x", "resources": {"requests": {"cpu": "100m"}},
         "resizePolicy": [{"resourceName": "cpu", "restartPolicy": "NotRequired"}]}]}}  # fmt: skip
    meta = {
        "name": "web",
        "namespace": "shop",
        "uid": "u9",
        "annotations": {actuator.PAUSE: "true"},
    }
    own = {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Rollout",
        "metadata": meta,
        "spec": {
            "replicas": 3,
            "selector": {"matchLabels": {"app": "web"}},
            "strategy": {"canary": {"steps": [{"setWeight": 20}]}},  # not a Deployment's
            "template": template,
        },
        "status": {"phase": "Healthy", "conditions": [{"type": "Progressing", "status": "True"}]},
    }
    custom.get_namespaced_custom_object.return_value = own
    d = actuator.Apps(apps, custom).read_namespaced_rollout("web", "shop")
    assert custom.get_namespaced_custom_object.call_args.args == (
        "argoproj.io",
        "v1alpha1",
        "shop",
        "rollouts",
        "web",
    )
    c = d.spec.template.spec.containers[0]
    assert (d.metadata.uid, d.spec.replicas, d.spec.selector.match_labels) == (
        "u9",
        3,
        {"app": "web"},
    )
    assert (c.resources.requests, c.resize_policy[0].restart_policy) == (
        {"cpu": "100m"},
        "NotRequired",
    )
    base = k.V1Deployment(
        spec=k.V1DeploymentSpec(
            selector=k.V1LabelSelector(match_labels={"app": "base"}),
            template=k.V1PodTemplateSpec(spec=k.V1PodSpec(containers=[container({"cpu": "7m"})])),
        )
    )
    apps.read_namespaced_deployment.return_value = base
    ref = {"apiVersion": "apps/v1", "kind": "Deployment", "name": "web-base"}
    reffed = {"metadata": meta, "spec": {"replicas": 2, "workloadRef": ref}}
    custom.get_namespaced_custom_object.return_value = reffed
    d = actuator.Apps(apps, custom).read_namespaced_rollout("web", "shop")
    assert apps.read_namespaced_deployment.call_args.args == ("web-base", "shop")
    assert d.spec.template.spec.containers[0].resources.requests == {"cpu": "7m"}
    assert d.spec.selector.match_labels == {"app": "base"}
    reffed["spec"]["selector"] = {"matchLabels": {"app": "web"}}  # as Argo's examples give it
    d = actuator.Apps(apps, custom).read_namespaced_rollout("web", "shop")
    assert d.spec.selector.match_labels == {"app": "web"}  # its own, not the Deployment's
    assert (
        actuator.Apps(apps, custom).list_namespaced_replica_set is apps.list_namespaced_replica_set
    )


def test_a_rollout_it_cant_read_as_a_deployment_fails_and_never_hides_anothers_pause() -> None:
    custom, apps = MagicMock(), MagicMock()
    meta = {"name": "web", "namespace": "shop", "uid": "u9"}
    other = {"apiVersion": "v1", "kind": "PodTemplate", "name": "web-template"}
    custom.get_namespaced_custom_object.return_value = {
        "metadata": meta,
        "spec": {"selector": {"matchLabels": {"app": "web"}}, "workloadRef": other},
    }
    core, _ = cluster()
    a, _ = start(core, actuator.Apps(apps, custom), of("Rollout"))
    assert a.outcome == "failed"
    assert a.reason.startswith("reading the Rollout: 422: not read as a Deployment")
    paused = {"name": "db", "namespace": "shop", "annotations": {actuator.PAUSE: "true"}}
    custom.list_namespaced_custom_object.return_value = {
        "items": [custom.get_namespaced_custom_object.return_value, {"metadata": paused}]
    }
    act = make(hub_of([], []), core, actuator.Apps(apps, custom))
    act.pauses({"shop/Rollout/web/app", "shop/Rollout/db/app"})
    assert act.paused == {"shop/Rollout/db"} and not apps.read_namespaced_deployment.called


# --- CronJobs, through the webhook only (slice 12.8c) -------------------------------------

CRON = "shop/CronJob/api/app"


def cronned(parallelism: int | None = None) -> tuple[Any, Any]:
    """cluster(), api a CronJob (uid u1) whose Jobs' template is cluster()'s Deployment's;
    its Jobs from list_namespaced_job, their pods by `batch.kubernetes.io/job-name`."""
    core, apps = cluster()
    d = apps.read_namespaced_deployment.return_value
    apps.read_namespaced_cron_job.return_value = k.V1CronJob(
        metadata=d.metadata,
        spec=k.V1CronJobSpec(
            schedule="* * * * *",
            job_template=k.V1JobTemplateSpec(
                spec=k.V1JobSpec(template=d.spec.template, parallelism=parallelism)
            ),
        ),
    )
    apps.read_namespaced_deployment.side_effect = ApiException(status=404)
    apps.list_namespaced_job.return_value = k.V1JobList(items=[])
    runs: dict[str, list[Any]] = {}
    core.list_namespaced_pod.side_effect = lambda ns, label_selector: k.V1PodList(
        items=runs.get(label_selector.removeprefix("batch.kubernetes.io/job-name="), [])
    )
    core.runs = runs  # each Job's pods, by its name
    return core, apps


def job(
    core: Any, apps: Any, name: str, at: float, *pods: Any, ended: str = "", uid: str = "u1"
) -> k.V1Job:
    """A Job of api created at `at` (its CronJob's `uid`), with these pods; `ended`: Complete
    or Failed."""
    j = k.V1Job(
        metadata=k.V1ObjectMeta(
            name=name,
            creation_timestamp=datetime.fromtimestamp(at, UTC),
            owner_references=[owner("CronJob", "api", uid)],
        ),
        status=k.V1JobStatus(
            conditions=[k.V1JobCondition(type=ended, status="True")] if ended else None
        ),
    )
    apps.list_namespaced_job.return_value.items.append(j)
    core.runs[name] = list(pods)
    return j


def ran(name: str, phase: str = "Running", held: bool = True, oom: str = "") -> k.V1Pod:
    """A run's pod with api's held requests (or its template's); `oom`: state or last_state."""
    p = pod(name, "", phase, **({"cpu": "115m", "memory": "96Mi"} if held else {}))
    st = status("app", None)
    if oom:
        setattr(st, oom, k.V1ContainerState(terminated=k.V1ContainerStateTerminated(
            exit_code=137, reason="OOMKilled")))  # fmt: skip
    p.status.container_statuses = [st]
    return p


def test_a_cronjob_is_held_for_its_next_run_never_resized_in_place() -> None:
    core, apps = cronned()
    a, held = start(core, apps, of("CronJob"), at=100.0)
    assert (a.outcome, a.pods, a.reason) == (
        "applied",
        0,
        "held for its next run's pods, given them as they start",
    )
    assert held["requests"] == {"cpu": "115m", "memory": "96Mi"}
    assert held["first_run"] == {"after": 100.0, "until": 100.0 + 7 * 86400}
    assert not core.patch_namespaced_pod_resize.called
    actuator.event(core, a)
    on = core.create_namespaced_event.call_args.args[1].involved_object
    assert (on.api_version, on.kind, on.name) == ("batch/v1", "CronJob", "api")
    core.list_namespaced_resource_quota.return_value = quota({"cpu": "3"}, {"cpu": "2"})
    one, _ = start(core, apps, of("CronJob", cpu=2.0, memory=None))
    assert one.outcome == "applied"  # 2 + 1 x 1 fits
    core, apps = cronned(parallelism=2)
    core.list_namespaced_resource_quota.return_value = quota({"cpu": "3"}, {"cpu": "2"})
    two, _ = start(core, apps, of("CronJob", cpu=2.0, memory=None))
    assert two.reason == "2 pods at cpu 2000m would pass ResourceQuota team's cpu 3"
    restarts, _ = start(*cronned(), of("CronJob"))  # the pre-checks hold: its resizePolicy
    assert restarts.outcome == "applied"
    core, apps = cronned()
    template = apps.read_namespaced_cron_job.return_value.spec.job_template.spec.template
    template.spec.containers[0].resize_policy = [
        k.V1ContainerResizePolicy(resource_name="cpu", restart_policy="RestartContainer")
    ]
    skipped, _ = start(core, apps, of("CronJob"))
    assert skipped.reason == "its resizePolicy restarts the container for ['cpu']"


def run_of(cronjob: str, ago: int = 0) -> str:
    """A Job as the CronJob controller names it: for its scheduled minute, `ago` minutes back."""
    return f"{cronjob}-{int(time.time() // 60) - ago}"


def test_the_webhook_gives_a_cronjobs_run_its_hold_and_a_lone_job_nothing() -> None:
    held = {CRON: HELD["shop/Deployment/api/app"]}
    expected = patched(new_pod())
    assert expected is not None
    assert patched(owned_by("Job", run_of("api")), held) == expected
    assert patched(owned_by("Job", "api-manual"), held) is None  # no CronJob's name
    assert patched(owned_by("Job", "api-123"), held) is None  # digits, no recent minute
    assert actuator.RUN_AGE == 30 * 1440  # a month of minutes
    assert patched(owned_by("Job", run_of("api", ago=actuator.RUN_AGE - 1)), held) == expected
    assert patched(owned_by("Job", run_of("api", ago=actuator.RUN_AGE + 2)), held) is None
    assert patched(owned_by("Job", run_of("api", ago=-4)), held) == expected  # a clock ahead
    assert patched(owned_by("Job", run_of("api", ago=-7)), held) is None
    assert patched(owned_by("Job", run_of("other")), held) is None  # another's: not held
    assert patched(owned_by("Job", run_of("api"))) is None  # a Deployment api's hold
    req = owned_by("Job", run_of("api"))["request"]
    assert actuator.mutation(req, held, "x", frozenset({"shop/CronJob/api"})) == []


def test_a_cronjobs_first_run_under_its_hold_is_watched_until_it_ends() -> None:
    core, apps = cronned()
    t0 = time.time() - 600
    _, held = start(core, apps, of("CronJob"), at=t0)
    act = make(hub_of([], []), core, apps, held={CRON: held})
    job(core, apps, "api-1", t0 - 60, ran("api-1-a", "Failed"))  # before the hold
    job(core, apps, "api-2", t0 + 60, ran("api-2-a", held=False))  # from its template
    assert act.watch() == [] and "job" not in act.held[CRON]["first_run"]
    job(core, apps, "api-x", t0 + 90, ran("api-x-a", "Failed"), uid="u2")  # another CronJob's
    j = job(core, apps, "api-3", t0 + 120, ran("api-3-a"))
    job(core, apps, "api-4", t0 + 180, ran("api-4-a"))  # a later run
    apps.list_namespaced_job.return_value.items.reverse()  # listed in no particular order
    assert act.watch() == [] and act.held[CRON]["first_run"]["job"] == "api-3"
    j.status.conditions = [k.V1JobCondition(type="Complete", status="True")]
    assert act.watch() == [] and "first_run" not in act.held[CRON]  # over: kept, unwatched
    assert act.held[CRON]["outcome"] == "applied"
    held["first_run"] = {"after": t0, "until": t0 + 7 * 86400, "job": "api-3"}
    apps.list_namespaced_job.return_value.items.remove(j)  # its history limit took it
    assert act.watch() == [] and "first_run" not in act.held[CRON]
    held["first_run"] = {"after": t0 + 1000, "until": t0 + 7 * 86400}  # none since
    assert act.watch() == [] and "first_run" in act.held[CRON]
    held["first_run"]["until"] = t0  # 7 days without a run
    assert act.watch() == [] and "first_run" not in act.held[CRON]


@pytest.mark.parametrize(
    ("bad", "why_"),
    [
        (ran("api-3-a", "Failed"), "api-3-a Failed"),
        (ran("api-3-a", "Failed", oom="state"), "api-3-a: app was OOM-killed"),  # Never
        (ran("api-3-a", oom="last_state"), "api-3-a: app was OOM-killed"),  # OnFailure
    ],
)
def test_a_failed_or_oom_killed_pod_of_that_run_rolls_the_hold_back(bad: Any, why_: str) -> None:
    core, apps = cronned()
    t0 = time.time() - 600
    _, held = start(core, apps, of("CronJob"), at=t0)
    act = make(hub_of([], []), core, apps, held={CRON: held})
    job(core, apps, "api-3", t0 + 60, ran("api-3-ok", "Succeeded"), bad, ended="Failed")
    (a,) = act.watch()
    assert (a.outcome, a.kind, a.pods) == ("rolled_back", "CronJob", 0)
    assert a.reason.startswith(
        f"{why_}: its next run's pods start from its template, not cpu 115m, memory 96Mi; "
        "no new Sizing of it until"
    )
    h = act.held[CRON]
    assert h["outcome"] == "rolled_back" and "first_run" not in h and h["backoff_until"]
    assert not core.patch_namespaced_pod_resize.called  # nothing in place
    assert patched(owned_by("Job", run_of("api")), act.held) is None  # the webhook stops
    core.runs["api-3"] = [ran("api-3-ok"), ran("api-3-b", "Failed", held=False)]  # a template's
    held2 = start(core, apps, of("CronJob"), at=t0)[1]
    act = make(hub_of([], []), core, apps, held={CRON: held2})
    assert act.watch() == []  # not the Actuator's doing


def test_a_held_cronjob_steps_through_hand_back_pause_revert_and_catch_up() -> None:
    core, apps = cronned()
    seen: list[httpx.Request] = []
    sizing = {**wanted().model_dump(), "kind": "CronJob"}
    hub = hub_of([{"kill_switch": False, "sizings": [sizing]}] * 3, seen)
    act = make(hub, core, apps)
    act.step()  # applied at once, held for its next run
    assert act.held[CRON]["outcome"] == "applied" and "first_run" in act.held[CRON]
    stale = ran("api-9-a", held=False)  # a run from its template while the Kill switch was on
    job(core, apps, "api-9", time.time() + 1, stale)
    core.replace_namespaced_config_map.reset_mock()
    act.step()  # no catch-up: its pods are never resized in place
    assert not core.patch_namespaced_pod_resize.called
    assert core.replace_namespaced_config_map.called  # saved while its first run is watched
    apps.list_namespaced_cron_job.return_value = MagicMock(
        items=[MagicMock(metadata=k.V1ObjectMeta(name="api", annotations={actuator.PAUSE: "true"}))]
    )
    act.step()
    assert act.is_paused(CRON)
    a = actuator.put_back(core, apps, CRON, dict(act.held[CRON]), "reverted", "reverted (1)", 0.0)
    assert (a.outcome, a.pods) == ("reverted", 0)
    assert (
        a.reason
        == "reverted (1): its next run's pods start from its template, not cpu 115m, memory 96Mi"
    )
    template = apps.read_namespaced_cron_job.return_value.spec.job_template.spec.template
    template.spec.containers[0].resources.requests = {"cpu": "2"}
    assert act.changed(CRON, act.held[CRON]) == "its template's requests changed: cpu 1 to 2"
    (back,) = act.hand_back()
    assert back.outcome == "handed_back" and "first_run" not in act.held[CRON]
    posted = [json.loads(r.content) for r in seen if r.method == "POST"]
    assert [(x["kind"], x["outcome"]) for p in posted for x in p] == [("CronJob", "applied")]


def test_automation_of_a_cronjob_keeps_the_churn_rule() -> None:
    core, apps = cronned()
    scaling = scalers()
    _, held = actuator.apply(core, apps, of("CronJob"), 5.0, *scaling, {}, True)
    assert held["lowered_at"] == 5.0  # cpu 1 down to 115m: once a day at most
    near = of("CronJob", cpu=0.95)  # 5% under its template's 1: too small to move
    _, held = actuator.apply(core, apps, near, 5.0, *scaling, {}, True)
    assert held["deferred"] == ["cpu"] and held["requests"] == {"memory": "96Mi"}


def test_apps_reads_cronjobs_and_their_jobs_from_the_batch_api() -> None:
    batch = MagicMock()
    apps = actuator.Apps(MagicMock(), MagicMock(), batch)
    assert apps.read_namespaced_cron_job("api", "shop") is batch.read_namespaced_cron_job()
    assert batch.read_namespaced_cron_job.call_args_list[0].args == ("api", "shop")
    assert apps.list_namespaced_cron_job("shop") is batch.list_namespaced_cron_job.return_value
    assert apps.list_namespaced_job("shop") is batch.list_namespaced_job.return_value
    assert batch.list_namespaced_job.call_args.args == ("shop",)


# --- what the audit found (slice 12.9a) ------------------------------------------------------


def settled(monkeypatch: pytest.MonkeyPatch, answers: list[Any], core: Any, apps: Any) -> Any:
    """An Actuator that applied run 1 to api's two pods; `answers`: the Hub's, after."""
    monkeypatch.setattr(actuator, "CANARY_SECONDS", 0)
    seen: list[httpx.Request] = []
    a = make(hub_of([desired(1)] * 3 + answers, seen), core, apps)
    for _ in range(3):
        a.step()
    assert a.held[KEY]["outcome"] == "applied"
    a.seen = seen
    return a


def restarted(core: Any, name: str, count: int) -> None:
    p = next(p for p in core.list_namespaced_pod.return_value.items if p.metadata.name == name)
    st = status("app", None)
    st.restart_count = count
    p.status.container_statuses = [st]


def test_a_catch_up_counts_restarts_from_now_not_from_ever(monkeypatch: pytest.MonkeyPatch) -> None:
    core, apps = cluster()
    a = settled(monkeypatch, [desired(1)], core, apps)
    for watched in actuator.WATCHED:  # its hour is over
        a.held[KEY].pop(watched, None)
    restarted(core, "api-1-a", 2)  # long ago, under the requests it still has
    late = pod("api-1-c", "api-1")  # started from its template: a catch-up
    late.metadata.namespace = "shop"
    core.list_namespaced_pod.return_value.items.append(late)
    a.step()
    h = a.held[KEY]
    assert h["outcome"] == "rolling" and h["pods"]["api-1-a"]["restarts"] == 2
    assert set(h["pods"]) == {"api-1-a", "api-1-b", "api-1-c"}
    assert actuator.trouble(core, apps, KEY, h, time.time()) is None
    restarted(core, "api-1-a", 3)  # a new one is news
    assert actuator.trouble(core, apps, KEY, h, time.time()) == "api-1-a: app restarted"


def test_a_catch_up_keeps_the_count_of_a_pod_it_still_watches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core, apps = cluster()
    a = settled(monkeypatch, [], core, apps)
    assert a.held[KEY]["pods"]["api-1-a"]["restarts"] == 0  # within its hour
    restarted(core, "api-1-a", 1)
    late = pod("api-1-c", "api-1")
    late.metadata.namespace = "shop"
    core.list_namespaced_pod.return_value.items.append(late)
    a.catch_up()
    assert a.held[KEY]["pods"]["api-1-a"]["restarts"] == 0  # not forgiven by the catch-up


def refusing(core: Any, *names: str, status_: int = 503) -> None:
    """Resizes of these pods refused from now on."""
    patch = core.patch_namespaced_pod_resize.side_effect

    def refuse(name: str, ns: str, body: Any) -> None:
        if name in names:
            raise ApiException(status=status_, reason="busy")
        patch(name, ns, body)

    core.patch_namespaced_pod_resize.side_effect = refuse
    core.patch = patch


def test_a_put_back_a_pod_refused_is_finished_later(monkeypatch: pytest.MonkeyPatch) -> None:
    core, apps = cluster()
    a = settled(monkeypatch, [httpx.Response(503)] * 3, core, apps)
    refusing(core, "api-1-b")
    h = a.held[KEY]
    back = actuator.rollback(core, apps, KEY, h, "a test", time.time())
    assert back.pods == 1 and back.reason.count("; 1 refused, first api-1-b: 503: busy; tried")
    assert "tried again each minute" in back.reason and h["unfinished"] is True
    before = len(resized(core))
    a.step()  # the Hub down: still refused, still to finish
    assert h["unfinished"] and len(resized(core)) == before + 1
    core.patch_namespaced_pod_resize.side_effect = core.patch
    a.killed = True
    a.step()  # the Hub down, the Kill switch on as far as it knows: a resize is a change
    assert h["unfinished"] and len(resized(core)) == before + 1
    a.killed = False
    core.replace_namespaced_config_map.reset_mock()
    a.step()  # the Hub still down: finished all the same
    assert "unfinished" not in h and resized(core)[-1] == ("api-1-b", body(cpu="1"))
    assert h["outcome"] == "rolled_back" and core.replace_namespaced_config_map.called
    assert [json.loads(r.content) for r in a.seen if r.method == "POST"][1:] == []  # said once


def test_a_put_back_is_not_finished_on_a_template_a_person_changed_or_a_workload_gone() -> None:
    def left(change: Any) -> tuple[Any, Any]:
        core, apps = cluster()
        _, held = start(core, apps, wanted())
        for at in (600.0, 660.0):
            actuator.advance(core, apps, KEY, held, at)
        refusing(core, "api-1-a", "api-1-b")
        actuator.put_back(core, apps, KEY, held, "reverted", "x", 0.0)
        assert held["unfinished"]
        core.patch_namespaced_pod_resize.side_effect = core.patch
        core.patch_namespaced_pod_resize.reset_mock()
        change(apps)
        actuator.finish(core, apps, KEY, held)
        return core, held

    def theirs(apps: Any) -> None:
        template = apps.read_namespaced_deployment.return_value.spec.template
        template.spec.containers[0].resources.requests = {"cpu": "2"}

    def gone(apps: Any) -> None:
        apps.read_namespaced_deployment.side_effect = ApiException(status=404)

    def down(apps: Any) -> None:
        apps.read_namespaced_deployment.side_effect = ApiException(status=500)

    core, held = left(lambda apps: None)
    assert "unfinished" not in held and [n for n, _ in resized(core)] == ["api-1-a", "api-1-b"]
    for change in (theirs, gone):
        core, held = left(change)
        assert "unfinished" not in held and resized(core) == []
    core, held = left(down)
    assert held["unfinished"] and resized(core) == []
    core, apps = cluster(requests={})  # its template sets none: nothing to put back
    _, held = start(core, apps, wanted())
    core.list_namespaced_pod.side_effect = ApiException(status=500)  # its pods unlisted
    a = actuator.put_back(core, apps, KEY, held, "reverted", "x", 0.0)
    assert "1 refused, first listing its pods" in a.reason and "unfinished" not in held


def test_an_approval_in_its_backoff_keeps_a_put_back_still_to_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core, apps = cluster()
    a = settled(monkeypatch, [desired(1, 2)], core, apps)
    refusing(core, "api-1-b")
    actuator.rollback(core, apps, KEY, a.held[KEY], "a test", time.time())
    a.step()  # run 2: skipped, in its backoff
    h = a.held[KEY]
    assert (h["run_id"], h["outcome"], h["unfinished"]) == (2, "skipped", True)
    assert h["requests"] == {"cpu": "115m", "memory": "96Mi"} and h["template"]["cpu"] == "1"
    core.patch_namespaced_pod_resize.side_effect = core.patch
    actuator.finish(core, apps, KEY, h)
    assert "unfinished" not in h and resized(core)[-1] == ("api-1-b", body(cpu="1"))


def test_a_rollout_waits_out_a_passing_failure_ten_minutes_at_most() -> None:
    core, apps = cluster()
    _, held = start(core, apps, wanted())
    listing = core.list_namespaced_pod.return_value
    core.list_namespaced_pod.side_effect = ApiException(status=503, reason="busy")
    assert actuator.advance(core, apps, KEY, held, 600.0) is None
    assert (held["outcome"], held["stalled"]) == ("rolling", 600.0)
    assert actuator.advance(core, apps, KEY, held, 1199.0) is None
    core.list_namespaced_pod.side_effect = None
    core.list_namespaced_pod.return_value = listing
    assert actuator.advance(core, apps, KEY, held, 1200.0) is None  # on: the next pod
    assert "stalled" not in held and [n for n, _ in resized(core)] == ["api-1-a", "api-1-b"]
    _, held = start(*cluster(), wanted())
    core, apps = cluster()
    _, held = start(core, apps, wanted())
    core.list_namespaced_pod.side_effect = ApiException(status=0, reason="ProtocolError")
    assert actuator.advance(core, apps, KEY, held, 600.0) is None
    failed = actuator.advance(core, apps, KEY, held, 1200.0)  # ten minutes of it
    assert failed.outcome == "failed" and "stalled" not in held
    assert failed.reason == "resizing its next pod: 0: ProtocolError"
    core, apps = cluster()
    _, held = start(core, apps, wanted())
    core.list_namespaced_pod.side_effect = ApiException(status=403, reason="no")
    assert actuator.advance(core, apps, KEY, held, 600.0).outcome == "failed"  # a refusal
    for status_, verdict in ((None, True), (0, True), (429, True), (500, True), (404, False)):
        assert actuator.passing(ApiException(status=status_)) is verdict


def test_a_refused_approval_keeps_what_the_one_before_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core, apps = cluster()
    a = settled(monkeypatch, [desired(1, 2)] * 2 + [desired(1, 2, 3)], core, apps)
    core.list_namespaced_limit_range.return_value = limit_range(min={"cpu": "3"})
    a.step()  # run 2: skipped by its LimitRange
    h = a.held[KEY]
    assert (h["run_id"], h["outcome"], h["refused"]) == (1, "applied", 2)
    assert h["requests"] == {"cpu": "115m", "memory": "96Mi"}
    a.step()  # run 2 again: said once
    posted = [x for r in a.seen if r.method == "POST" for x in json.loads(r.content)]
    assert [(x["run_id"], x["outcome"]) for x in posted] == [(1, "applied"), (2, "skipped")]
    assert posted[1]["reason"] == (
        "cpu 115m is under its LimitRange's minimum 3; run 1's requests stay held"
    )
    core.list_namespaced_limit_range.return_value = k.V1LimitRangeList(items=[])
    late = pod("api-1-c", "api-1")
    late.metadata.namespace = "shop"
    core.list_namespaced_pod.return_value.items.append(late)
    a.step()  # run 3 applies, and the mark goes
    assert a.held[KEY]["run_id"] == 3 and "refused" not in a.held[KEY]
    fresh = make(hub_of([desired(1)], []), *cluster())
    fresh.core.list_namespaced_limit_range.return_value = limit_range(min={"cpu": "3"})
    fresh.step()  # nothing held before: the skip is what it holds, as ever
    assert fresh.held[KEY]["outcome"] == "skipped" and "refused" not in fresh.held[KEY]


def test_a_network_failure_is_an_api_exception_and_no_call_waits_forever() -> None:
    import urllib3

    api = MagicMock()
    calm = actuator.Calm(api)
    assert calm.read_namespaced_pod("a", "shop") is api.read_namespaced_pod.return_value
    assert api.read_namespaced_pod.call_args.kwargs == {"_request_timeout": actuator.API_TIMEOUT}
    calm.read_namespaced_pod("a", "shop", _request_timeout=5)
    assert api.read_namespaced_pod.call_args.kwargs == {"_request_timeout": 5}
    api.list_namespaced_pod.side_effect = urllib3.exceptions.ProtocolError("reset")
    with pytest.raises(ApiException) as e:
        calm.list_namespaced_pod("shop")
    assert (e.value.status, e.value.reason) == (0, "ProtocolError")
    api.list_namespaced_pod.side_effect = ApiException(status=403)
    with pytest.raises(ApiException) as e:
        calm.list_namespaced_pod("shop")
    assert e.value.status == 403
    api.list_namespaced_pod.side_effect = KeyError("a bug")
    with pytest.raises(KeyError):
        calm.list_namespaced_pod("shop")


def test_a_minute_that_fails_keeps_what_it_changed_before() -> None:
    core, apps = cluster()
    seen: list[httpx.Request] = []
    a = make(hub_of([desired(1), desired(1)], seen), core, apps)
    a.held[KEY] = {"run_id": 0, "outcome": "applied", "requests": {"cpu": "2"}, "template": {}}
    a.catch_up = MagicMock(side_effect=RuntimeError("a bug"))  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        a.step()  # handed back (no template kept), then run 1's canary, then the bug
    saved = json.loads(core.replace_namespaced_config_map.call_args.args[2].data[actuator.KEY])
    assert saved[KEY]["outcome"] == "rolling" and saved[KEY]["pod"] == "api-1-a"
    said = [x["outcome"] for r in seen if r.method == "POST" for x in json.loads(r.content)]
    assert said == ["handed_back"] and a.unsent == []  # told in the minute that failed
    a.catch_up = MagicMock(return_value=[])  # type: ignore[method-assign]
    a.step()
    assert [n for n, _ in resized(core)] == ["api-1-a"]  # not done twice
    assert len([r for r in seen if r.method == "POST"]) == 1


def test_finishing_a_put_back_goes_pod_by_pod_and_gives_up_on_a_verdict() -> None:
    def left(*names: str, status_: int = 503) -> tuple[Any, Any, Any]:
        core, apps = cluster()
        _, held = start(core, apps, wanted())
        for at in (600.0, 660.0):
            actuator.advance(core, apps, KEY, held, at)
        refusing(core, *names, status_=status_)
        actuator.put_back(core, apps, KEY, held, "reverted", "x", 0.0)
        core.patch_namespaced_pod_resize.reset_mock()
        actuator.finish(core, apps, KEY, held)
        return core, apps, held

    core, apps, held = left("api-1-a")  # the first listed refuses, again
    assert held["unfinished"] and [n for n, _ in resized(core)] == ["api-1-a"]
    core, apps, held = left("api-1-a", "api-1-b")
    core.patch_namespaced_pod_resize.side_effect = core.patch
    refusing(core, "api-1-a")  # b no longer does: not kept waiting behind a
    core.patch_namespaced_pod_resize.reset_mock()
    actuator.finish(core, apps, KEY, held)
    b = next(p for p in core.list_namespaced_pod.return_value.items if p.metadata.name == "api-1-b")
    assert held["unfinished"] and b.spec.containers[0].resources.requests["cpu"] == "1"
    for verdict in (403, 404):  # forbidden for good, or the pod gone meanwhile
        core, apps, held = left("api-1-a", status_=verdict)
        assert "unfinished" not in held


def test_a_put_back_to_finish_outlives_a_skipped_sizing_and_a_failed_hold_a_refused_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core, apps = cluster()
    a = settled(monkeypatch, [desired(1, 2)], core, apps)
    refusing(core, "api-1-b")
    actuator.put_back(core, apps, KEY, a.held[KEY], "reverted", "x", 0.0)
    core.list_namespaced_limit_range.return_value = limit_range(min={"cpu": "3"})
    a.step()  # run 2 skipped, the minute after a Revert: no backoff
    h = a.held[KEY]
    assert (h["run_id"], h["outcome"], h["unfinished"]) == (2, "skipped", True)
    assert h["requests"] == {"cpu": "115m", "memory": "96Mi"} and h["template"]["cpu"] == "1"
    core, apps = cluster()
    half = {"kill_switch": False, "sizings": [wanted(2, cpu=0.5).model_dump()]}
    a = settled(monkeypatch, [half], core, apps)
    actuator.put_back(core, apps, KEY, a.held[KEY], "reverted", "x", 0.0)  # none refused
    core.list_namespaced_limit_range.return_value = limit_range(min={"cpu": "3"})
    a.step()  # run 2 skipped: nothing of run 1's hold is left to undo
    assert a.held[KEY]["requests"]["cpu"] == "500m" and "unfinished" not in a.held[KEY]
    core, apps = cluster()
    a = settled(monkeypatch, [{"kill_switch": False, "sizings": [wanted(2, cpu=0.5).model_dump()]}],
                core, apps)  # fmt: skip
    refusing(core, "api-1-b")
    actuator.put_back(core, apps, KEY, a.held[KEY], "reverted", "x", 0.0)
    a.step()  # b still refuses; run 2 rolls on a: its requests now, the old undo moot
    h = a.held[KEY]
    assert (h["outcome"], h["requests"]["cpu"]) == ("rolling", "500m") and "unfinished" not in h
    core, apps = cluster()
    failed = {"run_id": 1, "outcome": "failed", "requests": {"cpu": "115m"}, "at": 0.0}
    failed["template"] = {"cpu": "1"}
    a = make(hub_of([desired(2)], []), core, apps, held={KEY: failed})
    core.list_namespaced_limit_range.return_value = limit_range(min={"cpu": "3"})
    a.step()  # its pods may have run 1's requests: still a Revert's to undo
    assert (a.held[KEY]["run_id"], a.held[KEY]["outcome"], a.held[KEY]["refused"]) == (
        1,
        "failed",
        2,
    )


def test_a_pod_that_already_had_the_requests_is_never_notready_trouble(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core, apps = cluster()
    a = settled(monkeypatch, [], core, apps)
    for watched in actuator.WATCHED:
        a.held[KEY].pop(watched, None)
    late = pod("api-1-c", "api-1")
    late.metadata.namespace = "shop"
    core.list_namespaced_pod.return_value.items.append(late)
    a.catch_up()
    h, now = a.held[KEY], time.time()
    assert "at" not in h["pods"]["api-1-a"] and h["pods"]["api-1-c"]["at"]

    def unready(name: str) -> None:
        p = next(p for p in core.list_namespaced_pod.return_value.items if p.metadata.name == name)
        since = datetime.fromtimestamp(now + 10, UTC)
        p.status.conditions = [
            k.V1PodCondition(type="Ready", status="False", last_transition_time=since)
        ]

    unready("api-1-a")  # not resized by this rollout: its own trouble
    assert actuator.trouble(core, apps, KEY, h, now + 300) is None
    unready("api-1-c")  # the one it resized
    assert actuator.trouble(core, apps, KEY, h, now + 300) == "api-1-c: NotReady for 2 minutes"


def test_a_bug_every_minute_still_reports_each_minutes_rollback() -> None:
    core, apps = cluster()
    seen: list[httpx.Request] = []
    a = make(hub_of([desired(1)] * 2, seen), core, apps)
    a.held[KEY] = {"run_id": 0, "outcome": "applied", "requests": {"cpu": "2"}, "template": {}}
    a.act = MagicMock(side_effect=RuntimeError("a bug"))  # type: ignore[method-assign]
    for _ in range(2):
        with pytest.raises(RuntimeError):
            a.step()
    said = [x["outcome"] for r in seen if r.method == "POST" for x in json.loads(r.content)]
    assert said == ["handed_back"]  # the first minute's, told then; the second had none


# --- the audit's narrower findings (slice 12.9b) ---------------------------------------------


def test_the_kill_switch_and_unsent_reports_outlive_a_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core, apps = cluster()
    down = [httpx.Response(503)]
    a = settled(monkeypatch, [desired(1, killed=True), *down], core, apps)
    for watched in actuator.WATCHED:  # its hour over: nothing else to save
        a.held[KEY].pop(watched, None)
    count = core.replace_namespaced_config_map.call_count
    a.step()  # the Kill switch on: nothing else to save, saved for that
    assert a.killed and core.replace_namespaced_config_map.call_count == count + 1
    report = actuator.rollback(core, apps, KEY, a.held[KEY], "a test", time.time())
    a.unsent.append(report)  # the Hub down when it happened
    a.killed = True
    a.save()
    data = core.replace_namespaced_config_map.call_args.args[2].data
    core.read_namespaced_config_map.return_value = k.V1ConfigMap(data=data)
    seen: list[httpx.Request] = []
    b = make(hub_of([*down, desired(1, killed=True)], seen), core, apps)
    b.load()
    assert b.killed and [x.outcome for x in b.unsent] == ["rolled_back"]
    assert b.held[KEY]["outcome"] == "rolled_back"
    held = {KEY: HELD[KEY]}
    assert actuator.admit(new_pod(), held, b.killed, "x")["response"].get("patch") is None
    core.patch_namespaced_pod_resize.reset_mock()
    b.step()  # the Hub down at its start: still under the Kill switch
    assert b.killed and resized(core) == []
    b.step()  # the Hub back: the report it never took
    posted = [x for r in seen if r.method == "POST" for x in json.loads(r.content)]
    assert [x["outcome"] for x in posted] == ["rolled_back"]
    data = core.replace_namespaced_config_map.call_args.args[2].data
    assert json.loads(data[actuator.STATE]) == {"killed": True, "unsent": []}
    core.read_namespaced_config_map.return_value = k.V1ConfigMap(data={actuator.KEY: "{}"})
    old = make(hub_of([], []), core, apps)
    old.load()  # a ConfigMap from before 12.9b
    assert not old.killed and old.unsent == []
    good = report.model_dump()
    newer = {**good, "outcome": "an outcome of a later version"}
    for state, killed, kept in (
        ({"killed": True, "unsent": [newer, good, "x"]}, True, 1),  # what it can't read: dropped
        ("not json", False, 0),
        (["a list"], False, 0),
    ):
        text = state if isinstance(state, str) else json.dumps(state)
        data = {actuator.KEY: json.dumps({KEY: HELD[KEY]}), actuator.STATE: text}
        core.read_namespaced_config_map.return_value = k.V1ConfigMap(data=data)
        other = make(hub_of([], []), core, apps)
        other.load()  # never its holds with it
        assert other.held == {KEY: HELD[KEY]}
        assert (other.killed, len(other.unsent)) == (killed, kept)
    many = make(hub_of([], []), core, apps)
    many.unsent = [report.model_copy(update={"at": float(i)}) for i in range(250)]
    kept_ = json.loads(many.state())["unsent"]
    assert actuator.KEPT_REPORTS == 200 and [x["at"] for x in kept_] == [
        float(i) for i in range(50, 250)
    ]  # the newest: its holds come first in the ConfigMap's megabyte


def test_a_passing_failure_doesnt_use_an_approval_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(actuator, "CANARY_SECONDS", 0)
    core, apps = cluster()
    seen: list[httpx.Request] = []
    a = make(hub_of([desired(1)] * 4, seen), core, apps)
    read = apps.read_namespaced_deployment
    ok, read.side_effect = read.return_value, ApiException(status=503, reason="busy")
    a.step()
    assert a.held[KEY]["trying"]["run"] == 1 and "outcome" not in a.held[KEY]
    since = a.held[KEY]["trying"]["since"]
    a.step()  # again, counted from the first
    assert a.held[KEY]["trying"]["since"] == since and a.unsent == []
    assert [r for r in seen if r.method == "POST"] == []  # nothing said
    read.side_effect, read.return_value = None, ok
    a.step()  # the API server back: applied as if nothing
    assert a.held[KEY]["outcome"] == "rolling" and "trying" not in a.held[KEY]
    core, apps = cluster()
    b = make(hub_of([desired(1)] * 2, seen), core, apps)
    apps.read_namespaced_deployment.side_effect = ApiException(status=503, reason="busy")
    b.step()
    b.held[KEY]["trying"]["since"] -= actuator.WAIT_SECONDS  # ten minutes of it
    b.step()
    assert b.held[KEY]["outcome"] == "failed" and "trying" not in b.held[KEY]
    posted = [x for r in seen if r.method == "POST" for x in json.loads(r.content)]
    assert [x["reason"] for x in posted] == ["reading the Deployment: 503: busy"]
    core, apps = cluster()
    c = make(hub_of([desired(1)], []), core, apps)
    apps.read_namespaced_deployment.side_effect = ApiException(status=403, reason="no")
    c.step()  # a refusal: its verdict, at once
    assert c.held[KEY]["outcome"] == "failed"
    core, apps = cluster()
    e = settled(monkeypatch, [desired(1, 2)] * 2, core, apps)
    apps.read_namespaced_deployment.side_effect = ApiException(status=503, reason="busy")
    e.step()  # run 2 waits; run 1 stays held meanwhile
    assert (e.held[KEY]["outcome"], e.held[KEY]["trying"]["run"]) == ("applied", 2)
    e.held[KEY]["trying"]["since"] -= actuator.WAIT_SECONDS
    e.step()  # ten minutes of it: run 2 failed, run 1 still held
    assert (e.held[KEY]["run_id"], e.held[KEY]["refused"]) == (1, 2) and "trying" not in e.held[KEY]
    for where in ("list_namespaced_limit_range", "patch_namespaced_pod_resize"):
        core, apps = cluster()
        d = make(hub_of([desired(1)], []), core, apps)
        getattr(core, where).side_effect = ApiException(status=500, reason="boom")
        d.step()  # its checks, or the canary's resize: passing too
        assert "outcome" not in d.held[KEY] and d.held[KEY]["trying"]["run"] == 1


def test_a_catch_up_waits_out_a_passing_failure_still_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core, apps = cluster()
    a = settled(monkeypatch, [], core, apps)
    late = pod("api-1-c", "api-1")
    late.metadata.namespace = "shop"
    core.list_namespaced_pod.return_value.items.append(late)
    core.list_namespaced_limit_range.side_effect = ApiException(status=503, reason="busy")
    assert a.catch_up() == [] and a.held[KEY]["outcome"] == "applied"  # its webhook goes on
    core.list_namespaced_limit_range.side_effect = ApiException(status=403, reason="no")
    (failed,) = a.catch_up()  # a refusal ends the hold, as before
    assert failed.outcome == "failed" and a.held[KEY]["outcome"] == "failed"


def test_a_rollout_scaled_to_zero_ends_applied_and_held() -> None:
    core, apps = cluster()
    _, held = start(core, apps, wanted())
    canary = core.read_namespaced_pod("api-1-a", "shop")  # allocated, Ready
    core.read_namespaced_pod.side_effect = None
    core.read_namespaced_pod.return_value = canary
    core.list_namespaced_pod.return_value = k.V1PodList(items=[])
    done = actuator.advance(core, apps, KEY, held, 600.0)
    assert (done.outcome, done.pods) == ("applied", 1)
    assert done.reason == "1 pods resized in place, then none left to resize"
    assert held["outcome"] == "applied" and patched(new_pod(), {KEY: held}) is not None


def test_an_approval_of_the_reverted_run_applies_only_if_approved_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    undo = {"id": 7, "namespace": "shop", "kind": "Deployment", "name": "api", "container": "app"}
    again = {"kill_switch": False, "sizings": [{**wanted(1).model_dump(), "again": True}]}
    core, apps = cluster()
    a = settled(monkeypatch, [{**desired(1), "reverts": [undo]}, desired(1), again], core, apps)
    a.step()  # reverted
    count = len(resized(core))
    a.step()  # run 1 as approved before its Revert: nothing
    assert a.held[KEY]["outcome"] == "reverted" and len(resized(core)) == count
    a.step()  # approved again since
    assert a.held[KEY]["outcome"] == "rolling" and a.held[KEY]["revert"] == 7
    core, apps = cluster()
    d = settled(monkeypatch, [{**desired(1), "reverts": [undo]}, again], core, apps)
    d.held[KEY]["refused"] = 2  # a later run refused meanwhile (found in 12.9's live check)
    d.step()
    assert d.held[KEY]["outcome"] == "reverted"
    d.step()  # that mark is run 2's: run 1, approved again, applies
    assert d.held[KEY]["outcome"] == "rolling" and "refused" not in d.held[KEY]
    core, apps = cluster()
    both = {**again, "reverts": [undo]}  # a Revert asked while the Hub answered: beside it
    c = settled(monkeypatch, [both, both, again], core, apps)
    c.step()  # the Revert is this minute's word: put back, not applied again
    assert c.held[KEY]["outcome"] == "reverted"
    count = len(resized(core))
    c.step()  # still listed (its report not taken yet): still waits
    assert c.held[KEY]["outcome"] == "reverted" and len(resized(core)) == count
    c.step()  # the approval alone: made after it
    assert c.held[KEY]["outcome"] == "rolling"
    core, apps = cluster()
    b = settled(monkeypatch, [again], core, apps)
    count = len(resized(core))
    b.step()  # still applied: that flag alone applies nothing twice
    assert b.held[KEY]["outcome"] == "applied" and len(resized(core)) == count
    assert "again" not in a.unsent[0].model_dump() if a.unsent else True


def test_a_call_to_an_api_server_that_never_answers_is_tried_once() -> None:
    """Found in 12.9's live check: the client's default retried a timed-out read three
    times, so one call held the Actuator's minute four timeouts long."""
    import socket

    silent = socket.socket()  # accepts, and never answers
    silent.bind(("127.0.0.1", 0))
    silent.listen(5)
    before = k.Configuration.get_default_copy()
    try:
        actuator.single()
        cfg = k.Configuration.get_default_copy()
        assert cfg.retries == 0
        cfg.host = f"http://127.0.0.1:{silent.getsockname()[1]}"
        core = actuator.Calm(k.CoreV1Api(k.ApiClient(cfg)))
        began = time.time()
        with pytest.raises(ApiException) as e:
            core.list_namespaced_pod("shop", _request_timeout=0.3)
        assert e.value.status == 0 and actuator.passing(e.value)
        assert time.time() - began < 0.9  # once: four tries would take 1.2 s
    finally:
        k.Configuration.set_default(before)
        silent.close()
    assert actuator.API_TIMEOUT == 15


# --- the Hub against an Actuator's reports (slice 12.9c) -------------------------------------

ACME_CHANNEL, BANK_CHANNEL = "C0123456789", "C0999999999"


def slacked(
    settings: Settings,
    database_url: str,
    bank_channel: bool = True,
    handler: Any = None,
    acme: contracts.Contract | None = None,
) -> tuple[Any, Any]:
    """The Hub with Slack: acme and bank automate `app` containers, each with its channel.
    `handler`: Slack's answers, given the FakeSlack to pass a request on to."""
    from tests.test_slack import TOKEN, FakeSlack

    settings.slack_bot_token = SecretStr(TOKEN)
    settings.database_url = SecretStr(database_url)
    settings.approver_token = SecretStr("approver-token")
    slack = FakeSlack()
    theirs = {"slack": {"operator_channel": BANK_CHANNEL}} if bank_channel else {}
    app = create_app(
        settings,
        scripted(),
        lambda c: None,
        {
            "acme": acme
            or automating("acme", A_TOKEN, AUTO, slack={"operator_channel": ACME_CHANNEL}),
            "bank": automating("bank", B_TOKEN, AUTO, **theirs),
        },
        slack_http=httpx.AsyncClient(
            transport=httpx.MockTransport((lambda r: handler(slack, r)) if handler else slack)
        ),
    )
    return app, slack


def back(at: float, **more: Any) -> dict[str, Any]:
    return report(outcome="rolled_back", reason="r", at=at, **more)


def posts(slack: Any) -> list[tuple[str, str]]:
    return [(p["channel"], p["text"]) for m, p, _ in slack.calls if m == "chat.postMessage"]


def test_a_report_with_a_number_that_isnt_one_is_refused_and_an_honest_one_taken(
    settings: Settings, database_url: str
) -> None:
    app, _ = slacked(settings, database_url)
    json_ = {**as_actuator(A_TOKEN), "Content-Type": "application/json"}
    now = time.time()
    with TestClient(app) as c:
        for field, value in (
            ("cpu", "NaN"),
            ("memory", "1e999"),  # infinity, as JSON reads it
            ("at", "NaN"),
            ("at", "-Infinity"),
            ("cpu", "-1"),
            ("memory", "-0.5"),
            ("at", "-1"),
        ):
            body = json.dumps([report(), report(name="bad", **{field: "X"})]).replace('"X"', value)
            refused = c.post("/actuator/reports", content=body, headers=json_)
            assert refused.status_code == 422, (field, value)
            assert refused.json() == {"detail": f"not a number an Actuator sends: ['{field}']"}
        assert sql(database_url, ("select count(*) from actuations", ())) == [(0,)]  # whole
        honest = [
            report(outcome=o, name=o.replace("_", "-"), cpu=None, at=now + 3600)  # a clock ahead
            for o in ("applied", "skipped", "failed", "handed_back", "rolled_back", "reverted")
        ]
        honest.append(report(name="zero", cpu=0.0, memory=0.0, pods=0, at=0.0))
        taken = c.post("/actuator/reports", json=honest, headers=as_actuator(A_TOKEN))
        assert taken.status_code == 204
        assert sql(database_url, ("select count(*) from actuations", ())) == [(7,)]
    # its own model is as it was: what the Actuator builds after a resize, and reads back
    assert actuator.Actuation(**report(cpu=float("nan"))).cpu != 0


def test_a_report_dated_ahead_is_as_old_as_its_arrival(database_url: str) -> None:
    async def go() -> tuple[list[dict[str, Any]], dict[Any, Any], dict[Any, Any]]:
        async with connect(database_url) as pool:
            store = Store(pool)
            await store.migrate()
            ahead = [back(1000.0 + 10 * 365 * 86400 + i) for i in range(3)]  # ten years on
            await store.record_actuations("acme", "test", ahead, 1000.0)
            await store.record_actuations("acme", "test", [report(at=1060.0)], 1060.0)
            await store.record_actuations("acme", "test", ahead, 2000.0)  # again: kept once
            rows = await store.actuations("acme", 0.0)
            week = actuator.ROLLBACKS_FOR
            counted = actuator.rollbacks_of(rows, "test", 1000.0 + week - 1)
            later = actuator.rollbacks_of(await store.actuations("acme", 1001.0), "test", 2000.0)
            return rows, counted, later

    rows, counted, later = asyncio.run(go())
    assert [(r["outcome"], r["at"]) for r in rows] == [
        ("applied", 1060.0),  # the newest is the one that came last, whatever the others say
        *[("rolled_back", 1000.0)] * 3,
    ]
    assert counted == {("shop", "Deployment", "api"): [1000.0] * 3}  # Rollbacks of then
    assert later == {}  # and no longer, once their 7 days from then are over


def test_a_rollback_slack_didnt_take_is_told_later_never_lost(
    settings: Settings, database_url: str
) -> None:
    from squero import api

    app, slack = slacked(settings, database_url)
    now, day = time.time(), 86400.0
    with TestClient(app) as c:
        sql(database_url, run("acme", '{"test": null}'), sized("acme", 1))
        slack.answers = [httpx.Response(500)]
        rows = [back(now - 2 * day), back(now - day), back(now)]
        rows.append(report(at=now - 3 * day))  # applied before them: no Rollback, not counted
        c.post("/actuator/reports", json=rows, headers=as_actuator(A_TOKEN))
        # Slack down as it arrives (the telling its report started): the first not taken,
        # the others not tried behind it; and acme left alone 5 minutes
        tell(c)
        tell(c, at=now + api.REST - 30)
        assert len(posts(slack)) == 1 and len(untold(database_url)) == 3
        tell(c, at=now + api.REST + 30)  # back: each once, the third with what it ended
        assert len(posts(slack)) == 4 and untold(database_url) == []
        tell(c, at=now + api.REST + 60)
        assert len(posts(slack)) == 4  # told: never again
        c.post("/actuator/reports", json=[back(now + 1, name="web")], headers=as_actuator(A_TOKEN))
        tell(c)
        assert len(posts(slack)) == 5  # Slack took its last: acme rests no more
    texts = [t for _, t in posts(slack)[1:4]]
    until = datetime.fromtimestamp(now - 2 * day + 7 * day, UTC)
    assert (
        texts[:2]
        == [":rewind: acme: the Actuator rolled back test/shop/Deployment/api container app: r"] * 2
    )
    assert texts[2] == (
        ":rewind: acme: the Actuator rolled back test/shop/Deployment/api container app: r\n"
        ":octagonal_sign: acme: Automation of test/shop/Deployment/api ended: 3 rollbacks in "
        f"7 days. It resumes on {until:%Y-%m-%d %H:%M} UTC unless more follow; remove "
        "`automate` from its rule to end it for good."
    )


def test_a_flood_of_rollbacks_is_capped_and_never_spends_another_contracts_posts(
    settings: Settings, database_url: str
) -> None:
    from squero import api

    app, slack = slacked(settings, database_url)
    now = time.time()
    with TestClient(app) as c:
        flood = [back(now + i, name=f"w{i:02d}") for i in range(25)]
        c.post("/actuator/reports", json=flood, headers=as_actuator(A_TOKEN))
        tell(c)
        assert api.ROLLBACK_POSTS == 20 and len(posts(slack)) == 21
        assert posts(slack)[-1] == (
            ACME_CHANNEL,
            ":warning: acme: 5 more Rollbacks or Step-ups reported by test's Actuator are not "
            "posted here (20 in the last hour already): see the Automation page.",
        )
        assert untold(database_url) == []  # said, in that line
        c.post("/actuator/reports", json=[back(now + 100 + i) for i in range(3)],
               headers=as_actuator(A_TOKEN))  # fmt: skip
        tell(c)  # within the hour: one such line is enough, these wait for the next
        assert len(posts(slack)) == 21 and len(untold(database_url)) == 3
        c.post("/actuator/reports", json=[back(now)], headers=as_actuator(B_TOKEN))
        tell(c)  # bank's own posts, in bank's own channel
        assert posts(slack)[-1] == (
            BANK_CHANNEL,
            ":rewind: bank: the Actuator rolled back test/shop/Deployment/api container app: r",
        )
        assert untold(database_url) == [("acme", "api")] * 3
        tell(c, at=now + 3601)  # an hour on: acme's are told
        assert len(posts(slack)) == 25 and untold(database_url) == []
    assert {ch for ch, text in posts(slack) if " acme: " in text} == {ACME_CHANNEL}
    assert [ch for ch, text in posts(slack) if " bank: " in text] == [BANK_CHANNEL]


def test_what_an_actuator_sends_reaches_slack_as_text(
    settings: Settings, database_url: str
) -> None:
    app, slack = slacked(settings, database_url)
    forged = report(
        outcome="rolled_back",
        at=time.time(),
        name="<!channel>",
        container="<@U012345>",
        reason="denied: <https://evil.example/x|click here> <!here> & more",
    )
    with TestClient(app) as c:
        c.post("/actuator/reports", json=[forged], headers=as_actuator(A_TOKEN))
        tell(c)
    ((channel, text),) = posts(slack)
    assert channel == ACME_CHANNEL and "<" not in text and ">" not in text
    assert "&lt;!channel&gt; container &lt;@U012345&gt;: denied: " in text
    assert "&lt;!here&gt; &amp; more" in text


def test_a_late_rollback_says_when_and_a_week_old_one_is_given_up(
    settings: Settings, database_url: str
) -> None:
    from squero import api

    app, slack = slacked(settings, database_url, bank_channel=False)
    now = time.time()
    with TestClient(app) as c:
        down, when = httpx.Response(500), datetime.fromtimestamp(now, UTC)

        def late(by: float, i: int) -> None:
            """A Rollback Slack doesn't take as it arrives, told again `by` seconds on."""
            slack.answers = [down]
            c.post("/actuator/reports", json=[back(now + i)], headers=as_actuator(A_TOKEN))
            tell(c)
            assert len(untold(database_url)) == 1
            tell(c, at=now + by)
            assert untold(database_url) == []

        late(api.LATE - 30, 0)  # under ten minutes late: as it would have been
        assert len(posts(slack)) == 2 and not posts(slack)[-1][1].endswith("UTC)")
        late(api.LATE + 30, 1)
        assert len(posts(slack)) == 4
        assert posts(slack)[-1][1].endswith(f": r (reported {when:%Y-%m-%d %H:%M} UTC)")
        late(api.TELL_FOR - 3600, 2)  # not yet a week: still tried, and taken
        assert len(posts(slack)) == 6
        late(api.TELL_FOR + 3600, 3)  # a week of Slack not taking it: given up
        assert len(posts(slack)) == 7
        c.post("/actuator/reports", json=[back(now)], headers=as_actuator(B_TOKEN))
        tell(c)  # bank has no channel: nothing to tell, nothing left untold
        assert untold(database_url) == [] and len(posts(slack)) == 7


def test_the_scheduler_tells_what_no_report_started_and_only_what_is_untold(
    settings: Settings, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from squero import api

    monkeypatch.setattr(api, "CHECK_TICK", 0.05)
    app, slack = slacked(settings, database_url)
    now = time.time()
    with TestClient(app):
        sql(
            database_url,
            rolled_back("acme", now - 5, name="before"),  # stored before 12.9c: told
            (
                "insert into actuations values ('acme', 'test', 'shop', 'Deployment', 'left', "
                "'app', 1, 'rolled_back', null, null, 1, 'x', %s, %s, false)",
                (now, now),
            ),
        )
        slack.until(1)
        time.sleep(0.3)  # a few more ticks: nothing more
    assert [t for _, t in posts(slack)] == [
        ":rewind: acme: the Actuator rolled back test/shop/Deployment/left container app: x"
    ]


def test_one_contracts_telling_that_fails_never_stops_anothers(
    settings: Settings, database_url: str, caplog: Any
) -> None:
    def broken(slack: Any, request: httpx.Request) -> httpx.Response:
        if json.loads(request.content)["channel"] == ACME_CHANNEL:
            raise RuntimeError("a bug telling acme")  # nothing Slack's client expects
        return slack(request)

    app, slack = slacked(settings, database_url, handler=broken)
    now = time.time()
    with TestClient(app) as c:
        c.post("/actuator/reports", json=[back(now)], headers=as_actuator(A_TOKEN))
        c.post("/actuator/reports", json=[back(now)], headers=as_actuator(B_TOKEN))
        tell(c)  # every Contract: acme's fails, bank's is told all the same
    assert [ch for ch, _ in posts(slack)] == [BANK_CHANNEL]
    assert untold(database_url) == [("acme", "api")]  # still to tell
    assert "telling acme of its rollbacks failed" in caplog.text


def refusing_channel(slack: Any, request: httpx.Request) -> httpx.Response:
    slack(request)  # counted
    return httpx.Response(200, json={"ok": False, "error": "channel_not_found"})


def test_a_contract_slack_refuses_rests_longer_each_time_an_hour_at_most(
    settings: Settings, database_url: str
) -> None:
    from squero import api

    app, slack = slacked(settings, database_url, handler=refusing_channel)
    now = time.time()
    with TestClient(app) as c:
        c.post("/actuator/reports", json=[back(now)], headers=as_actuator(A_TOKEN))
        tell(c)  # asked once as it arrived; resting 5 minutes
        tried = [0.0]
        for wait in (300, 600, 1200, 2400, 3600, 3600):  # twice as long, an hour at most
            tell(c, at=now + tried[-1] + wait - 5)
            assert len(posts(slack)) == len(tried), wait
            tried.append(tried[-1] + wait + 5)
            tell(c, at=now + tried[-1])
            assert len(posts(slack)) == len(tried), wait
        assert api.REST == 300 and len(untold(database_url)) == 1


def test_a_refused_channel_is_asked_once_however_many_ticks(
    settings: Settings, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from squero import api

    monkeypatch.setattr(api, "CHECK_TICK", 0.02)
    app, slack = slacked(settings, database_url, handler=refusing_channel)
    with TestClient(app) as c:
        c.post("/actuator/reports", json=[back(time.time())], headers=as_actuator(A_TOKEN))
        slack.until(1)
        time.sleep(0.5)  # some twenty ticks
    assert len(posts(slack)) == 1


def test_slack_never_answering_holds_no_tick_of_the_scheduler(
    settings: Settings, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from squero import api

    async def silent(slack: Any, request: httpx.Request) -> httpx.Response:
        slack(request)
        await asyncio.sleep(3600)  # never, as far as this test goes
        raise AssertionError

    ticks = []
    due = Store.due_checks

    async def counted(self: Store, at: float) -> list[tuple[str, str]]:
        ticks.append(at)
        return await due(self, at)

    monkeypatch.setattr(Store, "due_checks", counted)
    monkeypatch.setattr(api, "CHECK_TICK", 0.02)
    app, slack = slacked(settings, database_url, handler=silent)
    with TestClient(app) as c:
        untold_row = (
            "insert into actuations values ('acme', 'test', 'shop', 'Deployment', 'left', "
            "'app', 1, 'rolled_back', null, null, 1, 'x', %s, %s, false)",
            (time.time(), time.time()),
        )
        sql(database_url, untold_row)  # no report started its telling: a tick does
        slack.until(1)  # and it is waiting on Slack now
        before = len(ticks)
        end = time.monotonic() + 3
        while len(ticks) < before + 5 and time.monotonic() < end:
            time.sleep(0.02)
        took = c.post("/actuator/reports", json=[report()], headers=as_actuator(A_TOKEN))
    assert len(ticks) >= before + 5  # Checks, digests and deploys go on beside it
    assert took.status_code == 204 and len(posts(slack)) == 1  # and the Actuator is answered


def test_a_number_that_cant_be_said_in_json_is_a_422_wherever_it_is(
    settings: Settings, database_url: str
) -> None:
    app, _ = slacked(settings, database_url)
    json_ = {**as_actuator(A_TOKEN), "Content-Type": "application/json"}
    with TestClient(app) as c:
        for field in ("pods", "run_id"):  # integers: FastAPI's own check, which echoed it
            body = json.dumps([report(**{field: "X"})]).replace('"X"', "NaN")
            refused = c.post("/actuator/reports", content=body, headers=json_)
            assert refused.status_code == 422
            (said,) = refused.json()["detail"]
            assert said["loc"] == ["body", 0, field] and set(said) == {"type", "loc", "msg"}


def test_a_clusters_flood_never_spends_another_clusters_posts(
    settings: Settings, database_url: str
) -> None:
    other = "c" * 40
    data = automating("acme", A_TOKEN, AUTO).model_dump(mode="json", exclude_none=True)
    data["clusters"][1]["actuator_token_secret"] = "act2"
    data["slack"] = {"operator_channel": ACME_CHANNEL}
    secrets = {"act": SecretStr(A_TOKEN), "act2": SecretStr(other), "r": SecretStr("r" * 40)}
    app, slack = slacked(settings, database_url, acme=contracts.build(data, secrets=secrets))
    now = time.time()
    with TestClient(app) as c:
        flood = [back(now + i, name=f"w{i:02d}") for i in range(25)]
        c.post("/actuator/reports", json=flood, headers=as_actuator(other))  # edge's
        tell(c)
        assert len(posts(slack)) == 21  # 20, and "5 more"
        c.post("/actuator/reports", json=[back(now + 100)], headers=as_actuator(other))
        tell(c)  # edge's line was just said: this one waits
        assert len(posts(slack)) == 21 and untold(database_url) == [("acme", "api")]
        c.post("/actuator/reports", json=flood[:23], headers=as_actuator(A_TOKEN))  # test's
        tell(c)  # its own 20 posts, and a line that counts its own 3, never edge's one
        assert len(posts(slack)) == 42 and untold(database_url) == [("acme", "api")]
        tell(c, at=now + 3601)  # an hour on: edge's is told
        assert len(posts(slack)) == 43 and untold(database_url) == []
    said = [t for _, t in posts(slack) if "more Rollbacks" in t]
    assert [t.split("reported by ")[1].split("'s")[0] for t in said] == ["edge", "test"]
    assert [t.split(" more ")[0].rsplit(" ", 1)[1] for t in said] == ["5", "3"]
    assert posts(slack)[-1][1].startswith(":rewind: acme: the Actuator rolled back edge/shop/")


def test_a_rollback_is_told_as_soon_as_its_report_is_answered(
    settings: Settings, database_url: str
) -> None:
    app, slack = slacked(settings, database_url)
    with TestClient(app) as c:  # no tick in this test's time (30 s), no telling asked
        c.post("/actuator/reports", json=[back(time.time())], headers=as_actuator(A_TOKEN))
        slack.until(1)
    assert [ch for ch, _ in posts(slack)] == [ACME_CHANNEL]


# slice 12.10: a pod whose node is gone is never a Sizing's fault, nor resized


def shown(p: Any, conditions: dict[str, Any], since: float = 0.0, **status_: Any) -> Any:
    """`p` with these conditions (a status, or (status, reason)), all since `since`."""
    when = datetime.fromtimestamp(since, UTC)
    p.status.conditions = [
        k.V1PodCondition(
            type=t,
            status=s if isinstance(s, str) else s[0],
            reason=None if isinstance(s, str) else s[1],
            last_transition_time=when,
        )
        for t, s in conditions.items()
    ]
    for field, value in status_.items():
        setattr(p.status, field, value)
    return p


# as the node-lifecycle controller leaves a pod whose node is NotReady
NODE_GONE = {"Ready": "False", "ContainersReady": "True"}
PROBE_FAILS = {"Ready": "False", "ContainersReady": "False"}
SHUT = "Pod was terminated in response to imminent node shutdown."


def gated(p: Any, gate: str = "example.com/lb") -> Any:
    p.spec.readiness_gates = [k.V1PodReadinessGate(condition_type=gate)]
    return p


@pytest.mark.parametrize(
    ("p", "is_lost"),
    [
        (shown(pod("a", "api-1"), NODE_GONE), True),
        (shown(pod("a", "api-1"), PROBE_FAILS), False),
        (shown(pod("a", "api-1"), {"Ready": "False"}), False),  # the kubelet's word unknown
        (shown(pod("a", "api-1"), {"Ready": "True", "ContainersReady": "True"}), False),
        (shown(pod("a", "api-1"), {}), False),
        # a readiness gate not met gives the same shape through no node's fault
        (shown(gated(pod("a", "api-1")), NODE_GONE), False),
        (shown(gated(pod("a", "api-1")), NODE_GONE | {"example.com/lb": "False"}), False),
        (shown(gated(pod("a", "api-1")), NODE_GONE | {"example.com/lb": "True"}), True),
        # removed for its node
        (shown(pod("a", "", "Failed"), {"DisruptionTarget": ("True", "DeletionByPodGC")}), True),
        (
            shown(pod("a", "", "Failed"), {"DisruptionTarget": ("True", "DeletionByTaintManager")}),
            True,
        ),
        (shown(pod("a", "", "Failed"), {"DisruptionTarget": ("False", "DeletionByPodGC")}), False),
        # a drain or a node under pressure may be a Sizing's doing: not counted as node loss
        (
            shown(pod("a", "", "Failed"), {"DisruptionTarget": ("True", "EvictionByEvictionAPI")}),
            False,
        ),
        (
            shown(pod("a", "", "Failed"), {"DisruptionTarget": ("True", "TerminationByKubelet")}),
            False,
        ),
        # its node shut down: the kubelet's documented reason and message, both
        (shown(pod("a", "", "Failed"), {}, reason="Terminated", message=SHUT), True),
        (shown(pod("a", "", "Failed"), {}, reason="Evicted", message=SHUT), False),
        (shown(pod("a", "", "Failed"), {}, reason="Terminated", message="out of memory"), False),
    ],
)
def test_a_pod_whose_node_is_gone_is_told_from_the_pod_alone(p: Any, is_lost: bool) -> None:
    assert actuator.lost(p) is is_lost


def test_a_resized_pod_notready_with_its_node_is_no_rollback_a_failing_probe_still_is() -> None:
    core, apps = cluster()
    h = watched(core, apps)
    a, b = core.list_namespaced_pod.return_value.items[:2]
    unhealthy(b)
    shown(b, NODE_GONE, since=1500.0)
    assert actuator.trouble(core, apps, KEY, h, 2000.0) is None
    shown(b, PROBE_FAILS, since=1500.0)
    assert actuator.trouble(core, apps, KEY, h, 2000.0) == "api-1-b: NotReady for 2 minutes"
    unhealthy(a, restarts=1, oom=True)  # a restart before its node went is still the Sizing's
    shown(a, NODE_GONE, since=1500.0)
    assert actuator.trouble(core, apps, KEY, h, 2000.0) == "api-1-a: app was OOM-killed"


def test_an_evicted_pods_replacement_lost_with_its_node_is_no_verdict() -> None:
    core, apps = cluster()
    _, held = start(core, apps, wanted())
    infeasible(core)
    actuator.advance(core, apps, KEY, held, 60.0, "open", PDB)
    pods = core.list_namespaced_pod.return_value.items
    pods[0].metadata.deletion_timestamp = datetime.now(UTC)
    new = up("api-1-c", ready=False, cpu="115m", memory="96Mi")
    pods.append(new)
    assert actuator.trouble(core, apps, KEY, held, 660.0) == (
        "api-1-a evicted: no replacement Ready in 10 minutes"
    )
    shown(new, NODE_GONE)
    assert actuator.trouble(core, apps, KEY, held, 660.0) is None


def test_a_lost_pod_is_never_the_canary_and_one_alone_behind_is_tried_again() -> None:
    core, apps = cluster()
    a, b = core.list_namespaced_pod.return_value.items[:2]
    shown(a, NODE_GONE)
    done, held = start(core, apps, wanted())
    assert done is None and held["pod"] == "api-1-b" and "passing" not in held
    assert [n for n, _ in resized(core)] == ["api-1-b"]
    core, apps = cluster()
    a, b = core.list_namespaced_pod.return_value.items[:2]
    b.spec.containers[0].resources.requests = {"cpu": "115m", "memory": "96Mi"}  # has them
    shown(a, NODE_GONE)
    done, held = start(core, apps, wanted())
    assert (done.outcome, done.reason) == (
        "skipped",
        "the pods without these requests are on a node that is gone",
    )
    assert held["passing"] is True and resized(core) == []  # act() tries again next minute
    shown(a, {"Ready": "True", "ContainersReady": "True"})  # its node back
    done, held = start(core, apps, wanted())
    assert done is None and held["pod"] == "api-1-a"


def losing(core: Any, name: str) -> None:
    """From now `name` reads as on a node that is gone, however it is read."""
    read = core.read_namespaced_pod.side_effect

    def lost_now(pod_: str, ns: str) -> Any:
        p = read(pod_, ns)
        return shown(p, NODE_GONE) if pod_ == name else p

    core.read_namespaced_pod.side_effect = lost_now
    live = core.list_namespaced_pod.return_value.items
    shown(next(p for p in live if p.metadata.name == name), NODE_GONE)


def test_a_rollout_goes_on_without_a_pod_whose_node_is_gone() -> None:
    core, apps = cluster()
    _, held = start(core, apps, wanted())
    core.patch_namespaced_pod_resize.side_effect = None  # the kubelet is gone: never allocated
    losing(core, "api-1-a")  # the canary's node goes after its ten minutes
    assert actuator.advance(core, apps, KEY, held, 600.0) is None  # not "in progress" too long
    assert held["pod"] == "api-1-b" and held["resized"] == 2  # the next one, never api-1-a again
    assert held["canary_until"] == 600.0
    core, apps = cluster()
    _, held = start(core, apps, wanted())
    losing(core, "api-1-a")  # inside its ten minutes: nobody was watched, so the next is the canary
    assert actuator.advance(core, apps, KEY, held, 60.0) is None
    assert (held["pod"], held["resized"], held["canary_until"]) == ("api-1-b", 1, 660.0)
    assert actuator.advance(core, apps, KEY, held, 120.0) is None and held["pod"] == "api-1-b"
    core, apps = cluster()
    _, held = start(core, apps, wanted())
    losing(core, "api-1-b")  # the other pod's node goes before its turn
    done = actuator.advance(core, apps, KEY, held, 600.0)
    assert (done.outcome, done.reason) == ("applied", "1 of 1 pods resized in place, one at a time")
    assert [n for n, _ in resized(core)] == ["api-1-a"]  # the lost pod never resized
    core, apps = cluster()
    _, held = start(core, apps, wanted())
    for name in ("api-1-a", "api-1-b"):
        losing(core, name)  # every pod's node gone: as scaled to zero, never "no node has room"
    done = actuator.advance(core, apps, KEY, held, 600.0)
    assert done.reason == "1 pods resized in place, then none left to resize"


def test_a_catch_up_leaves_a_lost_pod_until_its_node_is_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core, apps = cluster()
    a = settled(monkeypatch, [], core, apps)
    late = pod("api-1-c", "api-1")  # from its template, the Actuator down then
    late.metadata.namespace = "shop"
    core.list_namespaced_pod.return_value.items.append(shown(late, NODE_GONE))
    before = len(resized(core))
    assert a.catch_up() == [] and a.held[KEY]["outcome"] == "applied"
    assert len(resized(core)) == before and core.list_namespaced_limit_range.call_count == 1
    shown(late, {"Ready": "True", "ContainersReady": "True"})
    assert a.catch_up() == [] and a.held[KEY]["pod"] == "api-1-c"  # rolling again


@pytest.mark.parametrize(
    ("how", "rolled_back"),
    [
        ({"conditions": {"DisruptionTarget": ("True", "DeletionByPodGC")}}, False),
        ({"conditions": {}, "reason": "Terminated", "message": SHUT}, False),
        ({"conditions": {"DisruptionTarget": ("True", "EvictionByEvictionAPI")}}, True),
    ],
)
def test_a_runs_pod_failed_with_its_node_is_no_rollback(how: Any, rolled_back: bool) -> None:
    core, apps = cronned()
    t0 = time.time() - 600
    _, held = start(core, apps, of("CronJob"), at=t0)
    act = make(hub_of([], []), core, apps, held={CRON: held})
    job(core, apps, "api-3", t0 + 60, shown(ran("api-3-a", "Failed"), **how))
    assert [a.outcome for a in act.watch()] == (["rolled_back"] if rolled_back else [])
    assert ("first_run" in act.held[CRON]) is not rolled_back  # still watched: the Job retries
    core.runs["api-3"] = [shown(ran("api-3-a", "Failed", oom="state"), **how)]
    if not rolled_back:  # an OOM kill is the Sizing's, whatever became of its node after
        assert [a.outcome for a in act.watch()] == ["rolled_back"]


def test_a_rollback_still_puts_a_lost_pods_requests_back() -> None:
    core, apps = cluster()
    h = watched(core, apps)
    a, b = core.list_namespaced_pod.return_value.items[:2]
    unhealthy(a, restarts=1)
    shown(unhealthy(b), NODE_GONE)  # its node may return: not left with requests nobody holds
    before = len(resized(core))
    back = actuator.rollback(core, apps, KEY, h, "api-1-a: app restarted", 2000.0)
    assert back.pods == 2 and {n for n, _ in resized(core)[before:]} == {"api-1-a", "api-1-b"}


# slice 12.11: a container its node's restart started again is no Sizing's fault


def ended(p: Any, restarts: int, reason: str, code: int) -> Any:
    """`p`'s container restarted `restarts` times, its last end as the runtime reports it."""
    unhealthy(p, restarts=restarts)
    last = k.V1ContainerStateTerminated(exit_code=code, reason=reason)
    p.status.container_statuses[0].last_state = k.V1ContainerState(terminated=last)
    return p


def test_a_restart_with_its_node_is_no_rollback_a_crash_then_or_later_still_is() -> None:
    core, apps = cluster()
    h = watched(core, apps)
    a = core.list_namespaced_pod.return_value.items[0]

    def found() -> str | None:
        return actuator.trouble(core, apps, KEY, h, 2000.0)

    ended(a, 1, "Unknown", 255)  # as kind's containerd reported it once its node was back
    assert found() is None
    ended(a, 1, "Unknown", 137)  # the reason alone is not it
    assert found() == "api-1-a: app restarted"
    ended(a, 1, "Error", 255)  # nor the exit code alone: a crash can end with 255
    assert found() == "api-1-a: app restarted"
    ended(a, 2, "Error", 1)  # a crash after its node's restart
    assert found() == "api-1-a: app restarted"
    ended(a, 2, "OOMKilled", 137)
    assert found() == "api-1-a: app was OOM-killed"
    a.status.container_statuses[0].last_state = None  # restarted, its last end unknown: counted
    assert found() == "api-1-a: app restarted"


def test_a_returned_nodes_pod_is_notready_from_when_its_containers_started_again() -> None:
    core, apps = cluster()
    h = watched(core, apps)
    b = core.list_namespaced_pod.return_value.items[1]

    def since(ready: float, own: float, own_status: str = "False") -> None:
        ended(b, 1, "Unknown", 255)
        shown(b, {"Ready": "False"}, since=ready)
        at = datetime.fromtimestamp(own, UTC)
        b.status.conditions.append(
            k.V1PodCondition(type="ContainersReady", status=own_status, last_transition_time=at)
        )

    since(1500.0, 1950.0)  # its node went at 1500 and is back at 1950, its app slow to be ready
    assert actuator.trouble(core, apps, KEY, h, 2000.0) is None
    assert actuator.trouble(core, apps, KEY, h, 2069.0) is None
    assert actuator.trouble(core, apps, KEY, h, 2070.0) == "api-1-b: NotReady for 2 minutes"
    since(1950.0, 1500.0)  # the later of the two, whichever it is
    assert actuator.trouble(core, apps, KEY, h, 2000.0) is None
    since(900.0, 1950.0)  # NotReady since before its resize: still not its doing
    assert actuator.trouble(core, apps, KEY, h, 2100.0) is None
    gated(b)
    since(1500.0, 1950.0, "True")  # held by a gate, its containers ready: counted from 1500
    assert actuator.trouble(core, apps, KEY, h, 2000.0) == "api-1-b: NotReady for 2 minutes"


# --- slice 13.1a: OOM kills of automated containers, seen, told apart and reported -----------

OOM = {"actuator": {"reflexes": {"oom": {}}}}


def test_the_oom_reflex_needs_an_actuator_and_takes_how_far_a_step_up_may_go() -> None:
    assert automating("acme", A_TOKEN, AUTO, **OOM).actuator.reflexes.oom is not None
    assert automating("acme", A_TOKEN, AUTO).actuator.reflexes.oom is None  # off unless set
    assert automating("acme", A_TOKEN, AUTO, **OOM).actuator.reflexes.oom.max == 2.0
    data = actuated("acme", A_TOKEN).model_dump(mode="json", exclude_none=True)
    data["clusters"][0].pop("actuator_token_secret")  # no cluster runs an Actuator
    with pytest.raises(ValueError, match="actuator: reflexes, but no cluster has an actuator"):
        contracts.build({**data, **OOM}, secrets={"r": SecretStr("r" * 40)})
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):  # no such Reflex
        automating("acme", A_TOKEN, AUTO, actuator={"reflexes": {"throttle": {}}})
    assert automating("acme", A_TOKEN, AUTO, **OOM).actuator.reflexes.oom.expires == 7 * 86400
    for bad in ("30m", "91d"):  # an hour at least, 90 days at most
        with pytest.raises(ValueError, match="must be from 1h to 90d"):
            automating("acme", A_TOKEN, AUTO, actuator={"reflexes": {"oom": {"expires": bad}}})
    for bad in (1.0, 4.5):  # never no step at all, nor past 4 times its template's limit
        with pytest.raises(ValueError, match="max"):
            automating("acme", A_TOKEN, AUTO, actuator={"reflexes": {"oom": {"max": bad}}})
    three = automating("acme", A_TOKEN, AUTO, actuator={"reflexes": {"oom": {"max": 3}}})
    assert three.actuator.reflexes.oom.max == 3.0


def test_the_hub_lists_a_contracts_automated_containers_only_with_its_oom_reflex(
    settings: Settings, database_url: str
) -> None:
    now = time.time()
    acme = automating("acme", A_TOKEN, AUTO, **OOM)
    bank = automating("bank", B_TOKEN, {**AUTO, "name": "ledger"}, **OOM)
    with TestClient(hub(settings, database_url, acme, bank)) as c:
        sql(
            database_url,
            run("acme", '{"test": null}'),
            sized("acme", 1),
            sized("acme", 1, "learning", state="learning", memory=None),  # no Sizing yet
            sized("acme", 1, "steady", cpu={"action": "keep", "current": 1.0, "target": 1.0}),
            sized("acme", 1, "db", kind="StatefulSet", memory={**KEEP, "basis": 3.0 * MI}),
            sized("acme", 1, "side", container="sidecar"),  # no rule automates it
            sized("acme", 1, "broken"),
            sized("acme", 1, "undone"),
            sized("acme", 1, "moved", namespace="old"),  # no longer one of its namespaces
            *(rolled_back("acme", now - d * 86400, name="broken") for d in (1, 2, 3)),  # ended
            revert("acme", now - 50, "undone"),  # its Automation stopped until an approval
            run("bank", '{"test": null}'),
            sized("bank", 2, "ledger"),
            sized("bank", 2, "api"),  # bank automates only its ledger
        )
        kinds = {"kinds": ",".join(actuator.KINDS)}
        mine = c.get("/actuator/desired", headers=as_actuator(A_TOKEN), params=kinds).json()
        old = c.get("/actuator/desired", headers=as_actuator(A_TOKEN)).json()
        theirs = c.get("/actuator/desired", headers=as_actuator(B_TOKEN), params=kinds).json()
        sql(database_url, ("insert into kill_switches values ('acme', 't', 1)", ()))
        killed = c.get("/actuator/desired", headers=as_actuator(A_TOKEN), params=kinds).json()
    # each with its memory peak over its Sizing's window (slice 13.1c), None when not known
    one = {"namespace": "shop", "kind": "Deployment", "container": "app", "peak": None}
    assert mine["oom"] == [{**one, "name": n} for n in ("api", "learning", "steady")] + [
        {**one, "kind": "StatefulSet", "name": "db", "peak": 3.0 * MI}
    ]
    assert old["oom"] == mine["oom"][:-1]  # an Actuator naming no kinds: Deployments only
    assert theirs["oom"] == [{**one, "name": "ledger"}] and killed["oom"] == []
    week = 7 * 86400  # a Step-up's term (slice 13.1c): the Contract's, 7 days unless it says
    assert (mine["oom_expires"], theirs["oom_expires"], killed["oom_expires"]) == (week, week, None)
    # how far a Step-up may go (slice 13.1b): the Contract's, 2 unless it says; none when off
    assert (mine["oom_max"], theirs["oom_max"], killed["oom_max"]) == (2.0, 2.0, None)
    off = automating("acme", A_TOKEN, AUTO)  # the same Contract without the Reflex
    with TestClient(hub(settings, database_url, off)) as c:
        sql(database_url, ("delete from kill_switches", ()))
        mine = c.get("/actuator/desired", headers=as_actuator(A_TOKEN), params=kinds).json()
    assert mine["oom"] == [] and [w["name"] for w in mine["automated"]] == ["api", "db"]
    assert mine["oom_max"] is None and mine["oom_expires"] is None
    own = {"reflexes": {"oom": {"max": 3, "expires": "36h"}}}
    with TestClient(
        hub(settings, database_url, automating("acme", A_TOKEN, AUTO, actuator=own))
    ) as c:
        mine = c.get("/actuator/desired", headers=as_actuator(A_TOKEN), params=kinds).json()
    assert (mine["oom_max"], mine["oom_expires"]) == (3.0, 36 * 3600)


KEEP = {"action": "keep", "current": 1.0, "target": 1.0}


def seen_kill(**more: Any) -> dict[str, Any]:
    return report(outcome="oom_killed", run_id=0, cpu=None, memory=256.0 * MI, pods=1, **more)


def test_an_oom_kill_seen_is_stored_never_told_nor_held_nor_something_to_revert(
    settings: Settings, database_url: str
) -> None:
    from tests.test_console import OPERATOR, VIEW

    settings.console_operators = {OPERATOR}
    app, slack = slacked(settings, database_url)
    now = time.time()
    path, same = "/api/v1/contracts/acme/automation", {**VIEW, "Sec-Fetch-Site": "same-origin"}
    with TestClient(app) as c:
        mixed = [
            report(at=now - 300),  # api: applied
            seen_kill(at=now - 200, reason="OOM-killed: api-1-a at its memory limit"),
            seen_kill(at=now - 100, name="lone", reason="OOM-killed: lone-1-a"),  # nothing held
            back(now - 50, name="web"),
            {**seen_kill(at=now - 40, reason="memory limit"), "outcome": "stepped_up"}
            | {"memory": 356.0 * MI},
            # its end is no Sizing's hand-back: api still holds its cpu, and can be reverted
            {**seen_kill(at=now - 30, reason="its template's memory changed"), "pods": 0}
            | {"outcome": "step_ended", "memory": None},
        ]
        sent = c.post("/actuator/reports", json=mixed, headers=as_actuator(A_TOKEN))
        again = c.post("/actuator/reports", json=mixed[1:2], headers=as_actuator(A_TOKEN))
        tell(c)
        page = c.get(path, headers=VIEW).json()
        what = {"cluster": "test", "namespace": "shop", "name": "lone", "container": "app"}
        never = c.post(f"{path}/revert", json=what, headers=same)
    assert (sent.status_code, again.status_code) == (204, 204)
    stored = sql(
        database_url, ("select name, outcome, told, memory from actuations order by at", ())
    )
    assert stored == [
        ("api", "applied", True, None),
        ("api", "oom_killed", True, 256.0 * MI),  # once, though sent twice
        ("lone", "oom_killed", True, 256.0 * MI),
        ("web", "rolled_back", True, None),
        ("api", "stepped_up", True, 356.0 * MI),  # a Step-up (slice 13.1b): stored, as untold
        ("api", "step_ended", True, None),
    ]
    assert len(posts(slack)) == 1 and "rolled back test/shop/Deployment/web" in posts(slack)[0][1]
    shown = {h["name"]: h for h in page["clusters"][0]["containers"]}
    assert list(shown) == ["api", "web"]  # lone holds nothing: not a row
    assert shown["api"]["outcome"] == "applied" and shown["api"]["revertable"]
    assert never.status_code == 409 and "nothing to revert" in never.text


def killed(
    core: Any,
    name: str,
    ago: float,
    limit: str | None = "256Mi",
    reason: str = "OOMKilled",
    node: str = "n1",
    how: str = "last_state",
) -> Any:
    """api's pod `name`, of its ReplicaSet api-1, its container ended `ago` seconds ago."""
    p = next(p for p in core.list_namespaced_pod.return_value.items if p.metadata.name == name)
    p.metadata.labels = {"app": "api", "pod-template-hash": "1"}
    p.spec.node_name = node
    p.spec.containers[0].resources.limits = {"memory": limit} if limit else None
    st = status("app", None)
    end = k.V1ContainerStateTerminated(
        exit_code=137, reason=reason, finished_at=datetime.fromtimestamp(time.time() - ago, UTC)
    )
    setattr(st, how, k.V1ContainerState(terminated=end))
    p.status.container_statuses = [st]
    return p


def system_oom(core: Any, *ago: float) -> None:
    """The node's kubelet said SystemOOM that many seconds ago."""
    core.list_namespaced_event.side_effect = None
    core.list_namespaced_event.return_value = k.CoreV1EventList(
        items=[
            k.CoreV1Event(
                metadata=k.V1ObjectMeta(name=f"e{i}"),
                involved_object=k.V1ObjectReference(kind="Node", name="n1"),
                reason="SystemOOM",
                last_timestamp=datetime.fromtimestamp(time.time() - s, UTC),
            )
            for i, s in enumerate(ago)
        ]
    )


API = actuator.Watched(namespace="shop", kind="Deployment", name="api", container="app")


def posted(seen: list[httpx.Request]) -> list[list[str]]:
    """The outcomes of each batch of reports the Hub was sent."""
    return [
        [a["outcome"] for a in json.loads(r.content)]
        for r in seen
        if r.url.path == "/actuator/reports"
    ]


def looking(core: Any, apps: Any, **kw: Any) -> actuator.Actuator:
    system_oom(core)
    return make(hub_of([], []), core, apps, **kw)


def test_an_oom_kill_at_its_limit_is_container_limit_and_reported_once() -> None:
    core, apps = cluster()
    a = looking(core, apps)
    at = killed(core, "api-1-a", 60).status.container_statuses[0].last_state.terminated
    (seen,) = a.ooms([API])
    assert seen.model_dump() == {
        "namespace": "shop",
        "kind": "Deployment",
        "name": "api",
        "container": "app",
        "run_id": 0,  # nothing of it held
        "outcome": "oom_killed",
        "cpu": None,
        "memory": 256.0 * MI,
        "pods": 1,
        "reason": "OOM-killed: api-1-a with a memory limit of 256Mi, its node not out of "
        "memory (container-limit). Nothing changed",
        "at": at.finished_at.timestamp(),
    }
    asked = "involvedObject.kind=Node,involvedObject.name=n1,reason=SystemOOM"
    assert core.list_namespaced_event.call_args == call("default", field_selector=asked)
    assert a.ooms([API]) == [] and resized(core) == []  # said once, and nothing changed
    a.held[KEY] = {"run_id": 7, "outcome": "applied"}
    a.oom_said.clear()  # as after its restart: the same report again, which the Hub stores once
    (again,) = a.ooms([API])
    assert again.model_dump() == {**seen.model_dump(), "run_id": 7}  # held now: its run


@pytest.mark.parametrize(
    ("events", "limit", "told"),
    [
        (
            (70.0,),
            "256Mi",
            "api-1-a with its node n1 out of memory (node-level: a SystemOOM Event)",
        ),
        (
            (180.0,),
            "256Mi",
            "api-1-a with its node n1 out of memory (node-level: a SystemOOM Event)",
        ),
        (
            (181.0,),
            "256Mi",
            "api-1-a with a memory limit of 256Mi, its node not out of memory (container-limit)",
        ),
        (
            (3000.0, 181.0),
            "256Mi",
            "api-1-a with a memory limit of 256Mi, its node not out of memory (container-limit)",
        ),
        ((), None, "api-1-a with no memory limit (node-level)"),
    ],
)
def test_a_kill_near_its_nodes_system_oom_or_without_a_limit_is_node_level(
    events: tuple[float, ...], limit: str | None, told: str
) -> None:
    core, apps = cluster()
    a = looking(core, apps)
    killed(core, "api-1-a", 60, limit)
    system_oom(core, *events)
    (seen,) = a.ooms([API])
    assert seen.reason == f"OOM-killed: {told}. Nothing changed"
    assert seen.memory == (256.0 * MI if limit else None)
    assert core.list_namespaced_event.called is bool(limit)  # no limit: nothing to read


def test_node_events_it_may_not_read_leave_a_kill_container_limit_said_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    core, apps = cluster()
    a = looking(core, apps)
    core.list_namespaced_event.side_effect = ApiException(status=403, reason="Forbidden")
    killed(core, "api-1-a", 60)
    killed(core, "api-1-b", 50, node="n2")
    with caplog.at_level("WARNING"):
        (seen,) = a.ooms([API])
    assert seen.pods == 2 and seen.reason == (
        "OOM-killed: api-1-a with a memory limit of 256Mi (container-limit: its node's Events "
        "not read); api-1-b with a memory limit of 256Mi (container-limit: its node's Events "
        "not read). Node Events not read: 403: Forbidden. Nothing changed"
    )
    assert caplog.text.count("reading Node Events in default") == 1
    assert core.list_namespaced_event.call_count == 2  # each node asked once


def test_only_fresh_kills_of_listed_containers_count_and_a_crash_loop_is_said_every_10_minutes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core, apps = cluster()
    a = looking(core, apps)
    killed(core, "api-1-a", 600)  # 10 minutes old: no news
    killed(core, "api-1-b", 60, reason="Error")  # the runtime's own word only
    killed(core, "api-x-a", 60)  # another Deployment's, its ReplicaSet api-x
    assert a.ooms([API]) == []
    other = actuator.Watched(namespace="shop", kind="Deployment", name="api", container="side")
    killed(core, "api-1-a", 599)
    assert a.ooms([other]) == []  # the container named is not the one killed
    (first,) = a.ooms([API])
    assert first.pods == 1
    clock = time.time()
    monkeypatch.setattr(actuator.time, "time", lambda: clock + 300)  # the test's clock too
    killed(core, "api-1-a", 10)  # killed again, 10 seconds ago
    assert a.ooms([API]) == []  # one report a container every 10 minutes
    monkeypatch.setattr(actuator.time, "time", lambda: clock + 601)
    killed(core, "api-1-b", 5, reason="OOMKilled")
    (later,) = a.ooms([API])  # its kills since the last report, together: none left out
    assert later.pods == 2 and "api-1-a" in later.reason and "api-1-b" in later.reason
    monkeypatch.setattr(actuator.time, "time", lambda: clock + 1202)
    assert a.ooms([API]) == []  # and none told twice: both are old news by its next report


def test_kills_of_one_container_are_one_report_and_past_five_pods_only_counted() -> None:
    core, apps = cluster()
    a = looking(core, apps)
    live = core.list_namespaced_pod.return_value.items
    for i in range(7):
        extra = pod(f"api-1-p{i}", "api-1")
        extra.metadata.namespace = "shop"
        live.append(extra)
        killed(core, f"api-1-p{i}", 100 - i)
    (seen,) = a.ooms([API])
    assert seen.pods == 7 and seen.reason.count("(container-limit)") == 5
    assert core.list_namespaced_event.call_count == 1  # their one node's Events, read once
    assert seen.reason.endswith("; and 2 more. Nothing changed")
    assert seen.at == max(
        p.status.container_statuses[0].last_state.terminated.finished_at.timestamp()
        for p in live
        if p.metadata.name.startswith("api-1-p")
    )


def test_a_namespace_it_may_not_list_does_not_hide_the_others_kills() -> None:
    core, apps = cluster()
    a = looking(core, apps)
    killed(core, "api-1-a", 60)
    pods = core.list_namespaced_pod.return_value

    def listed(ns: str, **_: Any) -> Any:
        if ns == "bank":
            raise ApiException(status=403, reason="Forbidden")
        return pods

    core.list_namespaced_pod.side_effect = listed
    theirs = actuator.Watched(namespace="bank", kind="Deployment", name="api", container="app")
    (seen,) = a.ooms([theirs, API])
    assert seen.namespace == "shop"


def test_a_cronjobs_run_killed_for_good_is_its_cronjobs_kill() -> None:
    core, apps = cluster()
    a = looking(core, apps)
    p = killed(core, "api-1-a", 60, how="state")
    minute = int(time.time() // 60)
    p.metadata.labels = {}
    p.metadata.owner_references = [owner("Job", f"nightly-{minute}")]
    p.status.phase = "Failed"
    nightly = actuator.Watched(namespace="shop", kind="CronJob", name="nightly", container="app")
    (seen,) = a.ooms([nightly])
    assert (seen.kind, seen.name, seen.pods) == ("CronJob", "nightly", 1)


def test_a_minute_reports_kills_only_when_the_hub_lists_containers_and_took_its_reports() -> None:
    core, apps = cluster()
    system_oom(core)
    killed(core, "api-1-a", 60)
    listing = {**desired(), "oom": [API.model_dump()]}
    seen: list[httpx.Request] = []
    a = make(hub_of([desired(), listing, listing, listing], seen), core, apps)
    a.step()  # an older Hub, or the Reflex off: nothing listed, nothing reported
    assert posted(seen) == [] and not core.list_namespaced_event.called
    a.unsent.append(actuator.Actuation(**report()))  # one the Hub has not taken yet
    a.http.reports.append(httpx.Response(503))  # type: ignore[attr-defined]
    a.step()
    a.step()  # taken now, at this minute's end
    assert posted(seen) == [["applied"], ["applied"]]  # it waited: never crowding one out
    assert not core.list_namespaced_event.called
    a.step()
    assert posted(seen)[2:] == [["oom_killed"]]


def test_an_oom_kill_in_a_watch_hour_still_rolls_back_and_is_reported_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core, apps = cluster()
    listing = {**desired(1), "oom": [API.model_dump()]}
    a = settled(monkeypatch, [listing], core, apps)
    system_oom(core)
    p = killed(core, "api-1-a", 30, limit=None)
    p.spec.containers[0].resources.requests = {"cpu": "115m", "memory": "96Mi"}  # the held ones
    p.status.container_statuses[0].restart_count = 1
    a.step()
    assert posted(a.seen)[-1] == ["rolled_back", "oom_killed"]
    assert a.held[KEY]["outcome"] == "rolled_back"


def test_a_minute_reports_50_containers_at_most_the_others_the_next() -> None:
    core, apps = cluster()
    a = looking(core, apps)
    killed(core, "api-1-a", 60)
    live = core.list_namespaced_pod.return_value.items
    many = []
    for i in range(60):  # a node out of memory: one pod each of 60 Workloads
        p = pod(f"w{i:02d}-1-a", f"w{i:02d}-1")
        p.metadata.namespace = "shop"
        live.append(p)
        killed(core, p.metadata.name, 60)
        many.append(API.model_copy(update={"name": f"w{i:02d}"}))
    first = a.ooms([API, *many])
    assert len(first) == actuator.OOM_REPORTS == 50
    rest = a.ooms([API, *many])  # the next minute: the ones not yet said, each once
    assert len(rest) == 11 and not {x.name for x in first} & {x.name for x in rest}
    assert a.ooms([API, *many]) == []


def test_an_actuator_ignores_what_a_newer_hub_adds_to_its_answer() -> None:
    newer = {**desired(1), "oom": [API.model_dump() | {"since": 1}], "later": [{"x": 1}]}
    got = actuator.Desired.model_validate(newer)
    assert got.oom == [API] and not hasattr(got, "later")


# --- slice 13.1b: the Step-up (ADR 0005) -------------------------------------------------------


@pytest.mark.parametrize(
    ("limit", "asks", "top", "step"),
    [
        (256, 128, 512, ("356Mi", "178Mi")),  # 100 MiB more is the larger
        (1024, 512, 2048, ("1229Mi", "615Mi")),  # 1.2 times is the larger, in whole Mi
        (1024, 1024, 2048, ("1229Mi", "1229Mi")),  # Guaranteed: the very same quantity
        (456, 228, 512, ("512Mi", "256Mi")),  # the last one stops at the top
        (512, 256, 512, None),  # at it
        (600, 300, 512, None),  # past it (its template's limit was lowered since)
        (300, 100, 512, ("400Mi", "134Mi")),  # the request's factor, rounded up
        (256, 128, 511.5, ("356Mi", "178Mi")),
        (500, 250, 511.5, ("511Mi", "256Mi")),  # a top of no whole Mi: never past it
    ],
)
def test_one_step_up_is_the_larger_of_a_fifth_and_100_mib_never_past_its_top(
    limit: float, asks: float, top: float, step: Any
) -> None:
    mi = Decimal(MI)
    assert actuator.next_step(limit * mi, asks * mi, Decimal(str(top)) * mi) == step


def limited(limit: str = "256Mi", request: str | None = "128Mi", **kw: Any) -> tuple[Any, Any]:
    """api with a memory limit (and request) in its template and on its two pods."""
    requests = {"cpu": "1"} | ({"memory": request} if request else {})
    core, apps = cluster(limits={"memory": limit}, requests=requests, **kw)
    for p in core.list_namespaced_pod.return_value.items[:2]:
        p.spec.containers[0].resources = k.V1ResourceRequirements(
            requests=dict(requests), limits={"memory": limit}
        )
    return core, apps


def stepping(core: Any, apps: Any, top: float = 2.0, **kw: Any) -> actuator.Actuator:
    a = looking(core, apps, **kw)
    a.oom_max = top
    return a


def kill(core: Any, name: str, ago: float = 30, **kw: Any) -> Any:
    """`name` OOM-killed `ago` seconds ago at the memory limit it has now."""
    p = next(p for p in core.list_namespaced_pod.return_value.items if p.metadata.name == name)
    return killed(core, name, ago, limit=p.spec.containers[0].resources.limits["memory"], **kw)


def memory(core: Any, name: str) -> tuple[str | None, str | None]:
    p = next(p for p in core.list_namespaced_pod.return_value.items if p.metadata.name == name)
    r = p.spec.containers[0].resources
    return (r.limits or {}).get("memory"), (r.requests or {}).get("memory")


def test_a_kill_at_its_limit_steps_every_pods_limit_and_request_up_in_place() -> None:
    core, apps = limited()
    a = stepping(core, apps)
    kill(core, "api-1-a")
    (up,) = a.ooms([API])
    assert (up.outcome, up.pods, up.memory, up.cpu, up.run_id) == (
        "stepped_up",
        2,
        356.0 * MI,
        None,
        0,
    )
    assert up.reason == (
        "memory limit 256Mi to 356Mi, request 128Mi to 178Mi on 2 of 2 pods in place: OOM-killed: "
        "api-1-a with a memory limit of 256Mi, its node not out of memory (container-limit)"
    )
    grown = {"limits": {"memory": "356Mi"}, "requests": {"memory": "178Mi"}}
    wanted_ = {"spec": {"containers": [{"name": "app", "resources": grown}]}}
    assert resized(core) == [("api-1-a", wanted_), ("api-1-b", wanted_)]  # the pod killed or not
    assert memory(core, "api-1-a") == memory(core, "api-1-b") == ("356Mi", "178Mi")
    assert (
        core.list_namespaced_pod.return_value.items[0].spec.containers[0].resources.requests["cpu"]
        == "1"
    )
    st = a.stepped[KEY]
    assert (st["limit"], st["request"], st["steps"]) == ("356Mi", "178Mi", 1)
    assert st["template"] == {"limit": "256Mi", "request": "128Mi"} and a.steps == [st["at"]]
    said = core.create_namespaced_event.call_args.args[1]
    assert (said.reason, said.type) == ("MemorySteppedUp", "Normal")
    assert actuator.to_tell(up.outcome, up.reason)  # one the Operator hears of (slice 13.1c)
    assert said.message.startswith("app: memory limit 256Mi to 356Mi, request 128Mi to 178Mi")


def test_a_further_step_needs_a_kill_at_the_step_ten_minutes_on_and_stops_at_its_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core, apps = limited()
    a = stepping(core, apps)
    kill(core, "api-1-a")
    a.ooms([API])
    clock = time.time()

    def later(seconds: float) -> None:
        monkeypatch.setattr(actuator.time, "time", lambda: clock + seconds)
        a.oom_said.clear()

    later(60)  # its restart: the same kill, from before the step every pod has now
    (same_kill,) = a.ooms([API])
    assert same_kill.outcome == "oom_killed" and same_kill.reason.endswith(
        "No Step-up: these kills were before its last Step-up, which every pod has"
    )
    a.oom_said.clear()
    kill(core, "api-1-a", 5)  # killed again, at 356Mi
    (soon,) = a.ooms([API])
    assert soon.reason.endswith(
        "No Step-up: stepped up 1 minutes ago: one a container every 10 minutes"
    )
    assert len(resized(core)) == 2 and a.stepped[KEY]["steps"] == 1
    later(601)
    kill(core, "api-1-a", 5)
    (second,) = a.ooms([API])
    assert second.outcome == "stepped_up" and second.reason.startswith(
        "memory limit 356Mi to 456Mi, request 178Mi to 228Mi on 2 of 2 pods in place"
    )
    later(1202)
    kill(core, "api-1-b", 5)
    (third,) = a.ooms([API])
    assert third.reason.startswith("memory limit 456Mi to 512Mi, request 228Mi to 256Mi on 2 of 2")
    assert a.stepped[KEY]["steps"] == 3 and len(a.steps) == 3
    later(1803)
    kill(core, "api-1-b", 5)
    (capped,) = a.ooms([API])
    assert capped.outcome == "oom_killed" and capped.reason.endswith(
        "No Step-up: it is at 2 times its template's limit (512Mi): raise it in its manifest, "
        "or look for a leak"
    )
    assert memory(core, "api-1-a") == ("512Mi", "256Mi") and len(resized(core)) == 6
    assert actuator.to_tell(capped.outcome, capped.reason)  # a person's to answer: told
    assert not actuator.to_tell(soon.outcome, soon.reason)  # another reason: only stored


def test_a_pod_killed_below_the_step_is_brought_to_it_never_a_further_step() -> None:
    core, apps = limited()
    a = stepping(core, apps)
    kill(core, "api-1-a")
    a.ooms([API])
    before = dict(a.stepped[KEY])
    new = pod("api-1-c", "api-1", cpu="1", memory="128Mi")  # from its template since
    new.metadata.namespace = "shop"
    new.spec.containers[0].resources.limits = {"memory": "256Mi"}
    core.list_namespaced_pod.return_value.items.append(new)
    kill(core, "api-1-c", 5)
    a.oom_said.clear()
    (level,) = a.ooms([API])
    assert level.outcome == "stepped_up" and level.pods == 1 and level.memory == 356.0 * MI
    assert level.reason.startswith(
        "brought to its Step-up, memory limit 356Mi, request 178Mi on 1 of 3 pods in place: "
    )
    assert memory(core, "api-1-c") == ("356Mi", "178Mi")
    assert a.stepped[KEY] == before and len(a.steps) == 1  # no step, none of the hour's budget
    assert not actuator.to_tell(level.outcome, level.reason)  # nor one the Operator is told of


@pytest.mark.parametrize(
    ("why_not", "set_up"),
    [
        ("the Workload is paused", "paused"),
        ("its resizePolicy restarts the container for memory", "policy"),
        ("a VPA in mode Auto sizes it", "vpa"),
        ("an HPA scales on its memory", "hpa"),
        ("its template sets no memory limit", "no_limit"),
        ("the cluster's 10 Step-ups an hour are used", "budget"),
        ("memory 356Mi is over its LimitRange's maximum 300Mi", "limit_range"),
        ("2 pods at memory 356Mi would pass ResourceQuota team's limits.memory 1Gi", "limits"),
        (
            "2 pods at memory 178Mi would pass ResourceQuota team's requests.memory 300Mi",
            "requests",
        ),
        ("reading it: 503: busy", "busy"),
    ],
)
def test_what_keeps_a_step_up_from_being_made_is_said_with_the_kill(
    why_not: str, set_up: str
) -> None:
    restart = [k.V1ContainerResizePolicy(resource_name="memory", restart_policy="RestartContainer")]
    core, apps = limited(policy=restart if set_up == "policy" else None)
    a = stepping(core, apps)
    template = apps.read_namespaced_deployment.return_value
    if set_up == "paused":
        template.metadata.annotations = {actuator.PAUSE: "true"}
    elif set_up == "vpa":
        vpa = {"spec": {"targetRef": {"kind": "Deployment", "name": "api"}}}
        a.custom = scalers(vpas=[vpa])[1]
    elif set_up == "hpa":
        a.autoscaling = scalers(hpas=[hpa("api", "memory")])[0]
    elif set_up == "no_limit":
        template.spec.template.spec.containers[0].resources.limits = None
    elif set_up == "budget":
        a.steps = [time.time() - 3599] * 10
    elif set_up == "limit_range":
        core.list_namespaced_limit_range.return_value = limit_range(max={"memory": "300Mi"})
    elif set_up == "limits":
        used = quota({"limits.memory": "1Gi"}, {"limits.memory": "900Mi"})
        core.list_namespaced_resource_quota.return_value = used
    elif set_up == "requests":
        used = quota({"requests.memory": "300Mi"}, {"requests.memory": "256Mi"})
        core.list_namespaced_resource_quota.return_value = used
    elif set_up == "busy":
        apps.list_namespaced_replica_set.side_effect = ApiException(status=503, reason="busy")
    kill(core, "api-1-a")
    (seen,) = a.ooms([API])
    assert seen.outcome == "oom_killed" and seen.reason == (
        "OOM-killed: api-1-a with a memory limit of 256Mi, its node not out of memory "
        f"(container-limit). No Step-up: {why_not}"
    )
    assert resized(core) == [] and a.stepped == {} and len(a.steps) in (0, 10)
    if set_up == "budget":  # an hour on, its ten are old
        a.steps = [time.time() - 3601] * 10
        a.oom_said.clear()
        assert a.ooms([API])[0].outcome == "stepped_up" and len(a.steps) == 1


def test_a_guaranteed_pods_limit_and_request_stay_the_very_same() -> None:
    core, apps = limited("1Gi", "1Gi")
    a = stepping(core, apps)
    kill(core, "api-1-a")
    (up,) = a.ooms([API])
    assert up.reason.startswith("memory limit 1024Mi to 1229Mi, request 1024Mi to 1229Mi")
    assert memory(core, "api-1-a") == memory(core, "api-1-b") == ("1229Mi", "1229Mi")


def test_one_pods_refusal_or_a_lost_pod_leaves_the_others_their_step() -> None:
    core, apps = limited()
    a = stepping(core, apps)
    extra = pod("api-1-c", "api-1", cpu="1", memory="128Mi")
    extra.metadata.namespace = "shop"
    extra.spec.containers[0].resources.limits = {"memory": "256Mi"}
    core.list_namespaced_pod.return_value.items.append(shown(extra, NODE_GONE))
    grow = core.patch_namespaced_pod_resize.side_effect

    def refusing(name: str, ns: str, body_: Any) -> None:
        if name == "api-1-a":  # the first it tries
            raise ApiException(status=403, reason="its node has no room")
        grow(name, ns, body_)

    core.patch_namespaced_pod_resize.side_effect = refusing
    kill(core, "api-1-b")
    (up,) = a.ooms([API])
    assert (
        up.outcome == "stepped_up"
        and up.pods == 1
        and up.reason.startswith(
            "memory limit 256Mi to 356Mi, request 128Mi to 178Mi on 1 of 2 pods in place; "
            "1 refused: api-1-a: 403: its node has no room: OOM-killed: "
        )
    )
    assert [n for n, _ in resized(core)] == ["api-1-a", "api-1-b"]  # never the lost api-1-c
    assert memory(core, "api-1-b")[0] == "356Mi" and memory(core, "api-1-c")[0] == "256Mi"
    assert memory(core, "api-1-a")[0] == "256Mi"


def holding(a: actuator.Actuator, memory_: str | None, had: str | None = "128Mi") -> dict[str, Any]:
    """api held applied at cpu 115m and this memory request, its template's then `had`."""
    requests = {"cpu": "115m"} | ({"memory": memory_} if memory_ else {})
    template: dict[str, Any] = {"cpu": "1"} | ({"memory": had} if memory_ else {})
    a.held[KEY] = {"run_id": 7, "outcome": "applied", "requests": requests, "template": template}
    return a.held[KEY]


@pytest.mark.parametrize(
    ("held", "asks"),
    [("200Mi", "279Mi"), ("96Mi", "178Mi"), (None, "178Mi")],  # raised; lowered; cpu only
)
def test_a_step_up_takes_the_containers_memory_from_its_sizing_never_a_lowered_request(
    held: str | None, asks: str
) -> None:
    core, apps = limited()
    a = stepping(core, apps)
    h = holding(a, held)
    for name in ("api-1-a", "api-1-b"):  # as the Sizing left them
        p = next(p for p in core.list_namespaced_pod.return_value.items if p.metadata.name == name)
        p.spec.containers[0].resources.requests = {"cpu": "115m", "memory": held or "128Mi"}
    kill(core, "api-1-a")
    (up,) = a.ooms([API])
    assert up.outcome == "stepped_up" and up.run_id == 7
    assert a.stepped[KEY]["request"] == asks and memory(core, "api-1-b") == ("356Mi", asks)
    assert h["requests"] == {"cpu": "115m"} and h["template"] == {"cpu": "1"}  # cpu stays held
    assert h["outcome"] == "applied" and not actuator.lowered(h)


@pytest.mark.parametrize(
    ("held", "outcome", "after"),
    [
        ("96Mi", "rolled_back", "rolled_back"),  # it had lowered the request: its doing, maybe
        ("200Mi", "oom_killed", "applied"),  # raised: only said
        (None, "oom_killed", "applied"),
        (
            "only-a-limit",
            "rolled_back",
            "rolled_back",
        ),  # 96Mi where its template's limit asked 256Mi
    ],
)
def test_a_node_level_kill_rolls_back_a_sizing_that_lowered_its_memory_else_is_only_said(
    held: str | None, outcome: str, after: str
) -> None:
    core, apps = limited()
    a = stepping(core, apps)
    h = holding(a, "96Mi", None) if held == "only-a-limit" else holding(a, held)
    kill(core, "api-1-a", 60)
    system_oom(core, 70.0)
    (got,) = a.ooms([API])
    assert got.outcome == outcome and h["outcome"] == after and a.stepped == {}
    assert "api-1-a with its node n1 out of memory (node-level: a SystemOOM Event)" in got.reason
    if outcome == "rolled_back":
        assert got.reason.startswith("OOM-killed: api-1-a with its node n1 out of memory")
        assert "no new Sizing of it until" in got.reason and h["rollbacks"]
        a.oom_said.clear()
        system_oom(core)  # the same kill seen again, at its limit this time
        (again,) = a.ooms([API])
        assert again.outcome == "oom_killed" and a.stepped == {}  # answered by that Rollback


def reflexing(top: float | None = 2.0, *runs: int) -> dict[str, Any]:
    more = {} if top is None else {"oom_max": top}
    return {**desired(*runs), "oom": [API.model_dump()], **more}


def test_in_a_watch_hour_a_kill_at_its_limit_is_a_step_up_not_a_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core, apps = limited(request="64Mi")  # the Sizing raises its memory request: 96Mi
    a = settled(monkeypatch, [reflexing(2.0, 1)] * 2, core, apps)
    system_oom(core)
    p = kill(core, "api-1-a")
    p.status.container_statuses[0].restart_count = 3  # crash-looping, NotReady for long
    shown(p, PROBE_FAILS, since=time.time() - 300)
    a.step()
    assert posted(a.seen)[-1] == ["stepped_up"] and a.held[KEY]["outcome"] == "applied"
    assert a.held[KEY]["requests"] == {"cpu": "115m"}  # its memory is the Step-up's now
    assert memory(core, "api-1-b") == ("356Mi", "134Mi")  # the Sizing's 96Mi, a step up
    a.step()
    assert posted(a.seen) == [["applied"], ["stepped_up"]]  # nothing more the minute after
    crashed = ended(core.list_namespaced_pod.return_value.items[1], 1, "Error", 1)
    crashed.spec.containers[0].resources.requests = {"cpu": "115m", "memory": "134Mi"}
    a.http = hub_of([reflexing(2.0, 1)], a.seen)
    a.step()  # a crash is still the Sizing's to answer for: only an OOM kill is the Reflex's
    assert posted(a.seen)[-1] == ["rolled_back"] and "api-1-b: app restarted" in str(
        json.loads(a.seen[-1].content)[0]["reason"]
    )


def test_in_a_watch_hour_a_kill_under_a_lowered_memory_request_is_a_rollback_never_a_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core, apps = limited()  # its template asks 128Mi, the Sizing 96Mi
    a = settled(monkeypatch, [reflexing(2.0, 1)], core, apps)
    system_oom(core)
    p = kill(core, "api-1-a")
    p.status.container_statuses[0].restart_count = 1
    a.step()
    assert posted(a.seen)[-1] == ["rolled_back", "oom_killed"] and a.stepped == {}
    assert a.held[KEY]["outcome"] == "rolled_back"


@pytest.mark.parametrize("never_looked", ["hub_of_13_1a", "reports_waiting", "not_listed"])
def test_a_kill_the_reflex_never_looked_at_is_a_rollback_as_before(
    monkeypatch: pytest.MonkeyPatch, never_looked: str
) -> None:
    core, apps = limited(request=None)
    answer = reflexing(None, 1) if never_looked == "hub_of_13_1a" else reflexing(2.0, 1)
    if never_looked == "not_listed":
        answer["oom"] = []
    a = settled(monkeypatch, [answer], core, apps)
    system_oom(core)
    p = kill(core, "api-1-a")
    p.status.container_statuses[0].restart_count = 1
    if never_looked == "reports_waiting":
        a.unsent.append(actuator.Actuation(**report()))
    a.step()
    assert "rolled_back" in posted(a.seen)[-1] and "stepped_up" not in posted(a.seen)[-1]
    assert a.stepped == {} and a.held[KEY]["outcome"] == "rolled_back"


STEP = {
    "limit": "356Mi",
    "request": "178Mi",
    "template": {"limit": "256Mi", "request": "128Mi"},
    "at": 1.0,
    "steps": 1,
}


def test_a_sizing_leaves_the_memory_of_a_container_a_step_up_holds_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core, apps = limited()
    a = settled(monkeypatch, [], core, apps)
    a.stepped[KEY] = dict(STEP)
    del a.held[KEY]["requests"]["memory"], a.held[KEY]["template"]["memory"]  # as a step does
    for p in core.list_namespaced_pod.return_value.items[:2]:  # and its pods at the step
        p.spec.containers[0].resources.limits = {"memory": "356Mi"}
    both = wanted(2, cpu=0.5, memory=64 * MI).model_dump()
    only = wanted(3, cpu=None, memory=64 * MI).model_dump()
    a.http = hub_of([{**desired(), "sizings": [both]}, {**desired(), "sizings": [only]}], a.seen)
    before = len(resized(core))
    a.step()
    (_, sent) = resized(core)[before]
    assert sent == body(cpu="500m")  # its cpu, and the memory left alone
    assert a.held[KEY]["notes"] == "; memory left to its Step-up"  # said when it ends
    a.step()
    (said,) = json.loads(a.seen[-1].content)
    assert (said["run_id"], said["outcome"]) == (3, "skipped") and said["reason"].startswith(
        "it asks memory only, which is its Step-up's while it lasts"
    )
    assert a.held[KEY]["run_id"] == 2  # what it holds stays


def test_its_step_ups_and_the_hours_count_outlive_a_restart() -> None:
    core, apps = limited()
    a = stepping(core, apps)
    kill(core, "api-1-a")
    a.ooms([API])
    a.save()
    saved = core.replace_namespaced_config_map.call_args.args[2].data
    state = json.loads(saved[actuator.STATE])
    assert state["stepped"] == a.stepped and state["steps"] == a.steps
    core.read_namespaced_config_map.return_value = k.V1ConfigMap(data=saved)
    b = make(hub_of([], []), core, apps)
    b.load()
    assert b.stepped == a.stepped and b.steps == a.steps
    plain = make(hub_of([], []), *cluster())  # none: its state reads as before this slice
    assert set(json.loads(plain.state())) == {"killed", "unsent"}


def test_a_template_changed_since_makes_the_step_up_no_longer_its_own() -> None:
    core, apps = limited()
    a = stepping(core, apps)
    kill(core, "api-1-a")
    a.ooms([API])
    template = apps.read_namespaced_deployment.return_value.spec.template.spec.containers[0]
    template.resources.limits = {"memory": "300Mi"}  # a person raised it in the manifest
    a.oom_said.clear()
    kill(core, "api-1-a", 5)  # its pod still at the step's 356Mi
    (seen,) = a.ooms([API])
    assert seen.reason.endswith("No Step-up: its pods' memory limit is not its template's")
    assert a.stepped == {} and len(resized(core)) == 2


NIGHTLY = actuator.Watched(namespace="shop", kind="CronJob", name="nightly", container="app")
RUNS = "shop/CronJob/nightly/app"


def nightly(parallelism: int | None = None) -> tuple[Any, Any, Any]:
    """limited(), with a CronJob `nightly` of the same template, and a run's pod of it."""
    core, apps = limited()
    d = apps.read_namespaced_deployment.return_value
    apps.read_namespaced_cron_job.return_value = k.V1CronJob(
        metadata=k.V1ObjectMeta(name="nightly", namespace="shop", uid="u9"),
        spec=k.V1CronJobSpec(
            schedule="* * * * *",
            job_template=k.V1JobTemplateSpec(
                spec=k.V1JobSpec(template=d.spec.template, parallelism=parallelism)
            ),
        ),
    )
    p = core.list_namespaced_pod.return_value.items[0]
    return core, apps, p


def run_killed(core: Any, p: Any, ago: float = 30) -> None:
    kill(core, p.metadata.name, ago, how="state")
    p.metadata.labels = {}
    p.metadata.owner_references = [owner("Job", f"nightly-{int(time.time() // 60)}")]
    p.status.phase = "Failed"


def test_a_cronjobs_step_up_is_for_its_next_runs_pods_never_in_place() -> None:
    core, apps, p = nightly(parallelism=3)
    a = stepping(core, apps)
    run_killed(core, p)
    (up,) = a.ooms([NIGHTLY])
    assert (up.outcome, up.kind, up.pods, up.memory) == ("stepped_up", "CronJob", 0, 356.0 * MI)
    assert up.reason.startswith(
        "memory limit 256Mi to 356Mi, request 128Mi to 178Mi for its next runs' pods, given them "
        "as they start: OOM-killed: api-1-a with a memory limit of 256Mi"
    )
    assert resized(core) == [] and a.stepped[RUNS]["limit"] == "356Mi" and len(a.steps) == 1
    a.oom_said.clear()
    (again,) = a.ooms([NIGHTLY])  # the same run's kill, from before the step
    assert again.outcome == "oom_killed"
    assert again.reason.endswith("No Step-up: its next runs' pods start at its Step-up")
    # a quota counts its parallelism: 3 pods a run
    core, apps, p = nightly(parallelism=3)
    used = quota({"limits.memory": "1Gi"}, {"limits.memory": "800Mi"})
    core.list_namespaced_resource_quota.return_value = used
    a = stepping(core, apps)
    run_killed(core, p)
    assert a.ooms([NIGHTLY])[0].reason.endswith(
        "No Step-up: 3 pods at memory 356Mi would pass ResourceQuota team's limits.memory 1Gi"
    )


@pytest.mark.parametrize(
    ("reflexed", "held", "rolled_back"),
    [(True, "200Mi", False), (True, "96Mi", True), (False, "200Mi", True)],
)
def test_a_watched_runs_oom_kill_is_the_reflexes_unless_its_sizing_lowered_the_memory(
    reflexed: bool, held: str, rolled_back: bool
) -> None:
    core, apps = cronned()
    t0 = time.time() - 600
    h = {
        "run_id": 1,
        "outcome": "applied",
        "requests": {"cpu": "115m", "memory": held},
        "template": {"cpu": "1", "memory": "128Mi"},
        "first_run": {"after": t0, "until": t0 + 7 * 86400},
    }
    bad = pod("api-3-a", "", "Failed", cpu="115m", memory=held)
    bad.spec.containers[0].resources.limits = {"memory": "256Mi"}
    st = status("app", None)
    ended_ = k.V1ContainerStateTerminated(exit_code=137, reason="OOMKilled")
    st.state = k.V1ContainerState(terminated=ended_)
    bad.status.container_statuses = [st]
    job(core, apps, "api-3", t0 + 60, bad)
    reason = actuator.first_run(core, apps, CRON, dict(h), time.time(), reflexed)
    assert reason == ("api-3-a: app was OOM-killed" if rolled_back else None)
    act = make(hub_of([], []), core, apps, held={CRON: h})  # and as its minute's watch asks
    act.reflexed = frozenset({CRON} if reflexed else ())
    assert [a.outcome for a in act.watch()] == (["rolled_back"] if rolled_back else [])


def test_a_revert_drops_the_containers_step_up_with_its_sizing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core, apps = limited(request="64Mi")
    a = settled(monkeypatch, [], core, apps)
    a.stepped[KEY] = {"limit": "356Mi", "request": "178Mi", "template": {}, "at": 1.0, "steps": 1}
    undo = actuator.Undo(id=4, namespace="shop", kind="Deployment", name="api", container="app")
    (done,) = a.revert([undo])
    assert done.outcome == "reverted" and a.stepped == {}
    assert (
        "; its Step-up dropped: its pods keep their memory until they start again, its memory "
        "peak not known to be within its template's limit"
    ) in done.reason
    a.stepped[KEY] = dict(STEP)  # reverted again, the Hub's peak within its template's 256Mi
    a.peaks = {KEY: 200.0 * MI}
    for p in core.list_namespaced_pod.return_value.items[:2]:
        p.spec.containers[0].resources.limits = {"memory": "356Mi"}
    before = len(resized(core))
    (done,) = a.revert([undo.model_copy(update={"id": 5})])
    assert (
        "; its Step-up dropped: 2 of 2 pods back to memory limit 256Mi, request 128Mi in place"
    ) in done.reason
    assert [b for _, b in resized(core)[before:]] == [
        actuator.step_body("app", "256Mi", "128Mi")
    ] * 2


def test_only_a_kill_since_it_got_its_requests_is_the_reflexes_an_old_one_excuses_nothing() -> None:
    core, apps = limited()
    h = watched(core, apps)  # resized at 1000, each pod with no restart then
    b = kill(core, "api-1-b", 5)  # OOM-killed last week, as its status still says
    b.spec.containers[0].resources.requests = {"cpu": "115m", "memory": "128Mi"}
    shown(b, PROBE_FAILS, since=1500.0)  # and NotReady since its resize
    assert actuator.trouble(core, apps, KEY, h, 2000.0, True) == "api-1-b: NotReady for 2 minutes"
    b.status.container_statuses[0].restart_count = 1  # killed since: the Reflex's to answer
    assert actuator.trouble(core, apps, KEY, h, 2000.0, True) is None
    assert actuator.trouble(core, apps, KEY, h, 2000.0) == "api-1-b: app was OOM-killed"


def test_a_pod_asked_the_step_but_never_given_it_earns_no_further_one() -> None:
    core, apps = limited()
    a = stepping(core, apps)
    kill(core, "api-1-a")
    a.ooms([API])
    before = dict(a.stepped[KEY])
    p = kill(core, "api-1-b", -5)  # killed since the step, its spec asking 356Mi
    given = k.V1ResourceRequirements(limits={"memory": "256Mi"}, requests={"memory": "128Mi"})
    p.status.container_statuses[0].resources = given  # its node had no room: still 256Mi
    a.oom_said.clear()
    (level,) = a.ooms([API])
    assert level.outcome == "stepped_up" and level.pods == 1
    assert level.reason.startswith("brought to its Step-up, memory limit 356Mi, request 178Mi")
    assert a.stepped[KEY] == before and len(a.steps) == 1


def test_a_step_no_pod_took_is_none_and_one_is_kept_before_a_pod_is_touched() -> None:
    core, apps = limited()
    a = stepping(core, apps)
    kept: list[bool] = []

    def refusing(name: str, ns: str, body_: Any) -> None:
        kept.append(KEY in json.loads(a.state()).get("stepped", {}))
        kept.append(core.replace_namespaced_config_map.called)  # and saved: a restart knows
        raise ApiException(status=403, reason="its node has no room")

    core.patch_namespaced_pod_resize.side_effect = refusing
    kill(core, "api-1-a")
    (seen,) = a.ooms([API])
    assert kept == [True] * 4
    assert seen.outcome == "oom_killed" and seen.reason.endswith(
        "No Step-up: every pod refused it: api-1-a: 403: its node has no room; "
        "api-1-b: 403: its node has no room"
    )
    assert a.stepped == {} and a.steps == []  # not kept, nor one of the hour's ten


def test_no_step_up_while_the_containers_sizing_still_rolls_out() -> None:
    core, apps = limited(request="64Mi")
    a = stepping(core, apps)
    _, a.held[KEY] = start(core, apps, wanted())
    assert a.held[KEY]["outcome"] == "rolling"
    before = len(resized(core))
    kill(core, "api-1-b")
    (seen,) = a.ooms([API])
    assert seen.reason.endswith("No Step-up: its Sizing is still rolling out")
    assert len(resized(core)) == before and a.held[KEY]["requests"]["memory"] == "96Mi"


def test_a_kill_the_reflex_looks_at_and_declines_is_said_and_no_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core, apps = limited(request="64Mi")
    a = settled(monkeypatch, [reflexing(2.0, 1)], core, apps)
    a.autoscaling = scalers(hpas=[hpa("api", "memory")])[0]
    system_oom(core)
    p = kill(core, "api-1-a")
    p.status.container_statuses[0].restart_count = 1
    a.step()
    assert posted(a.seen)[-1] == ["oom_killed"] and a.held[KEY]["outcome"] == "applied"
    assert json.loads(a.seen[-1].content)[0]["reason"].endswith(
        "No Step-up: an HPA scales on its memory"
    )


def stepped_pod(**resources: Any) -> dict[str, Any]:
    """A new pod of api, its container `app` asking this memory (256Mi and 128Mi unless said)."""
    asks = {"requests": {"cpu": "1", "memory": "128Mi"}, "limits": {"memory": "256Mi"}}
    return new_pod(**(resources or asks))["request"]


def test_a_new_pod_starts_at_its_containers_step_up_its_limit_and_request_both() -> None:
    stepped = {KEY: dict(STEP)}
    ops = actuator.mutation(stepped_pod(), {}, "squero-actuator", stepped=stepped)
    at = "/spec/containers/1/resources"
    assert ops == [
        {"op": "add", "path": f"{at}/requests/memory", "value": "178Mi"},
        {"op": "add", "path": f"{at}/limits/memory", "value": "356Mi"},
    ]
    held = {KEY: {"outcome": "applied", "requests": {"cpu": "115m"}, "template": {"cpu": "1"}}}
    ops = actuator.mutation(stepped_pod(), held, "squero-actuator", stepped=stepped)
    assert ops[0] == {"op": "add", "path": f"{at}/requests/cpu", "value": "115m"}  # its Sizing's
    assert [o["path"].rsplit("/", 2)[1:] for o in ops[1:]] == [
        ["requests", "memory"],
        ["limits", "memory"],
    ]
    assert (
        actuator.mutation(stepped_pod(), held, "squero-actuator") == ops[:1]
    )  # none held: as before
    # the Kill switch: nothing, as for a Sizing's requests
    review = {"request": stepped_pod()}
    answer = actuator.admit(review, {}, True, "squero-actuator", frozenset(), stepped)
    assert "patch" not in answer["response"]
    answer = actuator.admit(review, {}, False, "squero-actuator", frozenset(), stepped)
    assert json.loads(base64.b64decode(answer["response"]["patch"])) == ops[1:]
    paused = frozenset({"shop/Deployment/api"})
    assert actuator.mutation(stepped_pod(), {}, "squero-actuator", paused, stepped) == []


@pytest.mark.parametrize(
    ("resources", "kind", "ops"),
    [
        # a template with only a limit asks that much: defaulted on the pod, or not yet
        ({"requests": {"cpu": "1", "memory": "256Mi"}, "limits": {"memory": "256Mi"}}, "", 2),
        ({"requests": {"cpu": "1"}, "limits": {"memory": "256Mi"}}, "", 2),
        # a person changed its template's memory since: theirs
        ({"requests": {"cpu": "1", "memory": "128Mi"}, "limits": {"memory": "300Mi"}}, "", 0),
        ({"requests": {"cpu": "1", "memory": "100Mi"}, "limits": {"memory": "256Mi"}}, "", 0),
        # a DaemonSet's new pod: a raise reaches it in place only
        (
            {"requests": {"cpu": "1", "memory": "256Mi"}, "limits": {"memory": "256Mi"}},
            "DaemonSet",
            0,
        ),
    ],
)
def test_a_new_pod_gets_the_step_up_only_from_the_template_it_started_from(
    resources: Any, kind: str, ops: int
) -> None:
    st = {**STEP, "template": {"limit": "256Mi", "request": None}, "request": "356Mi"}
    if resources["requests"].get("memory") in ("128Mi", "100Mi"):
        st = dict(STEP)
    req = stepped_pod(**resources)
    key = KEY
    if kind:
        req["object"]["metadata"]["ownerReferences"] = [{"kind": kind, "name": "api"}]
        key = f"shop/{kind}/api/app"
    got = actuator.mutation(req, {}, "squero-actuator", stepped={key: st})
    assert len(got) == ops
    if ops:
        assert got[-1] == {
            "op": "add",
            "path": "/spec/containers/1/resources/limits/memory",
            "value": "356Mi",
        }


def test_a_guaranteed_pod_stays_guaranteed_at_its_step_up() -> None:
    equal = {"requests": {"cpu": "1", "memory": "1Gi"}, "limits": {"cpu": "1", "memory": "1Gi"}}
    st = {**STEP, "limit": "1229Mi", "request": "1229Mi"}
    st["template"] = {"limit": "1Gi", "request": "1Gi"}
    req = new_pod(sidecar={"resources": equal}, **equal)["request"]
    ops = actuator.mutation(req, {}, "squero-actuator", stepped={KEY: st})
    assert [o["value"] for o in ops] == ["1229Mi", "1229Mi"]
    apart = {**st, "request": "1000Mi"}  # one that would leave it Burstable: never given
    assert actuator.mutation(req, {}, "squero-actuator", stepped={KEY: apart}) == []


def test_a_cronjobs_next_run_starts_at_its_step_up() -> None:
    req = stepped_pod()
    minute = int(time.time() // 60)
    req["object"]["metadata"]["labels"] = {}
    req["object"]["metadata"]["ownerReferences"] = [{"kind": "Job", "name": f"nightly-{minute}"}]
    ops = actuator.mutation(req, {}, "squero-actuator", stepped={RUNS: dict(STEP)})
    assert [o["value"] for o in ops] == ["178Mi", "356Mi"]


def kept(core: Any, apps: Any, **kw: Any) -> actuator.Actuator:
    """An Actuator holding api's Step-up to 356Mi, its two pods at it."""
    a = stepping(core, apps, **kw)
    a.stepped[KEY] = dict(STEP)
    for p in core.list_namespaced_pod.return_value.items[:2]:
        p.spec.containers[0].resources = k.V1ResourceRequirements(
            requests={"cpu": "1", "memory": "178Mi"}, limits={"memory": "356Mi"}
        )
    return a


def test_each_minute_a_pod_asking_less_than_the_step_up_is_brought_to_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    core, apps = limited()
    a = kept(core, apps)
    assert a.keep_steps() == [] and resized(core) == []  # every pod asks it
    live = core.list_namespaced_pod.return_value.items
    for name in ("api-1-c", "api-1-d", "api-1-e"):  # from its template: the webhook was down
        new = pod(name, "api-1", cpu="1", memory="128Mi")
        new.metadata.namespace = "shop"
        new.spec.containers[0].resources.limits = {"memory": "256Mi"}
        live.append(new)
    shown(live[-1], NODE_GONE)  # api-1-e: its node gone
    grow = core.patch_namespaced_pod_resize.side_effect

    def refusing(name: str, ns: str, body_: Any) -> None:
        if name == "api-1-d":
            raise ApiException(status=403, reason="its node has no room")
        grow(name, ns, body_)

    core.patch_namespaced_pod_resize.side_effect = refusing
    with caplog.at_level("WARNING"):
        (up,) = a.keep_steps()
    assert (up.outcome, up.pods, up.memory) == ("stepped_up", 1, 356.0 * MI)
    assert up.reason == (
        "brought to its Step-up, memory limit 356Mi, request 178Mi on 1 of 4 pods in place"
    )
    assert [n for n, _ in resized(core)] == ["api-1-c", "api-1-d"]  # never the lost api-1-e
    assert memory(core, "api-1-c") == ("356Mi", "178Mi") and "api-1-d" in caplog.text
    assert a.keep_steps() == []  # api-1-d refuses again: tried, logged, not reported
    assert len(a.steps) == 0 and a.stepped[KEY] == STEP  # no step, none of the hour's ten


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("limit", "its template's memory changed: limit 256Mi to 300Mi: its Step-up dropped"),
        ("request", "its template's memory changed: request 128Mi to unset: its Step-up dropped"),
        ("container", "no container 'app' in its template: its Step-up dropped"),
        ("gone", "no such Deployment: its Step-up dropped"),
        ("busy", None),
        ("paused", None),
    ],
)
def test_a_step_up_is_handed_back_when_a_person_changes_its_templates_memory(
    change: str, reason: str | None
) -> None:
    core, apps = limited()
    a = kept(core, apps)
    template = apps.read_namespaced_deployment.return_value.spec.template.spec.containers[0]
    if change == "limit":
        template.resources.limits = {"memory": "300Mi"}
    elif change == "request":
        template.resources.requests = {"cpu": "1"}
    elif change == "container":
        template.name = "web"
    elif change == "gone":
        apps.read_namespaced_deployment.side_effect = ApiException(status=404)
    elif change == "busy":
        apps.read_namespaced_deployment.side_effect = ApiException(status=503, reason="busy")
    elif change == "paused":
        a.paused = frozenset({"shop/Deployment/api"})
        template.resources.limits = {"memory": "300Mi"}  # a paused Workload's is left alone
    got = a.keep_steps()
    if reason is None:
        assert got == [] and a.stepped[KEY] == STEP
        return
    (back_,) = got
    assert (back_.outcome, back_.pods, back_.reason) == ("step_ended", 0, reason)
    assert a.stepped == {} and resized(core) == []
    said = core.create_namespaced_event.call_args.args[1]
    assert (said.reason, said.message) == ("HandedBack", f"app: {reason}")


def test_a_minute_keeps_its_step_ups_and_reads_their_workloads_pause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core, apps = limited()
    a = kept(core, apps)
    late = pod("api-1-c", "api-1", cpu="1", memory="128Mi")
    late.metadata.namespace = "shop"
    late.spec.containers[0].resources.limits = {"memory": "256Mi"}
    core.list_namespaced_pod.return_value.items.append(late)
    seen: list[httpx.Request] = []
    a.http = hub_of([reflexing(), desired(), desired(killed=True)], seen)
    note = {actuator.PAUSE: "true"}
    apps.list_namespaced_deployment.return_value.items = [
        MagicMock(metadata=k.V1ObjectMeta(name="api", annotations=note))
    ]
    a.step()  # paused: its Step-up waits
    assert posted(seen) == [] and "shop/Deployment/api" in a.paused
    apps.list_namespaced_deployment.return_value.items = []
    a.step()  # the Reflex off at the Hub since: what it holds is still kept
    assert posted(seen) == [["stepped_up"]] and memory(core, "api-1-c")[0] == "356Mi"
    late.spec.containers[0].resources.limits = {"memory": "256Mi"}
    a.step()  # the Kill switch: nothing
    assert len(posted(seen)) == 1 and memory(core, "api-1-c")[0] == "256Mi"


def test_a_step_up_only_ever_raises_a_pod_one_above_it_keeps_its_own() -> None:
    core, apps = limited()
    a = stepping(core, apps)
    high = core.list_namespaced_pod.return_value.items[1]  # from a Step-up dropped since
    high.spec.containers[0].resources = k.V1ResourceRequirements(
        requests={"cpu": "1", "memory": "256Mi"}, limits={"memory": "512Mi"}
    )
    kill(core, "api-1-a")
    (up,) = a.ooms([API])
    assert up.outcome == "stepped_up" and up.pods == 1  # api-1-a only
    assert memory(core, "api-1-b") == ("512Mi", "256Mi") and memory(core, "api-1-a")[0] == "356Mi"
    assert a.keep_steps() == [] and [n for n, _ in resized(core)] == ["api-1-a"]


def test_a_resize_its_node_cant_fit_is_put_back_and_the_pod_left_never_tried_again() -> None:
    core, apps = limited()
    a = kept(core, apps)
    b = core.list_namespaced_pod.return_value.items[1]  # asks the step, and was never given it
    waits = k.V1PodCondition(type="PodResizePending", status="True", reason="Infeasible")
    b.status.conditions = [waits]
    given = k.V1ResourceRequirements(limits={"memory": "256Mi"}, requests={"memory": "128Mi"})
    b.status.container_statuses = [status("app", given)]
    (said,) = a.keep_steps()
    assert (said.outcome, said.pods) == ("stepped_up", 0) and said.reason.endswith(
        "on 0 of 2 pods in place; 1 left as they were: their node has no room"
    )
    back_ = {"limits": {"memory": "256Mi"}, "requests": {"memory": "128Mi"}}
    assert resized(core) == [
        ("api-1-b", {"spec": {"containers": [{"name": "app", "resources": back_}]}})
    ]
    assert a.stepped[KEY]["left"] == ["api-1-b"] and memory(core, "api-1-b") == ("256Mi", "128Mi")
    assert a.keep_steps() == [] and len(resized(core)) == 1  # left: not brought to it again
    kill(core, "api-1-b", 5)  # nor by its own kill, which earns no step either
    (seen,) = a.ooms([API])
    assert seen.outcome == "oom_killed" and len(resized(core)) == 1
    assert seen.reason.endswith(
        "No Step-up: 1 of its pods keep their memory, their node without room for its Step-up; "
        "the others have it"
    )
    a.oom_said.clear()
    kill(core, "api-1-a", 5)  # a further step: for the pods that could take the last
    (up,) = a.ooms([API])
    assert up.outcome == "stepped_up" and [n for n, _ in resized(core)][1:] == ["api-1-a"]
    assert a.stepped[KEY]["left"] == ["api-1-b"] and a.stepped[KEY]["limit"] == "456Mi"
    core.list_namespaced_pod.return_value.items.remove(b)  # gone: forgotten
    assert a.keep_steps() == [] and "left" not in a.stepped[KEY]


# --- slice 13.1c: a Step-up's term ----------------------------------------------------------

WEEK = 7 * 86400.0


def aged(core: Any, apps: Any, peak: Any = "unlisted", days: float = 7.0) -> actuator.Actuator:
    """kept(), its Step-up `days` old, the Hub saying a week's term and this memory peak."""
    a = kept(core, apps)
    a.stepped[KEY]["at"] = time.time() - days * 86400
    a.oom_expires = WEEK
    a.peaks = {} if peak == "unlisted" else {KEY: peak}
    return a


def test_a_step_up_still_ends_at_its_term_once_the_reflex_is_switched_off() -> None:
    core, apps = limited()
    a = aged(core, apps, days=6.0)
    assert a.keep_steps() == [] and a.stepped[KEY]["expires"] == WEEK  # its term, kept with it
    a.oom_expires = None  # the Reflex off: the Hub sends no term, and lists no container
    a.stepped[KEY]["at"] -= 86400
    (end,) = a.keep_steps()
    assert end.outcome == "step_ended" and a.stepped == {}
    assert end.reason.startswith("its term of 7 days over, the Hub no longer listing it")
    b = kept(*limited())  # one that never heard a term (a Hub of 13.1b): it never expires
    b.stepped[KEY]["at"] = 0.0
    assert b.keep_steps() == [] and KEY in b.stepped


@pytest.mark.parametrize(
    ("peak", "days", "kept_"),
    [
        (100.0 * MI, 6.9, True),  # its term not over
        (300.0 * MI, 7.0, True),  # over, and still needed: above its template's 256Mi
        (None, 7.0, True),  # over, and the Hub doesn't know: kept
        (256.0 * MI, 7.0, False),  # at its template's limit: no longer needed
        ("unlisted", 7.0, False),  # no longer under Automation, or the Reflex off
    ],
)
def test_a_step_up_ends_a_term_after_its_last_step_unless_its_peak_says_it_is_needed(
    peak: Any, days: float, kept_: bool
) -> None:
    core, apps = limited()
    a = aged(core, apps, peak, days)
    got = a.keep_steps()
    assert (KEY in a.stepped) is kept_ and len(got) == (0 if days < 7 else 1)
    if kept_ and days >= 7:
        assert time.time() - a.stepped[KEY]["renewed"] < 5 and resized(core) == []
        (kept_on,) = got  # said, so the Hub still knows of it past its 90 days of Actuations
        assert (kept_on.outcome, kept_on.pods, kept_on.memory) == ("stepped_up", 0, 356.0 * MI)
        assert kept_on.reason == "kept for another term: its memory peak is " + (
            "not known against its template's limit 256Mi"
            if peak is None
            else "300Mi above its template's limit 256Mi"
        )
        assert not actuator.to_tell(kept_on.outcome, kept_on.reason)  # nor posted to Slack
        a.peaks = {KEY: 1.0}  # no longer needed: but its next term counts from the renewal
        assert a.keep_steps() == [] and KEY in a.stepped
        a.stepped[KEY]["renewed"] -= WEEK
        assert [x.outcome for x in a.keep_steps()] == ["step_ended"]
    if not kept_:
        (ended_,) = got
        assert (ended_.outcome, ended_.pods) == ("step_ended", 0)
        said = core.create_namespaced_event.call_args.args[1]
        assert said.reason == "StepUpExpired" and said.message == f"app: {ended_.reason}"


def test_an_expired_step_ups_pods_go_back_in_place_only_where_the_peak_says_they_fit() -> None:
    core, apps = limited()
    a = aged(core, apps, 200.0 * MI)
    low = pod("api-1-c", "api-1", cpu="1", memory="128Mi")  # one left at its template's
    low.metadata.namespace = "shop"
    low.spec.containers[0].resources.limits = {"memory": "256Mi"}
    gone = pod("api-1-d", "api-1", cpu="1", memory="178Mi")  # one whose node is gone
    gone.metadata.namespace = "shop"
    gone.spec.containers[0].resources.limits = {"memory": "356Mi"}
    core.list_namespaced_pod.return_value.items += [low, shown(gone, NODE_GONE)]
    grow = core.patch_namespaced_pod_resize.side_effect

    def refusing(name: str, ns: str, body_: Any) -> None:
        if name == "api-1-a":  # Kubernetes 1.33: a memory limit is not lowered in place
            raise ApiException(status=422, reason="memory limits cannot be decreased")
        grow(name, ns, body_)

    core.patch_namespaced_pod_resize.side_effect = refusing
    (ended_,) = a.keep_steps()
    assert ended_.reason == (
        "its term of 7 days over, its memory peak 200Mi within its template's "
        "limit 256Mi: 1 of 2 pods back to memory limit 256Mi, request 128Mi in place; 1 refused: "
        "api-1-a: 422: memory limits cannot be decreased"
    )
    assert [n for n, _ in resized(core)] == ["api-1-a", "api-1-b"]  # not api-1-c, nor api-1-d
    assert resized(core)[0][1] == actuator.step_body("app", "256Mi", "128Mi")
    assert memory(core, "api-1-b") == ("256Mi", "128Mi") and a.stepped == {}
    # no longer listed: its usage unknown, so its pods keep theirs
    core, apps = limited()
    a = aged(core, apps)
    (ended_,) = a.keep_steps()
    assert ended_.reason == (
        "its term of 7 days over, the Hub no longer listing it: its pods keep "
        "their memory until they start again, its memory peak not known to be within its "
        "template's limit"
    )
    assert resized(core) == [] and memory(core, "api-1-a") == ("356Mi", "178Mi")


def test_a_step_up_without_a_term_never_ends_and_a_cronjobs_runs_just_start_from_its_template() -> (
    None
):
    core, apps = limited()
    a = aged(core, apps, 1.0, days=400)
    a.oom_expires = None  # a Hub of 13.1b
    assert a.keep_steps() == [] and KEY in a.stepped
    core, apps, _ = nightly()
    a = stepping(core, apps)
    a.stepped[RUNS] = {**STEP, "at": time.time() - 8 * 86400}
    a.oom_expires, a.peaks = WEEK, {RUNS: 1.0}
    (ended_,) = a.keep_steps()
    assert ended_.kind == "CronJob" and ended_.reason.endswith(
        "its template's limit 256Mi: its next runs' pods start from its template"
    )
    assert resized(core) == []


def test_a_minute_takes_the_term_and_the_peaks_from_the_hub() -> None:
    core, apps = limited()
    a = kept(core, apps)
    a.stepped[KEY]["at"] = time.time() - 2 * 3600
    listing = {**reflexing(), "oom": [{**API.model_dump(), "peak": 100.0 * MI}]}
    seen: list[httpx.Request] = []
    a.http = hub_of([listing, {**listing, "oom_expires": 3600}], seen)
    a.step()  # no term from the Hub: kept
    assert KEY in a.stepped and posted(seen) == []
    a.step()
    assert a.stepped == {} and posted(seen) == [["step_ended"]]
    assert memory(core, "api-1-a") == ("256Mi", "128Mi")
    assert json.loads(seen[-1].content)[0]["reason"].startswith("its term of 60 minutes over, ")
    assert [actuator.span(x) for x in (150, 7200, 36 * 3600, 2 * 86400, 7 * 86400)] == [
        "2 minutes",
        "2 hours",
        "36 hours",
        "2 days",
        "7 days",
    ]


def test_a_step_up_and_a_container_at_its_cap_reach_the_operators_channel_and_no_other(
    settings: Settings, database_url: str
) -> None:
    app, slack = slacked(settings, database_url)
    now = time.time()
    none = {"run_id": 0, "cpu": None}
    stepped = "memory limit 128Mi to 228Mi, request 64Mi to 114Mi on 2 of 2 pods in place"
    at_cap = (
        "OOM-killed: api-1-a with a memory limit of 256Mi (container-limit). No Step-up: it is "
        "at 2 times its template's limit (256Mi): raise it in its manifest, or look for a leak"
    )
    sent = [
        report(**none, outcome="stepped_up", memory=228.0 * MI, at=now - 50, reason=stepped),
        report(**none, outcome="stepped_up", memory=228.0 * MI, at=now - 40, pods=1)
        | {"reason": "brought to its Step-up, memory limit 228Mi, request 114Mi on 1 of 2 pods"},
        report(**none, outcome="oom_killed", memory=256.0 * MI, at=now - 90000, reason=at_cap),
        seen_kill(at=now - 20, reason="OOM-killed: api-1-a. Nothing changed"),
        report(**none, outcome="step_ended", memory=None, pods=0, at=now - 10, reason="expired"),
    ]
    with TestClient(app) as c:
        c.post("/actuator/reports", json=sent, headers=as_actuator(A_TOKEN))
        tell(c)
        assert posts(slack) == [  # in the order they happened
            (
                ACME_CHANNEL,
                ":warning: acme: test/shop/Deployment/api container app is still OOM-killed at "
                f"its Step-up's cap: {at_cap}",
            ),
            (
                ACME_CHANNEL,
                ":arrow_up: acme: the Actuator stepped up test/shop/Deployment/api container app: "
                f"{stepped}. It ends by itself once the need is gone; to keep it, set its memory "
                "limit to 228Mi in its manifest.",
            ),
        ]
        assert untold(database_url) == []
        again = {**sent[2], "at": now - 80000}  # killed again at its cap: said once a day
        c.post("/actuator/reports", json=[again], headers=as_actuator(A_TOKEN))
        tell(c)
        assert len(posts(slack)) == 2 and untold(database_url) == []
        c.post(
            "/actuator/reports", json=[{**again, "at": now - 3000}], headers=as_actuator(A_TOKEN)
        )
        tell(c)  # a day after the one that was said
        assert len(posts(slack)) == 3 and posts(slack)[-1][1].startswith(":warning: acme: test/")
        # bank's Step-up: its own channel, and its own day for a container of the same name
        c.post("/actuator/reports", json=sent[:3], headers=as_actuator(B_TOKEN))
        tell(c)
    assert [ch for ch, _ in posts(slack)[3:]] == [BANK_CHANNEL] * 2
    assert all(text.split(": ", 1)[1].startswith("bank: ") for _, text in posts(slack)[3:])


def test_a_step_up_after_three_rollbacks_never_says_automation_ended_again(
    settings: Settings, database_url: str
) -> None:
    app, slack = slacked(settings, database_url)
    now = time.time()
    step = "memory limit 128Mi to 228Mi, request 64Mi to 114Mi on 2 of 2 pods in place"
    up = report(run_id=0, cpu=None, outcome="stepped_up", memory=228.0 * MI, at=now - 10)
    with TestClient(app) as c:
        sql(database_url, run("acme", '{"test": null}'), sized("acme", 1))
        thrice = [back(now - 300 + i) for i in range(3)]
        c.post("/actuator/reports", json=thrice, headers=as_actuator(A_TOKEN))
        tell(c)
        c.post("/actuator/reports", json=[{**up, "reason": step}], headers=as_actuator(A_TOKEN))
        tell(c)
    said = [text for _, text in posts(slack)]
    assert len(said) == 4 and said[-1].startswith(":arrow_up: acme: the Actuator stepped up")
    assert ["Automation of" in text for text in said] == [False, False, True, False]


def test_the_automation_view_shows_a_step_up_and_reverts_a_container_with_only_one(
    settings: Settings, database_url: str
) -> None:
    from tests.test_console import OPERATOR, VIEW

    settings.console_operators = {OPERATOR}
    app, _ = slacked(settings, database_url)
    now = time.time()
    path, same = "/api/v1/contracts/acme/automation", {**VIEW, "Sec-Fetch-Site": "same-origin"}
    step = {"run_id": 0, "cpu": None, "outcome": "stepped_up", "memory": 228.0 * MI}
    stepped = "memory limit 128Mi to 228Mi, request 64Mi to 114Mi on 2 of 2 pods in place"

    def of(name: str, ago: float, **more: Any) -> dict[str, Any]:
        return report(name=name, at=now - ago, **{"reason": stepped, **more})

    sent = [
        of("api", 900, reason="2 of 2 pods resized in place"),  # a Sizing held, then stepped up
        of("api", 800, **step),
        of("api", 700, **{**step, "memory": 328.0 * MI}),  # twice: its last one counts
        of("lone", 600, **step),  # nothing held of it but its Step-up
        of("ended", 500, **step),
        of("ended", 400, **{**step, "outcome": "step_ended", "memory": None}, pods=0),
        of("undone", 300, reason="2 of 2 pods resized in place"),
        of("undone", 200, **step),
        of("undone", 100, outcome="reverted", cpu=None, reason="reverted by the Operator (3)"),
    ]
    with TestClient(app) as c:
        c.post("/actuator/reports", json=sent, headers=as_actuator(A_TOKEN))
        c.post("/actuator/reports", json=[of("ledger", 50, **step)], headers=as_actuator(B_TOKEN))
        # acme's other cluster, which runs no Actuator: never a row of this one's
        sql(database_url, rolled_back("acme", now - 60, "edge", "edgy", "stepped_up"))
        page = c.get(path, headers=VIEW).json()
        theirs = c.get("/api/v1/contracts/bank/automation", headers=VIEW).json()
        what = {"cluster": "test", "namespace": "shop", "container": "app", "kind": "Deployment"}
        lone = c.post(f"{path}/revert", json={**what, "name": "lone"}, headers=same)
        ended_ = c.post(f"{path}/revert", json={**what, "name": "ended"}, headers=same)
        other = c.post(f"{path}/revert", json={**what, "name": "ledger"}, headers=same)
    shown_ = {h["name"]: h for h in page["clusters"][0]["containers"]}
    assert list(shown_) == ["api", "lone", "undone"]  # `ended` holds nothing any more: no row
    assert [(n, h["stepped"]) for n, h in shown_.items()] == [
        ("api", 328.0 * MI),
        ("lone", 228.0 * MI),
        ("undone", None),  # a Revert dropped it
    ]
    assert shown_["api"]["outcome"] == "applied" and shown_["api"]["revertable"]
    assert shown_["lone"]["outcome"] == "stepped_up" and shown_["lone"]["revertable"]
    assert shown_["lone"]["memory"] is None  # its limit is `stepped`: it holds no request
    assert not shown_["undone"]["revertable"]
    assert [h["name"] for h in theirs["clusters"][0]["containers"]] == ["ledger"]  # bank's own
    assert lone.status_code == 200 and (ended_.status_code, other.status_code) == (409, 409)
    assert next(h for h in lone.json()["clusters"][0]["containers"] if h["name"] == "lone")[
        "reverting"
    ]


def test_a_revert_lowers_a_step_ups_pods_by_the_peaks_of_the_minute_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Hub stops listing a reverted container in the very answer that asks the Revert."""
    core, apps = limited()
    a = kept(core, apps)
    listing = {**reflexing(), "oom": [{**API.model_dump(), "peak": 200.0 * MI}]}
    undo = {"id": 4, "namespace": "shop", "kind": "Deployment", "name": "api", "container": "app"}
    seen: list[httpx.Request] = []
    a.http = hub_of([listing, {**reflexing(), "oom": [], "reverts": [undo]}], seen)
    a.step()
    assert a.peaks == {KEY: 200.0 * MI} and posted(seen) == []
    a.step()
    assert posted(seen) == [["reverted"]] and a.stepped == {}
    assert memory(core, "api-1-a") == memory(core, "api-1-b") == ("256Mi", "128Mi")
    assert "2 of 2 pods back to memory limit 256Mi, request 128Mi in place" in str(
        json.loads(seen[-1].content)[0]["reason"]
    )


def test_a_step_up_reported_without_its_limit_never_stops_the_rollbacks_behind_it(
    settings: Settings, database_url: str
) -> None:
    app, slack = slacked(settings, database_url)
    now = time.time()
    odd = report(run_id=0, cpu=None, outcome="stepped_up", memory=None, at=now - 20)
    odd["reason"] = "memory limit 128Mi to 228Mi, request 64Mi to 114Mi on 2 of 2 pods in place"
    with TestClient(app) as c:
        c.post("/actuator/reports", json=[odd, back(now - 10)], headers=as_actuator(A_TOKEN))
        tell(c)
    said = [text for _, text in posts(slack)]
    assert len(said) == 2 and untold(database_url) == []
    assert said[0].endswith("to keep it, raise its memory limit in its manifest.")
    assert said[1].startswith(":rewind: acme: the Actuator rolled back")


# --- the Startup boost (slice 13.4) -------------------------------------------------------

BOOSTING = {**AUTO, "startup_boost": 2}
NOTE = "/metadata/annotations"
CPU = "/spec/containers/1/resources/limits/cpu"


def test_a_startup_boost_is_a_factor_from_one_and_a_half_to_four_on_an_automated_rule() -> None:
    c = automating("acme", A_TOKEN, {**AUTO, "name": "db"}, BOOSTING)
    assert c.sizing.startup_boost("test", "shop", "Deployment", "api", "app") == 2.0
    # the rule that picks its policy decides, as for `automate`; none without a rule
    assert c.sizing.startup_boost("test", "shop", "Deployment", "db", "app") is None
    assert c.sizing.startup_boost("test", "shop", "Deployment", "api", "sidecar") is None
    for ok in (1.5, 4):
        automating("acme", A_TOKEN, {**AUTO, "startup_boost": ok})
    for bad in (1.4, 4.1):
        with pytest.raises(ValueError, match="startup_boost"):
            automating("acme", A_TOKEN, {**AUTO, "startup_boost": bad})
    with pytest.raises(ValueError, match="startup_boost: only on a rule with automate"):
        automating("acme", A_TOKEN, {**AUTO, "automate": False, "startup_boost": 2})


def test_the_hub_lists_the_boosts_of_a_contracts_own_automated_containers(
    settings: Settings, database_url: str
) -> None:
    now = time.time()
    acme = automating("acme", A_TOKEN, {**AUTO, "name": "plain"}, BOOSTING)
    bank = automating("bank", B_TOKEN, {**AUTO, "name": "ledger", "startup_boost": 3})
    with TestClient(hub(settings, database_url, acme, bank)) as c:
        sql(
            database_url,
            run("acme", '{"test": null}'),
            sized("acme", 1),
            sized("acme", 1, "learning", state="learning", memory=None),  # no Sizing yet
            sized("acme", 1, "plain"),  # automated by a rule without a boost
            sized("acme", 1, "db", kind="StatefulSet"),
            sized("acme", 1, "nightly", kind="CronJob"),  # never a CronJob's run
            sized("acme", 1, "side", container="sidecar"),  # no rule automates it
            sized("acme", 1, "broken"),
            *(rolled_back("acme", now - d * 86400, name="broken") for d in (1, 2, 3)),  # ended
            run("bank", '{"test": null}'),
            sized("bank", 2, "ledger"),
            sized("bank", 2, "api"),  # bank boosts only its ledger
        )
        kinds = {"kinds": ",".join(actuator.KINDS)}
        mine = c.get("/actuator/desired", headers=as_actuator(A_TOKEN), params=kinds).json()
        old = c.get("/actuator/desired", headers=as_actuator(A_TOKEN)).json()
        theirs = c.get("/actuator/desired", headers=as_actuator(B_TOKEN), params=kinds).json()
        sql(database_url, ("insert into kill_switches values ('acme', 't', 1)", ()))
        killed = c.get("/actuator/desired", headers=as_actuator(A_TOKEN), params=kinds).json()
    one = {"namespace": "shop", "kind": "Deployment", "container": "app", "factor": 2.0}
    assert "nightly" in [w["name"] for w in mine["automated"]]  # its Sizing still goes
    assert mine["boosts"] == [{**one, "name": n} for n in ("api", "learning")] + [
        {**one, "kind": "StatefulSet", "name": "db"}
    ]
    assert old["boosts"] == mine["boosts"][:-1]  # an Actuator naming no kinds: Deployments only
    assert theirs["boosts"] == [{**one, "name": "ledger", "factor": 3.0}]
    assert killed["boosts"] == []


def starting(review: Any = None, factor: float = 2.0, **kw: Any) -> list[Any]:
    """What the webhook does to a new pod of api (its `app` asks 250m of cpu under a limit
    of 500m unless `review` says) with a Startup boost of `factor`."""
    review = review or new_pod(requests={"cpu": "250m"}, limits={"cpu": "500m"})
    return actuator.mutation(review["request"], {}, "squero-actuator", boosts={KEY: factor}, **kw)


def own(limit: str = "500m") -> str:
    return json.dumps({"app": limit})


def test_a_new_pod_starts_with_its_cpu_limit_boosted_and_its_own_kept_in_an_annotation() -> None:
    assert starting() == [
        {"op": "add", "path": CPU, "value": "1000m"},
        {"op": "add", "path": NOTE, "value": {actuator.BOOST: own()}},
    ]
    noted = new_pod(requests={"cpu": "250m"}, limits={"cpu": "1", "memory": "1Gi"})
    noted["request"]["object"]["metadata"]["annotations"] = {"team": "shop"}
    assert starting(noted, 1.5) == [
        {"op": "add", "path": CPU, "value": "1500m"},
        {"op": "add", "path": f"{NOTE}/squero-actuator~1startup-boost", "value": own("1")},
    ]
    # a limit alone is its request too: said, so that the request stays where it was
    alone = starting(new_pod(limits={"cpu": "500m"}))
    assert alone[0] == {
        "op": "add",
        "path": "/spec/containers/1/resources/requests",
        "value": {"cpu": "500m"},
    }
    assert alone[1:] == starting()
    for kind in ("StatefulSet", "DaemonSet"):  # the limit only: it fits whatever node it starts on
        review = owned_by(kind, "api", requests={"cpu": "250m"}, limits={"cpu": "500m"})
        key = f"shop/{kind}/api/app"
        assert actuator.mutation(review["request"], {}, "x", boosts={key: 2.0}) == starting()
    answer = actuator.admit(noted, {}, False, "x", boosts={KEY: 2.0})["response"]
    assert json.loads(base64.b64decode(answer["patch"])) == starting(noted)


def test_a_boost_goes_beside_a_sizings_requests_and_a_step_up() -> None:
    held = {KEY: {"outcome": "applied", "requests": {"cpu": "900m"}, "template": {"cpu": "250m"}}}
    review = new_pod(requests={"cpu": "250m"}, limits={"cpu": "500m"})
    ops = actuator.mutation(review["request"], held, "squero-actuator", boosts={KEY: 2.0})
    at = "/spec/containers/1/resources"
    # its request never above its own limit, whatever the boost makes of it meanwhile
    assert ops[0] == {"op": "add", "path": f"{at}/requests/cpu", "value": "500m"}
    assert ops[1:] == starting()
    alone = new_pod(limits={"cpu": "500m"})["request"]  # a limit alone: the Sizing's request
    held[KEY] = {"outcome": "applied", "requests": {"cpu": "115m"}, "template": {"cpu": None}}
    ops = actuator.mutation(alone, held, "squero-actuator", boosts={KEY: 2.0})
    assert ops[0] == {"op": "add", "path": f"{at}/requests", "value": {"cpu": "115m"}}
    assert ops[1:] == starting()
    asks = {"requests": {"cpu": "1", "memory": "128Mi"}, "limits": {"cpu": "2", "memory": "256Mi"}}
    ops = starting(new_pod(**asks), stepped={KEY: dict(STEP)})
    assert [(o["path"].removeprefix(at), o["value"]) for o in ops[:3]] == [
        ("/requests/memory", "178Mi"),
        ("/limits/memory", "356Mi"),
        ("/limits/cpu", "4000m"),
    ]


GUARANTEED = {"requests": {"cpu": "1", "memory": "1Gi"}, "limits": {"cpu": "1", "memory": "1Gi"}}


def pod_level() -> dict[str, Any]:
    review = new_pod(requests={"cpu": "250m"}, limits={"cpu": "500m"})
    review["request"]["object"]["spec"]["resources"] = {"limits": {"cpu": "600m"}}
    return review


@pytest.mark.parametrize(
    "review",
    [
        new_pod(requests={"cpu": "250m"}, limits={"memory": "1Gi"}),  # no CPU limit: none needed
        new_pod(none=True),
        new_pod(sidecar={"resources": GUARANTEED}, **GUARANTEED),  # a Guaranteed pod
        new_pod(  # Guaranteed by its limits alone
            sidecar={"resources": {"limits": GUARANTEED["limits"]}}, limits=GUARANTEED["limits"]
        ),
        owned_by("Job", run_of("api"), requests={"cpu": "250m"}, limits={"cpu": "500m"}),
        new_pod(ns="kube-system", requests={"cpu": "250m"}, limits={"cpu": "500m"}),
        pod_level(),  # a pod-level limit, which a container's may not pass
        new_pod(operation="UPDATE", requests={"cpu": "250m"}, limits={"cpu": "500m"}),
    ],
)
def test_a_pod_that_gets_no_startup_boost(review: Any) -> None:
    boosts = {KEY: 2.0, CRON: 2.0}
    assert actuator.mutation(review["request"], {}, "squero-actuator", boosts=boosts) == []


def test_no_boost_twice_nor_where_the_limit_could_not_be_given_back_in_place() -> None:
    assert starting(factor=0) == []
    other = new_pod(requests={"cpu": "250m"}, limits={"cpu": "500m"})
    assert actuator.mutation(other["request"], {}, "x", boosts={"shop/Deployment/web/app": 2}) == []
    assert starting(paused=frozenset({"shop/Deployment/api"})) == []
    assert actuator.admit(other, {}, True, "x", boosts={KEY: 2.0})["response"].get("patch") is None
    restarting = new_pod(requests={"cpu": "250m"}, limits={"cpu": "500m"})
    app = restarting["request"]["object"]["spec"]["containers"][1]
    app["resizePolicy"] = [{"resourceName": "cpu", "restartPolicy": "RestartContainer"}]
    assert starting(restarting) == []  # giving it back would restart the container
    app["resizePolicy"] = [
        {"resourceName": "cpu", "restartPolicy": "NotRequired"},
        {"resourceName": "memory", "restartPolicy": "RestartContainer"},
    ]
    assert starting(restarting) == starting()
    # a pod that already carries the annotation (its template's, or the webhook asked
    # again): its limit is not multiplied a second time
    restarting["request"]["object"]["metadata"]["annotations"] = {actuator.BOOST: own("250m")}
    assert starting(restarting) == []


def started(
    core: Any,
    name: str = "api-1-a",
    ready: bool = False,
    age: float | None = 30.0,
    limit: str = "1000m",
    was: Any = "500m",
    restarts: int = 0,
) -> Any:
    """api's pod `name` as the webhook boosted it, its `app` running for `age` seconds
    (None: not started yet) under a CPU limit of `limit`, its own (`was`) in its annotation.
    The pod itself is an hour old: its image's pull and init containers took that long."""
    p = next(p for p in core.list_namespaced_pod.return_value.items if p.metadata.name == name)
    p.metadata.labels = {"app": "api", "pod-template-hash": "1"}
    p.metadata.uid = f"uid-{name}"
    note = was if isinstance(was, str) and was.startswith(("{", "[", "!")) else own(was)
    p.metadata.annotations = {actuator.BOOST: note}
    p.spec.containers[0].resources = k.V1ResourceRequirements(
        requests={"cpu": "250m"}, limits={"cpu": limit}
    )
    p.status.start_time = datetime.fromtimestamp(time.time() - 3600, UTC)
    st = status("app", None)
    st.restart_count = restarts
    st.state = k.V1ContainerState(waiting=k.V1ContainerStateWaiting(reason="PodInitializing"))
    if age is not None:
        at = datetime.fromtimestamp(time.time() - age, UTC)
        st.state = k.V1ContainerState(running=k.V1ContainerStateRunning(started_at=at))
    p.status.container_statuses = [st]
    return shown(p, {"Ready": "True", "ContainersReady": "True"} if ready else PROBE_FAILS)


def cpu_limit(core: Any, name: str) -> str | None:
    p = next(p for p in core.list_namespaced_pod.return_value.items if p.metadata.name == name)
    return (p.spec.containers[0].resources.limits or {}).get("cpu")


def boosting(core: Any, apps: Any, **kw: Any) -> actuator.Actuator:
    """An Actuator the Hub last told to boost api's `app` (or `boosts`)."""
    return make(hub_of([], []), core, apps, **{"boosts": {KEY: 2.0}, **kw})


def test_a_boost_ends_when_its_pod_is_ready_or_five_minutes_after_its_start() -> None:
    core, apps = cluster()
    a = boosting(core, apps)
    started(core, "api-1-a", age=30)  # still starting
    started(core, "api-1-b", ready=True, age=30)
    a.keep_boosts()
    assert (cpu_limit(core, "api-1-a"), cpu_limit(core, "api-1-b")) == ("1000m", "500m")
    body = {"spec": {"containers": [{"name": "app", "resources": {"limits": {"cpu": "500m"}}}]}}
    assert resized(core) == [("api-1-b", body)]  # the limit only: its request untouched
    event = core.create_namespaced_event.call_args.args[1]
    assert (event.reason, event.type) == ("StartupBoostEnded", "Normal")
    assert (event.involved_object.kind, event.involved_object.name) == ("Pod", "api-1-b")
    assert event.involved_object.uid == "uid-api-1-b"
    assert event.message == (
        "app: startup boost over, CPU limit 1000m back to 500m: its pod is Ready"
    )
    assert a.boost_ns == {"shop"} and a.boosting == {KEY: 2.0}
    a.keep_boosts()  # given back: said once
    assert len(resized(core)) == 1 and core.create_namespaced_event.call_count == 1
    started(core, "api-1-a", age=actuator.BOOST_SECONDS - 5)
    a.keep_boosts()
    assert cpu_limit(core, "api-1-a") == "1000m"
    started(core, "api-1-a", age=actuator.BOOST_SECONDS)  # never Ready: 5 minutes all the same
    a.keep_boosts()
    assert cpu_limit(core, "api-1-a") == "500m" and actuator.BOOST_SECONDS == 300
    assert core.create_namespaced_event.call_args.args[1].message.endswith(
        "back to 500m: 5 minutes after its start"
    )


def test_a_container_that_restarts_loses_its_boost_at_once() -> None:
    core, apps = cluster()
    a = boosting(core, apps)
    started(core, age=5, restarts=1)  # a restart gets no boost, crash-looping or not
    a.keep_boosts()
    assert cpu_limit(core, "api-1-a") == "500m"
    assert core.create_namespaced_event.call_args.args[1].message.endswith(
        "back to 500m: it restarted"
    )
    p = started(core, "api-1-b", age=None, restarts=1)  # between two runs
    a.keep_boosts()
    assert cpu_limit(core, "api-1-b") == "500m"
    p = started(core, "api-1-b")
    p.status.container_statuses = None  # no status yet: nothing to count from
    a.keep_boosts()
    assert cpu_limit(core, "api-1-b") == "1000m" and a.boost_ns == {"shop"}


def test_an_annotation_is_anyones_word_a_limit_is_only_ever_lowered_to_it() -> None:
    core, apps = cluster()
    a = boosting(core, apps)
    for was in ("2", "1000m", "100m", 0.5, "!", "[1]", '{"other": "500m"}', '{"app": "x"}'):
        # above its limit or at it; under its request of 250m; a number, not the quantity it
        # wrote; no JSON; no object; no such container; no quantity
        started(core, ready=True, was=was)
        a.keep_boosts()
        assert resized(core) == [] and a.boost_ns == {"shop"}, was
    for p in core.list_namespaced_pod.return_value.items[2:4]:  # Succeeded; being deleted
        started(core, p.metadata.name, ready=True)
        p.status.phase = "Succeeded" if p.metadata.name == "api-1-done" else "Running"
    gone = started(core, "api-1-b", age=600)
    shown(gone, NODE_GONE)  # its node is gone: no kubelet to resize it
    a.keep_boosts()
    assert resized(core) == []
    started(core, "api-1-b", age=None)  # its image still pulled: no start to count from
    a.keep_boosts()
    assert resized(core) == []
    started(core, "api-x-a", ready=True)  # another Workload's, boosted before its rule went
    a.keep_boosts()
    assert [n for n, _ in resized(core)] == ["api-x-a"]


def test_a_paused_workloads_boost_waits_and_a_refused_resize_is_tried_again(
    caplog: Any,
) -> None:
    core, apps = cluster()
    a = boosting(core, apps, paused=frozenset({"shop/Deployment/api"}))
    started(core, ready=True)
    a.keep_boosts()
    assert resized(core) == [] and a.boost_ns == {"shop"}
    a.paused, a.boosts = frozenset(), {}  # its rule gone meanwhile
    patch = core.patch_namespaced_pod_resize.side_effect
    core.patch_namespaced_pod_resize.side_effect = refusal(500, "the kubelet is busy")
    a.keep_boosts()
    a.keep_boosts()
    assert a.boost_ns == {"shop"}  # still read: a boost is left there
    assert cpu_limit(core, "api-1-a") == "1000m" and not core.create_namespaced_event.called
    assert "ending shop/api-1-a's startup boost: 500" in caplog.text
    core.patch_namespaced_pod_resize.side_effect = patch
    a.keep_boosts()
    assert cpu_limit(core, "api-1-a") == "500m" and a.boost_ns == set()


def capping(**item: Any) -> Any:
    """A LimitRange `caps` with this one item, a Container's unless `type` says."""
    ranges = limit_range(**{f: v for f, v in item.items() if f != "type"})
    ranges.items[0].metadata = k.V1ObjectMeta(name="caps")
    ranges.items[0].spec.limits[0].type = item.get("type", "Container")
    return ranges


@pytest.mark.parametrize(
    ("ranges", "quotas", "vpa", "why"),
    [
        (capping(max={"cpu": "4"}), None, None, "shop: LimitRange caps caps cpu"),
        (capping(max_limit_request_ratio={"cpu": "2"}), None, None, "LimitRange caps caps cpu"),
        (capping(type="Pod", max={"cpu": "8"}), None, None, "LimitRange caps caps cpu"),
        (None, quota({"limits.cpu": "10"}, {}), None, "ResourceQuota team counts limits.cpu"),
        (None, None, "Auto", "shop/Deployment/api: a VPA in mode Auto sizes it"),
    ],
)
def test_no_boost_where_it_could_get_a_pod_refused_or_a_vpa_sizes_it(
    ranges: Any, quotas: Any, vpa: Any, why: str, caplog: Any
) -> None:
    core, apps = cluster()
    if ranges:
        core.list_namespaced_limit_range.return_value = ranges
    if quotas:
        core.list_namespaced_resource_quota.return_value = quotas
    a = boosting(core, apps)
    if vpa:
        target = {"kind": "Deployment", "name": "api"}
        a.custom.list_namespaced_custom_object.return_value = {
            "items": [{"spec": {"targetRef": target, "updatePolicy": {"updateMode": vpa}}}]
        }
    started(core, ready=True)
    a.keep_boosts()
    a.keep_boosts()
    assert a.boosting == {} and caplog.text.count("no startup boost in") == 1  # said once
    assert why in caplog.text
    assert cpu_limit(core, "api-1-a") == "500m"  # a boost given before is still ended


def test_what_caps_nothing_bars_no_boost() -> None:
    core, apps = cluster()
    core.list_namespaced_limit_range.return_value = capping(
        min={"cpu": "10m"}, max={"memory": "8Gi"}, default={"cpu": "1"}
    )
    core.list_namespaced_resource_quota.return_value = quota({"requests.cpu": "10"}, {})
    a = boosting(core, apps, boosts={KEY: 2.0, "shop/Deployment/web/app": 3.0})
    target = {"kind": "Deployment", "name": "api"}
    a.custom.list_namespaced_custom_object.return_value = {
        "items": [{"spec": {"targetRef": target, "updatePolicy": {"updateMode": "Off"}}}]
    }
    a.keep_boosts()
    assert a.boosting == {KEY: 2.0, "shop/Deployment/web/app": 3.0}
    assert a.custom.list_namespaced_custom_object.call_count == 1  # its VPAs read once


def test_a_namespace_it_cannot_read_gets_no_boost_and_the_others_still_do(caplog: Any) -> None:
    core, apps = cluster()
    pods = core.list_namespaced_pod.return_value

    def listed(ns: str) -> Any:
        if ns == "bar":
            raise refusal(403, "Forbidden")
        return pods

    core.list_namespaced_pod.side_effect = listed
    a = boosting(core, apps, boosts={KEY: 2.0, "bar/Deployment/api/app": 2.0})
    started(core, ready=True)
    a.keep_boosts()
    assert a.boosting == {KEY: 2.0} and a.boost_ns == {"bar", "shop"}
    assert "no startup boost in bar: reading it: 403" in caplog.text
    assert cpu_limit(core, "api-1-a") == "500m"
    a.boosts = {KEY: 2.0}  # bar's rule gone: still read until it can be, for a boost left there
    a.keep_boosts()
    a.keep_boosts()
    assert a.boost_ns == {"bar", "shop"}


def test_a_namespace_is_read_until_its_last_boost_is_over(monkeypatch: pytest.MonkeyPatch) -> None:
    core, apps = cluster()
    seen: list[httpx.Request] = []
    boost = {"namespace": "shop", "kind": "Deployment", "name": "api", "container": "app"}
    with_ = {**desired(), "boosts": [{**boost, "factor": 2.5}]}
    a = make(hub_of([with_, desired(), desired(), desired(), desired()], seen), core, apps)
    a.step()
    assert a.boosts == a.boosting == {KEY: 2.5} and a.boost_ns == {"shop"}
    apps.list_namespaced_deployment.assert_called_with("shop")  # its pause is read
    state = json.loads(core.replace_namespaced_config_map.call_args.args[2].data[actuator.STATE])
    assert (state["boosts"], state["boost_ns"]) == ({KEY: 2.5}, ["shop"])
    a.step()  # its rule dropped: nothing more to boost
    assert a.boosts == a.boosting == {} and a.boost_ns == {"shop"}  # one minute more
    started(core, age=30)  # a pod the webhook boosted in between
    a.step()
    assert a.boost_ns == {"shop"} and cpu_limit(core, "api-1-a") == "1000m"
    started(core, ready=True)
    a.step()
    assert a.boost_ns == set() and cpu_limit(core, "api-1-a") == "500m"
    lists = core.list_namespaced_pod.call_count
    a.step()
    assert core.list_namespaced_pod.call_count == lists  # read no more
    state = json.loads(core.replace_namespaced_config_map.call_args.args[2].data[actuator.STATE])
    assert "boosts" not in state and "boost_ns" not in state


def test_boosts_go_on_while_the_hub_is_unreachable_and_stop_under_the_kill_switch() -> None:
    core, apps = cluster()
    boost = {"namespace": "shop", "kind": "Deployment", "name": "api", "container": "app"}
    with_ = {**desired(), "boosts": [{**boost, "factor": 2.0}]}
    down = httpx.Response(503)
    a = make(hub_of([with_, down, desired(killed=True), with_], []), core, apps)
    a.step()
    saved = core.replace_namespaced_config_map.call_args.args[2].data
    core.read_namespaced_config_map.return_value = k.V1ConfigMap(data=saved)
    b = make(a.http, core, apps)  # the Actuator restarts, the Hub unreachable
    b.load()
    assert b.boosts == {KEY: 2.0} and b.boosting == {} and b.boost_ns == {"shop"}
    started(core, ready=True)
    b.step()
    assert b.boosting == {KEY: 2.0} and cpu_limit(core, "api-1-a") == "500m"
    started(core, "api-1-b", ready=True)
    b.step()  # the Kill switch: its word on boosts is kept, and nothing is resized
    assert b.killed and b.boosts == {KEY: 2.0} and cpu_limit(core, "api-1-b") == "1000m"
    review = new_pod(requests={"cpu": "250m"}, limits={"cpu": "500m"})
    answer = actuator.admit(review, b.held, b.killed, "x", b.paused, b.stepped, b.boosting)
    assert answer["response"].get("patch") is None
    b.step()
    assert not b.killed and cpu_limit(core, "api-1-b") == "500m"


# --- slice 13.3a: the burst Reflex, the Hub's side ----------------------------------------

BURST = {"actuator": {"reflexes": {"burst": {}}}}
HOT = {"throttled": 0.4, "peak": 0.7}


def test_the_burst_reflex_needs_an_actuator_and_takes_its_term() -> None:
    on = automating("acme", A_TOKEN, AUTO, **BURST).actuator.reflexes
    assert on.burst is not None and on.burst.expires == 86400 and on.oom is None
    assert automating("acme", A_TOKEN, AUTO).actuator.reflexes.burst is None  # off unless set
    data = actuated("acme", A_TOKEN).model_dump(mode="json", exclude_none=True)
    data["clusters"][0].pop("actuator_token_secret")  # no cluster runs an Actuator
    with pytest.raises(ValueError, match="actuator: reflexes, but no cluster has an actuator"):
        contracts.build({**data, **BURST}, secrets={"r": SecretStr("r" * 40)})
    for bad in ("30m", "8d"):  # an hour at least; a week at most, the shortest built-in window
        with pytest.raises(ValueError, match="must be from 1h to 7d"):
            automating("acme", A_TOKEN, AUTO, actuator={"reflexes": {"burst": {"expires": bad}}})
    week = automating("acme", A_TOKEN, AUTO, actuator={"reflexes": {"burst": {"expires": "7d"}}})
    assert week.actuator.reflexes.burst.expires == 7 * 86400
    assert on.burst.max == 2.0  # slice 13.3b: how far a Burst may go
    for bad in (1.0, 4.5):  # never no step at all, nor past 4 times its template's limit
        with pytest.raises(ValueError, match="max"):
            automating("acme", A_TOKEN, AUTO, actuator={"reflexes": {"burst": {"max": bad}}})
    three = automating("acme", A_TOKEN, AUTO, actuator={"reflexes": {"burst": {"max": 3}}})
    assert three.actuator.reflexes.burst.max == 3.0


def read(cid: str, ago: float = 60.0) -> Any:
    """A Sizing run of `cid`'s cluster `test`, `ago` seconds ago."""
    return (
        "insert into sizing_runs (contract_id, at, clusters) values (%s, %s, %s)",
        (cid, time.time() - ago, '{"test": null}'),
    )


def test_the_hub_tells_an_actuator_which_of_its_own_containers_a_burst_is_due(
    settings: Settings, database_url: str
) -> None:
    acme = automating("acme", A_TOKEN, AUTO, **BURST)
    bank = automating("bank", B_TOKEN, {**AUTO, "name": "ledger"}, **BURST)
    kinds = {"kinds": ",".join(actuator.KINDS)}

    def pulled(c: TestClient, token: str = A_TOKEN, **params: str) -> Any:
        got = c.get("/actuator/desired", headers=as_actuator(token), params=params).json()
        got["burst"].sort(key=lambda b: (b["kind"], b["name"]))
        return got

    with TestClient(hub(settings, database_url, acme, bank)) as c:
        sql(
            database_url,
            read("acme"),  # run 1
            sized("acme", 1, burst=HOT),
            sized("acme", 1, "calm", burst={"throttled": 0.25, "peak": 0.1}),  # not over a quarter
            sized("acme", 1, "learning", state="learning", memory=None, burst={"peak": 0.3}),
            sized("acme", 1, "before"),  # a row stored before the Reflex was set
            sized("acme", 1, "db", kind="StatefulSet", burst=HOT),
            sized("acme", 1, "nightly", kind="CronJob", burst=HOT),  # never a CronJob's
            sized("acme", 1, "side", container="sidecar", burst=HOT),  # no rule automates it
            read("bank"),  # run 2
            sized("bank", 2, "ledger", burst={"throttled": 0.9, "peak": 2.0}),
            sized("bank", 2, "api", burst=HOT),  # bank automates only its ledger
        )
        mine, old, theirs = pulled(c, **kinds), pulled(c), pulled(c, B_TOKEN, **kinds)
        sql(database_url, ("insert into kill_switches values ('acme', 't', 1)", ()))
        killed = pulled(c, **kinds)
        sql(
            database_url,
            ("delete from kill_switches", ()),
            ("update sizing_runs set at = %s where id = 1", (time.time() - 7300,)),
        )
        stale = pulled(c, **kinds)
    one = {"namespace": "shop", "kind": "Deployment", "container": "app"}
    none = {"throttled": None, "peak": None}
    assert mine["burst"] == [
        {**one, "name": "api", **HOT},
        {**one, "name": "before", **none},
        {**one, "name": "calm", "throttled": None, "peak": 0.1},
        {**one, "name": "learning", "throttled": None, "peak": 0.3},
        {**one, "kind": "StatefulSet", "name": "db", **HOT},
    ]
    assert "nightly" in [w["name"] for w in mine["automated"]]  # its Sizing still goes
    assert old["burst"] == mine["burst"][:-1]  # an Actuator naming no kinds: Deployments only
    assert theirs["burst"] == [{**one, "name": "ledger", "throttled": 0.9, "peak": 2.0}]
    assert (mine["burst_run"], theirs["burst_run"]) == (1, 2)  # a Burst is due once a run
    # slice 13.3b: how far a Burst may go and its term, the Contract's; none when off
    assert (mine["burst_max"], mine["burst_expires"]) == (2.0, 86400)
    assert (killed["burst_max"], killed["burst_expires"]) == (None, None)
    assert (killed["burst"], killed["burst_run"]) == ([], None)
    # a read two hours old: no Burst is due for it, and its peak is not known
    assert stale["burst"] == [{**b, **none} for b in mine["burst"]] and stale["burst_run"] == 1
    off = automating("acme", A_TOKEN, AUTO, **OOM)  # the same Contract with the other Reflex only
    with TestClient(hub(settings, database_url, off)) as c:
        sql(database_url, ("update sizing_runs set at = %s where id = 1", (time.time() - 60,)))
        mine = pulled(c, **kinds)
    assert (mine["burst"], mine["burst_run"]) == ([], None) and len(mine["automated"]) == 5
    assert (mine["burst_max"], mine["burst_expires"]) == (None, None)
    own = {"reflexes": {"burst": {"max": 3, "expires": "36h"}}}
    with TestClient(
        hub(settings, database_url, automating("acme", A_TOKEN, AUTO, actuator=own))
    ) as c:
        mine = pulled(c, **kinds)
    assert (mine["burst_max"], mine["burst_expires"]) == (3.0, 36 * 3600)


def test_an_actuator_makes_no_burst_for_a_hub_that_sends_no_cap() -> None:
    """A Hub of slice 13.3a lists the throttled and knows none of the outcomes."""
    core, apps = capped()
    seen: list[httpx.Request] = []
    told = {**desired(), "burst": [{**SHOP_API, "throttled": 1.0001, "peak": 3.0}], "burst_run": 7}
    a = make(hub_of([told], seen), core, apps)
    a.step()
    assert resized(core) == [] and posted(seen) == [] and a.bursts == {} == a.burst_seen
    assert not core.create_namespaced_event.called
    got = actuator.Desired.model_validate(told)  # nothing bounded: a share can pass 1 by a hair
    assert (got.burst[0].throttled, got.burst[0].peak, got.burst_run) == (1.0001, 3.0, 7)
    older = actuator.Desired.model_validate(desired())  # a Hub from before this
    assert older.burst == [] and (older.burst_run, older.burst_max, older.burst_expires) == (
        None,
        None,
        None,
    )


# --- slice 13.3b: the Burst, the Actuator's side (ADR 0005) -----------------------------------

SHOP_API = {"namespace": "shop", "kind": "Deployment", "name": "api", "container": "app"}
SHARE = "throttled in 80% of its CPU periods over the last hour"
DAY_S = 86400


def capped(limit: str | None = "200m", request: str | None = "100m", **kw: Any) -> tuple[Any, Any]:
    """api with a CPU limit (and request) in its template and on its two pods."""
    requests = {"cpu": request} if request else {}
    limits = {"cpu": limit} if limit else None
    core, apps = cluster(limits=limits, requests=requests, **kw)
    for p in core.list_namespaced_pod.return_value.items[:2]:
        p.spec.containers[0].resources = k.V1ResourceRequirements(
            requests=dict(requests), limits=dict(limits) if limits else None
        )
    return core, apps


def bursting(core: Any, apps: Any, top: float = 2.0, **kw: Any) -> actuator.Actuator:
    """An Actuator the Hub lets make Bursts, to `top` times a template's limit, a day long."""
    a = make(hub_of([], []), core, apps, **kw)
    a.burst_max, a.burst_expires = top, DAY_S
    return a


def cpu_body(limit: str) -> Any:
    return {"spec": {"containers": [{"name": "app", "resources": {"limits": {"cpu": limit}}}]}}


def throttling(run: int = 1, share: float | None = 0.8, **more: Any) -> dict[str, Any]:
    """The Hub's answer: api's `app` throttled `share` in read `run`, Bursts to twice."""
    listed = [{**SHOP_API, "throttled": share, "peak": more.pop("peak", None)}]
    cap = {"burst_max": 2.0, "burst_expires": DAY_S}
    return {**desired(), "burst": listed, "burst_run": run, **cap, **more}


def test_a_burst_raises_the_cpu_limit_by_a_quarter_of_its_templates_on_every_pod() -> None:
    core, apps = capped()
    a = bursting(core, apps)
    a.held[KEY] = {"run_id": 7, "outcome": "applied", "requests": {"cpu": "100m"}}
    up = a.burst(KEY, SHARE, 1000.0)
    assert isinstance(up, actuator.Actuation)
    assert (up.outcome, up.cpu, up.pods, up.run_id, up.memory) == ("burst", 0.25, 2, 7, None)
    assert up.reason == f"CPU limit 200m to 250m on 2 of 2 pods in place: {SHARE}"
    assert up.reason.startswith(actuator.BURST_SAID)
    # the limit only: no request is touched, on the two live pods of its own ReplicaSet
    assert resized(core) == [("api-1-a", cpu_body("250m")), ("api-1-b", cpu_body("250m"))]
    assert a.bursts == {
        KEY: {"limit": "250m", "template": "200m", "at": 1000.0, "steps": 1, "expires": DAY_S}
    }
    assert a.steps == [1000.0] and a.held[KEY]["requests"] == {"cpu": "100m"}
    event = core.create_namespaced_event.call_args.args[1]
    assert (event.reason, event.type, event.involved_object.kind) == (
        "CpuBurst",
        "Normal",
        "Deployment",
    )
    assert event.message == f"app: {up.reason}"
    saved = json.loads(core.replace_namespaced_config_map.call_args.args[2].data[actuator.STATE])
    assert saved["bursts"] == a.bursts  # kept, so a restart brings its pods to it
    b = make(hub_of([], []), core, apps)
    core.read_namespaced_config_map.return_value = k.V1ConfigMap(
        data=core.replace_namespaced_config_map.call_args.args[2].data
    )
    b.load()
    assert b.bursts == a.bursts and b.steps == [1000.0]


@pytest.mark.parametrize(
    ("top", "limits"),
    [(2.0, ["250m", "300m", "350m", "400m"]), (1.6, ["250m", "300m", "320m"]), (1.1, ["220m"])],
)
def test_a_burst_goes_a_step_a_read_never_past_its_cap_nor_twice_in_45_minutes(
    top: float, limits: list[str]
) -> None:
    core, apps = capped()
    a = bursting(core, apps, top)
    now = 1000.0
    for i, limit in enumerate(limits):
        up = a.burst(KEY, SHARE, now)
        assert isinstance(up, actuator.Actuation) and up.cpu == float(actuator.qty(limit))
        assert cpu_limit(core, "api-1-a") == limit and a.bursts[KEY]["steps"] == i + 1
        early = a.burst(KEY, SHARE, now + actuator.BURST_EVERY - 1)
        assert early == "burst 44 minutes ago: one a container every 45 minutes"
        now += actuator.BURST_EVERY
    assert a.burst(KEY, SHARE, now) == (
        f"it is at {top:g} times its template's limit ({limits[-1]}): raise it in its manifest"
    )
    assert cpu_limit(core, "api-1-a") == limits[-1] and a.bursts[KEY]["steps"] == len(limits)
    assert float(actuator.BURST_STEP) == 0.25 and actuator.BURST_EVERY == 2700


def vpa(mode: str = "Auto") -> dict[str, Any]:
    target = {"kind": "Deployment", "name": "api"}
    return {"items": [{"spec": {"targetRef": target, "updatePolicy": {"updateMode": mode}}}]}


RESTARTS = [k.V1ContainerResizePolicy(resource_name="cpu", restart_policy="RestartContainer")]


@pytest.mark.parametrize(
    ("setup", "why"),
    [
        ("no limit", "its template sets no CPU limit"),
        ("paused", "the Workload is paused"),
        ("guaranteed", "it is Guaranteed: its CPU limit can't leave its request"),
        ("restarts", "its resizePolicy restarts the container for cpu"),
        ("vpa", "a VPA in mode Auto sizes it"),
        ("rolling", "its Sizing is still rolling out"),
        ("max", "cpu 250m is over its LimitRange's maximum 240m"),
        ("ratio", "cpu 100m under limit 250m passes its LimitRange's ratio 2"),
        ("quota", "ResourceQuota team counts limits.cpu"),
        ("no pods", "no live pods"),
        ("forbidden", "reading it: 403: Forbidden"),
        ("cron", "a CronJob's pods get none"),
        ("job", "a Job's pods get none"),
        ("budget", "the cluster's 10 Step-ups and Bursts an hour are used"),
        ("bursts", "the cluster's 5 Bursts an hour are used"),
        ("changed", "its template's CPU limit changed"),
    ],
)
def test_a_burst_it_does_not_make_and_why(setup: str, why: str) -> None:
    core, apps = capped(
        limit=None if setup == "no limit" else "200m",
        request="200m" if setup == "guaranteed" else "100m",
        policy=RESTARTS if setup == "restarts" else None,
    )
    a = bursting(core, apps)
    key, now = KEY, 5000.0
    if setup == "paused":
        apps.read_namespaced_deployment.return_value.metadata.annotations = {actuator.PAUSE: "true"}
    if setup == "guaranteed":  # its memory too: every container asks what it may use
        c = apps.read_namespaced_deployment.return_value.spec.template.spec.containers[0]
        c.resources = k.V1ResourceRequirements(
            requests={"cpu": "200m", "memory": "64Mi"}, limits={"cpu": "200m", "memory": "64Mi"}
        )
    if setup == "vpa":
        a.custom.list_namespaced_custom_object.return_value = vpa()
    if setup == "rolling":
        a.held[KEY] = {"run_id": 1, "outcome": "rolling", "requests": {"cpu": "100m"}}
    if setup == "max":
        core.list_namespaced_limit_range.return_value = limit_range(max={"cpu": "240m"})
    if setup == "ratio":
        ratio = limit_range(max_limit_request_ratio={"cpu": "2"})
        core.list_namespaced_limit_range.return_value = ratio
    if setup == "quota":  # however much room it leaves now
        core.list_namespaced_resource_quota.return_value = quota({"limits.cpu": "100"}, {})
    if setup == "no pods":
        core.list_namespaced_pod.return_value = k.V1PodList(items=[])
    if setup == "forbidden":
        apps.read_namespaced_deployment.side_effect = refusal(403, "Forbidden")
    if setup in ("cron", "job"):
        key = f"shop/{'CronJob' if setup == 'cron' else 'Job'}/api/app"
    if setup == "budget":
        a.steps = [now - 3599 + i for i in range(actuator.STEPS_AN_HOUR)]
    if setup == "bursts":
        a.bursts = {
            f"shop/Deployment/w{i}/app": {"limit": "250m", "template": "200m", "at": now - 3599}
            for i in range(actuator.BURSTS_AN_HOUR)
        }
    if setup == "changed":
        a.bursts[KEY] = {"limit": "250m", "template": "150m", "at": 0.0, "steps": 1}
    before = dict(a.bursts)
    assert a.burst(key, SHARE, now) == why
    assert resized(core) == [] and a.bursts == before and not core.create_namespaced_event.called
    assert len(a.steps) == (actuator.STEPS_AN_HOUR if setup == "budget" else 0)


def test_what_does_not_hold_a_burst_back() -> None:
    core, apps = capped()
    a = bursting(core, apps)
    a.custom.list_namespaced_custom_object.return_value = vpa("Off")
    core.list_namespaced_limit_range.return_value = limit_range(
        max={"cpu": "250m"}, max_limit_request_ratio={"cpu": "2.5"}
    )
    core.list_namespaced_resource_quota.return_value = quota({"requests.cpu": "1"}, {})
    a.held[KEY] = {"run_id": 1, "outcome": "applied", "requests": {"cpu": "100m"}}
    a.steps = [1.0] * actuator.STEPS_AN_HOUR  # an hour and more ago: not this hour's
    a.bursts = {  # four this hour, and one that isn't
        f"shop/Deployment/w{i}/app": {"limit": "250m", "template": "200m", "at": 5000.0 - 60 * i}
        for i in (1, 2, 3, 4, 61)
    }
    a.autoscaling.list_namespaced_horizontal_pod_autoscaler.return_value = MagicMock(
        items=[hpa()]  # on cpu: its requests are untouched, so nothing it divides by moves
    )
    up = a.burst(KEY, SHARE, 5000.0)
    assert isinstance(up, actuator.Actuation) and up.pods == 2 and a.steps == [5000.0]
    assert (actuator.STEPS_AN_HOUR, actuator.BURSTS_AN_HOUR) == (10, 5)


def test_a_limit_alone_is_its_request_for_a_limit_ranges_ratio() -> None:
    core, apps = capped(request=None)  # its pods ask their limit: 250m over 200m
    a = bursting(core, apps)
    core.list_namespaced_limit_range.return_value = limit_range(
        max_limit_request_ratio={"cpu": "1.2"}
    )
    assert a.burst(KEY, SHARE, 1.0) == "cpu 200m under limit 250m passes its LimitRange's ratio 1.2"


def test_a_burst_no_pod_took_is_none_and_one_some_refused_is_said() -> None:
    core, apps = capped()
    a = bursting(core, apps)
    patch = core.patch_namespaced_pod_resize.side_effect
    core.patch_namespaced_pod_resize.side_effect = refusal(422, "no")
    assert a.burst(KEY, SHARE, 1.0) == ("every pod refused it: api-1-a: 422: no; api-1-b: 422: no")
    assert a.bursts == {} and a.steps == []  # not kept, not counted

    def one(name: str, ns: str, body: Any) -> None:
        if name == "api-1-b":
            raise refusal(422, "no")
        patch(name, ns, body)

    core.patch_namespaced_pod_resize.side_effect = one
    up = a.burst(KEY, SHARE, 2.0)
    assert isinstance(up, actuator.Actuation) and up.pods == 1
    assert up.reason == (
        f"CPU limit 200m to 250m on 1 of 2 pods in place; 1 refused: api-1-b: 422: no: {SHARE}"
    )
    core.patch_namespaced_pod_resize.side_effect = refusal(422, "no")
    assert str(a.burst(KEY, SHARE, 2.0 + actuator.BURST_EVERY)).startswith("every pod refused")
    assert a.bursts[KEY]["limit"] == "250m" and a.steps == [2.0]  # the one before is kept
    high = capped()
    for p in high[0].list_namespaced_pod.return_value.items[:2]:
        p.spec.containers[0].resources.limits = {"cpu": "500m"}  # boosted above it meanwhile
    b = bursting(*high)
    up = b.burst(KEY, SHARE, 3.0)  # none to raise is none refused: it stands, for new pods
    assert isinstance(up, actuator.Actuation) and up.pods == 0 and resized(high[0]) == []
    assert b.bursts[KEY]["limit"] == "250m"


def listed(share: float | None = 0.8, name: str = "api", **more: Any) -> actuator.Throttled:
    return actuator.Throttled(**{**SHOP_API, "name": name}, throttled=share, **more)


def test_one_read_is_one_burst_and_a_reason_not_to_is_said_once_a_day() -> None:
    core, apps = capped()
    a = bursting(core, apps)
    assert a.burst_all([listed(None)], 1, 100.0) == []  # not throttled enough: nothing due
    assert a.burst_all([listed()], None, 100.0) == [] and a.burst_seen == {}  # no read named
    (up,) = a.burst_all([listed()], 1, 100.0)
    assert up.outcome == "burst" and up.reason.endswith(f": {SHARE}") and a.burst_seen == {KEY: 1}
    assert a.burst_all([listed()], 1, 100.0 + actuator.BURST_EVERY) == []  # that read: handled
    assert a.burst_all([listed()], None, 100.0 + actuator.BURST_EVERY) == []  # nor one unnamed
    assert a.burst_seen == {KEY: 1}
    # a Revert meanwhile: the Hub lists it no more, its Burst is dropped, and an approval
    # lists it again within that read, which makes no second Burst
    assert a.burst_all([], 1, 110.0) == [] and a.burst_seen == {KEY: 1}
    was = a.bursts.pop(KEY)
    assert a.burst_all([listed()], 1, 120.0 + actuator.BURST_EVERY) == []
    assert a.bursts == {} and len(resized(core)) == 2
    a.bursts[KEY] = was
    (no,) = a.burst_all([listed(0.5)], 2, 200.0)  # a `sizing refresh` minutes later
    assert (no.outcome, no.cpu, no.pods) == ("throttled", None, 0)
    assert no.reason == (
        "throttled in 50% of its CPU periods over the last hour. No Burst: burst 1 minutes "
        "ago: one a container every 45 minutes"
    )
    assert a.burst_seen == {KEY: 2} and len(resized(core)) == 2
    a.burst_max = 1.25  # at its cap from here on: a standing reason
    at = 200.0 + actuator.BURST_EVERY
    (cap,) = a.burst_all([listed()], 3, at)
    assert actuator.BURST_CAP in cap.reason and actuator.to_tell(cap.outcome, cap.reason)
    assert a.burst_all([listed()], 4, at + 3600) == [] and a.burst_seen == {KEY: 4}
    assert a.burst_all([listed()], 5, at + 86399) == []  # the same reason: once a day
    (again,) = a.burst_all([listed()], 6, at + 86400)
    assert again.reason == cap.reason
    apps.read_namespaced_deployment.return_value.metadata.annotations = {actuator.PAUSE: "true"}
    (other,) = a.burst_all([listed()], 7, at + 86500)  # another reason: said at once
    assert other.reason.endswith("No Burst: the Workload is paused")
    a.burst_all([listed(name="web")], 8, at + 86600)  # a new read: the last one's forgotten
    assert a.burst_seen == {"shop/Deployment/web/app": 8}


def test_a_burst_waits_out_a_sizing_that_still_rolls_and_is_made_in_that_read() -> None:
    core, apps = capped()
    a = bursting(core, apps)
    a.held[KEY] = {"run_id": 3, "outcome": "rolling", "requests": {"cpu": "100m"}}
    (no,) = a.burst_all([listed()], 1, 100.0)  # the Sizing and the read came in one answer
    assert no.reason.endswith("No Burst: its Sizing is still rolling out") and no.run_id == 3
    assert a.burst_seen == {}  # not handled: tried again the next minute
    assert a.burst_all([listed()], 1, 160.0) == []  # said once
    a.held[KEY]["outcome"] = "applied"
    (up,) = a.burst_all([listed()], 1, 220.0)
    assert up.outcome == "burst" and a.burst_seen == {KEY: 1}


def test_a_read_handled_is_kept_across_a_restart_and_fifty_containers_a_minute() -> None:
    core, apps = capped()
    a = bursting(core, apps)
    a.burst_max = 1.25
    a.burst_all([listed()], 1, 100.0)
    a.burst_all([listed(0.5)], 2, 200.0)  # declined: 45 minutes
    a.save()
    core.read_namespaced_config_map.return_value = k.V1ConfigMap(
        data=core.replace_namespaced_config_map.call_args.args[2].data
    )
    b = bursting(core, apps)
    b.load()
    assert b.burst_seen == {KEY: 2} and b.bursts == a.bursts
    assert b.burst_all([listed(0.5)], 2, 200.0 + actuator.BURST_EVERY) == []  # never twice a read
    many = [listed(name=f"w{i:02}") for i in range(60)]
    apps.read_namespaced_deployment.side_effect = refusal(404, "Not Found")
    first = b.burst_all(many, 3, 1.0)
    assert len(first) == actuator.OOM_REPORTS == 50 and first[0].reason.endswith(
        "No Burst: reading it: 404: Not Found"
    )
    assert [x.name for x in b.burst_all(many, 3, 1.0)] == [f"w{i}" for i in range(50, 60)]
    assert b.burst_all(many, 3, 1.0) == []
    # the next read, the same standing reason: nothing to say, and still 50 looked at a minute
    reads = apps.read_namespaced_deployment.call_count
    assert b.burst_all(many, 4, 2.0) == [] and len(b.burst_seen) == 50
    assert apps.read_namespaced_deployment.call_count == reads + 50
    assert b.burst_all(many, 4, 2.0) == [] and len(b.burst_seen) == 60


def test_a_minute_makes_a_burst_only_when_the_hub_caps_it_and_took_its_reports() -> None:
    core, apps = capped()
    seen: list[httpx.Request] = []
    waiting = actuation(9)
    answers = [throttling(), throttling(), throttling(), throttling(killed=True), throttling(2)]
    a = make(hub_of(answers, seen), core, apps, unsent=[waiting])
    a.http.reports.append(httpx.Response(503))  # type: ignore[attr-defined]
    a.step()  # a report the Hub hasn't taken: no Burst behind it
    assert a.bursts == {} and resized(core) == [] and a.burst_max is None
    a.step()  # taken at this minute's end: still none
    assert posted(seen)[-1] == ["applied"] and a.bursts == {}
    a.step()
    assert posted(seen)[-1] == ["burst"] and cpu_limit(core, "api-1-a") == "250m"
    assert a.burst_expires == DAY_S and a.burst_peaks == {KEY: None}
    assert not apps.list_namespaced_deployment.called  # made on its template's own word
    a.bursts[KEY]["at"] -= 2 * DAY_S  # long past its term, and the Kill switch is on
    a.step()
    apps.list_namespaced_deployment.assert_called_with("shop")  # its pause is read from now
    assert KEY in a.bursts and cpu_limit(core, "api-1-a") == "250m"  # nothing ended
    a.bursts[KEY]["at"] = time.time() - actuator.BURST_EVERY
    a.step()  # the next read, the switch off again
    assert posted(seen)[-1] == ["burst"] and cpu_limit(core, "api-1-a") == "300m"


def test_a_step_up_takes_its_place_in_the_hour_before_a_burst(monkeypatch: Any) -> None:
    core, apps = capped()
    a = make(hub_of([{**throttling(), **reflexing()}], []), core, apps)
    order: list[str] = []
    monkeypatch.setattr(a, "ooms", lambda w: order.append("ooms") or [])
    monkeypatch.setattr(a, "burst_all", lambda *x: order.append("bursts") or [])
    a.step()
    assert order == ["ooms", "bursts"]


MADE = {"limit": "250m", "template": "200m", "at": 0.0, "steps": 1, "expires": DAY_S}


def burst_pod(**resources: Any) -> dict[str, Any]:
    """A new pod of api, its `app` asking 100m of cpu under a limit of 200m unless said."""
    return new_pod(**(resources or {"requests": {"cpu": "100m"}, "limits": {"cpu": "200m"}}))


def test_a_new_pod_starts_at_its_containers_burst_and_a_boost_goes_on_top_of_it() -> None:
    bursts = {KEY: dict(MADE)}
    ops = actuator.mutation(burst_pod()["request"], {}, "x", bursts=bursts)
    assert ops == [{"op": "add", "path": CPU, "value": "250m"}]  # the limit only, no annotation
    both = actuator.mutation(burst_pod()["request"], {}, "x", boosts={KEY: 2.0}, bursts=bursts)
    assert both == [
        {"op": "add", "path": CPU, "value": "500m"},  # twice its Burst's
        # given back to its Burst's limit, not its template's
        {"op": "add", "path": NOTE, "value": {actuator.BOOST: own("250m")}},
    ]
    alone = actuator.mutation(burst_pod(limits={"cpu": "200m"})["request"], {}, "x", bursts=bursts)
    assert alone == [  # a limit alone is its request too: said, so the request stays
        {"op": "add", "path": "/spec/containers/1/resources/requests", "value": {"cpu": "200m"}},
        {"op": "add", "path": CPU, "value": "250m"},
    ]
    review = burst_pod()
    answer = actuator.admit(review, {}, False, "x", bursts=bursts)["response"]
    assert json.loads(base64.b64decode(answer["patch"])) == ops
    assert actuator.admit(review, {}, True, "x", bursts=bursts)["response"].get("patch") is None
    paused = frozenset({"shop/Deployment/api"})
    assert actuator.mutation(review["request"], {}, "x", paused, bursts=bursts) == []
    noted = burst_pod()  # one that carries the annotation gets no boost, and its Burst still
    noted["request"]["object"]["metadata"]["annotations"] = {actuator.BOOST: own("100m")}
    assert actuator.mutation(noted["request"], {}, "x", boosts={KEY: 2.0}, bursts=bursts) == ops


@pytest.mark.parametrize(
    "review",
    [
        burst_pod(requests={"cpu": "100m"}, limits={"cpu": "300m"}),  # no longer its template's
        burst_pod(requests={"cpu": "100m"}, limits={"memory": "1Gi"}),  # no CPU limit now
        burst_pod(none=True),
        new_pod(  # Guaranteed since
            sidecar={"resources": {"limits": {"cpu": "200m", "memory": "1Gi"}}},
            limits={"cpu": "200m", "memory": "1Gi"},
        ),
        owned_by("Job", run_of("api"), requests={"cpu": "100m"}, limits={"cpu": "200m"}),
        pod_level(),
    ],
)
def test_a_new_pod_that_does_not_start_at_a_burst(review: Any) -> None:
    bursts = {KEY: dict(MADE), CRON: dict(MADE)}
    if (review["request"]["object"]["spec"].get("resources") or {}).get("limits"):
        review["request"]["object"]["spec"]["containers"][1]["resources"]["limits"] = {
            "cpu": "200m"
        }
        review["request"]["object"]["spec"]["containers"][1]["resources"]["requests"] = {
            "cpu": "100m"
        }
    assert actuator.mutation(review["request"], {}, "squero-actuator", bursts=bursts) == []


def burst_at(core: Any, apps: Any, ago: float = 60.0, **kw: Any) -> actuator.Actuator:
    """An Actuator holding api's Burst to 250m, made `ago` seconds ago, its pods at it."""
    a = bursting(core, apps, **kw)
    a.bursts[KEY] = {**MADE, "at": time.time() - ago}
    for p in core.list_namespaced_pod.return_value.items[:2]:
        p.spec.containers[0].resources.limits = {"cpu": "250m"}
    return a


def test_each_minute_a_pod_below_its_burst_is_brought_to_it_and_none_is_lowered() -> None:
    core, apps = capped()
    a = burst_at(core, apps)
    assert a.keep_bursts() == [] and resized(core) == []  # every pod at it
    low, high = core.list_namespaced_pod.return_value.items[:2]
    low.spec.containers[0].resources.limits = {"cpu": "200m"}  # the webhook was down
    high.spec.containers[0].resources.limits = {"cpu": "500m"}  # boosted for its start
    (up,) = a.keep_bursts()
    assert (up.outcome, up.pods, up.cpu) == ("burst", 1, 0.25)
    assert up.reason == "brought to its Burst, CPU limit 250m on 1 of 2 pods in place"
    assert not actuator.to_tell(up.outcome, up.reason)
    assert resized(core) == [("api-1-a", cpu_body("250m"))] and KEY in a.bursts
    a.paused = frozenset({"shop/Deployment/api"})
    low.spec.containers[0].resources.limits = {"cpu": "200m"}
    assert a.keep_bursts() == [] and cpu_limit(core, "api-1-a") == "200m"  # paused: left
    a.paused = frozenset()
    core.patch_namespaced_pod_resize.side_effect = refusal(500, "busy")
    assert a.keep_bursts() == [] and KEY in a.bursts  # tried again next minute
    apps.read_namespaced_deployment.side_effect = refusal(500, "busy")
    assert a.keep_bursts() == [] and KEY in a.bursts  # the API server's trouble: kept


@pytest.mark.parametrize(
    ("change", "why"),
    [
        ("raised", "its template's CPU limit changed: 200m to 250m: its Burst dropped"),
        ("unset", "its template's CPU limit changed: 200m to unset: its Burst dropped"),
        ("renamed", "no container 'app' in its template: its Burst dropped"),
        ("gone", "no such Deployment: its Burst dropped"),
    ],
)
def test_a_person_changing_the_templates_cpu_limit_gets_the_container_back(
    change: str, why: str
) -> None:
    core, apps = capped()
    a = burst_at(core, apps)
    c = apps.read_namespaced_deployment.return_value.spec.template.spec.containers[0]
    if change == "raised":  # the limit Slack named: theirs from here on
        c.resources.limits = {"cpu": "250m"}
    if change == "unset":
        c.resources.limits = None
    if change == "renamed":
        c.name = "web"
    if change == "gone":
        apps.read_namespaced_deployment.side_effect = refusal(404, "Not Found")
    (end,) = a.keep_bursts()
    assert (end.outcome, end.pods, end.cpu, end.reason) == ("burst_ended", 0, None, why)
    assert a.bursts == {} and resized(core) == []  # its pods are the rollout's, not touched
    event = core.create_namespaced_event.call_args.args[1]
    assert (event.reason, event.message) == ("HandedBack", f"app: {why}")
    same = apps.read_namespaced_deployment.return_value.spec.template.spec.containers[0]
    if change == "raised":
        same.resources.limits = {"cpu": "0.25"}  # the same quantity, written otherwise
        b = burst_at(core, apps)
        b.bursts[KEY]["template"] = "250m"
        assert b.keep_bursts() == [] and KEY in b.bursts


@pytest.mark.parametrize(
    ("peak", "kept"),
    [(0.181, True), (None, True), (0.18, False), (0.05, False), ("unlisted", False)],
)
def test_a_burst_ends_at_its_term_unless_its_cpu_peak_says_it_is_still_needed(
    peak: Any, kept: bool
) -> None:
    core, apps = capped()
    a = burst_at(core, apps, ago=DAY_S)
    boosted_ = core.list_namespaced_pod.return_value.items[1]
    boosted_.spec.containers[0].resources.limits = {"cpu": "500m"}  # on top of its Burst
    if peak != "unlisted":
        a.burst_peaks = {KEY: peak}
    (said,) = a.keep_bursts()
    if kept:
        how = "not known" if peak is None else "181m, above"
        assert (said.outcome, said.pods, said.cpu) == ("burst", 0, 0.25)
        assert said.reason == (
            f"kept for another term: its CPU peak is {how} 90% of its template's limit 200m"
        )
        assert not actuator.to_tell(said.outcome, said.reason)
        assert KEY in a.bursts and resized(core) == []
        assert a.keep_bursts() == []  # a term from now
        a.bursts[KEY]["renewed"] -= DAY_S - 1
        assert a.keep_bursts() == []
        a.bursts[KEY]["renewed"] -= 1
        assert len(a.keep_bursts()) == 1
        return
    why = (
        "the Hub no longer listing it"
        if peak == "unlisted"
        else f"its CPU peak {actuator.cpu_text(peak)} within 90% of its template's limit 200m"
    )
    assert (said.outcome, said.cpu) == ("burst_ended", None) and a.bursts == {}
    assert said.reason == (
        f"its term of 24 hours over, {why}: 2 of 2 pods back to CPU limit 200m in place"
    )
    # every pod above its template's limit, one still boosted on top of the Burst too
    assert resized(core) == [("api-1-a", cpu_body("200m")), ("api-1-b", cpu_body("200m"))]
    event = core.create_namespaced_event.call_args.args[1]
    assert (event.reason, event.message) == ("BurstExpired", f"app: {said.reason}")
    assert float(actuator.BURST_KEEP) == 0.9


def test_a_bursts_term_is_its_own_and_not_yet_over_a_second_before() -> None:
    core, apps = capped()
    a = burst_at(core, apps, ago=DAY_S - 5)
    assert a.keep_bursts() == [] and KEY in a.bursts
    a.burst_expires = None  # the Reflex switched off: the Hub sends no term, and lists none
    a.bursts[KEY]["at"] -= 10
    (end,) = a.keep_bursts()  # it still ends, at the term it was made with
    assert end.outcome == "burst_ended" and "the Hub no longer listing it" in end.reason
    b = burst_at(*capped(), ago=3 * 3600)
    b.burst_expires = 2 * 3600  # the Contract's term shortened since: its own from then on
    (end,) = b.keep_bursts()
    assert end.reason.startswith("its term of 2 hours over") and b.bursts == {}
    c = burst_at(*capped(), ago=DAY_S)
    c.burst_peaks = {KEY: 0.05}
    c.apps.list_namespaced_replica_set.side_effect = refusal(500, "busy")
    (end,) = c.keep_bursts()
    assert end.reason.endswith(
        "its pods keep their CPU limit until they start again (reading it: 500: busy)"
    )
    d = burst_at(*capped(), ago=DAY_S)
    d.burst_peaks = {KEY: 0.05}
    d.core.patch_namespaced_pod_resize.side_effect = refusal(422, "no")
    (end,) = d.keep_bursts()
    assert end.reason.endswith(
        "0 of 2 pods back to CPU limit 200m in place; 2 refused: api-1-a: 422: no; api-1-b: 422: no"
    )


def test_each_minute_the_hub_answers_a_burst_is_kept_and_ended_by_the_peak_it_sends() -> None:
    core, apps = capped()
    a = burst_at(core, apps, ago=DAY_S)
    low = core.list_namespaced_pod.return_value.items[0]
    seen: list[httpx.Request] = []
    answers = [throttling(share=None, peak=0.19), throttling(share=None, peak=0.05)]
    a.http = hub_of(answers, seen)
    low.spec.containers[0].resources.limits = {"cpu": "200m"}  # started with the webhook down
    a.step()  # a peak above 90% of its template's limit: another term, and its pod brought up
    assert posted(seen) == [["burst", "burst"]] and cpu_limit(core, "api-1-a") == "250m"
    assert a.burst_peaks == {KEY: 0.19} and "renewed" in a.bursts[KEY]
    a.bursts[KEY]["renewed"] -= DAY_S
    a.step()  # a peak within it: over
    assert posted(seen)[-1] == ["burst_ended"] and a.bursts == {}
    assert cpu_limit(core, "api-1-a") == cpu_limit(core, "api-1-b") == "200m"


def test_a_revert_drops_a_burst_and_lowers_its_pods() -> None:
    core, apps = capped()
    a = burst_at(core, apps)
    undo = {"id": 4, **SHOP_API}
    seen: list[httpx.Request] = []
    a.http = hub_of([{**throttling(share=None), "reverts": [undo]}], seen)
    a.step()
    assert posted(seen) == [["reverted"]] and a.bursts == {}
    assert cpu_limit(core, "api-1-a") == cpu_limit(core, "api-1-b") == "200m"
    reason = json.loads(seen[-1].content)[0]["reason"]
    assert reason == (
        "reverted by the Operator (4); its Burst dropped: 2 of 2 pods back to CPU limit "
        "200m in place: nothing held"
    )


# --- slice 13.3b: the Burst, the Hub's side ---------------------------------------------------

MADE_SAID = f"CPU limit 200m to 250m on 2 of 2 pods in place: {SHARE}"
CAP_SAID = (
    f"{SHARE}. No Burst: it is at 2 times its template's limit (400m): raise it in its manifest"
)


def test_what_of_a_burst_the_operator_is_told() -> None:
    assert actuator.to_tell("burst", MADE_SAID)
    assert not actuator.to_tell("burst", "brought to its Burst, CPU limit 250m on 1 of 2 pods")
    assert not actuator.to_tell("burst", "kept for another term: its CPU peak is not known")
    assert actuator.to_tell("throttled", CAP_SAID)
    assert not actuator.to_tell("throttled", f"{SHARE}. No Burst: the Workload is paused")
    assert not actuator.to_tell("burst_ended", "its term of 24 hours over")
    assert not actuator.to_tell("stepped_up", MADE_SAID)  # each Reflex's own words only
    assert not actuator.to_tell("oom_killed", CAP_SAID)


def test_a_burst_and_a_container_throttled_at_its_cap_reach_the_operators_channel_and_no_other(
    settings: Settings, database_url: str
) -> None:
    app, slack = slacked(settings, database_url)
    now = time.time()
    none = {"run_id": 0, "memory": None}
    oom_cap = "OOM-killed: api-1-a. No Step-up: it is at 2 times its template's limit (256Mi)"
    sent = [
        report(**none, outcome="throttled", cpu=None, pods=0, at=now - 90000, reason=CAP_SAID),
        report(**none, outcome="burst", cpu=0.25, at=now - 50, reason=MADE_SAID),
        report(**none, outcome="burst", cpu=0.25, at=now - 40, pods=1)
        | {"reason": "brought to its Burst, CPU limit 250m on 1 of 2 pods in place"},
        report(**none, outcome="throttled", cpu=None, pods=0, at=now - 30)
        | {"reason": f"{SHARE}. No Burst: the Workload is paused"},
        report(**none, outcome="burst_ended", cpu=None, pods=0, at=now - 20, reason="expired"),
        # the same container at its Step-up's cap the same day: its own line, not this one's
        report(run_id=0, cpu=None, outcome="oom_killed", memory=1.0, at=now - 10, reason=oom_cap),
    ]
    with TestClient(app) as c:
        taken = c.post("/actuator/reports", json=sent, headers=as_actuator(A_TOKEN))
        assert taken.status_code == 204  # the Hub knows the three outcomes (migration 0033)
        tell(c)
        where = "test/shop/Deployment/api container app"
        assert posts(slack) == [  # in the order they happened
            (
                ACME_CHANNEL,
                f":warning: acme: {where} is still throttled at its Burst's cap: {CAP_SAID}",
            ),
            (
                ACME_CHANNEL,
                f":arrow_up: acme: the Actuator raised the CPU limit of {where}: {MADE_SAID}. "
                "It ends by itself once the need is gone; to keep it, set its CPU limit to "
                "250m in its manifest.",
            ),
            (
                ACME_CHANNEL,
                f":warning: acme: {where} is still OOM-killed at its Step-up's cap: {oom_cap}",
            ),
        ]
        assert untold(database_url) == []
        again = {**sent[0], "at": now - 80000}  # throttled again at its cap: said once a day
        c.post("/actuator/reports", json=[again], headers=as_actuator(A_TOKEN))
        tell(c)
        assert len(posts(slack)) == 3 and untold(database_url) == []
        odd = {**sent[1], "cpu": None, "at": now - 5}  # a Burst reported without its limit
        c.post(
            "/actuator/reports",
            json=[{**again, "at": now - 3000}, odd],
            headers=as_actuator(A_TOKEN),
        )
        tell(c)  # a day after the one that was said
    said = [text for _, text in posts(slack)]
    assert len(said) == 5 and said[3].startswith(":warning: acme:")
    assert said[4].endswith("to keep it, raise its CPU limit in its manifest.")
    assert slack.channels() == {ACME_CHANNEL}  # never bank's


def test_the_automation_view_shows_a_burst_and_reverts_a_container_with_only_one(
    settings: Settings, database_url: str
) -> None:
    from tests.test_console import OPERATOR, VIEW

    settings.console_operators = {OPERATOR}
    app, _ = slacked(settings, database_url)
    now = time.time()
    path, same = "/api/v1/contracts/acme/automation", {**VIEW, "Sec-Fetch-Site": "same-origin"}
    burst = {"run_id": 0, "memory": None, "outcome": "burst", "cpu": 0.25}
    step = {"run_id": 0, "cpu": None, "outcome": "stepped_up", "memory": 228.0 * MI}

    def of(name: str, ago: float, **more: Any) -> dict[str, Any]:
        return report(name=name, at=now - ago, **{"reason": MADE_SAID, **more})

    sent = [
        of("api", 900, reason="2 of 2 pods resized in place"),  # a Sizing held, then burst
        of("api", 800, **burst),
        of("api", 700, **{**burst, "cpu": 0.3}),  # twice: its last one counts
        of("api", 650, **step),  # and a Step-up beside it: each its own
        of("api", 640, outcome="throttled", cpu=None, pods=0, memory=None),  # no row of its own
        of("lone", 600, **burst),  # nothing held of it but its Burst
        of("ended", 500, **burst),
        of("ended", 400, **{**burst, "outcome": "burst_ended", "cpu": None}, pods=0),
        of("stepped", 350, **step),
        of("stepped", 340, **{**burst, "outcome": "burst_ended", "cpu": None}, pods=0),
        of("undone", 300, reason="2 of 2 pods resized in place"),
        of("undone", 200, **burst),
        of("undone", 100, outcome="reverted", cpu=None, reason="reverted by the Operator (3)"),
    ]
    with TestClient(app) as c:
        c.post("/actuator/reports", json=sent, headers=as_actuator(A_TOKEN))
        c.post("/actuator/reports", json=[of("ledger", 50, **burst)], headers=as_actuator(B_TOKEN))
        # acme's other cluster, which runs no Actuator: never a row of this one's
        sql(database_url, rolled_back("acme", now - 60, "edge", "edgy", "burst"))
        page = c.get(path, headers=VIEW).json()
        theirs = c.get("/api/v1/contracts/bank/automation", headers=VIEW).json()
        what = {"cluster": "test", "namespace": "shop", "container": "app", "kind": "Deployment"}
        lone = c.post(f"{path}/revert", json={**what, "name": "lone"}, headers=same)
        ended_ = c.post(f"{path}/revert", json={**what, "name": "ended"}, headers=same)
        other = c.post(f"{path}/revert", json={**what, "name": "ledger"}, headers=same)
    shown_ = {h["name"]: h for h in page["clusters"][0]["containers"]}
    assert list(shown_) == ["api", "lone", "stepped", "undone"]  # `ended` holds nothing: no row
    assert [(n, h["burst"], h["stepped"]) for n, h in shown_.items()] == [
        ("api", 0.3, 228.0 * MI),
        ("lone", 0.25, None),
        ("stepped", None, 228.0 * MI),  # a Burst's end ends no Step-up
        ("undone", None, None),  # a Revert dropped it
    ]
    assert shown_["api"]["outcome"] == "applied" and shown_["api"]["revertable"]
    assert shown_["api"]["cpu"] == 0.115  # its Sizing's request, never the Burst's limit
    assert shown_["lone"]["outcome"] == "burst" and shown_["lone"]["revertable"]
    assert shown_["lone"]["cpu"] is None  # its limit is `burst`: it holds no request
    assert not shown_["undone"]["revertable"]
    assert [h["name"] for h in theirs["clusters"][0]["containers"]] == ["ledger"]  # bank's own
    assert lone.status_code == 200 and (ended_.status_code, other.status_code) == (409, 409)
    assert next(h for h in lone.json()["clusters"][0]["containers"] if h["name"] == "lone")[
        "reverting"
    ]
