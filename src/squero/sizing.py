"""Sizings (slices 9.2, 9.3): the requests each Workload container should have, read-only.

A pure function of one container's pooled usage (usage.py), the time and its Sizing
policy (contracts.py; the numbers below are `production`'s):

- CPU: the 95th percentile, recent samples weighted more (half-life 24 h, like VPA),
  plus 15% headroom; a DaemonSet's, its busiest node's own (audit A9). A container
  that is throttled hides its real demand, so its CPU is never lowered while it is.
- Memory: the peak over the window, plus 20%. Weighting can't move a maximum: memory
  follows a drop only once the old peak leaves the window. After an OOM kill the peak
  is the limit, not the demand, so memory is never lowered then.
- Then the floors, then the policy's max (if any), then the limit: limits are kept, a
  request never goes above one, and a cap or a limit below what the container needs
  is flagged as risk. Then rounding up to a millicore or a MiB.
- A request changes only when the difference is at least 10% and at least 10m CPU or
  16 MiB; a container without one always gets one.
- Under 24 hours of history a container is still learning and gets no Sizing. Its
  history is covered time (slice 9.7): a gap in Prometheus counts for nothing.

Each risk has a kind, for counting and ranking (slice 9.4). Waste is the request
above the Sizing, times the average pods over the container's history. Each week the
Operator gets a digest of it (slice 9.5b).
"""

import asyncio
import bisect
import math
import statistics
import zoneinfo
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

from squero import blockers, listprices, usage
from squero.contracts import (
    BATCH_KINDS,
    BUILTIN,
    SPOT_KINDS,
    ClusterPrices,
    Contract,
    Policy,
    SpotRule,
    on_demand_of,
)
from squero.slack import clean
from squero.usage import NEEDS_FOR, Bounds, ContainerUsage, Divides, Workload

if TYPE_CHECKING:
    from squero.store import Store

PRODUCTION = BUILTIN["production"]
HALF_LIFE_S = PRODUCTION.half_life
MIN_CHANGE = {"cpu": 0.010, "memory": 16 * 2**20}
THROTTLED = 0.10  # share of CFS periods throttled above which CPU demand is hidden
BURST_OVER = 0.25  # share of the last hour's CFS periods throttled above which a Burst is due
BURST_FRESH = 7200  # a read older than this triggers no Burst, and its peak is not known

Action = Literal["set", "raise", "lower", "keep"]
RiskKind = Literal[
    "oom_killed",
    "throttled",
    "under_requested",
    "limit_below_need",
    "capped_by_policy",
    "request_unset",
    "needs_larger_node",  # slice 15.1a
]
DERIVED = ("under_requested", "request_unset")  # the action says it: `sizing show` omits them


class Risk(NamedTuple):
    kind: RiskKind
    text: str


@dataclass
class Resource:
    current: float | None  # the request now: cores or bytes, None when unset
    target: float  # what the request should be (current when kept)
    action: Action
    basis: float  # the usage figure it came from: CPU p95 or memory peak


@dataclass
class Sizing:
    workload: Workload
    container: str
    state: Literal["ready", "learning"]
    history_s: float  # covered time, oldest to newest pooled sample (slice 9.7)
    cpu: Resource | None = None
    memory: Resource | None = None
    risks: list[Risk] = field(default_factory=list)


def weighted_percentile(
    samples: list[tuple[float, float]], q: float, now: float, half_life_s: float = HALF_LIFE_S
) -> float:
    """The q-th percentile of the values, each weighted 0.5 ** (age / half-life). With
    equal ages it is the nearest-rank percentile."""
    ordered = sorted((v, 0.5 ** (max(0.0, now - t) / half_life_s)) for t, v in samples)
    total = sum(w for _, w in ordered)
    running = 0.0
    for v, w in ordered:
        running += w
        if running >= q * total - 1e-12 * total:
            return v
    return ordered[-1][0]


def _round_up(resource: str, value: float) -> float:
    if resource == "cpu":
        return math.ceil(round(value * 1000, 6)) / 1000
    return float(math.ceil(round(value / 2**20, 6)) * 2**20)


def _round_down(resource: str, value: float) -> float:
    if resource == "cpu":
        return math.floor(round(value * 1000, 6)) / 1000
    return float(math.floor(round(value / 2**20, 6)) * 2**20)


def show(resource: str, value: float) -> str:
    return f"{value * 1000:.0f}m" if resource == "cpu" else f"{value / 2**20:.0f}Mi"


def _resource(
    resource: str,
    basis: float,
    u: ContainerUsage,
    risks: list[Risk],
    hold: Risk | None,
    policy: Policy,
    room: float | None = None,
) -> Resource:
    """headroom, floor, max, limit, the largest node's Room, rounding, then the change
    threshold. `hold`: why this resource may not be lowered (its usage hides the real
    demand). `room` (slice 15.1a): the most any node of its cluster has after its
    DaemonSets; it only ever stops a raise, never below the request it has now."""
    p = policy.cpu if resource == "cpu" else policy.memory
    target = max(basis * (1 + p.headroom), p.floor)
    if p.max is not None and target > p.max:
        risks.append(
            Risk(
                "capped_by_policy",
                f"{resource} capped at {show(resource, p.max)} by the policy: it needs "
                f"{show(resource, _round_up(resource, target))}",
            )
        )
        target = p.max
    limit = u.limits.get(resource)
    if limit is not None and target > limit:
        risks.append(
            Risk(
                "limit_below_need",
                f"{resource} limit {show(resource, limit)} is below the "
                f"{show(resource, _round_up(resource, target))} it needs",
            )
        )
        target = limit
    current = u.requests.get(resource)
    if room is not None:
        # rounded down, so the rounding up below stays within it; a request above it
        # (its node cordoned, or gone from this read) is no reason to lower one
        most = _round_down(resource, room)
        room = max(most, current or 0.0)
        if target > room:
            needs = f"it needs {show(resource, _round_up(resource, target))}, so a larger node"
            text = (
                f"{resource} kept at {show(resource, room)}, though no node has room for "
                f"more than {show(resource, most)} after its DaemonSets: {needs}"
                if room > most
                else f"{resource} capped at {show(resource, room)}, the most a node has "
                f"room for after its DaemonSets: {needs}"
            )
            risks.append(Risk("needs_larger_node", text))
            target = room
    target = _round_up(resource, target)
    if current is None:
        return Resource(None, target, "set", basis)
    if abs(target - current) < max(policy.threshold * current, MIN_CHANGE[resource]):
        return Resource(current, current, "keep", basis)
    if target < current and hold:
        risks.append(hold)
        return Resource(current, current, "keep", basis)
    return Resource(current, target, "raise" if target > current else "lower", basis)


def size(
    u: ContainerUsage,
    now: float,
    policy: Policy = PRODUCTION,
    history: float | None = None,
    room: dict[str, float] | None = None,
) -> Sizing:
    """The Sizing for one container, or `learning` under the policy's history:
    `history` as its cluster covers it (slice 9.7), else the container's own span, a
    container on its own having no cluster to tell a gap by. `room`: its cluster's
    largest Room after DaemonSets (largest_room), which caps it; none, no cap."""
    room = room or {}
    times = [t for t, _ in u.cpu + u.memory]
    if history is None:
        history = max(times) - min(times) if times else 0.0
    s = Sizing(u.workload, u.container, "learning", history)
    if history < policy.history:
        return s
    s.state = "ready"
    throttled = u.throttled is not None and u.throttled > THROTTLED
    if throttled:
        assert u.throttled is not None
        s.risks.append(
            Risk(
                "throttled",
                f"throttled {u.throttled:.0%} of CPU periods: its real CPU demand is higher",
            )
        )
    if u.cpu:
        hold = (
            Risk("throttled", "CPU kept: throttling hides how much it needs") if throttled else None
        )
        # audit A9: a DaemonSet's, its busiest node's
        basis = max(
            weighted_percentile(c, policy.cpu.percentile, now, policy.half_life)
            for c in (u.nodes.values() if u.nodes else [u.cpu])
        )
        s.cpu = _resource("cpu", basis, u, s.risks, hold, policy, room.get("cpu"))
    if u.memory:
        hold = None
        if u.oom_killed_pods:
            s.risks.append(
                Risk(
                    "oom_killed",
                    f"OOM-killed in {u.oom_killed_pods} pods: its peak is its limit, "
                    "not its demand",
                )
            )
            hold = Risk("oom_killed", "memory kept: OOM kills hide how much it needs")
        peak = max(v for _, v in u.memory)
        s.memory = _resource("memory", peak, u, s.risks, hold, policy, room.get("memory"))
    return s


def bursting(row: dict[str, Any], age: float | None) -> dict[str, float | None]:
    """What the burst Reflex goes by for a stored row's container (slice 13.3a), its read
    `age` seconds old: `throttled`, its share of the hour's CPU periods when over
    `BURST_OVER` (a Burst is due), else None; and `peak`, its highest CPU use (cores, a
    5-minute rate of one pod) over the Contract's `expires`. Both None from a read older
    than `BURST_FRESH`, or never made: an hour long past is no reason to raise a limit."""
    kept = row.get("burst") or {}
    if age is None or age > BURST_FRESH:
        return {"throttled": None, "peak": None}
    share = kept.get("throttled")
    over = share is not None and share > BURST_OVER
    return {"throttled": share if over else None, "peak": kept.get("peak")}


def pod_times(u: ContainerUsage) -> list[float]:
    """A time per pod per step: Running pods from kube-state-metrics, else (no phase
    series) its CPU samples, which outlive a pod by `rate`'s 5 minutes."""
    return u.running or [t for t, _ in u.cpu or u.memory]


def avg_pods(u: ContainerUsage, seen: list[float], over: ContainerUsage | None = None) -> float:
    """Pods per step from the container's first pod time to its last, over the steps
    its cluster has any (`seen`, sorted): a CronJob that runs an hour a day averages a
    twenty-fourth of its pods, as its requests reserve, and a gap in Prometheus (a
    scrape missed by every pod) counts for no one. `over`: the container whose span to
    average over (an instance type's pods over their container's, so the types add up
    to it: slice 15.3c)."""
    times = pod_times(u)
    if not times:
        return 0.0
    edge = pod_times(over) or times if over is not None else times
    steps = bisect.bisect_right(seen, max(edge)) - bisect.bisect_left(seen, min(edge))
    return len(times) / steps


def history(u: ContainerUsage, seen: list[float], step: int) -> float:
    """A container's history (slice 9.7): the covered time from its first usage sample
    to its last, a `step` for each time its cluster has any container's usage (`seen`,
    sorted). A gap in Prometheus counts for nothing, so an hour of data after three
    days without is an hour; a CronJob between its runs still counts. One step fewer
    than the times: with no gap, the span between the two."""
    times = [t for t, _ in u.cpu + u.memory]
    if not times:
        return 0.0
    steps = bisect.bisect_right(seen, max(times)) - bisect.bisect_left(seen, min(times))
    return (steps - 1) * step


def held(
    res: str,
    w: Workload,
    container: str,
    divides: list[Divides],
    asked: dict[str, dict[str, float]],
) -> dict[str, Any] | None:
    """A request its HPA divides by (slice 14.3): the HPA, its percentage, the same
    threshold as an absolute value (the percentage of the pod's requests as they are
    now, every container's summed; of one container's for a per-container metric) and
    the manifest change that frees the request, or None when no HPA divides by it.
    `asked`: each container of the Workload's requests. A `container` label that names
    none of them is the scrape target's own, and the metric the whole pod's."""
    for d in divides:
        own = d.container if d.container in asked else ""
        if d.resource != res or own not in ("", container):
            continue
        of = sum(r.get(res) or 0.0 for name, r in asked.items() if own in ("", name))
        average = d.percent / 100 * of if of else None
        change = None
        if average is not None:
            whose = f"container {own}'s {res} request" if own else f"the pod's {res} requests"
            why = [
                f"# {d.percent:g}% of {whose} now ({show(res, of)}), as a value.",
                "# Apply it before any request changes: a request cut first scales it out.",
            ]
            if d.hpa.startswith("keda-hpa-"):  # KEDA's name for the HPA of a ScaledObject
                what = f"# ScaledObject {w.namespace}/{d.hpa.removeprefix('keda-hpa-')} (KEDA)"
                lines = [f"{what}, its {res} trigger:", *why, f"- type: {res}"]
                lines += ["  metricType: AverageValue", "  metadata:"]
                lines += [f'    value: "{show(res, average)}"']
                lines += [f'    containerName: "{own}"'] if own else []
            else:
                what = f"# HorizontalPodAutoscaler {w.namespace}/{d.hpa}, its {res} metric"
                lines = [what + (f" of container {own}:" if own else ":"), *why, "target:"]
                lines += ["  type: AverageValue", f"  averageValue: {show(res, average)}"]
            change = "\n".join(lines)
        return {"hpa": d.hpa, "percent": d.percent, "average": average, "change": change}
    return None


def largest_room(found: dict[str, Any] | None) -> dict[str, float] | None:
    """The most a pod could ask of any node of a cluster (slice 15.1a): per resource,
    the largest Room after its DaemonSets among the nodes not cordoned, tainted ones
    included (a Workload's tolerations can't be seen: too generous changes nothing, too
    strict would shrink a correct Sizing). None, so no cap, for a cluster not read, one
    with a problem, or a read missing a node's allocatable: half a read is no ceiling."""
    nodes = (found or {}).get("nodes") or []
    if not found or found.get("problem"):
        return None
    if any(res not in n["allocatable"] for n in nodes for res in ("cpu", "memory")):
        return None
    open_ = [n for n in nodes if not n["cordoned"]]
    if not open_:
        return None
    return {
        res: max(n["allocatable"][res] - n["daemonsets"].get(res, 0.0) for n in open_)
        for res in ("cpu", "memory")
    }


def _worth(
    names: list[str], read: dict[str, Any], p: ClusterPrices | None, rate: dict[str, float]
) -> dict[str, Any]:
    """Nodes that could go with what they are: their capacity (allocatable where it
    wasn't read), and a month of it at the cluster's unit prices when it has them, each
    node times its `rate` (slice 16.1a: its own price against the cluster's, a spot
    node's below an on-demand one's; 1 for a node not in it)."""
    by = {n["node"]: n for n in read["nodes"]}

    def held(n: str) -> dict[str, float]:
        return {
            res: (by[n].get("capacity") or {}).get(res, by[n]["allocatable"][res])
            for res in ("cpu", "memory")
        }

    total = {res: sum(held(n)[res] for n in names) for res in ("cpu", "memory")}
    cost = sum(monthly(p, **held(n)) * rate.get(n, 1.0) for n in names) if p else None
    return {"nodes": names, **total, "cost": cost}


def rates(read: dict[str, Any], c: Contract, cluster: str) -> dict[str, float]:
    """Each node's price against its cluster's (slice 16.1a): the cluster's price is
    the share of its nodes all on-demand that its pricing applied (`c._spot_shares`), so
    an on-demand node costs it over that share and a spot node its own rate over it (a
    spot node this read shows and pricing's didn't: its rate against the on-demand price
    applied). Empty where the cluster's spot has no price, or this read wasn't made with
    the label: every node at the cluster's price."""
    cl = next(x for x in c.clusters if x.name == cluster)
    applied = c._spot_shares.get(cluster)
    if cl.spot is None or applied is None or read.get("spot") != cl.spot.label:
        return {}
    share, spot = applied
    return {n["node"]: (spot if n.get("spot") else 1.0) / share for n in read["nodes"]}


WHAT_IF = SpotRule(ratio=0.7, on_demand=2)  # a candidate's (slice 16.1c)
# slice 16.3a: why an Actuator doesn't steer a CronJob's runs, by the code it reports
SKIPS = {
    "no_retry": "its Jobs allow no retry (backoffLimit: 0)",
    "ignores_disruptions": (
        "its Jobs ignore disruptions (podFailurePolicy): a retry can't be told from a first try"
    ),
}
SIDES = ("on_demand", "spot")
MAX_CANDIDATES = 20  # shown per cluster, by saving; the total counts every one


def spot_split(replicas: int, rule: SpotRule) -> tuple[int, int]:
    """A Workload's replicas meant for on-demand and for spot nodes under a Spot ratio
    (slice 16.1c): on-demand the larger of its On-demand minimum and the share that isn't
    spot, rounded up (rounded first: 10 * 0.3 is 3.0000000000000004), never more than
    there are."""
    on_demand = on_demand_of(replicas, rule.ratio, rule.on_demand)
    return on_demand, replicas - on_demand


def _live(read: dict[str, Any]) -> dict[tuple[str, str, str], list[bool]]:
    """Each Workload's live pods in a read of the nodes, whether each runs on spot."""
    out: dict[tuple[str, str, str], list[bool]] = {}
    for n in read.get("nodes") or []:
        for p in n["own"]:
            if w := p.get("workload"):
                out.setdefault((p["namespace"], w["kind"], w["name"]), []).append(
                    bool(n.get("spot"))
                )
    return out


def blocking_of(
    read: dict[str, Any], report: dict[str, Any] | None, cl: Any, now: float
) -> set[tuple[str, str]]:
    """The Workloads (namespace, `Kind/name`) whose pods block a node by its cluster's
    Actuator's latest usable report (slice 16.1c), never Prometheus' guess: none without
    one. They get no replicas on spot."""
    if report is None or blockers.gap(report, cl, now):
        return set()
    return {
        (b["namespace"], b["workload"])
        for v in blockers.verdicts(read, report, cl, now).values()
        for b in v["blockers"]
        if b.get("workload")
    }


def spotted(
    read: dict[str, Any], c: Contract, cluster: str, blocking: set[tuple[str, str]]
) -> list[dict[str, Any]]:
    """The live Workloads its Actuator steers (slice 16.2a): in the cluster's namespaces
    now (the read may be an hour older than the Contract), under a Spot ratio their
    cluster can place, of a kind that can, each with its ratio and minimum; one that
    blocks a node with no share on spot (ratio 0: every replica kept off spot nodes, the
    Operator's choice)."""
    cl = next(x for x in c.clusters if x.name == cluster)
    out = []
    for ns, kind, name in sorted(_live(read)):
        rule = c.spot_for(cluster, ns, kind, name)
        if isinstance(rule, SpotRule) and kind in SPOT_KINDS and ns in cl.namespaces:
            held = (ns, f"{kind}/{name}") in blocking
            out.append(
                {"namespace": ns, "kind": kind, "name": name, "on_demand": rule.on_demand}
                | {"ratio": 0.0 if held else rule.ratio}
            )
    return out


def cronjobs_of(read: dict[str, Any]) -> list[tuple[str, str]]:
    """The Contract's CronJobs a read of the nodes saw (slice 16.3a): every one listed
    (`kube_cronjob_info`), running or not, and any whose run's pods it saw; none in a
    read from before 16.3a, but those running."""
    seen = {(ns, name) for ns, name in read.get("cronjobs") or []}
    seen |= {(ns, name) for ns, kind, name in _live(read) if kind == "CronJob"}
    return sorted(seen)


def spot_runs(
    read: dict[str, Any], c: Contract, cluster: str, blocking: set[tuple[str, str]]
) -> list[str]:
    """The CronJobs whose runs its Actuator steers (slice 16.3a), `namespace/name`: under
    a rule's `spot: true` their cluster can place, in its namespaces now; one whose run
    blocks a node too (the Operator's choice: a run blocks only while it runs, which the
    Hub sees only then, and a spot node it holds is the cloud's to take anyway). Their
    Jobs allowing a retry is the Actuator's to read."""
    cl = next(x for x in c.clusters if x.name == cluster)
    return [
        f"{ns}/{name}"
        for ns, name in cronjobs_of(read)
        if c.spot_for(cluster, ns, "CronJob", name) is True and ns in cl.namespaces
    ]


def spot_nodes(read: dict[str, Any], label: str) -> list[str] | None:
    """The nodes a read shows carrying the cluster's spot `label` (slice 16.2a); None
    where it was made with another one, or none: then which they are isn't known."""
    if read.get("spot") != label:
        return None
    return [n["node"] for n in read["nodes"] if n.get("spot")]


def spot_workloads(
    read: dict[str, Any],
    c: Contract,
    cluster: str,
    blocking: set[tuple[str, str]],
    spotting: bool = False,
    skipped: dict[str, str] | None = None,
    runs: bool = False,
) -> dict[str, Any] | None:
    """Each live Workload under a Spot ratio its cluster can place (slice 16.1c): its
    replicas meant for each kind of node (`intended`) and where they run (`actual`, None
    in a read made without the cluster's spot label), or `why` it gets none on spot: a
    kind that can't, fewer than 2 replicas, or a node it blocks (`blocking`, by
    namespace and `Kind/name`). Once its Actuator places them (`spotting`, slice 16.2a),
    a `risk` where fewer run on-demand than its minimum: restarting it repairs that.
    A CronJob under `spot: true` (slice 16.3a), running or not: its live pods by side and
    no intended split, nor any risk (a run falling back to on-demand is the design), or
    why its runs aren't on spot: as its Actuator reported (`skipped`, `namespace/name` to
    a code), or that it doesn't steer runs yet (`runs`, its report's `spot_runs`).
    None where no Workload is under one."""
    cl = next(x for x in c.clusters if x.name == cluster)
    known = cl.spot is not None and read.get("spot") == cl.spot.label
    live = _live(read)
    for ns, name in cronjobs_of(read):
        live.setdefault((ns, "CronJob", name), [])
    out = []
    for (ns, kind, name), on in sorted(live.items()):
        rule = c.spot_for(cluster, ns, kind, name)
        if rule is None:
            continue
        n = len(on)
        sides_ = (on.count(False), on.count(True))
        if kind == "CronJob":
            code = (skipped or {}).get(f"{ns}/{name}")
            why = (
                "a CronJob runs on spot only by a rule naming kind: CronJob, spot: true"
                if rule is not True
                else SKIPS.get(code, code)
                if code
                else None
                if runs
                else "its Actuator doesn't steer its runs yet"
            )
            out.append(
                {
                    **{"namespace": ns, "kind": kind, "name": name, "replicas": n},
                    **{"ratio": None, "on_demand": None, "why": why, "intended": None},
                    "actual": dict(zip(SIDES, sides_, strict=True)) if known else None,
                    "risk": None,
                }
            )
            continue
        assert rule is not True  # a rule with `spot: true` names kind: CronJob
        why = None
        if kind not in SPOT_KINDS:
            why = f"a {kind} never runs on spot"
        elif (ns, f"{kind}/{name}") in blocking:
            why = "it blocks a node: none on spot"
        elif n < 2:
            why = "fewer than 2 replicas: none on spot"
        meant = spot_split(n, rule) if why is None else (n, 0)
        # below its minimum, not its share: pods from before it was steered are no risk;
        # every replica where all are meant on-demand (it blocks a node, or has no more
        # replicas than its minimum)
        every = why is not None or n <= rule.on_demand
        least = n if every else rule.on_demand
        risk = None
        if spotting and known and kind in SPOT_KINDS and sides_[0] < least:
            risk = (
                f"{sides_[0]} of its {n} on-demand, where all are meant to be"
                if every
                else f"{sides_[0]} on-demand, below its minimum of {rule.on_demand}"
            ) + ": restart it to repair"
        out.append(
            {
                **{"namespace": ns, "kind": kind, "name": name, "replicas": n},
                **{"ratio": rule.ratio, "on_demand": rule.on_demand, "why": why},
                "intended": dict(zip(SIDES, meant, strict=True)) if kind in SPOT_KINDS else None,
                "actual": dict(zip(SIDES, sides_, strict=True)) if known else None,
                "risk": risk,
            }
        )
    if not out:
        return None
    return {"workloads": out, "note": None if spotting else "its Actuator doesn't place them yet"}


def candidates(
    read: dict[str, Any],
    c: Contract,
    cluster: str,
    rows: list[dict[str, Any]],
    blocking: set[tuple[str, str]],
    skipped: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """Where a cluster's Workloads could run on spot (slice 16.1c): each Deployment or
    Rollout whose what-if puts a replica on spot (3 or more on average in its latest
    Sizings, `rows`, rounded half up: 2 stay on-demand), no pod
    claiming a persistent volume, none blocking a node (`blocking`), and under no Spot
    ratio, with its what-if (WHAT_IF: 70% on spot, 2 on-demand at least): the replicas
    each way and, where the cluster names its spot nodes and has a price, what its spot
    replicas would save a month at a spot node's rate against the on-demand one. An
    upper bound, never summed with Savings. None where the read can't say (a problem).
    And each CronJob (slice 16.3a), its what-if a run entirely on spot: its average pods
    (`replicas`, no split) at the discount, if its Jobs allow a retry, which only its
    Actuator reads: one it reported they don't (`skipped`) is none."""
    if read.get("problem"):
        return None
    if not read.get("pvc"):
        return {"workloads": [], "more": 0, "saving": None, "note": (
            "its nodes were read before the Hub read their volumes: the next hourly run does"
        )}  # fmt: skip
    cl = next(x for x in c.clusters if x.name == cluster)
    claim = {
        (p["namespace"], p["workload"]["kind"], p["workload"]["name"])
        for n in read["nodes"]
        for p in n["own"]
        if p.get("pvc") and p.get("workload")
    }
    asked: dict[tuple[str, str, str], dict[str, float]] = {}
    for r in rows:
        if r["cluster"] != cluster or r["kind"] not in (*SPOT_KINDS, "CronJob"):
            continue
        w = asked.setdefault((r["namespace"], r["kind"], r["name"]), {"pods": 0.0})
        w["pods"] = max(w["pods"], r.get("pods") or 0.0)
        for res in ("cpu", "memory"):
            w[res] = w.get(res, 0.0) + ((r.get("requests") or {}).get(res) or 0.0)
    price, note = c.price(cluster), None
    rate = None
    if cl.spot is None:
        note = "set the cluster's spot to price it"
    elif price is None:
        note = "no price for this cluster"
    else:
        share, against = c._spot_shares.get(cluster, (1.0, cl.spot.discount))
        rate = (ClusterPrices(cpu=price.cpu / share, memory=price.memory / share), against)
    found: list[dict[str, Any]] = []
    for (ns, kind, name), w in asked.items():
        if (
            (ns, kind, name) in claim
            or (kind != "CronJob" and (ns, f"{kind}/{name}") in blocking)
            or c.spot_for(cluster, ns, kind, name)
            or (kind == "CronJob" and f"{ns}/{name}" in (skipped or {}))
        ):
            continue
        if kind == "CronJob":  # its runs whole: no split, its pods on average
            on_demand, spot, moved = None, None, w["pods"]
        else:
            replicas = math.floor(w["pods"] + 0.5)  # half up: 2.5 is 3
            on_demand, spot = spot_split(replicas, WHAT_IF)
            moved = float(spot)
        if not moved:  # 2 replicas stay on-demand, or no run seen: nothing to move
            continue
        saving = None
        if rate is not None:
            each = monthly(rate[0], cpu=w["cpu"], memory=w["memory"])
            saving = moved * each * max(0.0, 1 - rate[1])
        found.append(
            {
                **{"namespace": ns, "kind": kind, "name": name, "replicas": round(w["pods"], 1)},
                **{"requests": {"cpu": w["cpu"], "memory": w["memory"]}},  # one pod's
                **{"on_demand": on_demand, "spot": spot, "saving": saving, "moved": moved},
            }
        )
    found.sort(key=lambda f: (-(f["saving"] or 0.0), -f["moved"] * f["requests"]["cpu"],
                              f["namespace"], f["name"]))  # fmt: skip
    for f in found:
        del f["moved"]
    total = sum(f["saving"] for f in found) if rate is not None else None
    return {
        "workloads": found[:MAX_CANDIDATES],
        "more": max(0, len(found) - MAX_CANDIDATES),
        "saving": total,
        "note": note if found else None,  # no money to speak of without a candidate
    }


def _sized_type(
    kind: str,
    g: ContainerUsage,
    now: float,
    policy: Policy,
    covered: list[float],
    steps: list[float],
    window: int,
    row: ContainerUsage | None = None,
) -> dict[str, Any]:
    """A DaemonSet container's Sizing for its pods on nodes of one instance type (slice
    15.3a), from those pods' own usage and requests, under the container's policy and
    with its history counted the same way; `learning` under it. Beside the row's pooled
    Sizing. No cap by the largest Room, as for the row. Its own Waste, ready, and whether
    its requests are its live pods' (slice 15.3c): what the row's money goes by where its
    Actuator applies the types. `pods`: its own while it runs, as shown; `share`: averaged
    over the container's span (`row`), what its money counts, so a type whose nodes came
    late counts for the time it ran and the types add up to the container's pods."""
    s = size(g, now, policy, history(g, covered, usage.step_for(window)))
    pods = avg_pods(g, steps, row)
    out: dict[str, Any] = {"type": kind, "pods": round(avg_pods(g, steps), 1), "state": s.state}
    out["share"] = round(pods, 3)
    out |= {"history_h": round(s.history_s / 3600, 1), "requests": dict(g.requests)}
    out["live"] = g.live
    waste = {}
    for name, res in (("cpu", s.cpu), ("memory", s.memory)):
        if res is not None:
            out[name] = {"action": res.action, "current": res.current, "target": res.target}
            out[name]["basis"] = res.basis
            waste[name] = max(0.0, (res.current or 0.0) - res.target) * pods
    if s.state == "ready":
        out["waste"] = waste
    return out


def by_type(row: dict[str, Any]) -> dict[str, dict[str, float | None]]:
    """A DaemonSet's Sizings per instance type as its Actuator applies them (slice 15.3b):
    each ready type's requests where it changes them, None where it keeps one. Empty:
    one Sizing for every pod."""
    if row.get("kind") != "DaemonSet":
        return {}
    return {
        g["type"]: {
            res: g[res]["target"] if g.get(res) and g[res]["action"] != "keep" else None
            for res in ("cpu", "memory")
        }
        for g in row.get("groups") or []
        if g["state"] == "ready"
    }


def changes(cpu: float | None, memory: float | None, types: dict[str, Any]) -> bool:
    """Whether a Sizing asks anything of its pods: some instance type's does, where it
    has types; else its one set does."""
    sets = list(types.values()) or [{"cpu": cpu, "memory": memory}]
    return any(v is not None for q in sets for v in q.values())


def group_lines(row: dict[str, Any]) -> list[str]:
    """A row's groups as indented lines under it, for `sizing show` and the Hub MCP."""
    lines = []
    for g in row.get("groups") or []:
        head = f"  on {g['type'] or 'nodes with no instance type'} ({g['pods']:g} pods)"
        if g["state"] != "ready":
            lines.append(f"{head}: learning ({g['history_h']:g} h of usage)")
            continue
        parts = []
        for res in ("cpu", "memory"):
            if r := g.get(res):
                now_ = "unset" if r["current"] is None else show(res, r["current"])
                parts.append(f"{res} {r['action']} {now_} -> {show(res, r['target'])}")
        lines.append(f"{head}: {', '.join(parts)}")
    return lines


def nodes_report(
    found: dict[str, dict[str, Any]],
    c: Contract,
    reports: dict[str, dict[str, Any]] | None = None,
    now: float = 0.0,
    rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Stored nodes as the console shows them (slice 15.1a), per cluster of the Contract
    that has a read: each node's Room (its allocatable less every pod's requests), split
    into the Contract's own pods (`mine`) and the rest (`others`: not visible, a count
    and a sum, nothing else of them is ever read), the cluster's largest Room after
    DaemonSets, which caps its Sizings, and the autoscaler the Contract names. Each
    node's `verdict` (slice 15.1b): whether it could be removed, by `rules`, going by its
    cluster's Actuator's latest report (`reports`, per cluster; `reported`: when the
    Hub got it) as of `now`. `could_go` (slice 15.4): the nodes that could go `now` and
    `after` the Contract's stored Sizings (`rows`) are applied, each with its capacity
    and its monthly cost under the Contract's prices (pass it with its effective ones);
    None where the read can't say (a problem, or one from before 15.4). `judged`: how
    many nodes got each verdict, so that "none could go" shows what was not judged.
    `placement` (slice 15.2a), where the Contract names the cluster's label: the label
    and taint, how many nodes carry it, the Workloads its Actuator is told to place,
    whether it places them (its last report says so), and a `note` when no node does.
    `spot` (slice 16.1a), where it names the cluster's spot label: the label, the
    discount, how many nodes carry it, and a `note` when none does; a node that could go
    is priced at its own rate, a spot node's at the discount. `candidates` and
    `spot_workloads` (slice 16.1c): where its Workloads could run on spot, and those
    under a Spot ratio, intended against actual."""
    out = []
    for cl in c.clusters:
        read = found.get(cl.name)
        if read is None:
            continue
        report = (reports or {}).get(cl.name)
        verdict = blockers.verdicts(read, report, cl, now)
        went = None
        usable = report if report and not blockers.gap(report, cl, now) else None
        fits = blockers.fit(usable, cl.autoscaler)  # audit A3: what its pods need of a node
        pins = blockers.pinned(read, cl)
        if (p := cl.placement) and not pins:  # audit A5: a Pinned node would look like any
            fits = (
                f"its nodes were not read with {p.label} yet, so its Pinned nodes can't be told: "
                "no node is counted as one that could go until the next hourly run"
                if read.get("placement") != p.label
                else f"no node carries {p.label} as kube-state-metrics shows it, so its Pinned "
                "nodes can't be told: no node is counted as one that could go"
            )
        counted = blockers.known(read, usable, cl)  # audit A4: its Actuator's own pod moves too
        first = None if read.get("problem") else blockers.could_go(counted, verdict, fits)
        if first is not None:
            sized = [r for r in rows or [] if r["cluster"] == cl.name]
            later = blockers.could_go(blockers.after(counted, sized), verdict, fits) or []
            price, rate = c.price(cl.name), rates(read, c, cl.name)
            went = {
                "now": _worth(first, read, price, rate),
                "after": _worth(later, read, price, rate),
                "note": fits if isinstance(fits, str) else None,
            }
        blocking = blocking_of(read, report, cl, now)  # slice 16.1c
        # slice 16.3a: the CronJobs its Actuator reported it doesn't steer, and why, only
        # ever looked up by the read's own CronJobs: another namespace's key shows nowhere
        skipped: dict[str, str] = (usable or {}).get("spot_skipped") or {}
        judged = {state: 0 for state in ("clear", "blocked", "unknown")}
        if cl.placement:
            judged["pinned"] = 0
        for v in verdict.values():
            judged[v["state"]] += 1
        placement = None
        if p := cl.placement:
            note = None
            if read.get("placement") != p.label:
                note = f"its nodes were not read with {p.label} yet: the next hourly run does"
            elif not pins and not read.get("problem"):
                note = (
                    f"no node carries {p.label} as kube-state-metrics shows it: label the "
                    "nodes, and allowlist the label in its --metric-labels-allowlist"
                )
            placement = {
                **{"label": p.label, "taint": p.taint, "pinned": len(pins), "note": note},
                **{"workloads": blockers.placing(verdict)},
                **{"placing": bool(report and report.get("placing"))},
            }
        spot = None
        if s := cl.spot:  # slice 16.1a
            count = sum(bool(n.get("spot")) for n in read["nodes"])
            said = None
            if read.get("spot") != s.label:
                said = f"its nodes were not read with {s.label} yet: the next hourly run does"
            elif not count and not read.get("problem"):
                said = (
                    f"no node carries {s.label} as kube-state-metrics shows it: none is on spot "
                    "now, or the label isn't in its --metric-labels-allowlist; every node is "
                    "priced on-demand"
                )
            spot = {"label": s.label, "discount": s.discount, "nodes": count, "note": said}
        nodes = []
        for n in read["nodes"]:
            mine = {res: sum(p[res] for p in n["own"]) for res in ("cpu", "memory")}
            rest = {res: max(0.0, n["requests"][res] - mine[res]) for res in mine}
            room = {res: v - n["requests"][res] for res, v in n["allocatable"].items()}
            nodes.append(
                {
                    **n,
                    "pinned": n["node"] in pins,
                    "verdict": verdict[n["node"]],
                    "room": room,
                    "mine": {"pods": len(n["own"]), **mine},
                    "others": {"pods": max(0, n["pods"] - len(n["own"])), **rest},
                }
            )
        out.append(
            {
                **{"cluster": cl.name, "at": read["at"], "autoscaler": cl.autoscaler},
                **{"rules": blockers.RULES[cl.autoscaler]},
                **{"settings": blockers.SETTINGS[cl.autoscaler]},  # slice 15.6
                **{"reported": report["received"] if report else None},
                **{"could_go": went, "judged": judged, "placement": placement, "spot": spot},
                **{"candidates": candidates(read, c, cl.name, rows or [], blocking, skipped)},
                **{
                    "spot_workloads": spot_workloads(
                        read,
                        c,
                        cl.name,
                        blocking,
                        spotting=bool(usable and usable.get("spotting")),
                        skipped=skipped,
                        runs=bool(usable and usable.get("spot_runs")),
                    )
                },
                **{"problem": read.get("problem"), "largest": largest_room(read), "nodes": nodes},
            }
        )
    return out


def rows(
    c: Contract,
    by_window: dict[int, list[tuple[str, ContainerUsage]]],
    now: float,
    scaled: dict[str, dict[Workload, list[Divides]]] | None = None,
    rooms: dict[str, dict[str, Any]] | None = None,
    typed: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Every container's Sizing under its policy, as `sizing show`, the stored runs and
    `sizing waste` see it, sorted by name. `off` gives an excluded row. `scaled`: per
    cluster, the HPA targets that divide by a request (slice 14.3): a Sizing that would
    change such a request is `held`. `rooms`: per cluster, its nodes (usage.rooms): no
    Sizing asks more than the largest Room after DaemonSets there, a DaemonSet's own
    aside (that Room already leaves its requests out), and a Workload whose containers
    together ask more says so on each of them. `typed`: the clusters whose Actuator applies
    a DaemonSet's Sizings per instance type (slice 15.3c): there a container with types is
    `by_type`, its Waste its types' and its pooled Sizing raising no risk about a type."""
    caps = {cluster: largest_room(found) for cluster, found in (rooms or {}).items()}
    out: dict[str, dict[str, Any]] = {}
    asked: dict[tuple[str, Workload], dict[str, dict[str, float]]] = {}  # per container
    for found in by_window.values():
        for cluster, u in found:
            asked.setdefault((cluster, u.workload), {})[u.container] = u.requests
    for window, found in by_window.items():
        seen: dict[str, set[float]] = {}
        used: dict[str, set[float]] = {}  # usage, not Running pods: history is of usage
        for cluster, u in found:
            seen.setdefault(cluster, set()).update(pod_times(u))
            used.setdefault(cluster, set()).update(t for t, _ in u.cpu + u.memory)
        steps = {cluster: sorted(times) for cluster, times in seen.items()}
        covered = {cluster: sorted(times) for cluster, times in used.items()}
        for cluster, u in found:
            w = u.workload
            row: dict[str, Any] = {
                "workload": f"{cluster}/{w.namespace}/{w.kind}/{w.name}/{u.container}",
                "cluster": cluster,
                "namespace": w.namespace,
                "kind": w.kind,
                "name": w.name,
                "container": u.container,
                "policy": c.sizing.policy_for(cluster, w.namespace, w.kind, w.name, u.container),
            }
            if c.allocation.labels:  # stored since 10.2b; teams are matched when shown
                row["labels"] = u.labels
            # What its requests cost, whatever its state (stored since 10.1), and
            # whether they are its live pods' (since 10.3b)
            pods = avg_pods(u, steps[cluster])
            # a run's pods average little: an hour a night is 0.04, which one place would
            # make nothing (its cost, its spot candidacy: slice 16.3a)
            places = 3 if w.kind in BATCH_KINDS else 1
            row["requests"] = {name: u.requests.get(name) for name in ("cpu", "memory")}
            row["live"] = u.live
            policy = c.sizing.policy(row["policy"])
            if policy is None:
                out[row["workload"]] = {**row, "state": "excluded", "pods": round(pods, places)}
                continue
            if policy.window != window:
                continue
            if c.actuator.reflexes.burst:  # read by `bursting` (slice 13.3a), whatever its state
                since = now - c.actuator.reflexes.burst.expires
                peak = max((v for at, v in u.cpu if at >= since), default=None)
                row["burst"] = {"throttled": u.throttled_recent, "peak": peak}
            cap = None if w.kind == "DaemonSet" else caps.get(cluster)
            covers = history(u, covered[cluster], usage.step_for(window))
            s = size(u, now, policy, covers, cap)
            row |= {
                "state": s.state,
                "history_h": round(s.history_s / 3600, 1),
                "needs_h": round(policy.history / 3600, 1),
                "window_h": round(window / 3600, 1),
                "pods": round(pods, places),
            }
            risks = list(s.risks)
            waste = {}
            for name, res in (("cpu", s.cpu), ("memory", s.memory)):
                if res is None:
                    continue
                row[name] = {
                    "action": res.action,
                    "current": res.current,
                    "target": res.target,
                    "basis": res.basis,
                    "limit": u.limits.get(name),  # None: no limit (stored since 9.6)
                }
                divides = (scaled or {}).get(cluster, {}).get(w, [])
                if res.action != "keep" and divides:
                    by = held(name, w, u.container, divides, asked[cluster, w])
                    if by:
                        row[name]["held"] = by
                if res.action == "set":
                    risks.append(Risk("request_unset", f"{name} has no request"))
                if res.action == "raise" and res.current is not None:
                    risks.append(
                        Risk(
                            "under_requested",
                            f"{name} request {show(name, res.current)} is below the "
                            f"{show(name, res.target)} it needs",
                        )
                    )
                current = res.current or 0.0
                waste[name] = max(0.0, current - res.target) * pods
            if s.state == "ready":
                row["waste"] = waste
            row["risks"] = [r._asdict() for r in risks]
            if u.groups:  # slice 15.3a: a DaemonSet's, per instance type
                row["groups"] = [
                    _sized_type(kind, g, now, policy, covered[cluster], steps[cluster], window, u)
                    for kind, g in sorted(u.groups.items())
                ]
                if cluster in (typed or set()):  # its Actuator applies them (slice 15.3c)
                    row["by_type"] = True
                    if s.state == "ready":
                        row["waste"] = {
                            res: sum((g.get("waste") or {}).get(res, 0.0) for g in row["groups"])
                            for res in ("cpu", "memory")
                        }
            out[row["workload"]] = row
    together: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for row in out.values():
        if row["kind"] != "DaemonSet" and caps.get(row["cluster"]):
            key = (row["cluster"], row["namespace"], row["kind"], row["name"])
            together.setdefault(key, []).append(row)
    for (cluster, *_), containers in together.items():
        for resource, most in (caps[cluster] or {}).items():
            asks = sum(
                r[resource]["target"] if r.get(resource) else (r["requests"].get(resource) or 0.0)
                for r in containers
            )
            if len(containers) > 1 and asks > most + 1e-9:
                text = (
                    f"its pod's containers ask {show(resource, asks)} of {resource} "
                    f"together, more than the {show(resource, _round_down(resource, most))} "
                    "a node has room for after its DaemonSets: it needs a larger node"
                )
                for r in containers:
                    if r["state"] == "ready":  # a risk of its Sizing: one learning has none
                        r["risks"].append({"kind": "needs_larger_node", "text": text})
    return [out[k] for k in sorted(out)]


FLOOR_MIN = 2  # a Floor never goes below it, unless the HPA's minimum already is
FLOOR_HISTORY = NEEDS_FOR // 2  # the most history a Floor asks for: Needs are kept 8 weeks
Needs = dict[tuple[str, str, str], dict[float, float]]  # per Workload: namespace, kind, name


def _share(part: int, of: int) -> str:
    return "under 1%" if part / of < 0.005 else f"{part / of:.0%}"


def floors(
    c: Contract,
    rows: list[dict[str, Any]],
    needs: dict[str, Needs],
    bounds: dict[str, dict[Workload, list[Bounds]]],
    now: float,
) -> dict[str, list[dict[str, Any]]]:
    """The Floor of each Workload with an autoscaler (slice 14.1b), per cluster in
    `bounds` (the clusters whose HPAs this run read; one without an HPA gets none).

    Under the policy with the highest `replicas.percentile` among the Workload's
    containers (none when all are `off`): that percentile of its hourly Needs over the
    policy's window, rounded up, never below 2 unless the HPA's minimum already is.
    Only its own stored hours count, each an hour of history (one never read is unknown,
    not idle), and never the hour under way, whose peak is partial. `learning` under the
    policy's history (at most 4 of the 8 weeks kept); `none` with two HPAs, a minimum not
    known, or a Workload that scales to zero (a minimum of 0, or replicas at 0 in the
    lookback: KEDA's HPA keeps a minimum of 1), which is left alone. A Need of 0 is an
    idle hour like any other: a Workload held at its minimum with nothing to do has one.

    Against the HPA's minimum today: lower, its `saving` is the replicas it would no
    longer hold (per hour, the minimum less the larger of the Floor and that hour's Need
    rounded up, averaged), each at its containers' Sizings, or requests where one has no
    Sizing, so it adds to Waste; higher, a risk. Another risk where the Need went above
    the HPA's maximum. `rows`: this run's (sizing.rows)."""
    of: dict[tuple[str, tuple[str, str, str]], list[dict[str, Any]]] = {}
    for r in rows:
        of.setdefault((r["cluster"], (r["namespace"], r["kind"], r["name"])), []).append(r)
    out: dict[str, list[dict[str, Any]]] = {}
    for cluster, found in bounds.items():
        out[cluster] = []
        for w, hpas in sorted(found.items(), key=lambda x: (x[0].namespace, x[0].kind, x[0].name)):
            key = (w.namespace, w.kind, w.name)
            containers = of.get((cluster, key), [])
            policies = [
                (p.replicas.percentile, name, p)
                for name in sorted({r["policy"] for r in containers})
                if (p := c.sizing.policy(name))
            ]
            if not policies:
                continue  # `off`, or no container of it read: nothing to go by
            q, name, policy = max(policies, key=lambda x: x[0])
            f: dict[str, Any] = {
                "workload": f"{cluster}/{w.namespace}/{w.kind}/{w.name}",
                **{"cluster": cluster, "namespace": w.namespace, "kind": w.kind, "name": w.name},
                **{"hpa": hpas[0].hpa, "policy": name, "percentile": q},
                **{"minimum": hpas[0].minimum, "maximum": hpas[0].maximum, "risks": []},
            }
            out[cluster].append(f)
            window = min(policy.window, NEEDS_FOR)
            values = [
                n
                for hour, n in needs.get(cluster, {}).get(key, {}).items()
                if hour >= now - window and hour + 3600 <= now
            ]
            minimum, maximum = hpas[0].minimum, hpas[0].maximum
            if len(hpas) > 1:
                f |= {"state": "none", "why": f"{len(hpas)} HPAs scale it"}
            elif minimum is None:
                f |= {"state": "none", "why": "its HPA's minimum is not known"}
            elif minimum == 0 or hpas[0].zero:
                f |= {"state": "none", "why": "it scales to zero: left alone"}
            else:
                wanted = min(policy.history, FLOOR_HISTORY)
                f |= {"history_h": float(len(values)), "needs_h": round(wanted / 3600, 1)}
                if len(values) * 3600 < wanted:
                    f["state"] = "learning"
                    continue
                low = weighted_percentile([(now, n) for n in values], q, now)
                floor = max(math.ceil(low - 1e-9), min(FLOOR_MIN, int(minimum)))
                f |= {"state": "ready", "floor": floor}
                pod = {"cpu": 0.0, "memory": 0.0}  # one replica, at its Sizings
                for r in containers:
                    for res in pod:
                        sized = r.get(res) if r["state"] == "ready" else None
                        asks = (r.get("requests") or {}).get(res) or 0.0
                        pod[res] += sized["target"] if sized else asks
                own = needs.get(cluster, {}).get(key, {})
                weekly = profile(own, floor, c.sizing.timezone, c.sizing.lead_time, now)
                if weekly["state"] == "ready":
                    test = backtest(own, floor, c.sizing.timezone, now)
                    if test["state"] == "over":  # no blocks or triggers for any view to show
                        weekly |= {"state": "irregular", "blocks": []}
                    else:
                        weekly |= scheduled(f, weekly)
                    if "replicas" in test:
                        test["excess"] = {res: test["replicas"] * v for res, v in pod.items()}
                    weekly["backtest"] = test
                f["profile"] = weekly
                up = [math.ceil(n - 1e-9) for n in values]
                if floor < minimum:
                    held = sum(max(0.0, minimum - max(floor, n)) for n in up) / len(up)
                    f["replicas"] = round(held, 2)
                    f["saving"] = {res: held * v for res, v in pod.items()}
                elif floor > minimum:
                    often = _share(sum(1 for n in up if n >= floor), len(up))
                    text = (
                        f"its minimum of {minimum:g} is below the {floor} replicas it "
                        f"needed in {often} of hours"
                    )
                    f["risks"].append({"kind": "minimum_below_floor", "text": text})
                over = sum(1 for n in values if maximum is not None and n > maximum + 1e-9)
                if over:
                    text = (
                        f"needed more than its maximum of {maximum:g} in "
                        f"{_share(over, len(values))} of hours"
                    )
                    f["risks"].append({"kind": "needed_above_maximum", "text": text})
    return out


PROFILE_WEEKS = 3  # weeks of stored Needs before a Weekly profile: a median ignores one odd
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _when(minute: int) -> tuple[str, str]:
    """A minute of the week (Monday 00:00 is 0, past either end wraps) as `Mon 07:45`
    and as a cron expression (minute, hour, any day, any month, weekday: Sunday is 0)."""
    day, rest = divmod(minute % (7 * 1440), 1440)
    hour, m = divmod(rest, 60)
    return f"{DAYS[day]} {hour:02d}:{m:02d}", f"{m} {hour} * * {(day + 1) % 7}"


def _slot(hour: float, zone: zoneinfo.ZoneInfo) -> int:
    """The hour of the week there (Monday 00:00 is 0) a stored hour starts in."""
    local = datetime.fromtimestamp(hour, zone)
    return local.weekday() * 24 + local.hour


def _slots(known: dict[float, float], floor: int, zone: zoneinfo.ZoneInfo) -> list[int]:
    """What each hour of the week asks, going by `known` (hourly Needs): the median of
    that hour's Needs, rounded up, never below `floor`."""
    by: dict[int, list[float]] = {}
    for hour, need in known.items():
        by.setdefault(_slot(hour, zone), []).append(need)
    return [
        max(floor, math.ceil(statistics.median(by[s]) - 1e-9))
        if len(by.get(s, [])) >= PROFILE_WEEKS  # fewer: one odd week would decide it
        else floor
        for s in range(168)
    ]


def profile(
    needs: dict[float, float], floor: int, timezone: str, lead: int, now: float
) -> dict[str, Any]:
    """A Workload's Weekly profile (slice 14.2) from its stored Needs: for each hour of
    the week in `timezone`, the median of that hour's Needs across the weeks stored,
    rounded up, never below its Floor (an hour stored in fewer than 3 weeks is at the
    Floor: one odd week would decide it). Only its
    own complete hours of the 8 weeks kept count; `learning` under 3 weeks of them.

    `blocks`: each run of hours asking the same, above the Floor (the Floor is the
    baseline, not a block), `since` `Mon 07:45` `until` `Mon 18:00`, with the cron
    expressions of both (`start`, `end`). One that asks more than the hour before it
    starts `lead` seconds early; it ends on the hour. `flat` when every hour asks the
    same: there is no weekly pattern."""
    known = {h: n for h, n in needs.items() if h >= now - NEEDS_FOR and h + 3600 <= now}
    out: dict[str, Any] = {"timezone": timezone, "lead_time": lead}
    out |= {"history_h": float(len(known)), "needs_h": float(PROFILE_WEEKS * 168)}
    if len(known) < PROFILE_WEEKS * 168:
        return {**out, "state": "learning"}
    zone = zoneinfo.ZoneInfo(timezone)
    # minutes past the hour a stored hour starts at there (30 in Asia/Kolkata)
    past = datetime.fromtimestamp(max(known), zone).minute
    slots = _slots(known, floor, zone)
    if len(set(slots)) == 1:
        return {**out, "state": "flat", "blocks": []}
    first = next(s for s in range(168) if slots[s] != slots[s - 1])  # where a run starts
    blocks = []
    done = 0
    while done < 168:
        s = (first + done) % 168
        length = 1
        while done + length < 168 and slots[(s + length) % 168] == slots[s]:
            length += 1
        if slots[s] > floor:
            early = lead // 60 if slots[s - 1] < slots[s] else 0
            begin, end = s * 60 + past - early, (s + length) * 60 + past
            (since, start), (until, stop) = _when(begin), _when(end)
            block = {"since": since, "until": until, "replicas": slots[s]}
            block |= {"start": start, "end": stop}
            blocks.append((begin % (7 * 1440), block))
        done += length
    return {**out, "state": "ready", "blocks": [b for _, b in sorted(blocks, key=lambda x: x[0])]}


BACKTEST_BUDGET = 0.20  # the error a Weekly profile may have and still be shown
BACKTEST_HOURS = 84  # stored hours of the newest week a verdict needs: half of it


def backtest(needs: dict[float, float], floor: int, timezone: str, now: float) -> dict[str, Any]:
    """A Weekly profile's Backtest (slice 14.4): the newest week (the 168 complete hours
    before `now`) replayed hour by hour against a profile built from the weeks kept
    before it. `waiting` under 3 weeks of stored hours before that week, or under half
    of that week stored.

    In an hour the Workload needed the larger of its Floor and its Need rounded up (what
    its HPA would run with the Floor as its minimum), so the profile is judged on what
    it adds above the Floor. `few` and `many`: the replica-hours it held too few and too
    many, as shares of the replica-hours needed; `error` their sum, `over` the budget
    above 20%, else `within`. An hour off by under a tenth of what it needed counts for
    nothing: the HPA's own tolerance. An hour never stored is skipped, and the lead time
    plays no part. `replicas`: the too many, averaged over the hours replayed."""
    end = now - now % 3600
    start = end - WEEK
    before = {h: n for h, n in needs.items() if now - NEEDS_FOR <= h and h + 3600 <= start}
    week = {h: n for h, n in needs.items() if start <= h and h + 3600 <= end}
    if len(before) < PROFILE_WEEKS * 168 or len(week) < BACKTEST_HOURS:
        return {"state": "waiting"}
    zone = zoneinfo.ZoneInfo(timezone)
    slots = _slots(before, floor, zone)
    needed = few = many = 0
    for hour, need in week.items():
        want = max(floor, math.ceil(need - 1e-9))
        gap = slots[_slot(hour, zone)] - want
        needed += want
        if abs(gap) * 10 >= want:  # a tenth of what it needed, in whole numbers
            few, many = few + max(0, -gap), many + max(0, gap)
    error = (few + many) / needed
    out = {"state": "over" if error > BACKTEST_BUDGET else "within", "error": error}
    out |= {"few": few / needed, "many": many / needed}
    return out | {"hours": len(week), "replicas": many / len(week)}


def scheduled(f: dict[str, Any], p: dict[str, Any]) -> dict[str, Any]:
    """How a ready Weekly profile gets applied: for KEDA's HPA (`keda-hpa-<name>`), a
    `cron` trigger per block for its ScaledObject, as text to paste; else a note, an HPA
    having no schedule of its own."""
    if not f["hpa"].startswith("keda-hpa-"):
        note = "an HPA has no schedule of its own: something else has to hold these replicas"
        return {"note": f"{note} (KEDA's cron trigger does)"}
    name = f["hpa"].removeprefix("keda-hpa-")
    lines = [
        f"# ScaledObject {f['namespace']}/{name} (KEDA): cron triggers, beside its others.",
        "# Each holds at least that many replicas in its window; the most asked for wins.",
    ]
    for b in p["blocks"]:
        lines += ["- type: cron", "  metadata:", f"    timezone: {p['timezone']}"]
        lines += [f"    start: {b['start']}", f"    end: {b['end']}"]
        lines += [f'    desiredReplicas: "{b["replicas"]}"']
    return {"triggers": "\n".join(lines)}


def profile_lines(f: dict[str, Any], currency: str | None = None) -> list[str]:
    """A Floor's Weekly profile, with its Backtest, as indented lines under its own
    (floor_text)."""
    p = f.get("profile")
    if not p:
        return []
    test = p.get("backtest") or {}
    off = ""
    if "error" in test:
        few, many = round(test["few"] * 100), round(test["many"] * 100)
        # the total as the sum of its parts as shown, so the line adds up
        off = f"off by {few + many}% last week (too few {few}%, too many {many}%"
    if p["state"] == "irregular":
        return [f"  load isn't weekly enough: {off}); it keeps its Floor"]
    if p["state"] == "learning":
        return [f"  Weekly profile learning ({p['history_h']:g} h of {p['needs_h']:g} h of Needs)"]
    if p["state"] == "flat":
        return ["  no weekly pattern: its Floor holds all week"]
    early = f", each rise {p['lead_time'] // 60} min early" if p["lead_time"] else ""
    lines = [f"  Weekly profile ({p['timezone']}{early}):"]
    lines += [f"    {b['since']} to {b['until']}: {b['replicas']} replicas" for b in p["blocks"]]
    if test.get("state") == "waiting":
        lines.append("    not backtested yet (needs four weeks)")
    elif off:
        priced = currency and test.get("cost") is not None
        worth = money(test["cost"], currency) if priced and currency else quantities(test["excess"])
        lines.append(f"    backtest: {off}{f': {worth}' if test['many'] else ''})")
    if p.get("note"):
        lines.append(f"    {p['note']}")
    if p.get("triggers"):
        lines += [f"    {line}" for line in p["triggers"].splitlines()]
    return lines


def floors_report(found: list[dict[str, Any]], contract: Contract) -> dict[str, Any]:
    """Stored Floors as the console, `sizings` and the digest show them: each saving's
    monthly cost with a priced Contract, and their total (the Workloads that can lower
    their HPA's minimum); a Backtest's too many priced the same way, outside the total
    (nothing holds them until the profile is applied)."""
    priced = contract.prices is not None
    total: dict[str, Any] = {"workloads": 0, "replicas": 0.0, "saving": {"cpu": 0.0, "memory": 0.0}}
    if priced:
        total["cost"] = 0.0
    out = []
    for f in found:
        f = dict(f)
        p = contract.price(f["cluster"]) if priced else None
        if saving := f.get("saving"):
            total["workloads"] += 1
            total["replicas"] += f["replicas"]
            for res in ("cpu", "memory"):
                total["saving"][res] += saving[res]
            if p:
                f["cost"] = monthly(p, **saving)
                total["cost"] += f["cost"]
        test = (f.get("profile") or {}).get("backtest") or {}
        if p and test.get("excess"):
            priced_test = {**test, "cost": monthly(p, **test["excess"])}
            f["profile"] = {**f["profile"], "backtest": priced_test}
        out.append(f)
    return {"floors": out, "floors_total": total}


def floor_text(f: dict[str, Any], currency: str | None = None) -> str:
    """One Floor in a line, as `sizing show` and the Hub MCP's `sizings` give it."""
    head = f"{f['workload']}: "
    if f["state"] == "none":
        return f"{head}no Floor: {f['why']}"
    if f["state"] == "learning":
        return f"{head}Floor learning ({f['history_h']:g} h of {f['needs_h']:g} h of Needs)"
    top = "" if f.get("maximum") is None else f", maximum {f['maximum']:g}"
    text = (
        f"{head}Floor {f['floor']} (HPA {f['hpa']}: minimum {f['minimum']:g}{top}; "
        f"{f['policy']}: {f['percentile']:.0%} of {f['history_h']:g} h of Needs)"
    )
    if f.get("saving"):
        priced = currency and f.get("cost") is not None
        worth = money(f["cost"], currency) if priced and currency else quantities(f["saving"])
        one = "replica" if f["replicas"] == 1 else "replicas"
        text += f" | {f['replicas']:g} {one} fewer on average: {worth}"
    if f["risks"]:
        text += " | risk: " + "; ".join(r["text"] for r in f["risks"])
    return text


def floors_line(found: dict[str, Any], currency: str | None) -> str | None:
    """The digest's one line on Floors (floors_report), or None with nothing to say."""
    total = found["floors_total"]
    below = sum(
        1 for f in found["floors"] if any(r["kind"] == "minimum_below_floor" for r in f["risks"])
    )
    parts = []
    if total["workloads"]:
        worth = money(total["cost"], currency) if currency else quantities(total["saving"])
        parts.append(
            f"{total['workloads']} can lower their HPA minimum "
            f"({total['replicas']:.1f} replicas fewer on average, {worth})"
        )
    if below:
        parts.append(f"{below} with a minimum below their Floor")
    return f"Floors: {'; '.join(parts)}." if parts else None


def nodes_line(clusters: list[dict[str, Any]], currency: str | None) -> str | None:
    """The digest's one line on the nodes that could go (nodes_report), or None when
    none could, now or after the Sizings. With how many nodes were not judged: "none"
    of a cluster nobody looked at is no finding."""

    def said(which: str) -> str | None:
        went = [c["could_go"][which] for c in clusters if c["could_go"]]
        count = sum(len(w["nodes"]) for w in went)
        if not count:
            return None
        held = {res: sum(w[res] for w in went) for res in ("cpu", "memory")}
        priced = currency and all(w["cost"] is not None for w in went)
        worth = money(sum(w["cost"] for w in went), currency) if priced and currency else None
        when = "now" if which == "now" else "after the Sizings"
        return f"up to {count} {when} ({worth or quantities(held)})"

    parts = [s for s in (said("now"), said("after")) if s]
    if not parts:
        return None
    unknown = sum(c["judged"]["unknown"] for c in clusters)
    judged = f"; {unknown} not judged" if unknown else ""
    return f"Nodes that could go: {', '.join(parts)}{judged}. An upper bound, not Savings."


GIB = 2**30
MONTH = 730.0  # hours in a month, as cloud price sheets count them


def rank(workload: dict[str, Any]) -> float:
    """A Workload's waste as one number to sort by: its monthly cost when priced."""
    if workload.get("cost"):
        return float(workload["cost"]["waste"])
    # Unpriced: a core weighs as much as 4 GiB (a common node shape)
    return float(workload["waste"]["cpu"] + workload["waste"]["memory"] / GIB / 4)


def monthly(p: ClusterPrices, cpu: float, memory: float) -> float:
    """Cores and bytes held for a month, in the prices' currency."""
    return (cpu * p.cpu + memory / GIB * p.memory) * MONTH


def money(value: float, currency: str) -> str:
    return f"{value:,.2f} {currency}/month"


def quantities(waste: dict[str, float]) -> str:
    return f"{waste['cpu']:.2f} cores, {waste['memory'] / GIB:.2f} GiB"


def requested(r: dict[str, Any]) -> tuple[float, float]:
    """A row's requests times its average pods. A row stored before 10.1 has its
    requests only when ready. One `by_type` (slice 15.3c): each type's pods' requests,
    and its pods on no known node the container's (the largest any pod asks)."""
    req = r.get("requests") or {res: (r.get(res) or {}).get("current") for res in ("cpu", "memory")}
    pods = r.get("pods") or 0.0
    if not r.get("by_type"):
        return (req.get("cpu") or 0.0) * pods, (req.get("memory") or 0.0) * pods
    groups = r.get("groups") or []
    shares = [g.get("share", g["pods"]) for g in groups]  # a type's pods over the container's
    rest = max(0.0, pods - sum(shares))  # rounded parts: never below 0

    def asks(res: str) -> float:
        own = sum(
            ((g.get("requests") or {}).get(res) or 0.0) * n
            for g, n in zip(groups, shares, strict=True)
        )
        return float(own + (req.get(res) or 0.0) * rest)

    return asks("cpu"), asks("memory")


def report(
    rows: list[dict[str, Any]], clusters: dict[str, Any], contract: Contract | None = None
) -> dict[str, Any]:
    """Stored rows (one per container) as Workloads ranked by waste, each with its
    containers and risks, plus the total, the learning and excluded rows and each
    cluster's status: what `sizing waste` and the console show. A Contract with
    prices adds each Workload's monthly cost of its waste and of its requests, and the
    totals; its requests count every container, ready or not (slice 10.1)."""
    priced = contract is not None and contract.prices is not None
    asked: dict[str, float] = {}  # Workload -> its requests' monthly cost
    if priced:
        assert contract is not None
        for r in rows:
            p = contract.price(r["cluster"])
            assert p is not None  # the Contract's prices back every cluster
            key = r["workload"].rsplit("/", 1)[0]
            asked[key] = asked.get(key, 0.0) + monthly(p, *requested(r))
    workloads: dict[str, dict[str, Any]] = {}
    learning: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for r in rows:
        if r["state"] != "ready":
            (learning if r["state"] == "learning" else excluded).append(r)
            continue
        w = workloads.setdefault(
            r["workload"].rsplit("/", 1)[0],
            {
                "workload": r["workload"].rsplit("/", 1)[0],
                **{k: r[k] for k in ("cluster", "namespace", "kind", "name")},
                "pods": 0.0,
                "waste": {"cpu": 0.0, "memory": 0.0},
                "risks": [],
                "containers": [],
            },
        )
        w["pods"] = max(w["pods"], r["pods"])
        for res in ("cpu", "memory"):
            w["waste"][res] += r["waste"].get(res, 0.0)
        w["risks"] += [{**risk, "container": r["container"]} for risk in r["risks"]]
        w["containers"].append(r)
    if priced:
        assert contract is not None
        for key, w in workloads.items():
            p = contract.price(w["cluster"])
            assert p is not None
            w["cost"] = {"waste": monthly(p, **w["waste"]), "requests": asked[key]}
    ranked = sorted(workloads.values(), key=lambda w: (-rank(w), w["workload"]))
    out = {
        "clusters": clusters,
        "total": {res: sum(w["waste"][res] for w in ranked) for res in ("cpu", "memory")},
        "workloads": ranked,
        "learning": learning,
        "excluded": excluded,
    }
    if priced:
        assert contract is not None and contract.prices is not None
        out["currency"] = contract.prices.currency
        out["cost"] = {
            "waste": sum(w["cost"]["waste"] for w in ranked),
            "requests": sum(asked.values()),
        }
    if contract is not None and contract._price_notes:  # list prices, or spot (16.1a)
        out["prices"] = contract._price_notes
    return out


def groupings(c: Contract) -> list[str]:
    """What allocate() can group the Contract's rows by."""
    teams = ["team"] if c.allocation.teams else []
    return ["namespace", *teams, *(f"label:{k}" for k in c.allocation.labels)]


def _group(r: dict[str, Any], c: Contract, by: str) -> str:
    if by == "namespace":
        return f"{r['cluster']}/{r['namespace']}"
    labels: dict[str, str] = r.get("labels") or {}  # rows stored before 10.2b have none
    if by == "team":
        return c.allocation.team(r["cluster"], r["namespace"], labels)
    key = by.removeprefix("label:")
    return labels.get(key, f"(no {key})")


def _label_keys(c: Contract, by: str) -> list[str]:
    """The pod labels a grouping reads."""
    if by == "team":
        return sorted({k for t in c.allocation.teams for k in t.labels})
    return [by.removeprefix("label:")] if by.startswith("label:") else []


def _warnings(rows: list[dict[str, Any]], c: Contract, by: str) -> list[str]:
    """A cluster where no pod has a label the grouping reads: kube-state-metrics
    doesn't allowlist it, or nobody sets it."""
    if by == "namespace":
        return []
    keys = _label_keys(c, by)
    seen: dict[str, set[str]] = {}
    for r in rows:
        seen.setdefault(r["cluster"], set()).update(r.get("labels") or {})
    return [
        f"{cluster}: no pod has label {key}; kube-state-metrics needs "
        f"--metric-labels-allowlist=pods=[{key}]"
        for cluster, found in sorted(seen.items())
        for key in keys
        if key not in found
    ]


def _sums(rows: list[dict[str, Any]], c: Contract, by: str) -> dict[str, dict[str, Any]]:
    """Each group's Workloads, requests (every container, ready or not) and waste
    (sized ones), and their monthly cost when the Contract has prices."""
    groups: dict[str, dict[str, Any]] = {}
    for r in rows:
        key = _group(r, c, by)
        g = groups.setdefault(
            key,
            {
                "group": key,
                "workloads": set(),
                "requests": {"cpu": 0.0, "memory": 0.0},
                "waste": {"cpu": 0.0, "memory": 0.0},
                "cost": {"requests": 0.0, "waste": 0.0},
            },
        )
        g["workloads"].add(r["workload"].rsplit("/", 1)[0])
        cpu, memory = requested(r)
        waste = r.get("waste", {})  # only sized rows carry it
        g["requests"]["cpu"] += cpu
        g["requests"]["memory"] += memory
        for res in ("cpu", "memory"):
            g["waste"][res] += waste.get(res, 0.0)
        if p := c.price(r["cluster"]):
            g["cost"]["requests"] += monthly(p, cpu, memory)
            g["cost"]["waste"] += monthly(p, waste.get("cpu", 0.0), waste.get("memory", 0.0))
    return groups


def _total(groups: Any, priced: bool) -> dict[str, Any]:
    """Groups' requests, waste and cost added up."""
    groups = list(groups)
    return {
        **{
            k: {res: sum(g[k][res] for g in groups) for res in ("cpu", "memory")}
            for k in ("requests", "waste")
        },
        "cost": {k: sum(g["cost"][k] for g in groups) for k in ("requests", "waste")}
        if priced
        else None,
    }


def allocate(rows: list[dict[str, Any]], c: Contract, by: str = "namespace") -> dict[str, Any]:
    """Stored rows grouped (slice 10.2) by namespace, team or a label: each group's
    requests (every container, ready or not) and waste (sized ones), in money when
    the Contract has prices, ranked by what its requests cost, and the Contract's
    total; the waste's cost is the available Savings (10.3a). A namespace is its
    cluster's (kind/lab, not lab); a team or a label value spans clusters."""
    if by not in groupings(c):
        raise ValueError(f"{c.id} groups by {', '.join(groupings(c))}, not {by!r}")
    priced = c.prices is not None
    groups = _sums(rows, c, by)
    out = [
        {**g, "workloads": len(g["workloads"]), "cost": g["cost"] if priced else None}
        for g in groups.values()
    ]
    out.sort(
        key=lambda g: (
            -(g["cost"]["requests"] if priced else rank({"waste": g["requests"]})),
            g["group"],
        )
    )
    return {
        "by": by,
        "currency": c.prices.currency if c.prices else None,
        "total": _total(groups.values(), priced),
        "groups": out,
        "groupings": groupings(c),
        "warnings": _warnings(rows, c, by),
    }


def _minus(a: dict[str, Any] | None, b: dict[str, Any] | None, priced: bool) -> dict[str, Any]:
    """Group a's requests, waste and cost less b's; a missing group counts as nothing."""
    zero = {"cpu": 0.0, "memory": 0.0}
    a = a or {"requests": zero, "waste": zero, "cost": {"requests": 0.0, "waste": 0.0}}
    b = b or {"requests": zero, "waste": zero, "cost": {"requests": 0.0, "waste": 0.0}}
    return {
        **{
            k: {res: a[k][res] - b[k][res] for res in ("cpu", "memory")}
            for k in ("requests", "waste")
        },
        "cost": {k: a["cost"][k] - b["cost"][k] for k in ("requests", "waste")} if priced else None,
    }


def compare(
    out: dict[str, Any],
    now: list[dict[str, Any]],
    then: list[dict[str, Any]],
    status: dict[str, dict[str, Any]],
    oldest: dict[str, float],
    c: Contract,
    since: float,
) -> dict[str, Any]:
    """allocate()'s `out` with each group's change since then (slice 10.3a): snapshots,
    each cluster's latest run at or before `since` against its latest now, both at
    today's prices. Only what both sides know is compared: a cluster with no run that
    old (named, with its oldest), a container whose requests were unknown then (learning
    or excluded before 10.1) and, when the grouping reads labels, those of a cluster
    where no pod had them then but some do now are left out of both. A group gone
    since shows its whole drop."""
    by, priced = out["by"], c.prices is not None
    clusters = {r["cluster"] for r in now} | set(status)
    missing = {k for k in clusters if (status.get(k) or {}).get("at") is None}
    keys = _label_keys(c, by)
    unknown = {r["workload"] for r in then if r["state"] != "ready" and "requests" not in r}

    def labelled(rows: list[dict[str, Any]]) -> set[str]:
        return {r["cluster"] for r in rows if set(keys) & set(r.get("labels") or {})}

    # A cluster whose pods got the labels since (stored before the Contract listed
    # them, or before kube-state-metrics exported them): every Workload would seem to
    # change group. A run stores labels on all its rows or on none.
    unlabelled = labelled(now) - labelled(then) if keys else set()
    unknown |= {r["workload"] for r in now + then if r["cluster"] in unlabelled}

    def known(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [r for r in rows if r["cluster"] not in missing and r["workload"] not in unknown]

    a, b = _sums(known(now), c, by), _sums(known(then), c, by)
    groups = [
        {**g, "change": _minus(a.get(g["group"]), b.get(g["group"]), priced)} for g in out["groups"]
    ]
    zero = {"cpu": 0.0, "memory": 0.0}
    for key in sorted(set(b) - {g["group"] for g in out["groups"]}):  # gone since
        cost = {"requests": 0.0, "waste": 0.0} if priced else None
        groups.append(
            {"group": key, "workloads": 0, "requests": zero, "waste": zero, "cost": cost}
            | {"change": _minus(None, b[key], priced)}
        )
    return {
        **out,
        "groups": groups,
        "compare": {
            "since": since,
            "clusters": {
                k: {"at": (status.get(k) or {}).get("at"), "oldest": oldest.get(k)}
                for k in sorted(clusters)
            },
            "change": _minus(_total(a.values(), True), _total(b.values(), True), priced),
            "left_out": sorted(unknown),
        },
    }


async def cost(
    store: "Store", c: Contract, by: str, now: float, back: float | None = None
) -> dict[str, Any]:
    """What `sizing cost` and the Cost page show: allocate() of each cluster's latest
    stored run, compared with `back` seconds before `now` when given."""
    c = await listprices.effective(store, c, now)
    clusters = {t.cluster for t in usage.targets(c)}
    rows, status = await store.latest_sizings(c.id, clusters)
    out = {
        "contract": c.id,
        "clusters": status,
        **allocate(rows, c, by),
        "realized": await savings(store, c, now, rows),
    }
    if c._price_notes:  # list prices, or spot (slice 16.1a)
        out["prices"] = c._price_notes
    if back is None:
        return out
    then, before = await store.latest_sizings(c.id, clusters, before=now - back)
    oldest = await store.first_sizings(c.id, clusters)
    return compare(out, rows, then, {k: before.get(k, {}) for k in clusters}, oldest, c, now - back)


HOURLY = 3600.0  # seconds between a Contract's stored Sizing runs


async def store_run(
    store: "Store", c: Contract, at: float, every: float = HOURLY
) -> usage.Collected | None:
    """Size every container of the Contract as of `at` and store the run, unless one
    started less than `every` seconds before; then prune old runs. What was read, or
    None when not due. A Contract without a Prometheus has nothing to run."""
    if not usage.targets(c):
        return None
    run_id = await store.claim_sizing_run(c.id, at, every)
    if run_id is None:
        return None
    since = await store.needs_since(c.id)  # slice 14.1a: each cluster's Needs from there
    found = await asyncio.to_thread(usage.collect, c, c.sizing.windows(), at, since)
    # Each cluster's last good run, read before this one finishes (it would compare
    # with itself)
    before, status = await store.latest_sizings(c.id, set(found.clusters))
    # slice 15.3c: where the cluster's Actuator applies a DaemonSet's Sizings per type
    typed = await store.typed_clusters(c.id)
    now = rows(c, found.by_window, at, found.scaled, found.rooms, typed)
    await store.finish_sizing_run(run_id, c.id, found.clusters, now)
    for cluster, read in found.rooms.items():  # slice 15.1a
        await store.save_rooms(c.id, cluster, at, read)
    for cluster, facts in found.nodes.items():
        await store.save_nodes(c.id, cluster, at, facts)
    for cluster, needs in found.needs.items():
        by = {(w.namespace, w.kind, w.name): hours for w, hours in needs.items()}
        await store.save_needs(c.id, cluster, by)
    if found.bounds:  # slice 14.1b: the Floors of the clusters whose HPAs were read
        kept: dict[str, Needs] = {}
        for n in await store.needs(c.id, set(found.bounds), at - NEEDS_FOR):
            key = (n["namespace"], n["kind"], n["name"])
            kept.setdefault(n["cluster"], {}).setdefault(key, {})[n["hour"]] = n["need"]
        # slice 14.1c: against the Floors the last run showed, before they are replaced
        shown = await store.latest_floors(c.id, set(found.bounds))
        await realize_floors(store, c, at, shown, found.bounds)
        for cluster, computed in floors(c, now, kept, found.bounds, at).items():
            await store.save_floors(c.id, cluster, at, computed)
    await realize(store, c, at, before, status, now, found.clusters, found.untyped)
    await store.prune_sizings(c.id, at)
    return found


async def realize_floors(
    store: "Store",
    c: Contract,
    at: float,
    shown: list[dict[str, Any]],
    bounds: dict[str, dict[Workload, list[Bounds]]],
) -> None:
    """Realized Floor savings (slice 14.1c), per Workload of the clusters whose HPAs this
    run read (`bounds`), against the Floors the last run showed (`shown`): an HPA's
    minimum lower than it was then, where a Floor below that minimum was shown, is
    stored as a drop. It counts down to the new minimum, never below that Floor: lower
    is not the Hub's advice. A minimum raised again raises the drops' `after` back
    toward their `before` (never past it) and a drop back at `before` is deleted, as is
    every drop of a Workload whose HPA is gone or that a second HPA now scales. A
    minimum not known ends nothing, nor does a cluster not read."""
    was = {(f["cluster"], f["namespace"], f["kind"], f["name"]): f for f in shown}
    lone: dict[tuple[str, str, str, str], Bounds] = {}  # the Workloads one HPA scales
    for cluster, found in bounds.items():
        for w, hpas in found.items():
            if len(hpas) == 1:
                lone[cluster, w.namespace, w.kind, w.name] = hpas[0]
    raised: dict[int, float] = {}
    ended: set[int] = set()
    for e in await store.floor_drops(c.id):
        if e["cluster"] not in bounds:
            continue  # not read: ends nothing
        hpa = lone.get((e["cluster"], e["namespace"], e["kind"], e["name"]))
        if hpa is None:
            ended.add(e["id"])
        elif hpa.minimum is not None:
            after = max(e["after"], hpa.minimum)
            if after >= e["before"]:
                ended.add(e["id"])
            elif after != e["after"]:
                raised[e["id"]] = after
    added = []
    for key, hpa in lone.items():
        f = was.get(key)
        if f is None or f["state"] != "ready" or hpa.minimum is None:
            continue
        if f["floor"] < f["minimum"] and hpa.minimum < f["minimum"]:
            where = dict(zip(("cluster", "namespace", "kind", "name"), key, strict=True))
            after = max(hpa.minimum, float(f["floor"]))
            added.append(
                {**where, "at": at, "before": f["minimum"], "after": after, "floor": f["floor"]}
            )
    if added or raised or ended:
        await store.save_floor_drops(c.id, added, raised, ended)


MATCH = 0.10  # a lowered request within 10% of a Sizing followed it
SHOWN_FOR = 14 * 86400.0  # how far back that Sizing may have been shown


def _key(r: dict[str, Any]) -> tuple[str, ...]:
    return tuple(r[k] for k in ("cluster", "namespace", "kind", "name", "container"))


def _of_type(r: dict[str, Any] | None, kind: str) -> dict[str, Any] | None:
    """A row's Sizing on nodes of one instance type, or None."""
    return next((g for g in (r or {}).get("groups") or [] if g["type"] == kind), None)


KEYS = ("cluster", "namespace", "kind", "name", "container")


async def realize(
    store: "Store",
    c: Contract,
    at: float,
    before: list[dict[str, Any]],
    status: dict[str, dict[str, Any]],
    now: list[dict[str, Any]],
    clusters: dict[str, str | None],
    untyped: set[str] | None = None,
) -> None:
    """Realized Savings (slice 10.3b), per container and resource, against each read
    cluster's previous good run: a request lowered to within 10% of a Sizing below the
    old one, shown in the 14 days before, is stored as a drop. A rise raises the
    drops' `after` back toward their `before` (never past it) and a drop back at
    `before` is deleted, as is every drop of a container gone from a read cluster or
    whose request was removed. Fewer pods change nothing here: they count when shown.
    Only rows read from live pods start, raise or end a drop: a container with none
    (kube-state-metrics down, scaled to 0) reads its window's largest request, the
    old one. ponytail: a CronJob whose pods are cleaned up between runs rarely has
    two live runs in a row, so it rarely realizes; read the newest pod's to fix.
    A DaemonSet's container applied per instance type (slice 15.3c, its row `by_type`)
    realizes per type instead, each type's pods' request against that type's Sizing. A
    container's drop in place when it comes to be applied by type becomes one per type,
    from the same run and request: the one of them with the lowest `after` down to what
    that type's pods ask now, the others never below their own `after`, so a chain of
    drops still adds up to the first request less what the type asks (no type drop is
    added for it that run: those made cover it); a type's drop ends when its type is
    gone, or the container is no longer applied by type. `untyped`: clusters whose pods'
    instance types weren't read this run: their DaemonSets' drops are left as they are
    and none is added, their rows' missing groups saying nothing."""
    read = {k for k, error in clusters.items() if error is None}
    then = {_key(r): r for r in before if r["cluster"] in read}
    current = {_key(r): r for r in now if r["cluster"] in read}
    raised: dict[int, float] = {}
    ended: set[int] = set()
    added: list[dict[str, Any]] = []
    made: set[tuple[tuple[str, ...], str, str]] = set()  # (container, resource, type) made now
    stored = await store.realized(c.id)
    lowest: dict[tuple[tuple[str, ...], str], int] = {}  # a container's lowest drop
    for e in sorted(stored, key=lambda e: e["after"]):
        if e.get("type") is None:
            lowest.setdefault((_key(e), e["resource"]), e["id"])
    blind = untyped or set()
    for e in stored:
        key = _key(e)
        if e["cluster"] not in read:
            continue  # a failed read ends nothing
        if e["kind"] == "DaemonSet" and e["cluster"] in blind:
            continue  # its types not read: nor are its drops
        r = current.get(key)
        res, kind = e["resource"], e.get("type")
        typed = bool(r and r.get("by_type"))
        if kind is not None and not typed:  # its types no longer applied, or it is gone
            ended.add(e["id"])
            continue
        if kind is None and typed:  # it comes to be applied by type: a drop per type
            assert r is not None
            if not r.get("live"):
                continue
            for g in r.get("groups") or []:
                asked = (g.get("requests") or {}).get(res)
                if not g.get("live") or asked is None:
                    continue
                if lowest[(key, res)] != e["id"]:
                    asked = max(e["after"], asked)
                if asked < e["before"]:
                    added.append(
                        {k: e[k] for k in (*KEYS, "resource", "at", "prev_run", "before")}
                        | {"type": g["type"], "after": asked, "sizing": e["sizing"]}
                    )
                    made.add((key, res, g["type"]))
            ended.add(e["id"])
            continue
        one = r if kind is None else _of_type(r, kind)
        if one is not None and not one.get("live"):
            continue  # its requests are the window's, not what runs now
        asked = (one or {}).get("requests", {}).get(res)
        if asked is None:  # gone, or its request removed: not a Sizing's saving
            ended.add(e["id"])
            continue
        after = max(e["after"], asked)
        if after >= e["before"]:
            ended.add(e["id"])
        elif after != e["after"]:
            raised[e["id"]] = after
    for key, r in current.items():
        old = then.get(key)
        if old is None or (r["kind"] == "DaemonSet" and r["cluster"] in blind):
            continue
        pairs: list[tuple[dict[str, Any], dict[str, Any] | None, str | None]] = (
            [(g, _of_type(old, g["type"]), g["type"]) for g in r.get("groups") or []]
            if r.get("by_type")
            else [(r, old, None)]
        )
        for mine, theirs, kind in pairs:
            if theirs is None or not (theirs.get("live") and mine.get("live")):
                continue  # unmarked before 10.3b (a type's, before 15.3c)
            for res in ("cpu", "memory"):
                was = (theirs.get("requests") or {}).get(res)  # None before 10.1
                asked = (mine.get("requests") or {}).get(res)
                if was is None or asked is None or asked >= was:
                    continue
                if kind is not None and (key, res, kind) in made:
                    continue
                shown = []
                for t, row in await store.sizing_history(c.id, key, at - SHOWN_FOR):  # type: ignore[arg-type]
                    sized = row if kind is None else _of_type(row, kind)
                    if t < at and sized and sized.get(res) and sized[res]["target"] < was:
                        shown.append(sized[res]["target"])
                near = [s for s in shown if abs(asked - s) <= MATCH * s]
                if near:
                    added.append(
                        {k: r[k] for k in KEYS}
                        | {
                            "type": kind,
                            "resource": res,
                            "at": at,
                            "prev_run": status[r["cluster"]]["run"],
                            "before": was,
                            "after": asked,
                            "sizing": min(near, key=lambda s: abs(asked - s)),
                        }
                    )
    if added or raised or ended:
        await store.save_realized(c.id, added, raised, ended)


async def savings(
    store: "Store", c: Contract, now: float, rows: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """The Contract's realized Savings as of `now`: each stored drop still in place,
    worth (before - after) x the container's pods in its latest run (a type's drop, that
    type's pods: slice 15.3c), at today's prices
    (cores and GiB without them), as a monthly run rate and accrued since the drop
    (that rate x the hours since, so a later partial rise lowers it too), and the rate
    of the drops of the last 7 days. The pods are the latest run's average, as its
    waste's. Only the Contract's current clusters count.

    With them, each HPA minimum lowered toward its Floor (slice 14.1c, `floors`): worth
    the replicas it no longer holds (per stored hour since the drop, the last 7 days
    at most: the old minimum less the larger of what it counts down to and that
    hour's Need rounded up, averaged) times the pod's requests as they are now, every
    container's. None until an hour since the drop is stored."""
    c = await listprices.effective(store, c, now)
    if rows is None:  # else the latest rows, already read
        clusters = {t.cluster for t in usage.targets(c)}
        rows, _ = await store.latest_sizings(c.id, clusters, before=now)
    latest = {_key(r): r for r in rows}
    priced = c.prices is not None
    zero = {"cpu": 0.0, "memory": 0.0}
    out: dict[str, Any] = {
        "currency": c.prices.currency if c.prices else None,
        "rate": dict(zero),  # cores and bytes held less, now
        "accrued": dict(zero),  # core-hours and byte-hours since each drop
        "week": dict(zero),
        "cost": {"rate": 0.0, "accrued": 0.0, "week": 0.0} if priced else None,
        "drops": [],
        "floors": [],
    }
    for e in await store.realized(c.id):
        r = latest.get(_key(e))
        if e["at"] > now or r is None:  # gone, or its cluster no longer the Contract's
            continue
        res = e["resource"]
        # a type's drop is worth that type's pods (slice 15.3c)
        one = r if e.get("type") is None else _of_type(r, e["type"]) or {}
        pods = one.get("share", one.get("pods")) or 0.0
        saved = (e["before"] - e["after"]) * pods
        hours = (now - e["at"]) / 3600
        drop = {
            "workload": "/".join(_key(e)),
            **{k: e[k] for k in ("resource", "at", "before", "after", "sizing")},
            "type": e.get("type"),
            "pods": pods,
            "saved": saved,
            "cost": None,
        }
        out["rate"][res] += saved
        out["accrued"][res] += saved * hours
        if e["at"] >= now - WEEK:
            out["week"][res] += saved
        if p := c.price(e["cluster"]):
            rate = monthly(p, saved, 0.0) if res == "cpu" else monthly(p, 0.0, saved)
            drop["cost"] = {"rate": rate, "accrued": rate / MONTH * hours}
            out["cost"]["rate"] += rate
            out["cost"]["accrued"] += drop["cost"]["accrued"]
            if e["at"] >= now - WEEK:
                out["cost"]["week"] += rate
        out["drops"].append(drop)
    out["drops"].sort(key=lambda d: (-(d["cost"] or {}).get("rate", d["saved"]), d["workload"]))
    lowered = [e for e in await store.floor_drops(c.id) if e["at"] <= now]
    if lowered:
        names = {e["cluster"] for e in lowered}
        needs: dict[tuple[str, ...], dict[float, float]] = {}
        for n in await store.needs(c.id, names, now - WEEK):  # the last 7 days' hours
            key = (n["cluster"], n["namespace"], n["kind"], n["name"])
            needs.setdefault(key, {})[n["hour"]] = n["need"]
        pod: dict[tuple[str, ...], dict[str, float]] = {}  # a replica's requests now
        for r in rows:
            asks = pod.setdefault(_key(r)[:4], dict(zero))
            for res in zero:
                asks[res] += (r.get("requests") or {}).get(res) or 0.0
    for e in lowered:
        key = (e["cluster"], e["namespace"], e["kind"], e["name"])
        if key not in pod:  # gone, or its cluster no longer the Contract's
            continue
        since = e["at"] - e["at"] % 3600  # the hour the drop was seen in
        fewer = [
            max(0.0, e["before"] - max(e["after"], math.ceil(n - 1e-9)))
            for hour, n in needs.get(key, {}).items()
            if hour >= since and hour + 3600 <= now
        ]
        replicas = sum(fewer) / len(fewer) if fewer else 0.0
        saved = {res: replicas * pod[key][res] for res in zero}
        hours = (now - e["at"]) / 3600
        drop = {
            "workload": "/".join(key),
            **{k: e[k] for k in ("at", "before", "after", "floor")},
            "replicas": round(replicas, 2),
            "saved": saved,
            "cost": None,
        }
        for res in zero:
            out["rate"][res] += saved[res]
            out["accrued"][res] += saved[res] * hours
            if e["at"] >= now - WEEK:
                out["week"][res] += saved[res]
        if p := c.price(e["cluster"]):
            rate = monthly(p, **saved)
            drop["cost"] = {"rate": rate, "accrued": rate / MONTH * hours}
            out["cost"]["rate"] += rate
            out["cost"]["accrued"] += drop["cost"]["accrued"]
            if e["at"] >= now - WEEK:
                out["cost"]["week"] += rate
        out["floors"].append(drop)
    return out


WEEK = 7 * 86400.0
TOP = 5  # Workloads named in the digest


def utc(at: float | None) -> str:
    return "never" if at is None else datetime.fromtimestamp(at, UTC).strftime("%Y-%m-%d %H:%M UTC")


def _kinds(report: dict[str, Any]) -> dict[str, set[str]]:
    return {w["workload"]: {r["kind"] for r in w["risks"]} for w in report["workloads"]}


def realized_line(r: dict[str, Any]) -> str | None:
    """The digest's line on realized Savings, or None when nothing was realized."""
    if not any(r["rate"].values()):
        return None
    if r["cost"]:
        cur = r["currency"]
        return (
            f"Realized this week: {money(r['cost']['week'], cur)} "
            f"({money(r['cost']['rate'], cur)} in all)."
        )
    return f"Realized this week: {quantities(r['week'])} ({quantities(r['rate'])} in all)."


def digest_text(
    contract_id: str,
    now: dict[str, Any],
    week: dict[str, Any],
    link: str | None = None,
    realized: dict[str, Any] | None = None,
    floors: dict[str, Any] | None = None,
    nodes: list[dict[str, Any]] | None = None,
) -> str:
    """The weekly digest (Slack mrkdwn) from report() now and as of a week ago: total
    waste and its change, the realized Savings (10.3b), the Floors' line (14.1b,
    floors_report), the top Workloads by waste,
    risk kinds a Workload didn't have a week ago, stale clusters, the containers still
    learning, then the link."""
    total = now["total"]
    lines = [f"*Weekly Sizing digest: {contract_id}*"]
    if not now["clusters"]:
        lines.append("No Sizings stored yet.")
    else:
        currency = now.get("currency")
        compared = any(st["at"] is not None for st in week["clusters"].values())
        then = week["total"]
        if not compared:
            change = "no Sizings from 7 days ago to compare"
        elif currency:  # today's prices on both weeks: a change of usage, not of price
            change = (
                f"{now['cost']['waste'] - week['cost']['waste']:+,.2f} {currency} vs 7 days ago"
            )
        else:
            change = (
                f"{total['cpu'] - then['cpu']:+.2f} cores, "
                f"{(total['memory'] - then['memory']) / GIB:+.2f} GiB vs 7 days ago"
            )
        if currency:
            waste = (
                f"Waste: {money(now['cost']['waste'], currency)} ({change}): {quantities(total)}"
            )
        else:
            waste = f"Waste: {quantities(total)} ({change})"
        lines.append(waste)
        if currency:
            lines.append(f"Requests cost {money(now['cost']['requests'], currency)}.")
        if realized and (line := realized_line(realized)):
            lines.append(line)
        if floors and (line := floors_line(floors, currency)):
            lines.append(line)
        if nodes and (line := nodes_line(nodes, currency)):  # slice 15.4
            lines.append(line)
        top = [w for w in now["workloads"] if rank(w) > 0][:TOP]
        if top:
            lines.append("*Top Workloads by waste*")
            lines += [
                f"{i}. `{w['workload']}`: "
                + (
                    f"{money(w['cost']['waste'], currency)} ({quantities(w['waste'])})"
                    if currency
                    else quantities(w["waste"])
                )
                for i, w in enumerate(top, 1)
            ]
        before = _kinds(week)
        new = {
            name: sorted(kinds - before.get(name, set()) if compared else kinds)
            for name, kinds in _kinds(now).items()
        }
        risky = [f"• `{name}`: {', '.join(k)}" for name, k in new.items() if k]
        if risky:
            lines.append("*New risks since last week*" if compared else "*Risks*")
            lines += risky
        stale = [
            f"• {cluster}: stale since {utc(st['since'])}: {st['error']}"
            if st["error"]
            else f"• {cluster}: never read"
            for cluster, st in sorted(now["clusters"].items())
            if st["error"] or st["at"] is None
        ]
        if stale:
            lines += ["*Stale clusters*", *stale]
        if now["learning"]:
            lines.append(f"Containers still learning: {len(now['learning'])}.")
    text = clean("\n".join(lines))
    return f"{text}\n<{link}|Open the Workloads page>" if link else text


async def digest(store: "Store", c: Contract, at: float, console_url: str | None) -> str:
    """The Contract's digest as of `at`, from its stored Sizings (as `sizing waste`)."""
    c = await listprices.effective(store, c, at)
    clusters = {t.cluster for t in usage.targets(c)}
    now = report(*await store.latest_sizings(c.id, clusters, before=at), c)
    week = report(*await store.latest_sizings(c.id, clusters, before=at - WEEK), c)
    link = f"{console_url.rstrip('/')}/app/?contract={c.id}" if console_url else None
    stored = floors_report(await store.latest_floors(c.id, clusters), c)
    rows, _ = await store.latest_sizings(c.id, clusters, before=at)
    names = {cl.name for cl in c.clusters}
    nodes = nodes_report(
        await store.latest_rooms(c.id, names), c, await store.latest_blockers(c.id, names), at, rows
    )
    return digest_text(c.id, now, week, link, await savings(store, c, at), stored, nodes)
