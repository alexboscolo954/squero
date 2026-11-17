"""The Agenda (slice 11.4): what the Operator should look at now in one Contract, from
what the Hub already stores, ranked without a model in a fixed order of urgency: the
Kill switch while it is on, pauses waiting for an Approval decision (they go Stale),
failing Checks, data the Hub can't read (an offline Relay too), Bills outside their
tolerance, running Investigations, and the Workloads whose waste costs most. Audit A6b:
one with a current Approved Sizing on a cluster that runs an Actuator leaves that ranking,
counted on one line last. The Hub MCP server's `now` and the console app's Workloads page
show it.
"""

from collections import Counter
from math import inf
from typing import Literal

from pydantic import BaseModel

from squero import billing, relay, usage
from squero.contracts import Contract
from squero.sizing import show
from squero.store import Store

Kind = Literal[
    "kill_switch", "approval", "check", "stale", "bill", "investigation", "waste", "approved"
]
WASTE_TOP = 3


class Item(BaseModel):
    kind: Kind
    title: str
    at: float | None = None  # a pause's deadline; when it started failing, ran or went on
    id: str | None = None  # the Investigation to open
    cluster: str | None = None
    workload: str | None = None  # cluster/namespace/kind/name


class Agenda(BaseModel):
    contract: str
    items: list[Item]


async def of(st: Store, c: Contract, now: float) -> Agenda:
    from squero import webapp  # webapp serves this: imported here, not in a cycle

    items: list[Item] = []
    if on := await st.kill_switch(c.id):
        items.append(
            Item(
                kind="kill_switch",
                title=f"The Kill switch is on ({on['set_by']}): approving is refused",
                at=on["set_at"],
            )
        )
    mine = [t for t in await st.unfinished() if t.contract_id == c.id]
    paused = [t for t in mine if t.status == "awaiting_approval"]
    for t in sorted(paused, key=lambda t: t.deadline or inf):
        n = len(t.pending)
        items.append(
            Item(
                kind="approval",
                title=f"{n} Proposed action{'s' * (n != 1)} wait{'s' * (n == 1)} for a "
                f"decision: {t.title}",
                at=t.deadline,
                id=t.id,
            )
        )
    # failing as the Hub's Slack thread counts it: a verdict of no, or a Run that errored
    failing = [
        s
        for s in await st.check_schedules(c.id)
        if s["last_passed"] is False or s["last_status"] == "failed"
    ]
    for s in sorted(failing, key=lambda s: -(s["last_started"] or 0)):
        what = "failed" if s["last_passed"] is False else "could not run"
        items.append(
            Item(
                kind="check",
                title=f"Check {s['name']} {what}",
                at=s["last_started"],
                id=s["last_id"],
            )
        )
    w = await webapp.workloads_of(st, c, now)
    stale = [
        Item(kind="stale", title=f"Sizing reads failing: {s.error}", at=s.since, cluster=k)
        for k, s in w.clusters.items()
        if s.error
    ]
    for cl in (cl for cl in c.clusters if cl.via == "relay"):  # slice 3.3b
        ok, since, text = relay.HUB.state(c.id, cl.name)
        if not ok:
            title = f"Relay {text}: the Hub can't read cluster {cl.name}"
            stale.append(Item(kind="stale", title=title, at=since, cluster=cl.name))
    names = {t.cluster for t in usage.targets(c)}
    _, tries = await st.bills(c.id)
    stale += [
        Item(kind="stale", title=f"Bill reads failing: {t['error']}", at=t["tried_at"], cluster=k)
        for k, t in tries.items()
        if t["error"] and k in names
    ]
    items += sorted(stale, key=lambda i: i.at or inf)
    bills = await billing.report(st, c, now, 7)
    outside = [b for b in bills if b["within"] is False]
    for b in sorted(outside, key=lambda b: -abs(b["gap"])):
        items.append(
            Item(
                kind="bill",
                title=f"Bill {b['gap']:+.1%} from the estimate ({b['start']} to {b['end']}), "
                f"outside {billing.TOLERANCE:.0%}",
                cluster=b["cluster"],
            )
        )
    for t in (t for t in mine if t.status == "running"):  # oldest first, as stored
        items.append(Item(kind="investigation", title=f"Running: {t.title}", at=t.started, id=t.id))
    # its waste is read again at the next hourly run, which supersedes the approval
    actuated = {cl.name for cl in c.clusters if cl.actuator_token_secret}
    approved = {
        "/".join(a[k] for k in ("cluster", "namespace", "kind", "name"))
        for a in await st.approved_sizings(c.id)
        if a["cluster"] in actuated
    }
    wasting = [x for x in w.workloads if x.waste.cpu or x.waste.memory]
    held = sum(x.workload in approved for x in wasting)
    for x in [x for x in wasting if x.workload not in approved][:WASTE_TOP]:
        size = f"{show('cpu', x.waste.cpu)} and {show('memory', x.waste.memory)}"
        money = f"{x.cost.waste:.2f} {w.currency} a month, " if x.cost and w.currency else ""
        items.append(
            Item(
                kind="waste",
                title=f"Waste: {money}{size} above its Sizings",
                cluster=x.cluster,
                workload=x.workload,
            )
        )
    if held:
        its = "their waste is" if held > 1 else "its waste is"
        title = f"{held} Workload{'s' * (held > 1)} approved: {its} read again within the hour"
        items.append(Item(kind="approved", title=title))
    return Agenda(contract=c.id, items=items)


def counts(a: Agenda) -> dict[str, int]:
    """How many items of each kind, the line of approved Workloads not one (it counts
    Workloads, not something to look at)."""
    return dict(Counter(i.kind for i in a.items if i.kind != "approved"))
