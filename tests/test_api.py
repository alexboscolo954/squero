import threading
import time
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from squero import agent
from squero.api import create_app
from squero.config import Settings
from squero.contracts import Contract
from tests.conftest import REPORT, call, make_contract, make_fake_cluster, scripted

URL = "/contracts/acme/alertmanager"
HOOK = {"Authorization": "Bearer hook-token"}
AUTH = {"Authorization": "Bearer approver-token"}
ALERT = {
    "status": "firing",
    "groupKey": "g1",
    "commonLabels": {"alertname": "KubePodCrashLooping", "namespace": "shop"},
    "alerts": [{"status": "firing", "labels": {"pod": "checkout-7d9f-abc"}, "fingerprint": "f1"}],
}


@pytest.fixture(autouse=True)
def _database(settings: Settings, database_url: str) -> None:
    settings.database_url = SecretStr(database_url)


def offline(request: httpx.Request) -> httpx.Response:
    """Slack's Web API for a test without a fake: unreachable, never the real one (two
    teachings tests asked slack.com for a socket and hung CI on its DNS, 2026-10-23)."""
    raise httpx.ConnectError(f"offline in tests: {request.url}")


def client(
    settings: Settings,
    fake_cluster: Any,
    *steps: Any,
    actions: bool = False,
    more: dict[str, tuple[Contract, Any]] | None = None,
    slack: Any = None,
    acme_channel: str | None = None,
    acme_client: str | None = None,
    acme: dict[str, Any] | None = None,
    price_http: Any = None,
    bill_http: Any = None,
    github_http: Any = None,
) -> TestClient:
    """Contract "acme" (token hook-token) on fake_cluster, plus `more`: id -> (Contract,
    its own fake cluster). The model is shared, so its steps run in alert order.
    slack: an httpx transport standing in for Slack's Web API (default: offline)."""
    settings.approver_token = SecretStr("approver-token")
    first = make_contract(
        enable_actions=actions,
        webhook_token="hook-token",
        slack_channel=acme_channel,
        client_channel=acme_client,
        **(acme or {}),
    )
    clusters = {"acme": (first, fake_cluster), **(more or {})}

    def mcp(c: Contract) -> agent.Toolbox:
        fake = clusters[c.id][1]
        return agent.build_mcp(c, settings, {cl.name: fake for cl in c.clusters})

    def changes(c: Contract) -> Any:  # deploy verification's polls, on the same fakes
        fake = clusters[c.id][1]
        k8s = next(i for i in c.integrations if i.name == "kubernetes")
        return agent.integration_server(c, k8s, settings, {cl.name: fake for cl in c.clusters})[0]

    by_id = {cid: c for cid, (c, _) in clusters.items()}
    http = httpx.AsyncClient(transport=slack or httpx.MockTransport(offline))
    return TestClient(
        create_app(
            settings,
            scripted(*steps),
            mcp,
            by_id,
            http,
            changes,
            price_http,
            bill_http,
            github_http,
        )
    )


def wait_for(c: TestClient, investigation_id: str, status: str) -> dict[str, Any]:
    # 15 s, not 2: a loaded CI runner once took longer than 2 s to run a scripted triage
    deadline = time.monotonic() + 15
    while True:
        t: dict[str, Any] = c.get(f"/investigations/{investigation_id}", headers=AUTH).json()
        if t["status"] == status:
            return t
        if time.monotonic() > deadline:
            raise AssertionError(f"triage stuck in {t['status']}: {t}")
        time.sleep(0.02)


def test_refuses_to_start_without_token_contracts_or_database(settings: Settings) -> None:
    with pytest.raises(RuntimeError, match="SQUERO_APPROVER_TOKEN"):
        create_app(settings, scripted(), by_id={})
    settings.approver_token = SecretStr("approver-token")
    with pytest.raises(RuntimeError, match="SQUERO_CONTRACTS_DIR"):
        create_app(settings, scripted())
    settings.database_url = None
    with pytest.raises(RuntimeError, match="SQUERO_DATABASE_URL"):
        create_app(settings, scripted(), by_id={})


def test_webhook_requires_auth(settings: Settings, fake_cluster: Any) -> None:
    with client(settings, fake_cluster) as c:
        assert c.post(URL, json=ALERT).status_code in {401, 403}
        bad = {"Authorization": "Bearer nope"}
        assert c.post(URL, json=ALERT, headers=bad).status_code == 401
        # An unknown Contract looks exactly like a wrong token.
        unknown = c.post("/contracts/nope/alertmanager", json=ALERT, headers=HOOK)
        assert (unknown.status_code, unknown.json()) == (401, {"detail": "invalid token"})
        assert c.post("/alertmanager", json=ALERT, headers=HOOK).status_code == 404
        assert c.get("/healthz").status_code == 200


def test_alert_to_report_and_dedup(settings: Settings, fake_cluster: Any) -> None:
    steps = [
        call("kubernetes_list_pods", "c1", cluster="test", namespace="shop"),
        call("TriageReport", "c2", **REPORT),
    ]
    with client(settings, fake_cluster, *steps) as c:
        r = c.post(URL, json=ALERT, headers=HOOK).json()
        assert r["status"] == "accepted"
        t = wait_for(c, r["investigation"], "done")
        assert t["report"]["category"] == "resource_limits"
        again = c.post(URL, json=ALERT, headers=HOOK).json()
        assert again == {"status": "duplicate", "investigation": r["investigation"]}
        assert "squero_tool_calls_total" in c.get("/metrics").text


def test_only_a_new_firing_alert_starts_a_new_triage(settings: Settings, fake_cluster: Any) -> None:
    pod2 = {"status": "firing", "labels": {"pod": "checkout-7d9f-def"}, "fingerprint": "f2"}
    both = {**ALERT, "alerts": [*ALERT["alerts"], pod2]}
    only_f2 = {**ALERT, "alerts": [pod2]}
    steps = [call("TriageReport", "c1", **REPORT), call("TriageReport", "c2", **REPORT)]
    with client(settings, fake_cluster, *steps) as c:
        first = c.post(URL, json=ALERT, headers=HOOK).json()["investigation"]
        wait_for(c, first, "done")
        grown = c.post(URL, json=both, headers=HOOK).json()
        assert grown["status"] == "accepted" and grown["investigation"] != first
        wait_for(c, grown["investigation"], "done")
        for shrunk in (only_f2, ALERT, both):
            r = c.post(URL, json=shrunk, headers=HOOK).json()
            assert r == {"status": "duplicate", "investigation": grown["investigation"]}


def test_resolved_alerts_are_ignored(settings: Settings, fake_cluster: Any) -> None:
    with client(settings, fake_cluster) as c:
        resolved = {
            **ALERT,
            "status": "resolved",
            "alerts": [{**ALERT["alerts"][0], "status": "resolved"}],
        }
        assert c.post(URL, json=resolved, headers=HOOK).json()["status"] == "ignored"


def test_approval_flow_over_http(settings: Settings, fake_cluster: Any) -> None:
    steps = [
        call(
            "kubernetes_evict_pod", "c1", cluster="test", namespace="shop", name="checkout-7d9f-abc"
        ),
        call("TriageReport", "c2", **REPORT),
    ]
    with client(settings, fake_cluster, *steps, actions=True) as c:
        tid = c.post(URL, json=ALERT, headers=HOOK).json()["investigation"]
        t = wait_for(c, tid, "awaiting_approval")
        assert t["pending"][0]["name"] == "kubernetes_evict_pod"
        fake_cluster[0].create_namespaced_pod_eviction.assert_not_called()

        r = c.post(f"/investigations/{tid}/decision", json={"approve": False}, headers=AUTH)
        assert r.status_code == 202
        wait_for(c, tid, "done")
        fake_cluster[0].create_namespaced_pod_eviction.assert_not_called()
        again = c.post(f"/investigations/{tid}/decision", json={"approve": True}, headers=AUTH)
        assert again.status_code == 409


def test_webhook_token_cannot_read_or_approve(settings: Settings, fake_cluster: Any) -> None:
    steps = [call("TriageReport", "c1", **REPORT)]
    with client(settings, fake_cluster, *steps) as c:
        tid = c.post(URL, json=ALERT, headers=HOOK).json()["investigation"]
        assert c.get(f"/investigations/{tid}", headers=HOOK).status_code == 401
        decision = c.post(f"/investigations/{tid}/decision", json={"approve": True}, headers=HOOK)
        assert decision.status_code == 401
        assert c.post(URL, json=ALERT, headers=AUTH).status_code == 401


def test_webhook_token_must_not_be_the_approver_token(settings: Settings) -> None:
    settings.approver_token = SecretStr("same")
    by_id = {"acme": make_contract(webhook_token="same")}
    with pytest.raises(RuntimeError, match=r"'acme'.s webhook token is the approver token"):
        create_app(settings, scripted(), by_id=by_id)


EVICT_THEN_REPORT = [
    call("kubernetes_evict_pod", "c1", cluster="test", namespace="shop", name="checkout-7d9f-abc"),
    call("TriageReport", "c2", **REPORT),
]
RESOLVED = {**ALERT, "status": "resolved", "alerts": [{**ALERT["alerts"][0], "status": "resolved"}]}


def test_resolved_group_makes_proposed_actions_stale(settings: Settings, fake_cluster: Any) -> None:
    with client(settings, fake_cluster, *EVICT_THEN_REPORT, actions=True) as c:
        tid = c.post(URL, json=ALERT, headers=HOOK).json()["investigation"]
        wait_for(c, tid, "awaiting_approval")
        r = c.post(URL, json=RESOLVED, headers=HOOK).json()
        assert r == {"status": "stale", "investigations": [tid]}
        assert wait_for(c, tid, "done")["report"] is not None
        late = c.post(f"/investigations/{tid}/decision", json={"approve": True}, headers=AUTH)
        assert late.status_code == 409
    fake_cluster[0].create_namespaced_pod_eviction.assert_not_called()


def test_proposed_actions_go_stale_without_a_decision(
    settings: Settings, fake_cluster: Any
) -> None:
    settings.decision_timeout_seconds = 0
    with client(settings, fake_cluster, *EVICT_THEN_REPORT, actions=True) as c:
        tid = c.post(URL, json=ALERT, headers=HOOK).json()["investigation"]
        t = wait_for(c, tid, "done")
        assert t["report"] is not None and t["executed_actions"] == []
    fake_cluster[0].create_namespaced_pod_eviction.assert_not_called()


def test_group_resolving_mid_investigation_makes_later_proposal_stale(
    settings: Settings, fake_cluster: Any
) -> None:
    started, release = threading.Event(), threading.Event()

    def slow_list(*args: Any, **kwargs: Any) -> Any:
        started.set()
        release.wait(5)
        return MagicMock(items=[])

    fake_cluster[0].list_namespaced_pod.side_effect = slow_list
    steps = [
        call("kubernetes_list_pods", "c0", cluster="test", namespace="shop"),
        *EVICT_THEN_REPORT,
    ]
    with client(settings, fake_cluster, *steps, actions=True) as c:
        tid = c.post(URL, json=ALERT, headers=HOOK).json()["investigation"]
        assert started.wait(5)
        r = c.post(URL, json=RESOLVED, headers=HOOK).json()
        assert r["status"] == "ignored"  # nothing paused yet
        release.set()
        assert wait_for(c, tid, "done")["report"] is not None
    fake_cluster[0].create_namespaced_pod_eviction.assert_not_called()


BANK_URL = "/contracts/bank/alertmanager"
BANK_HOOK = {"Authorization": "Bearer bank-token"}


def bank(actions: bool = False) -> dict[str, tuple[Contract, Any]]:
    """A second Contract with its own token and its own fake cluster."""
    c = make_contract(contract_id="bank", enable_actions=actions, webhook_token="bank-token")
    return {"bank": (c, make_fake_cluster())}


def test_one_contracts_token_cannot_post_to_another(settings: Settings, fake_cluster: Any) -> None:
    with client(settings, fake_cluster, more=bank()) as c:
        assert c.post(BANK_URL, json=ALERT, headers=HOOK).status_code == 401
        assert c.post(URL, json=ALERT, headers=BANK_HOOK).status_code == 401


def test_same_group_key_in_two_contracts_starts_two_triages(
    settings: Settings, fake_cluster: Any
) -> None:
    steps = [call("TriageReport", "c1", **REPORT), call("TriageReport", "c2", **REPORT)]
    with client(settings, fake_cluster, *steps, more=bank()) as c:
        a = c.post(URL, json=ALERT, headers=HOOK).json()
        wait_for(c, a["investigation"], "done")
        b = c.post(BANK_URL, json=ALERT, headers=BANK_HOOK).json()
        assert a["status"] == b["status"] == "accepted" and a["investigation"] != b["investigation"]
        assert wait_for(c, b["investigation"], "done")["contract_id"] == "bank"
        listed = {t["id"]: t["contract_id"] for t in c.get("/investigations", headers=AUTH).json()}
        assert listed == {a["investigation"]: "acme", b["investigation"]: "bank"}


def test_one_contract_resolving_leaves_anothers_actions_pending(
    settings: Settings, fake_cluster: Any
) -> None:
    # Both pause on their eviction before either reports.
    evict, report = EVICT_THEN_REPORT
    steps = [evict, evict, report, report]
    with client(settings, fake_cluster, *steps, actions=True, more=bank(actions=True)) as c:
        a = c.post(URL, json=ALERT, headers=HOOK).json()["investigation"]
        wait_for(c, a, "awaiting_approval")
        b = c.post(BANK_URL, json=ALERT, headers=BANK_HOOK).json()["investigation"]
        wait_for(c, b, "awaiting_approval")
        r = c.post(URL, json=RESOLVED, headers=HOOK).json()
        assert r == {"status": "stale", "investigations": [a]}
        wait_for(c, a, "done")
        assert c.get(f"/investigations/{b}", headers=AUTH).json()["status"] == "awaiting_approval"


def test_approved_action_runs_on_the_triages_own_contract(
    settings: Settings, fake_cluster: Any
) -> None:
    other = bank(actions=True)
    with client(settings, fake_cluster, *EVICT_THEN_REPORT, actions=True, more=other) as c:
        tid = c.post(BANK_URL, json=ALERT, headers=BANK_HOOK).json()["investigation"]
        wait_for(c, tid, "awaiting_approval")
        c.post(f"/investigations/{tid}/decision", json={"approve": True}, headers=AUTH)
        assert len(wait_for(c, tid, "done")["executed_actions"]) == 1
    other["bank"][1][0].create_namespaced_pod_eviction.assert_called_once()
    fake_cluster[0].create_namespaced_pod_eviction.assert_not_called()


def test_dedup_memory_expires_after_the_window(
    settings: Settings, fake_cluster: Any, database_url: str
) -> None:
    import psycopg

    steps = [call("TriageReport", "c1", **REPORT), call("TriageReport", "c2", **REPORT)]
    with client(settings, fake_cluster, *steps) as c:
        first = c.post(URL, json=ALERT, headers=HOOK).json()["investigation"]
        wait_for(c, first, "done")
        with psycopg.connect(database_url) as conn:
            conn.execute("update alert_groups set started = started - 31 * 60")
        again = c.post(URL, json=ALERT, headers=HOOK).json()
        assert again["status"] == "accepted" and again["investigation"] != first


def test_tool_calls_are_stored_per_investigation_and_contract(
    settings: Settings, fake_cluster: Any, database_url: str
) -> None:
    import psycopg

    steps = [
        call("kubernetes_list_pods", "c1", cluster="test", namespace="shop"),
        call("TriageReport", "c2", **REPORT),
    ]
    with client(settings, fake_cluster, *steps) as c:
        tid = c.post(URL, json=ALERT, headers=HOOK).json()["investigation"]
        wait_for(c, tid, "done")
    with psycopg.connect(database_url) as conn:
        rows = conn.execute(
            "select investigation_id, contract_id, tool, args, is_error, result from tool_calls"
        ).fetchall()
    assert [r[:5] for r in rows] == [
        (tid, "acme", "kubernetes_list_pods", {"cluster": "test", "namespace": "shop"}, False)
    ]
    assert "CrashLoopBackOff" in rows[0][5]
