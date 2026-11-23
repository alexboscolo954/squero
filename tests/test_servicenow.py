"""The ServiceNow builtin (slice 19.5a): past incidents of the Contract's own
assignment group as Evidence. Responses are synthetic, in the Table API's documented
shapes."""

import base64
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastmcp import Client
from pydantic import SecretStr

from squero import agent, cli, contracts, evals
from squero.config import Settings
from squero.servers import servicenow
from tests.conftest import REPORT, call, scripted
from tests.test_agent import ALERT

URL = "https://acme.service-now.com"
GROUP = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
OTHER = "f0e1d2c3b4a5f6e7d8c9b0a1f2e3d4c5"
SYS_ID = "c1d2e3f4a5b6c7d8e9f0a1b2c3d4e5f6"
SECRET = "s3cret-passw0rd"
INTEGRATION = {
    "name": "servicenow",
    "url": URL,
    "user": "squero.reader",
    "password_secret": "servicenow-password",
    "group": GROUP,
}


def cell(value: str, display: str | None = None) -> dict[str, str]:
    """A field as `sysparm_display_value=all` answers it."""
    return {"display_value": display or value, "value": value}


def incident(number: str = "INC0010042", group: Any = None, **over: Any) -> dict[str, Any]:
    return {
        "number": cell(number),
        "sys_id": cell(SYS_ID),
        "short_description": cell("payments 502s: fraud-check refused connections"),
        "state": cell("6", "Resolved"),
        "opened_at": cell("2026-08-01 10:00:00", "01/08/2026 12:00:00"),
        "resolved_at": cell("2026-08-01 12:30:00", "01/08/2026 14:30:00"),
        "close_code": cell("solved_permanently", "Solved (Permanently)"),
        "assignment_group": cell(GROUP, "Payments SRE") if group is None else group,
        "description": cell("fraud-check pods were OOMKilled.\nRaised its memory limit.\n"),
        "close_notes": cell("  Memory limit raised to 512Mi.\n"),
        **over,
    }


class ServiceNow:
    """A fake instance: records requests and answers with `rows`, whatever the query
    asked. A real one drops a clause it can't read, so the server can't trust it."""

    def __init__(self, rows: list[Any] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.rows = rows if rows is not None else [incident()]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json={"result": self.rows})

    def server(self, group: str = GROUP) -> Any:
        http = httpx.Client(base_url=URL, transport=httpx.MockTransport(self))
        return servicenow.build_server(URL, "squero.reader", SecretStr(SECRET), group, http)


def answering(response: httpx.Response | Exception) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(response, Exception):
            raise response
        return response

    http = httpx.Client(base_url=URL, transport=httpx.MockTransport(handler))
    return servicenow.build_server(URL, "squero.reader", SecretStr(SECRET), GROUP, http)


def like(*words: str) -> str:
    return "^".join(f"short_descriptionLIKE{w}^ORdescriptionLIKE{w}" for w in words)


@pytest.mark.anyio
async def test_search_writes_its_own_query_inside_the_group() -> None:
    """The model passes words; the server writes the query. An injection attempt
    can't add a clause: no `^`, `=` or colon of its own reaches ServiceNow."""
    fake = ServiceNow()
    text = "fraud-check^ORassignment_group=other^NQactive=true javascript:gs.x()"
    async with Client(fake.server()) as c:
        found = await c.call_tool("search_incidents", {"text": text})
    (request,) = fake.requests
    assert request.method == "GET" and request.url.path == "/api/now/table/incident"
    query = request.url.params["sysparm_query"]
    assert query == (
        f"assignment_group={GROUP}^"
        + like("fraud", "check", "ORassignment_group", "other", "NQactive")
        + "^opened_at>=javascript:gs.daysAgoStart(365)^ORDERBYDESCopened_at"
    )
    assert query.count("assignment_group=") == 1 and query.count("=") == 2
    assert query.count("javascript:") == 1 and "^NQ" not in query
    assert dict(request.url.params) == {
        "sysparm_query": query,
        "sysparm_fields": (
            "number,sys_id,short_description,state,opened_at,resolved_at,"
            "close_code,assignment_group"
        ),
        "sysparm_limit": "10",
        "sysparm_display_value": "all",
        "sysparm_exclude_reference_link": "true",
    }
    assert found.structured_content["result"] == [
        {
            "number": "INC0010042",
            "summary": "payments 502s: fraud-check refused connections",
            "state": "Resolved",  # its name, not its number
            "opened": "2026-08-01 10:00:00",  # as stored (UTC), not in the user's format
            "resolved": "2026-08-01 12:30:00",
            "resolution": "Solved (Permanently)",
            "url": f"{URL}/incident.do?sys_id={SYS_ID}",
        }
    ]


@pytest.mark.anyio
async def test_both_tools_claim_to_be_reads_and_a_slash_after_the_url_is_dropped() -> None:
    """A tool that doesn't claim read-only is interrupted for approval."""
    fake = ServiceNow()
    http = httpx.Client(base_url=URL, transport=httpx.MockTransport(fake))
    server = servicenow.build_server(URL + "/", "u", SecretStr(SECRET), GROUP, http)
    async with Client(server) as c:
        tools = {t.name: t.annotations.read_only_hint for t in await c.list_tools()}
        got = await c.call_tool("get_incident", {"number": "INC0010042"})
    assert tools == {"search_incidents": True, "get_incident": True}
    assert got.structured_content["url"] == f"{URL}/incident.do?sys_id={SYS_ID}"


@pytest.mark.parametrize(
    ("text", "words"),
    [
        ("payments", ["payments"]),
        ("fraud-check.risk", ["fraud", "check", "risk"]),
        ("oom oom OOM oom", ["oom", "OOM"]),  # each different word once
        ("a b c d e f g", ["a", "b", "c", "d", "e"]),  # the first five
        ("x" * 60, ["x" * 40]),
        ("a^b=c,d:e", ["a", "b", "c", "d", "e"]),
        ("^=,: ()", []),
    ],
)
def test_only_words_reach_the_query(text: str, words: list[str]) -> None:
    assert servicenow.terms(text) == words


@pytest.mark.anyio
async def test_a_search_with_no_words_asks_nothing() -> None:
    fake = ServiceNow()
    async with Client(fake.server()) as c:
        r = await c.call_tool("search_incidents", {"text": "^^ == ::"}, raise_on_error=False)
    assert r.is_error and r.content[0].text == "give some words to search for"
    assert fake.requests == []


@pytest.mark.parametrize(
    ("asked", "days", "limit"),
    [
        ({"days": 0, "limit": 0}, 1, "1"),
        ({"days": -5, "limit": -5}, 1, "1"),
        ({"days": 99999, "limit": 999}, 3650, "25"),
        ({"days": 30, "limit": 3}, 30, "3"),
    ],
)
@pytest.mark.anyio
async def test_days_and_limit_stay_in_bounds(asked: dict[str, int], days: int, limit: str) -> None:
    fake = ServiceNow()
    async with Client(fake.server()) as c:
        await c.call_tool("search_incidents", {"text": "x", **asked})
    params = fake.requests[0].url.params
    assert f"^opened_at>=javascript:gs.daysAgoStart({days})^" in params["sysparm_query"]
    assert params["sysparm_limit"] == limit


@pytest.mark.anyio
async def test_a_row_of_another_group_never_reaches_the_model(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ServiceNow answers with whatever it likes (a dropped clause, another shape):
    only a row whose stored group is the Contract's is kept, and the drop is logged
    by its count alone."""
    rows = [
        incident("INC0000001"),  # {display_value, value}
        incident("INC0000002", group=GROUP),  # a plain sys_id
        incident("INC0000003", group={"link": f"{URL}/api/x", "value": GROUP}),
        incident("INC0000004", group=cell(OTHER, "Bank SRE")),
        incident("INC0000005", group={"display_value": GROUP}),  # a name that looks like it
        incident("INC0000006", group=""),  # no group
        incident("INC0000007", group=[GROUP]),
        "not a row",
        {key: value for key, value in incident("INC0000008").items() if key != "assignment_group"},
    ]
    fake = ServiceNow(rows)
    with caplog.at_level(logging.WARNING, logger="squero.servers.servicenow"):
        async with Client(fake.server()) as c:
            found = await c.call_tool("search_incidents", {"text": "payments"})
    numbers = [r["number"] for r in found.structured_content["result"]]
    assert numbers == ["INC0000001", "INC0000002", "INC0000003"]
    (record,) = caplog.records
    assert record.getMessage() == (
        "servicenow: 6 of 9 rows were not the Contract's assignment group's: dropped"
    )


@pytest.mark.anyio
async def test_all_rows_of_the_group_log_nothing(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="squero.servers.servicenow"):
        async with Client(ServiceNow().server()) as c:
            await c.call_tool("search_incidents", {"text": "payments"})
    assert caplog.records == []


@pytest.mark.anyio
async def test_get_incident_reads_one_incident_of_the_group() -> None:
    fake = ServiceNow()
    async with Client(fake.server()) as c:
        got = (await c.call_tool("get_incident", {"number": "INC0010042"})).structured_content
    assert got == {
        "number": "INC0010042",
        "summary": "payments 502s: fraud-check refused connections",
        "state": "Resolved",
        "opened": "2026-08-01 10:00:00",
        "resolved": "2026-08-01 12:30:00",
        "resolution": "Solved (Permanently)",
        "url": f"{URL}/incident.do?sys_id={SYS_ID}",
        "description": "fraud-check pods were OOMKilled.\nRaised its memory limit.",
        "resolution_notes": "Memory limit raised to 512Mi.",
    }
    params = fake.requests[0].url.params
    assert params["sysparm_query"] == f"number=INC0010042^assignment_group={GROUP}"
    assert params["sysparm_limit"] == "1"
    assert params["sysparm_fields"].endswith(",assignment_group,description,close_notes")
    assert params["sysparm_display_value"] == "all"


@pytest.mark.parametrize(
    "row",
    [
        incident(group=cell(OTHER, "Bank SRE")),  # the group clause was dropped
        incident("INC0099999"),  # the number clause was dropped: another incident
        {**incident("INC0099999"), "number": {"display_value": "INC0010042"}},  # shown, not stored
    ],
)
@pytest.mark.anyio
async def test_get_incident_refuses_what_it_did_not_ask_for(row: dict[str, Any]) -> None:
    secret = "the bank's own outage"
    fake = ServiceNow([{**row, "description": cell(secret)}])
    async with Client(fake.server()) as c:
        r = await c.call_tool("get_incident", {"number": "INC0010042"}, raise_on_error=False)
    assert r.is_error
    assert r.content[0].text == "no incident INC0010042 in this Contract's assignment group"
    assert secret not in str(r)


@pytest.mark.parametrize(
    "number",
    [
        "../../sys_user",
        "INC0010042^ORactive=true",
        "inc0010042",
        "INC",
        "0010042",
        "INC0010042 ",
        "I0010042",
        "INC001",
        "ABCDEFGHIJK0010042",
        "INC" + "1" * 13,
        "",
    ],
)
@pytest.mark.anyio
async def test_an_odd_number_asks_nothing(number: str) -> None:
    fake = ServiceNow()
    async with Client(fake.server()) as c:
        r = await c.call_tool("get_incident", {"number": number}, raise_on_error=False)
    assert r.is_error and r.content[0].text.startswith("not an incident number: ")
    assert fake.requests == []


@pytest.mark.parametrize("number", ["INC0010042", "PRB0001", "CS" + "1" * 12, "ABCDEFGHIJ0010"])
@pytest.mark.anyio
async def test_a_number_with_another_prefix_is_asked(number: str) -> None:
    fake = ServiceNow([incident(number)])
    async with Client(fake.server()) as c:
        got = await c.call_tool("get_incident", {"number": number})
    assert got.structured_content["number"] == number


@pytest.mark.anyio
async def test_long_text_is_cut_and_an_odd_id_makes_no_link() -> None:
    row = incident(
        short_description=cell("s" * 400),
        description=cell("d" * 5000),
        close_notes=cell("n" * 2000),
        state=cell("6", "S" * 80),
        close_code=cell("c" * 150),
        sys_id=cell(f"{SYS_ID}/../../login.do"),
        resolved_at=cell(""),
        opened_at=cell("9" * 60),
    )
    async with Client(ServiceNow([row]).server()) as c:
        got = (await c.call_tool("get_incident", {"number": "INC0010042"})).structured_content
    assert got["summary"].startswith("s" * 300 + "\n...[truncated 100 chars]")
    assert got["description"].startswith("d" * 4000 + "\n...[truncated 1000 chars]")
    assert got["resolution_notes"].startswith("n" * 1500 + "\n...[truncated 500 chars]")
    assert got["state"] == "S" * 40 and got["resolution"] == "c" * 100
    assert got["url"] is None and got["resolved"] is None and got["opened"] == "9" * 40


@pytest.mark.anyio
async def test_a_field_that_is_not_text_is_empty() -> None:
    row = incident(
        state=6,
        close_code=None,
        opened_at=["x"],
        description={"value": 7},
        short_description={"value": "stored only"},  # no display value: the stored one
    )
    async with Client(ServiceNow([row]).server()) as c:
        got = (await c.call_tool("get_incident", {"number": "INC0010042"})).structured_content
    assert got["summary"] == "stored only"
    assert (got["state"], got["resolution"], got["opened"], got["description"]) == (
        "",
        None,
        None,
        "",
    )


@pytest.mark.parametrize(
    ("response", "said"),
    [
        (
            httpx.Response(401, json={"error": {"message": "User Not Authenticated"}}),
            "ServiceNow error 401: User Not Authenticated",
        ),
        (
            httpx.Response(403, json={"error": {"message": "m" * 500, "detail": SECRET}}),
            "ServiceNow error 403: " + "m" * 200,
        ),
        (
            httpx.Response(400, json={"error": {"message": "Bad\n  query\tgiven \n"}}),
            "ServiceNow error 400: Bad query given",
        ),
        (httpx.Response(401, json={"error": "nope"}), "ServiceNow error 401: Unauthorized"),
        (
            httpx.Response(401, json={"error": {"message": {"a": 1}}}),
            "ServiceNow error 401: Unauthorized",
        ),
        (
            httpx.Response(401, json={"error": {"message": ""}}),
            "ServiceNow error 401: Unauthorized",
        ),
        (httpx.Response(429, json=["slow down"]), "ServiceNow error 429: Too Many Requests"),
        (
            httpx.Response(500, text=f"<html>{SECRET}</html>"),
            "ServiceNow error 500: Internal Server Error",
        ),
        (
            httpx.Response(302, headers={"Location": "https://acme.service-now.com/login.do"}),
            "ServiceNow error 302: Found",
        ),
        (
            httpx.Response(200, text="<html>Instance Hibernating</html>"),
            "ServiceNow error: its answer was not the Table API's",
        ),
        (
            httpx.Response(200, json={"result": {"number": "INC0010042"}}),
            "ServiceNow error: its answer was not the Table API's",
        ),
        (
            httpx.Response(200, json=[incident()]),
            "ServiceNow error: its answer was not the Table API's",
        ),
        (httpx.Response(200, json={}), "ServiceNow error: its answer was not the Table API's"),
        (httpx.ConnectError(f"no route for {SECRET}"), "ServiceNow unreachable: ConnectError"),
        (httpx.ReadTimeout("slow"), "ServiceNow unreachable: ReadTimeout"),
    ],
)
@pytest.mark.anyio
async def test_an_error_names_the_status_never_the_credentials(
    response: httpx.Response | Exception, said: str
) -> None:
    async with Client(answering(response)) as c:
        search = await c.call_tool("search_incidents", {"text": "x"}, raise_on_error=False)
        get = await c.call_tool("get_incident", {"number": "INC0010042"}, raise_on_error=False)
    for r in (search, get):
        assert r.is_error and r.content[0].text == said
        assert SECRET not in str(r)


def test_the_contract_names_an_https_instance_and_a_group_id() -> None:
    def build(**over: Any) -> contracts.Contract:
        return contracts.build({"id": "a", "name": "a", "integrations": [{**INTEGRATION, **over}]})

    assert build().secret_refs() == {"servicenow-password"}
    assert build(url=URL + "/").integrations[0].url == URL + "/"
    for url in (
        "http://acme.service-now.com",
        URL + "/api/now",
        "acme.service-now.com",
        "https://user:pass@acme.service-now.com",  # it would show in every link
    ):
        with pytest.raises(Exception, match="String should match pattern"):
            build(url=url)
    for group in ("Network", GROUP.upper(), GROUP[:31], GROUP + "0", f"{GROUP}^ORactive=true"):
        with pytest.raises(Exception, match="String should match pattern"):
            build(group=group)
    with pytest.raises(Exception, match="password_secret"):
        contracts.build(
            {
                "id": "a",
                "name": "a",
                "integrations": [{k: v for k, v in INTEGRATION.items() if k != "password_secret"}],
            }
        )


def test_the_instances_host_names_the_contract() -> None:
    """Shared knowledge may not name it (the Shared knowledge check)."""
    c = contracts.build({"id": "acme", "name": "acme", "integrations": [INTEGRATION]})
    assert contracts.identifiers(c)["acme.service-now.com"] == "servicenow host"


def test_each_contract_uses_its_own_instance_and_credentials(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, str]] = []

    class Recorder:
        def __init__(self, base_url: str, auth: tuple[str, str], **rest: Any) -> None:
            seen.append((base_url, base64.b64encode(":".join(auth).encode()).decode()))
            assert rest == {"timeout": 15, "headers": {"Accept": "application/json"}}

    monkeypatch.setattr(servicenow.httpx, "Client", Recorder)
    for cid, password in (("shop", "shop-password"), ("bank", "bank-password")):
        integration = {**INTEGRATION, "url": f"https://{cid}.service-now.com", "user": cid}
        c = contracts.build(
            {"id": cid, "name": cid, "integrations": [integration]},
            secrets={"servicenow-password": SecretStr(password)},
        )
        agent.build_mcp(c, settings)
    assert seen == [
        ("https://shop.service-now.com", base64.b64encode(b"shop:shop-password").decode()),
        ("https://bank.service-now.com", base64.b64encode(b"bank:bank-password").decode()),
    ]


@pytest.mark.anyio
async def test_two_contracts_on_one_instance_each_see_their_own_group(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Isolation: one instance holds both groups' incidents and ignores the query.
    Each Contract's tools, built as the Hub builds them, show its own group's only."""
    instance = ServiceNow(
        [
            incident("INC0000001", short_description=cell("shop checkout down")),
            incident(
                "INC0000002",
                group=cell(OTHER, "Bank SRE"),
                short_description=cell("bank ledger down"),
            ),
        ]
    )

    real = httpx.Client  # the patch below replaces it for the whole module

    def client(**_: Any) -> httpx.Client:
        return real(base_url=URL, transport=httpx.MockTransport(instance))

    monkeypatch.setattr(servicenow.httpx, "Client", client)
    seen = {}
    for cid, group in (("shop", GROUP), ("bank", OTHER)):
        c = contracts.build(
            {"id": cid, "name": cid, "integrations": [{**INTEGRATION, "group": group}]},
            secrets={"servicenow-password": SecretStr("p")},
        )
        async with Client(agent.build_mcp(c, settings).mcp) as tools:
            found = await tools.call_tool("servicenow_search_incidents", {"text": "down"})
            other = "INC0000002" if cid == "shop" else "INC0000001"
            refused = await tools.call_tool(
                "servicenow_get_incident", {"number": other}, raise_on_error=False
            )
        seen[cid] = [r["summary"] for r in found.structured_content["result"]]
        assert refused.is_error and "down" not in str(refused)
    assert seen == {"shop": ["shop checkout down"], "bank": ["bank ledger down"]}
    assert [r.url.params["sysparm_query"].split("^")[0] for r in instance.requests[::2]] == [
        f"assignment_group={GROUP}",
        f"assignment_group={OTHER}",
    ]


def test_servicenow_has_a_health_check_and_a_guide_only_when_present(settings: Settings) -> None:
    c = contracts.build(
        {"id": "a", "name": "a", "integrations": [INTEGRATION]},
        secrets={"servicenow-password": SecretStr("p")},
    )
    assert agent.probes(c) == [
        ("servicenow", "servicenow_search_incidents", {"text": "health", "days": 1, "limit": 1})
    ]
    toolbox = agent.build_mcp(c, settings)
    assert toolbox.access == {
        "servicenow_search_incidents": "read",
        "servicenow_get_incident": "read",
    }
    assert agent.prompt("triage", toolbox) == agent.SYSTEM_PROMPT + "\n" + servicenow.GUIDE
    jira = {
        "name": "jira",
        "url": "https://acme.atlassian.net",
        "email": "sre@acme.example",
        "token_secret": "jira-token",
        "project": "OPS",
    }
    without = contracts.build(
        {"id": "b", "name": "b", "integrations": [jira]},
        secrets={"jira-token": SecretStr("t")},
    )
    assert "servicenow" not in agent.prompt("triage", agent.build_mcp(without, settings)).lower()


def test_jira_and_servicenow_live_in_one_contract(settings: Settings) -> None:
    jira = {
        "name": "jira",
        "url": "https://acme.atlassian.net",
        "email": "sre@acme.example",
        "token_secret": "jira-token",
        "project": "OPS",
    }
    c = contracts.build(
        {"id": "a", "name": "a", "integrations": [jira, INTEGRATION]},
        secrets={"jira-token": SecretStr("t"), "servicenow-password": SecretStr("p")},
    )
    assert set(agent.build_mcp(c, settings).access) == {
        "jira_search_issues",
        "jira_get_issue",
        "servicenow_search_incidents",
        "servicenow_get_incident",
    }


def test_the_server_runs_on_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """`squero mcp servicenow --contract a`, as for the other builtins."""
    ran: list[tuple[Any, ...]] = []
    monkeypatch.setattr(cli, "run_mcp", lambda *a: ran.append(a) or 0)
    assert cli.main(["mcp", "servicenow", "--contract", "a"]) == 0
    assert ran == [("servicenow", "a", "stdio", "127.0.0.1", 8765)]


@pytest.mark.anyio
async def test_a_saved_servicenow_investigation_replays(settings: Settings) -> None:
    """Save as Scenario names the builtin; replay offers its tools with a placeholder
    Contract that has no secrets, and answers from the recording."""
    from squero.models import Investigation, TriageReport

    c = contracts.build(
        {"id": "a", "name": "a", "integrations": [INTEGRATION]},
        secrets={"servicenow-password": SecretStr("p")},
    )
    t = Investigation(
        id="s", contract_id="a", title="t", started=0, status="done", kind="triage",
        input={"status": "firing", "alerts": []}, report=TriageReport(**REPORT),
    )  # fmt: skip
    row = {"tool": "servicenow_search_incidents", "args": {"text": "payments"}, "result": "[]"}
    scenario = yaml.safe_load(evals.scenario_yaml(t, c, [{**row, "is_error": False}], []))
    assert scenario["integrations"] == ["servicenow"]
    assert URL not in str(scenario) and GROUP not in str(scenario)
    replayed = evals.scenario_contract(
        "", False, [Path("no-runbooks")], scenario["clusters"], scenario["integrations"]
    )
    model = scripted(
        call("servicenow_search_incidents", "s", text="payments"),
        call("TriageReport", "r", **REPORT),
    )
    rows: list[str] = []

    async def sink(tool: str, args: dict[str, Any], result: str, is_error: bool) -> None:
        rows.append(f"{tool} {result} {is_error}")

    toolbox = evals.replay_server(replayed, settings, scenario["tools"])
    async with agent.open_agent(settings, toolbox, model, record=sink) as a:
        await agent.start(a, ALERT, "s")
    assert rows == ["servicenow_search_incidents [] False"]
