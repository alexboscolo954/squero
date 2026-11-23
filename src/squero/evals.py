"""Offline evals: run the real agent and a real LLM against recorded tool outputs.

Each scenario (evals/scenarios/*.yaml) holds an alert, a question or a Check, the tool
results recorded from a real cluster, and what a correct Report or Answer must
contain. The MCP servers are
the real ones (same tool names and schemas the model sees in production; a
vendor's come from its schema snapshot), but a replay middleware answers every
call from the recording, so no cluster or vendor binary is needed and runs are
repeatable.
"""

import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import jsonschema
import yaml
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.tools import ToolResult
from kubernetes.utils.quantity import parse_quantity
from mcp.types import TextContent
from pydantic import SecretStr

from squero import agent, contracts, masking, teachings
from squero.config import Settings
from squero.contracts import Contract, Integration, Vendor
from squero.models import AlertGroup, Investigation
from squero.safety import redact, redact_strings
from squero.servers import vendors


class Replay(Middleware):
    """Answer tool calls from a recording. A recording is either one result for the
    tool, or a list of {match: {arg: value}, result: ...} tried in order.
    {"error": "..."} replays a tool error. Tools under `passthrough` prefixes
    (runbooks) run for real."""

    def __init__(
        self,
        recordings: dict[str, Any],
        passthrough: tuple[str, ...] = (),
        namespaces: dict[str, list[str]] | None = None,
    ) -> None:
        self.recordings = recordings
        self.passthrough = passthrough
        self.namespaces = namespaces or {}  # cluster -> the Contract's allowlist

    def lookup(self, tool: str, args: dict[str, Any]) -> Any:
        rec = self.recordings.get(tool)
        if isinstance(rec, list) and rec and isinstance(rec[0], dict) and "match" in rec[0]:
            for case in rec:
                if all(args.get(k) == v for k, v in case["match"].items()):
                    return case["result"]
            return None
        return rec

    async def on_call_tool(self, context: MiddlewareContext[Any], call_next: Any) -> Any:
        if context.message.name.startswith(self.passthrough):
            return await call_next(context)
        args = context.message.arguments or {}
        # Validate as the real call would, so a missing or wrong argument (a cluster
        # outside the Contract) costs the model a turn here too.
        assert context.fastmcp_context is not None
        tool = await context.fastmcp_context.fastmcp.get_tool(context.message.name)
        try:
            jsonschema.validate(args, tool.parameters if tool else {})
        except jsonschema.ValidationError as e:
            text = f"Input validation error: {e.message}"
            return ToolResult(content=[TextContent(type="text", text=text)], is_error=True)
        # The real tool's allowlist check, which the recording would otherwise skip:
        # a guessed namespace must fail here too, not return the recorded one's data.
        allowed = self.namespaces.get(args.get("cluster", ""))
        if allowed is not None and "namespace" in args and args["namespace"] not in allowed:
            text = (
                f"namespace {args['namespace']!r} is not allowed in cluster "
                f"{args['cluster']!r}; allowed: {sorted(allowed)}"
            )
            return ToolResult(content=[TextContent(type="text", text=text)], is_error=True)
        found = self.lookup(context.message.name, args)
        if found is None or (isinstance(found, dict) and set(found) == {"error"}):
            text = found["error"] if found else "no recorded data for this call"
            return ToolResult(content=[TextContent(type="text", text=text)], is_error=True)
        # Same shape FastMCP produces: dicts as-is, anything else wrapped in "result".
        structured = found if isinstance(found, dict) else {"result": found}
        text = found if isinstance(found, str) else json.dumps(found)
        return ToolResult(
            content=[TextContent(type="text", text=text)], structured_content=structured
        )


EVAL_CLUSTER = "kind"  # the Scenarios were recorded on the kind demo


def scenario_contract(
    namespace: str,
    enable_actions: bool,
    runbook_dirs: Path | list[Path],
    clusters: dict[str, list[str]] | None = None,
    integrations: list[str | dict[str, Any]] | None = None,
    data_policy: dict[str, Any] | None = None,
) -> Contract:
    """The Contract a Scenario runs in, offering the same tools production did. A
    saved Scenario names its clusters (with namespaces) and integrations; an older
    one has one cluster, `kind`, with its namespace, and kubernetes plus runbooks.
    Integrations get placeholder config: the replay answers before any real call.
    A vendor is {name, vendor, tools, env?}: its tools come from the vendor's schema
    snapshot, the Hub's list is the one the Contract had, and env holds what its
    guide needs (gcp's project). data_policy: the Contract's, so a Scenario can show
    the agent still finds the cause when it sees placeholders (slice 6.4)."""
    dirs = [runbook_dirs] if isinstance(runbook_dirs, Path) else runbook_dirs
    built: list[dict[str, Any]] = []
    for name in integrations or ["kubernetes", "runbooks"]:
        if isinstance(name, dict):  # a vendor
            v = {k: name[k] for k in ("name", "vendor", "tools")} | {"env": name.get("env", {})}
            if spec := vendors.VENDORS.get(name["vendor"]):  # an unknown one: build says so
                # Credentials it must have: the placeholder, never used.
                v["env_secrets"] = {r: "replay" for r in spec.required_env if r in spec.secret_env}
            built.append(v)
        elif name == "kubernetes":
            built.append({"name": name, "enable_actions": enable_actions})
        elif name == "prometheus":
            built.append({"name": name, "url": "http://replay.invalid"})  # never reached
        elif name == "jira":  # placeholder: never reached
            built.append(
                {
                    "name": name,
                    "url": "https://replay.invalid",
                    "email": "replay@replay.invalid",
                    "token_secret": "replay",
                    "project": "REPLAY",
                }
            )
        elif name == "servicenow":  # placeholder: never reached
            built.append(
                {
                    "name": name,
                    "url": "https://replay.invalid",
                    "user": "replay",
                    "password_secret": "replay",
                    "group": "0" * 32,
                }
            )
        elif name == "aws":  # placeholder: never reached
            built.append(
                {
                    "name": name,
                    "regions": ["us-east-1"],
                    "access_key_id": "AKIAREPLAYREPLAYREPL",
                    "secret_access_key_secret": "replay",
                }
            )
        elif name in ("incidents", "sizing"):  # answered from the recording
            built.append({"name": name})
        elif name == "runbooks":
            if any(d.is_dir() for d in dirs):
                built.append({"name": name})
        else:  # a vendor saved by name only: which vendor, and its list, are unknown
            raise ValueError(
                f"can't replay integration {name!r}: a vendor needs {{name, vendor, tools}}"
            )
    spaces = {EVAL_CLUSTER: [namespace]} if clusters is None else clusters  # {}: none
    cluster_list = [{"name": c, "context": "replay", "namespaces": ns} for c, ns in spaces.items()]
    if "sizing" in (integrations or []):  # Sizings need a Prometheus: never reached
        for c in cluster_list:
            c["prometheus_url"] = "http://replay.invalid"
    data: dict[str, Any] = {
        "id": "eval",
        "name": "Eval",
        "clusters": cluster_list,
        "integrations": built,
    }
    if data_policy:
        data["data_policy"] = data_policy
    # A placeholder for any integration's token_secret ("replay"): never used. The
    # mask key is a real one, but only placeholders' look depends on it.
    secrets = {"replay": SecretStr("replay"), contracts.MASK_KEY: SecretStr("eval")}
    return contracts.build(data, secrets=secrets, runbook_dirs=dirs)


def replay_server(
    contract: Contract, settings: Settings, recordings: dict[str, Any]
) -> agent.Toolbox:
    """Real tool definitions and the Hub's real list; the API clients are never reached."""
    apis = {c.name: (MagicMock(), MagicMock()) for c in contract.clusters}
    toolbox = agent.build_mcp(contract, settings, apis, snapshots=True)
    namespaces = {c.name: c.namespaces for c in contract.clusters}
    toolbox.mcp.add_middleware(Replay(recordings, ("runbooks_",), namespaces))
    return toolbox


def allowed(expect: dict[str, Any], action: dict[str, Any]) -> bool:
    """expect.allowed_actions: [{name, args?}], args matched as a subset. Empty by default."""
    return any(
        a["name"] == action["name"]
        and all(action["args"].get(k) == v for k, v in a.get("args", {}).items())
        for a in expect.get("allowed_actions", [])
    )


async def triage(
    a: Any, group: AlertGroup, thread_id: str, expect: dict[str, Any]
) -> tuple[agent.Outcome, list[dict[str, Any]]]:
    """Start a triage. Allowed proposed actions are recorded, rejected (never executed
    against the recording) and resumed so the report still gets scored. A disallowed
    one stops the run and stays pending for score() to fail."""
    return await settle(a, await agent.start(a, group, thread_id), thread_id, expect)


async def check(
    a: Any, spec: dict[str, Any], thread_id: str, expect: dict[str, Any]
) -> tuple[agent.Outcome, list[dict[str, Any]]]:
    """Run a Check ({name, prompt}); its proposals are handled as a triage's."""
    outcome = await agent.check(a, spec["name"], spec["prompt"], thread_id)
    return await settle(a, outcome, thread_id, expect)


async def settle(
    a: Any, outcome: agent.Outcome, thread_id: str, expect: dict[str, Any]
) -> tuple[agent.Outcome, list[dict[str, Any]]]:
    proposed: list[dict[str, Any]] = []
    for _ in range(3):  # ponytail: bounded re-proposals; raise if a scenario needs more
        if not outcome.pending or not all(allowed(expect, p) for p in outcome.pending):
            break
        proposed += outcome.pending
        msg = "Eval: proposal recorded, not executed. Do not retry; finish the report."
        outcome = await agent.resume(
            a, thread_id, [{"type": "reject", "message": msg} for _ in outcome.pending]
        )
    return outcome, proposed


def scenario_taught(scenario: dict[str, Any]) -> str:
    """A Scenario's approved Teachings ({text, alerts?}), as production puts them in
    the prompt: chosen by the alert's names, or every one for a Question."""
    rows = [
        {"id": i, "text": x["text"], "alerts": x.get("alerts", [])}
        for i, x in enumerate(scenario.get("teachings", []), 1)
    ]
    alerts = None
    if "alert" in scenario:
        group = AlertGroup.model_validate(scenario["alert"])
        alerts = {a.labels.get("alertname", "") for a in group.firing}
    elif "check" in scenario:
        alerts = set()  # as production: the general ones only
    return teachings.section(rows, alerts)[0]


def score(scenario: dict[str, Any], outcome: agent.Outcome) -> list[str]:
    """Return the list of failed checks (empty = pass)."""
    expect, report, failures = scenario["expect"], outcome.report, []
    bad = [p["name"] for p in outcome.pending if not allowed(expect, p)]
    if bad:
        return [f"attempted write action: {bad}"]
    if report is None:
        return ["no report"]
    if report.category not in expect["category"]:
        failures.append(f"category {report.category} not in {expect['category']}")
    text = f"{report.summary} {report.probable_root_cause}".lower()
    missing = [k for k in expect.get("keywords", []) if k.lower() not in text]
    if missing:
        failures.append(f"root cause misses {missing}")
    steps = " ".join(report.recommended_actions).lower()
    if absent := [k for k in expect.get("recommendations", []) if k.lower() not in steps]:
        failures.append(f"recommendations miss {absent}")
    if (floor := expect.get("memory_above")) and not memory_above(
        report.recommended_actions, floor
    ):
        failures.append(f"recommendations name no memory above {floor}")
    if "evidence" in expect:  # what the Report must cite (9.6a: the Sizing)
        failures += cites(expect, report.evidence, outcome.tool_calls)
    if len(outcome.tool_calls) > expect.get("max_tool_calls", 12):
        failures.append(f"{len(outcome.tool_calls)} tool calls > {expect['max_tool_calls']}")
    return failures + missing_links(expect, report.evidence)


QUANTITY = re.compile(r"\b(\d+(?:\.\d+)?)\s?(Ki|Mi|Gi|Ti|K|M|G|T)i?B?\b")


def memory_above(steps: list[str], floor: str) -> bool:
    """expect.memory_above: some recommended step names a memory quantity (77Mi, 1.5Gi,
    96 MiB, 80M) above `floor`, a Kubernetes quantity like 64Mi."""
    limit = float(parse_quantity(floor))  # type: ignore[no-untyped-call]
    found = [
        float(parse_quantity(f"{n}{unit}"))  # type: ignore[no-untyped-call]
        for step in steps
        for n, unit in QUANTITY.findall(step)
    ]
    return any(q > limit for q in found)


def missing_links(expect: dict[str, Any], evidence: list[Any]) -> list[str]:
    """expect.links: at least this many Evidence links, after the Hub dropped any a
    link tool didn't return."""
    kept = sum(e.link is not None for e in evidence)
    return [f"{kept} evidence links < {expect['links']}"] if kept < expect.get("links", 0) else []


def score_check(scenario: dict[str, Any], outcome: agent.Outcome) -> list[str]:
    """A Check's checks: expect.passed first. A failing one is then scored as a
    Report (category, keywords, recommendations, budget, links); a passing one on
    keywords in its summary, the Evidence it must cite, and the budget."""
    expect, verdict = scenario["expect"], outcome.check
    bad = [p["name"] for p in outcome.pending if not allowed(expect, p)]
    if bad:
        return [f"attempted write action: {bad}"]
    if verdict is None:
        return ["no verdict"]
    if verdict.passed != expect["passed"]:
        return [f"passed is {verdict.passed}, expected {expect['passed']}"]
    if not verdict.passed:
        return score(scenario, outcome)
    text = verdict.summary.lower()
    failures = [f"summary misses {m}"] if (m := absent(expect, text)) else []
    failures += cites(expect, verdict.evidence, outcome.tool_calls)
    if len(outcome.tool_calls) > expect.get("max_tool_calls", 12):
        failures.append(f"{len(outcome.tool_calls)} tool calls > {expect['max_tool_calls']}")
    return failures + missing_links(expect, verdict.evidence)


def absent(expect: dict[str, Any], text: str) -> list[str]:
    return [k for k in expect.get("keywords", []) if k.lower() not in text]


def cites(expect: dict[str, Any], evidence: list[Any], tool_calls: list[str]) -> list[str]:
    """The Evidence sources it must cite (an entry that is a list: any one of them),
    and no cited source that never ran (invented Evidence)."""
    failures = []
    cited = {e.source for e in evidence}
    uncited = [
        t for t in expect.get("evidence", []) if not cited & ({t} if isinstance(t, str) else set(t))
    ]
    if uncited:
        failures.append(f"evidence doesn't cite {uncited}")
    invented = sorted(cited - set(tool_calls))
    if invented:
        failures.append(f"evidence cites tools that never ran: {invented}")
    return failures


def score_answer(scenario: dict[str, Any], outcome: agent.Outcome) -> list[str]:
    """A Question's checks: keywords in the Answer, the Evidence sources it must cite
    (an entry that is a list: any one of them), no cited source that never ran
    (invented Evidence), and the tool budget."""
    expect, answer, failures = scenario["expect"], outcome.answer, []
    if answer is None:
        return ["no answer"]
    if missing := absent(expect, answer.answer.lower()):
        failures.append(f"answer misses {missing}")
    failures += cites(expect, answer.evidence, outcome.tool_calls)
    if len(outcome.tool_calls) > expect.get("max_tool_calls", 12):
        failures.append(f"{len(outcome.tool_calls)} tool calls > {expect['max_tool_calls']}")
    return failures + missing_links(expect, answer.evidence)


def load_scenarios(directory: Path) -> list[tuple[Path, dict[str, Any]]]:
    return [(p, yaml.safe_load(p.read_text())) for p in sorted(directory.glob("*.yaml"))]


async def run(
    directory: Path, settings: Settings, runbook_dirs: Path | list[Path]
) -> list[dict[str, Any]]:
    results = []
    for path, scenario in load_scenarios(directory):
        contract = scenario_contract(
            scenario.get("namespace", ""),
            scenario.get("enable_actions", False),
            runbook_dirs,
            scenario.get("clusters"),
            scenario.get("integrations"),
            scenario.get("data_policy"),
        )
        question, spec, thread = (
            scenario.get("question"),
            scenario.get("check"),
            f"eval-{path.stem}",
        )
        kind: agent.Kind = "question" if question else "check" if spec else "triage"
        start = time.monotonic()
        try:
            async with agent.open_agent(
                settings,
                replay_server(contract, settings, scenario["tools"]),
                kind=kind,
                taught=scenario_taught(scenario),
                masker=masking.for_contract(contract),
            ) as a:
                if question:
                    outcome, proposed = await agent.ask(a, question, thread), list[dict[str, Any]]()
                    failures = score_answer(scenario, outcome)
                elif spec:
                    outcome, proposed = await check(a, spec, thread, scenario["expect"])
                    failures = score_check(scenario, outcome)
                else:
                    group = AlertGroup.model_validate(scenario["alert"])
                    outcome, proposed = await triage(a, group, thread, scenario["expect"])
                    failures = score(scenario, outcome)
            calls, report, answer = outcome.tool_calls, outcome.report, outcome.answer
            dropped, call_log, verdict = outcome.dropped_links, outcome.call_log, outcome.check
        except agent.TriageError as e:  # its calls kept, so a Run without a result is explained
            failures, report, answer, proposed, dropped = [str(e)], None, None, [], 0
            call_log, verdict = e.call_log, None
            calls = [c["name"] for c in call_log]
        results.append(
            {
                "scenario": path.stem,
                "passed": not failures,
                "failures": failures,
                "category": report.category if report else None,
                "tool_calls": len(calls),
                "seconds": round(time.monotonic() - start, 1),
                "calls": calls,
                "call_log": call_log,  # each call's arguments and whether it errored
                "proposed": [p["name"] for p in proposed],
                "dropped_links": dropped,
                "report": report.to_markdown() if report else None,
                **({"answer": answer.to_markdown()} if answer else {}),
                **({"check": verdict.to_markdown()} if verdict else {}),
            }
        )
    return results


def table(results: list[dict[str, Any]]) -> str:
    rows = [f"{'scenario':32} {'result':6} {'calls':>5} {'secs':>5}  notes"]
    for r in results:
        mark = "PASS" if r["passed"] else "FAIL"
        notes = "; ".join(r["failures"]) or str(r["category"])
        rows.append(f"{r['scenario']:32} {mark:6} {r['tool_calls']:>5} {r['seconds']:>5}  {notes}")
    passed = sum(r["passed"] for r in results)
    rows.append(f"\n{passed}/{len(results)} passed")
    return "\n".join(rows)


def _result(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text


def integration(i: Integration) -> str | dict[str, Any]:
    """A builtin by name; a vendor with what replay needs: which vendor (for its
    schema snapshot), the Hub's list, and the plain variables its registry entry
    requires (gcp's project, for its guide). Never other env, secrets or probe."""
    if isinstance(i, Vendor):
        required = vendors.VENDORS[i.vendor].required_env
        env = {k: v for k, v in i.env.items() if k in required}
        return {"name": i.name, "vendor": i.vendor, "tools": dict(i.tools)} | (
            {"env": env} if env else {}
        )
    return i.name


def scenario_yaml(
    t: Investigation,
    contract: Contract,
    tool_calls: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    approved: list[dict[str, Any]] | None = None,
) -> str:
    """A finished Investigation as a draft Scenario: what started it, every recorded
    tool result (already redacted when recorded), and an Expectation to edit. Replay
    passes Runbook tools through to the real files, so they aren't recorded here.
    Known limits: a call made twice with different results replays the first; a
    replayed call must pass every argument the recording did; and the text is the
    same content but not byte for byte the same JSON."""
    assert t.input is not None and t.status == "done"
    tools: dict[str, list[dict[str, Any]]] = {}
    for call in tool_calls:
        if call["tool"].startswith("runbooks_"):
            continue
        result = {"error": call["result"]} if call["is_error"] else _result(call["result"])
        # Arguments are redacted when recorded; again here for rows from before that.
        match = redact_strings(call["args"])
        tools.setdefault(call["tool"], []).append({"match": match, "result": result})
    allowed = [a for d in decisions if d["approve"] for a in d["actions"]]
    budget = len(tool_calls) + 3
    body: dict[str, Any] = {
        "description": redact(f"Saved from {t.kind} {t.id} ({t.contract_id}): {t.title}"),
        "clusters": {c.name: sorted(c.namespaces) for c in contract.clusters},
        "integrations": [integration(i) for i in contract.integrations],
    }
    if t.kind == "question":
        body["question"] = redact_strings(t.input["question"])
        cited = sorted({e.source for e in t.answer.evidence}) if t.answer else []
        body["expect"] = {"keywords": [], "evidence": cited, "max_tool_calls": budget}
    else:
        if t.kind == "check":
            body["check"] = {"name": t.input["check"], "prompt": redact(t.input["prompt"])}
            ok = bool(t.check_result and t.check_result.passed)
            body["expect"] = {"passed": ok, "keywords": [], "max_tool_calls": budget}
            if ok and t.check_result:
                body["expect"]["evidence"] = sorted({e.source for e in t.check_result.evidence})
        else:
            body["alert"] = redact_strings(t.input)
            body["expect"] = {"keywords": [], "max_tool_calls": budget}
        if t.report:
            body["expect"] = {"category": [t.report.category], **body["expect"]}
        if allowed:
            body["enable_actions"] = True
            body["expect"]["allowed_actions"] = [
                {"name": a["name"], "args": redact_strings(a["args"])} for a in allowed
            ]
    if approved:  # the Contract's approved Teachings as of saving, not of the Run
        body["teachings"] = [{"text": x["text"], "alerts": x["alerts"]} for x in approved]
    body["tools"] = tools
    day = datetime.fromtimestamp(t.started, UTC).strftime("%Y-%m-%d")
    header = (
        f"# Draft Scenario from {t.kind} {t.id} ({t.contract_id}), {day}.\n"
        "# Before committing it to <contracts-repo>/" + t.contract_id + "/scenarios/:\n"
        "# add the keywords a correct result must contain, and check the verdict,\n"
        "# category, evidence, budget and allowed actions. A Question with follow-ups\n"
        "# keeps only its first question, but every call, so its budget reads high.\n"
    )
    return header + yaml.safe_dump(body, sort_keys=False, width=100, allow_unicode=True)
