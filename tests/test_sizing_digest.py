"""The weekly Sizing digest (slice 9.5b): waste and its change in a week, the top
Workloads by waste, new risks, stale clusters and the containers still learning, posted
once per due time to the operator channel only, never twice after a restart."""

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from squero import cli, contracts, sizing
from squero.config import Settings
from squero.store import Store, connect
from squero.usage import ContainerUsage, Workload
from tests.conftest import make_contract, make_fake_cluster
from tests.test_api import client
from tests.test_sizing_store import MI, NOW, container, prometheus_contract, rows_of
from tests.test_slack import ACME, ACME_CLIENT, BANK, TOKEN, FakeSlack

WEEK = 7 * 86400.0
PROM = [{"name": "kubernetes"}, {"name": "prometheus", "url": "http://prometheus"}]
READ = {"at": NOW, "error": None, "since": None}


def named(name: str, **kw: Any) -> ContainerUsage:
    u = container(pods=1, **kw)
    u.workload = Workload("shop", "Deployment", name)
    return u


def now_report(clusters: dict[str, Any] | None = None) -> dict[str, Any]:
    """w1..w6 waste less and less CPU (and the same memory); `full` wastes nothing
    (its requests are below what it needs); `leaky` is OOM-killed and `hot` throttled;
    `new` learns."""
    us = [named(f"w{i}", cores=i / 10) for i in range(1, 7)]
    us += [
        named("full", cores=1.0, peak=900 * MI),
        named("leaky", cores=0.5, oom_killed_pods=2),
        named("hot", cores=0.5, throttled=0.5),
        named("new", hours=2),
    ]
    return sizing.report(rows_of(*us), clusters or {"test": READ})


def week_report() -> dict[str, Any]:
    """A week ago: leaky was already OOM-killed, hot not yet throttled, and w1 wasted
    a core more (it ran two pods)."""
    w1 = container(pods=2, cores=0.1)
    w1.workload = Workload("shop", "Deployment", "w1")
    us = [w1, named("leaky", cores=0.5, oom_killed_pods=1), named("hot", cores=0.5)]
    return sizing.report(rows_of(*us), {"test": {**READ, "at": NOW - WEEK}})


def test_the_digest_says_waste_its_change_the_top_five_and_new_risks() -> None:
    now, week = now_report(), week_report()
    text = sizing.digest_text("acme", now, week)
    lines = text.splitlines()
    assert lines[0] == "*Weekly Sizing digest: acme*"
    cpu = now["total"]["cpu"] - week["total"]["cpu"]
    gib = (now["total"]["memory"] - week["total"]["memory"]) / 2**30
    assert lines[1] == (
        f"Waste: {now['total']['cpu']:.2f} cores, {now['total']['memory'] / 2**30:.2f} GiB "
        f"({cpu:+.2f} cores, {gib:+.2f} GiB vs 7 days ago)"
    )
    top = lines[
        lines.index("*Top Workloads by waste*") + 1 : lines.index("*New risks since last week*")
    ]
    assert [line.split("`")[1] for line in top] == [
        f"test/shop/Deployment/w{i}" for i in range(1, 6)
    ]  # five, most first; w6 and the Workloads without waste left out
    assert top[0] == "1. `test/shop/Deployment/w1`: 0.89 cores, 0.91 GiB"
    risks = lines[lines.index("*New risks since last week*") + 1 :]
    assert "• `test/shop/Deployment/hot`: throttled" in risks  # new this week
    assert "• `test/shop/Deployment/full`: under_requested" in risks  # a new Workload
    assert not any("leaky" in r for r in risks)  # OOM-killed a week ago too
    assert lines[-1] == "Containers still learning: 1."
    assert "Stale" not in text and "Open the Workloads page" not in text


def test_without_a_week_old_run_it_says_so_and_lists_every_risk_as_a_risk() -> None:
    week = sizing.report([], {})
    text = sizing.digest_text("acme", now_report(), week)
    assert "(no Sizings from 7 days ago to compare)" in text
    assert "vs 7 days ago" not in text and "*New risks since last week*" not in text
    assert "*Risks*\n" in text and "`test/shop/Deployment/leaky`: oom_killed" in text


def test_stale_and_never_read_clusters_are_named_with_escaped_errors() -> None:
    clusters = {
        "test": READ,
        "eu": {"at": NOW - 86400, "error": "HTTP 500 <!channel>", "since": NOW - 3600},
        "us": {"at": None, "error": None, "since": None},
    }
    lines = sizing.digest_text("acme", now_report(clusters), week_report()).splitlines()
    stale = lines[lines.index("*Stale clusters*") + 1 :][:2]
    when = datetime.fromtimestamp(NOW - 3600, UTC).strftime("%Y-%m-%d %H:%M UTC")
    assert stale == [f"• eu: stale since {when}: HTTP 500 &lt;!channel&gt;", "• us: never read"]


def test_no_sizings_yet_and_the_link_only_when_given() -> None:
    empty = sizing.report([], {})
    assert (
        sizing.digest_text("acme", empty, empty)
        == "*Weekly Sizing digest: acme*\nNo Sizings stored yet."
    )
    text = sizing.digest_text("acme", empty, empty, "https://hub.ts.net/app/?contract=acme")
    assert text.endswith("\n<https://hub.ts.net/app/?contract=acme|Open the Workloads page>")


# --- contract.yaml ---------------------------------------------------------------


def test_the_digest_is_on_monday_at_nine_utc_unless_changed_or_off() -> None:
    assert prometheus_contract().sizing.digest == contracts.Digest(
        schedule="0 9 * * 1", timezone="UTC"
    )
    data = prometheus_contract().model_dump()
    data["sizing"] = {"digest": False}  # YAML's bare `off`
    assert contracts.build(data).sizing.digest == "off"
    data["sizing"] = {"digest": {"schedule": "30 8 * * 5", "timezone": "Europe/Rome"}}
    assert contracts.build(data).sizing.digest == contracts.Digest(
        schedule="30 8 * * 5", timezone="Europe/Rome"
    )
    for bad in ({"schedule": "nope"}, {"timezone": "Mars/Base"}, {"schedule": "* * * * *"}):
        data["sizing"] = {"digest": bad}
        with pytest.raises(ValidationError):
            contracts.build(data)


# --- the schedule ----------------------------------------------------------------


@pytest.fixture
async def store(database_url: str) -> Any:
    async with connect(database_url) as pool:
        s = Store(pool)
        await s.migrate()
        yield s


@pytest.mark.anyio
async def test_a_due_digest_is_claimed_once_even_when_claimed_together(store: Store) -> None:
    await store.sync_digests({"acme": ("key", 100.0)})
    assert not await store.claim_digest("acme", 99.0, 200.0)
    got = await asyncio.gather(*(store.claim_digest("acme", 100.0, 200.0) for _ in range(5)))
    assert sorted(got) == [False] * 4 + [True]
    assert not await store.claim_digest("acme", 199.0, 300.0)
    assert await store.claim_digest("acme", 200.0, 300.0)


@pytest.mark.anyio
async def test_a_changed_schedule_is_due_anew_and_a_contract_left_out_loses_its_row(
    store: Store,
) -> None:
    await store.sync_digests({"acme": ("a", 100.0), "bank": ("b", 100.0)})
    await store.sync_digests({"acme": ("a", 500.0), "bank": ("b2", 500.0)})
    assert await store.claim_digest("acme", 100.0, 900.0)  # same schedule: kept 100
    assert not await store.claim_digest("bank", 100.0, 900.0)  # changed: due at 500
    assert await store.claim_digest("bank", 500.0, 900.0)
    await store.sync_digests({"acme": ("a", 2000.0)})  # bank turned off
    assert not await store.claim_digest("bank", 5000.0, 9000.0)
    await store.sync_digests({"acme": ("a", 2000.0), "bank": ("b2", 3000.0)})  # back on
    assert not await store.claim_digest("bank", 2999.0, 9000.0)  # a new time, not 900
    assert await store.claim_digest("bank", 3000.0, 9000.0)


@pytest.mark.anyio
async def test_the_baseline_is_each_clusters_latest_run_up_to_a_week_ago(store: Store) -> None:
    for at, cores in ((NOW - WEEK - 3600, 0.1), (NOW - WEEK + 3600, 0.5), (NOW, 0.9)):
        run = await store.claim_sizing_run("acme", at, 0)
        assert run is not None
        u = container(pods=1, cores=cores)
        await store.finish_sizing_run(run, "acme", {"test": None}, rows_of(u))
    rows, status = await store.latest_sizings("acme", {"test"}, before=NOW - WEEK)
    assert status["test"]["at"] == NOW - WEEK - 3600
    assert rows[0]["cpu"]["basis"] == pytest.approx(0.1)


# --- the Hub ---------------------------------------------------------------------


def schedule(database_url: str) -> list[dict[str, Any]]:
    with psycopg.connect(database_url, row_factory=psycopg.rows.dict_row) as conn:
        return list(conn.execute("select * from sizing_digests").fetchall())


def tick(c: TestClient, at: float) -> list[str]:
    assert c.portal is not None
    posted: list[str] = c.portal.call(c.app.state.due_digests, at)  # type: ignore[attr-defined]
    return posted


def to(slack: FakeSlack) -> list[dict[str, Any]]:
    return [p for m, p, _ in slack.calls if m == "chat.postMessage"]


def seed(database_url: str, contract_id: str = "acme", u: ContainerUsage | None = None) -> None:
    async def go() -> None:
        async with connect(database_url) as pool:
            s = Store(pool)
            await s.migrate()
            run = await s.claim_sizing_run(contract_id, NOW, 0)
            assert run is not None
            await s.finish_sizing_run(
                run, contract_id, {"test": None}, rows_of(u or container(pods=1))
            )

    asyncio.run(go())


@pytest.fixture
def hub(settings: Settings, fake_cluster: Any, database_url: str) -> Any:
    settings.database_url = SecretStr(database_url)
    settings.slack_bot_token = SecretStr(TOKEN)
    slack = FakeSlack()

    def make(acme: dict[str, Any] | None = None, channel: str | None = ACME) -> TestClient:
        return client(
            settings,
            fake_cluster,
            slack=slack.transport(),
            acme_channel=channel,
            acme_client=ACME_CLIENT if channel else None,
            acme={"integrations": PROM, **(acme or {})},
        )

    make.slack = slack  # type: ignore[attr-defined]
    return make


def test_the_hub_posts_once_per_due_time_to_the_operator_channel_only(
    hub: Any, database_url: str, settings: Settings
) -> None:
    settings.console_url = "https://hub.example.ts.net/"
    with hub() as c:
        (row,) = schedule(database_url)
        due = row["next_due"]
        assert datetime.fromtimestamp(due, UTC).strftime("%a %H:%M") == "Mon 09:00"
        assert tick(c, due - 1) == []
        assert tick(c, due) == ["acme"]
        assert tick(c, due) == [] and tick(c, due + 3600) == []
    (post,) = to(hub.slack)
    assert post["channel"] == ACME  # never the client channel
    assert post["text"].startswith("*Weekly Sizing digest: acme*")
    assert post["text"].endswith(
        "<https://hub.example.ts.net/app/?contract=acme|Open the Workloads page>"
    )
    with hub() as c:  # a restart: the claimed time stays claimed
        assert schedule(database_url)[0]["next_due"] == due + WEEK
        assert tick(c, due + 60) == []
        assert tick(c, due + 4 * WEEK) == ["acme"]  # missed weeks: one post
        assert tick(c, due + 4 * WEEK + 60) == []
    assert len(to(hub.slack)) == 2
    assert schedule(database_url)[0]["next_due"] == due + 5 * WEEK


def test_the_hub_digest_is_built_from_its_stored_sizings(hub: Any, database_url: str) -> None:
    seed(database_url)
    seed(database_url, "bank")  # another Contract's Workloads never show
    with hub() as c:
        assert tick(c, schedule(database_url)[0]["next_due"]) == ["acme"]
    (post,) = to(hub.slack)
    assert "1. `test/shop/Deployment/api`: 0.89 cores, 0.91 GiB" in post["text"]
    assert post["text"].count("`test/shop") == 1
    assert "Open the Workloads page" not in post["text"]


def test_each_contracts_digest_goes_to_its_own_channel_with_its_own_workloads(
    settings: Settings, fake_cluster: Any, database_url: str
) -> None:
    settings.database_url = SecretStr(database_url)
    settings.slack_bot_token = SecretStr(TOKEN)
    slack = FakeSlack()
    seed(database_url)
    seed(database_url, "bank", named("ledger"))
    bank = make_contract(contract_id="bank", slack_channel=BANK, integrations=PROM)
    with client(
        settings,
        fake_cluster,
        slack=slack.transport(),
        acme_channel=ACME,
        acme={"integrations": PROM},
        more={"bank": (bank, make_fake_cluster())},
    ) as c:
        due = max(r["next_due"] for r in schedule(database_url))
        assert sorted(tick(c, due)) == ["acme", "bank"]
    posts = {p["channel"]: p["text"] for p in to(slack)}
    assert len(to(slack)) == 2 and set(posts) == {ACME, BANK}
    assert "Deployment/api`" in posts[ACME] and "ledger" not in posts[ACME]
    assert "Deployment/ledger`" in posts[BANK] and "Deployment/api`" not in posts[BANK]


@pytest.mark.parametrize(
    "case",
    [
        {"acme": {"sizing": {"digest": "off"}}},
        {"acme": {"integrations": [{"name": "kubernetes"}]}},  # no Prometheus
        {"channel": None},  # no Slack
        {},  # no bot token: the Hub can't post
    ],
)
def test_no_digest_without_slack_a_prometheus_and_a_bot_token_or_when_off(
    hub: Any, database_url: str, settings: Settings, case: dict[str, Any]
) -> None:
    if not case:
        settings.slack_bot_token = None
    with hub(**case) as c:
        assert schedule(database_url) == []
        assert tick(c, 2e9) == []
    assert to(hub.slack) == []


# --- the CLI ---------------------------------------------------------------------


def test_cli_prints_and_posts_now_without_moving_the_schedule(
    database_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(database_url)
    monkeypatch.setenv("SQUERO_DATABASE_URL", database_url)
    monkeypatch.setenv("SQUERO_SLACK_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("SQUERO_CONSOLE_URL", "https://hub.example.ts.net")
    data = prometheus_contract().model_dump()
    data["slack"] = {"operator_channel": ACME, "client_channel": ACME_CLIENT}
    monkeypatch.setattr(cli, "contract", lambda settings, cid: contracts.build(data))
    slack = FakeSlack()
    real = httpx.AsyncClient
    monkeypatch.setattr(cli.httpx, "AsyncClient", lambda: real(transport=slack.transport()))
    assert cli.main(["sizing", "digest", "--contract", "acme"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("*Weekly Sizing digest: acme*") and "`test/shop/Deployment/api`" in out
    assert "<https://hub.example.ts.net/app/?contract=acme|Open the Workloads page>" in out
    assert slack.calls == []
    assert cli.main(["sizing", "digest", "--contract", "acme", "--post"]) == 0
    (post,) = to(slack)
    assert post["channel"] == ACME and post["text"] == out.rstrip("\n")
    assert schedule(database_url) == []


def test_a_workload_without_waste_is_never_in_the_top() -> None:
    us = [named("w1", cores=0.1), named("full", cores=1.0, peak=900 * MI)]
    now = sizing.report(rows_of(*us), {"test": READ})
    lines = sizing.digest_text("acme", now, now).splitlines()
    top = lines[lines.index("*Top Workloads by waste*") + 1 :]
    assert len(top) == 1 and top[0].startswith("1. `test/shop/Deployment/w1`")


@pytest.mark.anyio
async def test_the_digest_compares_its_time_with_a_week_before_it(store: Store) -> None:
    """Runs eight days before, at, and an hour after the digest's time: it reads the
    one at its time, compared with the one a week before; the later run is not yet."""
    for at, pods in ((NOW - WEEK - 86400, 2), (NOW, 1), (NOW + 3600, 3)):
        run = await store.claim_sizing_run("acme", at, 0)
        assert run is not None
        await store.finish_sizing_run(run, "acme", {"test": None}, rows_of(container(pods=pods)))
    text = await sizing.digest(store, prometheus_contract(), NOW, None)
    assert "Waste: 0.89 cores, 0.91 GiB (-0.89 cores, -0.91 GiB vs 7 days ago)" in text


def acted(outcome: str, name: str = "api", cluster: str = "test", **more: Any) -> dict[str, Any]:
    """One Actuation as the store reads it."""
    return {
        "cluster": cluster,
        "namespace": "shop",
        "kind": "Deployment",
        "name": name,
        "container": "app",
        "run_id": 1,
        "outcome": outcome,
        "cpu": 0.1,
        "memory": None,
        "pods": 1,
        "reason": "",
        "at": NOW,
    } | more


def test_the_digest_sums_the_weeks_actuations_by_the_containers_they_reached() -> None:
    """Audit A13: one line, an outcome counting each container once (reported again, or
    by a later run), the same name in another cluster apart; what changed nothing left out."""
    week = sizing.report([], {})
    rows = [
        acted("burst", name="web"),  # first: the line keeps its own order
        acted("applied"),
        acted("applied", run_id=2),  # the same container again
        acted("applied", cluster="other"),
        acted("applied", name="web"),
        acted("rolled_back", name="web"),
        acted("reverted"),
        acted("failed", name="db"),
        acted("stepped_up"),
        *(acted(o) for o in ("skipped", "oom_killed", "throttled", "step_ended", "burst_ended")),
        acted("handed_back"),
    ]
    lines = sizing.digest_text("acme", now_report(), week, acted=rows).splitlines()
    assert lines[2] == (
        "Actuator this week: 3 resized, 1 rolled back, 1 reverted, 1 failed, "
        "1 with a Step-up, 1 with a Burst."
    )
    quiet = [acted(o) for o in ("skipped", "oom_killed", "step_ended", "handed_back")]
    for nothing in (None, [], quiet):
        assert "Actuator" not in sizing.digest_text("acme", now_report(), week, acted=nothing)


@pytest.mark.anyio
async def test_the_digest_counts_its_own_contracts_actuations_of_its_week(store: Store) -> None:
    """The week up to the digest's time, never before it or after it, and never another
    Contract's Actuations (isolation)."""
    run = await store.claim_sizing_run("acme", NOW, 0)
    assert run is not None
    await store.finish_sizing_run(run, "acme", {"test": None}, rows_of(container()))
    report = {k: v for k, v in acted("applied").items() if k != "cluster"}
    for contract, name, at in (
        ("acme", "api", NOW - 3600),  # in its week
        ("acme", "old", NOW - WEEK - 3600),  # the week before
        ("acme", "late", NOW + 3600),  # after the digest's time
        ("bank", "theirs", NOW - 3600),  # another Contract's two
        ("bank", "theirs-too", NOW - 3600),
    ):
        await store.record_actuations(contract, "test", [report | {"name": name, "at": at}], at)
    text = await sizing.digest(store, prometheus_contract(), NOW, None)
    assert "Actuator this week: 1 resized." in text.splitlines()
