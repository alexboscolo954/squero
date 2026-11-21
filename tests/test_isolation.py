"""Contract isolation: nothing of one Contract is reachable while working for another.

Every test uses the two synthetic demo Contracts (demo/contracts). Add one here for
each feature that stores, reads or sends Contract data.
"""

import json
import logging
from typing import Any

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from squero import agent, contracts, masking
from squero.config import Settings

Contracts = dict[str, contracts.Contract]


def tools_for(c: contracts.Contract, settings: Settings, fake_cluster: Any) -> agent.Toolbox:
    return agent.build_mcp(c, settings, {cl.name: fake_cluster for cl in c.clusters})


@pytest.mark.anyio
async def test_each_contract_gets_only_its_own_clusters(
    two_contracts: Contracts, settings: Settings, fake_cluster: Any
) -> None:
    enums = {}
    for cid, c in two_contracts.items():
        async with Client(tools_for(c, settings, fake_cluster).mcp) as client:
            tools = {t.name: t for t in await client.list_tools()}
        enums[cid] = tools["kubernetes_list_pods"].input_schema["properties"]["cluster"]["enum"]
    assert enums == {"demo-shop": ["kind"], "demo-bank": ["bank-kind"]}


@pytest.mark.anyio
async def test_naming_another_contracts_cluster_fails(
    two_contracts: Contracts, settings: Settings, fake_cluster: Any
) -> None:
    shop = tools_for(two_contracts["demo-shop"], settings, fake_cluster)
    async with Client(shop.mcp) as client:
        with pytest.raises(ToolError, match="not one of this Contract's clusters: \\['kind'\\]"):
            await client.call_tool(
                "kubernetes_list_pods", {"cluster": "bank-kind", "namespace": "bank"}
            )
        # Same physical cluster, but demo-shop may only read its own namespace.
        with pytest.raises(ToolError, match="'bank' is not allowed in cluster 'kind'"):
            await client.call_tool("kubernetes_list_pods", {"cluster": "kind", "namespace": "bank"})
    fake_cluster[0].list_namespaced_pod.assert_not_called()


@pytest.mark.anyio
async def test_recent_changes_lists_only_the_contracts_own_namespaces(
    two_contracts: Contracts, settings: Settings, fake_cluster: Any
) -> None:
    """demo-shop and demo-bank share one physical cluster: shop's timeline never lists
    bank's namespace, and shop can't ask for bank's cluster."""
    shop = tools_for(two_contracts["demo-shop"], settings, fake_cluster)
    async with Client(shop.mcp) as client:
        await client.call_tool("kubernetes_recent_changes", {"cluster": "kind"})
        with pytest.raises(ToolError, match="not one of this Contract's clusters"):
            await client.call_tool("kubernetes_recent_changes", {"cluster": "bank-kind"})
    apps = fake_cluster[1]
    listed = {c.args[0] for c in apps.list_namespaced_replica_set.call_args_list}
    assert listed == set(two_contracts["demo-shop"].clusters[0].namespaces)
    assert "bank" not in listed
    apps.list_replica_set_for_all_namespaces.assert_not_called()


@pytest.mark.anyio
async def test_shared_gitops_namespaces_show_only_the_contracts_own_deploys(
    two_contracts: Contracts, settings: Settings, fake_cluster: Any
) -> None:
    """argocd and flux-system are shared by every team on the cluster: demo-shop sees
    its Applications and Kustomizations, never bank's, nor one that spans both, nor
    one that deploys to another cluster."""
    from tests.test_kubernetes_server import ago, app, fake_custom

    shop = two_contracts["demo-shop"]
    assert shop.clusters[0].gitops_namespaces == ["argocd", "flux-system"]
    history = [{"revision": "r1", "deployedAt": ago(5)}]
    bank_ks = {
        "metadata": {"name": "bank-ledger", "namespace": "flux-system"},
        "spec": {"targetNamespace": "bank"},
        "status": {"history": [{"firstReconciled": ago(3), "metadata": {"revision": "bank@1"}}]},
    }
    # Another cluster's orders namespace (where another Contract may run): its
    # objects sit in this cluster's flux-system but deploy through a kubeconfig.
    remote = {"kubeConfig": {"secretRef": {"name": "other-cluster"}}, "targetNamespace": "orders"}
    elsewhere_ks = {
        "metadata": {"name": "elsewhere-ks", "namespace": "flux-system"},
        "spec": remote,
        "status": {"history": [{"firstReconciled": ago(2), "metadata": {"revision": "x@1"}}]},
    }
    elsewhere_hr = {
        "metadata": {"name": "elsewhere-hr", "namespace": "flux-system"},
        "spec": remote,
        "status": {"history": [{"version": 1, "namespace": "orders", "lastDeployed": ago(2)}]},
    }
    unnamed = app("unnamed-dest", "orders", history=history)
    del unnamed["spec"]["destination"]["server"]
    custom = fake_custom(
        {
            ("argocd", "applications"): [
                app("orders-inventory", "orders", history=history),
                app("bank-vault", "bank", history=history),
                app("spans", "orders", history=history, resources=[{"namespace": "bank"}]),
                unnamed,
            ],
            ("flux-system", "kustomizations"): [bank_ks, elsewhere_ks],
            ("flux-system", "helmreleases"): [elsewhere_hr],
        }
    )
    tb = agent.build_mcp(shop, settings, {"kind": (*fake_cluster, custom)})
    async with Client(tb.mcp) as client:
        result = await client.call_tool("kubernetes_recent_changes", {"cluster": "kind"})
    out = result.structured_content["result"]
    assert [c["argocd_application"] for c in out] == ["orders-inventory"]
    for trace in ("bank", "spans", "elsewhere", "unnamed"):
        assert trace not in json.dumps(out)


@pytest.mark.anyio
async def test_runbooks_never_cross_contracts(
    two_contracts: Contracts, settings: Settings, fake_cluster: Any
) -> None:
    async def runbooks(cid: str, alert: str, keywords: str) -> tuple[list[str], list[str]]:
        tb = tools_for(two_contracts[cid], settings, fake_cluster)
        async with Client(tb.mcp) as client:
            hit = await client.call_tool("runbooks_runbook_for_alert", {"alertname": alert})
            found = await client.call_tool("runbooks_search_runbooks", {"keywords": keywords})
        mapped = [b["title"] for b in hit.structured_content["runbooks"]]
        return mapped, [b["title"] for b in found.structured_content["result"]]

    shop_mapped, shop_found = await runbooks("demo-shop", "KubePodCrashLooping", "ledger")
    bank_mapped, bank_found = await runbooks("demo-bank", "KubePodCrashLooping", "checkout")
    # A Contract's own Runbook overrides the shared one with the same name.
    assert shop_mapped == ["Crash loop (demo-shop's own)"]
    assert bank_mapped == ["Crash loop (shared)"]
    assert shop_found == [] and bank_found == []


@pytest.mark.anyio
async def test_audit_lines_name_the_contract(
    two_contracts: Contracts,
    settings: Settings,
    fake_cluster: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="squero.audit")
    bank = tools_for(two_contracts["demo-bank"], settings, fake_cluster)
    async with Client(bank.mcp) as client:
        await client.call_tool(
            "kubernetes_list_pods", {"cluster": "bank-kind", "namespace": "bank"}
        )
    lines = [json.loads(r.message) for r in caplog.records if r.name == "squero.audit"]
    assert [(line["contract"], line["tool"]) for line in lines] == [
        ("demo-bank", "kubernetes_list_pods")
    ]


def test_each_contract_uses_its_own_prometheus_credentials(
    two_contracts: Contracts, settings: Settings, fake_cluster: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from squero.servers import prometheus

    seen: list[tuple[str, str]] = []

    class Recorder:
        def __init__(self, base_url: str, auth: Any, **_: Any) -> None:
            sent = next(auth.auth_flow(httpx.Request("GET", base_url))) if auth else None
            seen.append((base_url, sent.headers["Authorization"] if sent else ""))

    monkeypatch.setattr(prometheus.httpx, "Client", Recorder)
    for cid in ("demo-shop", "demo-bank"):
        tools_for(two_contracts[cid], settings, fake_cluster)
    assert seen == [
        ("http://localhost:9090", "Bearer shop-demo-token"),
        ("http://localhost:9091", "Bearer bank-demo-token"),
    ]


@pytest.mark.anyio
async def test_a_question_for_one_contract_cannot_reach_anothers_cluster(
    two_contracts: Contracts, settings: Settings, fake_cluster: Any
) -> None:
    from langchain.messages import ToolMessage

    from tests.conftest import call, scripted

    answer = {"answer": "I can't see bank-kind.", "evidence": [{"source": "x", "finding": "y"}]}
    model = scripted(
        call("kubernetes_list_pods", "q1", cluster="bank-kind", namespace="bank"),
        call("Answer", "q2", **answer),
    )
    shop = tools_for(two_contracts["demo-shop"], settings, fake_cluster)
    async with agent.open_agent(settings, shop, model, kind="question") as a:
        await agent.ask(a, "What runs in bank-kind?", "iso-q")
        state = await a.aget_state({"configurable": {"thread_id": "iso-q"}})
    (result,) = [
        m for m in state.values["messages"] if isinstance(m, ToolMessage) and m.tool_call_id == "q1"
    ]
    assert result.status == "error" and "not one of this Contract's clusters" in str(result.content)
    fake_cluster[0].list_namespaced_pod.assert_not_called()


@pytest.mark.anyio
async def test_a_vendor_process_sees_only_its_own_contracts_secrets(
    two_contracts: Contracts,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    fake_cluster: Any,
) -> None:
    """Each Contract's vendor subprocess gets its own secrets, and none of the Hub's
    environment (model key, approver token, database URL, other Contracts)."""
    import sys
    from pathlib import Path

    from squero.contracts import Vendor
    from squero.servers import vendors

    fixture = Path(__file__).parent / "fixtures" / "vendor_server.py"
    monkeypatch.setitem(
        vendors.VENDORS,
        "fixture",
        vendors.Spec((sys.executable, str(fixture)), frozenset({"TOKEN"})),
    )
    monkeypatch.setenv("SQUERO_MODEL_API_KEY", "hub-only-model-key")
    seen = {}
    for cid, c in two_contracts.items():
        vendor = Vendor(
            name="x",
            vendor="fixture",
            env_secrets={"TOKEN": "prometheus-token"},
            tools={"environment": "read"},
        )
        mine = c.model_copy(update={"integrations": [*c.integrations, vendor]})
        async with Client(tools_for(mine, settings, fake_cluster).mcp) as client:
            result = await client.call_tool("x_environment", {})
        seen[cid] = json.loads(result.content[0].text)
    for cid, env in seen.items():
        assert env["TOKEN"] == two_contracts[cid].secret("prometheus-token").get_secret_value()
        assert not any(name.startswith("SQUERO_") for name in env), env
        assert "hub-only-model-key" not in json.dumps(env)
    assert seen["demo-shop"]["TOKEN"] != seen["demo-bank"]["TOKEN"]


@pytest.mark.anyio
async def test_a_stored_result_is_readable_only_in_its_own_investigation(
    two_contracts: Contracts, settings: Settings, fake_cluster: Any
) -> None:
    """One checkpoint store for every Contract, as in the Hub: demo-bank's Investigation
    asking for demo-shop's stored result id gets nothing of it."""
    from kubernetes import client as k
    from langgraph.checkpoint.memory import InMemorySaver

    from tests.conftest import REPORT, call, crashlooping_pod, scripted
    from tests.test_agent import ALERT, agent_result_id

    pods = [crashlooping_pod() for _ in range(60)]  # a listing far over the limit
    pods[-1].metadata.name = "shop-only-pod"
    fake_cluster[0].list_namespaced_pod.return_value = k.V1PodList(items=pods)
    settings.tool_output_limit = 1000
    rid = agent_result_id("s1")
    saver = InMemorySaver(serde=agent.CHECKPOINT_SERDE)
    where = {"cluster": "kind", "namespace": "demo"}
    shop = scripted(
        call("kubernetes_list_pods", "s1", **where), call("TriageReport", "s2", **REPORT)
    )
    async with agent.open_agent(
        settings, tools_for(two_contracts["demo-shop"], settings, fake_cluster), shop, saver
    ) as a:
        await agent.start(a, ALERT, "shop-1")
        state = await a.aget_state({"configurable": {"thread_id": "shop-1"}})
        assert "shop-only-pod" in state.values["spilled"][rid]
    bank = scripted(
        call("read_more", "b1", result_id=rid, grep="shop-only"),
        call("TriageReport", "b2", **REPORT),
    )
    async with agent.open_agent(
        settings, tools_for(two_contracts["demo-bank"], settings, fake_cluster), bank, saver
    ) as a:
        await agent.start(a, ALERT, "bank-1")
        state = await a.aget_state({"configurable": {"thread_id": "bank-1"}})
    answer = next(str(m.content) for m in state.values["messages"] if m.type == "tool")
    assert answer == f"no stored result {rid!r} in this Investigation; ids: []"
    assert "shop-only" not in json.dumps([str(m.content) for m in state.values["messages"]])


@pytest.mark.anyio
async def test_an_alert_group_never_joins_another_contracts_triage(
    two_contracts: Contracts, database_url: str
) -> None:
    """Identical labels on the same physical cluster: demo-bank's group finds no open
    Triage of demo-shop's to join."""
    from squero import related
    from squero.models import AlertGroup, Investigation
    from squero.store import Store, connect, now

    labels = {"alertname": "Errors", "cluster": "kind", "service": "ledger"}
    group = {
        "status": "firing",
        "groupKey": "g",
        "commonLabels": labels,
        "alerts": [{"status": "firing", "labels": labels}],
    }
    async with connect(database_url) as pool:
        store = Store(pool)
        await store.migrate()
        shop = Investigation(
            id="shop-t",
            contract_id="demo-shop",
            title="t",
            group_key="other",
            started=now(),
            input=group,
        )
        await store.insert(shop)
        assert [t.id for t in await store.open_triages("demo-shop", now() - 60, "g")] == ["shop-t"]
        candidates = await store.open_triages("demo-bank", now() - 60, "g")
    assert candidates == []
    bank = two_contracts["demo-bank"]
    assert related.find(AlertGroup.model_validate(group), candidates, bank.related) is None


@pytest.mark.anyio
async def test_a_related_group_is_separated_only_through_its_own_triage(
    two_contracts: Contracts, database_url: str
) -> None:
    """demo-bank's related group, named with demo-shop's Triage: not found, and the
    claim inserts nothing (no orphan Triage)."""
    from squero.models import AlertGroup, Investigation
    from squero.store import Store, connect, now

    labels = {"alertname": "Errors", "service": "ledger"}
    group = {
        "status": "firing",
        "groupKey": "g",
        "commonLabels": labels,
        "alerts": [{"status": "firing", "labels": labels}],
    }
    async with connect(database_url) as pool:
        store = Store(pool)
        await store.migrate()
        shop, bank = (
            Investigation(id=f"{c}-t", contract_id=c, title="t", group_key="g0", started=now())
            for c in ("demo-shop", "demo-bank")
        )
        await store.insert(shop)
        await store.insert(bank)
        rid = await store.add_related(
            bank.id,
            "demo-bank",
            AlertGroup.model_validate(group),
            {"service": "ledger"},
            False,
            now(),
        )
        assert await store.related_group(rid, shop.id) is None
        new = Investigation(id="new", contract_id="demo-shop", title="t", started=now())
        assert not await store.separate(rid, shop.id, new)
        assert await store.get("new") is None
        assert (await store.related_group(rid, bank.id) or {})["separate_id"] is None


@pytest.mark.anyio
async def test_past_incidents_never_cross_contracts(
    two_contracts: Contracts, settings: Settings, fake_cluster: Any, database_url: str
) -> None:
    """The same words in both Contracts' past Reports: each finds and reads only its own."""
    from squero.models import Investigation
    from squero.store import Store, connect, now
    from tests.conftest import REPORT

    async with connect(database_url) as pool:
        store = Store(pool)
        await store.migrate()
        for cid in two_contracts:
            report = {**REPORT, "summary": "ledger OOMKilled: batch size unset"}
            t = Investigation(
                id=f"{cid}-past", contract_id=cid, title="t", status="done", started=now(),
                report=report,
            )  # fmt: skip
            await store.insert(t)
        for cid, c in two_contracts.items():
            apis = {cl.name: fake_cluster for cl in c.clusters}
            tb = agent.build_mcp(c, settings, apis, store=store)
            other = next(o for o in two_contracts if o != cid)
            async with Client(tb.mcp) as client:
                found = await client.call_tool("incidents_search", {"keywords": "ledger batch"})
                assert [i["id"] for i in found.structured_content["incidents"]] == [f"{cid}-past"]
                assert (await client.call_tool("incidents_read", {"id": f"{cid}-past"})).data
                with pytest.raises(ToolError, match="no past Report"):
                    await client.call_tool("incidents_read", {"id": f"{other}-past"})


@pytest.mark.anyio
async def test_past_incidents_by_meaning_never_cross_contracts(
    two_contracts: Contracts, database_url: str
) -> None:
    """The same vector in both Contracts' past Reports: a search by meaning finds only
    its own, and one Contract can't set another's embedding."""
    from squero.models import Investigation
    from squero.servers import incidents
    from squero.store import Store, connect, now
    from tests.conftest import REPORT
    from tests.test_embeddings import MODEL, Fake, vec

    async with connect(database_url) as pool:
        store = Store(pool)
        await store.migrate()
        for cid in two_contracts:
            t = Investigation(
                id=f"{cid}-past", contract_id=cid, title="t", status="done", started=now(),
                report={**REPORT, "summary": "certificate expired"},
            )  # fmt: skip
            await store.insert(t)
            assert await store.set_embedding(cid, f"{cid}-past", MODEL, vec(0))
        for cid in two_contracts:
            other = next(o for o in two_contracts if o != cid)
            assert not await store.set_embedding(cid, f"{other}-past", MODEL, vec(1))
            embedder = Fake(store, {"x509": vec(0)})
            embedder.contract_id = cid
            async with Client(incidents.build_server(cid, store, embedder)) as client:
                found = await client.call_tool("search", {"keywords": "x509 handshake"})
            assert [i["id"] for i in found.structured_content["incidents"]] == [f"{cid}-past"]


@pytest.mark.anyio
async def test_checks_never_cross_contracts(two_contracts: Contracts, database_url: str) -> None:
    """Two Contracts with a Check of the same name: each has its own schedule row, a
    claim fires only its own, and an Open failure is set only on its own."""
    from squero.models import Investigation
    from squero.store import Store, connect

    async with connect(database_url) as pool:
        store = Store(pool)
        await store.migrate()
        for cid in two_contracts:
            await store.sync_checks(cid, {"healthy": (f"{cid} key", 100.0)})
        a, b = list(two_contracts)
        t = Investigation(id="ta", kind="check", contract_id=a, title="t", started=100.0)
        assert await store.claim_check(t, "healthy", 100.0, 200.0, None) == "started"
        assert await store.set_failing(a, "healthy", "ta", None)
        mine, theirs = (
            await store.check_schedule(a, "healthy"),
            await store.check_schedule(b, "healthy"),
        )
        assert mine and theirs
        assert (mine["next_due"], mine["last_id"], mine["failing_id"]) == (200.0, "ta", "ta")
        assert (theirs["next_due"], theirs["last_id"], theirs["failing_id"]) == (100.0, None, None)
        assert theirs["schedule"] == f"{b} key"
        assert [r["contract_id"] for r in await store.check_schedules(b)] == [b]


@pytest.mark.anyio
async def test_deploy_verifications_never_cross_contracts(
    two_contracts: Contracts, database_url: str
) -> None:
    """The same cluster and namespace names in both Contracts: separate pending rows,
    and one Contract's Investigation can't claim the other's."""
    from squero.models import Investigation
    from squero.store import Store, connect

    async with connect(database_url) as pool:
        store = Store(pool)
        await store.migrate()
        a, b = list(two_contracts)
        for cid in (a, b):
            change = [{"namespace": "shop", "object": f"deployment {cid}-app", "revision": "2"}]
            await store.queue_deploy(cid, "kind", "shop", change, 0, 60)
        due = {r["contract_id"]: r for r in await store.due_deploys(60)}
        assert sorted(due) == sorted([a, b])
        assert due[b]["changes"][0]["object"] == f"deployment {b}-app"
        theirs = Investigation(id="x", kind="check", contract_id=a, title="t", started=60)
        assert await store.claim_deploy(due[b]["id"], theirs, "deploy:kind/shop", 60, None) == (
            "not due"
        )
        assert await store.get("x") is None
        assert [r["contract_id"] for r in await store.deploy_checks(b)] == [b]


@pytest.mark.anyio
async def test_teachings_never_cross_contracts(two_contracts: Contracts, database_url: str) -> None:
    """Each Contract's approved Teachings are its own, and one Contract's Teaching can't
    be decided or retired as another's."""
    from squero.store import Store, connect

    async with connect(database_url) as pool:
        store = Store(pool)
        await store.migrate()
        ids = {}
        for cid in two_contracts:
            ids[cid, "approved"] = await store.add_teaching(
                cid, f"{cid} lesson", [], approved_by="x"
            )
            ids[cid, "proposed"] = await store.add_teaching(cid, f"{cid} draft", [])
        for cid in two_contracts:
            other = next(o for o in two_contracts if o != cid)
            assert [t["text"] for t in await store.teachings(cid, "approved")] == [f"{cid} lesson"]
            assert await store.decide_teaching(ids[other, "proposed"], cid, True, "x") is None
            assert not await store.retire_teaching(ids[other, "approved"], cid, "x")
        assert {t["status"] for t in await store.teachings()} == {"approved", "proposed"}


def test_each_contract_sizes_with_its_own_policies(two_contracts: Contracts) -> None:
    """Both demo Contracts name a policy lean-agent, with other numbers: each gets its
    own, and a rule of one never picks a policy for the other's Workloads."""
    shop, bank = two_contracts["demo-shop"].sizing, two_contracts["demo-bank"].sizing
    shop_lean, bank_lean = shop.policy("lean-agent"), bank.policy("lean-agent")
    assert shop_lean and bank_lean
    assert (shop_lean.cpu.headroom, shop_lean.window) == (0.10, 7 * 86400)
    assert (bank_lean.cpu.headroom, bank_lean.window) == (0.30, 14 * 86400)
    assert shop.policy_for("kind", "lab", "DaemonSet", "agent", "agent") == "lean-agent"
    assert bank.policy_for("kind", "lab", "DaemonSet", "agent", "agent") == "production"


@pytest.mark.anyio
async def test_a_digest_shows_only_its_own_contracts_workloads(
    two_contracts: Contracts, database_url: str
) -> None:
    """Both demo Contracts store Sizings for a Workload of the same name: each one's
    digest names only its own cluster's, and its schedule claim is its own."""
    from squero import sizing
    from squero.store import Store, connect
    from tests.test_sizing_store import NOW, container, rows_of

    async with connect(database_url) as pool:
        store = Store(pool)
        await store.migrate()
        for cid, c in two_contracts.items():
            run = await store.claim_sizing_run(cid, NOW, 0)
            assert run is not None
            cluster = c.clusters[0].name
            rows = [
                {**r, "cluster": cluster, "workload": f"{cluster}/{r['workload'].split('/', 1)[1]}"}
                for r in rows_of(container(pods=1))
            ]
            await store.finish_sizing_run(run, cid, {cluster: None}, rows)
        await store.sync_digests({cid: ("key", NOW) for cid in two_contracts})
        assert await store.claim_digest("demo-shop", NOW, NOW + 1)
        assert await store.claim_digest("demo-bank", NOW, NOW + 1)  # not taken by shop's
        for cid, c in two_contracts.items():
            text = await sizing.digest(store, c, NOW, None)
            own, other = ("kind", "bank-kind") if cid == "demo-shop" else ("bank-kind", "kind")
            assert f"`{own}/shop/Deployment/api`" in text
            assert f"`{other}/" not in text
            assert ("USD/month" in text) == (cid == "demo-shop")  # its prices only (10.1)


@pytest.mark.anyio
async def test_sizing_for_workload_reads_only_its_own_contracts(
    two_contracts: Contracts, database_url: str
) -> None:
    """Both demo Contracts store a Sizing for a Workload named api: each one's tool
    finds only its own, and can't name the other's cluster."""
    from squero.servers import sizing as tool
    from squero.store import Store, connect
    from tests.test_sizing_store import NOW, container, rows_of

    where = {
        cid: (c.clusters[0].name, sorted(c.clusters[0].namespaces)[0])
        for cid, c in two_contracts.items()
    }
    async with connect(database_url) as pool:
        store = Store(pool)
        await store.migrate()
        for cid, (cluster, namespace) in where.items():
            rows = [
                {
                    **r,
                    "cluster": cluster,
                    "namespace": namespace,
                    "workload": f"{cluster}/{namespace}/Deployment/api/app",
                }
                for r in rows_of(container(pods=1))
            ]
            run = await store.claim_sizing_run(cid, NOW, 0)
            assert run is not None
            await store.finish_sizing_run(run, cid, {cluster: None}, rows)
        for cid, c in two_contracts.items():
            (cluster, namespace), (other, theirs) = (
                where[cid],
                next(v for k, v in where.items() if k != cid),
            )
            async with Client(tool.build_server(c, store)) as client:
                args = {"cluster": cluster, "namespace": namespace, "name": "api"}
                mine = await client.call_tool("for_workload", args, raise_on_error=False)
                cross = await client.call_tool(
                    "for_workload",
                    {"cluster": other, "namespace": theirs, "name": "api"},
                    raise_on_error=False,
                )
            assert mine.structured_content is not None
            (w,) = mine.structured_content["workloads"]
            assert w["workload"] == f"{cluster}/{namespace}/Deployment/api"
            assert cross.is_error


@pytest.mark.anyio
async def test_promql_names_only_the_contracts_own_clusters(
    two_contracts: Contracts, settings: Settings
) -> None:
    """Each demo Contract's prometheus tools take only its own cluster, and a call
    naming the other's is refused before any request."""
    for cid, c in two_contracts.items():
        own = c.clusters[0].name
        other = next(o.clusters[0].name for k, o in two_contracts.items() if k != cid)
        prom = next(i for i in c.integrations if i.name == "prometheus")
        server, _ = agent.integration_server(c, prom, settings)
        async with Client(server) as client:
            tools = {t.name: t for t in await client.list_tools()}
            enum = tools["query"].input_schema["properties"]["cluster"]["enum"]
            r = await client.call_tool(
                "query", {"cluster": other, "promql": "up"}, raise_on_error=False
            )
        assert enum == [own]
        assert r.is_error


@pytest.mark.anyio
async def test_no_promql_on_a_prometheus_another_contract_reads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, settings: Settings
) -> None:
    """demo-bank pointed at demo-shop's Prometheus, in its own namespaces: loading
    allows it (the Hub's queries are scoped), but free PromQL could read the other's
    series, so neither gets PromQL tools or a Prometheus check; the Kubernetes tools stay."""
    import shutil

    from tests.conftest import DEMO_CONTRACTS, use_age_keys

    use_age_keys(monkeypatch, tmp_path, "demo-shop", "demo-bank")
    copy = tmp_path / "contracts"
    shutil.copytree(DEMO_CONTRACTS, copy, ignore=shutil.ignore_patterns("test-keys"))
    bank = copy / "demo-bank" / "contract.yaml"
    bank.write_text(bank.read_text().replace("http://localhost:9091", "http://localhost:9090/"))
    loaded = contracts.load(copy)
    for c in loaded.values():
        assert c.shared_stores == {c.clusters[0].name}
        prom = next(i for i in c.integrations if i.name == "prometheus")
        server, _ = agent.integration_server(c, prom, settings)
        async with Client(server) as client:
            assert await client.list_tools() == []
        assert not [p for p in agent.probes(c) if p[1] == "prometheus_query"]
    apart = contracts.load(DEMO_CONTRACTS)  # their own Prometheus each: PromQL stays
    assert all(not c.shared_stores for c in apart.values())


def test_the_same_value_masks_differently_in_each_contract(two_contracts: Contracts) -> None:
    """Each Contract keys its placeholders with its own mask-key: one Contract's
    placeholder means nothing in another's request (slice 6.4)."""
    shop = masking.for_contract(two_contracts["demo-shop"])
    bank = masking.for_contract(two_contracts["demo-bank"])
    assert shop and bank
    text = "10.9.8.7 paged ops@example.org"
    seen_shop: masking.Seen = {}
    seen_bank: masking.Seen = {}
    in_shop, in_bank = shop.mask(text, seen_shop), bank.mask(text, seen_bank)
    assert in_shop != in_bank and "10.9.8.7" not in in_shop + in_bank
    assert masking.unmask(in_shop, seen_bank) == in_shop  # the bank can't read the shop's
    assert masking.unmask(in_shop, seen_shop) == text
    assert "shop.example" not in shop.mask("db.shop.example", {})
    assert "db.shop.example" in bank.mask("db.shop.example", {})  # not one of its terms
