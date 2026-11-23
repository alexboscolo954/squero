"""Outbound webhooks in the Hub (slice 19.3): each event fires where the Operator's thread
is told, once. A Triage's Report, a decision it waits for and gets (never the Hub's
instruction to the model), what a Run executed, a failed Run; a Check as its thread speaks;
a Rollback and a Stale notice only once Slack took them; nothing for a Question. A receiver
that fails never fails the Hub's work, and one Contract's events never reach another's URL."""

import json
import time
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from squero import actuator, contracts, notices, webhooks
from squero.api import create_app
from squero.config import Settings
from squero.store import Store, now
from tests.conftest import REPORT, call, make_contract, make_fake_cluster, scripted
from tests.test_actuator import (
    A_TOKEN,
    AUTO,
    actuated,
    as_actuator,
    automating,
    report,
    run,
    sized,
    sql,
    tell,
)
from tests.test_api import (  # noqa: F401 - _database: every test here needs Postgres
    ALERT,
    AUTH,
    BANK_HOOK,
    BANK_URL,
    EVICT_THEN_REPORT,
    HOOK,
    RESOLVED,
    URL,
    _database,
    client,
    offline,
    wait_for,
)
from tests.test_app_investigations import SAME
from tests.test_app_questions import ASK
from tests.test_checks import CHECK, DAY, failed, passed, tick
from tests.test_console import ANSWER_1, VIEW
from tests.test_console import OPERATOR as LOGIN
from tests.test_notices import CHANNEL, Posts
from tests.test_notices import contract as noticed
from tests.test_sizing_store import NOW, row, store, stored  # noqa: F401 - store: a fixture
from tests.test_slack import ACME, EVICT_AGAIN, TOKEN, FakeSlack, until
from tests.test_webhooks import HOOK as BLOCK
from tests.test_webhooks import SECRETS, SHOWN, SIGNING

HOOKED = {"webhook": BLOCK, "secrets": SECRETS}
ONE = call("TriageReport", "r1", **REPORT)
SENTINEL = "label-only-the-raw-alert-has"
FIRING = {**ALERT, "commonLabels": {**ALERT["commonLabels"], "team": SENTINEL}}
H = 3600.0


class Receiver:
    """The webhook's other end, in the Hub's one outbound client: each request kept;
    `answers` overrides the next replies."""

    def __init__(self) -> None:
        self.seen: list[httpx.Request] = []
        self.answers: list[httpx.Response] = []

    def __call__(self, r: httpx.Request) -> httpx.Response:
        self.seen.append(r)
        return self.answers.pop(0) if self.answers else httpx.Response(204)

    def events(self, host: str = "hooks.acme.example") -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.seen if r.url.host == host]

    def named(self, host: str = "hooks.acme.example") -> list[str]:
        return [e["event"] for e in self.events(host)]


def routed(receiver: Receiver, slack: Any = None) -> httpx.MockTransport:
    return httpx.MockTransport(
        lambda r: receiver(r) if (r.url.host or "").startswith("hooks.") else (slack or offline)(r)
    )


def test_a_triage_sends_its_report_once_signed_without_the_raw_alert(
    settings: Settings, fake_cluster: Any
) -> None:
    receiver = Receiver()
    with client(settings, fake_cluster, ONE, slack=routed(receiver), acme=HOOKED) as c:
        tid = c.post(URL, json=FIRING, headers=HOOK).json()["investigation"]
        t = wait_for(c, tid, "done")
        until(lambda: receiver.named() == ["report.finished"])
        assert SENTINEL in json.dumps(t["input"])  # the Hub has it; the event doesn't
    (r,) = receiver.seen
    (event,) = receiver.events()
    assert event["version"] == 1 and event["contract"] == "acme"
    assert event["investigation"] == {
        "id": tid,
        "kind": "triage",
        "title": t["title"],
        "status": "done",
        "started": t["started"],
    }
    assert event["report"] == SHOWN and event["executed_actions"] == []
    assert set(event) == {
        "version", "id", "event", "contract", "at", "investigation", "report", "executed_actions"
    }  # fmt: skip
    assert SENTINEL not in r.content.decode() and "groupKey" not in r.content.decode()
    at = int(r.headers["squero-signature"].split(",")[0][2:])
    assert r.headers["squero-signature"] == webhooks.signature(SecretStr(SIGNING), at, r.content)
    assert abs(at - time.time()) < 60


def test_a_decision_is_asked_for_given_and_followed_by_what_ran_then_the_report(
    settings: Settings, fake_cluster: Any
) -> None:
    receiver = Receiver()
    with client(
        settings, fake_cluster, *EVICT_THEN_REPORT, actions=True, slack=routed(receiver),
        acme=HOOKED,
    ) as c:  # fmt: skip
        tid = c.post(URL, json=ALERT, headers=HOOK).json()["investigation"]
        t = wait_for(c, tid, "awaiting_approval")
        until(lambda: receiver.named() == ["approval.needed"])
        asked = receiver.events()[0]
        assert asked["proposed"] == [
            {
                "name": "kubernetes_evict_pod",
                "args": {"cluster": "test", "namespace": "shop", "name": "checkout-7d9f-abc"},
            }
        ]
        assert asked["deadline"] == t["deadline"] and asked["investigation"]["status"] == (
            "awaiting_approval"
        )
        c.post(f"/investigations/{tid}/decision", json={"approve": True}, headers=AUTH)
        done = wait_for(c, tid, "done")
        until(lambda: len(receiver.seen) == 4)
    assert receiver.named() == [
        "approval.needed",
        "approval.decided",
        "action.executed",
        "report.finished",
    ]
    _, decided, ran, finished = receiver.events()
    assert {k: decided[k] for k in ("approve", "by", "actions", "reason")} == {
        "approve": True,
        "by": "operator",
        "actions": ["kubernetes_evict_pod"],
        "reason": None,
    }
    assert decided["investigation"]["id"] == tid
    assert len(done["executed_actions"]) == 1  # from the Hub's own record, never the model's
    assert ran["actions"] == done["executed_actions"] == finished["executed_actions"]
    assert finished["report"]["summary"] == REPORT["summary"]


def test_each_run_sends_only_what_it_executed_itself(settings: Settings, fake_cluster: Any) -> None:
    receiver = Receiver()
    steps = (EVICT_THEN_REPORT[0], EVICT_AGAIN, EVICT_THEN_REPORT[1])
    with client(
        settings, fake_cluster, *steps, actions=True, slack=routed(receiver), acme=HOOKED
    ) as c:
        tid = c.post(URL, json=ALERT, headers=HOOK).json()["investigation"]
        for asked in (1, 4):  # two decisions, one after the other
            until(lambda n=asked: len(receiver.seen) == n)
            wait_for(c, tid, "awaiting_approval")
            c.post(f"/investigations/{tid}/decision", json={"approve": True}, headers=AUTH)
        done = wait_for(c, tid, "done")
        until(lambda: len(receiver.seen) == 7)
    assert receiver.named() == [
        "approval.needed",
        "approval.decided",
        "action.executed",
        "approval.needed",
        "approval.decided",
        "action.executed",
        "report.finished",
    ]
    first, second = (e["actions"] for e in receiver.events() if e["event"] == "action.executed")
    assert len(first) == len(second) == 1  # the second Run's alone, not both again
    assert [*first, *second] == done["executed_actions"]
    assert receiver.events()[-1]["executed_actions"] == done["executed_actions"]


def test_a_rejection_carries_the_operators_reason_and_nothing_ran(
    settings: Settings, fake_cluster: Any
) -> None:
    receiver = Receiver()
    with client(
        settings, fake_cluster, *EVICT_THEN_REPORT, actions=True, slack=routed(receiver),
        acme=HOOKED,
    ) as c:  # fmt: skip
        tid = c.post(URL, json=ALERT, headers=HOOK).json()["investigation"]
        wait_for(c, tid, "awaiting_approval")
        said = {"approve": False, "message": "not during the sale"}
        c.post(f"/investigations/{tid}/decision", json=said, headers=AUTH)
        wait_for(c, tid, "done")
        until(lambda: len(receiver.seen) == 3)
    assert receiver.named() == ["approval.needed", "approval.decided", "report.finished"]
    _, decided, finished = receiver.events()
    assert (decided["approve"], decided["by"], decided["reason"]) == (
        False,
        "operator",
        "not during the sale",
    )
    assert finished["executed_actions"] == []


def test_a_stale_decision_says_why_never_what_the_hub_tells_the_model(
    settings: Settings, fake_cluster: Any
) -> None:
    receiver = Receiver()
    with client(
        settings, fake_cluster, *EVICT_THEN_REPORT, actions=True, slack=routed(receiver),
        acme=HOOKED,
    ) as c:  # fmt: skip
        tid = c.post(URL, json=ALERT, headers=HOOK).json()["investigation"]
        wait_for(c, tid, "awaiting_approval")
        c.post(URL, json=RESOLVED, headers=HOOK)
        wait_for(c, tid, "done")
        until(lambda: len(receiver.seen) == 3)
    _, decided, _ = receiver.events()
    assert (decided["approve"], decided["by"]) == (False, "stale")
    assert decided["reason"] == "The alert group resolved before a decision"
    sent = b"".join(r.content for r in receiver.seen).decode()
    assert "Do not retry" not in sent and "finish the report" not in sent


def test_a_failed_run_is_an_event_with_its_error_and_no_report(
    settings: Settings, fake_cluster: Any
) -> None:
    receiver = Receiver()
    with client(settings, fake_cluster, slack=routed(receiver), acme=HOOKED) as c:  # no model step
        tid = c.post(URL, json=ALERT, headers=HOOK).json()["investigation"]
        t = wait_for(c, tid, "failed")
        until(lambda: receiver.named() == ["investigation.failed"])
    (event,) = receiver.events()
    assert event["error"] == t["error"] and event["error"]
    assert event["investigation"]["status"] == "failed"
    assert "report" not in event and "executed_actions" not in event


def test_a_receiver_that_fails_never_fails_the_run(
    settings: Settings, fake_cluster: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(webhooks, "BACKOFF", (0.0, 0.0, 0.0))
    receiver = Receiver()
    receiver.answers = [httpx.Response(500)] * 4
    settings.console_operators = {LOGIN}
    with client(settings, fake_cluster, ONE, slack=routed(receiver), acme=HOOKED) as c:
        fresh = c.get("/api/v1/contracts/acme/health", headers=VIEW).json()["integrations"]
        tid = c.post(URL, json=ALERT, headers=HOOK).json()["investigation"]
        assert wait_for(c, tid, "done")["report"]["summary"] == REPORT["summary"]
        until(lambda: len(receiver.seen) == 4)  # the first try and its three retries
        until(
            lambda: (
                not c.get("/api/v1/contracts/acme/health", headers=VIEW).json()["integrations"][0][
                    "ok"
                ]
            )
        )
        after = c.get("/api/v1/contracts/acme/health", headers=VIEW)
    assert len({r.headers["squero-id"] for r in receiver.seen}) == 1
    # Health: by the last delivery, with no request made to say so
    assert (fresh[0]["name"], fresh[0]["ok"]) == ("webhook", True)
    assert fresh[0]["detail"] == "no event sent since the Hub started"
    last = after.json()["integrations"][0]
    assert last["name"] == "webhook" and last["detail"].endswith(": HTTP 500")
    assert "9f8e7d6c5b4a" not in after.text and len(receiver.seen) == 4


def test_no_webhook_no_event_and_no_health_row(settings: Settings, fake_cluster: Any) -> None:
    receiver = Receiver()
    settings.console_operators = {LOGIN}
    with client(settings, fake_cluster, ONE, slack=routed(receiver)) as c:
        tid = c.post(URL, json=ALERT, headers=HOOK).json()["investigation"]
        wait_for(c, tid, "done")
        rows = c.get("/api/v1/contracts/acme/health", headers=VIEW).json()["integrations"]
        time.sleep(0.3)  # anything it would send is sent by now
    assert receiver.seen == [] and "webhook" not in [p["name"] for p in rows]


def test_one_contracts_events_never_reach_anothers_url(
    settings: Settings, fake_cluster: Any
) -> None:
    """Isolation: two Contracts, each its own URL and signing secret."""
    receiver = Receiver()
    theirs = {"hook-url": "https://hooks.bank.example/in", "hook-key": "whk-bank-signing-key"}
    bank = make_contract(
        contract_id="bank", webhook_token="bank-token", webhook=BLOCK, secrets=theirs
    )
    two = (ONE, call("TriageReport", "r2", **REPORT))
    with client(
        settings, fake_cluster, *two, slack=routed(receiver), acme=HOOKED,
        more={"bank": (bank, make_fake_cluster())},
    ) as c:  # fmt: skip
        mine = c.post(URL, json=ALERT, headers=HOOK).json()["investigation"]
        wait_for(c, mine, "done")
        other = c.post(BANK_URL, json=ALERT, headers=BANK_HOOK).json()["investigation"]
        wait_for(c, other, "done")
        until(lambda: len(receiver.seen) == 2)
    (a,), (b,) = receiver.events(), receiver.events("hooks.bank.example")
    assert (a["contract"], a["investigation"]["id"]) == ("acme", mine)
    assert (b["contract"], b["investigation"]["id"]) == ("bank", other)
    for r in receiver.seen:
        key = SIGNING if r.url.host == "hooks.acme.example" else "whk-bank-signing-key"
        at = int(r.headers["squero-signature"].split(",")[0][2:])
        assert r.headers["squero-signature"] == webhooks.signature(SecretStr(key), at, r.content)


def test_a_question_sends_nothing(settings: Settings, fake_cluster: Any) -> None:
    receiver = Receiver()
    settings.console_operators = {LOGIN}
    steps = [call("Answer", "a1", **ANSWER_1), call("TriageReport", "r1", **REPORT)]
    with client(settings, fake_cluster, *steps, slack=routed(receiver), acme=HOOKED) as c:
        qid = c.post(ASK, json={"question": "is checkout healthy?"}, headers=SAME).json()["id"]
        wait_for(c, qid, "done")
        tid = c.post(URL, json=ALERT, headers=HOOK).json()["investigation"]
        wait_for(c, tid, "done")
        until(lambda: len(receiver.seen) >= 1)
        time.sleep(0.3)
    (event,) = receiver.events()  # the Triage's alone: the Operator's own conversation, none
    assert (event["event"], event["investigation"]["id"]) == ("report.finished", tid)


def test_a_check_speaks_as_its_thread_does(settings: Settings, fake_cluster: Any) -> None:
    """Failing: its Report. Recovered: once. Passing with nothing open: quiet."""
    settings.slack_bot_token = SecretStr(TOKEN)
    receiver, slack = Receiver(), FakeSlack()
    steps = (failed(1), passed(2), passed(3))
    acme = {"checks": [CHECK], **HOOKED}
    with client(
        settings, fake_cluster, *steps, slack=routed(receiver, slack), acme_channel=ACME, acme=acme
    ) as c:
        (first,) = tick(c, now() + DAY)
        wait_for(c, first, "done")
        until(lambda: len(receiver.seen) == 1)
        until(lambda: len(slack.calls) >= 2)  # its thread is open: the next pass recovers it
        (second,) = tick(c, now() + 2 * DAY)
        wait_for(c, second, "done")
        until(lambda: len(receiver.seen) == 2)
        (third,) = tick(c, now() + 3 * DAY)
        wait_for(c, third, "done")
        time.sleep(0.5)  # anything it would send is sent by now
    broke, recovered = receiver.events()
    assert receiver.named() == ["report.finished"] * 2  # the third, passing: quiet
    assert broke["investigation"] == {
        "id": first,
        "kind": "check",
        "title": broke["investigation"]["title"],
        "status": "done",
        "started": broke["investigation"]["started"],
        "check": "shop-healthy",
    }
    assert broke["verdict"]["passed"] is False and broke["report"]["summary"] == REPORT["summary"]
    assert recovered["investigation"]["id"] == second and recovered["verdict"]["passed"] is True
    assert "report" not in recovered


def test_a_rollback_is_an_event_once_slack_took_it_and_never_twice(
    settings: Settings, database_url: str
) -> None:
    settings.slack_bot_token = SecretStr(TOKEN)
    settings.database_url = SecretStr(database_url)
    settings.approver_token = SecretStr("approver-token")
    receiver, slack = Receiver(), FakeSlack()
    extra = {"slack": {"operator_channel": "C0123456789"}, "webhook": BLOCK}
    acme = contracts.build(
        {**actuated("acme", A_TOKEN).model_dump(mode="json", exclude_none=True), **extra},
        secrets={k: SecretStr(v) for k, v in {**SECRETS, "act": A_TOKEN, "r": "r" * 40}.items()},
    )
    app = create_app(
        settings,
        scripted(),
        lambda c: None,  # type: ignore[arg-type, return-value]
        {"acme": acme},
        slack_http=httpx.AsyncClient(transport=routed(receiver, slack)),
    )
    back = report(outcome="rolled_back", reason="api-1-a: app was OOM-killed: 2 pods back")
    said = actuator.STEP_SAID + "raised after an OOM kill"
    more = {"run_id": 0, "memory": 228.0 * 2**20}
    up = report(**more, cpu=0.5, outcome="stepped_up", at=120.0, reason=said)
    burst = report(
        **more, cpu=2.0, name="web", outcome="burst", at=130.0, reason=actuator.BURST_SAID + "up"
    )
    refused = httpx.Response(200, json={"ok": False, "error": "not_in_channel"})
    with TestClient(app) as c:
        slack.answers = [refused]
        c.post("/actuator/reports", json=[back, report(at=1.0)], headers=as_actuator(A_TOKEN))
        tell(c)
        time.sleep(0.2)
        assert receiver.seen == []  # Slack didn't take it: no event, and its retries send none
        tell(c, at=time.time() + 4000)  # after the Contract's rest: Slack takes it now
        until(lambda: len(receiver.seen) == 1)
        bare = report(run_id=0, cpu=None, name="db", outcome="stepped_up", at=140.0, reason=said)
        again = [back, up, burst, bare]  # the Rollback sent again: told once
        c.post("/actuator/reports", json=again, headers=as_actuator(A_TOKEN))
        tell(c, at=time.time() + 4000)
        until(lambda: len(receiver.seen) == 4)
        tell(c, at=time.time() + 4000)
        time.sleep(0.2)
    rolled, stepped, raised, unsaid = receiver.events()
    assert receiver.named() == ["rollback", *["actuation"] * 3]  # the applied one: never
    assert "memory" not in unsaid and "cpu" not in unsaid  # a Step-up that reported no limit
    assert {k: v for k, v in rolled.items() if k not in ("id", "at", "version")} == {
        "event": "rollback",
        "contract": "acme",
        "cluster": "test",
        "namespace": "shop",
        "kind": "Deployment",
        "name": "api",
        "container": "app",
        "outcome": "rolled_back",
        "reason": "api-1-a: app was OOM-killed: 2 pods back",
        "actuated_at": 100.0,  # the Actuator's own time; `at` stays when the event was made
    }  # no cpu or memory: Slack's Rollback line names none
    assert rolled["at"].startswith("20") and rolled["version"] == 1
    # the limit Slack's line names, and no other: a Step-up's memory, a Burst's CPU
    assert (stepped["outcome"], stepped["memory"], "cpu" in stepped) == (
        "stepped_up",
        228.0 * 2**20,
        False,
    )
    assert (raised["outcome"], raised["cpu"], "memory" in raised) == ("burst", 2.0, False)


def test_the_rollback_that_ends_automation_says_so(settings: Settings, database_url: str) -> None:
    settings.slack_bot_token = SecretStr(TOKEN)
    settings.database_url = SecretStr(database_url)
    settings.approver_token = SecretStr("approver-token")
    receiver, slack = Receiver(), FakeSlack()
    extra = {"slack": {"operator_channel": "C0123456789"}, "webhook": BLOCK}
    plain = automating("acme", A_TOKEN, AUTO).model_dump(mode="json", exclude_none=True)
    acme = contracts.build(
        {**plain, **extra},
        secrets={k: SecretStr(v) for k, v in {**SECRETS, "act": A_TOKEN, "r": "r" * 40}.items()},
    )
    app = create_app(
        settings,
        scripted(),
        lambda c: None,  # type: ignore[arg-type, return-value]
        {"acme": acme},
        slack_http=httpx.AsyncClient(transport=routed(receiver, slack)),
    )
    at, day = time.time(), 86400.0
    with TestClient(app) as c:
        sql(database_url, run("acme", '{"test": null}'), sized("acme", 1))
        for ago in (3, 2, 1):  # the third in 7 days ends its Automation
            back = report(outcome="rolled_back", reason="r", at=at - ago * day)
            c.post("/actuator/reports", json=[back], headers=as_actuator(A_TOKEN))
            tell(c)
        until(lambda: len(receiver.seen) == 3)
    assert receiver.named() == ["rollback"] * 3
    assert ["automation_ended" in e for e in receiver.events()] == [False, False, True]
    assert receiver.events()[2]["automation_ended"] is True


def test_a_relays_notice_carries_no_error_as_its_notice_shows_none() -> None:
    offline = "offline since 1972-09-27 00:00 UTC"
    assert notices._event(("edge", "relay"), "failing", NOW, offline) == {
        "cluster": "edge",
        "kind": "relay",
        "state": "failing",
        "since": NOW,
    }
    assert notices._event(("test", "bill"), "failing", NOW, "HTTP 403")["error"] == "HTTP 403"
    assert "error" not in notices._event(("test", "bill"), "recovered", NOW, None)


@pytest.mark.anyio
async def test_a_recovery_slack_didnt_take_is_no_event(store: Store) -> None:  # noqa: F811
    await stored(store, "acme", NOW, {"test": "boom"})
    posts, events, c = Posts(), [], noticed()
    await notices.tick(store, c, NOW + 3 * H, posts, None, lambda e, d: events.append(d["state"]))
    await stored(store, "acme", NOW + 4 * H, {"test": None}, row())
    posts.refuse = True
    await notices.tick(store, c, NOW + 5 * H, posts, None, lambda e, d: events.append(d["state"]))
    assert events == ["failing"]  # what Slack got, and nothing more


@pytest.mark.anyio
async def test_a_stale_notice_is_an_event_each_time_slack_took_one(
    store: Store,  # noqa: F811
) -> None:
    await stored(store, "acme", NOW - H, {"test": None}, row())
    await stored(store, "acme", NOW, {"test": "ConnectError: token=abcd1234efgh refused"})
    posts, events = Posts(), []
    c = noticed()

    async def look(at: float) -> None:
        await notices.tick(store, c, at, posts, None, lambda e, d: events.append((e, d)))

    posts.refuse = True
    await look(NOW + 3 * H)
    await look(NOW + 3 * H + 600)
    assert events == []  # a post Slack didn't take is tried again: no event for the tries
    posts.refuse = False
    await look(NOW + 3 * H + 1200)
    await look(NOW + 3 * H + 1800)  # already noticed: nothing more
    failing = {"cluster": "test", "kind": "sizing", "state": "failing", "since": NOW}
    error = {"error": "ConnectError: token=abcd1234efgh refused"}  # redacted when it is sent
    assert events == [("stale_notice", failing | error)]
    await look(NOW + 3 * H + 1200 + 86400)
    assert events[1] == ("stale_notice", failing | error | {"state": "still_failing"})
    await stored(store, "acme", NOW + 2 * 86400, {"test": None}, row())
    await look(NOW + 2 * 86400 + 60)
    assert events[2] == ("stale_notice", failing | {"state": "recovered"})
    assert len(events) == 3 and len(posts.sent) == 3 and posts.sent[0][0] == CHANNEL
    await notices.tick(store, c, NOW + 3 * 86400, posts, None)  # no webhook: as before
    assert len(events) == 3


def test_the_hub_sends_a_stale_notice_to_the_contracts_webhook(
    settings: Settings, fake_cluster: Any, database_url: str
) -> None:
    import asyncio

    from squero.store import connect
    from tests.test_sizing_digest import PROM as prom

    async def seed() -> None:
        async with connect(database_url) as pool:
            s = Store(pool)
            await s.migrate()
            await stored(s, "acme", NOW, {"test": "boom token=abcd1234efgh"})

    asyncio.run(seed())
    settings.slack_bot_token = SecretStr(TOKEN)
    receiver, slack = Receiver(), FakeSlack()
    with client(
        settings, fake_cluster, slack=routed(receiver, slack), acme_channel=ACME,
        acme={"integrations": prom, **HOOKED},
    ) as c:  # fmt: skip
        assert c.portal is not None
        c.portal.call(c.app.state.due_notices, NOW + 3 * H)  # type: ignore[attr-defined]
        until(lambda: len(receiver.seen) == 1)
    (event,) = receiver.events()
    assert {k: event[k] for k in ("event", "contract", "cluster", "kind", "state", "since")} == {
        "event": "stale_notice",
        "contract": "acme",
        "cluster": "test",
        "kind": "sizing",
        "state": "failing",
        "since": NOW,
    }
    assert event["error"] == "boom token=[REDACTED]"
