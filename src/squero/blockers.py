"""Blockers (slice 15.1b): what keeps a node from being removed.

An Actuator reports, hourly, the facts of the pods in its namespaces that may block a
node's removal (`Report`): never a conclusion, since which facts block depends on who
removes nodes there. The Hub applies the rules of the autoscaler the Contract names for
the cluster (`reasons`), `kubectl drain`'s where it names none, and gives each node a
verdict (`verdicts`): blocked, clear or unknown. Clear needs positive evidence: no gap
(no Actuator, a report too old or cut short, a namespace it could not read, pods the Hub
can't see) ever gives it. Without a usable report the Hub goes by what Prometheus shows
(`seen`, slice 15.1c): a pod with no controller still blocks, the rest is unknown.
Read-only: nothing is evicted for it.

The nodes that could go (`could_go`, slice 15.4): clear ones whose pods fit, pod by pod,
in the Room of the others with the same taints; `after` gives the same read with the
requests the ready Sizings would leave. Every gap means the node stays."""

import math
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:  # the Actuator imports this module, and reads no Contract
    from squero.contracts import Cluster

MAX_PODS = 1000  # in one report: the Actuator cuts it there and says so
MAX_BYTES = 900_000  # and there: the Hub's proxy refuses a body over 1 MB
STALE = 7200  # seconds since the Hub received a report after which it says nothing
SAFE = "cluster-autoscaler.kubernetes.io/safe-to-evict"
SAFE_VOLUMES = "cluster-autoscaler.kubernetes.io/safe-to-evict-local-volumes"
DO_NOT_DISRUPT = "karpenter.sh/do-not-disrupt"
ANNOTATIONS = (SAFE, SAFE_VOLUMES, DO_NOT_DISRUPT)  # the ones an Actuator sends, as they are
# what `kubectl drain` and Cluster Autoscaler take for a controller; a DaemonSet's pods
# are never sent: they stay with their node
BUILT_IN = ("ReplicationController", "ReplicaSet", "Job", "StatefulSet")
RULES = {
    "cluster-autoscaler": "Cluster Autoscaler's rules",
    "karpenter": "Karpenter's rules",
    None: "kubectl drain's rules (the Contract names no autoscaler)",
}

Name = Field(max_length=253)
# What Kubernetes lets an object's name and a kind be. An Actuator's strings reach the
# console and, from slice 15.4, the Hub MCP's model: a name of another shape is no name
NAME = re.compile(r"[a-z0-9]([-a-z0-9.]*[a-z0-9])?")
KIND = re.compile(r"[A-Za-z][A-Za-z0-9]{0,62}")


class PodFacts(BaseModel):
    """One pod's facts, as its Actuator read them: no verdict."""

    model_config = ConfigDict(extra="forbid")

    namespace: str = Name
    pod: str = Name
    node: str = Name
    # its controller, as its owner reference names it (a ReplicaSet, not its Deployment);
    # both empty: no controller
    kind: str = Field("", max_length=63)
    name: str = Field("", max_length=253)
    empty_dir: list[str] = Field([], max_length=50)  # its emptyDir volumes, by name
    in_memory: list[str] = Field([], max_length=50)  # those of them with medium Memory
    host_path: list[str] = Field([], max_length=50)
    annotations: dict[str, str] = Field({}, max_length=len(ANNOTATIONS))  # of ANNOTATIONS
    # the PodDisruptionBudget covering it that allows the fewest disruptions, and how many
    budget: str | None = Field(None, max_length=253)
    allowed: int | None = Field(None, ge=0)
    started: float | None = None  # when it started, by its status: a duration counts from it


class Report(BaseModel):
    """An Actuator's hourly report: the pods in `namespaces` with a fact that may block.
    `namespaces`: the ones it read in full. `truncated`: there were more than it sent."""

    model_config = ConfigDict(extra="forbid")

    at: float
    namespaces: list[str] = Field(max_length=1000)
    truncated: bool = False
    pods: list[PodFacts]


def unreal(r: Report) -> str | None:
    """Which of a report's fields no Actuator sends, or None (as actuator.unreal, slice
    12.9c): a time that isn't a number or is below zero, a name too long for one."""
    if not (math.isfinite(r.at) and r.at >= 0):
        return "at"
    if any(p.started is not None and not math.isfinite(p.started) for p in r.pods):
        return "pods"
    if any(len(ns) > 253 for ns in r.namespaces):
        return "namespaces"
    for p in r.pods:
        named = [p.namespace, p.pod, p.node, *([] if p.budget is None else [p.budget])]
        if not all(NAME.fullmatch(n) for n in named):  # what Kubernetes itself refuses
            return "pods"
        names = [*p.empty_dir, *p.in_memory, *p.host_path, *p.annotations.values()]
        if any(len(n) > 253 for n in names) or set(p.annotations) - set(ANNOTATIONS):
            return "pods"
    return None


GO_UNITS = {"ns": 1e-9, "us": 1e-6, "µs": 1e-6, "ms": 1e-3, "s": 1.0, "m": 60.0, "h": 3600.0}
GO_DURATION = re.compile(r"(\d+(?:\.\d+)?)(ns|us|µs|ms|s|m|h)")


def go_duration(text: str) -> float | None:
    """Seconds of a Go duration string (`30m`, `1h30m`), or None for anything else."""
    parts = GO_DURATION.findall(text)
    if not parts or "".join(n + u for n, u in parts) != text:
        return None
    return sum(float(n) * GO_UNITS[u] for n, u in parts)


def _custom(kind: str) -> str:
    """A custom controller, with its kind when it is shaped like one: an owner
    reference's kind is whatever its writer put there."""
    return f"a custom controller ({kind})" if KIND.fullmatch(kind) else "a custom controller"


def _listed(volumes: list[str]) -> str:
    """Volume names, or how many when one isn't shaped like a name."""
    if all(NAME.fullmatch(v) for v in volumes):
        return ", ".join(volumes)
    return f"{len(volumes)} {'volume' if len(volumes) == 1 else 'volumes'}"


def reasons(p: dict[str, Any], autoscaler: str | None, now: float = 0.0) -> list[str]:
    """Why a pod with these facts keeps its node at `now`, under who removes nodes
    there; none: it doesn't. From each one's own documentation:

    - all three: a PodDisruptionBudget covering it that allows no disruption (with
      several, all must allow one).
    - Cluster Autoscaler (its FAQ): annotated `safe-to-evict: "false"`; and, unless
      annotated `"true"`: no controller or a custom one (its
      `skip-nodes-with-custom-controller-pods` defaults to true), local storage (a
      hostPath, or an emptyDir not in memory) not all named in
      `safe-to-evict-local-volumes`, a kube-system pod with no PodDisruptionBudget.
    - Karpenter: annotated `karpenter.sh/do-not-disrupt: "true"`, or with a duration
      (`30m`) for that long after the pod started (one whose start isn't known: still);
      any other value is ignored, as Karpenter ignores it.
    - `kubectl drain` without force flags: a pod not managed by one of the built-in
      controllers, a pod using emptyDir."""
    out = []
    noted = p.get("annotations") or {}
    budget = p.get("budget")
    if budget is not None and not p.get("allowed"):
        out.append(f"PodDisruptionBudget {budget} allows no disruption")
    kind = p.get("kind") or ""
    alone = "no controller" if not kind else _custom(kind)
    if autoscaler == "karpenter":
        value = noted.get(DO_NOT_DISRUPT, "")
        lasts, started = go_duration(value), p.get("started")
        if value == "true":
            out.append(f"annotated {DO_NOT_DISRUPT}: true")
        elif lasts is not None and (started is None or now < started + lasts):
            out.append(f"annotated {DO_NOT_DISRUPT}: {value}, for that long after it started")
    elif autoscaler == "cluster-autoscaler":
        if noted.get(SAFE) == "false":
            out.append(f"annotated {SAFE}: false")
        elif noted.get(SAFE) != "true":  # "true" lifts the rules below, not a budget
            if kind not in BUILT_IN:
                out.append(alone)
            on_disk = set(p.get("empty_dir") or []) - set(p.get("in_memory") or [])
            safe = {v.strip() for v in noted.get(SAFE_VOLUMES, "").split(",")}
            kept = sorted((on_disk | set(p.get("host_path") or [])) - safe)
            if kept:
                out.append(f"local storage ({_listed(kept)})")
            if p["namespace"] == "kube-system" and budget is None:
                out.append("a kube-system pod with no PodDisruptionBudget")
    else:  # a person, with kubectl drain and no force flags
        if kind not in BUILT_IN:
            out.append(alone)
        if p.get("empty_dir"):
            out.append(f"emptyDir data ({_listed(sorted(p['empty_dir']))})")
    return out


def seen(
    n: dict[str, Any], read: dict[str, Any], autoscaler: str | None, gap: str
) -> dict[str, Any]:
    """A node's verdict without a usable report (slice 15.1c), from what Prometheus
    shows of the Contract's pods on it: blocked by one with no controller, or a custom
    one, where that blocks (under Cluster Autoscaler's rules an annotation could lift
    it, and Prometheus doesn't show annotations: said); else unknown, since its volumes,
    its annotations and which pods a PodDisruptionBudget covers stay unseen. Never
    clear. `gap`: why there is no report. A budget of its pods' namespaces that allows
    no disruption is named with it."""
    own = [p for p in n["own"] if not p.get("bound")]
    found = []
    if autoscaler != "karpenter":  # its docs name no such rule
        unless = (
            " (unless annotated safe-to-evict: true, which only an Actuator can see)"
            if autoscaler == "cluster-autoscaler"
            else ""
        )
        for p in own:
            kind = p.get("kind")
            if kind is None or kind in BUILT_IN:  # None: read before 15.1c, not known
                continue
            said = "no controller" if not kind else _custom(kind)
            found.append({"namespace": p["namespace"], "pod": p["pod"], "reasons": [said + unless]})
    if found:
        return {"state": "blocked", "why": None, "blockers": found}
    spaces = {p["namespace"] for p in own}
    tight = [
        f"{b['budget']} of {b['namespace']}"
        for b in read.get("budgets") or []
        if b["namespace"] in spaces
    ]
    if tight:
        gap += f"; PodDisruptionBudget {', '.join(tight)} allows no disruption"
    return {"state": "unknown", "why": gap, "blockers": []}


def verdicts(
    read: dict[str, Any], report: dict[str, Any] | None, cluster: "Cluster", now: float
) -> dict[str, dict[str, Any]]:
    """Each node of a cluster's stored read (usage.rooms) with its verdict: `blocked`
    with the pods that block it and why, `clear`, or `unknown` with why. `report`: its
    Actuator's latest, with the time the Hub `received` it (the clock its age goes by:
    the Actuator's own is not trusted). Without a usable one (no Actuator, none yet,
    one too old): what Prometheus shows (`seen`)."""
    gap = None
    if not cluster.actuator_token_secret:
        gap = "the cluster runs no Actuator"
    elif report is None:
        gap = "its Actuator has reported no Blockers yet"
    elif now - report["received"] > STALE:
        at = datetime.fromtimestamp(report["received"], UTC).strftime("%Y-%m-%d %H:%M UTC")
        gap = f"its Actuator's last report is from {at}"
    out: dict[str, dict[str, Any]] = {}
    for n in read.get("nodes") or []:
        if gap or report is None:
            out[n["node"]] = seen(n, read, cluster.autoscaler, gap or "")
            continue
        found = []
        for p in report["pods"]:
            # a namespace the Contract has since left is no longer its own to show
            if p["node"] != n["node"] or p["namespace"] not in cluster.namespaces:
                continue
            said = reasons(p, cluster.autoscaler, now)
            if said:
                found.append({"namespace": p["namespace"], "pod": p["pod"], "reasons": said})
        own = [p for p in n["own"] if not p.get("bound")]
        unread = sorted({p["namespace"] for p in own} - set(report["namespaces"]))
        why = None  # of unknown: asked only where nothing is known to block
        if found:
            pass
        elif "bound" not in n:
            why = "its pods were read before the Hub counted the ones that stay with a node"
        elif report.get("truncated"):
            why = f"its Actuator's report was cut at {MAX_PODS} pods"
        elif unread:
            why = f"its Actuator could not read {', '.join(unread)}"
        elif n["pods"] - n["bound"] - len(own) > 0:
            hidden = n["pods"] - n["bound"] - len(own)
            why = f"{hidden} {'pod' if hidden == 1 else 'pods'} not visible"
        state = "blocked" if found else "unknown" if why else "clear"
        out[n["node"]] = {"state": state, "why": why, "blockers": found}
    return out


# --- the nodes that could go (slice 15.4) ----------------------------------------------

OWN_TAINT = "node.kubernetes.io/"  # the ones Kubernetes sets itself: cordon, conditions
CONTROL_PLANE = ("node-role.kubernetes.io/control-plane", "node-role.kubernetes.io/master")


def _group(n: dict[str, Any]) -> tuple[str, ...]:
    """The taints a pod must tolerate to run on a node: its own, without those
    Kubernetes sets itself (a cordoned node's pods ran on it before) and without
    `PreferNoSchedule`, which keeps no pod out. Pods move only between nodes of one
    group: a Workload's tolerations can't be seen."""
    kept = (t for t in n["taints"] if not t.startswith(OWN_TAINT))
    return tuple(sorted(t for t in kept if not t.endswith(":PreferNoSchedule")))


def _closed(n: dict[str, Any]) -> bool:
    """Whether a node takes no new pod now: cordoned, or under a taint Kubernetes set
    itself for its condition (not ready, under pressure). Its own pods can still leave."""
    held = (t for t in n["taints"] if t.startswith(OWN_TAINT))
    return n["cordoned"] or any(not t.endswith(":PreferNoSchedule") for t in held)


def after(read: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    """A cluster's stored read with the requests its pods would have once its ready
    Sizings (`rows`: the cluster's stored ones) are applied, container by container. A
    container with no ready Sizing, or of a pod whose Workload isn't known, keeps its
    own request: nothing is freed that no Sizing frees. A container that asks nothing
    today, so that the pod doesn't list it, asks its Sizing then. Each node's requests
    move by what its own pods' did; the pods the Hub can't see stay as they are."""
    sized = {
        (r["namespace"], r["kind"], r["name"], r["container"]): {
            res: r[res]["target"] for res in ("cpu", "memory") if r.get(res)
        }
        for r in rows
        if r.get("state") == "ready"
    }
    of: dict[tuple[str, str, str], dict[str, dict[str, float]]] = {}  # by Workload
    for (ns, kind, name, container), target in sized.items():
        of.setdefault((ns, kind, name), {})[container] = target
    nodes = []
    for n in read.get("nodes") or []:
        own, moved = [], {"cpu": 0.0, "memory": 0.0}
        for p in n["own"]:
            w = p.get("workload")
            targets = of.get((p["namespace"], w["kind"], w["name"]), {}) if w else {}
            asks = {"cpu": 0.0, "memory": 0.0}
            listed = set()
            for ctr in p.get("containers") or []:
                listed.add(ctr["name"])
                for res in asks:
                    asks[res] += targets.get(ctr["name"], {}).get(res, ctr[res])
            if "containers" not in p:  # read before 15.4: as it is
                asks = {res: p[res] for res in asks}
            else:  # a container asking nothing today has no series: its Sizing sets one
                for container, target in targets.items():
                    if container not in listed:
                        for res in asks:
                            asks[res] += target.get(res, 0.0)
            for res in moved:
                moved[res] += asks[res] - p[res]
            own.append({**p, **asks})
        requests = {res: n["requests"][res] + moved[res] for res in moved}
        nodes.append({**n, "own": own, "requests": requests})
    return {**read, "nodes": nodes}


def could_go(read: dict[str, Any], verdict: dict[str, dict[str, Any]]) -> list[str] | None:
    """The nodes of a cluster's read that could be removed, by name, in the order the
    arithmetic lets them go: a node whose `verdict` is clear (so every pod on it is
    known, and none blocks), not a control-plane node, each of whose pods that would
    move (its DaemonSets' and static pods go with it) fits whole, CPU and memory and a
    pod slot, on one other node of its taint group that takes pods (not cordoned, not
    under a condition taint) and isn't going itself. Fewest requests first, the largest
    pod placed first, each on the first node by name with room, and what a node
    receives is no longer free for the next;
    a node that received pods and then goes itself must find room for those too.

    An upper bound: affinity, spread and tolerations can't be seen. A node without its
    allocatable or its pod slots neither goes nor receives. None for a read from before
    15.4, which lacks what this goes by."""
    nodes = read.get("nodes") or []
    if any("slots" not in n or any("containers" not in p for p in n["own"]) for n in nodes):
        return None

    def whole(n: dict[str, Any]) -> bool:
        return n["slots"] is not None and all(r in n["allocatable"] for r in ("cpu", "memory"))

    def moving(n: dict[str, Any]) -> list[dict[str, Any]]:
        return [p for p in n["own"] if not p.get("bound")]

    free = {
        n["node"]: [
            n["allocatable"]["cpu"] - n["requests"]["cpu"],
            n["allocatable"]["memory"] - n["requests"]["memory"],
            n["slots"] - n["pods"],
        ]
        for n in nodes
        if whole(n) and not _closed(n)
    }
    group = {n["node"]: _group(n) for n in nodes}
    candidates = [
        n
        for n in nodes
        if verdict[n["node"]]["state"] == "clear"
        and whole(n)
        and not any(t.startswith(CONTROL_PLANE) for t in n["taints"])
    ]
    candidates.sort(
        key=lambda n: (
            sum(p["cpu"] for p in moving(n)),
            sum(p["memory"] for p in moving(n)),
            n["node"],
        )
    )
    gone = []
    landed: dict[str, list[dict[str, Any]]] = {}  # the pods each node has received
    for n in candidates:
        name = n["node"]
        room = {
            other: list(left)
            for other, left in sorted(free.items())
            if other != name and group[other] == group[name]
        }
        pods = [*moving(n), *landed.get(name, [])]
        pods.sort(key=lambda p: (-p["cpu"], -p["memory"], p["namespace"], p["pod"]))
        placed = []
        for p in pods:
            fit = next(
                (
                    other
                    for other, left in room.items()
                    if left[0] >= p["cpu"] - 1e-9 and left[1] >= p["memory"] - 1e-9 and left[2] >= 1
                ),
                None,
            )
            if fit is None:
                break
            left = room[fit]
            left[0], left[1], left[2] = left[0] - p["cpu"], left[1] - p["memory"], left[2] - 1
            placed.append((fit, p))
        else:
            free.pop(name, None)  # it goes: nothing more lands on it
            free.update(room)
            landed.pop(name, None)
            for other, p in placed:
                landed.setdefault(other, []).append(p)
            gone.append(name)
    return gone
