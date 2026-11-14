"""The Hub MCP server (slice 11.1a): both doors, each call within one Contract, that
Contract's Data policy on everything returned (errors included), placeholders back
only for the Contract that handed them out, an audit line per call, and none of it in
the Investigation agent's tools."""

import asyncio
import json
import logging
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from fastmcp import Client
from pydantic import SecretStr

from squero import agent, cli, contracts, hubmcp, masking, webapp
from squero.api import create_app
from squero.config import Settings
from squero.models import Answer, Investigation, TriageReport
from squero.store import Store, connect
from tests.conftest import REPORT, make_contract
from tests.conftest import call as call_step
from tests.test_api import client, scripted, wait_for
from tests.test_console import OPERATOR
from tests.test_sizing_store import NOW, row, stored
from tests.test_webapp import INTEGRATIONS, PROM, seed

TOKEN = "t" * 40
MCP = {"Authorization": f"Bearer {TOKEN}", "Tailscale-User-Login": OPERATOR}
ACCEPT = {"Accept": "application/json, text/event-stream"}
# acme masks its own id and its namespace; bank masks nothing
ACME = {
    "integrations": INTEGRATIONS,
    "data_policy": {"mask": {"terms": ["acme", "shop"]}},
    "secrets": {contracts.MASK_KEY: "k-acme"},
}
TOOLS = {
    "list_contracts",
    "sizings",
    "sizing",
    "cost",
    "savings",
    "bill",
    "investigations",
    "investigation",
    "checks",
    "ask",
    "decide",
    "turn_on_kill_switch",
    "approve_sizing",
    "revert_sizing",
    "propose_change",
    "now",
}


@pytest.fixture(autouse=True)
def _clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("squero.api.now", lambda: NOW)


def hub(
    settings: Settings,
    fake_cluster: Any,
    database_url: str,
    clusters_of_bank: bool = False,
    acme_clusters: dict[str, list[str]] | None = None,
    steps: list[Any] = (),  # type: ignore[assignment]
    acme_extra: dict[str, Any] | None = None,
    slack: Any = None,
    channel: str | None = None,
    github: Any = None,
) -> TestClient:
    settings.database_url = SecretStr(database_url)
    settings.console_operators = {OPERATOR}
    settings.mcp_token = SecretStr(TOKEN)
    clusters = {"test": ["shop"], "edge": ["shop"]} if clusters_of_bank else None
    bank = make_contract(contract_id="bank", integrations=INTEGRATIONS, clusters=clusters)
    acme = {**ACME, "clusters": acme_clusters} if acme_clusters else ACME
    acme = {**acme, **(acme_extra or {})}
    more = {"bank": (bank, fake_cluster)}
    return client(
        settings,
        fake_cluster,
        *steps,
        acme=acme,
        more=more,
        slack=slack,
        acme_channel=channel,
        github_http=github,
    )


def rpc(c: TestClient, method: str, params: dict[str, Any], headers: Any = None) -> Any:
    return c.post(
        "/mcp/",
        headers={**ACCEPT, **(MCP if headers is None else headers)},
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
    )


def call(c: TestClient, tool: str, **args: Any) -> Any:
    """The tool's structured answer, or its error text as {"error": ...}."""
    r = rpc(c, "tools/call", {"name": tool, "arguments": args})
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    if result.get("isError"):
        return {"error": result["content"][0]["text"]}
    content = result["structuredContent"]
    return content.get("result", content)  # a list comes wrapped


def mask(value: str) -> str:
    return masking.Masker(b"k-acme", ["acme", "shop"]).placeholder("name", value)


def seed_run(database_url: str, cid: str, *rows: dict[str, Any]) -> None:
    """One Sizing run at NOW with these rows."""

    async def go() -> None:
        async with connect(database_url) as pool:
            store = Store(pool)
            await store.migrate()
            await stored(store, cid, NOW, {r["cluster"]: None for r in rows}, *rows)

    asyncio.run(go())


# --- the doors -------------------------------------------------------------------------


def test_a_call_needs_the_token_and_a_listed_operators_identity(
    settings: Settings, fake_cluster: Any, database_url: str
) -> None:
    init = {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "t", "version": "1"},
    }
    bearer = {"Authorization": f"Bearer {TOKEN}"}
    with hub(settings, fake_cluster, database_url) as c:
        refused = [
            rpc(c, "initialize", init, h).status_code
            for h in (
                {"Tailscale-User-Login": OPERATOR},  # no token
                {"Authorization": "Bearer approver-token", "Tailscale-User-Login": OPERATOR},
                {"Authorization": f"Bearer {TOKEN}x", "Tailscale-User-Login": OPERATOR},
                bearer,  # no identity: not through tailscale serve
                {**bearer, "Tailscale-User-Login": "mallory@example.com"},
            )
        ]
        ok = rpc(c, "initialize", init, {**MCP, "Host": "hub.tail1234.ts.net"})
        listed = rpc(c, "tools/list", {})
    assert refused == [401, 401, 401, 403, 403]
    assert "result" in ok.json(), ok.text
    assert ok.json()["result"]["serverInfo"]["name"] == "squero-hub"
    tools = {t["name"]: t for t in listed.json()["result"]["tools"]}
    assert set(tools) == TOOLS


def test_it_is_off_without_its_token(
    settings: Settings, fake_cluster: Any, database_url: str
) -> None:
    settings.database_url = SecretStr(database_url)
    settings.console_operators = {OPERATOR}
    with client(settings, fake_cluster) as c:
        assert rpc(c, "tools/list", {}).status_code == 404


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"console_operators": set()}, "needs SQUERO_CONSOLE_OPERATORS"),
        ({"mcp_token": SecretStr("t" * 31)}, "too short"),
        ({"mcp_token": SecretStr("a" * 32), "approver_token": SecretStr("a" * 32)}, "approver"),
        ({"mcp_token": SecretStr("h" * 32)}, "webhook token is the MCP token"),
    ],
)
def test_it_refuses_to_start_on_a_token_that_would_open_another_door(
    settings: Settings, change: dict[str, Any], error: str
) -> None:
    settings.approver_token = SecretStr("approver-token")
    settings.console_operators = {OPERATOR}
    settings.mcp_token = SecretStr(TOKEN)
    for k, v in change.items():
        setattr(settings, k, v)
    acme = make_contract(webhook_token="h" * 32)
    with pytest.raises(RuntimeError, match=error):
        create_app(settings, scripted(), by_id={"acme": acme})


def test_a_token_of_32_is_enough(settings: Settings, database_url: str) -> None:
    settings.approver_token = SecretStr("approver-token")
    settings.database_url = SecretStr(database_url)
    settings.console_operators = {OPERATOR}
    settings.mcp_token = SecretStr("t" * 32)
    create_app(settings, scripted(), by_id={"acme": make_contract()})


def test_the_agent_never_gets_these_tools(settings: Settings, fake_cluster: Any) -> None:
    c = make_contract(integrations=INTEGRATIONS)
    names = set(agent.build_mcp(c, settings, {"test": fake_cluster}).access)
    assert "kubernetes_list_pods" in names and not names & TOOLS


# --- within one Contract, by its Data policy ---------------------------------------------


def test_contracts_are_listed_masked_by_their_own_policy(
    settings: Settings, fake_cluster: Any, database_url: str
) -> None:
    with hub(settings, fake_cluster, database_url) as c:
        listed = call(c, "list_contracts")
    assert listed == [
        {"id": mask("acme"), "name": mask("acme"), "clusters": ["test"], "kill_switch": False},
        {"id": "bank", "name": "bank", "clusters": ["test"], "kill_switch": False},
    ]


def test_sizings_rank_filter_and_mask_and_take_the_placeholder_back(
    settings: Settings, fake_cluster: Any, database_url: str
) -> None:
    risk = {"kind": "oom_killed", "text": "OOM-killed in shop"}
    seed_run(
        database_url,
        "acme",
        row("api", waste={"cpu": 2.0, "memory": 0.0}),
        row("web", waste={"cpu": 3.0, "memory": 0.0}, risks=[risk]),
        row("db", waste={"cpu": 1.0, "memory": 0.0}),
    )
    with hub(settings, fake_cluster, database_url) as c:
        acme = call(c, "list_contracts")[0]["id"]
        ranked = call(c, "sizings", contract=acme, top=2)
        by_real_id = call(c, "sizings", contract="acme", top=2)
        risky = call(c, "sizings", contract=acme, risky=True)
        in_shop = call(c, "sizings", contract=acme, namespace=mask("shop"), cluster="test")
        elsewhere = call(c, "sizings", contract=acme, namespace="other")
        unknown = call(c, "sizings", contract=acme, cluster="nope")
        never = call(c, "sizings", contract="<name-00000000>")
    shop = mask("shop")
    assert [w["workload"] for w in ranked["workloads"]] == [
        f"test/{shop}/Deployment/web",
        f"test/{shop}/Deployment/api",
    ]
    assert ranked == by_real_id and ranked["matching"] == 3
    assert ranked["workloads"][0]["waste"] == {"cpu": "3000m", "memory": "0Mi"}
    assert ranked["workloads"][0]["risks"] == [f"OOM-killed in {shop}"]
    assert ranked["clusters"] == [
        {"cluster": "test", "as_of": "1972-09-27 00:00 UTC", "failing": None, "since": None}
    ]
    assert [w["workload"] for w in risky["workloads"]] == [f"test/{shop}/Deployment/web"]
    assert in_shop["matching"] == 3 and elsewhere["matching"] == 0
    assert unknown == {"error": "cluster 'nope' has no Sizings: one of ['test']"}
    assert never == {"error": "unknown Contract: list_contracts names them"}
    assert "shop" not in json.dumps([ranked, risky, in_shop]) and "acme" not in json.dumps(ranked)


def test_sizings_list_each_autoscaled_workloads_floor_masked_and_filtered(
    settings: Settings, fake_cluster: Any, database_url: str
) -> None:
    """Slice 14.1b: a line per stored Floor of the Contract, in the namespace asked for,
    its names masked like the rest; never another Contract's."""
    where = {"cluster": "test", "kind": "Deployment", "hpa": "api", "policy": "production"}
    ready = {"percentile": 0.1, "minimum": 4.0, "maximum": 9.0, "risks": [], "state": "ready"}
    ready |= {"history_h": 30.0, "needs_h": 24.0, "floor": 2, "replicas": 2.0}
    ready |= {"saving": {"cpu": 0.2, "memory": 256.0 * 2**20}}
    floor = {
        **where,
        **ready,
        "workload": "test/shop/Deployment/api",
        "namespace": "shop",
        "name": "api",
    }
    other = {**floor, "workload": "test/other/Deployment/far", "namespace": "other", "name": "far"}
    weekly = {"state": "learning", "timezone": "UTC", "lead_time": 900}
    other["profile"] = {**weekly, "history_h": 100.0, "needs_h": 504.0}  # slice 14.2
    test = {"state": "over", "error": 0.34, "few": 0.21, "many": 0.13, "hours": 168}
    test |= {"replicas": 0.4, "excess": {"cpu": 0.04, "memory": 2.0**20}}
    odd = {**weekly, "state": "irregular", "history_h": 672.0, "needs_h": 504.0}  # slice 14.4
    odd |= {"blocks": [], "backtest": test}
    seed_run(database_url, "acme", row("api"))

    async def go() -> None:
        async with connect(database_url) as pool:
            store = Store(pool)
            await store.save_floors("acme", "test", NOW, [floor, other])
            await store.save_floors(
                "bank", "test", NOW, [{**floor, "floor": 9, "hpa": "keda-hpa-api", "profile": odd}]
            )
            far = {**floor, "workload": "edge/shop/Deployment/api", "cluster": "edge", "floor": 7}
            await store.save_floors("bank", "edge", NOW, [far])

    asyncio.run(go())
    with hub(settings, fake_cluster, database_url, clusters_of_bank=True) as c:
        every = call(c, "sizings", contract="acme")
        edge = call(c, "sizings", contract="bank", cluster="edge")["floors"]
        (irregular,) = call(c, "sizings", contract="bank", cluster="test")["floors"]
        in_shop = call(c, "sizings", contract="acme", namespace=mask("shop"))
        elsewhere = call(c, "sizings", contract="acme", cluster="test", namespace="nowhere")
    line = (
        f"test/{mask('shop')}/Deployment/api: Floor 2 (HPA api: minimum 4, maximum 9; production: "
        "10% of 30 h of Needs) | 2 replicas fewer on average: 0.20 cores, 0.25 GiB"
    )
    assert in_shop["floors"] == [line] and elsewhere["floors"] == []
    assert len(every["floors"]) == 2 and line in every["floors"]
    (far,) = [f for f in every["floors"] if f != line]
    assert far.splitlines()[1] == "  Weekly profile learning (100 h of 504 h of Needs)"
    assert len(edge) == 1 and "Floor 7 " in edge[0]  # one cluster of the Contract's two
    assert irregular.splitlines()[1:] == [  # over its Backtest's budget: no blocks, no triggers
        "  load isn't weekly enough: off by 34% last week (too few 21%, too many 13%); "
        "it keeps its Floor"
    ]
    assert "load isn't weekly" not in json.dumps(every)


def test_sizings_give_a_profiles_backtest_with_what_its_too_many_cost(
    settings: Settings, fake_cluster: Any, database_url: str
) -> None:
    """Slice 14.4: within its budget a Weekly profile comes with its Backtest, the too
    many in the Contract's currency."""
    test = {"state": "within", "error": 0.12, "few": 0.04, "many": 0.08, "hours": 168}
    test |= {"replicas": 0.4, "excess": {"cpu": 0.5, "memory": 2.0**30}}
    block = {"since": "Mon 09:00", "until": "Mon 12:00", "replicas": 7}
    weekly = {"state": "ready", "timezone": "UTC", "lead_time": 0, "history_h": 672.0}
    weekly |= {"needs_h": 504.0, "backtest": test}
    weekly |= {"blocks": [{**block, "start": "0 9 * * 1", "end": "0 12 * * 1"}]}
    floor = {"workload": "test/shop/Deployment/api", "cluster": "test", "namespace": "shop"}
    floor |= {"kind": "Deployment", "name": "api", "hpa": "api", "policy": "production"}
    floor |= {"percentile": 0.1, "minimum": 2.0, "maximum": 9.0, "risks": [], "state": "ready"}
    floor |= {"history_h": 168.0, "needs_h": 24.0, "floor": 2, "profile": weekly}
    seed_run(database_url, "acme", row("api"))

    async def go() -> None:
        async with connect(database_url) as pool:
            await Store(pool).save_floors("acme", "test", NOW, [floor])

    asyncio.run(go())
    prices = {"prices": {"currency": "EUR", "cpu": 0.04, "memory": 0.005}}
    with hub(settings, fake_cluster, database_url, acme_extra=prices) as c:
        (shown,) = call(c, "sizings", contract="acme")["floors"]
    assert shown.splitlines()[1:] == [
        "  Weekly profile (UTC):",
        "    Mon 09:00 to Mon 12:00: 7 replicas",
        "    backtest: off by 12% last week (too few 4%, too many 8%: 18.25 EUR/month)",
    ]


def test_sizings_filter_by_cluster(
    settings: Settings, fake_cluster: Any, database_url: str
) -> None:
    seed_run(database_url, "bank", row("api"), row("web", cluster="edge"))
    with hub(settings, fake_cluster, database_url, clusters_of_bank=True) as c:
        both = call(c, "sizings", contract="bank")
        edge = call(c, "sizings", contract="bank", cluster="edge")
    assert both["matching"] == 2
    assert [w["workload"] for w in edge["workloads"]] == ["edge/shop/Deployment/web"]


def test_one_workloads_sizing_and_its_errors_are_masked_too(
    settings: Settings, fake_cluster: Any, database_url: str
) -> None:
    seed(database_url, ("acme", NOW, row("api")))
    with hub(settings, fake_cluster, database_url) as c:
        call(c, "sizings", contract="acme")  # hands out shop's placeholder
        found = call(
            c, "sizing", contract="acme", cluster="test", namespace=mask("shop"), name="api"
        )
        plain = call(c, "sizing", contract="acme", cluster="test", namespace="shop", name="api-7d")
        refused = call(c, "sizing", contract="acme", cluster="test", namespace="kube", name="api")
    (w,) = found["workloads"]
    assert w["workload"] == f"test/{mask('shop')}/Deployment/api"
    assert plain["workloads"][0]["name"] == "api"  # a pod's name finds its Workload
    assert refused == {
        "error": f"namespace 'kube' is not allowed in cluster 'test'; allowed: ['{mask('shop')}']"
    }


def test_no_term_reaches_the_client_anywhere_in_an_answer(
    settings: Settings, fake_cluster: Any, database_url: str
) -> None:
    """A cluster named after the client, in keys or values, answers or errors: the raw
    HTTP body carries both the structured answer and its text copy."""
    risk = {"kind": "oom_killed", "text": "OOM-killed in shop"}
    seed_run(database_url, "acme", row("api", cluster="acme-east", risks=[risk]))
    with hub(settings, fake_cluster, database_url, acme_clusters={"acme-east": ["shop"]}) as c:

        def raw(tool: str, **args: Any) -> str:
            r = rpc(c, "tools/call", {"name": tool, "arguments": args})
            assert r.status_code == 200
            return r.text

        bodies = [
            raw("list_contracts"),
            raw("sizings", contract="acme"),
            raw("sizing", contract="acme", cluster="acme-east", namespace="shop", name="api"),
            raw("sizings", contract="acme", cluster="nope"),
            raw("sizing", contract="acme", cluster="nope", namespace="shop", name="api"),
            raw("sizing", contract="acme", cluster="acme-east", namespace="kube", name="api"),
            raw("cost", contract="acme", compare_days=1),
            raw("savings", contract="acme", compare_days=1),
            raw("bill", contract="acme"),
            raw("investigations", contract="acme"),
            raw("checks", contract="acme"),
        ]
    assert '"isError":true' in bodies[3].replace(" ", "")
    assert "Deployment/api" in bodies[1] and "Deployment/api" in bodies[2]
    for body in bodies:
        assert "acme" not in body.lower() and "shop" not in body.lower(), body


def test_an_unexpected_errors_text_never_reaches_the_client(
    settings: Settings, fake_cluster: Any, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*_: Any) -> Any:
        raise RuntimeError("db at acme.shop.internal is down")

    monkeypatch.setattr(webapp, "workloads_of", broken)
    with hub(settings, fake_cluster, database_url) as c:
        error = call(c, "sizings", contract="acme")["error"]
    assert "acme" not in error and "shop" not in error


# --- isolation --------------------------------------------------------------------------


def test_a_placeholder_comes_back_only_for_the_contract_that_handed_it_out(
    settings: Settings, fake_cluster: Any, database_url: str
) -> None:
    """Same cluster, namespace and Workload names in both Contracts; acme masks."""
    seed(
        database_url, ("acme", NOW, row(policy="acme-only")), ("bank", NOW, row(policy="bank-only"))
    )
    with hub(settings, fake_cluster, database_url) as c:
        acme = call(c, "list_contracts")[0]["id"]
        mine = call(c, "sizings", contract=acme)
        theirs = call(c, "sizings", contract="bank")
        # acme's placeholders mean nothing in bank's calls, its id included
        shop_in_bank = call(c, "sizings", contract="bank", namespace=mask("shop"))
        sizing_in_bank = call(
            c, "sizing", contract="bank", cluster="test", namespace=mask("shop"), name="api"
        )
        not_bank = call(c, "sizings", contract=mask("bank"))
    assert mine["workloads"] and theirs["workloads"]
    assert {x["workload"] for x in theirs["workloads"]} == {"test/shop/Deployment/api"}
    assert shop_in_bank["matching"] == 0
    assert "is not allowed" in sizing_in_bank["error"]
    assert not_bank == {"error": "unknown Contract: list_contracts names them"}


def test_a_placeholder_never_handed_out_stays_as_written(
    settings: Settings, fake_cluster: Any, database_url: str
) -> None:
    """A placeholder the client computed or invented, before any listing: not resolved."""
    seed(database_url, ("acme", NOW, row()))
    with hub(settings, fake_cluster, database_url) as c:
        unresolved = call(c, "sizings", contract=mask("acme"))
    assert unresolved == {"error": "unknown Contract: list_contracts names them"}


# --- audit ------------------------------------------------------------------------------


def test_serve_writes_the_audit_log_to_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Live 11.1a found every squero INFO line dropped: uvicorn configures only its own."""
    import uvicorn

    hub = logging.getLogger("squero")
    before = (hub.handlers[:], hub.level)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    monkeypatch.chdir("/")  # no .env with moved settings here
    try:
        assert cli.main(["serve"]) == 0
        logging.getLogger("squero.audit").info('{"event": "tool_call", "channel": "mcp"}')
    finally:
        hub.handlers[:], hub.level = before
    assert (
        'INFO:     squero.audit {"event": "tool_call", "channel": "mcp"}' in capsys.readouterr().err
    )


def test_every_call_is_an_audit_line_with_its_channel_login_and_contract(
    settings: Settings, fake_cluster: Any, database_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    with (
        caplog.at_level(logging.INFO, logger="squero.audit"),
        hub(settings, fake_cluster, database_url) as c,
    ):
        acme = call(c, "list_contracts")[0]["id"]
        call(c, "sizings", contract=acme, namespace=mask("shop"))
    lines = [json.loads(r.message) for r in caplog.records if r.name == "squero.audit"]
    assert [(x["tool"], x["status"]) for x in lines] == [
        ("list_contracts", "ok"),
        ("sizings", "ok"),
    ]
    assert lines[1] == {
        "event": "tool_call",
        "channel": "mcp",
        "login": OPERATOR,
        "contract": mask("acme"),  # as the client wrote it: masked stays masked
        "tool": "sizings",
        "args": {"contract": mask("acme"), "namespace": mask("shop")},
        "status": "ok",
        "ms": lines[1]["ms"],
    }


# --- 11.1b: cost, savings, bill, Investigations, Checks, ask -----------------------------


def insert(database_url: str, *ts: Investigation, checks: dict[str, str] | None = None) -> None:
    async def go() -> None:
        async with connect(database_url) as pool:
            store = Store(pool)
            await store.migrate()
            for t in ts:
                await store.insert(t)
            for cid, name in (checks or {}).items():
                await store.sync_checks(cid, {name: ("0 6 * * * UTC", NOW + 3600)})

    asyncio.run(go())


def triage(cid: str, tid: str, status: str = "done", started: float = NOW) -> Investigation:
    return Investigation(
        id=tid,
        contract_id=cid,
        title=f"checkout crash looping in shop ({cid})",
        status=status,  # type: ignore[arg-type]
        started=started,
        report=TriageReport.model_validate(REPORT) if status == "done" else None,
    )


def test_cost_and_savings_group_the_contracts_own_workloads_masked(
    settings: Settings, fake_cluster: Any, database_url: str
) -> None:
    seed_run(database_url, "acme", row("api"), row("web"))
    seed_run(database_url, "bank", row("api", policy="bank-only"))
    with hub(settings, fake_cluster, database_url) as c:
        by_ns = call(c, "cost", contract="acme")
        bad = call(c, "cost", contract="acme", by="team")
        compared = call(c, "cost", contract="acme", compare_days=1)
        saved = call(c, "savings", contract="acme")
    (group,) = by_ns["groups"]
    assert group["group"] == f"test/{mask('shop')}" and group["workloads"] == 2
    assert by_ns["groupings"] == ["namespace"] and "realized" not in by_ns
    assert bad == {"error": "by is one of namespace"}
    assert compared["compare"]["since"] == NOW - 86400
    assert compared["compare"]["clusters"] == [{"cluster": "test", "at": None, "oldest": NOW}]
    assert saved["available"]["waste"] == {"cpu": 2.0, "memory": 0.0}
    assert set(saved) == {"currency", "available", "realized", "compare", "prices"}
    assert "shop" not in json.dumps([by_ns, saved])


def test_savings_list_the_hpa_minimums_lowered_toward_their_floors_masked(
    settings: Settings, fake_cluster: Any, database_url: str
) -> None:
    """Slice 14.1c: in the realized Savings, the Contract's own, its names masked."""
    seed_run(database_url, "acme", row("api"))
    where = {"cluster": "test", "namespace": "shop", "kind": "Deployment", "name": "api"}
    drop = {**where, "at": NOW, "before": 6.0, "after": 3.0, "floor": 3}

    async def go() -> None:
        async with connect(database_url) as pool:
            store = Store(pool)
            await store.save_floor_drops("acme", [drop], {}, set())
            await store.save_floor_drops("bank", [{**drop, "before": 9.0}], {}, set())

    asyncio.run(go())
    with hub(settings, fake_cluster, database_url) as c:
        saved = call(c, "savings", contract="acme")
    (lowered,) = saved["realized"]["floors"]
    assert lowered["workload"] == f"test/{mask('shop')}/Deployment/api"
    assert (lowered["before"], lowered["after"], lowered["floor"]) == (6.0, 3.0, 3.0)
    assert "shop" not in json.dumps(saved)


def test_a_bill_is_the_contracts_own(
    settings: Settings, fake_cluster: Any, database_url: str
) -> None:
    with hub(settings, fake_cluster, database_url) as c:
        bills = call(c, "bill", contract="acme", days=3)
        too_many = rpc(
            c, "tools/call", {"name": "bill", "arguments": {"contract": "acme", "days": 0}}
        )
    assert bills == {"tolerance": 0.05, "min_coverage": 0.9, "clusters": []}  # none billed
    assert too_many.json()["result"]["isError"]


def test_investigations_list_and_open_only_the_contracts_own(
    settings: Settings, fake_cluster: Any, database_url: str
) -> None:
    insert(
        database_url,
        triage("acme", "a-old", started=NOW - 60),
        triage("acme", "a-new"),
        triage("bank", "b-1"),
    )
    with hub(settings, fake_cluster, database_url) as c:
        # after startup: the Hub closes what a previous process left running
        insert(
            database_url,
            triage("acme", "a-running", status="running", started=NOW - 30),
            triage("acme", "a-waiting", status="awaiting_approval", started=NOW - 20),
            triage("bank", "b-running", status="running", started=NOW - 10),
        )
        listed = call(c, "investigations", contract="acme")
        opened = call(c, "investigations", contract="acme", open=True)
        newest_open = call(c, "investigations", contract="acme", open=True, limit=1)
        one = call(c, "investigations", contract="acme", limit=1)
        theirs = call(c, "investigations", contract="bank")
    assert [t["id"] for t in listed] == ["a-new", "a-waiting", "a-running", "a-old"]
    assert listed[0]["category"] == "resource_limits" and listed[0]["severity"] == "high"
    assert listed[0]["title"] == f"checkout crash looping in {mask('shop')} ({mask('acme')})"
    assert [t["id"] for t in opened] == ["a-waiting", "a-running"]
    assert [t["id"] for t in newest_open] == ["a-waiting"]
    assert [t["id"] for t in one] == ["a-new"]
    assert [t["id"] for t in theirs] == ["b-1", "b-running"]


def test_one_investigation_in_full_never_another_contracts(
    settings: Settings, fake_cluster: Any, database_url: str
) -> None:
    insert(database_url, triage("acme", "a-1"), triage("bank", "b-1"))
    with hub(settings, fake_cluster, database_url) as c:
        mine = call(c, "investigation", contract="acme", id="a-1")
        theirs = call(c, "investigation", contract="acme", id="b-1")
        missing = call(c, "investigation", contract="acme", id="nope")
    assert mine["report"]["probable_root_cause"] == REPORT["probable_root_cause"]
    assert mine["report"]["affected"] == [f"{mask('shop')}/deployment/checkout"]
    assert mine["decisions"] == [] and mine["pending"] == [] and mine["deadline"] is None
    assert theirs == missing == {"error": "no such Investigation in this Contract"}


def test_checks_are_the_contracts_own_schedules(
    settings: Settings, fake_cluster: Any, database_url: str
) -> None:
    insert(database_url, checks={"acme": "nightly", "bank": "bank-only"})
    with hub(settings, fake_cluster, database_url) as c:
        (check,) = call(c, "checks", contract="acme")
    assert check["name"] == "nightly" and check["next"] == "1972-09-27 01:00 UTC"
    assert check["last"] is None and check["schedule"] == "0 6 * * * UTC"


def test_ask_starts_a_question_with_the_placeholders_put_back(
    settings: Settings, fake_cluster: Any, database_url: str
) -> None:
    answer = {
        "answer": "checkout in shop is fine.",
        "evidence": [{"source": "kubernetes_list_pods", "finding": "all ready in shop"}],
    }
    from tests.test_slack import ACME as CHANNEL
    from tests.test_slack import FakeSlack

    settings.slack_bot_token = SecretStr("xoxb-test")
    slack = FakeSlack()
    steps = [call_step("Answer", "a1", **answer)]
    with hub(
        settings, fake_cluster, database_url, steps=steps, slack=slack.transport(), channel=CHANNEL
    ) as c:
        acme = call(c, "list_contracts")[0]["id"]
        call(c, "sizings", contract=acme)  # nothing stored: nothing handed out but the id
        asked = call(c, "ask", contract=acme, question=f"  is checkout in {acme} healthy?  ")
        empty = rpc(
            c, "tools/call", {"name": "ask", "arguments": {"contract": acme, "question": ""}}
        )
        q = wait_for(c, asked["id"], "done")
        shown = call(c, "investigation", contract=acme, id=asked["id"])
    assert asked["status"] == "running"
    assert (q["kind"], q["contract_id"]) == ("question", "acme")
    assert q["input"] == {
        "question": "is checkout in acme healthy?"
    }  # the real name, for the agent
    assert q["slack_ts"] is None
    # the Contract has an operator channel and the Hub a bot, yet nothing was posted
    assert [m for m, *_ in slack.calls if m.startswith("chat.")] == []
    assert shown["answer"]["answer"] == f"checkout in {mask('shop')} is fine."
    assert empty.json()["result"]["isError"]


def test_ask_is_the_one_tool_not_marked_read_only(
    settings: Settings, fake_cluster: Any, database_url: str
) -> None:
    with hub(settings, fake_cluster, database_url) as c:
        tools = {
            t["name"]: t["annotations"] for t in rpc(c, "tools/list", {}).json()["result"]["tools"]
        }
    writes = {n for n, a in tools.items() if not a["readOnlyHint"]}
    assert writes == {
        "ask", "decide", "turn_on_kill_switch", "approve_sizing", "revert_sizing",
        "propose_change",
    }  # fmt: skip


def test_a_grouping_named_after_the_client_comes_back_from_its_placeholder(
    settings: Settings, fake_cluster: Any, database_url: str
) -> None:
    labelled = row("api", labels={"acme.io/team": "pay"})
    seed_run(database_url, "acme", labelled)
    allocation = {"allocation": {"labels": ["acme.io/team"]}}
    with hub(settings, fake_cluster, database_url, acme_extra=allocation) as c:
        groupings = call(c, "cost", contract="acme")["groupings"]
        by_label = call(c, "cost", contract="acme", by=groupings[1])
    assert groupings == ["namespace", f"label:{mask('acme')}.io/team"]
    assert by_label["by"] == groupings[1] and by_label["groups"][0]["group"] == "pay"


@pytest.mark.anyio
async def test_a_question_that_could_not_start_says_so() -> None:
    async def refused(cid: str, text: str) -> str | None:
        return None

    async def never(*_: Any) -> Any:
        raise AssertionError("not called")

    contracts = {"bank": make_contract(contract_id="bank")}
    server = hubmcp.build(contracts, lambda: None, lambda: NOW, refused, never, never, never)  # type: ignore[arg-type,return-value]
    async with Client(server) as mcp:
        result = await mcp.call_tool(
            "ask", {"contract": "bank", "question": "ok?"}, raise_on_error=False
        )
    assert result.is_error and result.content[0].text == "the Question could not be started"  # type: ignore[union-attr]


def test_a_long_answer_is_masked_before_it_is_cut(
    settings: Settings, fake_cluster: Any, database_url: str
) -> None:
    """acme straddles the 500th character: cut first, "acm" would go out as written."""
    answer = Answer(answer="x" * 496 + " acme is fine", evidence=[{"source": "s", "finding": "f"}])
    q = Investigation(id="q-1", kind="question", contract_id="acme", title="ok?", started=NOW)
    q.answer = answer
    insert(database_url, q)
    with hub(settings, fake_cluster, database_url) as c:
        (listed,) = call(c, "investigations", contract="acme")
    assert listed["answer"].startswith("x" * 496 + " <") and len(listed["answer"]) == 500
    assert "acm" not in listed["answer"]


# --- 11.2a: decide, and the Kill switch ---------------------------------------------------

ACTING = {"integrations": [{"name": "kubernetes", "enable_actions": True}, PROM]}


def paused(c: TestClient) -> str:
    from tests.test_api import ALERT, HOOK, URL

    tid: str = c.post(URL, json=ALERT, headers=HOOK).json()["investigation"]
    wait_for(c, tid, "awaiting_approval")
    return tid


def decisions(database_url: str) -> list[tuple[bool, str, str]]:
    import psycopg

    with psycopg.connect(database_url) as conn:
        rows = conn.execute("select approve, decided_by, message from decisions order by id")
        return [(r[0], r[1], r[2]) for r in rows.fetchall()]


def test_decide_approves_the_pause_it_names_and_only_this_contracts(
    settings: Settings, fake_cluster: Any, database_url: str
) -> None:
    from tests.test_api import EVICT_THEN_REPORT

    with hub(settings, fake_cluster, database_url, steps=EVICT_THEN_REPORT, acme_extra=ACTING) as c:
        tid = paused(c)
        shown = call(c, "investigation", contract="acme", id=tid)
        pause = shown["pause"]
        stale = call(c, "decide", contract="acme", id=tid, pause=pause + "1", approve=True)
        theirs = call(c, "decide", contract="bank", id=tid, pause=pause, approve=True)
        approved = call(c, "decide", contract="acme", id=tid, pause=pause, approve=True)
        done = wait_for(c, tid, "done")
        again = call(c, "decide", contract="acme", id=tid, pause=pause, approve=True)
        after = call(c, "investigation", contract="acme", id=tid)
    assert isinstance(pause, str) and float(pause) > done["started"]
    assert [p["name"] for p in shown["pending"]] == ["kubernetes_evict_pod"]
    assert stale == again == {"error": "that pause is not open: read the Investigation again"}
    assert theirs == {"error": "no such Investigation in this Contract"}
    assert approved == {"id": tid, "decided": "approved"}
    assert fake_cluster[0].create_namespaced_pod_eviction.call_count == 1
    assert decisions(database_url) == [(True, f"mcp:{OPERATOR}", "")]
    # acme masks emails: the login it shows back is a placeholder
    login = masking.Masker(b"k-acme", ["acme", "shop"]).placeholder("email", OPERATOR)
    assert after["pause"] is None and after["decisions"][0]["decided_by"] == f"mcp:{login}"


def test_a_rejections_reason_reaches_the_agent_with_the_placeholders_put_back(
    settings: Settings, fake_cluster: Any, database_url: str
) -> None:
    from tests.test_api import EVICT_THEN_REPORT

    with hub(settings, fake_cluster, database_url, steps=EVICT_THEN_REPORT, acme_extra=ACTING) as c:
        tid = paused(c)
        pause = call(c, "investigation", contract="acme", id=tid)["pause"]  # hands out shop
        why = f"not in {mask('shop')} during the sale"
        rejected = call(
            c, "decide", contract="acme", id=tid, pause=pause, approve=False, reason=why
        )
        wait_for(c, tid, "done")
    assert rejected == {"id": tid, "decided": "rejected"}
    assert decisions(database_url) == [(False, f"mcp:{OPERATOR}", "not in shop during the sale")]
    assert fake_cluster[0].create_namespaced_pod_eviction.call_count == 0


def test_the_kill_switch_goes_on_from_mcp_and_refuses_approving_not_rejecting(
    settings: Settings, fake_cluster: Any, database_url: str
) -> None:
    from squero.api import KILLED
    from tests.test_api import EVICT_THEN_REPORT

    with hub(settings, fake_cluster, database_url, steps=EVICT_THEN_REPORT, acme_extra=ACTING) as c:
        tid = paused(c)
        pause = call(c, "investigation", contract="acme", id=tid)["pause"]
        on = call(c, "turn_on_kill_switch", contract="acme")
        again = call(c, "turn_on_kill_switch", contract="acme")
        listed = {x["name"]: x["kill_switch"] for x in call(c, "list_contracts")}
        refused = call(c, "decide", contract="acme", id=tid, pause=pause, approve=True)
        kept = call(c, "investigation", contract="acme", id=tid)["pause"]
        rejected = call(c, "decide", contract="acme", id=tid, pause=pause, approve=False)
        wait_for(c, tid, "done")
    assert on["kill_switch"] == "on" and again["kill_switch"] == "already on"
    assert listed == {mask("acme"): True, "bank": False}
    assert refused == {"error": KILLED} and kept == pause
    assert rejected["decided"] == "rejected"
    assert fake_cluster[0].create_namespaced_pod_eviction.call_count == 0
    import psycopg

    with psycopg.connect(database_url) as conn:
        row = conn.execute("select contract_id, set_by from kill_switches").fetchall()
    assert row == [("acme", f"mcp:{OPERATOR}")]


def test_nothing_on_the_hub_mcp_server_turns_a_kill_switch_off(
    settings: Settings, fake_cluster: Any, database_url: str
) -> None:
    with hub(settings, fake_cluster, database_url) as c:
        tools = {t["name"]: t for t in rpc(c, "tools/list", {}).json()["result"]["tools"]}
    switch = tools["turn_on_kill_switch"]
    assert set(switch["inputSchema"]["properties"]) == {"contract"}  # no on/off to pass
    assert tools["decide"]["annotations"]["destructiveHint"] is True
    assert not [n for n in tools if "off" in n or "clear" in n]


@pytest.mark.anyio
async def test_a_change_without_an_operators_login_is_refused() -> None:
    """In-process, with no HTTP request: guard() never ran, so nobody is named."""
    called: list[Any] = []

    async def record(*args: Any) -> Any:
        called.append(args)
        return None

    contracts = {"bank": make_contract(contract_id="bank")}
    server = hubmcp.build(contracts, lambda: None, lambda: NOW, record, record, record, record)  # type: ignore[arg-type,return-value]
    sizing = {"cluster": "test", "namespace": "shop", "name": "api", "container": "app"}
    async with Client(server) as mcp:
        results = [
            await mcp.call_tool("turn_on_kill_switch", {"contract": "bank"}, raise_on_error=False),
            await mcp.call_tool(
                "approve_sizing",
                {"contract": "bank", "cpu": "115m", "memory": "512Mi", **sizing},
                raise_on_error=False,
            ),
            await mcp.call_tool(
                "propose_change",
                {
                    "contract": "bank",
                    "title": "t",
                    "edits": [{"edit": "set_default", "policy": "batch"}],
                },
                raise_on_error=False,
            ),
        ]
    for result in results:
        assert result.is_error and "only the Operator" in result.content[0].text  # type: ignore[union-attr]
    assert called == []


# --- Approved Sizings (slice 11.2b) ------------------------------------------------------

MI = 2**20


def sized(name: str = "api", cpu: str = "lower", memory: str = "keep", **extra: Any) -> Any:
    """A ready row: cpu 1 core lowered to 115m, memory 512Mi kept (unless told)."""
    return row(
        name,
        cpu={
            "action": cpu,
            "current": 1.0,
            "target": 1.0 if cpu == "keep" else 0.115,
            "basis": 0.1,
            "limit": None,
        },
        memory={
            "action": memory,
            "current": 512 * MI,
            "target": 512 * MI if memory == "keep" else 96 * MI,
            "basis": 80 * MI,
            "limit": None,
        },
        **extra,
    )


def approved_rows(database_url: str) -> list[tuple[Any, ...]]:
    import psycopg

    with psycopg.connect(database_url) as conn:
        found = conn.execute(
            "select contract_id, name, cpu, memory, approved_by from approved_sizings"
        )
        return found.fetchall()


def test_a_sizing_is_approved_with_the_values_seen_and_only_in_its_contract(
    settings: Settings, fake_cluster: Any, database_url: str
) -> None:
    seed(database_url, ("acme", NOW, sized()), ("bank", NOW, sized()))
    shop = mask("shop")
    at = {"cluster": "test", "name": "api", "container": "app"}
    with hub(settings, fake_cluster, database_url) as c:
        call(c, "sizings", contract="acme")  # hands out shop's placeholder
        ok = call(
            c, "approve_sizing", contract="acme", namespace=shop, cpu="115m", memory="512Mi", **at
        )
        again = call(
            c, "approve_sizing", contract="acme", namespace=shop, cpu="115m", memory="512Mi", **at
        )
        one = call(c, "sizing", contract="acme", cluster="test", namespace=shop, name="api")
        ranked = call(c, "sizings", contract="acme")
        theirs = call(c, "sizing", contract="bank", cluster="test", namespace="shop", name="api")
        theirs_ranked = call(c, "sizings", contract="bank")
    assert ok == {
        "workload": f"test/{shop}/Deployment/api/app",
        "approved": "approved",
        "cpu": "115m",
        "memory": "512Mi",
    }
    assert again["approved"] == "already approved"
    # memory is kept, so only cpu is approved for the Actuator to change
    assert approved_rows(database_url) == [("acme", "api", 0.115, None, f"mcp:{OPERATOR}")]
    login = masking.Masker(b"k-acme", ["acme", "shop"]).placeholder("email", OPERATOR)
    shown = {"by": f"mcp:{login}", "at": "1972-09-27 00:00 UTC"}
    assert one["workloads"][0]["containers"][0]["approved"] == shown
    assert ranked["workloads"][0]["containers"][0]["approved"] == shown
    # bank has the same cluster, namespace and Workload: nothing of acme's shows there
    assert "approved" not in theirs["workloads"][0]["containers"][0]
    assert "approved" not in theirs_ranked["workloads"][0]["containers"][0]


def test_approving_is_refused_unless_it_is_the_latest_sizing_and_changes_something(
    settings: Settings, fake_cluster: Any, database_url: str
) -> None:
    seed_run(
        database_url,
        "bank",
        sized(),
        sized("same", cpu="keep"),
        row("new", state="learning"),
        sized("twin"),
        sized("twin", kind="StatefulSet", workload="test/shop/StatefulSet/twin/app"),
    )

    def approve(name: str, cpu: str | None = "115m", **more: Any) -> Any:
        where = {"cluster": "test", "namespace": "shop", "container": "app", **more}
        return call(
            c, "approve_sizing", contract="bank", name=name, cpu=cpu, memory="512Mi", **where
        )

    with hub(settings, fake_cluster, database_url) as c:
        old = approve("api", cpu="120m")
        same = approve("same", cpu="1000m")
        new = approve("new", cpu=None)
        pod = approve("api-7d9f")
        twins = approve("twin")
        twin = approve("twin", kind="StatefulSet")
        elsewhere = approve("api", namespace="kube")
    assert old == {"error": "its latest Sizing is cpu 115m, memory 512Mi: read it again"}
    assert same == {"error": "its Sizing keeps the requests as they are: nothing to approve"}
    assert new == {"error": "it is learning: it has no Sizing to approve"}
    assert pod == {"error": "no stored Sizing of that container: `sizing` lists them"}
    assert twins == {"error": "two Workloads share that name: pass kind"}
    assert twin["approved"] == "approved"
    assert elsewhere["error"].startswith("namespace 'kube' is not allowed")
    assert [r[1] for r in approved_rows(database_url)] == ["twin"]


def test_approving_a_sizing_is_refused_while_the_kill_switch_is_on(
    settings: Settings, fake_cluster: Any, database_url: str
) -> None:
    seed(database_url, ("bank", NOW, sized()))
    where = {"cluster": "test", "namespace": "shop", "name": "api", "container": "app"}
    with hub(settings, fake_cluster, database_url) as c:
        call(c, "turn_on_kill_switch", contract="bank")
        refused = call(c, "approve_sizing", contract="bank", cpu="115m", memory="512Mi", **where)
    assert refused == {"error": hubmcp.KILLED}
    assert approved_rows(database_url) == []


# --- Proposed changes (slice 11.3) -------------------------------------------------------


def clone(tmp_path: Any, settings: Settings) -> None:
    """The contracts repo cloned at SQUERO_CONTRACTS_DIR, acme's file at MAIN."""
    from tests.test_proposals import MAIN, TEXT

    (tmp_path / ".git" / "refs" / "heads").mkdir(parents=True)
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (tmp_path / ".git" / "refs" / "heads" / "main").write_text(MAIN + "\n")
    (tmp_path / "acme").mkdir()
    (tmp_path / "acme" / "contract.yaml").write_text(TEXT)
    settings.contracts_dir = tmp_path
    settings.contracts_repo = "me/contracts"
    settings.contracts_token = SecretStr("github_pat_" + "s" * 40)


def test_a_change_is_proposed_as_a_pull_request_with_the_placeholders_put_back(
    settings: Settings, fake_cluster: Any, database_url: str, tmp_path: Any
) -> None:
    """The Kill switch doesn't stop it: a pull request changes nothing until merged."""
    import base64

    from tests.test_proposals import TEXT, GitHub, sizing

    clone(tmp_path, settings)
    gh = GitHub({"acme/contract.yaml": TEXT})
    seed_run(database_url, "acme", row("db"))
    shop = mask("shop")
    rule = {"edit": "set_rule", "namespace": shop, "name": "db", "policy": "lean"}
    with hub(
        settings, fake_cluster, database_url, github=httpx.Client(transport=httpx.MockTransport(gh))
    ) as c:
        call(c, "sizings", contract="acme")  # hands out shop's placeholder
        call(c, "turn_on_kill_switch", contract="acme")
        opened = call(
            c,
            "propose_change",
            contract="acme",
            edits=[rule],
            title="db leaner",
            why=f"{shop} db idles",
        )
        theirs = call(c, "propose_change", contract="bank", edits=[rule], title="t")
        plain = {**rule, "namespace": "shop"}
        unloaded = call(c, "propose_change", contract="bank", edits=[plain], title="t")
    assert opened == {"pull_request": "https://github.com/me/contracts/pull/7"}
    (ref, put, pull) = gh.writes()
    assert ref[2]["ref"] == "refs/heads/squero/acme-19720927-000000"
    rules = sizing(base64.b64decode(put[2]["content"]).decode())["rules"]
    assert rules[0] == {"namespace": "shop", "name": "db", "policy": "lean"}
    assert pull[2]["body"].startswith("shop db idles\n")
    assert pull[2]["body"].endswith(f"Hub MCP server by {OPERATOR}.")
    # acme's placeholder means nothing to bank; bank has no file in the clone; and
    # neither call reached GitHub
    assert "was not handed out by this Hub" in theirs["error"]
    assert unloaded == {"error": "the Hub did not load bank/contract.yaml from its clone"}
    assert len(gh.requests) == 5


def test_a_placeholder_never_handed_out_proposes_nothing(
    settings: Settings, fake_cluster: Any, database_url: str, tmp_path: Any
) -> None:
    from tests.test_proposals import TEXT, GitHub

    clone(tmp_path, settings)
    gh = GitHub({"acme/contract.yaml": TEXT})
    rule = {"edit": "set_rule", "namespace": mask("shop"), "policy": "off"}
    with hub(
        settings, fake_cluster, database_url, github=httpx.Client(transport=httpx.MockTransport(gh))
    ) as c:
        refused = call(c, "propose_change", contract="acme", edits=[rule], title="t")
    assert "was not handed out by this Hub" in refused["error"]
    assert gh.requests == []


def test_without_the_repo_and_its_token_nothing_is_proposed(
    settings: Settings, fake_cluster: Any, database_url: str
) -> None:
    edit = {"edit": "set_default", "policy": "batch"}
    settings.contracts_repo = "me/contracts"  # and no token
    with hub(settings, fake_cluster, database_url) as c:
        refused = call(c, "propose_change", contract="bank", edits=[edit], title="t")
    assert refused == {
        "error": "set SQUERO_CONTRACTS_REPO and SQUERO_CONTRACTS_TOKEN to propose changes"
    }


# --- the Agenda (slice 11.4) -------------------------------------------------------------


def test_now_ranks_one_contracts_agenda_and_counts_every_contracts(
    settings: Settings, fake_cluster: Any, database_url: str
) -> None:
    seed_run(database_url, "acme", row("api", waste={"cpu": 2.0, "memory": 0.0}))
    seed_run(database_url, "bank", row("web"), row("db"))
    with hub(settings, fake_cluster, database_url) as c:
        call(c, "turn_on_kill_switch", contract="bank")
        one = call(c, "now", contract="acme")
        every = call(c, "now")
        tools = {t["name"]: t for t in rpc(c, "tools/list", {}).json()["result"]["tools"]}
    shop = mask("shop")
    assert one == {
        "contract": mask("acme"),
        "items": [
            {
                "kind": "waste",
                "title": "Waste: 2000m and 0Mi above its Sizings",
                "at": None,
                "id": None,
                "cluster": "test",
                "workload": f"test/{shop}/Deployment/api",
            }
        ],
    }
    assert every == [
        {"contract": mask("acme"), "name": mask("acme"), "waste": 1},
        {"contract": "bank", "name": "bank", "kill_switch": 1, "waste": 2},
    ]
    assert tools["now"]["annotations"]["readOnlyHint"] is True
    assert "contract" not in tools["now"]["inputSchema"].get("required", [])


# --- Reverts (slice 12.6a) ---------------------------------------------------------------


def test_a_revert_is_asked_only_of_what_its_own_contracts_actuator_reported(
    settings: Settings, fake_cluster: Any, database_url: str
) -> None:
    import psycopg

    from tests.test_actuator import A_TOKEN, B_TOKEN, actuated, rolled_back

    settings.database_url = SecretStr(database_url)
    settings.console_operators = {OPERATOR}
    settings.mcp_token = SecretStr(TOKEN)
    masked = contracts.build(
        {**actuated("dino", "d" * 40).model_dump(mode="json", exclude_none=True),
         "data_policy": {"mask": {"terms": ["shop"]}}},
        secrets={"act": SecretStr("d" * 40), contracts.MASK_KEY: SecretStr("k-dino")},
    )  # fmt: skip
    more = {"bank": (actuated("bank", A_TOKEN), fake_cluster),
            "corp": (actuated("corp", B_TOKEN), fake_cluster),
            "dino": (masked, fake_cluster)}  # fmt: skip
    at = {"cluster": "test", "namespace": "shop", "name": "api", "container": "app"}
    with client(settings, fake_cluster, acme=ACME, more=more) as c:
        with psycopg.connect(database_url) as conn:
            conn.execute(*rolled_back("bank", 1.0))  # an Actuation of bank's api
            conn.execute(*rolled_back("dino", 1.0))
        ok = call(c, "revert_sizing", contract="bank", **at)
        shown = call(c, "revert_sizing", contract="dino", **at)  # hands out shop's placeholder
        shop = shown["workload"].split("/")[1]
        again = call(c, "revert_sizing", contract="dino", **{**at, "namespace": shop})
        web = call(c, "revert_sizing", contract="bank", **{**at, "name": "web"})
        edge = call(c, "revert_sizing", contract="bank", **{**at, "cluster": "edge"})
        kube = call(c, "revert_sizing", contract="bank", **{**at, "namespace": "kube"})
        theirs = call(c, "revert_sizing", contract="corp", **at)  # bank's Actuation, not corp's
        call(c, "turn_on_kill_switch", contract="bank")
        killed = call(c, "revert_sizing", contract="bank", **at)
        with psycopg.connect(database_url) as conn:
            rows = conn.execute(
                "select contract_id, cluster, namespace, kind, name, container, asked_by "
                "from reverts"
            ).fetchall()
    assert ok == {"workload": "test/shop/Deployment/api/app", "revert": "asked"}
    nothing = {"error": "the Actuator has reported nothing of that container: nothing to revert"}
    assert web == nothing and theirs == nothing
    assert edge == {"error": "cluster 'edge' runs no Actuator: one of ['test']"}
    assert kube == {"error": "namespace 'kube' is not one of ['shop']"}
    assert killed["error"].startswith("The Contract's Kill switch is on: a Revert is a change")
    assert shop != "shop" and again == {"workload": f"test/{shop}/Deployment/api/app",
                                        "revert": "asked"}  # fmt: skip
    assert [r[0] for r in rows] == ["bank", "dino", "dino"]
    assert rows[0] == ("bank", "test", "shop", "Deployment", "api", "app", f"mcp:{OPERATOR}")
    assert rows[2][2] == "shop"  # the placeholder put back
