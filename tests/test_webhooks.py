"""Outbound webhooks on their own (slice 19.3): the signature a receiver checks, the
allowlist of what an event holds (redacted and cut to size), and the Sender: in order per
Contract, re-signed on each of three retries, a refusal dropped at once, a redirect never
followed, and never a failure or a wait for whoever emits. The URL is a secret: only its
host is ever logged."""

import asyncio
import hashlib
import hmac
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import prometheus_client
import pytest
from pydantic import SecretStr, ValidationError

from squero import contracts, observability, webhooks
from squero.models import CheckResult, Investigation, TriageReport
from tests.conftest import REPORT, make_contract, use_age_keys
from tests.test_contracts import BASE, encrypt, problems, write

URL = "https://hooks.acme.example/in/9f8e7d6c5b4a/squero?source=hub"
SIGNING = "whk-acme-signing-key"
HOOK = {"url_secret": "hook-url", "signing_secret": "hook-key"}
SECRETS = {"hook-url": URL, "hook-key": SIGNING}


def hooked(cid: str = "acme", url: str = URL, signing: str = SIGNING) -> contracts.Contract:
    secrets = {"hook-url": url, "hook-key": signing}
    return make_contract(contract_id=cid, webhook=HOOK, secrets=secrets)


class Receiver:
    """The URL's other end: keeps each request; `answers` overrides the next replies
    (an exception is raised)."""

    def __init__(self) -> None:
        self.seen: list[httpx.Request] = []
        self.answers: list[Any] = []

    async def __call__(self, r: httpx.Request) -> httpx.Response:
        self.seen.append(r)
        reply = self.answers.pop(0) if self.answers else httpx.Response(204)
        if isinstance(reply, Exception):
            raise reply
        return reply  # type: ignore[no-any-return]

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))

    def events(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.seen]


def sender(receiver: Receiver, clock: list[float] | None = None) -> tuple[Any, list[float]]:
    naps: list[float] = []

    async def nap(seconds: float) -> None:
        naps.append(seconds)
        if clock:
            clock[0] += seconds

    at = clock or [1_800_000_000.0]
    return webhooks.Sender(receiver.client(), sleep=nap, now=lambda: at[0]), naps


def count(outcome: str) -> float:
    return webhooks.EVENTS.labels(outcome)._value.get()  # type: ignore[no-any-return]


def test_events_are_counted_in_the_hubs_own_metric_by_how_they_ended() -> None:
    assert webhooks.EVENTS is observability.WEBHOOK_EVENTS
    before = count("dropped")
    webhooks.EVENTS.labels("dropped").inc()
    text = prometheus_client.generate_latest().decode()
    assert f'squero_webhook_events_total{{outcome="dropped"}} {before + 1}' in text


def test_the_signature_is_the_hmac_of_the_time_a_dot_and_the_body() -> None:
    body = b'{"version": 1}'
    want = hmac.new(SIGNING.encode(), b"1800000000." + body, hashlib.sha256).hexdigest()
    assert webhooks.signature(SecretStr(SIGNING), 1_800_000_000, body) == f"t=1800000000,v1={want}"
    # the time is signed with the bytes: an old event can't be given a new time
    assert webhooks.signature(SecretStr(SIGNING), 1_800_000_001, body) != f"t=1800000001,v1={want}"
    assert webhooks.signature(SecretStr("other"), 1_800_000_000, body) != f"t=1800000000,v1={want}"
    assert (
        webhooks.signature(SecretStr(SIGNING), 1_800_000_000, body + b" ").split("v1=")[1] != want
    )


def test_what_leaves_is_redacted_and_cut_to_size() -> None:
    out = webhooks.scrubbed(
        {
            "text": "restarted with token=abcd1234efgh in its env",
            "long": "x" * 5000,
            "many": [f"line {i}" for i in range(60)],
            "nested": [{"args": {"password": "password=hunter2hunter2"}}, ("a", 1)],
            "kept": [1, 2.5, True, None],
            7: "a key that isn't a string",
        }
    )
    assert out["text"] == "restarted with token=[REDACTED] in its env"
    assert out["long"].startswith("x" * 4000) and "[truncated 1000 chars]" in out["long"]
    assert len(out["long"]) < 4100
    assert out["many"] == [f"line {i}" for i in range(50)]
    assert "hunter2" not in json.dumps(out) and out["nested"][1] == ["a", 1]
    assert out["kept"] == [1, 2.5, True, None] and out["7"] == "a key that isn't a string"


SENTINEL = "alert-label-only-the-raw-group-has"
EVIDENCE = [{**e, "link": None} for e in REPORT["evidence"]]  # as the Report holds it
SHOWN = {**REPORT, "evidence": EVIDENCE, "runbook": None}


def triage(**more: Any) -> Investigation:
    fields: dict[str, Any] = {
        "id": "i-1",
        "contract_id": "acme",
        "title": "KubePodCrashLooping shop",
        "started": 1_800_000_000.0,
        "status": "done",
        "report": TriageReport(**REPORT),
        "input": {"commonLabels": {"pod": SENTINEL}, "check": "shop-healthy"},
        "slack_channel": "C0ACME0001",
        "slack_ts": "1700000000.000001",
        "client_sent_by": "slack:U0OPERATOR1",
        "group_key": "g1-" + SENTINEL,
    }
    return Investigation(**{**fields, **more})


def test_an_event_holds_what_the_thread_shows_and_nothing_else_of_the_investigation() -> None:
    t = triage()
    assert webhooks.investigation(t) == {
        "id": "i-1",
        "kind": "triage",
        "title": "KubePodCrashLooping shop",
        "status": "done",
        "started": 1_800_000_000.0,
    }
    shown = webhooks.report(t)
    assert shown == {"report": SHOWN}  # the Report, with its Evidence
    whole = json.dumps([webhooks.investigation(t), shown])
    for kept_out in (SENTINEL, "C0ACME0001", "1700000000.000001", "U0OPERATOR1"):
        assert kept_out not in whole
    assert webhooks.report(triage(report=None)) == {}  # a failed Run has none


def test_a_checks_event_names_it_and_carries_its_verdict() -> None:
    evidence = REPORT["evidence"]
    verdict = CheckResult(
        passed=False,
        summary="checkout crash loops.",
        evidence=evidence,
        report=TriageReport(**REPORT),
    )
    t = triage(kind="check", report=None, check_result=verdict)
    assert webhooks.investigation(t)["check"] == "shop-healthy"  # its name, not its input
    assert webhooks.report(t) == {
        "verdict": {"passed": False, "summary": "checkout crash loops.", "evidence": EVIDENCE},
        "report": SHOWN,
    }
    fine = CheckResult(passed=True, summary="All pods run.", evidence=evidence)
    assert webhooks.report(triage(kind="check", report=None, check_result=fine)) == {
        "verdict": {"passed": True, "summary": "All pods run.", "evidence": EVIDENCE}
    }


def test_proposed_actions_are_each_tool_and_its_arguments_only() -> None:
    pending = [{"name": "kubernetes_evict_pod", "args": {"name": "checkout-1"}, "id": "call-1"}]
    assert webhooks.proposed(pending) == [
        {"name": "kubernetes_evict_pod", "args": {"name": "checkout-1"}}
    ]


@pytest.mark.anyio
async def test_an_event_is_posted_signed_with_its_id_and_the_time() -> None:
    receiver = Receiver()
    hooks, _ = sender(receiver)
    data = {"investigation": {"id": "i-1"}, "error": "boom token=abcd1234efgh"}
    hooks.emit(hooked(), "investigation.failed", data)
    await hooks.drain()
    (r,) = receiver.seen
    assert (r.method, str(r.url)) == ("POST", URL)  # the URL whole: its path's key, its query
    body = json.loads(r.content)
    event_id = body.pop("id")
    assert str(uuid.UUID(event_id)) == event_id
    assert body == {
        "version": 1,
        "event": "investigation.failed",
        "contract": "acme",
        "at": "2027-01-15T08:00:00+00:00",
        "investigation": {"id": "i-1"},
        "error": "boom token=[REDACTED]",
    }
    assert r.headers["content-type"] == "application/json"
    assert r.headers["squero-event"] == "investigation.failed"
    assert r.headers["squero-id"] == event_id
    assert r.headers["user-agent"] == "squero-webhook/1"
    # what a receiver does: the header's time and the raw body, with the shared secret
    assert r.headers["squero-signature"] == webhooks.signature(
        SecretStr(SIGNING), 1_800_000_000, r.content
    )
    assert "authorization" not in r.headers and SIGNING not in r.content.decode()
    assert r.extensions["timeout"]["read"] == 10
    assert hooks.health("acme") == (True, "last event delivered at 2027-01-15 08:00 UTC")
    await hooks.close()


@pytest.mark.anyio
async def test_no_field_of_an_event_can_stand_in_for_the_envelopes() -> None:
    receiver = Receiver()
    hooks, _ = sender(receiver)
    forged = {"version": 9, "id": "mine", "event": "x", "contract": "bank", "at": 100.0, "n": 1}
    hooks.emit(hooked(), "rollback", forged)
    await hooks.drain()
    (event,) = receiver.events()
    assert (event["version"], event["event"], event["contract"]) == (1, "rollback", "acme")
    assert event["at"] == "2027-01-15T08:00:00+00:00" and event["id"] != "mine"
    assert event["n"] == 1 and receiver.seen[0].headers["squero-id"] == event["id"]
    await hooks.close()


@pytest.mark.anyio
async def test_a_contract_without_a_webhook_sends_nothing_and_events_arrive_in_order() -> None:
    receiver = Receiver()
    hooks, _ = sender(receiver)
    hooks.emit(make_contract(), "report.finished", {})
    hooks.emit(None, "report.finished", {})  # a Contract no longer configured
    await hooks.drain()
    assert receiver.seen == [] and hooks.health("acme")[1] == "no event sent since the Hub started"
    c = hooked()
    for n in range(5):
        hooks.emit(c, "rollback", {"n": n})
    await hooks.drain()
    assert [e["n"] for e in receiver.events()] == [0, 1, 2, 3, 4]
    assert len({e["id"] for e in receiver.events()}) == 5
    await hooks.close()


@pytest.mark.anyio
async def test_a_slow_delivery_holds_the_ones_behind_it_in_order() -> None:
    """One worker per Contract: a second would let a later event overtake a slow one."""
    arrived: list[int] = []

    async def uneven(r: httpx.Request) -> httpx.Response:
        n = json.loads(r.content)["n"]
        await asyncio.sleep(0.05 if n == 0 else 0)
        arrived.append(n)
        return httpx.Response(200)

    hooks, _ = sender(Receiver())
    hooks.http = httpx.AsyncClient(transport=httpx.MockTransport(uneven))
    c = hooked()
    for n in range(4):
        hooks.emit(c, "rollback", {"n": n})
    await hooks.drain()
    assert arrived == [0, 1, 2, 3] and len(hooks.workers) == 1
    await hooks.close()


def test_the_guides_own_receiver_accepts_an_event_and_refuses_a_changed_or_old_one() -> None:
    """The snippet in docs/contracts.md, run as written."""
    guide = (Path(__file__).parent.parent / "docs" / "contracts.md").read_text()
    section = guide.split("### Checking an event in your receiver")[1]
    code = section.split("```python\n")[1].split("```")[0]
    scope: dict[str, Any] = {}
    exec(code, scope)  # noqa: S102 - the repository's own documentation
    verified = scope["verified"]
    body = b'{"version": 1, "event": "report.finished"}'
    at = int(time.time())
    header = webhooks.signature(SecretStr(SIGNING), at, body)
    assert verified(SIGNING, header, body)
    assert not verified(SIGNING, header, body + b" ")  # a changed body
    assert not verified("another-secret", header, body)
    old = webhooks.signature(SecretStr(SIGNING), at - 3600, body)  # signed, but an hour old
    assert not verified(SIGNING, old, body)
    forged = header.replace(f"t={at}", f"t={at + 1}")  # a new time on an old signature
    assert not verified(SIGNING, forged, body)


@pytest.mark.anyio
async def test_a_failure_is_retried_three_times_each_attempt_signed_afresh() -> None:
    receiver, clock = Receiver(), [1_800_000_000.0]
    receiver.answers = [
        httpx.Response(500, text="oops"),
        httpx.ConnectError(f"no route to {URL}"),
        httpx.Response(429),
        httpx.Response(200),
    ]
    hooks, naps = sender(receiver, clock)
    before = count("delivered")
    hooks.emit(hooked(), "report.finished", {"n": 1})
    hooks.emit(hooked(), "report.finished", {"n": 2})  # waits its turn behind the retries
    await hooks.drain()
    assert naps == [1.0, 5.0, 25.0]
    first, later = receiver.seen[:4], receiver.seen[4:]
    assert len({r.content for r in first}) == 1  # the same bytes, the same id
    assert len({r.headers["squero-id"] for r in first}) == 1
    times = [r.headers["squero-signature"].split(",")[0] for r in first]
    assert times == ["t=1800000000", "t=1800000001", "t=1800000006", "t=1800000031"]
    for r in first:  # each attempt's signature is its own time's
        at = int(r.headers["squero-signature"].split(",")[0][2:])
        assert r.headers["squero-signature"] == webhooks.signature(
            SecretStr(SIGNING), at, r.content
        )
    assert [json.loads(r.content)["n"] for r in later] == [2]
    assert count("delivered") == before + 2
    await hooks.close()


@pytest.mark.anyio
async def test_after_three_retries_it_is_logged_and_counted_by_its_host_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    receiver = Receiver()
    lost = httpx.ConnectError(f"no route to {URL}")  # an error's own text can quote the URL
    receiver.answers = [httpx.Response(503, text=f"echo {URL} {SIGNING}"), lost, lost, lost]
    hooks, naps = sender(receiver)
    before = count("failed")
    with caplog.at_level(logging.WARNING):
        hooks.emit(hooked(), "rollback", {"name": "api"})
        hooks.emit(hooked(), "rollback", {"name": "web"})  # the next event still goes
        await hooks.drain()
    assert len(receiver.seen) == 5 and naps == [1.0, 5.0, 25.0]
    assert count("failed") == before + 1
    assert "acme: webhook event rollback failed to hooks.acme.example: ConnectError" in caplog.text
    for secret in ("9f8e7d6c5b4a", "source=hub", SIGNING, "/in/"):
        assert secret not in caplog.text  # never the URL, nor the receiver's reply
    assert hooks.health("acme")[0] is True  # by its last delivery: the second arrived
    await hooks.close()


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410, 422, 301, 302, 307, 308, 100, 199])
@pytest.mark.anyio
async def test_a_refusal_or_a_redirect_is_dropped_at_once(status: int) -> None:
    receiver = Receiver()
    receiver.answers = [httpx.Response(status, headers={"location": "https://evil.example/"})]
    hooks, naps = sender(receiver)
    hooks.emit(hooked(), "report.finished", {})
    await hooks.drain()
    assert len(receiver.seen) == 1 and naps == []  # no retry, and no second host asked
    ok, text = hooks.health("acme")
    assert not ok and text == f"last event not delivered at 2027-01-15 08:00 UTC: HTTP {status}"
    await hooks.close()


@pytest.mark.parametrize("answer", [httpx.Response(500), httpx.Response(599), httpx.Response(429)])
@pytest.mark.anyio
async def test_a_server_error_and_too_many_requests_are_retried(answer: httpx.Response) -> None:
    receiver = Receiver()
    receiver.answers = [answer]
    hooks, naps = sender(receiver)
    hooks.emit(hooked(), "report.finished", {})
    await hooks.drain()
    assert len(receiver.seen) == 2 and naps == [1.0]
    await hooks.close()


@pytest.mark.parametrize("status", [200, 201, 202, 204, 299])
@pytest.mark.anyio
async def test_any_2xx_is_delivered(status: int) -> None:
    receiver = Receiver()
    receiver.answers = [httpx.Response(status)]
    hooks, _ = sender(receiver)
    hooks.emit(hooked(), "report.finished", {})
    await hooks.drain()
    assert len(receiver.seen) == 1 and hooks.health("acme")[0]
    await hooks.close()


@pytest.mark.anyio
async def test_an_attempt_has_ten_seconds_in_all(monkeypatch: pytest.MonkeyPatch) -> None:
    assert webhooks.SECONDS == 10
    monkeypatch.setattr(webhooks, "SECONDS", 0.05)
    calls = []

    async def slow(r: httpx.Request) -> httpx.Response:
        calls.append(r)
        if len(calls) == 1:
            await asyncio.sleep(5)
        return httpx.Response(200)

    receiver = Receiver()
    hooks, naps = sender(receiver)
    hooks.http = httpx.AsyncClient(transport=httpx.MockTransport(slow))
    hooks.emit(hooked(), "report.finished", {})
    await asyncio.wait_for(hooks.drain(), 2)
    assert len(calls) == 2 and naps == [1.0]  # the slow one given up on, then retried
    await hooks.close()


@pytest.mark.parametrize(
    ("url", "refusal"),
    [
        ("http://hooks.acme.example/in", "its URL must be an https:// URL"),
        ("hooks.acme.example/in", "its URL must be an https:// URL"),
        ("ftp://hooks.acme.example/in", "its URL must be an https:// URL"),
        ("https:///in", "its URL must be an https:// URL"),
        ("", "its URL must be an https:// URL"),
        ("https://user:pw@hooks.acme.example/in", "its URL must not carry a user or a password"),
        ("https://key@hooks.acme.example/in", "its URL must not carry a user or a password"),
        ("https://hooks.acme.example:port/in", "its URL is not a URL"),
        ("https://[bad/in", "its URL is not a URL"),
    ],
)
@pytest.mark.anyio
async def test_a_url_that_cant_be_used_is_refused_unquoted_and_nothing_is_sent(
    url: str, refusal: str, caplog: pytest.LogCaptureFixture
) -> None:
    assert contracts.webhook_refusal(url) == refusal
    receiver = Receiver()
    hooks, _ = sender(receiver)
    before = count("dropped")
    with caplog.at_level(logging.WARNING):
        hooks.emit(hooked(url=url), "report.finished", {})
        await hooks.drain()
    assert receiver.seen == [] and count("dropped") == before + 1
    assert f"acme: webhook event report.finished dropped: {refusal}" in caplog.text
    if url:
        assert url not in caplog.text
    await hooks.close()


def test_an_https_url_with_a_key_in_its_path_or_query_is_fine() -> None:
    for url in (URL, " https://hooks.acme.example ", "HTTPS://hooks.acme.example:8443/a?b=c"):
        assert contracts.webhook_refusal(url) is None


@pytest.mark.anyio
async def test_emitting_never_raises_and_never_waits(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(webhooks, "QUEUED", 2)
    gate = asyncio.Event()

    async def held(r: httpx.Request) -> httpx.Response:
        await gate.wait()
        return httpx.Response(200)

    receiver = Receiver()
    hooks, _ = sender(receiver)
    hooks.http = httpx.AsyncClient(transport=httpx.MockTransport(held))
    before = count("dropped")
    c = hooked()
    hooks.emit(c, "rollback", {"n": 0})
    await asyncio.sleep(0)  # the worker takes the first and waits on the receiver
    for n in range(1, 6):
        hooks.emit(c, "rollback", {"n": n})  # two wait; three find the queue full
    assert count("dropped") == before + 3 and "QueueFull" in caplog.text

    class Odd:
        def __str__(self) -> str:
            raise RuntimeError("can't be written")

    hooks.emit(c, "rollback", {"odd": Odd()})  # an event that can't be written: dropped
    assert count("dropped") == before + 4
    gate.set()
    await hooks.drain()
    await hooks.close()


@pytest.mark.anyio
async def test_a_contract_whose_secret_is_gone_loses_its_event_not_the_next_ones() -> None:
    receiver = Receiver()
    hooks, _ = sender(receiver)
    broken = make_contract(webhook=HOOK, secrets={"hook-key": SIGNING})  # no URL to read
    before = count("failed")
    hooks.emit(broken, "report.finished", {"n": 1})
    hooks.emit(hooked(), "report.finished", {"n": 2})
    await hooks.drain()
    assert [e["n"] for e in receiver.events()] == [2] and count("failed") == before + 1
    await hooks.close()


@pytest.mark.anyio
async def test_one_contracts_events_go_only_to_its_own_url_with_its_own_secret() -> None:
    """Isolation: two Contracts, each its own URL and signing secret."""
    receiver = Receiver()
    hooks, _ = sender(receiver)
    a = hooked("acme")
    b = hooked("bank", "https://hooks.bank.example/in", "whk-bank-signing-key")
    hooks.emit(a, "report.finished", {"summary": "acme's report"})
    hooks.emit(b, "report.finished", {"summary": "bank's report"})
    await hooks.drain()
    by_host = {r.url.host: r for r in receiver.seen}
    assert set(by_host) == {"hooks.acme.example", "hooks.bank.example"}
    mine, theirs = by_host["hooks.acme.example"], by_host["hooks.bank.example"]
    assert (json.loads(mine.content)["contract"], json.loads(theirs.content)["contract"]) == (
        "acme",
        "bank",
    )
    assert b"bank's report" not in mine.content and b"acme's report" not in theirs.content
    at = 1_800_000_000
    assert mine.headers["squero-signature"] == webhooks.signature(
        SecretStr(SIGNING), at, mine.content
    )
    theirs_signed = webhooks.signature(SecretStr("whk-bank-signing-key"), at, theirs.content)
    assert theirs.headers["squero-signature"] == theirs_signed
    assert theirs.headers["squero-signature"] != webhooks.signature(
        SecretStr(SIGNING), at, theirs.content
    )
    await hooks.close()


@pytest.mark.anyio
async def test_closing_stops_the_workers_and_drops_what_waits() -> None:
    gate = asyncio.Event()

    async def held(r: httpx.Request) -> httpx.Response:
        await gate.wait()
        return httpx.Response(200)

    receiver = Receiver()
    hooks, _ = sender(receiver)
    hooks.http = httpx.AsyncClient(transport=httpx.MockTransport(held))
    hooks.emit(hooked(), "rollback", {})
    await asyncio.sleep(0)
    await hooks.close()
    assert all(w.done() for w in hooks.workers.values()) and hooks.workers


def test_a_url_that_cant_be_used_is_refused_when_the_contracts_load_never_quoted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    use_age_keys(monkeypatch, tmp_path, "demo-shop")
    root = tmp_path / "contracts"
    path = write(root, {**BASE, "id": "demo-shop", "webhook": HOOK})
    plain = "http://hooks.acme.example/in/9f8e7d6c5b4a"
    encrypt(path.parent / "secrets.sops.yaml", {"hook-url": plain, "hook-key": SIGNING})
    found = problems(root)
    assert any(p.endswith("webhook: its URL must be an https:// URL") for p in found), found
    assert "9f8e7d6c5b4a" not in "\n".join(found)
    encrypt(path.parent / "secrets.sops.yaml", {"hook-url": URL, "hook-key": SIGNING})
    loaded = contracts.load(root)["demo-shop"]
    assert loaded.webhook is not None and loaded.secret("hook-url") == SecretStr(URL)
    encrypt(path.parent / "secrets.sops.yaml", {"hook-key": SIGNING})  # its URL missing
    missing = problems(root)
    assert any("secret 'hook-url' is not in" in p for p in missing)
    assert not any("webhook:" in p for p in missing)  # said once, as a missing secret


def test_a_contract_names_its_webhooks_two_secrets() -> None:
    c = hooked()
    assert c.webhook is not None and c.secret(c.webhook.url_secret) == SecretStr(URL)
    assert {"hook-url", "hook-key"} <= c.secret_refs()  # each checked at load
    assert make_contract().webhook is None
    for missing in ("url_secret", "signing_secret"):
        block = {k: v for k, v in HOOK.items() if k != missing}
        with pytest.raises(ValidationError, match=missing):
            make_contract(webhook=block, secrets=SECRETS)
    with pytest.raises(ValidationError, match="url"):  # a plain URL is refused, not ignored
        make_contract(webhook={**HOOK, "url": URL}, secrets=SECRETS)
    with pytest.raises(ValidationError, match="events"):
        make_contract(webhook={**HOOK, "events": ["rollback"]}, secrets=SECRETS)
