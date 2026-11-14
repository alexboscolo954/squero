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


def _found(p: dict[str, Any], autoscaler: str | None, now: float) -> list[tuple[str, str]]:
    """What makes a pod with these facts keep its node at `now`, under who removes
    nodes there, each as (which rule, in words); none: it doesn't. From each one's own
    documentation:

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
        out.append(("budget", f"PodDisruptionBudget {budget} allows no disruption"))
    kind = p.get("kind") or ""
    alone = ("no_controller", "no controller") if not kind else ("custom", _custom(kind))
    if autoscaler == "karpenter":
        value = noted.get(DO_NOT_DISRUPT, "")
        lasts, started = go_duration(value), p.get("started")
        if value == "true":
            out.append(("do_not_disrupt", f"annotated {DO_NOT_DISRUPT}: true"))
        elif lasts is not None and (started is None or now < started + lasts):
            said = f"annotated {DO_NOT_DISRUPT}: {value}, for that long after it started"
            out.append(("do_not_disrupt_for", said))
    elif autoscaler == "cluster-autoscaler":
        if noted.get(SAFE) == "false":
            out.append(("safe_false", f"annotated {SAFE}: false"))
        elif noted.get(SAFE) != "true":  # "true" lifts the rules below, not a budget
            if kind not in BUILT_IN:
                out.append(alone)
            safe = {v.strip() for v in noted.get(SAFE_VOLUMES, "").split(",")}
            kept = sorted(set(_local(p)) - safe)
            if kept:
                out.append(("local_storage", f"local storage ({_listed(kept)})"))
            if p["namespace"] == "kube-system" and budget is None:
                out.append(("kube_system", "a kube-system pod with no PodDisruptionBudget"))
    else:  # a person, with kubectl drain and no force flags
        if kind not in BUILT_IN:
            out.append(alone)
        if p.get("empty_dir"):
            out.append(("emptydir", f"emptyDir data ({_listed(sorted(p['empty_dir']))})"))
    return out


def _local(p: dict[str, Any]) -> list[str]:
    """A pod's local volumes as Cluster Autoscaler counts them: its hostPaths and its
    emptyDirs that aren't in memory."""
    on_disk = set(p.get("empty_dir") or []) - set(p.get("in_memory") or [])
    return sorted(on_disk | set(p.get("host_path") or []))


def reasons(p: dict[str, Any], autoscaler: str | None, now: float = 0.0) -> list[str]:
    """Why a pod with these facts keeps its node at `now`, in words (`_found`)."""
    return [text for _, text in _found(p, autoscaler, now)]


# --- the change that removes a Blocker (slice 15.6) ------------------------------------

# The autoscaler's own settings that decide the same thing for the whole cluster: named
# for the Operator to check, never compared (they live in kube-system or in objects of
# no namespace, which the Hub doesn't read). Defaults as their documentation gave them
# when looked up (2026-11-12): a cluster may run others.
SETTINGS: dict[str | None, list[str]] = {
    "cluster-autoscaler": [
        "--skip-nodes-with-local-storage (true by default): false lets it remove a node "
        "whose pods have local storage, and their data with it",
        "--skip-nodes-with-system-pods (true by default): false lets it remove a node "
        "holding kube-system pods no PodDisruptionBudget covers",
        "--skip-nodes-with-custom-controller-pods (true by default): false lets it evict "
        "pods of custom controllers",
        "--scale-down-utilization-threshold (0.5 by default): a node whose pods ask more "
        "than that share of it is not considered for removal",
    ],
    "karpenter": [
        "NodePool spec.disruption.consolidationPolicy (WhenEmptyOrUnderutilized by "
        "default): WhenEmpty removes only empty nodes",
        "NodePool spec.disruption.consolidateAfter (0s by default): how long a node "
        "waits before it is consolidated",
        "NodePool spec.disruption.budgets (10% of nodes by default): how many nodes may "
        "be disrupted at once",
    ],
    None: [],
}
PLACEHOLDER = "REPLACE-WITH-A-LABEL-OF-THE-POD"  # a label no pod has: it selects none


def _pod(p: dict[str, Any]) -> str:
    """A pod named for a manifest's comment, when its namespace and name are shaped
    like names: without a report they are Prometheus' labels, and a comment that a
    newline ends is no longer a comment."""
    if NAME.fullmatch(p["namespace"]) and NAME.fullmatch(p["pod"]):
        return f"pod {p['namespace']}/{p['pod']}"
    return "this pod"


def _annotation(p: dict[str, Any], w: dict[str, str] | None, key: str, value: str, why: str) -> str:
    """A manifest fragment that sets one annotation where the pod gets its annotations
    from: its Workload's pod template when the Hub knows a Workload of a kind with one,
    else the pod itself. `why`: what it costs, said in the fragment, which travels
    without the page around it. Names not shaped like names are left out, not quoted."""
    placed = NAME.fullmatch(p["namespace"])  # a Workload is named with its namespace
    known = w and w["kind"] != "Pod" and KIND.fullmatch(w["kind"]) and NAME.fullmatch(w["name"])
    known = known and placed
    builtin = known and w and w["kind"] in (*BUILT_IN, "Deployment", "DaemonSet", "CronJob")
    pod = _pod(p)
    lines = [f"# {why}"]
    if builtin and w:
        lines.append(f"# On the pod template of {w['kind']} {p['namespace']}/{w['name']}:")
        nest = ["spec:", "  template:", "    metadata:", "      annotations:"]
        # what changing a template does is part of what it costs
        if w["kind"] == "CronJob":
            nest = ["spec:", "  jobTemplate:", "    spec:", "      template:"]
            nest += ["        metadata:", "          annotations:"]
            lines.append("# It reaches the Jobs started after it, not the ones running.")
        elif w["kind"] == "Job":
            lines.append(
                "# A Job's template can't be changed once created: set it where it is made."
            )
        else:
            lines.append("# Changing a pod template restarts every pod of it, one rollout.")
    else:
        where = f"{w['kind']} {p['namespace']}/{w['name']} sets" if known and w else None
        lines.append(
            f"# On {pod}, wherever {where} its pods' annotations:"
            if where
            else f"# On {pod} itself: the Hub knows no template it comes from."
        )
        nest = ["metadata:", "  annotations:"]
    indent = " " * (len(nest[-1]) - len(nest[-1].lstrip()) + 2)
    return "\n".join([*lines, *nest, f'{indent}{key}: "{value}"'])


def changes(
    p: dict[str, Any], autoscaler: str | None, now: float = 0.0, w: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    """For each reason a pod keeps its node (`_found`), the change that removes it:
    `change` in words, with what it costs, and a `manifest` to copy where the change
    adds something. Words only where it would undo something set on purpose (a budget
    allowing no disruption, which may also be a pod that is down right now; an
    annotation that says "don't"): the Operator decides that with the client. Under
    `kubectl drain`'s rules the change is a flag of the drain, said with what it
    deletes. `w`: the pod's Workload, where the Hub's read resolved one."""
    out = []
    pod = _pod(p)
    for rule, text in _found(p, autoscaler, now):
        manifest = None
        if rule == "budget":
            change = (
                "check first that its pods are healthy: a budget also allows no "
                "disruption while one of them is down. Then give its Workload more "
                f"replicas than PodDisruptionBudget {p['budget']} requires, or let that "
                "budget allow one disruption, so one of its pods may be down while a "
                "node is removed"
            )
        elif rule == "safe_false":
            change = (
                f"remove the annotation {SAFE} from its pod template if its pods may "
                "be evicted: someone set it on purpose"
            )
        elif rule == "do_not_disrupt":
            change = (
                f"remove the annotation {DO_NOT_DISRUPT} from its pod template if its "
                "pods may be disrupted: someone set it on purpose"
            )
        elif rule == "do_not_disrupt_for":
            change = "it ends by itself, that long after the pod started; nothing to change"
        elif rule == "no_controller" and autoscaler == "cluster-autoscaler":
            change = (
                "run it under a Deployment, StatefulSet or Job, which brings an evicted "
                "pod back; or, if losing it is acceptable, the annotation below lets "
                "Cluster Autoscaler evict it"
            )
            why = f"{pod} has no controller: once evicted it is gone for good."
            manifest = _annotation(p, None, SAFE, "true", why)
        elif rule == "no_controller":
            change = (
                "run it under a Deployment, StatefulSet or Job, which brings an evicted "
                "pod back; `kubectl drain --force` deletes it, and nothing does"
            )
        elif rule == "custom" and autoscaler == "cluster-autoscaler":
            change = (
                "if its controller brings an evicted pod back, the annotation below "
                "lets Cluster Autoscaler evict it (its "
                "--skip-nodes-with-custom-controller-pods=false does so for all such pods)"
            )
            why = f"{pod} is a custom controller's: only if that controller recreates it."
            manifest = _annotation(p, w, SAFE, "true", why)
        elif rule == "custom":
            change = (
                "`kubectl drain --force` evicts it; whether it comes back is up to its controller"
            )
        elif rule == "local_storage":
            every = _local(p)
            named = all(NAME.fullmatch(v) for v in every)
            value = ",".join(every) if named else "REPLACE-WITH-EVERY-LOCAL-VOLUME"
            change = (
                "if the data in its local volumes may be lost when the pod moves, name "
                "every one of them in the annotation below (Cluster Autoscaler's "
                "--skip-nodes-with-local-storage=false does so for all pods)"
            )
            why = f"{pod}: the data in these local volumes is lost whenever it is replaced."
            manifest = _annotation(p, w, SAFE_VOLUMES, value, why)
        elif rule == "emptydir":
            change = (
                "`kubectl drain --delete-emptydir-data` removes the node anyway: the "
                "data in its emptyDir volumes is deleted with the pod"
            )
        else:  # kube_system
            name = w["name"] if w and w["kind"] != "Pod" and NAME.fullmatch(w["name"]) else p["pod"]
            name = name if NAME.fullmatch(name) else "REPLACE-WITH-A-NAME"
            change = (
                "give it a PodDisruptionBudget, as below, with its selector set to the "
                "pod's labels, which the Hub doesn't read (Cluster Autoscaler's "
                "--skip-nodes-with-system-pods=false does so for all kube-system pods)"
            )
            manifest = "\n".join(
                [
                    f"# {pod}: Cluster Autoscaler removes no node holding a kube-system pod",
                    "# that no PodDisruptionBudget covers. Replace the selector with the pod's",
                    "# own labels: as it stands it selects no pod, and changes nothing.",
                    "apiVersion: policy/v1",
                    "kind: PodDisruptionBudget",
                    "metadata:",
                    f"  name: {name}",
                    "  namespace: kube-system",
                    "spec:",
                    "  maxUnavailable: 1",
                    "  selector:",
                    "    matchLabels:",
                    f"      {PLACEHOLDER}: REPLACE-WITH-ITS-VALUE",
                ]
            )
        out.append({"reason": text, "change": change, "manifest": manifest})
    return out


def _named(w: dict[str, str] | None) -> str | None:
    """A pod's Workload as `Deployment/web`, to group its replicas by; None: not known."""
    return f"{w['kind']}/{w['name']}" if w else None


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
            w = p.get("workload")
            facts = {"namespace": p["namespace"], "pod": p["pod"], "kind": kind}
            change = changes(facts, autoscaler, 0.0, w)[0]  # its controller: all that is seen
            change["reason"] += unless
            found.append(
                {
                    **{"namespace": p["namespace"], "pod": p["pod"], "workload": _named(w)},
                    **{"reasons": [change["reason"]], "changes": [change]},
                }
            )
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
        whose = {(p["namespace"], p["pod"]): p.get("workload") for p in n["own"]}
        for p in report["pods"]:
            # a namespace the Contract has since left is no longer its own to show
            if p["node"] != n["node"] or p["namespace"] not in cluster.namespaces:
                continue
            w = whose.get((p["namespace"], p["pod"]))
            made = changes(p, cluster.autoscaler, now, w)
            if made:
                found.append(
                    {
                        **{"namespace": p["namespace"], "pod": p["pod"], "workload": _named(w)},
                        **{"reasons": [c["reason"] for c in made], "changes": made},
                    }
                )
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
