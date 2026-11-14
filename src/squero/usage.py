"""Workload usage for Sizings (slice 9.1): read-only, from each cluster's Prometheus.

The Hub queries Prometheus itself, with no model call. Prometheus does the time math
(`rate` handles counter resets, `max_over_time` keeps memory peaks between steps); this
module pools the samples of a Workload's pods per container, so a Sizing is per replica
whatever the replica count, and follows pod owners up to their Workload.

Every query carries the cluster's namespaces and label matchers: on a store shared by
several clusters or Contracts (Thanos, Mimir) nothing else may come back.
"""

import dataclasses
import logging
import math
import re
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import SecretStr

from squero.contracts import Cluster, Contract, Prometheus, ksm_label
from squero.listprices import SAFE

log = logging.getLogger(__name__)

MAX_POINTS = 10_000  # Prometheus refuses a range query over 11,000 points per series
# the burst Reflex's trigger (slice 13.3a): throttling over the hour before the read, told
# only of a container that ran this many CFS periods in it (a minute's worth at the
# default 100 ms), so one throttled in the second it ran all hour is no Burst
RECENT = 3600
MIN_PERIODS = 600
CONTAINERS = 'container!="",container!="POD"'  # cAdvisor's pod-level and pause series
REQUIRED = {  # metric -> what it gives a Sizing
    "container_cpu_usage_seconds_total": "CPU usage (cAdvisor)",
    "container_memory_working_set_bytes": "memory usage (cAdvisor)",
    "kube_pod_container_resource_requests": "requests (kube-state-metrics)",
    "kube_pod_owner": "pod owners (kube-state-metrics)",
}
OPTIONAL = {
    "kube_pod_container_resource_limits": "limits",
    "kube_replicaset_owner": "Deployments and Rollouts behind ReplicaSets",
    "kube_job_owner": "CronJobs behind Jobs",
    "kube_pod_container_status_last_terminated_reason": "OOM kills",
    "container_cpu_cfs_throttled_periods_total": "CPU throttling",
    "kube_pod_status_phase": "pods running, for waste (else counted from CPU samples)",
}
KSM = {m for m in (*REQUIRED, *OPTIONAL) if m.startswith("kube_")}


@dataclass(frozen=True)
class Target:
    """One cluster's metrics, scoped to its Contract's namespaces."""

    cluster: str
    url: str
    token: SecretStr | None
    labels: dict[str, str]
    namespaces: list[str]
    # kube-state-metrics scraped without honor_labels puts the pod's namespace in
    # exported_namespace (its own namespace wins `namespace`); check() finds out.
    ksm_namespace: str = "namespace"
    relay: tuple[str, str] | None = None  # (contract, cluster): read through its Relay


def targets(contract: Contract) -> list[Target]:
    """A Target per cluster with a Prometheus: its own URL, else the Contract's
    prometheus integration's. A cluster with neither has no Sizings. A Relay's cluster
    is read through its Relay (slice 3.3b), never Direct: no URL, only when the Contract
    has Prometheus at all, as its tools are."""
    shared = next((i for i in contract.integrations if isinstance(i, Prometheus)), None)
    out = []
    for c in contract.clusters:
        if c.via == "relay":
            if shared:
                where = (contract.id, c.name)
                labels, spaces = dict(c.prometheus_labels), list(c.namespaces)
                out.append(Target(c.name, "", None, labels, spaces, relay=where))
            continue
        url, secret = (
            (c.prometheus_url, c.prometheus_token_secret)
            if c.prometheus_url
            else ((shared.url, shared.token_secret) if shared else (None, None))
        )
        if url:
            token = contract.secret(secret) if secret else None
            out.append(Target(c.name, url, token, dict(c.prometheus_labels), list(c.namespaces)))
    return out


def _string(value: str) -> str:
    """A PromQL double-quoted string."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def _any_of(values: list[str]) -> str:
    """An RE2 alternation matching exactly these values (PromQL anchors =~)."""
    return _string("|".join(re.escape(v) for v in values))


def selector(metric: str, t: Target, *extra: str) -> str:
    """`metric{...}` limited to the Target's namespaces and labels: no other Contract's
    or cluster's series can match."""
    ns = t.ksm_namespace if metric.startswith("kube_") else "namespace"
    matchers = [f"{ns}=~{_any_of(t.namespaces)}"]
    matchers += [f"{k}={_string(v)}" for k, v in sorted(t.labels.items())]
    return f"{metric}{{{','.join([*matchers, *extra])}}}"


class Prom:
    """Prometheus' HTTP API, read-only. Raises UsageError on anything but success."""

    def __init__(self, t: Target, http: httpx.Client | None = None) -> None:
        if http is None:
            auth = {"Authorization": f"Bearer {t.token.get_secret_value()}"} if t.token else {}
            http = httpx.Client(base_url=t.url, headers=auth, timeout=30)
        self.http = http

    def _get(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            resp = self.http.get(path, params=params)
            body = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            raise UsageError(f"Prometheus unreachable: {e}") from None
        if resp.status_code != 200 or body.get("status") != "success":
            raise UsageError(f"Prometheus error {resp.status_code}: {body.get('error', '')}")
        result: list[dict[str, Any]] = body["data"]["result"]
        return result

    def instant(self, promql: str, at: float) -> list[dict[str, Any]]:
        return self._get("/api/v1/query", {"query": promql, "time": at})

    def range(self, promql: str, start: float, end: float, step: int) -> list[dict[str, Any]]:
        return self._get(
            "/api/v1/query_range", {"query": promql, "start": start, "end": end, "step": step}
        )


class UsageError(Exception):
    pass


def reader(t: Target) -> Prom:
    """How the Hub reads a Target: Prometheus' API, or its Relay's (slice 3.3b)."""
    if t.relay is None:
        return Prom(t)
    from squero import relay  # it imports this module

    return relay.Reader(relay.HUB, t.relay)


def step_for(window_s: int) -> int:
    """Seconds between samples: one minute, coarser when the window needs it so no
    series goes over MAX_POINTS."""
    return max(60, 60 * math.ceil(window_s / MAX_POINTS / 60))


def check(prom: Prom, t: Target, at: float | None = None) -> tuple[Target, dict[str, str]]:
    """Which metrics are missing (metric -> what it gives), and the Target with the
    namespace label kube-state-metrics uses here."""
    at = at or time.time()
    # How kube-state-metrics is scraped, asked of the whole store (within the cluster's
    # labels), never of the Contract's namespaces: a Contract owning the namespace that
    # kube-state-metrics runs in would otherwise match every pod's series by it.
    scoped = ",".join(f"{k}={_string(v)}" for k, v in sorted(t.labels.items()))
    renamed = f'kube_pod_owner{{exported_namespace!=""{"," + scoped if scoped else ""}}}'
    if prom.instant(f"count({renamed})", at):
        t = dataclasses.replace(t, ksm_namespace="exported_namespace")
    found = set()
    for metric in (*REQUIRED, *OPTIONAL):
        if metric not in found and prom.instant(f"count({selector(metric, t)})", at):
            found.add(metric)
    missing = {m: w for m, w in {**REQUIRED, **OPTIONAL}.items() if m not in found}
    return t, missing


def label(m: dict[str, str], name: str) -> str:
    """A pod's own label on a kube-state-metrics series: scraped without honor_labels,
    Prometheus renames the colliding namespace, pod and container to exported_*."""
    return m.get(f"exported_{name}", m.get(name, ""))


@dataclass(frozen=True)
class Workload:
    namespace: str
    kind: str  # Deployment, StatefulSet, DaemonSet, Job, CronJob, Rollout, ReplicaSet, Pod, ...
    name: str


def _controller_first(series: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """An object can list several owners; its controller is the one that counts."""
    return sorted(series, key=lambda s: s["metric"].get("owner_is_controller") != "true")


def resolve(
    pod_owners: list[dict[str, Any]],
    rs_owners: list[dict[str, Any]],
    job_owners: list[dict[str, Any]],
) -> dict[tuple[str, str], Workload]:
    """(namespace, pod) -> its Workload, from kube-state-metrics' owner series: a
    ReplicaSet up to its Deployment or Rollout, a Job up to its CronJob, an ownerless
    pod as itself, any other owner (a StatefulSet, a custom resource) as it is."""

    def owners(series: list[dict[str, Any]], key: str) -> dict[tuple[str, str], tuple[str, str]]:
        out: dict[tuple[str, str], tuple[str, str]] = {}
        for s in reversed(_controller_first(series)):  # the controller written last wins
            m = s["metric"]
            out[(label(m, "namespace"), label(m, key))] = (
                m.get("owner_kind", "<none>"),
                m.get("owner_name", ""),
            )
        return out

    rs, jobs = owners(rs_owners, "replicaset"), owners(job_owners, "job_name")
    result = {}
    for (ns, pod), (kind, name) in owners(pod_owners, "pod").items():
        if kind == "<none>":
            kind, name = "Pod", pod
        elif kind == "ReplicaSet" and rs.get((ns, name), ("<none>",))[0] != "<none>":
            kind, name = rs[(ns, name)]
        elif kind == "Job" and jobs.get((ns, name), ("<none>",))[0] != "<none>":
            kind, name = jobs[(ns, name)]
        result[(ns, pod)] = Workload(ns, kind, name)
    return result


def owners_from_api(
    core: Any, apps: Any, batch: Any, namespaces: list[str]
) -> dict[tuple[str, str], Workload]:
    """The same map from the Kubernetes API, for a cluster without kube-state-metrics
    or pods it doesn't list. ponytail: live pods only, so a replaced pod's usage has
    no Workload this way; kube-state-metrics keeps history."""

    def series(objs: list[Any], key: str) -> list[dict[str, Any]]:
        out = []
        for o in objs:
            refs = o.metadata.owner_references or [None]
            for r in refs:
                out.append(
                    {
                        "metric": {
                            "namespace": o.metadata.namespace,
                            key: o.metadata.name,
                            "owner_kind": r.kind if r else "<none>",
                            "owner_name": r.name if r else "<none>",
                            "owner_is_controller": "true" if r and r.controller else "false",
                        }
                    }
                )
        return out

    pods, rss, jobs = [], [], []
    for ns in namespaces:
        pods += core.list_namespaced_pod(ns).items
        rss += apps.list_namespaced_replica_set(ns).items
        jobs += batch.list_namespaced_job(ns).items
    return resolve(series(pods, "pod"), series(rss, "replicaset"), series(jobs, "job_name"))


@dataclass
class ContainerUsage:
    """One container of one Workload over the window, its pods' samples pooled."""

    workload: Workload
    container: str
    cpu: list[tuple[float, float]] = field(default_factory=list)  # (time, cores) per pod per step
    memory: list[tuple[float, float]] = field(default_factory=list)  # (time, working set bytes)
    requests: dict[str, float] = field(default_factory=dict)  # cpu (cores), memory (bytes)
    limits: dict[str, float] = field(default_factory=dict)
    pods: set[str] = field(default_factory=set)
    oom_killed_pods: int = 0
    throttled: float | None = None  # share of CFS periods throttled, None: no data
    # the same over the last hour (slice 13.3a); None: not read, or too few periods to tell
    throttled_recent: float | None = None
    running: list[float] = field(default_factory=list)  # a time per Running pod per step
    labels: dict[str, str] = field(default_factory=dict)  # the allocation's, newest pod's
    live: bool = False  # requests or limits read from pods seen in the last 5 minutes


@dataclass
class Usage:
    rows: list[ContainerUsage]
    unknown: list[str]  # namespace/pod with usage but no Workload
    fallback_error: str | None = None  # why the Kubernetes API couldn't fill the gaps


def usage(
    prom: Prom,
    t: Target,
    window_s: int,
    end: float | None = None,
    fallback: Callable[[], dict[tuple[str, str], Workload]] | None = None,
    labels: Sequence[str] = (),
    recent: bool = False,
) -> Usage:
    """Every container's pooled usage over the window, and the pods whose Workload
    nobody knows. `fallback` (the Kubernetes API) is asked once, only when a pod with
    usage isn't in kube-state-metrics; kube-state-metrics wins where both know a pod.
    A fallback that fails leaves those pods unknown, never the rest unread. `labels`:
    the pod label keys each Workload gets from kube_pod_labels, its newest pod's.
    `recent`: also each container's throttling over the last hour (slice 13.3a)."""
    end = end or time.time()
    start, step = end - window_s, step_for(window_s)
    window = f"{window_s}s"

    def at_end(promql: str) -> list[dict[str, Any]]:
        return prom.instant(promql, end)

    owners = resolve(
        at_end(f"last_over_time({selector('kube_pod_owner', t)}[{window}])"),
        at_end(f"last_over_time({selector('kube_replicaset_owner', t)}[{window}])"),
        at_end(f"last_over_time({selector('kube_job_owner', t)}[{window}])"),
    )
    cpu_q = selector("container_cpu_usage_seconds_total", t, CONTAINERS)
    mem_q = selector("container_memory_working_set_bytes", t, CONTAINERS)
    cpu = prom.range(f"rate({cpu_q}[{max(300, step)}s])", start, end, step)
    mem = prom.range(f"max_over_time({mem_q}[{step}s])", start, end, step)
    pods = {(label(s["metric"], "namespace"), label(s["metric"], "pod")) for s in cpu + mem}
    error = None
    if fallback and pods - owners.keys():
        try:
            owners = {**fallback(), **owners}
        except Exception as e:  # forbidden, unreachable: kube-state-metrics' answer stands
            error = f"{type(e).__name__}: {str(e)[:200]}"

    rows: dict[tuple[Workload, str], ContainerUsage] = {}
    for samples, into in ((cpu, "cpu"), (mem, "memory")):
        for s in samples:
            m = s["metric"]
            w = owners.get((label(m, "namespace"), label(m, "pod")))
            if w is None:
                continue
            container = label(m, "container")
            r = rows.setdefault((w, container), ContainerUsage(w, container))
            r.pods.add(label(m, "pod"))
            getattr(r, into).extend((float(at), float(v)) for at, v in s["values"])
    for metric, into in (
        ("kube_pod_container_resource_requests", "requests"),
        ("kube_pod_container_resource_limits", "limits"),
    ):
        # The live pods' (seen in the last 5 minutes), so a lowered request shows the
        # next run (slice 10.3b); a container with none live keeps the window's (a
        # CronJob between runs, a Workload scaled to 0).
        live: set[tuple[int, str]] = set()
        for span in ("300s", window):
            for s in at_end(f"last_over_time({selector(metric, t)}[{span}])"):
                m = s["metric"]
                res = m.get("resource")
                if res not in ("cpu", "memory") or not (hit := _known(rows, owners, m)):
                    continue
                if span == window and (id(hit), res) in live:
                    continue
                if span != window:
                    live.add((id(hit), res))
                    hit.live = True
                # ponytail: the largest across those pods; mid-rollout that is the old
                # or the new value, whichever is bigger, until the old pods are gone
                current = getattr(hit, into)
                current[res] = max(current.get(res, 0.0), float(s["value"][1]))
    oom = selector("kube_pod_container_status_last_terminated_reason", t, 'reason="OOMKilled"')
    for s in at_end(f"max_over_time({oom}[{window}])"):
        if float(s["value"][1]) > 0 and (hit := _known(rows, owners, s["metric"])):
            hit.oom_killed_pods += 1

    def throttling(span: str, least: float) -> dict[tuple[Workload, str], float]:
        """Each container's share of CFS periods throttled over `span`, its pods pooled,
        where they ran more than none and at least `least`."""
        throttled, periods = (
            _sum_by_row(at_end(f"increase({selector(m, t, CONTAINERS)}[{span}])"), rows, owners)
            for m in (
                "container_cpu_cfs_throttled_periods_total",
                "container_cpu_cfs_periods_total",
            )
        )
        return {k: throttled.get(k, 0.0) / n for k, n in periods.items() if n > 0 and n >= least}

    for key, share in throttling(window, 0).items():
        rows[key].throttled = share
    if recent:
        for key, share in throttling(f"{RECENT}s", MIN_PERIODS).items():
            rows[key].throttled_recent = share
    # Pods counted from their phase, not from CPU samples: `rate` over 5 minutes
    # outlives a pod by that long, which makes a one-minute Job look like six.
    running: dict[tuple[str, str], set[float]] = defaultdict(set)
    phase = selector("kube_pod_status_phase", t, 'phase="Running"')
    for s in prom.range(phase, start, end, step):
        pod = (label(s["metric"], "namespace"), label(s["metric"], "pod"))
        running[pod].update(float(at) for at, v in s["values"] if float(v) > 0)
    for (w, _), r in rows.items():
        r.running = sorted(at for pod in r.pods for at in running.get((w.namespace, pod), ()))
    if labels:
        newest = _newest_labels(at_end, t, window, owners, labels)
        for (w, _), r in rows.items():
            r.labels = newest.get(w, {})
    unknown = sorted(f"{n}/{p}" for n, p in pods - owners.keys())
    return Usage(list(rows.values()), unknown, error)


def _newest_labels(
    at_end: Callable[[str], list[dict[str, Any]]],
    t: Target,
    window: str,
    owners: dict[tuple[str, str], Workload],
    keys: Sequence[str],
) -> dict[Workload, dict[str, str]]:
    """Each Workload's labels (only `keys`) from its newest pod by kube_pod_created,
    so a relabel shows once the rollout starts; pods without it order by name. A pod
    can have several series (a kube-state-metrics restart, a StatefulSet pod's name
    reused): each pod incarnation is its uid, and of one pod's series the one with
    the most listed labels wins. ponytail: so a label removed in place shows only once
    the old series leaves the window; an instant query at `end` would show it at once."""

    def pod_id(m: dict[str, str]) -> tuple[str, str, str]:
        return label(m, "namespace"), label(m, "pod"), m.get("uid", "")

    created = {
        pod_id(s["metric"]): float(s["value"][1])
        for s in at_end(f"last_over_time({selector('kube_pod_created', t)}[{window}])")
    }
    newest: dict[Workload, tuple[Any, dict[str, str]]] = {}
    for s in at_end(f"last_over_time({selector('kube_pod_labels', t)}[{window}])"):
        m = s["metric"]
        pod = pod_id(m)
        if (w := owners.get(pod[:2])) is None:
            continue
        found = {k: m[ksm_label(k)] for k in keys if m.get(ksm_label(k))}
        order = (created.get(pod, 0.0), len(found), sorted(found.items()), pod[1])
        if w not in newest or order > newest[w][0]:
            newest[w] = (order, found)
    return {w: found for w, (_, found) in newest.items()}


def _known(
    rows: dict[tuple[Workload, str], ContainerUsage],
    owners: dict[tuple[str, str], Workload],
    m: dict[str, str],
) -> ContainerUsage | None:
    """The row a kube-state-metrics or cAdvisor series belongs to, if it has usage."""
    w = owners.get((label(m, "namespace"), label(m, "pod")))
    return rows.get((w, label(m, "container"))) if w else None


def _sum_by_row(
    series: list[dict[str, Any]],
    rows: dict[tuple[Workload, str], ContainerUsage],
    owners: dict[tuple[str, str], Workload],
) -> dict[tuple[Workload, str], float]:
    out: dict[tuple[Workload, str], float] = defaultdict(float)
    for s in series:
        if r := _known(rows, owners, s["metric"]):
            out[(r.workload, r.container)] += float(s["value"][1])
    return out


TYPE_LABELS = ("label_node_kubernetes_io_instance_type", "label_beta_kubernetes_io_instance_type")
REGION_LABELS = (
    "label_topology_kubernetes_io_region",
    "label_failure_domain_beta_kubernetes_io_region",
)
PROVIDERS = {"aws": "aws", "azure": "azure", "gce": "gcp"}  # kube_node_info provider_id scheme


def nodes(prom: Prom, t: Target, end: float, provider: str | None = None) -> dict[str, Any]:
    """The cluster's nodes for list prices (slice 10.4): per node its provider, region,
    instance type and capacity, from kube-state-metrics (whose node labels must be
    allowlisted). `problem` says why a node can't be priced; the nodes are kept anyway.
    Node series carry no namespace: only the cluster's labels scope them."""
    scoped = ",".join(f"{k}={_string(v)}" for k, v in sorted(t.labels.items()))

    def read(metric: str, *extra: str) -> list[dict[str, Any]]:
        return prom.instant(f"{metric}{{{','.join(filter(None, [scoped, *extra]))}}}", end)

    info = {s["metric"].get("node", ""): s["metric"] for s in read("kube_node_info")}
    labels = {s["metric"].get("node", ""): s["metric"] for s in read("kube_node_labels")}
    capacity = {
        res: {
            s["metric"].get("node", ""): float(s["value"][1])
            for s in read("kube_node_status_capacity", f"resource={_string(res)}")
        }
        for res in ("cpu", "memory")
    }
    out, lacking = [], 0
    for name in sorted(info):
        have = labels.get(name, {})
        kind = next((have[k] for k in TYPE_LABELS if have.get(k)), "")
        region = next((have[k] for k in REGION_LABELS if have.get(k)), "")
        scheme = info[name].get("provider_id", "").partition(":")[0]
        who = provider or PROVIDERS.get(scheme)
        cpu, memory = capacity["cpu"].get(name), capacity["memory"].get(name)
        if not (SAFE.fullmatch(kind) and SAFE.fullmatch(region) and who and cpu and memory):
            lacking += 1
        out.append(
            {
                "provider": who or "",
                "region": region,
                "type": kind,
                "vcpu": cpu or 0.0,
                "gib": (memory or 0.0) / 2**30,
            }
        )
    problem = None
    if not out:
        problem = "no nodes read: kube_node_info is missing"
    elif lacking:
        problem = (
            f"{lacking} of {len(out)} nodes lack an instance type, region, provider or "
            "capacity: allowlist the node labels node.kubernetes.io/instance-type and "
            "topology.kubernetes.io/region in kube-state-metrics, or set the cluster's provider"
        )
    return {"nodes": out, "problem": problem}


def node_hours(prom: Prom, t: Target, start: float, end: float) -> tuple[float, float, int]:
    """The vCPU-hours and GiB-hours of the cluster's nodes between `start` and `end`
    (slice 10.5a), from kube-state-metrics' node capacity, one sample an hour, and how
    many hourly samples there were (a sleeping laptop leaves gaps; slice 10.5d). Node
    series carry no namespace: only the cluster's labels scope them."""
    # ponytail: an hourly sample stands for its hour; a node that lived 20 minutes is
    # counted whole or not at all. Finer steps if the gap to the bill shows it.
    scoped = ",".join(f"{k}={_string(v)}" for k, v in sorted(t.labels.items()))

    def hours(resource: str) -> tuple[float, int]:
        match = ",".join(filter(None, [scoped, f"resource={_string(resource)}"]))
        series = prom.range(f"sum(kube_node_status_capacity{{{match}}})", start, end, 3600)
        return (
            sum(float(v) for s in series for _, v in s["values"]),
            sum(len(s["values"]) for s in series),
        )

    (vcpu, sampled), (memory, _) = hours("cpu"), hours("memory")
    return vcpu, memory / 2**30, sampled


SHARED_STORE = (
    "the store is shared with another Contract and has no cluster labels: node series "
    "would cross Contracts"
)
POD_REQUESTS = "kube_pod_container_resource_requests"


def rooms(prom: Prom, t: Target, at: float) -> dict[str, Any]:
    """The cluster's nodes with what their Room is made of (slice 15.1a), from
    kube-state-metrics as it is at `at`: per node its allocatable, taints, whether it is
    cordoned, its instance type where the label is allowlisted, and the requests of the
    pods on it, Running or Pending (a finished pod holds nothing; init containers are
    left out).

    Node series carry no namespace, and a node's Room needs every pod on it: `requests`,
    `daemonsets` (its DaemonSets' pods' share) and `pods` are summed by Prometheus over
    all namespaces, scoped by the cluster's labels only, so a pod outside the Contract's
    namespaces reaches the Hub as part of a sum and a count per node, never by name.
    `own`: the Contract's pods there, each with its requests, read in its namespaces.
    `problem` when no node is read; the caller refuses a store shared with another
    Contract that has no cluster labels (`SHARED_STORE`), as for list prices."""
    scoped = [f"{k}={_string(v)}" for k, v in sorted(t.labels.items())]

    def of(metric: str, *extra: str) -> str:
        return f"{metric}{{{','.join([*scoped, *extra])}}}"

    def per_node(series: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for s in series:
            m = s["metric"]
            by = out.setdefault(label(m, "node"), {})
            by[m.get("resource", "")] = by.get(m.get("resource", ""), 0.0) + float(s["value"][1])
        return out

    both = 'resource=~"cpu|memory"'
    by = "by (node, exported_node, resource)"
    # kube-state-metrics puts the pod's uid on every pod series: a join that needs no
    # namespace or pod label, whichever way those were scraped
    phase = of("kube_pod_status_phase", 'phase=~"Pending|Running"')
    live = f"* on (uid) group_left() max by (uid) ({phase} == 1)"
    daemons = of("kube_pod_info", 'created_by_kind="DaemonSet"')
    of_daemons = f"* on (uid) group_left() max by (uid) ({daemons})"
    allocatable = per_node(prom.instant(of("kube_node_status_allocatable", both), at))
    requests = per_node(prom.instant(f"sum {by} ({of(POD_REQUESTS, both)} {live})", at))
    daemonsets = per_node(
        prom.instant(f"sum {by} ({of(POD_REQUESTS, both)} {of_daemons} {live})", at)
    )
    pods = {
        label(s["metric"], "node"): int(float(s["value"][1]))
        for s in prom.instant(f"count by (node, exported_node) ({of('kube_pod_info')} {live})", at)
    }
    # the Contract's pods by node, from kube_pod_info so one asking nothing is still its
    # own; only its live pods are listed, so a finished one's requests go unused
    asks: dict[tuple[str, str], dict[str, float]] = {}
    for s in prom.instant(selector(POD_REQUESTS, t, both), at):
        m = s["metric"]
        pod = asks.setdefault((label(m, "namespace"), label(m, "pod")), {})
        pod[m.get("resource", "")] = pod.get(m.get("resource", ""), 0.0) + float(s["value"][1])
    own: dict[str, list[dict[str, Any]]] = {}
    for s in prom.instant(f"{selector('kube_pod_info', t)} {live}", at):
        m = s["metric"]
        who = (label(m, "namespace"), label(m, "pod"))
        mine = {"cpu": 0.0, "memory": 0.0} | asks.get(who, {})
        own.setdefault(label(m, "node"), []).append({"namespace": who[0], "pod": who[1], **mine})
    taints: dict[str, list[str]] = {}
    for s in prom.instant(of("kube_node_spec_taint"), at):
        m = s["metric"]
        value = f"={m['value']}" if m.get("value") else ""
        taints.setdefault(label(m, "node"), []).append(
            f"{m.get('key', '')}{value}:{m.get('effect', '')}"
        )
    cordoned = {
        label(s["metric"], "node")
        for s in prom.instant(of("kube_node_spec_unschedulable") + " == 1", at)
    }
    labels = {
        label(s["metric"], "node"): s["metric"] for s in prom.instant(of("kube_node_labels"), at)
    }
    out = []
    for name in sorted({*allocatable, *requests} - {""}):  # "": a pod on no node yet
        have = labels.get(name, {})
        out.append(
            {
                "node": name,
                "type": next((have[k] for k in TYPE_LABELS if have.get(k)), ""),
                "taints": sorted(taints.get(name, [])),
                "cordoned": name in cordoned,
                "allocatable": allocatable.get(name, {}),
                "requests": {"cpu": 0.0, "memory": 0.0} | requests.get(name, {}),
                "daemonsets": {"cpu": 0.0, "memory": 0.0} | daemonsets.get(name, {}),
                "pods": pods.get(name, 0),
                "own": sorted(own.get(name, []), key=lambda p: (p["namespace"], p["pod"])),
            }
        )
    problem = None if out else "no nodes read: kube_node_status_allocatable is missing"
    return {"nodes": out, "problem": problem}


NEEDS_FOR = 56 * 86400  # Needs are kept, and first read, this far back (slice 14.1a)
HPA = "kube_horizontalpodautoscaler"


def needs(prom: Prom, t: Target, start: float, end: float) -> dict[Workload, dict[float, float]]:
    """Each autoscaled Workload's Need (slice 14.1a) from `start` to `end`, as the peak
    of every UTC hour (by its start). At each step: the replicas of the Workload's HPA
    (KEDA's is one) times the largest, over the metrics it scales on, of the metric's
    current value over its target, both in the unit of the target's own type. A step
    gives none where a target has no current value (or the HPA no target at all), a
    target is not above zero, or two HPAs name the Workload. With no replicas nothing
    ran and nothing was needed: 0, whatever the series say. The replicas are the
    smaller of the step's and the step before's: an HPA reports a scale-up's new count
    at once, but the utilization of the pods that already have metrics, the old ones.

    All from kube-state-metrics, as Sizings are; samples at each step, so a peak
    between two is missed (9 minutes apart on the first read of 8 weeks)."""

    def read(metric: str) -> list[dict[str, Any]]:
        return prom.range(selector(f"{HPA}_{metric}", t), start, end, step)

    step = step_for(int(end - start))
    of: dict[tuple[str, str], dict[float, Workload]] = {}  # an HPA's Workload at each step
    for s in read("info"):
        m = s["metric"]
        hpa = (label(m, "namespace"), m.get("horizontalpodautoscaler", ""))
        w = Workload(hpa[0], m.get("scaletargetref_kind", ""), m.get("scaletargetref_name", ""))
        of.setdefault(hpa, {}).update((float(at), w) for at, _ in s["values"])
    if not of:
        return {}  # no HPA here: one query an hour
    Values = dict[tuple[str, str], dict[float, dict[tuple[str, ...], float]]]

    def by_step(metric: str) -> Values:
        """Per HPA and step, each series' value by what tells it from the HPA's others:
        the metric, its target's type and (kube-state-metrics 2.19) its container."""
        out: Values = {}
        for s in read(metric):
            m = s["metric"]
            hpa = (label(m, "namespace"), m.get("horizontalpodautoscaler", ""))
            what = (
                m.get("metric_name", ""),
                m.get("metric_target_type", ""),
                label(m, "container"),
            )
            for at, v in s["values"]:
                out.setdefault(hpa, {}).setdefault(float(at), {})[what] = float(v)
        return out

    replicas, wanted, now = (
        by_step(m)
        for m in ("status_current_replicas", "spec_target_metric", "status_target_metric")
    )
    counts: dict[tuple[str, str], dict[float, float]] = {}
    for who, by in replicas.items():
        last: tuple[float, float] | None = None
        for when in sorted(by):
            count = next(iter(by[when].values()))
            near = last is not None and when - last[0] <= 1.5 * step  # not across a gap
            counts.setdefault(who, {})[when] = min(count, last[1]) if last and near else count
            last = (when, count)
    found: dict[Workload, dict[float, list[float | None]]] = {}  # a need per HPA per step
    for hpa, steps in of.items():
        for at, w in steps.items():
            n = counts.get(hpa, {}).get(at)
            targets, values = wanted.get(hpa, {}).get(at, {}), now.get(hpa, {}).get(at, {})
            need = None
            if n == 0:
                need = 0.0
            elif n and targets and all(v > 0 and k in values for k, v in targets.items()):
                need = n * max(values[k] / v for k, v in targets.items())
            found.setdefault(w, {}).setdefault(at, []).append(need)
    out: dict[Workload, dict[float, float]] = {}
    for w, steps_ in found.items():
        for at, (need, *more) in steps_.items():
            if need is None or more:  # unknown, or two HPAs: nothing to go by
                continue
            hour = at - at % 3600
            out.setdefault(w, {})[hour] = max(need, out.get(w, {}).get(hour, 0.0))
    return out


@dataclass(frozen=True, order=True)
class Divides:
    """An HPA target that divides by a request (slice 14.3): a `Utilization` of cpu or
    memory, of the whole pod or of one container."""

    hpa: str
    resource: str  # cpu or memory
    container: str  # the series' own `container` label: a container's name, or not one
    percent: float


@dataclass(frozen=True, order=True)
class Bounds:
    """An HPA's minimum and maximum replicas (slice 14.1b); None: not known. `zero`:
    its replicas were 0 at some time in the lookback, so its Workload scales to zero."""

    hpa: str
    minimum: float | None
    maximum: float | None
    zero: bool = False


def _hpas(prom: Prom, t: Target, at: float) -> dict[tuple[str, str], Workload]:
    """Each HPA (namespace, name) there is at `at`, with the Workload it scales."""
    of = {}
    for s in prom.instant(selector(f"{HPA}_info", t), at):
        m = s["metric"]
        hpa = (label(m, "namespace"), m.get("horizontalpodautoscaler", ""))
        of[hpa] = Workload(
            hpa[0], m.get("scaletargetref_kind", ""), m.get("scaletargetref_name", "")
        )
    return of


def bounds(prom: Prom, t: Target, at: float, back: int) -> dict[Workload, list[Bounds]]:
    """Each Workload's HPAs with their minimum and maximum replicas as they are at `at`
    (slice 14.1b), from kube-state-metrics, and whether its replicas were 0 at some time
    in the `back` seconds before. That, not the minimum, says a Workload scales to
    zero: KEDA gives its HPA a minimum of 1 and scales to zero itself. Nor does a Need
    of 0: an idle Workload held at its minimum has one too."""
    of = _hpas(prom, t, at)
    if not of:
        return {}
    found: dict[tuple[str, str], dict[str, float]] = {}
    for which in ("min", "max"):
        for s in prom.instant(selector(f"{HPA}_spec_{which}_replicas", t), at):
            m = s["metric"]
            hpa = (label(m, "namespace"), m.get("horizontalpodautoscaler", ""))
            found.setdefault(hpa, {})[which] = float(s["value"][1])
    replicas = selector(f"{HPA}_status_current_replicas", t)
    for s in prom.instant(f"min_over_time({replicas}[{back}s])", at):
        m = s["metric"]
        hpa = (label(m, "namespace"), m.get("horizontalpodautoscaler", ""))
        found.setdefault(hpa, {})["least"] = float(s["value"][1])
    out: dict[Workload, list[Bounds]] = {}
    for hpa, w in sorted(of.items()):
        has = found.get(hpa, {})
        zero = has.get("least") == 0
        out.setdefault(w, []).append(Bounds(hpa[1], has.get("min"), has.get("max"), zero))
    return out


def scaled(prom: Prom, t: Target, at: float) -> dict[Workload, list[Divides]]:
    """Each Workload's HPA targets that divide by a request (slice 14.3), as
    kube-state-metrics has them at `at`: plain selectors, so a target changed since
    (to an `AverageValue`) is gone at once, not five minutes later."""
    of = _hpas(prom, t, at)
    if not of:
        return {}
    out: dict[Workload, list[Divides]] = {}
    targets = selector(f"{HPA}_spec_target_metric", t, 'metric_target_type="utilization"')
    for s in prom.instant(targets, at):
        m = s["metric"]
        hpa = (label(m, "namespace"), m.get("horizontalpodautoscaler", ""))
        if hpa in of and m.get("metric_name") in ("cpu", "memory"):
            found = Divides(hpa[1], m["metric_name"], label(m, "container"), float(s["value"][1]))
            out.setdefault(of[hpa], []).append(found)
    return {w: sorted(found) for w, found in out.items()}


@dataclass
class Collected:
    """A Contract's usage per window, (cluster, usage) pairs, and per cluster None when
    it was read or why it wasn't. `notes`: missing metrics and Workload-less pods."""

    by_window: dict[int, list[tuple[str, ContainerUsage]]]
    clusters: dict[str, str | None]
    notes: list[str] = field(default_factory=list)
    nodes: dict[str, dict[str, Any]] = field(default_factory=dict)  # per cluster, 10.4
    # per cluster, each Workload's Need by hour (slice 14.1a); a cluster not read has none
    needs: dict[str, dict[Workload, dict[float, float]]] = field(default_factory=dict)
    # per cluster, the HPA targets that divide by a request (slice 14.3)
    scaled: dict[str, dict[Workload, list[Divides]]] = field(default_factory=dict)
    # per cluster read, each Workload's HPAs' minimum and maximum (slice 14.1b)
    bounds: dict[str, dict[Workload, list[Bounds]]] = field(default_factory=dict)
    # per cluster, its nodes and what their Room is made of (slice 15.1a), or why not
    rooms: dict[str, dict[str, Any]] = field(default_factory=dict)


def collect(
    c: Contract,
    windows: list[int],
    end: float | None = None,
    needs_since: dict[str, float] | None = None,
) -> Collected:
    """Every cluster's container usage over each window, ending at `end`. A cluster
    that fails is named in `clusters` and never stops the others. `needs_since`: also
    each cluster's Needs (slice 14.1a) from the hour given for it, 8 weeks back for
    one it doesn't name and never further; a read of them that fails stops nothing,
    nor one of the HPAs' targets (slice 14.3) or bounds (slice 14.1b)."""
    from kubernetes import client as k

    from squero.servers import kubernetes

    end = end or time.time()
    out = Collected({w: [] for w in windows}, {})
    for t in targets(c):
        cluster = next(cl for cl in c.clusters if cl.name == t.cluster)

        def from_api(cl: Cluster = cluster, t: Target = t) -> Any:
            api = kubernetes.api_client(cl, c)
            return owners_from_api(
                k.CoreV1Api(api), k.AppsV1Api(api), k.BatchV1Api(api), t.namespaces
            )

        prom = reader(t)
        recent = c.actuator.reflexes.burst is not None  # slice 13.3a
        # A Relay's cluster has no Kubernetes API here: its Workloads come from metrics.
        fallback = from_api if t.relay is None else None
        try:
            t, missing = check(prom, t, end)
            # ponytail: each window queried in full (a 7d and a 14d policy read the 14d
            # range twice), so a policy's OOM kills, throttling and step are its own
            by_window = {
                w: usage(prom, t, w, end, fallback, c.allocation.labels, recent) for w in windows
            }
        except UsageError as e:
            out.clusters[t.cluster] = str(e)
            continue
        out.clusters[t.cluster] = None
        if needs_since is not None:
            # ponytail: a cluster whose HPAs never give a Need (no target series) is
            # read 8 weeks back every hour; keep a per-cluster read mark if that costs
            start = max(needs_since.get(t.cluster, 0.0), end - NEEDS_FOR)
            try:
                out.needs[t.cluster] = needs(prom, t, start, end)
            except UsageError as e:
                log.warning("%s/%s: its Needs not read: %s", c.id, t.cluster, e)
        try:
            out.scaled[t.cluster] = scaled(prom, t, end)
        except UsageError as e:
            log.warning("%s/%s: its HPAs' targets not read: %s", c.id, t.cluster, e)
        try:
            out.bounds[t.cluster] = bounds(prom, t, end, min(max(windows), NEEDS_FOR))
        except UsageError as e:
            log.warning("%s/%s: its HPAs' bounds not read: %s", c.id, t.cluster, e)
        # Node series carry no namespace: one guard for every read of them
        crossing = t.cluster in c._shared_stores and not t.labels
        if crossing:
            out.rooms[t.cluster] = {"nodes": [], "problem": SHARED_STORE}
        else:
            try:
                out.rooms[t.cluster] = rooms(prom, t, end)
            except UsageError as e:
                out.rooms[t.cluster] = {"nodes": [], "problem": str(e)}
        if c.list_prices:
            if crossing:
                out.nodes[t.cluster] = {"nodes": [], "problem": SHARED_STORE}
            else:
                try:
                    out.nodes[t.cluster] = nodes(prom, t, end, cluster.provider)
                except UsageError as e:
                    out.nodes[t.cluster] = {"nodes": [], "problem": str(e)}
        for w, found in by_window.items():
            rows = sorted(
                found.rows, key=lambda r: (r.workload.namespace, r.workload.name, r.container)
            )
            out.by_window[w] += [(t.cluster, r) for r in rows]
        found = by_window[windows[-1]]
        for metric, what in missing.items():
            level = "missing" if metric in REQUIRED else "optional, missing"
            out.notes.append(f"{c.id}/{t.cluster}: {level}: {metric} ({what})")
        if found.fallback_error:
            out.notes.append(f"{c.id}/{t.cluster}: Kubernetes API: {found.fallback_error}")
        if found.unknown:
            out.notes.append(f"{c.id}/{t.cluster}: no Workload for {len(found.unknown)} pods")
    return out
