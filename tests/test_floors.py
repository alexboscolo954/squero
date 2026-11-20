"""Floors (slice 14.1b): the minimum replicas the Hub computes for a Workload with an
autoscaler, from its stored Needs and its HPA's minimum and maximum; its saving and risks;
stored per cluster and shown by `sizing show`, the API, the Hub MCP and the digest."""

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from squero import contracts, sizing, usage
from squero.config import Settings
from squero.contracts import BUILTIN, Contract
from squero.store import Store, connect
from squero.usage import HPA, NEEDS_FOR, Bounds, Target, UsageError, Workload
from tests.fake_prometheus import FakePrometheus
from tests.test_needs import hpa as needs_hpa
from tests.test_needs import metric
from tests.test_sizing_policy import lab, run_show
from tests.test_sizing_store import PROM, prometheus_contract, store, stored  # noqa: F401
from tests.test_sizing_store import row as sizing_row
from tests.test_usage import _prom, cpu, deployment, mem
from tests.test_webapp import VIEW, app_dir, hub  # noqa: F401

DAY = 86_400.0
H = 3600.0
MI = 2**20
GI = 2**30
NOW = 1000 * DAY
SHOP = Target("test", "http://prometheus", None, {}, ["shop"])
API = Workload("shop", "Deployment", "api")
KEY = ("shop", "Deployment", "api")


def row(
    container: str = "app",
    policy: str = "production",
    state: str = "ready",
    cpu_target: float = 0.1,
    memory_target: float = 128.0 * MI,
    asks: dict[str, float] | None = None,
    name: str = "api",
    cluster: str = "test",
) -> dict[str, Any]:
    """A stored row of one container: asking 1 core and 1 GiB, sized 100m and 128 MiB."""
    asks = {"cpu": 1.0, "memory": float(GI)} if asks is None else asks
    where = {"cluster": cluster, "namespace": "shop", "kind": "Deployment", "name": name}
    r: dict[str, Any] = {"workload": f"{cluster}/shop/Deployment/{name}/{container}", **where}
    r |= {"container": container, "policy": policy, "state": state, "requests": asks}
    if state == "ready":
        sized = {"action": "lower", "basis": 0.0}
        r["cpu"] = {**sized, "current": asks.get("cpu"), "target": cpu_target}
        r["memory"] = {**sized, "current": asks.get("memory"), "target": memory_target}
    return r


def hourly(values: list[float], last: float = NOW - H) -> dict[float, float]:
    """A Need for each of the complete hours that end with the one starting at `last`."""
    return {last - i * H: v for i, v in enumerate(reversed(values))}


def floor_of(
    values: list[float] | dict[float, float],
    minimum: float | None = 2.0,
    maximum: float | None = 5.0,
    rows: list[dict[str, Any]] | None = None,
    c: Contract | None = None,
    hpas: list[Bounds] | None = None,
) -> dict[str, Any]:
    needs = {"test": {KEY: values if isinstance(values, dict) else hourly(values)}}
    bounds = {"test": {API: hpas or [Bounds("api", minimum, maximum)]}}
    found = sizing.floors(
        c or prometheus_contract(), [row()] if rows is None else rows, needs, bounds, NOW
    )
    (f,) = found["test"]
    return f


# --- the policy setting ----------------------------------------------------------------


def test_the_floors_percentile_is_a_policy_setting() -> None:
    """10% under production and batch, 25% under high-availability; yours changes its
    base's, between 0 and 0.5."""
    got = {name: p.replicas.percentile for name, p in BUILTIN.items()}
    assert got == {"production": 0.10, "high-availability": 0.25, "batch": 0.10}
    data = prometheus_contract().model_dump()
    policies = {"steady": {"base": "high-availability", "replicas": {"percentile": 0.4}}}
    data["sizing"] = {"policies": policies | {"plain": {"base": "high-availability"}}}
    c = contracts.build(data)
    assert c.sizing.policies["steady"].replicas.percentile == 0.4
    assert c.sizing.policies["steady"].history == 72 * H  # the rest is its base's
    assert c.sizing.policies["plain"].replicas.percentile == 0.25
    for bad in (0.51, -0.1):
        policies["steady"]["replicas"] = {"percentile": bad}
        with pytest.raises(ValidationError):
            contracts.build(data)


# --- the Floor -------------------------------------------------------------------------


def test_a_floor_is_a_low_percentile_of_the_hourly_needs_rounded_up() -> None:
    """A hundred hours, ten of them at 2.2 and the rest at 5: the 10th percentile is 2.2,
    so 3 replicas. Against a minimum of 4 it would hold one replica fewer in those ten
    hours: a tenth of a replica on average, at the pod's Sizings."""
    f = floor_of([5.0] * 90 + [2.2] * 10, minimum=4.0, maximum=9.0)
    assert f == {
        "workload": "test/shop/Deployment/api",
        "cluster": "test",
        "namespace": "shop",
        "kind": "Deployment",
        "name": "api",
        "hpa": "api",
        "policy": "production",
        "percentile": 0.10,
        "minimum": 4.0,
        "maximum": 9.0,
        "risks": [],
        "history_h": 100.0,
        "needs_h": 24.0,
        "state": "ready",
        "floor": 3,
        "replicas": 0.1,
        "saving": {"cpu": pytest.approx(0.01), "memory": pytest.approx(12.8 * MI)},
        "profile": {  # slice 14.2: three weeks of Needs before one
            "timezone": "UTC",
            "lead_time": 900,
            "history_h": 100.0,
            "needs_h": 504.0,
            "state": "learning",
        },
    }
    assert floor_of([5.0] * 91 + [2.2] * 9, minimum=4.0)["floor"] == 5  # the 10th is a 5
    assert floor_of([3.0000000001] * 30, minimum=4.0)["floor"] == 3  # not 4 for a rounding


def test_a_floor_is_never_below_2_unless_the_minimum_already_is() -> None:
    low = floor_of([0.5] * 30, minimum=4.0)
    assert low["floor"] == 2 and low["replicas"] == 2.0  # 4 less the Floor, every hour
    one = floor_of([0.5] * 30, minimum=1.0)
    assert one["floor"] == 1 and "saving" not in one and one["risks"] == []
    assert floor_of([1.5] * 30, minimum=1.0)["floor"] == 2


def test_the_saving_counts_only_the_hours_the_minimum_held_replicas_up() -> None:
    """A minimum of 6 and a Floor of 2: an hour needing 1.2 would run 2 (4 fewer), one
    needing 4.5 would run 5 (1 fewer), one needing 8 changes nothing."""
    f = floor_of([1.2] * 10 + [4.5] * 10 + [8.0] * 10, minimum=6.0, maximum=None)
    assert f["floor"] == 2 and f["replicas"] == round((40 + 10 + 0) / 30, 2)
    assert f["saving"]["cpu"] == pytest.approx(50 / 30 * 0.1)


def test_the_saving_is_at_the_sizings_and_at_the_request_where_there_is_none() -> None:
    """A sized container at its Sizing; a learning and an excluded one at what they ask:
    the pod a removed replica would have been, so it adds to Waste."""
    rows = [
        row(cpu_target=0.1, memory_target=128.0 * MI),
        row("young", state="learning", asks={"cpu": 0.2, "memory": 64.0 * MI}),
        row("proxy", policy="off", state="excluded", asks={"cpu": 0.05}),
    ]
    rows.append(row(cluster="other", cpu_target=5.0))  # the same Workload, another cluster
    f = floor_of([1.0] * 30, minimum=4.0, rows=rows)
    assert f["replicas"] == 2.0
    assert f["saving"] == {"cpu": pytest.approx(2 * 0.35), "memory": pytest.approx(2 * 192 * MI)}


def test_a_minimum_below_the_floor_is_a_risk_not_a_saving() -> None:
    f = floor_of([5.0] * 80 + [2.0] * 20, minimum=2.0, maximum=9.0)
    assert f["floor"] == 2 and f["risks"] == [] and "saving" not in f  # at its Floor
    f = floor_of([5.0] * 95 + [2.0] * 5, minimum=2.0, maximum=9.0)
    assert f["floor"] == 5 and "saving" not in f and "replicas" not in f
    assert f["risks"] == [
        {
            "kind": "minimum_below_floor",
            "text": "its minimum of 2 is below the 5 replicas it needed in 95% of hours",
        }
    ]


def test_a_need_above_the_maximum_is_a_risk_with_its_share_of_hours() -> None:
    f = floor_of([3.0] * 94 + [7.5] * 6, minimum=3.0, maximum=5.0)
    assert f["risks"] == [
        {"kind": "needed_above_maximum", "text": "needed more than its maximum of 5 in 6% of hours"}
    ]
    assert floor_of([3.0] * 94 + [5.0] * 6, minimum=3.0, maximum=5.0)["risks"] == []  # at it
    assert floor_of([3.0] * 94 + [7.5] * 6, minimum=3.0, maximum=None)["risks"] == []
    long = [row(policy="long")]
    rare = floor_of([3.0] * 299 + [7.5], minimum=3.0, maximum=5.0, rows=long, c=weeks())
    assert rare["risks"][0]["text"] == "needed more than its maximum of 5 in under 1% of hours"
    both = floor_of([7.5] * 30, minimum=3.0, maximum=5.0)
    assert [r["kind"] for r in both["risks"]] == ["minimum_below_floor", "needed_above_maximum"]


def weeks(history: str = "24h") -> Contract:
    """A Contract whose default policy looks back 90 days."""
    data = prometheus_contract().model_dump()
    policy = {"base": "production", "window": "90d", "history": history}
    data["sizing"] = {"default": "long", "policies": {"long": policy}}
    return contracts.build(data)


# --- its history -----------------------------------------------------------------------


def test_under_the_policys_history_a_floor_is_learning() -> None:
    """23 complete hours of 24; the hour under way (its peak is partial) and an hour
    before the policy's window count for nothing, nor does an hour never stored."""
    f = floor_of([3.0] * 23, minimum=4.0)
    assert f["state"] == "learning" and (f["history_h"], f["needs_h"]) == (23.0, 24.0)
    assert "floor" not in f and "saving" not in f and f["risks"] == []
    assert floor_of([3.0] * 24, minimum=4.0)["state"] == "ready"
    for extra in (NOW - H / 2, NOW - 7 * DAY - H):  # under way; a week and an hour ago
        f = floor_of(hourly([3.0] * 23) | {extra: 0.4}, minimum=4.0)
        assert f["state"] == "learning" and f["history_h"] == 23.0
    edge = floor_of(hourly([3.0] * 23) | {NOW - 7 * DAY: 0.4}, minimum=4.0)
    assert edge["state"] == "ready" and edge["history_h"] == 24.0  # the window's first hour
    holes = {NOW - (2 * i + 1) * H: 3.0 for i in range(23)}  # every other hour, 46 hours
    assert floor_of(holes, minimum=4.0)["history_h"] == 23.0


def test_the_policy_is_the_most_cautious_of_the_workloads_containers() -> None:
    """The highest percentile among its containers' policies, with that policy's window
    and history; `off` for every container, or no container read, gives no Floor."""
    data = prometheus_contract().model_dump()
    data["sizing"] = {"rules": [{"container": "db", "policy": "high-availability"}]}
    c = contracts.build(data)
    values = [5.0] * 60 + [3.0] * 20 + [1.5] * 20  # the 10th is 1.5, the 25th is 3
    assert floor_of(values, minimum=6.0)["floor"] == 2
    rows = [row(), row("db", policy="high-availability")]
    f = floor_of(values, minimum=6.0, rows=rows, c=c)
    assert (f["floor"], f["policy"], f["percentile"], f["needs_h"]) == (
        3,
        "high-availability",
        0.25,
        72.0,
    )
    assert floor_of([3.0] * 71, minimum=6.0, rows=rows, c=c)["state"] == "learning"
    far = floor_of(hourly([3.0] * 72) | {NOW - 13 * DAY: 0.4}, minimum=6.0, rows=rows, c=c)
    assert far["history_h"] == 73.0  # its window is 14 days
    off = [row(policy="off", state="excluded")]
    bounds = {"test": {API: [Bounds("api", 6.0, 9.0)]}}
    needs = {"test": {KEY: hourly(values)}}
    assert sizing.floors(c, off, needs, bounds, NOW) == {"test": []}
    assert sizing.floors(c, [row(name="web")], needs, bounds, NOW) == {"test": []}


def test_a_floor_asks_for_no_more_history_than_half_of_what_is_kept() -> None:
    """Needs are kept 8 weeks: a policy needing 60 days of a 90-day window gets its Floor
    on 4 weeks, over the 8 weeks there are."""
    c, long = weeks("60d"), [row(policy="long")]
    f = floor_of([3.0] * 671, minimum=4.0, rows=long, c=c)
    assert f["state"] == "learning" and f["needs_h"] == 672.0
    assert floor_of([3.0] * 672, minimum=4.0, rows=long, c=c)["state"] == "ready"
    old = floor_of(hourly([3.0] * 672) | {NOW - NEEDS_FOR - H: 0.4}, minimum=4.0, rows=long, c=c)
    assert old["history_h"] == 672.0  # past the 8 weeks: not counted even were it there


# --- no Floor --------------------------------------------------------------------------


def test_two_hpas_an_unknown_minimum_or_scaling_to_zero_give_no_floor() -> None:
    values = [3.0] * 30
    two = floor_of(values, hpas=[Bounds("a", 2.0, 5.0), Bounds("b", 4.0, 9.0)])
    assert (two["state"], two["why"], two["hpa"]) == ("none", "2 HPAs scale it", "a")
    lost = floor_of(values, minimum=None)
    assert (lost["state"], lost["why"]) == ("none", "its HPA's minimum is not known")
    zero = floor_of(values, minimum=0.0)
    assert (zero["state"], zero["why"]) == ("none", "it scales to zero: left alone")
    # KEDA's HPA keeps a minimum of 1 while KEDA scales to zero: its replicas at 0 say so
    keda = floor_of(values, hpas=[Bounds("api", 1.0, 5.0, zero=True)])
    assert (keda["state"], keda["why"]) == ("none", "it scales to zero: left alone")
    for f in (two, lost, zero, keda):
        assert "floor" not in f and "saving" not in f and f["risks"] == []


def test_an_idle_workload_held_at_its_minimum_gets_its_floor() -> None:
    """A Need of 0 is not scaling to zero: two replicas with nothing to do (an empty
    queue, CPU at 0%) need none, and that is where a Floor saves most."""
    fake = FakePrometheus()
    needs_hpa(fake, [4.0] * 121)
    metric(fake, [10.0] * 121, [0.0] * 121, name="queue", kind="average")
    found = usage.needs(_prom(fake, SHOP), SHOP, NOW - 2 * H, NOW)
    assert found[API] == {NOW - 2 * H: 0.0, NOW - H: 0.0, NOW: 0.0}
    f = floor_of([0.0] * 30, minimum=4.0)
    assert (f["state"], f["floor"], f["replicas"]) == ("ready", 2, 2.0)
    mixed = floor_of([3.0] * 20 + [0.0] * 10, minimum=1.0)
    assert (mixed["state"], mixed["floor"]) == ("ready", 1)  # as low as its minimum


def test_only_the_clusters_read_get_floors_from_their_own_needs() -> None:
    """A cluster whose HPAs this run read gets its list, empty without one; another
    cluster's or Workload's Needs are never this one's."""
    c = prometheus_contract()
    elsewhere = {
        "other": {KEY: hourly([3.0] * 30)},
        "test": {("shop", "Deployment", "web"): hourly([3.0] * 30)},
    }
    bounds = {"test": {API: [Bounds("api", 4.0, 9.0)]}, "edge": {}}
    found = sizing.floors(c, [row(), row(cluster="other")], elsewhere, bounds, NOW)
    assert set(found) == {"test", "edge"} and found["edge"] == []
    (f,) = found["test"]
    assert f["state"] == "learning" and f["history_h"] == 0.0


# --- shown -----------------------------------------------------------------------------


def priced(cid: str = "acme") -> Contract:
    data = prometheus_contract(cid).model_dump()
    data["prices"] = {"currency": "EUR", "cpu": 0.04, "memory": 0.005}
    return contracts.build(data)


def test_stored_floors_are_priced_and_totalled() -> None:
    saver = floor_of([1.0] * 30, minimum=4.0)
    at_it = floor_of([3.0] * 30, minimum=3.0)
    below = floor_of([5.0] * 30, minimum=2.0, maximum=9.0)
    got = sizing.floors_report([saver, at_it, below], priced())
    cost = (0.2 * 0.04 + 2 * 128 * MI / GI * 0.005) * 730
    assert got["floors"][0]["cost"] == pytest.approx(cost)
    assert "cost" not in got["floors"][1] and "cost" not in got["floors"][2]
    assert got["floors_total"] == {
        "workloads": 1,
        "replicas": 2.0,
        "saving": saver["saving"],
        "gpus": 0.0,  # slice 17.4b: none of their replicas holds a GPU
        "fixed": 0,  # and each has an HPA
        "cost": pytest.approx(cost),
    }
    plain = sizing.floors_report([saver, at_it, below], prometheus_contract())
    assert "cost" not in plain["floors"][0] and "cost" not in plain["floors_total"]
    assert "cost" not in saver  # the stored Floor is left as it was


def test_a_floor_reads_as_one_line() -> None:
    saver = floor_of([1.0] * 30, minimum=4.0)
    head = "test/shop/Deployment/api: "
    line = "Floor 2 (HPA api: minimum 4, maximum 5; production: 10% of 30 h of Needs)"
    assert (
        sizing.floor_text(saver)
        == f"{head}{line} | 2 replicas fewer on average: 0.20 cores, 0.25 GiB"
    )
    (cost,) = sizing.floors_report([saver], priced())["floors"]
    assert sizing.floor_text(cost, "EUR").endswith("| 2 replicas fewer on average: 6.75 EUR/month")
    one = floor_of([1.0] * 30, minimum=3.0)
    assert sizing.floor_text(one).endswith("| 1 replica fewer on average: 0.10 cores, 0.12 GiB")
    below = floor_of([7.5] * 30, minimum=3.0, maximum=None)
    assert sizing.floor_text(below) == (
        f"{head}Floor 8 (HPA api: minimum 3; production: 10% of 30 h of Needs) | risk: its "
        "minimum of 3 is below the 8 replicas it needed in 100% of hours"
    )
    young = floor_of([3.0] * 5)
    assert sizing.floor_text(young) == f"{head}Floor learning (5 h of 24 h of Needs)"
    assert (
        sizing.floor_text(floor_of([3.0] * 30, minimum=0.0))
        == f"{head}no Floor: it scales to zero: left alone"
    )


def test_the_digest_has_one_line_on_floors_when_there_is_something_to_say() -> None:
    saver = floor_of([1.0] * 30, minimum=4.0)
    below = floor_of([5.0] * 30, minimum=2.0, maximum=9.0)
    quiet = [floor_of([3.0] * 30, minimum=3.0), floor_of([3.0] * 5)]
    c = priced()
    report = sizing.report([], {"test": {"at": NOW, "error": None, "since": None}}, c)
    lines = sizing.digest_text(
        "acme", report, report, None, None, sizing.floors_report([saver, below], c)
    )
    assert (
        "Floors: 1 can lower their HPA minimum (2.0 replicas fewer on average, 6.75 EUR/month); "
        "1 with a minimum below their Floor." in lines.splitlines()
    )
    only = sizing.floors_line(sizing.floors_report([saver], prometheus_contract()), None)
    worth = "(2.0 replicas fewer on average, 0.20 cores, 0.25 GiB)"
    assert only == f"Floors: 1 can lower their HPA minimum {worth}."
    assert (
        sizing.floors_line(sizing.floors_report([below], c), "EUR")
        == "Floors: 1 with a minimum below their Floor."
    )
    assert sizing.floors_line(sizing.floors_report(quiet, c), "EUR") is None
    assert "Floors" not in sizing.digest_text(
        "acme", report, report, None, None, sizing.floors_report(quiet, c)
    )
    assert "Floors" not in sizing.digest_text("acme", report, report)


# --- the read --------------------------------------------------------------------------


def limits(
    fake: FakePrometheus,
    low: float | None,
    high: float | None,
    name: str = "api",
    ns: str = "shop",
    **extra: str,
) -> None:
    who = {"namespace": ns, "horizontalpodautoscaler": name, **extra}
    if low is not None:
        fake.add(f"{HPA}_spec_min_replicas", low, **who)
    if high is not None:
        fake.add(f"{HPA}_spec_max_replicas", high, **who)


def test_each_hpas_minimum_and_maximum_are_read_per_workload() -> None:
    fake = FakePrometheus()
    needs_hpa(fake, name="b", of="api")
    limits(fake, 4.0, 9.0, name="b")
    needs_hpa(fake, name="a", of="api")
    limits(fake, 2.0, None, name="a")
    needs_hpa(fake, name="db-scaler", of="db", kind="StatefulSet")
    limits(fake, 1.0, 3.0, name="orphan")  # no info series: no Workload to give it to
    needs_hpa(fake, name="theirs", of="api", ns="bank")
    limits(fake, 7.0, 8.0, name="theirs", ns="bank")
    replicas = f"{HPA}_status_current_replicas"
    fake.add(replicas, 0.0, namespace="shop", horizontalpodautoscaler="a")  # its least
    fake.add(replicas, 1.0, namespace="shop", horizontalpodautoscaler="b")
    fake.add(replicas, 0.0, namespace="bank", horizontalpodautoscaler="b")  # bank's b
    assert usage.bounds(_prom(fake, SHOP), SHOP, NOW, 7 * 86400) == {
        API: [Bounds("a", 2.0, None, zero=True), Bounds("b", 4.0, 9.0)],
        Workload("shop", "StatefulSet", "db"): [Bounds("db-scaler", None, None)],
    }
    (least,) = [q for q in fake.queries if replicas in q]
    assert least.startswith("min_over_time(") and least.endswith("[604800s])")
    assert all('namespace=~"shop"' in q for q in fake.queries)
    assert not [q for q in fake.queries if "last_over_time" in q]  # as they are now


def test_bounds_without_honor_labels_and_without_an_hpa() -> None:
    t = Target("test", "http://prometheus", None, {}, ["shop"], ksm_namespace="exported_namespace")
    fake = FakePrometheus()
    assert usage.bounds(_prom(fake, t), t, NOW, 3600) == {} and len(fake.queries) == 1
    own = {"ns": "monitoring", "exported_namespace": "shop"}
    needs_hpa(fake, **own)
    limits(fake, 2.0, 5.0, **own)
    limits(fake, 7.0, 8.0, ns="shop", exported_namespace="bank")  # bank's, scraped from shop
    fake.add(
        f"{HPA}_status_current_replicas",
        0.0,
        namespace="monitoring",
        **{"exported_namespace": "shop", "horizontalpodautoscaler": "api"},
    )
    assert usage.bounds(_prom(fake, t), t, NOW, 3600) == {API: [Bounds("api", 2.0, 5.0, True)]}


def scaled_fake(hours: int = 0, current: float = 100.0) -> FakePrometheus:
    """The lab's Workloads; `api` under an HPA from 2 to 5 with 3 replicas at `current`
    percent against 50: it needed 6 all along."""
    fake = FakePrometheus()
    lab(fake)
    needs_hpa(fake, [3.0] * hours if hours else [3.0], there=[1.0] * hours if hours else None)
    metric(fake, [50.0] * hours if hours else [50.0], [current] * hours if hours else [current])
    limits(fake, 2.0, 5.0)
    return fake


def test_a_failed_read_of_the_bounds_stops_nothing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fake = scaled_fake()
    monkeypatch.setattr(usage, "Prom", lambda t: _prom(fake, t))
    c = prometheus_contract()
    assert usage.collect(c, [86400], NOW).bounds == {"test": {API: [Bounds("api", 2.0, 5.0)]}}
    usage.collect(c, [86400, 90 * 86400], NOW)  # the longest window, 8 weeks at most
    least = [q.rsplit("[", 1)[1] for q in fake.queries if q.startswith("min_over_time(")]
    assert least == ["86400s])", f"{NEEDS_FOR}s])"]

    def bounds(*args: Any) -> Any:
        raise UsageError("Prometheus error 503: busy")

    monkeypatch.setattr(usage, "bounds", bounds)
    with caplog.at_level(logging.WARNING):
        found = usage.collect(c, [86400], NOW)
    assert found.clusters == {"test": None} and found.bounds == {} and found.scaled["test"]
    assert "acme/test: its HPAs' bounds not read: Prometheus error 503" in caplog.text


def test_show_prints_each_autoscaled_workloads_floor(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`sizing show` has no database: it reads the Needs Prometheus still has."""
    monkeypatch.setattr("time.time", lambda: NOW)
    out = run_show(monkeypatch, capsys, scaled_fake(int(NEEDS_FOR / 540) + 1))
    assert (
        "test/shop/Deployment/api: Floor 6 (HPA api: minimum 2, maximum 5; production: 10% of "
        "168 h of Needs) | risk: its minimum of 2 is below the 6 replicas it needed in 100% of "
        "hours; needed more than its maximum of 5 in 100% of hours" in out.splitlines()
    )
    assert "  no weekly pattern: its Floor holds all week" in out.splitlines()  # slice 14.2
    assert len([line for line in out.splitlines() if "Floor " in line]) == 2
    fake = scaled_fake()
    as_json = run_show(monkeypatch, capsys, fake, as_json=True)
    assert "Floor" not in as_json and not [q for q in fake.queries if "status_target" in q]


# --- stored ----------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_clusters_floors_are_replaced_by_each_run(store: Store) -> None:  # noqa: F811
    a, b = floor_of([1.0] * 30, minimum=4.0), floor_of([3.0] * 5)
    await store.save_floors("acme", "test", NOW, [a, b])
    await store.save_floors(
        "acme", "edge", NOW + H, [{**a, "workload": "edge/shop/Deployment/api"}]
    )
    await store.save_floors("bank", "test", NOW, [{**a, "floor": 9}])
    got = await store.latest_floors("acme", {"test", "edge"})
    assert [(f["workload"], f["at"]) for f in got] == [
        ("edge/shop/Deployment/api", NOW + H),
        ("test/shop/Deployment/api", NOW),
        ("test/shop/Deployment/api", NOW),
    ]
    assert got[1] == {**a, "at": NOW}
    assert await store.latest_floors("acme", {"edge"}) == [got[0]]  # a cluster gone: not shown
    await store.save_floors("acme", "test", NOW + 2 * H, [])  # its HPAs gone since
    assert await store.latest_floors("acme", {"test"}) == []
    assert [f["floor"] for f in await store.latest_floors("bank", {"test"})] == [9]
    assert await store.latest_floors("nobody", {"test"}) == []


@pytest.mark.anyio
async def test_a_run_stores_the_floors_of_the_hpas_it_read(
    store: Store,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """From the Needs stored, this run's included (1.8 in its hour); the next run
    without the HPA takes its Floor away."""
    fake = scaled_fake(current=30.0)
    monkeypatch.setattr(usage, "Prom", lambda t: _prom(fake, t))
    c = prometheus_contract()
    await store.save_needs(c.id, "test", {KEY: hourly([1.2] * 30)})
    await store.save_needs(c.id, "edge", {KEY: hourly([9.0] * 30)})  # another cluster's
    assert await sizing.store_run(store, c, NOW) is not None
    (f,) = await store.latest_floors(c.id, {"test"})
    assert (f["state"], f["floor"], f["minimum"], f["maximum"], f["at"]) == (
        "ready",
        2,
        2.0,
        5.0,
        NOW,
    )
    assert f["history_h"] == 30.0 and f["risks"] == [] and "saving" not in f
    fake.series = [s for s in fake.series if not s["__name__"].startswith(HPA)]
    assert await sizing.store_run(store, c, NOW + H) is not None
    assert await store.latest_floors(c.id, {"test"}) == []
    monkeypatch.setattr(usage, "bounds", lambda *a: (_ for _ in ()).throw(UsageError("down")))
    await store.save_floors(c.id, "test", NOW + H, [f])
    assert await sizing.store_run(store, c, NOW + 2 * H) is not None
    assert len(await store.latest_floors(c.id, {"test"})) == 1  # not read: the last kept


@pytest.mark.anyio
async def test_the_stored_digest_carries_the_contracts_own_floors(store: Store) -> None:  # noqa: F811
    c = prometheus_contract()
    await stored(store, c.id, NOW, {"test": None}, sizing_row())
    assert "Floors" not in await sizing.digest(store, c, NOW + 1, None)
    await store.save_floors("bank", "test", NOW, [floor_of([1.0] * 30, minimum=4.0)])
    assert "Floors" not in await sizing.digest(store, c, NOW + 1, None)
    await store.save_floors(c.id, "test", NOW, [floor_of([1.0] * 30, minimum=4.0)])
    text = await sizing.digest(store, c, NOW + 1, None)
    worth = "(2.0 replicas fewer on average, 0.20 cores, 0.25 GiB)"
    assert f"Floors: 1 can lower their HPA minimum {worth}." in text.splitlines()


def test_the_workloads_route_gives_the_floors_and_their_total(
    settings: Settings,
    fake_cluster: Any,
    database_url: str,
    app_dir: Path,  # noqa: F811
) -> None:
    saver = floor_of([1.0] * 30, minimum=4.0)
    young = {**floor_of([3.0] * 5), "workload": "test/shop/Deployment/web", "name": "web"}

    async def seed() -> None:
        async with connect(database_url) as pool:
            s = Store(pool)
            await s.migrate()
            await s.save_floors("acme", "test", NOW, [saver, young])
            await s.save_floors("bank", "test", NOW, [{**saver, "floor": 9}])

    asyncio.run(seed())
    with hub(settings, fake_cluster, database_url) as c:
        body = c.get("/api/v1/contracts/acme/workloads", headers=VIEW).json()
    assert [(f["name"], f["state"], f["floor"]) for f in body["floors"]] == [
        ("api", "ready", 2),
        ("web", "learning", None),
    ]
    assert body["floors"][0]["saving"] == {"cpu": pytest.approx(0.2), "memory": 256 * MI}
    assert body["floors"][0]["at"] == NOW
    assert body["floors_total"]["workloads"] == 1 and body["floors_total"]["replicas"] == 2.0


def test_each_demo_contract_stores_only_its_own_floors(
    two_contracts: dict[str, Contract], database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both demo Contracts end to end on one fake Prometheus, an HPA of the same name in
    every namespace of each, with different minimums: a run stores the Floors of its own
    Contract's namespaces, from its own Needs."""
    fake = FakePrometheus()
    for i, c in enumerate(two_contracts.values()):
        for cl in c.clusters:
            for ns in cl.namespaces:
                (p,) = deployment(fake, "api", pods=1, ns=ns)
                cpu(fake, p, [0.1] * 100, ns=ns)
                mem(fake, p, [80.0 * MI] * 100, ns=ns)
                needs_hpa(fake, [3.0], ns=ns)
                metric(fake, [50.0], [10.0], ns=ns)  # 0.6 now: under what is stored
                limits(fake, 4.0 + i, 9.0, ns=ns)
    monkeypatch.setattr(usage, "Prom", lambda t: _prom(fake, t))

    async def go() -> dict[str, list[tuple[str, float, float]]]:
        async with connect(database_url) as pool:
            s = Store(pool)
            await s.migrate()
            out = {}
            for i, c in enumerate(two_contracts.values()):
                c = c.model_copy(update={"integrations": [*c.integrations, PROM]})
                for cl in c.clusters:
                    needs = {
                        (ns, "Deployment", "api"): hourly([1.0 + i] * 30) for ns in cl.namespaces
                    }
                    await s.save_needs(c.id, cl.name, needs)
                assert await sizing.store_run(s, c, NOW) is not None
                found = await s.latest_floors(c.id, {cl.name for cl in c.clusters})
                out[c.id] = [(f["namespace"], f["minimum"], f["replicas"]) for f in found]
            return out

    found = asyncio.run(go())
    for i, (cid, c) in enumerate(two_contracts.items()):
        spaces = sorted(ns for cl in c.clusters for ns in cl.namespaces)
        assert sorted(found[cid]) == [(ns, 4.0 + i, 2.0 + i) for ns in spaces]
