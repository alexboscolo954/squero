"""Spot nodes priced (slice 16.1a): the cluster's spot label and discount; spot nodes
read through kube-state-metrics; a list price counts each spot node at the discount, a
typed price is blended by the cluster's spot and on-demand capacity; every pod costs the
blended price, and a node that could go is priced at its own rate."""

from typing import Any

import pytest
from pydantic import ValidationError

from squero import contracts, listprices, sizing, usage, webapp
from squero.store import Store
from tests.conftest import make_contract
from tests.fake_prometheus import FakePrometheus
from tests.test_could_go import node as room_node
from tests.test_could_go import pod, read_of
from tests.test_listprices import C5, M5, ROWS, facts, listed, node, read
from tests.test_sizing_store import NOW, prometheus_contract, store  # noqa: F401
from tests.test_usage import _prom

SPOT = {"label": "karpenter.sh/capacity-type=spot", "discount": 0.35}
LABEL = {"label_karpenter_sh_capacity_type": "spot"}


def spotted(*nodes: tuple[Any, bool], label: str | None = SPOT["label"]) -> dict[str, Any]:
    """Node facts as the hourly run stores them, each node spot or not."""
    out = facts(*(n for n, _ in nodes))
    for f, (_, on_spot) in zip(out["nodes"], nodes, strict=True):
        f["spot"] = on_spot
    return out | {"spot": label}


def typed(**cluster: Any) -> contracts.Contract:
    data = prometheus_contract().model_dump()
    data["prices"] = {"cpu": 0.04, "memory": 0.005}
    data["clusters"][0] |= cluster
    return contracts.build(data)


# --- the Contract -----------------------------------------------------------------------


def test_spot_is_a_label_and_the_share_of_on_demand_a_spot_node_costs() -> None:
    assert listed(spot=SPOT).clusters[0].spot == contracts.Spot(**SPOT)
    for wrong in (
        {"label": "karpenter.sh/capacity-type", "discount": 0.3},  # a key alone
        {"label": "a=b", "discount": 0},
        {"label": "a=b", "discount": 1},  # no discount at all
        {"label": "a=b", "discount": 0.65, "off": True},
        {"label": "a=b"},
    ):
        with pytest.raises(ValidationError):
            listed(spot=wrong)


# --- the read ---------------------------------------------------------------------------


def test_each_node_says_whether_it_is_spot_as_kube_state_metrics_shows_it() -> None:
    fake = FakePrometheus()
    node(fake, "a")
    fake.add("kube_node_labels", 1, node="b", label_node_kubernetes_io_instance_type="m5.large",
             label_topology_kubernetes_io_region="us-east-1", **LABEL)  # fmt: skip
    fake.add("kube_node_info", 1, node="b", provider_id="aws:///us-east-1a/i-b")
    for res, v in (("cpu", 2), ("memory", 8 * 2**30)):
        fake.add("kube_node_status_capacity", v, node="b", resource=res)
    t = usage.Target("test", "http://prometheus", None, {}, ["shop"])
    got = usage.nodes(_prom(fake, t), t, NOW, None, SPOT["label"])
    assert [n["spot"] for n in got["nodes"]] == [False, True] and got["spot"] == SPOT["label"]
    plain = read(fake)
    assert not any(n["spot"] for n in plain["nodes"]) and plain["spot"] is None
    rooms = usage.rooms(_prom(fake, t), t, NOW, None, SPOT["label"])
    assert rooms["spot"] == SPOT["label"]


def test_the_hourly_run_reads_the_nodes_of_a_typed_cluster_with_spot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakePrometheus()
    node(fake, "a")
    monkeypatch.setattr(usage, "Prom", lambda t: _prom(fake, t))
    c = typed(spot=SPOT)
    assert usage.collect(c, [86400], NOW).nodes["test"]["spot"] == SPOT["label"]
    assert usage.collect(typed(), [86400], NOW).nodes == {}  # typed, no spot: no read


# --- the cluster's price ----------------------------------------------------------------


def test_a_spot_nodes_list_price_counts_at_the_discount() -> None:
    nodes = {"test": spotted(*[(M5, False)] * 6, (C5, True), (C5, True))}
    prices, notes = listprices.resolve(listed(spot=SPOT), nodes, ROWS, NOW)
    p = prices["test"]
    paid = 6 * 0.096 + 2 * 0.17 * 0.35
    assert p.cpu * 20 + p.memory * 64 == pytest.approx(paid)  # still adds back up
    assert notes[0]["note"].endswith(", spot x0.35 on 2 of 8 nodes")
    plain, _ = listprices.resolve(listed(), nodes, ROWS, NOW)
    assert plain["test"].cpu * 20 + plain["test"].memory * 64 == pytest.approx(0.916)


def test_a_deal_is_an_on_demand_nodes_only() -> None:
    """A committed-use deal (`list_prices.discount`) covers no spot capacity: a spot node
    pays its own discount of the list price and no more (the Operator's choice)."""
    nodes = {"test": spotted((M5, False), (M5, True))}
    shares: dict[str, Any] = {}
    prices, notes = listprices.resolve(listed(discount=0.6, spot=SPOT), nodes, ROWS, NOW, shares)
    paid = 0.096 * 0.6 + 0.096 * 0.35
    assert prices["test"].cpu * 4 + prices["test"].memory * 16 == pytest.approx(paid)
    assert shares["test"] == (pytest.approx(paid / (2 * 0.096 * 0.6)), pytest.approx(0.35 / 0.6))
    assert "discount x0.6" in notes[0]["note"] and "spot x0.35 on 1 of 2 nodes" in notes[0]["note"]
    deal_only, _ = listprices.resolve(listed(discount=0.6), nodes, ROWS, NOW)
    assert deal_only["test"].cpu * 4 + deal_only["test"].memory * 16 == pytest.approx(
        2 * 0.096 * 0.6
    )


@pytest.mark.parametrize(
    ("label", "said"),
    [
        (None, ", spot not priced yet: the next hourly run reads karpenter.sh/capacity-type=spot"),
        (
            "other=x",
            ", spot not priced yet: the next hourly run reads karpenter.sh/capacity-type=spot",
        ),
    ],
)
def test_a_read_made_without_the_label_prices_every_node_on_demand(label: Any, said: str) -> None:
    nodes = {"test": spotted((M5, True), (M5, True), label=label)}
    prices, notes = listprices.resolve(listed(spot=SPOT), nodes, ROWS, NOW)
    assert prices["test"].cpu * 4 + prices["test"].memory * 16 == pytest.approx(0.192)
    assert notes[0]["note"].endswith(said)


def test_a_read_that_failed_says_why_spot_isnt_priced() -> None:
    nodes = {"test": {"nodes": [], "problem": "no nodes read: kube_node_info is missing"}}
    c = listed(spot=SPOT, prices={"cpu": 0.04, "memory": 0.005})
    prices, notes = listprices.resolve(c, nodes, ROWS, NOW)
    assert prices["test"].cpu == 0.04
    assert (
        notes[0]["note"]
        == "typed prices, spot not priced: no nodes read: kube_node_info is missing"
    )


def test_the_share_applied_is_kept_for_each_spot_cluster() -> None:
    shares: dict[str, float] = {}
    nodes = {"test": spotted((M5, False), (M5, True))}
    listprices.resolve(listed(spot=SPOT), nodes, ROWS, NOW, shares)
    assert shares == {"test": (pytest.approx(0.675), 0.35)}  # two m5.large, one at 0.35
    typed_too: dict[str, float] = {}
    listprices.resolve(
        listed(spot=SPOT, prices={"cpu": 0.04, "memory": 0.005}), nodes, ROWS, NOW, typed_too
    )
    assert typed_too == {"test": (pytest.approx(0.675), 0.35)}
    none: dict[str, float] = {}
    listprices.resolve(listed(), nodes, ROWS, NOW, none)
    assert none == {}


def test_no_node_carrying_the_label_says_to_allowlist_it() -> None:
    nodes = {"test": spotted((M5, False), (M5, False))}
    prices, notes = listprices.resolve(listed(spot=SPOT), nodes, ROWS, NOW)
    assert prices["test"].cpu * 4 + prices["test"].memory * 16 == pytest.approx(0.192)
    assert (
        "no node carries karpenter.sh/capacity-type=spot as kube-state-metrics shows it"
        in notes[0]["note"]
    )


def test_a_typed_price_is_the_on_demand_rate_blended_by_size() -> None:
    # two equal nodes, one spot: the cluster pays (1 + 0.35) / 2 of the typed rate
    two = spotted((M5, False), (M5, True))
    c = listed(spot=SPOT, prices={"cpu": 0.04, "memory": 0.005})
    prices, notes = listprices.resolve(c, {"test": two}, ROWS, NOW)
    assert prices["test"].cpu == pytest.approx(0.04 * 0.675)
    assert prices["test"].memory == pytest.approx(0.005 * 0.675)
    said = "typed prices, spot x0.35 on 1 of 2 nodes: x0.675 of them"
    assert notes[0] == {"cluster": "test", "source": "typed", "note": said}
    # weighted by size, one vCPU worth `weight` GiB as a price is split
    big = spotted((C5, True), (M5, False))  # 4 vCPU 8 GiB spot, 2 vCPU 8 GiB on demand
    share = (0.35 * (7 * 4 + 8) + (7 * 2 + 8)) / (7 * 4 + 8 + 7 * 2 + 8)
    prices, _ = listprices.resolve(c, {"test": big}, ROWS, NOW)
    assert prices["test"].cpu == pytest.approx(0.04 * share)
    unspotted, notes = listprices.resolve(
        listed(prices={"cpu": 0.04, "memory": 0.005}), {"test": big}, ROWS, NOW
    )
    assert unspotted["test"].cpu == 0.04 and notes[0]["note"] == "typed prices"


def test_what_a_clusters_nodes_cost_as_a_share_of_on_demand() -> None:
    assert listprices.spot_paid([], 7.0, 0.35) == (1.0, 0)
    assert listprices.spot_paid([(2, 8, False)], 7.0, 0.35) == (1.0, 0)
    assert listprices.spot_paid([(2, 8, True)], 7.0, 0.35) == (pytest.approx(0.35), 1)


@pytest.mark.anyio
async def test_a_typed_contract_blends_its_spot_clusters_only(store: Store) -> None:  # noqa: F811
    data = make_contract(clusters={"a": ["shop"], "b": ["shop"]}).model_dump()
    data["integrations"].append({"name": "prometheus", "url": "http://prometheus"})
    data["prices"] = {"cpu": 0.04, "memory": 0.005, "currency": "EUR"}
    data["clusters"][0]["spot"] = SPOT
    c = contracts.build(data)
    plain = prometheus_contract()
    assert await listprices.effective(store, plain, NOW) is plain  # no spot: as it is
    await store.save_nodes("acme", "a", NOW, spotted((M5, False), (M5, True)))
    eff = await listprices.effective(store, c, NOW)
    a = eff.price("a")
    assert a and (a.cpu, a.memory) == (pytest.approx(0.04 * 0.675), pytest.approx(0.005 * 0.675))
    b = eff.price("b")  # the Contract's own
    assert b and (b.cpu, b.memory) == (0.04, 0.005)
    assert eff.prices and eff.prices.currency == "EUR"
    assert [n["cluster"] for n in eff._price_notes] == ["a"]
    assert eff._spot_shares == {"a": (pytest.approx(0.675), 0.35)}
    assert await listprices.effective(store, eff, NOW) is eff  # resolved once
    assert c._price_notes == []  # the loaded Contract is left alone
    await store.save_nodes("bank", "a", NOW, spotted((M5, True), (M5, True)))  # another's
    again = await listprices.effective(store, c, NOW)
    assert again.price("a") == eff.price("a")


# --- nodes that could go ----------------------------------------------------------------


def test_a_node_that_could_go_is_priced_at_its_own_rate() -> None:
    c = typed(spot=SPOT)
    share = (1 + 0.35) / 2  # two nodes of one size
    c._spot_shares = {"test": (share, 0.35)}  # as its pricing applied it
    a, b = room_node("a", pod("a1", 0.5)), room_node("b", pod("b1", 0.5)) | {"spot": True}
    read = read_of(a, b) | {"spot": SPOT["label"]}
    rate = sizing.rates(read, c, "test")
    assert rate == {"a": pytest.approx(1 / share), "b": pytest.approx(0.35 / share)}
    blended = contracts.ClusterPrices(cpu=0.04 * share, memory=0.005 * share)
    each = sizing.monthly(blended, cpu=4.1, memory=16.5 * 2**30)  # a node's capacity
    on_demand = sizing._worth(["a"], read, blended, rate)
    assert on_demand["cost"] == pytest.approx(each / share)
    assert on_demand["cost"] == pytest.approx(
        sizing.monthly(
            contracts.ClusterPrices(cpu=0.04, memory=0.005), cpu=4.1, memory=16.5 * 2**30
        )
    )
    assert sizing._worth(["b"], read, blended, rate)["cost"] == pytest.approx(each * 0.35 / share)
    assert sizing.rates(read | {"spot": None}, c, "test") == {}  # read without the label
    assert sizing.rates(read, typed(spot=SPOT), "test") == {}  # its spot wasn't priced
    assert sizing.rates(read, typed(), "test") == {}  # no spot in the Contract


@pytest.mark.anyio
async def test_the_nodes_page_prices_a_node_that_could_go_from_the_stored_prices(
    store: Store,  # noqa: F811
) -> None:
    """Through what the Hub stored: the node facts its price blends, the read of the
    nodes, its Actuator's report. An on-demand node costs the typed rate, a spot node
    the discount of it, whatever the blend."""
    from tests.test_actuator import A_TOKEN, actuated
    from tests.test_could_go import FRESH

    data = actuated("acme", A_TOKEN, spot=SPOT).model_dump()
    data["integrations"].append({"name": "prometheus", "url": "http://prometheus"})
    data["prices"] = {"cpu": 0.04, "memory": 0.005}
    c = contracts.build(data, secrets=actuated("acme", A_TOKEN)._secrets)
    big = room_node("big", pod("p1", 3.0), pod("p2", 0.5))  # fits nowhere: stays
    a, b = room_node("a", pod("a1", 0.1)), room_node("b", pod("b1", 0.1)) | {"spot": True}
    await store.save_rooms("acme", "test", NOW, read_of(a, b, big) | {"spot": SPOT["label"]})
    await store.save_blockers(
        "acme", "test", NOW, {k: v for k, v in FRESH.items() if k != "received"}
    )
    sized = [(4.0, 16.0, False), (4.0, 16.0, True), (4.0, 16.0, False)]
    stored = facts(*[("aws", "us-east-1", "m5.xlarge", v, g) for v, g, _ in sized])
    for f, (_, _, on_spot) in zip(stored["nodes"], sized, strict=True):
        f["spot"] = on_spot
    await store.save_nodes("acme", "test", NOW, stored | {"spot": SPOT["label"]})
    got = await webapp.nodes_of(store, c, NOW)
    went = got.clusters[0].could_go
    assert went is not None and went.now.nodes == ["a", "b"]
    full = contracts.ClusterPrices(cpu=0.04, memory=0.005)
    each = sizing.monthly(full, cpu=4.1, memory=16.5 * 2**30)  # a node's capacity, on demand
    assert went.now.cost == pytest.approx(each + each * 0.35)
    assert [n.spot for n in got.clusters[0].nodes] == [False, True, False]


def test_the_nodes_page_marks_spot_nodes_and_says_when_none_shows() -> None:
    c = typed(spot=SPOT)
    a, b = room_node("a", pod("a1")), room_node("b", pod("b1")) | {"spot": True}
    read = read_of(a, b) | {"spot": SPOT["label"]}
    (shown,) = sizing.nodes_report({"test": read}, c, {}, NOW)
    assert shown["spot"] == {**SPOT, "nodes": 1, "note": None}
    got = webapp.Nodes.model_validate({"clusters": [shown]})
    assert [n.spot for n in got.clusters[0].nodes] == [False, True]
    none = read_of(room_node("a")) | {"spot": SPOT["label"]}
    (bare,) = sizing.nodes_report({"test": none}, c, {}, NOW)
    assert bare["spot"]["note"].startswith("no node carries karpenter.sh/capacity-type=spot")
    (old,) = sizing.nodes_report({"test": read_of(a)}, c, {}, NOW)
    assert old["spot"]["note"].startswith("its nodes were not read with")
    (plain,) = sizing.nodes_report({"test": read}, typed(), {}, NOW)
    assert plain["spot"] is None
