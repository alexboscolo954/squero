"""Spot ratios (slice 16.1c): `spot: {ratio, on_demand}` on a Sizing rule, refused where it
can't apply; the replicas meant for each kind of node; the Workloads that could run on
spot with a what-if; and those under a Spot ratio, intended against actual. Nothing new
reaches the Actuator."""

from typing import Any

import pytest
from pydantic import ValidationError

from squero import contracts, sizing, usage
from squero.contracts import SpotRule
from tests.fake_prometheus import FakePrometheus
from tests.test_actuator import A_TOKEN, actuated
from tests.test_could_go import FRESH, NOW, node, pod, read_of
from tests.test_nodes import SHOP, cluster
from tests.test_sizing_store import store  # noqa: F401
from tests.test_usage import _prom

SPOT = {"label": "karpenter.sh/capacity-type=spot", "discount": 0.35}
RATIO = {"ratio": 0.7, "on_demand": 2}
GI = 2**30


def spotted(*rules: dict[str, Any], prices: bool = True, **cl: Any) -> contracts.Contract:
    """Cluster `test` runs an Actuator and names its spot label (unless `cl` says
    otherwise); `edge` does neither."""
    data = actuated("acme", A_TOKEN, **({"spot": SPOT} | cl)).model_dump()
    data["sizing"]["rules"] = list(rules)
    data["integrations"].append({"name": "prometheus", "url": "http://prometheus"})
    if prices:
        data["prices"] = {"cpu": 0.04, "memory": 0.005}
    return contracts.build(data, secrets=actuated("acme", A_TOKEN)._secrets)


# --- the rule ---------------------------------------------------------------------------


def test_a_spot_ratio_is_a_ratio_and_an_on_demand_minimum_both_required() -> None:
    rule = {"namespace": "shop", "policy": "production", "spot": RATIO}
    assert spotted(rule).sizing.rules[0].spot == SpotRule(ratio=0.7, on_demand=2)
    assert spotted(rule | {"spot": {"ratio": 1, "on_demand": 1}})  # all but one on spot
    for wrong in (
        {"ratio": 0, "on_demand": 2},
        {"ratio": 1.1, "on_demand": 2},
        {"ratio": 0.7, "on_demand": 0},
        {"ratio": 0.7},
        {"on_demand": 2},
        {"ratio": 0.7, "on_demand": 2, "max": 3},
    ):
        with pytest.raises(ValidationError):
            spotted(rule | {"spot": wrong})


@pytest.mark.parametrize(
    ("rule", "said"),
    [
        ({"container": "app"}, "a Workload's, not a container's"),
        ({"kind": "StatefulSet"}, "a StatefulSet never runs on spot"),
        ({"kind": "DaemonSet"}, "a DaemonSet never runs on spot"),
        ({"kind": "Job"}, "a Job never runs on spot"),
        ({"kind": "CronJob"}, "CronJobs on spot come with slice 16.3"),
    ],
)
def test_a_spot_ratio_where_it_cant_apply_is_refused(rule: dict[str, Any], said: str) -> None:
    with pytest.raises(ValidationError, match=said):
        spotted({"namespace": "shop", "policy": "production", "spot": RATIO} | rule)


def test_a_spot_ratio_needs_a_cluster_with_a_spot_label_and_an_actuator() -> None:
    rule = {"namespace": "shop", "policy": "off", "spot": RATIO}  # any policy, off too
    assert spotted(rule).sizing.rules[0].spot
    with pytest.raises(ValidationError, match="spot: a cluster needs both a spot label"):
        spotted(rule, spot=None)
    with pytest.raises(ValidationError, match="cluster 'edge' needs both a spot label"):
        spotted(rule | {"cluster": "edge"})
    data = actuated("acme", A_TOKEN, spot=SPOT).model_dump()
    data["clusters"][0]["actuator_token_secret"] = None
    data["sizing"]["rules"] = [rule]
    with pytest.raises(ValidationError, match="spot: a cluster needs both a spot label"):
        contracts.build(data)


def test_a_workloads_ratio_is_the_first_rule_without_a_container_that_matches_it() -> None:
    c = spotted(
        {"namespace": "shop", "container": "sidecar", "policy": "batch"},  # skipped
        {"namespace": "shop", "name": "api", "policy": "production", "spot": RATIO},
        {"namespace": "shop", "name": "web", "policy": "production"},  # shadows the next
        {"namespace": "shop", "policy": "production", "spot": {"ratio": 0.5, "on_demand": 1}},
    )
    assert c.spot_for("test", "shop", "Deployment", "api") == SpotRule(**RATIO)
    assert c.spot_for("test", "shop", "Deployment", "web") is None
    assert c.spot_for("test", "shop", "Rollout", "cart") == SpotRule(ratio=0.5, on_demand=1)
    assert c.spot_for("test", "shop", "StatefulSet", "db")  # told it can't where shown
    assert c.spot_for("edge", "shop", "Deployment", "api") is None  # no spot, no Actuator
    assert c.spot_for("nowhere", "shop", "Deployment", "api") is None


# --- the numbers ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("replicas", "ratio", "minimum", "split"),
    [
        (10, 0.7, 2, (3, 7)),  # 10 * 0.3 is 3.0000000000000004 in floats
        (4, 0.7, 2, (2, 2)),
        (3, 0.5, 1, (2, 1)),  # rounded up
        (5, 1.0, 1, (1, 4)),
        (1, 0.7, 2, (1, 0)),  # never more on-demand than there are
        (0, 0.7, 2, (0, 0)),
    ],
)
def test_the_replicas_meant_for_each_kind_of_node(
    replicas: int, ratio: float, minimum: int, split: tuple[int, int]
) -> None:
    assert sizing.spot_split(replicas, SpotRule(ratio=ratio, on_demand=minimum)) == split


# --- the candidates ---------------------------------------------------------------------


def row(name: str, pods: float, cpu: float = 0.5, gib: float = 1.0, **more: Any) -> dict[str, Any]:
    out = {"cluster": "test", "namespace": "shop", "kind": "Deployment", "name": name}
    return (
        out
        | {"container": "app", "pods": pods, "requests": {"cpu": cpu, "memory": gib * GI}}
        | more
    )


def stamped(*nodes: dict[str, Any], label: str | None = SPOT["label"]) -> dict[str, Any]:
    return read_of(*nodes) | {"pvc": True, "spot": label}


def shown(
    c: contracts.Contract, read: dict[str, Any], rows: list[dict[str, Any]], report: Any = FRESH
) -> dict[str, Any]:
    (got, *_) = sizing.nodes_report({"test": read}, c, {"test": report}, NOW, rows)
    return got


def names(found: dict[str, Any]) -> list[str]:
    return [w["name"] for w in found["workloads"]]


def test_a_candidate_has_two_replicas_on_average_no_volume_and_blocks_no_node() -> None:
    c = spotted()
    db = pod("db-0", workload=("Deployment", "db")) | {"pvc": True}
    rows = [
        row("api", 4.0),
        row("web", 1.9),  # under 2 on average
        row("db", 3.0),  # claims a volume
        row("seq", 3.0, kind="StatefulSet"),  # a kind that can't
        row("api", 4.0, cluster="edge"),  # another cluster's
    ]
    got = shown(c, stamped(node("a", pod("api-1"), db)), rows)["candidates"]
    assert names(got) == ["api"]
    assert got["workloads"][0] | {"saving": None} == {
        **{"namespace": "shop", "kind": "Deployment", "name": "api", "replicas": 4.0},
        **{"requests": {"cpu": 0.5, "memory": 1.0 * GI}, "on_demand": 2, "spot": 2},
        "saving": None,
    }


def test_a_workload_its_actuator_names_as_blocking_is_no_candidate() -> None:
    web = pod("w1", workload=("Deployment", "web")) | {"kind": ""}  # no controller: blocks
    report = FRESH | {
        "pods": [{"namespace": "shop", "pod": "w1", "node": "a", "kind": "", "name": ""}]
    }
    rows = [row("api", 3.0), row("web", 3.0)]
    read = stamped(node("a", pod("api-1"), web))
    assert names(shown(spotted(), read, rows, report)["candidates"]) == ["api"]
    # without a usable report (none, or too old), Prometheus' guess names nothing
    old = report | {"received": NOW - 3 * 3600}
    assert names(shown(spotted(), read, rows, old)["candidates"]) == ["api", "web"]


def test_a_workload_under_a_spot_ratio_is_no_candidate_where_its_cluster_can_place_it() -> None:
    rule = {"namespace": "shop", "name": "api", "policy": "production", "spot": RATIO}
    rows = [row("api", 3.0), row("web", 3.0)]
    assert names(shown(spotted(rule), stamped(node("a")), rows)["candidates"]) == ["web"]


def test_a_workload_the_what_if_moves_nothing_of_is_no_candidate() -> None:
    """2 replicas on average (2.4 rounds to 2) stay on-demand under 2 at least: nothing
    to move, so not listed (the Operator's choice, 2026-11-15)."""
    rows = [row("two", 2.0), row("almost", 2.4), row("three", 2.5)]
    assert names(shown(spotted(), stamped(node("a")), rows)["candidates"]) == ["three"]


def test_a_candidates_replicas_round_half_up_and_its_containers_add_up() -> None:
    rows = [row("api", 2.5, cpu=0.25), row("api", 2.5, cpu=0.25, gib=0.5, container="side")]
    (got,) = shown(spotted(), stamped(node("a")), rows)["candidates"]["workloads"]
    assert (got["on_demand"], got["spot"]) == (2, 1)  # 2.5 is 3 replicas: 2 and 1
    assert got["requests"] == {"cpu": 0.5, "memory": 1.5 * GI}


def test_the_saving_is_the_spot_replicas_at_a_spot_nodes_rate_against_on_demand() -> None:
    c = spotted()
    rows = [row("api", 10.0, cpu=1.0, gib=4.0)]
    each = sizing.monthly(contracts.ClusterPrices(cpu=0.04, memory=0.005), cpu=1.0, memory=4 * GI)
    got = shown(c, stamped(node("a")), rows)["candidates"]
    assert got["workloads"][0]["saving"] == pytest.approx(7 * each * 0.65)
    assert got["saving"] == pytest.approx(7 * each * 0.65) and got["note"] is None
    # a blended price is undone first, and the rate is a spot node's against on-demand
    cheaper = contracts.ClusterPrices(cpu=0.04 * 0.8, memory=0.005 * 0.8)
    test, *rest = c.clusters
    blended = c.model_copy(
        update={"clusters": [test.model_copy(update={"prices": cheaper}), *rest]}
    )
    blended._spot_shares = {"test": (0.8, 0.35 / 0.6)}
    got = shown(blended, stamped(node("a")), rows)["candidates"]
    assert got["saving"] == pytest.approx(7 * each * (1 - 0.35 / 0.6))
    blended._spot_shares = {"test": (1.0, 1.2)}  # a spot rate above on-demand saves nothing
    assert shown(blended, stamped(node("a")), rows)["candidates"]["saving"] == 0


def test_without_spot_or_a_price_a_candidate_has_no_money_and_says_why() -> None:
    rows = [row("api", 3.0)]
    plain = shown(spotted(spot=None), stamped(node("a")), rows)["candidates"]
    assert names(plain) == ["api"] and plain["saving"] is None
    assert plain["workloads"][0]["saving"] is None
    assert plain["note"] == "set the cluster's spot to price it"
    unpriced = shown(spotted(prices=False), stamped(node("a")), rows)["candidates"]
    assert unpriced["saving"] is None and unpriced["note"] == "no price for this cluster"
    none = shown(spotted(spot=None), stamped(node("a")), [])["candidates"]
    assert none == {"workloads": [], "more": 0, "saving": None, "note": None}  # nothing to say


def test_candidates_are_ranked_by_saving_and_the_total_counts_every_one() -> None:
    rows = [row(f"w{i:02}", 3.0, cpu=0.1 * (i + 1)) for i in range(25)]
    got = shown(spotted(), stamped(node("a")), rows)["candidates"]
    assert names(got)[:2] == ["w24", "w23"] and len(got["workloads"]) == sizing.MAX_CANDIDATES
    assert got["more"] == 5
    every = 0.0
    for i in range(25):
        each = sizing.monthly(
            contracts.ClusterPrices(cpu=0.04, memory=0.005), cpu=0.1 * (i + 1), memory=GI
        )
        every += 1 * each * 0.65  # 3 replicas: 2 on-demand, 1 on spot
    assert got["saving"] == pytest.approx(every)


def test_a_read_from_before_the_volumes_were_read_lists_no_candidate() -> None:
    old = read_of(node("a", pod("api-1")))  # no "pvc" stamp
    got = shown(spotted(), old, [row("api", 3.0)])["candidates"]
    assert got["workloads"] == [] and got["note"].startswith("its nodes were read before")
    broken = old | {"nodes": [], "problem": "no nodes read"}
    assert shown(spotted(), broken, [row("api", 3.0)])["candidates"] is None


# --- intended against actual ------------------------------------------------------------


def test_a_workload_under_a_spot_ratio_shows_intended_against_actual() -> None:
    rule = {"namespace": "shop", "policy": "production", "spot": RATIO}
    pods = [pod(f"api-{i}") for i in range(4)]
    a, b = node("a", *pods[:3]), node("b", pods[3]) | {"spot": True}
    got = shown(spotted(rule), stamped(a, b), [])["spot_workloads"]
    assert got["note"] == "its Actuator doesn't place them yet"
    assert got["workloads"] == [
        {
            **{"namespace": "shop", "kind": "Deployment", "name": "api", "replicas": 4},
            **{"ratio": 0.7, "on_demand": 2, "why": None},
            **{"intended": {"on_demand": 2, "spot": 2}, "actual": {"on_demand": 3, "spot": 1}},
            "risk": None,
        }
    ]
    unread = shown(spotted(rule), stamped(a, b, label=None), [])["spot_workloads"]
    assert unread["workloads"][0]["actual"] is None  # not read with the label: not known
    assert shown(spotted(), stamped(a, b), [])["spot_workloads"] is None  # no rule


def test_a_workload_that_cant_have_spot_replicas_says_why() -> None:
    rule = {"namespace": "shop", "policy": "production", "spot": RATIO}
    db = [pod(f"db-{i}", workload=("StatefulSet", "db")) for i in range(3)]
    one = pod("one-0", workload=("Deployment", "one"))
    web = [pod(f"w{i}", workload=("Deployment", "web")) | {"kind": ""} for i in range(2)]
    report = FRESH | {
        "pods": [{"namespace": "shop", "pod": "w0", "node": "a", "kind": "", "name": ""}]
    }
    cron = [pod("nightly-1", workload=("CronJob", "nightly"))]
    read = stamped(node("a", *db, one, *web, *cron))
    got = shown(spotted(rule), read, [], report)["spot_workloads"]
    why = {w["name"]: (w["why"], w["intended"]) for w in got["workloads"]}
    assert why == {
        "db": ("a StatefulSet never runs on spot", None),
        "nightly": ("CronJobs on spot come with slice 16.3", None),
        "one": ("fewer than 2 replicas: none on spot", {"on_demand": 1, "spot": 0}),
        "web": ("it blocks a node: none on spot", {"on_demand": 2, "spot": 0}),
    }


# --- the read ---------------------------------------------------------------------------


def test_the_read_of_the_nodes_says_which_pods_claim_a_persistent_volume() -> None:
    fake = cluster()  # api-1 and db-0 in shop, on n1
    fake.add("kube_pod_spec_volumes_persistentvolumeclaims_info", 1, namespace="shop", pod="db-0",
             persistentvolumeclaim="data", volume="data")  # fmt: skip
    fake.add(
        "kube_pod_spec_volumes_persistentvolumeclaims_info",
        1,
        namespace="bank",
        pod="api-1",
        persistentvolumeclaim="x",
        volume="x",
    )  # another Contract's pod of the same name
    got = usage.rooms(_prom(fake, SHOP), SHOP, NOW)
    claims = {p["pod"]: p["pvc"] for n in got["nodes"] for p in n["own"]}
    assert claims["db-0"] is True and claims["api-1"] is False and got["pvc"] is True
    asked = [q for q in fake.queries if "persistentvolumeclaims" in q]
    assert len(asked) == 1 and 'namespace=~"shop"' in asked[0]


@pytest.mark.anyio
async def test_one_contracts_spot_lists_never_name_anothers_workloads(
    store: Any,  # noqa: F811
) -> None:
    from squero import webapp

    a = spotted({"namespace": "shop", "name": "api", "policy": "production", "spot": RATIO})
    data = a.model_dump() | {"id": "bank", "name": "bank"}
    data["sizing"]["rules"][0]["name"] = "ledger"
    b = contracts.build(data, secrets=a._secrets)
    mine = node("n", *[pod(f"api-{i}") for i in range(2)])
    theirs = node("n", *[pod(f"ledger-{i}", workload=("Deployment", "ledger")) for i in range(2)])
    await store.save_rooms("acme", "test", NOW, stamped(mine))
    await store.save_rooms("bank", "test", NOW, stamped(theirs))
    for cid, name in (("acme", "web"), ("bank", "vault")):  # each its own candidate
        run = await store.claim_sizing_run(cid, NOW, 0)
        assert run is not None
        await store.finish_sizing_run(run, cid, {"test": None}, [row(name, 3.0)])
    got = await webapp.nodes_of(store, a, NOW)
    assert "ledger" not in got.model_dump_json() and "vault" not in got.model_dump_json()
    assert got.clusters[0].candidates
    assert [w.name for w in got.clusters[0].candidates.workloads] == ["web"]
    (mine_shown,) = got.clusters
    assert mine_shown.spot_workloads and [w.name for w in mine_shown.spot_workloads.workloads] == [
        "api"
    ]
    (theirs_shown,) = (await webapp.nodes_of(store, b, NOW)).clusters
    assert "api-" not in theirs_shown.model_dump_json()
    assert "web" not in theirs_shown.model_dump_json()
    assert theirs_shown.candidates
    assert [w.name for w in theirs_shown.candidates.workloads] == ["vault"]
    assert theirs_shown.spot_workloads
    assert [w.name for w in theirs_shown.spot_workloads.workloads] == ["ledger"]


def test_a_rule_counts_only_where_its_cluster_has_both_a_spot_label_and_an_actuator() -> None:
    data = actuated("acme", A_TOKEN, spot=SPOT).model_dump()
    third = data["clusters"][1] | {
        "name": "third",
        "context": "third",
        "actuator_token_secret": "act",
    }
    data["clusters"][1] |= {"spot": SPOT}  # edge: a spot label, no Actuator
    data["clusters"].append(third)  # third: an Actuator, no spot label
    data["sizing"]["rules"] = [{"namespace": "shop", "policy": "production", "spot": RATIO}]
    c = contracts.build(data, secrets=actuated("acme", A_TOKEN)._secrets)
    assert c.spot_for("test", "shop", "Deployment", "api") == SpotRule(**RATIO)
    assert c.spot_for("edge", "shop", "Deployment", "api") is None
    assert c.spot_for("third", "shop", "Deployment", "api") is None
    # its Workloads there are candidates, and listed under no ratio
    reads = {
        name: stamped(node("a", *[pod(f"api-{i}") for i in range(2)])) for name in ("edge", "third")
    }
    rows = [row("api", 3.0, cluster=name) for name in ("edge", "third")]
    for shown_ in sizing.nodes_report(reads, c, {}, NOW, rows):
        assert shown_["spot_workloads"] is None
        assert names(shown_["candidates"]) == ["api"]


def test_candidates_rank_by_saving_not_by_cores() -> None:
    rows = [row("cores", 3.0, cpu=0.5, gib=0.5), row("memory", 3.0, cpu=0.1, gib=10.0)]
    got = shown(spotted(), stamped(node("a")), rows)["candidates"]
    assert names(got) == ["memory", "cores"]  # 0.054 an hour a pod against 0.0225


def test_a_volume_is_read_by_namespace_and_pod() -> None:
    from tests.test_nodes import node as fake_node
    from tests.test_nodes import pod as fake_pod

    both = usage.Target("test", "http://prometheus", None, {}, ["shop", "web"])
    fake = FakePrometheus()
    fake_node(fake, "n1")
    fake_pod(fake, "x-0")
    fake_pod(fake, "x-0", ns="web")
    fake.add("kube_pod_spec_volumes_persistentvolumeclaims_info", 1, namespace="web", pod="x-0",
             persistentvolumeclaim="data")  # fmt: skip
    got = usage.rooms(_prom(fake, both), both, NOW)
    assert {(p["namespace"], p["pvc"]) for n in got["nodes"] for p in n["own"]} == {
        ("shop", False),
        ("web", True),
    }


def test_the_hub_mcp_says_the_candidates_and_caps_the_workloads_under_a_ratio() -> None:
    from squero import hubmcp, webapp

    rule = {"namespace": "shop", "policy": "production", "spot": RATIO}
    many = [
        pod(f"w{i}-{j}", workload=("Deployment", f"w{i:02}")) for i in range(25) for j in range(2)
    ]
    got = shown(spotted(rule), stamped(node("a", *many)), [row("api", 3.0)])
    mcp = hubmcp._nodes(webapp.Nodes.model_validate({"clusters": [got]}).clusters[0], 20)
    assert mcp["spot_candidates"] == got["candidates"] | {"workloads": [
        webapp.Candidate.model_validate(w).model_dump() for w in got["candidates"]["workloads"]
    ]}  # fmt: skip
    assert len(mcp["spot_workloads"]["workloads"]) == 20
    assert mcp["spot_workloads"]["more_workloads"] == 5
