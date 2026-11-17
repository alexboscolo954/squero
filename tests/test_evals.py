import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from squero import agent, evals
from squero.config import Settings
from squero.models import AlertGroup, TriageReport
from tests.conftest import REPORT, call, scripted
from tests.test_agent import ALERT

SCENARIOS = Path(__file__).parent.parent / "evals" / "scenarios"


def replay(settings: Settings, recordings: dict[str, Any], actions: bool = False) -> agent.Toolbox:
    contract = evals.scenario_contract("shop", actions, Path("no-runbooks"))
    return evals.replay_server(contract, settings, recordings)


def test_replay_matches_args_in_order() -> None:
    r = evals.Replay(
        {"t": [{"match": {"previous": True}, "result": "crash"}, {"match": {}, "result": "fine"}]}
    )
    assert r.lookup("t", {"previous": True}) == "crash"
    assert r.lookup("t", {"previous": False}) == "fine"
    assert r.lookup("missing", {}) is None


@pytest.mark.anyio
async def test_replay_server_feeds_the_agent(settings: Settings) -> None:
    recordings = {"kubernetes_pod_logs": "java.lang.OutOfMemoryError: Java heap space"}
    model = scripted(
        call(
            "kubernetes_pod_logs", "c1", cluster="kind", namespace="shop", name="p", previous=True
        ),
        call("TriageReport", "c2", **REPORT),
    )
    group = AlertGroup.model_validate({"status": "firing", "alerts": []})
    async with agent.open_agent(settings, replay(settings, recordings), model) as a:
        outcome = await agent.start(a, group, "e1")
    scenario: dict[str, Any] = {"expect": {"category": ["resource_limits"], "keywords": ["memory"]}}
    assert evals.score(scenario, outcome) == []
    scenario["expect"]["keywords"] = ["DATABASE_URL"]
    assert evals.score(scenario, outcome) == ["root cause misses ['DATABASE_URL']"]


@pytest.mark.anyio
async def test_replay_validates_arguments_like_the_real_call(settings: Settings) -> None:
    from fastmcp import Client

    tb = replay(settings, {"kubernetes_list_pods": []})
    async with Client(tb.mcp) as c:
        ok = await c.call_tool("kubernetes_list_pods", {"cluster": "kind", "namespace": "shop"})
        bad = await c.call_tool(
            "kubernetes_list_pods", {"cluster": "prod", "namespace": "shop"}, raise_on_error=False
        )
        missing = await c.call_tool(
            "kubernetes_list_pods", {"namespace": "shop"}, raise_on_error=False
        )
    assert not ok.is_error
    assert bad.is_error and "'prod' is not one of ['kind']" in bad.content[0].text
    assert missing.is_error and "'cluster' is a required property" in missing.content[0].text
    async with Client(tb.mcp) as c:  # a guessed namespace fails, as the real tool does
        guessed = await c.call_tool(
            "kubernetes_list_pods",
            {"cluster": "kind", "namespace": "default"},
            raise_on_error=False,
        )
    assert guessed.is_error
    assert guessed.content[0].text == (
        "namespace 'default' is not allowed in cluster 'kind'; allowed: ['shop']"
    )


@pytest.mark.anyio
async def test_allowed_proposal_is_rejected_and_scored(settings: Settings) -> None:
    recordings = {"kubernetes_evict_pod": "evicted"}
    group = AlertGroup.model_validate({"status": "firing", "alerts": []})
    evict = call("kubernetes_evict_pod", "c1", cluster="kind", namespace="shop", name="p")
    scenario: dict[str, Any] = {"expect": {"category": ["resource_limits"]}}

    model = scripted(evict, call("TriageReport", "c2", **REPORT))
    async with agent.open_agent(settings, replay(settings, recordings, actions=True), model) as a:
        outcome, proposed = await evals.triage(a, group, "e2", scenario["expect"])
    assert proposed == [] and evals.score(scenario, outcome) == [
        "attempted write action: ['kubernetes_evict_pod']"
    ]

    scenario["expect"]["allowed_actions"] = [
        {"name": "kubernetes_evict_pod", "args": {"namespace": "shop"}}
    ]
    model = scripted(evict, call("TriageReport", "c2", **REPORT))
    async with agent.open_agent(settings, replay(settings, recordings, actions=True), model) as a:
        outcome, proposed = await evals.triage(a, group, "e3", scenario["expect"])
    assert [p["name"] for p in proposed] == ["kubernetes_evict_pod"]
    assert outcome.executed_actions == []  # rejected, never ran
    assert evals.score(scenario, outcome) == []

    scenario["expect"]["allowed_actions"][0]["args"] = {"namespace": "other"}
    assert not evals.allowed(scenario["expect"], proposed[0])


@pytest.mark.parametrize("path", sorted(SCENARIOS.glob("*.yaml")), ids=lambda p: p.stem)
def test_scenarios_are_well_formed(path: Path, settings: Settings) -> None:
    s = yaml.safe_load(path.read_text())
    assert s["tools"] and ("namespace" in s or "clusters" in s)  # clusters {}: none
    # Every recording answers a tool the Scenario's Contract offers (a vendor's from
    # its snapshot): a renamed or unlisted tool would never be called.
    contract = evals.scenario_contract(
        s.get("namespace", ""),
        s.get("enable_actions", False),
        [Path("no-runbooks")],
        s.get("clusters"),
        s.get("integrations"),
    )
    assert set(s["tools"]) <= set(evals.replay_server(contract, settings, {}).access)
    if "question" in s:  # an Answer: keywords and the Evidence it must cite
        assert "alert" not in s and s["question"].strip() and s["expect"]["keywords"]
        for source in s["expect"]["evidence"]:
            assert ({source} if isinstance(source, str) else set(source)) <= set(s["tools"])
    elif "check" in s:  # a verdict; a failing one is a Report too
        assert "alert" not in s and s["check"]["name"] and s["check"]["prompt"].strip()
        assert isinstance(s["expect"]["passed"], bool)
        assert s["expect"]["passed"] or s["expect"]["category"]
        for source in s["expect"].get("evidence", []):
            assert ({source} if isinstance(source, str) else set(source)) <= set(s["tools"])
    else:
        AlertGroup.model_validate(s["alert"])
        assert s["expect"]["category"]


@pytest.mark.anyio
async def test_an_answer_is_scored_on_keywords_and_real_evidence(settings: Settings) -> None:
    recordings = {"kubernetes_list_pods": [{"name": "checkout-1", "restarts": 4}]}
    answer = {
        "answer": "checkout-1 restarted 4 times.",
        "evidence": [{"source": "kubernetes_list_pods", "finding": "checkout-1 restarts 4"}],
    }
    model = scripted(
        call("kubernetes_list_pods", "c1", cluster="kind", namespace="shop"),
        call("Answer", "c2", **answer),
    )
    async with agent.open_agent(
        settings, replay(settings, recordings), model, kind="question"
    ) as a:
        outcome = await agent.ask(a, "Is checkout healthy?", "q1")
    expect: dict[str, Any] = {"keywords": ["checkout"], "evidence": ["kubernetes_list_pods"]}
    assert evals.score_answer({"expect": expect}, outcome) == []
    expect["evidence"] = [["kubernetes_pod_logs", "kubernetes_list_pods"]]  # any of them
    assert evals.score_answer({"expect": expect}, outcome) == []

    expect.update(keywords=["reports"], evidence=["kubernetes_pod_logs"], max_tool_calls=0)
    assert evals.score_answer({"expect": expect}, outcome) == [
        "answer misses ['reports']",
        "evidence doesn't cite ['kubernetes_pod_logs']",
        "2 tool calls > 0",  # the scripted Answer counts, like TriageReport
    ]
    assert outcome.answer is not None
    outcome.answer.evidence.append(outcome.answer.evidence[0].model_copy(update={"source": "x"}))
    assert "evidence cites tools that never ran: ['x']" in evals.score_answer(
        {"expect": {}}, outcome
    )
    assert evals.score_answer({"expect": {}}, agent.Outcome("t")) == ["no answer"]


@pytest.mark.anyio
async def test_the_outcome_logs_each_calls_arguments_and_errors(settings: Settings) -> None:
    """What a failed Scenario needs to be diagnosed: which arguments each call sent,
    redacted, and which got an error (here: nothing recorded for them)."""
    recordings = {"kubernetes_list_pods": [{"match": {"namespace": "shop"}, "result": []}]}
    model = scripted(
        call(
            "kubernetes_list_pods",
            "c1",
            cluster="kind",
            namespace="shop",
            label_selector="token=hunter2hunter2",
        ),
        call("kubernetes_get_events", "c2", cluster="kind", namespace="shop"),
        call("TriageReport", "c3", **REPORT),
    )
    async with agent.open_agent(settings, replay(settings, recordings), model) as a:
        outcome = await agent.start(a, ALERT, "e1")
    first, second = outcome.call_log[:2]
    assert first["name"] == "kubernetes_list_pods" and first["error"] is False
    assert first["args"]["namespace"] == "shop" and "hunter2" not in json.dumps(first["args"])
    assert second == {
        "name": "kubernetes_get_events",
        "args": {"cluster": "kind", "namespace": "shop"},
        "error": True,
    }


@pytest.mark.anyio
async def test_a_run_without_a_report_keeps_its_calls(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Audit A12: a Scenario whose agent stops without a report is scored as such, with
    the calls it made (one Sonnet run showed none, so it couldn't be explained)."""
    from langchain_core.messages import AIMessage

    scenario = {
        "description": "x",
        "namespace": "shop",
        "alert": ALERT.model_dump(mode="json"),
        "expect": {"category": ["app_error"]},
        "tools": {"kubernetes_list_pods": [{"match": {}, "result": []}]},
    }
    (tmp_path / "stops.yaml").write_text(yaml.safe_dump(scenario))
    model = scripted(
        call("kubernetes_list_pods", "c1", cluster="kind", namespace="shop"),
        AIMessage(content="no report from me"),
    )
    real = agent.open_agent
    monkeypatch.setattr(agent, "open_agent", lambda s, tb, **kw: real(s, tb, model, **kw))
    (result,) = await evals.run(tmp_path, settings, [])
    assert result["failures"] == ["agent stopped without a report (call limit reached?)"]
    assert result["tool_calls"] == 1 and result["calls"] == ["kubernetes_list_pods"]
    assert result["call_log"][0]["args"] == {"cluster": "kind", "namespace": "shop"}


@pytest.mark.anyio
async def test_past_incidents_replay_from_the_recording(settings: Settings) -> None:
    """Never the Hub's database: an id the recording doesn't hold errors, as the real
    tool does for an unknown one."""
    from fastmcp import Client
    from fastmcp.exceptions import ToolError

    contract = evals.scenario_contract("shop", False, Path("x"), {}, ["incidents"])
    recordings = {
        "incidents_search": {"incidents": [{"id": "p1", "summary": "reports oom"}]},
        "incidents_read": [{"match": {"id": "p1"}, "result": {"id": "p1", "report": "## r"}}],
    }
    async with Client(evals.replay_server(contract, settings, recordings).mcp) as c:
        found = await c.call_tool("incidents_search", {"keywords": "anything"})
        assert found.structured_content == recordings["incidents_search"]
        assert (await c.call_tool("incidents_read", {"id": "p1"})).structured_content == {
            "id": "p1",
            "report": "## r",
        }
        with pytest.raises(ToolError, match="no recorded data"):
            await c.call_tool("incidents_read", {"id": "invented"})


def test_a_scenarios_teachings_reach_its_prompt_as_in_production() -> None:
    alert = {
        "status": "firing",
        "alerts": [{"status": "firing", "labels": {"alertname": "KubePodCrashLooping"}}],
    }
    scenario = {
        "alert": alert,
        "teachings": [
            {"text": "Never roll back checkout.", "alerts": ["KubePodCrashLooping"]},
            {"text": "For disks only.", "alerts": ["DiskFull"]},
            {"text": "In general."},
        ],
    }
    taught = evals.scenario_taught(scenario)
    assert "Never roll back checkout." in taught and "In general." in taught
    assert "For disks only." not in taught
    question = {"question": "?", "teachings": scenario["teachings"]}
    assert "For disks only." in evals.scenario_taught(question)  # a Question: every one
    assert evals.scenario_taught({"alert": alert}) == ""


def test_recommendations_are_scored_when_the_scenario_asks() -> None:
    report = TriageReport.model_validate(
        {**REPORT, "recommended_actions": ["Restore DATABASE_URL from secret checkout-db."]}
    )
    outcome = agent.Outcome("t", report=report)
    expect = {"category": ["resource_limits"], "recommendations": ["checkout-db"]}
    assert evals.score({"expect": expect}, outcome) == []
    expect["recommendations"] = ["rollout undo"]
    assert evals.score({"expect": expect}, outcome) == ["recommendations miss ['rollout undo']"]


def test_a_triage_must_cite_the_evidence_and_memory_its_scenario_asks_for() -> None:
    """9.6a: an OOM Report cites the Sizing and recommends memory above the peak."""
    evidence = [{"source": "sizing_for_workload", "finding": "OOM-killed at its 64Mi limit"}]
    steps = ["Raise the memory request and limit to 77Mi."]
    report = TriageReport.model_validate(
        {**REPORT, "evidence": evidence, "recommended_actions": steps}
    )
    expect = {
        "category": ["resource_limits"],
        "evidence": ["sizing_for_workload"],
        "memory_above": "64Mi",
    }
    ran = agent.Outcome("t", report=report, tool_calls=["sizing_for_workload"])
    assert evals.score({"expect": expect}, ran) == []
    never = agent.Outcome("t", report=report, tool_calls=[])
    assert evals.score({"expect": expect}, never) == [
        "evidence cites tools that never ran: ['sizing_for_workload']"
    ]
    other = TriageReport.model_validate({**REPORT, "recommended_actions": ["Keep 64Mi."]})
    assert evals.score({"expect": expect}, agent.Outcome("t", report=other)) == [
        "recommendations name no memory above 64Mi",
        "evidence doesn't cite ['sizing_for_workload']",
        f"evidence cites tools that never ran: {sorted({e['source'] for e in REPORT['evidence']})}",
    ]


EMPTY = {"incidents": [], "note": "No past Report in the last 90 days matches."}
CITED = [
    {"source": "kubernetes_pod_logs", "finding": "OutOfMemoryError"},
    {"source": "incidents_search", "finding": "No past Report matches."},
]


async def cited_after(settings: Settings, *searches: Any) -> list[str]:
    """The Evidence sources a Report keeps after searches with these results."""
    contract = evals.scenario_contract("shop", False, Path("x"), {}, ["incidents"])
    recordings = {
        "incidents_search": [
            {"match": {"keywords": f"k{i}"}, "result": r} for i, r in enumerate(searches)
        ]
    }
    steps = [call("incidents_search", f"s{i}", keywords=f"k{i}") for i in range(len(searches))]
    model = scripted(*steps, call("TriageReport", "r", **{**REPORT, "evidence": CITED}))
    toolbox = evals.replay_server(contract, settings, recordings)
    group = AlertGroup.model_validate({"status": "firing", "alerts": []})
    async with agent.open_agent(settings, toolbox, model) as a:
        outcome = await agent.start(a, group, "e1")
    assert outcome.report is not None
    return [e.source for e in outcome.report.evidence]


@pytest.mark.anyio
async def test_evidence_citing_an_empty_search_is_dropped(settings: Settings) -> None:
    assert await cited_after(settings, EMPTY) == ["kubernetes_pod_logs"]
    assert await cited_after(settings, EMPTY, EMPTY) == ["kubernetes_pod_logs"]


@pytest.mark.anyio
async def test_evidence_citing_a_search_that_found_one_is_kept(settings: Settings) -> None:
    found = {"incidents": [{"id": "p1", "summary": "reports oom"}]}
    both = ["kubernetes_pod_logs", "incidents_search"]
    assert await cited_after(settings, found) == both
    assert await cited_after(settings, EMPTY, found) == both  # one found something
    assert await cited_after(settings) == both  # never searched: not the Hub's to judge


def test_the_only_evidence_is_kept() -> None:
    from langchain.messages import ToolMessage

    report = TriageReport.model_validate({**REPORT, "evidence": CITED[1:]})
    empty = ToolMessage(content=json.dumps(EMPTY), name="incidents_search", tool_call_id="s")
    agent.drop_empty_searches(report, [empty])
    assert [e.source for e in report.evidence] == ["incidents_search"]


def test_a_result_the_hub_cant_read_counts_as_found() -> None:
    """A stored oversized result, or an error text: not known to be empty, so kept."""
    from langchain.messages import ToolMessage

    report = TriageReport.model_validate({**REPORT, "evidence": CITED})
    odd = ToolMessage(
        content="[stored result r1: 9000 chars]", name="incidents_search", tool_call_id="s"
    )
    agent.drop_empty_searches(report, [odd])
    assert [e.source for e in report.evidence] == ["kubernetes_pod_logs", "incidents_search"]


@pytest.mark.anyio
async def test_a_check_is_run_and_scored_on_its_verdict(settings: Settings) -> None:
    recordings = {"kubernetes_list_pods": [{"name": "checkout-1", "restarts": 0}]}
    listed = call("kubernetes_list_pods", "c1", cluster="kind", namespace="shop")
    ev = [{"source": "kubernetes_list_pods", "finding": "checkout-1 runs, 0 restarts"}]
    ok = call("CheckResult", "c2", passed=True, summary="checkout runs.", evidence=ev)
    spec = {"name": "checkout-up", "prompt": "Is checkout running?"}
    async with agent.open_agent(
        settings, replay(settings, recordings), scripted(listed, ok), kind="check"
    ) as a:
        outcome, proposed = await evals.check(a, spec, "k1", {})
    assert proposed == [] and outcome.check and outcome.check.passed
    expect: dict[str, Any] = {"passed": True, "keywords": ["checkout"]}
    expect["evidence"] = [["kubernetes_list_pods", "kubernetes_describe_pod"]]
    assert evals.score_check({"expect": expect}, outcome) == []
    expect.update(keywords=["reports"], evidence=["kubernetes_pod_logs"], max_tool_calls=0)
    assert evals.score_check({"expect": expect}, outcome) == [
        "summary misses ['reports']",
        "evidence doesn't cite ['kubernetes_pod_logs']",
        "2 tool calls > 0",
    ]
    assert evals.score_check({"expect": {"passed": False}}, outcome) == [
        "passed is True, expected False"
    ]
    assert evals.score_check({"expect": {"passed": True}}, agent.Outcome("t")) == ["no verdict"]


@pytest.mark.anyio
async def test_a_failing_check_is_scored_as_a_report(settings: Settings) -> None:
    bad = call(
        "CheckResult",
        "c1",
        passed=False,
        summary="x",
        evidence=[REPORT["evidence"][0]],
        report=REPORT,
    )
    spec = {"name": "checkout-up", "prompt": "Is checkout running?"}
    async with agent.open_agent(settings, replay(settings, {}), scripted(bad), kind="check") as a:
        outcome, _ = await evals.check(a, spec, "k2", {})
    expect: dict[str, Any] = {
        "passed": False,
        "category": ["resource_limits"],
        "keywords": ["64Mi"],
    }
    assert evals.score_check({"expect": expect}, outcome) == []
    expect["category"] = ["bad_config"]
    assert evals.score_check({"expect": expect}, outcome) == [
        "category resource_limits not in ['bad_config']"
    ]


@pytest.mark.anyio
async def test_a_check_proposing_an_unlisted_action_fails(settings: Settings) -> None:
    evict = call("kubernetes_evict_pod", "c1", cluster="kind", namespace="shop", name="p")
    spec = {"name": "checkout-up", "prompt": "Is checkout running?"}
    toolbox = replay(settings, {"kubernetes_evict_pod": "evicted"}, actions=True)
    async with agent.open_agent(settings, toolbox, scripted(evict), kind="check") as a:
        outcome, _ = await evals.check(a, spec, "k3", {})
    assert evals.score_check({"expect": {"passed": False}}, outcome) == [
        "attempted write action: ['kubernetes_evict_pod']"
    ]


def test_a_checks_scenario_gets_the_general_teachings_only() -> None:
    taught = [{"text": "For crash loops.", "alerts": ["KubePodCrashLooping"]}, {"text": "General."}]
    text = evals.scenario_taught({"check": {"name": "x", "prompt": "y"}, "teachings": taught})
    assert "General." in text and "For crash loops." not in text


@pytest.mark.anyio
async def test_a_checks_allowed_proposal_is_rejected_and_the_verdict_scored(
    settings: Settings,
) -> None:
    evict = call("kubernetes_evict_pod", "c1", cluster="kind", namespace="shop", name="p")
    bad = call(
        "CheckResult",
        "c2",
        passed=False,
        summary="x",
        evidence=[REPORT["evidence"][0]],
        report=REPORT,
    )
    expect = {"passed": False, "category": ["resource_limits"]}
    expect["allowed_actions"] = [{"name": "kubernetes_evict_pod"}]  # type: ignore[assignment]
    spec = {"name": "checkout-up", "prompt": "Is checkout running?"}
    toolbox = replay(settings, {"kubernetes_evict_pod": "evicted"}, actions=True)
    async with agent.open_agent(settings, toolbox, scripted(evict, bad), kind="check") as a:
        outcome, proposed = await evals.check(a, spec, "k4", expect)
    assert [p["name"] for p in proposed] == ["kubernetes_evict_pod"]
    assert outcome.executed_actions == [] and outcome.check is not None
    assert evals.score_check({"expect": expect}, outcome) == []


@pytest.mark.anyio
async def test_the_two_cluster_scenario_answers_each_cluster_its_own(settings: Settings) -> None:
    from fastmcp import Client

    s = yaml.safe_load((SCENARIOS / "two-clusters-alert-picks-one.yaml").read_text())
    contract = evals.scenario_contract(
        "", False, [Path("no-runbooks")], s["clusters"], s["integrations"]
    )
    toolbox = evals.replay_server(contract, settings, s["tools"])
    async with Client(toolbox.mcp) as c:
        args = {"namespace": "demo"}
        eu = await c.call_tool("kubernetes_list_pods", {"cluster": "prod-eu", **args})
        us = await c.call_tool("kubernetes_list_pods", {"cluster": "prod-us", **args})
        assert "OOMKilled" in str(eu.structured_content) and "OOMKilled" not in str(
            us.structured_content
        )
        pod = {"cluster": "prod-us", "namespace": "demo", "name": "reports-55b4fffc66-hmq7f"}
        missing = await c.call_tool("kubernetes_describe_pod", pod, raise_on_error=False)
        assert missing.is_error  # the broken pod exists in prod-eu only
    assert s["alert"]["commonLabels"]["cluster"] == "prod-eu"
