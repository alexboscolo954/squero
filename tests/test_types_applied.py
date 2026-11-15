"""Nodes (slice 15.3b): the Actuator applies a DaemonSet's Sizing per instance type, each
pod getting its node's type's; the Hub sends each type's requests and each node's type."""

import base64
import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from squero import actuator, sizing
from squero.config import Settings
from tests.test_actuator import (
    A_TOKEN,
    AUTO,
    B_TOKEN,
    MI,
    as_actuator,
    automating,
    body,
    hub,
    hub_of,
    kinded,
    limit_range,
    make,
    of,
    owned_by,
    owner,
    pod,
    quota,
    report,
    resized,
    run,
    scalers,
    sized,
    sql,
    start,
)

DS = "shop/DaemonSet/api/app"
# api-1-a's node, api-1-b's, and api-1-c's: a type no Sizing here is for
NODES = {"n1": "m5.large", "n2": "m5.4xlarge", "n3": "c5.large"}
BOTH = {"m5.large": {"cpu": 0.1, "memory": None}, "m5.4xlarge": {"cpu": 0.3, "memory": 64 * MI}}


def daemons(**kw: Any) -> tuple[Any, Any]:
    """A DaemonSet api with a pod on each of three nodes, each asking its template's cpu 1."""
    core, apps = kinded("DaemonSet", **kw)
    late = pod("api-1-c", "api-1")
    late.metadata.namespace = "shop"
    late.metadata.owner_references = [owner("DaemonSet", "api", "u1")]
    pods = core.list_namespaced_pod.return_value.items
    pods.append(late)
    for p in pods:
        p.spec.node_name = {"api-1-a": "n1", "api-1-b": "n2", "api-1-c": "n3"}.get(p.metadata.name)
    return core, apps


def typed(run_id: int = 1, types: dict[str, Any] | None = None, **more: Any) -> Any:
    w = of("DaemonSet", run_id=run_id, **({"cpu": 0.115, "memory": None} | more))
    return w.model_copy(update={"types": BOTH if types is None else types})


def go(core: Any, apps: Any, w: Any, old: Any = None, at: float = 0.0, **kw: Any) -> Any:
    return actuator.apply(core, apps, w, at, *scalers(), old, nodes=NODES, **kw)


def applied(core: Any, apps: Any, w: Any) -> dict[str, Any]:
    """`w` applied on every pod it changes; its hold."""
    _, h = go(core, apps, w)
    for at in (600.0, 660.0, 720.0):
        if h["outcome"] == "rolling":
            actuator.advance(core, apps, DS, h, at, nodes=NODES)
    assert h["outcome"] == "applied"
    return h


def test_each_pod_gets_its_nodes_types_requests_and_one_of_a_type_not_sized_its_templates() -> None:
    core, apps = daemons()
    a, h = go(core, apps, typed())
    assert a is None and resized(core) == [("api-1-a", body(cpu="100m"))]  # the canary, by n1
    assert h["types"] == {
        "m5.large": {"cpu": "100m"},
        "m5.4xlarge": {"cpu": "300m", "memory": "64Mi"},
    }
    assert h["requests"] == {"cpu": "300m", "memory": "64Mi"}  # the most any type asks
    assert h["template"] == {"cpu": "1", "memory": None}
    assert actuator.advance(core, apps, DS, h, 600.0, nodes=NODES) is None
    done = actuator.advance(core, apps, DS, h, 660.0, nodes=NODES)
    assert resized(core)[1:] == [("api-1-b", body(cpu="300m", memory="64Mi"))]  # c: left
    assert done is not None and (done.outcome, done.pods) == ("applied", 2)
    assert (done.cpu, done.memory) == (None, None)  # no one number: its reason has each
    assert done.reason == (
        "2 of 3 pods resized in place, one at a time; "
        "by instance type: m5.4xlarge cpu 300m, memory 64Mi; m5.large cpu 100m"
    )


def test_types_are_only_a_daemonsets_and_a_reason_names_three_of_them() -> None:
    core, apps = kinded("StatefulSet")
    _, h = go(core, apps, of("StatefulSet", memory=None).model_copy(update={"types": BOTH}))
    assert "types" not in h and resized(core) == [("api-1-a", body(cpu="115m"))]  # its one Sizing
    many = {"types": {t: {"cpu": "1"} for t in ("b", "a", "", "d", "c")}, "requests": {}}
    assert actuator.held_text(many) == "no instance type cpu 1; a cpu 1; b cpu 1 and 2 more"
    assert actuator.set_by(many) == {} and actuator.set_by({"requests": {"cpu": "1"}}) == {"cpu": 1}


def test_a_hold_of_one_set_becomes_one_per_type_and_back() -> None:
    core, apps = daemons()
    _, old = start(core, apps, of("DaemonSet", memory=None))  # cpu 115m for every pod
    for at in (600.0, 660.0, 720.0):
        actuator.advance(core, apps, DS, old, at)
    assert old["outcome"] == "applied" and len(resized(core)) == 3
    core.patch_namespaced_pod_resize.reset_mock()
    keeps = {"m5.large": {"cpu": None, "memory": None}, "m5.4xlarge": {"cpu": 0.3, "memory": None}}
    a, h = go(core, apps, typed(2, keeps), old, 1000.0)
    # m5.large keeps what its pods hold; api-1-c, of no sized type, goes back to its template's
    assert h["types"] == {"m5.large": {"cpu": "115m"}, "m5.4xlarge": {"cpu": "300m"}}
    assert a is None and resized(core) == [("api-1-b", body(cpu="300m"))]
    actuator.advance(core, apps, DS, h, 1600.0, nodes=NODES)
    assert actuator.advance(core, apps, DS, h, 1660.0, nodes=NODES) is not None
    assert resized(core)[1:] == [("api-1-c", body(cpu="1"))]
    # a type the next Sizing has not keeps what it holds
    _, later = go(core, apps, typed(3, {"m5.4xlarge": {"cpu": 0.5, "memory": None}}), h, 2000.0)
    assert later["types"] == {"m5.large": {"cpu": "115m"}, "m5.4xlarge": {"cpu": "500m"}}
    # a type first sized under a hold with types starts from its template's, not another's
    _, new = go(core, apps, typed(3, {"c5.large": {"cpu": None, "memory": None}}), h, 2000.0)
    assert set(new["types"]) == {"m5.large", "m5.4xlarge"}  # nothing for c5.large: no type of it
    # one Sizing for every pod again (fewer than two types left)
    core.patch_namespaced_pod_resize.reset_mock()
    _, one = go(core, apps, of("DaemonSet", run_id=4, cpu=0.2, memory=None), h, 3000.0)
    assert "types" not in one and one["requests"] == {"cpu": "200m"}
    assert resized(core) == [("api-1-a", body(cpu="200m"))]


def test_nothing_is_resized_while_no_nodes_type_is_known(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(actuator, "CANARY_SECONDS", 0)
    core, apps = daemons()
    a, h = actuator.apply(core, apps, typed(), 0.0, *scalers())
    assert a.outcome == "skipped"
    assert a.reason.startswith("its nodes' instance types are not known yet; by instance type")
    assert h["passing"] and not resized(core)  # tried again with the Hub's next answer
    _, h = go(core, apps, typed())
    core.patch_namespaced_pod_resize.reset_mock()
    for none in (None, {}):
        assert actuator.advance(core, apps, DS, h, 10.0**6, nodes=none) is None  # it waits
    assert h["outcome"] == "rolling" and not resized(core)
    h = applied(core, apps, typed())
    core.patch_namespaced_pod_resize.reset_mock()
    late = pod("api-1-d", "api-1")  # a pod started from its template on a new m5.large node
    late.metadata.namespace, late.spec.node_name = "shop", "n4"
    late.metadata.owner_references = [owner("DaemonSet", "api", "u1")]
    core.list_namespaced_pod.return_value.items.append(late)
    quiet = {"kill_switch": False, "sizings": []}  # no good read of the nodes: none sent
    seen: list[httpx.Request] = []
    known = {**quiet, "node_types": {**NODES, "n4": "m5.large"}}
    answers = [httpx.Response(503), quiet, known, quiet, quiet]
    act = make(hub_of(answers, seen), core, apps, held={DS: h})  # a restart: it knows no type
    act.step()  # the Hub away
    act.step()  # the Hub back, with no node's type
    assert not resized(core) and act.held[DS]["outcome"] == "applied"
    assert dict(seen[0].url.params)["types"] == "1"  # it says it knows types
    act.step()  # each node's type: the new pod catches up to its type's
    assert resized(core) == [("api-1-d", body(cpu="100m"))]
    assert act.held[DS]["types"] == h["types"]
    act.step()  # an answer with no node's type: what it knew stays
    assert act.held[DS]["outcome"] == "applied" and act.node_types["n4"] == "m5.large"
    assert core.create_namespaced_event.call_args.args[1].message.startswith(
        "app: requests by instance type (Approved Sizing, run 1): 1 of 4 pods resized in place"
    )
    more = pod("api-1-e", "api-1")
    more.metadata.namespace, more.spec.node_name = "shop", "n2"
    more.metadata.owner_references = [owner("DaemonSet", "api", "u1")]
    core.list_namespaced_pod.return_value.items.append(more)
    act.step()
    assert resized(core)[1:] == [("api-1-e", body(cpu="300m", memory="64Mi"))]
    for none in (None, {}):  # by itself too: never a guess
        assert actuator.wants(h, more, none) == {}
    assert actuator.wants(h, more, {"n2": "c5.large"}) == {"cpu": "1"}  # its template's


def test_a_rollback_puts_every_types_pods_back_and_leaves_the_pod_it_never_resized() -> None:
    core, apps = daemons()
    h = applied(core, apps, typed())
    pods = {p.metadata.name: p for p in core.list_namespaced_pod.return_value.items}
    assert actuator.at_hold(h, pods["api-1-a"], "app") and actuator.at_hold(
        h, pods["api-1-b"], "app"
    )
    assert not actuator.at_hold(h, pods["api-1-c"], "app")  # its template's: not its doing
    assert not actuator.at_hold({**h, "types": {"m5.large": {}}}, pods["api-1-c"], "app")
    pods["api-1-a"].spec.node_name = "n3"  # wherever it runs now, it has a type's requests
    assert actuator.at_hold(h, pods["api-1-a"], "app")
    core.patch_namespaced_pod_resize.reset_mock()
    a = actuator.put_back(core, apps, DS, h, "rolled_back", "api-1-a: app restarted", 2000.0)
    assert resized(core) == [("api-1-a", body(cpu="1")), ("api-1-b", body(cpu="1"))]
    assert a.pods == 2 and a.reason == (
        "api-1-a: app restarted: 2 pods back from each instance type's requests: m5.4xlarge "
        "cpu 300m, memory 64Mi; m5.large cpu 100m to their template's cpu 1 "
        "(['memory'] left: the template sets none)"
    )
    assert actuator.undo({**h, "unfinished": True})["types"] == h["types"]  # for its finish
    assert actuator.undo(h) == {}


def test_a_pre_check_that_fails_for_one_type_skips_the_container_never_clamped() -> None:
    core, apps = daemons()
    core.list_namespaced_limit_range.return_value = limit_range(min={"cpu": "200m"})
    a, _ = go(core, apps, typed())
    assert a.outcome == "skipped" and not resized(core)
    assert a.reason.startswith(
        "cpu 100m is under its LimitRange's minimum 200m (its pods' on m5.large); by instance"
    )
    core, apps = daemons()
    core.list_namespaced_resource_quota.return_value = quota(
        {"requests.cpu": "6"}, {"requests.cpu": "3"}
    )
    more = {"m5.large": {"cpu": 2.0, "memory": None}, "m5.4xlarge": {"cpu": 3.0, "memory": None}}
    a, _ = go(core, apps, typed(types=more))  # a quota is held to the most any type asks
    assert a.reason.startswith("3 pods at cpu 3000m would pass ResourceQuota team's requests.cpu 6")
    core, apps = daemons(limits={"cpu": "200m"})
    _, h = go(core, apps, typed(types={"m5.large": {"cpu": 0.1}, "m5.4xlarge": {"cpu": 0.3}}))
    assert h["types"] == {"m5.large": {"cpu": "100m"}, "m5.4xlarge": {"cpu": "200m"}}  # its limit
    assert "; capped at its limit: ['cpu']" in h["notes"]


def test_automations_churn_rule_is_each_types_own() -> None:
    core, apps = daemons()
    h = applied(core, apps, typed())  # m5.large cpu 100m; m5.4xlarge cpu 300m, memory 64Mi

    def auto(old: Any = h, **cpu: float) -> Any:
        types = {t: {"cpu": v, "memory": None} for t, v in cpu.items()}
        return go(core, apps, typed(2, types), old, 10.0**6, automated=True)

    assert auto(**{"m5.large": 0.104, "m5.4xlarge": 0.31}) == (None, {})  # 4% and 3% up
    _, one = auto(**{"m5.large": 0.104, "m5.4xlarge": 0.5})  # only m5.4xlarge moves enough
    assert one["types"] == {
        "m5.large": {"cpu": "100m"},
        "m5.4xlarge": {"cpu": "500m", "memory": "64Mi"},  # its memory kept as held
    }
    assert one["deferred"] == ["cpu"] and "lowered_at" not in one
    recent = {**h, "lowered_at": 10.0**6 - 86399}  # lowered under a day ago
    assert auto(recent, **{"m5.large": 0.05, "m5.4xlarge": 0.31}) == (None, {})
    _, down = auto(**{"m5.large": 0.05, "m5.4xlarge": 0.31})
    assert down["types"]["m5.large"] == {"cpu": "50m"} and down["lowered_at"] == 10.0**6


def test_a_step_up_keeps_the_memory_of_every_type() -> None:
    core, apps = daemons()
    a, h = go(core, apps, typed(), stepped=True)
    assert a is None and h["types"] == {"m5.large": {"cpu": "100m"}, "m5.4xlarge": {"cpu": "300m"}}
    assert h["notes"].startswith("; memory left to its Step-up; by instance type")
    only = {"m5.4xlarge": {"cpu": None, "memory": 64 * MI}}
    a, h = go(core, apps, typed(types=only), stepped=True)
    assert a.reason == "it asks memory only, which is its Step-up's while it lasts"
    assert h["requests"] == {} and "types" not in h
    core, apps = daemons()
    h = applied(core, apps, typed())
    assert not actuator.lowered(h)  # its template sets no memory: nothing lowered
    h["template"]["memory"] = "128Mi"
    assert actuator.lowered(h)  # m5.4xlarge's 64Mi is, whatever the others ask
    assert not actuator.lowered({**h, "types": {"m5.large": {"cpu": "100m"}}})
    actuator.stepped_over(h)  # a Step-up made: its memory is no Sizing's
    assert h["requests"] == {"cpu": "300m"} and h["template"] == {"cpu": "1"}
    assert h["types"] == {"m5.large": {"cpu": "100m"}, "m5.4xlarge": {"cpu": "300m"}}


# --- the webhook ----------------------------------------------------------------------------

HELD = {
    "shop/DaemonSet/db/app": {
        "run_id": 1,
        "outcome": "applied",
        "requests": {"cpu": "2"},
        "template": {"cpu": "1"},
        "types": {"m5.large": {"cpu": "100m"}, "m5.4xlarge": {"cpu": "300m"}, "big": {"cpu": "2"}},
    }
}


def for_node(*names: str, operator: str = "In", key: str = "metadata.name") -> dict[str, Any]:
    """A DaemonSet's new pod as its controller makes it (read on kind, v1.37): no nodeName,
    its node named in its node affinity."""
    review = owned_by("DaemonSet", "db")  # app asks cpu 1
    field = {"key": key, "operator": operator, "values": list(names)}
    review["request"]["object"]["spec"]["affinity"] = {
        "nodeAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [{"matchFields": [field]}]
            }
        }
    }
    return review


def given(review: Any, nodes: Any = NODES) -> Any:
    response = actuator.admit(review, HELD, False, "squero-actuator", nodes=nodes)["response"]
    if "patch" not in response:
        return None
    return [(op["path"], op["value"]) for op in json.loads(base64.b64decode(response["patch"]))]


def test_a_new_pod_gets_its_nodes_types_requests_where_they_are_lower() -> None:
    cpu = "/spec/containers/1/resources/requests/cpu"
    assert given(for_node("n1")) == [(cpu, "100m")]
    assert given(for_node("n2")) == [(cpu, "300m")]
    assert given(for_node("n3")) is None  # a type with no Sizing: its template's
    assert given(for_node("n9")) is None  # a node the Hub never said
    assert given(for_node("n1"), None) is None and given(for_node("n1"), {}) is None
    assert given(for_node("n1"), {"n1": "big"}) is None  # a raise: in place only
    assert given(owned_by("DaemonSet", "db")) is None  # no node named


def test_a_new_pods_node_is_read_from_its_affinity_or_its_node_name() -> None:
    def node(review: Any) -> Any:
        return actuator.node_of(review["request"]["object"])

    assert node(for_node("n1")) == "n1"
    assert node(for_node("n1", "n2")) is None  # not one node
    assert node(for_node("n1", operator="NotIn")) is None
    assert node(for_node("n1", key="metadata.namespace")) is None
    assert node(owned_by("DaemonSet", "db")) is None
    named = owned_by("DaemonSet", "db")
    named["request"]["object"]["spec"]["nodeName"] = "n7"
    assert node(named) == "n7" and actuator.node_of({}) is None


# --- the Hub --------------------------------------------------------------------------------


def group(kind: str, action: str = "lower", state: str = "ready") -> dict[str, Any]:
    target = 1.0 if action == "keep" else 0.05
    g = {"type": kind, "state": state, "pods": 2, "history_h": 200}
    if state == "ready":
        g["cpu"] = {"action": action, "current": 1.0, "target": target}
        g["memory"] = {"action": "keep", "current": 1.0, "target": 1.0}
    return g


GROUPS = [group("m5.large"), group("m5.4xlarge", "keep"), group("", state="learning")]
KEEP = {"action": "keep", "current": 1.0, "target": 1.0}


def rooms(cid: str, types: dict[str, str] | None) -> Any:
    read = {"nodes": [], "problem": None} | ({} if types is None else {"types": types})
    return ("insert into cluster_rooms values (%s, 'test', 1, %s)", (cid, json.dumps(read)))


def test_what_a_row_asks_by_type_and_whether_it_asks_anything() -> None:
    row = {"kind": "DaemonSet", "groups": GROUPS}
    assert sizing.by_type(row) == {
        "m5.large": {"cpu": 0.05, "memory": None},
        "m5.4xlarge": {"cpu": None, "memory": None},  # ready, keeping both; "" is learning
    }
    assert sizing.by_type({**row, "kind": "Deployment"}) == {}
    assert sizing.by_type({"kind": "DaemonSet"}) == {}
    assert sizing.changes(None, None, sizing.by_type(row))
    assert not sizing.changes(0.1, None, {"m5.4xlarge": {"cpu": None, "memory": None}})
    assert sizing.changes(0.1, None, {}) and not sizing.changes(None, None, {})


def test_an_actuator_that_knows_types_gets_each_types_sizing_and_each_nodes_type(
    settings: Settings, database_url: str
) -> None:
    acme = automating("acme", A_TOKEN, AUTO)
    bank = automating("bank", B_TOKEN, AUTO)
    ds = {"kind": "DaemonSet", "groups": GROUPS}
    types_only = (
        "insert into approved_sizings values ('acme', 1, 'test', 'shop', 'DaemonSet', 'ok', "
        "'app', null, null, 't', 1, %s)",
        (json.dumps({"m5.large": {"cpu": 0.05, "memory": None}}),),
    )
    with TestClient(hub(settings, database_url, acme, bank)) as c:
        sql(
            database_url,
            run("acme", '{"test": null}'),  # run 1
            sized("acme", 1, "agent", **ds, cpu=KEEP),  # only a type changes something
            sized("acme", 1, "both", **ds),  # its one Sizing lowers cpu too
            sized("acme", 1, "pooled", kind="DaemonSet", groups=[group("a", "keep")] * 2),
            sized("acme", 1, "api", groups=GROUPS),  # a Deployment has no types
            sized("acme", 1, "ok", **ds, cpu=KEEP),
            types_only,
            rooms("acme", {"n1": "m5.large"}),
            run("bank", '{"test": null}'),  # run 2
            sized("bank", 2, "api"),
            rooms("bank", {"b1": "c5.large"}),
        )
        kinds = {"kinds": ",".join(actuator.KINDS)}

        def get(token: str, **params: str) -> Any:
            return c.get("/actuator/desired", headers=as_actuator(token), params=kinds | params)

        knows, plain = get(A_TOKEN, types="1").json(), get(A_TOKEN).json()
        done = {"kind": "DaemonSet", "name": "agent", "cpu": None, "memory": None, "pods": 2}
        told = report(**done, reason="2 of 3 pods resized in place; by instance type: a cpu 50m")
        took = c.post("/actuator/reports", json=[told], headers=as_actuator(A_TOKEN))
        none = get(B_TOKEN, types="1").json()  # no Sizing of bank's has types: its nodes still
        sql(database_url, run("bank", '{"test": null}'), sized("bank", 3, "agent", **ds))
        theirs = get(B_TOKEN, types="1").json()
        sql(database_url, ("update cluster_rooms set rooms = rooms - 'types'", ()))
        unread = get(A_TOKEN, types="1").json()  # no good read of its nodes' types
    by_name = {w["name"]: w for w in knows["automated"]}
    assert sorted(by_name) == ["agent", "api", "both"]  # not pooled: no type of it changes
    asks = {"m5.large": {"cpu": 0.05, "memory": None}, "m5.4xlarge": {"cpu": None, "memory": None}}
    assert (by_name["agent"]["cpu"], by_name["agent"]["types"]) == (None, asks)
    assert (by_name["both"]["cpu"], by_name["both"]["types"]) == (0.115, asks)
    assert by_name["api"]["types"] == {}
    assert [(w["name"], w["types"]) for w in knows["sizings"]] == [
        ("ok", {"m5.large": {"cpu": 0.05, "memory": None}})
    ]
    assert knows["node_types"] == {"n1": "m5.large"}
    # the report of a Sizing applied by type has no one number: the Hub takes it as it is
    assert took.status_code == 204 and sql(
        database_url, ("select outcome, cpu, memory, pods from actuations", ())
    ) == [("applied", None, None, 2)]
    # an Actuator that knows no types: the one Sizing for every pod, where it changes something
    assert sorted(w["name"] for w in plain["automated"]) == ["api", "both", "pooled"]
    assert all(w["types"] == {} for w in plain["automated"])
    assert plain["sizings"] == [] and plain["node_types"] is None
    # each Contract's nodes are its own, sent whatever the Sizings: a hold is caught up by them
    assert none["node_types"] == {"b1": "c5.large"}
    assert [w["name"] for w in none["automated"]] == ["api"]
    assert theirs["node_types"] == {"b1": "c5.large"}
    assert "n1" not in json.dumps(theirs) and "b1" not in json.dumps(knows)
    assert unread["node_types"] is None and len(unread["automated"]) == 3  # what it knew stays
