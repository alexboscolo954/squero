"""PagerDuty (slice 19.2): an alert source, and a place the Client report goes.

A V3 webhook's incident becomes an Alert group, as Grafana's notification does. The
delivery is checked by its signature, over the raw body, with the subscription's secret.
The Hub calls two endpoints with the Contract's REST API key and no other: an incident's
alerts (read), and a note on it (the Client report, only when the Operator sends it). It
never acknowledges or resolves: that is a write to someone else's paging system.

What PagerDuty sends and answers is data: only an id of its own shape reaches a URL, and
a refusal is shown by its status and code, never its message (it can quote the `From`)."""

import asyncio
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import SecretStr

from squero.models import Alert, AlertGroup

API = "https://api.pagerduty.com"  # ponytail: US accounts; the EU service region has its own host
ACCEPT = "application/vnd.pagerduty+json;version=2"
SIGNATURE = "x-pagerduty-signature"
STARTS = {"incident.triggered", "incident.reopened"}
ENDS = {"incident.resolved"}
ID = re.compile(r"[A-Za-z0-9]{1,40}")
MAX_ALERTS, MAX_SUMMARY, MAX_DETAILS, MAX_NOTE = 10, 500, 2000, 8000
# Inside PagerDuty's 5 for a delivery, and in all (httpx's own limit is per phase): a
# slow API costs the incident's details only, never the delivery
READ_SECONDS = 3.0
PREFIX = "pagerduty:"  # its own group keys: an incident id says nothing in another source


class Refused(Exception):
    """PagerDuty answered no: nothing was written, a retry may work."""


class Unknown(Exception):
    """No answer (timeout, connection lost): PagerDuty may have written the note."""


def verified(secret: SecretStr, body: bytes, header: str) -> bool:
    """Whether one of the header's `v1=` signatures is the HMAC-SHA256 of the raw body
    with this secret (PagerDuty sends two while a secret is rotated)."""
    key = secret.get_secret_value().encode()
    if not key:
        return False
    want = b"v1=" + hmac.new(key, body, hashlib.sha256).hexdigest().encode()
    return any(hmac.compare_digest(want, s.strip().encode()) for s in header.split(","))


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _named(value: Any) -> str:
    """A reference's `summary` (a service, a priority), or nothing."""
    return _text(value.get("summary")) if isinstance(value, dict) else ""


@dataclass(frozen=True)
class Incident:
    """What a delivery says of an incident, when it starts or ends one."""

    id: str
    firing: bool
    title: str = ""
    number: int | None = None
    url: str = ""
    service: str = ""
    urgency: str = ""
    priority: str = ""
    key: str = ""  # its incident_key: the dedup key of the event that opened it

    @property
    def group_key(self) -> str:
        return PREFIX + self.id


def incident(body: bytes) -> Incident | None:
    """The incident a signed delivery starts or ends; None for any other event and for a
    shape this doesn't know, which the Hub ignores: PagerDuty drops a refused delivery
    for good and disables the subscription after three."""
    try:
        event = json.loads(body)["event"]
        kind, data = event["event_type"], event["data"]
        found = data["id"]
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(kind, str) or kind not in STARTS | ENDS:
        return None
    if not isinstance(found, str) or not ID.fullmatch(found):
        return None  # it goes into a URL: only an id of PagerDuty's own shape
    number, url = data.get("number"), _text(data.get("html_url"))
    return Incident(
        id=found,
        firing=kind in STARTS,
        title=_text(data.get("title")),
        number=number if isinstance(number, int) and not isinstance(number, bool) else None,
        url=url if url.startswith("https://") else "",
        service=_named(data.get("service")),
        urgency=_text(data.get("urgency")),
        priority=_named(data.get("priority")),
        key=_text(data.get("incident_key")),
    )


def keys(found: Incident, alerts: list[dict[str, Any]]) -> set[str]:
    """Every dedup key the incident carries: its own and its alerts'."""
    return {found.key, *(_text(a.get("alert_key")) for a in alerts)} - {""}


def digest(group_key: str) -> str:
    """The dedup key Alertmanager and Grafana give PagerDuty for an Alert group: the
    SHA-256 of the group key they send the Hub (`notify.Key.Hash`). Grafana's is stored
    here under its own prefix, which it never sent."""
    return hashlib.sha256(group_key.removeprefix("grafana:").encode()).hexdigest()


def group(found: Incident, alerts: list[dict[str, Any]]) -> AlertGroup:
    """The incident as an Alert group, as Alertmanager would send it: its title as the
    alertname, its service, urgency and priority as labels, and each of its first alerts
    with its summary and details, cut to size, where the agent reads annotations."""
    status = "firing" if found.firing else "resolved"
    labels = {"alertname": found.title or "PagerDuty incident", "source": "pagerduty"}
    labels |= {
        k: v
        for k, v in (
            ("service", found.service),
            ("urgency", found.urgency),
            ("priority", found.priority),
        )
        if v
    }
    link = {"incident": found.url} if found.url else {}
    listed = []
    for a in alerts[:MAX_ALERTS]:
        body = a.get("body")
        details = body.get("details") if isinstance(body, dict) else None
        notes = {
            "summary": _text(a.get("summary"))[:MAX_SUMMARY],
            "details": json.dumps(details, default=str)[:MAX_DETAILS] if details else "",
        }
        severity = _text(a.get("severity"))
        listed.append(
            Alert(
                status=status,
                labels=labels | ({"severity": severity} if severity else {}),
                annotations={k: v for k, v in notes.items() if v} | link,
                fingerprint=_text(a.get("id")) or found.id,
            )
        )
    if not listed:  # no key, or no answer in time: the incident alone
        listed = [Alert(status=status, labels=labels, annotations=link, fingerprint=found.id)]
    return AlertGroup(
        status=status,
        receiver="pagerduty",
        groupKey=found.group_key,
        groupLabels={"incident": found.id},
        commonLabels=labels,
        alerts=listed,
    )


def _headers(key: SecretStr) -> dict[str, str]:
    return {"Authorization": f"Token token={key.get_secret_value()}", "Accept": ACCEPT}


def _why(resp: httpx.Response) -> str:
    """A refusal by its status and PagerDuty's own code, never its message."""
    try:
        code = resp.json()["error"]["code"]
    except (ValueError, KeyError, TypeError):
        code = None
    return f"HTTP {resp.status_code}" + (f", code {code}" if isinstance(code, int) else "")


async def _read(
    http: httpx.AsyncClient, key: SecretStr, path: str, params: dict[str, Any] | None = None
) -> httpx.Response:
    """One read with this Contract's key on the request alone (the client is shared),
    answered within READ_SECONDS in all, or Unknown."""
    try:
        async with asyncio.timeout(READ_SECONDS):
            return await http.get(
                f"{API}{path}", params=params, headers=_headers(key), timeout=READ_SECONDS
            )
    except (httpx.HTTPError, TimeoutError) as e:
        raise Unknown(type(e).__name__) from None


async def alerts(http: httpx.AsyncClient, key: SecretStr, incident_id: str) -> list[dict[str, Any]]:
    """The incident's first alerts. Refused or Unknown when PagerDuty doesn't give them
    in time."""
    resp = await _read(http, key, f"/incidents/{incident_id}/alerts", {"limit": MAX_ALERTS})
    if resp.status_code != 200:
        raise Refused(_why(resp))
    try:
        listed = resp.json()["alerts"]
    except (ValueError, KeyError, TypeError):
        raise Refused("an answer that lists no alerts") from None
    return [a for a in listed if isinstance(a, dict)] if isinstance(listed, list) else []


async def note(
    http: httpx.AsyncClient, key: SecretStr, sender: SecretStr, incident_id: str, text: str
) -> None:
    """The Client report as a note on the incident, written as `sender` (a user of the
    account). Refused: nothing was written. Unknown: it may have been."""
    try:
        resp = await http.post(
            f"{API}/incidents/{incident_id}/notes",
            json={"note": {"content": text[:MAX_NOTE]}},
            headers=_headers(key) | {"From": sender.get_secret_value()},
            timeout=20,
        )
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:  # nothing was sent
        raise Refused(f"could not connect ({type(e).__name__})") from None
    except httpx.HTTPError as e:
        raise Unknown(type(e).__name__) from None
    if resp.status_code >= 500:  # a gateway timeout or a late failure: maybe written
        raise Unknown(f"HTTP {resp.status_code}")
    if resp.status_code not in (200, 201):
        raise Refused(_why(resp))


async def reachable(http: httpx.AsyncClient, key: SecretStr) -> str:
    """The health check: one cheap authenticated read. "" when the key works."""
    try:
        resp = await _read(http, key, "/abilities")
    except Unknown as e:
        return str(e)
    return "" if resp.status_code == 200 else _why(resp)
