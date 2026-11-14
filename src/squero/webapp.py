"""The console app (slice 9.5a): a React single-page app at /app/ and its JSON API at
/api/v1/; since slice 11.7b the whole console (the Jinja pages are gone).

The console's doors (console.py): the page and its files need a
listed Operator's Tailscale identity; every API call also needs the approver cookie,
and answers 401 with the token page's address without it. The app is built in the
image's Docker build (console/, Vite) into SQUERO_APP_DIR; nothing loads from a CDN,
and its CSP allows nothing but the Hub's own origin. Since 11.5a it carries
model-written text (Reports, Answers, tool output) and alert titles from client
Alertmanagers, as fields: the app renders everything as text, never as markup, and
an Evidence link as text too, never a link.
"""

import hmac
import json
import logging
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import yaml
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

from squero import actuator, agenda, billing, listprices, sizing, usage
from squero.console import (
    COOKIE,
    CORRECTION_LIMIT,
    QUESTION_LIMIT,
    Ask,
    Decide,
    DecideTeaching,
    Feedback,
    FileJira,
    Health,
    action_key,
    same_origin,
    scenario_file,
    set_cookie,
)
from squero.contracts import Contract, Jira
from squero.models import Answer, CheckResult, Investigation, TriageReport
from squero.safety import redact
from squero.store import Store
from squero.teachings import LIMIT as TEACHING_LIMIT
from squero.teachings import section

log = logging.getLogger("squero.webapp")

APP_DIR = Path(os.environ.get("SQUERO_APP_DIR", "/opt/squero/app"))
HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
}
ASSET = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]*")  # Vite's hashed names; no path
TYPES = {".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml"}
TOKEN_PAGE = "/app/?view=token"  # noqa: S105 (a page, not a secret)
SHOWN = 4000  # characters of a tool call's result, as the Jinja page shows
DAY = 86400.0


class Risk(BaseModel):
    kind: str
    text: str
    container: str | None = None


class HeldByHpa(BaseModel):
    """A request its HPA divides by (slice 14.3): the Actuator leaves it alone."""

    hpa: str
    percent: float  # the HPA's Utilization target
    average: float | None  # the same threshold in cores or bytes; None: no request to go by
    change: str | None  # the manifest change that frees it


class Resource(BaseModel):
    action: str
    current: float | None
    target: float
    basis: float
    held: HeldByHpa | None = None


class Waste(BaseModel):
    cpu: float = 0.0  # cores
    memory: float = 0.0  # bytes


class Cost(BaseModel):
    """Monthly, in the Contract's currency (slice 10.1)."""

    waste: float  # its requests above the Sizings
    requests: float  # every request, ready or not


class Container(BaseModel):
    workload: str
    cluster: str
    namespace: str
    kind: str
    name: str
    container: str
    policy: str
    state: Literal["ready", "learning", "excluded"]
    history_h: float | None = None
    needs_h: float | None = None
    pods: float | None = None
    cpu: Resource | None = None
    memory: Resource | None = None
    waste: Waste | None = None
    risks: list[Risk] = []


class Workload(BaseModel):
    workload: str
    cluster: str
    namespace: str
    kind: str
    name: str
    pods: float
    waste: Waste
    cost: Cost | None = None  # None: the Contract has no prices
    risks: list[Risk]
    containers: list[Container]


class Change(BaseModel):
    requests: Waste
    waste: Waste
    cost: Cost | None = None  # None: the Contract has no prices


class Group(BaseModel):
    group: str  # cluster/namespace, a team or a label value
    workloads: int
    requests: Waste  # cores and bytes, ready or not
    waste: Waste  # priced, the available Savings
    cost: Cost | None = None  # None: the Contract has no prices
    change: Change | None = None  # since the compared run; None: not compared


class Compared(BaseModel):
    at: float | None  # the cluster's run compared with; None: none that old, left out
    oldest: float | None  # its oldest stored run


class Comparison(BaseModel):
    since: float
    clusters: dict[str, Compared]
    change: Change  # the Contract's, of what both sides know
    left_out: list[str]  # containers whose requests or labels were unknown then


class SpentCost(BaseModel):
    rate: float
    accrued: float


class SavingsCost(BaseModel):
    rate: float  # a month's worth, at today's prices
    accrued: float  # since each drop
    week: float  # the rate of the drops of the last 7 days


class Drop(BaseModel):
    workload: str  # cluster/namespace/kind/name/container
    resource: Literal["cpu", "memory"]
    at: float  # the run that saw it
    before: float  # the request per pod
    after: float
    sizing: float
    pods: float  # in the container's latest run
    saved: float  # cores or bytes: (before - after) x pods
    cost: SpentCost | None = None


class FloorDrop(BaseModel):
    """An HPA's minimum lowered toward its Floor (slice 14.1c)."""

    workload: str  # cluster/namespace/kind/name
    at: float  # the run that saw it
    before: float  # the HPA's minimum
    after: float  # what it counts down to: never below the Floor shown then
    floor: float
    replicas: float  # no longer held, on average over the last 7 days
    saved: Waste  # those replicas at the pod's requests now
    cost: SpentCost | None = None


class Realized(BaseModel):
    currency: str | None = None
    rate: Waste  # cores and bytes held less, now
    accrued: Waste  # core-hours and byte-hours
    week: Waste
    cost: SavingsCost | None = None  # None: the Contract has no prices
    drops: list[Drop]  # by what they save
    floors: list[FloorDrop] = []  # counted in the totals above


class Bill(BaseModel):
    """A billed cluster's cloud bill beside its estimate; `note` says why there is none."""

    cluster: str
    tag: str
    start: str
    end: str
    days: int
    billed: float | None
    estimated: float | None
    currency: str | None
    gap: float | None
    within: bool | None  # None with a gap: too few sampled hours to judge
    hours: int  # node-hours a complete estimate rests on
    sampled: int | None
    coverage: float | None
    note: str | None


class Bills(BaseModel):
    tolerance: float
    min_coverage: float
    clusters: list[Bill]


class PriceNote(BaseModel):
    cluster: str
    source: str  # typed, list, or none (the note says why)
    note: str


class Allocation(BaseModel):
    contract: str
    by: str  # namespace, team or label:<key>
    currency: str | None = None
    total: Change
    groups: list[Group]  # ranked by what their requests cost; then any gone since
    compare: Comparison | None = None
    realized: Realized
    groupings: list[str]  # what this Contract can be grouped by
    warnings: list[str]  # clusters where no pod has a label the grouping reads
    prices: list[PriceNote] = []  # where each cluster's price comes from (list_prices)


class ClusterStatus(BaseModel):
    at: float | None  # the latest run that read it
    error: str | None  # why the runs since failed
    since: float | None


class Block(BaseModel):
    """Hours of the week that need more than the Floor (slice 14.2)."""

    since: str  # Mon 07:45, in the profile's time zone
    until: str
    replicas: int
    start: str  # both as cron expressions
    end: str


class Backtest(BaseModel):
    """A Weekly profile's Backtest (slice 14.4): the newest week against a profile built
    from the weeks before it."""

    state: Literal["waiting", "within", "over"]  # waiting: under four stored weeks
    error: float | None = None  # few + many
    few: float | None = None  # replica-hours too few, as a share of those needed
    many: float | None = None
    hours: int | None = None  # of the newest week replayed
    replicas: float | None = None  # the too many, on average
    excess: dict[str, float] | None = None  # those replicas at their Sizings
    cost: float | None = None  # a month of them, with a priced Contract


class Profile(BaseModel):
    """A Workload's Weekly profile (slice 14.2)."""

    # irregular: its Backtest is over the budget, so it has no blocks: the Floor holds
    state: Literal["ready", "learning", "flat", "irregular"]
    timezone: str
    lead_time: int  # seconds a block that asks more starts early
    history_h: float
    needs_h: float
    blocks: list[Block] = []
    triggers: str | None = None  # KEDA: cron triggers to paste into its ScaledObject
    note: str | None = None  # a plain HPA: why there is none
    backtest: Backtest | None = None


class Floor(BaseModel):
    """A Workload with an autoscaler (slice 14.1b): its Floor against its HPA's minimum."""

    workload: str
    cluster: str
    namespace: str
    kind: str
    name: str
    hpa: str
    policy: str
    percentile: float
    minimum: float | None
    maximum: float | None
    state: Literal["ready", "learning", "none"]
    why: str | None = None  # of `none`
    history_h: float | None = None  # hours of stored Needs it goes by
    needs_h: float | None = None
    floor: int | None = None
    replicas: float | None = None  # fewer on average at the Floor, when it is below the minimum
    saving: Waste | None = None  # those replicas at their Sizings
    cost: float | None = None  # monthly, with prices
    risks: list[Risk] = []
    profile: Profile | None = None
    at: float  # the run that computed it


class FloorsTotal(BaseModel):
    workloads: int  # that can lower their HPA's minimum
    replicas: float
    saving: Waste
    cost: float | None = None


class Workloads(BaseModel):
    contract: str
    clusters: dict[str, ClusterStatus]
    total: Waste
    currency: str | None = None  # the Contract's prices', when it has them
    cost: Cost | None = None
    workloads: list[Workload]  # ranked by waste, by its cost when priced
    learning: list[Container]
    excluded: list[Container]
    prices: list[PriceNote] = []
    floors: list[Floor] = []
    floors_total: FloorsTotal | None = None


class NodePod(BaseModel):
    namespace: str
    pod: str
    cpu: float
    memory: float


class Share(BaseModel):
    """Some of a node's pods and what they ask together."""

    pods: int
    cpu: float
    memory: float


class Node(BaseModel):
    """A node and its Room (slice 15.1a)."""

    node: str
    type: str  # its instance type, "" when the label is not allowlisted
    taints: list[str]  # key=value:effect
    cordoned: bool
    allocatable: dict[str, float]
    requests: dict[str, float]  # of every Running or Pending pod on it
    daemonsets: dict[str, float]  # of those, its DaemonSets' pods
    pods: int
    room: dict[str, float]  # allocatable less requests
    mine: Share  # the Contract's pods
    others: Share  # not visible: pods outside its namespaces, never named
    own: list[NodePod]


class ClusterNodes(BaseModel):
    cluster: str
    at: float  # the run that read them
    autoscaler: str | None = None  # as the Contract names it
    problem: str | None = None  # why there are none
    largest: dict[str, float] | None = None  # the Room after DaemonSets that caps Sizings
    nodes: list[Node]


class Nodes(BaseModel):
    clusters: list[ClusterNodes]


class Point(BaseModel):
    at: float
    pods: float | None = None
    cpu: Resource | None = None
    memory: Resource | None = None


class History(BaseModel):
    points: list[Point]  # oldest first: hourly over 14 days, the last of each day over 400


class ContractSummary(BaseModel):
    id: str
    name: str


class ContractOverview(ContractSummary):
    """A Contract at a glance (slice 11.7a): where it runs, where it talks, which model,
    and this month's model spend against its budget."""

    clusters: list[str]
    operator_channel: str | None
    model: str | None  # None: the Hub's default
    spent_usd: float
    budget_usd: float | None


class Brief(BaseModel):
    """An Investigation in a list (slice 11.5a)."""

    id: str
    contract: str
    kind: str
    title: str
    status: str
    failed: bool  # a Check whose verdict is no, whatever its Run's status
    started: float


class Usage(BaseModel):
    input_tokens: int
    output_tokens: int
    cost_usd: float | None


class Decision(BaseModel):
    approve: bool
    actions: list[str]  # the Proposed actions' tool names
    message: str
    decided_by: str
    at: float


class Related(BaseModel):
    title: str
    matched: dict[str, str]
    louder: bool
    at: float
    separate_id: str | None


class ToolCall(BaseModel):
    tool: str
    args: dict[str, Any]
    result: str  # its first SHOWN characters
    is_error: bool


class Verdict(BaseModel):
    correct: bool
    correction: str | None
    by: str
    at: float


class Taught(BaseModel):
    id: int
    status: str
    text: str


class Turn(BaseModel):
    """An earlier turn of a Question: what was asked, when, and the Answer it got."""

    question: str
    at: float
    answer: Answer | None  # None: that Run failed


class JiraState(BaseModel):
    """The Contract's Jira project for this Report (slice 11.5b): its issue once filed,
    who is filing it while there is none yet, or nothing: it can be filed."""

    project: str
    issue: str | None
    url: str | None  # the issue's page
    by: str | None
    since: float | None  # being filed (or Jira never answered) since then


class InvestigationPage(Brief):
    """One Investigation in full: its Report, Answer or Check verdict as fields (the
    app shows each as text), what waits for a decision, and what it did."""

    error: str | None
    deadline: float | None
    report: TriageReport | None
    answer: Answer | None
    check_result: CheckResult | None
    pending: list[str]  # each Proposed action's description
    executed_actions: list[str]
    usage: Usage
    masked: dict[str, int]
    decisions: list[Decision]
    related: list[Related]
    tool_calls: list[ToolCall]
    feedback: list[Verdict]
    taught: list[Taught]
    scenario: bool  # finished, with its input recorded: it downloads as a Scenario
    jira: JiraState | None  # a finished Report of a Contract with Jira
    can_judge: bool  # a finished Triage's or Check's Report: right or wrong
    can_follow_up: bool  # a Question no longer answering (slice 11.7a)
    asked: str | None  # a Question's latest question, which the Answer answers
    exchange: list[Turn]  # its earlier turns, oldest first


def brief(t: Investigation) -> dict[str, Any]:
    failed = bool(t.check_result and not t.check_result.passed)
    return {
        "id": t.id,
        "contract": t.contract_id,
        "kind": t.kind,
        "title": t.title,
        "status": t.status,
        "failed": failed,
        "started": t.started,
    }


class Token(BaseModel):
    token: str


class DecisionIn(BaseModel):
    deadline: float  # the pause's, as the page showed it: an older one decides nothing
    approve: bool
    reason: str = ""


class QuestionIn(BaseModel):
    question: str


class FeedbackIn(BaseModel):
    correct: bool
    correction: str = ""


class Teaching(BaseModel):
    id: int
    status: str  # proposed, approved, rejected or retired
    text: str
    alerts: list[str]  # empty: every alert
    source_id: str | None  # the Investigation it was drafted from
    correction: str | None  # the Operator's words it was drafted from
    contradicts: int | None  # approving it retires that approved one
    decided_by: str | None
    decided_at: float | None


class Teachings(BaseModel):
    """A Contract's Teachings (slice 11.6a), newest first in each list."""

    limit: int  # characters of one Teaching
    left_out: int  # approved ones that don't fit in a prompt
    proposed: list[Teaching]
    approved: list[Teaching]
    past: list[Teaching]  # rejected and retired


class TeachingDecisionIn(BaseModel):
    approve: bool
    text: str = ""  # an approval's text, as the Operator edited it


class TeachingIn(BaseModel):
    text: str
    alerts: list[str] = []  # empty: every alert


class CheckSchedule(BaseModel):
    name: str
    schedule: str  # "<cron> <timezone>"
    next_due: float
    last_id: str | None  # the latest Run's Investigation
    last_status: str | None
    last_passed: bool | None
    last_started: float | None
    failing_id: str | None  # the Open failure's Investigation
    skipped_at: float | None
    skipped_reason: str | None


class DeployCheck(BaseModel):
    cluster: str
    namespace: str
    changes: list[dict[str, str]]  # {namespace, object, revision}
    due: float
    state: str  # pending, started or skipped
    investigation_id: str | None
    run_status: str | None
    passed: bool | None
    skipped_reason: str | None


class Checks(BaseModel):
    """A Contract's scheduled Checks and latest Deploy verifications (slice 11.6b)."""

    checks: list[CheckSchedule]
    deploys: list[DeployCheck]  # newest first, at most 30


class Probe(BaseModel):
    name: str
    ok: bool
    ms: int
    detail: str  # the error, redacted and capped


class HealthPage(BaseModel):
    """One Contract's integrations checked now through its own tools, its last finished
    Investigation, its config with only the names of its secrets, this month's model
    spend, and the Hub's settings with every secret masked (slice 11.6b)."""

    integrations: list[Probe]
    last: Brief | None
    config: str  # the Contract as loaded, defaults included, as YAML
    secrets: dict[str, bool]  # each secret's name: did it decrypt
    spent_usd: float  # this calendar month (UTC)
    budget_usd: float | None
    hub: str


class KillSwitch(BaseModel):
    """A Contract's Kill switch (slice 11.2a): who turned it on, and when."""

    on: bool
    by: str | None = None
    since: float | None = None


class Held(BaseModel):
    """One container the Actuator reported on (slices 12.5, 12.6b): its last Actuation,
    whether the Contract automates it, and what stops that."""

    namespace: str
    kind: str
    name: str
    container: str
    automated: bool  # a Sizing rule automates it
    resumes: float | None  # its Workload's Automation ended by Rollbacks, until then
    stopped: bool  # Reverted since its last approval: Automation leaves it alone
    reverting: bool  # a Revert asked, not yet done
    # it holds requests (its last Actuation that isn't a skip applied or failed) or a Step-up
    revertable: bool
    stepped: float | None = None  # its Step-up's memory limit now, bytes (slice 13.1c)
    burst: float | None = None  # its Burst's CPU limit now, cores (slice 13.3b)
    outcome: str
    cpu: float | None
    memory: float | None
    pods: int
    reason: str
    at: float


class HeldCluster(BaseModel):
    cluster: str
    containers: list[Held]


class AutomationPage(BaseModel):
    kill_switch: bool
    clusters: list[HeldCluster]  # each with an Actuator


class RevertIn(BaseModel):
    cluster: str
    namespace: str
    kind: str = "Deployment"
    name: str
    container: str


Step = dict[tuple[str, ...], dict[str, Any]]


REFLEXES = {"stepped_up": "step_ended", "burst": "burst_ended"}  # each one's making, its end


async def done(st: Store, contract_id: str) -> tuple[list[dict[str, Any]], Step, Step]:
    """What the Contract's Actuators did, newest first, in three: with Sizings (their
    Actuations but what a Reflex saw, made and ended, which are no Sizing's); each
    container's Step-up that lasts (slice 13.1c), by cluster, namespace, kind, name and
    container: its last `stepped_up`, unless its end or a Revert came after; and each
    one's Burst that lasts (slice 13.3b), the same way."""
    seen = await st.actuations(contract_id, 0.0)
    lasting: dict[str, Step] = {made: {} for made in REFLEXES}
    for made, ended in REFLEXES.items():
        decided = set()
        for a in seen:
            key = tuple(a[k] for k in ("cluster", "namespace", "kind", "name", "container"))
            if key not in decided and a["outcome"] in (made, ended, "reverted"):
                decided.add(key)
                if a["outcome"] == made:
                    lasting[made][key] = a
    own = ("oom_killed", "throttled", *REFLEXES, *REFLEXES.values())
    return [a for a in seen if a["outcome"] not in own], lasting["stepped_up"], lasting["burst"]


async def revert(st: Store, c: Contract, keys: tuple[str, ...], by: str, at: float) -> None:
    """Ask a Revert of the container `keys` names (cluster, namespace, kind, name,
    container), from the console or the Hub MCP server (slice 12.6a); ValueError with
    the reason when it is refused."""
    cl = next((x for x in c.clusters if x.name == keys[0]), None)
    if cl is None or not cl.actuator_token_secret:
        runs = [x.name for x in c.clusters if x.actuator_token_secret]
        raise ValueError(f"cluster {keys[0]!r} runs no Actuator: one of {runs}")
    if keys[1] not in cl.namespaces:
        raise ValueError(f"namespace {keys[1]!r} is not one of {sorted(cl.namespaces)}")
    seen, stepped, burst = await done(st, c.id)
    fields = ("cluster", "namespace", "kind", "name", "container")
    reflexed = keys in stepped or keys in burst
    if not reflexed and not any(tuple(a[k] for k in fields) == keys for a in seen):
        raise ValueError("the Actuator has reported nothing of that container: nothing to revert")
    if await st.ask_revert(c.id, keys, by, at) == "killed":
        raise ValueError(
            "The Contract's Kill switch is on: a Revert is a change, refused. "
            "Turn it off in the console first."
        )


async def automation_of(st: Store, c: Contract, now: float) -> AutomationPage:
    """What each Actuator of the Contract last did with each container (90 days)."""
    seen, stepped, burst = await done(st, c.id)  # newest first
    out = []
    for cl in c.clusters:
        if not cl.actuator_token_secret:
            continue
        ended = actuator.resumes(actuator.rollbacks_of(seen, cl.name, now))
        stopped = await st.reverted(c.id, cl.name)
        reverting = {
            (r["namespace"], r["kind"], r["name"], r["container"])
            for r in await st.pending_reverts(c.id, cl.name)
        }
        last: dict[tuple[str, ...], dict[str, Any]] = {}
        held: dict[tuple[str, ...], str] = {}  # a skip leaves what was held (slice 12.9b)
        for a in seen:
            if a["cluster"] == cl.name and a["namespace"] in cl.namespaces:
                key = (a["namespace"], a["kind"], a["name"], a["container"])
                last.setdefault(key, a)
                if a["outcome"] != "skipped":
                    held.setdefault(key, a["outcome"])
        steps = {k[1:]: a for k, a in stepped.items() if k[0] == cl.name and k[1] in cl.namespaces}
        for where, step in steps.items():  # a container it only stepped up is a row too:
            last.setdefault(where, {**step, "memory": None})  # its limit is `stepped`, no request
        bursts = {k[1:]: a for k, a in burst.items() if k[0] == cl.name and k[1] in cl.namespaces}
        for where, made in bursts.items():  # and one it only burst: its `cpu` is a limit
            last.setdefault(where, {**made, "cpu": None})
        containers = [
            Held(
                **{k: a[k] for k in Held.model_fields if k in a},
                automated=c.sizing.automated(cl.name, *key),
                resumes=ended.get(key[:3]),
                stopped=key in stopped,
                reverting=key in reverting,
                revertable=held.get(key) in ("applied", "failed") or key in steps or key in bursts,
                stepped=(steps.get(key) or {}).get("memory"),
                burst=(bursts.get(key) or {}).get("cpu"),
            )
            for key, a in sorted(last.items())
        ]
        out.append(HeldCluster(cluster=cl.name, containers=containers))
    return AutomationPage(kill_switch=await st.kill_switch(c.id) is not None, clusters=out)


def daily(points: list[Point]) -> list[Point]:
    """The last point of each UTC day."""
    last: dict[int, Point] = {}
    for p in points:
        last[int(p.at // DAY)] = p
    return list(last.values())


async def workloads_of(st: Store, c: Contract, now: float) -> Workloads:
    """The Contract's latest Sizings, ranked: the Workloads page's and the Hub MCP
    server's (slice 11.1a)."""
    c = await listprices.effective(st, c, now)
    names = {t.cluster for t in usage.targets(c)}
    rows, clusters = await st.latest_sizings(c.id, names)
    floors = sizing.floors_report(await st.latest_floors(c.id, names), c)
    return Workloads.model_validate(
        {"contract": c.id, **sizing.report(rows, clusters, c), **floors}
    )


async def allocation_of(
    st: Store, c: Contract, by: str, now: float, compare: int | None
) -> Allocation:
    """Cost by namespace, team or label, compared with `compare` days ago: the Cost
    page's and the Hub MCP server's (slice 11.1b). ValueError: a grouping it can't do."""
    if by not in sizing.groupings(c):
        raise ValueError(f"by is one of {', '.join(sizing.groupings(c))}")
    back = None if compare is None else compare * DAY
    return Allocation.model_validate(await sizing.cost(st, c, by, now, back))


def router(
    operators: set[str],
    by_id: dict[str, Contract],
    store: Callable[[], Store],
    approver_token: str,
    now: Callable[[], float],
    decide: Decide,
    file_jira: FileJira,
    jira_of: Callable[[Investigation], Jira | None],
    feedback: Feedback,
    decide_teaching: DecideTeaching,
    health: Health,
    hub_config: dict[str, Any],
    ask: Ask,
) -> APIRouter:
    """decide, file_jira, jira_of, feedback and decide_teaching: the Jinja console's, so
    both consoles record the same thing (`console:<login>`). health and hub_config: the
    Health page's, and ask a Question's, as the Jinja console's."""
    key = action_key(approver_token)
    r = APIRouter()

    def operator(request: Request) -> str:
        login = request.headers.get("Tailscale-User-Login", "").strip()
        if not login or login not in operators:
            raise HTTPException(403, "not an Operator (sign in through tailscale serve)")
        return login

    def viewer(request: Request, response: Response, login: str = Depends(operator)) -> str:
        response.headers.update(HEADERS)
        if not hmac.compare_digest(request.cookies.get(COOKIE, ""), key):
            raise HTTPException(
                401, {"error": "paste the approver token first", "token_url": TOKEN_PAGE}
            )
        return login

    def changing(request: Request, login: str = Depends(viewer)) -> str:
        """A change, not a read: also from the console's own pages, so another site
        can't make the browser send it (the Jinja console's rule)."""
        if not same_origin(request):
            raise HTTPException(403, "cross-site request refused")
        return login

    def contract(contract_id: str) -> Contract:
        c = by_id.get(contract_id)
        if c is None:
            raise HTTPException(404, "unknown Contract")
        return c

    @r.post("/api/v1/token", status_code=204)
    async def token(given: Token, request: Request) -> Response:
        """The token page's: the identity and the console's own page only, since it is
        what earns the cookie every other call needs."""
        operator(request)
        if not same_origin(request):
            raise HTTPException(403, "cross-site request refused")
        if not hmac.compare_digest(given.token.encode(), approver_token.encode()):
            raise HTTPException(403, "that is not the approver token")
        done = Response(status_code=204, headers=HEADERS)
        set_cookie(done, key)
        return done

    @r.get("/app/", include_in_schema=False)
    async def page(login: str = Depends(operator)) -> HTMLResponse:
        index = APP_DIR / "index.html"
        if not index.is_file():
            raise HTTPException(404, "the console app isn't built into this image")
        return HTMLResponse(index.read_text(), headers={**HEADERS, "Cache-Control": "no-store"})

    @r.get("/app/assets/{name}", include_in_schema=False)
    async def asset(name: str, login: str = Depends(operator)) -> Response:
        path = APP_DIR / "assets" / name
        if not ASSET.fullmatch(name) or not path.is_file():
            raise HTTPException(404, "no such file")
        media = TYPES.get(path.suffix, "application/octet-stream")
        cache = {"Cache-Control": "max-age=31536000, immutable"}  # hashed names
        return Response(path.read_bytes(), media_type=media, headers={**HEADERS, **cache})

    api = APIRouter(prefix="/api/v1", dependencies=[Depends(viewer)])

    @api.get("/contracts")
    async def contracts() -> list[ContractSummary]:
        return [
            ContractSummary(id=c.id, name=c.name)
            for c in sorted(by_id.values(), key=lambda c: c.id)
        ]

    @api.get("/overview")
    async def overview() -> list[ContractOverview]:
        """Every Contract at a glance, by id."""
        return [
            ContractOverview(
                id=c.id,
                name=c.name,
                clusters=[x.name for x in c.clusters],
                operator_channel=c.slack.operator_channel if c.slack else None,
                model=c.model,
                spent_usd=await store().month_spend(c.id, now()),
                budget_usd=c.monthly_budget_usd,
            )
            for c in sorted(by_id.values(), key=lambda c: c.id)
        ]

    @api.get("/contracts/{contract_id}/workloads")
    async def workloads(contract_id: str) -> Workloads:
        return await workloads_of(store(), contract(contract_id), now())

    @api.get("/contracts/{contract_id}/nodes")
    async def nodes(contract_id: str) -> Nodes:
        c = contract(contract_id)
        read = await store().latest_rooms(c.id, {cl.name for cl in c.clusters})
        return Nodes.model_validate({"clusters": sizing.nodes_report(read, c)})

    @api.get("/contracts/{contract_id}/cost")
    async def cost(
        contract_id: str,
        by: str = "namespace",
        compare: int | None = Query(None, ge=1, le=400, description="days ago"),
    ) -> Allocation:
        try:
            return await allocation_of(store(), contract(contract_id), by, now(), compare)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None

    @api.get("/contracts/{contract_id}/billing")
    async def bills(contract_id: str, days: int = Query(7, ge=1, le=billing.KEPT_DAYS)) -> Bills:
        rows = await billing.report(store(), contract(contract_id), now(), days)
        return Bills.model_validate(
            {"tolerance": billing.TOLERANCE, "min_coverage": billing.MIN_COVERAGE, "clusters": rows}
        )

    @api.get("/contracts/{contract_id}/history")
    async def history(
        contract_id: str,
        cluster: str,
        namespace: str,
        kind: str,
        name: str,
        container: str,
        days: int = Query(14, description="14 (hourly) or 400 (daily)"),
    ) -> History:
        if days not in (14, 400):
            raise HTTPException(422, "days is 14 or 400")
        c = contract(contract_id)
        workload = (cluster, namespace, kind, name, container)
        rows = await store().sizing_history(c.id, workload, now() - days * DAY)
        points = [Point.model_validate({"at": at, **row}) for at, row in rows]
        return History(points=daily(points) if days > 14 else points)

    @api.get("/contracts/{contract_id}/agenda")
    async def now_(contract_id: str) -> agenda.Agenda:
        """What to look at now in this Contract, most urgent first (slice 11.4)."""
        return await agenda.of(store(), contract(contract_id), now())

    @api.get("/contracts/{contract_id}/investigations")
    async def investigations(
        contract_id: str, limit: int = Query(100, ge=1, le=500)
    ) -> list[Brief]:
        """Newest first; passing Checks left out, as the Checks list has their last Run."""
        c = contract(contract_id)
        return [Brief.model_validate(brief(t)) for t in await store().recent(limit, c.id)]

    async def investigation_of(investigation_id: str) -> Investigation:
        t = await store().get(investigation_id)
        if t is None or t.contract_id not in by_id:
            raise HTTPException(404, "unknown investigation")
        return t

    def judged(t: Investigation) -> bool:
        return t.kind in ("triage", "check") and t.status == "done" and t.report is not None

    def jira_state(t: Investigation) -> JiraState | None:
        j = jira_of(t)
        if j is None or t.report is None or t.status != "done":
            return None
        url = f"{j.url.rstrip('/')}/browse/{t.jira_issue}" if t.jira_issue else None
        return JiraState(
            project=j.project, issue=t.jira_issue, url=url, by=t.jira_by, since=t.jira_at
        )

    @api.get("/investigations/{investigation_id}")
    async def investigation(investigation_id: str) -> InvestigationPage:
        t = await investigation_of(investigation_id)
        st = store()
        # Each follow-up replaced the Answer before it: the first question (the title)
        # got the first turn's Answer, each follow-up the next one's.
        turns = await st.question_turns(t.id)
        questions = [(t.title, t.started)] + [(x["question"], x["at"]) for x in turns]
        exchange = [
            {"question": q, "at": at, "answer": x["answer"]}
            for (q, at), x in zip(questions, turns, strict=False)
        ]
        return InvestigationPage.model_validate(
            {
                **brief(t),
                "error": t.error,
                "deadline": t.deadline,
                "report": t.report,
                "answer": t.answer,
                "check_result": t.check_result,
                "pending": [str(p.get("description", p.get("name", ""))) for p in t.pending],
                "executed_actions": t.executed_actions,
                "usage": await st.investigation_usage(t.id),
                "masked": await st.masked_counts(t.id),
                "decisions": [
                    {**d, "actions": [a.get("name", "") for a in d["actions"]]}
                    for d in await st.decisions(t.id)
                ],
                "related": await st.related(t.id),
                "tool_calls": [
                    {**c, "result": (c["result"] or "")[:SHOWN]} for c in await st.tool_calls(t.id)
                ],
                "feedback": await st.feedback(t.id),
                "taught": [x for x in await st.teachings(t.contract_id) if x["source_id"] == t.id],
                "scenario": t.status == "done" and t.input is not None,
                "jira": jira_state(t),
                "can_judge": judged(t),
                "can_follow_up": t.kind == "question" and t.status in ("done", "failed"),
                "asked": questions[-1][0] if t.kind == "question" else None,
                "exchange": exchange,
            }
        )

    @api.post("/investigations/{investigation_id}/decision")
    async def decision(
        investigation_id: str, given: DecisionIn, login: str = Depends(changing)
    ) -> InvestigationPage:
        """Approve or reject every Proposed action of the pause the page showed, as
        Slack's buttons do (409 while the Kill switch is on, for an approval)."""
        t = await investigation_of(investigation_id)
        if not await decide(t.id, given.deadline, given.approve, given.reason, login):
            raise HTTPException(409, "already decided or Stale: nothing to decide")
        return await investigation(t.id)

    @api.post("/investigations/{investigation_id}/feedback")
    async def judge(
        investigation_id: str, given: FeedbackIn, login: str = Depends(changing)
    ) -> InvestigationPage:
        """Right, or wrong with what was wrong: the agent drafts a Teaching from it."""
        t = await investigation_of(investigation_id)
        if not judged(t):
            raise HTTPException(409, "feedback is for a finished Report")
        correction = given.correction.strip()
        if not given.correct and not 0 < len(correction) <= CORRECTION_LIMIT:
            raise HTTPException(422, f"say what was wrong, in 1 to {CORRECTION_LIMIT} characters")
        await feedback(t, given.correct, None if given.correct else correction, login)
        return await investigation(t.id)

    def question_text(given: QuestionIn) -> str:
        text = given.question.strip()
        if not 0 < len(text) <= QUESTION_LIMIT:
            raise HTTPException(422, f"ask a question of 1 to {QUESTION_LIMIT} characters")
        return text

    @api.post("/contracts/{contract_id}/questions")
    async def question(
        contract_id: str, given: QuestionIn, login: str = Depends(changing)
    ) -> InvestigationPage:
        """A Question about this Contract: it only reads, and is answered in the
        background (the page shows it running)."""
        c = contract(contract_id)
        asked = await ask(c.id, question_text(given), None)
        if asked is None:
            raise HTTPException(409, "could not start the Question")
        return await investigation(asked)

    @api.post("/investigations/{investigation_id}/questions")
    async def follow_up(
        investigation_id: str, given: QuestionIn, login: str = Depends(changing)
    ) -> InvestigationPage:
        """A follow-up: the same Question, with its earlier exchange."""
        t = await investigation_of(investigation_id)
        if await ask(t.contract_id, question_text(given), t.id) is None:
            raise HTTPException(409, "still answering, or not a Question")
        return await investigation(t.id)

    @api.post("/investigations/{investigation_id}/jira")
    async def jira_issue(
        investigation_id: str, login: str = Depends(changing)
    ) -> InvestigationPage:
        """The Report into the Contract's Jira project, once."""
        t = await investigation_of(investigation_id)
        key, message = await file_jira(t, login)
        if key is None:
            raise HTTPException(409, message)
        return await investigation(t.id)

    @api.get("/investigations/{investigation_id}/scenario.yaml", response_class=Response)
    async def scenario(investigation_id: str) -> Response:
        return await scenario_file(await investigation_of(investigation_id), by_id, store())

    @api.get("/contracts/{contract_id}/teachings")
    async def teachings(contract_id: str) -> Teachings:
        """Lessons for this Contract's future Investigations: only approved ones are
        used, and never by another Contract."""
        rows = await store().teachings(contract(contract_id).id)
        approved = [x for x in rows if x["status"] == "approved"]
        return Teachings.model_validate(
            {
                "limit": TEACHING_LIMIT,
                "left_out": section(approved, None)[1],
                "proposed": [x for x in rows if x["status"] == "proposed"],
                "approved": approved,
                "past": [x for x in rows if x["status"] in ("rejected", "retired")],
            }
        )

    def teaching_text(text: str) -> str:
        text = text.strip()
        if not 0 < len(text) <= TEACHING_LIMIT:
            raise HTTPException(422, f"a Teaching is 1 to {TEACHING_LIMIT} characters")
        return text

    async def teaching_of(c: Contract, teaching_id: int) -> dict[str, Any]:
        row = await store().teaching(teaching_id)
        if row is None or row["contract_id"] != c.id:
            raise HTTPException(404, "unknown Teaching")
        return row

    @api.post("/contracts/{contract_id}/teachings")
    async def write_teaching(
        contract_id: str, given: TeachingIn, login: str = Depends(changing)
    ) -> Teachings:
        """Written by the Operator: approved at once."""
        c = contract(contract_id)
        alerts = [a.strip() for a in given.alerts if a.strip()]
        await store().add_teaching(
            c.id, teaching_text(given.text), alerts, approved_by=f"console:{login}"
        )
        return await teachings(c.id)

    @api.post("/contracts/{contract_id}/teachings/{teaching_id}/decision")
    async def teaching_decision(
        contract_id: str,
        teaching_id: int,
        given: TeachingDecisionIn,
        login: str = Depends(changing),
    ) -> Teachings:
        """Approve a proposed Teaching with the text as edited, or reject it, once."""
        c = contract(contract_id)
        row = await teaching_of(c, teaching_id)
        text = teaching_text(given.text) if given.approve else None
        if not await decide_teaching(row, given.approve, text, login):
            raise HTTPException(409, "already decided")
        return await teachings(c.id)

    @api.post("/contracts/{contract_id}/teachings/{teaching_id}/retire")
    async def retire_teaching(
        contract_id: str, teaching_id: int, login: str = Depends(changing)
    ) -> Teachings:
        c = contract(contract_id)
        row = await teaching_of(c, teaching_id)
        if not await store().retire_teaching(row["id"], c.id, f"console:{login}"):
            raise HTTPException(409, "not an approved Teaching")
        return await teachings(c.id)

    @api.get("/contracts/{contract_id}/checks")
    async def checks(contract_id: str) -> Checks:
        c = contract(contract_id)
        return Checks.model_validate(
            {
                "checks": await store().check_schedules(c.id),
                "deploys": await store().deploy_checks(c.id),
            }
        )

    @api.get("/contracts/{contract_id}/health")
    async def health_(contract_id: str) -> HealthPage:
        c = contract(contract_id)
        last = (await store().last_done()).get(c.id)
        return HealthPage.model_validate(
            {
                "integrations": await health(c),
                "last": brief(last) if last else None,
                # Secret values never live in the model: only their names, and whether
                # each one decrypted.
                "config": yaml.safe_dump(c.model_dump(mode="json"), sort_keys=False),
                "secrets": {name: c.has_secret(name) for name in sorted(c.secret_refs())},
                "spent_usd": await store().month_spend(c.id, now()),
                "budget_usd": c.monthly_budget_usd,
                "hub": redact(yaml.safe_dump(hub_config, sort_keys=False)),  # to be sure
            }
        )

    async def switch(c: Contract) -> KillSwitch:
        on = await store().kill_switch(c.id)
        return (
            KillSwitch(on=False)
            if on is None
            else KillSwitch(on=True, by=on["set_by"], since=on["set_at"])
        )

    @api.get("/contracts/{contract_id}/kill-switch")
    async def kill_switch(contract_id: str) -> KillSwitch:
        return await switch(contract(contract_id))

    @api.post("/contracts/{contract_id}/kill-switch")
    async def turn_on(contract_id: str, login: str = Depends(changing)) -> KillSwitch:
        c = contract(contract_id)
        if await store().turn_on_kill_switch(c.id, f"console:{login}", now()):
            log.info("kill switch %s on by console:%s", c.id, login)
        return await switch(c)

    @api.delete("/contracts/{contract_id}/kill-switch")
    async def turn_off(contract_id: str, login: str = Depends(changing)) -> KillSwitch:
        """The only way a Kill switch goes off: the Operator, in the console."""
        c = contract(contract_id)
        if await store().turn_off_kill_switch(c.id):
            log.info("kill switch %s off by console:%s", c.id, login)
        return await switch(c)

    @api.get("/contracts/{contract_id}/automation")
    async def automation(contract_id: str) -> AutomationPage:
        return await automation_of(store(), contract(contract_id), now())

    @api.post("/contracts/{contract_id}/automation/revert")
    async def revert_(
        contract_id: str, given: RevertIn, login: str = Depends(changing)
    ) -> AutomationPage:
        """The Operator's Revert of one container (slice 12.6b), as `revert_sizing`."""
        c = contract(contract_id)
        keys = (given.cluster, given.namespace, given.kind, given.name, given.container)
        try:
            await revert(store(), c, keys, f"console:{login}", now())
        except ValueError as e:
            raise HTTPException(409, str(e)) from None
        log.info("revert %s %s by console:%s", c.id, "/".join(keys), login)
        return await automation_of(store(), c, now())

    r.include_router(api)
    return r


def openapi() -> str:
    """The API's schema, as console/openapi.json holds it (the app's types come from
    it: `make console-types`)."""
    app = FastAPI(title="squero console", version="1")
    never: Any = None  # the schema needs no behaviour
    app.include_router(
        router(set(), {}, never, "x", never, never, never, never, never, never, never, {}, never)
    )
    return json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"
