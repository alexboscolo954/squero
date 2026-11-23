"""Alertmanager webhook receiver for every Contract.

POST /contracts/{id}/alertmanager  That Contract's Alertmanager (its own webhook token).
POST /contracts/{id}/grafana       That Contract's Grafana Alerting, same token, normalized
                                   into the same Alert group.
POST /contracts/{id}/pagerduty     That Contract's PagerDuty (its signature, not a token): an
                                   incident starting or ending, as an Alert group.
                                Starts a triage, or makes a resolved group's
                                proposed actions stale. Unknown id = wrong token = 401.
GET  /investigations[/{id}]     Status, report, pending actions (approver token).
POST /investigations/{id}/decision  Approve or reject pending actions (approver token).
                                The same decision path as the Slack buttons.
GET  /healthz, /metrics

State lives in Postgres (store.py): Investigations, the dedup memory, every
Approval decision and the LangGraph checkpoints, so a pending approval survives a
restart. ponytail: timers and the concurrency limit are per process, so run one
replica; several would each re-arm the same deadlines (harmless: claims are
atomic) but share no limit.
"""

import asyncio
import contextlib
import functools
import json
import logging
import re
import secrets
import time
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastmcp import Client
from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, ValidationError

from squero import (
    actuator,
    agent,
    billing,
    blockers,
    checks,
    clientchannels,
    console,
    contracts,
    deploys,
    embeddings,
    hubmcp,
    listprices,
    masking,
    notices,
    pagerduty,
    proposals,
    protocol,
    related,
    relay,
    sizing,
    teachings,
    usage,
    webapp,
    webhooks,
)
from squero.config import Settings
from squero.contracts import Jira, Kubernetes, ServiceNow
from squero.models import AlertGroup, GrafanaAlertGroup, Investigation
from squero.observability import PENDING_APPROVALS, TRIAGE_SECONDS, TRIAGES
from squero.safety import redact, redact_capped
from squero.servers import jira, servicenow
from squero.sizing import utc
from squero.slack import (
    STATUS,
    Slack,
    clean,
    client_note,
    client_text,
    feedback_blocks,
    parent_blocks,
    parent_text,
    reject_modal,
    related_blocks,
    related_text,
    reply_text,
    teaching_blocks,
    teaching_text,
    text_modal,
)
from squero.store import Store, connect, now

log = logging.getLogger("squero.api")
CHECK_TICK = 30  # seconds between looks for due Checks
NOTICE_EVERY = 600  # seconds between looks for Stale notices (audit A14)
# Rollbacks told in Slack (slice 12.9c): a cluster's in one hour, the rest counted in one
# line; and how long one Slack never took is still tried
ROLLBACK_POSTS, TELL_FOR, LATE = 20, 7 * 86400, 600
REST = 300.0  # a Contract whose post Slack didn't take is left alone this long, doubling
REPORTED = ("triage", "check")  # kinds whose Report gets Feedback, Jira, the client
MODALS = ("reject", "correction", "teaching_edit")  # Slack modals: <callback>_submit
TEACHING_ACTIONS = {
    "feedback_right",
    "feedback_wrong",
    "correction_submit",
    "teaching_approve",
    "teaching_edit",
    "teaching_edit_submit",
    "teaching_reject",
}
DEDUP_SECONDS = 30 * 60
PROBE_SECONDS = 10  # each health check


def usd(amount: float) -> str:
    """Cents above a dollar, tenths of a cent below: small spends stay visible."""
    return f"${amount:.2f}" if amount >= 1 else f"${amount:.4f}"


def shown_settings(settings: Settings) -> dict[str, Any]:
    """The Hub's settings for the console. pydantic masks SecretStr fields; these
    two can carry credentials as plain strings, so they're masked here too."""
    shown = settings.model_dump(mode="json")
    shown["model_headers"] = {name: "**********" for name in settings.model_headers}
    if settings.model_base_url:
        url = urlsplit(settings.model_base_url)
        if url.username or url.password:
            host = url.netloc.rsplit("@", 1)[1]
            shown["model_base_url"] = url._replace(netloc=f"**********@{host}").geturl()
    return shown


KILLED = "The Contract's Kill switch is on: approving is refused. Reject, or turn it off first."


class KillSwitchOn(Exception):
    """An approval refused because the Contract's Kill switch is on (slice 11.2a)."""


class Decision(BaseModel):
    approve: bool
    message: str = ""


def create_app(
    settings: Settings | None = None,
    model: BaseChatModel | None = None,
    mcp_factory: Any = None,
    by_id: dict[str, contracts.Contract] | None = None,
    slack_http: httpx.AsyncClient | None = None,
    changes_server: Any = None,
    price_http: httpx.Client | None = None,
    bill_http: httpx.Client | None = None,
    github_http: httpx.Client | None = None,
) -> FastAPI:
    """mcp_factory(contract) -> Toolbox, by_id (the loaded Contracts), slack_http
    (an httpx client for Slack's Web API) and changes_server(contract) -> its
    kubernetes MCP server (for deploy verification's polls), for tests."""
    settings = settings or Settings()
    if not settings.approver_token:
        raise RuntimeError("set SQUERO_APPROVER_TOKEN")
    approver_token = settings.approver_token.get_secret_value()
    if by_id is None:
        if not settings.contracts_dir:
            raise RuntimeError("set SQUERO_CONTRACTS_DIR")
        by_id = contracts.load(settings.contracts_dir)
        usage.ask_stores(settings.contracts_dir, by_id)  # slice A1
    for c in by_id.values():
        if contracts.token_matches(c, approver_token):
            raise RuntimeError(f"Contract {c.id!r}'s webhook token is the approver token")
    # what a Proposed change is checked against: the Contracts as loaded now (slice 11.3)
    loaded = (
        proposals.snapshot(settings.contracts_dir, by_id) if settings.contracts_dir else (None, {})
    )
    mcp_token = settings.mcp_token.get_secret_value() if settings.mcp_token else ""
    if mcp_token:
        if not settings.console_operators:
            raise RuntimeError(
                "SQUERO_MCP_TOKEN needs SQUERO_CONSOLE_OPERATORS: the logins that may use it"
            )
        if len(mcp_token) < 32:
            raise RuntimeError("SQUERO_MCP_TOKEN is too short: use 32 characters or more")
        if secrets.compare_digest(mcp_token.encode(), approver_token.encode()):
            raise RuntimeError("SQUERO_MCP_TOKEN is the approver token: give it its own")
        for c in by_id.values():
            if contracts.token_matches(c, mcp_token):
                raise RuntimeError(f"Contract {c.id!r}'s webhook token is the MCP token")
    # Each Relay's and Actuator's token names one Contract's cluster, and nothing else
    # (slices 3.1, 12.1).
    try:
        relay_tokens = relay.tokens(by_id)
        actuator_tokens = relay.tokens(by_id, "Actuator", relay_tokens)
    except ValueError as e:
        raise RuntimeError(str(e)) from None
    for article, role, known in (
        ("a", "Relay", relay_tokens),
        ("an", "Actuator", actuator_tokens),
    ):
        for other, what in ((approver_token, "the approver token"), (mcp_token, "the MCP token")):
            if other and relay.who(f"Bearer {other}", known):
                raise RuntimeError(f"{article} {role} token is {what}: give each its own")
        for c in by_id.values():
            for digest, (cid, cluster) in known:
                webhook = c.webhook_token_sha256 or ""
                if secrets.compare_digest(digest.hex(), webhook):
                    raise RuntimeError(
                        f"{cid}/{cluster}: its {role} token is {c.id!r}'s webhook token"
                    )
    if not settings.database_url:
        raise RuntimeError("set SQUERO_DATABASE_URL")
    database_url = settings.database_url.get_secret_value()
    operator = settings.slack_operator_id
    if settings.slack_app_token and not (
        settings.slack_bot_token and operator and re.fullmatch(r"[UW][A-Z0-9]{8,}", operator)
    ):
        raise RuntimeError(
            "SQUERO_SLACK_APP_TOKEN needs SQUERO_SLACK_BOT_TOKEN and "
            "SQUERO_SLACK_OPERATOR_ID (U...)"
        )
    buttons = settings.slack_app_token is not None
    hub_provider = settings.model.split(":", 1)[0]
    prices = settings.model_prices
    for c in by_id.values():
        effective = c.model or settings.model
        if c.monthly_budget_usd and effective not in settings.model_prices:
            raise RuntimeError(
                f"Contract {c.id!r} has a budget, but its model {effective!r} has no price "
                "in SQUERO_MODEL_PRICES"
            )
        embedder = settings.embedding_model
        if c.monthly_budget_usd and embeddings.on(settings, c) and embedder not in prices:
            raise RuntimeError(
                f"Contract {c.id!r} has a budget, but the embedding model {embedder!r} has "
                "no price in SQUERO_MODEL_PRICES"
            )
        if c.model and settings.model_base_url and c.model.split(":", 1)[0] != hub_provider:
            raise RuntimeError(
                f"Contract {c.id!r}'s model {c.model!r} would go through the Hub's gateway "
                f"(SQUERO_MODEL_BASE_URL): use a {hub_provider}: model, as SQUERO_MODEL does"
            )
    models: dict[str, BaseChatModel] = {}  # per Contract, built once

    def model_for(c: contracts.Contract) -> BaseChatModel:
        if model is not None:  # tests: one scripted model for every Contract
            return model
        if c.id not in models:
            models[c.id] = agent.build_model(agent.for_contract(settings, c))
        return models[c.id]

    relays = relay.HUB  # the connected Relays, by Contract and cluster
    if mcp_factory is None:
        mcp_factory = lambda c: agent.build_mcp(c, settings, store=store(), relays=relays)  # noqa: E731

    def kube(c: contracts.Contract) -> Kubernetes:
        return next(i for i in c.integrations if isinstance(i, Kubernetes))

    if changes_server is None:  # the kubernetes server only: never vendors, every poll
        changes_server = lambda c: agent.integration_server(c, kube(c), settings)[0]  # noqa: E731
    seen: dict[tuple[str, str], deploys.Seen] = {}  # per (Contract, cluster)
    pollers: dict[str, Any] = {}  # each Contract's kubernetes server, built once
    # The workloads not running ready at the last poll, per (Contract, cluster,
    # namespace): "before" for a deploy the next poll sees. None: couldn't tell.
    failing: dict[tuple[str, str, str], set[str] | None] = {}
    db: dict[str, Any] = {}  # store and checkpointer, opened in the lifespan
    slots = asyncio.Semaphore(settings.max_concurrent_triages)
    # audit A10: with two Contracts or more, each takes at most half the slots (at least
    # one), waiting in its own queue, so one's alert storm can't hold them all
    share = max(1, settings.max_concurrent_triages // 2) if len(by_id) > 1 else None
    shares = {cid: asyncio.Semaphore(share) for cid in by_id} if share else {}
    tasks: set[asyncio.Task[None]] = set()

    def store() -> Store:
        st: Store = db["store"]
        return st

    hub_mcp = (
        hubmcp.build(
            by_id,
            store,
            now,
            lambda cid, text: console_ask(cid, text, None),
            lambda *decision: mcp_decide(*decision),
            lambda cid, by: turn_on_kill_switch(cid, by),
            lambda *change: propose_change(*change),
        ).http_app(path="/", stateless_http=True, json_response=True)
        if mcp_token
        else None
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        async with connect(database_url) as pool, slack_http or httpx.AsyncClient() as http:
            db["slack"] = Slack(settings.slack_bot_token, http)
            db["http"] = http  # no default headers: each caller sets its own auth
            db["hooks"] = app.state.hooks = webhooks.Sender(http)  # slice 19.3
            db["store"] = Store(pool)
            await store().migrate()
            db["checkpointer"] = AsyncPostgresSaver(pool, serde=agent.CHECKPOINT_SERDE)
            app.state.checkpointer = db["checkpointer"]  # for tests
            await db["checkpointer"].setup()
            await recover()
            await sync_checks()
            await sync_digests()
            spawn(scheduler())
            app.state.due_checks = due_checks  # for tests: one tick at a given time
            app.state.poll_deploys, app.state.due_deploys = poll_deploys, due_deploys
            app.state.due_sizings, app.state.due_digests = due_sizings, due_digests
            app.state.tell = tell
            app.state.due_notices = due_notices  # for tests: one look at a given time
            if settings.slack_app_token:
                spawn(db["slack"].listen(settings.slack_app_token, on_socket))
            try:
                async with AsyncExitStack() as stack:
                    if hub_mcp:  # a mounted app's lifespan only runs if its parent's does
                        await stack.enter_async_context(hub_mcp.lifespan(hub_mcp))
                    yield
            finally:
                for task in [*tasks, *sizing_task]:
                    task.cancel()
                await asyncio.gather(*tasks, *sizing_task, return_exceptions=True)
                await db["hooks"].close()  # events still queued are lost with the Hub

    app = FastAPI(title="squero", lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, e: RequestValidationError) -> JSONResponse:
        """A 422 that says where and why, never the value (slice 12.9c): FastAPI's own
        echoes it, and a NaN in a body can't be said in JSON, so that answer was a 500
        (which an Actuator sends again forever); nor is a request's content the Hub's to
        send back."""
        said = [{k: x[k] for k in ("type", "loc", "msg") if k in x} for x in e.errors()]
        return JSONResponse({"detail": said}, status_code=422)

    bearer = HTTPBearer()

    def check(creds: HTTPAuthorizationCredentials, expected: str) -> None:
        if not secrets.compare_digest(creds.credentials.encode(), expected.encode()):
            raise HTTPException(401, "invalid token")

    def approver_auth(creds: HTTPAuthorizationCredentials = Depends(bearer)) -> None:  # noqa: B008
        check(creds, approver_token)

    def emit(contract_id: str, event: str, data: dict[str, Any]) -> None:
        """One event for the Contract's Outbound webhook (slice 19.3), if it has one:
        queued, never waited for, and never a failure of what it tells of."""
        if "hooks" in db:
            db["hooks"].emit(by_id.get(contract_id), event, data)

    def announce(t: Investigation) -> None:
        """Where a Triage or a Check stands, as its thread says it (slice 19.3): it waits
        for a decision, it has its Report, or its Run failed. A Question is the
        Operator's own conversation: none."""
        if t.kind == "question":
            return
        about = {"investigation": webhooks.investigation(t)}
        if t.status == "awaiting_approval":
            asked = {"proposed": webhooks.proposed(t.pending), "deadline": t.deadline}
            emit(t.contract_id, "approval.needed", about | asked)
        elif t.status == "done":
            done = webhooks.report(t) | {"executed_actions": t.executed_actions}
            emit(t.contract_id, "report.finished", about | done)
        elif t.status == "failed":
            emit(t.contract_id, "investigation.failed", about | {"error": t.error})

    async def notify(t: Investigation) -> None:
        log.info("investigation %s %s", t.id, t.status)
        PENDING_APPROVALS.set(await store().pending_count())
        if t.kind == "check":
            await check_slack(t)
            return
        if t.status in ("done", "failed"):
            TRIAGES.labels(t.status).inc()
            TRIAGE_SECONDS.observe(now() - t.started)
        announce(t)
        done = t.kind == "triage" and t.status == "done"
        await to_slack(t, await reflexes_of(t) if done else None)

    async def reflexes_of(t: Investigation) -> str | None:
        """The Step-ups and Bursts (slice 13.3b) the Contract's Actuators made on the
        Workloads this Triage's Alerts name, from an hour before it began (slice 13.1c): a
        line the Hub itself adds to the Report's thread. Nothing the model sees, so no
        Scenario changes."""
        group = t.input or {}
        common = group.get("commonLabels") or {}
        named = [{**common, **(a.get("labels") or {})} for a in group.get("alerts") or []]
        lines = []
        for a in await store().actuations(t.contract_id, t.started - 3600):
            made = {"stepped_up": "stepped up", "burst": "raised the CPU limit of"}.get(
                a["outcome"]
            )
            if made is None or not actuator.to_tell(a["outcome"], a["reason"]):
                continue
            for labels in named or [common]:
                if labels.get("namespace") != a["namespace"]:
                    continue
                if labels.get("cluster", a["cluster"]) != a["cluster"]:
                    continue
                pod = labels.get("pod") or ""
                if labels.get(a["kind"].lower()) == a["name"] or pod.startswith(a["name"] + "-"):
                    when = datetime.fromtimestamp(a["at"], UTC)
                    where = clean("/".join(a[k] for k in ("cluster", "namespace", "kind", "name")))
                    lines.append(
                        f":arrow_up: The Actuator {made} {where} container "
                        f"{clean(a['container'])} at {when:%H:%M} UTC: {clean(a['reason'])}"
                    )
                    break
        return "\n".join(lines[:3]) or None

    async def to_slack(t: Investigation, note: str | None = None) -> None:
        """The Investigation's thread in its own Contract's channel: start it if it has
        none yet (or the first post failed), keep the parent's status (and buttons)
        current, and reply with the note and anything the Operator should read."""
        contract = by_id.get(t.contract_id)
        channel = t.slack_channel or (
            contract.slack.operator_channel if contract and contract.slack else None
        )
        if not channel or not settings.slack_bot_token:
            return
        slack: Slack = db["slack"]
        if t.kind == "question":
            # The thread's root is the Operator's own message: never post or edit a
            # parent, only reply. A Question never pauses, so it has no buttons.
            if t.slack_ts:
                status = reply_text(t) or f"{STATUS[t.status]} · `{t.id}`"
                await slack.post(channel, status, thread_ts=t.slack_ts)
            return
        blocks = await parent(t)
        if t.slack_ts is None:
            ts = await slack.post(channel, parent_text(t), blocks=blocks)
            if ts and await store().set_slack_thread(t.id, channel, ts):
                t.slack_channel, t.slack_ts = channel, ts
        else:
            await slack.update(channel, t.slack_ts, parent_text(t), blocks)
        if not t.slack_ts:
            return
        for reply in (note, reply_text(t, buttons)):
            if reply:
                await slack.post(channel, reply, thread_ts=t.slack_ts)
        if buttons and t.kind in REPORTED and t.status == "done" and t.report:
            await slack.post(channel, "Was this Report right?", t.slack_ts, feedback_blocks(t.id))

    async def check_slack(t: Investigation) -> None:
        """A passing Check is quiet. A failing one (or one whose Run failed) opens the
        Check's Open failure, a thread like a Triage's; later failures reply in it,
        unless they need a decision (their own thread, the Open failure now). The
        first pass after replies "recovered" there and ends it."""
        name = str((t.input or {}).get("check", ""))
        row = await store().check_schedule(t.contract_id, name)
        open_id = row["failing_id"] if row else None
        verdict = t.check_result
        if t.status == "done" and verdict and verdict.passed:
            if t.slack_ts or open_id:  # as Slack: a passing Check is quiet unless it
                announce(t)  # ends a failure, or a decision was asked in its thread
            if t.slack_ts:  # resumed after a decision in its own thread
                await to_slack(t)
            if open_id and await store().set_failing(t.contract_id, name, None, open_id):
                text = f":white_check_mark: Recovered: {clean(verdict.summary, 1000)} · `{t.id}`"
                await note_in(open_id, text)
            return
        announce(t)
        if open_id and not t.slack_ts and t.status != "awaiting_approval":
            what = verdict.summary if verdict else f"the Run failed: {t.error or 'unknown error'}"
            await note_in(open_id, f":x: Still failing: {clean(what, 1000)} · `{t.id}`")
            return
        await to_slack(t)
        moved = t.slack_ts and open_id != t.id
        if moved and await store().set_failing(t.contract_id, name, t.id, open_id) and open_id:
            await note_in(open_id, f":arrow_right: Continued in a new thread: `{t.id}`")

    async def note_in(investigation_id: str, text: str) -> None:
        other = await store().get(investigation_id)
        if other:
            await thread_note(other, text)

    async def thread_note(t: Investigation, text: str) -> None:
        """A reply in the Investigation's thread, when it has one."""
        if t.slack_channel and t.slack_ts and settings.slack_bot_token:
            await db["slack"].post(t.slack_channel, text, thread_ts=t.slack_ts)

    def usage_sink(t: Investigation) -> agent.UsageSink:
        """Each model call's tokens and cost, on the Investigation it is for."""
        contract = by_id[t.contract_id]
        name = contract.model or settings.model
        price = settings.model_prices.get(name)

        async def used(input_tokens: int, output_tokens: int) -> None:
            cost = price.cost(input_tokens, output_tokens) if price else None
            await store().record_usage(t.id, t.contract_id, name, input_tokens, output_tokens, cost)

        return used

    maskers = {c.id: masking.for_contract(c) for c in by_id.values()}  # None: no mask

    def masked_sink(t: Investigation) -> masking.MaskSink:
        """What each model call masked, for the console's count."""

        async def seen(placeholders: list[str]) -> None:
            await store().record_masked(t.id, t.contract_id, placeholders)

        return seen

    async def taught(t: Investigation) -> str:
        """The Contract's approved Teachings for this Run's prompt: a Triage's for its
        alert names first, then the general ones; a Question's, every one."""
        approved = await store().teachings(t.contract_id, "approved")  # never others
        alerts = None
        if t.kind == "triage":
            group = AlertGroup.model_validate(t.input) if t.input else None
            alerts = {a.labels.get("alertname", "") for a in group.firing} if group else set()
        elif t.kind == "check":
            alerts = set()  # no alert names: the general ones only
        text, left = teachings.section(approved, alerts)
        if left:
            log.warning("investigation %s: %d approved Teachings didn't fit", t.id, left)
        return text

    async def give_feedback(
        t: Investigation, correct: bool, correction: str | None, by: str, who: str
    ) -> None:
        """The Operator's verdict on a finished Triage Report, recorded first; a
        correction then has a Teaching drafted from it."""
        await store().add_feedback(t, correct, correction, by)
        log.info("investigation %s feedback correct=%s by %s", t.id, correct, by)
        if correct:
            await thread_note(t, f":+1: {who} marked this Report right.")
            return
        if not correction:
            return
        await thread_note(t, f":-1: {who} marked this Report wrong: {clean(correction, 2000)}")
        spawn(propose(t, correction, by))

    async def propose(t: Investigation, correction: str, by: str) -> None:
        """A Teaching drafted from the Operator's words, into the review queue. Never
        fails anything else: the feedback or decision is already recorded."""
        contract = by_id.get(t.contract_id)
        if contract is None:
            return
        try:
            if reason := await over_budget(t.contract_id):
                await thread_note(t, f":no_entry: No Teaching drafted. {reason}")
                return
            approved = await store().teachings(t.contract_id, "approved")  # never others
            context = (
                t.report.to_markdown(t.executed_actions)
                if t.report
                else f"No Report ({t.status}) for {t.title}"
            )
            d = await teachings.draft(
                model_for(contract),
                agent.for_contract(settings, contract),
                context,
                correction,
                approved,
                usage_sink(t),
                maskers[contract.id],
            )
        except Exception:
            log.exception("investigation %s: no Teaching drafted", t.id)
            await thread_note(
                t, ":warning: Couldn't draft a Teaching from that: write one on the console."
            )
            return
        pending = await store().teachings(t.contract_id, "proposed")
        twin = d.duplicate_of or next(
            (x["id"] for x in approved + pending if teachings.same(x["text"], d.text)), None
        )
        if twin is not None:
            twice = f":repeat: Already taught, or waiting for review: Teaching #{twin}."
            await thread_note(t, twice)
            return
        tid = await store().add_teaching(
            t.contract_id,
            redact(d.text),  # stored as the review post shows it
            d.alerts,
            source_id=t.id,
            correction=correction,
            contradicts=d.contradicts,
        )
        log.info("investigation %s proposed teaching %s (from %s)", t.id, tid, by)
        row = await store().teaching(tid)
        assert row is not None
        if t.slack_channel and t.slack_ts and settings.slack_bot_token:
            text = teaching_text(row)
            if not buttons:
                text += "\nReview it on the console's Teachings page."
            blocks = teaching_blocks(row, t.id) if buttons else None
            ts = await db["slack"].post(t.slack_channel, text, t.slack_ts, blocks)
            if ts:
                await store().set_teaching_post(tid, ts)
        await budget_notices(t.contract_id)

    async def decide_teaching(
        teaching_id: int,
        contract_id: str,
        approve: bool,
        by: str,
        who: str,
        text: str | None = None,
        source_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Approve (with the text the Operator submitted) or reject a proposed
        Teaching, once, then show the verdict on its review post."""
        row = await store().decide_teaching(teaching_id, contract_id, approve, by, text, source_id)
        if row is None:
            return None
        log.info("teaching %s %s by %s", teaching_id, row["status"], by)
        source = await store().get(row["source_id"]) if row["source_id"] else None
        if row["slack_ts"] and source and source.slack_channel and settings.slack_bot_token:
            mark = ":white_check_mark: Approved" if approve else ":no_entry: Rejected"
            shown = f"{teaching_text(row)}\n{mark} by {who}."
            if row["retired"]:
                shown += f" Teaching #{row['retired']} is retired."
            await db["slack"].update(source.slack_channel, row["slack_ts"], shown, [])
        return row

    async def over_budget(contract_id: str) -> str | None:
        """Why a new Run may not start: the Contract has spent its monthly budget.
        Checked only where a new Run would start, never on a resume (an approved
        action must still be reported). ponytail: Runs that start together can all
        pass the check and overshoot; a reservation per Run if that ever matters."""
        budget = by_id[contract_id].monthly_budget_usd
        spent = await store().month_spend(contract_id, now()) if budget else 0.0
        if not budget or spent < budget:
            return None
        return (
            f"Contract {contract_id!r} has used its monthly budget: {usd(spent)} of "
            f"{usd(budget)}. New Investigations are refused until next month, or until "
            "monthly_budget_usd is raised."
        )

    async def refuse(t: Investigation, reason: str) -> None:
        """No Run, no model call: the Operator still hears about it (fail notifies)."""
        log.warning("investigation %s refused: %s", t.id, reason)
        await fail(t, reason)
        await budget_notices(t.contract_id)

    async def budget_notices(contract_id: str) -> None:
        """80% and 100% of the month's budget, each posted once per Contract per month
        in its operator channel (logged when it has none)."""
        c = by_id[contract_id]
        if not c.monthly_budget_usd:
            return
        at = now()
        spent = await store().month_spend(c.id, at)
        for level in (80, 100):
            if spent < c.monthly_budget_usd * level / 100:
                continue
            if not await store().notice_once(c.id, at, level):
                continue
            if level == 100:
                text = (
                    f":no_entry: *[{clean(c.id)}]* has used its monthly budget: "
                    f"{usd(spent)} of {usd(c.monthly_budget_usd)}. New Investigations are "
                    "refused until next month, or until `monthly_budget_usd` is raised."
                )
            else:
                text = (
                    f":warning: *[{clean(c.id)}]* has used {level}% of its monthly budget: "
                    f"{usd(spent)} of {usd(c.monthly_budget_usd)}."
                )
            log.warning("budget %s %d%%: %.4f of %.2f", c.id, level, spent, c.monthly_budget_usd)
            if c.slack and settings.slack_bot_token:
                await db["slack"].post(c.slack.operator_channel, text)

    async def fail(t: Investigation, error: str) -> None:
        t.status, t.error, t.pending, t.deadline = "failed", error, [], None
        await store().save(t)
        await notify(t)

    async def recover() -> None:
        """After a restart. A Run that was cut off is never replayed: if it was resuming
        an approval, the write may already have run, and replaying could run it twice."""
        for t in await store().unfinished():
            if t.contract_id not in by_id:
                await fail(t, f"Contract {t.contract_id!r} is no longer configured")
            elif t.status == "running":
                error = (
                    "The Hub restarted during this run. It was not resumed, so nothing runs twice."
                )
                last = await store().last_decision(t.id)
                if last and last["approve"]:
                    names = ", ".join(a["name"] for a in last["actions"])
                    error += (
                        f" The approved actions ({names}) may or may not have run:"
                        " check the cluster."
                    )
                await fail(t, error)
            elif t.group_resolved:
                await reject_stale(t.id, t.deadline, "The alert group resolved before a decision")
            else:
                spawn(expire(t.id, t.deadline))  # a deadline already past fires at once

    def apply(t: Investigation, outcome: agent.Outcome) -> None:
        t.report, t.answer, t.pending = outcome.report, outcome.answer, outcome.pending
        t.check_result = outcome.check
        t.executed_actions = outcome.executed_actions
        t.status = "done" if outcome.done else "awaiting_approval"
        t.deadline = now() + settings.decision_timeout_seconds if t.pending else None

    def agent_kind(t: Investigation) -> agent.Kind:
        return t.kind

    async def run(
        t: Investigation, step: Any, note: str | None = None, teach: tuple[str, str] | None = None
    ) -> None:
        """teach: (the Operator's words, who): a Teaching is drafted once the Run ends,
        with its Report as context, never alongside it."""
        before = len(t.executed_actions)  # the list grows over a Triage's Runs
        if t.kind != "check" or t.slack_ts:  # a Check speaks only once it has a verdict
            await to_slack(t, note)  # "investigating", in a new or the existing thread
        async with shares.get(t.contract_id) or contextlib.nullcontext(), slots:
            try:
                # Always the Investigation's own Contract, on start and on every resume.
                toolbox = mcp_factory(by_id[t.contract_id])

                async def record(tool: str, args: dict[str, Any], result: str, error: bool) -> None:
                    await store().record_tool_call(t.id, t.contract_id, tool, args, result, error)

                contract = by_id[t.contract_id]
                checkpointer = db["checkpointer"]
                async with agent.open_agent(
                    settings,
                    toolbox,
                    model_for(contract),
                    checkpointer,
                    record,
                    kind=agent_kind(t),
                    usage=usage_sink(t),
                    taught=await taught(t),
                    masker=maskers[t.contract_id],
                    masked=masked_sink(t),
                    killed=lambda: kill_switch_on(t.contract_id),
                ) as a:
                    apply(t, await step(a))
            except Exception as e:  # report every failure, never lose an alert silently
                log.exception("investigation %s failed", t.id)
                t.status, t.error, t.pending, t.deadline = (
                    "failed",
                    f"{type(e).__name__}: {e}",
                    [],
                    None,
                )
        await store().save(t)
        # What this Run executed (slice 19.3), from the Hub's own record. A Run that
        # failed adds none (its list is as it was): its error says its actions may or may
        # not have run.
        if ran := t.executed_actions[before:]:
            about = {"investigation": webhooks.investigation(t), "actions": ran}
            emit(t.contract_id, "action.executed", about)
        if t.status == "awaiting_approval":
            fresh = await store().get(t.id)  # the group may have resolved during the Run
            if fresh and fresh.group_resolved:
                await reject_stale(t.id, t.deadline, "The alert group resolved before a decision")
                return  # no one to ask; the resumed run reports
        await notify(t)
        await budget_notices(t.contract_id)
        if t.status == "awaiting_approval":
            spawn(expire(t.id, t.deadline))
        if t.kind == "triage" and t.status == "done" and t.report:
            spawn(index(t))
        if teach:
            await propose(t, *teach)

    async def index(t: Investigation) -> None:
        """A finished Triage Report's embedding, for later searches by meaning. Its
        failure is logged and the Report is still found by its words."""
        embedder = embeddings.for_contract(settings, store(), by_id[t.contract_id])
        row = await store().past_report(t.contract_id, t.id) if embedder else None
        if embedder and row:
            await embedder.index(row)

    async def expire(investigation_id: str, deadline: float | None) -> None:
        await asyncio.sleep(max(0.0, (deadline or 0) - now()))
        await reject_stale(
            investigation_id, deadline, f"No decision within {settings.decision_timeout_seconds}s"
        )

    async def kill_switch_on(contract_id: str) -> bool:
        return await store().kill_switch(contract_id) is not None

    async def settle(
        t: Investigation,
        deadline: float | None,
        approve: bool,
        message: str,
        decided_by: str,
        note: str,
        reason: str | None = None,
        why: str | None = None,
    ) -> bool:
        """The one decision path (API, Slack buttons, Stale): claim the pause named
        by its deadline, record who decided, resume. False when that pause is
        already decided, Stale, or not the current one. reason: what the Operator
        typed when rejecting, which has a Teaching drafted after the Run. why: what made
        it Stale. Either is the reason an Outbound webhook gets (slice 19.3), never
        `message`: that is the Hub's instruction to the model.
        KillSwitchOn: an approval while the Contract's Kill switch is on; nothing is
        claimed, so the pause can still be rejected, or approved once it is off."""
        if approve and await kill_switch_on(t.contract_id):
            raise KillSwitchOn
        pending = await store().claim(t.id, deadline)
        if pending is None:
            return False
        log.info(
            "investigation %s decision approve=%s by %s actions=%s",
            t.id,
            approve,
            decided_by,
            [p["name"] for p in pending],
        )
        await store().record_decision(t.id, approve, pending, message, decided_by)
        decisions: list[dict[str, Any]] = [
            {"type": "approve"} if approve else {"type": "reject", "message": message}
            for _ in pending
        ]
        t.status, t.pending, t.deadline = "running", [], None
        decided = {  # slice 19.3; a Question never pauses, so never one's
            "investigation": webhooks.investigation(t),
            "approve": approve,
            "by": decided_by,
            "actions": [p["name"] for p in pending],
            "reason": why or reason or None,
        }
        emit(t.contract_id, "approval.decided", decided)
        teach = None
        if reason and not approve:
            names = ", ".join(sorted({p["name"] for p in pending}))
            teach = (f"The Operator rejected the proposed {names}, saying: {reason}", decided_by)
        spawn(run(t, lambda a: agent.resume(a, t.id, decisions), note, teach))
        return True

    async def reject_stale(investigation_id: str, deadline: float | None, reason: str) -> bool:
        """A stale proposed action is rejected automatically; the investigation still
        ends with a report. False when someone else already decided this pause."""
        t = await store().get(investigation_id)
        if t is None:
            return False
        msg = f"{reason}. The action was not executed. Do not retry; finish the report."
        note = f":hourglass: Stale, rejected automatically: {clean(reason, 500)}."
        return await settle(t, deadline, False, msg, "stale", note, why=reason)

    async def interact(p: dict[str, Any]) -> None:
        try:
            await interaction(p)
        except Exception:  # a malformed payload must not vanish silently
            log.exception("slack interaction %s failed", p.get("type"))

    def client_channel(t: Investigation) -> str | None:
        """From the loaded Contract, never from a click: the config decides."""
        contract = by_id.get(t.contract_id)
        return contract.slack.client_channel if contract and contract.slack else None

    async def noted(t: Investigation) -> list[str]:
        """The PagerDuty incidents the Client report goes on as a note (slice 19.2):
        this Investigation's own, where its Contract's PagerDuty has a key and a `From`.
        From the loaded Contract and the Hub's own memory, never from a click."""
        contract = by_id.get(t.contract_id)
        pd = contract.pagerduty if contract else None
        if not pd or not pd.from_secret:
            return []
        found = await store().groups_of(t.contract_id, t.id, pagerduty.PREFIX)
        return [k.removeprefix(pagerduty.PREFIX) for k in found]

    def chat_channels(t: Investigation) -> list[contracts.ClientChannel]:
        """The Contract's Teams and Google Chat channels (slice 19.4), from the loaded
        Contract, never from a click."""
        contract = by_id.get(t.contract_id)
        return list(contract.client_channels) if contract else []

    async def parent(t: Investigation) -> list[dict[str, Any]]:
        """The thread's parent, with the buttons this Investigation has now: the Send to
        client one names every place the Client report would go."""
        labels = [c.label for c in chat_channels(t)]
        incidents = bool(await noted(t))
        return parent_blocks(
            t,
            buttons,
            client_channel(t),
            jira_project(t),
            incidents,
            labels,
            servicenow=servicenow_of(t) is not None,
        )

    def jira_of(t: Investigation) -> Jira | None:
        """From the loaded Contract, never from a click."""
        contract = by_id.get(t.contract_id)
        found = [i for i in contract.integrations if isinstance(i, Jira)] if contract else []
        return found[0] if found else None

    def jira_project(t: Investigation) -> str | None:
        j = jira_of(t)
        return j.project if j else None

    def servicenow_of(t: Investigation) -> ServiceNow | None:
        """From the loaded Contract, never from a click."""
        contract = by_id.get(t.contract_id)
        found = [i for i in contract.integrations if isinstance(i, ServiceNow)] if contract else []
        return found[0] if found else None

    async def file_servicenow(t: Investigation, by: str, who: str) -> tuple[str | None, str]:
        """The Report as an incident in the Contract's ServiceNow assignment group, once
        (slice 19.5b), as file_jira does: claim, create, record. A refusal frees the
        claim for a retry; no usable answer keeps it, since ServiceNow may have created
        the incident. Returns (number, what to tell the Operator).
        ponytail: an unknown outcome keeps the claim for good (no duplicate incident);
        clearing servicenow_at in the database is the only reset, after checking."""
        s = servicenow_of(t)
        at = await store().claim_servicenow(t.id, by) if s and t.report else None
        if s is None or t.report is None or at is None:
            return None, "Already filed (or being filed), or nothing to file."
        password = by_id[t.contract_id].secret(s.password_secret)
        short = servicenow.incident_summary(t.report)
        text = servicenow.report_text(t.report, t.executed_actions, t.id)
        try:
            number, sys_id, grouped = await servicenow.create_incident(
                db["http"], s.url, s.user, password, s.group, short, text
            )
        except servicenow.Refused as e:
            await store().release_servicenow(t.id, at)
            why = clean(str(e), 500)
            return None, f"ServiceNow refused the incident ({why}): nothing was created."
        except servicenow.Unknown as e:
            log.warning("investigation %s: ServiceNow create got no usable answer: %s", t.id, e)
            return None, (
                f"ServiceNow didn't answer ({e}): it may have created the incident. "
                "Check the assignment group's incidents before filing it by hand."
            )
        await store().set_servicenow_incident(t.id, at, number, sys_id)
        t.servicenow_incident, t.servicenow_id = number, sys_id
        t.servicenow_by, t.servicenow_at = by, at
        log.info("investigation %s filed as %s by %s", t.id, number, by)
        # It exists, so the claim stays; the Operator is told it isn't where it should be.
        stray = (
            "" if grouped else " ServiceNow did not put it in the assignment group: move it there."
        )
        if not grouped:
            log.warning("investigation %s: %s is not in the Contract's group", t.id, number)
        if t.slack_channel and t.slack_ts and settings.slack_bot_token:
            slack: Slack = db["slack"]
            blocks = await parent(t)
            await slack.update(t.slack_channel, t.slack_ts, parent_text(t), blocks)
            url = f"{s.url.rstrip('/')}/incident.do?sys_id={sys_id}"
            await slack.post(
                t.slack_channel, f":ticket: Filed as {number} by {who}: {url}{stray}", t.slack_ts
            )
        return number, f"Filed as {number}.{stray}"

    async def file_jira(t: Investigation, by: str, who: str) -> tuple[str | None, str]:
        """The Report as an issue in the Contract's Jira project, once, from the Slack
        button or the console: claim, create, record. A refusal frees the claim for a
        retry; no answer keeps it, since Jira may have created the issue. Then the
        Slack thread, if any, shows it. Returns (key, what to tell the Operator).
        ponytail: an unknown outcome keeps the claim for good (no duplicate issue);
        clearing jira_at in the database is the only reset, after checking Jira."""
        j = jira_of(t)
        at = await store().claim_jira(t.id, by) if j and t.report else None
        if j is None or t.report is None or at is None:
            return None, "Already filed (or being filed), or nothing to file."
        token = by_id[t.contract_id].secret(j.token_secret)
        summary = jira.issue_summary(t.report)
        document = jira.report_document(t.report, t.executed_actions, t.id)
        try:
            key = await jira.create_issue(
                db["http"], j.url, j.email, token, j.project, j.issue_type, summary, document
            )
        except jira.Refused as e:
            await store().release_jira(t.id, at)
            return None, f"Jira refused the issue ({clean(str(e), 500)}): nothing was created."
        except jira.Unknown as e:
            log.warning("investigation %s: Jira create got no answer: %s", t.id, e)
            return None, (
                f"Jira didn't answer ({e}): it may have created the issue. "
                f"Check project {j.project} before filing it by hand."
            )
        await store().set_jira_issue(t.id, at, key)
        t.jira_issue, t.jira_by, t.jira_at = key, by, at
        log.info("investigation %s filed as %s by %s", t.id, key, by)
        if t.slack_channel and t.slack_ts and settings.slack_bot_token:
            slack: Slack = db["slack"]
            blocks = await parent(t)
            await slack.update(t.slack_channel, t.slack_ts, parent_text(t), blocks)
            url = f"{j.url.rstrip('/')}/browse/{key}"
            await slack.post(
                t.slack_channel, f":ticket: Filed as {key} by {who}: {url}", t.slack_ts
            )
        return key, f"Filed as {key}."

    async def send_to_client(t: Investigation, channel: str, user: str) -> None:
        """The Client report, once, to every place the client reads: claim, post to the
        Slack client channel as plain text (no blocks, so no controls), then a note on
        its PagerDuty incidents (slice 19.2), then each Teams and Google Chat channel
        (slice 19.4). The claim is one for all: a refusal frees it only while nothing
        has been sent; after that the thread names what wasn't taken, and nothing is
        sent twice. No answer keeps the claim: it may have arrived. ponytail: a crash
        between the claim and the post leaves it marked sent but unsent; check the
        client channel."""
        slack: Slack = db["slack"]
        client, incidents, chats = client_channel(t), await noted(t), chat_channels(t)
        wanted = client or incidents or chats
        at = await store().claim_client_report(t.id, f"slack:{user}") if wanted else None
        if not wanted or at is None:
            await slack.ephemeral(channel, user, "Already sent, or nothing to send.")
            return
        if client and await slack.post(client, client_text(t)) is None:
            await store().release_client_report(t.id, at)
            await slack.ephemeral(channel, user, "Slack refused the post: nothing was sent.")
            return
        secret = by_id[t.contract_id].secret
        # each further place: who it is, its send, and what a refusal or no answer is called
        sends: list[tuple[str, Any, str, str]] = []
        if incidents:
            pd = by_id[t.contract_id].pagerduty
            assert pd and pd.api_key_secret and pd.from_secret  # noted() checked
            key, sender = secret(pd.api_key_secret), secret(pd.from_secret)
        for incident in incidents:
            note = functools.partial(pagerduty.note, db["http"], key, sender, incident)
            lost = f"for incident {incident} ({{}}): it may have the note. Check the incident."
            sends.append(("PagerDuty", note, f"the note on incident {incident}", lost))
        for c in chats:
            post = functools.partial(clientchannels.send, db["http"], c.kind, secret(c.url_secret))
            lost = "({}): it may have the report. Check the channel."
            sends.append((c.label, post, "the report", lost))
        sent, took, missed = bool(client), [], []
        for who, send, what, lost in sends:
            try:
                await send(client_note(t))
                sent = True
                took.append(who)
            except (pagerduty.Refused, clientchannels.Refused) as e:
                if not sent:  # nothing has gone anywhere: a retry may work
                    await store().release_client_report(t.id, at)
                    first = "the note" if who == "PagerDuty" else what
                    text = f"{who} refused {first} ({e}): nothing was sent."
                    await slack.ephemeral(channel, user, text)
                    return
                missed.append(f"{who} refused {what} ({e}).")
            except (pagerduty.Unknown, clientchannels.Unknown) as e:
                sent = True  # it may be there: never a second one
                missed.append(f"{who} didn't answer {lost.format(e)}")
        t.client_sent_at, t.client_sent_by = at, f"slack:{user}"
        log.info("investigation %s client report sent by %s", t.id, t.client_sent_by)
        assert t.slack_ts is not None  # the click came from this thread's parent
        blocks = parent_blocks(  # sent: no Send button now
            t, buttons, client, jira_project(t), servicenow=servicenow_of(t) is not None
        )
        await slack.update(channel, t.slack_ts, parent_text(t), blocks)
        said = f":outbox_tray: Sent to the client by <@{user}>."
        if "PagerDuty" in took:
            said += " A note is on its PagerDuty incident."
        # a Teams workflow's yes is that it took the request, not that it posted
        if accepted := [who for who in took if who != "PagerDuty"]:
            said += f" Accepted by {', '.join(accepted)}."
        await slack.post(channel, " ".join([said, *missed]), t.slack_ts)

    async def triage_separately(
        t: Investigation, related_id: int, channel: str, user: str, message_ts: str | None
    ) -> None:
        """A Related alert group of t gets its own Triage, once: claimed together with
        that Triage, so a double click starts one. The budget is checked before the
        claim, so a refusal leaves the button for later. From then on the group is
        that Triage's own: its re-sends, resolution and Stale rule go there.
        ponytail: a group that resolved before the click isn't tracked, so its
        Triage starts as firing; track related groups' resolution if that matters."""
        slack: Slack = db["slack"]
        row = await store().related_group(related_id, t.id)
        gone = "Already triaged separately, or not a related group of this Triage."
        if row is None:
            await slack.ephemeral(channel, user, gone)
            return
        if reason := await over_budget(t.contract_id):
            await slack.ephemeral(channel, user, f"Not started. {reason}")
            return
        group = AlertGroup.model_validate(row["input"])
        new = Investigation(
            id=str(uuid.uuid4()),
            contract_id=t.contract_id,
            title=row["title"],
            group_key=row["group_key"],
            started=now(),
            input=row["input"],
        )
        if not await store().separate(related_id, t.id, new):
            await slack.ephemeral(channel, user, gone)
            return
        remembered = await store().alert_group(t.contract_id, new.group_key)
        seen = remembered[2] if remembered else {a.fingerprint for a in group.firing}
        await store().remember_group(t.contract_id, new.group_key, new.started, new.id, seen)
        log.info("related group %s of %s triaged separately as %s", related_id, t.id, new.id)
        if message_ts:  # the post clicked: its button goes, the rest stays
            severity = related.labels(group).get("severity")
            text = related_text(row["title"], row["matched"], severity, row["louder"])
            await slack.update(channel, message_ts, f"{text}\n:mag: Triaged separately.", [])
        title = clean(row["title"], 200)
        assert t.slack_ts is not None  # the click came from this thread
        reply = f":mag: <@{user}> had *{title}* triaged separately, in a new thread."
        await slack.post(channel, reply, t.slack_ts)
        spawn(run(new, lambda a: agent.start(a, group, new.id)))

    async def teaching_interaction(
        action_id: str,
        p: dict[str, Any],
        pause: dict[str, Any],
        t: Investigation,
        channel: str,
        user: str,
    ) -> None:
        """Feedback on a Report and the review of its Teachings, from the Operator in
        the Investigation's own channel (checked by the caller)."""
        slack: Slack = db["slack"]
        by, who = f"slack:{user}", f"<@{user}>"

        def typed(callback: str) -> str:
            value = p["view"]["state"]["values"][callback][callback].get("value") or ""
            return str(value).strip()

        if action_id in ("feedback_right", "feedback_wrong", "correction_submit"):
            if not (t.kind in REPORTED and t.status == "done" and t.report):
                await slack.ephemeral(channel, user, "Feedback is for a finished Report.")
            elif action_id == "feedback_right":
                await give_feedback(t, True, None, by, who)
            elif action_id == "feedback_wrong":
                meta = json.dumps({"id": t.id, "channel": channel})
                label = "Your correction. The agent drafts a Teaching from it for your review."
                modal = text_modal("correction", "What was wrong?", label, meta, "Send", 2000)
                await slack.open_view(p["trigger_id"], modal)
            elif correction := typed("correction"):
                await give_feedback(t, False, correction, by, who)
            return
        tid = int(pause["teaching"])
        gone = "Already decided, or not a Teaching of this thread."
        if action_id == "teaching_edit":
            row = await store().teaching(tid)
            if not row or row["status"] != "proposed" or row["source_id"] != t.id:
                await slack.ephemeral(channel, user, gone)
                return
            meta = json.dumps({"id": t.id, "channel": channel, "teaching": tid})
            modal = text_modal(
                "teaching_edit",
                "Edit and approve",
                "The Teaching, as it will be used",
                meta,
                "Approve",
                teachings.LIMIT,
                row["text"],
            )
            await slack.open_view(p["trigger_id"], modal)
            return
        approve = action_id in ("teaching_approve", "teaching_edit_submit")
        text = typed("teaching_edit") if action_id == "teaching_edit_submit" else None
        if text is not None and not 0 < len(text) <= teachings.LIMIT:
            size = f"A Teaching is 1 to {teachings.LIMIT} characters: nothing changed."
            await slack.ephemeral(channel, user, size)
            return
        decided = await decide_teaching(tid, t.contract_id, approve, by, who, text, t.id)
        if decided is None:
            await slack.ephemeral(channel, user, gone)

    async def interaction(p: dict[str, Any]) -> None:
        """A Slack button click or reject modal, from Socket Mode (already acked)."""
        slack: Slack = db["slack"]
        user = p.get("user", {}).get("id", "")
        if p.get("type") == "block_actions":
            action = p["actions"][0]
            pause, channel = json.loads(action["value"]), p["channel"]["id"]
        elif p.get("type") == "view_submission" and p["view"]["callback_id"] in MODALS:
            pause = json.loads(p["view"]["private_metadata"])
            channel = pause["channel"]
            action = {"action_id": f"{p['view']['callback_id']}_submit"}
        else:
            return
        if user != operator:
            await slack.ephemeral(channel, user, "Only the Operator can decide.")
            return
        t = await store().get(pause["id"])
        # A button only decides its own Investigation, from its own Contract's channel.
        if t is None or t.slack_channel != channel:
            await slack.ephemeral(channel, user, "That Investigation isn't in this channel.")
            return
        if action["action_id"] == "send_to_client":
            await send_to_client(t, channel, user)
            return
        if action["action_id"] in TEACHING_ACTIONS:
            await teaching_interaction(action["action_id"], p, pause, t, channel, user)
            return
        if action["action_id"] == "triage_separately":
            message_ts = p.get("container", {}).get("message_ts")
            await triage_separately(t, int(pause["related"]), channel, user, message_ts)
            return
        if action["action_id"] == "create_jira":
            key, message = await file_jira(t, f"slack:{user}", f"<@{user}>")
            if key is None:
                await slack.ephemeral(channel, user, message)
            return
        if action["action_id"] == "create_servicenow":
            number, message = await file_servicenow(t, f"slack:{user}", f"<@{user}>")
            if number is None:
                await slack.ephemeral(channel, user, message)
            return
        deadline = pause["deadline"]
        by, who = f"slack:{user}", f"<@{user}>"
        if action["action_id"] == "approve":
            try:
                done = await settle(
                    t, deadline, True, "", by, f":white_check_mark: Approved by {who}."
                )
            except KillSwitchOn:
                await slack.ephemeral(channel, user, KILLED)
                return
        elif action["action_id"] == "reject":
            # Claim only on submit: a cancelled modal leaves the pause undecided.
            done = t.status == "awaiting_approval" and t.deadline == deadline
            if done:
                meta = json.dumps({**pause, "channel": channel})
                await slack.open_view(p["trigger_id"], reject_modal(meta))
        else:
            reason = (p["view"]["state"]["values"]["reason"]["reason"].get("value") or "").strip()
            message = reason or "Rejected by the Operator. Do not retry."
            shown = f": {clean(reason, 2000)}" if reason else ""
            done = await settle(
                t, deadline, False, message, by, f":no_entry: Rejected by {who}{shown}", reason
            )
        if not done:
            await slack.ephemeral(channel, user, "Already decided or Stale: nothing to decide.")

    by_channel = {c.slack.operator_channel: c for c in by_id.values() if c.slack}
    client_channels = {c.slack.client_channel for c in by_id.values() if c.slack}

    def on_socket(kind: str, payload: dict[str, Any]) -> None:
        spawn(interact(payload) if kind == "interactive" else mention(payload))

    async def mention(p: dict[str, Any]) -> None:
        try:
            await on_mention(p.get("event", {}))
        except Exception:  # a malformed event must not vanish silently
            log.exception("slack mention failed")

    async def on_mention(event: dict[str, Any]) -> None:
        """@squero in a Contract's operator channel: a new Question, or in a Question's
        thread, a follow-up on it. Only the Operator, and never in a client channel."""
        if event.get("type") != "app_mention":
            return
        slack: Slack = db["slack"]
        user, channel, ts = event["user"], event["channel"], event["ts"]
        root = event.get("thread_ts") or ts
        if user != operator:
            await slack.ephemeral(channel, user, "Only the Operator can ask.")
            return
        contract = by_channel.get(channel)
        if contract is None:
            where = "the client sees this channel" if channel in client_channels else "no Contract"
            await slack.ephemeral(channel, user, f"Ask in a Contract's operator channel ({where}).")
            return
        text = re.sub(r"^(\s*<@[UW][A-Z0-9]+>)+\s*", "", event.get("text", ""))[:4000].strip()
        if not text:
            await slack.ephemeral(channel, user, "Ask a question after the mention.")
            return
        t = await store().by_thread(channel, root)
        if t is None:
            await start_question(contract.id, text, ts, channel, root)  # None: redelivered
        elif t.kind != "question":
            await slack.ephemeral(channel, user, "Follow-ups work in Question threads only.")
        elif not await follow_up(t, text, ts) and t.last_mention_ts != ts:
            # Still running. A redelivery of the mention it's answering needs no reply.
            await slack.ephemeral(channel, user, "Still answering the last one: ask again after.")

    async def start_question(
        contract_id: str,
        text: str,
        asked_ts: str,
        channel: str | None = None,
        root: str | None = None,
    ) -> Investigation | None:
        """A new Question, from Slack (its thread) or the console (no thread). None
        when its Slack thread already has one (a redelivered mention)."""
        t = Investigation(
            id=str(uuid.uuid4()),
            kind="question",
            contract_id=contract_id,
            title=text,  # whole: cut where shown, after masking (audit A2)
            started=now(),
            slack_channel=channel,
            slack_ts=root,
            last_mention_ts=asked_ts,
            input={"question": text},
        )
        if not await store().insert_question(t):
            return None
        if reason := await over_budget(contract_id):
            await refuse(t, reason)
            return t
        log.info("question %s for %s", t.id, contract_id)
        spawn(run(t, lambda a: agent.ask(a, text, t.id)))
        return t

    async def follow_up(t: Investigation, text: str, asked_ts: str) -> bool:
        """One more message on a finished Question; False while it's still running,
        or when asked_ts isn't newer than the last ask (a redelivery)."""
        if not await store().claim_follow_up(t.id, asked_ts, text):
            return False
        t.status, t.error, t.last_mention_ts = "running", None, asked_ts
        if reason := await over_budget(t.contract_id):
            await refuse(t, reason)
            return True
        log.info("question %s follow-up", t.id)
        spawn(run(t, lambda a: agent.ask(a, text, t.id)))
        return True

    async def sync_checks() -> None:
        """Each configured Check's schedule row; a changed schedule is due anew."""
        at = now()
        for c in by_id.values():
            due = {
                k.name: (f"{k.schedule} {k.timezone}", checks.next_due(k.schedule, k.timezone, at))
                for k in c.checks
            }
            await store().sync_checks(c.id, due)

    async def due_checks(at: float | None = None) -> list[str]:
        """Start every due Check once; the ids started. A Check removed from its
        contract.yaml is ignored. At 100% of the budget it is skipped, quietly (the
        budget notice already said so)."""
        at = now() if at is None else at
        started = []
        for contract_id, name in await store().due_checks(at):
            c = by_id.get(contract_id)
            k = next((k for k in c.checks if k.name == name), None) if c else None
            if c is None or k is None:
                continue
            t = Investigation(
                id=str(uuid.uuid4()),
                kind="check",
                contract_id=c.id,
                title=f"Check {name}",
                started=at,
                input={"check": name, "prompt": k.prompt},
            )
            following = checks.next_due(k.schedule, k.timezone, at)
            status = await store().claim_check(t, name, at, following, await over_budget(c.id))
            log.info("check %s/%s: %s", c.id, name, status)
            if status == "started":
                spawn(run(t, check_step(t, k.name, k.prompt)))
                started.append(t.id)
        return started

    def check_step(t: Investigation, name: str, prompt: str) -> Any:
        return lambda a: agent.check(a, name, prompt, t.id)

    async def poll_deploys(at: float | None = None) -> int:
        """Look for deploys in each opted-in Contract's clusters; queue each new one
        for verification. The number queued."""
        at = now() if at is None else at
        queued = 0
        for c in by_id.values():
            if c.deploy_checks is None:
                continue
            try:
                if c.id not in pollers:
                    pollers[c.id] = changes_server(c)
                async with Client(pollers[c.id]) as client:
                    for cl in c.direct_clusters:
                        args = {"cluster": cl.name, "since_minutes": deploys.WINDOW_MINUTES}
                        r = await client.call_tool("recent_changes", args, raise_on_error=False)
                        if r.is_error or not isinstance(r.structured_content, dict):
                            log.warning("deploys %s/%s: recent_changes failed", c.id, cl.name)
                            continue  # no baseline from a failed poll
                        entries = r.structured_content.get("result") or []
                        fresh = seen.setdefault((c.id, cl.name), deploys.Seen()).new(entries, at)
                        only = c.deploy_checks.namespaces
                        groups: dict[str, list[dict[str, str]]] = {}
                        for e in fresh:
                            if deploys.wanted(str(e.get("namespace", "")), only):
                                groups.setdefault(str(e["namespace"]), []).append(
                                    deploys.summary(e)
                                )
                        delay = 60.0 * c.deploy_checks.delay_minutes
                        for namespace, changes in groups.items():
                            known = [failing.get((c.id, cl.name, n)) for n in namespace.split(", ")]
                            before = (
                                None
                                if any(k is None for k in known)
                                else sorted(set().union(*[k for k in known if k is not None]))
                            )
                            await store().queue_deploy(
                                c.id, cl.name, namespace, changes, at, delay, before
                            )
                            log.info("deploy in %s/%s/%s queued", c.id, cl.name, namespace)
                            queued += len(changes)
                        here = [n for n in only or cl.namespaces if n in cl.namespaces]
                        for namespace in here:  # now: "before" for the next poll's deploys
                            pods = await client.call_tool(
                                "list_pods",
                                {"cluster": cl.name, "namespace": namespace},
                                raise_on_error=False,
                            )
                            listed = pods.structured_content if not pods.is_error else None
                            failing[(c.id, cl.name, namespace)] = (
                                deploys.unhealthy(listed.get("result") or [])
                                if isinstance(listed, dict)
                                else None
                            )
            except Exception:  # one Contract's cluster never stops the others
                log.exception("deploys %s: poll failed", c.id)
        return queued

    async def due_deploys(at: float | None = None) -> list[str]:
        """Start every due deploy verification once; the ids started."""
        at = now() if at is None else at
        started = []
        for row in await store().due_deploys(at):
            c = by_id.get(row["contract_id"])
            if c is None or c.deploy_checks is None:
                continue
            name = deploys.name(row["cluster"], row["namespace"])
            text = deploys.prompt(
                row["cluster"],
                row["namespace"],
                row["changes"],
                c.deploy_checks.prompt,
                row["failing_before"],
            )
            t = Investigation(
                id=str(uuid.uuid4()),
                kind="check",
                contract_id=c.id,
                title=f"Deploy check {row['namespace']}",
                started=at,
                input={"check": name, "prompt": text},
            )
            status = await store().claim_deploy(row["id"], t, name, at, await over_budget(c.id))
            log.info("deploy check %s/%s: %s", c.id, name, status)
            if status == "started":
                spawn(run(t, check_step(t, name, text)))
                started.append(t.id)
        return started

    async def due_sizings(at: float | None = None) -> list[str]:
        """Store every due Contract's hourly Sizing run; the Contracts run. One
        Contract's failure never stops the others."""
        at = now() if at is None else at
        done = []
        for c in by_id.values():
            try:
                if await sizing.store_run(store(), c, at) is not None:
                    done.append(c.id)
            except Exception:
                log.exception("Sizings %s failed", c.id)
        if any(c.list_prices for c in by_id.values()):
            try:
                http = price_http or listprices.client()
                try:
                    await listprices.refresh(store(), by_id.values(), http, at)
                finally:
                    if http is not price_http:
                        http.close()
            except Exception:
                log.exception("list prices failed")
        if any(cl.billing for c in by_id.values() for cl in c.clusters):
            try:
                http = bill_http or billing.client()
                try:
                    await billing.refresh(store(), by_id.values(), http, at)
                finally:
                    if http is not bill_http:
                        http.close()
            except Exception:
                log.exception("bills failed")
        return done

    def digested() -> dict[str, tuple[contracts.Contract, contracts.Digest]]:
        """The Contracts that get a weekly digest: Slack, a Prometheus and a bot token."""
        return {
            c.id: (c, c.sizing.digest)
            for c in by_id.values()
            if c.slack
            and settings.slack_bot_token
            and isinstance(c.sizing.digest, contracts.Digest)
            and usage.targets(c)
        }

    async def sync_digests() -> None:
        at = now()
        await store().sync_digests(
            {
                cid: (f"{d.schedule} {d.timezone}", checks.next_due(d.schedule, d.timezone, at))
                for cid, (_, d) in digested().items()
            }
        )

    async def due_digests(at: float | None = None) -> list[str]:
        """Post every due digest once, to its operator channel only; the Contracts
        posted. Claimed before posting: a post that fails is lost for that week, never
        sent twice."""
        at = now() if at is None else at
        posted = []
        for cid, (c, d) in digested().items():
            try:
                following = checks.next_due(d.schedule, d.timezone, at)
                if not await store().claim_digest(cid, at, following):
                    continue
                assert c.slack is not None
                text = await sizing.digest(store(), c, at, settings.console_url)
                ts = await db["slack"].post(c.slack.operator_channel, text)
                log.info("sizing digest %s: %s", cid, "posted" if ts else "not posted")
                if ts:
                    posted.append(cid)
            except Exception:
                log.exception("sizing digest %s failed", cid)
        return posted

    async def due_notices(at: float | None = None) -> None:
        """Stale notices (audit A14) of every Contract with a Slack channel, while the Hub
        has a bot token; one Contract failing stops none of the others."""
        if not settings.slack_bot_token:
            return
        at = now() if at is None else at

        async def post(channel: str, text: str, thread: str | None) -> str | None:
            ts: str | None = await db["slack"].post(channel, text, thread_ts=thread)
            return ts

        for cid, c in by_id.items():
            try:
                took = functools.partial(emit, cid)  # slice 19.3: what Slack took
                await notices.tick(store(), c, at, post, settings.console_url, took)
            except Exception:
                log.exception("stale notices %s failed", cid)

    sizing_task: list[asyncio.Task[Any]] = []

    async def scheduler() -> None:
        polled = noticed = 0.0
        while True:
            await asyncio.sleep(CHECK_TICK)
            if not sizing_task or sizing_task[0].done():  # a slow Prometheus never overlaps
                sizing_task[:] = [asyncio.create_task(due_sizings())]
            try:
                await due_checks()
                await due_digests()
                if time.monotonic() - noticed >= NOTICE_EVERY:
                    noticed = time.monotonic()
                    await due_notices()
                if not telling.locked():  # beside the tick, never in it: Slack may be slow
                    spawn(tell())  # Rollbacks Slack didn't take when they arrived (12.9c)
                if time.monotonic() - polled >= deploys.POLL_SECONDS:
                    polled = time.monotonic()
                    await poll_deploys()
                await due_deploys()
            except Exception:  # the next tick tries again
                log.exception("scheduled Checks failed")

    def spawn(coro: Any) -> None:
        task = asyncio.create_task(coro)
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    @app.post("/contracts/{contract_id}/alertmanager", status_code=202)
    async def alertmanager(
        contract_id: str,
        group: AlertGroup,
        creds: HTTPAuthorizationCredentials = Depends(bearer),  # noqa: B008
    ) -> dict[str, Any]:
        if not contracts.token_matches(by_id.get(contract_id), creds.credentials):
            raise HTTPException(401, "invalid token")  # also for an unknown Contract
        return await accept(contract_id, group)

    async def relay_alert(named: relay.Who, params: Any) -> tuple[int, Any]:
        """An alert group a Relay forwards (slice 3.4), with the token its Alertmanager
        sent: the Contract is the Relay's own, by its token, never anything it sends,
        and the alert's token is checked against it as the Contract's own route does."""
        contract_id = named[0]
        token = params.get("token") if isinstance(params, dict) else None
        if not isinstance(token, str) or not contracts.token_matches(by_id[contract_id], token):
            return 401, {"detail": "invalid token"}
        try:
            group = AlertGroup.model_validate(params.get("group"))
        except ValidationError:
            return 422, {"detail": "not an Alertmanager alert group"}
        return 202, await accept(contract_id, group)

    async def accept(contract_id: str, group: AlertGroup) -> dict[str, Any]:
        """An alert group for the Contract, from its route or its Relay."""
        if not group.firing:
            # Still investigating ones go stale when they pause (see run()).
            same_group = await store().mark_group_resolved(contract_id, group.groupKey)
            stale = [
                t.id
                for t in same_group
                if t.status == "awaiting_approval"
                and await reject_stale(
                    t.id, t.deadline, "The alert group resolved before a decision"
                )
            ]
            if stale:
                return {"status": "stale", "investigations": stale}
            return {"status": "ignored", "reason": "no firing alerts"}
        firing = {a.fingerprint for a in group.firing}
        started = now()
        seen: set[str] = set()
        # Two Contracts can send the same group key: the memory is per Contract.
        remembered = await store().alert_group(contract_id, group.groupKey)
        if remembered and started - remembered[0] <= DEDUP_SECONDS:
            _, previous, seen = remembered
            # Alertmanager re-sends on every repeat_interval and when an Alert resolves.
            # Only an Alert not seen before is new work.
            if firing <= seen:
                return {"status": "duplicate", "investigation": previous}
        contract = by_id[contract_id]
        window = 60 * (contract.related.window_minutes or settings.related_window_minutes)
        candidates = await store().open_triages(contract_id, started - window, group.groupKey)
        if match := related.find(group, candidates, contract.related):
            # Remembered under the Triage it joined: its re-sends are duplicates of it.
            t = match.investigation
            rid = await store().add_related(
                t.id, contract_id, group, match.matched, match.louder, started
            )
            await store().remember_group(contract_id, group.groupKey, started, t.id, seen | firing)
            log.info("alert group %s joined investigation %s", group.groupKey, t.id)
            if t.slack_channel and t.slack_ts and settings.slack_bot_token:
                severity = related.labels(group).get("severity")
                text = related_text(group.title, match.matched, severity, match.louder)
                blocks = related_blocks(text, t.id, rid) if buttons else None
                await db["slack"].post(t.slack_channel, text, t.slack_ts, blocks)
            return {"status": "related", "investigation": t.id}

        t = Investigation(
            id=str(uuid.uuid4()),
            contract_id=contract_id,
            title=group.title,
            group_key=group.groupKey,
            started=started,
            input=group.model_dump(mode="json"),
        )
        await store().insert(t)
        await store().remember_group(contract_id, group.groupKey, started, t.id, seen | firing)
        if reason := await over_budget(contract_id):
            # Recorded and remembered, so repeats are duplicates, not one refusal each.
            await refuse(t, reason)
            return {"status": "refused", "investigation": t.id, "reason": reason}
        spawn(run(t, lambda a: agent.start(a, group, t.id)))
        return {"status": "accepted", "investigation": t.id}

    @app.post("/contracts/{contract_id}/grafana", status_code=202)
    async def grafana(
        contract_id: str,
        payload: GrafanaAlertGroup,
        creds: HTTPAuthorizationCredentials = Depends(bearer),  # noqa: B008
    ) -> dict[str, Any]:
        """Grafana Alerting's webhook contact point, with the Contract's webhook token as
        its Bearer credentials: from here on the same path as Alertmanager's."""
        return await alertmanager(contract_id, payload.normalized(), creds)

    @app.post("/contracts/{contract_id}/pagerduty", status_code=202)
    async def pagerduty_webhook(contract_id: str, request: Request) -> dict[str, Any]:
        """PagerDuty's V3 webhook (slice 19.2), checked by its signature over the raw
        body: an incident starting or ending is an Alert group, from here on the same
        path as Alertmanager's. One whose key is the hash of a group this Contract sent
        itself is that group again: it joins its Triage. A signed delivery is never
        refused, whatever it holds: PagerDuty drops a refused one for good."""
        body = await request.body()
        contract = by_id.get(contract_id)
        pd = contract.pagerduty if contract else None
        signature = request.headers.get(pagerduty.SIGNATURE, "")
        if not (contract and pd) or not pagerduty.verified(
            contract.secret(pd.webhook_secret), body, signature
        ):
            raise HTTPException(401, "invalid signature")  # also for an unknown Contract
        found = pagerduty.incident(body)
        if found is None:
            return {"status": "ignored", "reason": "not an incident starting or ending"}
        if not found.firing:
            return await accept(contract_id, pagerduty.group(found, []))
        started = now()
        remembered = await store().alert_group(contract_id, found.group_key)
        if remembered and started - remembered[0] <= DEDUP_SECONDS:
            # before the API call: PagerDuty's retries and repeats cost none
            return {"status": "duplicate", "investigation": remembered[1]}
        listed: list[dict[str, Any]] = []
        if pd.api_key_secret:
            try:
                key = contract.secret(pd.api_key_secret)
                listed = await pagerduty.alerts(db["http"], key, found.id)
            except (pagerduty.Refused, pagerduty.Unknown) as e:  # the incident alone
                log.warning(
                    "%s: PagerDuty incident %s: alerts not read: %s", contract_id, found.id, e
                )
        wanted = pagerduty.keys(found, listed)
        for group_key, tid in await store().groups_since(contract_id, started - DEDUP_SECONDS):
            if group_key.startswith(pagerduty.PREFIX) or pagerduty.digest(group_key) not in wanted:
                continue
            # Remembered under the Triage it is: its re-sends are duplicates of it, and
            # its end resolves nothing (no Triage has its key).
            await store().remember_group(contract_id, found.group_key, started, tid, {found.id})
            log.info("PagerDuty incident %s is alert group of investigation %s", found.id, tid)
            t = await store().get(tid)
            if t and t.slack_channel and t.slack_ts and settings.slack_bot_token:
                number = f" #{found.number}" if found.number else ""
                link = f": {found.url}" if found.url else ""
                text = f":pager: PagerDuty incident{number} is this alert group{link}"
                await db["slack"].post(t.slack_channel, clean(text), t.slack_ts)
                if t.kind != "question":  # its button may be new: the note has somewhere to go
                    blocks = await parent(t)
                    await db["slack"].update(t.slack_channel, t.slack_ts, parent_text(t), blocks)
            return {"status": "joined", "investigation": tid}
        return await accept(contract_id, pagerduty.group(found, listed))

    @app.get("/investigations", dependencies=[Depends(approver_auth)])
    async def list_investigations() -> list[Investigation]:
        return await store().recent(50)

    @app.get("/investigations/{investigation_id}", dependencies=[Depends(approver_auth)])
    async def get_investigation(investigation_id: str) -> Investigation:
        t = await store().get(investigation_id)
        if t is None:
            raise HTTPException(404, "unknown investigation")
        return t

    @app.post(
        "/investigations/{investigation_id}/decision",
        status_code=202,
        dependencies=[Depends(approver_auth)],
    )
    async def decide(investigation_id: str, decision: Decision) -> dict[str, str]:
        t = await store().get(investigation_id)
        if t is None:
            raise HTTPException(404, "unknown investigation")
        if decision.approve:
            message, note = "", ":white_check_mark: Approved through the API."
        else:
            message = decision.message or "Rejected by the Operator. Do not retry."
            shown = f": {clean(decision.message, 2000)}" if decision.message else ""
            note = f":no_entry: Rejected through the API{shown}"
        try:
            decided = t.status == "awaiting_approval" and await settle(
                t, t.deadline, decision.approve, message, "operator", note, decision.message
            )
        except KillSwitchOn:
            raise HTTPException(409, KILLED) from None
        if not decided:  # already decided, stale, or never paused
            current = await store().get(t.id)
            status = current.status if current else t.status
            raise HTTPException(409, f"investigation is {status}, nothing to decide")
        return {"status": "resumed"}

    async def console_decide(
        investigation_id: str, deadline: float, approve: bool, reason: str, login: str
    ) -> bool:
        """A console decision on one pause (the deadline its page showed)."""
        t = await store().get(investigation_id)
        if t is None or t.status != "awaiting_approval":
            return False
        who = f"{clean(login, 200)} in the console"
        try:
            return await decide_as(t, deadline, approve, reason, f"console:{login}", who)
        except KillSwitchOn:
            raise HTTPException(409, KILLED) from None

    async def decide_as(
        t: Investigation, deadline: float, approve: bool, reason: str, by: str, who: str
    ) -> bool:
        """The Operator's decision from the console or the Hub MCP server; `who` is
        how the thread names them."""
        if approve:
            message, note = "", f":white_check_mark: Approved by {who}."
        else:
            message = reason.strip() or "Rejected by the Operator. Do not retry."
            shown = f": {clean(reason.strip(), 2000)}" if reason.strip() else ""
            note = f":no_entry: Rejected by {who}{shown}"
        return await settle(t, deadline, approve, message, by, note, reason.strip() or None)

    async def mcp_decide(
        t: Investigation, deadline: float, approve: bool, reason: str, login: str
    ) -> str | None:
        """A decision through the Hub MCP server on one pause: None when made, else
        why not (slice 11.2a)."""
        try:
            done = await decide_as(
                t, deadline, approve, reason, f"mcp:{login}", f"{clean(login, 200)} via MCP"
            )
        except KillSwitchOn:
            return KILLED
        return None if done else "Already decided or Stale: nothing to decide."

    async def turn_on_kill_switch(contract_id: str, by: str) -> bool:
        """False when it was already on."""
        on = await store().turn_on_kill_switch(contract_id, by, now())
        log.info("kill switch %s %s by %s", contract_id, "on" if on else "already on", by)
        return on

    async def propose_change(
        c: contracts.Contract, edits: list[proposals.Edit], title: str, why: str, login: str
    ) -> str:
        """The pull request's URL; proposals.Refused says why not."""
        if not (settings.contracts_repo and settings.contracts_token):
            raise proposals.Refused(
                "set SQUERO_CONTRACTS_REPO and SQUERO_CONTRACTS_TOKEN to propose changes"
            )
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(now()))
        branch = f"{proposals.BRANCHES}{c.id}-{stamp}"
        token = settings.contracts_token.get_secret_value()
        http = github_http or httpx.Client(timeout=30)
        try:
            url = await proposals.propose(
                http, settings.contracts_repo, token, loaded, c, edits, title, why, login, branch
            )
        finally:
            if http is not github_http:
                http.close()
        log.info("proposed change %s for %s by %s", url, c.id, login)
        return url

    async def console_feedback(
        t: Investigation, correct: bool, correction: str | None, login: str
    ) -> None:
        await give_feedback(t, correct, correction, f"console:{login}", clean(login, 200))

    async def console_decide_teaching(
        row: dict[str, Any], approve: bool, text: str | None, login: str
    ) -> bool:
        who = f"{clean(login, 200)} in the console"
        by = f"console:{login}"
        return (
            await decide_teaching(row["id"], row["contract_id"], approve, by, who, text) is not None
        )

    async def console_ask(contract_id: str, text: str, question_id: str | None) -> str | None:
        """A Question from the console, or a follow-up on question_id: the
        Investigation's id, or None when there's nothing to ask (still running, not
        a Question). The ask's time orders it like a Slack ts."""
        asked = f"{now():.6f}"
        if question_id is None:
            t = await start_question(contract_id, text, asked)
            return t.id if t else None
        t = await store().get(question_id)
        if t is None or not await follow_up(t, text, asked):  # the claim checks the kind
            return None
        return t.id

    async def health(c: contracts.Contract) -> list[dict[str, Any]]:
        """Each of a Contract's integrations, through its own tools: ok, how long, and
        on failure the error (redacted, capped). Never the data a check returns."""

        async def check(
            client: Client[Any], name: str, tool: str | None, args: Any
        ) -> dict[str, Any]:
            start, detail = time.monotonic(), ""
            try:
                if tool is None:  # a vendor: it starts, and offers every listed tool
                    offered = await asyncio.wait_for(client.list_tools(), PROBE_SECONDS)
                    missing = sorted(set(args["expect"]) - {t.name for t in offered})
                    detail = f"not offered: {', '.join(missing)}" if missing else ""
                else:
                    result = await asyncio.wait_for(
                        client.call_tool(tool, args, raise_on_error=False), PROBE_SECONDS
                    )
                    if result.is_error:
                        texts = (getattr(b, "text", "") for b in result.content)
                        detail = " ".join(texts) or "error"
            except Exception as e:  # a timeout, an unreachable cluster: it's the answer
                detail = f"{type(e).__name__}: {e}"
            ms = round(1000 * (time.monotonic() - start))
            return {"name": name, "ok": not detail, "ms": ms, "detail": redact_capped(detail, 300)}

        # Each Relay: connected since when, its version and Protocol, or offline since when
        # (3.3b, 18.3); each Actuator: when last seen, its version and Protocol (18.3).
        states: list[dict[str, Any]] = []
        for cl in c.clusters:
            if cl.via == "relay":
                ok, _, text = relays.state(c.id, cl.name)
                seen = protocol.SEEN.get((c.id, cl.name, "Relay"))
                if ok and seen and not seen.refused:
                    text += f", Protocol {seen.protocol}"
                elif seen and seen.refused:
                    text += f"; {protocol.refusal('Relay', cl.name, seen.protocol)}"
                states.append({"name": f"relay {cl.name}", "ok": ok, "ms": 0, "detail": text})
            if cl.actuator_token_secret:
                seen = protocol.SEEN.get((c.id, cl.name, "Actuator"))
                if seen is None:
                    ok, text = True, f"not seen since the Hub started at {utc(relays.started)}"
                else:
                    ok, text = not seen.refused, f"seen at {utc(seen.at)}"
                    text += f", version {seen.version}" if seen.version else ""
                    if seen.refused:
                        text += f"; {protocol.refusal('Actuator', cl.name, seen.protocol)}"
                    else:
                        text += f", Protocol {seen.protocol}"
                states.append({"name": f"actuator {cl.name}", "ok": ok, "ms": 0, "detail": text})
        if c.pagerduty and c.pagerduty.api_key_secret:  # slice 19.2: its key still works
            start = time.monotonic()
            detail = await pagerduty.reachable(db["http"], c.secret(c.pagerduty.api_key_secret))
            ms = round(1000 * (time.monotonic() - start))
            states.append({"name": "pagerduty", "ok": not detail, "ms": ms, "detail": detail})
        if c.webhook:  # slice 19.3: by its last delivery, no request made to say so
            ok, text = db["hooks"].health(c.id)
            states.append({"name": "webhook", "ok": ok, "ms": 0, "detail": text})
        for s in states:
            s["detail"] = redact_capped(s["detail"], 300)
        try:
            async with Client(mcp_factory(c).mcp) as client:
                probed = await asyncio.gather(*(check(client, *p) for p in agent.probes(c)))
                return [*states, *probed]
        except Exception as e:  # the tools couldn't even be built (a bad kubeconfig)
            detail = redact_capped(f"{type(e).__name__}: {e}", 300)
            return [*states, {"name": "tools", "ok": False, "ms": 0, "detail": detail}]

    async def console_file_jira(t: Investigation, login: str) -> tuple[str | None, str]:
        return await file_jira(t, f"console:{login}", login)

    async def console_file_servicenow(t: Investigation, login: str) -> tuple[str | None, str]:
        return await file_servicenow(t, f"console:{login}", login)

    if settings.console_operators:  # off by default: 404, not a login page
        app.include_router(console.redirects())  # the Jinja console's old addresses
        app.include_router(
            webapp.router(
                settings.console_operators,
                by_id,
                store,
                approver_token,
                now,
                console_decide,
                console_file_jira,
                jira_of,
                console_feedback,
                console_decide_teaching,
                health,
                shown_settings(settings),
                console_ask,
                file_servicenow=console_file_servicenow,  # by name: FileJira's type too
                servicenow_of=servicenow_of,
            )
        )

    if hub_mcp:  # the Hub MCP server: at /mcp/, only through tailscale serve
        app.mount("/mcp", hubmcp.guard(hub_mcp, mcp_token, settings.console_operators))

    app.state.relays = relays

    @app.websocket("/relay")
    async def relay_socket(ws: WebSocket) -> None:
        """A Relay dialing in (slice 3.1). Its token alone says which Contract's cluster
        it serves; refused before the socket opens otherwise. Never logged."""
        named = relay.who(ws.headers.get("authorization", ""), relay_tokens)
        if named is None:
            await ws.close(code=1008)  # before accept: the upgrade gets a 403
            return
        # slice 18.3: a Protocol it doesn't accept is a 426 carrying its own, after the token
        if protocol.see(*named, "Relay", ws.headers.get(protocol.HEADER), time.time()).refused:
            own = {protocol.HEADER: str(protocol.OWN["Relay"])}
            await ws.send_denial_response(Response(status_code=426, headers=own))
            return
        await ws.accept()

        async def receive() -> str | None:
            m = await ws.receive()
            if m["type"] == "websocket.disconnect":
                return None
            return m.get("text")  # None for a binary frame: that ends the connection

        await relays.hold(named, ws.send_text, receive, ws.close, relay_alert)

    async def actuator_of(  # async: SEEN written on the event loop, not a threadpool
        request: Request,
        response: Response,
        creds: HTTPAuthorizationCredentials = Depends(bearer),  # noqa: B008
    ) -> tuple[str, contracts.Cluster]:
        """The Contract and cluster an Actuator's token names (slice 12.1), or a 401; a 426
        for a Protocol the Hub doesn't accept (slice 18.3), its version and Protocol noted,
        and the Hub's own on every answer. A dependency, so both come before its body is
        read: a body a newer Protocol changed is a 426, never a 422 it would drop."""
        named = relay.who(f"Bearer {creds.credentials}", actuator_tokens)
        if named is None:
            raise HTTPException(401, "invalid token")
        cid, name = named
        own = {protocol.HEADER: str(protocol.OWN["Actuator"])}
        response.headers.update(own)
        said = request.headers.get(protocol.HEADER)
        version = request.headers.get(protocol.VERSION)
        if protocol.see(cid, name, "Actuator", said, time.time(), version).refused:
            raise HTTPException(426, f"this Hub speaks Protocol {own[protocol.HEADER]}", own)
        return cid, next(cl for cl in by_id[cid].clusters if cl.name == name)

    async def rollbacks(cid: str, cluster: str) -> dict[tuple[str, ...], list[float]]:
        """Each Workload's Rollbacks on this cluster in the last 7 days, oldest first."""
        at = time.time()
        rows = await store().actuations(cid, at - actuator.ROLLBACKS_FOR)
        return actuator.rollbacks_of(rows, cluster, at)

    telling = asyncio.Lock()  # one teller at a time: the route's and the scheduler's
    posted: dict[tuple[str, str], list[float]] = {}  # a cluster's Rollback posts, last hour
    summed: dict[tuple[str, str], float] = {}  # when its "N more" line was last posted
    resting: dict[str, tuple[float, float]] = {}  # a Contract Slack refused: until, how long
    capped: dict[tuple[str, ...], float] = {}  # when a container at its cap was last said

    def told(r: dict[str, Any]) -> bool:
        """Whether the Operator is told of this Actuation in Slack (slices 12.4, 13.1c)."""
        return actuator.to_tell(r["outcome"], r["reason"])

    async def tell(only: str | None = None, at: float | None = None) -> None:
        """Tell each Contract's Operator of its Rollbacks not told yet (slice 12.9c): from
        what is stored, after the Actuator was answered and again each tick, so one Slack
        didn't take is told late, never lost. One Contract's failure never stops another's."""
        async with telling:
            for cid in [only] if only else list(by_id):
                try:
                    await tell_contract(cid, at or time.time())  # at: for tests
                except Exception:
                    log.exception("telling %s of its rollbacks failed", cid)

    async def tell_contract(cid: str, at: float) -> None:
        if at < resting.get(cid, (0.0, 0.0))[0]:
            return  # Slack didn't take its last: left alone a while
        slack = by_id[cid].slack if settings.slack_bot_token else None
        # no channel to tell, or a week of Slack not taking it: given up
        await store().mark_all_told(cid, at if slack is None else at - TELL_FOR)
        counts = await store().untold_counts(cid, at)
        if not counts or slack is None:
            return

        async def say(text: str) -> bool:
            """Whether Slack took it; if not, this Contract rests: 5 minutes, then twice as
            long each time, an hour at most (a channel Slack refuses for good is asked a
            few times an hour, not at every tick)."""
            if await db["slack"].post(slack.operator_channel, text) is not None:
                resting.pop(cid, None)
                return True
            wait = min(2 * resting.get(cid, (0.0, REST / 2))[1], 3600.0)
            resting[cid] = (at + wait, wait)
            return False

        seen = await store().actuations(cid, at - actuator.ROLLBACKS_FOR)
        sized, _ = await store().latest_sizings(cid, set(counts))
        automated = {
            (r["cluster"], r["namespace"], r["kind"], r["name"])
            for r in sized
            if by_id[cid].sizing.automated(
                r["cluster"], r["namespace"], r["kind"], r["name"], r["container"]
            )
        }

        def order(r: dict[str, Any]) -> tuple[Any, ...]:
            return (r["at"], r["received_at"], r["container"], r["run_id"])

        for cluster, waiting in sorted(counts.items()):
            key = (cid, cluster)
            posts = posted[key] = [t for t in posted.get(key, []) if at - t < 3600]
            room = ROLLBACK_POSTS - len(posts)  # a pass reads no more than it may post
            rows = await store().untold(cid, cluster, at, room) if room > 0 else []
            for r in rows:
                w = (r["namespace"], r["kind"], r["name"])
                # what an Actuator sent, as text: its names and its reason (an API server's
                # message among them) never ping a channel or make a link
                where = f"{cluster}/{clean('/'.join(w))}"
                what = f"{where} container {clean(r['container'])}"
                text = f":rewind: {cid}: the Actuator rolled back {what}: {clean(r['reason'])}"
                if r["outcome"] == "stepped_up":  # slice 13.1c: what to change in the manifest
                    # a report without its limit is still said: never one that stops the
                    # Rollbacks behind it from being told
                    to = "raise its memory limit"
                    if r["memory"]:
                        to = f"set its memory limit to {actuator.memory_text(r['memory'])}"
                    text = (
                        f":arrow_up: {cid}: the Actuator stepped up {what}: "
                        f"{clean(r['reason'])}. It ends by itself once the need is gone; to "
                        f"keep it, {to} in its manifest."
                    )
                elif r["outcome"] == "burst":  # slice 13.3b, as a Step-up's
                    to = "raise its CPU limit"
                    if r["cpu"]:
                        to = f"set its CPU limit to {actuator.cpu_text(r['cpu'])}"
                    text = (
                        f":arrow_up: {cid}: the Actuator raised the CPU limit of {what}: "
                        f"{clean(r['reason'])}. It ends by itself once the need is gone; to "
                        f"keep it, {to} in its manifest."
                    )
                elif r["outcome"] in ("oom_killed", "throttled"):  # at its cap: once a day
                    one = (cid, cluster, *w, r["container"], r["outcome"])
                    if r["at"] - capped.get(one, -86400.0) < 86400:  # by the kills' own time
                        await store().mark_told(cid, [r])
                        continue
                    capped[one] = r["at"]
                    still = (
                        "still OOM-killed at its Step-up's cap"
                        if r["outcome"] == "oom_killed"
                        else "still throttled at its Burst's cap"
                    )
                    text = f":warning: {cid}: {what} is {still}: {clean(r['reason'])}"
                if at - r["received_at"] >= LATE:
                    when = datetime.fromtimestamp(r["received_at"], UTC)
                    text += f" (reported {when:%Y-%m-%d %H:%M} UTC)"
                mine = sorted(
                    (
                        x
                        for x in seen
                        if x["outcome"] == "rolled_back"
                        and (x["cluster"], x["namespace"], x["kind"], x["name"]) == (cluster, *w)
                    ),
                    key=order,
                )
                # the Rollback that ends its Workload's Automation (slice 12.5): its third
                # in 7 days, said with it, from what is stored, so a retry says it too
                third = sum(order(x) <= order(r) for x in mine) == actuator.ENDING
                if r["outcome"] == "rolled_back" and third and (cluster, *w) in automated:
                    ts = [x["at"] for x in mine]
                    until = datetime.fromtimestamp(actuator.resumes({w: ts})[w], UTC)
                    text += (
                        f"\n:octagonal_sign: {cid}: Automation of {where} ended: {len(ts)} "
                        f"rollbacks in 7 days. It resumes on {until:%Y-%m-%d %H:%M} UTC "
                        "unless more follow; remove `automate` from its rule to end it for good."
                    )
                if not await say(text):
                    return  # told at a later pass, the others after it
                posts.append(at)
                await store().mark_told(cid, [r])
                # slice 19.3: once Slack took it, so a refused post's retries send no
                # second event. The hourly "N more" line is Slack's alone.
                rolled = r["outcome"] == "rolled_back"
                told_of = {k: r[k] for k in ("namespace", "kind", "name", "container")}
                told_of |= {"cluster": cluster, "outcome": r["outcome"], "reason": r["reason"]}
                told_of |= {"actuated_at": r["at"]}  # the Actuator's own time
                # the limit Slack's line names, and no other: a Step-up's memory, a Burst's CPU
                limit = {"stepped_up": "memory", "burst": "cpu"}.get(r["outcome"])
                if limit and r[limit]:
                    told_of[limit] = r[limit]
                if rolled and third and (cluster, *w) in automated:
                    told_of["automation_ended"] = True
                emit(cid, "rollback" if rolled else "actuation", told_of)
            rest = waiting - len(rows)
            if rest <= 0 or at - summed.get(key, 0.0) < 3600:
                continue  # none over its hour's posts; or said within the hour: counted next
            text = (
                f":warning: {cid}: {rest} more Rollbacks or Step-ups reported by {cluster}'s "
                "Actuator "
                f"are not posted here ({ROLLBACK_POSTS} in the last hour already): see the "
                "Automation page."
            )
            if not await say(text):
                return
            summed[key] = at
            await store().mark_all_told(cid, at, cluster)

    async def automated(
        cid: str, cluster: contracts.Cluster
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """The containers under Automation on this cluster now, as their newest Sizings'
        rows, and that run (its `run` and `at`): of a Workload without 3 Rollbacks in 7 days,
        not reverted since their last approval."""
        c = by_id[cid]
        if not any(r.automate for r in c.sizing.rules):
            return [], {}
        rows, status = await store().latest_sizings(cid, {cluster.name})
        ended = actuator.resumes(await rollbacks(cid, cluster.name))
        reverted = await store().reverted(cid, cluster.name)  # until its next approval
        out = []
        for r in rows:
            where = (r["namespace"], r["kind"], r["name"])
            if (
                r["namespace"] in cluster.namespaces
                and where not in ended
                and (*where, r["container"]) not in reverted
                and c.sizing.automated(cluster.name, *where, r["container"])
            ):
                out.append(r)
        return out, status.get(cluster.name) or {}

    async def placement(cid: str, cluster: contracts.Cluster) -> actuator.Placing | None:
        """The cluster's Placement for its Actuator (slice 15.2a): the Workloads whose pods
        block a node for a reason set in their spec, by its last report and the last read
        of its nodes. None: off. No list (`workloads` None) without a usable report or
        read: the Actuator keeps the last it got."""
        if not (p := cluster.placement):
            return None
        names = {cluster.name}
        read = (await store().latest_rooms(cid, names)).get(cluster.name)
        report = (await store().latest_blockers(cid, names)).get(cluster.name)
        known = None
        if read and not read.get("problem") and not blockers.gap(report, cluster, time.time()):
            known = blockers.placing(blockers.verdicts(read, report, cluster, time.time()))
        return actuator.Placing(label=p.label, taint=p.taint, workloads=known)

    async def spotting(cid: str, cluster: contracts.Cluster) -> actuator.Spotting | None:
        """The cluster's spot nodes for its Actuator (slice 16.2a): the Workloads under a
        Spot ratio it steers, by the last read of its nodes and its last report (none that
        block a node), and the nodes carrying the label. None: the cluster names no spot
        label. No list without a usable report or read: the Actuator keeps the last."""
        if not (s := cluster.spot):
            return None
        names = {cluster.name}
        read = (await store().latest_rooms(cid, names)).get(cluster.name)
        report = (await store().latest_blockers(cid, names)).get(cluster.name)
        now = time.time()
        if not read or read.get("problem") or blockers.gap(report, cluster, now):
            return actuator.Spotting(label=s.label, taint=s.taint)
        blocking = sizing.blocking_of(read, report, cluster, now)
        found = sizing.spotted(read, by_id[cid], cluster.name, blocking)
        return actuator.Spotting(
            label=s.label,
            taint=s.taint,
            workloads=[actuator.Spotted(**w) for w in found],
            nodes=sizing.spot_nodes(read, s.label),
            cronjobs=sizing.spot_runs(read, by_id[cid], cluster.name, blocking),
        )

    # slice 15.3c: whether each cluster's Actuator applies types, as last stored: the store
    # is written only when a pull says otherwise
    knows: dict[tuple[str, str], bool] = {}

    def automation(
        rows: list[dict[str, Any]], run: int | None, typed: bool = False
    ) -> list[actuator.Wanted]:
        """The Sizings Automation applies (slice 12.5), of the `automated` containers:
        ready, changing something. `typed`: for an Actuator that applies a DaemonSet's
        per instance type (slice 15.3b), where some type's must change something; one
        with types goes by them only (slice 15.3c): none ready, nothing is sent."""
        out = []
        for r in rows:
            if r.get("state") != "ready":
                continue
            values = {
                res: r[res]["target"] if r.get(res) and r[res]["action"] != "keep" else None
                for res in ("cpu", "memory")
            }
            types = sizing.by_type(r) if typed else {}
            if typed and r.get("kind") == "DaemonSet" and r.get("groups") and not types:
                continue  # every type still learning: each pod keeps its template's
            if sizing.changes(values["cpu"], values["memory"], types):
                out.append(actuator.Wanted(run_id=run, **{**r, **values}, types=types))
        return out

    @app.get("/actuator/desired")
    async def actuator_desired(
        who: tuple[str, contracts.Cluster] = Depends(actuator_of),
        kinds: str = "Deployment",
        types: bool = False,
    ) -> actuator.Desired:
        """What an Actuator should apply now: its cluster's current Approved Sizings in the
        cluster's namespaces, Automation's for the containers no approval covers, the
        Reverts it hasn't done, and whether it may evict now (slice 12.7); nothing while
        the Kill switch is on; where the Contract sets `reflexes.oom`, the automated
        containers whose OOM kills to report (slice 13.1a); the automated containers
        whose rule sets a Startup boost (slice 13.4); and, where it sets `reflexes.burst`,
        what the burst Reflex goes by (slice 13.3a). Only of the `kinds` it names
        that the Hub's knows (slice
        12.8a): an Actuator that names none, 0.43.0 or older, reads every Sizing as a
        Deployment's, so it gets Deployments' only. To one that says it knows `types`
        (slice 15.3b), a DaemonSet's Sizing goes with each instance type's, and each
        node's type with them; any other gets the one Sizing for every pod."""
        cid, cluster = who
        known = set(kinds.split(",")) & set(actuator.KINDS)
        if knows.get((cid, cluster.name)) != types:  # slice 15.3c: what its money goes by
            await store().actuator_types(cid, cluster.name, types, time.time())
            knows[(cid, cluster.name)] = types
        if await store().kill_switch(cid) is not None:  # a read is no change: slice 15.1b
            return actuator.Desired(
                kill_switch=True, sizings=[], blockers=cluster.namespaces, constraints=True
            )
        reverted = await store().reverted(cid, cluster.name)  # an approval before it is undone
        approved = [
            actuator.Wanted.model_validate({**a, "types": of})
            for a in await store().approved_sizings(cid)
            if a["cluster"] == cluster.name
            and a["namespace"] in cluster.namespaces
            and a["kind"] in known
            and (a["namespace"], a["kind"], a["name"], a["container"]) not in reverted
            # one approved for its types only is nothing to an Actuator that knows none
            and sizing.changes(a["cpu"], a["memory"], of := (a["types"] or {}) if types else {})
        ]
        mine = {(w.namespace, w.kind, w.name, w.container) for w in approved}
        rows, read = await automated(cid, cluster)
        run = read.get("run")
        reflex = by_id[cid].actuator.reflexes.oom
        bursting = by_id[cid].actuator.reflexes.burst
        burst = bursting is not None
        age = None if read.get("at") is None else time.time() - read["at"]
        automatic = [
            w
            for w in automation(rows, run, types)
            if (w.namespace, w.kind, w.name, w.container) not in mine and w.kind in known
        ]
        # slice 15.3b: each node's type, from the last good read of them, to an Actuator that
        # knows types, whatever is sent with it: what it holds is caught up by them, on a
        # node that joined since and after its own restart
        node_types = None
        if types:
            found = await store().latest_rooms(cid, {cluster.name})
            node_types = found.get(cluster.name, {}).get("types")
        return actuator.Desired(
            kill_switch=False,
            node_types=node_types,
            placement=await placement(cid, cluster),
            spot=await spotting(cid, cluster),
            constraints=True,  # audit A3: and what its pods need of a node; A4: its own pod
            blockers=cluster.namespaces,  # slice 15.1b: where to report Blockers' facts of
            eviction=by_id[cid].actuator.evicting(datetime.now(UTC)),
            sizings=approved,
            automated=automatic,
            # slice 13.1a: with the Contract's `reflexes.oom`, the containers whose OOM
            # kills its Actuator reports, a Sizing ready or not
            # and each one's memory peak over its Sizing's window (slice 13.1c): a
            # Step-up is kept past its term while that is above its template's limit
            oom=[
                actuator.Watched(**r, peak=(r.get("memory") or {}).get("basis"))
                for r in rows
                if r["kind"] in known
            ]
            if reflex
            else [],
            oom_max=reflex.max if reflex else None,  # slice 13.1b: how far a Step-up may go
            oom_expires=reflex.expires if reflex else None,
            # slice 13.4: the Startup boost of each automated container's rule; never a
            # CronJob's run
            boosts=[
                actuator.Boost(**r, factor=factor)
                for r in rows
                if r["kind"] in known - {"CronJob"}
                and (
                    factor := by_id[cid].sizing.startup_boost(
                        cluster.name, r["namespace"], r["kind"], r["name"], r["container"]
                    )
                )
            ],
            # slice 13.3a: with the Contract's `reflexes.burst`, the automated containers
            # (never a CronJob's) with what the last hourly read says of each: throttled
            # enough for a Burst, and its CPU peak
            burst=[
                actuator.Throttled(**r, **sizing.bursting(r, age))
                for r in rows
                if r["kind"] in known - {"CronJob"}
            ]
            if burst
            else [],
            burst_run=run if burst else None,
            # slice 13.3b: how far a Burst may go, and its term
            burst_max=bursting.max if bursting else None,
            burst_expires=bursting.expires if bursting else None,
            reverts=[
                actuator.Undo.model_validate(r)
                for r in await store().pending_reverts(cid, cluster.name)
                if r["namespace"] in cluster.namespaces and r["kind"] in known
            ],
        )

    @app.post("/actuator/blockers", status_code=204)
    async def actuator_blockers(
        report: blockers.Report,
        who: tuple[str, contracts.Cluster] = Depends(actuator_of),
    ) -> None:
        """An Actuator's facts of the pods that may block a node's removal (slice 15.1b),
        sent when they change and hourly at least (audit A4), stored as its Contract's
        cluster's latest, by its token only. Refused whole: more pods than it may send, a
        namespace outside the cluster's (its own pod, `itself`, is outside them), a value
        no Actuator sends. Dated by the Hub's clock."""
        cid, cluster = who
        if len(report.pods) > blockers.MAX_PODS:
            raise HTTPException(413, f"at most {blockers.MAX_PODS} pods in a report")
        named = {p.namespace for p in report.pods} | set(report.namespaces)
        named |= {key.partition("/")[0] for key in report.needed}  # audit A3
        outside = sorted(named - set(cluster.namespaces))
        if outside:
            raise HTTPException(422, f"namespaces not in cluster {cluster.name!r}: {outside}")
        unreal = blockers.unreal(report)
        if unreal:
            raise HTTPException(422, f"not what an Actuator sends: {unreal}")
        await store().save_blockers(cid, cluster.name, time.time(), report.model_dump())
        log.info(
            "blockers %s/%s: %d pods in %d namespaces%s",
            cid,
            cluster.name,
            len(report.pods),
            len(report.namespaces),
            ", cut short" if report.truncated else "",
        )

    @app.post("/actuator/reports", status_code=204)
    async def actuator_reports(
        reports: list[actuator.Actuation],
        who: tuple[str, contracts.Cluster] = Depends(actuator_of),
    ) -> None:
        """An Actuator's Actuations, stored as its Contract's cluster's, by its token only;
        one naming a namespace outside the cluster's or a kind it doesn't resize (audit A7)
        is refused whole, as is one with a number that isn't one (slice 12.9c). Its
        Rollbacks are told after it is answered. Audit A7: at most `HOURLY_REPORTS` stored
        from the cluster an hour, the rest refused with 429, for the Actuator to send again."""
        cid, cluster = who
        if len(reports) > actuator.MAX_REPORTS:
            raise HTTPException(413, f"at most {actuator.MAX_REPORTS} reports at once")
        outside = sorted({r.namespace for r in reports} - set(cluster.namespaces))
        if outside:
            raise HTTPException(422, f"namespaces not in cluster {cluster.name!r}: {outside}")
        strange = sorted({r.kind for r in reports} - set(actuator.KINDS))
        if strange:
            raise HTTPException(422, f"kinds an Actuator doesn't resize: {strange}")
        unreal = sorted({f for f in map(actuator.unreal, reports) if f})
        if unreal:  # said without the value: a NaN can't be said in JSON
            raise HTTPException(422, f"not a number an Actuator sends: {unreal}")
        rows = [r.model_dump() for r in reports]
        new, refused = await store().record_actuations(
            cid, cluster.name, rows, time.time(), told, actuator.HOURLY_REPORTS
        )
        if any(told(n) for n in new):
            spawn(tell(cid))  # not in its request: Slack may be slow, or down
        for r in (r for r, d in zip(reports, rows, strict=True) if d not in refused):
            log.info(
                "actuation %s/%s %s %s/%s/%s/%s: %s",
                cid,
                cluster.name,
                r.outcome,
                r.namespace,
                r.kind,
                r.name,
                r.container,
                redact(r.reason),
            )
        if refused:
            log.warning(
                "actuator %s/%s: %d reports refused, its %d an hour reached",
                cid,
                cluster.name,
                len(refused),
                actuator.HOURLY_REPORTS,
            )
            raise HTTPException(
                429, f"at most {actuator.HOURLY_REPORTS} reports an hour from a cluster"
            )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/metrics")
    async def metrics(request: Request) -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app
