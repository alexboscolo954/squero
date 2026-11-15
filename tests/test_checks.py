"""Scheduled Checks (slice 8.1): cron in the Check's zone, fired once per due time from
Postgres, quiet when they pass, an Open failure in one Slack thread when they don't."""

import asyncio
import json
import time
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from squero import agent, checks
from squero.config import Settings
from squero.models import CheckResult, Evidence, Investigation
from squero.store import Store, connect, now
from tests import test_api
from tests.conftest import REPORT, call, make_contract, make_fake_cluster
from tests.test_api import AUTH, bank, client
from tests.test_console import OPERATOR as LOGIN
from tests.test_console import SAME, VIEW
from tests.test_cost import HAIKU
from tests.test_restart import wait_for
from tests.test_slack import (
    ACME,
    BANK,
    EVICT,
    TOKEN,
    FakeSlack,
    FakeSocket,
    sock,  # noqa: F401 (a fixture)
    until,
)
from tests.test_teachings import (
    Recording,
    buttons,  # noqa: F401 (a fixture)
    posted,
    press,
    rows,
    seed,
)

CHECK = {"name": "shop-healthy", "schedule": "*/15 * * * *", "prompt": "Do all pods in shop run?"}
EV = REPORT["evidence"][0]
DAY = 86400.0


def passed(i: int = 1) -> Any:
    return call("CheckResult", f"p{i}", passed=True, summary="All pods in shop run.", evidence=[EV])


def failed(i: int = 1) -> Any:
    summary = "checkout crash loops."
    return call("CheckResult", f"f{i}", passed=False, summary=summary, evidence=[EV], report=REPORT)


def hub(settings: Settings, fake_cluster: Any, *steps: Any, **kw: Any) -> TestClient:
    acme = {"checks": [CHECK], **kw.pop("acme", {})}
    return client(settings, fake_cluster, *steps, acme=acme, **kw)


def tick(c: TestClient, at: float) -> list[str]:
    """One scheduler tick at time `at`, in the Hub's own loop."""
    assert c.portal is not None
    started: list[str] = c.portal.call(c.app.state.due_checks, at)  # type: ignore[attr-defined]
    return started


def schedule(database_url: str, name: str = "shop-healthy") -> dict[str, Any]:
    with psycopg.connect(database_url, row_factory=psycopg.rows.dict_row) as conn:
        row = conn.execute("select * from check_schedules where name = %s", (name,)).fetchone()
    assert row is not None
    return dict(row)


def first_thread(c: TestClient, tid: str) -> str:
    ts: str = c.get(f"/investigations/{tid}", headers=AUTH).json()["slack_ts"]
    return ts


def posts(slack: FakeSlack) -> list[tuple[str, str | None]]:
    """(text, thread_ts) of each chat.postMessage."""
    return [(p["text"], p.get("thread_ts")) for m, p, _ in slack.calls if m == "chat.postMessage"]


@pytest.fixture(autouse=True)
def _db(settings: Settings, database_url: str) -> None:
    settings.database_url = SecretStr(database_url)


@pytest.fixture
def slack(settings: Settings) -> FakeSlack:
    settings.slack_bot_token = SecretStr(TOKEN)
    return FakeSlack()


# Schedules


def utc(*args: int) -> float:
    return datetime(*args, tzinfo=UTC).timestamp()


def test_next_due_is_strictly_after_in_the_checks_own_zone() -> None:
    at = utc(2026, 10, 20, 7, 0)  # 09:00 in Rome (UTC+2)
    assert checks.next_due("0 9 * * *", "Europe/Rome", at) == utc(2026, 10, 21, 7, 0)
    assert checks.next_due("0 9 * * *", "UTC", at) == utc(2026, 10, 20, 9, 0)
    # Rome leaves summer time on 2026-10-25: 09:00 is then 08:00 UTC.
    assert checks.next_due("0 9 * * *", "Europe/Rome", utc(2026, 10, 25, 0)) == utc(
        2026, 10, 25, 8, 0
    )


@pytest.mark.parametrize(
    ("schedule", "timezone", "error"),
    [
        ("*/5 * * * *", "UTC", "5 minutes apart"),
        ("0,10 * * * *", "UTC", "10 minutes apart"),
        ("99 * * * *", "UTC", "bad cron schedule"),
        ("0 0 30 2 *", "UTC", "bad cron schedule"),  # never fires
        ("0 9 * * *", "Mars/Olympus", "unknown time zone"),
    ],
)
def test_bad_or_too_fast_schedules_are_refused(schedule: str, timezone: str, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        checks.validate(schedule, timezone)
    with pytest.raises(ValidationError, match=error):
        make_contract(checks=[{**CHECK, "schedule": schedule, "timezone": timezone}])


def test_a_contracts_checks() -> None:
    checks.validate("*/15 * * * *", "UTC")
    checks.validate("0 9 * * 1-5", "Europe/Rome")
    c = make_contract(checks=[CHECK])
    assert (c.checks[0].name, c.checks[0].timezone) == ("shop-healthy", "UTC")
    with pytest.raises(ValidationError, match="duplicate check names"):
        make_contract(checks=[CHECK, CHECK])
    with pytest.raises(ValidationError):
        make_contract(checks=[{**CHECK, "prompt": "x" * 2001}])
    with pytest.raises(ValidationError):
        make_contract(checks=[{**CHECK, "name": "Not A Slug"}])


def test_a_failing_check_needs_its_report() -> None:
    with pytest.raises(ValidationError, match="needs its report"):
        CheckResult(passed=False, summary="x", evidence=[Evidence(**EV)])
    ok = CheckResult(passed=True, summary="x", evidence=[Evidence(**EV)], report=REPORT)
    assert ok.report is None  # a passing Check has no Report


def test_a_check_result_loads_from_a_checkpoint() -> None:
    serde = agent.CHECKPOINT_SERDE
    value = CheckResult(passed=False, summary="x", evidence=[Evidence(**EV)], report=REPORT)
    assert serde.loads_typed(serde.dumps_typed(value)) == value


def test_invented_links_are_dropped_from_a_checks_report_too() -> None:
    link = {**EV, "link": "https://evil.example/x"}
    report = {**REPORT, "evidence": [link]}
    found = CheckResult(passed=False, summary="x", evidence=[Evidence(**link)], report=report)
    assert agent.drop_unreturned_links(found, set()) == 2
    assert found.evidence[0].link is None
    assert found.report and found.report.evidence[0].link is None


# The schedule in Postgres


@pytest.fixture
async def store(database_url: str) -> Any:
    async with connect(database_url) as pool:
        s = Store(pool)
        await s.migrate()
        yield s


def a_check(contract_id: str = "acme", at: float = 0.0) -> Investigation:
    return Investigation(
        id=f"{contract_id}-{at}-{time.monotonic_ns()}",
        kind="check",
        contract_id=contract_id,
        title="Check shop-healthy",
        started=at,
        input={"check": "shop-healthy", "prompt": "p"},
    )


@pytest.mark.anyio
async def test_a_due_check_fires_once_even_when_claimed_together(store: Store) -> None:
    await store.sync_checks("acme", {"shop-healthy": ("k", 100.0)})
    claims = [store.claim_check(a_check(), "shop-healthy", 500.0, 900.0, None) for _ in range(5)]
    assert sorted(await asyncio.gather(*claims)) == ["not due"] * 4 + ["started"]
    assert rows_of(await store.recent(10)) == 1


def rows_of(found: list[Investigation]) -> int:
    return len([t for t in found if t.kind == "check"])


@pytest.mark.anyio
async def test_missed_fire_times_run_once_then_the_schedule_goes_on(store: Store) -> None:
    await store.sync_checks("acme", {"shop-healthy": ("k", 100.0)})  # 3 intervals missed
    assert await store.claim_check(a_check(), "shop-healthy", 3000.0, 3600.0, None) == "started"
    assert await store.claim_check(a_check(), "shop-healthy", 3000.0, 3600.0, None) == "not due"
    assert await store.due_checks(3599.0) == []
    assert await store.due_checks(3600.0) == [("acme", "shop-healthy")]


@pytest.mark.anyio
async def test_no_new_run_while_the_last_is_open(store: Store) -> None:
    await store.sync_checks("acme", {"shop-healthy": ("k", 100.0)})
    first = a_check()
    assert await store.claim_check(first, "shop-healthy", 100.0, 200.0, None) == "started"
    assert await store.claim_check(a_check(), "shop-healthy", 200.0, 300.0, None) == "skipped"
    row = await store.check_schedule("acme", "shop-healthy")
    assert row and (row["next_due"], row["last_id"]) == (300.0, first.id)
    assert row["skipped_reason"] == "its previous Run is still open"
    await store.save(first.model_copy(update={"status": "done"}))
    assert await store.claim_check(a_check(), "shop-healthy", 300.0, 400.0, None) == "started"


@pytest.mark.anyio
async def test_a_skip_records_why_and_starts_nothing(store: Store) -> None:
    await store.sync_checks("acme", {"shop-healthy": ("k", 100.0)})
    t = a_check()
    assert await store.claim_check(t, "shop-healthy", 100.0, 200.0, "over budget") == "skipped"
    assert await store.get(t.id) is None
    row = await store.check_schedule("acme", "shop-healthy")
    assert row and (row["skipped_at"], row["skipped_reason"], row["last_id"]) == (
        100.0,
        "over budget",
        None,
    )


@pytest.mark.anyio
async def test_a_changed_schedule_is_due_anew_an_unchanged_one_keeps_its_time(
    store: Store,
) -> None:
    await store.sync_checks("acme", {"a": ("k1", 100.0), "b": ("k1", 100.0)})
    await store.sync_checks("acme", {"a": ("k1", 999.0), "b": ("k2", 999.0)})
    a, b = await store.check_schedule("acme", "a"), await store.check_schedule("acme", "b")
    assert a and b and (a["next_due"], b["next_due"]) == (100.0, 999.0)


@pytest.mark.anyio
async def test_the_open_failure_is_set_only_from_what_it_was(store: Store) -> None:
    await store.sync_checks("acme", {"shop-healthy": ("k", 100.0)})
    assert await store.set_failing("acme", "shop-healthy", "t1", None)
    assert not await store.set_failing("acme", "shop-healthy", "t2", None)  # t1 opened it
    assert await store.set_failing("acme", "shop-healthy", None, "t1")


@pytest.mark.anyio
async def test_passing_checks_are_left_out_of_the_list(store: Store) -> None:
    ok = CheckResult(passed=True, summary="x", evidence=[Evidence(**EV)])
    bad = CheckResult(passed=False, summary="x", evidence=[Evidence(**EV)], report=REPORT)
    for id_, verdict in [("pass", ok), ("fail", bad), ("running", None)]:
        await store.insert(a_check().model_copy(update={"id": id_, "check_result": verdict}))
    assert {t.id for t in await store.recent(10)} == {"fail", "running"}


# The Hub


def test_a_passing_check_is_quiet(
    settings: Settings, fake_cluster: Any, slack: FakeSlack, database_url: str
) -> None:
    with hub(settings, fake_cluster, passed(), slack=slack.transport(), acme_channel=ACME) as c:
        (tid,) = tick(c, now() + DAY)
        t = wait_for(c, tid, "done")
        time.sleep(0.3)  # anything it would post is posted by now
    assert t["kind"] == "check" and t["check_result"]["passed"] is True and t["report"] is None
    assert slack.calls == []
    assert schedule(database_url)["last_id"] == tid


def test_an_open_failure_keeps_one_thread_until_it_recovers(
    settings: Settings, fake_cluster: Any, slack: FakeSlack, database_url: str
) -> None:
    steps = (failed(1), failed(2), passed(3))
    with hub(settings, fake_cluster, *steps, slack=slack.transport(), acme_channel=ACME) as c:
        (first,) = tick(c, now() + DAY)
        t = wait_for(c, first, "done")
        until(lambda: schedule(database_url)["failing_id"] == first)
        until(lambda: len(posts(slack)) == 2)  # the parent, then the Report in its thread
        parent = posts(slack)[0]
        assert parent[1] is None and "Check shop-healthy" in parent[0]
        assert ":x: Check failed" in parent[0] and "done" not in parent[0]
        assert t["report"]["summary"] == REPORT["summary"]
        assert schedule(database_url)["failing_id"] == first
        thread = first_thread(c, first)  # set after the parent post, not at "done"

        (second,) = tick(c, now() + 2 * DAY)
        wait_for(c, second, "done")
        until(lambda: any("Still failing" in p for p, _ in posts(slack)))
        (third,) = tick(c, now() + 3 * DAY)
        wait_for(c, third, "done")
        until(lambda: any("Recovered" in p for p, _ in posts(slack)))
    later = [p for p in posts(slack) if "Still failing" in p[0] or "Recovered" in p[0]]
    assert later == [
        (f":x: Still failing: checkout crash loops. · `{second}`", thread),
        (f":white_check_mark: Recovered: All pods in shop run. · `{third}`", thread),
    ]
    assert len([p for p in posts(slack) if p[1] is None]) == 1  # one parent in all
    assert schedule(database_url)["failing_id"] is None


def test_a_failure_that_needs_a_decision_gets_its_own_thread_and_resumes(
    settings: Settings, fake_cluster: Any, slack: FakeSlack, database_url: str
) -> None:
    steps = (failed(1), EVICT, passed(2))
    with hub(
        settings, fake_cluster, *steps, actions=True, slack=slack.transport(), acme_channel=ACME
    ) as c:
        (first,) = tick(c, now() + DAY)
        wait_for(c, first, "done")
        until(lambda: len([p for p in posts(slack) if p[1] is None]) == 1)
        (second,) = tick(c, now() + 2 * DAY)
        t = wait_for(c, second, "awaiting_approval")
        until(lambda: len([p for p in posts(slack) if p[1] is None]) == 2)
        until(lambda: schedule(database_url)["failing_id"] == second)
        until(lambda: any("Continued in a new thread" in p for p, _ in posts(slack)))
        (moved,) = [p for p in posts(slack) if "Continued in a new thread" in p[0]]
        assert moved == (
            f":arrow_right: Continued in a new thread: `{second}`",
            first_thread(c, first),
        )
        assert tick(c, now() + 3 * DAY) == []  # its last Run is still open
        assert schedule(database_url)["skipped_reason"] == "its previous Run is still open"
        c.post(f"/investigations/{second}/decision", json={"approve": True}, headers=AUTH)
        done = wait_for(c, second, "done")  # resumed from its checkpoint
        until(lambda: any("Recovered" in p for p, _ in posts(slack)))
    assert done["check_result"]["passed"] is True
    assert done["executed_actions"]
    (recovered,) = [p for p in posts(slack) if "Recovered" in p[0]]
    assert recovered[1] == done["slack_ts"] and t["id"] == second  # in its own thread
    verdicts = [p for p in posts(slack) if "Passed:" in p[0]]
    assert [p[1] for p in verdicts] == [done["slack_ts"]]  # the resumed Run's verdict
    assert schedule(database_url)["failing_id"] is None


def test_a_check_whose_run_fails_counts_as_failing(
    settings: Settings, fake_cluster: Any, slack: FakeSlack, database_url: str
) -> None:
    with hub(settings, fake_cluster, failed(1), slack=slack.transport(), acme_channel=ACME) as c:
        (first,) = tick(c, now() + DAY)
        wait_for(c, first, "done")
        until(lambda: schedule(database_url)["failing_id"] == first)
        (second,) = tick(c, now() + 2 * DAY)  # the scripted model has nothing left
        wait_for(c, second, "failed")
        until(lambda: any("Still failing: the Run failed" in p for p, _ in posts(slack)))


def test_an_over_budget_check_is_skipped_quietly(
    settings: Settings, fake_cluster: Any, slack: FakeSlack, database_url: str
) -> None:
    settings.model_prices = {settings.model: HAIKU}
    with hub(
        settings,
        fake_cluster,
        slack=slack.transport(),
        acme_channel=ACME,
        acme={"monthly_budget_usd": 0.01},
    ) as c:
        with psycopg.connect(database_url) as conn:
            conn.execute(
                "insert into usage (contract_id, model, input_tokens, output_tokens, cost_usd, "
                "at) values ('acme', 'fake', 0, 0, 1.0, %s)",
                (now(),),
            )
        assert tick(c, now() + DAY) == []
    row = schedule(database_url)
    assert "monthly budget" in row["skipped_reason"] and row["last_id"] is None
    assert slack.calls == []


def test_a_check_removed_from_its_contract_is_ignored(
    settings: Settings, fake_cluster: Any, database_url: str
) -> None:
    with hub(settings, fake_cluster, passed()) as c:
        with psycopg.connect(database_url) as conn:
            conn.execute(
                "insert into check_schedules (contract_id, name, schedule, next_due) "
                "values ('acme', 'gone', 'x', 0)"
            )
        started = tick(c, now() + DAY)
        assert len(started) == 1  # shop-healthy only
        wait_for(c, started[0], "done")
    assert schedule(database_url, "gone")["last_id"] is None


def test_a_check_gets_the_general_teachings_and_its_prompt(
    settings: Settings, fake_cluster: Any, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    Recording.sent = []
    monkeypatch.setattr(test_api, "scripted", lambda *s: Recording(messages=iter(s)))
    seed(database_url)
    with hub(settings, fake_cluster, passed(), more=bank()) as c:
        (tid,) = tick(c, now() + DAY)
        wait_for(c, tid, "done")
    (sent,) = Recording.sent
    system, user = str(sent[0].content), str(sent[1].content)
    assert system.startswith(agent.CHECK_PROMPT) and "ACME-GENERAL" in system
    for never in ("ACME-CRASHLOOP", "ACME-OTHER-ALERT", "ACME-PROPOSED", "BANK-APPROVED"):
        assert never not in system, never
    assert CHECK["prompt"] in user and "shop-healthy" in user


def test_same_named_checks_of_two_contracts_stay_apart(
    settings: Settings,
    fake_cluster: Any,
    slack: FakeSlack,
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each Contract's Check runs with its own prompt and posts in its own channel."""
    Recording.sent = []
    monkeypatch.setattr(test_api, "scripted", lambda *s: Recording(messages=iter(s)))
    theirs = {**CHECK, "prompt": "Is the BANK ledger replicating?"}
    other = make_contract(contract_id="bank", slack_channel=BANK, checks=[theirs])
    more = {"bank": (other, make_fake_cluster())}
    steps = (failed(1), failed(2))
    with hub(
        settings, fake_cluster, *steps, slack=slack.transport(), acme_channel=ACME, more=more
    ) as c:
        started = tick(c, now() + DAY)
        found = [wait_for(c, tid, "done") for tid in started]
        until(lambda: len([p for p in posts(slack) if p[1] is None]) == 2)
        # the Open failure is set after the post returns: wait for it, not just the post
        until(
            lambda: (
                len(got := rows(database_url, "select failing_id from check_schedules")) == 2
                and all(r[0] for r in got)
            )
        )
    prompts = {"acme": CHECK["prompt"], "bank": theirs["prompt"]}
    assert sorted(t["contract_id"] for t in found) == ["acme", "bank"]
    for t in found:
        assert t["input"]["prompt"] == prompts[t["contract_id"]]
    users = sorted(str(m[1].content) for m in Recording.sent)
    assert [CHECK["prompt"] in u for u in users].count(True) == 1
    assert [theirs["prompt"] in u for u in users].count(True) == 1
    parents = [
        (p["channel"], p["text"])
        for m, p, _ in slack.calls
        if m == "chat.postMessage" and "thread_ts" not in p
    ]
    assert sorted((ch, "[acme]" in text, "[bank]" in text) for ch, text in parents) == [
        (ACME, True, False),
        (BANK, False, True),
    ]
    got = rows(
        database_url, "select contract_id, last_id, failing_id from check_schedules order by 1"
    )
    assert [r[0] for r in got] == ["acme", "bank"]
    for t in found:
        assert dict((r[0], r[1:]) for r in got)[t["contract_id"]] == (t["id"], t["id"])


def test_the_console_shows_checks_and_takes_feedback_on_a_failing_one(
    settings: Settings, fake_cluster: Any, database_url: str
) -> None:
    settings.console_operators = {LOGIN}
    with hub(settings, fake_cluster, failed()) as c:
        (tid,) = tick(c, now() + DAY)
        wait_for(c, tid, "done")
        checks = c.get("/api/v1/contracts/acme/checks", headers=VIEW).json()["checks"]
        page = c.get(f"/api/v1/investigations/{tid}", headers=VIEW).json()
        saved = c.get(f"/api/v1/investigations/{tid}/scenario.yaml", headers=VIEW)
        r = c.post(f"/api/v1/investigations/{tid}/feedback", json={"correct": True}, headers=SAME)
    (shop,) = checks
    assert shop["name"] == "shop-healthy" and shop["schedule"].startswith("*/15 * * * *")
    assert shop["last_passed"] is False and shop["last_id"] == tid  # failed
    assert page["kind"] == "check" and page["failed"] is True and page["can_judge"] is True
    assert page["check_result"]["passed"] is False
    assert saved.status_code == 200 and "check:" in saved.text  # a draft Scenario (part b)
    assert r.status_code == 200
    assert rows(database_url, "select correct from feedback") == [(True,)]


def test_the_api_lists_failing_checks_not_passing_ones(
    settings: Settings, fake_cluster: Any
) -> None:
    with hub(settings, fake_cluster, passed(1), failed(2)) as c:
        (one,) = tick(c, now() + DAY)
        wait_for(c, one, "done")
        (two,) = tick(c, now() + 2 * DAY)
        wait_for(c, two, "done")
        listed = [t["id"] for t in c.get("/investigations", headers=AUTH).json()]
    assert two in listed and one not in listed


def test_check_results_are_valid_json_schema() -> None:
    schema = CheckResult.model_json_schema()
    assert json.dumps(schema) and schema["required"] == ["passed", "summary", "evidence"]


def test_the_scheduler_runs_a_check_missed_while_the_hub_was_down(
    settings: Settings, fake_cluster: Any, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from squero import api
    from tests.test_teachings import migrated

    monkeypatch.setattr(api, "CHECK_TICK", 0.05)
    # the scheduler's clock held mid-quarter: run on the real one, a test that started
    # just before :00, :15, :30 or :45 saw the next fire time come while it waited
    at = time.time() // 900 * 900 + 450
    monkeypatch.setattr(api, "now", lambda: at)
    migrated(database_url)
    with psycopg.connect(database_url) as conn:  # due long ago, same schedule as configured
        conn.execute(
            "insert into check_schedules (contract_id, name, schedule, next_due) "
            "values ('acme', 'shop-healthy', '*/15 * * * * UTC', 0)"
        )
    with hub(settings, fake_cluster, passed()) as c:
        until(lambda: schedule(database_url)["last_id"] is not None)
        wait_for(c, schedule(database_url)["last_id"], "done")
        time.sleep(0.2)  # more ticks: not due again until the next fire time
    row = schedule(database_url)
    assert rows(database_url, "select count(*) from investigations") == [(1,)]
    assert row["next_due"] == at + 450  # the next quarter hour after the run, not before


def test_slack_feedback_on_a_failing_checks_report(
    settings: Settings,
    fake_cluster: Any,
    buttons: FakeSocket,  # noqa: F811 (the imported fixture)
    database_url: str,
) -> None:
    slack = FakeSlack(buttons.url)
    with hub(settings, fake_cluster, failed(), slack=slack.transport(), acme_channel=ACME) as c:
        (tid,) = tick(c, now() + DAY)
        wait_for(c, tid, "done")
        until(lambda: "feedback_right" in posted(slack))
        press(buttons, slack, "feedback_right")
        until(lambda: rows(database_url, "select count(*) from feedback") == [(1,)])
    assert rows(database_url, "select investigation_id, correct from feedback") == [(tid, True)]
