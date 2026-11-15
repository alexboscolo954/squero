"""The Actuator (slice 12.1): applies the Hub's Approved Sizings inside a client's cluster.

It runs in that cluster with its own token, bound to one Contract's cluster, and RBAC that
can resize pods but never edit a workload (which could change its image). Every minute
it pulls its desired state from the Hub, resizes the pods of each new Approved Sizing's
workload in place (Kubernetes 1.33+), keeps what it did in one ConfigMap so a restart
applies nothing twice, writes an Event on the workload and reports each outcome (an
Actuation) to the Hub. The Hub decides and never pushes; a Kill switch reaches it as no
Sizings at all.

What it applied it holds (slice 12.2): its pod-creation webhook gives a new pod of that
Deployment the same requests (it fails open: with the Actuator down, pods start from their
template), until a person changes the template's requests, which hands the container back.

Before a resize (slice 12.3) it checks what would refuse or fight it: a pod's QoS class, a
VPA, an HPA on a resource's utilization, the namespace's LimitRange and ResourceQuota. Then
it resizes one canary pod, watches it 10 minutes, and the others one at a time, each until
the kubelet allocated it and the pod is Ready; an Infeasible resize, or one not done in 10
minutes, stops it there. Deployments, and from slice 12.8a StatefulSets and DaemonSets: a
DaemonSet's new pod gets only lowered requests from the webhook (it must fit any node), a
DaemonSet's pod whose node has no room is left as it runs while the others go on, and
neither kind's pod is ever evicted. From slice 12.8b Argo Rollouts, in place only: read as a
Deployment (`Apps`), their pods their own ReplicaSets', never evicted. From slice 12.8c
CronJobs, through the webhook only: held at once for their next run's pods, that first run
watched until it ends (`first_run`), a pod of it Failed or OOM-killed a Rollback.

Slice 12.9a (an audit's findings): a network failure is an ApiException (`Calm`), a minute
that fails keeps and reports what it did, a rollout waits out a passing failure, a put back
some pod refused is finished later (`finish`), restarts are counted from when a rollout
begins, and a refused approval leaves the earlier hold as it is. Slice 12.9b: the Kill
switch as last heard and the reports not yet taken are kept in its ConfigMap (`state.json`),
an approval a passing failure met is tried again, a rollout scaled to zero ends applied, a
CronJob's run is told by a recent minute in its Job's name, and an approval made again after
a Revert (`again`) applies the run that Revert undid. Slice 12.10: a pod whose node is gone
(`lost`) is never a Sizing's fault, nor given one's requests: no Rollback for its NotReady or
its run's Failed pod, and a rollout goes on without it. Slice 12.11: nor is a container its
node's own restart started again (`rebooted`).

Slice 13.1a: with the Contract's `reflexes.oom` it reports the OOM kills of the containers
the Hub lists, told as container-limit or node-level. Slice 13.1b (ADR 0005): it answers a
container-limit one with a Step-up, the container's memory limit and request raised
together on every pod in place (`step_up`). Slice 13.4: the Startup boost, a new pod's
CPU limit times its rule's factor from the webhook, back in place once the pod is Ready or
5 minutes old (`keep_boosts`). Slice 13.3b: the Burst, a container's CPU limit raised by a
quarter of its template's on every pod in place when the Hub's hourly read says it is
throttled (`burst`), kept and ended each minute (`keep_bursts`). Those three are the only
times it changes a limit.

Under Automation (slice 12.5) it also applies, unasked, the newest Sizing of each container
the Contract automates: a change under 10% ignored, an increase at once, a decrease at most
once a day. A workload a person annotated `squero-actuator/pause: "true"` is left alone,
as under the Kill switch. While the Hub is unreachable it changes nothing new, and says so
in an Event after 15 minutes.

A Revert (slice 12.6a), the Operator's undo, puts a container's pods back to its template's
requests in place, as a rollback does, and the Actuator stops holding it.

With the Contract's eviction on (slice 12.7), a pod whose resize its node can never fit (the
kubelet's Infeasible, or from Kubernetes 1.35 the API server's refusal) is evicted through
the Eviction API (PodDisruptionBudgets hold) inside one of the Contract's windows, one at a
time and at least 5 minutes apart; the webhook gives its
replacement the requests. Outside a window the rollout waits on it; a replacement not Ready
in 10 minutes is a rollback. A pod of a Deployment with one replica, or that no
PodDisruptionBudget covers, is never evicted: its rollout fails as with eviction off.
"""

import base64
import copy
import json
import logging
import math
import re
import ssl
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
import urllib3
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from kubernetes.utils.quantity import parse_quantity
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from squero import blockers
from squero.contracts import on_demand_of

log = logging.getLogger("squero.actuator")

CONFIGMAP = "squero-actuator"  # in its own namespace: what it applied, by container
KEY = "applied.json"
KEPT_REPORTS = 200  # of its unsent reports, the newest kept across a restart: the
# ConfigMap's megabyte is its holds' first
STATE = "state.json"  # beside it: the Kill switch as last heard, reports not yet taken
RUN_AGE = 30 * 1440  # minutes: how old a CronJob run's scheduled time may be (slice 12.9b)
MAX_REPORTS = 1000  # per POST, and kept unsent while the Hub is unreachable
BLOCKERS_EVERY = 3600  # seconds between its reports of Blockers' facts (slice 15.1b)
BLOCKERS_RETRY = 300  # after one that didn't reach the Hub: its reports go stale at two hours
PAGE = 200  # pods a list call when it reads whole namespaces: its memory limit is 256Mi
MIRROR = "kubernetes.io/config.mirror"  # on a node's own static pods
MIN_MINOR = 33  # Kubernetes 1.33: in-place pod resize on by default
MI = 2**20
WEBHOOK = "squero-actuator"  # its MutatingWebhookConfiguration and its Service, by name
WEBHOOK_PORT = 8443
MAX_REVIEW = 1024 * 1024  # bytes: an AdmissionReview of one pod is far smaller
CANARY_SECONDS = 600  # the first pod resized is watched this long before the others
WAIT_SECONDS = 600  # how long one pod's resize may take, Deferred included
WATCH_SECONDS = 3600  # resized pods are watched this long for a rollback (slice 12.4)
NOT_READY_SECONDS = 120  # a resized pod NotReady this long is a reason to roll back
BACKOFF_SECONDS = 86400  # after a rollback, no new Sizing of that container this long, doubling
ROLLBACKS_FOR = 7 * 86400  # how far back rollbacks count for the doubling
ENDING = 3  # Rollbacks of a Workload in 7 days that end its Automation (slice 12.5)
OOM_RECENT = 600  # an OOM kill this old is no news; one report a container this often (13.1a)
OOM_NEAR = 120  # a node's SystemOOM Event this close to a kill makes it node-level
OOM_SHOWN = 5  # pods named in one report's reason
# the Step-up (slice 13.1b, ADR 0005): to the larger of 1.2 times and 100 MiB more (VPA's
# numbers), one a container every 10 minutes, 10 a cluster an hour
STEP_FACTOR = Decimal("1.2")
STEP_MORE = 100 * 1024 * 1024
STEP_EVERY = 600
STEPS_AN_HOUR = 10
# how a Step-up made begins its reason (pods only brought to one: "brought to"), and how a
# container at its Step-up's cap is said: what the Hub tells the Operator of (slice 13.1c)
STEP_SAID = "memory limit "
AT_CAP = "No Step-up: it is at "
# the Burst (slice 13.3b): a quarter of its template's CPU limit a step; a container's at
# least 45 minutes apart (the Hub's reads are an hour apart, and a step lands up to a minute
# after its read); at most 5 of the cluster's 10 an hour, so that an OOM crash loop's
# Step-up never waits behind throttling; kept past its term while its CPU peak is above
# 90% of its template's limit
BURST_STEP = Decimal("0.25")
BURST_EVERY = 2700
BURSTS_AN_HOUR = 5
BURST_KEEP = Decimal("0.9")
BURST_SAID = "CPU limit "  # how a Burst made begins its reason, as STEP_SAID
BURST_CAP = "No Burst: it is at "
ROLLING = "its Sizing is still rolling out"  # minutes at most: a Burst waits it out
OOM_REPORTS = 50  # containers reported in one minute, the rest the next: a node out of
# memory kills many at once, and of its unsent reports only the newest 200 outlive a restart
# why Kubernetes removed a pod whose node is gone: its taint manager, or its pod garbage
# collector once the Node is deleted (a DisruptionTarget condition's reasons, slice 12.10)
NODE_GONE = ("DeletionByTaintManager", "DeletionByPodGC")
SHUTDOWN = "imminent node shutdown"  # in a pod's message, its reason Terminated: the kubelet's
CHURN = Decimal("0.10")  # under Automation, a change smaller than this is ignored (slice 12.5)
LOWER_EVERY = 86400  # under Automation, a container's requests go down at most this often
UNREACHABLE_SECONDS = 900  # the Hub unanswered this long: an Event says so
PAUSE = "squero-actuator/pause"  # a workload annotated "true" is left alone
# the Startup boost (slice 13.4): on a pod the webhook boosted, each boosted container's own
# CPU limit (JSON, name to quantity), given back once the pod is Ready or this long started
BOOST = "squero-actuator/startup-boost"
BOOST_SECONDS = 300
# the kinds it resizes (StatefulSet and DaemonSet from slice 12.8a, Argo Rollouts from 12.8b),
# as named in its API calls (a Rollout's through `Apps`)
KINDS = {
    "Deployment": "deployment",
    "StatefulSet": "stateful_set",
    "DaemonSet": "daemon_set",
    "Rollout": "rollout",
    "CronJob": "cron_job",  # slice 12.8c: through the webhook only
}
ARGO = ("argoproj.io", "v1alpha1")  # an Argo Rollout's API group and version
API = {"Rollout": "/".join(ARGO), "CronJob": "batch/v1"}  # others: apps/v1
RUN_WATCH = 7 * 86400  # a CronJob's first run under its hold is watched this long at most
EVICT_EVERY = 300  # at least this long between two evictions (slice 12.7)
# held while rolling; "waiting": on an Infeasible pod, to evict it; "evicted": its replacement
ROLLOUT = (
    *("pod", "since", "canary_until", "resized", "notes", "waiting", "evicted", "no_room"),
    "stalled",  # since when the API server hasn't let it go on (slice 12.9a)
)
# seconds one call to the API server may take, once (`single`: its client's own retries
# would make it four times that). ponytail: a minute of some twenty calls can still take
# five against an API server that accepts and never answers; stop at the first if it matters
API_TIMEOUT = 15
WATCHED = ("pods", "watch_until", "first_run")  # held while watched
Outcome = Literal[
    "applied",
    "skipped",
    "failed",
    "handed_back",
    "rolled_back",
    "reverted",
    "oom_killed",
    "stepped_up",
    "step_ended",
    "throttled",  # slice 13.3b: throttled, and no Burst made of it
    "burst",
    "burst_ended",
]
# held, never reported: "rolling"
Ending = Callable[[Outcome, str], "Actuation"]  # how advance() ends a rollout


class ActuatorSettings(BaseSettings):
    """The Actuator's own configuration, set by the client in its manifest. No field may
    be named like a Service link (`port`, `service_host`): see RelaySettings."""

    model_config = SettingsConfigDict(env_prefix="SQUERO_ACTUATOR_")

    hub: str  # https://<the Hub's domain>
    token: SecretStr
    namespace: str  # its own, where its ConfigMap lives (the manifest's downward API)
    interval: float = 60


class Wanted(BaseModel):
    """One current Approved Sizing, as the Hub sends it: cores and bytes, None to leave."""

    model_config = ConfigDict(extra="ignore")

    run_id: int
    namespace: str
    kind: str
    name: str
    container: str
    cpu: float | None
    memory: float | None
    # approved after a Revert of that container (slice 12.9b): applied even if it is the
    # run that Revert undid, which an approval from before it never is
    again: bool = False
    # slice 15.3b: a DaemonSet's Sizing per instance type ("" where a node has no such
    # label), each as `cpu` and `memory` above. With any, each pod gets its node's type's
    # and `cpu` and `memory` are for an Actuator that knows no types
    types: dict[str, dict[str, float | None]] = {}


class Undo(BaseModel):
    """A Revert the Operator asked of one container (slice 12.6a)."""

    model_config = ConfigDict(extra="ignore")

    id: int
    namespace: str
    kind: str
    name: str
    container: str


class Watched(BaseModel):
    """A container whose OOM kills the Hub wants reported (slice 13.1a)."""

    model_config = ConfigDict(extra="ignore")

    namespace: str
    kind: str
    name: str
    container: str
    # slice 13.1c: its memory peak (bytes) over its Sizing's window, None when not known
    peak: float | None = None


class Throttled(BaseModel):
    """A container the burst Reflex goes by (slice 13.3a), as the Hub's last hourly read
    has it. Nothing here is bounded: a share of two sums can pass 1 by a hair. A released
    Actuator drops the Hub's whole answer over an item it can't read: only ever add
    optional fields here."""

    model_config = ConfigDict(extra="ignore")

    namespace: str
    kind: str
    name: str
    container: str
    # its share of CPU periods throttled in the hour before that read, when over a quarter:
    # a Burst is due. None: none is
    throttled: float | None = None
    # cores: its highest CPU use over the Contract's term; None: not known
    peak: float | None = None


class Boost(BaseModel):
    """A container whose new pods get a Startup boost (slice 13.4), and its factor."""

    model_config = ConfigDict(extra="ignore")

    namespace: str
    kind: str
    name: str
    container: str
    factor: float = Field(ge=1.5, le=4)


class Placing(BaseModel):
    """The cluster's Placement (slice 15.2a): its Pinned nodes' label (`key=value`) and
    taint, and the Workloads whose new pods prefer them, `namespace/Kind/name`; None:
    not known now (no usable report or read of the nodes), the last list stays."""

    model_config = ConfigDict(extra="ignore")

    label: str
    taint: str | None = None
    workloads: list[str] | None = None


class Spotted(BaseModel):
    """A Workload under a Spot ratio (slice 16.2a): its replicas' share meant for spot
    nodes and its On-demand minimum, which its webhook counts per ReplicaSet."""

    model_config = ConfigDict(extra="ignore")

    namespace: str
    kind: str
    name: str
    ratio: float
    on_demand: int


class Spotting(BaseModel):
    """The cluster's spot nodes (slice 16.2a): their label (`key=value`) and taint, the
    Workloads under a Spot ratio, none blocking a node (None: not known now, the last list
    stays), and the nodes carrying the label as last read (None: not known), by which a
    pod it didn't place counts."""

    model_config = ConfigDict(extra="ignore")

    label: str
    taint: str | None = None
    workloads: list[Spotted] | None = None
    nodes: list[str] | None = None


class Desired(BaseModel):
    kill_switch: bool
    sizings: list[Wanted]  # Approved Sizings
    # Automation's (slice 12.5): a list of their own, so an older Actuator never applies them
    # as if approved, without the churn rule or the pause annotation
    automated: list[Wanted] = []
    reverts: list[Undo] = []  # slice 12.6a
    # slice 12.7: the Contract's eviction off, or on and its window closed or open now
    eviction: Literal["off", "closed", "open"] = "off"
    # slice 13.1a: the automated containers of a Contract with `reflexes.oom`; an Actuator
    # given none never reports an OOM kill, so an older Hub never gets an outcome it refuses
    oom: list[Watched] = []
    # slice 13.1b: how many times its template's memory limit a Step-up may reach. None, as
    # from a Hub of 13.1a: OOM kills are reported and nothing is stepped up
    oom_max: float | None = None
    # slice 13.1c: seconds after its last step that a Step-up ends, unless the peak says
    # it is still needed. None, as from a Hub of 13.1b: a Step-up never expires
    oom_expires: float | None = None
    boosts: list[Boost] = []  # slice 13.4; none from an older Hub
    # slice 15.3b: each node's instance type ("" for none), from the read the Sizings per
    # type were made of, to an Actuator that knows types. None: not read, what it knew stays
    node_types: dict[str, str] | None = None
    # slice 13.3a: the automated containers of a Contract with `reflexes.burst`, and the
    # Sizing run they were read in (a Burst is due once a run). Read by no Actuator yet:
    # slice 13.3b acts on them
    burst: list[Throttled] = []
    burst_run: int | None = None
    # slice 13.3b: how many times its template's CPU limit a Burst may reach (None, as
    # from a Hub of 13.3a: no Burst is made), and its term in seconds
    burst_max: float | None = None
    burst_expires: float | None = None
    # slice 15.1b: the namespaces whose pods' Blocker facts to report, the cluster's own;
    # sent under the Kill switch too (a read is no change). None, as from an older Hub:
    # nothing is reported
    blockers: list[str] | None = None
    # slice 15.2a: the cluster's Placement; None: off (as from an older Hub). Not sent
    # under the Kill switch, where the Actuator keeps what it had and places nothing
    placement: Placing | None = None
    # slice 16.2a: where the cluster names its spot nodes; None: none (as from an older
    # Hub). Not sent under the Kill switch either. Read by no Actuator yet: 16.2b places
    spot: Spotting | None = None


class Actuation(BaseModel):
    """What the Actuator did with one Approved Sizing: the requests it set, on how many pods."""

    model_config = ConfigDict(extra="forbid")

    namespace: str = Field(max_length=253)
    kind: str = Field(max_length=63)
    name: str = Field(max_length=253)
    container: str = Field(max_length=253)
    run_id: int
    outcome: Outcome
    cpu: float | None = None
    memory: float | None = None
    pods: int = Field(ge=0)
    reason: str = Field(max_length=1000)
    at: float


def to_tell(outcome: str, reason: str) -> bool:
    """Whether the Hub tells the Operator of such an Actuation in Slack: a Rollback (slice
    12.4), a Step-up made, and a container still OOM-killed at its Step-up's cap, which a
    person must answer (slice 13.1c). Not an OOM kill only seen, pods only brought to a
    Step-up, or its end."""
    return (
        outcome == "rolled_back"
        or (outcome == "stepped_up" and reason.startswith(STEP_SAID))
        or (outcome == "oom_killed" and AT_CAP in reason)
        # slice 13.3b: a Burst made, and a container still throttled at its Burst's cap
        or (outcome == "burst" and reason.startswith(BURST_SAID))
        or (outcome == "throttled" and BURST_CAP in reason)
    )


def unreal(a: Actuation) -> str | None:
    """Which of an Actuation's numbers no Actuator sends (slice 12.9c), or None: one that
    isn't a number (NaN, infinity) or is below zero. The Hub refuses the whole report
    for it; nothing an honest Actuator may send is refused, since a refused report is
    dropped there, its Rollbacks with it. Checked by the Hub, not by the model: the
    Actuator builds one after it has resized, and reads its own stored ones back."""
    for name in ("cpu", "memory", "at"):
        value = getattr(a, name)
        if value is not None and not (math.isfinite(value) and value >= 0):
            return name
    return None


def rollbacks_of(
    actuations: list[dict[str, Any]], cluster: str, now: float
) -> dict[tuple[str, ...], list[float]]:
    """The Hub's side: each Workload's Rollbacks on `cluster` in the 7 days to `now`,
    oldest first, from its stored Actuations."""
    out: dict[tuple[str, ...], list[float]] = {}
    for r in sorted(actuations, key=lambda r: r["at"]):
        if (
            r["cluster"] == cluster
            and r["outcome"] == "rolled_back"
            and r["at"] >= now - ROLLBACKS_FOR
        ):
            out.setdefault((r["namespace"], r["kind"], r["name"]), []).append(r["at"])
    return out


def resumes(rollbacks: dict[tuple[str, ...], list[float]]) -> dict[tuple[str, ...], float]:
    """Each Workload whose Automation the Rollbacks ended, and when it resumes: once the
    oldest of the last three is 7 days old."""
    return {w: ts[-ENDING] + ROLLBACKS_FOR for w, ts in rollbacks.items() if len(ts) >= ENDING}


def qty(q: str) -> Decimal:
    return parse_quantity(q)  # type: ignore[no-untyped-call,no-any-return]


def cpu_text(cores: float) -> str:
    return f"{math.ceil(round(cores * 1000, 6))}m"


def memory_text(b: float) -> str:
    return f"{math.ceil(round(b / MI, 6))}Mi"


def same(a: str | None, b: str | None) -> bool:
    """Two requests as quantities (`0.1` is `100m`); unset is only ever unset."""
    return a == b if a is None or b is None else qty(a) == qty(b)


def no_room(e: ApiException) -> str | None:
    """The API server's refusal of a resize its pod's node can never fit (Kubernetes 1.35+
    says so at once, where 1.33 marks the pod's resize Infeasible), its message; else None."""
    message = why(e).split(": ", 1)[-1]
    return message if e.status == 403 and "enough allocatable" in message else None


def why(e: ApiException) -> str:
    """The API server's own message, short: never more than it said."""
    try:
        message = json.loads(e.body or "{}").get("message") or e.reason
    except (ValueError, AttributeError):
        message = e.reason
    return f"{e.status}: {message}"[:300]


def passing(e: ApiException) -> bool:
    """A failure that may be gone next minute: the network's, a 429 or the server's own."""
    return not e.status or e.status == 429 or e.status >= 500


def single() -> None:
    """The Kubernetes clients made from now on try each call once: the Actuator's own
    minute is the retry. Their default retries a read that timed out three times, so a call
    to an API server that never answers held the minute four timeouts long (found in
    12.9's live check: no pull for almost four minutes, the Kill switch unheard)."""
    cfg = client.Configuration.get_default_copy()
    cfg.retries = 0
    client.Configuration.set_default(cfg)


class Calm:
    """A Kubernetes API whose network failures (urllib3's, which its client lets through)
    are ApiExceptions as its refusals are, so the handling of one covers both and none ends
    the Actuator's minute; and no call waits forever (slice 12.9a)."""

    def __init__(self, api: Any) -> None:
        self.api = api

    def __getattr__(self, name: str) -> Any:
        call = getattr(self.api, name)

        def calm(*args: Any, **kw: Any) -> Any:
            kw.setdefault("_request_timeout", API_TIMEOUT)
            try:
                return call(*args, **kw)
            except urllib3.exceptions.HTTPError as e:
                raise ApiException(status=0, reason=type(e).__name__) from e

        return calm


class Apps:
    """AppsV1Api with Argo Rollouts beside its own kinds (slice 12.8b): a Rollout read as
    a Deployment, its template its own or its `workloadRef` Deployment's."""

    def __init__(self, apps: Any, custom: Any, batch: Any = None) -> None:
        self.apps, self.custom, self.batch, self.decoder = apps, custom, batch, client.ApiClient()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.apps, name)

    def shaped(self, r: dict[str, Any]) -> Any:
        spec = r.get("spec") or {}
        ref = spec.get("workloadRef") or {}
        if ref.get("kind") == "Deployment":
            d = self.apps.read_namespaced_deployment(ref.get("name"), r["metadata"]["namespace"])
            on = self.decoder.sanitize_for_serialization(d.spec)  # type: ignore[no-untyped-call]
            spec = {
                **spec,
                "template": on["template"],
                "selector": spec.get("selector") or on["selector"],
            }
        # its status left out; its spec's own fields (a canary strategy) a Deployment ignores
        body = json.dumps({"metadata": r.get("metadata"), "spec": spec})
        try:
            return self.decoder.deserialize(body, "V1Deployment", None)
        except ValueError as e:  # no template: a workloadRef of another kind, or none
            raise ApiException(status=422, reason=f"not read as a Deployment: {e}") from e

    def read_namespaced_rollout(self, name: str, ns: str) -> Any:
        return self.shaped(self.custom.get_namespaced_custom_object(*ARGO, ns, "rollouts", name))

    def list_namespaced_rollout(self, ns: str) -> Any:
        """Their metadata only, all the pause annotation needs: one Rollout that can't be
        shaped never hides another's pause."""
        found = self.custom.list_namespaced_custom_object(*ARGO, ns, "rollouts")
        metas = [json.dumps(r.get("metadata") or {}) for r in found.get("items") or []]
        return SimpleNamespace(
            items=[
                SimpleNamespace(metadata=self.decoder.deserialize(m, "V1ObjectMeta", None))
                for m in metas
            ]
        )

    # a CronJob's and its Jobs' (slice 12.8c), from BatchV1Api
    def read_namespaced_cron_job(self, name: str, ns: str) -> Any:
        return self.batch.read_namespaced_cron_job(name, ns)

    def list_namespaced_cron_job(self, ns: str) -> Any:
        return self.batch.list_namespaced_cron_job(ns)

    def list_namespaced_job(self, ns: str) -> Any:
        return self.batch.list_namespaced_job(ns)


def template_of(d: Any, kind: str) -> Any:
    """The pod template of a workload read with `read`: a CronJob's is its Jobs'."""
    return (d.spec.job_template.spec if kind == "CronJob" else d.spec).template


def read(apps: Any, kind: str, name: str, ns: str) -> Any:
    """The workload `kind`/`name` (one of KINDS) from the API server."""
    return getattr(apps, f"read_namespaced_{KINDS[kind]}")(name, ns)


def pods_of(core: Any, apps: Any, d: Any, kind: str) -> list[Any]:
    """The workload's live pods, however its selector reads: a Deployment's or Rollout's
    are its own ReplicaSets', a StatefulSet's or DaemonSet's its own (slice 12.8a). A CronJob's
    none: they are never resized in place, the webhook gives its next run's pods the hold
    (slice 12.8c)."""
    if kind == "CronJob":
        return []
    ns = d.metadata.namespace
    labels = ",".join(f"{k}={v}" for k, v in sorted((d.spec.selector.match_labels or {}).items()))
    if kind in ("Deployment", "Rollout"):
        mine = {
            rs.metadata.name
            for rs in apps.list_namespaced_replica_set(ns, label_selector=labels).items
            if any(o.uid == d.metadata.uid for o in rs.metadata.owner_references or [])
        }

        def own(o: Any) -> bool:
            return bool(o.kind == "ReplicaSet" and o.name in mine)
    else:

        def own(o: Any) -> bool:
            return bool(o.uid == d.metadata.uid)

    return [
        p
        for p in core.list_namespaced_pod(ns, label_selector=labels).items
        if any(own(o) for o in p.metadata.owner_references or [])
        and p.status.phase not in ("Succeeded", "Failed")
        and not p.metadata.deletion_timestamp
    ]


def requests_of(c: Any) -> dict[str, str]:
    return dict((c.resources.requests if c and c.resources else None) or {})


def limits_of(c: Any) -> dict[str, str]:
    return dict((c.resources.limits if c and c.resources else None) or {})


def qos(containers: list[tuple[dict[str, str], dict[str, str]]]) -> str:
    """A pod's QoS class from its containers' (requests, limits). Kubernetes refuses a
    resize that changes it. ponytail: init containers left out; add them if one matters."""
    if all(not r and not lim for r, lim in containers):
        return "BestEffort"
    guaranteed = all(
        all(res in lim and same(r.get(res, lim[res]), lim[res]) for res in ("cpu", "memory"))
        for r, lim in containers
    )
    return "Guaranteed" if guaranteed else "Burstable"


def qos_change(
    containers: list[tuple[str, dict[str, str], dict[str, str]]], name: str, set_to: dict[str, str]
) -> str | None:
    """Why setting `name`'s requests to `set_to` would change the pod's QoS class, or None."""
    before = [(r, lim) for _, r, lim in containers]
    after = [({**r, **set_to} if n == name else r, lim) for n, r, lim in containers]
    was, would = qos(before), qos(after)
    if was == would:
        return None
    if was == "Guaranteed":
        return "it is Guaranteed: its limits must change too"
    return f"it would turn its pods from {was} to {would}"


def vpas(custom: Any, ns: str) -> list[Any]:
    """The namespace's VPAs; none where no VPA is installed."""
    try:
        found = custom.list_namespaced_custom_object(
            "autoscaling.k8s.io", "v1", ns, "verticalpodautoscalers"
        )
    except ApiException as e:
        if e.status == 404:
            return []
        raise
    return found.get("items") or []


def vpa_mode(
    custom: Any, ns: str, kind: str, name: str, found: list[Any] | None = None
) -> str | None:
    """The mode of a VPA that changes this workload's pods, or None (none, or `Off`);
    among `found`, the namespace's VPAs already read, or reading them."""
    for v in vpas(custom, ns) if found is None else found:
        spec = v.get("spec") or {}
        ref = spec.get("targetRef") or {}
        if ref.get("kind") == kind and ref.get("name") == name:
            mode = (spec.get("updatePolicy") or {}).get("updateMode") or "Auto"
            if mode != "Off":
                return str(mode)
    return None


def hpa_resources(autoscaling: Any, ns: str, kind: str, name: str, container: str) -> set[str]:
    """The resources an HPA of this workload scales on by utilization: a request it
    divides by, so the Actuator leaves it alone. An HPA without metrics means cpu."""
    out: set[str] = set()
    for h in autoscaling.list_namespaced_horizontal_pod_autoscaler(ns).items:
        ref = h.spec.scale_target_ref
        if ref.kind != kind or ref.name != name:
            continue
        if not h.spec.metrics:
            out.add("cpu")
        for m in h.spec.metrics or []:
            if m.type == "Resource" and m.resource.target.type == "Utilization":
                out.add(m.resource.name)
            if (
                m.type == "ContainerResource"
                and m.container_resource.container == container
                and m.container_resource.target.type == "Utilization"
            ):
                out.add(m.container_resource.name)
    return out


def limit_range_breach(
    core: Any, ns: str, set_to: dict[str, str], limits: dict[str, str]
) -> str | None:
    """Why the namespace's LimitRange would refuse these requests, or None. Never clamped."""
    for lr in core.list_namespaced_limit_range(ns).items:
        for item in lr.spec.limits or []:
            if item.type != "Container":
                continue
            for r, q in set_to.items():
                low, high = (item.min or {}).get(r), (item.max or {}).get(r)
                ratio = (item.max_limit_request_ratio or {}).get(r)
                if low and qty(q) < qty(low):
                    return f"{r} {q} is under its LimitRange's minimum {low}"
                if high and qty(q) > qty(high):
                    return f"{r} {q} is over its LimitRange's maximum {high}"
                if ratio and r in limits and qty(limits[r]) / qty(q) > qty(ratio):
                    return f"{r} {q} under limit {limits[r]} passes its LimitRange's ratio {ratio}"
    return None


def quota_breach(
    core: Any, ns: str, set_to: dict[str, str], now: dict[str, str], pods: int, limits: bool = False
) -> str | None:
    """Why raising requests (or `limits`, slice 13.1b) on `pods` pods would pass a
    ResourceQuota, or None."""
    for rq in core.list_namespaced_resource_quota(ns).items:
        hard, used = (rq.status.hard or {}), (rq.status.used or {})
        for r, q in set_to.items():
            more = (qty(q) - qty(now.get(r) or "0")) * pods
            for key in (f"limits.{r}",) if limits else (f"requests.{r}", r):
                if more > 0 and key in hard and qty(used.get(key) or "0") + more > qty(hard[key]):
                    quota = f"ResourceQuota {rq.metadata.name}'s {key} {hard[key]}"
                    return f"{pods} pods at {r} {q} would pass {quota}"
    return None


def boost_barred(core: Any, ns: str) -> str | None:
    """Why no pod of `ns` gets a Startup boost (slice 13.4), or None: a LimitRange that
    caps cpu (a maximum, or a limit-to-request ratio: the request stays while the limit
    grows) or a ResourceQuota that counts `limits.cpu` could refuse the boosted pod's
    creation, which its own limit would have passed.
    ponytail: the whole namespace, whatever room they leave; compare each pod's boosted
    limit with the LimitRange (a rollout's pods at once can still pass a quota) if needed."""
    for lr in core.list_namespaced_limit_range(ns).items:
        for item in lr.spec.limits or []:
            if "cpu" in (item.max or {}) or "cpu" in (item.max_limit_request_ratio or {}):
                return f"LimitRange {lr.metadata.name} caps cpu"
    return counts_cpu_limits(core, ns)


def counts_cpu_limits(core: Any, ns: str) -> str | None:
    """The namespace's ResourceQuota that counts `limits.cpu`, said, or None. With one, a
    CPU limit raised on new pods (a Startup boost, a Burst) can get a pod's creation
    refused however much room it leaves now: a rollout or an HPA makes several at once."""
    for rq in core.list_namespaced_resource_quota(ns).items:
        hard = {**((rq.spec.hard if rq.spec else None) or {}), **(rq.status.hard or {})}
        if "limits.cpu" in hard:
            return f"ResourceQuota {rq.metadata.name} counts limits.cpu"
    return None


def limit_cpu(
    core: Any, ns: str, container: str, pods: list[Any], limit: str, up: bool
) -> tuple[int, int, list[str]]:
    """Set `container`'s CPU limit to `limit` in place on those of `pods` below it (`up`,
    a Burst: only ever raised) or above it (its end: only ever lowered): how many took it,
    how many it was asked of, and each refusal."""
    body = {"spec": {"containers": [{"name": container, "resources": {"limits": {"cpu": limit}}}]}}
    done, asked, refused = 0, 0, []
    for p in pods:
        spec = next((c for c in p.spec.containers if c.name == container), None)
        has = limits_of(spec).get("cpu")
        if not has or (qty(has) >= qty(limit) if up else qty(has) <= qty(limit)):
            continue
        asked += 1
        try:
            core.patch_namespaced_pod_resize(p.metadata.name, ns, body)
            done += 1
        except ApiException as e:
            refused.append(f"{p.metadata.name}: {why(e)}")
    return done, asked, refused


def resize_state(pod: Any, container: str, set_to: dict[str, str]) -> tuple[str, str]:
    """Where a pod's resize is: done (allocated, and the pod Ready), waiting, deferred or
    infeasible (the kubelet's word, Kubernetes 1.33+), with its message."""
    conditions = pod.status.conditions or []
    for c in conditions:
        if c.type == "PodResizePending" and c.status == "True":
            state = "infeasible" if (c.reason or "").lower() == "infeasible" else "deferred"
            return state, c.message or ""
        if c.type == "PodResizeInProgress" and c.status == "True":
            return "waiting", "in progress"
    st = next((s for s in pod.status.container_statuses or [] if s.name == container), None)
    allocated = (st.resources.requests if st and st.resources else None) or {}
    if not all(same(allocated.get(r), q) for r, q in set_to.items()):
        return "waiting", "not yet allocated"
    if not any(c.type == "Ready" and c.status == "True" for c in conditions):
        return "waiting", "not Ready"
    return "done", ""


def resize(core: Any, pod: Any, container: str, set_to: dict[str, str]) -> None:
    body = {"spec": {"containers": [{"name": container, "resources": {"requests": set_to}}]}}
    core.patch_namespaced_pod_resize(pod.metadata.name, pod.metadata.namespace, body)


def needs(pod: Any, container: str, set_to: dict[str, str]) -> bool:
    c = next((c for c in pod.spec.containers if c.name == container), None)
    now = requests_of(c)
    return not all(same(now.get(r), q) for r, q in set_to.items())


def wants(h: dict[str, Any], pod: Any, nodes: dict[str, str] | None = None) -> dict[str, str]:
    """The requests a hold wants of one pod: every step that looks at a pod asks here,
    never the hold's own set. A hold with `types` (slice 15.3b) wants each pod's node's
    instance type's, by `nodes` (node -> type); its template's of a pod whose type has
    none (a request its template doesn't set is left); and nothing while it knows no
    node's type (a restart with the Hub away), so that nothing is resized by a guess."""
    types = h.get("types")
    if types is None:
        return dict(h["requests"])
    if not nodes:
        return {}
    kind = nodes.get(pod.spec.node_name)
    if kind in types:
        return dict(types[kind])
    had = h.get("template") or {}
    return {r: had[r] for r in h["requests"] if had.get(r) is not None}


def at_hold(h: dict[str, Any], pod: Any, container: str) -> bool:
    """Whether a pod has the requests held as `h`: the Actuator's doing, to watch and to
    put back. Any instance type's, of a hold with `types`: wherever the pod runs now."""
    types = h.get("types")
    if types is None:
        return not needs(pod, container, h["requests"])
    return any(t and not needs(pod, container, t) for t in types.values())


def set_by(h: dict[str, Any]) -> dict[str, float]:
    """What a hold's Actuation says it set: its requests, as numbers. None of a hold with
    `types`, which has no one number: its reason names each type's."""
    if h.get("types") is not None:
        return {}
    return {r: float(qty(q)) for r, q in h["requests"].items()}


TYPES_SAID = 3  # instance types a reason names: it is cut at 1000 characters


def held_text(h: dict[str, Any]) -> str:
    """What a hold asks, for an Actuation's reason: its requests, or each instance type's
    (three types, the rest counted)."""
    if h.get("types") is None:
        return texts(h["requests"])
    said = [f"{t or 'no instance type'} {texts(q)}" for t, q in sorted(h["types"].items())]
    more = f" and {len(said) - TYPES_SAID} more" if len(said) > TYPES_SAID else ""
    return f"{'; '.join(said[:TYPES_SAID])}{more}"


def node_of(pod: dict[str, Any]) -> str | None:
    """The node a DaemonSet's new pod is for (slice 15.3b): it has no `nodeName` when it is
    admitted, its controller names the node in its node affinity instead."""
    spec = pod.get("spec") or {}
    if spec.get("nodeName"):
        return str(spec["nodeName"])
    required = ((spec.get("affinity") or {}).get("nodeAffinity") or {}).get(
        "requiredDuringSchedulingIgnoredDuringExecution"
    ) or {}
    for term in required.get("nodeSelectorTerms") or []:
        for f in term.get("matchFields") or []:
            values = f.get("values") or []
            if f.get("key") == "metadata.name" and f.get("operator") == "In" and len(values) == 1:
                return str(values[0])
    return None


def apply(
    core: Any,
    apps: Any,
    w: Wanted,
    at: float,
    autoscaling: Any,
    custom: Any,
    old: dict[str, Any] | None = None,
    automated: bool = False,
    eviction: str = "off",
    left: list[str] | None = None,
    stepped: bool = False,
    nodes: dict[str, str] | None = None,
) -> tuple[Actuation | None, dict[str, Any]]:
    """Start resizing `w`'s container in place on its workload's pods (slice 12.3): the
    pre-checks, then one canary pod. An Actuation when it ends here (skipped or failed),
    None while it rolls; and what to remember of it, empty when Automation's churn rule
    leaves nothing to change. `old`: the hold a new Sizing replaces (None: catching up).
    A DaemonSet's Sizing with `types` (slice 15.3b) is one set of requests per instance
    type, each pod getting its node's by `nodes`: every pre-check holds for every type,
    and one that fails for any skips the container."""
    set_to: dict[str, str] = {}
    template_had: dict[str, str | None] = {}  # its template's requests then: a change hands back
    notes: list[str] = []
    # per instance type; None: one set for every pod. Then `set_to` is the most any type
    # asks of each resource: what a quota must take, and which resources are held
    types: dict[str, dict[str, str]] | None = {} if w.types and w.kind == "DaemonSet" else None

    def hold() -> dict[str, Any]:
        kept = {"requests": set_to, "template": template_had}
        return kept if types is None else {**kept, "types": types}

    def done(outcome: Outcome, pods: int, reason: str) -> tuple[Actuation, dict[str, Any]]:
        values = set_by(hold())
        a = Actuation(
            **w.model_dump(exclude={"cpu", "memory", "again", "types"}),
            outcome=outcome,
            pods=pods,
            reason=(reason + "".join(notes))[:1000],
            at=at,
            **values,
        )
        return a, {"run_id": w.run_id, "outcome": outcome, **hold(), "at": at}

    def failed(e: ApiException, reason: str) -> tuple[Actuation, dict[str, Any]]:
        a, held = done("failed", 0, reason)
        if passing(e):
            held["passing"] = True  # nothing changed, and it may work next minute (12.9b)
        return a, held

    if w.kind not in KINDS:
        return done("skipped", 0, f"a {w.kind} is not resized")
    try:
        d = read(apps, w.kind, w.name, w.namespace)
    except ApiException as e:
        if e.status == 404:
            return done("skipped", 0, f"no such {w.kind}")
        return failed(e, f"reading the {w.kind}: {why(e)}")
    containers = template_of(d, w.kind).spec.containers
    template = next((c for c in containers if c.name == w.container), None)
    if template is None:
        return done("skipped", 0, f"no container {w.container!r} in its template")
    limits, asked = limits_of(template), requests_of(template)
    kept = old if old and old.get("outcome") in ("applied", "rolling") else {}
    held_template = kept.get("template") or {}
    since = at - (old or {}).get("lowered_at", -LOWER_EVERY)
    capped: set[str] = set()
    deferred: set[str] = set()
    fresh: set[str] = set()  # the resources this Sizing sets, for any type
    lowered = False

    def one(values: dict[str, float | None], holds: dict[str, str]) -> dict[str, str]:
        """One set of requests: the Sizing's `values`, never above the limit, less what
        Automation's churn rule (slice 12.5) leaves out, over what its pods hold now."""
        nonlocal lowered
        out = dict(holds)  # a resource this Sizing leaves alone keeps what it holds
        for res, text in (("cpu", cpu_text), ("memory", memory_text)):
            value = values.get(res)
            if value is None or (stepped and res == "memory"):
                continue  # memory: its Step-up's while it lasts (slice 13.1b)
            q = text(value)
            if res in limits and qty(q) > qty(limits[res]):
                q = limits[res]  # requests never above the limit: the Sizing's risk stays shown
                capped.add(res)
            was = holds.get(res) or asked.get(res)  # what its pods ask now
            lower = bool(was) and qty(q) < qty(was or "0")
            if automated and was and qty(was) > 0:
                change = abs(qty(q) / qty(was) - 1)
                if change < CHURN or (lower and since < LOWER_EVERY):
                    deferred.add(res)
                    continue
            lowered = lowered or (old is not None and lower)
            fresh.add(res)
            out[res] = q
        return out

    pooled = kept.get("requests") or {}
    if types is None:
        asks = [{"cpu": w.cpu, "memory": w.memory}]
        set_to.update(one(asks[0], pooled))
    else:
        asks = list(w.types.values())
        before = kept.get("types")
        # a type the last Sizing had and this one has not keeps what it holds
        types.update({t: dict(q) for t, q in (before or {}).items() if t not in w.types})
        for t, values in w.types.items():
            # its pods ask their type's, or the one set held before there were types, or
            # their template's
            types[t] = one(values, pooled if before is None else before.get(t) or {})
        for t in [t for t, q in types.items() if not q]:
            del types[t]  # nothing for it: its pods keep their template's
        for q in types.values():
            for res, value in q.items():
                if res not in set_to or qty(value) > qty(set_to[res]):
                    set_to[res] = value
    for res in set_to:
        template_had[res] = asked.get(res) if res in fresh else held_template.get(res)
    if stepped and any(a.get("memory") is not None for a in asks):
        notes.append("; memory left to its Step-up")
    if capped:
        notes.append(f"; capped at its limit: {sorted(capped)}")
    if stepped and not fresh and not deferred and any(a.get("memory") is not None for a in asks):
        notes.clear()
        set_to.clear()
        template_had.clear()
        types = None
        return done("skipped", 0, "it asks memory only, which is its Step-up's while it lasts")
    if automated and not fresh:  # nothing moves enough: the churn rule (slice 12.5)
        return None, {}
    if types is not None:
        notes.append(f"; by instance type: {held_text(hold())}")
    restarts = sorted(
        p.resource_name
        for p in template.resize_policy or []
        if p.restart_policy == "RestartContainer" and p.resource_name in set_to
    )
    if restarts:
        return done("skipped", 0, f"its resizePolicy restarts the container for {restarts}")
    try:
        mode = vpa_mode(custom, w.namespace, w.kind, w.name)
        if mode:
            return done("skipped", 0, f"a VPA in mode {mode} sizes it")
        scaled = hpa_resources(autoscaling, w.namespace, w.kind, w.name, w.container)
        for r in sorted(scaled & set(set_to)):
            del set_to[r], template_had[r]
            for q in (types or {}).values():
                q.pop(r, None)
            notes.append(f"; {r} left to its HPA, which scales on its utilization")
        if not set_to:
            return done("skipped", 0, "nothing its HPA doesn't scale on")
        pairs = [(c.name, requests_of(c), limits_of(c)) for c in containers]
        for t, q in [("", set_to)] if types is None else sorted(types.items()):
            why_not = qos_change(pairs, w.container, q) or limit_range_breach(
                core, w.namespace, q, limits
            )
            if why_not and types is not None:
                why_not += f" (its pods' on {t or 'nodes with no instance type'})"
            if why_not:
                return done("skipped", 0, why_not)
        if w.kind == "CronJob":  # through the webhook only (slice 12.8c): its next run
            runs = d.spec.job_template.spec.parallelism or 1
            why_not = quota_breach(core, w.namespace, set_to, asked, runs)
            if why_not:
                return done("skipped", 0, why_not)
            a, held = done("applied", 0, "held for its next run's pods, given them as they start")
            held["first_run"] = {"after": at, "until": at + RUN_WATCH}
            if lowered:
                held["lowered_at"] = at
            if deferred:
                held["deferred"] = sorted(deferred)
            return a, held
        pods = pods_of(core, apps, d, w.kind)
        if not pods:
            return done("skipped", 0, "no live pods")
        why_not = quota_breach(core, w.namespace, set_to, asked, len(pods))
        if why_not:
            return done("skipped", 0, why_not)
    except ApiException as e:  # a namespace its RoleBinding misses: the others still go on
        return failed(e, f"checking it: {why(e)}")
    if types is not None and not nodes:
        a, held = done("skipped", 0, "its nodes' instance types are not known yet")
        held["passing"] = True  # the Hub sends them with its next good read
        return a, held

    def its(p: Any) -> dict[str, str]:
        return wants(hold(), p, nodes)

    behind = [
        p for p in pods if needs(p, w.container, its(p)) and p.metadata.name not in (left or [])
    ]
    todo = [p for p in behind if not lost(p)]  # one whose node is gone is no canary (12.10)
    if behind and not todo:
        a, held = done("skipped", 0, "the pods without these requests are on a node that is gone")
        held["passing"] = True  # their replacements start within minutes: tried again then
        return a, held
    if not todo:
        return done("skipped", 0, f"all {len(pods)} pods already have these requests")
    room = None
    try:
        resize(core, todo[0], w.container, its(todo[0]))
    except ApiException as e:
        room = no_room(e)
        if room is None or (eviction == "off" and w.kind != "DaemonSet"):  # off: failed at once
            return failed(e, f"{todo[0].metadata.name}: {why(e)}")
    _, held = done("applied", 1, "")
    held |= {
        "outcome": "rolling",
        # the canary, and each pod that already has them (a catch-up's others, whose old
        # restarts are no news: slice 12.9a), counted from now
        "pods": {
            p.metadata.name: baseline(p, w.container, at)
            if p is todo[0]
            else {"restarts": restarts_of(p, w.container)}  # not resized now: no NotReady rule
            for p in pods
            if p is todo[0] or not needs(p, w.container, its(p))
        },
        "pod": todo[0].metadata.name,
        "since": at,
        "canary_until": at + CANARY_SECONDS,
        "resized": 1,
        "notes": "".join(notes),
    }
    if room:  # its node can never fit it: evicted in a window, as one Infeasible (slice 12.7)
        held["no_room"] = room
    if lowered:
        held["lowered_at"] = at
    if deferred:  # an approval of this run still applies them
        held["deferred"] = sorted(deferred)
    return None, held


def advance(
    core: Any,
    apps: Any,
    key: str,
    h: dict[str, Any],
    now: float,
    eviction: str = "off",
    policy: Any = None,
    nodes: dict[str, str] | None = None,
) -> Actuation | None:
    """One minute of a rolling resize: wait for its current pod, then (after the canary's
    10 minutes) resize the next one. An Actuation once it ends: applied when every live pod
    has the requests, failed when a pod's resize is Infeasible or not done in 10 minutes.
    With eviction on (slice 12.7) an Infeasible pod is evicted instead, when `eviction` is
    open, its replacement getting the requests from the webhook; until then it waits. Not
    a pod `unevictable` names: that still fails. A DaemonSet's pod whose node has no room is
    left as it runs instead (slice 12.8a), the rollout failing only when every pod is.
    `policy` is a PolicyV1Api."""
    ns, kind, name, container = key.split("/")
    if h.get("types") is not None and not nodes:
        return None  # no node's type known (a restart, the Hub away): it waits, unjudged

    def end(outcome: Outcome, reason: str) -> Actuation:
        h["outcome"] = outcome
        for k in ROLLOUT:
            h.pop(k, None)
        h["watch_until"] = now + WATCH_SECONDS  # the pods resized, applied or not
        return Actuation(
            namespace=ns,
            kind=kind,
            name=name,
            container=container,
            run_id=h["run_id"],
            outcome=outcome,
            pods=resized,
            reason=(reason + notes)[:1000],
            at=now,
            **set_by(h),
        )

    resized, notes, pod = h["resized"], h.get("notes", ""), h["pod"]
    if "evicted" in h:
        return replaced(core, apps, key, h, now, end)
    try:
        p = core.read_namespaced_pod(pod, ns)
    except ApiException as e:
        if e.status != 404:
            log.warning("reading %s/%s: %s", ns, pod, why(e))
            return None
        p = None  # gone: its replacement is one of the pods still to do
    if p is not None and not p.metadata.deletion_timestamp and not lost(p):
        state, message = resize_state(p, container, wants(h, p, nodes))
        if "no_room" in h:  # the API server refused its resize: no room on its node, ever
            state, message = "infeasible", h["no_room"]
        late = now - h["since"] >= WAIT_SECONDS
        if kind == "DaemonSet" and (state == "infeasible" or (state == "deferred" and late)):
            leave(core, p, key, h, message, nodes)  # its node has no room: the others go on
            resized, notes = h["resized"], h["notes"]
        elif state == "infeasible":
            never = None if eviction == "off" else unevictable(apps, policy, ns, kind, name, p)
            if eviction == "off" or never:
                why_ = f"{pod}: Infeasible: {message}; {resized - 1} other pods resized"
                return end("failed", why_ + (f"; {never}" if never else ""))
            if eviction == "open":
                return evict(core, apps, key, h, now, message, end)
            wait(core, key, h, "window", f"{pod}: Infeasible: {message}; it waits to be "
                 "evicted, in an eviction window and one pod at a time")  # fmt: skip
            return None
        elif state != "done":
            if not late:
                return None
            return end("failed", f"{pod}: {state} for {WAIT_SECONDS // 60} minutes: {message}")
    if p is not None and lost(p) and now < h["canary_until"]:  # only a canary has that wait
        # the canary's node went inside its ten minutes: the next pod resized is the canary
        resized = h["resized"] = 0
    elif now < h["canary_until"]:
        return None
    try:
        pods = pods_of(core, apps, read(apps, kind, name, ns), kind)
        pods = [x for x in pods if not lost(x)]  # its node gone: a catch-up's, if it returns
        left = h.get("left") or []
        behind = [x for x in pods if needs(x, container, wants(h, x, nodes))]
        todo = [x for x in behind if x.metadata.name not in left]
        if not pods:  # scaled to zero meanwhile: held, and its webhook gives them to new pods
            return end("applied", f"{resized} pods resized in place, then none left to resize")
        if not todo:
            if len(behind) == len(pods):  # every pod left: none has room
                return end("failed", f"no node of its {len(pods)} pods has room for them")
            return end("applied", f"{resized} of {len(pods)} pods resized in place, one at a time")
        resize(core, todo[0], container, wants(h, todo[0], nodes))
    except ApiException as e:
        room = no_room(e)
        if room is None and passing(e) and now - h.setdefault("stalled", now) < WAIT_SECONDS:
            log.warning("%s waits: %s", key, why(e))  # tried again next minute, 10 at most
            return None
        if room is None or (eviction == "off" and kind != "DaemonSet"):
            return end("failed", f"resizing its next pod: {why(e)}")
        h["no_room"] = room
    h.pop("stalled", None)
    if resized == 0:  # the canary was left: the next pod resized is the canary
        h["canary_until"] = now + CANARY_SECONDS
    h.update(pod=todo[0].metadata.name, since=now, resized=resized + 1)
    h.setdefault("pods", {})[todo[0].metadata.name] = baseline(todo[0], container, now)
    return None


def leave(
    core: Any,
    p: Any,
    key: str,
    h: dict[str, Any],
    message: str,
    nodes: dict[str, str] | None = None,
) -> None:
    """A DaemonSet's pod whose node has no room for the requests (slice 12.8a): its pending
    resize undone (back to what the kubelet allocated it) but for the requests it lowers,
    which always fit and are set in place; the pod left out of this rollout and of
    catch-ups, said in an Event; the other pods go on."""
    ns, kind, name, container = key.split("/")
    pod = p.metadata.name
    st = container_status(p, container)
    allocated = (st.resources.requests if st and st.resources else None) or {}
    its = wants(h, p, nodes)
    lower = {r: q for r, q in its.items() if r in allocated and qty(q) < qty(allocated[r])}
    pending = h.pop("no_room", None) is None  # refused at once (Kubernetes 1.35+): nothing is
    back = {r: allocated[r] for r in its if r in allocated} if pending else {}
    try:
        if back or lower:
            resize(core, p, container, back | lower)
    except ApiException as e:
        log.warning("resizing %s back: %s", pod, why(e))
    h.setdefault("left", []).append(pod)
    (h.get("pods") or {}).pop(pod, None)
    h["resized"] -= 1
    if h["resized"] == 0:  # the canary: no wait on it, the next pod resized is the canary
        h["canary_until"] = 0.0
    h["notes"] = h.get("notes", "") + f"; {pod} left without its raise: its node has no room"
    said = f"{pod} left without its raise: its node has no room for {texts(h['requests'])}"
    said += f" ({message})" + (f"; lowered in place to {texts(lower)}" if lower else "")
    log.info("%s: %s", key, said)
    post(core, ns, ("apps/v1", kind, name), "PodLeft", said, False)


def selects(s: Any, labels: dict[str, str]) -> bool:
    """A label selector `s` matches `labels`: a null one matches nothing, an empty one all."""
    if s is None:
        return False
    for e in s.match_expressions or []:
        has, values = e.key in labels, e.values or []
        if not {
            "In": labels.get(e.key) in values,
            "NotIn": labels.get(e.key) not in values,
            "Exists": has,
            "DoesNotExist": not has,
        }.get(e.operator, False):
            return False
    return all(labels.get(k) == v for k, v in (s.match_labels or {}).items())


def pod_facts(p: Any, budgets: list[Any], label: str | None = None) -> blockers.PodFacts | None:
    """A pod's facts for the Hub's Blocker rules (slice 15.1b), or None for one that
    blocks no one's removal of its node: not on a node yet, finished or on its way out,
    a DaemonSet's or the node's own static pod (they stay with it), or with no fact at
    all. `budgets`: its namespace's PodDisruptionBudgets; the one covering it that
    allows the fewest disruptions is named, one whose status isn't written yet as
    allowing none. Facts only: which of them block is the Hub's to say. With the
    cluster's Placement `label` (slice 15.2b): whether the pod carries its preference,
    and its Workload as the webhook names it, where it names one."""
    meta, spec = p.metadata, p.spec
    noted = meta.annotations or {}
    if not spec.node_name or p.status.phase in ("Succeeded", "Failed"):
        return None
    if meta.deletion_timestamp is not None or MIRROR in noted:
        return None
    by = next((o for o in meta.owner_references or [] if o.controller), None)
    if by is not None and by.kind == "DaemonSet":
        return None
    volumes = spec.volumes or []
    covering = [
        (b.status.disruptions_allowed or 0 if b.status else 0, b.metadata.name)
        for b in budgets
        if selects(b.spec.selector, meta.labels or {})
    ]
    allowed, budget = min(covering) if covering else (None, None)
    f = blockers.PodFacts(
        namespace=meta.namespace,
        pod=meta.name,
        node=spec.node_name,
        kind=by.kind if by else "",
        name=by.name if by else "",
        empty_dir=sorted(v.name for v in volumes if v.empty_dir is not None)[:50],
        in_memory=sorted(
            v.name for v in volumes if v.empty_dir is not None and v.empty_dir.medium == "Memory"
        )[:50],
        host_path=sorted(v.name for v in volumes if v.host_path is not None)[:50],
        annotations={a: noted[a][:253] for a in blockers.ANNOTATIONS if a in noted},
        budget=budget,
        allowed=allowed,
        started=p.status.start_time.timestamp() if p.status.start_time else None,
        placed=carries(p, label or ""),  # no label: no key matches
    )
    told = bool(f.empty_dir or f.host_path or f.annotations) or allowed == 0
    told = told or f.kind not in blockers.BUILT_IN
    if not (told or (f.namespace == "kube-system" and budget is None)):
        return None
    if not label:  # only to a Hub that gave it a Placement: an older one refuses the field
        return f
    owners = {o.kind: o.name for o in meta.owner_references or []}
    of = workload_of(owners, meta.labels or {})
    return f.model_copy(update={"workload": "/".join(of)}) if of else f


def carries(p: Any, label: str) -> bool:
    """Whether a pod prefers the nodes with `label` (`key=value`), as Placement's
    preference does (slice 15.2b): its own or the webhook's, the same fact."""
    affinity = p.spec.affinity
    node = affinity.node_affinity if affinity else None
    terms = (node.preferred_during_scheduling_ignored_during_execution if node else None) or []
    key, _, value = label.partition("=")
    return any(
        e.key == key and e.operator == "In" and value in (e.values or [])
        for t in terms
        if t.preference
        for e in t.preference.match_expressions or []
    )


def blocking(
    core: Any,
    policy: Any,
    namespaces: list[str],
    now: float,
    label: str | None = None,
    spotting: bool = False,
) -> blockers.Report:
    """The Blocker facts of `namespaces`' pods (slice 15.1b), read a page at a time. A
    namespace it may not read, or whose read fails, is left out of `namespaces`: the Hub
    then knows what it wasn't told. Over `MAX_PODS`, or over the size the Hub's proxy
    lets through: cut there, and said. With the cluster's Placement `label` (slice
    15.2b) it says it places, and which pods carry the preference."""
    read, found = [], []
    for ns in sorted(set(namespaces)):
        try:
            budgets = policy.list_namespaced_pod_disruption_budget(ns).items
            here, token = [], None
            while True:
                page = core.list_namespaced_pod(ns, limit=PAGE, _continue=token)
                here += [f for p in page.items if (f := pod_facts(p, budgets, label))]
                token = page.metadata._continue if page.metadata else None
                if not token:
                    break
        except ApiException as e:
            log.warning("the Blockers of %s not read: %s", ns, why(e))
            continue
        read.append(ns)
        found += here
    found.sort(key=lambda f: (f.namespace, f.pod))
    report = blockers.Report(
        at=now,
        namespaces=read,
        truncated=len(found) > blockers.MAX_PODS,
        pods=found[: blockers.MAX_PODS],
        placing=label is not None,
        spotting=spotting,  # slice 16.2b: only a Hub that sent spot nodes is told
    )
    while report.pods and len(report.model_dump_json()) > blockers.MAX_BYTES:
        fewer = report.pods[: len(report.pods) * 3 // 4]  # long names: cut by size too
        report = report.model_copy(update={"pods": fewer, "truncated": True})
    return report


def unevictable(apps: Any, policy: Any, ns: str, kind: str, name: str, p: Any) -> str | None:
    """Why the workload's pod `p` is never evicted, if it is not: not a Deployment's (a
    StatefulSet's keeps its state, a DaemonSet's would start again on its node), one replica
    (eviction would stop it) or no PodDisruptionBudget covering it (nothing would keep
    enough pods up). A read that fails: never evicted either."""
    if kind != "Deployment":
        return f"a {kind}'s pod is never evicted"
    try:
        replicas = apps.read_namespaced_deployment(name, ns).spec.replicas
        if (1 if replicas is None else replicas) < 2:
            return "it has one replica, so it is never evicted"
        budgets = policy.list_namespaced_pod_disruption_budget(ns).items
    except ApiException as e:
        return f"not evicted: checking it: {why(e)}"
    labels = p.metadata.labels or {}
    if not any(selects(b.spec.selector, labels) for b in budgets):
        return "no PodDisruptionBudget covers it, so it is never evicted"
    return None


def is_ready(p: Any) -> bool:
    return any(c.type == "Ready" and c.status == "True" for c in (p.status.conditions or []))


def lost(p: Any) -> bool:
    """Whether the pod's node is gone (slice 12.10), told from the pod alone: the Actuator
    reads no node. While a node is NotReady, Kubernetes sets only the pod's `Ready` to False:
    `ContainersReady` and its readiness gates stay as the kubelet last wrote them, where a
    failing probe shows both False. Or Kubernetes removed it for that (`NODE_GONE`), or its
    node shut down. Such a pod is no Sizing's doing, and no kubelet is there to resize it."""
    status = p.status
    conds = {c.type: c for c in (status.conditions if status else None) or []}
    gone = conds.get("DisruptionTarget")
    if gone and gone.status == "True" and gone.reason in NODE_GONE:
        return True
    if status and status.reason == "Terminated" and SHUTDOWN in (status.message or ""):
        return True
    gates = [g.condition_type for g in (p.spec.readiness_gates if p.spec else None) or []]
    kubelet = [conds.get(t) for t in ("ContainersReady", *gates)]  # all it makes Ready from
    ready = conds.get("Ready")
    return bool(ready and ready.status != "True" and all(c and c.status == "True" for c in kubelet))


def wait(core: Any, key: str, h: dict[str, Any], why_: str, message: str) -> None:
    """The rollout waits to evict its Infeasible pod: said once in an Event per reason."""
    if h.get("waiting") != why_:
        h["waiting"] = why_
        ns, kind, name, _ = key.split("/")
        log.info("%s waits: %s", key, message)
        post(core, ns, ("apps/v1", kind, name), "EvictionWaiting", message, False)


def evict(
    core: Any, apps: Any, key: str, h: dict[str, Any], now: float, message: str, end: Ending
) -> Actuation | None:
    """Evict the rollout's Infeasible pod through the Eviction API, so PodDisruptionBudgets
    hold (slice 12.7): its ReplicaSet starts a replacement, which the webhook gives the
    requests. A budget that allows no disruption now: it waits."""
    ns, kind, name, _ = key.split("/")
    pod = h["pod"]
    try:
        before = [p.metadata.name for p in pods_of(core, apps, read(apps, kind, name, ns), kind)]
    except ApiException as e:
        log.warning("listing %s's pods to evict %s: %s", key, pod, why(e))
        return None
    h["evicted"] = {"pod": pod, "at": now, "before": before}  # first: the webhook reads it
    body = client.V1Eviction(metadata=client.V1ObjectMeta(name=pod, namespace=ns))
    try:
        core.create_namespaced_pod_eviction(pod, ns, body)
    except ApiException as e:
        del h["evicted"]
        if e.status == 429:
            wait(core, key, h, "budget", f"{pod}: Infeasible: {message}; a PodDisruptionBudget "
                 "allows no eviction now")  # fmt: skip
            return None
        return end("failed", f"evicting {pod}: {why(e)}")
    h.pop("waiting", None)
    h["evicted_at"] = now
    h["notes"] = h.get("notes", "") + f"; {pod} evicted, its resize Infeasible"
    said = f"{pod} evicted: its resize was Infeasible ({message}); its replacement starts with "
    log.info("%s: %s", key, said + held_text(h))
    post(core, ns, ("apps/v1", kind, name), "PodEvicted", said + held_text(h), False)
    return None


def replacements(d: Any, pods: list[Any], ev: dict[str, Any]) -> list[Any] | None:
    """The pods started since the eviction `ev`; None when none is owed: none started and
    the Deployment already has as many live pods as it wants (an HPA scaled it down)."""
    new = [p for p in pods if p.metadata.name not in ev["before"]]
    wants = d.spec.replicas if d.spec.replicas is not None else 1
    return None if not new and len(pods) >= wants else new


def replaced(
    core: Any, apps: Any, key: str, h: dict[str, Any], now: float, end: Ending
) -> Actuation | None:
    """Whether the evicted pod's replacement is up with the requests: then it stands for
    the evicted pod in the rollout (a canary's 10 minutes start again). Not Ready in 10
    minutes is trouble(): a rollback."""
    ns, kind, name, container = key.split("/")
    ev = h["evicted"]
    try:
        d = read(apps, kind, name, ns)
        pods = pods_of(core, apps, d, kind)
    except ApiException as e:
        log.warning("watching %s's replacement: %s", key, why(e))
        return None
    new = replacements(d, pods, ev)
    if new is None:  # none owed: the rollout goes on with the pods it has
        del h["evicted"]
        return None
    up = [p for p in new if is_ready(p)]
    if not up:
        return None
    p = up[0]
    if not at_hold(h, p, container):
        return end(
            "failed",
            f"{p.metadata.name}, replacing evicted {ev['pod']}, started from its template",
        )
    del h["evicted"]
    h.pop("no_room", None)
    h.setdefault("pods", {})[p.metadata.name] = baseline(p, container, now)
    h.update(pod=p.metadata.name, since=now)
    if h["resized"] == 1:
        h["canary_until"] = now + CANARY_SECONDS
    return None


def gist(reason: str) -> str:
    """A reason without its numbers: a skip for a quota is the same skip at next hour's values."""
    return re.sub(r"\d[\w.]*", "N", reason)


def texts(requests: dict[str, str]) -> str:
    return ", ".join(f"{r} {q}" for r, q in sorted(requests.items()))


def baseline(pod: Any, container: str, at: float) -> dict[str, Any]:
    """What a pod was when it was resized: a later restart is counted from here."""
    return {"at": at, "restarts": restarts_of(pod, container)}


def restarts_of(pod: Any, container: str) -> int:
    st = container_status(pod, container)
    return int(st.restart_count or 0) if st else 0


def container_status(pod: Any, container: str) -> Any:
    statuses = (pod.status.container_statuses if pod.status else None) or []
    return next((s for s in statuses if s.name == container), None)


def lowered(h: dict[str, Any], limit: str | None = None) -> bool:
    """Whether the hold `h` asks less memory for its container than its template did: such
    a Sizing can bring an OOM kill on (slice 13.1b), a likelier victim on a fuller node. A
    template that sets only a memory `limit` asks that much: Kubernetes makes it the pod's
    request. Of a hold with `types` (slice 15.3b): any instance type's does."""
    sets = [h.get("requests") or {}] if h.get("types") is None else list(h["types"].values())
    had = (h.get("template") or {}).get("memory") or limit
    return bool(had and any(q.get("memory") and qty(q["memory"]) < qty(had) for q in sets))


def stepped_over(h: dict[str, Any]) -> None:
    """A hold's memory is its container's Step-up's from now, no Sizing's (slice 13.1b):
    every instance type's too."""
    del h["requests"]["memory"]
    (h.get("template") or {}).pop("memory", None)
    for q in (h.get("types") or {}).values():
        q.pop("memory", None)


def next_step(limit: Decimal, request: Decimal, top: Decimal) -> tuple[str, str] | None:
    """The memory limit and request one Step-up above these (bytes), in whole Mi: the limit
    to the larger of 1.2 times and 100 MiB more, never past `top`; the request by the same
    factor, so the very same quantity where the two were equal (a Guaranteed pod's must
    stay so). None: no whole Mi is left under `top`."""
    up = math.ceil(round(max(limit * STEP_FACTOR, limit + STEP_MORE) / MI, 6))
    new = min(up, math.floor(top / MI))
    if new * MI <= limit:
        return None
    asked = min(math.ceil(round(request * new / limit, 6)), new)  # equal ones stay equal
    return f"{new}Mi", f"{asked}Mi"


def span(seconds: float) -> str:
    """A term as a person says it: days from two on, else hours, else minutes."""
    if seconds >= 2 * 86400:
        return f"{round(seconds / 86400)} days"
    if seconds >= 2 * 3600:
        return f"{round(seconds / 3600)} hours"
    return f"{round(seconds / 60)} minutes"


def body_text(had: dict[str, Any]) -> str:
    return f"{had['limit']}, request {had['request'] or had['limit']}"


def step_body(container: str, limit: str, request: str) -> dict[str, Any]:
    """The resize that gives a pod's container a Step-up's memory limit and request."""
    resources = {"limits": {"memory": limit}, "requests": {"memory": request}}
    return {"spec": {"containers": [{"name": container, "resources": resources}]}}


def rebooted(st: Any) -> bool:
    """Whether the container last ended with its node (slice 12.11), not by itself: a node
    that restarts and comes back starts its containers again, and the runtime, having lost
    track of the old one, reports reason Unknown, exit code 255 (seen on kind, containerd).
    A crash reads Error, an OOM kill OOMKilled: those are a Sizing's to answer for."""
    ended = st.last_state.terminated if st.last_state else None
    return bool(ended and ended.reason == "Unknown" and ended.exit_code == 255)


def trouble(
    core: Any, apps: Any, key: str, h: dict[str, Any], now: float, reflexed: bool = False
) -> str | None:
    """Why the Sizing held as `h` made its pods unhealthy (slice 12.4), or None: a pod with
    its requests whose container restarted (an OOM kill named as one) since it got them, or
    a pod it resized that has been NotReady for 2 minutes since, or an evicted pod's
    replacement not Ready in 10 minutes (slice 12.7). Throttling is not one, nor a pod
    whose node is gone (`lost`, slice 12.10) or a restart that was its node's (`rebooted`,
    slice 12.11). `reflexed`: the Step-up answers for this container's OOM kills now (slice
    13.1b), so an OOM-killed pod is no trouble of the Sizing's, unless the Sizing lowered
    its memory request."""
    ns, kind, name, container = key.split("/")
    try:
        d = read(apps, kind, name, ns)
        pods = pods_of(core, apps, d, kind)
    except ApiException as e:
        log.warning("watching %s: %s", key, why(e))
        return None
    ev = h.get("evicted")
    new = replacements(d, pods, ev) if ev else []
    late = ev and now - ev["at"] >= WAIT_SECONDS
    # a replacement lost with its node (slice 12.10) is no verdict yet
    if ev and late and new is not None and not any(is_ready(p) or lost(p) for p in new):
        return f"{ev['pod']} evicted: no replacement Ready in {WAIT_SECONDS // 60} minutes"
    seen = h.get("pods") or {}
    for p in pods:
        if not at_hold(h, p, container):
            continue  # not the Actuator's requests: not its doing
        st = container_status(p, container)
        spec = next((c for c in p.spec.containers if c.name == container), None)
        own = limits_of(spec).get("memory")
        mine = seen.get(p.metadata.name) or {"at": 0.0, "restarts": 0}  # new: from the webhook
        again = bool(st) and (st.restart_count or 0) > mine["restarts"]
        if reflexed and again and oom_killed_at(st) is not None and not lowered(h, own):
            # an OOM kill since it got them is the Reflex's to answer (slice 13.1b): no
            # Sizing changes a limit. One from before is no news, and excuses nothing
            continue
        if again and not rebooted(st):
            ended = st.last_state.terminated if st.last_state else None
            what = "was OOM-killed" if ended and ended.reason == "OOMKilled" else "restarted"
            return f"{p.metadata.name}: {container} {what}"
        if "at" not in mine or p.metadata.name not in seen or lost(p):
            continue  # a new pod may take its time to be Ready; only one it resized counts,
            # and never one whose node is gone (slice 12.10)
        conds = {c.type: c for c in (p.status.conditions if p.status else None) or []}
        ready, own = conds.get("Ready"), conds.get("ContainersReady")
        if not ready or ready.status == "True" or not ready.last_transition_time:
            continue
        since = down = ready.last_transition_time.timestamp()
        if own and own.status != "True" and own.last_transition_time:
            # its node back after a while (slice 12.11): `Ready` still dates from when the
            # node went, its containers are not ready only since they started again
            down = max(down, own.last_transition_time.timestamp())
        if since >= mine["at"] and now - down >= NOT_READY_SECONDS:
            return f"{p.metadata.name}: NotReady for {NOT_READY_SECONDS // 60} minutes"
    return None


def first_run(
    core: Any, apps: Any, key: str, h: dict[str, Any], now: float, reflexed: bool = False
) -> str | None:
    """Why a CronJob's first run under the requests held as `h` (slice 12.8c) is a reason
    to roll back, or None: a pod of it with them Failed (not with its node, slice 12.10) or
    OOM-killed. That run is the first
    Job, created since, whose pods got them (one started from its template, the webhook
    missing it, is not); it ends the watch once it finished or is gone, as do 7 days
    without one."""
    ns, kind, name, container = key.split("/")
    run = h["first_run"]

    def pods(job: str) -> list[Any]:  # Succeeded and Failed ones too: pods_of drops them
        selector = f"batch.kubernetes.io/job-name={job}"
        return list(core.list_namespaced_pod(ns, label_selector=selector).items)

    try:
        cj = read(apps, kind, name, ns)
        jobs = sorted(
            (
                j
                for j in apps.list_namespaced_job(ns).items
                if any(o.uid == cj.metadata.uid for o in j.metadata.owner_references or [])
                and j.metadata.creation_timestamp.timestamp() >= run["after"]
            ),
            key=lambda j: j.metadata.creation_timestamp,
        )
        if "job" not in run:
            chosen = next(
                (
                    j.metadata.name
                    for j in jobs
                    if any(at_hold(h, p, container) for p in pods(j.metadata.name))
                ),
                None,
            )
            if chosen is None:
                if now >= run["until"]:
                    h.pop("first_run")  # no run in 7 days: kept, no longer watched
                return None
            run["job"] = chosen
        job = next((j for j in jobs if j.metadata.name == run["job"]), None)
        for p in pods(run["job"]) if job else []:
            if not at_hold(h, p, container):
                continue  # from its template: not the Actuator's doing
            st = container_status(p, container)
            killed = any(
                state and state.terminated and state.terminated.reason == "OOMKilled"
                for state in ((st.state, st.last_state) if st else ())
            )
            spec = next((c for c in p.spec.containers if c.name == container), None)
            if killed and reflexed and not lowered(h, limits_of(spec).get("memory")):
                continue  # the Reflex's to answer, as a Deployment's (slice 13.1b)
            if killed:
                return f"{p.metadata.name}: {container} was OOM-killed"
            if p.status.phase == "Failed" and not lost(p):  # its node gone: the Job's retry
                return f"{p.metadata.name} Failed"
    except ApiException as e:
        log.warning("watching %s: %s", key, why(e))
        return None
    finished = job is None or any(
        c.type in ("Complete", "Failed") and c.status == "True"
        for c in (job.status.conditions if job.status else None) or []
    )
    if finished:
        h.pop("first_run")  # its first run is over: kept, no longer watched
    return None


def rollback(core: Any, apps: Any, key: str, h: dict[str, Any], why_: str, now: float) -> Actuation:
    """Every pod with the Actuator's requests back to its template's, in place and at once;
    then no new Sizing of that container for 24 hours, doubling with each rollback in 7 days."""
    rollbacks = [t for t in h.get("rollbacks") or [] if now - t < ROLLBACKS_FOR] + [now]
    until = now + BACKOFF_SECONDS * 2 ** (len(rollbacks) - 1)
    a = put_back(core, apps, key, h, "rolled_back", why_, now)
    h.update(rollbacks=rollbacks, backoff_until=until)
    when = datetime.fromtimestamp(until, UTC)
    a.reason = f"{a.reason}; no new Sizing of it until {when:%Y-%m-%d %H:%M} UTC"[:1000]
    return a


def put_back(
    core: Any, apps: Any, key: str, h: dict[str, Any], outcome: Outcome, why_: str, now: float
) -> Actuation:
    """Every pod with the requests held as `h` back to its template's, in place and at once
    (a request the template doesn't set left as it is); `h` is no longer held."""
    ns, kind, name, container = key.split("/")
    had = h.get("template") or {}
    back = {r: had[r] for r in h["requests"] if had.get(r) is not None}
    unset = sorted(r for r in h["requests"] if had.get(r) is None)
    done, refused, stuck = 0, [], 0
    try:
        pods = pods_of(core, apps, read(apps, kind, name, ns), kind)
    except ApiException as e:
        pods, refused = [], [f"listing its pods: {why(e)}"]
    ev = h.pop("evicted", None)  # first: the webhook gives its next replacement nothing
    for p in pods:
        if ev and p.metadata.name not in ev["before"] and p.status.phase == "Pending":
            # an evicted pod's replacement, unscheduled at the held size: started again from
            # its template, the hold gone (a Pending pod's eviction ignores budgets)
            body = client.V1Eviction(
                metadata=client.V1ObjectMeta(name=p.metadata.name, namespace=ns)
            )
            try:
                core.create_namespaced_pod_eviction(p.metadata.name, ns, body)
                stuck += 1
            except ApiException as e:
                refused.append(f"evicting {p.metadata.name}: {why(e)}")
            continue
        if back and at_hold(h, p, container):
            try:
                resize(core, p, container, back)
                done += 1
            except ApiException as e:
                refused.append(f"{p.metadata.name}: {why(e)}")
    for k in ROLLOUT + WATCHED:
        h.pop(k, None)
    h["outcome"] = outcome
    if kind == "CronJob":  # never resized in place (slice 12.8c)
        reason = f"{why_}: its next run's pods start from its template, not {texts(h['requests'])}"
    else:
        each = "" if h.get("types") is None else "each instance type's requests: "
        reason = f"{why_}: {done} pods back from {each}{held_text(h)}"
        if back:
            reason += f" to their template's {texts(back)}"
        if unset:
            reason += f" ({unset} left: the template sets none)"
    if stuck:
        reason += f"; {stuck} Pending replacement evicted to start from its template"
    if refused:
        reason += f"; {len(refused)} refused, first {refused[0]}"
        if back and kind != "CronJob":
            h["unfinished"] = True
            reason += "; tried again each minute"
    return Actuation(
        namespace=ns,
        kind=kind,
        name=name,
        container=container,
        run_id=h["run_id"],
        outcome=outcome,
        pods=done,
        reason=reason[:1000],
        at=now,
        **{r: float(qty(q)) for r, q in back.items()},
    )


def undo(h: dict[str, Any]) -> dict[str, Any]:
    """What a put back still to finish needs of the hold `h`, kept when a Sizing that
    changes nothing (skipped, failed) takes its place."""
    kept = ("unfinished", "requests", "template", "types")
    return {k: h[k] for k in kept if k in h and h.get("unfinished")}


def finish(core: Any, apps: Any, key: str, h: dict[str, Any]) -> None:
    """A Rollback or Revert that some pod refused (slice 12.9a), again: each pod still with
    the requests held as `h` back to its template's, until none refuses. A template that no
    longer asks what it did then is a person's: theirs, and nothing more to undo. Only a
    refusal that may pass is tried again; another is the API server's verdict, said once."""
    ns, kind, name, container = key.split("/")
    had = h.get("template") or {}
    back = {r: had[r] for r in h["requests"] if had.get(r) is not None}
    pods = []
    try:
        d = read(apps, kind, name, ns)
        c = next((c for c in template_of(d, kind).spec.containers if c.name == container), None)
        if c is not None and all(same(requests_of(c).get(r), q) for r, q in back.items()):
            pods = pods_of(core, apps, d, kind)
    except ApiException as e:
        if e.status != 404:  # 404: the workload is gone, and its pods with it
            log.warning("putting %s back: %s", key, why(e))
            return
    again = False
    for p in pods:
        if not at_hold(h, p, container):
            continue
        try:
            resize(core, p, container, back)
        except ApiException as e:  # one pod's refusal never keeps the others waiting
            if passing(e):
                again = True
                log.warning("putting %s back: %s", p.metadata.name, why(e))
            elif e.status != 404:  # its verdict (a pod gone meanwhile aside): not tried again
                log.error("%s keeps %s: %s", p.metadata.name, held_text(h), why(e))
    if not again:
        log.info("%s: no pod left to put back", key)
        h.pop("unfinished")


EVENTS = {
    "applied": "SizingApplied",
    "skipped": "SizingSkipped",
    "failed": "SizingFailed",
    "handed_back": "HandedBack",
    "rolled_back": "SizingRolledBack",
    "reverted": "SizingReverted",
}


GONE = dict.fromkeys(("handed_back", "rolled_back", "reverted"), "back to its template")


def event(core: Any, a: Actuation, h: dict[str, Any] | None = None) -> None:
    """A Kubernetes Event on the workload, so `kubectl describe` shows what was done;
    `h`, its hold, tells Automation's from an approval's."""
    values = ", ".join(
        f"{r} {text(v)}"
        for r, v, text in (("cpu", a.cpu, cpu_text), ("memory", a.memory, memory_text))
        if v is not None
    )
    by = "Automation" if (h or {}).get("automated") else "Approved Sizing"
    if (h or {}).get("types"):  # no one number (slice 15.3b): its reason names each type's
        values = "requests by instance type"
    message = (
        f"{a.container}: {GONE.get(a.outcome) or values or 'no change'} "
        f"({by}, run {a.run_id}): {a.reason}"
    )
    warning = a.outcome in ("failed", "rolled_back")
    post(core, a.namespace, ("apps/v1", a.kind, a.name), EVENTS[a.outcome], message, warning)


def post(
    core: Any,
    ns: str,
    what: tuple[str, str, str],
    reason: str,
    message: str,
    warning: bool,
    uid: str | None = None,
) -> None:
    """An Event on `what` (api version, kind, name) in `ns`; a refused one is only logged."""
    now = datetime.now(UTC)
    api_version, kind, name = what
    api_version = API.get(kind, api_version)
    body = client.CoreV1Event(
        metadata=client.V1ObjectMeta(name=f"{name}.{uuid.uuid4().hex[:16]}"),
        involved_object=client.V1ObjectReference(
            api_version=api_version, kind=kind, name=name, namespace=ns, uid=uid
        ),
        reason=reason,
        message=message[:1024],
        type="Warning" if warning else "Normal",
        source=client.V1EventSource(component="squero-actuator"),
        reporting_component="squero-actuator",
        first_timestamp=now,
        last_timestamp=now,
        count=1,
    )
    try:
        core.create_namespaced_event(ns, body)
    except ApiException as e:
        log.warning("event on %s/%s: %s", ns, name, why(e))


def check_version(version: Any) -> None:
    """Refuse a cluster without in-place pod resize on by default."""
    v = version.get_code()
    minor = re.match(r"\d+", v.minor or "")
    if (int(v.major), int(minor.group()) if minor else 0) < (1, MIN_MINOR):
        raise SystemExit(f"Kubernetes {v.git_version}: the Actuator needs 1.{MIN_MINOR} or later")


# --- the pod-creation webhook (slice 12.2) ------------------------------------------------


def workload_of(owners: dict[str, str], labels: dict[str, str]) -> tuple[str, str] | None:
    """The kind and name of the workload a pod is of, from its owners (kind to name) and
    labels alone, no read: the webhook has 2 seconds. None: not one the Actuator handles."""
    kind = next((k for k in ("StatefulSet", "DaemonSet") if owners.get(k)), "Deployment")
    name = owners.get(kind, "")
    # a CronJob's run, its Job named for its minute (slice 12.8c); a lone Job's pod falls
    # through to a Deployment's, which its missing `pod-template-hash` refuses
    run = re.fullmatch(r"(.+)-(\d+)", owners.get("Job", ""))
    if run and not -5 <= time.time() / 60 - int(run.group(2)) <= RUN_AGE:
        run = None  # digits that are no recent minute: a lone Job's own name (slice 12.9b)
    if kind == "Deployment" and run:
        kind, name = "CronJob", run.group(1)
    elif kind == "Deployment" and "rollouts-pod-template-hash" in labels:
        kind = "Rollout"  # Argo's ReplicaSet, named and labelled as a Deployment's (12.8b)
    if kind in ("Deployment", "Rollout"):
        hashed = labels.get(
            "rollouts-pod-template-hash" if kind == "Rollout" else "pod-template-hash"
        )
        rs = owners.get("ReplicaSet", "")
        if not hashed or not rs.endswith(f"-{hashed}"):
            return None
        name = rs.removesuffix(f"-{hashed}")
    return kind, name


def oom_killed_at(st: Any) -> float | None:
    """When the container was last OOM-killed, as its status shows it (running again, or
    ended for good), or None. The runtime's own word only: a kill it reports as `Error`,
    137 (it sometimes misses the cgroup's event) is not told from a SIGKILL."""
    for state in (st.state, st.last_state):
        ended = state.terminated if state else None
        if ended and ended.reason == "OOMKilled" and ended.finished_at:
            return float(ended.finished_at.timestamp())
    return None


def system_ooms(core: Any, node: str) -> list[float]:
    """When `node`'s kubelet saw the node itself out of memory: its `SystemOOM` Events,
    which like every Node's are in `default` (slice 13.1a). Raises ApiException."""
    selector = f"involvedObject.kind=Node,involvedObject.name={node},reason=SystemOOM"
    events = core.list_namespaced_event("default", field_selector=selector).items
    times = ((e.last_timestamp, e.event_time, e.first_timestamp) for e in events)
    return [float(t.timestamp()) for ts in times for t in ts if t]


def stepped_for(
    st: dict[str, Any] | None, kind: str, requests: dict[str, str], limits: dict[str, str]
) -> tuple[str, str] | None:
    """The memory limit and request a new pod's container starts with under its Step-up
    `st` (slice 13.1b), or None: none held; a DaemonSet's (a raise reaches it in place
    only, as slice 12.8a has it); or its template's memory is no longer what the Step-up
    started from (a person's change wins). A template with only a limit asks that much."""
    if not st or kind == "DaemonSet":
        return None
    had = st["template"]
    asked = requests.get("memory")
    if not same(limits.get("memory"), had["limit"]) or not (
        same(asked, had["request"]) or (had["request"] is None and same(asked, had["limit"]))
    ):
        return None
    return st["limit"], st["request"]


def boosted(c: dict[str, Any], factor: float | None, kind: str, limit: str | None) -> str | None:
    """The CPU limit a new pod's container `c` starts with under its Startup boost (slice
    13.4), from `limit` (its own, or its Burst's), or None: no boost for it, no CPU limit
    to raise, a CronJob's run, or a `resizePolicy` that would restart the container when
    the limit is given back."""
    restarts = any(
        p.get("resourceName") == "cpu" and p.get("restartPolicy") == "RestartContainer"
        for p in c.get("resizePolicy") or []
    )
    if not factor or not limit or kind == "CronJob" or restarts:
        return None
    return cpu_text(float(qty(limit)) * factor)


def mutation(
    req: dict[str, Any],
    held: dict[str, dict[str, Any]],
    own: str,
    paused: Any = frozenset(),
    stepped: dict[str, dict[str, Any]] | None = None,
    boosts: dict[str, float] | None = None,
    bursts: dict[str, dict[str, Any]] | None = None,
    nodes: dict[str, str] | None = None,
    placing: Placing | None = None,
    steering: Callable[[dict[str, Any], str, str, str, bool], list[Any]] | None = None,
) -> list[Any]:
    """The JSON Patch giving a new pod of a workload the requests applied to it: only
    where its container still asks what the template asked then (a person's change wins),
    never above its limit, never in kube-* or the Actuator's own namespace, never for a
    paused workload (`ns/kind/name` in `paused`). A DaemonSet's pod gets only requests
    lower than its template's (slice 12.8a): it must fit on whatever node it starts; a raise
    reaches it in place, where its node has room; and of a hold with `types` (slice 15.3b)
    its node's instance type's, by `nodes`, nothing where its node or that type is not
    known. An Argo Rollout's pod is told by its
    `rollouts-pod-template-hash` label (slice 12.8b). A container in `boosts` starts with
    its CPU limit times its factor (the Startup boost, slice 13.4), its own limit kept in
    the pod's `BOOST` annotation; never a pod that already carries one (its template's),
    nor one with pod-level resources. A container in `bursts` starts at its Burst's CPU
    limit (slice 13.3b) while its own is still its template's then, a boost on top of it.
    A pod of a Workload `placing` lists prefers its Pinned nodes (slice 15.2b, `placed`).
    Last, `steering` puts a pod of a Workload under a Spot ratio on its side (slice 16.2b),
    given the pod as the rest leaves it, its namespace, Workload and whether the review is
    a dry run (counted then is never created)."""
    ns = req.get("namespace") or ""
    if req.get("operation") != "CREATE" or ns == own or ns.startswith("kube-"):
        return []
    pod = req.get("object") or {}
    meta = pod.get("metadata") or {}
    owners = {o.get("kind"): o.get("name", "") for o in meta.get("ownerReferences") or []}
    labels = meta.get("labels") or {}
    of = workload_of(owners, labels)
    if of is None:
        return []  # not a workload's pod it resizes
    kind, name = of
    if f"{ns}/{kind}/{name}" in paused:
        return []
    ops: list[Any] = []
    notes = meta.get("annotations")
    # no CPU limit raised past a pod-level one (`spec.resources`), which would refuse the
    # pod; and no boost twice
    bounded = bool((pod.get("spec") or {}).get("resources"))
    unboosted = BOOST in (notes or {}) or bounded
    own_limits: dict[str, str] = {}  # of the containers boosted
    everyone = [
        (
            x.get("name", ""),
            dict((x.get("resources") or {}).get("requests") or {}),
            dict((x.get("resources") or {}).get("limits") or {}),
        )
        for x in (pod.get("spec") or {}).get("containers") or []
    ]
    for i, c in enumerate((pod.get("spec") or {}).get("containers") or []):
        key = f"{ns}/{kind}/{name}/{c.get('name')}"
        h = held.get(key) or {}
        resources = c.get("resources") or {}
        requests = resources.get("requests") or {}
        limits = resources.get("limits") or {}
        new = {}
        # applied, or rolling with a pod evicted for these requests (slice 12.7); and its
        # template's requests still what they were: changed since, they are theirs
        had = h.get("template")  # None: held before slice 12.2 recorded it
        if (
            (h.get("outcome") == "applied" or "evicted" in h)
            and had is not None
            and all(same(requests.get(r), had.get(r)) for r in h["requests"])
        ):
            its = h["requests"]
            if h.get("types") is not None:  # its node's instance type's (slice 15.3b)
                its = h["types"].get((nodes or {}).get(node_of(pod) or ""), {})
            for r, q in its.items():
                if r in limits and qty(q) > qty(limits[r]):
                    q = limits[r]
                if kind == "DaemonSet" and not (requests.get(r) and qty(q) < qty(requests[r])):
                    continue  # a raise: in place only, where its node has room
                if not same(requests.get(r), q):
                    new[r] = q
        up = stepped_for((stepped or {}).get(key), kind, requests, limits)
        grown = {**limits, "memory": up[0]} if up else limits
        if up:  # its memory is its Step-up's (slice 13.1b), whatever a Sizing holds
            new["memory"] = up[1]
        mine = c.get("name", "")

        def keeps(asks: dict[str, str], lims: dict[str, str], mine: str = mine) -> bool:
            after = [
                ({**r, **asks} if n == mine else r, lims if n == mine else lim)
                for n, r, lim in everyone
            ]
            return qos([(r, lim) for _, r, lim in everyone]) == qos(after)

        if new and not keeps(new, grown):
            new, up = {}, None  # a pod's QoS class stays its template's (slice 12.3)
        # a Guaranteed pod's limit can't leave its request (a limit alone is its request)
        asks = {"cpu": requests.get("cpu", limits.get("cpu", ""))}
        burst = (bursts or {}).get(key)
        raised = None  # its Burst's limit, while its own is still its template's then
        if (
            burst
            and not bounded
            and kind != "CronJob"
            and same(limits.get("cpu"), burst["template"])
        ):
            raised = burst["limit"] if keeps(asks, {**limits, "cpu": burst["limit"]}) else None
        top = raised or limits.get("cpu")
        more = None if unboosted else boosted(c, (boosts or {}).get(key), kind, top)
        if more and not keeps(asks, {**limits, "cpu": more}):
            more = None
        if (more or raised) and "cpu" not in requests:
            # a limit alone is its request too: said, so the request stays where it was
            new.setdefault("cpu", limits["cpu"])
        at = f"/spec/containers/{i}/resources"
        if not new:
            pass
        elif "resources" not in c:
            ops.append({"op": "add", "path": at, "value": {"requests": new}})
        elif "requests" not in resources:
            ops.append({"op": "add", "path": f"{at}/requests", "value": new})
        else:
            ops += [{"op": "add", "path": f"{at}/requests/{r}", "value": q} for r, q in new.items()]
        if up:
            ops.append({"op": "add", "path": f"{at}/limits/memory", "value": up[0]})
        if more or raised:
            ops.append({"op": "add", "path": f"{at}/limits/cpu", "value": more or raised})
        if more:
            assert top is not None
            own_limits[mine] = top  # what it is given back to: its Burst's while that lasts
    listed = placing.workloads or [] if placing else []
    if placing and f"{ns}/{kind}/{name}" in listed and kind != "DaemonSet":  # it stays put
        ops += placed(pod, placing)
    if own_limits:
        value = json.dumps(own_limits, sort_keys=True)
        if notes is None:
            ops.append({"op": "add", "path": "/metadata/annotations", "value": {BOOST: value}})
        else:
            at = "/metadata/annotations/" + BOOST.replace("/", "~1")
            ops.append({"op": "add", "path": at, "value": value})
    if steering:  # last, on the pod as the rest leaves it: both may add an affinity
        ops += steering(added(pod, ops), ns, kind, name, bool(req.get("dryRun")))
    return ops


PREFERRED = "preferredDuringSchedulingIgnoredDuringExecution"


def placed(pod: dict[str, Any], placing: Placing) -> list[Any]:
    """The JSON Patch that gives a new pod Placement (slice 15.2b): one preferred node
    affinity term of weight 100 for the Pinned nodes' label, after the pod's own terms
    and never in place of one (a preference, never a requirement: a pod with no room
    there starts elsewhere), none where it prefers that label already; and a toleration
    of their taint, where the Contract names one and the pod has none like it."""
    spec = pod.get("spec") or {}
    key, _, value = placing.label.partition("=")
    term = {
        "weight": 100,
        "preference": {"matchExpressions": [{"key": key, "operator": "In", "values": [value]}]},
    }
    affinity = spec.get("affinity")
    node = (affinity or {}).get("nodeAffinity")
    terms = (node or {}).get(PREFERRED)
    has = any(
        e.get("key") == key and e.get("operator") == "In" and value in (e.get("values") or [])
        for t in terms or []
        for e in (t.get("preference") or {}).get("matchExpressions") or []
    )
    ops: list[Any] = []
    if has:
        pass
    elif affinity is None:
        ops.append(
            {"op": "add", "path": "/spec/affinity", "value": {"nodeAffinity": {PREFERRED: [term]}}}
        )
    elif node is None:
        ops.append(
            {"op": "add", "path": "/spec/affinity/nodeAffinity", "value": {PREFERRED: [term]}}
        )
    elif terms is None:
        ops.append(
            {"op": "add", "path": f"/spec/affinity/nodeAffinity/{PREFERRED}", "value": [term]}
        )
    else:
        ops.append(
            {"op": "add", "path": f"/spec/affinity/nodeAffinity/{PREFERRED}/-", "value": term}
        )
    if placing.taint:
        pair, _, effect = placing.taint.rpartition(":")
        tkey, eq, tvalue = pair.partition("=")
        tolerates = {"key": tkey, "operator": "Equal" if eq else "Exists", "effect": effect}
        tolerates |= {"value": tvalue} if eq else {}
        have = spec.get("tolerations")
        if have is None:
            ops.append({"op": "add", "path": "/spec/tolerations", "value": [tolerates]})
        elif not any({k: t.get(k) for k in tolerates} == tolerates for t in have):
            ops.append({"op": "add", "path": "/spec/tolerations/-", "value": tolerates})
    return ops


REQUIRED = "requiredDuringSchedulingIgnoredDuringExecution"
ROLLOUT_HASH = "rollouts-pod-template-hash"
CAPACITY = "squero.io/capacity"  # the side its webhook put a pod on (slice 16.2b)
DELETION_COST = "controller.kubernetes.io/pod-deletion-cost"
SIDES = ("on-demand", "spot")
SPOT_BUDGET = 1.2  # seconds for both reads at admission: the API server gives the webhook 2


def added(pod: dict[str, Any], ops: list[Any]) -> dict[str, Any]:
    """A copy of the pod as the webhook's own JSON Patch leaves it: `add`s only, at the
    paths it builds (a list only ever appended to, `-`; `~1` for a key's `/`)."""
    out = copy.deepcopy(pod)
    for op in ops:
        *path, last = [p.replace("~1", "/").replace("~0", "~") for p in op["path"].split("/")[1:]]
        node: Any = out
        for p in path:
            node = node[int(p)] if isinstance(node, list) else node[p]
        if isinstance(node, list):
            node.append(op["value"])
        else:
            node[last] = op["value"]
    return out


def escape(key: str) -> str:
    """A map key as a JSON Patch path segment."""
    return key.replace("~", "~0").replace("/", "~1")


def settled(pod: dict[str, Any], key: str) -> bool:
    """Whether a pod's own spec already decides whether it runs on spot nodes (slice
    16.2b): a nodeSelector or a required node affinity on the spot label's `key`."""
    spec = pod.get("spec") or {}
    if key in (spec.get("nodeSelector") or {}):
        return True
    needs = ((spec.get("affinity") or {}).get("nodeAffinity") or {}).get(REQUIRED) or {}
    return any(
        e.get("key") == key
        for t in needs.get("nodeSelectorTerms") or []
        for e in t.get("matchExpressions") or []
    )


def steered(pod: dict[str, Any], side: str, spot: "Spotting") -> list[Any]:
    """The JSON Patch that puts a new pod on its side (slice 16.2b, ADR 0006), against
    the pod as the webhook's other changes already left it. Both get the `CAPACITY` label
    the minute's count goes by. On-demand: required off the spot nodes (`NotIn`, which a
    node without the label meets), in each of the pod's own required terms, which are
    ORed, so a term beside them would loosen them; and a deletion cost of 1, so a
    scale-down takes spot replicas first, unless the pod sets its own. Spot: a preference
    for the spot nodes and their taint's toleration, as Placement's (`placed`)."""
    meta = pod.get("metadata") or {}
    ops: list[Any] = []
    if meta.get("labels") is None:
        ops.append({"op": "add", "path": "/metadata/labels", "value": {CAPACITY: side}})
    else:
        ops.append({"op": "add", "path": f"/metadata/labels/{escape(CAPACITY)}", "value": side})
    if side == "spot":
        return ops + placed(pod, Placing(label=spot.label, taint=spot.taint))
    key, _, value = spot.label.partition("=")
    off = {"key": key, "operator": "NotIn", "values": [value]}
    affinity = (pod.get("spec") or {}).get("affinity")
    node = (affinity or {}).get("nodeAffinity")
    terms = ((node or {}).get(REQUIRED) or {}).get("nodeSelectorTerms") or []
    needs = {REQUIRED: {"nodeSelectorTerms": [{"matchExpressions": [off]}]}}
    if affinity is None:
        ops.append({"op": "add", "path": "/spec/affinity", "value": {"nodeAffinity": needs}})
    elif node is None:
        ops.append({"op": "add", "path": "/spec/affinity/nodeAffinity", "value": needs})
    elif not terms:
        ops.append({"op": "add", "path": f"/spec/affinity/nodeAffinity/{REQUIRED}",
                    "value": needs[REQUIRED]})  # fmt: skip
    else:
        at = f"/spec/affinity/nodeAffinity/{REQUIRED}/nodeSelectorTerms"
        for i, t in enumerate(terms):
            if t.get("matchExpressions") is None:
                ops.append({"op": "add", "path": f"{at}/{i}/matchExpressions", "value": [off]})
            else:
                ops.append({"op": "add", "path": f"{at}/{i}/matchExpressions/-", "value": off})
    notes = meta.get("annotations")
    if notes is None:
        ops.append({"op": "add", "path": "/metadata/annotations", "value": {DELETION_COST: "1"}})
    elif DELETION_COST not in notes:
        at = f"/metadata/annotations/{escape(DELETION_COST)}"
        ops.append({"op": "add", "path": at, "value": "1"})
    return ops


def sides(pods: list[Any], spot_nodes: set[str] | None) -> dict[str, int]:
    """How many of a ReplicaSet's live pods run on each side (slice 16.2b): by the label
    its webhook gave them; one it didn't place by its node, where the spot nodes are
    known; a Pending one without the label, or any where they aren't, as neither.
    Terminating and finished pods are gone already."""
    out = dict.fromkeys(SIDES, 0)
    for p in pods:
        if p.metadata.deletion_timestamp or p.status.phase in ("Succeeded", "Failed"):
            continue
        side = (p.metadata.labels or {}).get(CAPACITY)
        node = p.spec.node_name
        if side not in SIDES and node and spot_nodes is not None:
            side = "spot" if node in spot_nodes else "on-demand"
        if side in SIDES:
            out[side] += 1
    return out


def admit(
    review: Any,
    held: dict[str, dict[str, Any]],
    killed: bool,
    own: str,
    paused: Any = frozenset(),
    stepped: dict[str, dict[str, Any]] | None = None,
    boosts: dict[str, float] | None = None,
    bursts: dict[str, dict[str, Any]] | None = None,
    nodes: dict[str, str] | None = None,
    placing: Placing | None = None,
    steering: Callable[[dict[str, Any], str, str, str, bool], list[Any]] | None = None,
) -> dict[str, Any]:
    """The AdmissionReview answer: always allowed; a patch unless the Kill switch is on."""
    req = review.get("request") if isinstance(review, dict) else None
    req = req if isinstance(req, dict) else {}
    answer: dict[str, Any] = {"uid": req.get("uid", ""), "allowed": True}
    try:
        ops = (
            []
            if killed
            else mutation(req, held, own, paused, stepped, boosts, bursts, nodes, placing, steering)
        )
    except Exception:  # fail open, as the webhook's failurePolicy does
        log.exception("webhook: pod left as it is")
        ops = []
    if ops:
        answer["patchType"] = "JSONPatch"
        answer["patch"] = base64.b64encode(json.dumps(ops).encode()).decode()
    return {"apiVersion": "admission.k8s.io/v1", "kind": "AdmissionReview", "response": answer}


def certificate(host: str) -> tuple[bytes, bytes]:
    """A self-signed certificate for `host` and its key (PEM), new at every start: the
    Actuator patches it into its own webhook's caBundle, so no cert-manager is needed."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    pem = serialization.Encoding.PEM
    private = key.private_bytes(
        pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    return cert.public_bytes(pem), private


def trust(admission: Any, cert: bytes) -> None:
    """Its webhook's caBundle becomes this start's certificate (RBAC: this object only)."""
    ca = base64.b64encode(cert).decode()
    patch = [{"op": "replace", "path": "/webhooks/0/clientConfig/caBundle", "value": ca}]
    admission.patch_mutating_webhook_configuration(WEBHOOK, patch)


def serve(a: "Actuator", cert: bytes, key: bytes, port: int = WEBHOOK_PORT) -> ThreadingHTTPServer:
    """The webhook over TLS on `port`, in its own threads; it reads `a`'s holds and Kill
    switch as they are at each pod's creation."""

    class Webhook(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            path = urlsplit(self.path).path  # the API server adds ?timeout=2s
            if path != "/mutate" or not 0 < length <= MAX_REVIEW:
                self.send_error(404 if path != "/mutate" else 413)
                return
            try:
                review = json.loads(self.rfile.read(length))
            except ValueError:
                self.send_error(400)
                return
            answer = admit(
                *(review, a.held, a.killed, a.namespace, a.paused, a.stepped, a.boosting),
                *(a.bursts, a.node_types, a.placement),
                # only with a list: a cluster without spot pays nothing for it
                a.steer_pod if a.spot and a.spot.workloads else None,
            )
            body = json.dumps(answer).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            pass  # every pod's creation: too much for the log

    server = ThreadingHTTPServer(("0.0.0.0", port), Webhook)  # noqa: S104
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    with tempfile.TemporaryDirectory() as d:  # ssl reads files; gone once loaded
        (Path(d) / "tls.crt").write_bytes(cert)
        (Path(d) / "tls.key").write_bytes(key)
        tls.load_cert_chain(Path(d) / "tls.crt", Path(d) / "tls.key")
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@dataclass
class Actuator:
    """One cluster's Actuator: `step` is one minute's work."""

    http: httpx.Client  # base_url the Hub, with the token's header
    core: Any
    apps: Any
    namespace: str
    held: dict[str, dict[str, Any]] = field(default_factory=dict)
    unsent: list[Actuation] = field(default_factory=list)
    stored: str = ""  # its state() as last saved
    reverting: frozenset[str] | set[str] = frozenset()  # containers with a Revert in this pull
    killed: bool = False
    autoscaling: Any = None  # AutoscalingV2Api: HPAs (slice 12.3)
    custom: Any = None  # CustomObjectsApi: VPAs
    paused: frozenset[str] = frozenset()  # `ns/kind/name` of the workloads a person paused
    unanswered: float | None = None  # since when the Hub hasn't answered
    told: bool = False  # the Event that it hasn't is out
    eviction: str = "off"  # the Hub's last word on eviction (slice 12.7)
    policy: Any = None  # PolicyV1Api: PodDisruptionBudgets, read before an eviction
    # slice 13.1a, in memory only (the Hub stores a report sent again once): when each
    # container's OOM kills were last reported
    oom_said: dict[tuple[str, ...], float] = field(default_factory=dict)
    # slice 13.1b: each container's Step-up (its memory limit and request, its template's
    # then, when, how many), the times of this hour's, and what the Hub last allowed
    stepped: dict[str, dict[str, Any]] = field(default_factory=dict)
    steps: list[float] = field(default_factory=list)
    oom_max: float | None = None
    reflexed: frozenset[str] = frozenset()  # containers whose OOM kills it answers this minute
    # slice 13.1c: the Step-up's term, and each listed container's memory peak, as the Hub
    # last said them
    oom_expires: float | None = None
    peaks: dict[str, float | None] = field(default_factory=dict)
    events_unread: bool = False  # said once: it may not read Node Events
    # slice 13.4, the Startup boost: each container's factor as the Hub last said it (kept,
    # so it goes on while the Hub is unreachable); those the webhook boosts now (`boosting`:
    # no VPA, nothing in its namespace that would refuse the pod); the namespaces whose
    # pods may still carry a boost to end; and why not, said once
    boosts: dict[str, float] = field(default_factory=dict)
    boosting: dict[str, float] = field(default_factory=dict)
    boost_ns: set[str] = field(default_factory=set)
    boost_bars: dict[str, str] = field(default_factory=dict)
    # slice 13.3b, the Burst: each container's (its CPU limit, its template's then, when,
    # how many steps, its term), and the last of the Hub's reads handled for each listed
    # container, both kept; what the Hub last allowed and said; and, in memory, the "no
    # Burst" last said of each container, so a standing reason is said once a day
    bursts: dict[str, dict[str, Any]] = field(default_factory=dict)
    burst_seen: dict[str, int] = field(default_factory=dict)
    burst_max: float | None = None
    burst_expires: float | None = None
    burst_peaks: dict[str, float | None] = field(default_factory=dict)
    burst_said: dict[str, tuple[str, float]] = field(default_factory=dict)
    blockers_due: float = 0.0  # when its next report of Blockers' facts goes (slice 15.1b)
    # slice 15.3b: each node's instance type, as the Hub last said it from a good read
    node_types: dict[str, str] = field(default_factory=dict)
    # slice 15.2b: the cluster's Placement as the Hub last said it, its list the last one
    # it knew; in memory, so a restart says its Workloads' Events again
    placement: Placing | None = None
    placed_news: list[tuple[str, str, str]] = field(default_factory=list)  # to say on them
    # slice 16.2b: the cluster's spot nodes as the Hub last said them, its list and nodes
    # the last it knew; per ReplicaSet (`ns/name`) its replicas (None: read at the next
    # admission) and its pods on each side, recounted each minute and kept up with the
    # webhook's admissions, which run in threads of their own: under `spot_lock`
    spot: Spotting | None = None
    spot_counts: dict[str, dict[str, Any]] = field(default_factory=dict)
    spot_lock: threading.Lock = field(default_factory=threading.Lock)
    spot_seq: dict[str, int] = field(default_factory=dict)  # admissions counted, per key

    def load(self) -> None:
        try:
            cm = self.core.read_namespaced_config_map(CONFIGMAP, self.namespace)
        except ApiException as e:
            if e.status != 404:
                raise
            return
        self.held = json.loads((cm.data or {}).get(KEY) or "{}")
        try:  # what it can't read (another version's) is dropped, never its holds with it
            state = json.loads((cm.data or {}).get(STATE) or "{}")
            self.killed = bool(state.get("killed", False))
            self.stepped = dict(state.get("stepped") or {})
            self.steps = [float(t) for t in state.get("steps") or []]
            self.boosts = {k: float(f) for k, f in (state.get("boosts") or {}).items()}
            self.boost_ns = set(state.get("boost_ns") or [])
            self.bursts = dict(state.get("bursts") or {})
            self.burst_seen = {k: int(r) for k, r in (state.get("burst_seen") or {}).items()}
            for a in state.get("unsent") or []:
                try:
                    self.unsent.append(Actuation(**a))
                except (ValidationError, TypeError) as e:
                    log.error("a stored report dropped: %s", type(e).__name__)
        except (ValueError, AttributeError) as e:
            log.error("reading %s: %s", STATE, type(e).__name__)
        self.stored = self.state()

    def state(self) -> str:
        """What a restart must not forget beside its holds (slice 12.9b): the Kill switch as
        the Hub last said it (the Hub may be unreachable then), and the reports not yet taken."""
        unsent = [a.model_dump() for a in self.unsent[-KEPT_REPORTS:]]
        more = {"stepped": self.stepped, "steps": self.steps} if self.stepped or self.steps else {}
        if self.boosts or self.boost_ns:
            more |= {"boosts": self.boosts, "boost_ns": sorted(self.boost_ns)}
        if self.bursts or self.burst_seen:
            more |= {"bursts": self.bursts, "burst_seen": self.burst_seen}
        return json.dumps({"killed": self.killed, "unsent": unsent, **more}, sort_keys=True)

    def save(self) -> None:
        body = client.V1ConfigMap(
            metadata=client.V1ObjectMeta(name=CONFIGMAP),
            data={KEY: json.dumps(self.held, sort_keys=True), STATE: (state := self.state())},
        )
        try:
            try:
                self.core.replace_namespaced_config_map(CONFIGMAP, self.namespace, body)
            except ApiException as e:
                if e.status != 404:
                    raise
                self.core.create_namespaced_config_map(self.namespace, body)
            self.stored = state
        except ApiException as e:  # pods already match: a restart finds nothing to change
            log.error("saving %s: %s", CONFIGMAP, why(e))

    def pull(self) -> Desired | None:
        try:
            params = {"kinds": ",".join(KINDS), "types": "1"}  # slice 15.3b: it knows types
            r = self.http.get("/actuator/desired", params=params)
            r.raise_for_status()
            return Desired.model_validate_json(r.content)
        except httpx.HTTPStatusError as e:
            log.warning("the Hub answered %s for the desired state", e.response.status_code)
        except (httpx.HTTPError, ValidationError) as e:
            log.warning("the Hub's desired state: %s", type(e).__name__)
        return None

    def report(self) -> None:
        while self.unsent:
            batch = self.unsent[:MAX_REPORTS]
            try:
                r = self.http.post("/actuator/reports", json=[a.model_dump() for a in batch])
                r.raise_for_status()
            except httpx.HTTPError as e:
                code = e.response.status_code if isinstance(e, httpx.HTTPStatusError) else None
                if code and 400 <= code < 500 and code not in (401, 403, 429):
                    # the Hub's verdict on these reports, not an outage: resending never helps
                    log.error("the Hub refused %d reports (%d): dropped", len(batch), code)
                    del self.unsent[: len(batch)]
                    continue
                log.warning("reporting to the Hub: %s", code or type(e).__name__)
                self.unsent = self.unsent[-MAX_REPORTS:]  # the oldest go first
                return
            del self.unsent[: len(batch)]

    def step(self) -> None:
        desired = self.pull()
        self.answered(desired is not None, time.time())
        if desired is not None and desired.kill_switch != self.killed:
            self.killed = desired.kill_switch
            log.warning("the Kill switch is %s", "on" if self.killed else "off")
        if desired is not None and not desired.kill_switch:  # else kept as last said
            self.boosts = {
                "/".join((b.namespace, b.kind, b.name, b.container)): b.factor
                for b in desired.boosts
            }
        if desired is not None and desired.node_types is not None:  # else as last said
            self.node_types = desired.node_types
        if desired is not None and not desired.kill_switch:  # else kept as last said
            self.place(desired.placement)
            self.steer(desired.spot)  # slice 16.2b
        wanted = (desired.sizings + desired.automated) if desired else []
        mine = {*self.held, *self.stepped, *self.boosts, *self.bursts}
        mine |= set(self.placement.workloads or []) if self.placement else set()  # 15.2b
        mine |= (
            {f"{w.namespace}/{w.kind}/{w.name}" for w in (self.spot.workloads or [])}
            if self.spot
            else set()
        )  # 16.2b
        self.pauses({*mine, *(f"{w.namespace}/{w.kind}/{w.name}" for w in wanted)})
        done: list[Actuation] = []
        undoing = any("unfinished" in h for h in self.held.values())  # saved once finished too
        # slice 13.1b: the Step-up answers for these containers' OOM kills this minute, so
        # they are no Rollback; none when it won't look (the Hub unreachable or of 13.1a,
        # the Kill switch, reports still waiting): then an OOM kill is a Rollback, as before
        looks = desired is not None and not self.killed and not self.unsent
        self.oom_max = desired.oom_max if desired is not None and looks else None
        self.reflexed = frozenset(
            "/".join((w.namespace, w.kind, w.name, w.container))
            for w in (desired.oom if desired is not None and self.oom_max is not None else [])
        )
        # slice 13.3b: no Burst from a Hub of 13.3a (it would refuse the outcome), nor when
        # it won't look
        self.burst_max = desired.burst_max if desired is not None and looks else None
        try:
            done += self.hand_back()
            if not self.killed:  # the Actuator undoes its own harm, the Hub reachable or not
                done += self.watch()
                for key, h in self.held.items():
                    if h.get("unfinished"):
                        finish(self.core, self.apps, key, h)
                self.keep_boosts()
            if desired is not None and not self.killed:  # the Hub's word: rollouts go on
                self.eviction = desired.eviction
                self.reverting = {
                    "/".join((u.namespace, u.kind, u.name, u.container)) for u in desired.reverts
                }
                done += self.revert(desired.reverts) + self.advance(desired.eviction)
                done += self.act(desired.sizings, desired.automated)
                done += self.catch_up()
                self.oom_expires = desired.oom_expires
                self.peaks = {
                    "/".join((w.namespace, w.kind, w.name, w.container)): w.peak
                    for w in desired.oom
                }
                done += self.keep_steps()
                if desired.oom and looks:  # never crowding out what the Hub hasn't
                    done += self.ooms(desired.oom)  # taken yet (slice 13.1a)
                # slice 13.3b, after the OOM kills: a Step-up takes its place in the
                # cluster's hour before a Burst does
                self.burst_expires = desired.burst_expires
                self.burst_peaks = {
                    "/".join((t.namespace, t.kind, t.name, t.container)): t.peak
                    for t in desired.burst
                }
                done += self.keep_bursts()
                if self.burst_max is not None:
                    done += self.burst_all(desired.burst, desired.burst_run, time.time())
        except Exception:  # its own bug: what it changed before is kept, not done twice
            self.unsent += done
            self.save()
            self.send()  # a Rollback before the bug still reaches the Hub
            raise
        rolling = any(
            h.get("outcome") == "rolling" or "pods" in h or "first_run" in h
            for h in self.held.values()
        )
        self.unsent += done
        if done or rolling or undoing:
            self.save()  # before reporting: a restart sends again what the Hub didn't take
        self.send()
        try:  # on its own: an Event never costs a Rollback (slice 15.2b)
            self.say_placed()
        except Exception:
            log.exception("saying Placement's Events failed")
        if not self.killed:
            try:  # on its own too: a count is the webhook's, never a Rollback's (16.2b)
                self.recount()
            except Exception:
                log.exception("counting the steered pods failed")
        if desired is not None and desired.blockers is not None:
            try:  # last and on its own: a read for a report never costs a Rollback
                self.blocked(desired.blockers, time.time())
            except Exception:
                log.exception("reporting its Blockers failed")

    def place(self, given: Placing | None) -> None:
        """Take the Hub's Placement (slice 15.2b): None, off; a list that isn't known now
        keeps the last one. Each Workload that joins the list or leaves it is said on it
        later in the minute (`say_placed`): its new pods prefer the Pinned nodes, or no
        longer do; its running pods keep what they have."""
        had = set((self.placement.workloads or []) if self.placement else [])
        if given is not None and given.workloads is None:
            given = given.model_copy(update={"workloads": sorted(had)})
        now = set((given.workloads or []) if given else [])
        also = f", tolerating {given.taint}" if given and given.taint else ""
        for key in sorted(now - had):
            assert given is not None
            said = f"Placement: its new pods prefer the nodes labelled {given.label}{also}"
            self.placed_news.append((key, "Placed", said))
        for key in sorted(had - now):
            said = "Placement ended: its new pods are placed as before; running ones keep theirs"
            self.placed_news.append((key, "PlacementEnded", said))
        self.placement = given

    def steer(self, given: Spotting | None) -> None:
        """Take the Hub's spot nodes (slice 16.2b): None, none; a list or nodes not known
        now keep the last. Each Workload that joins the list or leaves it is said on it
        later in the minute, as Placement's are."""
        had = (
            {f"{w.namespace}/{w.kind}/{w.name}": w for w in (self.spot.workloads or [])}
            if self.spot
            else {}
        )
        if given is not None and self.spot is not None:
            if given.workloads is None:
                given = given.model_copy(update={"workloads": self.spot.workloads})
            if given.nodes is None:
                given = given.model_copy(update={"nodes": self.spot.nodes})
        now = (
            {f"{w.namespace}/{w.kind}/{w.name}": w for w in (given.workloads or [])}
            if given
            else {}
        )
        for key in sorted(now.keys() - had.keys()):
            assert given is not None
            w = now[key]
            said = (
                f"Spot: none of its new replicas on the nodes labelled {given.label}, its pods "
                "blocking a node" if not w.ratio else
                f"Spot: {w.ratio:.0%} of its new replicas prefer the nodes labelled "
                f"{given.label}, at least {w.on_demand} kept off them"
            )  # fmt: skip
            self.placed_news.append((key, "SpotPlaced", said))
        for key in sorted(had.keys() - now.keys()):
            said = "Spot ended: its new replicas are placed as before; running ones keep theirs"
            self.placed_news.append((key, "SpotEnded", said))
        self.spot = given

    def steer_pod(self, pod: dict[str, Any], ns: str, kind: str, name: str, dry: bool) -> list[Any]:
        """A new pod's side (slice 16.2b), by its ReplicaSet: on-demand while fewer of its
        pods are than the Spot ratio keeps there, then spot. Its replicas are read once a
        minute, and again once the pods counted reach them (a rollout grows its new
        ReplicaSet within the minute); a ReplicaSet counted full is recounted, its pods
        listed (a replacement for a pod lost since: the Operator's choice). Both reads
        share SPOT_BUDGET, well inside the webhook's 2 s: one that fails or is slow places
        nothing. A count read meanwhile is never laid over admissions counted since
        (`spot_seq`). The patch is built, then counted (unless a dry run), under the lock."""
        spot = self.spot
        w = next(
            (x for x in spot.workloads or [] if (x.namespace, x.kind, x.name) == (ns, kind, name)),
            None,
        ) if spot else None  # fmt: skip
        meta = pod.get("metadata") or {}
        rs = next((o.get("name") for o in meta.get("ownerReferences") or []
                   if o.get("kind") == "ReplicaSet"), None)  # fmt: skip
        if spot is None or w is None or not rs or settled(pod, spot.label.partition("=")[0]):
            return []
        key = f"{ns}/{rs}"
        with self.spot_lock:
            c = self.spot_counts.get(key)
            stale = c is None or c["replicas"] is None or sum(c[s] for s in SIDES) >= c["replicas"]
            seq = self.spot_seq.get(key, 0)
        fresh, replicas = None, 0
        if stale:
            start = time.monotonic()
            try:
                replicas = (
                    self.apps.read_namespaced_replica_set(
                        rs, ns, _request_timeout=SPOT_BUDGET
                    ).spec.replicas
                    or 0
                )
                if c is None or sum(c[s] for s in SIDES) >= replicas:
                    left = SPOT_BUDGET - (time.monotonic() - start)
                    if left < 0.1:
                        raise TimeoutError("no time left to list its pods")
                    fresh = self.counted(meta.get("labels") or {}, ns, rs, spot, left)
            except Exception as e:  # slow or refused: the pod as it is
                log.warning("spot: %s/%s not read, its pod left as it is: %s", ns, rs, e)
                return []
        with self.spot_lock:
            c = self.spot_counts.setdefault(key, {"replicas": None, **dict.fromkeys(SIDES, 0)})
            if stale:
                c["replicas"] = replicas
            if fresh is not None and self.spot_seq.get(key, 0) == seq:
                c.update(fresh)
            target = on_demand_of(c["replicas"], w.ratio, w.on_demand)
            side = "on-demand" if c["on-demand"] < target else "spot"
            ops = steered(pod, side, spot)
            if not dry:
                c[side] += 1
                self.spot_seq[key] = self.spot_seq.get(key, 0) + 1
        return ops

    def counted(
        self, labels: dict[str, str], ns: str, rs: str, spot: Spotting, timeout: float
    ) -> dict[str, int]:
        """A ReplicaSet's pods on each side, listed at admission by its template's hash."""
        hashed = [f"{k}={labels[k]}" for k in ("pod-template-hash", ROLLOUT_HASH) if labels.get(k)]
        found = self.core.list_namespaced_pod(
            ns, label_selector=hashed[0] if hashed else None, _request_timeout=timeout
        ).items
        mine = [
            p
            for p in found
            if any(
                o.kind == "ReplicaSet" and o.name == rs for o in p.metadata.owner_references or []
            )
        ]
        return sides(mine, set(spot.nodes) if spot.nodes is not None else None)

    def recount(self) -> None:
        """Each steered Workload's ReplicaSets' pods on each side, counted again (slice
        16.2b), one list a namespace; a namespace it can't list keeps its counts, and so
        does a ReplicaSet the webhook counted a pod of while it listed (`spot_seq`): the
        next minute's count has it. Their replicas are read again at their next
        admission."""
        spot = self.spot
        if spot is None or not spot.workloads:
            with self.spot_lock:
                self.spot_counts = {}
            return
        steered_ = {(w.namespace, w.kind, w.name) for w in spot.workloads}
        nodes = set(spot.nodes) if spot.nodes is not None else None
        with self.spot_lock:
            before = dict(self.spot_seq)
        counts: dict[str, dict[str, Any]] = {}
        kept: set[str] = set()
        for ns in sorted({w.namespace for w in spot.workloads}):
            try:
                pods = self.core.list_namespaced_pod(ns).items
            except ApiException as e:
                log.warning("spot: the pods of %s not counted: %s", ns, why(e))
                kept.add(ns)
                continue
            by_rs: dict[str, list[Any]] = {}
            for p in pods:
                owners = {o.kind: o.name for o in p.metadata.owner_references or []}
                of = workload_of(owners, p.metadata.labels or {})
                if of and (ns, *of) in steered_ and owners.get("ReplicaSet"):
                    by_rs.setdefault(owners["ReplicaSet"], []).append(p)
            for rs, mine in by_rs.items():
                counts[f"{ns}/{rs}"] = {"replicas": None, **sides(mine, nodes)}
        with self.spot_lock:
            old = self.spot_counts
            moved = {k for k in old if self.spot_seq.get(k, 0) != before.get(k, 0)}
            keep = {k: v for k, v in old.items() if k.split("/")[0] in kept or k in moved}
            self.spot_counts = counts | keep

    def say_placed(self) -> None:
        """The Events `place` left to say, each on its Workload; one refused is only logged."""
        news, self.placed_news = self.placed_news, []
        for key, reason, message in news:
            ns, kind, name = key.split("/", 2)
            post(self.core, ns, ("apps/v1", kind, name), reason, message, False)

    def blocked(self, namespaces: list[str], now: float) -> None:
        """Report the Blocker facts of `namespaces` to the Hub once an hour (slice 15.1b).
        One that didn't reach it goes again in five minutes; one it refused (an older
        Hub has no such route) waits the hour: sending it again changes nothing."""
        if now < self.blockers_due:
            return
        self.blockers_due = now + BLOCKERS_EVERY
        # placing nothing under the Kill switch: no Workload to restart is named then
        label = self.placement.label if self.placement and not self.killed else None
        # slice 16.2b, the same way, and only once it knows its list: before, it steers nothing
        spotting = self.spot is not None and self.spot.workloads is not None and not self.killed
        report = blocking(self.core, self.policy, namespaces, now, label, spotting)
        try:
            r = self.http.post("/actuator/blockers", json=blockers.sent(report))
            r.raise_for_status()
        except httpx.HTTPError as e:
            code = e.response.status_code if isinstance(e, httpx.HTTPStatusError) else None
            if code and 400 <= code < 500 and code not in (401, 403, 429):
                log.error("the Hub refused its Blockers (%d)", code)
                return
            log.warning("reporting its Blockers to the Hub: %s", code or type(e).__name__)
            self.blockers_due = now + BLOCKERS_RETRY

    def send(self) -> None:
        self.report()
        if self.state() != self.stored:  # taken (or refused): not sent again after a restart
            self.save()

    def answered(self, ok: bool, now: float) -> None:
        """Count how long the Hub hasn't answered; at 15 minutes, one Event on its ConfigMap."""
        if ok:
            if self.told:
                log.warning("the Hub answers again")
            self.unanswered, self.told = None, False
            return
        if self.unanswered is None:
            self.unanswered = now
        if not self.told and now - self.unanswered >= UNREACHABLE_SECONDS:
            self.told = True
            message = (
                f"the Hub has not answered for {UNREACHABLE_SECONDS // 60} minutes: "
                "holding what it applied, changing nothing new"
            )
            log.warning(message)
            post(self.core, self.namespace, ("v1", "ConfigMap", CONFIGMAP), "HubUnreachable",
                 message, warning=True)  # fmt: skip

    def pauses(self, keys: set[str]) -> None:
        """Which workloads (`ns/kind/name`) a person paused, read again for each kind in each
        namespace it has keys in; one it can't list keeps what it knew."""
        paused: set[str] = set()
        for ns, kind in sorted({tuple(k.split("/")[:2]) for k in keys}):
            if kind not in KINDS:
                continue
            try:
                found = getattr(self.apps, f"list_namespaced_{KINDS[kind]}")(ns).items
            except ApiException as e:
                log.warning("reading %s's %ss: %s", ns, kind, why(e))
                paused |= {p for p in self.paused if p.startswith(f"{ns}/{kind}/")}
                continue
            paused |= {
                f"{ns}/{kind}/{d.metadata.name}"
                for d in found
                if (d.metadata.annotations or {}).get(PAUSE) == "true"
            }
        for p in sorted(paused ^ self.paused):
            log.warning("%s is %s", p, "paused" if p in paused else "no longer paused")
        self.paused = frozenset(paused)

    def ooms(self, watched: list[Watched]) -> list[Actuation]:
        """The OOM kills of the last 10 minutes of the containers the Hub lists (slice
        13.1a): told as container-limit or node-level and reported; and, when the Hub
        sends `oom_max` (slice 13.1b), answered: a Step-up, a Rollback, or said. One read of
        each namespace's pods. A container is looked at every 10 minutes at most, with its
        pods killed since the last time (a pod shows its last kill only), and 50
        containers a minute at most."""
        now = time.time()
        wanted = {(w.namespace, w.kind, w.name, w.container) for w in watched}
        found: dict[tuple[str, ...], list[tuple[Any, float]]] = {}
        for ns in sorted({w.namespace for w in watched}):
            try:
                pods = self.core.list_namespaced_pod(ns).items
            except ApiException as e:  # the other namespaces are still looked at
                log.warning("looking for OOM kills in %s: %s", ns, why(e))
                continue
            for p in pods:
                owners = {o.kind: o.name for o in p.metadata.owner_references or []}
                of = workload_of(owners, p.metadata.labels or {})
                if of is None:
                    continue
                for st in (p.status.container_statuses if p.status else None) or []:
                    key: tuple[str, ...] = (ns, *of, st.name)
                    at = oom_killed_at(st)
                    if key in wanted and at is not None and now - at < OOM_RECENT:
                        found.setdefault(key, []).append((p, at))
        out: list[Actuation] = []
        nodes: dict[str, Any] = {}  # each node's SystemOOM times, or why they were not read
        for key, kills in sorted(found.items()):
            if now - self.oom_said.get(key, 0.0) < OOM_RECENT:
                continue  # said within 10 minutes: these wait for its next report
            if len(out) == OOM_REPORTS:
                break  # the others next minute: never so many that a Rollback's is lost
            self.oom_said[key] = now
            a = self.answer(key, kills, self.oom_kinds(key[3], kills, nodes), now, nodes)
            log.warning("%s: %s", "/".join(key), a.reason)
            out.append(a)
        return out

    def oom_kinds(
        self, container: str, kills: list[tuple[Any, float]], nodes: dict[str, Any]
    ) -> list[tuple[str, str]]:
        """Which kind each of a container's OOM kills was, and how its report says it."""
        out = []
        for p, at in kills:
            spec = next((c for c in p.spec.containers if c.name == container), None)
            memory = limits_of(spec).get("memory")
            node = p.spec.node_name or ""
            if not memory:
                out.append(("node-level", f"{p.metadata.name} with no memory limit (node-level)"))
                continue
            if node not in nodes:
                try:
                    nodes[node] = system_ooms(self.core, node)
                except ApiException as e:  # no Role in `default`: installed before 13.1a
                    nodes[node] = why(e)
                    if not self.events_unread:
                        self.events_unread = True
                        log.warning("reading Node Events in default: %s", why(e))
            times = nodes[node]
            if isinstance(times, str):  # why, said once at the report's end
                text = (
                    f"with a memory limit of {memory} (container-limit: its node's Events not read)"
                )
                out.append(("container-limit", f"{p.metadata.name} {text}"))
            elif any(abs(t - at) <= OOM_NEAR for t in times):
                text = f"with its node {node} out of memory (node-level: a SystemOOM Event)"
                out.append(("node-level", f"{p.metadata.name} {text}"))
            else:
                text = f"with a memory limit of {memory}, its node not out of memory"
                out.append(("container-limit", f"{p.metadata.name} {text} (container-limit)"))
        return out

    def oom_report(
        self,
        key: tuple[str, ...],
        kills: list[tuple[Any, float]],
        kinds: list[tuple[str, str]],
        nodes: dict[str, Any] | None = None,
        then: str = "Nothing changed",
    ) -> Actuation:
        """One container's OOM kills as an Actuation: which kind each was, and `then`."""
        ns, kind, name, container = key
        limits = [
            limits_of(c).get("memory")
            for p, _ in kills
            for c in p.spec.containers
            if c.name == container
        ]
        limit = next((q for q in limits if q), None)
        told = [text for _, text in kinds[:OOM_SHOWN]]
        more = f"; and {len(kills) - OOM_SHOWN} more" if len(kills) > OOM_SHOWN else ""
        unread = next((w for w in (nodes or {}).values() if isinstance(w, str)), "")
        more += f". Node Events not read: {unread}" if unread else ""
        h = self.held.get("/".join(key)) or {}
        return Actuation(
            namespace=ns,
            kind=kind,
            name=name,
            container=container,
            run_id=h.get("run_id", 0),  # 0: nothing of it held
            outcome="oom_killed",
            memory=float(qty(limit)) if limit else None,
            pods=len(kills),
            reason=("OOM-killed: " + "; ".join(told) + more + f". {then}")[:1000],
            at=max(at for _, at in kills),
        )

    def answer(
        self,
        key: tuple[str, ...],
        kills: list[tuple[Any, float]],
        kinds: list[tuple[str, str]],
        now: float,
        nodes: dict[str, Any] | None = None,
    ) -> Actuation:
        """What a container's OOM kills get (slice 13.1b): a Rollback where its node was
        out of memory and the Actuator had lowered its memory request; a Step-up where a
        memory limit was reached; else, and from a Hub that sends no `oom_max`, only said."""
        seen = self.oom_report(key, kills, kinds, nodes)
        if self.oom_max is None:
            return seen
        k = "/".join(key)
        h = self.held.get(k) or {}
        holds = h.get("outcome") in ("applied", "rolling", "failed") and h.get("requests")
        limit = str(seen.memory) if seen.memory else None  # a pod's, as its template's
        if holds and lowered(h, limit) and any(kind == "node-level" for kind, _ in kinds):
            a = rollback(
                self.core, self.apps, k, h, seen.reason.removesuffix(". Nothing changed"), now
            )
            event(self.core, a, h)
            return a
        last = (h.get("rollbacks") or [0.0])[-1]
        if h.get("outcome") == "rolled_back" and seen.at <= last:
            return seen  # answered by that Rollback: no step for the same kill
        mine = [
            kill for kill, (kind, _) in zip(kills, kinds, strict=True) if kind == "container-limit"
        ]
        if not mine:
            return seen
        stepped = self.step_up(key, mine, now)
        if isinstance(stepped, str):  # why not, said with the kills
            return self.oom_report(key, kills, kinds, nodes, f"No Step-up: {stepped}")
        stepped.reason = (stepped.reason + ": " + seen.reason.removesuffix(". Nothing changed"))[
            :1000
        ]
        return stepped

    def step_up(
        self, key: tuple[str, ...], kills: list[tuple[Any, float]], now: float
    ) -> Actuation | str:
        """One Step-up of a container's memory limit and request on every pod of its
        Workload, in place (slice 13.1b, ADR 0005); or why not. A kill of a pod already at
        the container's current step earns the next one, 10 minutes after the last at the
        soonest; a pod below it is only brought to it."""
        ns, kind, name, container = key
        k = "/".join(key)
        try:
            d = read(self.apps, kind, name, ns)
            template = next(
                (c for c in template_of(d, kind).spec.containers if c.name == container), None
            )
            top = limits_of(template).get("memory") if template is not None else None
            if template is None or not top:
                return "its template sets no memory limit"
            had = {"limit": top, "request": requests_of(template).get("memory")}
            if (d.metadata.annotations or {}).get(PAUSE) == "true":
                return "the Workload is paused"
            if any(
                x.restart_policy == "RestartContainer" and x.resource_name == "memory"
                for x in template.resize_policy or []
            ):
                return "its resizePolicy restarts the container for memory"
            mode = vpa_mode(self.custom, ns, kind, name)
            if mode:
                return f"a VPA in mode {mode} sizes it"
            if "memory" in hpa_resources(self.autoscaling, ns, kind, name, container):
                return "an HPA scales on its memory"
            st = self.stepped.get(k)
            if st and st["template"] != had:  # a person changed it: theirs again
                del self.stepped[k]
                st = None
            h = self.held.get(k) or {}
            if h.get("outcome") == "rolling":
                return "its Sizing is still rolling out"
            holds = h.get("outcome") == "applied"  # its pods ask what it holds
            held = (h.get("requests") or {}).get("memory") if holds else None
            # what its pods ask now, and never less than its template did
            base = max(qty(held or "0"), qty(had["request"] or top))
            limit = qty(st["limit"] if st else top)
            request = qty(st["request"]) if st else min(base, limit)
            # a CronJob's pods are never resized in place: its next runs' start at it
            cron = kind == "CronJob"
            pods = [p for p in pods_of(self.core, self.apps, d, kind) if not lost(p)]
            many = (d.spec.job_template.spec.parallelism or 1) if cron else len(pods)

            def has(p: Any) -> Decimal | None:
                """The memory limit the pod's container runs with: what the kubelet says
                it gave it, where it says (a resize its node can't fit stays asked, never
                given, on Kubernetes 1.33 and 1.34), else what its spec asks."""
                status = container_status(p, container)
                given = (status.resources.limits if status and status.resources else None) or {}
                c = next((c for c in p.spec.containers if c.name == container), None)
                q = given.get("memory") or limits_of(c).get("memory")
                return qty(q) if q else None

            earned = any(has(p) == limit and (not st or at > st["at"]) for p, at in kills)
            target = (st["limit"], st["request"]) if st else None
            if earned:
                if st and now - st["at"] < STEP_EVERY:
                    ago = int((now - st["at"]) // 60)
                    return f"stepped up {ago} minutes ago: one a container every 10 minutes"
                target = next_step(limit, request, qty(top) * Decimal(str(self.oom_max)))
                if target is None:
                    return (
                        f"it is at {self.oom_max:g} times its template's limit "
                        f"({memory_text(float(limit))}): raise it in its manifest, or look "
                        "for a leak"
                    )
                self.steps = [t for t in self.steps if now - t < 3600]
                if len(self.steps) >= STEPS_AN_HOUR:
                    return f"the cluster's {STEPS_AN_HOUR} Step-ups an hour are used"
                new = {"memory": target[0]}
                breach = (
                    limit_range_breach(self.core, ns, new, {})
                    or quota_breach(self.core, ns, new, {"memory": str(limit)}, many, limits=True)
                    or quota_breach(
                        self.core, ns, {"memory": target[1]}, {"memory": str(request)}, many
                    )
                )
                if breach:
                    return breach
            if target is None:
                return "its pods' memory limit is not its template's"
            if not earned and cron:
                return "its next runs' pods start at its Step-up"
            left = (st or {}).get("left") or []  # pods whose node had no room for it
            if not earned and all(
                (has(p) or 0) >= qty(target[0]) or p.metadata.name in left for p in pods
            ):
                if any(p.metadata.name in left for p, _ in kills):
                    return (
                        f"{len(left)} of its pods keep their memory, their node without room "
                        "for its Step-up; the others have it"
                    )
                return "these kills were before its last Step-up, which every pod has"
        except ApiException as e:
            return f"reading it: {why(e)}"
        body = step_body(container, *target)
        was = memory_text(float(limit)), memory_text(float(request))
        if earned:  # kept before a pod is touched: a restart then brings its pods to it
            self.steps.append(now)
            self.stepped[k] = {
                "limit": target[0],
                "request": target[1],
                "template": had,
                "at": now,
                "steps": (st or {}).get("steps", 0) + 1,
                **({"left": left} if left else {}),
            }
            self.save()
        done, refused = 0, []
        for p in pods:
            got = has(p)
            if got is None or got >= qty(target[0]) or p.metadata.name in left:
                continue  # only ever raised: one above it (a step dropped since) keeps its own
            try:  # one pod's refusal (its node has no room) leaves the others their step
                self.core.patch_namespaced_pod_resize(p.metadata.name, ns, body)
                done += 1
            except ApiException as e:
                refused.append(f"{p.metadata.name}: {why(e)}")
        if earned and not done and not cron:  # a step no pod took is none: not kept, not counted
            self.steps.pop()
            if st:
                self.stepped[k] = st
            else:
                del self.stepped[k]
            return "every pod refused it: " + "; ".join(refused[:3])
        if earned and holds and held:  # its memory is the Step-up's now, no Sizing's
            stepped_over(h)
        said = (
            f"{STEP_SAID}{was[0]} to {target[0]}, request {was[1]} to {target[1]}"
            if earned
            else f"brought to its Step-up, memory limit {target[0]}, request {target[1]}"
        )
        said += (
            " for its next runs' pods, given them as they start"
            if cron
            else f" on {done} of {len(pods)} pods in place"
        )
        said += f"; {len(refused)} refused: {'; '.join(refused[:3])}" if refused else ""
        a = Actuation(
            namespace=ns,
            kind=kind,
            name=name,
            container=container,
            run_id=h.get("run_id", 0),
            outcome="stepped_up",
            memory=float(qty(target[0])),
            pods=done,
            reason=said[:1000],
            at=now,
        )
        post(
            self.core, ns, ("apps/v1", kind, name), "MemorySteppedUp", f"{container}: {said}", False
        )
        return a

    def keep_steps(self) -> list[Actuation]:
        """Each container's Step-up, each minute (slice 13.1b): handed back when a person
        changed the memory limit or request in its Workload's template, or the Workload is
        gone; else its pods that ask less are brought to it in place (one started from its
        template with the webhook down, a DaemonSet's new pod), and said when any was. Not
        a paused Workload's, nor a CronJob's pods."""
        out = []
        for k, st in sorted(self.stepped.items()):
            ns, kind, name, container = k.split("/")
            if self.is_paused(k):
                continue
            reason: str | None = None
            try:
                d = read(self.apps, kind, name, ns)
            except ApiException as e:
                if e.status != 404:
                    log.warning("reading %s: %s", k, why(e))  # the API server's trouble
                    continue
                reason = f"no such {kind}"
            else:
                c = next(
                    (c for c in template_of(d, kind).spec.containers if c.name == container), None
                )
                had = {"limit": limits_of(c).get("memory"), "request": requests_of(c).get("memory")}
                was_ = st["template"]
                if c is None:
                    reason = f"no container {container!r} in its template"
                elif had != was_:
                    reason = "its template's memory changed: " + ", ".join(
                        f"{x} {was_[x] or 'unset'} to {had[x] or 'unset'}"
                        for x in ("limit", "request")
                        if had[x] != was_[x]
                    )
            if reason:
                del self.stepped[k]
                said = f"{reason}: its Step-up dropped"
                a = Actuation(
                    namespace=ns, kind=kind, name=name, container=container,
                    run_id=(self.held.get(k) or {}).get("run_id", 0),
                    outcome="step_ended", pods=0, reason=said, at=time.time(),
                )  # fmt: skip
                log.info("handed back %s: %s", k, said)
                post(
                    self.core,
                    ns,
                    ("apps/v1", kind, name),
                    "HandedBack",
                    f"{container}: {said}",
                    False,
                )
                out.append(a)
                continue
            now = time.time()
            if self.oom_expires:
                # kept with it, as a Burst's: with the Reflex switched off the Hub sends no
                # term, and a Step-up still ends at the one it had
                st["expires"] = self.oom_expires
            due = st.get("expires")
            if due and now - max(st["at"], st.get("renewed", 0.0)) >= due:
                peak, top = self.peaks.get(k), st["template"]["limit"]
                if k in self.peaks and (peak is None or peak > float(qty(top))):
                    st["renewed"] = now  # still needed, or not known: another term
                    how = f"{memory_text(peak)} above" if peak is not None else "not known against"
                    said = (
                        f"kept for another term: its memory peak is {how} its template's "
                        f"limit {top}"
                    )
                    log.info("%s: its Step-up %s", k, said)
                    out.append(  # said, so the Hub's page still knows it after 90 days
                        Actuation(
                            namespace=ns,
                            kind=kind,
                            name=name,
                            container=container,
                            run_id=(self.held.get(k) or {}).get("run_id", 0),
                            outcome="stepped_up",
                            memory=float(qty(st["limit"])),
                            pods=0,
                            reason=said,
                            at=now,
                        )
                    )
                else:
                    del self.stepped[k]
                    term = f"its term of {span(due)} over"
                    said = (
                        f"{term}, its memory peak {memory_text(peak)} within its template's "
                        f"limit {top}: {self.step_back(k, st)}"
                        if peak is not None
                        else f"{term}, the Hub no longer listing it: {self.step_back(k, st)}"
                    )
                    a = Actuation(
                        namespace=ns, kind=kind, name=name, container=container,
                        run_id=(self.held.get(k) or {}).get("run_id", 0),
                        outcome="step_ended", pods=0, reason=said[:1000], at=now,
                    )  # fmt: skip
                    log.info("%s: its Step-up %s", k, said)
                    what = ("apps/v1", kind, name)
                    post(self.core, ns, what, "StepUpExpired", f"{container}: {said}", False)
                    out.append(a)
                    continue
            try:  # a CronJob has none to bring: its next runs' start at it
                pods = [p for p in pods_of(self.core, self.apps, d, kind) if not lost(p)]
            except ApiException as e:
                log.warning("reading %s's pods: %s", k, why(e))
                continue
            live = {p.metadata.name for p in pods}
            left = [n for n in st.get("left") or [] if n in live]  # no room on their node
            done, was = 0, len(left)
            for p in pods:
                spec = next((c for c in p.spec.containers if c.name == container), None)
                asks = limits_of(spec).get("memory")
                if not asks or p.metadata.name in left:
                    continue
                try:
                    if qty(asks) < qty(st["limit"]):  # only ever raised
                        body = step_body(container, st["limit"], st["request"])
                        self.core.patch_namespaced_pod_resize(p.metadata.name, ns, body)
                        done += 1
                    elif any(
                        c.type == "PodResizePending" and c.status == "True"
                        for c in (p.status.conditions if p.status else None) or []
                    ):
                        # its node has no room (Kubernetes 1.33 and 1.34 keep the resize
                        # asked): back to what it was given and left so, or a Sizing's
                        # rollout would read it as its own Infeasible pod, and evict it
                        status = container_status(p, container)
                        given = status.resources if status and status.resources else None
                        got = (given.limits if given else None) or {}
                        had_ = (given.requests if given else None) or {}
                        back = step_body(
                            container,
                            got.get("memory") or was_["limit"],
                            had_.get("memory") or was_["request"] or was_["limit"],
                        )
                        self.core.patch_namespaced_pod_resize(p.metadata.name, ns, back)
                        left.append(p.metadata.name)
                except ApiException as e:
                    log.warning("bringing %s to %s's Step-up: %s", p.metadata.name, k, why(e))
            if left or "left" in st:
                st["left"] = left
            if not left:
                st.pop("left", None)
            if done or len(left) > was:
                said = (
                    f"brought to its Step-up, memory limit {st['limit']}, request "
                    f"{st['request']} on {done} of {len(pods)} pods in place"
                )
                if len(left) > was:
                    said += f"; {len(left) - was} left as they were: their node has no room"
                out.append(
                    Actuation(
                        namespace=ns,
                        kind=kind,
                        name=name,
                        container=container,
                        run_id=(self.held.get(k) or {}).get("run_id", 0),
                        outcome="stepped_up",
                        memory=float(qty(st["limit"])),
                        pods=done,
                        reason=said,
                        at=time.time(),
                    )
                )
                log.info("%s: %s", k, said)
        return out

    def burst_all(self, listed: list[Throttled], run: int | None, now: float) -> list[Actuation]:
        """The Hub's read `run`, once for each container it says is throttled enough (slice
        13.3b): a Burst, or why not, said as `throttled` (a standing reason once a day). At
        most 50 containers a minute, as OOM kills; the others the next. One whose Sizing
        still rolls out is tried again each minute of that read."""
        if run is None:
            return []
        keys = {"/".join((t.namespace, t.kind, t.name, t.container)): t for t in listed}
        # forgotten by read, never by listing: a container a Revert unlists and an approval
        # lists again within one read is not burst a second time from it
        self.burst_seen = {k: r for k, r in self.burst_seen.items() if r == run}
        out: list[Actuation] = []
        handled = 0
        for k, t in sorted(keys.items()):
            if t.throttled is None or self.burst_seen.get(k) == run:
                continue
            if handled == OOM_REPORTS:
                break
            handled += 1
            self.burst_seen[k] = run  # handled, whatever comes of it: never twice a read
            share = f"throttled in {t.throttled:.0%} of its CPU periods over the last hour"
            got = self.burst(k, share, now)
            if got == ROLLING:
                del self.burst_seen[k]
            if isinstance(got, str):
                said = self.burst_said.get(k)
                if said and said[0] == got and now - said[1] < 86400:
                    continue
                self.burst_said[k] = (got, now)
                got = Actuation(
                    namespace=t.namespace, kind=t.kind, name=t.name, container=t.container,
                    run_id=(self.held.get(k) or {}).get("run_id", 0),
                    outcome="throttled", pods=0, reason=f"{share}. No Burst: {got}"[:1000], at=now,
                )  # fmt: skip
            log.warning("%s: %s", k, got.reason)
            out.append(got)
        return out

    def burst(self, k: str, share: str, now: float) -> Actuation | str:
        """One Burst of a container's CPU limit, a quarter of its template's more, on every
        pod of its Workload in place (slice 13.3b, ADR 0005); or why not. Requests are
        untouched. Never past `burst_max` times its template's limit; a container's at
        least 45 minutes apart."""
        ns, kind, name, container = k.split("/")
        if kind == "CronJob" or kind not in KINDS:
            return f"a {kind}'s pods get none"
        try:
            d = read(self.apps, kind, name, ns)
            containers = template_of(d, kind).spec.containers
            template = next((c for c in containers if c.name == container), None)
            top = limits_of(template).get("cpu") if template is not None else None
            if template is None or not top:
                return "its template sets no CPU limit"
            if (d.metadata.annotations or {}).get(PAUSE) == "true":
                return "the Workload is paused"
            if qos([(requests_of(c), limits_of(c)) for c in containers]) == "Guaranteed":
                return "it is Guaranteed: its CPU limit can't leave its request"
            if any(
                x.restart_policy == "RestartContainer" and x.resource_name == "cpu"
                for x in template.resize_policy or []
            ):
                return "its resizePolicy restarts the container for cpu"
            mode = vpa_mode(self.custom, ns, kind, name)
            if mode:
                return f"a VPA in mode {mode} sizes it"
            if (self.held.get(k) or {}).get("outcome") == "rolling":
                return ROLLING
            st = self.bursts.get(k)
            if st and not same(st["template"], top):
                return "its template's CPU limit changed"  # ended this minute, by keep_bursts
            if st and now - st["at"] < BURST_EVERY:
                ago = int((now - st["at"]) // 60)
                return f"burst {ago} minutes ago: one a container every {BURST_EVERY // 60} minutes"
            limit = qty(st["limit"] if st else top)
            cap = qty(top) * Decimal(str(self.burst_max))
            # whole millicores, never past the cap
            raised = math.floor(min(limit + qty(top) * BURST_STEP, cap) * 1000)
            new = f"{raised}m"
            if qty(new) <= limit:
                return (
                    f"it is at {self.burst_max:g} times its template's limit "
                    f"({cpu_text(float(limit))}): raise it in its manifest"
                )
            self.steps = [t for t in self.steps if now - t < 3600]
            if len(self.steps) >= STEPS_AN_HOUR:
                return f"the cluster's {STEPS_AN_HOUR} Step-ups and Bursts an hour are used"
            if sum(now - b["at"] < 3600 for b in self.bursts.values()) >= BURSTS_AN_HOUR:
                return f"the cluster's {BURSTS_AN_HOUR} Bursts an hour are used"
            asks = requests_of(template).get("cpu") or top
            breach = (
                limit_range_breach(self.core, ns, {"cpu": new}, {})
                or limit_range_breach(self.core, ns, {"cpu": asks}, {"cpu": new})
                or counts_cpu_limits(self.core, ns)
            )
            if breach:
                return breach
            pods = [p for p in pods_of(self.core, self.apps, d, kind) if not lost(p)]
            if not pods:
                return "no live pods"
        except ApiException as e:
            return f"reading it: {why(e)}"
        # kept before a pod is touched: a restart then brings its pods to it
        self.steps.append(now)
        self.bursts[k] = {
            "limit": new,
            "template": top,
            "at": now,
            "steps": (st or {}).get("steps", 0) + 1,
            "expires": self.burst_expires or (st or {}).get("expires"),
        }
        self.save()
        done, asked, refused = limit_cpu(self.core, ns, container, pods, new, up=True)
        if asked and not done:  # a Burst no pod took is none: not kept, not counted
            self.steps.pop()
            if st:
                self.bursts[k] = st
            else:
                del self.bursts[k]
            return "every pod refused it: " + "; ".join(refused[:3])
        said = (
            f"{BURST_SAID}{cpu_text(float(limit))} to {new} on {done} of {len(pods)} pods in place"
        )
        said += f"; {len(refused)} refused: {'; '.join(refused[:3])}" if refused else ""
        said += f": {share}"
        post(self.core, ns, ("apps/v1", kind, name), "CpuBurst", f"{container}: {said}", False)
        return Actuation(
            namespace=ns, kind=kind, name=name, container=container,
            run_id=(self.held.get(k) or {}).get("run_id", 0),
            outcome="burst", cpu=float(qty(new)), pods=done, reason=said[:1000], at=now,
        )  # fmt: skip

    def keep_bursts(self) -> list[Actuation]:
        """Each container's Burst, each minute the Hub answers (slice 13.3b): dropped when a
        person changed the CPU limit in its Workload's template, or the Workload is gone;
        at its term kept for another while the Hub's CPU peak is above 90% of that limit
        (or not known), else ended, its pods back to it in place; and its pods that ask
        less brought to it (one started from its template with the webhook down). Not a
        paused Workload's."""
        out = []
        for k, st in sorted(self.bursts.items()):
            ns, kind, name, container = k.split("/")
            if self.is_paused(k):
                continue

            def told(outcome: Outcome, pods: int, reason: str, st: Any = st, k: str = k) -> None:
                ns, kind, name, container = k.split("/")
                out.append(
                    Actuation(
                        namespace=ns,
                        kind=kind,
                        name=name,
                        container=container,
                        run_id=(self.held.get(k) or {}).get("run_id", 0),
                        outcome=outcome,
                        cpu=float(qty(st["limit"])) if outcome == "burst" else None,
                        pods=pods,
                        reason=reason[:1000],
                        at=time.time(),
                    )
                )
                log.info("%s: its Burst %s", k, reason)

            what = ("apps/v1", kind, name)
            top = st["template"]
            gone: str | None = None
            try:
                d = read(self.apps, kind, name, ns)
            except ApiException as e:
                if e.status != 404:
                    log.warning("reading %s: %s", k, why(e))  # the API server's trouble
                    continue
                gone = f"no such {kind}"
            else:
                c = next(
                    (c for c in template_of(d, kind).spec.containers if c.name == container), None
                )
                has = limits_of(c).get("cpu")
                if c is None:
                    gone = f"no container {container!r} in its template"
                elif not same(has, top):
                    gone = f"its template's CPU limit changed: {top} to {has or 'unset'}"
            if gone:
                del self.bursts[k]
                told("burst_ended", 0, f"{gone}: its Burst dropped")
                post(self.core, ns, what, "HandedBack", f"{container}: {out[-1].reason}", False)
                continue
            now = time.time()
            if self.burst_expires:
                st["expires"] = self.burst_expires  # the Contract's, as the Hub last said it
            term = st.get("expires")
            if term and now - max(st["at"], st.get("renewed", 0.0)) >= term:
                peak = self.burst_peaks.get(k)
                bar = float(qty(top) * BURST_KEEP)
                if k in self.burst_peaks and (peak is None or peak > bar):
                    st["renewed"] = now  # still needed, or not known: another term
                    how = "not known" if peak is None else f"{cpu_text(peak)}, above"
                    told(  # said, so the Hub's page still knows it after 90 days
                        "burst",
                        0,
                        f"kept for another term: its CPU peak is {how} 90% of its "
                        f"template's limit {top}",
                    )
                else:
                    del self.bursts[k]
                    why_ = (
                        f"its CPU peak {cpu_text(peak)} within 90% of its template's limit {top}"
                        if peak is not None
                        else "the Hub no longer listing it"
                    )
                    back = self.burst_back(k, st)
                    told("burst_ended", 0, f"its term of {span(term)} over, {why_}: {back}")
                    post(
                        self.core, ns, what, "BurstExpired", f"{container}: {out[-1].reason}", False
                    )
                    continue
            try:
                pods = [p for p in pods_of(self.core, self.apps, d, kind) if not lost(p)]
            except ApiException as e:
                log.warning("reading %s's pods: %s", k, why(e))
                continue
            done, _, refused = limit_cpu(self.core, ns, container, pods, st["limit"], up=True)
            for r in refused:
                log.warning("bringing a pod to %s's Burst: %s", k, r)
            if done:
                told(
                    "burst",
                    done,
                    f"brought to its Burst, CPU limit {st['limit']} on {done} of {len(pods)} "
                    "pods in place",
                )
        return out

    def burst_back(self, k: str, st: dict[str, Any]) -> str:
        """What becomes of the running pods of a Burst that ended: every one above its
        template's CPU limit back to it in place (lowering a CPU limit needs no room and
        kills nothing), a pod still boosted on top of the Burst included."""
        ns, kind, name, container = k.split("/")
        try:
            d = read(self.apps, kind, name, ns)
            pods = [p for p in pods_of(self.core, self.apps, d, kind) if not lost(p)]
        except ApiException as e:
            return f"its pods keep their CPU limit until they start again (reading it: {why(e)})"
        done, asked, refused = limit_cpu(self.core, ns, container, pods, st["template"], up=False)
        said = f"{done} of {asked} pods back to CPU limit {st['template']} in place"
        return said + (f"; {len(refused)} refused: {'; '.join(refused[:3])}" if refused else "")

    def keep_boosts(self) -> None:
        """The Startup boost's minute (slice 13.4), the Hub reachable or not: which
        containers the webhook boosts now (`boosting`: not under a VPA, nor in a namespace
        that `boost_barred`; one it can't read gets none), and each boosted pod's limit
        given back when due. A namespace is read one minute more after its last boost was
        wanted, then until no pod of it still carries one."""
        now = time.time()
        wanted: dict[str, list[str]] = {}
        for key in self.boosts:
            wanted.setdefault(key.split("/")[0], []).append(key)
        was = {key.split("/")[0] for key in self.boosting}
        allowed: dict[str, float] = {}
        bars: dict[str, str] = {}
        unended: set[str] = set()
        for ns in sorted({*wanted, *self.boost_ns}):
            try:
                if ns in wanted:
                    bars[ns] = boost_barred(self.core, ns) or ""
                    found = [] if bars[ns] else vpas(self.custom, ns)
                    for key in [] if bars[ns] else wanted[ns]:
                        _, kind, name, _ = key.split("/")
                        mode = vpa_mode(self.custom, ns, kind, name, found)
                        if mode:
                            bars[f"{ns}/{kind}/{name}"] = f"a VPA in mode {mode} sizes it"
                        else:
                            allowed[key] = self.boosts[key]
                pods = self.core.list_namespaced_pod(ns).items
            except ApiException as e:  # the other namespaces still go on
                bars[ns] = f"reading it: {why(e)}"
                allowed = {k: f for k, f in allowed.items() if k.split("/")[0] != ns}
                unended.add(ns)
                continue
            if [p for p in pods if self.end_boost(p, now)]:
                unended.add(ns)
        bars = {where: reason for where, reason in bars.items() if reason}
        for where, reason in sorted(bars.items()):
            if self.boost_bars.get(where) != reason:
                log.warning("no startup boost in %s: %s", where, reason)
        self.boost_bars, self.boosting = bars, allowed
        self.boost_ns = {*wanted, *unended, *(self.boost_ns & was)}

    def end_boost(self, p: Any, now: float) -> bool:
        """Give a boosted pod's containers their own CPU limit back in place, once the pod
        is Ready, the container has run for `BOOST_SECONDS` (its own start: not its image's
        pull nor its init containers) or it restarted (a restart gets no boost); whether a
        boost is still on the pod afterwards. Its annotation is read as anyone's word: only
        ever a limit lowered, never under the container's request. Not on a paused
        Workload, as any resize."""
        try:
            own = json.loads((p.metadata.annotations or {}).get(BOOST) or "{}")
        except ValueError:
            return False
        if not isinstance(own, dict) or lost(p) or p.metadata.deletion_timestamp:
            return False
        if (p.status.phase if p.status else None) in ("Succeeded", "Failed"):
            return False
        ns, ready = p.metadata.namespace, is_ready(p)
        owners = {o.kind: o.name for o in p.metadata.owner_references or []}
        of = workload_of(owners, p.metadata.labels or {})
        left = False
        for c in p.spec.containers:
            back, has = own.get(c.name), limits_of(c).get("cpu")
            try:
                floor = qty(requests_of(c).get("cpu") or "0")
                if not isinstance(back, str) or not has or not floor <= qty(back) < qty(has):
                    continue  # given back already, or not a boost
            except (ValueError, ArithmeticError):
                continue
            st = container_status(p, c.name)
            run = st.state.running if st and st.state else None
            long = bool(run and now - run.started_at.timestamp() >= BOOST_SECONDS)
            again = bool(st and st.restart_count)
            if not (ready or long or again) or (of and "/".join((ns, *of)) in self.paused):
                left = True
                continue
            body = {
                "spec": {"containers": [{"name": c.name, "resources": {"limits": {"cpu": back}}}]}
            }
            try:
                self.core.patch_namespaced_pod_resize(p.metadata.name, ns, body)
            except ApiException as e:  # tried again next minute
                log.warning("ending %s/%s's startup boost: %s", ns, p.metadata.name, why(e))
                left = True
                continue
            when = (
                "its pod is Ready"
                if ready
                else "it restarted"
                if again
                else f"{BOOST_SECONDS // 60} minutes after its start"
            )
            message = f"{c.name}: startup boost over, CPU limit {has} back to {back}: {when}"
            what = ("v1", "Pod", p.metadata.name)
            post(self.core, ns, what, "StartupBoostEnded", message, False, p.metadata.uid)
        return left

    def step_back(self, k: str, st: dict[str, Any]) -> str:
        """What becomes of the running pods of a Step-up that ended (slice 13.1c): back to
        their template's memory limit and request in place, where the Hub's peak says they
        fit under it; else left with theirs until they start again, so that no resize is
        left hanging above what a pod uses (Kubernetes 1.34 on) and no pod is killed for
        it. Kubernetes 1.33 refuses to lower a memory limit in place: said, pod by pod."""
        ns, kind, name, container = k.split("/")
        peak, had = self.peaks.get(k), st["template"]
        if kind == "CronJob":
            return "its next runs' pods start from its template"
        if peak is None or peak > float(qty(had["limit"])):
            return (
                "its pods keep their memory until they start again, its memory peak not "
                "known to be within its template's limit"
            )
        body = step_body(container, had["limit"], had["request"] or had["limit"])
        done, refused, high = 0, [], 0
        try:
            d = read(self.apps, kind, name, ns)
            for p in pods_of(self.core, self.apps, d, kind):
                spec = next((c for c in p.spec.containers if c.name == container), None)
                asks = limits_of(spec).get("memory")
                if lost(p) or not asks or qty(asks) <= qty(had["limit"]):
                    continue
                high += 1
                try:
                    self.core.patch_namespaced_pod_resize(p.metadata.name, ns, body)
                    done += 1
                except ApiException as e:
                    refused.append(f"{p.metadata.name}: {why(e)}")
        except ApiException as e:
            return f"its pods keep their memory until they start again (reading it: {why(e)})"
        said = f"{done} of {high} pods back to memory limit {body_text(had)} in place"
        return said + (f"; {len(refused)} refused: {'; '.join(refused[:3])}" if refused else "")

    def is_paused(self, key: str) -> bool:
        return "/".join(key.split("/")[:3]) in self.paused

    def watch(self) -> list[Actuation]:
        """Roll back each Sizing that made its pods unhealthy within its hour (slice 12.4)."""
        out, now = [], time.time()
        for key, h in self.held.items():
            if "first_run" in h:  # a CronJob's (slice 12.8c)
                reason = first_run(self.core, self.apps, key, h, now, key in self.reflexed)
                if reason:
                    a = rollback(self.core, self.apps, key, h, reason, now)
                    log.warning("rolled back %s: %s", key, a.reason)
                    event(self.core, a, h)
                    out.append(a)
                continue
            if "pods" not in h:
                continue
            if h.get("outcome") != "rolling" and now >= h.get("watch_until", 0):
                for k in WATCHED:  # its hour is over: kept, no longer watched
                    h.pop(k, None)
                continue
            reason = trouble(self.core, self.apps, key, h, now, key in self.reflexed)
            if reason:
                a = rollback(self.core, self.apps, key, h, reason, now)
                log.warning("rolled back %s: %s", key, a.reason)
                event(self.core, a, h)
                out.append(a)
        return out

    def revert(self, undos: list[Undo]) -> list[Actuation]:
        """Each Revert newer than the last it did of that container (slice 12.6a): its pods
        back to their template's requests, held no more; reported even with nothing held,
        so the Hub knows it is done. A paused Deployment's waits."""
        out = []
        for u in undos:
            key = "/".join((u.namespace, u.kind, u.name, u.container))
            h = self.held.setdefault(key, {})
            if h.get("revert", -1) >= u.id or self.is_paused(key):
                continue
            h["revert"] = u.id
            why_ = f"reverted by the Operator ({u.id})"
            st = self.stepped.pop(key, None)
            if st:  # its memory is a Sizing's again (slice 13.1b); its pods' as 13.1c has it
                why_ += f"; its Step-up dropped: {self.step_back(key, st)}"
            burst = self.bursts.pop(key, None)
            if burst:  # slice 13.3b
                why_ += f"; its Burst dropped: {self.burst_back(key, burst)}"
            if h.get("outcome") in ("applied", "rolling", "failed") and h.get("requests"):
                a = put_back(self.core, self.apps, key, h, "reverted", why_, time.time())
            else:
                a = Actuation(
                    **u.model_dump(exclude={"id"}),
                    run_id=h.get("run_id", 0),
                    outcome="reverted",
                    pods=0,
                    reason=f"{why_}: nothing held",
                    at=time.time(),
                )
            log.info("reverted %s: %s", key, a.reason)
            event(self.core, a, h)
            out.append(a)
        return out

    def advance(self, eviction: str = "off") -> list[Actuation]:
        """Each rollout's minute. An eviction only when the window is open, no other evicted
        pod awaits its replacement, and the last eviction is 5 minutes old (slice 12.7)."""
        out = []
        for key, h in self.held.items():
            if h.get("outcome") == "rolling" and not self.is_paused(key):
                now, may = time.time(), eviction
                last = max((x.get("evicted_at", 0.0) for x in self.held.values()), default=0.0)
                busy = any("evicted" in x for x in self.held.values())
                if may == "open" and (busy or now - last < EVICT_EVERY):
                    may = "closed"
                a = advance(self.core, self.apps, key, h, now, may, self.policy, self.node_types)
                if a:
                    log.info("%s %s: %s", a.outcome, key, a.reason)
                    event(self.core, a, h)
                    out.append(a)
        return out

    def catch_up(self) -> list[Actuation]:
        """Pods that started from their template while it held a Sizing (the Kill switch on,
        or the Actuator down: its webhook fails open) get its requests again: the pre-checks
        again, then a canary and one pod at a time, as a new approval would. The pods it
        is still watching stay watched."""
        out = []
        for key, h in self.held.items():
            if h.get("outcome") != "applied" or self.is_paused(key):
                continue
            ns, kind, name, container = key.split("/")
            try:
                pods = pods_of(self.core, self.apps, read(self.apps, kind, name, ns), kind)
            except ApiException as e:
                log.warning("checking %s: %s", key, why(e))
                continue
            left = h.get("left") or []  # a DaemonSet's pods with no room on their node
            behind = [
                p.metadata.name
                for p in pods
                if needs(p, container, wants(h, p, self.node_types))
                and p.metadata.name not in left
                and not lost(p)  # its node gone (slice 12.10): caught up if it comes back
            ]
            if not behind:
                continue
            values = set_by(h)
            w = Wanted(
                run_id=h["run_id"], namespace=ns, kind=kind, name=name, container=container,
                cpu=values.get("cpu"), memory=values.get("memory"),
                types={
                    t: {r: float(qty(v)) for r, v in q.items()}
                    for t, q in (h.get("types") or {}).items()
                },
            )  # fmt: skip
            a, new = apply(
                self.core, self.apps, w, time.time(), self.autoscaling, self.custom,
                eviction=self.eviction, left=left, stepped=key in self.stepped,
                nodes=self.node_types,
            )  # fmt: skip
            if new.pop("passing", False):
                log.warning("%s waits to catch up: %s", key, a.reason if a else "")
                continue  # still held: tried again next minute
            carried = (
                *("rollbacks", "backoff_until", "watch_until", "lowered_at", "automated"),
                *("revert", "evicted_at", "left"),
            )
            kept = {k: h[k] for k in carried if k in h}
            watched = (new.get("pods") or {}) | (h.get("pods") or {})  # still watched: as it was
            if watched:
                new["pods"] = watched
            self.held[key] = kept | new
            if a is None:
                log.info("catching up %s: %s from its template", key, ", ".join(behind))
                continue
            log.info("%s %s: %s", a.outcome, key, a.reason)
            event(self.core, a, h)
            out.append(a)
        return out

    def hand_back(self) -> list[Actuation]:
        """Each container it holds whose workload's template now asks other requests than
        when it applied (a person's change), or that is gone: its own again, never held."""
        out = []
        for key, h in self.held.items():
            if h.get("outcome") in ("applied", "rolling"):
                reason = self.changed(key, h)
                if reason is None:
                    continue
                ns, kind, name, container = key.split("/")
                for k in ROLLOUT + WATCHED:
                    h.pop(k, None)
                h["outcome"] = "handed_back"
                a = Actuation(
                    namespace=ns,
                    kind=kind,
                    name=name,
                    container=container,
                    run_id=h["run_id"],
                    outcome="handed_back",
                    pods=0,
                    reason=reason,
                    at=time.time(),
                )
                log.info("handed back %s: %s", key, reason)
                event(self.core, a, h)
                out.append(a)
        return out

    def changed(self, key: str, h: dict[str, Any]) -> str | None:
        """Why the container held as `h` is no longer the Actuator's, or None."""
        had = h.get("template")
        if had is None:
            return "held without its template's requests (before slice 12.2)"
        ns, kind, name, container = key.split("/")
        try:
            d = read(self.apps, kind, name, ns)
        except ApiException as e:
            if e.status == 404:
                return f"no such {kind}"
            log.warning("reading %s: %s", key, why(e))  # the API server's trouble, no change
            return None
        c = next((c for c in template_of(d, kind).spec.containers if c.name == container), None)
        if c is None:
            return f"no container {container!r} in its template"
        now = (c.resources.requests if c.resources else None) or {}
        moved = [r for r in h.get("requests") or {} if not same(now.get(r), had.get(r))]
        if not moved:
            return None
        return "its template's requests changed: " + ", ".join(
            f"{r} {had.get(r) or 'unset'} to {now.get(r) or 'unset'}" for r in moved
        )

    def act(self, sizings: list[Wanted], automated: list[Wanted] | None = None) -> list[Actuation]:
        """Each Approved Sizing newer than what it applied to that container: applied once,
        whatever its outcome (a refused one waits for the next approval). Then each
        Automation Sizing newer than the last it saw (slice 12.5): under the churn rule, never
        while the container rolls or backs off, and a skip said once while its reason holds."""
        out = []
        for w, auto in [*((w, False) for w in sizings), *((w, True) for w in automated or [])]:
            key = "/".join((w.namespace, w.kind, w.name, w.container))
            if self.is_paused(key) or key in self.reverting:
                continue  # waits: for the pause to lift; for a Revert still listed (12.9b)
            old = self.held.get(key, {})
            now = time.time()
            if "evicted" in old:
                continue  # its evicted pod's replacement first: 10 minutes at most
            if auto:
                if max(old.get("run_id", -1), old.get("seen", -1)) >= w.run_id:
                    continue
                rolling = old.get("outcome") == "rolling" and not old.get("waiting")
                if rolling or now < old.get("backoff_until", 0):
                    continue  # waits, unsaid: a new Sizing comes every hour
            elif old.get("refused") == w.run_id:
                continue  # said once: what it holds stays (slice 12.9a)
            elif old.get("run_id", -1) > w.run_id or (
                old.get("run_id") == w.run_id
                and not old.get("automated")
                and not (w.again and old.get("outcome") == "reverted")
            ):
                continue
            elif old.get("run_id") == w.run_id and old.get("outcome") in ("applied", "rolling"):
                # the run Automation holds, now approved
                if not old.get("deferred"):  # the same requests: approved as they are held
                    old.pop("automated")
                    continue
                if old.get("outcome") == "rolling":
                    continue  # what the churn rule left out goes once this rollout ends
            carried = ("rollbacks", "backoff_until", "lowered_at", "revert", "evicted_at")
            carry: dict[str, Any] = {k: old[k] for k in carried if k in old}
            a: Actuation | None
            if now < old.get("backoff_until", 0):
                until = datetime.fromtimestamp(old["backoff_until"], UTC)
                a = Actuation(
                    **w.model_dump(exclude={"cpu", "memory", "again", "types"}),
                    outcome="skipped",
                    pods=0,
                    reason=f"rolled back {len(old.get('rollbacks') or [])} times in 7 days: "
                    f"no new Sizing of it until {until:%Y-%m-%d %H:%M} UTC",
                    at=now,
                )
                self.held[key] = {
                    "run_id": w.run_id,
                    "outcome": "skipped",
                    "requests": {},
                    "template": {},
                    "at": now,
                    **carry,
                    **undo(old),
                }
            else:
                args = (self.core, self.apps, w, now, self.autoscaling, self.custom, old, auto)
                a, new = apply(
                    *args,
                    eviction=self.eviction,
                    stepped=key in self.stepped,
                    nodes=self.node_types,
                )
                if new.pop("passing", False):  # tried again each minute, 10 at most
                    t = old.get("trying") or {}
                    if t.get("run") != w.run_id:
                        t = {"run": w.run_id, "since": now}
                    if now - t["since"] < WAIT_SECONDS:
                        log.warning("%s waits: %s", key, a.reason if a else "")
                        self.held[key] = {**old, "trying": t}
                        continue
                old.pop("trying", None)
                if not new:  # nothing moves enough: the churn rule
                    self.held[key] = {**old, "seen": w.run_id}
                    continue
                if auto and a is not None and a.outcome in ("skipped", "failed"):
                    # what it holds stays held; the same reason again goes unsaid
                    kept = old if old.get("outcome") == "applied" else carry | new | undo(old)
                    said = gist(old.get("unapplied") or "") == gist(a.reason)
                    self.held[key] = {**kept, "seen": w.run_id, "unapplied": a.reason}
                    if said:
                        continue
                elif (
                    a is not None
                    and a.outcome in ("skipped", "failed")
                    and old.get("outcome") in ("applied", "rolling", "failed")
                ):  # a refused approval: the one before stays held, and reverted by a Revert
                    self.held[key] = {**old, "refused": w.run_id}
                    a.reason = f"{a.reason}; run {old['run_id']}'s requests stay held"[:1000]
                else:
                    refused = a is not None and a.outcome in ("skipped", "failed")
                    self.held[key] = carry | new | ({"automated": True} if auto else {})
                    self.held[key] |= undo(old) if refused else {}
            if a is None:
                log.info("rolling %s: canary %s", key, self.held[key]["pod"])
                continue
            log.info("%s %s: %s", a.outcome, key, a.reason)
            event(self.core, a, {"automated": auto})
            out.append(a)
        return out


def main(s: ActuatorSettings) -> None:  # pragma: no cover - the cluster's own process
    config.load_incluster_config()  # type: ignore[no-untyped-call]
    single()
    core, custom = Calm(client.CoreV1Api()), Calm(client.CustomObjectsApi())
    apps = Apps(Calm(client.AppsV1Api()), custom, Calm(client.BatchV1Api()))
    check_version(client.VersionApi())
    cert, key = certificate(f"{WEBHOOK}.{s.namespace}.svc")
    headers = {"Authorization": f"Bearer {s.token.get_secret_value()}"}
    with httpx.Client(base_url=s.hub, headers=headers, timeout=15) as http:
        a = Actuator(http, core, apps, s.namespace)
        a.autoscaling, a.custom = Calm(client.AutoscalingV2Api()), custom
        a.policy = Calm(client.PolicyV1Api())
        a.load()
        serve(a, cert, key)
        try:  # until then the API server can't call it, and pods start unchanged
            trust(client.AdmissionregistrationV1Api(), cert)
        except ApiException as e:
            log.error("its webhook's caBundle: %s: new pods start from their template", why(e))
        log.info("actuator started: %d containers held", len(a.held))
        while True:
            try:
                a.step()
            except Exception:  # a bug in one minute's work: said, and the next still runs
                log.exception("this minute failed")
            time.sleep(s.interval)
