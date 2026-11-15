"""Real MCP tools against the kind demo cluster (`make cluster demo integration`).

Guarded: refuses to run against any context that is not a kind cluster.
"""

import os
from typing import Any

import anyio
import pytest
from fastmcp import Client, FastMCP

from squero import agent, contracts
from squero.config import Settings
from squero.models import AlertGroup
from squero.servers import kubernetes
from tests.conftest import call, scripted

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


@pytest.fixture
def live(two_contracts: dict[str, contracts.Contract]) -> contracts.Contract:
    """demo-shop, on the kind demo cluster. Refuses any context that is not kind."""
    for c in two_contracts.values():
        for cluster in c.clusters:
            if not (cluster.context or "").startswith("kind-"):
                pytest.fail(f"integration tests only run against kind, got {cluster.context!r}")
    return two_contracts["demo-shop"]


def k8s(contract: contracts.Contract) -> FastMCP:
    k = next(i for i in contract.integrations if isinstance(i, contracts.Kubernetes))
    return kubernetes.build_server(contract, k, Settings(_env_file=None).tool_output_limit)


async def tool(client: Client, tool_name: str, /, **args: Any) -> Any:  # type: ignore[type-arg]
    result = await client.call_tool(tool_name, args)
    data = result.structured_content
    if isinstance(data, dict) and set(data) == {"result"} and isinstance(data["result"], str):
        return data["result"]  # str-returning tools are wrapped by FastMCP
    return data or result.content[0].text


def by_app(pods: list[dict[str, Any]], app: str) -> list[dict[str, Any]]:
    return [p for p in pods if p["name"].startswith(app)]


async def test_finds_all_three_failures(live: contracts.Contract) -> None:
    async with Client(k8s(live)) as c:
        for _ in range(60):  # reports' first OOM kill may still be to come (CI, 2026-11-11)
            pods = (await tool(c, "list_pods", cluster="kind", namespace="demo"))["result"]
            if by_app(pods, "reports")[0]["last_terminated"].get("app"):
                break
            await anyio.sleep(2)

        crashing = [p for p in by_app(pods, "checkout") if p["restarts"] >= 2]
        assert crashing, pods
        assert crashing[0]["last_terminated"]["app"]["exit_code"] == 1

        # Killed at its memory limit: OOMKilled, or Error with SIGKILL's 137 when the runtime
        # misses the cgroup's OOM event (one restart in CI, 2026-11-09; the next was OOMKilled).
        last = by_app(pods, "reports")[0]["last_terminated"]["app"]
        assert last["exit_code"] == 137 and last["terminated"] in {"OOMKilled", "Error"}, last

        frontend = by_app(pods, "frontend")[0]
        assert frontend["containers"]["web"]["waiting"] in {"ImagePullBackOff", "ErrImagePull"}

        logs = await tool(
            c, "pod_logs", cluster="kind", namespace="demo", name=crashing[0]["name"], previous=True
        )
        assert "DATABASE_URL is not set" in logs

        history = await tool(
            c, "describe_deployment", cluster="kind", namespace="demo", name="checkout"
        )
        revisions = [r["revision"] for r in history["rollout_history"]]
        assert revisions[:2] == ["2", "1"], history
        env = [r["config"]["app"]["env"] for r in history["rollout_history"][:2]]
        assert env == [[], ["DATABASE_URL"]], env  # the bad rollout is visible as a diff

        events = (await tool(c, "get_events", cluster="kind", namespace="demo"))["result"]
        assert any(e["reason"] in {"Failed", "BackOff"} for e in events)


async def test_demo_bank_cannot_read_demo_shops_namespace(
    two_contracts: dict[str, contracts.Contract],
) -> None:
    """Same kind cluster, two Contracts: each reads only its own namespace."""
    async with Client(k8s(two_contracts["demo-bank"])) as c:
        own = await c.call_tool("list_pods", {"cluster": "bank-kind", "namespace": "bank"})
        other = await c.call_tool(
            "list_pods", {"cluster": "bank-kind", "namespace": "demo"}, raise_on_error=False
        )
    assert not own.is_error
    assert other.is_error and "not allowed in cluster 'bank-kind'" in other.content[0].text


async def test_namespace_allowlist_holds_on_a_real_cluster(live: contracts.Contract) -> None:
    async with Client(k8s(live)) as c:
        result = await c.call_tool(
            "list_pods", {"cluster": "kind", "namespace": "kube-system"}, raise_on_error=False
        )
    assert result.is_error and "not allowed" in result.content[0].text


async def test_agent_triage_through_real_tools(live: contracts.Contract) -> None:
    """Scripted model, real tools: proves the whole loop works against a cluster."""
    async with Client(k8s(live)) as c:
        pods = (await tool(c, "list_pods", cluster="kind", namespace="demo"))["result"]
    pod = next(p["name"] for p in by_app(pods, "checkout") if p["restarts"] >= 2)

    report = {
        "summary": "checkout revision 2 crash loops: DATABASE_URL was removed.",
        "category": "bad_rollout",
        "probable_root_cause": "Rollout 2 dropped the DATABASE_URL env var.",
        "confidence": "high",
        "severity": "high",
        "affected": ["demo/deployment/checkout"],
        "evidence": [{"source": "kubernetes_pod_logs", "finding": "DATABASE_URL is not set"}],
        "recommended_actions": ["Roll back checkout to revision 1"],
    }
    model = scripted(
        call("kubernetes_list_pods", "c1", cluster="kind", namespace="demo"),
        call(
            "kubernetes_pod_logs", "c2", cluster="kind", namespace="demo", name=pod, previous=True
        ),
        call(
            "kubernetes_describe_deployment",
            "c3",
            cluster="kind",
            namespace="demo",
            name="checkout",
        ),
        call("TriageReport", "c4", **report),
    )
    alert = AlertGroup.model_validate(
        {
            "status": "firing",
            "commonLabels": {"alertname": "KubePodCrashLooping", "namespace": "demo"},
            "alerts": [{"status": "firing", "labels": {"pod": pod}}],
        }
    )
    hub = Settings(_env_file=None)
    async with agent.open_agent(hub, agent.build_mcp(live, hub), model) as a:
        outcome = await agent.start(a, alert, "it-1")
    assert outcome.report is not None and outcome.report.category == "bad_rollout"
    assert len(outcome.tool_calls) == 4


async def test_recent_changes_finds_the_dependency_rollout(live: contracts.Contract) -> None:
    """The real API: inventory's rollout, what it changed, and only demo-shop's namespaces
    (demo-bank's namespace sits on the same cluster)."""
    async with Client(k8s(live)) as c:
        changes = (await tool(c, "recent_changes", cluster="kind"))["result"]
    rollout = next(
        ch for ch in changes if ch.get("deployment") == "inventory" and ch["change"] == "rollout"
    )
    assert rollout["namespace"] == "orders"
    assert rollout["diff"] == {"env_added": {"app": ["API_VERSION"]}}
    assert {ch["namespace"] for ch in changes} == {"demo", "orders"}


async def test_recent_changes_reads_gitops_objects_of_its_own_namespaces(
    live: contracts.Contract,
) -> None:
    """demo/gitops.sh's stand-ins: orders' Application and Kustomization show, with
    their revisions; demo-bank's objects in the same shared namespaces never do."""
    async with Client(k8s(live)) as c:
        changes = (await tool(c, "recent_changes", cluster="kind"))["result"]
    sync = next(ch for ch in changes if ch.get("argocd_application") == "orders")
    assert (sync["revision"], sync["from_revision"]) == ("9e8f7a6", "1b2c3d4")
    applied = next(ch for ch in changes if ch.get("flux_kustomization") == "orders")
    assert applied["revision"] == "main@sha1:9e8f7a6" and applied["namespace"] == "orders"
    assert "bank" not in str(changes) and not any("error" in ch for ch in changes)


async def test_the_actuator_resizes_a_deployment_in_place_without_a_restart(
    live: contracts.Contract,
) -> None:
    """Slices 12.1, 12.3 and 12.4 on a real cluster: inventory's pod takes 5m more CPU than it
    has, the kubelet allocates it (the pod's status, not just the API server's yes),
    it is rolled back to the template's, and it never restarts."""
    import time

    import anyio
    from kubernetes import client

    from squero import actuator

    api = kubernetes.api_client(live.clusters[0], live)
    # through Calm, as the Actuator's own process calls them (slice 12.9a)
    core, apps = actuator.Calm(client.CoreV1Api(api)), actuator.Calm(client.AppsV1Api(api))
    try:
        actuator.check_version(client.VersionApi(api))
    except SystemExit as e:
        pytest.skip(str(e))
    d = apps.read_namespaced_deployment("inventory", "orders")
    (before,) = actuator.pods_of(core, apps, d, "Deployment")
    restarts = before.status.container_statuses[0].restart_count
    cpu = float(actuator.qty(before.spec.containers[0].resources.requests["cpu"])) + 0.005
    wanted = actuator.Wanted(
        run_id=1,
        namespace="orders",
        kind="Deployment",
        name="inventory",
        container="app",
        cpu=cpu,
        memory=None,
    )
    scalers = (client.AutoscalingV2Api(api), client.CustomObjectsApi(api))
    a, held = actuator.apply(core, apps, wanted, time.time(), *scalers)
    assert a is None, a.reason  # its one pod is the canary: resized, now watched
    for _ in range(60):
        p = core.read_namespaced_pod(before.metadata.name, "orders")
        state, message = actuator.resize_state(p, "app", held["requests"])
        if state == "done":  # the kubelet allocated it, and the pod is Ready
            break
        assert state == "waiting", f"{state}: {message}"
        await anyio.sleep(1)
    else:
        pytest.fail(f"the kubelet never allocated {actuator.cpu_text(cpu)}: {message}")
    status = p.status.container_statuses[0]
    assert p.metadata.uid == before.metadata.uid and status.restart_count == restarts
    # Slice 12.4: rolled back, in place, to the template's requests (no restart either).
    back = actuator.rollback(core, apps, "orders/Deployment/inventory/app", held, "a test", 0.0)
    assert (back.outcome, back.pods) == ("rolled_back", 1), back.reason
    template = {"cpu": held["template"]["cpu"]}
    for _ in range(60):
        p = core.read_namespaced_pod(before.metadata.name, "orders")
        if actuator.resize_state(p, "app", template)[0] == "done":
            break
        await anyio.sleep(1)
    else:
        pytest.fail(f"never back to {template}")
    assert p.status.container_statuses[0].restart_count == restarts


async def test_record_eval_fixtures(live: contracts.Contract) -> None:
    """With SQUERO_RECORD_DIR set, save real tool outputs for the eval scenarios."""
    out = os.environ.get("SQUERO_RECORD_DIR")
    if not out:
        pytest.skip("SQUERO_RECORD_DIR not set")
    import json
    from pathlib import Path

    async def namespace(c: Client, ns: str, deployments: tuple[str, ...]) -> dict[str, Any]:
        rec: dict[str, Any] = {}
        pods = (await tool(c, "list_pods", cluster="kind", namespace=ns))["result"]
        rec["kubernetes_list_pods"] = pods
        rec["kubernetes_list_deployments"] = await tool(
            c, "list_deployments", cluster="kind", namespace=ns
        )
        rec["kubernetes_get_events"] = await tool(c, "get_events", cluster="kind", namespace=ns)
        for name in deployments:
            rec[f"deploy:{name}"] = await tool(
                c, "describe_deployment", cluster="kind", namespace=ns, name=name
            )
        for p in pods:
            rec[f"pod:{p['name']}"] = await tool(
                c, "describe_pod", cluster="kind", namespace=ns, name=p["name"]
            )
            for previous in (False, True):
                r = await c.call_tool(
                    "pod_logs",
                    {"cluster": "kind", "namespace": ns, "name": p["name"], "previous": previous},
                    raise_on_error=False,
                )
                rec[f"logs:{p['name']}:{previous}"] = r.content[0].text
        return rec

    async with Client(k8s(live)) as c:
        rec = await namespace(c, "demo", ("checkout", "reports", "frontend"))
        rec["orders"] = await namespace(c, "orders", ("orders-api", "inventory"))
        # Both namespaces in one call: build_scenarios keeps each Scenario's own.
        changes = await tool(c, "recent_changes", cluster="kind")
        rec["kubernetes_recent_changes"] = changes["result"]
    Path(out).mkdir(parents=True, exist_ok=True)
    data = json.dumps(rec, indent=2, default=str)
    (Path(out) / "recording.json").write_text(data)


async def test_the_actuator_reads_blocker_facts_a_page_at_a_time_through_the_real_client(
    live: contracts.Contract, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slice 15.1b on a real cluster: `limit` and `_continue` through the client the
    Actuator runs with. Every pod its own page gives what one page of them all does."""
    from kubernetes import client

    from squero import actuator, blockers

    api = kubernetes.api_client(live.clusters[0], live)
    core, policy = actuator.Calm(client.CoreV1Api(api)), actuator.Calm(client.PolicyV1Api(api))
    spaces = ["demo", "orders"]
    there = {p.metadata.name for ns in spaces for p in core.list_namespaced_pod(ns).items}
    assert len(there) > 2

    def every(p: Any, budgets: list[Any], label: str | None) -> blockers.PodFacts:  # every pod
        return blockers.PodFacts(
            namespace=p.metadata.namespace, pod=p.metadata.name, node=p.spec.node_name or ""
        )

    real = actuator.blocking(core, policy, spaces, 1.0)
    assert real.namespaces == spaces and blockers.unreal(real) is None
    monkeypatch.setattr(actuator, "pod_facts", every)
    monkeypatch.setattr(actuator, "PAGE", 1)
    paged = {p.pod for p in actuator.blocking(core, policy, spaces, 1.0).pods}
    monkeypatch.setattr(actuator, "PAGE", 200)
    whole = {p.pod for p in actuator.blocking(core, policy, spaces, 1.0).pods}
    assert paged == whole and len(whole & there) > 2
