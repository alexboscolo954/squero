# ruff: noqa: F811 - sock is test_slack's fixture, imported for these tests
"""Create ServiceNow incident (slice 19.5b): the Operator files a finished Report into
the Contract's own ServiceNow assignment group, once, from Slack or the console.
ServiceNow, Jira Cloud and Slack's Web API are fakes behind one httpx transport, as
the Hub shares one client."""

import base64
import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from squero.models import Evidence, TriageReport
from squero.servers import servicenow
from squero.store import Store
from tests.conftest import REPORT, call, make_contract, make_fake_cluster
from tests.test_api import ALERT, HOOK, URL, client
from tests.test_console import SAME, VIEW
from tests.test_incidents import past, read, store  # noqa: F401 - its database fixture
from tests.test_jira_issue import ACME_SECRET as JIRA_SECRET
from tests.test_jira_issue import JIRA, FakeJira, paused_with_report
from tests.test_restart import wait_for
from tests.test_slack import (  # noqa: F401 - _slack: the bot token and a database
    ACME,
    ACME_CLIENT,
    BANK,
    TOKEN,
    FakeSlack,
    FakeSocket,
    _slack,
    buttons,
    click,
    ephemerals,
    replies,
    sock,
    until,
)

GROUP = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
BANK_GROUP = "f0e1d2c3b4a5f6e7d8c9b0a1f2e3d4c5"
SYS_ID = "c1d2e3f4a5b6c7d8e9f0a1b2c3d4e5f6"
INSTANCE = "https://acme.service-now.com"
LINK = f"{INSTANCE}/incident.do?sys_id={SYS_ID}"
SERVICENOW = {
    "name": "servicenow",
    "url": INSTANCE,
    "user": "squero.writer",
    "password_secret": "servicenow-password",
    "group": GROUP,
}
ACME_SECRET = {"servicenow-password": "acme-servicenow-password"}
BASIC = "Basic " + base64.b64encode(b"squero.writer:acme-servicenow-password").decode()
REPORT_STEP = call("TriageReport", "r", **REPORT)
FILED = "Already filed (or being filed), or nothing to file."
UNKNOWN = (
    "ServiceNow didn't answer ({}): it may have created the incident. "
    "Check the assignment group's incidents before filing it by hand."
)


def created(number: str = "INC0010001", sys_id: str = SYS_ID, group: Any = GROUP) -> httpx.Response:
    """201, as the Table API answers a create: a reference is {link, value}."""
    ref = {"link": f"{INSTANCE}/api/now/table/sys_user_group/{group}", "value": group}
    made = {"number": number, "sys_id": sys_id, "assignment_group": ref if group else ""}
    return httpx.Response(201, json={"result": made})


class FakeServiceNow:
    """Records creates; `answers` overrides the next ones (a Response, or an
    exception to raise, as a timeout would)."""

    def __init__(self) -> None:
        self.creates: list[httpx.Request] = []
        self.answers: list[Any] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.creates.append(request)
        if self.answers:
            answer = self.answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        group = json.loads(request.content)["assignment_group"]
        return created(f"INC00100{len(self.creates):02d}", group=group)


def transport(
    slack: FakeSlack, now: FakeServiceNow, jira: FakeJira | None = None
) -> httpx.MockTransport:
    def route(request: httpx.Request) -> httpx.Response:
        if request.url.host.endswith("service-now.com"):
            return now(request)
        if jira is not None and request.url.host.endswith("atlassian.net"):
            return jira(request)
        return slack(request)

    return httpx.MockTransport(route)


def done(settings: Any, fake_cluster: Any, slack: FakeSlack, now: FakeServiceNow, **kw: Any) -> Any:
    settings.console_operators = {"alex@example.com"}
    return client(
        settings,
        fake_cluster,
        REPORT_STEP,
        slack=transport(slack, now),
        acme_channel=ACME,
        acme={"integrations": [{"name": "kubernetes"}, SERVICENOW], "secrets": ACME_SECRET},
        **kw,
    )


def test_the_operator_files_the_report_once_from_slack_beside_jira(
    settings: Any, fake_cluster: Any, sock: FakeSocket
) -> None:
    """One Contract with Jira and ServiceNow: each button files only its own, and
    the other's button and claim remain."""
    slack, fake, jira = FakeSlack(sock.url), FakeServiceNow(), FakeJira()
    settings.console_operators = {"alex@example.com"}
    acme = {
        "integrations": [{"name": "kubernetes"}, JIRA, SERVICENOW],
        "secrets": {**JIRA_SECRET, **ACME_SECRET},
    }
    with client(
        settings,
        fake_cluster,
        REPORT_STEP,
        slack=transport(slack, fake, jira),
        acme_channel=ACME,
        acme=acme,
        acme_client=ACME_CLIENT,
    ) as c:
        tid = c.post(URL, json=ALERT, headers=HOOK).json()["investigation"]
        wait_for(c, tid, "done")
        until(lambda: "create_servicenow" in buttons(slack))
        all_three = buttons(slack)
        assert set(all_three) == {"send_to_client", "create_jira", "create_servicenow"}
        confirm = all_three["create_servicenow"]["confirm"]
        assert confirm["text"]["text"] == (
            "The full Report, with Evidence, goes to the Contract's ServiceNow assignment group."
        )
        assert len(confirm["text"]["text"]) <= 300 and confirm["confirm"]["text"] == "Create"
        sock.send(click("send_to_client", all_three["send_to_client"]["value"]))
        until(lambda: any("Sent to the client" in r for r in replies(slack)))
        assert set(buttons(slack)) == {"create_jira", "create_servicenow"}  # sending leaves both
        value = all_three["create_servicenow"]["value"]
        sock.send(click("create_servicenow", value))
        sock.send(click("create_servicenow", value))  # a double click
        until(lambda: any(":ticket: Filed as INC0010001" in r for r in replies(slack)))
        until(lambda: len(ephemerals(slack)) == 1)
        until(lambda: set(buttons(slack)) == {"create_jira"})  # Jira's is still offered
        parents = [p for m, p, _ in slack.calls if m == "chat.update"]
        assert parents[-1]["text"].endswith(" · :ticket: INC0010001")  # shown at once
        assert jira.creates == []
        page = c.get(f"/api/v1/investigations/{tid}", headers=VIEW).json()
        assert page["jira"]["issue"] is None and page["jira"]["since"] is None
        sock.send(click("create_jira", all_three["create_jira"]["value"]))
        until(lambda: any(":ticket: Filed as OPS-1" in r for r in replies(slack)))
        until(lambda: buttons(slack) == {})
        updates = [p for m, p, _ in slack.calls if m == "chat.update"]
        assert updates[-1]["text"].endswith(" · :ticket: OPS-1 · :ticket: INC0010001")
    (create,) = fake.creates
    assert len(jira.creates) == 1
    assert create.method == "POST" and create.url.path == "/api/now/table/incident"
    assert dict(create.url.params) == {"sysparm_fields": "number,sys_id,assignment_group"}
    assert create.headers["authorization"] == BASIC and TOKEN not in str(create.headers)
    assert create.headers["accept"] == "application/json"
    assert json.loads(create.content) == {
        "short_description": REPORT["summary"],
        "description": servicenow.report_text(TriageReport(**REPORT), [], tid),
        "assignment_group": GROUP,
    }
    assert f":ticket: Filed as INC0010001 by <@U0OPERATOR1>: {LINK}" in replies(slack)
    assert ephemerals(slack)[0][1] == FILED
    assert page["servicenow"] == {
        "incident": "INC0010001",
        "url": LINK,
        "by": "slack:U0OPERATOR1",
        "since": page["servicenow"]["since"],
    }


def test_a_refusal_can_be_retried_and_no_answer_cannot(
    settings: Any, fake_cluster: Any, sock: FakeSocket
) -> None:
    """A 403 created nothing: free the claim, and say why with ServiceNow's message
    and detail. A timeout may have created it: keep the claim, so a second click
    never files a duplicate."""
    slack, fake = FakeSlack(sock.url), FakeServiceNow()
    refusal = {
        "error": {"message": "Operation Failed", "detail": "Data Policy Exception:\n Caller"},
        "status": "failure",
    }
    fake.answers = [httpx.Response(403, json=refusal), httpx.ReadTimeout("slow")]
    with done(settings, fake_cluster, slack, fake) as c:
        tid = c.post(URL, json=ALERT, headers=HOOK).json()["investigation"]
        wait_for(c, tid, "done")
        until(lambda: "create_servicenow" in buttons(slack))
        value = buttons(slack)["create_servicenow"]["value"]
        sock.send(click("create_servicenow", value))
        until(lambda: len(ephemerals(slack)) == 1)
        assert "create_servicenow" in buttons(slack)  # refused: offered again
        sock.send(click("create_servicenow", value))  # retried: ServiceNow times out
        until(lambda: len(ephemerals(slack)) == 2)
        sock.send(click("create_servicenow", value))  # outcome unknown: no new create
        until(lambda: len(ephemerals(slack)) == 3)
        page = c.get(f"/api/v1/investigations/{tid}", headers=VIEW).json()
    assert [e[1] for e in ephemerals(slack)] == [
        "ServiceNow refused the incident (403 Operation Failed Data Policy Exception: Caller): "
        "nothing was created.",
        UNKNOWN.format("ReadTimeout"),
        FILED,
    ]
    assert len(fake.creates) == 2
    assert page["servicenow"]["incident"] is None and page["servicenow"]["url"] is None
    assert page["servicenow"]["since"] is not None  # being filed
    assert page["servicenow"]["by"] == "slack:U0OPERATOR1"


def test_the_console_files_it_once_and_the_slack_thread_shows_it(
    settings: Any, fake_cluster: Any, sock: FakeSocket
) -> None:
    slack, fake = FakeSlack(sock.url), FakeServiceNow()
    with done(settings, fake_cluster, slack, fake) as c:
        tid = c.post(URL, json=ALERT, headers=HOOK).json()["investigation"]
        wait_for(c, tid, "done")
        until(lambda: "create_servicenow" in buttons(slack))
        path = f"/api/v1/investigations/{tid}/servicenow"
        before = c.get(f"/api/v1/investigations/{tid}", headers=VIEW).json()
        paused_with_report(settings.database_url.get_secret_value())
        unfinished = c.get("/api/v1/investigations/p1", headers=VIEW).json()
        not_done = c.post("/api/v1/investigations/p1/servicenow", headers=SAME)
        assert c.post(path, headers=VIEW).status_code == 403  # no same-origin header
        cross = c.post(path, headers={**VIEW, "Sec-Fetch-Site": "cross-site"}).status_code
        r = c.post(path, headers=SAME)
        again = c.post(path, headers=SAME)
        until(lambda: any("Filed as INC0010001 by alex@example.com" in x for x in replies(slack)))
        assert "create_servicenow" not in buttons(slack)
    assert before["servicenow"] == {"incident": None, "url": None, "by": None, "since": None}
    assert before["jira"] is None  # no Jira in this Contract
    assert unfinished["servicenow"] is None  # a Report still waiting on a decision files nothing
    assert not_done.status_code == 409 and cross == 403
    filed = r.json()["servicenow"]
    assert r.status_code == 200 and filed["incident"] == "INC0010001" and filed["url"] == LINK
    assert filed["by"] == "console:alex@example.com"
    assert again.status_code == 409 and again.json()["detail"] == FILED
    assert len(fake.creates) == 1


def test_each_contract_files_into_its_own_group_with_its_own_password(
    settings: Any, fake_cluster: Any, sock: FakeSocket
) -> None:
    """Isolation: bank's button clicked in acme's channel files nothing; in its own
    channel it goes to bank's instance, with bank's user, password and group."""
    slack, fake = FakeSlack(sock.url), FakeServiceNow()
    bank_now = {
        **SERVICENOW,
        "url": "https://bank.service-now.com",
        "user": "bank.writer",
        "group": BANK_GROUP,
    }
    bank = make_contract(
        contract_id="bank",
        webhook_token="bank-token",
        slack_channel=BANK,
        integrations=[{"name": "kubernetes"}, bank_now],
        secrets={"servicenow-password": "bank-servicenow-password"},
    )
    more = {"bank": (bank, make_fake_cluster())}
    with client(
        settings,
        fake_cluster,
        REPORT_STEP,
        REPORT_STEP,
        slack=transport(slack, fake),
        acme_channel=ACME,
        acme={"integrations": [{"name": "kubernetes"}, SERVICENOW], "secrets": ACME_SECRET},
        more=more,
    ) as c:
        bank_id = c.post(
            "/contracts/bank/alertmanager",
            json=ALERT,
            headers={"Authorization": "Bearer bank-token"},
        ).json()["investigation"]
        wait_for(c, bank_id, "done")
        until(lambda: "create_servicenow" in buttons(slack))
        value = buttons(slack)["create_servicenow"]["value"]
        sock.send(click("create_servicenow", value, channel=ACME))  # bank's, clicked in acme's
        until(lambda: len(ephemerals(slack)) == 1)
        assert fake.creates == []
        sock.send(click("create_servicenow", value, channel=BANK))
        until(lambda: any("Filed as INC0010001" in r for r in replies(slack)))
    (create,) = fake.creates
    assert create.url.host == "bank.service-now.com"
    assert json.loads(create.content)["assignment_group"] == BANK_GROUP
    bank_basic = base64.b64encode(b"bank.writer:bank-servicenow-password").decode()
    assert create.headers["authorization"] == f"Basic {bank_basic}"
    assert "https://bank.service-now.com/incident.do?sys_id=" in " ".join(replies(slack))


def stored(database_url: str, **fields: Any) -> None:
    """An Investigation put straight into the database, as an earlier Run left it."""
    import asyncio

    from squero.models import Investigation
    from squero.store import connect

    async def go() -> None:
        async with connect(database_url) as pool:
            t = Investigation(
                **{
                    "contract_id": "acme",
                    "title": "t",
                    "started": 1,
                    "status": "done",
                    "report": TriageReport.model_validate(REPORT),
                    **fields,
                }
            )
            await Store(pool).insert(t)
            await Store(pool).save(t)

    asyncio.run(go())


def test_the_incident_lists_what_the_hub_ran(
    settings: Any, fake_cluster: Any, sock: FakeSocket
) -> None:
    """Executed actions come from the Hub's record, never from the model."""
    slack, fake = FakeSlack(sock.url), FakeServiceNow()
    with done(settings, fake_cluster, slack, fake) as c:
        ran = ["kubernetes_evict_pod shop/reports-1: ok"]
        stored(settings.database_url.get_secret_value(), id="ran1", executed_actions=ran)
        r = c.post("/api/v1/investigations/ran1/servicenow", headers=SAME)
    assert r.status_code == 200
    (create,) = fake.creates
    text = json.loads(create.content)["description"]
    assert "\n\nExecuted actions\n- kubernetes_evict_pod shop/reports-1: ok\n\nFiled by" in text


def test_a_click_for_a_contract_that_is_gone_files_nothing(
    settings: Any, fake_cluster: Any, sock: FakeSocket
) -> None:
    """Its thread is still in Slack after the Contract left the directory."""
    slack, fake = FakeSlack(sock.url), FakeServiceNow()
    with done(settings, fake_cluster, slack, fake):
        url = settings.database_url.get_secret_value()
        stored(url, id="gone1", contract_id="gone", slack_channel=ACME, slack_ts="1.1")
        sock.send(click("create_servicenow", json.dumps({"id": "gone1"})))
        until(lambda: len(ephemerals(slack)) == 1)
    assert ephemerals(slack)[0][1] == FILED and fake.creates == []


def test_the_button_is_only_on_a_finished_report_with_no_claim() -> None:
    from squero.models import Investigation
    from squero.slack import parent_blocks

    def offered(servicenow: bool = True, **fields: Any) -> bool:
        report = TriageReport.model_validate(REPORT)
        t = Investigation(
            **{"id": "t1", "contract_id": "acme", "title": "t", "started": 1, "status": "done"}
            | {"report": report}
            | fields
        )
        blocks = parent_blocks(t, True, servicenow=servicenow)
        ids = [e["action_id"] for b in blocks if b["type"] == "actions" for e in b["elements"]]
        return "create_servicenow" in ids

    assert offered()
    assert not offered(servicenow=False)  # the Contract has no ServiceNow
    assert not offered(status="awaiting_approval", pending=[], deadline=2.0)
    assert not offered(report=None)
    assert not offered(servicenow_at=5.0)  # being filed, or unknown: no second incident
    assert not offered(servicenow_at=5.0, servicenow_incident="INC0010001")
    assert offered(jira_at=5.0, jira_issue="OPS-1")  # Jira's claim is its own


PAGE = httpx.Response(200, text="<html>Sign in</html>")


@pytest.mark.parametrize(
    ("answer", "retried", "said"),
    [
        # nothing can have been created: the claim is freed
        (httpx.ConnectError("no route"), True, "could not connect (ConnectError)"),
        (httpx.ConnectTimeout("slow"), True, "could not connect (ConnectTimeout)"),
        (httpx.Response(400, json={"error": {"message": "Bad"}}), True, "400 Bad"),
        (  # ServiceNow's words are a third party's: no markup of theirs reaches Slack
            httpx.Response(400, json={"error": {"message": "<!channel> & <@U1>"}}),
            True,
            "400 &lt;!channel&gt; &amp; &lt;@U1&gt;",
        ),
        (httpx.Response(401, json={"error": "nope"}), True, "401 Unauthorized"),
        (httpx.Response(404, text="<html>gone</html>"), True, "404 Not Found"),
        (httpx.Response(302, headers={"Location": INSTANCE + "/login.do"}), True, "302 Found"),
        (httpx.Response(499, json=["x"]), True, "499 "),
        # it may have been created: the claim stays
        (httpx.Response(500, text="oops"), False, "HTTP 500"),
        (httpx.Response(504, text="Gateway Timeout"), False, "HTTP 504"),
        (httpx.WriteTimeout("slow"), False, "WriteTimeout"),
        (PAGE, False, "HTTP 200 without an incident's number and id"),
        (
            httpx.Response(200, json={"result": {"number": "INC0010001", "sys_id": SYS_ID}}),
            False,
            "HTTP 200 without an incident's number and id",
        ),
        (
            httpx.Response(201, text="created"),
            False,
            "HTTP 201 without an incident's number and id",
        ),
        (
            httpx.Response(201, json={"result": [{"number": "INC0010001", "sys_id": SYS_ID}]}),
            False,
            "HTTP 201 without an incident's number and id",
        ),
        (
            httpx.Response(201, json=["created"]),
            False,
            "HTTP 201 without an incident's number and id",
        ),
        (created("<!channel>"), False, "HTTP 201 without an incident's number and id"),
        (created("INC0010001 <!here>"), False, "HTTP 201 without an incident's number and id"),
        (created(sys_id="x"), False, "HTTP 201 without an incident's number and id"),
        (
            created(sys_id=f"{SYS_ID}&x=<!channel>"),
            False,
            "HTTP 201 without an incident's number and id",
        ),
    ],
)
def test_a_retry_is_allowed_only_when_nothing_can_have_been_created(
    settings: Any, fake_cluster: Any, sock: FakeSocket, answer: Any, retried: bool, said: str
) -> None:
    slack, fake = FakeSlack(sock.url), FakeServiceNow()
    fake.answers = [answer]
    with done(settings, fake_cluster, slack, fake) as c:
        tid = c.post(URL, json=ALERT, headers=HOOK).json()["investigation"]
        wait_for(c, tid, "done")
        path = f"/api/v1/investigations/{tid}/servicenow"
        first = c.post(path, headers=SAME)
        second = c.post(path, headers=SAME)
        t = c.get(f"/investigations/{tid}", headers={"Authorization": "Bearer approver-token"})
    assert first.status_code == 409
    if retried:
        expected = f"ServiceNow refused the incident ({said}): nothing was created."
    else:
        expected = UNKNOWN.format(said)
    assert first.json()["detail"] == expected
    assert len(fake.creates) == (2 if retried else 1)
    assert second.status_code == (200 if retried else 409)
    assert t.json()["servicenow_incident"] == ("INC0010002" if retried else None)
    assert t.json()["servicenow_id"] == (SYS_ID if retried else None)
    assert not any("<!" in r for r in replies(slack))


@pytest.mark.parametrize(
    "group",
    [BANK_GROUP, "", {"link": "x", "value": ""}, {"display_value": GROUP}],
)
def test_an_incident_outside_the_group_is_kept_and_the_operator_is_told(
    settings: Any, fake_cluster: Any, sock: FakeSocket, group: Any
) -> None:
    """ServiceNow may create the incident and drop a field the user may not set. It
    exists, so the claim stays (no duplicate), and the thread and the answer say so."""
    slack, fake = FakeSlack(sock.url), FakeServiceNow()
    made = {"number": "INC0010001", "sys_id": SYS_ID, "assignment_group": group}
    fake.answers = [httpx.Response(201, json={"result": made})]
    with done(settings, fake_cluster, slack, fake) as c:
        tid = c.post(URL, json=ALERT, headers=HOOK).json()["investigation"]
        wait_for(c, tid, "done")
        until(lambda: "create_servicenow" in buttons(slack))
        sock.send(click("create_servicenow", buttons(slack)["create_servicenow"]["value"]))
        until(lambda: any("Filed as INC0010001" in r for r in replies(slack)))
        again = c.post(f"/api/v1/investigations/{tid}/servicenow", headers=SAME)
        page = c.get(f"/api/v1/investigations/{tid}", headers=VIEW).json()
    (line,) = [r for r in replies(slack) if "Filed as" in r]
    assert line == (
        f":ticket: Filed as INC0010001 by <@U0OPERATOR1>: {LINK} "
        "ServiceNow did not put it in the assignment group: move it there."
    )
    assert again.status_code == 409 and len(fake.creates) == 1
    assert page["servicenow"]["incident"] == "INC0010001"


def test_an_incident_in_the_group_says_nothing_more(
    settings: Any, fake_cluster: Any, sock: FakeSocket
) -> None:
    slack, fake = FakeSlack(sock.url), FakeServiceNow()
    with done(settings, fake_cluster, slack, fake) as c:
        tid = c.post(URL, json=ALERT, headers=HOOK).json()["investigation"]
        wait_for(c, tid, "done")
        r = c.post(f"/api/v1/investigations/{tid}/servicenow", headers=SAME)
        until(lambda: any("Filed as INC0010001" in x for x in replies(slack)))
    assert r.status_code == 200
    (line,) = [x for x in replies(slack) if "Filed as" in x]
    assert line == f":ticket: Filed as INC0010001 by alex@example.com: {LINK}"


def test_the_description_is_plain_text_redacted_before_it_is_cut() -> None:
    report = TriageReport(
        **{
            **REPORT,
            "summary": "checkout is down\nsecond line",
            "affected": [],
            "evidence": [
                Evidence(source="logs", finding="see token=abcdefghij1234567890 in the log"),
                Evidence(source="grafana_generate_deeplink", finding="explore", link="https://g/x"),
            ],
        }
    )
    text = servicenow.report_text(report, ["kubernetes_evict_pod {}: ok"], "t1")
    assert text == (
        "checkout is down\nsecond line\n"
        "\n"
        "Root cause (high confidence, resource_limits): "
        "Memory limit 64Mi is below the JVM heap requirement.\n"
        "Severity: high. Affected: -\n"
        "\n"
        "Evidence\n"
        "- logs: see token=[REDACTED] in the log\n"
        "- grafana_generate_deeplink: explore (https://g/x)\n"
        "\n"
        "Recommendations\n"
        "1. Raise the memory limit to 512Mi\n"
        "2. Set -XX:MaxRAMPercentage=75\n"
        "\n"
        "Executed actions\n"
        "- kubernetes_evict_pod {}: ok\n"
        "\n"
        "Filed by squero from Investigation t1."
    )
    assert not any(mark in text for mark in ("**", "##", "`"))


def test_a_long_report_is_cut_and_keeps_its_last_line_and_no_half_secret() -> None:
    """The secret straddles the cut: redacting after it would leave half a token that
    matches no pattern."""
    foot = "\n\nFiled by squero from Investigation t-long."
    head = "Root cause (high confidence, resource_limits): "
    secret = "token=abcdefghij1234567890abcdefghij"
    lead = 4000 - len(foot) - len(servicenow.CUT) - len("s\n\n") - len(head) - len("x token=abc")
    report = TriageReport(
        **{**REPORT, "summary": "s", "probable_root_cause": "y" * lead + " " + secret + "z" * 900}
    )
    text = servicenow.report_text(report, [], "t-long")
    assert len(text) == 4000 and text.endswith(servicenow.CUT + foot)
    # cut inside the marker itself: where the raw secret's first half would have stayed
    assert "abcdefghij" not in text and "1234567890" not in text
    assert text.endswith(" token=[RED" + servicenow.CUT + foot)
    short = servicenow.report_text(TriageReport(**REPORT), [], "t1")
    assert len(short) < 4000 and servicenow.CUT not in short


def test_an_exact_fit_is_not_cut() -> None:
    foot = "\n\nFiled by squero from Investigation t1."
    fixed = len(servicenow.report_text(TriageReport(**{**REPORT, "summary": ""}), [], "t1"))
    fits = TriageReport(**{**REPORT, "summary": "s" * (4000 - fixed)})
    over = TriageReport(**{**REPORT, "summary": "s" * (4000 - fixed + 1)})
    assert len(servicenow.report_text(fits, [], "t1")) == 4000
    assert servicenow.CUT not in servicenow.report_text(fits, [], "t1")
    cut = servicenow.report_text(over, [], "t1")
    assert len(cut) == 4000 and cut.endswith(servicenow.CUT + foot)


def test_an_empty_section_is_left_out() -> None:
    report = TriageReport(**{**REPORT, "recommended_actions": []})
    text = servicenow.report_text(report, [], "t1")
    assert "Recommendations" not in text and "Executed actions" not in text
    assert "Evidence\n- kubernetes_pod_logs: " in text


def test_the_short_description_is_one_redacted_line_of_160() -> None:
    report = TriageReport(**{**REPORT, "summary": "down\n  now  token=abcdefghij1234567890"})
    assert servicenow.incident_summary(report) == "down now token=[REDACTED]"
    exact = TriageReport(**{**REPORT, "summary": "x" * 160})
    assert servicenow.incident_summary(exact) == "x" * 160
    long = servicenow.incident_summary(TriageReport(**{**REPORT, "summary": "x" * 600}))
    assert long == "x" * 159 + "…" and len(long) == 160


@pytest.mark.parametrize(
    ("body", "said"),
    [
        ({"error": {"message": "Operation Failed", "detail": "Caller is\n mandatory"}}, None),
        ({"error": {"message": "m" * 400, "detail": "d"}}, "m" * 300),
        ({"error": {"message": None, "detail": "only detail"}}, "only detail"),
        ({"error": {"message": {"a": 1}, "detail": ["b"]}}, "Forbidden"),
        ({"error": "nope"}, "Forbidden"),
        (["nope"], "Forbidden"),
        ("nope", "Forbidden"),
    ],
)
def test_a_refusal_is_servicenows_message_and_detail_on_one_line(
    body: Any, said: str | None
) -> None:
    resp = httpx.Response(403, json=body)
    assert servicenow._why(resp) == (said or "Operation Failed Caller is mandatory")
    assert servicenow._why(httpx.Response(403, text="<html>no</html>")) == "Forbidden"


@pytest.mark.anyio
async def test_a_redirect_is_never_followed_whatever_the_shared_client_does() -> None:
    """Followed, the create would go somewhere else with the Contract's password."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(302, headers={"Location": "https://elsewhere.example/x"})
        return created()

    follows = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    async with follows as http:
        with pytest.raises(servicenow.Refused, match="302 Found"):
            await servicenow.create_incident(
                http, INSTANCE + "/", "u", SecretStr("p"), GROUP, "short", "long"
            )
    (only,) = seen
    assert str(only.url) == (
        f"{INSTANCE}/api/now/table/incident?sysparm_fields=number%2Csys_id%2Cassignment_group"
    )


def test_creating_is_never_an_agent_tool() -> None:
    assert servicenow.ACCESS == {"search_incidents": "read", "get_incident": "read"}


@pytest.mark.anyio
async def test_a_past_report_names_its_incident_only_when_one_was_filed(store: Store) -> None:
    """`incidents_read`: nothing changes for a Report with no ServiceNow incident."""
    await store.insert(past("plain", "reports oom"))
    filed = past("filed", "reports oom").model_copy(update={"servicenow_incident": "INC0010007"})
    await store.insert(filed)
    assert "servicenow_incident" not in await read(store, "plain")
    assert (await read(store, "filed"))["servicenow_incident"] == "INC0010007"


@pytest.mark.anyio
async def test_the_claim_is_one_per_finished_report_and_freed_only_while_unfiled(
    store: Store,
) -> None:
    await store.insert(past("a", "reports oom"))
    await store.insert(past("b", "reports oom"))
    running = past("r", "reports oom").model_copy(update={"status": "running"})
    await store.insert(running)
    assert await store.claim_servicenow("r", "slack:U1") is None  # not finished
    at = await store.claim_servicenow("a", "slack:U1")
    assert at is not None and await store.claim_servicenow("a", "slack:U2") is None
    await store.release_servicenow("a", at + 1)  # another claim's time: nothing freed
    assert await store.claim_servicenow("a", "slack:U2") is None
    await store.release_servicenow("a", at)
    again = await store.claim_servicenow("a", "slack:U2")
    assert again is not None
    await store.set_servicenow_incident("a", again + 1, "INC0000009", SYS_ID)  # not this claim
    assert (await store.get("a")).servicenow_incident is None  # type: ignore[union-attr]
    await store.set_servicenow_incident("a", again, "INC0010001", SYS_ID)
    await store.release_servicenow("a", again)  # filed: never freed
    got, other = await store.get("a"), await store.get("b")
    assert got is not None and other is not None
    assert (got.servicenow_incident, got.servicenow_id) == ("INC0010001", SYS_ID)
    assert (got.servicenow_by, got.servicenow_at) == ("slack:U2", again)
    assert other.servicenow_at is None and other.servicenow_incident is None
    assert got.jira_at is None  # Jira's claim is its own
    assert await store.claim_jira("a", "slack:U1") is not None
