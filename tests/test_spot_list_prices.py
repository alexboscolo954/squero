"""Spot list prices (slice 16.4a): Azure's Spot meters and GCP's Spot SKUs from the reads
on-demand prices already make, shared; AWS's spot price history, read with each
Contract's own key and kept for it alone; a spot node at its spot list price where there
is one, else at the cluster's discount, node by node."""

from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import SecretStr

from squero import contracts, listprices
from squero.store import Store
from tests.test_billing import SECRET
from tests.test_listprices import M5, ROWS, http_of, price
from tests.test_sizing_store import NOW, store  # noqa: F401
from tests.test_spot import spotted

SPOT = {"label": "karpenter.sh/capacity-type=spot", "discount": 0.35}
KEY_A, KEY_B = "AKIAIOSFODNN7EXAMPLE", "AKIAI44QH8DHBEXAMPLE"


def on_aws(cid: str = "acme", key: str = KEY_A, deal: float = 1.0, **cl: Any) -> contracts.Contract:
    from tests.conftest import make_contract

    data = make_contract(contract_id=cid).model_dump()
    data["integrations"] += [
        {"name": "prometheus", "url": "http://prometheus"},
        {"name": "aws", "regions": ["us-east-1"], "access_key_id": key}
        | {"secret_access_key_secret": "aws-key"},
    ]
    data["list_prices"] = {"discount": deal}
    data["clusters"][0] |= {"spot": SPOT} | cl
    return contracts.build(data, secrets={"aws-key": SecretStr(SECRET)})


# --- the reads --------------------------------------------------------------------------


def history(*items: tuple[str, str, float], token: str = "") -> str:
    """DescribeSpotPriceHistory's reply, as its API reference shows it."""
    rows = "".join(
        f"<item><instanceType>{t}</instanceType><productDescription>Linux/UNIX</productDescription>"
        f"<spotPrice>{p}</spotPrice><timestamp>2026-11-15T20:56:05.000Z</timestamp>"
        f"<availabilityZone>{z}</availabilityZone></item>"
        for t, z, p in items
    )
    return (
        '<DescribeSpotPriceHistoryResponse xmlns="http://ec2.amazonaws.com/doc/2016-11-15/">'
        f"<requestId>r-1</requestId><spotPriceHistorySet>{rows}</spotPriceHistorySet>"
        f"<nextToken>{token}</nextToken></DescribeSpotPriceHistoryResponse>"
    )


def test_aws_spot_prices_are_the_regions_zones_averaged_over_the_last_day() -> None:
    seen: list[httpx.Request] = []
    pages = [
        history(
            ("m5.large", "us-east-1a", 0.03),
            ("m5.large", "us-east-1a", 0.05),
            ("m5.large", "us-east-1a", 0.04),
            token="t2",
        ),
        history(("m5.large", "us-east-1b", 0.02), ("m5.large", "us-east-1a", 0.04)),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=pages[len(seen) - 1])

    got = listprices.aws_spot(http_of(handler), "us-east-1", ["m5.large", "c5.xlarge"],
                              KEY_A, SECRET, NOW)  # fmt: skip
    # zone a: 0.03, 0.05, 0.04, 0.04 -> 0.04; zone b: 0.02; across the zones: 0.03
    assert got == {"m5.large": pytest.approx(0.03), "c5.xlarge": None}
    first, second = (parse_qs(r.content.decode()) for r in seen)
    assert seen[0].url == "https://ec2.us-east-1.amazonaws.com/"
    assert first["Action"] == ["DescribeSpotPriceHistory"] and first["Version"] == ["2016-11-15"]
    assert first["ProductDescription.1"] == ["Linux/UNIX"]
    assert first["InstanceType.1"] == ["m5.large"] and first["InstanceType.2"] == ["c5.xlarge"]
    assert first["StartTime"] == ["1972-09-26T00:00:00.000Z"]  # a day before NOW
    assert "NextToken" not in first and second["NextToken"] == ["t2"]
    auth = seen[0].headers["authorization"]
    assert f"Credential={KEY_A}/" in auth and "/us-east-1/ec2/aws4_request" in auth
    assert SECRET not in str(seen[0].headers) + seen[0].content.decode()


def test_azure_and_gcp_spot_prices_come_with_their_on_demand_read() -> None:
    def sku(desc: str, nanos: int, usage: str = "Preemptible") -> dict[str, Any]:
        rate = {"unitPrice": {"units": "0", "nanos": nanos}}
        return {"description": desc, "category": {"usageType": usage},
                "serviceRegions": ["us-central1"],
                "pricingInfo": [{"pricingExpression": {"tieredRates": [rate]}}]}  # fmt: skip

    on_demand = [sku("N2 Instance Core running in Americas", 30_000_000, "OnDemand"),
                 sku("N2 Instance Ram running in Americas", 4_000_000, "OnDemand")]  # fmt: skip
    legacy = [sku("Preemptible N2 Instance Core running in Americas", 9_000_000),
              sku("Preemptible N2 Instance Ram running in Americas", 1_000_000)]  # fmt: skip
    named = [sku("Spot Preemptible N2 Instance Core running in Americas", 7_000_000),
             sku("Spot Preemptible N2 Instance Ram running in Americas", 900_000)]  # fmt: skip
    wanted = {"n2-standard-2": (2.0, 8.0)}
    for skus in (on_demand + legacy + named, on_demand + named + legacy):  # a Spot SKU wins
        answer = http_of(lambda r, skus=skus: httpx.Response(200, json={"skus": skus}))
        got, spot = listprices.gcp(answer, "us-central1", wanted, "KEY")
        assert got["n2-standard-2"] == pytest.approx(2 * 0.03 + 8 * 0.004)
        assert spot["n2-standard-2"] == pytest.approx(2 * 0.007 + 8 * 0.0009)
    alone = http_of(lambda r: httpx.Response(200, json={"skus": on_demand + legacy}))
    assert listprices.gcp(alone, "us-central1", wanted, "KEY")[1]["n2-standard-2"] == (
        pytest.approx(2 * 0.009 + 8 * 0.001)
    )  # only a legacy one: used


# --- refresh ----------------------------------------------------------------------------


@pytest.mark.anyio
async def test_each_contract_reads_its_aws_spot_prices_with_its_own_key(
    store: Store,  # noqa: F811
) -> None:
    """Two Contracts, the same region and type: B's key refused. B falls back, and no
    request for B carries A's key, nor A's row reaches B."""
    a, b = on_aws("acme", KEY_A), on_aws("bank", KEY_B)
    c5 = ("aws", "us-east-1", "c5.xlarge", 4.0, 8.0)
    for c in (a, b):
        await store.save_nodes(c.id, "test", NOW, spotted((M5, True), (c5, False)))
        await store.put_list_price(M5[:3], 0.096, None, NOW)
        await store.put_list_price(c5[:3], 0.17, None, NOW)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host != "ec2.us-east-1.amazonaws.com":
            return httpx.Response(500)
        if KEY_B in request.headers["authorization"]:
            return httpx.Response(403, text="<Response><Errors><Error><Code>UnauthorizedOperation"
                                            "</Code></Error></Errors></Response>")  # fmt: skip
        return httpx.Response(200, text=history(("m5.large", "us-east-1a", 0.03)))

    await listprices.refresh(store, [a, b], http_of(handler), NOW)
    ec2 = [r for r in seen if r.url.host.startswith("ec2.")]
    assert len(ec2) == 2 and sum(KEY_A in r.headers["authorization"] for r in ec2) == 1
    asked = parse_qs(ec2[0].content.decode())
    assert asked["InstanceType.1"] == ["m5.large"] and "InstanceType.2" not in asked  # spot ones
    mine, theirs = await store.spot_prices("USD", "acme"), await store.spot_prices("USD", "bank")
    assert mine[M5[:3]]["hour_price"] == 0.03 and mine[M5[:3]]["contract_id"] == "acme"
    assert theirs[M5[:3]]["hour_price"] is None
    assert theirs[M5[:3]]["error"] == "HTTP 403 from ec2.us-east-1.amazonaws.com"
    assert SECRET not in str(theirs)
    priced = await listprices.effective(store, b, NOW)
    assert "x0.35 on 1 of 2 nodes" in priced._price_notes[0]["note"]  # its discount
    assert "HTTP 403" in priced._price_notes[0]["note"]
    seen.clear()
    await listprices.refresh(store, [a, b], http_of(handler), NOW + 600)  # fresh, or waiting
    assert not [r for r in seen if r.url.host.startswith("ec2.")]


@pytest.mark.anyio
async def test_no_aws_spot_read_where_it_isnt_needed(store: Store) -> None:  # noqa: F811
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=history())

    typed = on_aws("typed", prices={"cpu": 0.04, "memory": 0.005})
    plain = on_aws("plain", spot=None)
    unread = on_aws("unread")
    elsewhere = on_aws("far")
    await store.save_nodes("typed", "test", NOW, spotted((M5, True)))
    await store.save_nodes("plain", "test", NOW, spotted((M5, True)))
    await store.save_nodes("unread", "test", NOW, spotted((M5, True), label=None))
    far = ("aws", "eu-west-1", "m5.large", 2.0, 8.0)
    await store.save_nodes("far", "test", NOW, spotted((far, True)))
    for c in (typed, plain, unread):
        assert await listprices._aws_spot(store, c, http_of(handler), NOW) == 0
    assert await listprices._aws_spot(store, elsewhere, http_of(handler), NOW) == 1
    assert seen == []  # eu-west-1 isn't among its integration's regions: never asked
    row = (await store.spot_prices("USD", "far"))[far[:3]]
    assert row["error"] == "ValueError: eu-west-1 is not among the aws integration's regions"


@pytest.mark.anyio
async def test_azure_spot_prices_are_shared_and_never_a_contracts_own(store: Store) -> None:  # noqa: F811
    await store.put_spot_price(("azure", "eastus", "D2"), 0.02, None, NOW)
    await store.put_spot_price(("aws", "us-east-1", "m5.large"), 0.03, None, NOW, "USD", "acme")
    assert set(await store.spot_prices("USD", "bank")) == {("azure", "eastus", "D2")}
    assert set(await store.spot_prices("USD", "acme")) == {
        ("azure", "eastus", "D2"),
        ("aws", "us-east-1", "m5.large"),
    }
    assert await store.list_prices("USD") == {}  # never an on-demand row


# --- the price --------------------------------------------------------------------------


def spot_row(hour: float | None, error: str | None = None) -> dict[str, Any]:
    return (
        price(hour) | {"error": error}
        if hour
        else {"hour_price": None, "ok_at": None, "error": error}
    )


def test_a_spot_node_costs_its_spot_list_price_and_no_deal() -> None:
    c = on_aws(deal=0.6)
    nodes = {"test": spotted((M5, True), (M5, True), (M5, False))}
    shares: dict[str, Any] = {}
    spots = {M5[:3]: spot_row(0.03)}
    prices, notes = listprices.resolve(c, nodes, ROWS, NOW, shares, spots)
    paid = 0.03 + 0.03 + 0.096 * 0.6
    assert prices["test"].cpu * 6 + prices["test"].memory * 24 == pytest.approx(paid)
    assert notes[0]["note"].endswith(", spot list on 2 of 3 nodes")
    assert shares["test"] == (
        pytest.approx(paid / (3 * 0.096 * 0.6)),
        pytest.approx(0.03 / (0.096 * 0.6)),
    )


def test_a_spot_node_without_its_spot_list_price_costs_the_discount() -> None:
    c = on_aws()
    nodes = {"test": spotted((M5, True), (("aws", "us-east-1", "c5.xlarge", 4.0, 8.0), True))}
    spots = {
        M5[:3]: spot_row(0.03),
        ("aws", "us-east-1", "c5.xlarge"): spot_row(None, "HTTP 403 from ec2"),
    }
    prices, notes = listprices.resolve(c, nodes, ROWS, NOW, {}, spots)
    assert prices["test"].cpu * 6 + prices["test"].memory * 16 == pytest.approx(0.03 + 0.17 * 0.35)
    assert notes[0]["note"].endswith(", spot list on 1 of 2 nodes, x0.35 on 1 (HTTP 403 from ec2)")
    none = listprices.resolve(c, nodes, ROWS, NOW, {}, {})[1][0]["note"]
    assert none.endswith(", spot x0.35 on 2 of 2 nodes")  # nothing read yet: as before
    refused = {M5[:3]: spot_row(None, "HTTP 403 from ec2")}
    failed = listprices.resolve(c, {"test": spotted((M5, True))}, ROWS, NOW, {}, refused)
    failed = failed[1][0]["note"]
    assert failed.endswith(", spot x0.35 on 1 of 1 nodes (HTTP 403 from ec2)")


def test_a_typed_cluster_keeps_its_discount() -> None:
    c = on_aws(prices={"cpu": 0.04, "memory": 0.005})
    nodes = {"test": spotted((M5, True), (M5, False))}
    prices, notes = listprices.resolve(c, nodes, ROWS, NOW, {}, {M5[:3]: spot_row(0.001)})
    assert prices["test"].cpu == pytest.approx(0.04 * 0.675)
    assert "spot list" not in notes[0]["note"]


@pytest.mark.anyio
async def test_an_azure_read_stores_its_spot_prices_shared_and_a_missing_one_is_read(
    store: Store,  # noqa: F811
) -> None:
    from tests.test_listprices import azure_item, listed

    c = listed(spot=SPOT)
    d2 = ("azure", "eastus", "Standard_D2s_v3", 2.0, 8.0)
    await store.save_nodes(c.id, "test", NOW, spotted((d2, True), (d2, False)))
    items = [
        azure_item("Standard_D2s_v3", 0.096),
        azure_item("Standard_D2s_v3", 0.02, skuName="D2s v3 Spot"),
    ]
    reads: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        reads.append(request)
        return httpx.Response(200, json={"Items": items})

    await store.put_list_price(d2[:3], 0.096, None, NOW)  # fresh on demand: an older Hub's
    assert await listprices.refresh(store, [c], http_of(handler), NOW) == 1  # its spot isn't
    shared = await store.spot_prices("USD", "anyone")
    assert shared[d2[:3]]["hour_price"] == 0.02 and shared[d2[:3]]["contract_id"] == ""
    assert await listprices.refresh(store, [c], http_of(handler), NOW + 60) == 0  # both fresh
    assert len(reads) == 1


@pytest.mark.anyio
async def test_a_size_with_no_spot_price_is_read_once_a_day(store: Store) -> None:  # noqa: F811
    from tests.test_listprices import azure_item, listed

    c = listed(spot=SPOT)
    b1 = ("azure", "eastus", "Standard_B1s", 1.0, 1.0)
    await store.save_nodes(c.id, "test", NOW, spotted((b1, True)))
    reads: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        reads.append(request)
        return httpx.Response(200, json={"Items": [azure_item("Standard_B1s", 0.01)]})  # no Spot

    assert await listprices.refresh(store, [c], http_of(handler), NOW) == 1
    assert (await store.spot_prices("USD", ""))[b1[:3]]["error"] == "no spot price in the list"
    assert await listprices.refresh(store, [c], http_of(handler), NOW + 2 * 3600) == 0
    assert await listprices.refresh(store, [c], http_of(handler), NOW + 86400 + 1) == 1


@pytest.mark.anyio
@pytest.mark.parametrize(("status", "again"), [(403, False), (401, False), (500, True)])
async def test_a_refused_aws_spot_read_waits_a_day(
    store: Store,  # noqa: F811
    status: int,
    again: bool,
) -> None:
    """The Operator's choice: a denied call a day in the client's audit trail, not 24."""
    c = on_aws()
    await store.save_nodes(c.id, "test", NOW, spotted((M5, True)))
    http = http_of(lambda r: httpx.Response(status))
    assert await listprices._aws_spot(store, c, http, NOW) == 1
    assert await listprices._aws_spot(store, c, http, NOW + 2 * 3600) == (1 if again else 0)
    assert await listprices._aws_spot(store, c, http, NOW + 86400 + 1) == 1


@pytest.mark.anyio
async def test_a_read_with_a_problem_asks_aws_nothing(store: Store) -> None:  # noqa: F811
    c = on_aws()
    broken = {"nodes": [{"node": "a", "spot": True, "provider": "aws", "region": None,
                         "type": None, "vcpu": 2.0, "gib": 8.0}],
              "problem": "no instance type label", "spot": SPOT["label"]}  # fmt: skip
    await store.save_nodes(c.id, "test", NOW, broken)
    assert await listprices._aws_spot(store, c, http_of(lambda r: httpx.Response(500)), NOW) == 0
