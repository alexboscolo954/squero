"""Realized Savings (slice 10.3b): a request lowered to within 10% of a Sizing shown in
the 14 days before is stored as a drop by the hourly run, and worth (before - after)
x the pods now x today's prices, as a monthly rate and accrued since the drop. A later
rise takes it back, a gone container or a removed request ends it, fewer pods never
count as a saving."""

import json
from types import SimpleNamespace
from typing import Any

import pytest

from squero import contracts, sizing
from squero.store import Store, connect
from tests.test_prices import GI, priced
from tests.test_sizing_store import NOW, H, row

DAY = 24 * H
PRICES = {"cpu": 0.04, "memory": 0.005}
CORE = 0.04 * 730  # a core for a month: 29.2
GIB = 0.005 * 730  # a GiB for a month: 3.65


@pytest.fixture
async def store(database_url: str) -> Any:
    async with connect(database_url) as pool:
        s = Store(pool)
        await s.migrate()
        yield s


def sized(
    cpu: float | None,
    target: float | None = None,
    pods: float = 3.0,
    name: str = "api",
    memory: float = GI,
    memory_target: float | None = None,
    cluster: str = "test",
    live: bool | None = True,
) -> dict[str, Any]:
    """A stored row asking `cpu` cores and `memory` bytes per pod, its Sizing `target`
    (none: learning, no Sizing shown), read from live pods or not (None: unmarked, as
    stored before 10.3b)."""
    r = row(name, cluster, pods=pods, requests={"cpu": cpu, "memory": memory})
    if live is not None:
        r["live"] = live
    for res, current, t in (("cpu", cpu, target), ("memory", memory, memory_target)):
        if t is not None:
            r[res] = {"action": "lower", "current": current, "target": t, "basis": t}
    return r


async def hourly(
    store: Store,
    c: contracts.Contract,
    at: float,
    *rows: dict[str, Any],
    clusters: dict[str, str | None] | None = None,
) -> None:
    """One run as store_run does it: the previous good runs read first, then stored,
    then the drops."""
    clusters = clusters or {"test": None}
    before, status = await store.latest_sizings(c.id, set(clusters))
    run_id = await store.claim_sizing_run(c.id, at, 0)
    assert run_id is not None
    stored = [r for r in rows if clusters.get(r["cluster"]) is None]
    await store.finish_sizing_run(run_id, c.id, clusters, stored)
    await sizing.realize(store, c, at, before, status, stored, clusters)


@pytest.mark.anyio
async def test_a_request_lowered_to_its_sizing_saves_by_hand(store: Store) -> None:
    c = priced(PRICES)
    await hourly(store, c, NOW, sized(1.0, 0.115))
    await hourly(store, c, NOW + H, sized(0.115, 0.115))
    out = await sizing.savings(store, c, NOW + 11 * H)
    (d,) = out["drops"]
    assert (d["workload"], d["resource"]) == ("test/shop/Deployment/api/app", "cpu")
    assert (d["before"], d["after"], d["sizing"], d["at"], d["pods"]) == (
        1.0,
        0.115,
        0.115,
        NOW + H,
        3.0,
    )
    rate = 0.885 * 3 * CORE  # 77.526
    assert d["saved"] == pytest.approx(0.885 * 3)
    assert d["cost"] == pytest.approx({"rate": rate, "accrued": rate / 730 * 10})
    assert out["cost"] == pytest.approx({"rate": rate, "accrued": rate / 730 * 10, "week": rate})
    assert out["rate"] == pytest.approx({"cpu": 2.655, "memory": 0.0})
    assert out["accrued"] == pytest.approx({"cpu": 26.55, "memory": 0.0})  # core-hours
    assert (await sizing.savings(store, c, NOW + H / 2))["drops"] == []  # as of before it
    later = await sizing.savings(store, c, NOW + 8 * DAY)
    assert later["cost"]["week"] == 0.0 and later["cost"]["rate"] == pytest.approx(rate)


@pytest.mark.anyio
async def test_only_a_drop_within_10_percent_of_a_sizing_counts(store: Store) -> None:
    c = priced(PRICES)
    drops = (("near", 0.125), ("far", 0.13), ("half", 0.5))
    await hourly(store, c, NOW, *(sized(1.0, 0.115, name=n) for n, _ in drops))
    await hourly(store, c, NOW + H, *(sized(after, 0.115, name=n) for n, after in drops))
    names = [e["name"] for e in await store.realized(c.id)]
    assert names == ["near"]  # |0.125 - 0.115| <= 11.5m; 0.13 is 15m off


@pytest.mark.anyio
async def test_the_sizing_must_be_below_the_old_request_and_from_an_earlier_run(
    store: Store,
) -> None:
    c = priced(PRICES)
    # A Sizing that raised: old 100m, Sizing 105m, new 95m is within 10% but no saving
    await hourly(store, c, NOW, sized(0.1, 0.105, name="raise"))
    await hourly(store, c, NOW + H, sized(0.095, 0.105, name="raise"))
    assert await store.realized(c.id) == []
    # A request raised a little, near a Sizing just below the old one: no saving
    await hourly(store, c, NOW + H + 1, sized(0.11, 0.105, name="up"))
    await hourly(store, c, NOW + H + 2, sized(0.112, 0.105, name="up"))
    assert await store.realized(c.id) == []
    # A Sizing shown only by the run that saw the drop
    await hourly(
        store, c, NOW + 2 * H, sized(1.0, None, name="late"), sized(0.095, 0.105, name="raise")
    )
    await hourly(
        store, c, NOW + 3 * H, sized(0.115, 0.115, name="late"), sized(0.095, 0.105, name="raise")
    )
    assert await store.realized(c.id) == []


@pytest.mark.anyio
async def test_a_sizing_shown_more_than_14_days_before_doesnt_count(store: Store) -> None:
    c = priced(PRICES)
    await hourly(
        store, c, NOW - 15 * DAY, sized(1.0, 0.115, name="old"), sized(1.0, None, name="recent")
    )
    await hourly(
        store, c, NOW - 13 * DAY, sized(1.0, None, name="old"), sized(1.0, 0.115, name="recent")
    )
    await hourly(store, c, NOW - H, sized(1.0, None, name="old"), sized(1.0, None, name="recent"))
    await hourly(store, c, NOW, sized(0.115, None, name="old"), sized(0.115, None, name="recent"))
    assert [e["name"] for e in await store.realized(c.id)] == ["recent"]


@pytest.mark.anyio
async def test_a_rise_takes_back_the_newest_drops_first_and_never_grows_them(store: Store) -> None:
    c = priced(PRICES)

    async def saved(at: float) -> float:
        return float((await sizing.savings(store, c, at))["rate"]["cpu"]) / 3  # per pod

    await hourly(store, c, NOW, sized(1.0, 0.5))
    await hourly(store, c, NOW + H, sized(0.5, 0.115))  # 1000m -> 500m
    await hourly(store, c, NOW + 2 * H, sized(0.115, 0.115))  # 500m -> 115m
    assert await saved(NOW + 2 * H) == pytest.approx(0.885)
    await hourly(store, c, NOW + 3 * H, sized(0.3, 0.115))
    assert await saved(NOW + 3 * H) == pytest.approx(0.7)
    await hourly(store, c, NOW + 4 * H, sized(0.3, 0.115))  # the same again: no change
    assert await saved(NOW + 4 * H) == pytest.approx(0.7)
    await hourly(store, c, NOW + 5 * H, sized(0.2, None))  # lower again, to no Sizing
    assert await saved(NOW + 5 * H) == pytest.approx(0.7)
    await hourly(store, c, NOW + 6 * H, sized(0.8, None))
    assert await saved(NOW + 6 * H) == pytest.approx(0.2)
    assert [(e["before"], e["after"]) for e in await store.realized(c.id)] == [(1.0, 0.8)]
    await hourly(store, c, NOW + 7 * H, sized(1.2, None))
    assert await store.realized(c.id) == []  # back at its old request: deleted


@pytest.mark.anyio
async def test_fewer_pods_count_when_shown_and_never_as_a_drop(store: Store) -> None:
    c = priced(PRICES)
    await hourly(store, c, NOW, sized(1.0, 0.115, pods=3))
    await hourly(store, c, NOW + H, sized(1.0, 0.115, pods=1))  # scaled down: no drop
    assert await store.realized(c.id) == []
    await hourly(store, c, NOW + 2 * H, sized(0.115, 0.115, pods=1))
    await hourly(store, c, NOW + 3 * H, sized(0.115, 0.115, pods=2))
    out = await sizing.savings(store, c, NOW + 3 * H)
    assert out["cost"]["rate"] == pytest.approx(0.885 * 2 * CORE)  # the pods now


@pytest.mark.anyio
async def test_a_gone_container_or_a_removed_request_ends_it_a_failed_read_doesnt(
    store: Store,
) -> None:
    c = priced(PRICES)
    names = ("gone", "unset", "kept")
    await hourly(store, c, NOW, *(sized(1.0, 0.115, name=n) for n in names))
    await hourly(store, c, NOW + H, *(sized(0.115, 0.115, name=n) for n in names))
    assert len(await store.realized(c.id)) == 3
    await hourly(store, c, NOW + 2 * H, clusters={"test": "boom"})  # nothing read
    assert len(await store.realized(c.id)) == 3
    await hourly(
        store, c, NOW + 3 * H, sized(None, None, name="unset"), sized(0.115, 0.115, name="kept")
    )
    assert [e["name"] for e in await store.realized(c.id)] == ["kept"]


@pytest.mark.anyio
async def test_a_run_without_live_pods_leaves_a_drop_as_it_was(store: Store) -> None:
    """kube-state-metrics down, or the Workload scaled to 0 and back: the window's
    largest request is the old one, no rise; nothing is stored twice or restarted."""
    c = priced(PRICES)
    await hourly(store, c, NOW, sized(1.0, 0.115))
    await hourly(store, c, NOW + H, sized(0.115, 0.115))
    (first,) = await store.realized(c.id)
    await hourly(store, c, NOW + 2 * H, sized(1.0, 0.115, live=False))
    await hourly(store, c, NOW + 3 * H, sized(0.115, 0.115))  # back, live
    assert await store.realized(c.id) == [first]


@pytest.mark.anyio
async def test_a_row_stored_before_10_3b_never_starts_a_drop(store: Store) -> None:
    """The first run after the upgrade: the old row's request was the window's largest."""
    c = priced(PRICES)
    await hourly(store, c, NOW, sized(1.0, 0.115, live=None))
    await hourly(store, c, NOW + H, sized(0.115, 0.115))
    assert await store.realized(c.id) == []
    await hourly(store, c, NOW + 2 * H, sized(0.115, 0.115))
    await hourly(store, c, NOW + 3 * H, sized(0.115, 0.115, live=False))
    assert await store.realized(c.id) == []


@pytest.mark.anyio
async def test_cpu_and_memory_are_apart(store: Store) -> None:
    c = priced(PRICES)
    await hourly(store, c, NOW, sized(1.0, 0.115, memory=GI, memory_target=0.25 * GI))
    await hourly(store, c, NOW + H, sized(1.0, 0.115, memory=0.25 * GI, memory_target=0.25 * GI))
    out = await sizing.savings(store, c, NOW + H)
    (d,) = out["drops"]
    assert d["resource"] == "memory"
    assert out["cost"]["rate"] == pytest.approx(0.75 * 3 * GIB)
    assert out["rate"] == pytest.approx({"cpu": 0.0, "memory": 0.75 * 3 * GI})


@pytest.mark.anyio
async def test_a_drop_is_stored_once_when_two_runs_compare_with_the_same_run(store: Store) -> None:
    """`sizing refresh` started while the hourly run is still running."""
    c = priced(PRICES)
    await hourly(store, c, NOW, sized(1.0, 0.115))
    before, status = await store.latest_sizings(c.id, {"test"})
    now = [sized(0.115, 0.115)]
    for at in (NOW + H, NOW + H + 60):
        run_id = await store.claim_sizing_run(c.id, at, 0)
        assert run_id is not None
        await store.finish_sizing_run(run_id, c.id, {"test": None}, now)
        await sizing.realize(store, c, at, before, status, now, {"test": None})
    assert len(await store.realized(c.id)) == 1


@pytest.mark.anyio
async def test_unpriced_savings_are_in_cores_and_core_hours(store: Store) -> None:
    c = priced()
    await hourly(store, c, NOW, sized(1.0, 0.115))
    await hourly(store, c, NOW + H, sized(0.115, 0.115))
    out = await sizing.savings(store, c, NOW + 3 * H)
    assert out["cost"] is None and out["currency"] is None and out["drops"][0]["cost"] is None
    assert out["rate"]["cpu"] == pytest.approx(2.655)
    assert out["accrued"]["cpu"] == pytest.approx(2.655 * 2)


@pytest.mark.anyio
async def test_each_contract_sees_only_its_own_drops_and_sizings(store: Store) -> None:
    """Same cluster, namespace and Workload: bank's drop never matches acme's Sizing."""
    acme = priced(PRICES)
    bank = contracts.build({**priced(PRICES).model_dump(), "id": "bank"})
    await hourly(store, acme, NOW, sized(1.0, 0.115))
    await hourly(store, bank, NOW, sized(1.0, None))
    await hourly(store, bank, NOW + H, sized(0.115, None))
    await hourly(store, acme, NOW + H, sized(0.115, 0.115))
    assert await store.realized("bank") == []
    assert len(await store.realized("acme")) == 1
    assert (await sizing.savings(store, bank, NOW + H))["drops"] == []


@pytest.mark.anyio
async def test_a_cluster_no_longer_in_the_contract_shows_nothing(store: Store) -> None:
    c = priced(PRICES)
    await hourly(store, c, NOW, sized(1.0, 0.115, cluster="old"), clusters={"old": None})
    await hourly(store, c, NOW + H, sized(0.115, 0.115, cluster="old"), clusters={"old": None})
    assert len(await store.realized(c.id)) == 1
    assert (await sizing.savings(store, c, NOW + H))["drops"] == []  # the Contract has only test


@pytest.mark.anyio
async def test_store_run_compares_with_the_previous_run_not_itself(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = priced(PRICES)
    runs = iter([[sized(1.0, 0.115)], [sized(0.115, 0.115)]])
    monkeypatch.setattr(sizing, "rows", lambda c, by_window, at, scaled, rooms: next(runs))
    found = SimpleNamespace(
        clusters={"test": None}, by_window={}, notes=[], nodes={}, needs={}, scaled={}, bounds={}
    )
    found.rooms = {}
    monkeypatch.setattr(sizing.usage, "collect", lambda c, windows, at, since: found)
    await sizing.store_run(store, c, NOW, every=0)
    await sizing.store_run(store, c, NOW + H, every=0)
    (e,) = await store.realized(c.id)
    assert (e["before"], e["after"]) == (1.0, 0.115)


def test_sizing_savings_prints_each_drop(
    database_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import asyncio

    from squero import cli

    c = priced(PRICES)

    async def seed() -> None:
        async with connect(database_url) as pool:
            s = Store(pool)
            await s.migrate()
            await hourly(s, c, NOW, sized(1.0, 0.115))
            await hourly(s, c, NOW + H, sized(0.115, 0.115))

    monkeypatch.setenv("SQUERO_DATABASE_URL", database_url)
    monkeypatch.setattr(cli, "contract", lambda settings, cid: c)
    monkeypatch.setattr("time.time", lambda: NOW + 11 * H)
    assert cli.main(["sizing", "savings", "--contract", "acme"]) == 0
    assert "no realized Savings" in capsys.readouterr().out
    asyncio.run(seed())
    assert cli.main(["sizing", "savings", "--contract", "acme"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == [
        "acme: realized Savings 77.53 USD/month, 1.06 USD so far; 77.53 USD/month from this week's",
        "test/shop/Deployment/api/app cpu: 1000m to 115m (Sizing 115m) since 1972-09-27 01:00 UTC, "
        "3 pods: 77.53 USD/month, 1.06 USD so far",
    ]
    assert cli.main(["sizing", "savings", "--contract", "acme", "--json"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["contract"] == "acme" and body["drops"][0]["resource"] == "cpu"


def test_the_digest_says_what_was_realized_and_nothing_when_nothing_was() -> None:
    zero = {"cpu": 0.0, "memory": 0.0}
    none = {
        "currency": "USD",
        "rate": zero,
        "week": zero,
        "cost": {"rate": 0.0, "accrued": 0.0, "week": 0.0},
    }
    assert sizing.realized_line(none) is None
    some = {
        "currency": "USD",
        "rate": {"cpu": 2.655, "memory": 0.0},
        "week": zero,
        "cost": {"rate": 77.53, "accrued": 1.06, "week": 0.0},
    }
    assert (
        sizing.realized_line(some) == "Realized this week: 0.00 USD/month (77.53 USD/month in all)."
    )
    plain = {**some, "currency": None, "cost": None}
    assert sizing.realized_line(plain) == (
        "Realized this week: 0.00 cores, 0.00 GiB (2.65 cores, 0.00 GiB in all)."
    )


@pytest.mark.anyio
async def test_the_weekly_digest_carries_the_realized_line(store: Store) -> None:
    c = priced(PRICES)
    await hourly(store, c, NOW, sized(1.0, 0.115))
    assert "Realized" not in await sizing.digest(store, c, NOW, None)
    await hourly(store, c, NOW + H, sized(0.115, 0.115))
    text = await sizing.digest(store, c, NOW + 2 * H, None)
    assert "Realized this week: 77.53 USD/month (77.53 USD/month in all)." in text


def test_each_stored_row_says_whether_its_requests_are_its_live_pods() -> None:
    from tests.test_sizing_store import container, rows_of

    (live,) = rows_of(container(live=True))
    (window,) = rows_of(container())
    assert live["live"] is True and window["live"] is False
