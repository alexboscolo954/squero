"""The Weekly profile (slice 14.2): a Workload's Floors by hour of the week, the median of
each hour's Need across the stored weeks, as blocks of hours in the Contract's time zone,
each rise starting early; KEDA cron triggers to paste, a note for a plain HPA."""

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from squero import contracts, sizing, webapp
from squero.config import Settings
from squero.store import Store, connect
from squero.usage import NEEDS_FOR, Bounds
from tests.test_floors import API, KEY, row
from tests.test_sizing_store import prometheus_contract
from tests.test_webapp import VIEW, app_dir, hub  # noqa: F401

DAY = 86_400.0
H = 3600.0
NOW = 1000 * DAY  # Wednesday 1972-09-27 00:00 UTC
WEEKS = 3 * 168


def busy(t: datetime) -> float:
    """Office hours: 2.6 on weekdays from 08:00 to 18:00, 6.2 on Mondays from 09:00 to
    12:00, 1 the rest of the week."""
    if t.weekday() == 0 and 9 <= t.hour < 12:
        return 6.2
    return 2.6 if t.weekday() < 5 and 8 <= t.hour < 18 else 1.0


def stored(
    need: Callable[[datetime], float] = busy, hours: int = WEEKS, last: float = NOW - H
) -> dict[float, float]:
    """A Need for each of the `hours` complete hours ending with the one at `last`."""
    times = [last - i * H for i in range(hours)]
    return {t: need(datetime.fromtimestamp(t, UTC)) for t in times}


def weekly(
    needs: dict[float, float] | None = None, floor: int = 2, zone: str = "UTC", lead: int = 900
) -> dict[str, Any]:
    return sizing.profile(stored() if needs is None else needs, floor, zone, lead, NOW)


def spans(p: dict[str, Any]) -> list[tuple[str, str, int]]:
    return [(b["since"], b["until"], b["replicas"]) for b in p["blocks"]]


# --- the profile -----------------------------------------------------------------------


def test_each_hour_of_the_week_is_its_median_need_in_blocks_above_the_floor() -> None:
    """Three weeks of office hours over a Floor of 2: a block for every run of hours that
    asks the same. One that asks more than the hour before it starts 15 minutes early,
    one that follows a higher one starts on the hour; all end on the hour."""
    p = weekly()
    assert {k: p[k] for k in ("state", "timezone", "lead_time", "history_h", "needs_h")} == {
        "state": "ready",
        "timezone": "UTC",
        "lead_time": 900,
        "history_h": 504.0,
        "needs_h": 504.0,
    }
    assert spans(p) == [
        ("Mon 07:45", "Mon 09:00", 3),
        ("Mon 08:45", "Mon 12:00", 7),
        ("Mon 12:00", "Mon 18:00", 3),
        ("Tue 07:45", "Tue 18:00", 3),
        ("Wed 07:45", "Wed 18:00", 3),
        ("Thu 07:45", "Thu 18:00", 3),
        ("Fri 07:45", "Fri 18:00", 3),
    ]
    crons = [(b["start"], b["end"]) for b in p["blocks"]]
    assert crons[:4] == [
        ("45 7 * * 1", "0 9 * * 1"),
        ("45 8 * * 1", "0 12 * * 1"),
        ("0 12 * * 1", "0 18 * * 1"),
        ("45 7 * * 2", "0 18 * * 2"),
    ]
    assert spans(weekly(lead=0))[:2] == [
        ("Mon 08:00", "Mon 09:00", 3),
        ("Mon 09:00", "Mon 12:00", 7),
    ]
    assert spans(weekly(lead=7200))[0] == ("Mon 06:00", "Mon 09:00", 3)


def test_the_floor_is_the_baseline_and_no_hour_goes_below_it() -> None:
    """A Floor of 3 swallows the weekday blocks: only Monday's peak is above it. An hour
    never stored is at the Floor."""
    assert spans(weekly(floor=3)) == [("Mon 08:45", "Mon 12:00", 7)]
    holes = {
        t: n for t, n in stored(hours=4 * 168).items() if datetime.fromtimestamp(t, UTC).hour != 10
    }
    assert spans(weekly(holes, floor=3)) == [  # Monday's 10:00 was never read
        ("Mon 08:45", "Mon 10:00", 7),
        ("Mon 10:45", "Mon 12:00", 7),
    ]


def test_one_odd_week_changes_nothing() -> None:
    """A Tuesday night at 9 in one week of three: the median of that hour is still 1."""

    def odd(t: datetime) -> float:
        late = t.weekday() == 1 and t.hour == 3 and t.timestamp() > NOW - 7 * DAY
        return 9.0 if late else busy(t)

    assert weekly(stored(odd)) == weekly()
    twice = stored(lambda t: 9.0 if t.weekday() == 1 and t.hour == 3 and t.day != 26 else busy(t))
    assert ("Tue 02:45", "Tue 04:00", 9) in spans(weekly(twice))  # two weeks of three: it counts


def test_an_hour_stored_in_fewer_than_three_weeks_stays_at_the_floor() -> None:
    """Four weeks kept, but Tuesday 03:00 was only read in one of them (a gap in Prometheus
    the other weeks), or in two: one odd week would decide it, so it is at the Floor."""

    def tuesday(t: datetime) -> bool:
        return t.weekday() == 1 and t.hour == 3

    for weeks_read in (1, 2):
        needs = {}
        for t, n in stored(hours=4 * 168).items():
            at = datetime.fromtimestamp(t, UTC)
            if not tuesday(at):
                needs[t] = n
            elif t >= NOW - weeks_read * 7 * DAY:
                needs[t] = 9.0
        assert spans(weekly(needs)) == spans(weekly())
    three = {
        t: 9.0 if tuesday(datetime.fromtimestamp(t, UTC)) and t >= NOW - 21 * DAY else n
        for t, n in stored(hours=4 * 168).items()
    }
    assert ("Tue 02:45", "Tue 04:00", 9) in spans(weekly(three))  # three weeks of four


def test_a_block_can_cross_midnight_and_the_end_of_the_week() -> None:
    """Sunday 22:00 to Monday 02:00, and one from Monday 00:00 whose early start is on
    Sunday: Sunday is cron's day 0."""

    def night(t: datetime) -> float:
        return (
            4.5 if (t.weekday() == 6 and t.hour >= 22) or (t.weekday() == 0 and t.hour < 2) else 1.0
        )

    (b,) = weekly(stored(night))["blocks"]
    assert (b["since"], b["until"], b["replicas"]) == ("Sun 21:45", "Mon 02:00", 5)
    assert (b["start"], b["end"]) == ("45 21 * * 0", "0 2 * * 1")
    (b,) = weekly(stored(lambda t: 4.5 if t.weekday() == 0 and t.hour < 6 else 1.0))["blocks"]
    assert (b["since"], b["start"], b["end"]) == ("Sun 23:45", "45 23 * * 0", "0 6 * * 1")

    def two(t: datetime) -> float:
        return (
            4.5 if (t.weekday() == 0 and t.hour < 6) or (t.weekday() == 1 and t.hour == 8) else 1.0
        )

    listed = [b["since"] for b in weekly(stored(two))["blocks"]]
    assert listed == ["Tue 07:45", "Sun 23:45"]  # by when they start in the week


def test_the_hours_are_those_of_the_contracts_time_zone() -> None:
    """Monday 09:00 to 12:00 UTC is 18:00 to 21:00 in Tokyo, and 14:30 to 17:30 in
    Kolkata, where a stored hour starts on the half hour."""
    peak = stored(lambda t: 6.2 if t.weekday() == 0 and 9 <= t.hour < 12 else 1.0)
    (tokyo,) = weekly(peak, zone="Asia/Tokyo")["blocks"]
    assert (tokyo["since"], tokyo["until"]) == ("Mon 17:45", "Mon 21:00")
    (kolkata,) = weekly(peak, zone="Asia/Kolkata")["blocks"]
    assert (kolkata["since"], kolkata["until"]) == ("Mon 14:15", "Mon 17:30")
    assert (kolkata["start"], kolkata["end"]) == ("15 14 * * 1", "30 17 * * 1")
    late = stored(lambda t: 6.2 if t.weekday() == 0 and t.hour >= 20 else 1.0)
    (b,) = weekly(late, zone="Asia/Tokyo")["blocks"]  # Tuesday morning there
    assert (b["since"], b["until"]) == ("Tue 04:45", "Tue 09:00")


def test_a_profile_needs_three_weeks_of_the_workloads_own_complete_hours() -> None:
    p = weekly(stored(hours=WEEKS - 1))
    assert p == {
        "timezone": "UTC",
        "lead_time": 900,
        "history_h": 503.0,
        "needs_h": 504.0,
        "state": "learning",
    }
    under_way = stored(hours=WEEKS - 1) | {NOW - H / 2: 9.0}
    assert weekly(under_way)["state"] == "learning"
    old = stored(hours=WEEKS - 1) | {NOW - NEEDS_FOR - H: 9.0}
    assert weekly(old)["state"] == "learning"  # past the 8 weeks kept
    assert weekly(stored(hours=WEEKS))["state"] == "ready"
    eight = weekly(stored(hours=8 * 168) | {NOW - NEEDS_FOR - H: 9.0})
    assert eight["history_h"] == 8 * 168.0 and spans(eight) == spans(weekly())


def test_a_week_that_asks_the_same_every_hour_has_no_pattern() -> None:
    flat = weekly(stored(lambda t: 1.3))
    assert (flat["state"], flat["blocks"]) == ("flat", [])
    above = weekly(stored(lambda t: 4.2), floor=3)  # every hour's median over its Floor
    assert (above["state"], above["blocks"]) == ("flat", [])
    assert weekly(floor=9)["state"] == "flat"  # office hours, all under its Floor
    holes = {
        t: n
        for t, n in stored(lambda t: 1.3, 4 * 168).items()
        if datetime.fromtimestamp(t, UTC).hour
    }
    assert weekly(holes)["state"] == "flat"  # midnight never read: at the Floor like the rest


# --- how it is applied -----------------------------------------------------------------


def floor_with(
    hpa: str, needs: dict[float, float] | None = None, **sizing_config: Any
) -> dict[str, Any]:
    data = prometheus_contract().model_dump()
    data["sizing"] = sizing_config
    c = contracts.build(data)
    needs = stored(hours=4 * 168) if needs is None else needs
    bounds = {"test": {API: [Bounds(hpa, 2.0, 9.0)]}}
    (f,) = sizing.floors(c, [row()], {"test": {KEY: needs}}, bounds, NOW)["test"]
    return f


def test_a_keda_workload_gets_a_cron_trigger_for_each_block() -> None:
    f = floor_with("keda-hpa-api", timezone="Europe/Rome", lead_time="30m")
    p = f["profile"]
    assert (f["floor"], p["state"], p["timezone"], p["lead_time"]) == (
        2,
        "ready",
        "Europe/Rome",
        1800,
    )
    assert "note" not in p and len(p["blocks"]) == 7
    assert p["triggers"].splitlines()[:14] == [
        "# ScaledObject shop/api (KEDA): cron triggers, beside its others.",
        "# Each holds at least that many replicas in its window; the most asked for wins.",
        "- type: cron",
        "  metadata:",
        "    timezone: Europe/Rome",
        "    start: 30 9 * * 1",
        "    end: 0 11 * * 1",
        '    desiredReplicas: "3"',
        "- type: cron",
        "  metadata:",
        "    timezone: Europe/Rome",
        "    start: 30 10 * * 1",
        "    end: 0 14 * * 1",
        '    desiredReplicas: "7"',
    ]
    assert p["triggers"].count("- type: cron") == 7
    pasted = yaml.safe_load(p["triggers"])  # what the Operator pastes is valid YAML
    assert [t["type"] for t in pasted] == ["cron"] * 7
    for t, b in zip(pasted, p["blocks"], strict=True):
        want = {"timezone": "Europe/Rome", "start": b["start"], "end": b["end"]}
        assert t["metadata"] == want | {"desiredReplicas": str(b["replicas"])}
        assert b["start"] != b["end"]  # KEDA refuses a window that starts where it ends


def test_a_plain_hpa_gets_the_profile_with_a_note_and_no_trigger() -> None:
    p = floor_with("api")["profile"]
    assert p["state"] == "ready" and p["timezone"] == "UTC" and p["lead_time"] == 900
    assert "triggers" not in p
    assert p["note"] == (
        "an HPA has no schedule of its own: something else has to hold these replicas "
        "(KEDA's cron trigger does)"
    )
    assert floor_with("my-keda-hpa-api")["profile"].get("triggers") is None


def test_the_profile_looks_back_over_all_the_needs_kept_not_the_policys_window() -> None:
    """The Floor under production goes by 7 days; its profile by the weeks stored. A
    Floor still learning, or none, has no profile; a short history has one learning."""
    f = floor_with("api", stored(hours=WEEKS))
    assert (f["history_h"], f["profile"]["history_h"], f["profile"]["state"]) == (
        168.0,
        504.0,
        "ready",
    )
    young = floor_with("api", stored(hours=100))
    assert young["state"] == "ready" and young["profile"]["state"] == "learning"
    assert "profile" not in floor_with("api", stored(hours=10))  # its Floor is learning
    data = prometheus_contract().model_dump()
    two = {"test": {API: [Bounds("a", 2.0, 9.0), Bounds("b", 2.0, 9.0)]}}
    (none,) = sizing.floors(contracts.build(data), [row()], {"test": {KEY: stored()}}, two, NOW)[
        "test"
    ]
    assert none["state"] == "none" and "profile" not in none


def test_the_time_zone_and_the_lead_time_are_contract_settings() -> None:
    c = prometheus_contract()
    assert (c.sizing.timezone, c.sizing.lead_time) == ("UTC", 900)
    data = c.model_dump()
    for lead, seconds in (("30m", 1800), (0, 0), ("2h", 7200), (600, 600)):
        data["sizing"] = {"timezone": "America/Sao_Paulo", "lead_time": lead}
        got = contracts.build(data).sizing
        assert (got.timezone, got.lead_time) == ("America/Sao_Paulo", seconds)
    for bad in (
        {"timezone": "Mars/Olympus"},
        {"lead_time": "3h"},
        {"lead_time": 90},
        {"lead_time": -60},
    ):
        data["sizing"] = bad
        with pytest.raises(ValidationError):
            contracts.build(data)


# --- shown -----------------------------------------------------------------------------


def test_a_profile_reads_as_lines_under_its_floor() -> None:
    f = floor_with("api", stored(lambda t: 6.2 if t.weekday() == 0 and 9 <= t.hour < 12 else 1.0))
    assert sizing.profile_lines(f) == [
        "  Weekly profile (UTC, each rise 15 min early):",
        "    Mon 08:45 to Mon 12:00: 7 replicas",
        "    not backtested yet (needs four weeks)",  # slice 14.4
        "    an HPA has no schedule of its own: something else has to hold these replicas "
        "(KEDA's cron trigger does)",
    ]
    keda = floor_with("keda-hpa-api", f and stored(busy), lead_time=0)
    lines = sizing.profile_lines(keda)
    assert (
        lines[0] == "  Weekly profile (UTC):"
        and lines[1] == "    Mon 08:00 to Mon 09:00: 3 replicas"
    )
    assert lines[9:12] == [
        "    # ScaledObject shop/api (KEDA): cron triggers, beside its others.",
        "    # Each holds at least that many replicas in its window; the most asked for wins.",
        "    - type: cron",
    ]
    young = floor_with("api", stored(hours=100))
    assert sizing.profile_lines(young) == ["  Weekly profile learning (100 h of 504 h of Needs)"]
    flat = floor_with("api", stored(lambda t: 1.3))
    assert sizing.profile_lines(flat) == ["  no weekly pattern: its Floor holds all week"]
    assert sizing.profile_lines(floor_with("api", stored(hours=10))) == []


def test_the_workloads_route_gives_each_floors_profile(
    settings: Settings,
    fake_cluster: Any,
    database_url: str,
    app_dir: Path,  # noqa: F811
) -> None:
    import asyncio

    keda = floor_with("keda-hpa-api")
    young = {
        **floor_with("api", stored(hours=100)),
        "workload": "test/shop/Deployment/web",
        "name": "web",
    }

    async def seed() -> None:
        async with connect(database_url) as pool:
            s = Store(pool)
            await s.migrate()
            await s.save_floors("acme", "test", NOW, [keda, young])

    asyncio.run(seed())
    with hub(settings, fake_cluster, database_url) as c:
        body = c.get("/api/v1/contracts/acme/workloads", headers=VIEW).json()
    api, web = body["floors"]
    assert api["profile"]["state"] == "ready" and api["profile"]["note"] is None
    assert api["profile"]["blocks"][1] == {
        "since": "Mon 08:45",
        "until": "Mon 12:00",
        "replicas": 7,
        "start": "45 8 * * 1",
        "end": "0 12 * * 1",
    }
    assert api["profile"]["triggers"] == keda["profile"]["triggers"]
    assert web["profile"] == {
        **young["profile"],
        **{"blocks": [], "triggers": None, "note": None, "backtest": None},
    }
    assert (
        webapp.Floor.model_validate({**floor_with("api", stored(hours=10)), "at": NOW}).profile
        is None
    )
