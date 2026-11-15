"""Spot placing, the Hub's side (slice 16.2a): a cluster's spot taint; what each Actuator
is sent (its spot label and taint, the Workloads under a Spot ratio blocking no node, the
nodes carrying the label); its report saying it places; and the risk line below a
Workload's On-demand minimum once it does."""

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from squero import blockers, contracts, hubmcp, sizing, webapp
from squero.config import Settings
from tests.test_actuator import A_TOKEN, B_TOKEN, actuated, as_actuator, hub, sql
from tests.test_could_go import FRESH, NOW, node, pod, read_of
from tests.test_placement import CA, KEEP, facts

SPOT = {"label": "kubernetes.azure.com/scalesetpriority=spot", "discount": 0.35}
TAINT = "kubernetes.azure.com/scalesetpriority=spot:NoSchedule"
RATIO = {"ratio": 0.7, "on_demand": 2}
RULE = {"namespace": "shop", "policy": "production", "spot": RATIO}


def spotting(cid: str = "acme", token: str = A_TOKEN, **spot: Any) -> contracts.Contract:
    data = actuated(cid, token, spot=SPOT | spot, autoscaler=CA).model_dump()
    data["sizing"]["rules"] = [RULE]
    return contracts.build(data, secrets=actuated(cid, token)._secrets)


def stamped(*nodes: dict[str, Any]) -> dict[str, Any]:
    return read_of(*nodes) | {"pvc": True, "spot": SPOT["label"]}


def on_spot(n: dict[str, Any]) -> dict[str, Any]:
    return n | {"spot": True}


# --- the Contract -----------------------------------------------------------------------


def test_a_clusters_spot_nodes_may_name_their_taint() -> None:
    assert spotting(taint=TAINT).clusters[0].spot.taint == TAINT  # type: ignore[union-attr]
    assert spotting().clusters[0].spot.taint is None  # type: ignore[union-attr]
    for wrong in ("no-effect", "a=b:Sometimes", "a b:NoSchedule"):
        with pytest.raises(ValidationError):
            spotting(taint=wrong)


# --- what each Actuator is sent ---------------------------------------------------------


def test_the_hub_sends_each_actuator_its_own_clusters_spot_workloads_and_nodes(
    settings: Settings, database_url: str
) -> None:
    acme, bank = spotting(taint=TAINT), spotting("bank", B_TOKEN)
    app = hub(settings, database_url, acme, bank)
    api_pods = [pod(f"api-{i}") for i in range(3)]
    web = pod("web-1", workload=("Deployment", "web"))
    db = pod("db-0", workload=("StatefulSet", "db"))
    ours = stamped(node("a", *api_pods[:2], web, db), on_spot(node("s1", api_pods[2])))
    theirs = stamped(node("x", pod("ledger-1", workload=("Deployment", "ledger"))))
    with TestClient(app) as c:

        def desired(token: str) -> Any:
            return c.get("/actuator/desired", headers=as_actuator(token)).json()["spot"]

        unknown = {"label": SPOT["label"], "taint": TAINT, "workloads": None, "nodes": None}
        assert desired(A_TOKEN) == unknown  # no read of its nodes yet: the last list stays
        sql(
            database_url,
            ("insert into cluster_rooms values ('acme', 'test', 1, %s)", (json.dumps(ours),)),
            ("insert into cluster_rooms values ('bank', 'test', 1, %s)", (json.dumps(theirs),)),
        )
        assert desired(A_TOKEN) == unknown  # no report yet: which block a node isn't known

        def post(token: str, *pods: dict[str, Any]) -> None:
            body = {"at": NOW, "namespaces": ["shop"], "pods": list(pods)}
            assert (
                c.post("/actuator/blockers", headers=as_actuator(token), json=body).status_code
                == 204
            )

        post(A_TOKEN, facts("web-1", "a", annotations=KEEP), facts("api-0", "a"))
        post(B_TOKEN, facts("ledger-1", "x"))
        mine, other = desired(A_TOKEN), desired(B_TOKEN)
        sql(database_url, ("insert into kill_switches values ('acme', 't', 1)", ()))
        killed = c.get("/actuator/desired", headers=as_actuator(A_TOKEN)).json()
    assert mine == {
        "label": SPOT["label"],
        "taint": TAINT,
        # web blocks a node: kept off spot (no share); db is a StatefulSet: not steered
        "workloads": [
            {
                "namespace": "shop",
                "kind": "Deployment",
                "name": "api",
                "ratio": 0.7,
                "on_demand": 2,
            },
            {
                "namespace": "shop",
                "kind": "Deployment",
                "name": "web",
                "ratio": 0.0,
                "on_demand": 2,
            },
        ],
        "nodes": ["s1"],
    }
    assert [w["name"] for w in other["workloads"]] == ["ledger"] and other["nodes"] == []
    assert "ledger" not in json.dumps(mine) and "api" not in json.dumps(other)
    assert killed["kill_switch"] and killed["spot"] is None


def test_a_cluster_without_spot_nodes_is_sent_none(settings: Settings, database_url: str) -> None:
    app = hub(settings, database_url, actuated("acme", A_TOKEN))
    with TestClient(app) as c:
        assert c.get("/actuator/desired", headers=as_actuator(A_TOKEN)).json()["spot"] is None


def test_the_spot_nodes_are_unknown_in_a_read_made_without_the_label() -> None:
    read = stamped(node("a"), on_spot(node("s1")), on_spot(node("s2")))
    assert sizing.spot_nodes(read, SPOT["label"]) == ["s1", "s2"]
    assert sizing.spot_nodes(read | {"spot": None}, SPOT["label"]) is None
    assert sizing.spot_nodes(read, "other=x") is None
    assert sizing.spot_nodes(stamped(node("a")), SPOT["label"]) == []  # read: none on spot


# --- the report -------------------------------------------------------------------------


def test_an_actuator_says_it_steers_only_when_it_does() -> None:
    """A Hub from before 16.2a refuses a report with a field it doesn't know."""
    quiet = blockers.Report.model_validate({"at": NOW, "namespaces": [], "pods": []})
    assert "spotting" not in blockers.sent(quiet)
    told = quiet.model_copy(update={"spotting": True})
    assert blockers.sent(told)["spotting"] is True
    assert blockers.Report.model_validate(blockers.sent(told)).spotting


# --- the risk line ----------------------------------------------------------------------


def shown(report: dict[str, Any], *nodes: dict[str, Any]) -> dict[str, Any]:
    (got,) = sizing.nodes_report({"test": stamped(*nodes)}, spotting(), {"test": report}, NOW)
    return got["spot_workloads"]


def test_once_its_actuator_steers_a_workload_below_its_minimum_is_a_risk() -> None:
    api = [pod(f"api-{i}") for i in range(4)]
    below = (node("a", api[0]), on_spot(node("s", *api[1:])))  # 1 on-demand of a minimum of 2
    got = shown(FRESH | {"spotting": True}, *below)
    assert got["note"] is None
    (w,) = got["workloads"]
    assert w["risk"] == "1 on-demand, below its minimum of 2: restart it to repair"
    assert shown(FRESH, *below)["workloads"][0]["risk"] is None  # not steered yet: no risk
    assert shown(FRESH, *below)["note"] == "its Actuator doesn't place them yet"
    held = (node("a", *api[:2]), on_spot(node("s", *api[2:])))
    assert shown(FRESH | {"spotting": True}, *held)["workloads"][0]["risk"] is None


def test_a_workload_with_fewer_replicas_than_its_minimum_needs_them_all_on_demand() -> None:
    one = pod("api-0")
    got = shown(FRESH | {"spotting": True}, on_spot(node("s", one)))["workloads"][0]
    assert got["risk"] == "0 of its 1 on-demand, where all are meant to be: restart it to repair"
    fine = shown(FRESH | {"spotting": True}, node("a", one))["workloads"][0]
    assert fine["risk"] is None


def test_no_risk_where_it_isnt_known_where_the_pods_run() -> None:
    api = [pod(f"api-{i}") for i in range(2)]
    read = read_of(on_spot(node("s", *api))) | {"pvc": True, "spot": None}
    (got,) = sizing.nodes_report(
        {"test": read}, spotting(), {"test": FRESH | {"spotting": True}}, NOW
    )
    assert got["spot_workloads"]["workloads"][0]["risk"] is None


def test_the_risk_reaches_the_console_and_the_hub_mcp() -> None:
    api = [pod(f"api-{i}") for i in range(2)]
    (got,) = sizing.nodes_report(
        {"test": stamped(on_spot(node("s", *api)))},
        spotting(),
        {"test": FRESH | {"spotting": True}},
        NOW,
    )
    cl = webapp.Nodes.model_validate({"clusters": [got]}).clusters[0]
    assert cl.spot_workloads and cl.spot_workloads.workloads[0].risk
    mcp = hubmcp._nodes(cl, 20)
    assert mcp["spot_workloads"]["workloads"][0]["risk"].startswith("0 of its 2 on-demand")


def test_only_workloads_under_a_spot_ratio_are_steered() -> None:
    data = spotting().model_dump()
    data["sizing"]["rules"] = [RULE | {"name": "api"}]
    c = contracts.build(data, secrets=spotting()._secrets)
    pods = [pod(f"api-{i}") for i in range(2)] + [pod("web-1", workload=("Deployment", "web"))]
    found = sizing.spotted(stamped(node("a", *pods)), c, "test", set())
    assert [(w["name"], w["ratio"]) for w in found] == [("api", 0.7)]


def test_a_namespace_the_contract_has_left_is_steered_no_more() -> None:
    """The read may be an hour older than the Contract."""
    left = [pod(f"old-{i}", ns="gone", workload=("Deployment", "old")) for i in range(2)]
    data = spotting().model_dump()
    data["sizing"]["rules"] = [RULE | {"namespace": "*"}]
    c = contracts.build(data, secrets=spotting()._secrets)
    found = sizing.spotted(stamped(node("a", pod("api-0"), *left)), c, "test", set())
    assert [w["namespace"] for w in found] == ["shop"]


def test_a_workload_that_blocks_a_node_is_kept_off_spot_and_its_risk_says_so() -> None:
    web = [pod(f"w{i}", workload=("Deployment", "web")) for i in range(3)]
    blocking = {("shop", "Deployment/web")}
    found = sizing.spotted(stamped(node("a", *web)), spotting(), "test", blocking)
    assert found == [{"namespace": "shop", "kind": "Deployment", "name": "web", "on_demand": 2,
                      "ratio": 0.0}]  # fmt: skip
    report = FRESH | {"spotting": True, "pods": [facts("w0", "s", annotations=KEEP)]}
    got = shown(report, node("a", *web[1:]), on_spot(node("s", web[0])))["workloads"][0]
    assert got["why"] == "it blocks a node: none on spot"
    assert got["risk"] == "2 of its 3 on-demand, where all are meant to be: restart it to repair"


def test_a_stale_report_saying_it_steers_raises_no_risk() -> None:
    api = [pod(f"api-{i}") for i in range(2)]
    old = FRESH | {"spotting": True, "received": NOW - 3 * 3600}
    got = shown(old, on_spot(node("s", *api)))
    assert (
        got["workloads"][0]["risk"] is None and got["note"] == "its Actuator doesn't place them yet"
    )


def test_a_minimum_above_the_replicas_means_all_on_demand() -> None:
    data = spotting().model_dump()
    data["sizing"]["rules"] = [RULE | {"spot": {"ratio": 0.5, "on_demand": 3}}]
    c = contracts.build(data, secrets=spotting()._secrets)
    api = [pod(f"api-{i}") for i in range(2)]
    read = stamped(node("a", api[0]), on_spot(node("s", api[1])))
    (got,) = sizing.nodes_report({"test": read}, c, {"test": FRESH | {"spotting": True}}, NOW)
    (w,) = got["spot_workloads"]["workloads"]
    assert w["why"] is None and w["intended"] == {"on_demand": 2, "spot": 0}
    assert w["risk"] == "1 of its 2 on-demand, where all are meant to be: restart it to repair"


def test_a_workload_at_its_minimum_is_no_risk_whatever_its_share() -> None:
    """Pods from before it was steered: 2 of 10 on-demand under 0.7 with 2 is no risk."""
    api = [pod(f"api-{i}") for i in range(10)]
    at = shown(FRESH | {"spotting": True}, node("a", *api[:2]), on_spot(node("s", *api[2:])))
    assert at["workloads"][0]["risk"] is None
    below = shown(FRESH | {"spotting": True}, node("a", api[0]), on_spot(node("s", *api[1:])))
    assert (
        below["workloads"][0]["risk"] == "1 on-demand, below its minimum of 2: restart it to repair"
    )
