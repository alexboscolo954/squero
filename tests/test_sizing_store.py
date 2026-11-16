"""Waste and risk, stored (slice 9.4): waste is the request above the Sizing times the
average pods; risks have kinds; the Hub stores a run per Contract each hour, keeps it
hourly for 14 days and daily for 400, and one failing cluster never blanks another."""

import asyncio
import json
from typing import Any

import pytest
from pydantic import SecretStr

from squero import cli, contracts, sizing, usage
from squero.config import Settings
from squero.contracts import BUILTIN, Contract
from squero.store import Store, connect
from squero.usage import ContainerUsage, Workload, step_for
from tests.conftest import make_contract
from tests.fake_prometheus import FakePrometheus
from tests.test_api import client
from tests.test_sizing_policy import lab, requests
from tests.test_usage import _prom, cpu, deployment, mem

DAY = 86_400.0
H = 3600.0
MI = 2**20
NOW = 1000 * DAY  # midnight UTC
WEEK = 7 * 86_400
STEP = step_for(WEEK)


def prometheus_contract(cid: str = "acme") -> Contract:
    data = make_contract(contract_id=cid).model_dump()
    data["integrations"].append({"name": "prometheus", "url": "http://prometheus"})
    return contracts.build(data)


def container(
    pods: int = 3,
    hours: float = 48,
    cores: float = 0.1,
    peak: float = 80 * MI,
    request: dict[str, float] | None = None,
    step: int = STEP,
    gap: tuple[float, float] | None = None,
    **kw: Any,
) -> ContainerUsage:
    """`pods` pods at a flat `cores` and `peak` for `hours`, sampled every `step`, none
    between `gap`'s hours ago (a Prometheus that missed its scrapes)."""
    u = ContainerUsage(Workload("shop", "Deployment", "api"), "app", **kw)
    n = int(hours * H / step) + 1
    times = [NOW - hours * H + i * step for i in range(n)]
    if gap:
        times = [t for t in times if not NOW - gap[0] * H < t < NOW - gap[1] * H]
    for p in range(pods):
        u.pods.add(f"api-{p}")
        u.cpu += [(t, cores) for t in times]
        u.memory += [(t, peak) for t in times]
    u.requests = request if request is not None else {"cpu": 1.0, "memory": float(2**30)}
    return u


def rows_of(
    *us: ContainerUsage, c: Contract | None = None, window: int = WEEK
) -> list[dict[str, Any]]:
    return sizing.rows(c or prometheus_contract(), {window: [("test", u) for u in us]}, NOW)


def kinds(row: dict[str, Any]) -> list[str]:
    return sorted(r["kind"] for r in row["risks"])


# --- waste ----------------------------------------------------------------------


def test_waste_is_the_request_above_the_sizing_times_the_pods() -> None:
    """1 core and 1 GiB per pod, sized 115m and 96 MiB, three pods."""
    (row,) = rows_of(container())
    assert row["pods"] == 3
    assert row["cpu"]["target"] == 0.115 and row["memory"]["target"] == 96 * MI
    assert row["waste"]["cpu"] == pytest.approx((1.0 - 0.115) * 3)
    assert row["waste"]["memory"] == pytest.approx((1024 - 96) * MI * 3)


def test_a_cronjob_averages_its_pods_over_its_history() -> None:
    """One pod for an hour each day, two days, next to a Deployment that always runs:
    its requests reserve about a twenty-fourth of the time, so its waste is about a
    twenty-fourth."""
    u = ContainerUsage(Workload("shop", "CronJob", "report"), "app")
    u.requests = {"cpu": 1.0, "memory": float(2**30)}
    u.pods.add("report-1")
    window = BUILTIN["batch"].window
    step = step_for(window)
    for day in (2, 1, 0):
        for i in range(int(H / step) + 1):
            u.cpu.append((NOW - day * DAY + i * step, 0.1))
            u.memory.append((NOW - day * DAY + i * step, 80 * MI))
    always = container(pods=1, hours=72, step=step)
    (row,) = rows_of(u, always, window=window)  # always: production, another window
    assert row["name"] == "report" and row["policy"] == "batch" and row["state"] == "ready"
    assert row["history_h"] == 49  # slice 9.7: between its runs counts, at batch's step
    steps = 2 * DAY / step + int(H / step) + 1
    # three places for a batch run (slice 16.3a): one would make an hour a night nothing
    assert row["pods"] == round(len(u.cpu) / steps, 3) != round(len(u.cpu) / steps, 1)
    assert row["waste"]["cpu"] == pytest.approx((1.0 - 0.11) * len(u.cpu) / steps)


def test_pods_are_counted_from_their_running_phase() -> None:
    """CPU samples outlive a pod by `rate`'s 5 minutes; kube-state-metrics' phase
    doesn't: a pod Running a third of the steps averages a third, whatever its CPU."""
    u = container(pods=1, hours=24)
    times = sorted({t for t, _ in u.cpu})
    u.running = times[::3]
    always = container(pods=1, hours=24)
    always.workload = Workload("shop", "Deployment", "web")
    api, _web = rows_of(u, always)
    assert api["pods"] == round(len(times[::3]) / len(times), 1) == 0.3


def test_a_gap_in_prometheus_counts_for_no_one() -> None:
    """Every pod missed six hours of scrapes: still three pods."""
    (row,) = rows_of(container(gap=(30, 24)))
    assert row["pods"] == 3


# --- history is covered time (slice 9.7) ---------------------------------------------


def test_with_no_gap_history_is_the_span_from_the_first_sample_to_the_last() -> None:
    """One step fewer than the times: a day of samples is a day, so ready; a step short
    of a day is still learning."""
    (day,) = rows_of(container(hours=24))
    assert (day["state"], day["history_h"]) == ("ready", 24)
    u = container(hours=24)
    u.cpu, u.memory = ([s for s in ss if s[0] > NOW - DAY] for ss in (u.cpu, u.memory))
    (short,) = rows_of(u)
    assert (short["state"], short["history_h"]) == ("learning", 24)  # 23.97 h
    assert sizing.history(u, sorted({t for t, _ in u.cpu}), STEP) == DAY - STEP
    assert sizing.history(ContainerUsage(u.workload, "none"), [], STEP) == 0


def test_a_gap_in_prometheus_is_no_history() -> None:
    """The lab's case: an hour of data, 70 hours with no scrape at all, an hour of data.
    Two hours of history, not three days: still learning."""
    (row,) = rows_of(container(hours=72, gap=(71, 1)))
    assert (row["state"], row["history_h"]) == ("learning", 2)
    assert "cpu" not in row and "waste" not in row


def test_a_container_away_while_its_cluster_has_usage_keeps_its_history() -> None:
    """Scaled to zero for 70 hours next to a container that ran: three days."""
    away, web = container(hours=72, gap=(71, 1)), container(hours=72)
    web.workload = Workload("shop", "Deployment", "web")
    api, _web = rows_of(away, web)
    assert (api["state"], api["history_h"]) == ("ready", 72)


def test_an_excluded_container_that_ran_through_keeps_the_others_history() -> None:
    """`off` gives it no Sizing; its usage still says the cluster had data."""
    away, idle = container(hours=72, gap=(71, 1)), container(hours=72)
    idle.workload = Workload("shop", "Deployment", "idle")
    data = prometheus_contract().model_dump()
    data["sizing"] = {"rules": [{"name": "idle", "policy": "off"}]}
    api, off = rows_of(away, idle, c=contracts.build(data))
    assert off["state"] == "excluded"
    assert (api["state"], api["history_h"]) == ("ready", 72)


def test_history_starts_at_the_containers_own_first_sample() -> None:
    """Two hours old next to one that ran three days: two hours, whatever its cluster has."""
    young, web = container(hours=2), container(hours=72)
    web.workload = Workload("shop", "Deployment", "web")
    api, _web = rows_of(young, web)
    assert (api["state"], api["history_h"]) == ("learning", 2)


def test_history_counts_memory_samples_as_well_as_cpu() -> None:
    """A container whose CPU series ends a day before its memory's, and one with memory
    only: both have their two days."""
    u, only = container(hours=48), container(hours=48)
    u.cpu = [s for s in u.cpu if s[0] < NOW - DAY]
    only.cpu = []
    for one in (u, only):
        (row,) = rows_of(one)
        assert (row["state"], row["history_h"]) == ("ready", 48)


def test_only_periodic_workloads_count_their_run_times() -> None:
    """The known limit: with nothing else in the Contract's namespaces there, a CronJob's
    hour a day for three days is three hours."""
    u = ContainerUsage(Workload("shop", "CronJob", "report"), "app")
    window = BUILTIN["batch"].window
    step = step_for(window)
    for day in (2, 1, 0):
        u.cpu += [(NOW - day * DAY + i * step, 0.1) for i in range(int(H / step) + 1)]
    (row,) = rows_of(u, window=window)
    assert (row["state"], row["history_h"]) == ("learning", 3.1)


def test_another_clusters_usage_covers_no_gap_here() -> None:
    gapped, full = container(hours=72, gap=(71, 1)), container(hours=72)
    found = {WEEK: [("test", gapped), ("other", full)]}
    out = {r["cluster"]: r for r in sizing.rows(prometheus_contract(), found, NOW)}
    assert (out["test"]["state"], out["test"]["history_h"]) == ("learning", 2)
    assert (out["other"]["state"], out["other"]["history_h"]) == ("ready", 72)


def test_running_pods_without_usage_are_no_history() -> None:
    """kube-state-metrics scraped through a gap in the kubelet's: pods counted Running
    all along, no usage to size by."""
    u, full = container(pods=1, hours=72, gap=(71, 1)), container(hours=72)
    u.running = sorted({t for t, _ in full.cpu})
    (row,) = rows_of(u)
    assert (row["state"], row["history_h"], row["pods"]) == ("learning", 2, 1)


def test_a_raise_or_a_kept_request_wastes_nothing() -> None:
    raised = container(cores=0.5, request={"cpu": 0.1, "memory": float(2**30)})
    (row,) = rows_of(raised)
    assert row["cpu"]["action"] == "raise" and row["waste"]["cpu"] == 0
    held = container(peak=80 * MI, oom_killed_pods=1)
    (row,) = rows_of(held)
    assert row["memory"]["action"] == "keep" and row["waste"]["memory"] == 0
    assert row["waste"]["cpu"] > 0


def test_learning_and_excluded_rows_carry_no_waste() -> None:
    (learning,) = rows_of(container(hours=2))
    assert learning["state"] == "learning" and "waste" not in learning
    data = prometheus_contract().model_dump()
    data["sizing"] = {"rules": [{"name": "api", "policy": "off"}]}
    (off,) = rows_of(container(), c=contracts.build(data))
    assert off["state"] == "excluded" and "waste" not in off and off["name"] == "api"


# --- risk kinds -------------------------------------------------------------------


def test_every_risk_has_its_kind() -> None:
    (unset,) = rows_of(container(request={}))
    assert kinds(unset) == ["request_unset", "request_unset"]
    assert {"kind": "request_unset", "text": "cpu has no request"} in unset["risks"]
    (low,) = rows_of(container(cores=0.5, request={"cpu": 0.1, "memory": float(2**30)}))
    assert {"kind": "under_requested", "text": "cpu request 100m is below the 575m it needs"} in (
        low["risks"]
    )
    (throttled,) = rows_of(container(throttled=0.5))
    assert kinds(throttled) == ["throttled", "throttled"]
    (oom,) = rows_of(container(oom_killed_pods=1))
    assert kinds(oom) == ["oom_killed", "oom_killed"]
    (limited,) = rows_of(container(cores=0.5, limits={"cpu": 0.2}))
    assert "limit_below_need" in kinds(limited)
    data = prometheus_contract().model_dump()
    data["sizing"] = {"policies": {"small": {"base": "production", "cpu": {"max": "50m"}}}}
    data["sizing"]["default"] = "small"
    (capped,) = rows_of(container(), c=contracts.build(data))
    assert "capped_by_policy" in kinds(capped)


def test_show_leaves_out_what_the_action_already_says(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakePrometheus()
    (p,) = deployment(fake, "api", pods=1)
    cpu(fake, p, [0.5] * 1441)
    mem(fake, p, [80 * MI] * 1441)
    requests(fake, p, 0.1, 2**30)
    monkeypatch.setattr(cli, "contract", lambda settings, cid: prometheus_contract())
    monkeypatch.setattr(usage, "Prom", lambda t: _prom(fake, t))
    assert cli.main(["sizing", "show", "--contract", "acme"]) == 0
    out = capsys.readouterr().out
    assert "cpu raise 100m -> 575m" in out and "risk" not in out


# --- the store ----------------------------------------------------------------------


@pytest.fixture
async def store(database_url: str) -> Any:
    async with connect(database_url) as pool:
        s = Store(pool)
        await s.migrate()
        yield s


def row(name: str = "api", cluster: str = "test", **extra: Any) -> dict[str, Any]:
    return {
        "workload": f"{cluster}/shop/Deployment/{name}/app",
        "cluster": cluster,
        "namespace": "shop",
        "kind": "Deployment",
        "name": name,
        "container": "app",
        "policy": "production",
        "state": "ready",
        "pods": 1.0,
        "waste": {"cpu": 1.0, "memory": 0.0},
        "risks": [],
        **extra,
    }


async def stored(
    store: Store, cid: str, at: float, clusters: dict[str, str | None], *rows: dict[str, Any]
) -> int:
    run_id = await store.claim_sizing_run(cid, at, 0)
    assert run_id is not None
    await store.finish_sizing_run(run_id, cid, clusters, list(rows))
    return run_id


@pytest.mark.anyio
async def test_a_run_is_claimed_once_an_hour(store: Store) -> None:
    assert await store.claim_sizing_run("acme", NOW, H) is not None
    assert await store.claim_sizing_run("acme", NOW + H - 1, H) is None
    assert await store.claim_sizing_run("acme", NOW + H, H) is not None
    together = await asyncio.gather(
        *(store.claim_sizing_run("acme", NOW + 2 * H, H) for _ in range(5))
    )
    assert sum(r is not None for r in together) == 1


@pytest.mark.anyio
async def test_each_cluster_keeps_its_last_good_run(store: Store) -> None:
    first = await stored(store, "acme", NOW, {"a": None, "b": None}, row("x", "a"), row("y", "b"))
    await stored(store, "acme", NOW + H, {"a": None, "b": "boom"}, row("x2", "a"))
    last = await stored(store, "acme", NOW + 2 * H, {"a": None, "b": "bust"}, row("x3", "a"))
    await store.claim_sizing_run("acme", NOW + 3 * H, 0)  # still running: no clusters yet
    rows, status = await store.latest_sizings("acme", {"a", "b"})
    assert sorted(r["name"] for r in rows) == ["x3", "y"]
    assert status["a"] == {"at": NOW + 2 * H, "run": last, "error": None, "since": None}
    assert status["b"] == {"at": NOW, "run": first, "error": "bust", "since": NOW + H}


@pytest.mark.anyio
async def test_a_cluster_no_longer_in_the_contract_is_gone(store: Store) -> None:
    await stored(store, "acme", NOW, {"a": None, "b": None}, row("x", "a"), row("y", "b"))
    rows, status = await store.latest_sizings("acme", {"a"})
    assert [r["name"] for r in rows] == ["x"] and list(status) == ["a"]


@pytest.mark.anyio
async def test_a_cluster_never_read_has_no_rows(store: Store) -> None:
    await stored(store, "acme", NOW, {"a": "unreachable"})
    rows, status = await store.latest_sizings("acme", {"a"})
    assert rows == [] and status["a"] == {
        "at": None,
        "run": None,
        "error": "unreachable",
        "since": NOW,
    }


@pytest.mark.anyio
async def test_runs_are_hourly_for_14_days_then_daily_for_400(store: Store) -> None:
    times = [
        NOW - 401 * DAY,  # gone
        NOW - 399 * DAY + H,  # the last of its day: kept
        NOW - 20 * DAY + H,  # not the last of its day: gone
        NOW - 20 * DAY + 23 * H,  # kept: the last finished one
        NOW - 13 * DAY + H,  # within 14 days: kept
        NOW - 13 * DAY + 2 * H,
    ]
    for at in times:
        await stored(store, "acme", at, {"test": None}, row())
        if at == times[3]:  # then one that crashed: no clusters
            assert await store.claim_sizing_run("acme", at + H / 2, 0) is not None
    await store.prune_sizings("acme", NOW)
    left = await store.sizing_history("acme", ("test", "shop", "Deployment", "api", "app"), 0)
    assert [at for at, _ in left] == [times[1], times[3], times[4], times[5]]


@pytest.mark.anyio
async def test_history_is_one_containers_rows_oldest_first(store: Store) -> None:
    await stored(store, "acme", NOW - H, {"test": None}, row(pods=9.0))
    await stored(store, "acme", NOW, {"test": None}, row(pods=1.0))
    await stored(store, "acme", NOW + H, {"test": None}, row(pods=2.0), row("web"))
    left = await store.sizing_history("acme", ("test", "shop", "Deployment", "api", "app"), NOW)
    assert [(at, r["pods"]) for at, r in left] == [(NOW, 1.0), (NOW + H, 2.0)]


# --- isolation ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_contracts_stored_sizings_never_reach_another(store: Store) -> None:
    """Same cluster, namespace and Workload names in both Contracts."""
    old = NOW - 20 * DAY
    for at in (old, old + H):  # interleaved ids on one old day: A1, B1, A2, B2
        await stored(store, "shop", at, {"test": None}, row(policy="shop"))
        await stored(store, "bank", at, {"test": None}, row(policy="bank"))
    await stored(store, "shop", NOW, {"test": None}, row(policy="shop"))
    await stored(store, "bank", NOW, {"test": "bank only"}, row(policy="bank"))
    assert await store.claim_sizing_run("bank", NOW + 1, H) is None
    assert await store.claim_sizing_run("shop", NOW + 1, H) is None
    await store.prune_sizings("shop", NOW)
    workload = ("test", "shop", "Deployment", "api", "app")
    for cid in ("shop", "bank"):
        rows, status = await store.latest_sizings(cid, {"test"})
        assert [r["policy"] for r in rows] == [cid]
        history = await store.sizing_history(cid, workload, 0)
        assert {r["policy"] for _, r in history} == {cid}
        assert status["test"]["error"] == ("bank only" if cid == "bank" else None)
    shop = [at for at, _ in await store.sizing_history("shop", workload, 0)]
    bank = [at for at, _ in await store.sizing_history("bank", workload, 0)]
    assert shop == [old + H, NOW]  # pruned: its own last of that day kept
    assert bank == [old, old + H, NOW]  # not pruned by shop's prune


def test_each_demo_contract_stores_only_its_own_sizings(
    two_contracts: dict[str, Contract], database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both demo Contracts end to end on one fake Prometheus: each run stores only the
    namespaces its Contract allows."""
    fake = FakePrometheus()
    for c in two_contracts.values():
        for cl in c.clusters:
            for ns in cl.namespaces:
                (p,) = deployment(fake, f"{c.id}-api", pods=1, ns=ns)
                cpu(fake, p, [0.1] * 1441, ns=ns)
                mem(fake, p, [80 * MI] * 1441, ns=ns)
    monkeypatch.setattr(usage, "Prom", lambda t: _prom(fake, t))

    async def go() -> dict[str, set[str]]:
        async with connect(database_url) as pool:
            store = Store(pool)
            await store.migrate()
            out = {}
            for c in two_contracts.values():
                c = c.model_copy(update={"integrations": [*c.integrations, PROM]})
                assert await sizing.store_run(store, c, NOW) is not None
                rows, _ = await store.latest_sizings(c.id, {cl.name for cl in c.clusters})
                out[c.id] = {r["name"] for r in rows}
            return out

    found = asyncio.run(go())
    assert found["demo-shop"] and found["demo-bank"]
    for cid, names in found.items():
        assert names == {f"{cid}-api"}


def test_another_contracts_usage_covers_no_gap_of_this_one(
    two_contracts: dict[str, Contract], database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slice 9.7 on one Prometheus both demo Contracts read: demo-bank's container has
    usage through the 70 hours demo-shop's has none. Shop's history is its two hours:
    bank's samples never count as shop's cluster having data."""
    fake = FakePrometheus()
    hour = int(H / STEP)
    for c in two_contracts.values():
        gap = [None if c.id == "demo-shop" else 1.0] * (70 * hour - 1)
        for cl in c.clusters:
            for ns in cl.namespaces:
                (p,) = deployment(fake, f"{c.id}-api", pods=1, ns=ns)
                for add, v in ((cpu, 0.1), (mem, 80.0 * MI)):
                    edge = [1.0] * (hour + 1)  # an hour, both ends sampled
                    add(fake, p, [x and v for x in edge + gap + edge], ns=ns)
    monkeypatch.setattr(usage, "Prom", lambda t: _prom(fake, t))

    async def go() -> dict[str, set[tuple[str, float]]]:
        async with connect(database_url) as pool:
            store = Store(pool)
            await store.migrate()
            out = {}
            for c in two_contracts.values():
                c = c.model_copy(update={"integrations": [*c.integrations, PROM]})
                assert await sizing.store_run(store, c, NOW) is not None
                rows, _ = await store.latest_sizings(c.id, {cl.name for cl in c.clusters})
                out[c.id] = {(r["state"], r["history_h"]) for r in rows}
            return out

    found = asyncio.run(go())
    assert found["demo-shop"] == {("learning", 2)}
    assert found["demo-bank"] == {("ready", 72)}


PROM = contracts.Prometheus(name="prometheus", url="http://prometheus")


# --- the Hub and the CLI ----------------------------------------------------------


def lab_fake(monkeypatch: pytest.MonkeyPatch) -> FakePrometheus:
    fake = FakePrometheus()
    lab(fake)
    monkeypatch.setattr(usage, "Prom", lambda t: _prom(fake, t))
    return fake


def test_the_hub_stores_a_run_per_contract_each_hour(
    settings: Settings, fake_cluster: Any, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.database_url = SecretStr(database_url)
    fake = lab_fake(monkeypatch)
    integrations = [{"name": "kubernetes"}, {"name": "prometheus", "url": "http://prometheus"}]
    with client(settings, fake_cluster, acme={"integrations": integrations}) as c:
        assert c.portal is not None

        def tick(at: float) -> list[str]:
            done: list[str] = c.portal.call(c.app.state.due_sizings, at)  # type: ignore[union-attr,attr-defined]
            return done

        assert tick(NOW) == ["acme"]
        asked = len(fake.queries)
        assert tick(NOW + H - 1) == [] and len(fake.queries) == asked
        assert tick(NOW + H) == ["acme"]
        fake.status = 500
        assert tick(NOW + 2 * H) == ["acme"]  # stored, with the cluster's error

    async def latest() -> Any:
        async with connect(database_url) as pool:
            return await Store(pool).latest_sizings("acme", {"test"})

    rows, status = asyncio.run(latest())
    assert {r["name"] for r in rows} == {"api", "db", "idle", "report"}
    assert status["test"]["at"] == NOW + H
    assert status["test"]["since"] == NOW + 2 * H and "500" in status["test"]["error"]


def test_one_contracts_failure_never_stops_the_others(
    settings: Settings, fake_cluster: Any, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.database_url = SecretStr(database_url)
    lab_fake(monkeypatch)
    real = usage.collect

    def collect(c: Contract, *args: Any) -> usage.Collected:
        if c.id == "acme":
            raise RuntimeError("bad Contract")
        return real(c, *args)

    monkeypatch.setattr(usage, "collect", collect)
    integrations = [{"name": "kubernetes"}, {"name": "prometheus", "url": "http://prometheus"}]
    acme = {"integrations": integrations}
    more = {"bank": (prometheus_contract("bank"), fake_cluster)}
    with client(settings, fake_cluster, acme=acme, more=more) as c:
        assert c.portal is not None
        assert c.portal.call(c.app.state.due_sizings, NOW) == ["bank"]  # type: ignore[attr-defined]


def test_refresh_then_waste(
    database_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    lab_fake(monkeypatch)
    monkeypatch.setenv("SQUERO_DATABASE_URL", database_url)
    data = prometheus_contract().model_dump()
    data["sizing"] = {"rules": [{"name": "idle", "policy": "off"}]}
    monkeypatch.setattr(cli, "contract", lambda settings, cid: contracts.build(data))
    assert cli.main(["sizing", "refresh", "--contract", "acme"]) == 0
    assert "acme/test: stored" in capsys.readouterr().out
    assert cli.main(["sizing", "waste", "--contract", "acme"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("test: Sizings as of ")
    assert out[2].startswith("test/shop/Deployment/api: 0.89 cores, 0.91 GiB, avg 1 pods")
    assert "test/shop/Deployment/db: 0.89 cores" in out[3]
    assert "excluded: test/shop/Deployment/idle/app" in out
    assert cli.main(["sizing", "waste", "--contract", "acme", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["workloads"][0]["workload"] == "test/shop/Deployment/api"
    assert report["total"]["cpu"] == pytest.approx(
        sum(w["waste"]["cpu"] for w in report["workloads"])
    )


def test_waste_ranks_a_core_like_4_gib() -> None:
    def gib(n: float) -> dict[str, Any]:
        return {"waste": {"cpu": 0.0, "memory": n * 2**30}}

    core = {"waste": {"cpu": 1.0, "memory": 0.0}}
    assert sizing.rank(gib(3)) < sizing.rank(core) == sizing.rank(gib(4)) < sizing.rank(gib(5))


def test_waste_without_a_run_says_so(
    database_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SQUERO_DATABASE_URL", database_url)
    monkeypatch.setattr(cli, "contract", lambda settings, cid: prometheus_contract())
    assert cli.main(["sizing", "waste", "--contract", "acme"]) == 1
    assert "no Sizings stored yet" in capsys.readouterr().out


@pytest.mark.anyio
async def test_a_contract_without_prometheus_stores_nothing(store: Store) -> None:
    assert await sizing.store_run(store, make_contract(), NOW) is None
    assert await store.claim_sizing_run("acme", NOW, H) is not None  # nothing was claimed


@pytest.mark.anyio
async def test_each_run_prunes_its_contracts_old_runs(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    lab_fake(monkeypatch)
    c = prometheus_contract()
    for at in (NOW, NOW + 401 * DAY):
        assert await sizing.store_run(store, c, at) is not None
    left = await store.sizing_history("acme", ("test", "shop", "Deployment", "api", "app"), 0)
    assert [at for at, _ in left] == [NOW + 401 * DAY]


@pytest.mark.anyio
async def test_an_approved_sizing_is_current_until_a_later_run_reads_its_cluster(
    store: Store,
) -> None:
    """Slice 11.2b. A run still going, one failing on that cluster, one that read
    only another cluster, and another Contract's run never supersede it."""
    lowered = row(cpu={"action": "lower", "target": 0.1}, memory={"action": "keep", "target": 1})
    run = await stored(store, "acme", NOW, {"test": None, "edge": None}, lowered)
    assert await store.approve_sizing("acme", run, lowered, "mcp:op", NOW) == "approved"
    assert await store.approve_sizing("acme", run, lowered, "mcp:op", NOW) == "already"

    async def current() -> list[tuple[Any, ...]]:
        found = await store.approved_sizings("acme")
        return [(a["run_id"], a["name"], a["cpu"], a["memory"]) for a in found]

    assert await current() == [(run, "api", 0.1, None)]
    await stored(store, "acme", NOW + H, {"test": "boom", "edge": None})
    await stored(store, "bank", NOW + H, {"test": None})
    await store.claim_sizing_run("acme", NOW + 2 * H, 0)  # still running
    assert await current() == [(run, "api", 0.1, None)]
    assert await store.approved_sizings("bank") == []
    await stored(store, "acme", NOW + 3 * H, {"test": None})
    assert await current() == []


@pytest.mark.anyio
async def test_approving_again_after_a_revert_moves_the_approval_past_it(store: Store) -> None:
    """Slice 12.9b: before, the second approval was a no-op, so the Revert stayed the last
    word and the Sizing was never sent again."""
    lowered = row(cpu={"action": "lower", "target": 0.1})
    other = row("web", cpu={"action": "lower", "target": 0.1})
    side = row(cpu={"action": "lower", "target": 0.1}, container="sidecar")
    side["workload"] = "test/shop/Deployment/api/sidecar"
    late = row("late", cpu={"action": "lower", "target": 0.1})
    run = await stored(store, "acme", NOW, {"test": None}, lowered, other, side, late)
    keys = ("test", lowered["namespace"], lowered["kind"], "api", lowered["container"])
    for r in (lowered, other, side):
        assert await store.approve_sizing("acme", run, r, "mcp:op", NOW) == "approved"
    await store.ask_revert("acme", keys, "mcp:op", NOW + 60)
    for cid, cluster in (("bank", "test"), ("acme", "edge")):  # another Contract's, cluster's
        await store.ask_revert(cid, (cluster, *keys[1:3], "late", keys[4]), "mcp:op", NOW + 70)
        await store.ask_revert(cid, (cluster, *keys[1:4], "sidecar"), "mcp:op", NOW + 70)
    assert await store.approve_sizing("acme", run, late, "mcp:op", NOW + 90) == "approved"
    assert await store.approve_sizing("acme", run, side, "mcp:two", NOW + 120) == "already"
    await store.ask_revert("bank", (*keys[:3], "web", keys[4]), "mcp:op", NOW + 60)  # bank's web
    assert await store.reverted("acme", "test") == {keys[1:]}
    assert await store.approve_sizing("acme", run, other, "mcp:two", NOW + 120) == "already"
    assert await store.approve_sizing("acme", run, lowered, "mcp:two", NOW + 120) == "approved"
    assert await store.reverted("acme", "test") == set()
    found = {a["name"]: a for a in await store.approved_sizings("acme")}
    assert (found["api"]["approved_by"], found["api"]["approved_at"]) == ("mcp:two", NOW + 120)
    assert found["api"]["again"] is True and found["web"]["again"] is False
    assert found["late"]["again"] is False  # after api's Revert, not one of its own
    await store.ask_revert("acme", (*keys[:3], "web", keys[4]), "mcp:op", NOW + 150)
    found = {a["name"]: a for a in await store.approved_sizings("acme")}
    assert found["web"]["again"] is False  # reverted since: not approved again
    assert found["web"]["approved_at"] == NOW
    assert await store.approve_sizing("acme", run, lowered, "mcp:op", NOW + 180) == "already"


@pytest.mark.anyio
async def test_no_sizing_is_approved_while_the_kill_switch_is_on(store: Store) -> None:
    lowered = row(cpu={"action": "lower", "target": 0.1})
    run = await stored(store, "acme", NOW, {"test": None}, lowered)
    await store.turn_on_kill_switch("acme", "console:op", NOW)
    await store.turn_on_kill_switch("bank", "console:op", NOW)
    assert await store.approve_sizing("acme", run, lowered, "mcp:op", NOW) == "killed"
    await store.turn_off_kill_switch("acme")
    assert await store.approve_sizing("acme", run, lowered, "mcp:op", NOW) == "approved"


def test_a_stored_sizing_carries_its_memory_peak_which_a_step_ups_term_reads() -> None:
    """Slice 13.1c: the Hub sends an Actuator each automated container's `memory.basis` as
    its peak; were the key renamed, no Step-up would ever end."""
    from squero import actuator

    (row,) = rows_of(container(peak=300.0 * 2**20))
    assert row["memory"]["basis"] == 300.0 * 2**20
    fields = ("namespace", "kind", "name", "container")
    listed = actuator.Watched(**{k: row[k] for k in fields}, peak=row["memory"]["basis"])
    assert listed.peak == 300.0 * 2**20


# --- the burst Reflex's numbers (slice 13.3a) ------------------------------------------------


def bursting_contract(cid: str = "acme", **burst: Any) -> Contract:
    """prometheus_contract(), its cluster running an Actuator, with `reflexes.burst`."""
    data = make_contract(contract_id=cid).model_dump()
    data["integrations"].append({"name": "prometheus", "url": "http://prometheus"})
    data["clusters"][0]["actuator_token_secret"] = "act"
    data["actuator"] = {"reflexes": {"burst": burst}}
    return contracts.build(data, secrets={"act": SecretStr("a" * 40)})


def test_a_row_keeps_the_last_hours_throttling_and_its_cpu_peak_only_with_the_reflex() -> None:
    u = container(pods=1, hours=48, cores=0.1, throttled_recent=0.4)
    u.cpu.append((NOW - 30 * H, 0.9))  # a spike 30 hours ago
    u.cpu.append((NOW - 24 * H, 0.5))  # and one exactly a day ago
    (row,) = rows_of(u, c=bursting_contract())
    assert row["burst"] == {"throttled": 0.4, "peak": 0.5}  # over `expires`: a day unless said
    (row,) = rows_of(u, c=bursting_contract(expires="2d"))
    assert row["burst"] == {"throttled": 0.4, "peak": 0.9}
    (row,) = rows_of(u)
    assert "burst" not in row  # the Reflex off: nothing kept
    young = container(pods=1, hours=2, cores=0.3)  # still learning, and not throttled
    (row,) = rows_of(young, c=bursting_contract())
    assert row["state"] == "learning" and row["burst"] == {"throttled": None, "peak": 0.3}
    quiet = ContainerUsage(Workload("shop", "Deployment", "api"), "app")
    quiet.memory = [(NOW - h * H, 80.0 * MI) for h in range(48)]  # no CPU sample at all
    (row,) = rows_of(quiet, c=bursting_contract())
    assert row["burst"] == {"throttled": None, "peak": None}


@pytest.mark.parametrize(
    ("share", "age", "told"),
    [
        (0.26, 60.0, 0.26),
        (0.25, 60.0, None),  # a quarter is not more than a quarter
        (None, 60.0, None),
        (0.9, sizing.BURST_FRESH, 0.9),
        (0.9, sizing.BURST_FRESH + 1, None),  # an hour long past is no reason to raise a limit
        (0.9, None, None),  # never read
    ],
)
def test_a_burst_is_due_over_a_quarter_of_the_periods_of_a_fresh_read(
    share: float | None, age: float | None, told: float | None
) -> None:
    got = sizing.bursting({"burst": {"throttled": share, "peak": 0.7}}, age)
    fresh = age is not None and age <= sizing.BURST_FRESH
    assert got == {"throttled": told, "peak": 0.7 if fresh else None}
    assert sizing.bursting({}, 60.0) == {"throttled": None, "peak": None}  # a row from before
    assert (sizing.BURST_OVER, sizing.BURST_FRESH) == (0.25, 7200)


def test_the_hourly_read_asks_for_the_last_hour_only_with_the_reflex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakePrometheus()
    lab(fake)
    for metric, n in (("throttled_periods", 900), ("periods", 1000)):
        fake.add(f"container_cpu_cfs_{metric}_total", n, namespace="shop", pod="api-7d9f-0",
                 container="app")  # fmt: skip
    monkeypatch.setattr(usage, "Prom", lambda t: _prom(fake, t))
    found = usage.collect(prometheus_contract(), [WEEK], NOW)
    assert [u.throttled_recent for _, u in found.by_window[WEEK]] == [None] * 4
    assert not any("[3600s]" in q for q in fake.queries)
    found = usage.collect(bursting_contract(), [WEEK], NOW)
    api = next(u for _, u in found.by_window[WEEK] if u.workload.name == "api")
    assert api.throttled_recent == pytest.approx(0.9)
    (row,) = [
        r for r in sizing.rows(bursting_contract(), found.by_window, NOW) if r["name"] == "api"
    ]
    # (the fixture's samples are the window's first two days: none in the last one)
    assert row["burst"] == {"throttled": pytest.approx(0.9), "peak": None}
