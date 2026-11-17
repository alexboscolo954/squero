"""Deploy verification (slice 8.2): the deploys the Hub sees in a Contract's clusters,
each verified once by a Check after a delay.

The Hub polls the Contract's own kubernetes `recent_changes` (no model call). A change
is known by its identity (namespace, object, revision), never by its whole entry: its
age changes every poll, and an ArgoCD operation and its later history entry are one
deploy. Only identity reaches the Check's prompt: a change cause or sync message is
written by whoever deploys, so the agent reads those through its tools, as data.
"""

from typing import Any

POLL_SECONDS = 120
WINDOW_MINUTES = 60  # recent_changes looks this far back; seen keys live as long
OBJECTS = ("deployment", "argocd_application", "flux_kustomization", "flux_helmrelease")
SKIPPED = ("not ready",)  # a Flux condition, not a deploy

Key = tuple[str, str, str, str]


def identity(entry: dict[str, Any]) -> Key | None:
    """(namespace, object kind, object name, revision), or None for what isn't a
    deploy: a notice (an error reading GitOps objects) or a Flux "not ready"."""
    if entry.get("change") in SKIPPED:
        return None
    kind = next((k for k in OBJECTS if entry.get(k)), None)  # a notice names none
    if kind is None:
        return None
    revision = entry.get("revision") or entry.get("release_version") or entry.get("chart")
    return (str(entry.get("namespace", "")), kind, str(entry[kind]), str(revision))


def wanted(namespace: str, only: list[str]) -> bool:
    """A change's namespace (a GitOps object's targets joined by ", ") is wanted when
    every one of them is in `only`; an empty `only` wants all."""
    return not only or set(namespace.split(", ")) <= set(only)


class Seen:
    """One (Contract, cluster)'s known deploys. The first successful poll is the
    baseline: what it lists is known, never verified. Keys are kept until they have
    been out of every poll for the window, so a poll that misses some (a GitOps
    403, the result cap) never makes them new again."""

    def __init__(self) -> None:
        self.known: dict[Key, float] = {}
        self.baselined = False

    def new(self, entries: list[dict[str, Any]], at: float) -> list[dict[str, Any]]:
        found = []
        for entry in entries:
            key = identity(entry)
            if key is None:
                continue
            if key not in self.known and self.baselined:
                found.append(entry)
            self.known[key] = at
        horizon = at - 2 * WINDOW_MINUTES * 60
        self.known = {k: t for k, t in self.known.items() if t >= horizon}
        self.baselined = True
        return found


def summary(entry: dict[str, Any]) -> dict[str, str]:
    """The identity of a change, for the prompt and the console."""
    key = identity(entry)
    assert key is not None
    namespace, kind, name, revision = key
    return {"namespace": namespace, "object": f"{kind} {name}", "revision": revision}


def workload(pod: dict[str, Any]) -> str:
    """A list_pods entry's workload name: its `workload` (kind/name, from its owners,
    audit A11), or, from a Relay older than that, guessed from the pod's name as a
    Deployment's (name-<rs hash>-<id>)."""
    if of := pod.get("workload"):
        return str(of).partition("/")[2]
    parts = str(pod["name"]).split("-")
    return "-".join(parts[:-2]) if len(parts) > 2 else parts[0]


def unhealthy(pods: list[dict[str, Any]]) -> set[str]:
    """Workloads with a pod that isn't running ready (list_pods' entries)."""
    out = set()
    for p in pods:
        ready, _, total = str(p.get("ready", "0/0")).partition("/")
        done = p.get("phase") == "Succeeded"
        if not done and (p.get("phase") != "Running" or ready != total):
            out.add(workload(p))
    return out


def name(cluster: str, namespace: str) -> str:
    """Its Check name: never a slug, so never a scheduled Check's."""
    return f"deploy:{cluster}/{namespace}"


def prompt(
    cluster: str,
    namespace: str,
    changes: list[dict[str, str]],
    extra: str | None,
    before: list[str] | None = None,
) -> str:
    """before: the workloads that weren't running ready at the Hub's look just before
    the deploy; None when it couldn't tell."""
    listed = "\n".join(f"- {c['object']}, revision {c['revision']}" for c in changes)
    text = (
        f"Verify a deploy in namespace {namespace} of cluster {cluster}. These changed a "
        f"few minutes ago:\n{listed}\n"
        "Pass only if what changed is healthy now (every changed deployment has all its "
        "replicas ready on the new revision, its pods have not restarted since, no new "
        "warning events, nothing failing in its logs) and nothing else in the namespace "
        "started failing after the change. Something that was already failing the same "
        "way before the change is not this change's doing: say so in the summary, but "
        "don't fail the Check for it. Read the details of each change with recent_changes."
    )
    if before is None:
        text += "\nThe Hub couldn't tell what was failing before the change."
    elif before:
        text += (
            "\nAt the Hub's look just before the change, these were already failing: "
            f"{', '.join(before)}. Fail the Check for them only if the change made them fail "
            "in a new way."
        )
    else:
        text += "\nAt the Hub's look just before the change, nothing in it was failing."
    return f"{text}\n{extra}" if extra else text
