"""Each Investigation is one thread in its Contract's Slack channel: a parent message
whose status line is kept current, and a reply for each pause, Report or failure.

Web API over httpx with the Hub's bot token. Approve and reject buttons sit on the
parent while it waits; clicks arrive over Socket Mode (the app token), so Slack
needs no public endpoint. Slack is best effort: a failed post is logged and never
fails an Investigation.
"""

import asyncio
import json
import logging
import re
from collections.abc import Callable, Sequence
from typing import Any

import httpx
import websockets
from pydantic import SecretStr

from squero.models import Investigation
from squero.safety import redact_capped

log = logging.getLogger("squero.slack")
API = "https://slack.com/api/"
TEXT_LIMIT = 12_000  # Slack takes 40,000; long replies are unreadable anyway
RETRY_AFTER_CAP = 10.0  # ponytail: one retry after a 429, waiting at most this long
STATUS = {
    "running": ":mag: investigating",
    "awaiting_approval": ":raised_hand: waiting for your approval",
    "done": ":white_check_mark: done",
    "failed": ":x: failed",
}


# What Slack turns into a link by itself: a scheme or www. URL, or a host with a path.
URL = re.compile(
    r"(?:(?:https?|ftp)://|www\.)[^\s`<>|]+|(?<![\w@/.:-])(?:[a-z0-9-]+\.)+[a-z]{2,}(?::\d+)?/[^\s`<>|]*",
    re.IGNORECASE,
)
TRAILING = ".,;:!?)]}'\"*_~"  # punctuation, and Slack's own bold/italic/strike


def unlinked(text: str, links: frozenset[str] = frozenset()) -> str:
    """Every URL outside a code span goes into one, where Slack never links it, unless
    it is one of `links` (an Evidence link a link tool returned). A prompt injection
    could have the model write one carrying client data to its own host. ponytail: a
    bare domain with no path (x.evil.com) can still be linked by Slack; it carries
    data only in its labels and needs a click."""

    def wrap(m: re.Match[str]) -> str:
        url = m[0].rstrip(TRAILING)
        return m[0] if url in links else f"`{url}`{m[0][len(url) :]}"

    parts = text.split("`")  # odd parts are inside code spans: never linked
    return "`".join(URL.sub(wrap, p) if i % 2 == 0 else p for i, p in enumerate(parts))


def clean(text: str, limit: int = TEXT_LIMIT, links: frozenset[str] = frozenset()) -> str:
    """No self-made links, escape Slack markup, then redact and cap. Alert titles and
    Reports come from clients and from tool output: `<!channel>` or `<@U…>` must
    arrive as text, and a URL only as text (links: the ones that may stay links)."""
    text = unlinked(text, links)
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return redact_capped(escaped, limit)


def evidence_links(t: Investigation) -> frozenset[str]:
    """The Evidence links of a result: each one a link tool returned (agent.py)."""
    parts: list[Any] = [t.report, t.answer, t.check_result]
    return frozenset(e.link for p in parts if p for e in p.evidence if e.link)


def related_text(title: str, matched: dict[str, str], severity: str | None, louder: bool) -> str:
    """The thread reply for a Related alert group: what joined, on which labels."""
    shared = ", ".join(f"{k}={v}" for k, v in matched.items())
    text = (
        f":link: Related alert group joined this Triage: *{clean(title, 200)}* "
        f"(shares {clean(shared, 300)}). It is not investigated on its own."
    )
    if louder:
        text += (
            f"\n:warning: Its severity ({clean(severity or '?', 40)}) is higher than this Triage's."
        )
    return text


def related_blocks(text: str, investigation_id: str, related_id: int) -> list[dict[str, Any]]:
    """The Related alert group's post with its Triage separately button."""
    button = {
        "type": "button",
        "action_id": "triage_separately",
        "text": {"type": "plain_text", "text": "Triage separately"},
        "value": json.dumps({"id": investigation_id, "related": related_id}),
        "confirm": {
            "title": {"type": "plain_text", "text": "Triage it separately?"},
            "text": {
                "type": "plain_text",
                "text": "It gets a Triage and thread of its own, which counts against the "
                "Contract's budget.",
            },
            "confirm": {"type": "plain_text", "text": "Triage"},
            "deny": {"type": "plain_text", "text": "Cancel"},
        },
    }
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {"type": "actions", "elements": [button]},
    ]


def feedback_blocks(investigation_id: str) -> list[dict[str, Any]]:
    """Its own thread message after a Triage Report: the Report's reply can run past a
    section block's 3,000 characters, and the parent keeps only its own buttons."""
    value = json.dumps({"id": investigation_id})
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": "Was this Report right?"}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": "feedback_right",
                    "text": {"type": "plain_text", "text": ":+1: Right"},
                    "value": value,
                },
                {
                    "type": "button",
                    "action_id": "feedback_wrong",
                    "text": {"type": "plain_text", "text": ":-1: Wrong…"},
                    "value": value,
                },
            ],
        },
    ]


def text_modal(
    callback: str, title: str, label: str, metadata: str, submit: str, limit: int, initial: str = ""
) -> dict[str, Any]:
    """A modal with one required text input, named after the callback."""
    element: dict[str, Any] = {
        "type": "plain_text_input",
        "action_id": callback,
        "multiline": True,
        "max_length": limit,
    }
    if initial:
        element["initial_value"] = initial
    return {
        "type": "modal",
        "callback_id": callback,
        "private_metadata": metadata,
        "title": {"type": "plain_text", "text": title},
        "submit": {"type": "plain_text", "text": submit},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": callback,
                "label": {"type": "plain_text", "text": label},
                "element": element,
            }
        ],
    }


def teaching_text(row: dict[str, Any]) -> str:
    """A Teaching as its review post shows it."""
    alerts = ", ".join(row["alerts"]) or "every alert"
    text = f":bulb: Teaching #{row['id']} (for {clean(alerts, 300)}): *{clean(row['text'], 600)}*"
    if row.get("contradicts"):
        text += (
            f"\n:warning: It contradicts Teaching #{row['contradicts']}: "
            "approving it retires that one."
        )
    return text


def teaching_blocks(row: dict[str, Any], investigation_id: str) -> list[dict[str, Any]]:
    value = json.dumps({"id": investigation_id, "teaching": row["id"]})
    buttons = [
        ("teaching_approve", "Approve", "primary"),
        ("teaching_edit", "Edit…", None),
        ("teaching_reject", "Reject", "danger"),
    ]
    elements = []
    for action_id, label, style in buttons:
        b: dict[str, Any] = {
            "type": "button",
            "action_id": action_id,
            "text": {"type": "plain_text", "text": label},
            "value": value,
        }
        if style:
            b["style"] = style
        elements.append(b)
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": teaching_text(row)}},
        {"type": "actions", "elements": elements},
    ]


def mrkdwn(markdown: str) -> str:
    """The Report's markdown as Slack mrkdwn: headings and **bold** become *bold*,
    and list dashes become bullets."""
    text = re.sub(r"(?m)^#{1,6}\s+(.+)$", r"*\1*", markdown)
    return re.sub(r"(?m)^- ", "• ", text.replace("**", "*"))


def status(t: Investigation) -> str:
    """A Check that finished with a failing verdict is done, but not healthy."""
    if t.status == "done" and t.check_result and not t.check_result.passed:
        return ":x: Check failed"
    return STATUS[t.status]


def parent_text(t: Investigation) -> str:
    sent = " · :outbox_tray: sent to the client" if t.client_sent_at else ""
    filed = f" · :ticket: {clean(t.jira_issue, 40)}" if t.jira_issue else ""
    return f"*[{clean(t.contract_id)}] {clean(t.title, 300)}*\n{status(t)} · `{t.id}`{sent}{filed}"


def _client_report(t: Investigation) -> str:
    """The Client report, as markdown: no Evidence, Executed actions, Runbook, ids or
    controls. The model's own prose can still name pods or quote a log line."""
    assert t.report is not None
    r = t.report
    return "\n".join(
        [
            f"## {t.title}: {r.summary}",
            "",
            f"**Root cause** ({r.confidence} confidence): {r.probable_root_cause}",
            f"**Severity:** {r.severity}  ·  **Affected:** {', '.join(r.affected) or '-'}",
            "",
            "**Recommendations**",
            *[f"{i}. {a}" for i, a in enumerate(r.recommended_actions, 1)],
        ]
    )


def client_text(t: Investigation) -> str:
    """The Client report for Slack: its markup, and Slack's escaping."""
    return clean(mrkdwn(_client_report(t)))  # no Evidence here: no link stays


def client_note(t: Investigation) -> str:
    """The same Client report as plain text, for a place that isn't Slack (a PagerDuty
    note, slice 19.2): the same words, redacted and capped, with none of Slack's markup
    or escaping, which would show there as `&gt;` and stray asterisks."""
    plain = re.sub(r"(?m)^#{1,6}\s+", "", _client_report(t)).replace("**", "")
    return redact_capped(plain, TEXT_LIMIT)


CONFIRM_TEXT = 300  # Slack's limit for a confirm dialog's text: over it, the message is refused
GOES = "The Report, without Evidence, goes {}."


def destinations(places: list[str], room: int) -> str:
    """The places joined with "and", as many as fit in `room` characters, then how many
    more: a dialog Slack refuses would take the whole message, its status with it."""
    shown = list(places)
    while True:
        more = len(places) - len(shown)
        said = " and ".join([*shown, *([f"{more} more"] if more else [])])
        if len(said) <= room or len(shown) <= 1:
            return said[:room]
        shown.pop()


def parent_blocks(
    t: Investigation,
    buttons: bool,
    client: str | None = None,
    jira: str | None = None,
    noted: bool = False,
    channels: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """Always sent in full, so no stale buttons survive an update. Each button
    names its pause (the deadline): a late click can't decide a later pause.
    client: the Contract's client channel, for the Send to client button.
    jira: the Contract's Jira project, for the Create Jira issue button.
    noted: the Client report also goes on its PagerDuty incident as a note (19.2).
    channels: the Contract's Teams and Google Chat channels it also goes to, each by
    its label (19.4): the dialog names every destination, since the click is the consent."""
    places = [
        *([f"to the client channel {client}"] if client else []),
        *(["on its PagerDuty incident as a note"] if noted else []),
        *(f"to {label}" for label in channels),
    ]
    where = destinations(places, CONFIRM_TEXT - len(GOES.format("")))
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": parent_text(t)}}
    ]
    done: list[dict[str, Any]] = []  # buttons on a finished Report
    if buttons and where and t.report and not t.client_sent_at:  # a Report: done
        send = {
            "type": "button",
            "action_id": "send_to_client",
            "text": {"type": "plain_text", "text": "Send to client"},
            "value": json.dumps({"id": t.id}),
            "confirm": {
                "title": {"type": "plain_text", "text": "Send to the client?"},
                "text": {
                    "type": "plain_text",
                    "text": GOES.format(where),
                },
                "confirm": {"type": "plain_text", "text": "Send"},
                "deny": {"type": "plain_text", "text": "Cancel"},
            },
        }
        done.append(send)
    if buttons and jira and t.report and t.status == "done" and not t.jira_at:
        file = {
            "type": "button",
            "action_id": "create_jira",
            "text": {"type": "plain_text", "text": "Create Jira issue"},
            "value": json.dumps({"id": t.id}),
            "confirm": {
                "title": {"type": "plain_text", "text": "Create a Jira issue?"},
                "text": {
                    "type": "plain_text",
                    "text": f"The full Report, with Evidence, goes to Jira project {jira}.",
                },
                "confirm": {"type": "plain_text", "text": "Create"},
                "deny": {"type": "plain_text", "text": "Cancel"},
            },
        }
        done.append(file)
    if done:
        blocks.append({"type": "actions", "elements": done})
    if buttons and t.status == "awaiting_approval":
        value = json.dumps({"id": t.id, "deadline": t.deadline})
        names = ", ".join(sorted({p["name"] for p in t.pending}))
        approve = {
            "type": "button",
            "action_id": "approve",
            "text": {"type": "plain_text", "text": "Approve"},
            "style": "primary",
            "value": value,
            "confirm": {
                "title": {"type": "plain_text", "text": "Run these actions?"},
                "text": {"type": "plain_text", "text": names[:300]},
                "confirm": {"type": "plain_text", "text": "Approve"},
                "deny": {"type": "plain_text", "text": "Cancel"},
            },
        }
        reject = {
            "type": "button",
            "action_id": "reject",
            "text": {"type": "plain_text", "text": "Reject"},
            "style": "danger",
            "value": value,
        }
        blocks.append({"type": "actions", "elements": [approve, reject]})
    return blocks


def reject_modal(metadata: str) -> dict[str, Any]:
    return {
        "type": "modal",
        "callback_id": "reject",
        "private_metadata": metadata,
        "title": {"type": "plain_text", "text": "Reject"},
        "submit": {"type": "plain_text", "text": "Reject"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "reason",
                "optional": True,
                "label": {"type": "plain_text", "text": "Why? The agent reads this."},
                "element": {"type": "plain_text_input", "action_id": "reason", "multiline": True},
            }
        ],
    }


def reply_text(t: Investigation, buttons: bool = False) -> str | None:
    if t.status == "awaiting_approval":
        actions = "\n".join(f"• {clean(p['description'], 2000)}" for p in t.pending)
        decide = f"Decide with `POST /investigations/{t.id}/decision`."
        if buttons:
            decide = f"Approve or reject on the message above, or {decide[0].lower()}{decide[1:]}"
        return f"Waiting for your approval:\n{actions}\n{decide}"
    links = evidence_links(t)
    if t.status == "done" and t.report:
        return clean(mrkdwn(t.report.to_markdown(t.executed_actions)), links=links)
    if t.status == "done" and t.answer:
        return clean(mrkdwn(t.answer.to_markdown()), links=links)
    if t.status == "done" and t.check_result:
        return clean(mrkdwn(t.check_result.to_markdown()), links=links)
    if t.status == "failed":
        return f"Investigation failed: {clean(t.error or 'unknown error', 2000)}"
    return None


class Slack:
    def __init__(self, token: SecretStr | None, http: httpx.AsyncClient) -> None:
        self.token, self.http = token, http

    async def _call(
        self, method: str, payload: dict[str, Any], token: SecretStr | None = None
    ) -> dict[str, Any] | None:
        token = token or self.token
        assert token is not None
        headers = {"Authorization": f"Bearer {token.get_secret_value()}"}
        where = payload.get("channel", "-")
        for attempt in (1, 2):
            try:
                r = await self.http.post(API + method, json=payload, headers=headers, timeout=10)
            except httpx.HTTPError as e:
                log.warning("slack %s to %s: %s", method, where, type(e).__name__)
                return None
            if r.status_code == 429 and attempt == 1:
                wait = min(float(r.headers.get("Retry-After", "1")), RETRY_AFTER_CAP)
                await asyncio.sleep(wait)
                continue
            # Slack answers HTTP 200 with {"ok": false, "error": ...}: check `ok`.
            body: dict[str, Any] = (
                r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            )
            if not body.get("ok"):
                error = body.get("error") or f"HTTP {r.status_code}"
                log.warning("slack %s to %s failed: %s", method, where, error)
                return None
            return body
        return None

    async def post(
        self,
        channel: str,
        text: str,
        thread_ts: str | None = None,
        blocks: list[dict[str, Any]] | None = None,
    ) -> str | None:
        """The message's ts, or None when it wasn't posted. With blocks, text is
        the notification's fallback."""
        # No unfurls: Slack's servers would fetch a link or image the model wrote (a
        # prompt injection's way to send client data to a host it names).
        payload: dict[str, Any] = {
            "channel": channel,
            "text": text,
            "unfurl_links": False,
            "unfurl_media": False,
        }
        if thread_ts:
            payload["thread_ts"] = thread_ts
        if blocks is not None:
            payload["blocks"] = blocks
        body = await self._call("chat.postMessage", payload)
        return str(body["ts"]) if body else None

    async def update(
        self, channel: str, ts: str, text: str, blocks: list[dict[str, Any]] | None = None
    ) -> None:
        payload: dict[str, Any] = {"channel": channel, "ts": ts, "text": text}
        if blocks is not None:
            payload["blocks"] = blocks
        await self._call("chat.update", payload)

    async def ephemeral(self, channel: str, user: str, text: str) -> None:
        await self._call("chat.postEphemeral", {"channel": channel, "user": user, "text": text})

    async def open_view(self, trigger_id: str, view: dict[str, Any]) -> None:
        await self._call("views.open", {"trigger_id": trigger_id, "view": view})

    async def listen(
        self, app_token: SecretStr, handle: Callable[[str, dict[str, Any]], None]
    ) -> None:
        """Socket Mode until cancelled: ask for a websocket URL, ack every envelope
        before handling it (Slack wants the ack within 3s), and reconnect when Slack
        says disconnect (it does every few hours) or the socket drops. handle must
        not block: it gets each envelope's type ("interactive" for clicks,
        "events_api" for mentions) and payload, and spawns the work."""
        backoff = 1.0
        while True:
            try:
                body = await self._call("apps.connections.open", {}, app_token)
                if body:
                    url = body["url"]
                    # httpx's CA bundle (certifi): the system's may be missing (macOS).
                    tls = httpx.create_ssl_context() if url.startswith("wss:") else None
                    async with websockets.connect(url, ssl=tls) as ws:
                        backoff = 1.0
                        async for raw in ws:
                            msg = json.loads(raw)
                            if "envelope_id" in msg:
                                await ws.send(json.dumps({"envelope_id": msg["envelope_id"]}))
                            if msg.get("type") == "disconnect":
                                break
                            if msg.get("type") in ("interactive", "events_api"):
                                handle(msg["type"], msg["payload"])
            except Exception as e:  # the Hub runs on without buttons; keep trying
                log.warning("slack socket mode: %s", type(e).__name__)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
