"""Workload usage for Sizings (slice 9.1): scoping, owners, pooling, the metric check."""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from kubernetes import client as k
from pydantic import SecretStr, ValidationError

from squero import contracts, usage
from squero.usage import Target, Workload
from tests.conftest import make_contract
from tests.fake_prometheus import FakePrometheus

DAY = 86_400
SHOP = Target("kind", "http://prometheus", None, {}, ["shop"])


def prom(fake: FakePrometheus) -> usage.Prom:
    return usage.Prom(SHOP, fake.client())


def owner(
    fake: FakePrometheus, pod: str, kind: str, name: str, ns: str = "shop", **extra: str
) -> None:
    fake.add("kube_pod_owner", 1, namespace=ns, pod=pod, owner_kind=kind, owner_name=name, **extra)


def rs(fake: FakePrometheus, replicaset: str, kind: str, name: str, ns: str = "shop") -> None:
    fake.add(
        "kube_replicaset_owner",
        1,
        namespace=ns,
        replicaset=replicaset,
        owner_kind=kind,
        owner_name=name,
    )


def cpu(
    fake: FakePrometheus,
    pod: str,
    values: list[float],
    container: str = "app",
    ns: str = "shop",
    **extra: str,
) -> None:
    fake.add(
        "container_cpu_usage_seconds_total",
        values,
        namespace=ns,
        pod=pod,
        container=container,
        **extra,
    )


def mem(
    fake: FakePrometheus,
    pod: str,
    values: list[float],
    container: str = "app",
    ns: str = "shop",
    **extra: str,
) -> None:
    fake.add(
        "container_memory_working_set_bytes",
        values,
        namespace=ns,
        pod=pod,
        container=container,
        **extra,
    )


def deployment(
    fake: FakePrometheus, name: str = "checkout", pods: int = 2, ns: str = "shop"
) -> list[str]:
    rs(fake, f"{name}-7d9f", "Deployment", name, ns)
    names = [f"{name}-7d9f-{i}" for i in range(pods)]
    for p in names:
        owner(fake, p, "ReplicaSet", f"{name}-7d9f", ns, owner_is_controller="true")
    return names


# --- Contract schema -------------------------------------------------------------


def test_cluster_prometheus_override_and_labels_make_the_target() -> None:
    c = make_contract(
        clusters={"a": ["shop"], "b": ["shop"]},
        secrets={"thanos": "t0ken"},
    )
    data = c.model_dump()
    data["integrations"].append({"name": "prometheus", "url": "http://shared:9090"})
    data["clusters"][1] |= {
        "prometheus_url": "https://thanos.acme.example",
        "prometheus_token_secret": "thanos",
        "prometheus_labels": {"cluster": "prod-eu"},
    }
    c = contracts.build(data, secrets={"thanos": SecretStr("t0ken")})
    a, b = usage.targets(c)
    assert (a.url, a.token, a.labels) == ("http://shared:9090", None, {})
    assert (b.url, b.labels) == ("https://thanos.acme.example", {"cluster": "prod-eu"})
    assert b.token and b.token.get_secret_value() == "t0ken"
    assert "thanos" in c.secret_refs()  # checked against secrets.sops.yaml at load
    assert contracts.identifiers(c)["thanos.acme.example"] == "Prometheus host"


def test_a_cluster_without_any_prometheus_has_no_target() -> None:
    assert usage.targets(make_contract()) == []


def test_prometheus_token_secret_needs_a_url() -> None:
    with pytest.raises(ValidationError, match="needs prometheus_url"):
        contracts.Cluster(name="a", context="a", namespaces=["x"], prometheus_token_secret="t")


def test_prometheus_label_names_must_be_label_names() -> None:
    with pytest.raises(ValidationError):
        contracts.Cluster(
            name="a", context="a", namespaces=["x"], prometheus_labels={"bad-name": "x"}
        )


# --- Queries ---------------------------------------------------------------------


def test_selector_scopes_namespaces_and_cluster_labels_with_escaping() -> None:
    t = Target("c", "u", None, {"cluster": 'pr"od'}, ["shop", "a.b"])
    q = usage.selector("container_cpu_usage_seconds_total", t)
    assert q == 'container_cpu_usage_seconds_total{namespace=~"shop|a\\\\.b",cluster="pr\\"od"}'


def test_selector_uses_exported_namespace_only_for_kube_state_metrics() -> None:
    t = Target("c", "u", None, {}, ["shop"], ksm_namespace="exported_namespace")
    assert usage.selector("kube_pod_owner", t).startswith("kube_pod_owner{exported_namespace=~")
    assert usage.selector("container_memory_working_set_bytes", t).startswith(
        "container_memory_working_set_bytes{namespace=~"
    )


@pytest.mark.parametrize("window", [3600, DAY, 7 * DAY, 30 * DAY, 90 * DAY])
def test_step_keeps_every_range_query_under_the_point_limit(window: int) -> None:
    step = usage.step_for(window)
    assert step % 60 == 0 and step >= 60
    assert window / step <= usage.MAX_POINTS


def test_step_is_one_minute_when_the_window_allows() -> None:
    assert usage.step_for(DAY) == 60
    assert usage.step_for(30 * DAY) == 300


# --- The metric check ------------------------------------------------------------


def full(fake: FakePrometheus, ns_label: str = "namespace") -> None:
    for metric in usage.REQUIRED | usage.OPTIONAL:
        labels = {ns_label if metric in usage.KSM else "namespace": "shop"}
        if metric.startswith("container_"):
            labels |= {"container": "app", "pod": "p"}
        fake.add(metric, 1, **labels)


def test_check_finds_nothing_missing_on_a_full_setup() -> None:
    fake = FakePrometheus()
    full(fake)
    t, missing = usage.check(prom(fake), SHOP, at=1)
    assert missing == {} and t.ksm_namespace == "namespace"


def test_check_detects_kube_state_metrics_without_honor_labels() -> None:
    fake = FakePrometheus()
    full(fake, ns_label="exported_namespace")
    t, missing = usage.check(prom(fake), SHOP, at=1)
    assert missing == {} and t.ksm_namespace == "exported_namespace"


def test_check_names_what_is_missing() -> None:
    fake = FakePrometheus()
    fake.add("container_cpu_usage_seconds_total", 1, namespace="shop", container="app")
    _, missing = usage.check(prom(fake), SHOP, at=1)
    assert "container_cpu_usage_seconds_total" not in missing
    assert missing["kube_pod_owner"] == "pod owners (kube-state-metrics)"
    assert set(missing) == set(usage.REQUIRED | usage.OPTIONAL) - {
        "container_cpu_usage_seconds_total"
    }


def test_check_does_not_count_another_namespaces_metrics() -> None:
    fake = FakePrometheus()
    for metric in usage.REQUIRED:
        fake.add(metric, 1, namespace="bank", container="app")
    _, missing = usage.check(prom(fake), SHOP, at=1)
    assert set(usage.REQUIRED) <= set(missing)


# --- Owners ----------------------------------------------------------------------


def series(
    key: str, ns: str, name: str, kind: str, owner_name: str, controller: str = "true"
) -> dict[str, Any]:
    return {
        "metric": {
            "namespace": ns,
            key: name,
            "owner_kind": kind,
            "owner_name": owner_name,
            "owner_is_controller": controller,
        }
    }


@pytest.mark.parametrize(
    ("pod_kind", "pod_owner", "rs_owner", "job_owner", "expected"),
    [
        ("ReplicaSet", "web-1", ("Deployment", "web"), None, ("Deployment", "web")),
        ("ReplicaSet", "canary-1", ("Rollout", "canary"), None, ("Rollout", "canary")),
        ("ReplicaSet", "bare-1", ("<none>", "<none>"), None, ("ReplicaSet", "bare-1")),
        ("ReplicaSet", "unknown-rs", None, None, ("ReplicaSet", "unknown-rs")),
        ("Job", "nightly-123", None, ("CronJob", "nightly"), ("CronJob", "nightly")),
        ("Job", "once", None, ("<none>", "<none>"), ("Job", "once")),
        ("StatefulSet", "db", None, None, ("StatefulSet", "db")),
        ("DaemonSet", "agent", None, None, ("DaemonSet", "agent")),
        ("<none>", "<none>", None, None, ("Pod", "p")),
        ("SparkApplication", "etl", None, None, ("SparkApplication", "etl")),
    ],
)
def test_resolve_follows_owners_to_the_workload(
    pod_kind: str, pod_owner: str, rs_owner: Any, job_owner: Any, expected: tuple[str, str]
) -> None:
    pods = [series("pod", "shop", "p", pod_kind, pod_owner)]
    rss = [series("replicaset", "shop", pod_owner, *rs_owner)] if rs_owner else []
    jobs = [series("job_name", "shop", pod_owner, *job_owner)] if job_owner else []
    assert usage.resolve(pods, rss, jobs)[("shop", "p")] == Workload("shop", *expected)


def test_resolve_prefers_the_controller_owner() -> None:
    pods = [
        series("pod", "shop", "p", "ReplicaSet", "web-1", controller="true"),
        series("pod", "shop", "p", "ConfigMap", "cm", controller="false"),
    ]
    rss = [series("replicaset", "shop", "web-1", "Deployment", "web")]
    assert usage.resolve(pods, rss, [])[("shop", "p")] == Workload("shop", "Deployment", "web")
    assert usage.resolve(list(reversed(pods)), rss, [])[("shop", "p")].name == "web"


def test_resolve_reads_exported_namespace_when_told() -> None:
    pod = {
        "metric": {
            "exported_namespace": "shop",
            "pod": "p",
            "owner_kind": "DaemonSet",
            "owner_name": "d",
        }
    }
    assert usage.resolve([pod], [], [])[("shop", "p")].kind == "DaemonSet"


def test_owners_from_the_kubernetes_api() -> None:
    def ref(kind: str, name: str) -> k.V1OwnerReference:
        return k.V1OwnerReference(api_version="v1", kind=kind, name=name, uid="u", controller=True)

    def obj(name: str, *refs: k.V1OwnerReference) -> Any:
        return MagicMock(
            metadata=k.V1ObjectMeta(
                name=name, namespace="shop", owner_references=list(refs) or None
            )
        )

    core, apps, batch = MagicMock(), MagicMock(), MagicMock()
    core.list_namespaced_pod.return_value.items = [
        obj("web-1-a", ref("ReplicaSet", "web-1")),
        obj("nightly-1-x", ref("Job", "nightly-1")),
        obj("lonely"),
    ]
    apps.list_namespaced_replica_set.return_value.items = [obj("web-1", ref("Deployment", "web"))]
    batch.list_namespaced_job.return_value.items = [obj("nightly-1", ref("CronJob", "nightly"))]
    owners = usage.owners_from_api(core, apps, batch, ["shop"])
    assert owners == {
        ("shop", "web-1-a"): Workload("shop", "Deployment", "web"),
        ("shop", "nightly-1-x"): Workload("shop", "CronJob", "nightly"),
        ("shop", "lonely"): Workload("shop", "Pod", "lonely"),
    }
    core.list_namespaced_pod.assert_called_once_with("shop")


# --- Usage -----------------------------------------------------------------------

FLAT = [0.2] * 6
DIURNAL = [0.1, 0.3, 0.8, 0.9, 0.4, 0.1]
SPIKE = [0.1, 0.1, 2.5, 0.1, 0.1, 0.1]
LEAK = [100e6, 140e6, 180e6, 220e6, 260e6, 300e6]


def run(
    fake: FakePrometheus, t: Target = SHOP, **kw: Any
) -> tuple[dict[tuple[str, str], usage.ContainerUsage], list[str]]:
    found = usage.usage(usage.Prom(t, fake.client()), t, DAY, end=DAY, **kw)
    return {(r.workload.name, r.container): r for r in found.rows}, found.unknown


@pytest.mark.parametrize("shape", [FLAT, DIURNAL, SPIKE])
def test_cpu_samples_of_every_pod_are_pooled_not_summed(shape: list[float]) -> None:
    fake = FakePrometheus()
    a, b = deployment(fake)
    cpu(fake, a, shape)
    cpu(fake, b, [v / 2 for v in shape])
    rows, unknown = run(fake)
    r = rows[("checkout", "app")]
    assert sorted(v for _, v in r.cpu) == sorted(shape + [v / 2 for v in shape])
    assert max(v for _, v in r.cpu) == max(shape)  # a per-replica peak, never a+b
    assert r.pods == {a, b} and r.workload == Workload("shop", "Deployment", "checkout")
    assert unknown == []


def test_memory_keeps_a_slow_leak_and_containers_stay_apart() -> None:
    fake = FakePrometheus()
    (p,) = deployment(fake, pods=1)
    mem(fake, p, LEAK)
    mem(fake, p, [5e6] * 6, container="sidecar")
    rows, _ = run(fake)
    assert [v for _, v in rows[("checkout", "app")].memory] == LEAK
    assert [v for _, v in rows[("checkout", "sidecar")].memory] == [5e6] * 6


def test_pause_and_pod_level_cadvisor_series_are_left_out() -> None:
    fake = FakePrometheus()
    (p,) = deployment(fake, pods=1)
    cpu(fake, p, FLAT)
    cpu(fake, p, [9.0] * 6, container="POD")
    cpu(fake, p, [9.0] * 6, container="")
    rows, _ = run(fake)
    assert set(rows) == {("checkout", "app")}
    assert all(
        'container!=""' in q and 'container!="POD"' in q
        for q in fake.queries
        if "container_cpu_usage" in q
    )


def test_requests_limits_oom_and_throttling() -> None:
    fake = FakePrometheus()
    a, b = deployment(fake)
    for p in (a, b):
        mem(fake, p, LEAK)
        fake.add(
            "kube_pod_container_resource_requests",
            0.5,
            namespace="shop",
            pod=p,
            container="app",
            resource="cpu",
        )
        fake.add(
            "kube_pod_container_resource_requests",
            128e6,
            namespace="shop",
            pod=p,
            container="app",
            resource="memory",
        )
        fake.add(
            "kube_pod_container_resource_requests",
            1,
            namespace="shop",
            pod=p,
            container="app",
            resource="nvidia_com_gpu",
        )
        fake.add(
            "kube_pod_container_resource_limits",
            256e6,
            namespace="shop",
            pod=p,
            container="app",
            resource="memory",
        )
    fake.add(
        "kube_pod_container_status_last_terminated_reason",
        1,
        namespace="shop",
        pod=a,
        container="app",
        reason="OOMKilled",
    )
    fake.add(
        "kube_pod_container_status_last_terminated_reason",
        1,
        namespace="shop",
        pod=b,
        container="app",
        reason="Error",
    )
    fake.add(
        "kube_pod_container_status_last_terminated_reason",
        0,
        namespace="shop",
        pod=b,
        container="app",
        reason="OOMKilled",
    )
    fake.add(
        "container_cpu_cfs_throttled_periods_total", 30, namespace="shop", pod=a, container="app"
    )
    fake.add("container_cpu_cfs_periods_total", 100, namespace="shop", pod=a, container="app")
    fake.add("container_cpu_cfs_periods_total", 100, namespace="shop", pod=b, container="app")
    rows, _ = run(fake)
    r = rows[("checkout", "app")]
    assert r.requests == {"cpu": 0.5, "memory": 128e6}
    assert r.limits == {"memory": 256e6}
    assert r.oom_killed_pods == 1
    assert r.throttled == pytest.approx(0.15)


def throttle(fake: FakePrometheus, pod: str, hot: float, periods: float, ago: float = 0) -> None:
    """`pod`'s app ran `periods` CFS periods, `hot` of them throttled, `ago` seconds ago."""
    for metric, n in (("throttled_periods", hot), ("periods", periods)):
        fake.add(f"container_cpu_cfs_{metric}_total", n, ago, namespace="shop", pod=pod,
                 container="app")  # fmt: skip


def test_the_last_hours_throttling_is_read_when_asked_of_a_container_that_ran_enough() -> None:
    """Slice 13.3a, the burst Reflex's trigger."""
    fake = FakePrometheus()
    a, b = deployment(fake)
    pods = {n: deployment(fake, n, pods=1)[0] for n in ("idle", "edge", "old")}
    for p in (a, b, *pods.values()):
        cpu(fake, p, FLAT)
    throttle(fake, a, 300, 400)
    throttle(fake, b, 0, 400)  # its pods pooled: 300 of 800
    throttle(fake, pods["idle"], 500, usage.MIN_PERIODS - 1)  # ran too little to tell
    throttle(fake, pods["edge"], 300, usage.MIN_PERIODS)
    throttle(fake, pods["old"], 9000, 10000, ago=2 * 3600)  # the day's, not the hour's
    rows, _ = run(fake)
    assert all(r.throttled_recent is None for r in rows.values())
    assert not any("[3600s]" in q for q in fake.queries)  # two reads nobody asked for
    rows, _ = run(fake, recent=True)
    assert rows[("checkout", "app")].throttled_recent == pytest.approx(0.375)
    assert rows[("edge", "app")].throttled_recent == pytest.approx(0.5)
    idle, old = rows[("idle", "app")], rows[("old", "app")]
    assert idle.throttled_recent is None and idle.throttled is not None  # the window's: no floor
    assert old.throttled_recent is None and old.throttled == pytest.approx(0.9)
    assert usage.RECENT == 3600 and usage.MIN_PERIODS == 600
    hour = [q for q in fake.queries if "[3600s]" in q]
    assert len(hour) == 2 and all(q.startswith("increase(container_cpu_cfs_") for q in hour)
    assert all('namespace=~"shop"' in q or 'namespace="shop"' in q for q in hour)


def test_no_throttling_data_is_none_not_zero() -> None:
    fake = FakePrometheus()
    (p,) = deployment(fake, pods=1)
    cpu(fake, p, FLAT)
    rows, _ = run(fake)
    assert rows[("checkout", "app")].throttled is None


def test_a_pod_nobody_knows_is_reported_not_guessed() -> None:
    fake = FakePrometheus()
    cpu(fake, "mystery-1", FLAT)
    rows, unknown = run(fake)
    assert rows == {} and unknown == ["shop/mystery-1"]


def test_the_api_fills_only_the_pods_kube_state_metrics_misses() -> None:
    fake = FakePrometheus()
    (known,) = deployment(fake, pods=1)
    cpu(fake, known, FLAT)
    cpu(fake, "new-pod", FLAT)
    calls = []

    def fallback() -> dict[tuple[str, str], Workload]:
        calls.append(1)
        return {
            ("shop", "new-pod"): Workload("shop", "StatefulSet", "db"),
            ("shop", known): Workload("shop", "Pod", "wrong"),  # kube-state-metrics wins
        }

    rows, unknown = run(fake, fallback=fallback)
    assert set(rows) == {("checkout", "app"), ("db", "app")} and unknown == [] and calls == [1]


def test_the_api_is_not_asked_when_kube_state_metrics_knows_every_pod() -> None:
    fake = FakePrometheus()
    (p,) = deployment(fake, pods=1)
    cpu(fake, p, FLAT)
    run(fake, fallback=lambda: pytest.fail("asked the API"))


def test_without_kube_state_metrics_the_api_names_every_workload() -> None:
    fake = FakePrometheus()
    cpu(fake, "web-1-a", FLAT)
    rows, unknown = run(
        fake, fallback=lambda: {("shop", "web-1-a"): Workload("shop", "Deployment", "web")}
    )
    assert set(rows) == {("web", "app")} and unknown == []


def test_kube_state_metrics_scraped_without_honor_labels() -> None:
    """Prometheus renames every colliding target label: the pod's namespace, pod and
    container land in exported_*, while namespace/pod/container name kube-state-metrics."""
    fake = FakePrometheus()
    t = Target("kind", "http://prometheus", None, {}, ["shop"], ksm_namespace="exported_namespace")
    ksm = {"namespace": "monitoring", "pod": "kube-state-metrics-0", "container": "ksm"}
    fake.add(
        "kube_pod_owner",
        1,
        exported_namespace="shop",
        exported_pod="p",
        owner_kind="DaemonSet",
        owner_name="agent",
        **{k: v for k, v in ksm.items() if k != "container"},
    )
    cpu(fake, "p", FLAT)
    fake.add(
        "kube_pod_container_resource_requests",
        0.25,
        exported_namespace="shop",
        exported_pod="p",
        exported_container="app",
        resource="cpu",
        **ksm,
    )
    fake.add(
        "kube_pod_container_status_last_terminated_reason",
        1,
        exported_namespace="shop",
        exported_pod="p",
        exported_container="app",
        reason="OOMKilled",
        **ksm,
    )
    rows, unknown = run(fake, t)
    r = rows[("agent", "app")]
    assert r.requests == {"cpu": 0.25} and r.oom_killed_pods == 1 and unknown == []


def test_prometheus_errors_raise() -> None:
    fake = FakePrometheus(status=503)
    with pytest.raises(usage.UsageError, match="503"):
        run(fake)


# --- Isolation -------------------------------------------------------------------


def test_one_contract_never_sees_another_contracts_series(
    two_contracts: dict[str, contracts.Contract],
) -> None:
    """demo-shop and demo-bank on one shared store: each sees only its namespaces."""
    fake = FakePrometheus()
    deployment(fake, "checkout", pods=1, ns="demo")
    deployment(fake, "ledger", pods=1, ns="bank")
    cpu(fake, "checkout-7d9f-0", FLAT, ns="demo")
    cpu(fake, "ledger-7d9f-0", [7.0] * 6, ns="bank")
    for cid, mine, theirs in (
        ("demo-shop", "checkout", "ledger"),
        ("demo-bank", "ledger", "checkout"),
    ):
        (t,) = usage.targets(two_contracts[cid])
        fake.queries.clear()
        rows, unknown = run(fake, t)
        assert {w for w, _ in rows} == {mine} and unknown == []
        assert all(theirs not in str(r) for r in rows.values())
        ns = "|".join(t.namespaces)
        assert fake.queries and all(f'namespace=~"{ns}"' in q for q in fake.queries)


def test_cluster_labels_keep_two_clusters_apart_on_one_store() -> None:
    fake = FakePrometheus()
    for cluster, value in (("prod-eu", 0.2), ("prod-us", 5.0)):
        fake.add(
            "kube_pod_owner",
            1,
            namespace="shop",
            pod="p",
            owner_kind="StatefulSet",
            owner_name="db",
            cluster=cluster,
        )
        cpu(fake, "p", [value] * 6, cluster=cluster)
    eu = Target("eu", "http://thanos", None, {"cluster": "prod-eu"}, ["shop"])
    rows, _ = run(fake, eu)
    assert [v for _, v in rows[("db", "app")].cpu] == [0.2] * 6
    assert all('cluster="prod-eu"' in q for q in fake.queries)


def test_mid_rollout_the_larger_request_counts() -> None:
    fake = FakePrometheus()
    a, b = deployment(fake)
    for p, req in ((a, 1.0), (b, 0.25)):
        cpu(fake, p, FLAT)
        fake.add(
            "kube_pod_container_resource_requests",
            req,
            namespace="shop",
            pod=p,
            container="app",
            resource="cpu",
        )
    rows, _ = run(fake)
    assert rows[("checkout", "app")].requests == {"cpu": 1.0}


def test_after_a_rollout_the_live_pods_request_counts() -> None:
    """The old pod asked 1 core and is gone an hour; the new one asks 115m: Realized
    Savings sees the drop the next run, not when the old pod leaves the window
    (slice 10.3b). With no live pod, the window's request stands (a Deployment scaled
    to 0; a CronJob's is its newest run's, below)."""
    fake = FakePrometheus()
    a, b = deployment(fake)
    for p, req, ago in ((a, 1.0, 3600), (b, 0.115, 0)):
        cpu(fake, p, FLAT)
        for metric, res, value in (("requests", "cpu", req), ("limits", "memory", req * 2)):
            fake.add(
                f"kube_pod_container_resource_{metric}",
                value,
                ago=ago,
                namespace="shop",
                pod=p,
                container="app",
                resource=res,
            )
    rows, _ = run(fake)
    assert rows[("checkout", "app")].requests == {"cpu": 0.115}
    assert rows[("checkout", "app")].limits == {"memory": 0.23}
    assert rows[("checkout", "app")].live
    gone = FakePrometheus()
    (c, _) = deployment(gone)
    cpu(gone, c, FLAT)
    gone.add(
        "kube_pod_container_resource_requests",
        0.5,
        ago=3600,
        namespace="shop",
        pod=c,
        container="app",
        resource="cpu",
    )
    rows, _ = run(gone)
    assert rows[("checkout", "app")].requests == {"cpu": 0.5}
    assert not rows[("checkout", "app")].live


def between_runs(
    fake: FakePrometheus, owned: str, runs: list[tuple[str, float | None, float]], **extra: str
) -> None:
    """Runs of a Job (of `owned`: a CronJob, or none) gone before the last 5 minutes:
    (pod, kube_pod_created or None, its CPU request and twice that as memory limit)."""
    for i, (p, created, req) in enumerate(runs):
        job = p.rsplit("-", 1)[0]
        kind, name = ("CronJob", owned) if owned else ("<none>", "<none>")
        fake.add(
            "kube_job_owner",
            1,
            namespace="shop",
            job_name=job,
            owner_kind=kind,
            owner_name=name,
            **extra,
        )
        owner(fake, p, "Job", job, owner_is_controller="true", **extra)
        cpu(fake, p, FLAT, **extra)
        if created is not None:
            fake.add(
                "kube_pod_created", created, ago=3600 * (i + 1), namespace="shop", pod=p, **extra
            )
        for metric, res, value in (("requests", "cpu", req), ("limits", "memory", req * 2)):
            fake.add(
                f"kube_pod_container_resource_{metric}",
                value,
                ago=3600 * (i + 1),
                namespace="shop",
                pod=p,
                container="app",
                resource=res,
                **extra,
            )


def test_a_cronjob_between_runs_asks_what_its_newest_run_asked() -> None:
    """Its newest run by kube_pod_created, not the window's largest, so a lowered
    request realizes before the old runs leave the window (audit A11)."""
    fake = FakePrometheus()
    between_runs(
        fake,
        "nightly",
        [("nightly-200-b", 2000, 0.25), ("nightly-100-a", 1000, 1.0), ("nightly-300-c", 500, 2.0)],
    )
    rows, _ = run(fake)
    r = rows[("nightly", "app")]
    assert r.requests == {"cpu": 0.25} and r.limits == {"memory": 0.5} and r.live
    other = FakePrometheus()  # a newer pod of the same name elsewhere changes nothing
    runs: list[tuple[str, float | None, float]] = [
        ("nightly-200-b", 2000, 0.25),
        ("nightly-100-a", 1000, 1.0),
    ]
    between_runs(other, "nightly", runs, cluster="kind")
    for ns, cl in (("bank", "kind"), ("shop", "other")):
        other.add("kube_pod_created", 9000, namespace=ns, pod="nightly-100-a", cluster=cl)
    rows, _ = run(other, Target("kind", "http://p", None, {"cluster": "kind"}, ["shop"]))
    assert rows[("nightly", "app")].requests == {"cpu": 0.25}
    unknown = FakePrometheus()  # no kube_pod_created: the last name
    between_runs(unknown, "nightly", [("nightly-1-a", None, 0.25), ("nightly-2-b", None, 0.5)])
    rows, _ = run(unknown)
    assert rows[("nightly", "app")].requests == {"cpu": 0.5}


def test_only_a_cronjob_reads_its_newest_pod_between_runs() -> None:
    fake = FakePrometheus()
    between_runs(fake, "", [("once-b", 2000, 0.25), ("once-a", 1000, 1.0)])  # a retry
    a, b = deployment(fake, "web")
    for p, created, req in ((a, 1000, 1.0), (b, 2000, 0.25)):
        cpu(fake, p, FLAT)
        fake.add("kube_pod_created", created, ago=3600, namespace="shop", pod=p)
        fake.add(
            "kube_pod_container_resource_requests",
            req,
            ago=3600,
            namespace="shop",
            pod=p,
            container="app",
            resource="cpu",
        )
    rows, _ = run(fake)
    for name in ("once", "web"):  # a lone Job, a Deployment scaled to 0: the window's
        assert rows[(name, "app")].requests == {"cpu": 1.0} and not rows[(name, "app")].live


def test_api_owners_prefer_the_controller_reference() -> None:
    other = k.V1OwnerReference(api_version="v1", kind="Foo", name="x", uid="1", controller=False)
    ctrl = k.V1OwnerReference(
        api_version="v1", kind="StatefulSet", name="db", uid="2", controller=True
    )
    pod = MagicMock(
        metadata=k.V1ObjectMeta(name="db-0", namespace="shop", owner_references=[other, ctrl])
    )
    core, apps, batch = MagicMock(), MagicMock(), MagicMock()
    core.list_namespaced_pod.return_value.items = [pod]
    apps.list_namespaced_replica_set.return_value.items = []
    batch.list_namespaced_job.return_value.items = []
    owners = usage.owners_from_api(core, apps, batch, ["shop"])
    assert owners[("shop", "db-0")] == Workload("shop", "StatefulSet", "db")


def test_prometheus_does_the_time_math() -> None:
    """rate() handles counter resets on restarts; max_over_time() keeps memory peaks
    between steps. The fake ignores functions, so the query text is the evidence."""
    fake = FakePrometheus()
    run(fake)
    ranges = [q for q in fake.queries if "container_cpu_usage" in q or "working_set" in q]
    assert any(
        q.startswith("rate(container_cpu_usage_seconds_total{") and q.endswith("[300s])")
        for q in ranges
    )
    assert any(
        q.startswith("max_over_time(container_memory_working_set_bytes{") and q.endswith("[60s])")
        for q in ranges
    )


# --- squero sizing usage ------------------------------------------------------------


@pytest.mark.parametrize(("text", "seconds"), [("90m", 5400), ("6h", 21600), ("7d", 604800)])
def test_window_seconds(text: str, seconds: int) -> None:
    from squero import cli

    assert cli.window_seconds(text) == seconds


@pytest.mark.parametrize("bad", ["0d", "7", "1w", "-1h", "1.5h"])
def test_window_seconds_refuses_anything_else(bad: str) -> None:
    from squero import cli

    with pytest.raises(SystemExit):
        cli.window_seconds(bad)


def test_sizing_usage_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from squero import cli

    fake = FakePrometheus()
    (p,) = deployment(fake, pods=1)
    cpu(fake, p, DIURNAL)
    mem(fake, p, LEAK)
    data = make_contract().model_dump()
    data["integrations"].append({"name": "prometheus", "url": "http://prometheus"})
    monkeypatch.setattr(cli, "contract", lambda settings, cid: contracts.build(data))
    monkeypatch.setattr(usage, "Prom", lambda t: _prom(fake, t))
    assert cli.main(["sizing", "usage", "--contract", "acme", "--window", "1d", "--json"]) == 0
    out, err = capsys.readouterr()
    (row,) = __import__("json").loads(out)
    assert row["workload"] == "test/shop/Deployment/checkout/app" and row["cpu_max"] == 0.9
    assert row["memory_max_mib"] == round(300e6 / 2**20, 1)
    assert "missing: kube_pod_container_resource_requests" in err


def test_sizing_usage_without_any_prometheus_says_so(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from squero import cli

    monkeypatch.setattr(cli, "contract", lambda settings, cid: make_contract())
    assert cli.main(["sizing", "usage", "--contract", "acme"]) == 1
    assert "no cluster has a Prometheus" in capsys.readouterr().out


REAL_PROM = usage.Prom


def _prom(fake: FakePrometheus, t: Target) -> usage.Prom:
    return REAL_PROM(t, fake.client())


def test_running_pods_come_from_their_phase_in_the_targets_namespaces() -> None:
    """A time per Running pod per step; a step it wasn't Running (0), another phase or
    another namespace's pod of the same name doesn't count."""
    fake = FakePrometheus()
    (p,) = deployment(fake, pods=1)
    cpu(fake, p, FLAT)
    fake.add("kube_pod_status_phase", [1, 1, 0, 0, 1, 1], namespace="shop", pod=p, phase="Running")
    fake.add("kube_pod_status_phase", [0, 0, 1, 1, 0, 0], namespace="shop", pod=p, phase="Pending")
    fake.add("kube_pod_status_phase", [1] * 6, namespace="bank", pod=p, phase="Running")
    found = usage.usage(prom(fake), SHOP, DAY, end=DAY)
    (row,) = found.rows
    step = usage.step_for(DAY)
    assert row.running == [i * step for i in (0, 1, 4, 5)]


def test_a_failing_api_fallback_leaves_the_rest_read() -> None:
    fake = FakePrometheus()
    (known,) = deployment(fake, pods=1)
    cpu(fake, known, FLAT)
    cpu(fake, "new-pod", FLAT)

    def forbidden() -> dict[tuple[str, str], Workload]:
        raise PermissionError("pods is forbidden")

    found = usage.usage(usage.Prom(SHOP, fake.client()), SHOP, DAY, end=DAY, fallback=forbidden)
    assert [r.workload.name for r in found.rows] == ["checkout"]
    assert found.unknown == ["shop/new-pod"]
    assert found.fallback_error == "PermissionError: pods is forbidden"


# --- One Prometheus, several clusters ------------------------------------------------


def shared(
    cid: str, *clusters: dict[str, Any], url: str = "http://thanos:9090"
) -> contracts.Contract:
    data = make_contract(contract_id=cid).model_dump()
    data["clusters"] = [{"name": c["name"], "context": c["name"], **c} for c in clusters]
    data["integrations"].append({"name": "prometheus", "url": url})
    return contracts.build(data)


def test_two_clusters_on_one_prometheus_need_labels_to_tell_them_apart() -> None:
    c = shared(
        "acme", {"name": "eu", "namespaces": ["shop"]}, {"name": "us", "namespaces": ["shop", "x"]}
    )
    (problem,) = contracts._shared_metrics(Path("dir"), [c])
    assert "acme/eu and acme/us read the same Prometheus" in problem and "['shop']" in problem


def test_two_contracts_on_one_store_with_a_common_namespace_are_refused() -> None:
    a = shared("acme", {"name": "prod", "namespaces": ["default"]})
    b = shared("globex", {"name": "prod", "namespaces": ["default"]}, url="http://thanos:9090/")
    (problem,) = contracts._shared_metrics(Path("dir"), [a, b])
    assert "acme/prod and globex/prod" in problem


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ({"cluster": "eu"}, {"cluster": "us"}),
        ({"cluster": "eu", "env": "prod"}, {"cluster": "us"}),
    ],
)
def test_one_label_with_two_values_keeps_them_apart(
    first: dict[str, str], second: dict[str, str]
) -> None:
    c = shared(
        "acme",
        {"name": "eu", "namespaces": ["shop"], "prometheus_labels": first},
        {"name": "us", "namespaces": ["shop"], "prometheus_labels": second},
    )
    assert contracts._shared_metrics(Path("dir"), [c]) == []


@pytest.mark.parametrize(
    "second",
    [
        {"name": "us", "namespaces": ["other"]},
        {"name": "us", "namespaces": ["shop"], "prometheus_url": "http://us-prometheus:9090"},
        {"name": "us", "namespaces": ["shop"], "prometheus_url": "https://thanos:9090"},
    ],
)
def test_other_namespaces_or_another_store_are_fine(second: dict[str, Any]) -> None:
    c = shared("acme", {"name": "eu", "namespaces": ["shop"]}, second)
    assert contracts._shared_metrics(Path("dir"), [c]) == []


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (
            {"cluster": "eu"},
            {"cluster": "eu", "env": "prod"},
        ),  # a subset matches the superset's series
        ({}, {"cluster": "us"}),  # no labels match every cluster's series
    ],
)
def test_labels_that_can_match_the_same_series_are_refused(
    first: dict[str, str], second: dict[str, str]
) -> None:
    a = shared("acme", {"name": "a", "namespaces": ["shop"], "prometheus_labels": first})
    b = shared("globex", {"name": "b", "namespaces": ["shop"], "prometheus_labels": second})
    (problem,) = contracts._shared_metrics(Path("dir"), [a, b])
    assert "acme/a and globex/b" in problem


@pytest.mark.parametrize(
    ("url_a", "url_b"),
    [
        ("http://Thanos:9090", "http://thanos:9090/"),
        ("http://thanos", "http://thanos:80"),
        ("HTTPS://thanos.example/prom/", "https://THANOS.example:443/prom"),
    ],
)
def test_one_store_written_two_ways_is_one_store(url_a: str, url_b: str) -> None:
    a = shared("acme", {"name": "a", "namespaces": ["shop"]}, url=url_a)
    b = shared("globex", {"name": "b", "namespaces": ["shop"]}, url=url_b)
    assert len(contracts._shared_metrics(Path("dir"), [a, b])) == 1


def test_scrape_style_is_asked_of_the_store_not_the_contracts_namespaces() -> None:
    """kube-state-metrics runs in `monitoring`; a Contract owning `monitoring` must not
    mistake every pod's series (namespace=monitoring by scrape) for its own."""
    fake = FakePrometheus()
    full(fake, ns_label="exported_namespace")
    fake.add("kube_pod_owner", 1, namespace="monitoring", exported_namespace="bank", pod="x")
    owns_monitoring = Target("kind", "http://prometheus", None, {}, ["shop", "monitoring"])
    t, _ = usage.check(usage.Prom(owns_monitoring, fake.client()), owns_monitoring, at=1)
    assert t.ksm_namespace == "exported_namespace"


def test_scrape_style_probe_stays_within_the_clusters_labels() -> None:
    fake = FakePrometheus()
    full(fake)
    fake.add(
        "kube_pod_owner", 1, namespace="monitoring", exported_namespace="shop", cluster="other"
    )
    eu = Target("eu", "http://thanos", None, {"cluster": "eu"}, ["shop"])
    t, _ = usage.check(usage.Prom(eu, fake.client()), eu, at=1)
    assert t.ksm_namespace == "namespace"


def test_load_refuses_contracts_that_would_mix_usage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import shutil

    from tests.conftest import DEMO_CONTRACTS, use_age_keys

    use_age_keys(monkeypatch, tmp_path, "demo-shop", "demo-bank")
    copy = tmp_path / "contracts"
    shutil.copytree(DEMO_CONTRACTS, copy, ignore=shutil.ignore_patterns("test-keys"))
    bank = copy / "demo-bank" / "contract.yaml"
    bank.write_text(
        bank.read_text()
        .replace("http://localhost:9091", "http://localhost:9090")
        .replace("namespaces: [bank]", "namespaces: [bank, demo]")
    )
    mixed = "demo-bank/bank-kind and demo-shop/kind read the same Prometheus"
    with pytest.raises(contracts.ContractError, match=mixed):
        contracts.load(copy)


def test_sizing_show_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two days of usage at 0.1 core and 80 MiB against 1 CPU and 1 GiB is lowered; a
    pod with ten samples (18 minutes at production's 2-minute step) is still learning."""
    from squero import cli

    fake = FakePrometheus()
    (old,) = deployment(fake, "api", pods=1)
    (new,) = deployment(fake, "fresh", pods=1)
    cpu(fake, old, [0.1] * 1441)
    mem(fake, old, [80 * 2**20] * 1441)
    cpu(fake, new, [0.1] * 10)
    for p in (old, new):
        for res, value in (("cpu", 1.0), ("memory", 2.0**30)):
            fake.add(
                "kube_pod_container_resource_requests",
                value,
                namespace="shop",
                pod=p,
                container="app",
                resource=res,
            )
    data = make_contract().model_dump()
    data["integrations"].append({"name": "prometheus", "url": "http://prometheus"})
    monkeypatch.setattr(cli, "contract", lambda settings, cid: contracts.build(data))
    monkeypatch.setattr(usage, "Prom", lambda t: _prom(fake, t))
    assert cli.main(["sizing", "show", "--contract", "acme"]) == 0
    out = capsys.readouterr().out
    assert (
        "test/shop/Deployment/api/app (production): cpu lower 1000m -> 115m, memory lower "
        "1024Mi -> 96Mi" in out
    )
    assert "test/shop/Deployment/fresh/app (production): learning (0.3 h of 24 h)" in out
