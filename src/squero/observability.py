"""Audit log and Prometheus metrics for the agent itself.

Every tool call (read or write) is logged as one JSON line and counted, so you
can answer "what did the agent touch, and when?" after the fact.
"""

import json
import logging
import time
from typing import Any

from fastmcp.server.middleware import Middleware, MiddlewareContext
from prometheus_client import Counter, Gauge, Histogram

log = logging.getLogger("squero.audit")

TOOL_CALLS = Counter("squero_tool_calls_total", "MCP tool calls", ["tool", "status"])
TOOL_SECONDS = Histogram("squero_tool_call_seconds", "MCP tool call latency", ["tool"])
TRIAGES = Counter("squero_triages_total", "Triage runs", ["outcome"])
TRIAGE_SECONDS = Histogram(
    "squero_triage_seconds", "End-to-end triage time", buckets=(5, 10, 20, 30, 60, 120, 300)
)
PENDING_APPROVALS = Gauge("squero_pending_approvals", "Actions waiting for a human decision")
WEBHOOK_EVENTS = Counter(  # slice 19.3
    "squero_webhook_events_total", "Outbound webhook events, by how they ended", ["outcome"]
)


class AuditMiddleware(Middleware):
    def __init__(self, contract_id: str | None) -> None:
        self.contract_id = contract_id

    def who(self, context: MiddlewareContext[Any]) -> dict[str, Any]:
        """Whose call it was, first in the line."""
        return {"contract": self.contract_id}

    async def on_call_tool(self, context: MiddlewareContext[Any], call_next: Any) -> Any:
        tool = context.message.name
        start = time.monotonic()
        status = "error"
        try:
            result = await call_next(context)
            status = "error" if result.is_error else "ok"
            return result
        finally:
            elapsed = time.monotonic() - start
            TOOL_CALLS.labels(tool, status).inc()
            TOOL_SECONDS.labels(tool).observe(elapsed)
            # Arguments are namespaces, names, PromQL: no secrets by construction.
            log.info(
                json.dumps(
                    {
                        "event": "tool_call",
                        **self.who(context),
                        "tool": tool,
                        "args": context.message.arguments,
                        "status": status,
                        "ms": round(elapsed * 1000),
                    }
                )
            )
