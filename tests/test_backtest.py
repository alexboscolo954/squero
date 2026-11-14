"""The Backtest (slice 14.4): a Weekly profile checked against the newest stored week, which
a profile built from the weeks before it never saw; its error as replica-hours too few and
too many, a budget of 20%, and a Workload over it left with its Floor alone."""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from squero import contracts, sizing, usage, webapp
from squero.config import Settings
from squero.contracts import Contract
from squero.store import Store, connect
from squero.usage import NEEDS_FOR, Bounds
from tests.fake_prometheus import FakePrometheus
from tests.test_floors import API, KEY, limits, priced, row
from tests.test_needs import hpa as needs_hpa
from tests.test_needs import metric
from tests.test_profile import DAY, NOW, WEEKS, H, busy, floor_with, stored
from tests.test_sizing_store import PROM, prometheus_contract
from tests.test_usage import _prom, cpu, deployment, mem
from tests.test_webapp import VIEW, app_dir, hub  # noqa: F401

MI = 2**20
WEEK = 168 * H
MONDAY = NOW - 2 * DAY  # of the newest week: NOW is a Wednesday, 00:00 UTC


def peak(since: int) -> Callable[[datetime], float]:
    """6.2 for three hours of Monday from `since`, 1 the rest of the week."""
    return lambda t: 6.2 if t.weekday() == 0 and since <= t.hour < since + 3 else 1.0


def weeks(before: Callable[[datetime], float], newest: Callable[[datetime], float]) -> Any:
    """Three weeks of `before`, then the newest week of `newest`."""
    return stored(before, WEEKS, last=NOW - WEEK - H) | stored(newest, 168)


def verdict(needs: dict[float, float], zone: str = "UTC", now: float = NOW) -> dict[str, Any]:
    return sizing.backtest(needs, 2, zone, now)


# --- the verdict -----------------------------------------------------------------------


def test_a_week_like_the_ones_before_it_is_no_error() -> None:
    assert verdict(stored(hours=4 * 168)) == {
        "state": "within",
        "error": 0.0,
        "few": 0.0,
        "many": 0.0,
        "hours": 168,
        "replicas": 0.0,
    }


def test_the_error_is_the_replica_hours_too_few_and_too_many_of_those_needed() -> None:
    """The Monday peak came an hour late: the profile held 7 where 2 were needed in its
    first hour and 2 where 7 were in the hour after its last. Needed: 165 hours at the
    Floor of 2 and 3 at 7."""
    assert verdict(weeks(peak(9), peak(10))) == {
        "state": "within",
        "error": 10 / 351,
        "few": 5 / 351,
        "many": 5 / 351,
        "hours": 168,
        "replicas": 5 / 168,
    }


def test_a_need_under_the_floor_needs_the_floor() -> None:
    """What the HPA would run with the Floor as its minimum: an idle hour needs 2, so a
    profile at its Floor is right, and only what it holds above the Floor is too many."""
    idle = verdict(weeks(lambda t: 0.0, lambda t: 0.0))
    assert (idle["error"], idle["hours"]) == (0.0, 168)
    gone = verdict(weeks(peak(9), lambda t: 0.0))  # the peak never came: 3 hours at 7 for 2
    assert (gone["few"], gone["many"], gone["state"]) == (0.0, 15 / 336, "within")


def test_the_newest_week_plays_no_part_in_the_profile_it_is_replayed_against() -> None:
    """Monday 09:00 needed 1, 6 and 6 in the weeks before: a median of 6. With the newest
    week's 1 in it the profile would hold 4, and look half as wrong."""
    needs = stored(lambda t: 1.0, 4 * 168)
    needs |= {MONDAY + 9 * H - 7 * DAY: 6.0, MONDAY + 9 * H - 14 * DAY: 6.0}
    got = verdict(needs)
    assert (got["few"], got["many"], got["replicas"]) == (0.0, 4 / 336, 4 / 168)


def flat(before: float, newest: float) -> dict[str, Any]:
    return verdict(weeks(lambda t: before, lambda t: newest))


def test_an_hour_off_by_under_a_tenth_of_what_it_needed_counts_for_nothing() -> None:
    """The HPA's own tolerance, both ways; with whole replicas only from 11 up."""
    assert flat(11.0, 12.0)["error"] == 0.0  # 1 too few of 12
    assert flat(12.0, 11.0)["error"] == 0.0  # 1 too many of 11
    assert (flat(9.0, 10.0)["few"], flat(9.0, 10.0)["many"]) == (0.1, 0.0)  # a tenth counts
    assert (flat(10.0, 9.0)["few"], flat(10.0, 9.0)["many"]) == (0.0, 1 / 9)
    assert flat(27.0, 30.0)["few"] == 0.1  # 3 of 30, exactly a tenth
    assert flat(11.0, 10.0)["many"] == 0.1  # a tenth of the 10 needed, not of the 11 held


def test_the_budget_is_a_fifth_and_an_error_of_exactly_that_is_within_it() -> None:
    assert (flat(4.0, 5.0)["error"], flat(4.0, 5.0)["state"]) == (0.2, "within")
    assert (flat(3.0, 5.0)["error"], flat(3.0, 5.0)["state"]) == (0.4, "over")
    assert flat(5.0, 3.0)["state"] == "over"  # too many counts like too few


# --- what it needs ---------------------------------------------------------------------


def test_a_backtest_needs_three_weeks_before_the_newest_and_half_of_that_one() -> None:
    assert verdict(stored(hours=4 * 168 - 1)) == {"state": "waiting"}  # 503 hours before it
    before = stored(hours=WEEKS, last=NOW - WEEK - H)
    assert verdict(before | stored(hours=83)) == {"state": "waiting"}
    half = verdict(before | stored(hours=84))
    # the hours never stored are skipped: replayed as idle, Wednesday's office hours
    # would be too many
    assert (half["state"], half["hours"], half["error"]) == ("within", 84, 0.0)
    late = verdict(stored(peak(9), WEEKS, last=NOW - WEEK - H) | stored(peak(10), 84))
    assert (late["many"], late["replicas"]) == (5 / 183, 5 / 84)  # over the hours replayed
    old = stored(hours=WEEKS, last=NOW - NEEDS_FOR - H)  # no longer kept
    assert verdict(old | stored(hours=168)) == {"state": "waiting"}


def test_the_hour_under_way_is_not_replayed() -> None:
    needs = weeks(peak(9), peak(10)) | {NOW: 99.0}
    assert verdict(needs, now=NOW + 1800) == verdict(weeks(peak(9), peak(10)))


def test_the_hours_are_those_of_the_contracts_time_zone() -> None:
    """A peak on Monday from 09:00 in Rome, over the change to summer time (29 March
    2026): the same hours there every week, an hour earlier in UTC from then on."""
    rome = ZoneInfo("Europe/Rome")

    def need(t: datetime) -> float:
        return peak(9)(t.astimezone(rome))

    now = datetime(2026, 4, 8, tzinfo=UTC).timestamp()
    needs = stored(need, 4 * 168, last=now - H)
    assert verdict(needs, "Europe/Rome", now)["error"] == 0.0
    assert verdict(needs, "UTC", now)["error"] == 10 / 351


def test_the_floors_backtest_goes_by_the_contracts_time_zone() -> None:
    rome = ZoneInfo("Europe/Rome")
    now = datetime(2026, 4, 8, tzinfo=UTC).timestamp()
    needs = stored(lambda t: peak(9)(t.astimezone(rome)), 4 * 168, last=now - H)
    data = prometheus_contract().model_dump()
    data["sizing"] = {"timezone": "Europe/Rome"}
    bounds = {"test": {API: [Bounds("api", 2.0, 9.0)]}}
    found = sizing.floors(contracts.build(data), [row()], {"test": {KEY: needs}}, bounds, now)
    assert found["test"][0]["profile"]["backtest"]["error"] == 0.0


# --- on the Floor ----------------------------------------------------------------------

OFFICE = weeks(lambda t: 6.2 if t.weekday() < 5 and 8 <= t.hour < 18 else 1.0, lambda t: 1.0)


def test_a_profile_within_the_budget_carries_its_backtest_and_what_the_too_many_cost() -> None:
    f = floor_with("api", weeks(peak(9), peak(10)))
    p = f["profile"]
    assert p["state"] == "ready" and len(p["blocks"]) == 1 and "note" in p
    assert p["backtest"] == {
        **verdict(weeks(peak(9), peak(10))),
        # 5 replica-hours of 168, each at its Sizings: 100m and 128 MiB
        "excess": {"cpu": pytest.approx(5 / 168 * 0.1), "memory": 5 / 168 * 128 * MI},
    }
    assert sizing.profile_lines(f) == [
        "  Weekly profile (UTC, each rise 15 min early):",
        "    Mon 08:45 to Mon 12:00: 7 replicas",
        "    backtest: off by 2% last week (too few 1%, too many 1%: 0.00 cores, 0.00 GiB)",
        "    an HPA has no schedule of its own: something else has to hold these replicas "
        "(KEDA's cron trigger does)",
    ]
    steady = floor_with("keda-hpa-api")
    assert steady["profile"]["backtest"]["excess"] == {"cpu": 0.0, "memory": 0.0}
    assert "triggers" in steady["profile"]
    assert (
        sizing.profile_lines(steady)[8]
        == "    backtest: off by 0% last week (too few 0%, too many 0%)"
    )


def test_over_the_budget_the_workload_keeps_only_its_floor() -> None:
    """Office hours for three weeks, then a week of nothing: nothing of the profile is
    left on the stored Floor for any view to show, KEDA's triggers least of all."""
    f = floor_with("keda-hpa-api", OFFICE)
    p = f["profile"]
    assert (f["state"], f["floor"], p["state"], p["blocks"]) == ("ready", 2, "irregular", [])
    assert "triggers" not in p and "note" not in p
    assert (p["backtest"]["state"], p["backtest"]["few"]) == ("over", 0.0)
    assert p["backtest"]["error"] == p["backtest"]["many"] == 250 / 336  # 50 hours at 7 for 2
    lines = sizing.profile_lines(f, "EUR")
    assert lines == [
        "  load isn't weekly enough: off by 74% last week (too few 0%, too many 74%); "
        "it keeps its Floor"
    ]
    assert "note" not in floor_with("api", OFFICE)["profile"]


def test_under_four_weeks_the_profile_shows_as_not_backtested_yet() -> None:
    f = floor_with("api", stored(peak(9)))  # three weeks
    assert f["profile"]["backtest"] == {"state": "waiting"}
    assert sizing.profile_lines(f)[1:3] == [
        "    Mon 08:45 to Mon 12:00: 7 replicas",
        "    not backtested yet (needs four weeks)",
    ]
    assert "backtest" not in floor_with("api", stored(hours=100))["profile"]  # learning
    assert "backtest" not in floor_with("api", stored(lambda t: 1.3, 4 * 168))["profile"]  # flat


def test_the_too_many_are_priced_like_a_saving_and_stay_out_of_the_total() -> None:
    f = floor_with("api", weeks(peak(9), peak(10)))
    got = sizing.floors_report([f], priced())
    (shown,) = got["floors"]
    cost = 5 / 168 * (0.1 * 0.04 + 128 * MI / 2**30 * 0.005) * 730
    assert shown["profile"]["backtest"]["cost"] == pytest.approx(cost)
    assert got["floors_total"]["workloads"] == 0 and got["floors_total"]["cost"] == 0.0
    assert "cost" not in f["profile"]["backtest"]  # the stored Floor is left as it was
    assert sizing.profile_lines(shown, "EUR")[2] == (
        f"    backtest: off by 2% last week (too few 1%, too many 1%: {cost:,.2f} EUR/month)"
    )
    plain = sizing.floors_report([f], prometheus_contract())["floors"][0]
    assert "cost" not in plain["profile"]["backtest"]
    over = sizing.floors_report([floor_with("api", OFFICE)], priced())["floors"][0]
    assert over["profile"]["backtest"]["cost"] > 0


def test_the_workloads_route_gives_each_profiles_backtest(
    settings: Settings,
    fake_cluster: Any,
    database_url: str,
    app_dir: Path,  # noqa: F811
) -> None:
    late = floor_with("api", weeks(peak(9), peak(10)))
    odd = {**floor_with("keda-hpa-api", OFFICE), "workload": "test/shop/Deployment/web"}
    new = {**floor_with("api", stored(peak(9))), "workload": "test/shop/Deployment/young"}

    async def seed() -> None:
        async with connect(database_url) as pool:
            s = Store(pool)
            await s.migrate()
            await s.save_floors("acme", "test", NOW, [late, odd, new])

    asyncio.run(seed())
    with hub(settings, fake_cluster, database_url) as c:
        body = c.get("/api/v1/contracts/acme/workloads", headers=VIEW).json()
    api, web, young = (f["profile"] for f in body["floors"])
    assert api["backtest"] == {**late["profile"]["backtest"], "cost": None}
    assert (web["state"], web["blocks"], web["triggers"]) == ("irregular", [], None)
    assert web["backtest"]["state"] == "over" and web["backtest"]["hours"] == 168
    waiting = {"state": "waiting", "error": None, "few": None, "many": None, "hours": None}
    assert young["backtest"] == waiting | {"replicas": None, "excess": None, "cost": None}
    old = {**late, "profile": {k: v for k, v in late["profile"].items() if k != "backtest"}}
    assert webapp.Floor.model_validate({**old, "at": NOW}).profile.backtest is None  # of 14.2


def test_each_demo_contract_backtests_against_its_own_needs(
    two_contracts: dict[str, Contract], database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both demo Contracts end to end on one fake Prometheus, an HPA of the same name in
    every namespace of each: the first's load follows the week, the second's stopped
    last week. Each run's verdict goes by its own Contract's stored Needs."""
    fake = FakePrometheus()
    for c in two_contracts.values():
        for cl in c.clusters:
            for ns in cl.namespaces:
                (p,) = deployment(fake, "api", pods=1, ns=ns)
                cpu(fake, p, [0.1] * 100, ns=ns)
                mem(fake, p, [80.0 * MI] * 100, ns=ns)
                needs_hpa(fake, [1.0], ns=ns)
                metric(fake, [50.0], [10.0], ns=ns)
                limits(fake, 4.0, 9.0, ns=ns)
    monkeypatch.setattr(usage, "Prom", lambda t: _prom(fake, t))
    loads = [weeks(busy, busy), OFFICE]

    async def go() -> dict[str, set[tuple[str, str]]]:
        async with connect(database_url) as pool:
            s = Store(pool)
            await s.migrate()
            out = {}
            for i, c in enumerate(two_contracts.values()):
                c = c.model_copy(update={"integrations": [*c.integrations, PROM]})
                for cl in c.clusters:
                    needs = {(ns, "Deployment", "api"): loads[i] for ns in cl.namespaces}
                    await s.save_needs(c.id, cl.name, needs)
                assert await sizing.store_run(s, c, NOW) is not None
                found = await s.latest_floors(c.id, {cl.name for cl in c.clusters})
                assert len(found) == sum(len(cl.namespaces) for cl in c.clusters)
                out[c.id] = {
                    (f["profile"]["state"], f["profile"]["backtest"]["state"]) for f in found
                }
            return out

    found = asyncio.run(go())
    assert list(found.values()) == [{("ready", "within")}, {("irregular", "over")}]
