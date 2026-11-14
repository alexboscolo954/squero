"""Postgres state for the Hub: Investigations, the dedup memory, Approval decisions.

One pool serves this store and the LangGraph checkpointer, with the connection
settings the checkpointer needs (autocommit, no prepared statements, dict rows).
Migrations are numbered .sql files in migrations/, applied once each at startup.
"""

import json
import re
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from psycopg import AsyncConnection, Rollback
from psycopg.errors import UniqueViolation
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from squero.models import AlertGroup, Investigation
from squero.usage import NEEDS_FOR

Pool = AsyncConnectionPool[AsyncConnection[DictRow]]
MIGRATIONS = Path(__file__).parent / "migrations"
MIGRATION_LOCK = 4_242_001  # pg_advisory_xact_lock key: one Hub migrates at a time
SIZING_LOCK = 4_242_002  # with the Contract's hash: one Sizing run claimed at a time
HOURLY_FOR = 14 * 86400  # Sizing runs kept hourly this long, then the last of each day
DAILY_FOR = 400 * 86400  # and those this long
ACTUATIONS_FOR = 90 * 86400  # an Actuator's audit trail (slice 12.1)
# An Actuation as read (slice 12.9c): `at`, the Actuator's own clock, never later than the
# Hub's when it arrived. A report dated in the future would otherwise stay a container's
# last Actuation, count as a Rollback and outlive its 90 days for as long as it claims; the
# table's own `at` only tells a report from one sent again.
ACTUATION = (
    "contract_id, cluster, namespace, kind, name, container, run_id, outcome, cpu, memory, "
    "pods, reason, least(at, received_at) as at, received_at"
)
# An Actuation's columns after contract_id and cluster, in order, up to received_at
ACTUATION_FIELDS = (
    "namespace",
    "kind",
    "name",
    "container",
    "run_id",
    "outcome",
    "cpu",
    "memory",
    "pods",
    "reason",
    "at",
)
COLUMNS = (
    "id, kind, contract_id, title, group_key, group_resolved, status, started, report, "
    "pending, executed_actions, error, deadline, slack_channel, slack_ts, client_sent_at, "
    "client_sent_by, answer, last_mention_ts, input, jira_issue, jira_by, jira_at, check_result"
)


def now() -> float:
    """The one clock for stored times and comparisons."""
    return time.time()


@asynccontextmanager
async def connect(url: str) -> AsyncIterator[Pool]:
    pool: Pool = AsyncConnectionPool(
        url,
        connection_class=AsyncConnection[DictRow],
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        open=False,
    )
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


def month_start(at: float) -> float:
    """The start of `at`'s calendar month, UTC: budgets are monthly."""
    day = datetime.fromtimestamp(at, UTC)
    return day.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()


def _investigation(row: dict[str, Any]) -> Investigation:
    return Investigation.model_validate(row)


INSERT = (
    f"insert into investigations ({COLUMNS}) values "  # noqa: S608 - fixed columns
    f"({', '.join(['%s'] * len(COLUMNS.split(',')))})"
)


def _row(t: Investigation) -> tuple[Any, ...]:
    """The values for INSERT, in COLUMNS order."""
    return (
        t.id,
        t.kind,
        t.contract_id,
        t.title,
        t.group_key,
        t.group_resolved,
        t.status,
        t.started,
        Jsonb(t.report.model_dump(mode="json")) if t.report else None,
        Jsonb(t.pending),
        Jsonb(t.executed_actions),
        t.error,
        t.deadline,
        t.slack_channel,
        t.slack_ts,
        t.client_sent_at,
        t.client_sent_by,
        Jsonb(t.answer.model_dump(mode="json")) if t.answer else None,
        t.last_mention_ts,
        Jsonb(t.input) if t.input is not None else None,
        t.jira_issue,
        t.jira_by,
        t.jira_at,
        _json(t.check_result),
    )


def _json(m: BaseModel | None) -> Jsonb | None:
    return Jsonb(m.model_dump(mode="json")) if m else None


class Store:
    def __init__(self, pool: Pool) -> None:
        self.pool = pool

    async def migrate(self) -> None:
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute("select pg_advisory_xact_lock(%s)", (MIGRATION_LOCK,))
            await conn.execute(
                "create table if not exists schema_migrations "
                "(version text primary key, applied double precision not null)"
            )
            rows = await (await conn.execute("select version from schema_migrations")).fetchall()
            done = {r["version"] for r in rows}
            for path in sorted(MIGRATIONS.glob("*.sql")):
                if path.stem in done:
                    continue
                # Unprepared: a migration file holds several statements.
                await conn.execute(path.read_text().encode(), prepare=False)
                await conn.execute(
                    "insert into schema_migrations values (%s, %s)", (path.stem, now())
                )

    async def _one(self, sql: str, args: tuple[Any, ...]) -> dict[str, Any] | None:
        async with self.pool.connection() as conn:
            row: dict[str, Any] | None = await (await conn.execute(sql, args)).fetchone()
            return row

    async def _exec(self, sql: str, args: tuple[Any, ...]) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(sql, args)

    async def _all(
        self, sql: str, args: tuple[Any, ...] | dict[str, Any] = ()
    ) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            rows: list[dict[str, Any]] = await (await conn.execute(sql, args)).fetchall()
            return rows

    async def insert(self, t: Investigation) -> None:
        await self._exec(INSERT, _row(t))

    async def set_slack_thread(self, investigation_id: str, channel: str, ts: str) -> bool:
        """Only while it has none: a thread, once started, is never replaced."""
        row = await self._one(
            "update investigations set slack_channel = %s, slack_ts = %s "
            "where id = %s and slack_ts is null returning id",
            (channel, ts, investigation_id),
        )
        return row is not None

    async def claim_client_report(self, investigation_id: str, by: str) -> float | None:
        """Take the one send of a finished Report: its time, or None when it was
        already sent (or isn't finished)."""
        at = now()
        row = await self._one(
            "update investigations set client_sent_at = %s, client_sent_by = %s "
            "where id = %s and status = 'done' and report is not null "
            "and client_sent_at is null returning id",
            (at, by, investigation_id),
        )
        return at if row else None

    async def release_client_report(self, investigation_id: str, at: float) -> None:
        """The post failed: free the send for a retry, unless it was claimed again."""
        await self._exec(
            "update investigations set client_sent_at = null, client_sent_by = null "
            "where id = %s and client_sent_at = %s",
            (investigation_id, at),
        )

    async def claim_jira(self, investigation_id: str, by: str) -> float | None:
        """Take the one Jira issue of a finished Report: its time, or None when it
        was already claimed (filed, being filed, or unknown) or isn't finished."""
        at = now()
        row = await self._one(
            "update investigations set jira_at = %s, jira_by = %s "
            "where id = %s and status = 'done' and report is not null "
            "and jira_at is null returning id",
            (at, by, investigation_id),
        )
        return at if row else None

    async def set_jira_issue(self, investigation_id: str, at: float, key: str) -> None:
        await self._exec(
            "update investigations set jira_issue = %s where id = %s and jira_at = %s",
            (key, investigation_id, at),
        )

    async def release_jira(self, investigation_id: str, at: float) -> None:
        """Jira refused it: free the claim for a retry, unless it was claimed again."""
        await self._exec(
            "update investigations set jira_at = null, jira_by = null "
            "where id = %s and jira_at = %s and jira_issue is null",
            (investigation_id, at),
        )

    async def insert_question(self, t: Investigation) -> bool:
        """False when its thread already has an Investigation (Slack redelivered
        the mention, or the thread is taken)."""
        try:
            await self.insert(t)
        except UniqueViolation:
            return False
        return True

    async def by_thread(self, channel: str, ts: str) -> Investigation | None:
        row = await self._one(
            f"select {COLUMNS} from investigations "  # noqa: S608 - fixed columns
            "where slack_channel = %s and slack_ts = %s",
            (channel, ts),
        )
        return _investigation(row) if row else None

    async def claim_follow_up(self, investigation_id: str, mention_ts: str, question: str) -> bool:
        """One Run at a time per Question, and each mention at most once: only a
        finished Question, and only with a mention newer than the last one. The claim
        records the turn with the Answer it replaces, in the same statement."""
        row = await self._one(
            "with claimed as (update investigations set status = 'running', error = null, "
            "last_mention_ts = %s where id = %s and kind = 'question' "
            "and status in ('done', 'failed') "
            "and (last_mention_ts is null or last_mention_ts < %s) "
            "returning id, contract_id, answer) "
            "insert into question_turns (investigation_id, contract_id, question, answer, at) "
            "select id, contract_id, %s, answer, %s from claimed returning investigation_id",
            (mention_ts, investigation_id, mention_ts, question, now()),
        )
        return row is not None

    async def question_turns(self, investigation_id: str) -> list[dict[str, Any]]:
        """A Question's follow-ups, oldest first, each with the Answer it replaced."""
        return await self._all(
            "select question, answer, at from question_turns "
            "where investigation_id = %s order by id",
            (investigation_id,),
        )

    async def save(self, t: Investigation) -> None:
        """Everything a Run changes. Never group_resolved (only the resolve path sets
        it, so a Run that finishes after a resolve can't clear it), nor the Slack
        thread (set_slack_thread), nor the Client report's send, nor the last
        mention (claim_follow_up)."""
        await self._exec(
            "update investigations set status = %s, report = %s, pending = %s, "
            "executed_actions = %s, error = %s, deadline = %s, answer = %s, "
            "check_result = %s where id = %s",
            (
                t.status,
                Jsonb(t.report.model_dump(mode="json")) if t.report else None,
                Jsonb(t.pending),
                Jsonb(t.executed_actions),
                t.error,
                t.deadline,
                Jsonb(t.answer.model_dump(mode="json")) if t.answer else None,
                _json(t.check_result),
                t.id,
            ),
        )

    async def get(self, investigation_id: str) -> Investigation | None:
        row = await self._one(
            f"select {COLUMNS} from investigations where id = %s",  # noqa: S608
            (investigation_id,),
        )
        return _investigation(row) if row else None

    async def recent(self, limit: int = 50, contract_id: str | None = None) -> list[Investigation]:
        """Newest first; one Contract's only when contract_id is given. Passing Checks
        are left out: they are quiet (the Checks table shows each one's last Run)."""
        rows = await self._all(
            f"select {COLUMNS} from investigations "  # noqa: S608 - fixed columns
            "where (%s::text is null or contract_id = %s) "
            "and (check_result is null or not (check_result->>'passed')::boolean) "
            "order by started desc limit %s",
            (contract_id, contract_id, limit),
        )
        return [_investigation(r) for r in rows]

    async def last_done(self) -> dict[str, Investigation]:
        """Each Contract's latest Investigation that finished with a Report or Answer."""
        rows = await self._all(
            f"select distinct on (contract_id) {COLUMNS} from investigations "  # noqa: S608
            "where status = 'done' order by contract_id, started desc"
        )
        return {r["contract_id"]: _investigation(r) for r in rows}

    async def tool_calls(self, investigation_id: str) -> list[dict[str, Any]]:
        return await self._all(
            "select tool, args, result, is_error, at from tool_calls "
            "where investigation_id = %s order by id",
            (investigation_id,),
        )

    async def decisions(self, investigation_id: str) -> list[dict[str, Any]]:
        return await self._all(
            "select approve, actions, message, decided_by, at from decisions "
            "where investigation_id = %s order by id",
            (investigation_id,),
        )

    async def unfinished(self) -> list[Investigation]:
        rows = await self._all(
            f"select {COLUMNS} from investigations "  # noqa: S608
            "where status in ('running', 'awaiting_approval') order by started"
        )
        return [_investigation(r) for r in rows]

    async def pending_count(self) -> int:
        row = await self._one(
            "select coalesce(sum(jsonb_array_length(pending)), 0) as n from investigations "
            "where status = 'awaiting_approval'",
            (),
        )
        return int(row["n"]) if row else 0

    async def mark_group_resolved(self, contract_id: str, group_key: str) -> list[Investigation]:
        rows = await self._all(
            "update investigations set group_resolved = true "  # noqa: S608 - fixed columns
            f"where contract_id = %s and group_key = %s returning {COLUMNS}",
            (contract_id, group_key),
        )
        return [_investigation(r) for r in rows]

    async def claim(
        self, investigation_id: str, deadline: float | None
    ) -> list[dict[str, Any]] | None:
        """Take a pause's Proposed actions for one decision. Atomic: of an approval, a
        rejection, a timeout and a resolve racing for the same pause, exactly one
        gets the actions; the others get None."""
        row = await self._one(
            "update investigations set status = 'running', pending = '[]', deadline = null "
            "where id = %s and status = 'awaiting_approval' and deadline = %s "
            "returning (select pending from investigations where id = %s) as pending",
            (investigation_id, deadline, investigation_id),
        )
        return row["pending"] if row else None

    async def kill_switch(self, contract_id: str) -> dict[str, Any] | None:
        """The Contract's Kill switch while it is on (who, when), else None."""
        return await self._one(
            "select set_by, set_at from kill_switches where contract_id = %s", (contract_id,)
        )

    async def turn_on_kill_switch(self, contract_id: str, by: str, at: float) -> bool:
        """False when it was already on (the first who and when stay)."""
        row = await self._one(
            "insert into kill_switches (contract_id, set_by, set_at) values (%s, %s, %s) "
            "on conflict (contract_id) do nothing returning contract_id",
            (contract_id, by, at),
        )
        return row is not None

    async def turn_off_kill_switch(self, contract_id: str) -> bool:
        """False when it wasn't on."""
        row = await self._one(
            "delete from kill_switches where contract_id = %s returning contract_id",
            (contract_id,),
        )
        return row is not None

    async def record_decision(
        self,
        investigation_id: str,
        approve: bool,
        actions: list[dict[str, Any]],
        message: str,
        decided_by: str,
    ) -> None:
        await self._exec(
            "insert into decisions (investigation_id, approve, actions, message, decided_by, at) "
            "values (%s, %s, %s, %s, %s, %s)",
            (investigation_id, approve, Jsonb(actions), message, decided_by, now()),
        )

    async def last_decision(self, investigation_id: str) -> dict[str, Any] | None:
        return await self._one(
            "select approve, actions, message, decided_by, at from decisions "
            "where investigation_id = %s order by id desc limit 1",
            (investigation_id,),
        )

    async def record_tool_call(
        self,
        investigation_id: str,
        contract_id: str,
        tool: str,
        args: dict[str, Any],
        result: str,
        is_error: bool,
    ) -> None:
        await self._exec(
            "insert into tool_calls (investigation_id, contract_id, tool, args, result, "
            "is_error, at) values (%s, %s, %s, %s, %s, %s, %s)",
            (investigation_id, contract_id, tool, Jsonb(args), result, is_error, now()),
        )

    async def record_usage(
        self,
        investigation_id: str | None,  # None: a search's embedding, the Contract's only
        contract_id: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float | None,
    ) -> None:
        await self._exec(
            "insert into usage (investigation_id, contract_id, model, input_tokens, "
            "output_tokens, cost_usd, at) values (%s, %s, %s, %s, %s, %s, %s)",
            (investigation_id, contract_id, model, input_tokens, output_tokens, cost_usd, now()),
        )

    async def month_spend(self, contract_id: str, at: float) -> float:
        """One Contract's cost so far in `at`'s month."""
        row = await self._one(
            "select coalesce(sum(cost_usd), 0) as spent from usage "
            "where contract_id = %s and at >= %s",
            (contract_id, month_start(at)),
        )
        return float(row["spent"]) if row else 0.0

    async def notice_once(self, contract_id: str, at: float, level: int) -> bool:
        """True only for the first caller this month at this level."""
        row = await self._one(
            "insert into budget_notices values (%s, %s, %s, %s) "
            "on conflict do nothing returning level",
            (contract_id, month_start(at), level, at),
        )
        return row is not None

    async def investigation_usage(self, investigation_id: str) -> dict[str, Any]:
        """Tokens, and cost (None when no call was priced)."""
        row = await self._one(
            "select coalesce(sum(input_tokens), 0) as input_tokens, "
            "coalesce(sum(output_tokens), 0) as output_tokens, sum(cost_usd) as cost_usd "
            "from usage where investigation_id = %s",
            (investigation_id,),
        )
        return row or {"input_tokens": 0, "output_tokens": 0, "cost_usd": None}

    async def record_masked(
        self, investigation_id: str, contract_id: str, placeholders: list[str]
    ) -> None:
        """Placeholders one model call sent (masking.py), each kept once."""
        await self._exec(
            "insert into masked (investigation_id, contract_id, placeholder) "
            "select %s, %s, unnest(%s::text[]) on conflict do nothing",
            (investigation_id, contract_id, placeholders),
        )

    async def masked_counts(self, investigation_id: str) -> dict[str, int]:
        """How many distinct values of each kind were masked for the LLM."""
        rows = await self._all(
            "select split_part(trim(leading '<' from placeholder), '-', 1) as kind, "
            "count(*) as n from masked where investigation_id = %s group by 1 order by 1",
            (investigation_id,),
        )
        return {r["kind"]: r["n"] for r in rows}

    async def claim_sizing_run(self, contract_id: str, at: float, every: float) -> int | None:
        """A new Sizing run's id, or None when one started less than `every` seconds
        before `at`: an hourly run fires once however many ticks or restarts."""
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute(
                "select pg_advisory_xact_lock(%s, hashtext(%s))", (SIZING_LOCK, contract_id)
            )
            recent = await (
                await conn.execute(
                    "select 1 from sizing_runs where contract_id = %s and at > %s",
                    (contract_id, at - every),
                )
            ).fetchone()
            if recent:
                return None
            row = await (
                await conn.execute(
                    "insert into sizing_runs (contract_id, at) values (%s, %s) returning id",
                    (contract_id, at),
                )
            ).fetchone()
            assert row is not None
            return int(row["id"])

    async def finish_sizing_run(
        self,
        run_id: int,
        contract_id: str,
        clusters: dict[str, str | None],
        rows: list[dict[str, Any]],
    ) -> None:
        """Store a run's rows (sizing.rows()) and each cluster's status, together."""
        async with self.pool.connection() as conn, conn.transaction():
            for r in rows:
                await conn.execute(
                    "insert into sizings values (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        run_id,
                        contract_id,
                        r["cluster"],
                        r["namespace"],
                        r["kind"],
                        r["name"],
                        r["container"],
                        Jsonb(r),
                    ),
                )
            await conn.execute(
                "update sizing_runs set clusters = %s where id = %s and contract_id = %s",
                (Jsonb(clusters), run_id, contract_id),
            )

    async def save_needs(
        self,
        contract_id: str,
        cluster: str,
        found: dict[tuple[str, str, str], dict[float, float]],
    ) -> None:
        """A cluster's Needs as a run read them (slice 14.1a): per Workload (namespace,
        kind, name), each UTC hour's peak. An hour read again keeps the larger: the hour
        under way grows, and a coarser read never lowers a finer one."""
        async with self.pool.connection() as conn, conn.transaction():
            for workload, hours in found.items():
                for hour, need in hours.items():
                    await conn.execute(
                        "insert into needs values (%s, %s, %s, %s, %s, %s, %s) on conflict "
                        "(contract_id, cluster, namespace, kind, name, hour) do update "
                        "set need = greatest(needs.need, excluded.need)",
                        (contract_id, cluster, *workload, hour, need),
                    )

    async def needs_since(self, contract_id: str) -> dict[str, float]:
        """Per cluster, the newest hour a Need is stored for: where its next read starts."""
        rows = await self._all(
            "select cluster, max(hour) as hour from needs where contract_id = %s group by cluster",
            (contract_id,),
        )
        return {r["cluster"]: r["hour"] for r in rows}

    async def needs(
        self, contract_id: str, clusters: set[str], since: float = 0.0
    ) -> list[dict[str, Any]]:
        """The Contract's stored Needs of `clusters` (its clusters now) from `since`:
        cluster, namespace, kind, name, hour, need, oldest first."""
        return await self._all(
            "select cluster, namespace, kind, name, hour, need from needs where "
            "contract_id = %s and cluster = any(%s) and hour >= %s "
            "order by hour, cluster, namespace, kind, name",
            (contract_id, sorted(clusters), since),
        )

    async def save_floors(
        self, contract_id: str, cluster: str, at: float, floors: list[dict[str, Any]]
    ) -> None:
        """A cluster's Floors as the latest run computed them (slice 14.1b), replacing the
        last: an HPA gone since takes its Floor with it."""
        await self._exec(
            "insert into floors (contract_id, cluster, at, floors) values (%s, %s, %s, %s) "
            "on conflict (contract_id, cluster) do update set at = excluded.at, "
            "floors = excluded.floors",
            (contract_id, cluster, at, Jsonb(floors)),
        )

    async def latest_floors(self, contract_id: str, clusters: set[str]) -> list[dict[str, Any]]:
        """The Contract's Floors of `clusters` (its clusters now), each with the time of
        the run that computed it (`at`), by Workload."""
        rows = await self._all(
            "select at, floors from floors where contract_id = %s and cluster = any(%s)",
            (contract_id, sorted(clusters)),
        )
        found = [{**f, "at": r["at"]} for r in rows for f in r["floors"]]
        return sorted(found, key=lambda f: f["workload"])

    async def save_nodes(
        self, contract_id: str, cluster: str, at: float, facts: dict[str, Any]
    ) -> None:
        """A cluster's nodes as the latest run read them (slice 10.4), replacing the last."""
        await self._exec(
            "insert into cluster_nodes (contract_id, cluster, at, facts) values (%s, %s, %s, %s) "
            "on conflict (contract_id, cluster) do update set at = excluded.at, "
            "facts = excluded.facts",
            (contract_id, cluster, at, Jsonb(facts)),
        )

    async def save_rooms(
        self, contract_id: str, cluster: str, at: float, rooms: dict[str, Any]
    ) -> None:
        """A cluster's nodes and what their Room is made of, as the latest run read them
        (slice 15.1a), replacing the last."""
        await self._exec(
            "insert into cluster_rooms (contract_id, cluster, at, rooms) values (%s, %s, %s, %s) "
            "on conflict (contract_id, cluster) do update set at = excluded.at, "
            "rooms = excluded.rooms",
            (contract_id, cluster, at, Jsonb(rooms)),
        )

    async def latest_rooms(self, contract_id: str, clusters: set[str]) -> dict[str, dict[str, Any]]:
        """Each of the Contract's `clusters` (its clusters now) with its latest read of
        its nodes: `at`, `nodes`, `problem`."""
        rows = await self._all(
            "select cluster, at, rooms from cluster_rooms "
            "where contract_id = %s and cluster = any(%s)",
            (contract_id, sorted(clusters)),
        )
        return {r["cluster"]: {"at": r["at"], **r["rooms"]} for r in rows}

    async def latest_nodes(self, contract_id: str) -> dict[str, dict[str, Any]]:
        """Each cluster's latest node facts: `at`, `nodes`, `problem`."""
        rows = await self._all(
            "select cluster, at, facts from cluster_nodes where contract_id = %s", (contract_id,)
        )
        return {r["cluster"]: {"at": r["at"], **r["facts"]} for r in rows}

    async def list_prices(
        self, currency: str = "USD"
    ) -> dict[tuple[str, str, str], dict[str, Any]]:
        """Every list price looked up in `currency`, by (provider, region, instance type)."""
        rows = await self._all("select * from list_prices where currency = %s", (currency,))
        return {(r["provider"], r["region"], r["instance_type"]): r for r in rows}

    async def put_list_price(
        self,
        key: tuple[str, str, str],
        hour_price: float | None,
        error: str | None,
        at: float,
        currency: str = "USD",
    ) -> None:
        """A lookup's outcome: a price replaces the last good one; a failure keeps it
        (and its age) and records why."""
        await self._exec(
            "insert into list_prices (currency, provider, region, instance_type, hour_price, "
            "ok_at, tried_at, error) values (%s, %s, %s, %s, %s, %s, %s, %s) "
            "on conflict (currency, provider, region, instance_type) do update set "
            "hour_price = coalesce(excluded.hour_price, list_prices.hour_price), "
            "ok_at = case when excluded.hour_price is null then list_prices.ok_at "
            "else excluded.ok_at end, tried_at = excluded.tried_at, error = excluded.error",
            (currency, *key, hour_price, None if hour_price is None else at, at, error),
        )

    async def save_bill(
        self,
        contract_id: str,
        cluster: str,
        days: dict[str, tuple[float, str, bool]],
        at: float,
    ) -> None:
        """A cluster's billed days (slice 10.5a) as (amount, currency, still estimated),
        replacing what the Hub had for them, and the read's success."""
        for day, (amount, currency, estimated) in days.items():
            await self._exec(
                "insert into billed_costs (contract_id, cluster, day, amount, currency, "
                "estimated) values (%s, %s, %s, %s, %s, %s) "
                "on conflict (contract_id, cluster, day) do update set "
                "amount = excluded.amount, currency = excluded.currency, "
                "estimated = excluded.estimated",
                (contract_id, cluster, day, amount, currency, estimated),
            )
        await self._exec(
            "insert into billing_tries (contract_id, cluster, tried_at, ok_at, error) "
            "values (%s, %s, %s, %s, null) on conflict (contract_id, cluster) do update "
            "set tried_at = excluded.tried_at, ok_at = excluded.ok_at, error = null",
            (contract_id, cluster, at, at),
        )

    async def fail_bill(self, contract_id: str, cluster: str, error: str, at: float) -> None:
        """A read that failed: why, keeping the last good read's time."""
        await self._exec(
            "insert into billing_tries (contract_id, cluster, tried_at, error) "
            "values (%s, %s, %s, %s) on conflict (contract_id, cluster) do update "
            "set tried_at = excluded.tried_at, error = excluded.error",
            (contract_id, cluster, at, error),
        )

    async def bills(
        self, contract_id: str
    ) -> tuple[dict[str, dict[str, dict[str, Any]]], dict[str, dict[str, Any]]]:
        """Per cluster its billed days (day -> amount, currency, estimated), and per
        cluster its last read (`tried_at`, `ok_at`, `error`)."""
        days: dict[str, dict[str, dict[str, Any]]] = {}
        for r in await self._all(
            "select cluster, day, amount, currency, estimated from billed_costs "
            "where contract_id = %s",
            (contract_id,),
        ):
            days.setdefault(r["cluster"], {})[r["day"]] = r
        tries = {
            r["cluster"]: r
            for r in await self._all(
                "select cluster, tried_at, ok_at, error from billing_tries where contract_id = %s",
                (contract_id,),
            )
        }
        return days, tries

    async def prune_sizings(self, contract_id: str, at: float) -> None:
        """Keep a Contract's runs hourly for HOURLY_FOR, then its last finished run of
        each UTC day for DAILY_FOR; their rows go with them."""
        await self._exec(
            "delete from sizing_runs where contract_id = %s and (at < %s or (at < %s and "
            "id not in (select max(id) from sizing_runs where contract_id = %s "
            "and clusters <> '{}' group by floor(at / 86400))))",
            (contract_id, at - DAILY_FOR, at - HOURLY_FOR, contract_id),
        )
        await self._exec(
            "delete from actuations where contract_id = %s and least(at, received_at) < %s",
            (contract_id, at - ACTUATIONS_FOR),
        )
        await self._exec(
            "delete from needs where contract_id = %s and hour < %s",
            (contract_id, at - NEEDS_FOR),
        )

    async def latest_sizings(
        self, contract_id: str, clusters: set[str], before: float | None = None
    ) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
        """Each of `clusters`' rows (the Contract's clusters now: one removed from it is
        gone) from its latest run that read it, and per cluster `at` (that run's time,
        None if never read) and `error` with `since` when the runs after it failed: one
        failing cluster never blanks another. before: as of then (runs at or before it)."""
        runs = await self._all(
            "select id, at, clusters from sizing_runs where contract_id = %s "
            "and (%s::float8 is null or at <= %s) order by id desc",
            (contract_id, before, before),
        )
        status: dict[str, dict[str, Any]] = {}
        good: dict[str, int] = {}
        for run in runs:
            for cluster, error in run["clusters"].items():
                if cluster not in clusters:
                    continue
                st = status.setdefault(
                    cluster, {"at": None, "run": None, "error": None, "since": None}
                )
                if cluster in good:
                    continue
                if error is None:
                    good[cluster] = run["id"]
                    st["at"], st["run"] = run["at"], run["id"]
                else:
                    st["error"] = st["error"] or error
                    st["since"] = run["at"]
        rows = []
        for cluster, run_id in sorted(good.items()):
            rows += await self._all(
                "select row from sizings where contract_id = %s and run_id = %s and cluster = %s",
                (contract_id, run_id, cluster),
            )
        return [r["row"] for r in rows], status

    async def approve_sizing(
        self, contract_id: str, run_id: int, row: dict[str, Any], by: str, at: float
    ) -> Literal["approved", "already", "killed"]:
        """Store an Approved Sizing of `row`'s container from run `run_id`, with its
        requests where it changes them, unless the Contract's Kill switch is on."""
        keys = tuple(row[k] for k in ("cluster", "namespace", "kind", "name", "container"))
        wanted = [
            row[r]["target"] if row.get(r) and row[r]["action"] != "keep" else None
            for r in ("cpu", "memory")
        ]
        stored = await self._one(
            "insert into approved_sizings select %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s "
            "where not exists (select 1 from kill_switches where contract_id = %s) "
            # approved again after a Revert of that container: its time moves past the
            # Revert, so it is sent to the Actuator once more (slice 12.9b)
            "on conflict (contract_id, run_id, cluster, namespace, kind, name, container) "
            "do update set approved_by = excluded.approved_by, approved_at = excluded.approved_at "
            "where exists (select 1 from reverts r where "
            "r.contract_id = approved_sizings.contract_id "
            "and r.cluster = approved_sizings.cluster and r.namespace = approved_sizings.namespace "
            "and r.kind = approved_sizings.kind and r.name = approved_sizings.name "
            "and r.container = approved_sizings.container "
            "and r.asked_at >= approved_sizings.approved_at) returning run_id",
            (contract_id, run_id, *keys, *wanted, by, at, contract_id),
        )
        if stored:
            return "approved"
        return "killed" if await self.kill_switch(contract_id) else "already"

    async def approved_sizings(self, contract_id: str) -> list[dict[str, Any]]:
        """The Contract's current Approved Sizings: those no later run read the
        cluster of (a run still going, or failing there, supersedes nothing). `again`: one
        approved after a Revert of its container (slice 12.9b)."""
        return await self._all(
            "select a.*, exists (select 1 from reverts v where v.contract_id = a.contract_id "
            "and v.cluster = a.cluster and v.namespace = a.namespace and v.kind = a.kind "
            "and v.name = a.name and v.container = a.container "
            "and v.asked_at < a.approved_at) as again "
            "from approved_sizings a where contract_id = %s and not exists ("
            "select 1 from sizing_runs r where r.contract_id = a.contract_id "
            "and r.id > a.run_id and r.clusters -> a.cluster = 'null'::jsonb) "
            "order by approved_at",
            (contract_id,),
        )

    async def ask_revert(
        self, contract_id: str, keys: tuple[str, ...], by: str, at: float
    ) -> Literal["asked", "killed"]:
        """Store a Revert of the container `keys` names (cluster, namespace, kind, name,
        container), unless the Contract's Kill switch is on (slice 12.6a)."""
        stored = await self._one(
            "insert into reverts (contract_id, cluster, namespace, kind, name, container, "
            "asked_by, asked_at) select %s, %s, %s, %s, %s, %s, %s, %s where not exists "
            "(select 1 from kill_switches where contract_id = %s) returning id",
            (contract_id, *keys, by, at, contract_id),
        )
        return "asked" if stored else "killed"

    async def pending_reverts(self, contract_id: str, cluster: str) -> list[dict[str, Any]]:
        """The cluster's Reverts the Actuator hasn't reported done: no `reverted` Actuation
        of that container received since it was asked."""
        return await self._all(
            "select * from reverts r where contract_id = %s and cluster = %s and not exists ("
            "select 1 from actuations a where a.contract_id = r.contract_id "
            "and a.cluster = r.cluster and a.namespace = r.namespace and a.kind = r.kind "
            "and a.name = r.name and a.container = r.container and a.outcome = 'reverted' "
            "and a.received_at >= r.asked_at) order by id",
            (contract_id, cluster),
        )

    async def reverted(self, contract_id: str, cluster: str) -> set[tuple[str, ...]]:
        """(namespace, kind, name, container) of each container of the cluster Reverted
        since its last approval: Automation leaves it alone (slice 12.6a)."""
        rows = await self._all(
            "select distinct namespace, kind, name, container from reverts r "
            "where contract_id = %s and cluster = %s and not exists ("
            "select 1 from approved_sizings a where a.contract_id = r.contract_id "
            "and a.cluster = r.cluster and a.namespace = r.namespace and a.kind = r.kind "
            "and a.name = r.name and a.container = r.container and a.approved_at > r.asked_at)",
            (contract_id, cluster),
        )
        return {(r["namespace"], r["kind"], r["name"], r["container"]) for r in rows}

    async def record_actuations(
        self,
        contract_id: str,
        cluster: str,
        rows: list[dict[str, Any]],
        at: float,
        tell: Callable[[dict[str, Any]], bool] = lambda r: r["outcome"] == "rolled_back",
    ) -> list[dict[str, Any]]:
        """Store what an Actuator reported (slice 12.1); a row it sends again is kept once.
        The rows new to the Hub: what it tells the Operator about (slice 12.4), those
        `tell` says (a Rollback; a Step-up too, slice 13.1c) kept as still to tell."""
        new = []
        for r in rows:
            stored = await self._one(
                "insert into actuations values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                "%s, %s, %s, %s) on conflict do nothing returning run_id",
                (
                    contract_id,
                    cluster,
                    *(r[k] for k in ACTUATION_FIELDS),
                    at,
                    not tell(r),  # told: one to tell the Operator of is still to tell
                ),
            )
            if stored:
                new.append(r)
        return new

    async def actuations(self, contract_id: str, since: float) -> list[dict[str, Any]]:
        """The Contract's Actuations since `since`, newest first."""
        return await self._all(
            f"select {ACTUATION} from actuations "  # noqa: S608 - a constant column list
            "where contract_id = %s and least(at, received_at) >= %s "
            "order by least(at, received_at) desc, namespace, name, container",
            (contract_id, since),
        )

    async def untold_counts(self, contract_id: str, upto: float) -> dict[str, int]:
        """How many Rollbacks received by `upto` the Operator hasn't been told of (slice
        12.9c), by cluster."""
        rows = await self._all(
            "select cluster, count(*) as n from actuations where contract_id = %s "
            "and not told and received_at <= %s group by cluster",
            (contract_id, upto),
        )
        return {r["cluster"]: r["n"] for r in rows}

    async def untold(
        self, contract_id: str, cluster: str, upto: float, limit: int
    ) -> list[dict[str, Any]]:
        """The first `limit` of them on `cluster`, in the order they arrived; `sent_at`: the
        Actuator's own time, part of the row's key."""
        return await self._all(
            f"select {ACTUATION}, at as sent_at from actuations "  # noqa: S608
            "where contract_id = %s and cluster = %s and not told and received_at <= %s "
            "order by received_at, at, namespace, name, container limit %s",
            (contract_id, cluster, upto, limit),
        )

    async def mark_told(self, contract_id: str, rows: list[dict[str, Any]]) -> None:
        """These rows of `untold`: told."""
        keys = ("cluster", "namespace", "kind", "name", "container", "run_id", "sent_at")
        for r in rows:
            await self._exec(
                "update actuations set told = true where contract_id = %s and cluster = %s "
                "and namespace = %s and kind = %s and name = %s and container = %s "
                "and run_id = %s and at = %s",
                (contract_id, *(r[k] for k in keys)),
            )

    async def mark_all_told(
        self, contract_id: str, upto: float, cluster: str | None = None
    ) -> None:
        """Every untold Rollback of the Contract (of `cluster`, if named) received by
        `upto`: counted in one line, or given up on."""
        await self._exec(
            "update actuations set told = true where contract_id = %s and not told "
            "and received_at <= %s and (%s::text is null or cluster = %s)",
            (contract_id, upto, cluster, cluster),
        )

    async def first_sizings(self, contract_id: str, clusters: set[str]) -> dict[str, float]:
        """Each of `clusters`' oldest stored run that read it: how far back it compares."""
        rows = await self._all(
            "select e.key as cluster, min(r.at) as at from sizing_runs r, "
            "jsonb_each(r.clusters) e where r.contract_id = %s and e.key = any(%s) "
            "and e.value = 'null'::jsonb group by e.key",
            (contract_id, sorted(clusters)),
        )
        return {r["cluster"]: r["at"] for r in rows}

    async def realized(self, contract_id: str) -> list[dict[str, Any]]:
        """The Contract's realized request drops still saving (slice 10.3b)."""
        return await self._all(
            "select id, cluster, namespace, kind, name, container, resource, at, before, "
            "after, sizing from realized where contract_id = %s order by at, id",
            (contract_id,),
        )

    async def save_realized(
        self,
        contract_id: str,
        added: list[dict[str, Any]],
        raised: dict[int, float],
        ended: set[int],
    ) -> None:
        """New drops (a drop already stored against the same previous run is kept as
        it was), `after` raised by a later rise, and those no longer saving deleted."""
        async with self.pool.connection() as conn, conn.transaction():
            for e in added:
                await conn.execute(
                    "insert into realized (contract_id, cluster, namespace, kind, name, "
                    "container, resource, at, prev_run, before, after, sizing) values "
                    "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) on conflict do nothing",
                    (
                        contract_id,
                        *(e[k] for k in ("cluster", "namespace", "kind", "name", "container")),
                        *(e[k] for k in ("resource", "at", "prev_run", "before", "after")),
                        e["sizing"],
                    ),
                )
            for event_id, after in raised.items():
                await conn.execute(
                    "update realized set after = %s where id = %s and contract_id = %s",
                    (after, event_id, contract_id),
                )
            if ended:
                await conn.execute(
                    "delete from realized where contract_id = %s and id = any(%s)",
                    (contract_id, sorted(ended)),
                )

    async def floor_drops(self, contract_id: str) -> list[dict[str, Any]]:
        """The Contract's HPA minimums lowered toward a Floor and still saving (14.1c)."""
        return await self._all(
            "select id, cluster, namespace, kind, name, at, before, after, floor "
            "from floor_drops where contract_id = %s order by at, id",
            (contract_id,),
        )

    async def save_floor_drops(
        self,
        contract_id: str,
        added: list[dict[str, Any]],
        raised: dict[int, float],
        ended: set[int],
    ) -> None:
        """New drops (one already stored for the same run is kept as it was), `after`
        raised by a later rise of the minimum, and those no longer saving deleted."""
        async with self.pool.connection() as conn, conn.transaction():
            for e in added:
                await conn.execute(
                    "insert into floor_drops (contract_id, cluster, namespace, kind, name, at, "
                    "before, after, floor) values (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "on conflict do nothing",
                    (
                        contract_id,
                        *(e[k] for k in ("cluster", "namespace", "kind", "name")),
                        *(e[k] for k in ("at", "before", "after", "floor")),
                    ),
                )
            for drop_id, after in raised.items():
                await conn.execute(
                    "update floor_drops set after = %s where id = %s and contract_id = %s",
                    (after, drop_id, contract_id),
                )
            if ended:
                await conn.execute(
                    "delete from floor_drops where contract_id = %s and id = any(%s)",
                    (contract_id, sorted(ended)),
                )

    async def sizing_history(
        self, contract_id: str, workload: tuple[str, str, str, str, str], since: float
    ) -> list[tuple[float, dict[str, Any]]]:
        """One container's stored rows since `since`, oldest first. workload: cluster,
        namespace, kind, name, container."""
        rows = await self._all(
            "select r.at, s.row from sizings s join sizing_runs r on r.id = s.run_id "
            "where s.contract_id = %s and r.contract_id = %s and s.cluster = %s "
            "and s.namespace = %s and s.kind = %s and s.name = %s and s.container = %s "
            "and r.at >= %s order by r.at",
            (contract_id, contract_id, *workload, since),
        )
        return [(r["at"], r["row"]) for r in rows]

    async def sync_digests(self, due: dict[str, tuple[str, float]]) -> None:
        """due: Contract id -> (schedule key, its next fire time), for every Contract
        that gets a digest; the others' rows go, so turning one back on starts anew."""
        await self._exec("delete from sizing_digests where not contract_id = any(%s)", (list(due),))
        for contract_id, (key, next_due) in due.items():
            await self._exec(
                "insert into sizing_digests (contract_id, schedule, next_due) "
                "values (%s, %s, %s) on conflict (contract_id) do update set "
                "schedule = excluded.schedule, next_due = case when "
                "sizing_digests.schedule = excluded.schedule then sizing_digests.next_due "
                "else excluded.next_due end",
                (contract_id, key, next_due),
            )

    async def claim_digest(self, contract_id: str, at: float, next_due: float) -> bool:
        """True once per due time: its next due time moves to `next_due` (computed from
        now, so missed fire times post once) in the same statement that finds it due."""
        row = await self._one(
            "update sizing_digests set next_due = %s, last_at = %s "
            "where contract_id = %s and next_due <= %s returning contract_id",
            (next_due, at, contract_id, at),
        )
        return row is not None

    async def sync_checks(self, contract_id: str, due: dict[str, tuple[str, float]]) -> None:
        """due: name -> (schedule key, its next fire time). A new Check, or one whose
        schedule key changed, gets that time; the others keep theirs."""
        for name, (key, next_due) in due.items():
            await self._exec(
                "insert into check_schedules (contract_id, name, schedule, next_due) "
                "values (%s, %s, %s, %s) on conflict (contract_id, name) do update set "
                "schedule = excluded.schedule, next_due = case when "
                "check_schedules.schedule = excluded.schedule then check_schedules.next_due "
                "else excluded.next_due end",
                (contract_id, name, key, next_due),
            )

    async def due_checks(self, at: float) -> list[tuple[str, str]]:
        rows = await self._all(
            "select contract_id, name from check_schedules where next_due <= %s", (at,)
        )
        return [(r["contract_id"], r["name"]) for r in rows]

    async def claim_check(
        self, t: Investigation, name: str, at: float, next_due: float, skip: str | None
    ) -> str:
        """Fire one due Check, once: in one transaction its next due time moves to
        `next_due` (computed from now, so missed fire times run once), and either its
        Investigation t is inserted ("started") or the skip is recorded ("skipped":
        `skip` given, or its previous Run still open). "not due": already claimed."""
        async with self.pool.connection() as conn, conn.transaction():
            row = await (
                await conn.execute(
                    "select next_due, last_id from check_schedules where contract_id = %s "
                    "and name = %s for update",
                    (t.contract_id, name),
                )
            ).fetchone()
            if row is None or row["next_due"] > at:
                return "not due"
            if skip is None and row["last_id"]:
                last = await (
                    await conn.execute(
                        "select status from investigations where id = %s", (row["last_id"],)
                    )
                ).fetchone()
                if last and last["status"] in ("running", "awaiting_approval"):
                    skip = "its previous Run is still open"
            if skip:
                await conn.execute(
                    "update check_schedules set next_due = %s, skipped_at = %s, "
                    "skipped_reason = %s where contract_id = %s and name = %s",
                    (next_due, at, skip, t.contract_id, name),
                )
                return "skipped"
            await conn.execute(INSERT, _row(t))
            await conn.execute(
                "update check_schedules set next_due = %s, last_id = %s "
                "where contract_id = %s and name = %s",
                (next_due, t.id, t.contract_id, name),
            )
            return "started"

    async def check_schedule(self, contract_id: str, name: str) -> dict[str, Any] | None:
        return await self._one(
            "select * from check_schedules where contract_id = %s and name = %s",
            (contract_id, name),
        )

    async def check_schedules(self, contract_id: str | None = None) -> list[dict[str, Any]]:
        """With each one's last Run (status, verdict)."""
        return await self._all(
            "select s.*, i.status as last_status, (i.check_result->>'passed')::boolean "
            "as last_passed, i.started as last_started from check_schedules s "
            "left join investigations i on i.id = s.last_id "
            "where %s::text is null or s.contract_id = %s order by s.contract_id, s.name",
            (contract_id, contract_id),
        )

    async def set_failing(
        self, contract_id: str, name: str, investigation_id: str | None, expected: str | None
    ) -> bool:
        """Open (an id) or end (None) the Check's Open failure, only if it is still
        `expected`: two Runs finishing together can't both open one."""
        row = await self._one(
            "update check_schedules set failing_id = %s where contract_id = %s and "
            "name = %s and failing_id is not distinct from %s returning name",
            (investigation_id, contract_id, name, expected),
        )
        return row is not None

    async def queue_deploy(
        self,
        contract_id: str,
        cluster: str,
        namespace: str,
        changes: list[dict[str, str]],
        at: float,
        delay: float,
        failing_before: list[str] | None = None,
    ) -> None:
        """A deploy to verify `delay` seconds from now. One already pending in that
        namespace takes it in and is pushed back, but never past twice the delay from
        its first deploy, and keeps what was failing before that first one."""
        before = Jsonb(failing_before) if failing_before is not None else None
        await self._exec(
            "insert into deploy_checks (contract_id, cluster, namespace, first_seen, due, "
            "changes, failing_before) values (%s, %s, %s, %s, %s, %s, %s) "
            "on conflict (contract_id, cluster, namespace) where state = 'pending' do update "
            "set changes = deploy_checks.changes || excluded.changes, "
            "due = least(excluded.due, deploy_checks.first_seen + %s)",
            (contract_id, cluster, namespace, at, at + delay, Jsonb(changes), before, 2 * delay),
        )

    async def due_deploys(self, at: float) -> list[dict[str, Any]]:
        return await self._all(
            "select * from deploy_checks where state = 'pending' and due <= %s order by id",
            (at,),
        )

    async def claim_deploy(
        self, row_id: int, t: Investigation, name: str, at: float, skip: str | None
    ) -> str:
        """Fire one due verification, once: "started" (t inserted with it), "skipped"
        (recorded), "busy" (the namespace's previous one still open: it stays pending
        for the next tick) or "not due"."""
        async with self.pool.connection() as conn, conn.transaction():
            row = await (
                await conn.execute(
                    "select due from deploy_checks where id = %s and contract_id = %s "
                    "and state = 'pending' for update",
                    (row_id, t.contract_id),
                )
            ).fetchone()
            if row is None or row["due"] > at:
                return "not due"
            if skip:
                await conn.execute(
                    "update deploy_checks set state = 'skipped', skipped_reason = %s where id = %s",
                    (skip, row_id),
                )
                return "skipped"
            open_ = await (
                await conn.execute(
                    "select 1 from investigations where contract_id = %s and kind = 'check' "
                    "and input->>'check' = %s and status in ('running', 'awaiting_approval')",
                    (t.contract_id, name),
                )
            ).fetchone()
            if open_:
                return "busy"
            await conn.execute(INSERT, _row(t))
            await conn.execute(
                "update deploy_checks set state = 'started', investigation_id = %s where id = %s",
                (t.id, row_id),
            )
            return "started"

    async def deploy_checks(self, contract_id: str | None = None) -> list[dict[str, Any]]:
        """The latest verifications, with each started one's Run (status, verdict)."""
        return await self._all(
            "select d.*, i.status as run_status, (i.check_result->>'passed')::boolean "
            "as passed from deploy_checks d left join investigations i "
            "on i.id = d.investigation_id where %s::text is null or d.contract_id = %s "
            "order by d.id desc limit 30",
            (contract_id, contract_id),
        )

    async def similar(
        self,
        contract_id: str,
        keywords: str,
        since: float,
        limit: int,
        near: tuple[list[float], str, float] | None = None,
    ) -> list[dict[str, Any]]:
        """This Contract's finished Triage Reports since `since` matching any of the
        words, best match first. Only words reach the query: it never fails to parse.
        near (vector, model, max distance): also those whose embedding by that model is
        that close, the two rankings merged (reciprocal rank fusion)."""
        words = re.findall(r"\w+", keywords)[:20]
        if not words:
            return []
        vector, model, distance = near or (None, None, None)
        # ponytail: no vector index, a scan of one Contract's Reports; add HNSW past ~100k
        return await self._all(
            "with mine as (select id, started, title, report, report_search, embedding, "
            "embedding_model from investigations where contract_id = %(c)s "
            "and kind = 'triage' and status = 'done' and report is not null "
            "and started >= %(since)s), "
            "words as (select id, row_number() over (order by ts_rank(report_search, q) "
            "desc, started desc) r from mine, websearch_to_tsquery('english', %(words)s) q "
            "where report_search @@ q order by r limit 20), "
            "meaning as (select id, row_number() over (order by embedding <=> %(v)s::vector) "
            "r from mine where embedding_model = %(model)s "
            "and embedding <=> %(v)s::vector < %(d)s order by r limit 20), "
            "fused as (select id, sum(1.0 / (60 + r)) score from "
            "(select * from words union all select * from meaning) u group by id) "
            "select id, started, title, report from mine join fused using (id) "
            "order by score desc, started desc limit %(limit)s",
            {
                "c": contract_id,
                "since": since,
                "words": " or ".join(words),
                "v": json.dumps(vector) if vector else None,
                "model": model,
                "d": distance,
                "limit": limit,
            },
        )

    async def set_embedding(
        self, contract_id: str, investigation_id: str, model: str, vector: list[float]
    ) -> bool:
        """Only on this Contract's own Investigation."""
        row = await self._one(
            "update investigations set embedding = %s::vector, embedding_model = %s "
            "where id = %s and contract_id = %s returning id",
            (json.dumps(vector), model, investigation_id, contract_id),
        )
        return row is not None

    async def unembedded(self, contract_id: str, model: str) -> list[dict[str, Any]]:
        """This Contract's finished Triage Reports with no embedding by `model`."""
        return await self._all(
            "select id, title, report from investigations where contract_id = %s "
            "and kind = 'triage' and status = 'done' and report is not null "
            "and embedding_model is distinct from %s order by started",
            (contract_id, model),
        )

    async def past_report(self, contract_id: str, investigation_id: str) -> dict[str, Any] | None:
        """One finished Triage Report, only if it is this Contract's."""
        return await self._one(
            "select id, started, title, report, executed_actions, jira_issue "
            "from investigations where id = %s and contract_id = %s "
            "and kind = 'triage' and status = 'done' and report is not null",
            (investigation_id, contract_id),
        )

    async def add_feedback(
        self, t: Investigation, correct: bool, correction: str | None, by: str
    ) -> None:
        await self._exec(
            "insert into feedback (investigation_id, contract_id, correct, correction, by, at) "
            "values (%s, %s, %s, %s, %s, %s)",
            (t.id, t.contract_id, correct, correction, by, now()),
        )

    async def feedback(self, investigation_id: str) -> list[dict[str, Any]]:
        return await self._all(
            "select correct, correction, by, at from feedback "
            "where investigation_id = %s order by id",
            (investigation_id,),
        )

    async def teachings(
        self, contract_id: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        """Newest first; one Contract's and one status's only when given."""
        return await self._all(
            "select id, contract_id, text, alerts, status, source_id, correction, contradicts, "
            "proposed_at, decided_by, decided_at, slack_ts from teachings "
            "where (%s::text is null or contract_id = %s) and (%s::text is null or status = %s) "
            "order by id desc",
            (contract_id, contract_id, status, status),
        )

    async def teaching(self, teaching_id: int) -> dict[str, Any] | None:
        return await self._one(
            "select id, contract_id, text, alerts, status, source_id, correction, contradicts, "
            "slack_ts from teachings where id = %s",
            (teaching_id,),
        )

    async def add_teaching(
        self,
        contract_id: str,
        text: str,
        alerts: list[str],
        *,
        source_id: str | None = None,
        correction: str | None = None,
        contradicts: int | None = None,
        approved_by: str | None = None,
    ) -> int:
        """Proposed, or approved at once when the Operator wrote it (approved_by)."""
        at = now()
        row = await self._one(
            "insert into teachings (contract_id, text, alerts, status, source_id, correction, "
            "contradicts, proposed_at, decided_by, decided_at) "
            "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) returning id",
            (
                contract_id,
                text,
                Jsonb(alerts),
                "approved" if approved_by else "proposed",
                source_id,
                correction,
                contradicts,
                at,
                approved_by,
                at if approved_by else None,
            ),
        )
        assert row is not None
        return int(row["id"])

    async def set_teaching_post(self, teaching_id: int, ts: str) -> None:
        await self._exec("update teachings set slack_ts = %s where id = %s", (ts, teaching_id))

    async def decide_teaching(
        self,
        teaching_id: int,
        contract_id: str,
        approve: bool,
        by: str,
        text: str | None = None,
        source_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Approve (with the text the Operator submitted, if any) or reject a proposed
        Teaching of this Contract, once. Approving retires the approved Teaching of the
        same Contract it contradicts. source_id: from a Slack click, the Investigation
        whose thread holds it. Returns the decided row (and `retired`), or None."""
        async with self.pool.connection() as conn, conn.transaction():
            row: dict[str, Any] | None = await (
                await conn.execute(
                    "update teachings set status = %s, decided_by = %s, decided_at = %s, "
                    "text = coalesce(%s, text) where id = %s and contract_id = %s "
                    "and status = 'proposed' and (%s::text is null or source_id = %s) "
                    "returning id, contract_id, text, alerts, status, source_id, contradicts, "
                    "slack_ts",
                    (
                        "approved" if approve else "rejected",
                        by,
                        now(),
                        text,
                        teaching_id,
                        contract_id,
                        source_id,
                        source_id,
                    ),
                )
            ).fetchone()
            if row is None:
                return None
            row["retired"] = None
            if approve and row["contradicts"] is not None:
                retired = await (
                    await conn.execute(
                        "update teachings set status = 'retired', decided_by = %s, "
                        "decided_at = %s where id = %s and contract_id = %s "
                        "and status = 'approved' returning id",
                        (by, now(), row["contradicts"], contract_id),
                    )
                ).fetchone()
                row["retired"] = retired["id"] if retired else None
        return row

    async def retire_teaching(self, teaching_id: int, contract_id: str, by: str) -> bool:
        row = await self._one(
            "update teachings set status = 'retired', decided_by = %s, decided_at = %s "
            "where id = %s and contract_id = %s and status = 'approved' returning id",
            (by, now(), teaching_id, contract_id),
        )
        return row is not None

    async def open_triages(
        self, contract_id: str, since: float, group_key: str
    ) -> list[Investigation]:
        """The Triages a new Alert group of this Contract may join: started since
        `since`, own group still firing, not failed (a refusal fails too). Never one
        whose group the Operator split from this one, either way round, while the
        split-off Triage is within the window too: next incident, they may join."""
        rows = await self._all(
            f"select {COLUMNS} from investigations "  # noqa: S608 - fixed columns
            "where contract_id = %s and kind = 'triage' and started >= %s "
            "and not group_resolved and status <> 'failed' and not exists ("
            " select 1 from related_groups r join investigations i on i.id = r.investigation_id"
            " join investigations s on s.id = r.separate_id"
            " where r.contract_id = %s and s.started >= %s and ("
            "  (r.group_key = %s and i.group_key = investigations.group_key)"
            "  or (i.group_key = %s and r.group_key = investigations.group_key))"
            ") order by started desc",
            (contract_id, since, contract_id, since, group_key, group_key),
        )
        return [_investigation(r) for r in rows]

    async def add_related(
        self,
        investigation_id: str,
        contract_id: str,
        group: AlertGroup,
        matched: dict[str, str],
        louder: bool,
        at: float,
    ) -> int:
        row = await self._one(
            "insert into related_groups (investigation_id, contract_id, group_key, title, "
            "input, matched, louder, at) values (%s, %s, %s, %s, %s, %s, %s, %s) returning id",
            (
                investigation_id,
                contract_id,
                group.groupKey,
                group.title,
                Jsonb(group.model_dump(mode="json")),
                Jsonb(matched),
                louder,
                at,
            ),
        )
        assert row is not None
        return int(row["id"])

    async def related_group(self, related_id: int, investigation_id: str) -> dict[str, Any] | None:
        """One Related alert group, only if it joined that Triage."""
        return await self._one(
            "select group_key, title, input, matched, louder, separate_id from related_groups "
            "where id = %s and investigation_id = %s",
            (related_id, investigation_id),
        )

    async def separate(self, related_id: int, investigation_id: str, t: Investigation) -> bool:
        """Triage a Related alert group separately, once: t, its own Triage, is
        inserted together with the claim, or not at all (a second click)."""
        async with self.pool.connection() as conn, conn.transaction() as tx:
            await conn.execute(INSERT, _row(t))  # first: separate_id references it
            claimed = await (
                await conn.execute(
                    "update related_groups set separate_id = %s where id = %s "
                    "and investigation_id = %s and separate_id is null returning id",
                    (t.id, related_id, investigation_id),
                )
            ).fetchone()
            if claimed is None:
                raise Rollback(tx)
        return claimed is not None

    async def related(self, investigation_id: str) -> list[dict[str, Any]]:
        return await self._all(
            "select id, group_key, title, input, matched, louder, at, separate_id "
            "from related_groups where investigation_id = %s order by id",
            (investigation_id,),
        )

    async def alert_group(
        self, contract_id: str, group_key: str
    ) -> tuple[float, str, set[str]] | None:
        row = await self._one(
            "select started, investigation_id, seen from alert_groups "
            "where contract_id = %s and group_key = %s",
            (contract_id, group_key),
        )
        return (row["started"], row["investigation_id"], set(row["seen"])) if row else None

    async def remember_group(
        self,
        contract_id: str,
        group_key: str,
        started: float,
        investigation_id: str,
        seen: set[str],
    ) -> None:
        await self._exec(
            "insert into alert_groups values (%s, %s, %s, %s, %s) "
            "on conflict (contract_id, group_key) do update set started = excluded.started, "
            "investigation_id = excluded.investigation_id, seen = excluded.seen",
            (contract_id, group_key, started, investigation_id, sorted(seen)),
        )
