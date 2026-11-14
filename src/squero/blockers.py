"""Blockers (slice 15.1b): what keeps a node from being removed.

An Actuator reports, hourly, the facts of the pods in its namespaces that may block a
node's removal (`Report`): never a conclusion, since which facts block depends on who
removes nodes there. The Hub applies the rules of the autoscaler the Contract names for
the cluster (`reasons`), `kubectl drain`'s where it names none, and gives each node a
verdict (`verdicts`): blocked, clear or unknown. Clear needs positive evidence: every gap
(no Actuator, a report too old or cut short, a namespace it could not read, pods the Hub
can't see) is unknown. Read-only: nothing is evicted for it."""

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
    alone = "no controller" if not kind else f"a custom controller ({kind})"
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
                out.append(f"local storage ({', '.join(kept)})")
            if p["namespace"] == "kube-system" and budget is None:
                out.append("a kube-system pod with no PodDisruptionBudget")
    else:  # a person, with kubectl drain and no force flags
        if kind not in BUILT_IN:
            out.append(alone)
        if p.get("empty_dir"):
            out.append(f"emptyDir data ({', '.join(sorted(p['empty_dir']))})")
    return out


def verdicts(
    read: dict[str, Any], report: dict[str, Any] | None, cluster: "Cluster", now: float
) -> dict[str, dict[str, Any]]:
    """Each node of a cluster's stored read (usage.rooms) with its verdict: `blocked`
    with the pods that block it and why, `clear`, or `unknown` with why. `report`: its
    Actuator's latest, with the time the Hub `received` it (the clock its age goes by:
    the Actuator's own is not trusted)."""
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
            out[n["node"]] = {"state": "unknown", "why": gap, "blockers": []}
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
