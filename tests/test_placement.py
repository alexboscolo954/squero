"""Placement, the Hub's side (slice 15.2a): the Contract's label of a cluster's Pinned
nodes, which nodes carry it, the Workloads whose pods block a node for a reason set in
their spec (the list its Actuator is sent), a Pinned node's verdict, and which Workload to
restart to free a node once the Actuator places."""

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from squero import blockers, contracts, hubmcp, sizing, usage, webapp
from squero.blockers import DO_NOT_DISRUPT, SAFE
from squero.config import Settings
from tests.test_actuator import A_TOKEN, B_TOKEN, actuated, as_actuator, hub, sql
from tests.test_could_go import node, pod
from tests.test_could_go import read_of as plain_read
from tests.test_nodes import SHOP, _prom, cluster
from tests.test_nodes import node as prom_node
from tests.test_types import acting

NOW = 1_000_000.0
CA, KARPENTER = "cluster-autoscaler", "karpenter"
PINNED = {"label": "squero.io/pinned=yes"}
KEEP = {SAFE: "false"}  # Cluster Autoscaler's Blocker set in a pod's spec


def read_of(*nodes: dict[str, Any]) -> dict[str, Any]:
    """A read made with the Contract's Placement label, as the hourly run makes it."""
    return plain_read(*nodes) | {"placement": PINNED["label"]}


def placed(cid: str = "acme", token: str = A_TOKEN, **cl: Any) -> Any:
    return actuated(cid, token, placement=PINNED, autoscaler=CA, **cl)


def facts(pod_: str, on: str, ns: str = "shop", **more: Any) -> dict[str, Any]:
    return {"namespace": ns, "pod": pod_, "node": on, "kind": "ReplicaSet", "name": "rs", **more}


def report(*pods: dict[str, Any], **more: Any) -> dict[str, Any]:
    return {"received": NOW, "at": NOW, "namespaces": ["shop"], "pods": list(pods), **more}


def pinned(n: dict[str, Any]) -> dict[str, Any]:
    return n | {"pinned": True}


def verdict(rep: dict[str, Any] | None, *nodes: dict[str, Any], **cl: Any) -> Any:
    c = actuated("acme", A_TOKEN, **({"placement": PINNED, "autoscaler": CA} | cl))
    return blockers.verdicts(read_of(*nodes), rep, c.clusters[0], NOW)


# --- the Contract ------------------------------------------------------------------------


def test_a_clusters_placement_is_one_label_and_maybe_its_taint() -> None:
    for label in ("pool=pinned", "squero.io/pinned=yes", "a.b-c/k_1=v.2"):
        assert actuated("acme", A_TOKEN, placement={"label": label}).clusters[0].placement
    for taint in ("dedicated=pinned:NoSchedule", "pinned:NoExecute", "p:PreferNoSchedule"):
        p = actuated("acme", A_TOKEN, placement={"label": "pool=pinned", "taint": taint})
        assert p.clusters[0].placement and p.clusters[0].placement.taint == taint
    for wrong in (
        {"label": "pool"},  # a key alone
        {"label": "pool="},  # no value
        {"label": "-pool=x"},
        {"label": "pool=a b"},
        {"label": "pool=x", "taint": "dedicated=pinned"},  # no effect
        {"label": "pool=x", "taint": "dedicated:NoRun"},
        {"label": "pool=x", "spread": True},
    ):
        with pytest.raises(ValidationError):
            actuated("acme", A_TOKEN, placement=wrong)


def test_placement_needs_the_clusters_actuator() -> None:
    c = placed()
    data = c.model_dump()
    data["clusters"][1]["placement"] = PINNED  # `edge` runs no Actuator
    with pytest.raises(ValidationError, match="cluster 'edge': placement needs an actuator"):
        contracts.build(data, secrets=c._secrets)


# --- the read ---------------------------------------------------------------------------


def test_a_node_is_pinned_when_it_carries_the_label_as_kube_state_metrics_shows_it() -> None:
    fake = cluster()  # n1 and n2
    prom_node(fake, "n3")
    fake.add("kube_node_labels", 1, node="n2", label_squero_io_pinned="yes")
    fake.add("kube_node_labels", 1, node="n3", label_squero_io_pinned="no")
    got = usage.rooms(_prom(fake, SHOP), SHOP, NOW, PINNED["label"])
    assert {n["node"]: n["pinned"] for n in got["nodes"]} == {"n1": False, "n2": True, "n3": False}
    assert got["placement"] == "squero.io/pinned=yes"  # the label it was read with
    off = usage.rooms(_prom(fake, SHOP), SHOP, NOW)
    assert not any(n["pinned"] for n in off["nodes"]) and off["placement"] is None


def test_a_read_made_with_another_label_pins_nothing_until_the_next_run() -> None:
    rep = report(facts("api-1", "a", annotations=KEEP), placing=True)
    a = pinned(node("a", pod("api-1")))
    for label in (None, "pool=old"):  # before 15.2a, or the Contract's label since changed
        stale = plain_read(a) | {"at": NOW, "placement": label}
        assert blockers.pinned(stale, placed().clusters[0]) == set()
        assert blockers.verdicts(stale, rep, placed().clusters[0], NOW)["a"]["state"] == "blocked"
        (shown,) = sizing.nodes_report({"test": stale}, placed(), {"test": rep}, NOW)
        assert shown["placement"]["pinned"] == 0 and shown["nodes"][0]["pinned"] is False
        assert shown["placement"]["note"] == (
            "its nodes were not read with squero.io/pinned=yes yet: the next hourly run does"
        )
    fresh = read_of(a)
    assert blockers.pinned(fresh, placed().clusters[0]) == {"a"}
    assert blockers.pinned(fresh, actuated("acme", A_TOKEN).clusters[0]) == set()


def test_the_hourly_run_reads_the_clusters_pinned_nodes(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = cluster()
    fake.add("kube_node_labels", 1, node="n2", label_squero_io_pinned="yes")
    monkeypatch.setattr(usage, "Prom", lambda t: _prom(fake, t))
    c = acting(placement=PINNED)
    got = usage.collect(c, [86400], NOW).rooms["test"]["nodes"]
    assert [n["node"] for n in got if n["pinned"]] == ["n2"]
    plain = usage.collect(acting(), [86400], NOW).rooms["test"]["nodes"]
    assert not any(n["pinned"] for n in plain)


# --- the verdict ------------------------------------------------------------------------


def test_a_pinned_nodes_blockers_are_expected_with_no_change_to_remove_them() -> None:
    rep = report(facts("db-0", "a", annotations=KEEP), facts("api-1", "b", annotations=KEEP))
    a, b = pinned(node("a", pod("db-0"))), node("b", pod("api-1"))
    got = verdict(rep, a, b)
    assert got["a"]["state"] == "pinned" and got["a"]["why"] is None
    assert [x["pod"] for x in got["a"]["blockers"]] == ["db-0"]
    assert got["a"]["blockers"][0]["changes"] == []
    assert got["b"]["state"] == "blocked" and got["b"]["blockers"][0]["changes"]
    # no Placement: a node that carries a label is a node like any other
    plain = verdict(rep, a, b, placement=None)
    assert plain["a"]["state"] == "blocked" and "placement" not in plain["a"]["blockers"][0]
    # a Pinned node is pinned whatever is known of it
    assert verdict(None, a, b)["a"]["state"] == "pinned"
    assert verdict(report(), pinned(node("e")))["e"] == {
        "state": "pinned",
        "why": None,
        "blockers": [],
    }


@pytest.mark.parametrize(
    ("autoscaler", "more", "by"),
    [
        (CA, {"annotations": {SAFE: "false"}}, "shop/Deployment/api"),
        (KARPENTER, {"annotations": {DO_NOT_DISRUPT: "true"}}, "shop/Deployment/api"),
        (CA, {"empty_dir": ["scratch"]}, "shop/Deployment/api"),  # local storage
        (None, {"empty_dir": ["scratch"]}, "shop/Deployment/api"),  # kubectl drain's emptyDir
        (CA, {"budget": "tight", "allowed": 0}, None),  # comes and goes
        (KARPENTER, {"annotations": {DO_NOT_DISRUPT: "30m"}, "started": NOW}, None),
        (CA, {"kind": "Workflow", "name": "wf"}, None),  # a custom controller's
        (CA, {"kind": "", "name": ""}, None),  # no controller
    ],
)
def test_a_workload_is_placed_only_for_a_blocker_set_in_its_pods_spec(
    autoscaler: str | None, more: dict[str, Any], by: str | None
) -> None:
    rep = report(facts("api-1", "b", **more))
    (found,) = verdict(rep, node("b", pod("api-1")), autoscaler=autoscaler)["b"]["blockers"]
    assert found["placement"] == by


def test_only_a_workload_the_webhook_names_is_placed() -> None:
    rep = report(facts("api-1", "b", annotations=KEEP))
    for kind, by in [
        ("StatefulSet", "shop/StatefulSet/api"),
        ("Rollout", "shop/Rollout/api"),
        ("CronJob", "shop/CronJob/api"),
        ("Job", None),  # a lone Job's pod: no Workload the webhook keys
        ("ReplicaSet", None),
    ]:
        b = node("b", pod("api-1", workload=(kind, "api")))
        assert verdict(rep, b)["b"]["blockers"][0]["placement"] == by, kind
    unresolved = node("b", pod("api-1", workload=None))
    assert verdict(rep, unresolved)["b"]["blockers"][0]["placement"] is None


def test_a_placed_pod_running_off_the_pinned_nodes_says_why() -> None:
    rep = report(
        facts("api-1", "b", annotations=KEEP, placed=True),
        facts("api-2", "a", annotations=KEEP, placed=True),
        facts("api-3", "c", annotations=KEEP),
    )
    got = verdict(
        rep, pinned(node("a", pod("api-2"))), node("b", pod("api-1")), node("c", pod("api-3"))
    )
    note = "placed, on b: the pinned nodes had no room when it started"
    assert got["b"]["blockers"][0]["note"] == note
    assert got["a"]["blockers"][0]["note"] is None  # where it should be
    assert got["c"]["blockers"][0]["note"] is None  # started before it was placed


def test_the_list_is_every_placed_workload_pinned_nodes_included() -> None:
    rep = report(
        facts("db-0", "a", annotations=KEEP),
        facts("api-1", "b", annotations=KEEP),
        facts("api-2", "c", annotations=KEEP),
        facts("web-1", "c", budget="tight", allowed=0),
    )
    got = verdict(
        rep,
        pinned(node("a", pod("db-0", workload=("StatefulSet", "db")))),
        node("b", pod("api-1")),
        node("c", pod("api-2"), pod("web-1", workload=("Deployment", "web"))),
    )
    assert blockers.placing(got) == ["shop/Deployment/api", "shop/StatefulSet/db"]
    assert blockers.placing(verdict(rep, node("b", pod("api-1")), placement=None)) == []


def test_a_pinned_node_neither_goes_nor_receives() -> None:
    a, b, c = pinned(node("a")), node("b", pod("b1", 0.5)), node("c", pod("c1", 3.9))
    states = {"a": {"state": "pinned"}, "b": {"state": "clear"}, "c": {"state": "blocked"}}
    assert blockers.could_go(read_of(a, b, c), states) == []  # b's pod fits only on a
    states["a"] = {"state": "clear"}
    assert blockers.could_go(read_of(a, b, c), states) == ["a"]  # then b's pod has no room


# --- what frees a node ------------------------------------------------------------------


def restart(rep: dict[str, Any], *nodes: dict[str, Any], on: str = "b") -> Any:
    return verdict(rep, *nodes)[on].get("restart")


def test_a_node_says_which_workloads_to_restart_once_its_actuator_places() -> None:
    rep = report(facts("api-1", "b", annotations=KEEP), facts("db-0", "b", empty_dir=["d"]))
    a = pinned(node("a"))
    b = node("b", pod("api-1"), pod("db-0", workload=("StatefulSet", "db")))
    assert restart(rep, a, b) is None  # its Actuator doesn't place yet
    got = restart(rep | {"placing": True}, a, b)
    assert got == {"workloads": ["shop/Deployment/api", "shop/StatefulSet/db"], "why_not": None}
    assert verdict(rep | {"placing": True}, a, b)["a"].get("restart") is None  # pinned
    stale = rep | {"placing": True, "received": NOW - blockers.STALE - 1}
    assert restart(stale, a, b) is None  # what Prometheus shows can't tell
    custom = node("b", pod("api-1") | {"kind": "Workflow"})  # blocked by what Prometheus shows
    assert verdict(stale, a, custom)["b"]["state"] == "blocked"
    assert restart(stale, a, custom) is None


def test_a_blocker_that_isnt_placed_still_holds_the_node() -> None:
    rep = report(
        facts("api-1", "b", annotations=KEEP),
        facts("web-1", "b", budget="tight", allowed=0),
        placing=True,
    )
    b = node("b", pod("api-1"), pod("web-1", workload=("Deployment", "web")))
    got = restart(rep, pinned(node("a")), b)
    assert got == {"workloads": [], "why_not": "pod shop/web-1 would still hold it"}
    many = [facts(f"web-{i}", "b", budget="tight", allowed=0) for i in range(5)]
    crowd = node("b", *[pod(f"web-{i}", workload=("Deployment", "web")) for i in range(5)])
    said = restart(report(*many, placing=True), pinned(node("a")), crowd)["why_not"]
    assert said == "pod shop/web-0, shop/web-1, shop/web-2 and 2 more would still hold it"


def test_without_a_pinned_node_nothing_frees_it() -> None:
    rep = report(facts("api-1", "b", annotations=KEEP), placing=True)
    got = restart(rep, node("b", pod("api-1")))
    assert (
        got["why_not"]
        == "no node carries the label squero.io/pinned=yes, as kube-state-metrics shows it"
    )


def test_a_restart_moves_every_replica_so_the_pinned_room_must_take_them_all() -> None:
    rep = report(facts("api-1", "b", annotations=KEEP), placing=True)
    b, c = node("b", pod("api-1", 1.0)), node("c", pod("api-2", 1.0))
    room = pinned(node("a", pod("x", 2.0)))  # 2 cores free
    assert restart(rep, room, b, c)["workloads"] == ["shop/Deployment/api"]
    tight = pinned(node("a", pod("x", 2.5, gib=14.0)))  # 1.5 cores and 2 GiB free
    assert restart(rep, tight, b, c) == {
        "workloads": [],
        "why_not": "the pinned nodes lack room: about 0.5 cores short",
    }
    tighter = pinned(node("a", pod("x", 2.5, gib=15.5)))  # half a GiB free
    assert restart(rep, tighter, b, c)["why_not"] == (
        "the pinned nodes lack room: about 0.5 cores and 1.5 GiB short"
    )
    # replicas already on a Pinned node take no more room
    there = pinned(node("a", pod("x", 2.0), pod("api-2", 1.0)))
    assert restart(rep, there, b)["workloads"] == ["shop/Deployment/api"]


def test_each_pod_must_fit_whole_on_one_pinned_node_that_takes_pods() -> None:
    rep = report(facts("api-1", "b", annotations=KEEP), placing=True)
    b = node("b", pod("api-1", 2.0))
    halves = [pinned(node("a1", pod("x", 3.0))), pinned(node("a2", pod("y", 3.0)))]
    assert restart(rep, *halves, b)["why_not"] == (
        "no pinned node has room for pod shop/api-1 whole"
    )
    shut = pinned(node("a", cordoned=True))
    assert (
        restart(rep, shut, b)["why_not"]
        == "the pinned nodes lack room: about 2.0 cores and 1.0 GiB short"
    )
    full = pinned(node("a", pod("x", 0.1), slots=1))
    assert restart(rep, full, b)["why_not"] == "no pinned node has room for pod shop/api-1 whole"
    tainted = pinned(node("a", taints=("dedicated=pinned:NoSchedule",)))
    assert restart(rep, tainted, b)["workloads"] == ["shop/Deployment/api"]  # it tolerates it


def test_the_pinned_room_is_filled_largest_pod_first() -> None:
    rep = report(facts("api-1", "b", annotations=KEEP), placing=True)
    b = node("b", pod("api-1", 1.0), pod("api-2", 3.0))
    a1, a2 = pinned(node("a1", pod("x", 1.0))), pinned(node("a2", pod("y", 3.0)))
    # 3 cores on a1 first, then 1 on a2: smallest first would take a1's 3 for 1 core
    assert restart(rep, a1, a2, b)["workloads"] == ["shop/Deployment/api"]


# --- shown and sent ---------------------------------------------------------------------


def test_the_nodes_report_says_the_placement_and_counts_pinned_nodes() -> None:
    rep = report(facts("api-1", "b", annotations=KEEP), placing=True)
    read = read_of(pinned(node("a")), node("b", pod("api-1"))) | {"at": NOW}
    c = placed()
    (shown,) = sizing.nodes_report({"test": read}, c, {"test": rep}, NOW)
    assert shown["judged"] == {"clear": 0, "blocked": 1, "unknown": 0, "pinned": 1}
    assert shown["placement"] == {
        **{"label": "squero.io/pinned=yes", "taint": None, "pinned": 1, "note": None},
        **{"workloads": ["shop/Deployment/api"], "placing": True},
    }
    got = webapp.Nodes.model_validate({"clusters": [shown]})
    mcp = hubmcp._nodes(got.clusters[0], 20)
    assert mcp["placement"]["workloads"] == ["shop/Deployment/api"]
    blocked = next(n for n in mcp["nodes"] if n["node"] == "b")
    assert blocked["restart"] == {
        "workloads": ["shop/Deployment/api"],
        "more_workloads": 0,
        "why_not": None,
    }
    assert blocked["blockers"][0]["placed_by"] == "shop/Deployment/api"
    assert [n["could_be_removed"] for n in mcp["nodes"]] == ["blocked", "pinned"]
    none = read_of(node("b", pod("api-1"))) | {"at": NOW}
    (bare,) = sizing.nodes_report({"test": none}, c, {"test": rep | {"placing": False}}, NOW)
    assert bare["placement"]["note"].startswith("no node carries squero.io/pinned=yes")
    assert bare["placement"]["placing"] is False  # its Actuator doesn't say it places
    off = actuated("acme", A_TOKEN)
    (plain,) = sizing.nodes_report({"test": none}, off, {"test": rep}, NOW)
    assert plain["placement"] is None and "pinned" not in plain["judged"]


def test_the_hub_sends_each_actuator_its_own_clusters_placement(
    settings: Settings, database_url: str
) -> None:
    acme, bank = placed("acme", A_TOKEN), placed("bank", B_TOKEN)
    app = hub(settings, database_url, acme, bank)
    ours = read_of(
        pinned(node("a")), node("b", pod("api-1"), pod("web-1", workload=("Deployment", "web")))
    )
    theirs = read_of(node("x", pod("ledger-1", workload=("Deployment", "ledger"))))
    with TestClient(app) as c:

        def desired(token: str) -> Any:
            return c.get("/actuator/desired", headers=as_actuator(token)).json()["placement"]

        assert desired(A_TOKEN) is None  # no read of its nodes yet
        sql(
            database_url,
            ("insert into cluster_rooms values ('acme', 'test', 1, %s)", (json.dumps(ours),)),
            ("insert into cluster_rooms values ('bank', 'test', 1, %s)", (json.dumps(theirs),)),
        )
        assert desired(A_TOKEN) is None  # no report yet: what it knew stays

        def post(token: str, *pods: dict[str, Any]) -> None:
            body = {"at": NOW, "namespaces": ["shop"], "pods": list(pods)}
            sent = c.post("/actuator/blockers", headers=as_actuator(token), json=body)
            assert sent.status_code == 204

        post(
            A_TOKEN,
            facts("api-1", "b", annotations=KEEP),
            facts("web-1", "b", budget="t", allowed=0),
        )
        post(B_TOKEN, facts("ledger-1", "x", annotations=KEEP))
        mine, other = desired(A_TOKEN), desired(B_TOKEN)
        sql(database_url, ("insert into kill_switches values ('acme', 't', 1)", ()))
        killed = c.get("/actuator/desired", headers=as_actuator(A_TOKEN)).json()
    assert mine == {
        "label": "squero.io/pinned=yes",
        "taint": None,
        "workloads": ["shop/Deployment/api"],
    }
    assert other["workloads"] == ["shop/Deployment/ledger"]
    assert "ledger" not in json.dumps(mine) and "api" not in json.dumps(other)
    assert killed["kill_switch"] and killed["placement"] is None


def test_a_cluster_without_placement_is_sent_none(settings: Settings, database_url: str) -> None:
    app = hub(settings, database_url, actuated("acme", A_TOKEN))
    ours = read_of(node("b", pod("api-1")))
    with TestClient(app) as c:
        sql(
            database_url,
            ("insert into cluster_rooms values ('acme', 'test', 1, %s)", (json.dumps(ours),)),
        )
        body = {"at": NOW, "namespaces": ["shop"], "pods": [facts("api-1", "b", annotations=KEEP)]}
        c.post("/actuator/blockers", headers=as_actuator(A_TOKEN), json=body)
        assert c.get("/actuator/desired", headers=as_actuator(A_TOKEN)).json()["placement"] is None


def test_an_actuator_sends_the_new_fields_only_when_true() -> None:
    """A Hub from before 15.2a refuses a report with a field it doesn't know."""
    quiet = blockers.Report.model_validate({"at": NOW, "namespaces": [], "pods": [facts("a", "b")]})
    body = blockers.sent(quiet)
    assert "placing" not in body and "placed" not in body["pods"][0]
    assert body["pods"][0]["node"] == "b"
    told = quiet.model_copy(update={"placing": True})
    told.pods[0].placed = True
    body = blockers.sent(told)
    assert body["placing"] is True and body["pods"][0]["placed"] is True


def test_the_hub_mcp_caps_the_placed_workloads_and_a_restarts() -> None:
    many = [f"shop/Deployment/w{i:02}" for i in range(25)]
    shown = webapp.PlacementShown(label="a=b", pinned=1, workloads=many, placing=True)
    got = hubmcp._placement(shown)
    assert (len(got["workloads"]), got["more_workloads"]) == (20, 5)
    r = hubmcp._restart(webapp.Restart(workloads=many[:7], why_not=None))
    assert (r["workloads"], r["more_workloads"], r["why_not"]) == (many[:5], 2, None)
    long = hubmcp._restart(webapp.Restart(workloads=[], why_not="x" * 400))
    assert long["why_not"] == "x" * 300


def test_a_report_may_say_its_actuator_places_and_which_pods_carry_the_preference() -> None:
    sent = report(facts("api-1", "b", placed=True), placing=True)
    r = blockers.Report.model_validate({k: v for k, v in sent.items() if k != "received"})
    assert r.placing and r.pods[0].placed
    plain = blockers.Report.model_validate({"at": NOW, "namespaces": [], "pods": [facts("a", "b")]})
    assert not plain.placing and not plain.pods[0].placed
