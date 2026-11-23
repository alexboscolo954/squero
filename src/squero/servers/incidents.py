"""Similar incidents MCP server: the Contract's own past Triage Reports (slice 7.1).

Postgres full-text search over the Hub's Investigations, merged with a search by
meaning when the Contract has an Embedder (slice 7.4). The server is built for one
Contract and puts its id in every query; the model passes only words and ids, so
another Contract's Reports are never reachable.
"""

from datetime import UTC, datetime
from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from squero.embeddings import Embedder
from squero.models import TriageReport
from squero.servers import Access
from squero.store import Store, now

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
ACCESS: dict[str, Access] = {"search": "read", "read": "read"}  # ADR 0002
GUIDE = """\
Past incidents (incidents_ tools): when triaging an alert, call incidents_search once
for the alerting workload before writing the Report, even if the cause looks clear: a
past Report can hold what current tools can't show, such as a fix already tried. A
match is a lead, not proof: it was the agent's own conclusion then. Read it
(incidents_read), confirm what it says with the current tools, and cite it as
Evidence only next to current evidence. A search that found nothing is not Evidence.
"""


def _date(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%d %H:%M UTC")


def build_server(
    contract_id: str, store: Store | None, embedder: Embedder | None = None
) -> FastMCP:
    """store None: tool definitions only (replay answers before any call).
    embedder: this Contract's, to search by meaning too; None: words only."""
    mcp = FastMCP("incidents", instructions="This Contract's past Triage Reports.")

    def db() -> Store:
        if store is None:
            raise ToolError("past incidents need the Hub's database")
        return store

    @mcp.tool(annotations=READ)
    async def search(
        keywords: str,
        days: Annotated[int, Field(ge=1, le=365)] = 90,
        limit: Annotated[int, Field(ge=1, le=10)] = 5,
    ) -> dict[str, Any]:
        """Past Reports of this Contract matching any of the keywords (a workload,
        an error, a symptom), best match first. Read one with incidents_read."""
        near = None
        if embedder and (vector := await embedder.query(keywords)):
            near = (vector, embedder.model, embedder.max_distance)
        rows = await db().similar(contract_id, keywords, now() - days * 86400, limit, near)
        if not rows:
            return {"incidents": [], "note": f"No past Report in the last {days} days matches."}
        found = []
        for r in rows:
            report = TriageReport.model_validate(r["report"])
            found.append(
                {
                    "id": r["id"],
                    "date": _date(r["started"]),
                    "title": r["title"],
                    "summary": report.summary,
                    "category": report.category,
                    "root_cause": report.probable_root_cause,
                    "confidence": report.confidence,
                }
            )
        return {"incidents": found}

    @mcp.tool(annotations=READ)
    async def read(id: str) -> dict[str, Any]:
        """One past Report in full, with what the Operator did about it: the actions
        approved or rejected (and why), what ran, and its Jira issue."""
        row = await db().past_report(contract_id, id)
        if row is None:
            raise ToolError(f"no past Report {id!r} in this Contract")
        report = TriageReport.model_validate(row["report"])
        decisions = [
            {
                "approved": d["approve"],
                "actions": [a["name"] for a in d["actions"]],
                "message": d["message"],
                "by": d["decided_by"],
            }
            for d in await db().decisions(row["id"])
        ]
        return {
            "id": row["id"],
            "date": _date(row["started"]),
            "title": row["title"],
            "report": report.to_markdown(row["executed_actions"]),
            "decisions": decisions,
            "jira_issue": row["jira_issue"],
            # only when one was filed (slice 19.5b): nothing changes for the others
            **({"servicenow_incident": n} if (n := row["servicenow_incident"]) else {}),
        }

    return mcp
