# ruff: noqa: F811 - sock is test_slack's fixture, imported for these tests
"""PagerDuty in the Hub (slice 19.2): a signed delivery on the Contract's own URL becomes
an Alert group, behind the same dedup and resolve rules as Alertmanager's; an incident
that is an Alert group the Contract sent itself joins its Triage; the Client report goes
on the incident as a note only when the Operator sends it; a signed delivery is never
refused; and one Contract's secret, key and incidents never serve another's."""

import json
from typing import Any

import httpx
import psycopg
import pytest
from pydantic import SecretStr

from squero import pagerduty
from squero.config import Settings
from tests.conftest import REPORT, call, make_contract, make_fake_cluster
from tests.test_api import (  # noqa: F401 - _database: every test here needs Postgres
    ALERT,
    AUTH,
    BANK_HOOK,
    BANK_URL,
    EVICT_THEN_REPORT,
    HOOK,
    URL,
    _database,
    client,
    offline,
    wait_for,
)
from tests.test_console import OPERATOR as LOGIN
from tests.test_console import VIEW
from tests.test_grafana_alerts import FIRING, GRAFANA
from tests.test_pagerduty import ALERTS, FROM, INCIDENT, KEY, PD, SECRETS, SIGNING, event, raw, sign
from tests.test_slack import (  # noqa: F401 - sock: the Operator's clicks
    ACME,
    ACME_CLIENT,
    TOKEN,
    FakeSlack,
    FakeSocket,
    buttons,
    click,
    ephemerals,
    replies,
    sent_by,
    sock,
    to,
    until,
)

HOOKED = "/contracts/acme/pagerduty"
NOTED = {"pagerduty": PD, "secrets": SECRETS}
READ = {"pagerduty": {k: v for k, v in PD.items() if k != "from_secret"}, "secrets": SECRETS}
SIGNED = {"pagerduty": {"webhook_secret": "pd-signing"}, "secrets": SECRETS}
ONE = call("TriageReport", "r1", **REPORT)
TWO = call("TriageReport", "r2", **REPORT)
LINK = f"https://acme.pagerduty.com/incidents/{INCIDENT}"


class FakePagerDuty:
    """PagerDuty's REST API: keeps each request; `answers` overrides the next replies
    (an exception is raised); `alerts` is what an incident lists."""

    def __init__(self, alerts: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[httpx.Request] = []
        self.answers: list[Any] = []
        self.alerts = ALERTS if alerts is None else alerts

    def __call__(self, r: httpx.Request) -> httpx.Response:
        self.calls.append(r)
        if self.answers:
            reply = self.answers.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply  # type: ignore[no-any-return]
        if r.url.path.endswith("/alerts"):
            return httpx.Response(200, json={"alerts": self.alerts, "more": False})
        return httpx.Response(200, json={"note": {"id": "PWL7QXS"}, "abilities": []})

    def paths(self) -> list[str]:
        return [f"{r.method} {r.url.path}" for r in self.calls]


def both(pd: FakePagerDuty, slack: Any = None) -> httpx.MockTransport:
    """The Hub's one outbound client: PagerDuty's API, and Slack's (offline without one)."""
    return httpx.MockTransport(
        lambda r: pd(r) if r.url.host == "api.pagerduty.com" else (slack or offline)(r)
    )


def deliver(c: Any, payload: Any, cid: str = "acme", secret: str = SIGNING, **more: str) -> Any:
    body = payload if isinstance(payload, bytes) else raw(payload)
    headers = {"X-PagerDuty-Signature": sign(body, secret), "Content-Type": "application/json"}
    return c.post(f"/contracts/{cid}/pagerduty", content=body, headers=headers | more)


def listed(c: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = c.get("/investigations", headers=AUTH).json()
    return found


def hooked(cid: str = "bank", signing: str = "whsec-bank-signing", **more: Any) -> Any:
    """A second Contract with its own signing secret, key and fake cluster."""
    secrets = {**SECRETS, "pd-signing": signing, "pd-key": "bank-rest-key"}
    c = make_contract(
        contract_id=cid, webhook_token="bank-token", pagerduty=PD, secrets=secrets, **more
    )
    return {cid: (c, make_fake_cluster())}


def test_only_a_delivery_signed_with_the_contracts_own_secret_is_taken(
    settings: Settings, fake_cluster: Any
) -> None:
    pd = FakePagerDuty()
    with client(settings, fake_cluster, ONE, slack=both(pd), acme=READ, more=hooked()) as c:
        body = raw(event())
        plain = {"Content-Type": "application/json"}
        assert c.post(HOOKED, content=body, headers=plain).status_code == 401  # no signature
        # the Contract's webhook token and the approver token are not one
        assert c.post(HOOKED, content=body, headers=plain | HOOK).status_code == 401
        assert c.post(HOOKED, content=body, headers=plain | AUTH).status_code == 401
        assert deliver(c, body, secret="guessed").status_code == 401
        # another Contract's secret on this one's URL, and this one's on the other's
        assert deliver(c, body, secret="whsec-bank-signing").status_code == 401
        assert deliver(c, body, cid="bank").status_code == 401
        assert deliver(c, body, cid="nope").status_code == 401  # as for an unknown Contract
        tampered = {"X-PagerDuty-Signature": sign(body), "Content-Type": "application/json"}
        other = raw(event(incident="PGR0VU3"))
        assert c.post(HOOKED, content=other, headers=tampered).status_code == 401
        assert listed(c) == [] and pd.calls == []  # nothing read or started for any of them
        taken = deliver(c, body)
        assert (taken.status_code, taken.json()["status"]) == (202, "accepted")
        wait_for(c, taken.json()["investigation"], "done")


def test_a_contract_without_pagerduty_takes_no_delivery(
    settings: Settings, fake_cluster: Any
) -> None:
    with client(settings, fake_cluster) as c:
        body = raw(event())
        assert deliver(c, body).status_code == 401
        assert deliver(c, body, secret="").status_code == 401  # nor one signed with no key
        assert listed(c) == []


@pytest.mark.parametrize(
    "payload",
    [
        event("incident.acknowledged"),
        event("incident.annotated"),
        event("pagey.ping"),
        event(incident="PGR0VU2/notes"),
        {"event": {"event_type": "incident.triggered"}},  # fields missing
        {"something": "else"},
        b"not json at all",
        b"",
    ],
)
def test_a_signed_delivery_is_never_refused_whatever_it_holds(
    settings: Settings, fake_cluster: Any, payload: Any
) -> None:
    """PagerDuty drops a 4xx for good and disables the subscription after three."""
    pd = FakePagerDuty()
    with client(settings, fake_cluster, slack=both(pd), acme=READ) as c:
        got = deliver(c, payload)
        assert got.status_code == 202
        assert got.json() == {"status": "ignored", "reason": "not an incident starting or ending"}
        assert listed(c) == [] and pd.calls == []


def test_an_incident_is_triaged_once_with_its_alerts_details(
    settings: Settings, fake_cluster: Any
) -> None:
    pd = FakePagerDuty()
    with client(settings, fake_cluster, ONE, slack=both(pd), acme=READ) as c:
        first = deliver(c, event()).json()
        assert first["status"] == "accepted"
        t = wait_for(c, first["investigation"], "done")
        assert (t["title"], t["group_key"]) == ("Checkout is failing", "pagerduty:PGR0VU2")
        (alert,) = t["input"]["alerts"]
        assert alert["annotations"] == {
            "summary": "checkout 5xx above 5%",
            "details": '{"namespace": "shop", "rate": "0.07"}',
            "incident": LINK,
        }
        assert alert["labels"]["severity"] == "critical"
        (read,) = pd.calls  # one call per incident, with this Contract's key
        assert (read.method, read.url.path) == ("GET", "/incidents/PGR0VU2/alerts")
        assert read.headers["authorization"] == f"Token token={KEY}"
        again = {"status": "duplicate", "investigation": first["investigation"]}
        assert deliver(c, event()).json() == again  # PagerDuty's retry
        assert deliver(c, event("incident.reopened")).json() == again
        assert len(pd.calls) == 1 and len(listed(c)) == 1  # a repeat costs no call


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(401, json={"error": {"message": "bad key", "code": 2006}}),
        httpx.Response(500),
        httpx.ReadTimeout("slow"),
        httpx.Response(200, text="<html>"),
    ],
)
def test_without_its_alerts_an_incident_is_still_triaged_alone(
    settings: Settings, fake_cluster: Any, answer: Any, caplog: pytest.LogCaptureFixture
) -> None:
    pd = FakePagerDuty()
    pd.answers = [answer]
    with client(settings, fake_cluster, ONE, slack=both(pd), acme=READ) as c:
        first = deliver(c, event()).json()
        assert first["status"] == "accepted"
        t = wait_for(c, first["investigation"], "done")
        (alert,) = t["input"]["alerts"]
        assert alert["annotations"] == {"incident": LINK} and alert["fingerprint"] == INCIDENT
    assert "PagerDuty incident PGR0VU2: alerts not read" in caplog.text
    assert KEY not in caplog.text


def test_a_contract_with_no_key_asks_pagerduty_nothing(
    settings: Settings, fake_cluster: Any
) -> None:
    pd = FakePagerDuty()
    with client(settings, fake_cluster, ONE, slack=both(pd), acme=SIGNED) as c:
        first = deliver(c, event()).json()
        t = wait_for(c, first["investigation"], "done")
        assert [a["fingerprint"] for a in t["input"]["alerts"]] == [INCIDENT]
        assert pd.calls == []


def test_an_incident_that_is_an_alert_group_the_contract_sent_joins_its_triage(
    settings: Settings, fake_cluster: Any
) -> None:
    pd = FakePagerDuty()
    three = (ONE, TWO, call("TriageReport", "r3", **REPORT))
    with client(settings, fake_cluster, *three, slack=both(pd), acme=READ) as c:
        tid = c.post(URL, json=ALERT, headers=HOOK).json()["investigation"]  # group key g1
        wait_for(c, tid, "done")
        # by the incident's own key: Alertmanager gives PagerDuty the group key's hash
        joined = deliver(c, event(incident_key=pagerduty.digest("g1"))).json()
        assert joined == {"status": "joined", "investigation": tid}
        # by one of its alerts' keys, where the incident's is something else
        pd.alerts = [{**ALERTS[0], "alert_key": pagerduty.digest("g1")}]
        other = deliver(c, event(incident="PGR0VU3")).json()
        assert other == {"status": "joined", "investigation": tid}
        assert deliver(c, event(incident="PGR0VU3")).json() == {
            "status": "duplicate",
            "investigation": tid,
        }
        assert [t["id"] for t in listed(c)] == [tid]  # no second Triage
        # a key that is no group of this Contract's: its own Triage
        pd.alerts = ALERTS
        own = deliver(c, event(incident="PGR0VU4", incident_key=pagerduty.digest("g2"))).json()
        assert own["status"] == "accepted" and own["investigation"] != tid
        wait_for(c, own["investigation"], "done")
        # nor is an incident ever another incident's group, whatever key it carries
        # (on another service: two incidents of one service are Related alert groups)
        crafted = {"incident_key": pagerduty.digest("pagerduty:PGR0VU4"), "service": None}
        apart = deliver(c, event(incident="PGR0VU5", **crafted)).json()
        assert apart["status"] == "accepted" and apart["investigation"] != own["investigation"]
        wait_for(c, apart["investigation"], "done")


def test_a_grafana_group_is_joined_by_the_key_grafana_sent(
    settings: Settings, fake_cluster: Any
) -> None:
    pd = FakePagerDuty([])
    with client(settings, fake_cluster, ONE, slack=both(pd), acme=READ) as c:
        tid = c.post(GRAFANA, json=FIRING, headers=HOOK).json()["investigation"]
        wait_for(c, tid, "done")
        key = pagerduty.digest(FIRING["groupKey"])  # stored as grafana:<key>, sent without
        joined = deliver(c, event(incident_key=key)).json()
        assert joined == {"status": "joined", "investigation": tid}


def test_a_group_older_than_the_dedup_window_is_not_joined(
    settings: Settings, fake_cluster: Any, database_url: str
) -> None:
    pd = FakePagerDuty([])
    three = (ONE, TWO, call("TriageReport", "r3", **REPORT))
    with client(settings, fake_cluster, *three, slack=both(pd), acme=READ) as c:
        tid = c.post(URL, json=ALERT, headers=HOOK).json()["investigation"]
        wait_for(c, tid, "done")
        with psycopg.connect(database_url) as conn:
            conn.execute("update alert_groups set started = started - 1801")
        own = deliver(c, event(incident_key=pagerduty.digest("g1"))).json()
        assert own["status"] == "accepted" and own["investigation"] != tid
        wait_for(c, own["investigation"], "done")
        # and the incident itself, come back after the window: a new Triage, as an
        # Alertmanager group's re-send is
        with psycopg.connect(database_url) as conn:
            conn.execute("update alert_groups set started = started - 1801")
        again = deliver(c, event("incident.reopened")).json()
        assert again["status"] == "accepted" and again["investigation"] != own["investigation"]
        wait_for(c, again["investigation"], "done")


def test_one_contracts_incident_never_joins_anothers_group_or_triage(
    settings: Settings, fake_cluster: Any
) -> None:
    """Isolation: the same group key and the same incident id in two Contracts."""
    pd = FakePagerDuty([])
    with client(
        settings, fake_cluster, ONE, TWO, call("TriageReport", "r3", **REPORT),
        slack=both(pd), acme=READ, more=hooked(),
    ) as c:  # fmt: skip
        theirs = c.post(BANK_URL, json=ALERT, headers=BANK_HOOK).json()["investigation"]
        wait_for(c, theirs, "done")
        # acme's incident carries the hash of a key only bank sent: never bank's Triage
        mine = deliver(c, event(incident_key=pagerduty.digest("g1"))).json()
        assert mine["status"] == "accepted" and mine["investigation"] != theirs
        wait_for(c, mine["investigation"], "done")
        # the same incident id from bank's own PagerDuty: bank's group, joined to bank's
        same = deliver(
            c, event(incident_key=pagerduty.digest("g1")), "bank", "whsec-bank-signing"
        ).json()
        assert same == {"status": "joined", "investigation": theirs}
        by_contract = {t["id"]: t["contract_id"] for t in listed(c)}
        assert by_contract == {theirs: "bank", mine["investigation"]: "acme"}
        keys = [r.headers["authorization"] for r in pd.calls]
        assert keys == [f"Token token={KEY}", "Token token=bank-rest-key"]  # each its own key


def test_an_incident_resolving_makes_its_own_proposed_actions_stale(
    settings: Settings, fake_cluster: Any
) -> None:
    pd = FakePagerDuty()
    with client(
        settings, fake_cluster, *EVICT_THEN_REPORT, actions=True, slack=both(pd), acme=READ
    ) as c:
        tid = deliver(c, event()).json()["investigation"]
        wait_for(c, tid, "awaiting_approval")
        ended = deliver(c, event("incident.resolved")).json()
        assert ended == {"status": "stale", "investigations": [tid]}
        assert len(pd.calls) == 1  # an end reads nothing
        again = deliver(c, event("incident.resolved")).json()
        assert again == {"status": "ignored", "reason": "no firing alerts"}


def test_a_joined_incident_resolving_leaves_the_alert_groups_triage_alone(
    settings: Settings, fake_cluster: Any
) -> None:
    """Only the Triage's own Alert group makes its Proposed actions Stale."""
    pd = FakePagerDuty([])
    with client(
        settings, fake_cluster, *EVICT_THEN_REPORT, actions=True, slack=both(pd), acme=READ
    ) as c:
        tid = c.post(URL, json=ALERT, headers=HOOK).json()["investigation"]
        wait_for(c, tid, "awaiting_approval")
        joined = deliver(c, event(incident_key=pagerduty.digest("g1"))).json()
        assert joined == {"status": "joined", "investigation": tid}
        ended = deliver(c, event("incident.resolved")).json()
        assert ended == {"status": "ignored", "reason": "no firing alerts"}
        t = c.get(f"/investigations/{tid}", headers=AUTH).json()
        assert t["status"] == "awaiting_approval" and t["pending"]


def note_of(pd: FakePagerDuty) -> list[tuple[str, str, str, str]]:
    return [
        (
            r.url.path,
            r.headers["from"],
            r.headers["authorization"],
            json.loads(r.content)["note"]["content"],
        )
        for r in pd.calls
        if r.method == "POST"
    ]


def confirm(slack: FakeSlack) -> str:
    text: str = buttons(slack)["send_to_client"]["confirm"]["text"]["text"]
    return text


def test_the_client_report_goes_to_the_channel_and_on_a_joined_incident_as_a_note(
    settings: Settings, fake_cluster: Any, sock: FakeSocket, database_url: str
) -> None:
    settings.slack_bot_token = SecretStr(TOKEN)
    slack, pd = FakeSlack(sock.url), FakePagerDuty([])
    with client(
        settings, fake_cluster, ONE, slack=both(pd, slack),
        acme_channel=ACME, acme_client=ACME_CLIENT, acme=NOTED,
    ) as c:  # fmt: skip
        tid = c.post(URL, json=ALERT, headers=HOOK).json()["investigation"]
        wait_for(c, tid, "done")
        until(lambda: "send_to_client" in buttons(slack))
        assert confirm(slack).endswith(f"goes to the client channel {ACME_CLIENT}.")
        joined = deliver(c, event(incident_key=pagerduty.digest("g1"))).json()
        assert joined == {"status": "joined", "investigation": tid}
        # PagerDuty's link as text, never a link: what a third party sends is data
        said = f":pager: PagerDuty incident #42 is this alert group: `{LINK}`"
        until(lambda: said in replies(slack))
        where = f"goes to the client channel {ACME_CLIENT} and on its PagerDuty incident as a note."
        until(lambda: confirm(slack).endswith(where))
        assert note_of(pd) == []  # never on its own: only when the Operator sends it
        sock.send(click("send_to_client", buttons(slack)["send_to_client"]["value"]))
        sent = (
            ":outbox_tray: Sent to the client by <@U0OPERATOR1>. "
            "A note is on its PagerDuty incident."
        )
        until(lambda: sent in replies(slack))
        assert buttons(slack) == {}
        sock.send(click("send_to_client", json.dumps({"id": tid})))  # a second click
        until(lambda: len(ephemerals(slack)) == 1)
    (message,) = to(slack, ACME_CLIENT)
    # the same words the client's channel got, once, without Slack's markup
    assert note_of(pd) == [
        ("/incidents/PGR0VU2/notes", FROM, f"Token token={KEY}", message["text"].replace("*", ""))
    ]
    assert REPORT["summary"] in message["text"] and tid not in message["text"]
    assert sent_by(database_url) == [(tid, "slack:U0OPERATOR1")]
    assert pd.paths() == ["GET /incidents/PGR0VU2/alerts", "POST /incidents/PGR0VU2/notes"]


DENIED = httpx.Response(403, json={"error": {"message": f"User Not Found: {FROM}", "code": 2100}})


def test_with_no_client_channel_a_refused_note_can_be_retried_and_one_unanswered_cant(
    settings: Settings, fake_cluster: Any, sock: FakeSocket, database_url: str
) -> None:
    settings.slack_bot_token = SecretStr(TOKEN)
    slack, pd = FakeSlack(sock.url), FakePagerDuty()
    with client(
        settings, fake_cluster, ONE, slack=both(pd, slack), acme_channel=ACME, acme=NOTED
    ) as c:
        tid = deliver(c, event()).json()["investigation"]
        wait_for(c, tid, "done")
        until(lambda: "send_to_client" in buttons(slack))
        assert confirm(slack) == (
            "The Report, without Evidence, goes on its PagerDuty incident as a note."
        )
        value = buttons(slack)["send_to_client"]["value"]
        pd.answers = [DENIED]
        sock.send(click("send_to_client", value))
        refused = "PagerDuty refused the note (HTTP 403, code 2100): nothing was sent."
        until(lambda: ephemerals(slack) == [("U0OPERATOR1", refused)])
        assert sent_by(database_url) == [(tid, None)]  # released: it can be retried
        assert "send_to_client" in buttons(slack)
        pd.answers = [httpx.Response(502)]
        sock.send(click("send_to_client", value))
        maybe = (
            ":outbox_tray: Sent to the client by <@U0OPERATOR1>. PagerDuty didn't answer for "
            "incident PGR0VU2 (HTTP 502): it may have the note. Check the incident."
        )
        until(lambda: maybe in replies(slack))
        assert sent_by(database_url) == [(tid, "slack:U0OPERATOR1")] and buttons(slack) == {}
        sock.send(click("send_to_client", value))  # never a second note
        until(lambda: len(ephemerals(slack)) == 2)
    assert [path for path, *_ in note_of(pd)] == ["/incidents/PGR0VU2/notes"] * 2
    # with no client channel the report is posted nowhere in Slack: only the parent is
    assert len([p for p in to(slack, ACME) if "thread_ts" not in p]) == 1
    assert FROM not in json.dumps([p for _, p, _ in slack.calls])  # PagerDuty's message never shown


def test_after_the_channel_has_it_a_refused_note_is_named_and_nothing_is_sent_twice(
    settings: Settings, fake_cluster: Any, sock: FakeSocket, database_url: str
) -> None:
    settings.slack_bot_token = SecretStr(TOKEN)
    slack, pd = FakeSlack(sock.url), FakePagerDuty()
    with client(
        settings, fake_cluster, ONE, slack=both(pd, slack),
        acme_channel=ACME, acme_client=ACME_CLIENT, acme=NOTED,
    ) as c:  # fmt: skip
        tid = deliver(c, event()).json()["investigation"]
        wait_for(c, tid, "done")
        until(lambda: "send_to_client" in buttons(slack))
        pd.answers = [DENIED]
        sock.send(click("send_to_client", buttons(slack)["send_to_client"]["value"]))
        named = (
            ":outbox_tray: Sent to the client by <@U0OPERATOR1>. "
            "PagerDuty refused the note on incident PGR0VU2 (HTTP 403, code 2100)."
        )
        until(lambda: named in replies(slack))
        assert buttons(slack) == {} and ephemerals(slack) == []
    assert len(to(slack, ACME_CLIENT)) == 1 and len(note_of(pd)) == 1
    assert sent_by(database_url) == [(tid, "slack:U0OPERATOR1")]


def test_no_note_without_a_from_or_without_an_incident(
    settings: Settings, fake_cluster: Any, sock: FakeSocket
) -> None:
    settings.slack_bot_token = SecretStr(TOKEN)
    slack, pd = FakeSlack(sock.url), FakePagerDuty()
    # a key but no From: an incident's Triage has nowhere to send, so no button
    with client(
        settings, fake_cluster, ONE, slack=both(pd, slack), acme_channel=ACME, acme=READ
    ) as c:
        wait_for(c, deliver(c, event()).json()["investigation"], "done")
        slack.until(3)
        assert buttons(slack) == {}
    # a From, but an Alert group that is no incident: no button either, even beside
    # another Triage of the Contract that has one
    slack = FakeSlack(sock.url, ts=100)
    with client(
        settings, fake_cluster, ONE, TWO, slack=both(pd, slack), acme_channel=ACME, acme=NOTED
    ) as c:
        # on another service than the first part's, or it would be its Related alert group
        apart = event(incident="PGR0VU9", service=None)
        other = deliver(c, apart).json()["investigation"]
        wait_for(c, other, "done")
        slack.until(3)
        until(lambda: "send_to_client" in buttons(slack))
        tid = c.post(URL, json=ALERT, headers=HOOK).json()["investigation"]
        wait_for(c, tid, "done")
        until(lambda: buttons(slack) == {})  # the last parent posted: the Alert group's
        sock.send(click("send_to_client", json.dumps({"id": tid}), message_ts="1700000000.000101"))
        until(lambda: ephemerals(slack) == [("U0OPERATOR1", "Already sent, or nothing to send.")])
    assert note_of(pd) == []


def test_one_contracts_note_never_goes_on_anothers_incident(
    settings: Settings, fake_cluster: Any, sock: FakeSocket
) -> None:
    """Isolation: bank's Triage of the same incident id is not acme's to note."""
    settings.slack_bot_token = SecretStr(TOKEN)
    slack, pd = FakeSlack(sock.url), FakePagerDuty([])
    with client(
        settings, fake_cluster, ONE, TWO, slack=both(pd, slack),
        acme_channel=ACME, acme=NOTED, more=hooked(),
    ) as c:  # fmt: skip
        theirs = deliver(c, event(), "bank", "whsec-bank-signing").json()["investigation"]
        wait_for(c, theirs, "done")
        tid = c.post(URL, json=ALERT, headers=HOOK).json()["investigation"]  # no incident
        wait_for(c, tid, "done")
        slack.until(3)
        assert buttons(slack) == {}  # bank's incident gives acme's Triage no button
        sock.send(click("send_to_client", json.dumps({"id": tid})))
        until(lambda: len(ephemerals(slack)) == 1)
        sock.send(click("send_to_client", json.dumps({"id": theirs})))  # forged: bank's id
        until(lambda: len(ephemerals(slack)) == 2)
    assert note_of(pd) == []


def test_health_shows_pagerduty_where_the_contract_has_a_key(
    settings: Settings, fake_cluster: Any
) -> None:
    settings.console_operators = {LOGIN}
    pd = FakePagerDuty()
    with client(settings, fake_cluster, slack=both(pd), acme=READ, more=hooked("bank")) as c:
        pd.answers = [
            httpx.Response(200, json={"abilities": []}),
            httpx.Response(401, json={"error": {"message": f"key {KEY}", "code": 2006}}),
        ]
        mine = c.get("/api/v1/contracts/acme/health", headers=VIEW).json()["integrations"]
        theirs = c.get("/api/v1/contracts/bank/health", headers=VIEW)
    assert (mine[0]["name"], mine[0]["ok"], mine[0]["detail"]) == ("pagerduty", True, "")
    row = theirs.json()["integrations"][0]
    assert (row["name"], row["ok"], row["detail"]) == ("pagerduty", False, "HTTP 401, code 2006")
    assert KEY not in theirs.text
    keys = [(r.url.path, r.headers["authorization"]) for r in pd.calls]
    assert keys == [
        ("/abilities", f"Token token={KEY}"),
        ("/abilities", "Token token=bank-rest-key"),
    ]
    with client(settings, fake_cluster, slack=both(pd), acme=SIGNED) as c:  # no key: no row
        rows = c.get("/api/v1/contracts/acme/health", headers=VIEW).json()["integrations"]
    assert "pagerduty" not in [p["name"] for p in rows] and len(pd.calls) == 2
