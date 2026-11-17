import json
import os
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from kubernetes import client as k
from pydantic import SecretStr

from squero import agent, contracts, evals
from squero.config import Settings
from squero.servers import kubernetes
from tests.conftest import EXEC_USER, NOW, crashlooping_pod, kubeconfig, make_contract


def k8s(settings: Settings, fake_cluster: Any, actions: bool = False, **contract: Any) -> FastMCP:
    c = make_contract(enable_actions=actions, **contract)
    apis = {cl.name: fake_cluster for cl in c.clusters}
    k = c.integrations[0]
    assert isinstance(k, contracts.Kubernetes)
    return kubernetes.build_server(c, k, settings.tool_output_limit, apis)


@pytest.fixture(autouse=True)
def _clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ages are measured from NOW, the time the fixtures were built from: on the real
    clock a slow suite reaching these tests a minute late reads "5m" for "4m"."""
    monkeypatch.setattr(kubernetes, "_now", lambda: NOW)


def test_kubeconfig_secret_is_parsed_in_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        kubernetes.config,
        "new_client_from_config_dict",
        lambda d, context: seen.update(d=d, ctx=context),
    )
    monkeypatch.setattr(
        kubernetes.config, "new_client_from_config", lambda **_: pytest.fail("file")
    )
    data = {
        "id": "acme",
        "name": "Acme",
        "clusters": [
            {"name": "prod", "context": "p", "namespaces": ["shop"], "kubeconfig_secret": "kc"}
        ],
        "integrations": [{"name": "kubernetes"}],
    }
    c = contracts.build(data, secrets={"kc": SecretStr("apiVersion: v1\nkind: Config\n")})
    kubernetes.api_client(c.clusters[0], c)
    assert seen == {"d": {"apiVersion": "v1", "kind": "Config"}, "ctx": "p"}


@pytest.mark.anyio
async def test_an_only_cluster_is_named_outright(settings: Settings, fake_cluster: Any) -> None:
    """The alert's label may name it differently: no hunting for a match."""
    async with Client(k8s(settings, fake_cluster)) as c:
        tools = {t.name: t for t in await c.list_tools()}
    cluster = tools["list_pods"].input_schema["properties"]["cluster"]
    assert cluster["description"] == (
        "This Contract's only cluster: always pass 'test'. Namespaces: shop."
    )
    two = k8s(settings, fake_cluster, clusters={"prod": ["shop", "pay"], "stg": ["shop-stg"]})
    async with Client(two) as c:
        tools = {t.name: t for t in await c.list_tools()}
    assert (
        tools["list_pods"]
        .input_schema["properties"]["cluster"]["description"]
        .endswith("One of: prod (namespaces: pay, shop); stg (namespaces: shop-stg)")
    )


@pytest.mark.anyio
async def test_cluster_and_namespace_are_checked_together(
    settings: Settings, fake_cluster: Any
) -> None:
    server = k8s(settings, fake_cluster, clusters={"prod": ["shop"], "stg": ["shop-stg"]})
    async with Client(server) as c:
        tools = {t.name: t for t in await c.list_tools()}
        cluster = tools["list_pods"].input_schema["properties"]["cluster"]
        assert cluster["enum"] == ["prod", "stg"]
        assert "use the alert's cluster label" in cluster["description"]
        await c.call_tool("list_pods", {"cluster": "prod", "namespace": "shop"})
        with pytest.raises(ToolError, match="'shop' is not allowed in cluster 'stg'"):
            await c.call_tool("list_pods", {"cluster": "stg", "namespace": "shop"})
        with pytest.raises(
            ToolError, match=r"not one of this Contract's clusters: \['prod', 'stg'\]"
        ):
            await c.call_tool("list_pods", {"cluster": "bank-kind", "namespace": "shop"})
    assert fake_cluster[0].list_namespaced_pod.call_count == 1


@pytest.mark.anyio
async def test_namespace_allowlist_is_enforced(settings: Settings, fake_cluster: Any) -> None:
    async with Client(k8s(settings, fake_cluster)) as c:
        with pytest.raises(ToolError, match="not allowed"):
            await c.call_tool("list_pods", {"cluster": "test", "namespace": "kube-system"})
    fake_cluster[0].list_namespaced_pod.assert_not_called()


@pytest.mark.anyio
async def test_list_pods_surfaces_crash_and_oom(settings: Settings, fake_cluster: Any) -> None:
    async with Client(k8s(settings, fake_cluster)) as c:
        pods = (
            await c.call_tool("list_pods", {"cluster": "test", "namespace": "shop"})
        ).structured_content
    pod = pods["result"][0]
    assert pod["restarts"] == 7
    assert pod["containers"]["app"]["waiting"] == "CrashLoopBackOff"
    assert pod["last_terminated"]["app"]["terminated"] == "OOMKilled"
    assert pod["last_terminated"]["app"]["exit_code"] == 137


@pytest.mark.anyio
async def test_list_pods_names_each_pods_workload_from_its_owners(
    settings: Settings, fake_cluster: Any
) -> None:
    def owned(name: str, labels: dict[str, str], *owners: tuple[str, str, bool]) -> Any:
        p = crashlooping_pod()
        p.metadata.name, p.metadata.labels = name, labels
        p.metadata.owner_references = [
            k.V1OwnerReference(api_version="v1", kind=kd, name=n, uid=n, controller=ctl)
            for kd, n, ctl in owners
        ] or None
        return p

    hashed = {"pod-template-hash": "7d9f"}
    fake_cluster[0].list_namespaced_pod.return_value = k.V1PodList(
        items=[
            owned("web-api-7d9f-abc", hashed, ("ReplicaSet", "web-api-7d9f", True)),
            owned("db-0", {}, ("StatefulSet", "db", True)),
            owned("agent-x7", {}, ("DaemonSet", "agent", True)),
            owned(
                "canary-6b-x",
                {"rollouts-pod-template-hash": "6b"},
                ("ReplicaSet", "canary-6b", True),
            ),
            owned("lone-rs-q", {}, ("ReplicaSet", "lone-rs", True)),
            owned("migrate-q", {}, ("Job", "migrate", True)),
            owned("nightly-x", {}, ("Job", f"nightly-{int(time.time() // 60)}", True)),
            owned("bare", {}),
            owned("adopted", {}, ("Thing", "x", False)),
        ]
    )
    async with Client(k8s(settings, fake_cluster)) as c:
        pods = (
            await c.call_tool("list_pods", {"cluster": "test", "namespace": "shop"})
        ).structured_content
    assert [p["workload"] for p in pods["result"]] == [
        "Deployment/web-api",
        "StatefulSet/db",
        "DaemonSet/agent",
        "Rollout/canary",
        "ReplicaSet/lone-rs",  # no template hash: its controller
        "Job/migrate",
        "CronJob/nightly",  # its Job named for the minute it was scheduled
        "Pod/bare",
        "Pod/adopted",  # an owner that isn't its controller
    ]


@pytest.mark.anyio
async def test_describe_pod_never_returns_env_values(settings: Settings, fake_cluster: Any) -> None:
    async with Client(k8s(settings, fake_cluster)) as c:
        result = await c.call_tool(
            "describe_pod", {"cluster": "test", "namespace": "shop", "name": "checkout-7d9f-abc"}
        )
    text = str(result.structured_content)
    assert "DATABASE_URL" in text and "secret/checkout-creds" in text
    assert "supersecret" not in text
    assert "BackOff" in text  # pod events included


@pytest.mark.anyio
async def test_pod_logs_are_redacted_and_bounded(settings: Settings, fake_cluster: Any) -> None:
    fake_cluster[0].read_namespaced_pod_log.return_value += "x" * 20_000
    async with Client(k8s(settings, fake_cluster)) as c:
        result = await c.call_tool(
            "pod_logs", {"cluster": "test", "namespace": "shop", "name": "p", "previous": True}
        )
    text = result.content[0].text
    assert "abcdefghijklmnop" not in text
    assert len(text) < settings.tool_output_limit + 100
    _, kwargs = fake_cluster[0].read_namespaced_pod_log.call_args
    assert kwargs["previous"] is True


@pytest.mark.anyio
async def test_write_tools_absent_unless_enabled(settings: Settings, fake_cluster: Any) -> None:
    async with Client(k8s(settings, fake_cluster)) as c:
        names = {t.name for t in await c.list_tools()}
    assert "list_pods" in names
    assert not names & {"evict_pod", "scale_deployment"}


@pytest.mark.anyio
async def test_write_tools_marked_destructive_and_bounded(
    settings: Settings, fake_cluster: Any
) -> None:
    async with Client(k8s(settings, fake_cluster, actions=True)) as c:
        tools = {t.name: t for t in await c.list_tools()}
        assert tools["scale_deployment"].annotations.destructive_hint is True
        assert tools["list_pods"].annotations.read_only_hint is True
        with pytest.raises(ToolError, match="between 0 and 10"):
            await c.call_tool(
                "scale_deployment",
                {"cluster": "test", "namespace": "shop", "name": "checkout", "replicas": 500},
            )
        await c.call_tool(
            "evict_pod", {"cluster": "test", "namespace": "shop", "name": "checkout-7d9f-abc"}
        )
    fake_cluster[0].create_namespaced_pod_eviction.assert_called_once()


@pytest.mark.anyio
async def test_evict_refuses_bare_pods_and_reports_pdb(
    settings: Settings, fake_cluster: Any
) -> None:
    from kubernetes.client.exceptions import ApiException

    core = fake_cluster[0]
    async with Client(k8s(settings, fake_cluster, actions=True)) as c:
        core.create_namespaced_pod_eviction.side_effect = ApiException(status=429)
        with pytest.raises(ToolError, match="PodDisruptionBudget"):
            await c.call_tool(
                "evict_pod", {"cluster": "test", "namespace": "shop", "name": "checkout-7d9f-abc"}
            )
        core.read_namespaced_pod.return_value.metadata.owner_references = None
        with pytest.raises(ToolError, match="no controller"):
            await c.call_tool(
                "evict_pod", {"cluster": "test", "namespace": "shop", "name": "checkout-7d9f-abc"}
            )


@pytest.mark.anyio
async def test_pod_logs_retries_restart_race(
    settings: Settings, fake_cluster: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(kubernetes, "LOG_RETRY_SECONDS", 0)
    core = fake_cluster[0]
    core.read_namespaced_pod_log.side_effect = [
        "unable to retrieve container logs for containerd://abc",
        "FATAL: DATABASE_URL is not set",
    ]
    async with Client(k8s(settings, fake_cluster)) as c:
        result = await c.call_tool(
            "pod_logs", {"cluster": "test", "namespace": "shop", "name": "p", "previous": True}
        )
        assert "DATABASE_URL" in result.content[0].text
        # previous gone for good (GC'd while in CrashLoopBackOff): fall back to current
        gone = "unable to retrieve container logs for x"
        core.read_namespaced_pod_log.side_effect = [gone, gone, gone, "FATAL: boom"]
        result = await c.call_tool(
            "pod_logs", {"cluster": "test", "namespace": "shop", "name": "p", "previous": True}
        )
        assert "FATAL: boom" in result.content[0].text
        assert "previous instance's logs are gone" in result.content[0].text
        core.read_namespaced_pod_log.side_effect = None
        core.read_namespaced_pod_log.return_value = gone
        with pytest.raises(ToolError, match="being replaced"):
            await c.call_tool("pod_logs", {"cluster": "test", "namespace": "shop", "name": "p"})


@pytest.mark.anyio
async def test_a_dead_containers_missing_log_file_is_the_same_race_never_shown_as_logs(
    settings: Settings, fake_cluster: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The kubelet's other answer for a crashed run whose log file it already removed
    (seen in CI, 2026-11-12, where it reached the caller as the pod's logs)."""
    monkeypatch.setattr(kubernetes, "LOG_RETRY_SECONDS", 0)
    gone = (
        'failed to try resolving symlinks in path "/var/log/pods/shop_p_1b2c/app/2.log": '
        "lstat /var/log/pods/shop_p_1b2c/app/2.log: no such file or directory"
    )
    asked = {"cluster": "test", "namespace": "shop", "name": "p", "previous": True}
    core = fake_cluster[0]
    core.read_namespaced_pod_log.side_effect = [gone, "FATAL: DATABASE_URL is not set"]
    async with Client(k8s(settings, fake_cluster)) as c:
        result = await c.call_tool("pod_logs", asked)  # there on a second try
        assert result.content[0].text == "FATAL: DATABASE_URL is not set"
        core.read_namespaced_pod_log.side_effect = [gone, gone, gone, "FATAL: boom"]
        result = await c.call_tool("pod_logs", asked)  # gone for good: the latest run's
        assert "previous instance's logs are gone" in result.content[0].text
        assert "FATAL: boom" in result.content[0].text and "symlinks" not in result.content[0].text
        # an app's own line about symlinks is log content
        own = "failed to try resolving symlinks in path /data/in: retrying"
        core.read_namespaced_pod_log.side_effect = [own]
        result = await c.call_tool("pod_logs", asked)
        assert result.content[0].text == own


@pytest.mark.anyio
async def test_previous_logs_the_api_calls_not_found_fall_back_to_the_latest_run(
    settings: Settings, fake_cluster: Any
) -> None:
    """The API server can answer 400 "previous terminated container ... not found" for a
    crash-looping pod (seen in CI, 2026-11-01): that is the same "gone" as the kubelet's
    text, not an error to show the agent."""
    from kubernetes.client.exceptions import ApiException

    def bad_request(message: str) -> ApiException:
        e = ApiException(status=400, reason="Bad Request")
        e.body = json.dumps({"kind": "Status", "message": message, "code": 400})
        return e

    gone = bad_request('previous terminated container "app" in pod "p" not found')
    core = fake_cluster[0]
    core.read_namespaced_pod_log.side_effect = [gone, "FATAL: DATABASE_URL is not set"]
    async with Client(k8s(settings, fake_cluster)) as c:
        result = await c.call_tool(
            "pod_logs", {"cluster": "test", "namespace": "shop", "name": "p", "previous": True}
        )
        assert "DATABASE_URL is not set" in result.content[0].text
        assert "previous instance's logs are gone" in result.content[0].text
        # any other 400 is still an error, and so is this one when the latest run was asked for
        for previous, error in (
            (True, bad_request('container "nope" is not valid')),
            (False, gone),
        ):
            core.read_namespaced_pod_log.side_effect = [error]
            with pytest.raises(ToolError, match="cannot read logs: 400"):
                await c.call_tool(
                    "pod_logs",
                    {"cluster": "test", "namespace": "shop", "name": "p", "previous": previous},
                )


@pytest.mark.anyio
async def test_rollout_history_shows_config_diff(settings: Settings, fake_cluster: Any) -> None:
    from kubernetes import client as k

    def rs(rev: str, env: list[str]) -> Any:
        return k.V1ReplicaSet(
            metadata=k.V1ObjectMeta(
                name=f"checkout-{rev}",
                annotations={"deployment.kubernetes.io/revision": rev},
                owner_references=[
                    k.V1OwnerReference(
                        api_version="apps/v1", kind="Deployment", name="checkout", uid="dep1"
                    )
                ],
            ),
            spec=k.V1ReplicaSetSpec(
                selector=k.V1LabelSelector(),
                template=k.V1PodTemplateSpec(
                    spec=k.V1PodSpec(
                        containers=[
                            k.V1Container(
                                name="app",
                                image="shop/checkout:1.4.0",
                                env=[k.V1EnvVar(name=n, value="secret-value") for n in env],
                            )
                        ]
                    )
                ),
            ),
            status=k.V1ReplicaSetStatus(replicas=1),
        )

    fake_cluster[1].list_namespaced_replica_set.return_value = k.V1ReplicaSetList(
        items=[rs("1", ["DATABASE_URL"]), rs("2", [])]
    )
    async with Client(k8s(settings, fake_cluster)) as c:
        out = (
            await c.call_tool(
                "describe_deployment", {"cluster": "test", "namespace": "shop", "name": "checkout"}
            )
        ).structured_content
    history = {h["revision"]: h["config"]["app"]["env"] for h in out["rollout_history"]}
    assert history == {"2": [], "1": ["DATABASE_URL"]}
    assert "secret-value" not in str(out)


def plugin_cluster(**cluster: Any) -> contracts.Contract:
    data = {
        "id": "acme",
        "name": "Acme",
        "clusters": [{"name": "prod", "context": "p", "namespaces": ["shop"], **cluster}],
        "integrations": [{"name": "kubernetes"}],
    }
    kc = SecretStr(yaml.safe_dump(kubeconfig(EXEC_USER)))
    return contracts.build(data, secrets={"kc": kc})


def test_plugin_in_a_kubeconfig_secret_never_reaches_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded: list[Any] = []
    monkeypatch.setattr(
        kubernetes.config, "new_client_from_config_dict", lambda d, context: loaded.append(d)
    )
    c = plugin_cluster(kubeconfig_secret="kc")
    with pytest.raises(RuntimeError, match=r"Contract 'acme', cluster 'prod'.*exec plugin 'aws'"):
        kubernetes.api_client(c.clusters[0], c)
    assert loaded == []
    c = plugin_cluster(kubeconfig_secret="kc", allow_exec=True)
    kubernetes.api_client(c.clusters[0], c)
    assert len(loaded) == 1


@pytest.mark.parametrize("plugin_first", [True, False])
def test_default_kubeconfig_is_checked_as_kubectl_merges_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, plugin_first: bool
) -> None:
    """KUBECONFIG lists files; the first to define a user wins, as in kubectl."""
    plugin, token = tmp_path / "plugin.yaml", tmp_path / "token.yaml"
    plugin.write_text(yaml.safe_dump(kubeconfig(EXEC_USER)))
    token.write_text(yaml.safe_dump(kubeconfig({"token": "t"})))
    order = [plugin, token] if plugin_first else [token, plugin]
    monkeypatch.setenv("KUBECONFIG", os.pathsep.join(map(str, order)))
    loaded: list[Any] = []
    monkeypatch.setattr(kubernetes.config, "new_client_from_config", lambda **kw: loaded.append(kw))
    c = plugin_cluster()
    if plugin_first:
        with pytest.raises(RuntimeError, match="exec plugin 'aws'"):
            kubernetes.api_client(c.clusters[0], c)
        assert loaded == []
    else:
        kubernetes.api_client(c.clusters[0], c)
        assert loaded == [{"config_file": None, "context": "p"}]


def replica_set(
    deployment: str,
    revision: int,
    minutes_ago: int,
    image: str,
    env: tuple[str, ...] = (),
    chart: str | None = None,
    annotations: dict[str, str] | None = None,
    owner_kind: str = "Deployment",
) -> k.V1ReplicaSet:
    labels = {"app": deployment} | ({"helm.sh/chart": chart} if chart else {})
    container = k.V1Container(
        name="app", image=image, env=[k.V1EnvVar(name=n, value="v4lue") for n in env]
    )
    return k.V1ReplicaSet(
        metadata=k.V1ObjectMeta(
            name=f"{deployment}-r{revision}",
            creation_timestamp=NOW - timedelta(minutes=minutes_ago),
            annotations={"deployment.kubernetes.io/revision": str(revision)} | (annotations or {}),
            owner_references=[
                k.V1OwnerReference(
                    api_version="apps/v1", kind=owner_kind, name=deployment, uid=f"uid-{deployment}"
                )
            ],
        ),
        spec=k.V1ReplicaSetSpec(
            selector=k.V1LabelSelector(match_labels={"app": deployment}),
            template=k.V1PodTemplateSpec(
                metadata=k.V1ObjectMeta(labels=labels), spec=k.V1PodSpec(containers=[container])
            ),
        ),
    )


async def changes(server: FastMCP, **args: Any) -> list[dict[str, Any]]:
    async with Client(server) as c:
        result = await c.call_tool("recent_changes", {"cluster": "test", **args})
    return list(result.structured_content["result"])


@pytest.mark.anyio
async def test_recent_changes_diffs_each_rollout_against_the_revision_before(
    settings: Settings, fake_cluster: Any
) -> None:
    """A dependency's rollout shows up next to the alerting workload's, newest first,
    with what it changed and never an env value."""
    fake_cluster[1].list_namespaced_replica_set.return_value = k.V1ReplicaSetList(
        items=[  # not in revision order: the API doesn't promise one
            replica_set("orders", 2, 3, "py:3.12"),
            replica_set("orders", 1, 30, "py:3.12", env=("DB",)),
            replica_set("inventory", 1, 40, "inv:1.0", env=("PORT",), chart="inv-1.0"),
            replica_set(
                "inventory",
                2,
                4,
                "inv:1.1",
                env=("PORT", "API_VERSION"),
                chart="inv-1.1",
                annotations={
                    "meta.helm.sh/release-name": "inv",
                    "kubernetes.io/change-cause": "deploy token=hunter2hunter2",
                },
            ),
        ]
    )
    out = await changes(k8s(settings, fake_cluster))
    assert [(c["deployment"], c["revision"], c["change"]) for c in out] == [
        ("orders", "2", "rollout"),
        ("inventory", "2", "rollout"),
        ("orders", "1", "created"),
        ("inventory", "1", "created"),
    ]
    assert out[0]["diff"] == {"env_removed": {"app": ["DB"]}}
    assert out[1]["diff"] == {
        "images": {"app": {"from": "inv:1.0", "to": "inv:1.1"}},
        "env_added": {"app": ["API_VERSION"]},
        "helm_chart": {"from": "inv-1.0", "to": "inv-1.1"},
    }
    assert out[1]["helm_release"] == "inv"
    assert out[1]["when"] == "4m" and out[1]["namespace"] == "shop"
    assert "hunter2" not in out[1]["change_cause"]
    assert out[3]["diff"] == {} and "helm_release" not in out[3]
    assert "v4lue" not in str(out)


@pytest.mark.anyio
async def test_recent_changes_keeps_the_window_and_only_deployments(
    settings: Settings, fake_cluster: Any
) -> None:
    fake_cluster[1].list_namespaced_replica_set.return_value = k.V1ReplicaSetList(
        items=[
            replica_set("old", 1, 600, "a:1"),
            replica_set("gap", 7, 5, "b:2"),
            replica_set("argo", 1, 5, "c:1", owner_kind="Rollout"),
        ]
    )
    sidecar = replica_set("mesh", 2, 2, "a:1")
    sidecar.spec.template.spec.containers.append(k.V1Container(name="proxy", image="envoy"))
    fake_cluster[1].list_namespaced_replica_set.return_value.items += [
        replica_set("mesh", 1, 9, "a:1"),
        sidecar,
    ]
    out = await changes(k8s(settings, fake_cluster), since_minutes=60)
    assert [(c["deployment"], c["change"]) for c in out] == [
        ("mesh", "rollout"),
        ("gap", "rollout (the revision before is no longer kept)"),
        ("mesh", "created"),
    ]
    assert out[0]["diff"] == {"containers": {"proxy": "added"}}
    assert out[1]["diff"] == {}
    many = [replica_set(f"d{i}", 1, i, "a:1") for i in range(kubernetes.MAX_CHANGES + 5)]
    fake_cluster[1].list_namespaced_replica_set.return_value = k.V1ReplicaSetList(items=many)
    out = await changes(k8s(settings, fake_cluster))
    assert len(out) == kubernetes.MAX_CHANGES and out[0]["deployment"] == "d0"


SPEC = k.V1DeploymentSpec(selector=k.V1LabelSelector(), template=k.V1PodTemplateSpec())


def progressing(uid: str, message: str | None, minutes_ago: int | None) -> k.V1Deployment:
    """A Deployment "web" whose Progressing condition says `message`, updated then."""
    at = None if minutes_ago is None else NOW - timedelta(minutes=minutes_ago)
    return k.V1Deployment(
        metadata=k.V1ObjectMeta(name="web", uid=uid),
        spec=SPEC,
        status=k.V1DeploymentStatus(
            conditions=[
                k.V1DeploymentCondition(type="Available", status="True"),
                k.V1DeploymentCondition(
                    type="Progressing",
                    status="True",
                    reason="NewReplicaSetAvailable",
                    message=message,
                    last_update_time=at,
                    last_transition_time=NOW - timedelta(days=3),
                ),
            ]
        ),
    )


@pytest.mark.anyio
async def test_a_rollout_undo_is_timed_from_its_deployments_progressing_condition(
    settings: Settings, fake_cluster: Any
) -> None:
    """Audit A11b: an undo reuses an old ReplicaSet (bumped to a new revision, its old one
    in revision-history), so its creation time is the first rollout's. The current one
    takes its Deployment's Progressing update time while that names it, quoted."""
    apps = fake_cluster[1]
    undo = {"deployment.kubernetes.io/revision-history": "1"}
    reused = replica_set("web", 3, 600, "a:1", annotations=undo)
    apps.list_namespaced_replica_set.return_value = k.V1ReplicaSetList(
        items=[reused, replica_set("web", 2, 500, "b:2")]
    )
    server = k8s(settings, fake_cluster)

    def deployments(*items: k.V1Deployment) -> None:
        apps.list_namespaced_deployment.return_value = k.V1DeploymentList(items=list(items))

    deployments(progressing("uid-web", 'ReplicaSet "web-r3" has successfully progressed.', 5))
    (out,) = await changes(server, since_minutes=60)
    assert (out["deployment"], out["revision"], out["change"], out["when"]) == (
        "web",
        "3",
        "rollout undo",
        "5m",
    )
    assert out["diff"] == {"images": {"app": {"from": "b:2", "to": "a:1"}}}

    # Not timed: another ReplicaSet named, one whose name only starts with it, another
    # Deployment of the same name, no Deployment, a Deployment with no status.
    for d in (
        progressing("uid-web", 'ReplicaSet "web-r2" is progressing.', 5),
        progressing("uid-web", 'ReplicaSet "web-r30" is progressing.', 5),
        progressing("uid-other", 'ReplicaSet "web-r3" is progressing.', 5),
        k.V1Deployment(metadata=k.V1ObjectMeta(name="web", uid="uid-web"), spec=SPEC),
    ):
        deployments(d)
        assert await changes(server, since_minutes=60) == []
    deployments()
    assert await changes(server, since_minutes=60) == []

    # An older undo, superseded since, keeps its creation time even while the condition
    # still names it; one with no update time keeps it too, and is still an undo.
    deployments(progressing("uid-web", 'ReplicaSet "web-r3" is progressing.', 5))
    apps.list_namespaced_replica_set.return_value.items.append(replica_set("web", 4, 2, "c:3"))
    assert [c["revision"] for c in await changes(server, since_minutes=60)] == ["4"]
    apps.list_namespaced_replica_set.return_value.items[:] = [
        replica_set("web", 3, 10, "a:1", annotations=undo),
        replica_set("web", 2, 500, "b:2"),
    ]
    deployments(progressing("uid-web", 'ReplicaSet "web-r3" is progressing.', None))
    (out,) = await changes(server, since_minutes=60)
    assert (out["change"], out["when"]) == ("rollout undo", "10m")


@pytest.mark.anyio
async def test_recent_changes_never_queries_outside_the_allowlist(
    settings: Settings, fake_cluster: Any
) -> None:
    """Each allowed namespace on its own; no cluster-wide list, no other namespace."""
    server = k8s(settings, fake_cluster, clusters={"test": ["shop", "pay"], "stg": ["shop-stg"]})
    await changes(server)
    apps = fake_cluster[1]
    listed = (apps.list_namespaced_replica_set, apps.list_namespaced_deployment)
    for each in listed:
        assert sorted(c.args[0] for c in each.call_args_list) == ["pay", "shop"]
        each.reset_mock()
    apps.list_replica_set_for_all_namespaces.assert_not_called()
    apps.list_deployment_for_all_namespaces.assert_not_called()
    async with Client(server) as c:
        with pytest.raises(ToolError, match="not one of this Contract's clusters"):
            await c.call_tool("recent_changes", {"cluster": "bank-kind"})
    for each in listed:
        each.assert_not_called()


def test_the_changes_guide_comes_only_with_kubernetes(
    settings: Settings, fake_cluster: Any
) -> None:
    """A Contract without clusters (Grafana, a cloud) never hears of a tool it lacks."""
    with_k8s = agent.build_mcp(make_contract(), settings, {"test": fake_cluster})
    assert any("kubernetes_recent_changes" in g for g in with_k8s.guides)
    aws_only = evals.scenario_contract("", False, [Path("no-runbooks")], {}, ["aws"])
    prompt = agent.prompt("triage", evals.replay_server(aws_only, settings, {}))
    assert "recent_changes" not in prompt


def ago(minutes: int) -> str:
    return (NOW - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def fake_custom(objects: dict[tuple[str, str], list[dict[str, Any]]], errors: Any = None) -> Any:
    """A CustomObjectsApi: (namespace, plural) -> items; anything else is not installed."""
    from unittest.mock import MagicMock

    from kubernetes.client.exceptions import ApiException

    def list_objects(group: str, version: str, namespace: str, plural: str) -> Any:
        status = (errors or {}).get((namespace, plural))
        if status or (namespace, plural) not in objects:
            raise ApiException(status=status or 404, reason="Forbidden" if status else "Not Found")
        return {"items": objects[(namespace, plural)]}

    custom = MagicMock()
    custom.list_namespaced_custom_object.side_effect = list_objects
    return custom


def gitops_server(settings: Settings, fake_cluster: Any, custom: Any) -> FastMCP:
    c = contracts.build(
        {
            "id": "acme",
            "name": "acme",
            "clusters": [
                {
                    "name": "test",
                    "context": "test",
                    "namespaces": ["shop", "pay"],
                    "gitops_namespaces": ["argocd", "flux-system", "pay"],
                }
            ],
            "integrations": [{"name": "kubernetes"}],
        }
    )
    k = c.integrations[0]
    assert isinstance(k, contracts.Kubernetes)
    return kubernetes.build_server(
        c, k, settings.tool_output_limit, {"test": (*fake_cluster, custom)}
    )


def app(name: str, namespace: str | None, **status: Any) -> dict[str, Any]:
    dest = {"server": "https://kubernetes.default.svc"} | (
        {"namespace": namespace} if namespace else {}
    )
    return {
        "metadata": {"name": name, "namespace": "argocd"},
        "spec": {"destination": dest},
        "status": status,
    }


@pytest.mark.anyio
async def test_argocd_syncs_show_only_for_applications_wholly_inside_the_allowlist(
    settings: Settings, fake_cluster: Any
) -> None:
    fake_cluster[1].list_namespaced_replica_set.return_value = k.V1ReplicaSetList(items=[])
    history = [
        {
            "revision": "aaa",
            "deployedAt": ago(50),
            "source": {"repoURL": "git@x:shop", "path": "app"},
        },
        {
            "revision": "bbb",
            "deployedAt": ago(5),
            "source": {"repoURL": "git@x:shop", "path": "app"},
        },
    ]
    shop = app(
        "shop",
        "shop",
        history=history,
        resources=[{"kind": "Deployment", "namespace": "shop", "name": "checkout"}],
        operationState={
            "phase": "Failed",
            "startedAt": ago(2),
            "message": "hook failed token=supersecret123",
            "syncResult": {"revision": "ccc"},
        },
    )
    leaky = {"repoURL": "https://git.x/pay?token=ghtoken12345678", "path": "p"}
    by_destination = app("fresh", "pay", history=[history[1] | {"source": leaky}])
    remote = app("remote", "shop", history=history)
    remote["spec"]["destination"]["server"] = "https://prod-eu:6443"
    hidden = [
        app(
            "bank",
            "bank",
            history=history,
            operationState={"phase": "Failed", "startedAt": ago(1), "message": "bank-ledger down"},
        ),
        app("mixed", "shop", history=history, resources=[{"namespace": "bank", "name": "ledger"}]),
        app("nowhere", None, history=history),
        remote,
    ]
    objects = {("argocd", "applications"): [shop, by_destination, *hidden]}
    out = await changes(gitops_server(settings, fake_cluster, fake_custom(objects)))
    fresh = [c for c in out if c["argocd_application"] == "fresh"]
    assert [(c["namespace"], c["revision"]) for c in fresh] == [("pay", "bbb")]
    assert "ghtoken" not in str(fresh) and fresh[0]["source"].endswith(" p")
    everything = str(out)
    out = [c for c in out if c["argocd_application"] == "shop"]
    assert [c["change"] for c in out] == ["sync failed", "sync", "sync"]
    assert out[0]["revision"] == "ccc" and "supersecret123" not in out[0]["message"]
    assert out[1] == {
        "when": "5m",
        "namespace": "shop",
        "argocd_application": "shop",
        "change": "sync",
        "revision": "bbb",
        "from_revision": "aaa",
        "source": "git@x:shop app",
    }
    assert out[2]["from_revision"] is None
    for trace in ("bank", "mixed", "nowhere", "remote", "ledger", "prod-eu"):
        assert trace not in everything


@pytest.mark.anyio
async def test_flux_kustomizations_and_helmreleases_with_their_diffs(
    settings: Settings, fake_cluster: Any
) -> None:
    fake_cluster[1].list_namespaced_replica_set.return_value = k.V1ReplicaSetList(items=[])
    inventory = {"entries": [{"id": "shop_checkout_apps_Deployment"}, {"id": "_shop__Namespace"}]}
    ks = {
        "metadata": {"name": "apps", "namespace": "flux-system"},
        "spec": {},
        "status": {
            "inventory": inventory,
            "history": [
                {
                    "firstReconciled": ago(3),
                    "lastReconciledStatus": "HealthCheckFailed",
                    "metadata": {"revision": "main@sha1:222"},
                },
                {"firstReconciled": ago(400), "metadata": {"revision": "main@sha1:111"}},
            ],
            "conditions": [
                {
                    "type": "Ready",
                    "status": "False",
                    "reason": "HealthCheckFailed",
                    "message": "checkout not ready password=hunter2hunter2",
                    "lastTransitionTime": ago(1),
                }
            ],
        },
    }
    older_flux = {
        "metadata": {"name": "pay", "namespace": "flux-system"},
        "spec": {"targetNamespace": "pay"},
        "status": {
            "lastAppliedRevision": "main@sha1:999",
            "conditions": [{"type": "Ready", "status": "True", "lastTransitionTime": ago(10)}],
        },
    }
    unknown = {
        "metadata": {"name": "empty", "namespace": "flux-system"},
        "spec": {},
        "status": {
            "lastAppliedRevision": "x",
            "conditions": [{"type": "Ready", "status": "True", "lastTransitionTime": ago(1)}],
        },
    }
    hr = {
        "metadata": {"name": "podinfo", "namespace": "flux-system"},
        "spec": {"targetNamespace": "shop"},
        "status": {
            "history": [
                {
                    "version": 2,
                    "action": "upgrade",
                    "status": "deployed",
                    "chartName": "podinfo",
                    "chartVersion": "6.6.1",
                    "appVersion": "6.6.1",
                    "configDigest": "sha256:b",
                    "namespace": "shop",
                    "firstDeployed": ago(60),
                    "lastDeployed": ago(4),
                },
                {
                    "version": 1,
                    "action": "install",
                    "status": "superseded",
                    "chartName": "podinfo",
                    "chartVersion": "6.6.0",
                    "configDigest": "sha256:a",
                    "namespace": "shop",
                    "firstDeployed": ago(60),
                    "lastDeployed": ago(60),
                },
            ]
        },
    }
    own_namespace = {
        "metadata": {"name": "infra", "namespace": "flux-system"},
        "spec": {},
        "status": {"history": [{"version": 1, "lastDeployed": ago(2)}]},
    }
    release = {"version": 1, "chartName": "c", "chartVersion": "1", "lastDeployed": ago(2)}

    def helmrelease(name: str, namespace: str, spec: Any, **status: Any) -> dict[str, Any]:
        history = [release | status.pop("release", {})]
        meta = {"name": name, "namespace": namespace}
        return {"metadata": meta, "spec": spec, "status": {"history": history, **status}}

    beside_its_app = helmrelease("ledger-pay", "pay", {})  # release namespace: its own
    moved = helmrelease(
        "moved", "flux-system", {"targetNamespace": "shop"}, release={"namespace": "bank"}
    )
    spills = helmrelease(
        "spills",
        "flux-system",
        {"targetNamespace": "shop"},
        inventory={"entries": [{"id": "bank_vault_apps_Deployment"}]},
    )
    custom = fake_custom(
        {
            ("flux-system", "kustomizations"): [ks, older_flux, unknown],
            ("flux-system", "helmreleases"): [hr, own_namespace, moved, spills],
            ("pay", "helmreleases"): [beside_its_app],
        }
    )
    out = await changes(gitops_server(settings, fake_cluster, custom), since_minutes=120)
    assert [c["namespace"] for c in out if c.get("flux_helmrelease") == "ledger-pay"] == ["pay"]
    everything = str(out)
    out = [c for c in out if c.get("flux_helmrelease") != "ledger-pay"]
    names = [(c.get("flux_kustomization") or c.get("flux_helmrelease"), c["change"]) for c in out]
    assert names == [
        ("apps", "not ready"),
        ("apps", "applied"),
        ("podinfo", "upgrade"),
        ("pay", "applied (time of its last Ready change)"),
        ("podinfo", "install"),
    ]
    assert out[1]["revision"] == "main@sha1:222" and out[1]["from_revision"] == "main@sha1:111"
    assert out[1]["result"] == "HealthCheckFailed" and out[0]["message"].startswith(
        "checkout not ready password="
    )
    assert "hunter2" not in str(out)
    assert out[2] == {
        "when": "4m",
        "namespace": "shop",
        "flux_helmrelease": "podinfo",
        "change": "upgrade",
        "release_version": 2,
        "status": "deployed",
        "chart": "podinfo-6.6.1",
        "app_version": "6.6.1",
        "from_chart": "podinfo-6.6.0",
        "values_changed": True,
    }
    assert "from_chart" not in out[4] and "values_changed" not in out[4]
    assert out[3]["namespace"] == "pay" and out[3]["revision"] == "main@sha1:999"
    for trace in ("infra", "empty", "moved", "spills", "bank", "vault"):
        assert trace not in everything


@pytest.mark.anyio
async def test_gitops_errors_and_absent_controllers(settings: Settings, fake_cluster: Any) -> None:
    """Not installed (404): nothing. No access (403): said, so a gap is never silent."""
    fake_cluster[1].list_namespaced_replica_set.return_value = k.V1ReplicaSetList(items=[])
    custom = fake_custom({}, errors={("argocd", "applications"): 403})
    out = await changes(gitops_server(settings, fake_cluster, custom))
    assert out == [
        {
            "namespace": "argocd",
            "gitops": "applications",
            "error": "Kubernetes API 403 Forbidden (check the agent's RBAC)",
        }
    ]
    asked = {c.args[2:] for c in custom.list_namespaced_custom_object.call_args_list}
    assert asked == {
        (ns, plural)
        for ns in ("argocd", "flux-system", "pay")
        for _, _, plural in kubernetes.GITOPS
    }


@pytest.mark.anyio
async def test_gitops_namespaces_are_for_recent_changes_only(
    settings: Settings, fake_cluster: Any
) -> None:
    server = gitops_server(settings, fake_cluster, fake_custom({}))
    async with Client(server) as c:
        tools = {t.name: t for t in await c.list_tools()}
        assert (
            "argocd" not in tools["list_pods"].input_schema["properties"]["cluster"]["description"]
        )
        for tool in ("list_pods", "get_events", "list_deployments"):
            with pytest.raises(ToolError, match="'argocd' is not allowed"):
                await c.call_tool(tool, {"cluster": "test", "namespace": "argocd"})
    fake_cluster[0].list_namespaced_pod.assert_not_called()
