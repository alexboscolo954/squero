"""Outbound webhooks (slice 19.3): what a Contract's Operator channel gets, as signed JSON
events to one URL the Contract names.

A copy, never the record: events wait in memory, one queue per Contract so they arrive in
order, and a Hub restart loses the ones not sent yet. Sending never blocks or fails the
Hub's own work: an event that can't be queued or delivered is logged, counted and dropped.

Each event is an allowlist of what Slack shows, every string redacted and cut to size.
Each attempt is signed afresh (`Squero-Signature: t=<unix>,v1=<hex>`, HMAC-SHA256 of
`<t>.<body>` with the Contract's secret) and keeps the event's `id`, so a receiver can
refuse an old one and drop a repeat. The URL is a secret: only its host is ever logged."""

import asyncio
import hashlib
import hmac
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from squero.contracts import Contract, webhook_refusal
from squero.models import Investigation
from squero.observability import WEBHOOK_EVENTS as EVENTS
from squero.safety import redact_capped

log = logging.getLogger(__name__)

VERSION = 1
SECONDS = 10.0  # one attempt, in all (httpx's own limit is per phase)
BACKOFF = (1.0, 5.0, 25.0)  # before each of the three retries
QUEUED = 1000  # events waiting per Contract: more are dropped, never the Hub's own work
MAX_TEXT, MAX_ITEMS = 4000, 50  # each string, each list: what bounds an event's size


def signature(secret: SecretStr, at: int, body: bytes) -> str:
    """`t=<unix>,v1=<hex>`: the HMAC-SHA256 of `<t>.<body>`, the time signed with the
    bytes so a receiver can refuse an old event."""
    signed = f"{at}.".encode() + body
    return (
        f"t={at},v1="
        + hmac.new(secret.get_secret_value().encode(), signed, hashlib.sha256).hexdigest()
    )


def scrubbed(value: Any) -> Any:
    """A JSON-like value as it may leave the Hub: every string redacted and cut, every
    list cut, dict keys kept."""
    if isinstance(value, str):
        return redact_capped(value, MAX_TEXT)
    if isinstance(value, dict):
        return {str(k): scrubbed(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrubbed(v) for v in value[:MAX_ITEMS]]
    return value


def investigation(t: Investigation) -> dict[str, Any]:
    """What the Operator's thread says of an Investigation, and nothing else of it:
    never its input (the raw Alert group), its Slack thread or who was sent what."""
    out: dict[str, Any] = {
        "id": t.id,
        "kind": t.kind,
        "title": t.title,
        "status": t.status,
        "started": t.started,
    }
    if t.kind == "check":
        out["check"] = str((t.input or {}).get("check", ""))  # its name only
    return out


def report(t: Investigation) -> dict[str, Any]:
    """The Report as the thread shows it, with its Evidence; a Check's verdict with it."""
    out: dict[str, Any] = {}
    if t.check_result:
        c = t.check_result
        out["verdict"] = {
            "passed": c.passed,
            "summary": c.summary,
            "evidence": [e.model_dump(mode="json") for e in c.evidence],
        }
    r = t.report or (t.check_result.report if t.check_result else None)
    if r:
        out["report"] = {
            "summary": r.summary,
            "category": r.category,
            "probable_root_cause": r.probable_root_cause,
            "confidence": r.confidence,
            "severity": r.severity,
            "affected": r.affected,
            "evidence": [e.model_dump(mode="json") for e in r.evidence],
            "recommended_actions": r.recommended_actions,
            "runbook": r.runbook,
        }
    return out


def proposed(pending: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The Proposed actions as the thread asks about them: each tool and its arguments."""
    return [{"name": p.get("name"), "args": p.get("args")} for p in pending]


class Sender:
    """Queues a Contract's events and sends them in order, off the caller's path."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.http, self.sleep, self.now = http, sleep, now
        self.queues: dict[str, asyncio.Queue[tuple[Contract, str, str, bytes]]] = {}
        self.workers: dict[str, asyncio.Task[None]] = {}
        # each Contract's last delivery, for its Health row: when, whether it arrived, why not
        self.last: dict[str, tuple[float, bool, str]] = {}

    def emit(self, contract: Contract | None, event: str, data: dict[str, Any]) -> None:
        """Queue one event for the Contract's webhook, if it has one. Never raises and
        never waits: the Hub's own work (a Run, a decision, an Actuator's report) goes on
        whatever becomes of the event."""
        if contract is None or contract.webhook is None:
            return
        try:
            event_id = str(uuid.uuid4())
            at = datetime.fromtimestamp(self.now(), UTC).isoformat(timespec="seconds")
            head = {"version": VERSION, "id": event_id, "event": event, "contract": contract.id}
            # the envelope is the Hub's: no field of an event can stand in for one of its
            whole = {**head, "at": at} | {k: v for k, v in scrubbed(data).items() if k not in head}
            whole["at"] = at
            body = json.dumps(whole, default=str).encode()
            queue = self.queues.setdefault(contract.id, asyncio.Queue(QUEUED))
            queue.put_nowait((contract, event, event_id, body))
            worker = self.workers.get(contract.id)
            if worker is None or worker.done():
                self.workers[contract.id] = asyncio.create_task(self._work(contract.id))
        except Exception as e:  # a full queue, an event that can't be written: dropped
            self._ended(contract.id, event, "dropped", type(e).__name__)

    def _ended(self, cid: str, event: str, outcome: str, why: str, host: str = "") -> None:
        EVENTS.labels(outcome).inc()
        self.last[cid] = (self.now(), outcome == "delivered", why)
        if outcome != "delivered":
            to = f" to {host}" if host else ""
            log.warning("%s: webhook event %s %s%s: %s", cid, event, outcome, to, why)

    async def _work(self, cid: str) -> None:
        queue = self.queues[cid]
        while True:
            contract, event, event_id, body = await queue.get()
            try:
                await self._deliver(contract, event, event_id, body)
            except Exception as e:  # never the worker's end: the next event still goes
                self._ended(cid, event, "failed", type(e).__name__)
            finally:
                queue.task_done()

    async def _deliver(self, contract: Contract, event: str, event_id: str, body: bytes) -> None:
        hook = contract.webhook
        assert hook is not None
        url = contract.secret(hook.url_secret).get_secret_value().strip()
        if refusal := webhook_refusal(url):  # checked at load; never sent anywhere else
            self._ended(contract.id, event, "dropped", refusal)
            return
        host, why = urlsplit(url).hostname or "", ""
        for attempt in range(len(BACKOFF) + 1):
            at = int(self.now())
            headers = {
                "Content-Type": "application/json",
                "User-Agent": f"squero-webhook/{VERSION}",
                "Squero-Event": event,
                "Squero-Id": event_id,
                "Squero-Signature": signature(contract.secret(hook.signing_secret), at, body),
            }
            again = True
            try:
                async with asyncio.timeout(SECONDS):
                    resp = await self.http.post(
                        url, content=body, headers=headers, timeout=SECONDS, follow_redirects=False
                    )
                if 200 <= resp.status_code < 300:
                    self._ended(contract.id, event, "delivered", "")
                    return
                # the status only: a reply's body is the receiver's, and may quote the event
                why = f"HTTP {resp.status_code}"
                again = resp.status_code >= 500 or resp.status_code == 429
            except (httpx.HTTPError, TimeoutError) as e:
                why = type(e).__name__  # never its text: it can quote the URL
            if not again or attempt == len(BACKOFF):
                break
            await self.sleep(BACKOFF[attempt])
        self._ended(contract.id, event, "failed", why, host)

    def health(self, cid: str) -> tuple[bool, str]:
        """The Contract's webhook, by its last delivery: no request is made to say so."""
        if cid not in self.last:
            return True, "no event sent since the Hub started"
        at, ok, why = self.last[cid]
        when = datetime.fromtimestamp(at, UTC).strftime("%Y-%m-%d %H:%M UTC")
        return (
            ok,
            f"last event delivered at {when}"
            if ok
            else f"last event not delivered at {when}: {why}",
        )

    async def drain(self) -> None:
        """Wait for every queued event to end (for tests)."""
        for queue in list(self.queues.values()):
            await queue.join()

    async def close(self) -> None:
        for worker in self.workers.values():
            worker.cancel()
        await asyncio.gather(*self.workers.values(), return_exceptions=True)
