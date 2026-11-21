"""Shared fixtures: a fake cluster built from real kubernetes client models, and a
scripted chat model, so the agent runs end-to-end without a cluster or an LLM."""

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import psycopg
import pytest
from kubernetes import client as k
from langchain.messages import AIMessage, ToolCall
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from pydantic import SecretStr

from squero import agent, contracts, protocol
from squero.config import Settings

NOW = datetime.now(UTC)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _forget_protocols() -> Iterator[None]:
    """What parts last said (slice 18.3) is one process's memory: never another test's."""
    protocol.SEEN.clear()
    yield
    protocol.SEEN.clear()


DEMO_CONTRACTS = Path(__file__).parent.parent / "demo" / "contracts"


def use_age_keys(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *contract_ids: str) -> None:
    """Give sops only these demo Contracts' test keys."""
    keys = tmp_path / "keys.txt"
    keys.write_text(
        "".join((DEMO_CONTRACTS / "test-keys" / f"{c}.agekey").read_text() for c in contract_ids)
    )
    monkeypatch.delenv("SOPS_AGE_KEY", raising=False)
    monkeypatch.setenv("SOPS_AGE_KEY_FILE", str(keys))


@pytest.fixture
def two_contracts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, contracts.Contract]:
    """The two synthetic demo Contracts, for isolation tests: A's data must never reach B."""
    use_age_keys(monkeypatch, tmp_path, "demo-shop", "demo-bank")
    return contracts.load(DEMO_CONTRACTS)


CLUSTER = "test"


def make_contract(
    *,
    enable_actions: bool = False,
    clusters: dict[str, list[str]] | None = None,
    runbook_dirs: tuple[Path, ...] = (),
    contract_id: str = "acme",
    webhook_token: str | None = None,
    slack_channel: str | None = None,
    client_channel: str | None = None,
    secrets: dict[str, str] | None = None,
    **extra: Any,
) -> contracts.Contract:
    """A Contract in memory: by default one cluster "test" allowing namespace "shop"."""
    clusters = clusters or {CLUSTER: ["shop"]}
    integrations: list[dict[str, Any]] = [{"name": "kubernetes", "enable_actions": enable_actions}]
    if runbook_dirs:
        integrations.append({"name": "runbooks"})
    data = {
        "id": contract_id,
        "name": contract_id,
        "clusters": [{"name": n, "context": n, "namespaces": ns} for n, ns in clusters.items()],
        "integrations": integrations,
    }
    if webhook_token:
        data["webhook_token_sha256"] = contracts.token_hash(webhook_token)
    if slack_channel:
        data["slack"] = {"operator_channel": slack_channel, "client_channel": client_channel}
    values = {k: SecretStr(v) for k, v in (secrets or {}).items()}
    return contracts.build({**data, **extra}, secrets=values, runbook_dirs=runbook_dirs)


def toolbox(settings: Settings, fake_cluster: Any, **contract: Any) -> agent.Toolbox:
    """make_contract(**contract)'s tools, every cluster answered by fake_cluster."""
    c = make_contract(**contract)
    return agent.build_mcp(c, settings, {cl.name: fake_cluster for cl in c.clusters})


TEST_DATABASE = os.environ.get(
    "SQUERO_TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:55432/postgres"
)


@pytest.fixture
def database_url() -> Iterator[str]:
    """A fresh, empty database for one test, dropped after. Never skipped: without
    Postgres this fails (start it with `make db`)."""
    name = f"t_{uuid.uuid4().hex[:12]}"
    try:
        admin = psycopg.connect(TEST_DATABASE, autocommit=True)
    except psycopg.OperationalError as e:
        pytest.fail(f"no test Postgres at {TEST_DATABASE}: run `make db` ({e})")
    with admin:
        admin.execute(f'create database "{name}"')
    yield TEST_DATABASE.rsplit("/", 1)[0] + "/" + name
    with psycopg.connect(TEST_DATABASE, autocommit=True) as admin:
        admin.execute(f'drop database "{name}" with (force)')


@pytest.fixture
def settings(tmp_path: Any) -> Settings:
    return Settings(_env_file=None, model="fake")


def crashlooping_pod() -> k.V1Pod:
    return k.V1Pod(
        metadata=k.V1ObjectMeta(
            name="checkout-7d9f-abc",
            namespace="shop",
            creation_timestamp=NOW - timedelta(hours=1),
            owner_references=[
                k.V1OwnerReference(
                    api_version="apps/v1", kind="ReplicaSet", name="checkout-7d9f", uid="rs1"
                )
            ],
        ),
        spec=k.V1PodSpec(
            node_name="node-1",
            containers=[
                k.V1Container(
                    name="app",
                    image="shop/checkout:1.4.0",
                    env=[k.V1EnvVar(name="DATABASE_URL", value="postgres://u:supersecret@db/shop")],
                    env_from=[
                        k.V1EnvFromSource(secret_ref=k.V1SecretEnvSource(name="checkout-creds"))
                    ],
                    resources=k.V1ResourceRequirements(limits={"memory": "64Mi"}),
                )
            ],
        ),
        status=k.V1PodStatus(
            phase="Running",
            container_statuses=[
                k.V1ContainerStatus(
                    name="app",
                    image="shop/checkout:1.4.0",
                    image_id="",
                    ready=False,
                    restart_count=7,
                    state=k.V1ContainerState(
                        waiting=k.V1ContainerStateWaiting(reason="CrashLoopBackOff")
                    ),
                    last_state=k.V1ContainerState(
                        terminated=k.V1ContainerStateTerminated(
                            reason="OOMKilled",
                            exit_code=137,
                            finished_at=NOW - timedelta(minutes=2),
                        )
                    ),
                )
            ],
        ),
    )


def deployment() -> k.V1Deployment:
    return k.V1Deployment(
        metadata=k.V1ObjectMeta(name="checkout", namespace="shop", uid="dep1"),
        spec=k.V1DeploymentSpec(
            replicas=2,
            selector=k.V1LabelSelector(match_labels={"app": "checkout"}),
            template=k.V1PodTemplateSpec(
                spec=k.V1PodSpec(
                    containers=[k.V1Container(name="app", image="shop/checkout:1.4.0")]
                )
            ),
        ),
        status=k.V1DeploymentStatus(
            ready_replicas=0,
            updated_replicas=2,
            conditions=[
                k.V1DeploymentCondition(
                    type="Available", status="False", reason="MinimumReplicasUnavailable"
                )
            ],
        ),
    )


@pytest.fixture
def fake_cluster() -> tuple[MagicMock, MagicMock]:
    return make_fake_cluster()


def make_fake_cluster() -> tuple[MagicMock, MagicMock]:
    """(CoreV1Api, AppsV1Api) mocks answering with one crash-looping pod."""
    core, apps = MagicMock(), MagicMock()
    core.list_namespaced_pod.return_value = k.V1PodList(items=[crashlooping_pod()])
    core.read_namespaced_pod.return_value = crashlooping_pod()
    core.list_namespaced_event.return_value = k.CoreV1EventList(
        items=[
            k.CoreV1Event(
                metadata=k.V1ObjectMeta(name="e1", creation_timestamp=NOW),
                involved_object=k.V1ObjectReference(kind="Pod", name="checkout-7d9f-abc"),
                type="Warning",
                reason="BackOff",
                message="Back-off restarting failed container",
                count=7,
                last_timestamp=NOW,
            )
        ]
    )
    core.read_namespaced_pod_log.return_value = (
        "starting checkout\nAuthorization: Bearer abcdefghijklmnopqrstuvwxyz123456\n"
        "java.lang.OutOfMemoryError: Java heap space\n"
    )
    apps.list_namespaced_deployment.return_value = k.V1DeploymentList(items=[deployment()])
    apps.read_namespaced_deployment.return_value = deployment()
    apps.list_namespaced_replica_set.return_value = k.V1ReplicaSetList(items=[])
    return core, apps


class ScriptedModel(GenericFakeChatModel):
    """Replays a fixed list of AI messages; tool binding is a no-op."""

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ScriptedModel":
        return self


def scripted(*steps: AIMessage) -> ScriptedModel:
    return ScriptedModel(messages=iter(steps))


def call(tool: str, call_id: str, /, **args: Any) -> AIMessage:
    return AIMessage(content="", tool_calls=[ToolCall(name=tool, args=args, id=call_id)])


REPORT = {
    "summary": "checkout is crash looping: the JVM runs out of memory under a 64Mi limit.",
    "category": "resource_limits",
    "probable_root_cause": "Memory limit 64Mi is below the JVM heap requirement.",
    "confidence": "high",
    "severity": "high",
    "affected": ["shop/deployment/checkout"],
    "evidence": [
        {"source": "kubernetes_pod_logs", "finding": "java.lang.OutOfMemoryError: Java heap space"}
    ],
    "recommended_actions": ["Raise the memory limit to 512Mi", "Set -XX:MaxRAMPercentage=75"],
}


EXEC_USER = {"exec": {"command": "aws", "args": ["eks", "get-token"], "apiVersion": "v1beta1"}}
GCP_USER = {"auth-provider": {"name": "gcp", "config": {"cmd-path": "gcloud"}}}


def kubeconfig(user: dict[str, Any], context: str = "p", user_name: str = "u") -> dict[str, Any]:
    """A minimal kubeconfig whose `context` authenticates as `user`."""
    return {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{"name": "c", "cluster": {"server": "https://k8s.example"}}],
        "contexts": [{"name": context, "context": {"cluster": "c", "user": user_name}}],
        "users": [{"name": user_name, "user": user}],
    }
