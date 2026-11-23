"""ServiceNow MCP server: past incidents of one assignment group as Evidence, read-only
(slice 19.5a).

ServiceNow's Table API with the Contract's own user and password. The server writes
every query itself, inside the Contract's assignment group; the model passes only
words. ServiceNow drops a query clause it can't read and answers with the rest, so
the query alone proves nothing: every row that comes back is checked against the
group before the model sees it.
"""

import logging
import re
from typing import Any

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import SecretStr

from squero.safety import truncate
from squero.servers import Access

log = logging.getLogger(__name__)

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
ACCESS: dict[str, Access] = {"search_incidents": "read", "get_incident": "read"}  # ADR 0002
TABLE = "/api/now/table/incident"
FIELDS = [
    "number",
    "sys_id",
    "short_description",
    "state",
    "opened_at",
    "resolved_at",
    "close_code",
    "assignment_group",
]
NUMBER = r"[A-Z]{2,10}[0-9]{4,12}"  # INC0010001; an instance may rename the prefix
SYS_ID = r"[0-9a-f]{32}"
WORDS = 5  # each one is two conditions in the query
SUMMARY_LIMIT = 300
DESCRIPTION_LIMIT = 4000
NOTES_LIMIT = 1500
WHY_LIMIT = 200
GUIDE = """\
Past incidents in ServiceNow (servicenow_ tools): search the client's ServiceNow for
the failing service or the error (servicenow_search_incidents). An earlier incident
with the same cause, and how it was resolved (servicenow_get_incident), is strong
evidence and a likely fix; cite its number.
"""


def terms(text: str) -> list[str]:
    """Only word characters reach the query: no `^`, `=`, comma or `javascript:`, so
    a word can't end its condition or start one. The first few different words."""
    found = dict.fromkeys(w[:40] for w in re.findall(r"\w+", text))
    return list(found)[:WORDS]


def actual(field: Any) -> str:
    """A field's stored value. With display values the API answers {display_value,
    value}, a reference without them {link, value}, anything else a plain string."""
    if isinstance(field, dict):
        field = field.get("value")
    return field if isinstance(field, str) else ""


def shown(field: Any) -> str:
    """A field as ServiceNow shows it (a state's name, not its number)."""
    if isinstance(field, dict):
        field = field.get("display_value") or field.get("value")
    return field if isinstance(field, str) else ""


def build_server(
    url: str, user: str, password: SecretStr, group: str, http: httpx.Client | None = None
) -> FastMCP:
    base = url.rstrip("/")
    if http is None:
        auth = (user, password.get_secret_value())
        http = httpx.Client(
            base_url=base, auth=auth, timeout=15, headers={"Accept": "application/json"}
        )
    mcp = FastMCP("servicenow", instructions="Search the Contract's past ServiceNow incidents.")

    def rows(query: str, fields: list[str], limit: int) -> list[dict[str, Any]]:
        """The incidents a query finds, only those of the Contract's group."""
        params = {
            "sysparm_query": query,
            "sysparm_fields": ",".join(fields),
            "sysparm_limit": str(limit),
            "sysparm_display_value": "all",
            "sysparm_exclude_reference_link": "true",
        }
        try:
            resp = http.get(TABLE, params=params)
        except httpx.HTTPError as e:
            raise ToolError(f"ServiceNow unreachable: {type(e).__name__}") from None
        try:
            data = resp.json()
        except ValueError:
            data = None
        if resp.status_code != 200:
            error = data.get("error") if isinstance(data, dict) else None
            why = error.get("message") if isinstance(error, dict) else None
            why = why if isinstance(why, str) and why else resp.reason_phrase
            said = " ".join(why.split())[:WHY_LIMIT]  # one line
            raise ToolError(f"ServiceNow error {resp.status_code}: {said}")
        found = data.get("result") if isinstance(data, dict) else None
        if not isinstance(found, list):  # a login page, or an instance that is asleep
            raise ToolError("ServiceNow error: its answer was not the Table API's")
        own = [
            r for r in found if isinstance(r, dict) and actual(r.get("assignment_group")) == group
        ]
        if len(own) != len(found):
            log.warning(
                "servicenow: %d of %d rows were not the Contract's assignment group's: dropped",
                len(found) - len(own),
                len(found),
            )
        return own

    def summary(row: dict[str, Any]) -> dict[str, Any]:
        sys_id = actual(row.get("sys_id"))
        return {
            "number": actual(row.get("number"))[:40],
            "summary": truncate(shown(row.get("short_description")), SUMMARY_LIMIT),
            "state": shown(row.get("state"))[:40],
            "opened": actual(row.get("opened_at"))[:40] or None,  # UTC, as stored
            "resolved": actual(row.get("resolved_at"))[:40] or None,
            "resolution": shown(row.get("close_code"))[:100] or None,
            # A third party's answer: only an id goes into the link.
            "url": f"{base}/incident.do?sys_id={sys_id}" if re.fullmatch(SYS_ID, sys_id) else None,
        }

    @mcp.tool(annotations=READ)
    def search_incidents(text: str, days: int = 365, limit: int = 10) -> list[dict[str, Any]]:
        """Incidents of this Contract's ServiceNow assignment group whose short
        description or description has every word of `text` (a service, an error
        message; at most 5 words, fewer find more), opened in the last `days`, newest
        first."""
        found = terms(text)
        if not found:
            raise ToolError("give some words to search for")
        days, limit = max(1, min(days, 3650)), max(1, min(limit, 25))
        query = "^".join(
            [
                f"assignment_group={group}",
                *(f"short_descriptionLIKE{w}^ORdescriptionLIKE{w}" for w in found),
                f"opened_at>=javascript:gs.daysAgoStart({days})",
                "ORDERBYDESCopened_at",
            ]
        )
        return [summary(r) for r in rows(query, FIELDS, limit)]

    @mcp.tool(annotations=READ)
    def get_incident(number: str) -> dict[str, Any]:
        """One incident of this Contract's assignment group (e.g. INC0010001): its
        description, and how it was resolved."""
        if not re.fullmatch(NUMBER, number):
            raise ToolError(f"not an incident number: {number[:40]!r}")
        query = f"number={number}^assignment_group={group}"
        fields = [*FIELDS, "description", "close_notes"]
        # Checked on what came back: a dropped clause would answer with another incident.
        found = [r for r in rows(query, fields, 1) if actual(r.get("number")) == number]
        if not found:
            raise ToolError(f"no incident {number} in this Contract's assignment group")
        row = found[0]
        return {
            **summary(row),
            "description": truncate(shown(row.get("description")).strip(), DESCRIPTION_LIMIT),
            "resolution_notes": truncate(shown(row.get("close_notes")).strip(), NOTES_LIMIT),
        }

    return mcp
