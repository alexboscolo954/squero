"""Cloud list prices (slice 10.4): a cluster with no typed prices is priced from what
its nodes cost on demand.

The Hub reads each cluster's nodes (usage.nodes, every hourly run), looks up each
(provider, region, instance type) in the cloud's public price list once a day, and
turns the node prices into one CPU price and one GiB price per cluster: the nodes'
total price over their total vCPU and GiB, with one vCPU worth `weight` GiB, so the two
prices add back up to the nodes' price exactly. Typed prices always win; a cluster the
list can't price says why and the Contract stays unpriced rather than half in money.
"""

import re
import tempfile
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

import httpx
import ijson  # type: ignore[import-untyped]

from squero.contracts import Aws, Cluster, ClusterPrices, Contract, ListPrices, Prices

if TYPE_CHECKING:
    from squero.store import Store

DAY = 86400.0
HOUR = 3600.0
STALE = 3 * DAY  # a price older than this is still used, and said to be old
AWS = (
    "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/current/{region}/index.json"
)
AZURE = "https://prices.azure.com/api/retail/prices"
GCP = "https://cloudbilling.googleapis.com/v1/services/6F81-5844-456A/skus"  # Compute Engine

# A region and an instance type come from a cluster's labels, so from its owner: only
# plain names may reach a URL path or an OData filter.
SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")

Key = tuple[str, str, str]  # provider, region, instance type
Wanted = dict[str, tuple[float, float]]  # instance type -> (vCPU, GiB) of a node of it


def client() -> httpx.Client:
    """The clouds' price lists are large and slow: a long timeout."""
    return httpx.Client(timeout=300, follow_redirects=True)


def aws(http: httpx.Client, region: str, wanted: Wanted) -> dict[str, float | None]:
    """Linux, shared-tenancy, on-demand USD per hour (AWS is USD only). The region's offer file is
    hundreds of MB: streamed to disk once, then read twice (products, then terms)."""
    with tempfile.TemporaryFile() as f:
        with http.stream("GET", AWS.format(region=region)) as r:
            r.raise_for_status()
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
        f.seek(0)
        sku_type = {}
        for sku, product in ijson.kvitems(f, "products"):
            a = product.get("attributes", {})
            if (
                a.get("instanceType") in wanted
                and a.get("operatingSystem") == "Linux"
                and a.get("tenancy") == "Shared"
                and a.get("preInstalledSw") == "NA"
                and a.get("capacitystatus") == "Used"
                and a.get("operation") == "RunInstances"
            ):
                sku_type[sku] = a["instanceType"]
        f.seek(0)
        out: dict[str, float | None] = {}
        for sku, terms in ijson.kvitems(f, "terms.OnDemand"):
            if sku not in sku_type:
                continue
            for term in terms.values():
                for dim in term["priceDimensions"].values():
                    usd = dim.get("pricePerUnit", {}).get("USD")
                    if dim.get("unit") == "Hrs" and usd is not None and float(usd) > 0:
                        out[sku_type[sku]] = float(usd)
    return {t: out.get(t) for t in wanted}


def azure(
    http: httpx.Client, region: str, wanted: Wanted, currency: str = "USD"
) -> tuple[dict[str, float | None], dict[str, float | None]]:
    """Linux, pay-as-you-go (no Low Priority) price per hour in `currency`, on demand
    and, from the same items, Spot (a meter named "... Spot", slice 16.4a); the cheapest
    meter when a size has several."""
    types = " or ".join(f"armSkuName eq '{t}'" for t in sorted(wanted))
    url: str | None = AZURE
    params: dict[str, str] | None = {
        "currencyCode": currency,
        "$filter": f"serviceName eq 'Virtual Machines' and armRegionName eq '{region}' "
        f"and priceType eq 'Consumption' and ({types})",
    }
    best: dict[str, float] = {}
    spot: dict[str, float] = {}
    while url:
        r = http.get(url, params=params)
        r.raise_for_status()
        body = r.json()
        for item in body.get("Items", []):
            names = " ".join(item.get(k, "") for k in ("productName", "skuName", "meterName"))
            if (
                "Windows" in names
                or "Low Priority" in names
                or item.get("unitOfMeasure") != "1 Hour"
                or not item.get("retailPrice")
            ):
                continue
            t, into = item["armSkuName"], spot if "Spot" in names else best
            into[t] = min(into.get(t, float("inf")), float(item["retailPrice"]))
        url, params = body.get("NextPageLink"), None
    return {t: best.get(t) for t in wanted}, {t: spot.get(t) for t in wanted}


def gcp(
    http: httpx.Client, region: str, wanted: Wanted, key: str, currency: str = "USD"
) -> tuple[dict[str, float | None], dict[str, float | None]]:
    """Compute Engine bills a machine family's cores and RAM separately: a machine
    type's hourly price is its vCPU x the family's core price + its GiB x the RAM
    price (not custom), in `currency`: on demand, and from the same pages Spot
    (`Preemptible` SKUs, "Spot Preemptible N2 Instance Core ...", a legacy "Preemptible"
    one only where no Spot one is; slice 16.4a)."""
    core: dict[str, float] = {}
    ram: dict[str, float] = {}
    spot_core: dict[str, tuple[bool, float]] = {}  # family -> (a Spot SKU's, its price)
    spot_ram: dict[str, tuple[bool, float]] = {}
    token: str | None = ""
    while token is not None:
        r = http.get(
            GCP,
            params={"pageSize": "5000", "pageToken": token, "currencyCode": currency},
            headers={"X-Goog-Api-Key": key},  # not in the URL: errors and logs show URLs
        )
        r.raise_for_status()
        body = r.json()
        for sku in body.get("skus", []):
            desc = sku.get("description", "")
            usage = sku.get("category", {}).get("usageType")
            if (
                usage not in ("OnDemand", "Preemptible")
                or region not in sku.get("serviceRegions", [])
                or " Instance " not in desc
            ):
                continue
            family, _, rest = desc.partition(" Instance ")
            if not rest.startswith(("Core running", "Ram running")):
                continue
            rate = sku["pricingInfo"][0]["pricingExpression"]["tieredRates"][-1]["unitPrice"]
            if rate.get("currencyCode", currency) != currency:  # never a price in the wrong money
                raise ValueError(f"the catalog answered in {rate['currencyCode']}, not {currency}")
            price = float(rate.get("units", 0)) + rate.get("nanos", 0) / 1e9
            if usage == "OnDemand":
                (core if rest.startswith("Core") else ram)[family.lower()] = price
                continue
            named = family.startswith("Spot Preemptible ")
            name = family.removeprefix("Spot Preemptible ").removeprefix("Preemptible ").lower()
            into = spot_core if rest.startswith("Core") else spot_ram
            if named or name not in into or not into[name][0]:  # a Spot SKU wins
                into[name] = (named, price)
        token = body.get("nextPageToken") or None
    out: dict[str, float | None] = {}
    spot: dict[str, float | None] = {}
    for t, (vcpu, gib) in wanted.items():
        family = t.split("-")[0]
        out[t] = (
            vcpu * core[family] + gib * ram[family] if family in core and family in ram else None
        )
        spot[t] = (
            vcpu * spot_core[family][1] + gib * spot_ram[family][1]
            if family in spot_core and family in spot_ram
            else None
        )
    return out, spot


def fetch(
    http: httpx.Client,
    provider: str,
    region: str,
    wanted: Wanted,
    gcp_key: str | None,
    currency: str = "USD",
) -> tuple[dict[str, float | None], dict[str, float | None]]:
    """A group's on-demand prices and, where the same read gives them, its spot ones
    (Azure's and GCP's; AWS's need a Contract's credentials: `aws_spot`)."""
    for name in (region, *wanted):
        if not SAFE.fullmatch(name):
            raise ValueError(f"not a region or instance type name: {name[:40]!r}")
    if provider == "aws":
        return aws(http, region, wanted), {}
    if provider == "azure":
        return azure(http, region, wanted, currency)
    if provider == "gcp":
        if not gcp_key:
            raise ValueError("no GCP pricing key: set list_prices.gcp_key_secret")
        return gcp(http, region, wanted, gcp_key, currency)
    raise ValueError(f"no price list for provider {provider!r}")


def wanted_by_group(
    nodes: Iterable[tuple[Contract, dict[str, Any]]],
) -> dict[tuple[str, str, str], Wanted]:
    """(currency, provider, region) -> the instance types the clusters run, with a
    node's capacity; a cluster with a node that can't be priced asks for nothing."""
    out: dict[tuple[str, str, str], Wanted] = {}
    for c, facts in nodes:
        if facts.get("problem"):
            continue
        assert c.list_prices is not None
        for n in facts["nodes"]:
            group = (c.list_prices.currency, n["provider"], n["region"])
            out.setdefault(group, {})[n["type"]] = (n["vcpu"], n["gib"])
    return out


def why(e: Exception, key: str | None) -> str:
    """A failed lookup in a line: an HTTP error by its status, never its text (that
    names the URL), anything else by its message with the GCP key blanked out."""
    if isinstance(e, httpx.HTTPStatusError):
        return f"HTTP {e.response.status_code} from {e.request.url.host}"
    text = str(e)[:200].replace(key, "***") if key else str(e)[:200]
    return f"{type(e).__name__}: {text}"


async def refresh(
    store: "Store", contracts: Iterable[Contract], http: httpx.Client, now: float
) -> int:
    """Look up every list price the Contracts with `list_prices` need and don't have
    from the last day (a failed one is tried again after an hour); the lookups made.
    A failure keeps the last good price and its age."""
    import asyncio

    facts: list[tuple[Contract, dict[str, Any]]] = []
    keys: dict[str, str | None] = {}  # provider -> a GCP key, from any Contract with one
    for c in contracts:
        if not c.list_prices:
            continue
        if c.list_prices.gcp_key_secret:
            keys.setdefault("gcp", c.secret(c.list_prices.gcp_key_secret).get_secret_value())
        facts += [(c, f) for f in (await store.latest_nodes(c.id)).values()]
    looked = 0
    stored: dict[str, dict[Key, dict[str, Any]]] = {}
    spotted: dict[str, dict[Key, dict[str, Any]]] = {}  # the shared spot rows (16.4a)
    for (currency, provider, region), wanted in sorted(wanted_by_group(facts).items()):
        have = stored[currency] = stored.get(currency) or await store.list_prices(currency)
        spots = spotted[currency] = spotted.get(currency) or await store.spot_prices(currency, "")
        due: Wanted = {}
        for t, capacity in wanted.items():
            rows_ = [have.get((provider, region, t))]
            if provider != "aws":  # its spot price comes with the same read (16.4a)
                rows_.append(spots.get((provider, region, t)))
            if not all(_current(row, now) for row in rows_):
                due[t] = capacity
        if not due:
            continue
        looked += len(due)
        try:
            found, spot = await asyncio.to_thread(
                fetch, http, provider, region, due, keys.get("gcp"), currency
            )
            error = None
        except (httpx.HTTPError, ValueError, KeyError, ijson.JSONError) as e:
            (found, spot), error = ({}, {}), why(e, keys.get("gcp"))
        for t in due:
            price = found.get(t)
            what = error or (None if price else "not in the price list")
            await store.put_list_price((provider, region, t), price, what, now, currency)
            if provider != "aws":
                said = error or (None if spot.get(t) else NO_SPOT[0])
                await store.put_spot_price((provider, region, t), spot.get(t), said, now, currency)
    for c in contracts:  # slice 16.4a: AWS's spot prices, each Contract with its own key
        looked += await _aws_spot(store, c, http, now)
    return looked


NO_SPOT = ("no spot price in the list", "no spot price in its history")  # read, and none


def _current(row: dict[str, Any] | None, now: float, refused_waits: bool = False) -> bool:
    """A price row read in the last day, or one that failed in the last hour. A size the
    cloud has no spot price for (`NO_SPOT`) was read all the same: it waits the day, so
    its group isn't read again every hour (slice 16.4a); with `refused_waits` (AWS's
    spot reads, the client's key), a permission refused waits the day too, rather than
    filling the client's audit trail with a denied call an hour (the Operator's choice)."""
    fresh = row and row["ok_at"] is not None and now - row["ok_at"] < DAY
    error = (row or {}).get("error") or ""
    refused = refused_waits and error.startswith(("HTTP 401", "HTTP 403"))
    wait = DAY if error in NO_SPOT or refused else HOUR
    waiting = row and error and now - row["tried_at"] < wait
    return bool(fresh or waiting)


async def _aws_spot(store: "Store", c: Contract, http: httpx.Client, now: float) -> int:
    """The AWS spot prices of a Contract's spot nodes on list prices (slice 16.4a), read
    with its own `aws` integration's key and kept for it alone (in USD: a Contract on AWS
    prices in USD, its validation says). A region outside the integration's `regions` is
    refused, as every call of it is. The lookups made."""
    import asyncio

    aws_ = next((i for i in c.integrations if isinstance(i, Aws)), None)
    if not c.list_prices or aws_ is None:
        return 0
    wanted: dict[str, set[str]] = {}  # region -> the instance types of its spot nodes
    for cl in c.clusters:
        facts = (await store.latest_nodes(c.id)).get(cl.name)
        if (
            cl.spot is None
            or cl.prices
            or not facts
            or facts.get("problem")  # as wanted_by_group: no node of it to ask for
            or facts.get("spot") != cl.spot.label
        ):
            continue
        for n in facts["nodes"]:
            if n.get("spot") and n["provider"] == "aws":
                wanted.setdefault(n["region"], set()).add(n["type"])
    have = await store.spot_prices("USD", c.id)
    looked = 0
    for region, types in sorted(wanted.items()):
        # AWS's spot rows are only ever a Contract's own: none is shared
        due = sorted(
            t for t in types if not _current(have.get(("aws", region, t)), now, refused_waits=True)
        )
        if not due:
            continue
        looked += len(due)
        try:
            if not SAFE.fullmatch(region) or any(not SAFE.fullmatch(t) for t in due):
                raise ValueError(f"not a region or instance type name: {region[:40]!r}")
            if region not in aws_.regions:
                raise ValueError(f"{region} is not among the aws integration's regions")
            secret = c.secret(aws_.secret_access_key_secret).get_secret_value()
            found = await asyncio.to_thread(
                aws_spot, http, region, due, aws_.access_key_id, secret, now
            )
            error = None
        except (httpx.HTTPError, ValueError, KeyError) as e:
            found, error = {}, why(e, None)
        for t in due:
            said = error or (None if found.get(t) else NO_SPOT[1])
            await store.put_spot_price(("aws", region, t), found.get(t), said, now, "USD", c.id)
    return looked


EC2 = "ec2.{region}.amazonaws.com"


def aws_spot(
    http: httpx.Client, region: str, types: list[str], key_id: str, secret: str, now: float
) -> dict[str, float | None]:
    """Each type's Linux spot price in a region (slice 16.4a), USD an hour, from EC2's
    `DescribeSpotPriceHistory` over the last day (it also gives the price in effect at
    its start): averaged in each zone, then across the zones, so a zone whose price
    moved often weighs no more than one whose didn't. Read with a pattern, not an XML
    parser: a few known elements of a reply from AWS."""
    from datetime import UTC, datetime
    from statistics import mean
    from urllib.parse import urlencode

    from squero.servers.aws import authorization

    host = EC2.format(region=region)
    start = datetime.fromtimestamp(now - DAY, UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    by: dict[str, dict[str, list[float]]] = {}  # type -> zone -> its prices
    token = ""
    while True:
        form = {"Action": "DescribeSpotPriceHistory", "Version": "2016-11-15",
                "StartTime": start, "ProductDescription.1": "Linux/UNIX"}  # fmt: skip
        form |= {f"InstanceType.{i}": t for i, t in enumerate(types, 1)}
        form |= {"NextToken": token} if token else {}
        data = urlencode(sorted(form.items())).encode()
        amz = datetime.fromtimestamp(now, UTC).strftime("%Y%m%dT%H%M%SZ")
        headers = {"content-type": "application/x-www-form-urlencoded; charset=utf-8",
                   "host": host, "x-amz-date": amz}  # fmt: skip
        auth = authorization("POST", host, "/", "", headers, data, region, "ec2", key_id,
                             secret, amz)  # fmt: skip
        r = http.post(f"https://{host}/", content=data, headers={**headers, "authorization": auth})
        r.raise_for_status()
        for item in re.findall(r"<item>(.*?)</item>", r.text, re.S):
            f = dict(re.findall(r"<(\w+)>([^<]*)</\1>", item))
            if f.get("instanceType") in types and f.get("spotPrice"):
                zone = by.setdefault(f["instanceType"], {}).setdefault(
                    f.get("availabilityZone", ""), []
                )
                zone.append(float(f["spotPrice"]))
        found = re.search(r"<nextToken>([^<]+)</nextToken>", r.text)
        token = found.group(1) if found else ""
        if not token:
            break
    return {t: mean(mean(z) for z in by[t].values()) if by.get(t) else None for t in types}


def spot_paid(
    sizes: Iterable[tuple[float, float, bool]], weight: float, discount: float
) -> tuple[float, int]:
    """What a cluster's nodes cost as a share of their on-demand price (slice 16.1a),
    from each node's (vCPU, GiB, spot): its size (`weight` GiB a vCPU, as a price is
    split) at the discount where spot; and how many are spot. 1.0 with none."""
    total = paid = 0.0
    spot = 0
    for vcpu, gib, on_spot in sizes:
        size = weight * vcpu + gib
        total += size
        paid += size * (discount if on_spot else 1.0)
        spot += on_spot
    return (paid / total if total else 1.0), spot


def _spot_of(cluster: Cluster, facts: dict[str, Any] | None) -> tuple[bool, str]:
    """Whether a cluster's read of its nodes can price its spot ones (slice 16.1a), and
    what its price note says of them."""
    if cluster.spot is None:
        return False, ""
    label = cluster.spot.label
    if facts is not None and not facts["nodes"] and facts.get("problem"):
        return False, f", spot not priced: {facts['problem']}"
    if facts is None or facts.get("spot") != label:
        return False, f", spot not priced yet: the next hourly run reads {label}"
    count = sum(bool(n.get("spot")) for n in facts["nodes"])
    if not count:
        return False, (
            f", no node carries {label} as kube-state-metrics shows it: none is on spot now, "
            "or the label isn't in its --metric-labels-allowlist; every node is priced on-demand"
        )
    return True, f", spot x{cluster.spot.discount:g} on {count} of {len(facts['nodes'])} nodes"


def _typed(
    cluster: Cluster, typed: ClusterPrices, facts: dict[str, Any] | None, weight: float
) -> tuple[ClusterPrices, str, tuple[float, float] | None]:
    """A typed price with the cluster's spot nodes at their discount (slice 16.1a): read
    as the on-demand rate, times what its nodes cost as a share of it (also returned,
    with a spot node's rate against an on-demand one's; 1.0 where none was priced, so the
    Bill check can price spot hours of its window, slice 16.1b; None without `spot`)."""
    priced, said = _spot_of(cluster, facts)
    if not priced or cluster.spot is None or facts is None:
        unpriced = (1.0, cluster.spot.discount) if cluster.spot else None
        return typed, "typed prices" + said, unpriced
    sizes = [(n["vcpu"], n["gib"], bool(n.get("spot"))) for n in facts["nodes"]]
    share, _ = spot_paid(sizes, weight, cluster.spot.discount)
    blended = ClusterPrices(cpu=typed.cpu * share, memory=typed.memory * share)
    return blended, f"typed prices{said}: x{share:.3f} of them", (share, cluster.spot.discount)


def resolve(
    c: Contract,
    nodes: dict[str, dict[str, Any]],
    rows: dict[Key, dict[str, Any]],
    now: float,
    shares: dict[str, tuple[float, float]] | None = None,
    spot_rows: dict[Key, dict[str, Any]] | None = None,
) -> tuple[dict[str, ClusterPrices], list[dict[str, str]]]:
    """Each cluster's prices (typed, else from the list) and, per cluster, where its
    price came from or why it has none; in `shares`, each priced cluster's that sets
    `spot`, its price as a share of its nodes all on-demand (1.0 where no spot node was
    priced), and a spot node's against an on-demand one (slices 16.1a and 16.1b): a deal
    (`discount`) is an on-demand node's only, so a spot node pays its own discount of the
    list price and no more."""
    shares = {} if shares is None else shares
    assert c.list_prices is not None
    prices: dict[str, ClusterPrices] = {}
    notes = []
    for cluster in c.clusters:
        if cluster.prices:
            typed, said, share = _typed(
                cluster, cluster.prices, nodes.get(cluster.name), c.list_prices.weight
            )
            prices[cluster.name] = typed
            if share is not None:
                shares[cluster.name] = share
            notes.append({"cluster": cluster.name, "source": "typed", "note": said})
            continue
        got = _from_list(c, cluster, nodes.get(cluster.name), rows, now, spot_rows)
        if isinstance(got, str):
            notes.append({"cluster": cluster.name, "source": "none", "note": got})
        else:
            prices[cluster.name] = got[0]
            notes.append({"cluster": cluster.name, "source": "list", "note": got[1]})
            if got[2] is not None:
                shares[cluster.name] = got[2]
    return prices, notes


def _from_list(
    c: Contract,
    cluster: Cluster,
    facts: dict[str, Any] | None,
    rows: dict[Key, dict[str, Any]],
    now: float,
    spot_rows: dict[Key, dict[str, Any]] | None = None,
) -> tuple[ClusterPrices, str, tuple[float, float] | None] | str:
    """A cluster's prices from its nodes' list prices. A spot node costs its own spot
    list price where the cloud gives one (`spot_rows`, slice 16.4a: no deal on it),
    else its on-demand one at the cluster's discount, node by node; the price note says
    which, and a spot node's rate against an on-demand one is the effective one."""
    assert c.list_prices is not None
    priced, said = _spot_of(cluster, facts)
    deal = c.list_prices.discount  # an on-demand node's only: no deal covers spot capacity
    on_demand = 0.0  # the same nodes all on-demand
    if facts is None:
        return "no list price yet: the next hourly run reads the nodes"
    if facts["problem"]:
        return f"no list price: {facts['problem']}"
    total = vcpu = gib = 0.0
    oldest = now
    shapes: dict[str, int] = {}
    listed = fell = 0  # spot nodes at their spot list price, and at the discount
    spot_paid = spot_worth = 0.0  # what the spot nodes cost, and on demand at the deal
    fallback: list[str] = []  # why a spot node had no spot list price
    for n in facts["nodes"]:
        row = rows.get((n["provider"], n["region"], n["type"]))
        if row is None or row["hour_price"] is None:
            why = f" ({row['error']})" if row and row["error"] else " (looked up daily)"
            return f"no list price yet for {n['provider']} {n['type']} in {n['region']}{why}"
        on_spot = priced and cluster.spot is not None and n.get("spot")
        paid = row["hour_price"] * deal
        if on_spot and cluster.spot is not None:
            spot = (spot_rows or {}).get((n["provider"], n["region"], n["type"]))
            if spot and spot["hour_price"] is not None:
                paid, listed = spot["hour_price"], listed + 1
                oldest = min(oldest, spot["ok_at"])
            else:
                paid, fell = row["hour_price"] * cluster.spot.discount, fell + 1
                fallback.append(
                    spot["error"]
                    if spot and spot["error"]
                    else "no spot list price yet: looked up daily"
                )
            spot_paid += paid
            spot_worth += row["hour_price"] * deal
        total += paid
        on_demand += row["hour_price"] * deal
        vcpu += n["vcpu"]
        gib += n["gib"]
        oldest = min(oldest, row["ok_at"])
        shapes[f"{n['provider']} {n['type']} in {n['region']}"] = (
            shapes.get(f"{n['provider']} {n['type']} in {n['region']}", 0) + 1
        )
    w = c.list_prices.weight
    memory = total / (w * vcpu + gib)
    age = (now - oldest) / HOUR
    old = f", {age / 24:.0f} days old" if now - oldest > STALE else ""
    made = ", ".join(f"{n} x {s}" for s, n in sorted(shapes.items()))
    off = f", discount x{deal:g}" if deal != 1 else ""
    if listed and cluster.spot is not None:  # slice 16.4a: which spot nodes cost what
        of = len(facts["nodes"])
        said = f", spot list on {listed} of {of} nodes"
        said += f", x{cluster.spot.discount:g} on {fell} ({fallback[0]})" if fell else ""
    elif (
        fell
        and cluster.spot is not None
        and fallback[0] != "no spot list price yet: looked up daily"
    ):
        said += f" ({fallback[0]})"
    note = f"list price of {made} (1 vCPU = {w:g} GiB{off}), read {age:.0f} h ago{old}{said}"
    against = cluster.spot.discount / deal if cluster.spot else 1.0
    if spot_worth:  # the spot nodes' effective rate, list prices and discounts as applied
        against = spot_paid / spot_worth
    share = (total / on_demand, against) if cluster.spot and on_demand else None
    return ClusterPrices(cpu=w * memory, memory=memory), note, share


async def effective(store: "Store", c: Contract, now: float) -> Contract:
    """The Contract as its prices stand now: with `list_prices`, every cluster priced
    or the Contract left unpriced (money for some clusters and cores for the others
    can't be ranked), and `price_notes` saying where each price came from."""
    if c._price_notes or not (c.list_prices or any(cl.spot for cl in c.clusters)):
        return c  # already resolved, or nothing to resolve
    shares: dict[str, tuple[float, float]] = {}
    if not c.list_prices:  # typed prices only: its spot clusters' blended (slice 16.1a)
        facts = await store.latest_nodes(c.id)
        clusters, notes = [], []
        for cl in c.clusters:
            typed = c.price(cl.name)
            if cl.spot is None or typed is None:
                clusters.append(cl)
                continue
            blended, note, share = _typed(cl, typed, facts.get(cl.name), ListPrices().weight)
            clusters.append(cl.model_copy(update={"prices": blended}))
            notes.append({"cluster": cl.name, "source": "typed", "note": note})
            if share is not None:
                shares[cl.name] = share
        out = c.model_copy(update={"clusters": clusters})
        out._price_notes, out._spot_shares = notes, shares
        return out
    prices, notes = resolve(
        c,
        await store.latest_nodes(c.id),
        await store.list_prices(c.list_prices.currency),
        now,
        shares,
        await store.spot_prices(c.list_prices.currency, c.id),  # slice 16.4a
    )
    out = c.model_copy()
    if len(prices) == len(c.clusters):
        every = list(prices.values())
        out = c.model_copy(
            update={
                "clusters": [
                    cl.model_copy(update={"prices": prices[cl.name]}) for cl in c.clusters
                ],
                "prices": Prices(
                    currency=c.list_prices.currency,
                    cpu=sum(p.cpu for p in every) / len(every),
                    memory=sum(p.memory for p in every) / len(every),
                ),
            }
        )
    out._price_notes, out._spot_shares = notes, shares
    return out
