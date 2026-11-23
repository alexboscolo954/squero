"""The console's doors, shared by the console app (webapp.py) and the Hub MCP server.

Who: Tailscale's identity header. `tailscale serve` sets Tailscale-User-Login on
every request it proxies and strips any copy a client sends, so the header is only
as trustworthy as the port: the Hub listens on 127.0.0.1 behind `serve` (slice
0.5), but local processes and the other containers on the compose network could
still send it themselves. So the header alone opens only the token page: every
other page and call also needs the approver token, pasted once per browser and kept
as an HMAC of it in a Secure, HttpOnly, SameSite=Strict cookie. Every change must
come from the console's own pages (Sec-Fetch-Site or Origin): the identity header is
ambient, so a page elsewhere could otherwise make the browser send it.

The Jinja pages that were here are gone since slice 11.7b: their old addresses
redirect into the app.
"""

import hashlib
import hmac
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from squero import evals
from squero.contracts import Contract
from squero.models import Investigation
from squero.store import Store

HEADERS = {
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'",
    "X-Frame-Options": "DENY",
}
COOKIE = "squero_actions"  # renamed in 0.39.0: each browser pastes the approver token once more
COOKIE_DAYS = 30
# decide(investigation id, the pause's deadline, approve, reason, login) -> decided
Decide = Callable[[str, float, bool, str, str], Awaitable[bool]]
# ask(contract id, question, follow-up on this Question or None) -> its id, or None
Ask = Callable[[str, str, str | None], Awaitable[str | None]]
QUESTION_LIMIT = 4000
# health(contract) -> one {name, ok, ms, detail} per integration check
Health = Callable[[Contract], Awaitable[list[dict[str, Any]]]]
# (investigation, the Operator's login) -> (the Jira issue's key or the ServiceNow
# incident's number, or None and why)
FileJira = Callable[[Investigation, str], Awaitable[tuple[str | None, str]]]
# (a finished Triage, right, the correction when wrong, login)
Feedback = Callable[[Investigation, bool, str | None, str], Awaitable[None]]
# (a proposed Teaching's row, approve, the text the Operator submitted, login) -> decided
DecideTeaching = Callable[[dict[str, Any], bool, str | None, str], Awaitable[bool]]
CORRECTION_LIMIT = 2000


def action_key(approver_token: str) -> str:
    """What the cookie holds: proof of the token, never the token itself (a stolen
    cookie can't call the API)."""
    return hmac.new(approver_token.encode(), b"squero-console", hashlib.sha256).hexdigest()


def set_cookie(response: Response, key: str) -> None:
    """The approver cookie: proof of the token for the whole Hub."""
    response.set_cookie(
        COOKIE,
        key,
        max_age=COOKIE_DAYS * 86400,
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )


async def scenario_file(t: Investigation, by_id: dict[str, Contract], st: Store) -> Response:
    """A finished Investigation as a Scenario draft to download: the Hub never writes
    to the contracts repo."""
    if t.status != "done":
        raise HTTPException(409, f"investigation is {t.status}: only a finished one saves")
    if t.input is None or t.contract_id not in by_id:
        raise HTTPException(409, "started before Scenarios were recorded, or its Contract is gone")
    body = evals.scenario_yaml(
        t,
        by_id[t.contract_id],
        await st.tool_calls(t.id),
        await st.decisions(t.id),
        await st.teachings(t.contract_id, "approved"),
    )
    name = f"{t.contract_id}-{t.id[:8]}.yaml"
    return Response(
        body,
        media_type="application/yaml",
        headers={**HEADERS, "Content-Disposition": f'attachment; filename="{name}"'},
    )


def same_origin(request: Request) -> bool:
    site = request.headers.get("Sec-Fetch-Site")
    if site is not None:
        return site == "same-origin"
    origin = request.headers.get("Origin")
    return origin is not None and urlsplit(origin).netloc == request.headers.get("Host")


def redirects() -> APIRouter:
    """The Jinja console's old addresses, for bookmarks (slice 11.7b): an
    Investigation's page and its files go to its app page, anything else to the app.
    GET only: a form posted to an old address changes nothing (405)."""
    r = APIRouter()

    @r.get("/console", include_in_schema=False)
    @r.get("/console/{rest:path}", include_in_schema=False)
    async def moved(rest: str = "") -> RedirectResponse:
        parts = rest.split("/")
        if len(parts) >= 2 and parts[0] == "investigations" and parts[1]:
            return RedirectResponse(f"/app/?view=investigation&id={quote(parts[1], safe='')}", 303)
        return RedirectResponse("/app/", 303)

    return r
