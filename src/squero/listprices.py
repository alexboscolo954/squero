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

from squero.contracts import Cluster, ClusterPrices, Contract, ListPrices, Prices

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
) -> dict[str, float | None]:
    """Linux, pay-as-you-go (no Spot, no Low Priority) price per hour in `currency`;
    the cheapest meter when a size has several."""
    types = " or ".join(f"armSkuName eq '{t}'" for t in sorted(wanted))
    url: str | None = AZURE
    params: dict[str, str] | None = {
        "currencyCode": currency,
        "$filter": f"serviceName eq 'Virtual Machines' and armRegionName eq '{region}' "
        f"and priceType eq 'Consumption' and ({types})",
    }
    best: dict[str, float] = {}
    while url:
        r = http.get(url, params=params)
        r.raise_for_status()
        body = r.json()
        for item in body.get("Items", []):
            names = " ".join(item.get(k, "") for k in ("productName", "skuName", "meterName"))
            if (
                "Windows" in names
                or "Spot" in names
                or "Low Priority" in names
                or item.get("unitOfMeasure") != "1 Hour"
                or not item.get("retailPrice")
            ):
                continue
            t = item["armSkuName"]
            best[t] = min(best.get(t, float("inf")), float(item["retailPrice"]))
        url, params = body.get("NextPageLink"), None
    return {t: best.get(t) for t in wanted}


def gcp(
    http: httpx.Client, region: str, wanted: Wanted, key: str, currency: str = "USD"
) -> dict[str, float | None]:
    """Compute Engine bills a machine family's cores and RAM separately: a machine
    type's hourly price is its vCPU x the family's core price + its GiB x the RAM
    price (on demand, not custom, not Spot), in `currency`."""
    core: dict[str, float] = {}
    ram: dict[str, float] = {}
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
            if (
                sku.get("category", {}).get("usageType") != "OnDemand"
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
            (core if rest.startswith("Core") else ram)[family.lower()] = price
        token = body.get("nextPageToken") or None
    out: dict[str, float | None] = {}
    for t, (vcpu, gib) in wanted.items():
        family = t.split("-")[0]
        out[t] = (
            vcpu * core[family] + gib * ram[family] if family in core and family in ram else None
        )
    return out


def fetch(
    http: httpx.Client,
    provider: str,
    region: str,
    wanted: Wanted,
    gcp_key: str | None,
    currency: str = "USD",
) -> dict[str, float | None]:
    for name in (region, *wanted):
        if not SAFE.fullmatch(name):
            raise ValueError(f"not a region or instance type name: {name[:40]!r}")
    if provider == "aws":
        return aws(http, region, wanted)
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
    for (currency, provider, region), wanted in sorted(wanted_by_group(facts).items()):
        have = stored[currency] = stored.get(currency) or await store.list_prices(currency)
        due: Wanted = {}
        for t, capacity in wanted.items():
            row = have.get((provider, region, t))
            fresh = row and row["ok_at"] is not None and now - row["ok_at"] < DAY
            waiting = row and row["error"] and now - row["tried_at"] < HOUR
            if not (fresh or waiting):
                due[t] = capacity
        if not due:
            continue
        looked += len(due)
        try:
            found = await asyncio.to_thread(
                fetch, http, provider, region, due, keys.get("gcp"), currency
            )
            error = None
        except (httpx.HTTPError, ValueError, KeyError, ijson.JSONError) as e:
            found, error = {}, why(e, keys.get("gcp"))
        for t in due:
            price = found.get(t)
            what = error or (None if price else "not in the price list")
            await store.put_list_price((provider, region, t), price, what, now, currency)
    return looked


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
            f", no node carries {label} as kube-state-metrics shows it: allowlist it in its "
            "--metric-labels-allowlist, or every node is priced on-demand"
        )
    return True, f", spot x{cluster.spot.discount:g} on {count} of {len(facts['nodes'])} nodes"


def _typed(
    cluster: Cluster, typed: ClusterPrices, facts: dict[str, Any] | None, weight: float
) -> tuple[ClusterPrices, str, tuple[float, float] | None]:
    """A typed price with the cluster's spot nodes at their discount (slice 16.1a): read
    as the on-demand rate, times what its nodes cost as a share of it (also returned;
    None where spot isn't priced)."""
    priced, said = _spot_of(cluster, facts)
    if not priced or cluster.spot is None or facts is None:
        return typed, "typed prices" + said, None
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
) -> tuple[dict[str, ClusterPrices], list[dict[str, str]]]:
    """Each cluster's prices (typed, else from the list) and, per cluster, where its
    price came from or why it has none; in `shares`, each cluster's whose spot nodes
    were priced, its price as a share of its nodes all on-demand, and a spot node's
    against an on-demand one (slice 16.1a): a deal (`discount`) is an on-demand node's
    only, so a spot node pays its own discount of the list price and no more."""
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
        got = _from_list(c, cluster, nodes.get(cluster.name), rows, now)
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
) -> tuple[ClusterPrices, str, tuple[float, float] | None] | str:
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
    for n in facts["nodes"]:
        row = rows.get((n["provider"], n["region"], n["type"]))
        if row is None or row["hour_price"] is None:
            why = f" ({row['error']})" if row and row["error"] else " (looked up daily)"
            return f"no list price yet for {n['provider']} {n['type']} in {n['region']}{why}"
        on_spot = priced and cluster.spot is not None and n.get("spot")
        total += row["hour_price"] * (cluster.spot.discount if on_spot and cluster.spot else deal)
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
    note = f"list price of {made} (1 vCPU = {w:g} GiB{off}), read {age:.0f} h ago{old}{said}"
    against = cluster.spot.discount / deal if cluster.spot else 1.0
    share = (total / on_demand, against) if priced and on_demand else None
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
