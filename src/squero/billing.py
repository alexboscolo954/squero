"""Cloud bills (slices 10.5a to 10.5c): what Cost Explorer (AWS), Cost Management (Azure)
or the billing export in BigQuery (GCP) says a cluster's tagged compute cost, set beside
what its node-hours cost at the Hub's prices. Read-only; a shown cost never changes (a
billed rate feeding prices is a later slice)."""

import asyncio
import json
import time
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx

from squero import listprices, usage
from squero.contracts import Aws, Cluster, Contract
from squero.servers.aws import authorization
from squero.servers.vendors import GOOGLE_TOKEN_URL, GoogleKey

if TYPE_CHECKING:
    from squero.store import Store

TOLERANCE = 0.05  # the estimate may differ from the bill by this much and still reconcile
MIN_COVERAGE = 0.9  # below this share of the window's hours sampled, the estimate can't judge
LAG_DAYS = 2  # Cost Explorer settles a day within about 24 to 48 hours
# audit A6a: a day read this soon after it ended may still be filling in GCP's export
SETTLES = 3 * 86400
KEPT_DAYS = 30  # days read each time, and the most `--days` takes
DAY = 86400
HOUR = 3600
HOST = "ce.us-east-1.amazonaws.com"  # Cost Explorer has this one endpoint
AZURE_HOST = "management.azure.com"
BIGQUERY = "https://bigquery.googleapis.com/bigquery/v2/projects/{project}/queries"
BIGQUERY_SCOPE = "https://www.googleapis.com/auth/bigquery.readonly"
# Compute Engine bills a VM's cores and RAM on their own SKUs (disks, network and IPs are
# other lines of the same service): the estimate is vCPU and GiB hours, so only those
# count, Spot ones ("Spot Preemptible ...") included (slice 16.1b), usage before credits.
GCP_QUERY = """
SELECT FORMAT_DATE('%Y-%m-%d', DATE(usage_start_time, 'UTC')) AS day, currency,
       SUM(cost) AS cost,
       SUM(IF(STARTS_WITH(sku.description, 'Spot Preemptible')
              OR STARTS_WITH(sku.description, 'Preemptible'), cost, 0)) AS spot,
       SUM(IF(sku.description LIKE '% Instance Ram running%',
              usage.amount_in_pricing_units, 0)) AS ram_hours
FROM `{table}`
WHERE usage_start_time >= @start AND usage_start_time < @end
  AND service.description = 'Compute Engine' AND cost_type = 'regular'
  AND (sku.description LIKE '% Instance Core running%'
       OR sku.description LIKE '% Instance Ram running%')
  AND EXISTS (SELECT 1 FROM UNNEST(labels) AS l WHERE l.key = @key AND l.value = @value)
GROUP BY day, currency
"""
LOGIN = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
# day -> amount, currency, still estimated, what its spot lines cost (slice 16.4b; None
# where the total's read doesn't say: AWS's and Azure's come from a read of their own), and
# the RAM GiB-hours its lines charged for (audit A6a: GCP's export only, else None)
Bill = dict[str, tuple[float, str, bool, float | None, float | None]]
COMPUTE = "Amazon Elastic Compute Cloud - Compute"


def utc_day(at: float) -> date:
    return datetime.fromtimestamp(at, UTC).date()


def period(now: float, days: int) -> list[str]:
    """The `days` full UTC days ending LAG_DAYS days ago, oldest first."""
    last = utc_day(now) - timedelta(days=LAG_DAYS)
    return [(last - timedelta(days=i)).isoformat() for i in reversed(range(days))]


def request(
    tag: str, start: str, end: str, token: str | None = None, spot: bool = False
) -> dict[str, Any]:
    """Cost Explorer's GetCostAndUsage body: the tag's EC2 compute, usage only (before
    credits), unblended, a day at a time; `end` is exclusive. With `spot`, grouped by
    usage type, so its spot lines (`...SpotUsage:<type>`) can be told apart (slice
    16.4b): a read of its own, so a shape not as expected costs the spot part only."""
    key, _, value = tag.partition("=")
    body: dict[str, Any] = {
        "TimePeriod": {"Start": start, "End": end},
        "Granularity": "DAILY",
        "Metrics": ["UnblendedCost"],
        **({"GroupBy": [{"Type": "DIMENSION", "Key": "USAGE_TYPE"}]} if spot else {}),
        "Filter": {
            "And": [
                {"Tags": {"Key": key, "Values": [value], "MatchOptions": ["EQUALS"]}},
                {"Dimensions": {"Key": "SERVICE", "Values": [COMPUTE]}},
                {"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Usage"]}},
            ]
        },
    }
    if token:
        body["NextPageToken"] = token
    return body


def parse(body: dict[str, Any]) -> Bill:
    out: Bill = {}
    for r in body.get("ResultsByTime", []):
        cost = r["Total"]["UnblendedCost"]
        out[r["TimePeriod"]["Start"]] = (
            float(cost["Amount"]), cost["Unit"], bool(r["Estimated"]), None, None
        )  # fmt: skip
    return out


def parse_spot(body: dict[str, Any], before: dict[str, float] | None = None) -> dict[str, float]:
    """A grouped page's spot lines, a day at a time, added to the pages `before` (one
    day's groups may continue on the next page: not said otherwise); a day without a
    spot group is 0 (slice 16.4b)."""
    out = dict(before or {})
    for r in body.get("ResultsByTime", []):
        day = r["TimePeriod"]["Start"]
        out[day] = out.get(day, 0.0) + sum(
            float(g["Metrics"]["UnblendedCost"]["Amount"])
            for g in r.get("Groups", [])
            if any("SpotUsage" in k for k in g.get("Keys", []))
        )
    return out


def _aws_pages(
    http: httpx.Client,
    key_id: str,
    secret: str,
    tag: str,
    start: str,
    end: str,
    now: float,
    spot: bool = False,
) -> Iterator[dict[str, Any]]:
    """Each page of Cost Explorer's reply, signed with the key."""
    token = None
    while True:
        data = json.dumps(request(tag, start, end, token, spot)).encode()
        headers = {
            "content-type": "application/x-amz-json-1.1",
            "host": HOST,
            "x-amz-date": datetime.fromtimestamp(now, UTC).strftime("%Y%m%dT%H%M%SZ"),
            "x-amz-target": "AWSInsightsIndexService.GetCostAndUsage",
        }
        auth = authorization(
            "POST", HOST, "/", "", headers, data, "us-east-1", "ce",
            key_id, secret, headers["x-amz-date"],
        )  # fmt: skip
        resp = http.post(
            f"https://{HOST}/", content=data, headers={**headers, "authorization": auth}
        )
        resp.raise_for_status()
        body = resp.json()
        yield body
        token = body.get("NextPageToken")
        if not token:
            return


def aws_fetch(
    http: httpx.Client, key_id: str, secret: str, tag: str, start: str, end: str, now: float
) -> Bill:
    """The tag's billed days from `start` up to (not including) `end`."""
    out: Bill = {}
    for body in _aws_pages(http, key_id, secret, tag, start, end, now):
        out |= parse(body)
    return out


def aws_spot_fetch(
    http: httpx.Client, key_id: str, secret: str, tag: str, start: str, end: str, now: float
) -> dict[str, float]:
    """What the tag's spot lines cost each day (slice 16.4b), a read of its own."""
    out: dict[str, float] = {}
    for body in _aws_pages(http, key_id, secret, tag, start, end, now, spot=True):
        out = parse_spot(body, out)
    return out


def azure_request(tag: str, start: str, end: str, spot: bool = False) -> dict[str, Any]:
    """Cost Management's query body: the tag's Virtual Machines usage (before credits)
    a day at a time; `end` is exclusive, Azure's `to` is not. With `spot`, grouped by
    pricing model, so its Spot rows can be told apart (slice 16.4b): a read of its own."""
    key, _, value = tag.partition("=")
    last = date.fromisoformat(end) - timedelta(days=1)
    return {
        "type": "ActualCost",
        "timeframe": "Custom",
        "timePeriod": {"from": f"{start}T00:00:00Z", "to": f"{last}T23:59:59Z"},
        "dataset": {
            "granularity": "Daily",
            "aggregation": {"totalCost": {"name": "PreTaxCost", "function": "Sum"}},
            **({"grouping": [{"type": "Dimension", "name": "PricingModel"}]} if spot else {}),
            "filter": {
                "and": [
                    {"tags": {"name": key, "operator": "In", "values": [value]}},
                    {"dimensions": {"name": "ServiceName", "operator": "In",
                                    "values": ["Virtual Machines"]}},
                    {"dimensions": {"name": "ChargeType", "operator": "In", "values": ["Usage"]}},
                ]
            },
        },
    }  # fmt: skip


def azure_parse(body: dict[str, Any], before: Bill | None = None) -> Bill:
    """A page of rows, a day at a time. A day in two currencies (an EA account's spot
    rows are priced in USD whatever its own, slice 16.1b), on this page or on one
    `before`, is refused: the sum of two currencies is no amount."""
    props = body["properties"]
    names = [c["name"] for c in props["columns"]]
    cost, day, unit = (names.index(n) for n in ("PreTaxCost", "UsageDate", "Currency"))
    out: Bill = {}
    for row in props["rows"]:
        d = str(row[day])
        key = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        if key in out or key in (before or {}):
            raise ValueError(f"{key} was billed in two currencies")
        out[key] = (float(row[cost]), row[unit], False, None, None)
    return out


def azure_parse_spot(
    body: dict[str, Any], before: dict[str, float] | None = None
) -> dict[str, float]:
    """A grouped page's Spot rows, a day at a time, added to the pages `before`; a day
    without one is 0 (slice 16.4b). A reply without the `PricingModel` column is no
    answer: refused, the spot part not read."""
    props = body["properties"]
    names = [c["name"] for c in props["columns"]]
    if "PricingModel" not in names:
        raise ValueError("Cost Management's reply has no PricingModel column")
    cost, day, model = (names.index(n) for n in ("PreTaxCost", "UsageDate", "PricingModel"))
    out = dict(before or {})
    for row in props["rows"]:
        d = str(row[day])
        key = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        out[key] = out.get(key, 0.0) + (float(row[cost]) if row[model] == "Spot" else 0.0)
    return out


def azure_fetch(
    http: httpx.Client,
    tenant: str,
    client_id: str,
    secret: str,
    subscription: str,
    tag: str,
    start: str,
    end: str,
) -> Bill:
    """The tag's billed days from `start` up to (not including) `end`. Azure sends no row
    for a day without usage: a day before the latest one with a row is filled in as 0."""
    out: Bill = {}
    for body in _azure_pages(
        http, tenant, client_id, secret, subscription, azure_request(tag, start, end)
    ):
        out |= azure_parse(body, out)
    return fill_gaps(out, start)


def azure_spot_fetch(
    http: httpx.Client,
    tenant: str,
    client_id: str,
    secret: str,
    subscription: str,
    tag: str,
    start: str,
    end: str,
) -> dict[str, float]:
    """What the tag's Spot rows cost each day (slice 16.4b), a read of its own."""
    out: dict[str, float] = {}
    data = azure_request(tag, start, end, spot=True)
    for body in _azure_pages(http, tenant, client_id, secret, subscription, data):
        out = azure_parse_spot(body, out)
    return out


def _azure_pages(
    http: httpx.Client,
    tenant: str,
    client_id: str,
    secret: str,
    subscription: str,
    data: dict[str, Any],
) -> Iterator[dict[str, Any]]:
    """Each page of Cost Management's reply to `data`, with a token minted for it."""
    resp = http.post(
        LOGIN.format(tenant=tenant),
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": secret,
            "scope": "https://management.azure.com/.default",
        },
    )
    resp.raise_for_status()  # the token is used for this read and dropped
    headers = {"authorization": f"Bearer {resp.json()['access_token']}"}
    url: str | None = (
        f"https://{AZURE_HOST}/subscriptions/{subscription}"
        "/providers/Microsoft.CostManagement/query?api-version=2023-11-01"
    )
    while url:
        resp = http.post(url, json=data, headers=headers)
        resp.raise_for_status()
        body = resp.json()
        yield body
        url = body["properties"].get("nextLink")


def fill_gaps(out: Bill, start: str) -> Bill:
    """A day before the latest one with a row, and without one, had no usage: 0. After
    it, the cloud may just not have settled the day: left missing."""
    if out:
        unit = next(iter(out.values()))[1]
        spot = None if out[max(out)][3] is None else 0.0  # unread stays unread (16.4b)
        ram = None if out[max(out)][4] is None else 0.0  # and the same (audit A6a)
        day = date.fromisoformat(start)
        while day.isoformat() < max(out):
            out.setdefault(day.isoformat(), (0.0, unit, False, spot, ram))
            day += timedelta(days=1)
    return out


def gcp_request(table: str, tag: str, start: str, end: str) -> dict[str, Any]:
    """BigQuery's jobs.query body: the label's Compute Engine core and RAM usage from the
    billing export, a UTC day at a time; `end` is exclusive. The table is checked by
    Billing.table; the label goes in as parameters."""
    key, _, value = tag.partition("=")

    def param(name: str, kind: str, v: str) -> dict[str, Any]:
        return {"name": name, "parameterType": {"type": kind}, "parameterValue": {"value": v}}

    return {
        "query": GCP_QUERY.format(table=table),
        "useLegacySql": False,
        "parameterMode": "NAMED",
        "queryParameters": [
            param("start", "TIMESTAMP", f"{start} 00:00:00+00"),
            param("end", "TIMESTAMP", f"{end} 00:00:00+00"),
            param("key", "STRING", key),
            param("value", "STRING", value),
        ],
        "timeoutMs": 60000,
    }


def gcp_parse(body: dict[str, Any]) -> Bill:
    if not body.get("jobComplete"):
        raise ValueError("BigQuery had not finished the query in time")
    if body.get("pageToken"):
        raise ValueError("BigQuery answered in pages: more days than expected")
    out: Bill = {}
    for row in body.get("rows", []):
        day, unit, cost, spot, ram = (c["v"] for c in row["f"])
        if day in out:
            raise ValueError(f"{day} was billed in two currencies")
        out[day] = (float(cost), unit, False, float(spot or 0), float(ram or 0))
    return out


def gcp_fetch(
    http: httpx.Client, key: str, project: str, table: str, tag: str, start: str, end: str
) -> Bill:
    """The label's billed days from `start` up to (not including) `end`. The token is
    minted for this read with only the BigQuery read scope and dropped."""
    now = int(time.time())
    grant = {
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": GoogleKey(key, BIGQUERY_SCOPE).assertion(now),
    }
    resp = http.post(GOOGLE_TOKEN_URL, data=grant)
    resp.raise_for_status()
    resp = http.post(
        BIGQUERY.format(project=project),
        json=gcp_request(table, tag, start, end),
        headers={"authorization": f"Bearer {resp.json()['access_token']}"},
    )
    resp.raise_for_status()
    return fill_gaps(gcp_parse(resp.json()), start)


def client() -> httpx.Client:
    return httpx.Client(timeout=30)


def reader(c: Contract, cl: Cluster) -> Callable[[httpx.Client, str, str, str, float], Bill]:
    """The call that reads this cluster's bill, with its credentials bound: (http, tag,
    start, end, now) -> Bill."""
    if c.bill_provider(cl) == "azure":
        vendor = c.azure_vendor()
        assert vendor is not None

        def var(name: str) -> str:
            return vendor.env[name] if name in vendor.env else (
                c.secret(vendor.env_secrets[name]).get_secret_value()
            )  # fmt: skip

        return lambda http, tag, start, end, now: azure_fetch(
            http, var("AZURE_TENANT_ID"), var("AZURE_CLIENT_ID"), var("AZURE_CLIENT_SECRET"),
            var("AZURE_SUBSCRIPTION_ID"), tag, start, end,
        )  # fmt: skip
    if c.bill_provider(cl) == "gcp":
        gcp = c.gcp_vendor()
        assert gcp is not None and cl.billing is not None and cl.billing.table is not None
        table = cl.billing.table
        return lambda http, tag, start, end, now: gcp_fetch(
            http,
            c.secret(gcp.env_secrets["GOOGLE_SERVICE_ACCOUNT_KEY"]).get_secret_value(),
            gcp.env["GOOGLE_CLOUD_PROJECT"],
            table,
            tag,
            start,
            end,
        )
    aws = next(i for i in c.integrations if isinstance(i, Aws))
    return lambda http, tag, start, end, now: aws_fetch(
        http, aws.access_key_id, c.secret(aws.secret_access_key_secret).get_secret_value(),
        tag, start, end, now,
    )  # fmt: skip


def spot_reader(
    c: Contract, cl: Cluster
) -> Callable[[httpx.Client, str, str, str, float], dict[str, float]] | None:
    """The call that reads what this cluster's spot lines cost each day (slice 16.4b),
    its credentials bound; None for GCP, whose export query sums them with the total
    (only fields the query already reads: a second query would scan the table twice)."""
    if c.bill_provider(cl) == "gcp":
        return None
    if c.bill_provider(cl) == "azure":
        vendor = c.azure_vendor()
        assert vendor is not None

        def var(name: str) -> str:
            return vendor.env[name] if name in vendor.env else (
                c.secret(vendor.env_secrets[name]).get_secret_value()
            )  # fmt: skip

        return lambda http, tag, start, end, now: azure_spot_fetch(
            http, var("AZURE_TENANT_ID"), var("AZURE_CLIENT_ID"), var("AZURE_CLIENT_SECRET"),
            var("AZURE_SUBSCRIPTION_ID"), tag, start, end,
        )  # fmt: skip
    aws = next(i for i in c.integrations if isinstance(i, Aws))
    return lambda http, tag, start, end, now: aws_spot_fetch(
        http, aws.access_key_id, c.secret(aws.secret_access_key_secret).get_secret_value(),
        tag, start, end, now,
    )  # fmt: skip


async def refresh(
    store: "Store",
    contracts: Iterable[Contract],
    http: httpx.Client,
    now: float,
    force: bool = False,
) -> int:
    """Read every billed cluster's last KEPT_DAYS days unless it was read in the last
    day (a failed read is tried again after an hour); the reads made. A failure keeps
    the days already stored."""
    reads = 0
    end = utc_day(now)  # today is still filling: left out
    start = (end - timedelta(days=KEPT_DAYS)).isoformat()
    for c in contracts:
        billed = [cl for cl in c.clusters if cl.billing]
        if not billed:
            continue
        _, tries = await store.bills(c.id)
        for cl in billed:
            assert cl.billing is not None
            row = tries.get(cl.name)
            fresh = row and row["ok_at"] is not None and now - row["ok_at"] < DAY
            waiting = row and row["error"] and now - row["tried_at"] < HOUR
            if not force and (fresh or waiting):
                continue
            reads += 1
            try:
                got = await asyncio.to_thread(
                    reader(c, cl), http, cl.billing.tag, start, end.isoformat(), now
                )
            except (httpx.HTTPError, ValueError, KeyError) as e:
                await store.fail_bill(c.id, cl.name, listprices.why(e, None), now)
                continue
            # slice 16.4b: the spot part from a read of its own, after the total's; its
            # failure (a shape not as expected, a call refused) costs the spot part only
            spot_error = None
            if (read_spot := spot_reader(c, cl)) is not None:
                try:
                    spot = await asyncio.to_thread(
                        read_spot, http, cl.billing.tag, start, end.isoformat(), now
                    )
                    got = {
                        d: (a, u, e, spot.get(d, 0.0), ram) for d, (a, u, e, _, ram) in got.items()
                    }
                except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
                    spot_error = listprices.why(e, None)
            await store.save_bill(c.id, cl.name, got, now, spot_error)
    return reads


def _settling(billed: list[dict[str, Any]], sampled: list[float], read: float) -> str | None:
    """Why the window is still settling (audit A6a), or None: a day of it was billed for
    fewer RAM GiB-hours than its nodes were sampled for, by more than the tolerance, and
    was read (at `read`, the last good read) less than `SETTLES` after it ended. Not when
    every day is short: that is a real gap (a label missing from nodes), not a bill still
    filling; nor a day read later. Only GCP's export says how many hours a day charged
    for, and GCP's export was seen still filling a day 28 hours after it ended, complete
    30 to 38 hours after (slice 10.5c), where its estimate of a node's memory was 3%
    under what GCP billed: a complete day reads at or above its samples."""
    short = [
        b.get("ram_hours") is not None and b["ram_hours"] < s * (1 - TOLERANCE)
        for b, s in zip(billed, sampled, strict=True)
    ]
    if all(short):
        return None
    fresh = [
        (b["day"], b["ram_hours"], s)
        for b, s, low in zip(billed, sampled, short, strict=True)
        if low
        and read - (datetime.fromisoformat(b["day"]).replace(tzinfo=UTC).timestamp() + DAY)
        < SETTLES
    ]
    if not fresh:
        return None
    said = ", ".join(f"{day} is billed for {ram:.0f} of {s:.0f}" for day, ram, s in fresh)
    return (
        f"still settling: {said} GiB-hours its nodes were sampled for; the next daily read "
        "of the bill has more"
    )


def gap(estimated: float, billed: float) -> float | None:
    """How far the estimate is from the bill, as a fraction of the bill; None when the
    bill is nothing and the estimate isn't (no fraction to speak of)."""
    if billed == 0:
        return 0.0 if estimated == 0 else None
    return (estimated - billed) / billed


async def report(
    store: "Store",
    c: Contract,
    now: float,
    days: int = 7,
    prom: Callable[[usage.Target], usage.Prom] = usage.reader,
) -> list[dict[str, Any]]:
    """Per billed cluster: the period, the bill, the estimate at the Hub's prices, their
    gap and whether it is within TOLERANCE, or `note` saying why there is none. Where a
    cluster names its spot nodes, each hour's spot capacity is priced at a spot node's
    rate and the rest at an on-demand one's (slice 16.1b), and `note` says their share;
    `spot_billed` and `spot_estimated` set what its spot lines cost beside what the
    estimate prices its spot hours at (slice 16.4b), a spot price to check by the bill.
    Money stays in the currency it was read in; nothing is converted."""
    wanted = period(now, days)
    c = await listprices.effective(store, c, now)
    have, tries = await store.bills(c.id)
    targets = {t.cluster: t for t in usage.targets(c)}
    out = []
    for cl in c.clusters:
        if not cl.billing:
            continue
        row: dict[str, Any] = {
            "cluster": cl.name,
            "tag": cl.billing.tag,
            "start": wanted[0],
            "end": wanted[-1],
            "days": days,
            "billed": None,
            "estimated": None,
            "currency": None,
            "gap": None,
            "within": None,
            "hours": days * 24,  # the node-hours a complete estimate rests on
            "sampled": None,
            "coverage": None,
            "note": None,
            # slice 16.4b: what its spot lines cost, and what the estimate prices its spot
            # hours at: a spot price to check against what the cloud charged
            "spot_billed": None,
            "spot_estimated": None,
        }
        out.append(row)
        seen = have.get(cl.name, {})
        missing = [d for d in wanted if d not in seen]
        if missing:
            tried = tries.get(cl.name)
            row["note"] = (
                f"the bill failed to read: {tried['error']}"
                if tried and tried["error"]
                else f"no bill for {len(missing)} of {days} days yet "
                f"(first {missing[0]}): read daily, and the cloud settles a day in 1 to 2"
                if tried
                else "the bill is not read yet: the next hourly run reads it"
            )
            continue
        currencies = {seen[d]["currency"] for d in wanted}
        row["billed"] = sum(seen[d]["amount"] for d in wanted)
        spots = [seen[d].get("spot_amount") for d in wanted]  # None: read before 16.4b
        row["spot_billed"] = sum(spots) if all(x is not None for x in spots) else None
        row["currency"] = currencies.pop() if len(currencies) == 1 else None
        price = c.price(cl.name)
        if price is None:
            row["note"] = "no price for this cluster: its estimate waits for prices or list_prices"
            continue
        money = c.prices.currency if c.prices else "USD"
        if row["currency"] != money:
            row["note"] = (
                f"the bill is in {row['currency']} and the prices in {money}: not converted"
            )
            continue
        t = targets[cl.name]
        first = datetime.fromisoformat(wanted[0]).replace(tzinfo=UTC).timestamp()
        last = datetime.fromisoformat(wanted[-1]).replace(tzinfo=UTC).timestamp() + DAY - 1
        try:
            vcpu, gib, sampled, daily = await asyncio.to_thread(
                usage.node_hours, prom(t), t, first, last
            )
            spot_cpu, spot_gib = (
                await asyncio.to_thread(usage.spot_hours, prom(t), t, cl.spot.label, first, last)
                if cl.spot
                else (0.0, 0.0)
            )
            # slice 17.2c: a GPU node's GPUs, so its price adds back whole; not on GCP,
            # whose bill read here is the core and RAM lines (its GPUs are lines apart)
            gpus = (
                await asyncio.to_thread(usage.gpu_hours, prom(t), t, first, last)
                if price.gpu is not None and c.bill_provider(cl) != "gcp"
                else 0.0
            )
        except usage.UsageError as e:
            row["note"] = f"the node-hours failed to read: {e}"
            continue
        # the price is blended by the latest mix: undone, then each hour at its own rate
        share, against = c._spot_shares.get(cl.name, (1.0, 1.0))
        on_spot = spot_cpu * price.cpu + spot_gib * price.memory
        row["estimated"] = (vcpu * price.cpu + gib * price.memory - (1 - against) * on_spot) / share
        # ponytail: one GPU rate for spot and on-demand GPUs, the latest mix's
        row["estimated"] += gpus * (price.gpu or 0.0)
        row["gap"] = gap(row["estimated"], row["billed"])
        row["within"] = row["gap"] is not None and abs(row["gap"]) <= TOLERANCE
        row["sampled"], row["coverage"] = sampled, min(sampled / row["hours"], 1.0)
        if cl.spot:  # the spot hours at a spot node's rate (16.4b)
            row["spot_estimated"] = against * on_spot / share
        notes = []
        if cl.spot is None and row["spot_billed"]:
            notes.append(
                "the bill has spot lines: name the cluster's spot nodes (spot:) to price them "
                "at their own rate"
            )
        elif cl.spot and row["spot_billed"] is None:
            failed = (tries.get(cl.name) or {}).get("spot_error")
            notes.append(
                f"its spot lines failed to read: {failed}"
                if failed
                else "its spot lines come with the next daily read of the bill"
            )
        if cl.spot and spot_cpu:
            notes.append(f"spot: {spot_cpu / vcpu:.0%} of the vCPU-hours, at x{against:.3g}")
        elif cl.spot:
            notes.append(
                f"no node carried {cl.spot.label} in the window: if spot nodes ran, allowlist "
                "it in kube-state-metrics' --metric-labels-allowlist"
            )
        if sampled < row["hours"]:
            notes.append(f"the estimate rests on {sampled} of {row['hours']} sampled hours")
        read = (tries.get(cl.name) or {}).get("ok_at") or now
        billed_days, sampled_days = [seen[d] for d in wanted], [daily.get(d, 0.0) for d in wanted]
        if settling := _settling(billed_days, sampled_days, read):
            notes.append(settling)
        row["note"] = "; ".join(notes) or None
        if row["coverage"] < MIN_COVERAGE or settling:
            row["within"] = None  # missing samples, or bill lines, not prices: no verdict
    return out
