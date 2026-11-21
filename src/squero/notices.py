"""Stale notices (audit A14): when one of a Contract's reads has failed for STALE_AFTER
without a break (a cluster's Sizing reads, its Relay, its Bill reads), the Hub posts once
to the Contract's operator channel, reminds in that thread a day while it fails, and
replies there when it reads again. `failing` is the Agenda's source too, so the Now panel
and the notices always agree. Each post is claimed in Postgres before it is sent: a
restart never repeats one. A crash between a claim and its post delays that post a day,
and a recovery reply Slack refuses is not tried again (its row is gone with it)."""

import logging
from collections.abc import Awaitable, Callable
from typing import Literal, NamedTuple

from squero import relay, usage
from squero.contracts import Contract
from squero.sizing import utc
from squero.slack import clean
from squero.store import Store

log = logging.getLogger(__name__)

STALE_AFTER = 3 * 3600.0  # three missed hourly runs
REMIND_EVERY = 24 * 3600.0
Kind = Literal["sizing", "relay", "bill"]
Post = Callable[[str, str, str | None], Awaitable[str | None]]  # channel, text, thread: ts


class Failing(NamedTuple):
    """One read of a Contract that fails now."""

    cluster: str
    kind: Kind
    since: float | None  # when it began, where the read says; None: not known (a Bill's)
    at: float | None  # what the Agenda shows it by: `since`, or a Bill's last try
    error: str


def read(c: Contract) -> dict[str, set[str]]:
    """The clusters the Hub reads for the Contract, by kind of read."""
    names = {t.cluster for t in usage.targets(c)}
    return {
        "sizing": names,
        "bill": names,
        "relay": {cl.name for cl in c.clusters if cl.via == "relay"},
    }


async def failing(st: Store, c: Contract) -> list[Failing]:
    """The Contract's reads that fail now: each cluster's Sizing reads, its Relays that
    are offline, and its Bill reads (of a cluster it sizes)."""
    names = read(c)["sizing"]
    _, status = await st.latest_sizings(c.id, names)
    out = [
        Failing(k, "sizing", s["since"], s["since"], s["error"])
        for k, s in status.items()
        if s["error"]
    ]
    for cl in (cl for cl in c.clusters if cl.via == "relay"):  # slice 3.3b
        ok, since, text = relay.HUB.state(c.id, cl.name)
        if not ok:
            out.append(Failing(cl.name, "relay", since, since, text))
    _, tries = await st.bills(c.id)
    out += [
        Failing(k, "bill", None, t["tried_at"], t["error"])
        for k, t in tries.items()
        if t["error"] and k in names
    ]
    return out


def _span(seconds: float) -> str:
    """3 h, or 5 days 8 h: what a person reads at a glance."""
    hours = int(seconds // 3600)
    days, hours = divmod(hours, 24)
    if not days:
        return f"{hours} h"
    return f"{days} day{'s' if days != 1 else ''}" + (f" {hours} h" if hours else "")


WHAT = {"sizing": "Sizing reads", "relay": "its Relay", "bill": "Bill reads"}


def first(c: Contract, f: Failing, since: float, now: float, link: str | None) -> str:
    """The notice: the Contract, the cluster, what fails, since when, and why."""
    head = f":warning: *[{clean(c.id)}]* {clean(f.cluster)}: "
    when = f"since {utc(since)} ({_span(now - since)})"
    if f.kind == "relay":
        body = f"its Relay is offline {when}: the Hub can't read this cluster"
    else:
        body = f"{WHAT[f.kind]} failing {when}: {clean(f.error)}"
    return head + body + (f"\n<{link}|Open the Now panel>" if link else "")


def reminder(f: Failing, since: float, now: float) -> str:
    error = "" if f.kind == "relay" else f": {clean(f.error)}"
    return f"Still failing, {_span(now - since)} now{error}"


def recovered(cluster: str, kind: Kind, since: float, now: float) -> str:
    did = "is connected again" if kind == "relay" else "work again"
    what = "Its Relay" if kind == "relay" else WHAT[kind]
    return f":white_check_mark: {clean(cluster)}: {what} {did}, after {_span(now - since)}"


async def tick(st: Store, c: Contract, now: float, post: Post, console_url: str | None) -> None:
    """One look at the Contract's reads: note each new failure, post a notice once one
    has lasted STALE_AFTER, remind a day later in its thread, and reply when it reads
    again. Nothing without the Contract's Slack channel (its Agenda still shows them)."""
    if c.slack is None:
        return
    channel = c.slack.operator_channel
    link = f"{console_url.rstrip('/')}/app/?contract={c.id}" if console_url else None
    now_failing = {(f.cluster, f.kind): f for f in await failing(st, c)}
    reads = read(c)
    for f in now_failing.values():
        await st.note_stale(c.id, f.cluster, f.kind, f.since if f.since is not None else now)
    for row in await st.stale_notices(c.id):
        key = (row["cluster"], row["kind"])
        still = now_failing.get(key)
        if still is None:  # it reads again: a reply in its thread, if one was posted
            ended = await st.end_stale(c.id, *key)
            # a cluster no longer read (gone from the Contract) ends quietly: not "again"
            if ended and row["ts"] and key[0] in reads.get(key[1], set()):
                await post(channel, recovered(*key, row["since"], now), row["ts"])
            continue
        if now - row["since"] < STALE_AFTER:
            continue
        due = now - REMIND_EVERY  # a reminder a day; the first notice whenever it is due
        if row["ts"] is None:
            text, thread = first(c, still, row["since"], now, link), None
        else:
            text, thread = reminder(still, row["since"], now), row["ts"]
        if not await st.claim_stale(c.id, *key, now, due):
            continue
        ts = await post(channel, text, thread)
        # a post Slack didn't take: undone, so the next look tries again
        await st.told_stale(c.id, *key, ts, now if ts else row["told_at"])
        log.info("stale notice %s/%s %s: %s", c.id, key[0], key[1], "posted" if ts else "not")
