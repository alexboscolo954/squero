"""Cloud list prices (slice 10.4): a cluster with no typed prices is priced from what
its nodes cost on demand. Node facts come from kube-state-metrics, prices from the
clouds' public lists once a day; a node's price splits into a CPU and a GiB price by
one weight, and the cluster's prices are its nodes' totals, so they add back up."""

import asyncio
import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from squero import cli, contracts, listprices, sizing, usage
from squero.config import Settings
from squero.store import Store, connect
from tests.conftest import make_contract
from tests.fake_prometheus import FakePrometheus
from tests.test_api import client
from tests.test_sizing_policy import lab
from tests.test_sizing_store import NOW, H, prometheus_contract
from tests.test_usage import _prom, prom

GI = 2**30


def listed(weight: float = 7.0, discount: float = 1.0, **cluster: Any) -> contracts.Contract:
    data = make_contract().model_dump()
    data["integrations"].append({"name": "prometheus", "url": "http://prometheus"})
    data["list_prices"] = {"weight": weight, "discount": discount}
    if cluster:
        data["clusters"][0].update(cluster)
    return contracts.build(data)


def node(
    fake: FakePrometheus,
    name: str,
    kind: str | None = "m5.large",
    region: str | None = "us-east-1",
    cpu: float = 2,
    gib: float = 8,
    provider_id: str = "aws:///us-east-1a/i-0abc",
    **extra: str,
) -> None:
    fake.add("kube_node_info", 1, node=name, provider_id=provider_id, **extra)
    labels = {}
    if kind:
        labels["label_node_kubernetes_io_instance_type"] = kind
    if region:
        labels["label_topology_kubernetes_io_region"] = region
    fake.add("kube_node_labels", 1, node=name, **labels, **extra)
    fake.add("kube_node_status_capacity", cpu, node=name, resource="cpu", unit="core", **extra)
    fake.add(
        "kube_node_status_capacity", gib * GI, node=name, resource="memory", unit="byte", **extra
    )


def facts(*nodes: tuple[str, str, str, float, float], problem: str | None = None) -> dict[str, Any]:
    return {
        "nodes": [
            {"provider": p, "region": r, "type": t, "vcpu": v, "gib": g} for p, r, t, v, g in nodes
        ],
        "problem": problem,
    }


def price(usd: float | None, ok_at: float = NOW, error: str | None = None) -> dict[str, Any]:
    return {"hour_price": usd, "ok_at": ok_at if usd is not None else None, "error": error}


M5 = ("aws", "us-east-1", "m5.large", 2.0, 8.0)
C5 = ("aws", "us-east-1", "c5.xlarge", 4.0, 8.0)
ROWS = {
    ("aws", "us-east-1", "m5.large"): price(0.096),
    ("aws", "us-east-1", "c5.xlarge"): price(0.17),
}
# 6 m5.large and 2 c5.xlarge: 0.916 USD/h over 20 vCPU and 64 GiB
MIXED = {"test": facts(*[M5] * 6, *[C5] * 2)}


# --- the Contract ------------------------------------------------------------------


def test_list_prices_validate() -> None:
    assert listed().list_prices == contracts.ListPrices()
    assert listed(weight=4, discount=0.6).list_prices == contracts.ListPrices(
        weight=4, discount=0.6
    )
    for bad in ({"weight": 0}, {"weight": -1}, {"discount": 0}, {"discount": 1.2}):
        with pytest.raises(ValueError):
            contracts.ListPrices(**bad)
    base = listed().model_dump() | {"prices": {"cpu": 0.04, "memory": 0.005}}
    with pytest.raises(ValueError, match="by one of them"):
        contracts.build(base)
    cluster = make_contract().model_dump()
    cluster["list_prices"] = {}
    with pytest.raises(ValueError, match="needs a cluster with a Prometheus"):
        contracts.build(cluster)
    with pytest.raises(ValueError):
        listed(provider="oracle")
    # a cluster's typed prices are allowed without the Contract's: the list fills the rest
    assert listed(prices={"cpu": 0.04, "memory": 0.005}).clusters[0].prices


# --- the nodes ----------------------------------------------------------------------

TARGET = usage.Target("test", "http://prometheus", None, {}, ["shop"])


def read(fake: FakePrometheus, t: usage.Target = TARGET, provider: str | None = None) -> Any:
    return usage.nodes(prom(fake), t, NOW, provider)


def test_nodes_come_from_kube_state_metrics() -> None:
    fake = FakePrometheus()
    node(fake, "a", "m5.large", cpu=2, gib=8)
    node(fake, "b", "c5.xlarge", cpu=4, gib=8, provider_id="aws:///us-east-1b/i-0def")
    got = read(fake)
    assert got["problem"] is None
    on_demand = {"spot": False}  # slice 16.1a: no spot label given
    assert got["nodes"] == [
        {"provider": "aws", "region": "us-east-1", "type": "m5.large", "vcpu": 2, "gib": 8}
        | on_demand,
        {"provider": "aws", "region": "us-east-1", "type": "c5.xlarge", "vcpu": 4, "gib": 8}
        | on_demand,
    ]


def test_the_provider_is_read_from_the_node_or_declared() -> None:
    fake = FakePrometheus()
    node(fake, "a", "Standard_D2s_v3", "eastus", provider_id="azure:///subscriptions/x/vm")
    node(fake, "b", "n2-standard-2", "us-central1", provider_id="gce://proj/zone/vm")
    assert [n["provider"] for n in read(fake)["nodes"]] == ["azure", "gcp"]
    kind = FakePrometheus()
    node(kind, "k", "m5.large", provider_id="kind://docker/squero/squero-control-plane")
    assert "lack" in read(kind)["problem"]  # kind says nothing of a cloud
    assert read(kind, provider="aws")["problem"] is None
    assert read(kind, provider="aws")["nodes"][0]["provider"] == "aws"
    declared = FakePrometheus()
    node(declared, "a")  # an AWS provider_id
    assert read(declared, provider="azure")["nodes"][0]["provider"] == "azure"  # declared wins


def test_a_node_the_list_cant_price_says_why() -> None:
    fake = FakePrometheus()
    node(fake, "a")
    node(fake, "b", kind=None)  # kube-state-metrics doesn't allowlist the label
    problem = read(fake)["problem"]
    assert "1 of 2 nodes lack" in problem and "allowlist the node labels" in problem
    no_region = FakePrometheus()
    node(no_region, "a", region=None)
    assert "1 of 1" in read(no_region)["problem"]
    assert "kube_node_info is missing" in read(FakePrometheus())["problem"]


def test_a_hostile_label_never_reaches_a_url_or_a_filter() -> None:
    """Instance types and regions are the cluster owner's labels."""
    for kind, region in (
        ("x' or armSkuName ne '", "us-east-1"),
        ("m5.large", "../../etc"),
        ("m5.large", "us-east-1/../x"),
        ("m5 large", "us-east-1"),
    ):
        fake = FakePrometheus()
        node(fake, "a", kind, region)
        assert "1 of 1" in read(fake)["problem"]
    asked: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request)
        return httpx.Response(200, json={"Items": []})

    for provider, region, kind in (
        ("azure", "eastus", "x' or armSkuName ne '"),
        ("aws", "../../x", "m5.large"),
        ("gcp", "us-central1", "n2 standard"),
    ):
        with pytest.raises(ValueError, match="not a region or instance type name"):
            listprices.fetch(http_of(handler), provider, region, {kind: (1, 1)}, "KEY")
    assert asked == []


def test_a_nodes_capacity_is_needed_too() -> None:
    fake = FakePrometheus()
    node(fake, "a")
    fake.series = [s for s in fake.series if s["__name__"] != "kube_node_status_capacity"]
    assert "1 of 1" in read(fake)["problem"]


def test_only_the_clusters_own_nodes_are_read() -> None:
    fake = FakePrometheus()
    node(fake, "mine", cluster="eu")
    node(fake, "theirs", cluster="us")
    mine = usage.Target("eu", "http://prometheus", None, {"cluster": "eu"}, ["shop"])
    assert len(read(fake, mine)["nodes"]) == 1
    assert len(read(fake)["nodes"]) == 2  # no labels, no scoping: the shared-store guard


def test_a_shared_store_without_cluster_labels_reads_no_nodes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakePrometheus()
    node(fake, "a")
    lab(fake)
    monkeypatch.setattr(usage, "Prom", lambda t: _prom(fake, t))
    c = listed()
    c._shared_stores.add("test")
    got = usage.collect(c, [86400], NOW).nodes["test"]
    assert got["nodes"] == [] and "shared" in got["problem"]
    alone = usage.collect(listed(), [86400], NOW).nodes["test"]
    assert len(alone["nodes"]) == 1 and alone["problem"] is None
    typed = usage.collect(prometheus_contract(), [86400], NOW)
    assert typed.nodes == {}  # no list_prices: nothing asked


# --- the price lists ----------------------------------------------------------------


def aws_file(extra: dict[str, Any] | None = None) -> bytes:
    def product(kind: str, **a: str) -> dict[str, Any]:
        base = {
            "instanceType": kind,
            "operatingSystem": "Linux",
            "tenancy": "Shared",
            "preInstalledSw": "NA",
            "capacitystatus": "Used",
            "operation": "RunInstances",
        }
        return {"attributes": base | a}

    products = {
        "M5": product("m5.large"),
        "M5WIN": product("m5.large", operatingSystem="Windows"),
        "M5DED": product("m5.large", tenancy="Dedicated"),
        "M5SQL": product("m5.large", preInstalledSw="SQL Std"),
        "M5RES": product("m5.large", capacitystatus="AllocatedCapacityReservation"),
        "M5HOST": product("m5.large", operation="RunInstances:0002"),
        "C5": product("c5.xlarge"),
        "T3": product("t3.micro"),
        **(extra or {}),
    }
    usd = {"M5": "0.0960000000", "M5WIN": "0.1880", "M5DED": "0.1100", "M5SQL": "0.2",
           "M5RES": "0.5", "M5HOST": "0.7", "C5": "0.1700000000", "T3": "0.0104"}  # fmt: skip

    def term(sku: str) -> dict[str, Any]:
        dim = {"unit": "Hrs", "pricePerUnit": {"USD": usd[sku]}}
        upfront = {"unit": "Quantity", "pricePerUnit": {"USD": "99.0"}}
        return {
            f"{sku}.TERM": {"priceDimensions": {f"{sku}.TERM.D": dim, f"{sku}.TERM.U": upfront}}
        }

    body = {
        "formatVersion": "v1.0",
        "products": products,
        "terms": {"OnDemand": {k: term(k) for k in usd}},
    }
    return json.dumps(body).encode()


def http_of(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_aws_reads_linux_shared_on_demand_hours() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=aws_file())

    got = listprices.aws(
        http_of(handler), "us-east-1", {"m5.large": (2, 8), "c5.xlarge": (4, 8), "x9.huge": (1, 1)}
    )
    assert got == {"m5.large": 0.096, "c5.xlarge": 0.17, "x9.huge": None}
    assert seen == [listprices.AWS.format(region="us-east-1")]


def test_aws_failing_raises() -> None:
    with pytest.raises(httpx.HTTPStatusError):
        listprices.aws(http_of(lambda r: httpx.Response(503)), "us-east-1", {"m5.large": (2, 8)})


def azure_item(
    sku: str, price: float, product: str = "Virtual Machines Dsv3 Series", **kw: Any
) -> dict:
    return {
        "armSkuName": sku,
        "retailPrice": price,
        "productName": product,
        "skuName": kw.pop("skuName", "D2s v3"),
        "meterName": kw.pop("meterName", "D2s v3"),
        "unitOfMeasure": kw.pop("unit", "1 Hour"),
    } | kw


def test_azure_reads_pay_as_you_go_linux_hours_across_pages() -> None:
    asked = []
    page1 = {
        "Items": [
            azure_item("Standard_D2s_v3", 0.096),
            azure_item("Standard_D2s_v3", 0.05, "Virtual Machines Dsv3 Series Windows"),
            azure_item("Standard_D2s_v3", 0.02, skuName="D2s v3 Spot"),
            azure_item("Standard_D2s_v3", 0.03, skuName="D2s v3 Low Priority"),
            azure_item("Standard_D2s_v3", 0.01, unit="1/Month"),
        ],
        "NextPageLink": "https://prices.azure.com/api/retail/prices?page=2",
    }
    page2 = {"Items": [azure_item("Standard_D2s_v3", 0.101), azure_item("Standard_D4s_v3", 0.192)]}

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request)
        return httpx.Response(200, json=page2 if "page=2" in str(request.url) else page1)

    got, spot = listprices.azure(
        http_of(handler),
        "eastus",
        {"Standard_D2s_v3": (2, 8), "Standard_D4s_v3": (4, 16), "Z": (1, 1)},
    )
    assert got == {"Standard_D2s_v3": 0.096, "Standard_D4s_v3": 0.192, "Z": None}
    # slice 16.4a: the Spot meter, from the same items (never Low Priority's)
    assert spot == {"Standard_D2s_v3": 0.02, "Standard_D4s_v3": None, "Z": None}
    flt = asked[0].url.params["$filter"]
    assert "armRegionName eq 'eastus'" in flt and "priceType eq 'Consumption'" in flt
    assert "armSkuName eq 'Standard_D2s_v3'" in flt and "armSkuName eq 'Standard_D4s_v3'" in flt
    assert len(asked) == 2


def sku(desc: str, units: str, nanos: int, regions: list[str], usage_type: str = "OnDemand") -> Any:
    rate = {"unitPrice": {"units": units, "nanos": nanos}}
    return {
        "description": desc,
        "category": {"usageType": usage_type},
        "serviceRegions": regions,
        "pricingInfo": [{"pricingExpression": {"tieredRates": [rate]}}],
    }


def test_gcp_adds_a_machines_cores_and_ram() -> None:
    us = ["us-central1"]
    skus = [
        sku("N2 Instance Core running in Americas", "0", 31611000, us),
        sku("N2 Instance Ram running in Americas", "0", 4237000, us),
        sku("N2 Instance Core running in Frankfurt", "0", 99000000, ["europe-west3"]),
        sku(
            "N2 Spot Preemptible Instance Core running in Americas", "0", 1000000, us, "Preemptible"
        ),
        sku("N2 Custom Instance Core running in Americas", "0", 55000000, us),
        sku("N2 Instance Core running in Americas", "0", 1000000, us, "Commit1Yr"),
        sku("E2 Instance Core running in Americas", "0", 22000000, us),
    ]
    pages = [{"skus": skus[:3], "nextPageToken": "p2"}, {"skus": skus[3:]}]
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=pages[1 if request.url.params["pageToken"] else 0])

    got, _ = listprices.gcp(
        http_of(handler), "us-central1", {"n2-standard-4": (4, 16), "e2-medium": (2, 4)}, "KEY"
    )
    assert got["n2-standard-4"] == pytest.approx(4 * 0.031611 + 16 * 0.004237)
    assert got["e2-medium"] is None  # no E2 RAM price read: not guessed
    assert len(seen) == 2 and all(r.headers["X-Goog-Api-Key"] == "KEY" for r in seen)
    assert all("KEY" not in str(r.url) for r in seen)  # a URL ends up in errors and logs


def test_gcp_needs_its_key_and_an_unknown_cloud_has_no_list() -> None:
    with pytest.raises(ValueError, match="no GCP pricing key"):
        listprices.fetch(http_of(lambda r: httpx.Response(500)), "gcp", "r", {"t": (1, 1)}, None)
    with pytest.raises(ValueError, match="no price list for provider 'oracle'"):
        listprices.fetch(http_of(lambda r: httpx.Response(500)), "oracle", "r", {}, None)


# --- the cluster's prices ------------------------------------------------------------


def resolved(
    c: contracts.Contract, nodes: Any, rows: Any = ROWS, now: float = NOW
) -> tuple[Any, list[dict[str, str]]]:
    return listprices.resolve(c, nodes, rows, now)


def test_a_clusters_prices_add_back_up_to_its_nodes_price() -> None:
    prices, notes = resolved(listed(), MIXED)
    p = prices["test"]
    total = 6 * 0.096 + 2 * 0.17
    assert p.memory == pytest.approx(total / (7 * 20 + 64))  # 0.0044902 per GiB-hour
    assert p.cpu == pytest.approx(7 * p.memory)  # 0.0314314 per vCPU-hour
    assert p.cpu * 20 + p.memory * 64 == pytest.approx(total)
    assert notes[0]["source"] == "list"
    assert "2 x aws c5.xlarge in us-east-1, 6 x aws m5.large in us-east-1" in notes[0]["note"]
    assert "1 vCPU = 7 GiB" in notes[0]["note"]


def test_the_weight_and_the_discount_are_the_contracts() -> None:
    four, _ = resolved(listed(weight=4), MIXED)
    assert four["test"].cpu == pytest.approx(4 * four["test"].memory)
    assert four["test"].cpu * 20 + four["test"].memory * 64 == pytest.approx(0.916)
    deal, notes = resolved(listed(discount=0.6), MIXED)
    base, _ = resolved(listed(), MIXED)
    assert deal["test"].cpu == pytest.approx(0.6 * base["test"].cpu)
    assert deal["test"].memory == pytest.approx(0.6 * base["test"].memory)
    assert "discount x0.6" in notes[0]["note"]
    assert "discount" not in resolved(listed(), MIXED)[1][0]["note"]


def test_typed_prices_win_and_the_list_fills_the_gaps() -> None:
    data = make_contract(clusters={"a": ["shop"], "b": ["shop"]}).model_dump()
    data["integrations"].append({"name": "prometheus", "url": "http://prometheus"})
    data["list_prices"] = {}
    data["clusters"][0]["prices"] = {"cpu": 0.5, "memory": 0.05}
    c = contracts.build(data)
    prices, notes = resolved(c, {"a": facts(M5), "b": facts(M5)})
    assert prices["a"].cpu == 0.5 and prices["a"].memory == 0.05
    assert prices["b"].cpu == pytest.approx(7 * 0.096 / (14 + 8))
    assert [n["source"] for n in notes] == ["typed", "list"]


def test_a_cluster_without_a_price_says_why_and_the_rest_stay_priced() -> None:
    c = listed()
    for nodes, rows, want in (
        ({}, ROWS, "the next hourly run reads the nodes"),
        ({"test": facts(problem="3 of 8 nodes lack an instance type")}, ROWS, "3 of 8 nodes lack"),
        ({"test": facts(M5)}, {}, "no list price yet for aws m5.large in us-east-1"),
        ({"test": facts(M5)}, {M5[:3]: price(None, error="HTTPStatusError: 503")}, "503"),
    ):
        prices, notes = resolved(c, nodes, rows)
        assert prices == {} and notes[0]["source"] == "none" and want in notes[0]["note"]


def test_an_old_price_is_used_and_called_old() -> None:
    nodes = {"test": facts(M5)}
    fresh, notes = resolved(listed(), nodes, {M5[:3]: price(0.096, NOW)}, NOW + 5 * H)
    assert "read 5 h ago" in notes[0]["note"] and "old" not in notes[0]["note"]
    old, notes = resolved(listed(), nodes, {M5[:3]: price(0.096, NOW)}, NOW + 4 * 86400)
    assert old["test"] == fresh["test"] and "96 h ago, 4 days old" in notes[0]["note"]
    mixed = {M5[:3]: price(0.096, NOW), C5[:3]: price(0.17, NOW + 3 * 86400)}
    _, notes = resolved(listed(), {"test": facts(M5, C5)}, mixed, NOW + 3 * 86400)
    assert "72 h ago" in notes[0]["note"]  # the oldest of its prices


# --- the store ------------------------------------------------------------------------


@pytest.fixture
async def store(database_url: str) -> Any:
    async with connect(database_url) as pool:
        s = Store(pool)
        await s.migrate()
        yield s


KEY = ("aws", "us-east-1", "m5.large")


@pytest.mark.anyio
async def test_a_failed_lookup_keeps_the_last_good_price_and_its_age(store: Store) -> None:
    await store.put_list_price(KEY, 0.096, None, NOW)
    await store.put_list_price(KEY, None, "HTTPStatusError: 503", NOW + H)
    row = (await store.list_prices())[KEY]
    assert row["hour_price"] == 0.096 and row["ok_at"] == NOW
    assert row["tried_at"] == NOW + H and row["error"] == "HTTPStatusError: 503"
    await store.put_list_price(KEY, 0.1, None, NOW + 2 * H)
    row = (await store.list_prices())[KEY]
    assert (row["hour_price"], row["ok_at"], row["error"]) == (0.1, NOW + 2 * H, None)


@pytest.mark.anyio
async def test_the_latest_nodes_replace_the_last(store: Store) -> None:
    await store.save_nodes("acme", "test", NOW, facts(M5))
    await store.save_nodes("acme", "test", NOW + H, facts(M5, C5))
    await store.save_nodes("other", "test", NOW, facts(C5))
    got = await store.latest_nodes("acme")
    assert got["test"]["at"] == NOW + H and len(got["test"]["nodes"]) == 2
    assert (await store.latest_nodes("nobody")) == {}


class Lists:
    """The clouds' price lists, counting what was asked."""

    def __init__(self) -> None:
        self.asked: list[str] = []
        self.down = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.asked.append(str(request.url))
        if self.down:
            return httpx.Response(503)
        return httpx.Response(200, content=aws_file())


async def seeded(store: Store, nodes: dict[str, Any] | None = None) -> contracts.Contract:
    for cluster, f in (nodes or MIXED).items():
        await store.save_nodes("acme", cluster, NOW, f)
    return listed()


@pytest.mark.anyio
async def test_the_lists_are_read_daily_and_only_for_the_types_in_use(store: Store) -> None:
    c = await seeded(store)
    lists = Lists()
    http = http_of(lists)
    assert await listprices.refresh(store, [c], http, NOW) == 2
    assert len(lists.asked) == 1  # one file per region, whatever the types
    prices = await store.list_prices()
    assert prices[KEY]["hour_price"] == 0.096 and prices[("aws", "us-east-1", "c5.xlarge")]
    assert ("aws", "us-east-1", "t3.micro") not in prices
    assert await listprices.refresh(store, [c], http, NOW + 23 * H) == 0
    assert len(lists.asked) == 1
    assert await listprices.refresh(store, [c], http, NOW + 25 * H) == 2
    assert len(lists.asked) == 2


@pytest.mark.anyio
async def test_a_failing_list_is_tried_again_after_an_hour_and_keeps_the_old_price(
    store: Store,
) -> None:
    c = await seeded(store, {"test": facts(M5)})
    lists = Lists()
    http = http_of(lists)
    await listprices.refresh(store, [c], http, NOW)
    lists.down = True
    assert await listprices.refresh(store, [c], http, NOW + 25 * H) == 1
    row = (await store.list_prices())[KEY]
    assert row["hour_price"] == 0.096 and row["ok_at"] == NOW and "503" in row["error"]
    assert await listprices.refresh(store, [c], http, NOW + 25 * H + 3000) == 0
    lists.down = False
    assert await listprices.refresh(store, [c], http, NOW + 26 * H + 1) == 1
    assert (await store.list_prices())[KEY]["error"] is None


@pytest.mark.anyio
async def test_a_type_missing_from_the_list_is_recorded(store: Store) -> None:
    odd = ("aws", "us-east-1", "x9.huge", 4.0, 16.0)
    c = await seeded(store, {"test": facts(M5, odd)})
    await listprices.refresh(store, [c], http_of(Lists()), NOW)
    row = (await store.list_prices())[("aws", "us-east-1", "x9.huge")]
    assert row["hour_price"] is None and row["error"] == "not in the price list"


@pytest.mark.anyio
async def test_a_cluster_with_a_problem_asks_for_nothing(store: Store) -> None:
    c = await seeded(store, {"test": facts(M5, problem="1 of 1 nodes lack")})
    lists = Lists()
    assert await listprices.refresh(store, [c], http_of(lists), NOW) == 0 and not lists.asked
    plain = prometheus_contract()  # no list_prices: nothing to look up
    assert await listprices.refresh(store, [plain], http_of(lists), NOW) == 0


@pytest.mark.anyio
async def test_gcp_needs_the_contracts_key_and_is_read_with_it(store: Store) -> None:
    gcp_nodes = {"test": facts(("gcp", "us-central1", "n2-standard-4", 4.0, 16.0))}
    c = await seeded(store, gcp_nodes)
    key = ("gcp", "us-central1", "n2-standard-4")
    await listprices.refresh(store, [c], http_of(Lists()), NOW)
    row = (await store.list_prices())[key]
    assert row["hour_price"] is None and "no GCP pricing key" in row["error"]

    us = ["us-central1"]
    page = {
        "skus": [
            sku("N2 Instance Core running in Americas", "0", 31611000, us),
            sku("N2 Instance Ram running in Americas", "0", 4237000, us),
        ]
    }
    asked = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request.headers["X-Goog-Api-Key"])
        return httpx.Response(200, json=page)

    data = make_contract().model_dump()
    data["integrations"].append({"name": "prometheus", "url": "http://prometheus"})
    data["list_prices"] = {"gcp_key_secret": "gcp"}
    keyed = contracts.build(data, secrets={"gcp": SecretStr("K3Y")})
    await listprices.refresh(store, [keyed], http_of(handler), NOW + 2 * H)
    assert asked == ["K3Y"]
    assert (await store.list_prices())[key]["hour_price"] == pytest.approx(
        4 * 0.031611 + 16 * 0.004237
    )


@pytest.mark.anyio
async def test_a_failed_lookups_error_never_carries_a_url_or_the_key(store: Store) -> None:
    gcp_nodes = {"test": facts(("gcp", "us-central1", "n2-standard-4", 4.0, 16.0))}
    await seeded(store, gcp_nodes)
    data = make_contract().model_dump()
    data["integrations"].append({"name": "prometheus", "url": "http://prometheus"})
    data["list_prices"] = {"gcp_key_secret": "gcp"}
    keyed = contracts.build(data, secrets={"gcp": SecretStr("S3CRET")})
    await listprices.refresh(store, [keyed], http_of(lambda r: httpx.Response(403)), NOW)
    error = (await store.list_prices())[("gcp", "us-central1", "n2-standard-4")]["error"]
    assert error == "HTTP 403 from cloudbilling.googleapis.com"
    err = KeyError("the key S3CRET was refused")
    assert "S3CRET" not in listprices.why(err, "S3CRET") and "***" in listprices.why(err, "S3CRET")


# --- the Contract as its prices stand -------------------------------------------------


@pytest.mark.anyio
async def test_a_priced_contract_is_in_usd_with_each_clusters_prices(store: Store) -> None:
    c = await seeded(store)
    await listprices.refresh(store, [c], http_of(Lists()), NOW)
    eff = await listprices.effective(store, c, NOW)
    assert eff.prices and eff.prices.currency == "USD"
    p = eff.price("test")
    assert p and p.cpu * 20 + p.memory * 64 == pytest.approx(0.916)
    assert eff._price_notes[0]["source"] == "list"
    assert c.prices is None and c._price_notes == []  # the loaded Contract is left alone
    assert await listprices.effective(store, eff, NOW) is eff  # resolved once


@pytest.mark.anyio
async def test_a_contract_without_list_prices_is_left_as_it_is(store: Store) -> None:
    c = prometheus_contract()
    assert await listprices.effective(store, c, NOW) is c


@pytest.mark.anyio
async def test_one_unpriced_cluster_leaves_the_contract_unpriced(store: Store) -> None:
    data = make_contract(clusters={"a": ["shop"], "b": ["shop"]}).model_dump()
    data["integrations"].append({"name": "prometheus", "url": "http://prometheus"})
    data["list_prices"] = {}
    c = contracts.build(data)
    await store.save_nodes("acme", "a", NOW, facts(M5))
    await store.save_nodes("acme", "b", NOW, facts(M5, problem="1 of 1 nodes lack"))
    await listprices.refresh(store, [c], http_of(Lists()), NOW)
    eff = await listprices.effective(store, c, NOW)
    assert eff.prices is None  # money for one cluster and cores for the other can't be ranked
    assert [n["source"] for n in eff._price_notes] == ["list", "none"]


# --- end to end: the hourly run, the cost, the CLI and the Hub --------------------------


def lab_nodes(fake: FakePrometheus) -> None:
    lab(fake)
    for i in range(3):
        node(fake, f"n{i}", "m5.large", cpu=2, gib=8, provider_id=f"aws:///us-east-1a/i-{i}")


def test_the_hourly_run_stores_the_nodes_and_the_cost_follows_the_list(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakePrometheus()
    lab_nodes(fake)
    monkeypatch.setattr(usage, "Prom", lambda t: _prom(fake, t))
    c = listed()

    async def go() -> tuple[dict[str, Any], dict[str, Any]]:
        async with connect(database_url) as pool:
            store = Store(pool)
            await store.migrate()
            assert await sizing.store_run(store, c, NOW) is not None
            assert len((await store.latest_nodes("acme"))["test"]["nodes"]) == 3
            before = await sizing.cost(store, c, "namespace", NOW)
            await listprices.refresh(store, [c], http_of(Lists()), NOW)
            return before, await sizing.cost(store, c, "namespace", NOW)

    before, out = asyncio.run(go())
    assert before["currency"] is None and "no list price yet" in before["prices"][0]["note"]
    # 3 m5.large: 0.288 USD/h over 6 vCPU and 24 GiB, 1 vCPU = 7 GiB
    memory = 0.288 / (7 * 6 + 24)
    assert out["currency"] == "USD" and out["prices"][0]["source"] == "list"
    asked = out["total"]["requests"]
    assert out["total"]["cost"]["requests"] == pytest.approx(
        (asked["cpu"] * 7 * memory + asked["memory"] / GI * memory) * sizing.MONTH
    )


def test_the_digest_is_in_money_from_the_list(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakePrometheus()
    lab_nodes(fake)
    monkeypatch.setattr(usage, "Prom", lambda t: _prom(fake, t))
    c = listed()

    async def go() -> str:
        async with connect(database_url) as pool:
            store = Store(pool)
            await store.migrate()
            await sizing.store_run(store, c, NOW)
            await listprices.refresh(store, [c], http_of(Lists()), NOW)
            return await sizing.digest(store, c, NOW + H, None)

    text = asyncio.run(go())
    assert "USD/month" in text


def test_the_cli_shows_where_a_price_comes_from(
    database_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakePrometheus()
    lab_nodes(fake)
    monkeypatch.setattr(usage, "Prom", lambda t: _prom(fake, t))
    c = listed()
    monkeypatch.setattr(cli, "contract", lambda settings, cid: c)
    monkeypatch.setenv("SQUERO_DATABASE_URL", database_url)
    monkeypatch.setattr(listprices, "client", lambda: http_of(Lists()))
    assert cli.main(["sizing", "prices", "--contract", "acme"]) == 1
    assert cli.main(["sizing", "refresh", "--contract", "acme"]) == 0
    capsys.readouterr()
    assert cli.main(["sizing", "prices", "--contract", "acme"]) == 1  # not looked up yet
    assert "no list price yet" in capsys.readouterr().out
    assert cli.main(["sizing", "prices", "--contract", "acme", "--refresh"]) == 0
    out = capsys.readouterr().out
    assert "looked up 1 list prices" in out and "3 x aws m5.large in us-east-1" in out
    assert "per vCPU-hour" in out
    assert cli.main(["sizing", "cost", "--contract", "acme"]) == 0
    assert "prices test: list price of 3 x aws m5.large" in capsys.readouterr().out
    assert cli.main(["sizing", "waste", "--contract", "acme"]) == 0
    assert "prices test: list price" in capsys.readouterr().out
    monkeypatch.setattr(cli, "contract", lambda settings, cid: prometheus_contract())
    assert cli.main(["sizing", "prices", "--contract", "acme"]) == 0
    assert "no prices" in capsys.readouterr().out


def test_the_hub_looks_the_prices_up_with_its_hourly_run(
    settings: Settings, fake_cluster: Any, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.database_url = SecretStr(database_url)
    fake = FakePrometheus()
    lab_nodes(fake)
    monkeypatch.setattr(usage, "Prom", lambda t: _prom(fake, t))
    lists = Lists()
    integrations = [{"name": "kubernetes"}, {"name": "prometheus", "url": "http://prometheus"}]
    acme = {"integrations": integrations, "list_prices": {}}
    with client(settings, fake_cluster, acme=acme, price_http=http_of(lists)) as c:
        assert c.portal is not None

        def tick(at: float) -> list[str]:
            done: list[str] = c.portal.call(c.app.state.due_sizings, at)  # type: ignore[union-attr,attr-defined]
            return done

        assert tick(NOW) == ["acme"] and len(lists.asked) == 1  # nodes read, then looked up
        assert tick(NOW + H) == ["acme"] and len(lists.asked) == 1  # once a day
        lists.down = True
        assert tick(NOW + 25 * H) == ["acme"]  # a list that is down never stops the runs
        assert len(lists.asked) == 2


# --- currency (slice 10.5b) ------------------------------------------------------------

AZ = ("azure", "centralus", "Standard_B1s", 1.0, 1.0)


def brl(**cluster: Any) -> contracts.Contract:
    data = listed().model_dump()
    data["list_prices"]["currency"] = "BRL"
    data["clusters"][0].update({"provider": "azure", **cluster})
    return contracts.build(data)


def test_azure_asks_for_the_currency_and_usd_is_the_default() -> None:
    asked = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.params["currencyCode"])
        return httpx.Response(200, json={"Items": [azure_item("Standard_B1s", 0.0652)]})

    assert listprices.azure(http_of(handler), "centralus", {"Standard_B1s": (1, 1)}, "BRL")[0] == {
        "Standard_B1s": 0.0652
    }
    listprices.azure(http_of(handler), "centralus", {"Standard_B1s": (1, 1)})
    assert asked == ["BRL", "USD"]


@pytest.mark.parametrize("code", ["brl", "BR", "REAL", "R$"])
def test_a_currency_is_three_capital_letters(code: str) -> None:
    with pytest.raises(ValidationError, match="currency"):
        contracts.ListPrices(currency=code)


def test_a_currency_other_than_usd_needs_every_cluster_on_azure_or_gcp() -> None:
    assert brl().list_prices.currency == "BRL"  # type: ignore[union-attr]
    data = brl().model_dump()
    data["clusters"][0]["provider"] = "gcp"
    assert contracts.build(data).list_prices.currency == "BRL"  # type: ignore[union-attr]
    for provider in (None, "aws"):
        data["clusters"][0]["provider"] = provider
        with pytest.raises(ValidationError, match="only Azure's and GCP's"):
            contracts.build(data)


def test_gcp_asks_for_the_currency_and_never_prices_in_another() -> None:
    asked = []
    sku = {
        "description": "E2 Instance Core running in Americas",
        "category": {"usageType": "OnDemand"},
        "serviceRegions": ["us-central1"],
    }

    def answer(code: str) -> Any:
        def handler(request: httpx.Request) -> httpx.Response:
            asked.append(request.url.params["currencyCode"])
            rate = {"unitPrice": {"currencyCode": code, "units": "0", "nanos": 100_000_000}}
            ram = {**sku, "description": "E2 Instance Ram running in Americas"}
            skus = [sku, ram]
            for s in skus:
                s["pricingInfo"] = [{"pricingExpression": {"tieredRates": [rate]}}]
            return httpx.Response(200, json={"skus": skus})

        return handler

    wanted = {"e2-standard-2": (2.0, 8.0)}
    got, _ = listprices.gcp(http_of(answer("BRL")), "us-central1", wanted, "KEY", "BRL")
    assert got == {"e2-standard-2": pytest.approx(1.0)} and asked == ["BRL"]
    with pytest.raises(ValueError, match="answered in USD, not BRL"):
        listprices.gcp(http_of(answer("USD")), "us-central1", wanted, "KEY", "BRL")
    listprices.gcp(http_of(answer("USD")), "us-central1", wanted, "KEY")
    assert asked[-1] == "USD"
    listprices.fetch(http_of(answer("BRL")), "gcp", "us-central1", wanted, "KEY", "BRL")
    assert asked[-1] == "BRL"  # the Contract's currency reaches the catalog


@pytest.mark.anyio
async def test_a_brl_contract_is_priced_from_the_brl_lookup(store: Store) -> None:
    c = brl()
    await store.save_nodes(c.id, "test", NOW, facts(AZ))
    asked = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.params["currencyCode"])
        return httpx.Response(200, json={"Items": [azure_item("Standard_B1s", 0.0652)]})

    assert await listprices.refresh(store, [c], http_of(handler), NOW) == 1
    assert asked == ["BRL"]
    eff = await listprices.effective(store, c, NOW)
    assert eff.prices and eff.prices.currency == "BRL"
    p = eff.price("test")
    assert p and p.cpu * 1 + p.memory * 1 == pytest.approx(0.0652)


@pytest.mark.anyio
async def test_a_price_is_kept_per_currency(store: Store) -> None:
    key = ("azure", "centralus", "Standard_B1s")
    await store.put_list_price(key, 0.0125, None, NOW)
    await store.put_list_price(key, 0.0652, None, NOW, "BRL")
    assert (await store.list_prices())[key]["hour_price"] == 0.0125
    assert (await store.list_prices("BRL"))[key]["hour_price"] == 0.0652
    assert await store.list_prices("EUR") == {}


@pytest.mark.anyio
async def test_a_row_without_a_currency_is_usd(store: Store) -> None:
    """What the migration does to the rows already there."""
    async with store.pool.connection() as conn:
        await conn.execute(
            "insert into list_prices (provider, region, instance_type, hour_price, ok_at, "
            "tried_at) values ('aws', 'r', 't', 0.5, 1, 1)"
        )
    assert (await store.list_prices())[("aws", "r", "t")]["hour_price"] == 0.5
    assert await store.list_prices("BRL") == {}
