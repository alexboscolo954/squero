"""AWS bills (slice 10.5a): a billed cluster's tagged EC2 compute, read from Cost Explorer
a day at a time, set beside what its node-hours cost at the Hub's prices. The estimate
is node capacity-hours times the cluster's CPU and GiB prices; the gap is its distance
from the bill as a fraction of the bill, within 5% or flagged."""

import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from squero import billing, contracts, usage
from squero.config import Settings
from squero.store import Store, connect
from tests.conftest import make_contract
from tests.fake_prometheus import FakePrometheus
from tests.test_api import client
from tests.test_sizing_store import NOW, H
from tests.test_usage import _prom

GI = 2**30
TAG = "squero-cluster=kind"
DAYS = [
    "1972-09-19",
    "1972-09-20",
    "1972-09-21",
    "1972-09-22",
    "1972-09-23",
    "1972-09-24",
    "1972-09-25",
]
AWS = {
    "name": "aws",
    "regions": ["us-east-1"],
    "access_key_id": "AKIAIOSFODNN7EXAMPLE",
    "secret_access_key_secret": "aws-key",
}
SECRET = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
# 2 vCPU and 8 GiB for the 7 days (168 h): 336 vCPU-hours and 1344 GiB-hours, which at
# 0.03 and 0.004 per hour cost 10.08 + 5.376 = 15.456
ESTIMATE = 336 * 0.03 + 1344 * 0.004


def billed(contract_id: str = "acme", **cluster: Any) -> contracts.Contract:
    data = make_contract(contract_id=contract_id).model_dump()
    data["integrations"] += [{"name": "prometheus", "url": "http://prometheus"}, AWS]
    data["prices"] = {"currency": "USD", "cpu": 0.03, "memory": 0.004}
    data["clusters"][0]["billing"] = {"tag": TAG}
    data["clusters"][0].update(cluster)
    return contracts.build(data, secrets={"aws-key": SecretStr(SECRET)})


def nodes(fake: FakePrometheus, hours: int = 168) -> None:
    fake.add("kube_node_status_capacity", [2.0] * hours, node="n1", resource="cpu")
    fake.add("kube_node_status_capacity", [8.0 * GI] * hours, node="n1", resource="memory")


def prom_of(fake: FakePrometheus) -> Any:
    return lambda t: _prom(fake, t)


def body(amounts: dict[str, float], unit: str = "USD", estimated: bool = False) -> dict[str, Any]:
    return {
        "ResultsByTime": [
            {
                "TimePeriod": {"Start": d, "End": d},
                "Total": {"UnblendedCost": {"Amount": str(a), "Unit": unit}},
                "Estimated": estimated,
            }
            for d, a in amounts.items()
        ]
    }


class CostExplorer:
    """Cost Explorer, counting what was asked."""

    def __init__(self, amounts: dict[str, float] | None = None) -> None:
        self.asked: list[httpx.Request] = []
        self.amounts = amounts if amounts is not None else dict.fromkeys(DAYS, 2.2)
        self.status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.asked.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"__type": "x", "message": SECRET})
        return httpx.Response(200, json=body(self.amounts))

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))


@pytest.fixture
async def store(database_url: str) -> Any:
    async with connect(database_url) as pool:
        s = Store(pool)
        await s.migrate()
        yield s


def test_the_period_is_full_utc_days_ending_two_days_ago() -> None:
    assert billing.period(NOW, 7) == DAYS
    assert billing.period(NOW + 86_399, 1) == ["1972-09-25"]  # still the same UTC day
    assert billing.period(NOW + 86_400, 1) == ["1972-09-26"]


def test_the_request_asks_for_the_tags_ec2_compute_usage_before_credits() -> None:
    sent = billing.request(TAG, "1972-09-01", "1972-09-26")
    assert sent["TimePeriod"] == {"Start": "1972-09-01", "End": "1972-09-26"}
    assert sent["Granularity"] == "DAILY" and sent["Metrics"] == ["UnblendedCost"]
    assert sent["Filter"]["And"] == [
        {"Tags": {"Key": "squero-cluster", "Values": ["kind"], "MatchOptions": ["EQUALS"]}},
        {"Dimensions": {"Key": "SERVICE", "Values": ["Amazon Elastic Compute Cloud - Compute"]}},
        {"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Usage"]}},
    ]
    assert billing.request(TAG, "a", "b", "next")["NextPageToken"] == "next"


def test_a_tag_value_may_hold_an_equals_sign() -> None:
    assert billing.request("k=a=b", "a", "b")["Filter"]["And"][0]["Tags"]["Values"] == ["a=b"]


def test_the_days_are_parsed_with_their_unit_and_whether_aws_may_still_change_them() -> None:
    assert billing.parse(body({"1972-09-25": 2.5}, "EUR", estimated=True)) == {
        "1972-09-25": (2.5, "EUR", True)
    }


def test_the_request_is_signed_and_the_secret_never_sent() -> None:
    ce = CostExplorer()
    got = billing.aws_fetch(
        ce.client(), "AKIAIOSFODNN7EXAMPLE", SECRET, TAG, DAYS[0], "1972-09-26", NOW
    )
    assert got[DAYS[0]] == (2.2, "USD", False)
    (sent,) = ce.asked
    assert sent.url.host == "ce.us-east-1.amazonaws.com"
    assert sent.headers["x-amz-target"] == "AWSInsightsIndexService.GetCostAndUsage"
    assert sent.headers["authorization"].startswith(
        "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/19720927/us-east-1/ce/aws4_request"
    )
    assert SECRET not in str(sent.headers) + sent.content.decode()


def test_every_page_is_read() -> None:
    pages = [
        {**body({DAYS[0]: 1.0}), "NextPageToken": "t2"},
        body({DAYS[1]: 2.0}),
    ]
    tokens = []

    def handler(request: httpx.Request) -> httpx.Response:
        tokens.append(json.loads(request.content).get("NextPageToken"))
        return httpx.Response(200, json=pages[len(tokens) - 1])

    http = httpx.Client(transport=httpx.MockTransport(handler))
    got = billing.aws_fetch(http, "AKIAIOSFODNN7EXAMPLE", SECRET, TAG, DAYS[0], DAYS[2], NOW)
    assert tokens == [None, "t2"] and set(got) == {DAYS[0], DAYS[1]}


def test_the_gap_is_a_fraction_of_the_bill() -> None:
    assert billing.gap(105, 100) == pytest.approx(0.05)
    assert billing.gap(90, 100) == pytest.approx(-0.10)
    assert billing.gap(0, 0) == 0.0
    assert billing.gap(1, 0) is None  # no fraction of nothing


async def seed(
    store: Store, c: contracts.Contract, amounts: dict[str, float], unit: str = "USD"
) -> None:
    await store.save_bill(c.id, "test", {d: (a, unit, False) for d, a in amounts.items()}, NOW)


async def row(
    store: Store, c: contracts.Contract, fake: FakePrometheus, days: int = 7
) -> dict[str, Any]:
    (got,) = await billing.report(store, c, NOW, days, prom_of(fake))
    return got


@pytest.mark.anyio
async def test_a_bill_within_five_percent_reconciles(store: Store) -> None:
    c, fake = billed(), FakePrometheus()
    nodes(fake)
    await seed(store, c, dict.fromkeys(DAYS, 2.2))  # 15.40 billed
    got = await row(store, c, fake)
    assert got["billed"] == pytest.approx(15.4) and got["estimated"] == pytest.approx(ESTIMATE)
    assert got["gap"] == pytest.approx((ESTIMATE - 15.4) / 15.4)  # +0.36%
    assert got["within"] is True and got["note"] is None
    assert (got["start"], got["end"], got["currency"]) == (DAYS[0], DAYS[-1], "USD")


@pytest.mark.anyio
async def test_a_bill_further_away_is_flagged(store: Store) -> None:
    c, fake = billed(), FakePrometheus()
    nodes(fake)
    await seed(store, c, dict.fromkeys(DAYS, 2.0))  # 14.00 billed
    got = await row(store, c, fake)
    assert got["gap"] == pytest.approx((ESTIMATE - 14.0) / 14.0)  # +10.4%
    assert got["within"] is False


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("gap", "within"), [(0.049, True), (0.051, False), (-0.049, True), (-0.051, False)]
)
async def test_five_percent_is_the_edge_either_way(store: Store, gap: float, within: bool) -> None:
    c, fake = billed(), FakePrometheus()
    nodes(fake)
    total = ESTIMATE / (1 + gap)
    await seed(store, c, dict.fromkeys(DAYS, total / 7))
    got = await row(store, c, fake)
    assert got["gap"] == pytest.approx(gap) and got["within"] is within


@pytest.mark.anyio
async def test_a_day_without_a_bill_says_so_and_has_no_gap(store: Store) -> None:
    c, fake = billed(), FakePrometheus()
    nodes(fake)
    await seed(store, c, dict.fromkeys(DAYS[:5], 2.2))  # the 24th and 25th not settled yet
    got = await row(store, c, fake)
    assert got["gap"] is None and got["billed"] is None and got["within"] is None
    assert "no bill for 2 of 7 days yet (first 1972-09-24)" in got["note"]
    assert fake.queries == []  # nothing to compare: Prometheus not asked


@pytest.mark.anyio
async def test_before_the_first_read_the_note_says_the_next_run_reads_it(store: Store) -> None:
    got = await row(store, billed(), FakePrometheus())
    assert "not read yet" in got["note"]


@pytest.mark.anyio
async def test_a_failed_read_is_named_and_the_stored_days_stay(store: Store) -> None:
    c = billed()
    await seed(store, c, dict.fromkeys(DAYS[2:], 2.2))
    await store.fail_bill(c.id, "test", "HTTP 403 from ce.us-east-1.amazonaws.com", NOW)
    got = await row(store, c, FakePrometheus())
    assert got["note"] == "the bill failed to read: HTTP 403 from ce.us-east-1.amazonaws.com"
    days, tries = await store.bills(c.id)
    assert len(days["test"]) == 5 and tries["test"]["ok_at"] == NOW  # the last good read kept


@pytest.mark.anyio
async def test_a_bill_in_another_currency_is_shown_without_a_gap(store: Store) -> None:
    c, fake = billed(), FakePrometheus()
    nodes(fake)
    await seed(store, c, dict.fromkeys(DAYS, 2.2), unit="EUR")
    got = await row(store, c, fake)
    assert got["billed"] == pytest.approx(15.4) and got["currency"] == "EUR"
    assert got["gap"] is None and got["estimated"] is None
    assert "bill is in EUR and the prices in USD: not converted" in got["note"]


@pytest.mark.anyio
async def test_a_cluster_without_a_price_has_no_estimate(store: Store) -> None:
    data = billed().model_dump()
    data["prices"] = None
    c = contracts.build(data, secrets={"aws-key": SecretStr(SECRET)})
    await seed(store, c, dict.fromkeys(DAYS, 2.2))
    got = await row(store, c, FakePrometheus())
    assert got["billed"] == pytest.approx(15.4) and got["estimated"] is None
    assert "no price for this cluster" in got["note"]


@pytest.mark.anyio
async def test_prometheus_down_leaves_the_bill_and_says_why(store: Store) -> None:
    c, fake = billed(), FakePrometheus()
    fake.status = 500
    await seed(store, c, dict.fromkeys(DAYS, 2.2))
    got = await row(store, c, fake)
    assert got["billed"] == pytest.approx(15.4) and got["estimated"] is None
    assert got["note"].startswith("the node-hours failed to read: Prometheus error 500")


@pytest.mark.anyio
async def test_fewer_days_ask_for_fewer_node_hours(store: Store) -> None:
    c, fake = billed(), FakePrometheus()
    nodes(fake, hours=24)
    await seed(store, c, dict.fromkeys(DAYS, 2.2))
    got = await row(store, c, fake, days=1)
    assert got["start"] == got["end"] == DAYS[-1] and got["billed"] == pytest.approx(2.2)
    assert got["estimated"] == pytest.approx(48 * 0.03 + 192 * 0.004)  # 2.208


@pytest.mark.anyio
async def test_the_bill_is_read_daily_and_a_failure_is_tried_again_after_an_hour(
    store: Store,
) -> None:
    c, ce = billed(), CostExplorer()
    http = ce.client()
    assert await billing.refresh(store, [c], http, NOW) == 1
    assert await billing.refresh(store, [c], http, NOW + 23 * H) == 0
    assert await billing.refresh(store, [c], http, NOW + 25 * H) == 1
    assert await billing.refresh(store, [c], http, NOW + 26 * H) == 0  # a re-read counts too
    ce.status = 403
    assert await billing.refresh(store, [c], http, NOW + 50 * H) == 1  # a day on: fails
    assert await billing.refresh(store, [c], http, NOW + 50.5 * H) == 0  # waits an hour
    assert await billing.refresh(store, [c], http, NOW + 51.5 * H) == 1
    days, tries = await store.bills(c.id)
    assert (
        len(days["test"]) == 7
        and tries["test"]["error"] == "HTTP 403 from ce.us-east-1.amazonaws.com"
    )
    assert SECRET not in tries["test"]["error"]


@pytest.mark.anyio
async def test_force_reads_now(store: Store) -> None:
    c, ce = billed(), CostExplorer()
    assert await billing.refresh(store, [c], ce.client(), NOW) == 1
    assert await billing.refresh(store, [c], ce.client(), NOW + H, force=True) == 1


@pytest.mark.anyio
async def test_the_read_covers_the_last_thirty_full_days_and_not_today(store: Store) -> None:
    ce = CostExplorer()
    await billing.refresh(store, [billed()], ce.client(), NOW + 5 * H)
    sent = json.loads(ce.asked[0].content)
    assert sent["TimePeriod"] == {"Start": "1972-08-28", "End": "1972-09-27"}  # 27th is today


@pytest.mark.anyio
async def test_a_cluster_without_billing_is_never_read(store: Store) -> None:
    ce = CostExplorer()
    assert await billing.refresh(store, [make_contract()], ce.client(), NOW) == 0
    assert ce.asked == []


@pytest.mark.anyio
async def test_two_contracts_bills_stay_apart(store: Store) -> None:
    c = billed()
    await seed(store, c, dict.fromkeys(DAYS, 2.2))
    days, tries = await store.bills("other")
    assert days == {} and tries == {}


def test_the_estimate_counts_the_clusters_node_hours() -> None:
    fake = FakePrometheus()
    nodes(fake, hours=48)
    t = usage.Target("test", "http://prometheus", None, {}, ["shop"])
    vcpu, gib, sampled = usage.node_hours(_prom(fake, t), t, 0, 48 * 3600 - 1)
    assert (vcpu, gib, sampled) == (96.0, 384.0, 48)


def test_the_samples_counted_are_the_hours_the_nodes_were_seen() -> None:
    fake = FakePrometheus()
    nodes(fake, hours=30)  # 18 of the 48 hours missing
    t = usage.Target("test", "http://prometheus", None, {}, ["shop"])
    assert usage.node_hours(_prom(fake, t), t, 0, 48 * 3600 - 1) == (60.0, 240.0, 30)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"aws": False}, "set billing: it reads the aws, azure or gcp"),
        ({"prometheus": False}, "set billing: they need a Prometheus"),
    ],
)
def test_billing_needs_the_aws_integration_and_a_prometheus(
    change: dict[str, bool], message: str
) -> None:
    data = billed().model_dump()
    data["integrations"] = [
        i
        for i in data["integrations"]
        if not (i["name"] == "aws" and change.get("aws") is False)
        and not (i["name"] == "prometheus" and change.get("prometheus") is False)
    ]
    with pytest.raises(ValidationError, match=message):
        contracts.build(data)


@pytest.mark.parametrize(
    "tag", ["", "novalue", "=v", "k=", "k=v\nx", "k=" + "v" * 257, "a" * 129 + "=v"]
)
def test_a_malformed_tag_is_refused(tag: str) -> None:
    with pytest.raises(ValidationError, match="billing"):
        billed(billing={"tag": tag})


def test_the_hub_reads_the_bill_with_its_hourly_run(
    settings: Settings, fake_cluster: Any, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.database_url = SecretStr(database_url)
    fake = FakePrometheus()
    nodes(fake)
    monkeypatch.setattr(usage, "Prom", lambda t: _prom(fake, t))
    ce = CostExplorer()
    with client(
        settings,
        fake_cluster,
        more={"bills": (billed("bills"), fake_cluster)},
        bill_http=ce.client(),
    ) as c:
        assert c.portal is not None

        def tick(at: float) -> None:
            c.portal.call(c.app.state.due_sizings, at)  # type: ignore[union-attr,attr-defined]

        tick(NOW)
        assert len(ce.asked) == 1
        tick(NOW + H)
        assert len(ce.asked) == 1  # once a day
        ce.status = 503
        tick(NOW + 25 * H)  # a bill that is down never stops the runs
        assert len(ce.asked) == 2


# --- Azure (slice 10.5b) ---------------------------------------------------------------

TENANT = "11111111-1111-1111-1111-111111111111"
CLIENT = "22222222-2222-2222-2222-222222222222"
SUB = "33333333-3333-3333-3333-333333333333"
AZ_SECRET = "azure~client~secret"
AZURE = {
    "name": "azure",
    "vendor": "azure",
    "tools": {"subscription_list": "read"},
    "env": {"AZURE_TENANT_ID": TENANT, "AZURE_CLIENT_ID": CLIENT, "AZURE_SUBSCRIPTION_ID": SUB},
    "env_secrets": {"AZURE_CLIENT_SECRET": "az-secret"},
}


def azure_billed(
    currency: str = "USD", with_aws: bool = False, **cluster: Any
) -> contracts.Contract:
    data = make_contract().model_dump()
    data["integrations"] += [{"name": "prometheus", "url": "http://prometheus"}, AZURE]
    if with_aws:
        data["integrations"].append(AWS)
    data["prices"] = {"currency": currency, "cpu": 0.03, "memory": 0.004}
    data["clusters"][0]["billing"] = {"tag": TAG}
    data["clusters"][0].update(cluster)
    return contracts.build(
        data, secrets={"az-secret": SecretStr(AZ_SECRET), "aws-key": SecretStr(SECRET)}
    )


def azure_body(amounts: dict[str, float], unit: str = "BRL") -> dict[str, Any]:
    return {
        "properties": {
            "columns": [
                {"name": "PreTaxCost", "type": "Number"},
                {"name": "UsageDate", "type": "Number"},
                {"name": "Currency", "type": "String"},
            ],
            "rows": [[a, int(d.replace("-", "")), unit] for d, a in amounts.items()],
        }
    }


class CostManagement:
    """Entra's token endpoint and Cost Management's query, counting what was asked."""

    def __init__(self, amounts: dict[str, float] | None = None, unit: str = "BRL") -> None:
        self.asked: list[httpx.Request] = []
        self.amounts = amounts if amounts is not None else dict.fromkeys(DAYS, 2.2)
        self.unit = unit
        self.status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.asked.append(request)
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": "tok"})
        if self.status != 200:
            return httpx.Response(self.status, json={"error": AZ_SECRET})
        return httpx.Response(200, json=azure_body(self.amounts, self.unit))

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))


def test_the_azure_request_asks_for_the_tags_vm_usage_before_credits() -> None:
    sent = billing.azure_request(TAG, "1972-09-01", "1972-09-26")
    assert sent["type"] == "ActualCost" and sent["timeframe"] == "Custom"
    assert sent["timePeriod"] == {"from": "1972-09-01T00:00:00Z", "to": "1972-09-25T23:59:59Z"}
    data = sent["dataset"]
    assert data["granularity"] == "Daily"
    assert data["aggregation"] == {"totalCost": {"name": "PreTaxCost", "function": "Sum"}}
    assert data["filter"]["and"] == [
        {"tags": {"name": "squero-cluster", "operator": "In", "values": ["kind"]}},
        {"dimensions": {"name": "ServiceName", "operator": "In", "values": ["Virtual Machines"]}},
        {"dimensions": {"name": "ChargeType", "operator": "In", "values": ["Usage"]}},
    ]
    equals = billing.azure_request("k=a=b", "1972-09-01", "1972-09-26")
    assert equals["dataset"]["filter"]["and"][0]["tags"]["values"] == ["a=b"]


def test_azure_days_are_parsed_by_column_with_their_currency() -> None:
    assert billing.azure_parse(azure_body({"1972-09-25": 2.5})) == {
        "1972-09-25": (2.5, "BRL", False)
    }


def test_azure_asks_for_a_token_then_the_query_and_sends_the_secret_only_for_the_token() -> None:
    cm = CostManagement()
    got = billing.azure_fetch(
        cm.client(), TENANT, CLIENT, AZ_SECRET, SUB, TAG, DAYS[0], "1972-09-26"
    )
    assert got[DAYS[0]] == (2.2, "BRL", False)
    token, query = cm.asked
    assert str(token.url) == f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"
    assert f"client_secret={AZ_SECRET.replace('~', '%7E')}" in token.content.decode() or (
        AZ_SECRET in token.content.decode()
    )
    assert "scope=https%3A%2F%2Fmanagement.azure.com%2F.default" in token.content.decode()
    assert query.url.host == "management.azure.com"
    assert query.url.path == f"/subscriptions/{SUB}/providers/Microsoft.CostManagement/query"
    assert query.url.params["api-version"] == "2023-11-01"
    assert query.headers["authorization"] == "Bearer tok"
    assert AZ_SECRET not in str(query.headers) + query.content.decode()


def test_a_day_without_usage_before_the_latest_day_reads_as_zero() -> None:
    cm = CostManagement({DAYS[0]: 2.0, DAYS[3]: 3.0})
    got = billing.azure_fetch(cm.client(), TENANT, CLIENT, AZ_SECRET, SUB, TAG, DAYS[0], DAYS[5])
    assert got == {
        DAYS[0]: (2.0, "BRL", False),
        DAYS[1]: (0.0, "BRL", False),
        DAYS[2]: (0.0, "BRL", False),
        DAYS[3]: (3.0, "BRL", False),
    }  # DAYS[4] may just not have settled: left missing
    empty = CostManagement({})
    assert (
        billing.azure_fetch(empty.client(), TENANT, CLIENT, AZ_SECRET, SUB, TAG, DAYS[0], DAYS[5])
        == {}
    )


def test_every_azure_page_is_read() -> None:
    pages = [
        {
            **azure_body({DAYS[0]: 1.0}),
            "properties": {
                **azure_body({DAYS[0]: 1.0})["properties"],
                "nextLink": "https://management.azure.com/next",
            },
        },
        azure_body({DAYS[1]: 2.0}),
    ]
    urls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": "tok"})
        urls.append(str(request.url))
        return httpx.Response(200, json=pages[len(urls) - 1])

    http = httpx.Client(transport=httpx.MockTransport(handler))
    got = billing.azure_fetch(http, TENANT, CLIENT, AZ_SECRET, SUB, TAG, DAYS[0], DAYS[2])
    assert urls[1] == "https://management.azure.com/next" and set(got) == {DAYS[0], DAYS[1]}


@pytest.mark.anyio
async def test_an_azure_bill_is_read_with_the_integrations_credentials(store: Store) -> None:
    c, cm = azure_billed(), CostManagement()
    assert await billing.refresh(store, [c], cm.client(), NOW) == 1
    token, query = cm.asked
    assert TENANT in str(token.url) and f"client_id={CLIENT}" in token.content.decode()
    assert SUB in query.url.path
    days, _ = await store.bills(c.id)
    assert days["test"][DAYS[0]]["currency"] == "BRL"


@pytest.mark.anyio
async def test_a_throttled_azure_read_is_named_and_tried_again_after_an_hour(store: Store) -> None:
    c, cm = azure_billed(), CostManagement()
    cm.status = 429
    http = cm.client()
    assert await billing.refresh(store, [c], http, NOW) == 1
    assert await billing.refresh(store, [c], http, NOW + H / 2) == 0
    assert await billing.refresh(store, [c], http, NOW + 1.5 * H) == 1
    _, tries = await store.bills(c.id)
    assert tries["test"]["error"] == "HTTP 429 from management.azure.com"
    assert AZ_SECRET not in tries["test"]["error"]


@pytest.mark.anyio
async def test_a_cluster_reads_the_bill_of_its_provider(store: Store) -> None:
    both = azure_billed(with_aws=True, provider="azure")
    cm = CostManagement()
    await billing.refresh(store, [both], cm.client(), NOW)
    assert {r.url.host for r in cm.asked} == {"login.microsoftonline.com", "management.azure.com"}
    aws = azure_billed(with_aws=True, provider="aws")
    ce = CostExplorer()
    await billing.refresh(store, [aws], ce.client(), NOW, force=True)
    assert {r.url.host for r in ce.asked} == {"ce.us-east-1.amazonaws.com"}


@pytest.mark.anyio
async def test_a_brl_bill_is_compared_with_brl_prices_and_not_with_usd_ones(store: Store) -> None:
    fake = FakePrometheus()
    nodes(fake)
    brl = azure_billed("BRL")
    await store.save_bill(brl.id, "test", {d: (2.2, "BRL", False) for d in DAYS}, NOW)
    got = await row(store, brl, fake)
    assert got["currency"] == "BRL" and got["gap"] is not None and got["note"] is None
    usd = azure_billed("USD")
    got = await row(store, usd, fake)
    assert got["gap"] is None and "BRL" in got["note"] and "USD" in got["note"]


def test_billing_needs_one_clear_cloud_integration() -> None:
    with pytest.raises(ValidationError, match="aws, azure or gcp"):
        azure_billed(with_aws=True)  # both, no provider
    with pytest.raises(ValidationError, match="aws, azure or gcp"):
        azure_billed(provider="aws")  # provider aws, only azure held
    with pytest.raises(ValidationError, match="aws, azure or gcp"):
        azure_billed(provider="gcp")
    data = azure_billed().model_dump()
    data["integrations"] = [i for i in data["integrations"] if i["name"] != "azure"]
    with pytest.raises(ValidationError, match="aws, azure or gcp"):
        contracts.build(data)
    assert (
        azure_billed(with_aws=True, provider="azure").bill_provider(
            azure_billed(with_aws=True, provider="azure").clusters[0]
        )
        == "azure"
    )
    assert azure_billed().bill_provider(azure_billed().clusters[0]) == "azure"


def test_the_cli_prints_a_bill_of_nothing_beside_an_estimate(
    database_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import asyncio
    import time

    from squero import cli

    c, fake = billed(), FakePrometheus()
    nodes(fake)

    async def seed_zero() -> None:
        async with connect(database_url) as pool:
            store = Store(pool)
            await store.migrate()
            await store.save_bill(c.id, "test", {d: (0.0, "USD", False) for d in DAYS}, NOW)

    asyncio.run(seed_zero())
    report = billing.report

    async def reported(store: Store, c: Any, now: float, days: int) -> Any:
        return await report(store, c, now, days, prom_of(fake))

    monkeypatch.setattr(billing, "report", reported)
    monkeypatch.setattr(cli, "contract", lambda settings, cid: c)
    monkeypatch.setenv("SQUERO_DATABASE_URL", database_url)
    monkeypatch.setattr(time, "time", lambda: NOW)
    assert cli.main(["sizing", "bill", "--contract", c.id]) == 1
    assert "no gap (nothing billed)" in capsys.readouterr().out


# --- coverage (slice 10.5d) ----------------------------------------------------------------


@pytest.mark.anyio
async def test_a_complete_estimate_has_full_coverage_and_no_note(store: Store) -> None:
    c, fake = billed(), FakePrometheus()
    nodes(fake)
    await seed(store, c, dict.fromkeys(DAYS, 2.2))
    got = await row(store, c, fake)
    assert (got["hours"], got["sampled"], got["coverage"], got["note"]) == (168, 168, 1.0, None)
    assert got["within"] is True


@pytest.mark.anyio
async def test_a_few_missing_hours_are_noted_and_the_gap_still_judges(store: Store) -> None:
    c, fake = billed(), FakePrometheus()
    nodes(fake, hours=160)  # 95%: estimate 14.72 against 14.00 billed, +5.1%
    await seed(store, c, dict.fromkeys(DAYS, 2.0))
    got = await row(store, c, fake)
    assert got["sampled"] == 160 and got["coverage"] == pytest.approx(160 / 168)
    assert got["note"] == "the estimate rests on 160 of 168 sampled hours"
    assert got["gap"] == pytest.approx(0.0514, abs=1e-3) and got["within"] is False


@pytest.mark.anyio
async def test_too_few_sampled_hours_give_a_gap_but_no_verdict(store: Store) -> None:
    c, fake = billed(), FakePrometheus()
    nodes(fake, hours=100)
    await seed(store, c, dict.fromkeys(DAYS, 2.2))
    got = await row(store, c, fake)
    assert got["coverage"] == pytest.approx(100 / 168)
    assert got["gap"] is not None and got["gap"] < -0.3  # the numbers stay visible
    assert got["within"] is None  # not False: missing samples are not a price problem
    assert "100 of 168" in got["note"]


@pytest.mark.anyio
async def test_ninety_percent_still_gets_a_verdict(store: Store) -> None:
    c, fake = billed(), FakePrometheus()
    nodes(fake, hours=108)  # 108 of 120 = 90% over 5 days
    await seed(store, c, dict.fromkeys(DAYS, 2.2))
    got = await row(store, c, fake, days=5)
    assert got["coverage"] == pytest.approx(0.9) and got["within"] is not None


def test_the_cli_does_not_fail_a_row_for_missing_samples_alone(
    database_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import asyncio
    import time

    from squero import cli

    c, fake = billed(), FakePrometheus()
    nodes(fake, hours=100)

    async def seed_bills() -> None:
        async with connect(database_url) as pool:
            store = Store(pool)
            await store.migrate()
            await store.save_bill(c.id, "test", {d: (2.2, "USD", False) for d in DAYS}, NOW)

    asyncio.run(seed_bills())
    report = billing.report

    async def reported(store: Store, c: Any, now: float, days: int) -> Any:
        return await report(store, c, now, days, prom_of(fake))

    monkeypatch.setattr(billing, "report", reported)
    monkeypatch.setattr(cli, "contract", lambda settings, cid: c)
    monkeypatch.setenv("SQUERO_DATABASE_URL", database_url)
    monkeypatch.setattr(time, "time", lambda: NOW)
    assert cli.main(["sizing", "bill", "--contract", c.id]) == 0
    out = capsys.readouterr().out
    assert "no verdict" in out and "100 of 168 sampled hours" in out


# --- GCP (slice 10.5c) -----------------------------------------------------------------------

from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding  # noqa: E402

from tests.test_gcp import KEY as GCP_KEY  # noqa: E402
from tests.test_gcp import PRIVATE, unpad  # noqa: E402

TABLE = "acme-prod.billing.gcp_billing_export_v1_ABC123"
GCP_TAG = "squero-cluster=kind"
GCP = {
    "name": "gcp",
    "vendor": "gcp",
    "env": {"GOOGLE_CLOUD_PROJECT": "acme-prod"},
    "env_secrets": {"GOOGLE_SERVICE_ACCOUNT_KEY": "gcp-key"},
    "tools": {"list_log_names": "read"},
}


def gcp_billed(
    currency: str = "USD", billing: dict[str, Any] | None = None, **cluster: Any
) -> contracts.Contract:
    data = make_contract().model_dump()
    data["integrations"] += [{"name": "prometheus", "url": "http://prometheus"}, GCP]
    data["prices"] = {"currency": currency, "cpu": 0.03, "memory": 0.004}
    data["clusters"][0]["billing"] = billing or {"tag": GCP_TAG, "table": TABLE}
    data["clusters"][0].update(cluster)
    return contracts.build(data, secrets={"gcp-key": SecretStr(GCP_KEY)})


def gcp_body(amounts: dict[str, float], unit: str = "BRL") -> dict[str, Any]:
    return {
        "jobComplete": True,
        "rows": [{"f": [{"v": d}, {"v": unit}, {"v": str(a)}]} for d, a in amounts.items()],
    }


class BigQuery:
    """Google's token endpoint and BigQuery's query, counting what was asked."""

    def __init__(self, amounts: dict[str, float] | None = None, unit: str = "BRL") -> None:
        self.asked: list[httpx.Request] = []
        self.amounts = amounts if amounts is not None else dict.fromkeys(DAYS, 2.2)
        self.unit = unit
        self.status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.asked.append(request)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"message": GCP_KEY}})
        return httpx.Response(200, json=gcp_body(self.amounts, self.unit))

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))


def test_the_gcp_query_asks_for_the_labels_core_and_ram_usage_spot_included() -> None:
    sent = billing.gcp_request(TABLE, GCP_TAG, "1972-09-01", "1972-09-26")
    sql = sent["query"]
    assert f"FROM `{TABLE}`" in sql and sent["useLegacySql"] is False
    assert "service.description = 'Compute Engine'" in sql and "cost_type = 'regular'" in sql
    assert "'% Instance Core running%'" in sql and "'% Instance Ram running%'" in sql
    assert "SUM(cost)" in sql and "NOT LIKE" not in sql  # spot SKUs too (slice 16.1b)
    assert "tag" not in sql and "squero-cluster" not in sql and "kind" not in sql  # parameters
    params = {p["name"]: p["parameterValue"]["value"] for p in sent["queryParameters"]}
    assert params == {
        "start": "1972-09-01 00:00:00+00",
        "end": "1972-09-26 00:00:00+00",
        "key": "squero-cluster",
        "value": "kind",
    }
    assert sent["parameterMode"] == "NAMED"


def test_gcp_days_are_parsed_with_their_currency() -> None:
    assert billing.gcp_parse(gcp_body({"1972-09-25": 2.5})) == {"1972-09-25": (2.5, "BRL", False)}
    assert billing.gcp_parse({"jobComplete": True}) == {}  # a label with no usage


def test_a_query_that_did_not_finish_or_came_in_pages_or_two_currencies_is_refused() -> None:
    with pytest.raises(ValueError, match="had not finished"):
        billing.gcp_parse({"jobComplete": False})
    with pytest.raises(ValueError, match="pages"):
        billing.gcp_parse({**gcp_body({}), "pageToken": "t"})
    two = gcp_body({"1972-09-25": 1.0})
    two["rows"] += gcp_body({"1972-09-25": 2.0}, "USD")["rows"]
    with pytest.raises(ValueError, match="two currencies"):
        billing.gcp_parse(two)


def test_gcp_mints_a_bigquery_only_token_at_googles_endpoint_then_queries() -> None:
    bq = BigQuery()
    got = billing.gcp_fetch(
        bq.client(), GCP_KEY, "acme-prod", TABLE, GCP_TAG, DAYS[0], "1972-09-26"
    )
    assert got[DAYS[0]] == (2.2, "BRL", False)
    token, query = bq.asked
    assert str(token.url) == "https://oauth2.googleapis.com/token"
    form = dict(x.split("=", 1) for x in token.content.decode().split("&"))
    header, claims, signature = form["assertion"].split(".")
    PRIVATE.public_key().verify(  # signed by the Contract's own key
        unpad(signature), f"{header}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256()
    )
    body = json.loads(unpad(claims))
    assert body["scope"] == "https://www.googleapis.com/auth/bigquery.readonly"  # nothing else
    assert body["aud"] == "https://oauth2.googleapis.com/token"
    assert query.url.host == "bigquery.googleapis.com"
    assert query.url.path == "/bigquery/v2/projects/acme-prod/queries"
    assert query.headers["authorization"] == "Bearer tok"
    assert GCP_KEY not in str(query.headers) + query.content.decode()
    assert "private_key" not in token.content.decode()


def test_a_gcp_day_without_usage_before_the_latest_reads_as_zero() -> None:
    bq = BigQuery({DAYS[0]: 2.0, DAYS[3]: 3.0})
    got = billing.gcp_fetch(bq.client(), GCP_KEY, "acme-prod", TABLE, GCP_TAG, DAYS[0], DAYS[5])
    assert got == {
        DAYS[0]: (2.0, "BRL", False),
        DAYS[1]: (0.0, "BRL", False),
        DAYS[2]: (0.0, "BRL", False),
        DAYS[3]: (3.0, "BRL", False),
    }


@pytest.mark.anyio
async def test_a_gcp_bill_is_read_with_the_integrations_key_and_project(store: Store) -> None:
    c, bq = gcp_billed(), BigQuery()
    assert await billing.refresh(store, [c], bq.client(), NOW) == 1
    _, query = bq.asked
    assert query.url.path == "/bigquery/v2/projects/acme-prod/queries"
    sent = json.loads(query.content)
    assert f"`{TABLE}`" in sent["query"]
    days, _ = await store.bills(c.id)
    assert days["test"][DAYS[0]]["currency"] == "BRL"


@pytest.mark.anyio
async def test_a_refused_gcp_read_is_named_by_status_and_host_and_tried_after_an_hour(
    store: Store,
) -> None:
    c, bq = gcp_billed(), BigQuery()
    bq.status = 403
    http = bq.client()
    assert await billing.refresh(store, [c], http, NOW) == 1
    assert await billing.refresh(store, [c], http, NOW + H / 2) == 0
    assert await billing.refresh(store, [c], http, NOW + 1.5 * H) == 1
    _, tries = await store.bills(c.id)
    assert tries["test"]["error"] == "HTTP 403 from bigquery.googleapis.com"
    assert "private_key" not in tries["test"]["error"]


@pytest.mark.anyio
async def test_a_cluster_reads_the_bill_of_its_provider_among_three_clouds(store: Store) -> None:
    data = gcp_billed(provider="gcp").model_dump()
    data["integrations"] += [AZURE, AWS]
    c = contracts.build(
        data,
        secrets={
            "gcp-key": SecretStr(GCP_KEY),
            "az-secret": SecretStr(AZ_SECRET),
            "aws-key": SecretStr(SECRET),
        },
    )
    bq = BigQuery()
    await billing.refresh(store, [c], bq.client(), NOW)
    assert {r.url.host for r in bq.asked} == {"oauth2.googleapis.com", "bigquery.googleapis.com"}


def test_billing_across_clouds_needs_a_provider_when_the_contract_has_several() -> None:
    data = gcp_billed().model_dump()
    data["integrations"] += [AWS]
    with pytest.raises(ValidationError, match="set provider"):
        contracts.build(data)
    data["clusters"][0]["provider"] = "gcp"
    assert contracts.build(data).bill_provider(contracts.build(data).clusters[0]) == "gcp"
    one = gcp_billed()
    assert one.bill_provider(one.clusters[0]) == "gcp"  # the only cloud integration


@pytest.mark.parametrize(
    ("billing_field", "message"),
    [
        ({"tag": GCP_TAG}, "set billing.table"),
        ({"tag": "Squero-Cluster=kind", "table": TABLE}, "lowercase"),
        ({"tag": "squero cluster=kind", "table": TABLE}, "lowercase"),
        ({"tag": "1x=kind", "table": TABLE}, "lowercase"),
        ({"tag": "k=" + "v" * 64, "table": TABLE}, "lowercase"),
    ],
)
def test_a_gcp_cluster_needs_a_table_and_a_valid_label(
    billing_field: dict[str, str], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        gcp_billed(billing=billing_field)


@pytest.mark.parametrize(
    "table",
    [
        "x.y.z",
        "acme-prod.billing",
        "acme-prod.billing.t; DROP TABLE a",
        "acme-prod.billing.t`",
        "acme-prod.bill-ing.t",
        "ACME.billing.t",
        "acme-prod.billing.t.u",
    ],
)
def test_a_table_name_that_could_change_the_query_is_refused(table: str) -> None:
    with pytest.raises(ValidationError, match="billing"):
        gcp_billed(billing={"tag": GCP_TAG, "table": table})


def test_a_table_is_only_for_gcp_bills() -> None:
    with pytest.raises(ValidationError, match="only GCP bills use it"):
        azure_billed(billing={"tag": TAG, "table": TABLE})
    data = billed().model_dump()
    data["clusters"][0]["billing"]["table"] = TABLE
    with pytest.raises(ValidationError, match="only GCP bills use it"):
        contracts.build(data)


@pytest.mark.anyio
async def test_a_gcp_bill_in_brl_is_compared_with_brl_prices_only(store: Store) -> None:
    fake = FakePrometheus()
    nodes(fake)
    for money, note in (("BRL", None), ("USD", "not converted")):
        c = gcp_billed(money)
        await store.save_bill(c.id, "test", {d: (2.2, "BRL", False) for d in DAYS}, NOW)
        got = await row(store, c, fake)
        assert (got["gap"] is not None) == (note is None)
        assert note is None or note in got["note"]


@pytest.mark.anyio
async def test_two_gcp_contracts_bills_stay_apart(store: Store) -> None:
    a, b = gcp_billed(), gcp_billed()
    b = b.model_copy(update={"id": "other"})
    await store.save_bill(a.id, "test", {d: (2.2, "BRL", False) for d in DAYS}, NOW)
    days, tries = await store.bills(b.id)
    assert days == {} and tries == {}


def test_an_azure_day_in_two_currencies_is_refused() -> None:
    """An EA account's spot rows are priced in USD whatever its currency (slice 16.1b):
    the two are never summed, nor one kept, on one page or across two."""
    two = azure_body({DAYS[0]: 1.0})
    two["properties"]["rows"].append([0.5, int(DAYS[0].replace("-", "")), "USD"])
    with pytest.raises(ValueError, match=f"{DAYS[0]} was billed in two currencies"):
        billing.azure_parse(two)
    pages = [
        {**azure_body({DAYS[0]: 1.0}), "properties": {
            **azure_body({DAYS[0]: 1.0})["properties"], "nextLink": "https://management.azure.com/next"
        }},
        azure_body({DAYS[0]: 0.5}, unit="USD"),
    ]  # fmt: skip
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": "tok"})
        seen.append(str(request.url))
        return httpx.Response(200, json=pages[len(seen) - 1])

    http = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(ValueError, match="two currencies"):
        billing.azure_fetch(http, TENANT, CLIENT, AZ_SECRET, SUB, TAG, DAYS[0], DAYS[2])
