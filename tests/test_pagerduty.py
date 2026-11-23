"""PagerDuty on its own (slice 19.2): a delivery's signature over the raw body, the
incident a delivery starts or ends (and nothing for any other), the Alert group it becomes,
the dedup key an Alertmanager group has there, and the two calls the Hub makes with the
Contract's key: an incident's alerts, and a note. Built on the shapes PagerDuty documents."""

import asyncio
import hashlib
import hmac
import json
import time
from typing import Any

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from squero import agent, contracts, pagerduty
from squero.models import Investigation, TriageReport
from squero.slack import client_note, client_text
from tests.conftest import REPORT, make_contract
from tests.test_shared_stores import HERE

SIGNING, KEY, FROM = "whsec-acme-signing", "u+acmeRestApiKey", "oncall@acme.example"
INCIDENT = "PGR0VU2"
URL = f"https://acme.pagerduty.com/incidents/{INCIDENT}"


def event(kind: str = "incident.triggered", incident: Any = INCIDENT, **data: Any) -> Any:
    """A V3 delivery, as PagerDuty's webhook overview lays one out."""
    service = {
        "html_url": "https://acme.pagerduty.com/services/PF9KMXH",
        "id": "PF9KMXH",
        "self": "https://api.pagerduty.com/services/PF9KMXH",
        "summary": "checkout",
        "type": "service_reference",
    }
    priority = {"id": "PSO75BM", "summary": "P1", "type": "priority_reference"}
    return {
        "event": {
            "id": "01CH754SM0",
            "event_type": kind,
            "resource_type": "incident",
            "occurred_at": "2026-11-22T18:45:22.169Z",
            "agent": {"id": "PLH1HKV", "summary": "Tenex Engineer", "type": "user_reference"},
            "client": {"name": "PagerDuty"},
            "data": {
                "id": incident,
                "type": "incident",
                "self": f"https://api.pagerduty.com/incidents/{incident}",
                "html_url": f"https://acme.pagerduty.com/incidents/{incident}",
                "number": 42,
                "status": "triggered",
                "incident_key": "d3640fbd41094207a1c11e58e46b1662",
                "created_at": "2026-11-22T18:44:07Z",
                "title": "Checkout is failing",
                "service": service,
                "assignees": [],
                "teams": [],
                "priority": priority,
                "urgency": "high",
                "conference_bridge": None,
                "resolve_reason": None,
                **data,
            },
        }
    }


ALERTS = [
    {
        "id": "PT4KHLK",
        "type": "alert",
        "summary": "checkout 5xx above 5%",
        "status": "triggered",
        "alert_key": "srv01/HTTP",
        "severity": "critical",
        "body": {"type": "alert_body", "details": {"namespace": "shop", "rate": "0.07"}},
    }
]


def sign(body: bytes, secret: str = SIGNING) -> str:
    return "v1=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def raw(payload: Any) -> bytes:
    return json.dumps(payload).encode()


def test_a_delivery_is_checked_by_its_signature_over_the_raw_body() -> None:
    body, secret = raw(event()), SecretStr(SIGNING)
    good = sign(body)
    assert pagerduty.verified(secret, body, good)
    # PagerDuty's own example: a second signature while a secret is rotated
    assert pagerduty.verified(secret, body, f"{sign(body, 'old-secret')}, {good}")
    assert pagerduty.verified(secret, body, f"{good},{sign(body, 'new-secret')}")
    for header in (
        "",
        sign(body, "another-contracts-secret"),
        good.removeprefix("v1="),  # the digest without its version
        good.replace("v1=", "v2="),
        good[:-1],
        good + "0",
        good.upper(),
        "v1=",
        "v1=é",  # never an error: a header is anyone's bytes
    ):
        assert not pagerduty.verified(secret, body, header), header
    assert not pagerduty.verified(secret, body + b" ", good)  # one more byte in the body
    assert not pagerduty.verified(secret, body.replace(b"PGR0VU2", b"PGR0VU3"), good)
    # a Contract with an empty secret takes nothing: anyone can sign with no key
    assert not pagerduty.verified(SecretStr(""), body, sign(body, ""))


def test_an_incident_starting_or_ending_is_read_from_the_delivery() -> None:
    found = pagerduty.incident(raw(event()))
    assert found == pagerduty.Incident(
        id=INCIDENT,
        firing=True,
        title="Checkout is failing",
        number=42,
        url=URL,
        service="checkout",
        urgency="high",
        priority="P1",
        key="d3640fbd41094207a1c11e58e46b1662",
    )
    assert found is not None and found.group_key == "pagerduty:PGR0VU2"
    again = pagerduty.incident(raw(event("incident.reopened")))
    ended = pagerduty.incident(raw(event("incident.resolved")))
    assert again is not None and again.firing and ended is not None and not ended.firing


@pytest.mark.parametrize(
    "kind",
    [
        "incident.acknowledged",
        "incident.annotated",
        "incident.escalated",
        "incident.priority_updated",
        "incident.unacknowledged",
        "service.updated",
        "pagey.ping",
        "",
        None,
        7,
        ["incident.triggered"],  # never an error: a signed delivery is never refused
        {"incident.triggered": 1},
    ],
)
def test_any_other_event_is_nothing(kind: Any) -> None:
    assert pagerduty.incident(raw(event(kind))) is None


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json",
        b"[]",
        b'"event"',
        b"{}",
        raw({"event": None}),
        raw({"event": []}),
        raw({"event": {"event_type": "incident.triggered"}}),
        raw({"event": {"event_type": "incident.triggered", "data": None}}),
        raw({"event": {"event_type": "incident.triggered", "data": ["id"]}}),
        raw({"event": {"event_type": "incident.triggered", "data": "id"}}),
        raw(event(incident=7)),
        raw(event(incident=None)),
        raw(event(incident="")),
        raw(event(incident="PGR0VU2/notes")),  # it goes into a URL
        raw(event(incident="../users")),
        raw(event(incident="P" * 41)),
    ],
)
def test_a_shape_it_doesnt_know_is_nothing_never_an_error(body: bytes) -> None:
    assert pagerduty.incident(body) is None


def test_fields_of_another_type_are_left_out() -> None:
    odd = event(
        title=["x"],
        number="42",
        html_url="http://acme.pagerduty.com/incidents/1",  # only an https link is kept
        service="checkout",
        urgency=1,
        priority=None,
        incident_key=None,
    )
    assert pagerduty.incident(raw(odd)) == pagerduty.Incident(id=INCIDENT, firing=True)
    assert pagerduty.incident(raw(event(number=True))) == pagerduty.incident(
        raw(event(number=None))
    )


def test_an_incident_becomes_an_alert_group_with_its_alerts_details() -> None:
    found = pagerduty.incident(raw(event()))
    assert found is not None
    group = pagerduty.group(found, ALERTS)
    labels = {
        "alertname": "Checkout is failing",
        "source": "pagerduty",
        "service": "checkout",
        "urgency": "high",
        "priority": "P1",
    }
    assert group.model_dump(mode="json") == {
        "version": "4",
        "status": "firing",
        "receiver": "pagerduty",
        "groupKey": "pagerduty:PGR0VU2",
        "groupLabels": {"incident": INCIDENT},
        "commonLabels": labels,
        "commonAnnotations": {},
        "externalURL": "",
        "alerts": [
            {
                "status": "firing",
                "labels": labels | {"severity": "critical"},
                "annotations": {
                    "summary": "checkout 5xx above 5%",
                    "details": '{"namespace": "shop", "rate": "0.07"}',
                    "incident": URL,
                },
                "startsAt": None,
                "generatorURL": "",
                "fingerprint": "PT4KHLK",
            }
        ],
    }
    assert group.title == "Checkout is failing"
    prompt = agent.alert_prompt(group)  # where the agent reads it, as any Alert group
    assert '\\"rate\\": \\"0.07\\"' in prompt and "checkout 5xx above 5%" in prompt


def test_without_its_alerts_the_incident_alone_is_the_group() -> None:
    found = pagerduty.incident(raw(event(priority=None, html_url=None, title="")))
    assert found is not None
    group = pagerduty.group(found, [])
    (alert,) = group.alerts
    assert (
        alert.labels
        == group.commonLabels
        == {
            "alertname": "PagerDuty incident",
            "source": "pagerduty",
            "service": "checkout",
            "urgency": "high",
        }
    )
    assert (alert.annotations, alert.fingerprint, alert.status) == ({}, INCIDENT, "firing")
    ended = pagerduty.incident(raw(event("incident.resolved")))
    assert ended is not None
    group = pagerduty.group(ended, ALERTS)
    assert group.status == "resolved" and group.firing == []


def test_alerts_and_their_text_are_cut_to_size() -> None:
    found = pagerduty.incident(raw(event()))
    assert found is not None
    many = [
        {"id": f"A{i}", "summary": "s" * 600, "body": {"details": {"log": "d" * 3000}}}
        for i in range(12)
    ]
    odd = [{"id": 7, "summary": None, "body": "text", "severity": 3}, {"body": {"details": []}}]
    group = pagerduty.group(found, many)
    assert [a.fingerprint for a in group.alerts] == [f"A{i}" for i in range(10)]
    assert {len(a.annotations["summary"]) for a in group.alerts} == {500}
    assert {len(a.annotations["details"]) for a in group.alerts} == {2000}
    for alert in pagerduty.group(found, odd).alerts:  # an alert of another shape: no text
        assert alert.annotations == {"incident": URL} and alert.fingerprint == INCIDENT
        assert "severity" not in alert.labels


def test_an_alert_groups_dedup_key_is_the_sha256_of_its_group_key() -> None:
    """Alertmanager's `notify.Key.Hash`, which Grafana's PagerDuty contact point calls too."""
    abc = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert pagerduty.digest("abc") == abc
    assert pagerduty.digest("grafana:abc") == abc  # the prefix is the Hub's, never sent
    assert pagerduty.digest('{}:{alertname="X"}') != abc
    found = pagerduty.incident(raw(event(incident_key="k1")))
    assert found is not None
    assert pagerduty.keys(found, ALERTS) == {"k1", "srv01/HTTP"}
    assert pagerduty.keys(found, [{"alert_key": None}, {"alert_key": ""}, {}]) == {"k1"}
    bare = pagerduty.incident(raw(event(incident_key=None)))
    assert bare is not None and pagerduty.keys(bare, []) == set()


def answering(reply: Any, seen: list[httpx.Request]) -> httpx.AsyncClient:
    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(r)
        if isinstance(reply, Exception):
            raise reply
        return reply  # type: ignore[no-any-return]

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.anyio
async def test_an_incidents_alerts_are_read_with_the_contracts_key() -> None:
    seen: list[httpx.Request] = []
    http = answering(httpx.Response(200, json={"alerts": [*ALERTS, "odd"], "more": False}), seen)
    assert await pagerduty.alerts(http, SecretStr(KEY), INCIDENT) == ALERTS
    (r,) = seen
    assert (r.method, str(r.url)) == (
        "GET",
        "https://api.pagerduty.com/incidents/PGR0VU2/alerts?limit=10",
    )
    assert r.headers["authorization"] == f"Token token={KEY}"
    assert r.headers["accept"] == "application/vnd.pagerduty+json;version=2"
    assert r.extensions["timeout"]["read"] == 3  # inside PagerDuty's 5 for a delivery
    assert "from" not in r.headers


DENIED = {"error": {"message": f"Requester User Not Found: {FROM}", "code": 2100}}


@pytest.mark.parametrize(
    ("reply", "error", "said"),
    [
        (httpx.Response(401, json=DENIED), pagerduty.Refused, "HTTP 401, code 2100"),
        (httpx.Response(403, text="Forbidden"), pagerduty.Refused, "HTTP 403"),
        (httpx.Response(404, json={"error": {"code": "x"}}), pagerduty.Refused, "HTTP 404"),
        (httpx.Response(500, json=DENIED), pagerduty.Refused, "HTTP 500, code 2100"),
        (
            httpx.Response(200, json={"more": False}),
            pagerduty.Refused,
            "an answer that lists no alerts",
        ),
        (httpx.Response(200, text="<html>"), pagerduty.Refused, "an answer that lists no alerts"),
        (httpx.ReadTimeout("slow"), pagerduty.Unknown, "ReadTimeout"),
        (httpx.ConnectError(f"no route for {KEY}"), pagerduty.Unknown, "ConnectError"),
    ],
)
@pytest.mark.anyio
async def test_alerts_not_read_say_the_status_and_code_only(
    reply: Any, error: type[Exception], said: str
) -> None:
    with pytest.raises(error) as e:
        await pagerduty.alerts(answering(reply, []), SecretStr(KEY), INCIDENT)
    assert str(e.value) == said
    assert FROM not in str(e.value) and KEY not in str(e.value)


@pytest.mark.parametrize("listed", [{"id": "x"}, 7, "alerts", None])
@pytest.mark.anyio
async def test_alerts_that_arent_a_list_are_none(listed: Any) -> None:
    http = answering(httpx.Response(200, json={"alerts": listed}), [])
    assert await pagerduty.alerts(http, SecretStr(KEY), INCIDENT) == []


@pytest.mark.anyio
async def test_a_read_has_three_seconds_in_all_not_per_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PagerDuty waits 5 seconds for a delivery's answer; httpx's own limit restarts with
    every phase and every byte."""
    assert pagerduty.READ_SECONDS == 3
    monkeypatch.setattr(pagerduty, "READ_SECONDS", 0.05)

    async def slow(r: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json={"alerts": ALERTS})

    http = httpx.AsyncClient(transport=httpx.MockTransport(slow))
    started = time.monotonic()
    with pytest.raises(pagerduty.Unknown, match="TimeoutError"):
        await pagerduty.alerts(http, SecretStr(KEY), INCIDENT)
    assert await pagerduty.reachable(http, SecretStr(KEY)) == "TimeoutError"
    assert time.monotonic() - started < 2


def test_the_note_is_the_client_report_in_plain_text() -> None:
    """Slack's markup and escaping would show in PagerDuty as asterisks and `&gt;`."""
    odd = {"summary": "5xx > 5% & rising <fast>, token=abcdefghijklmnop1234"}
    t = Investigation(
        id="i1",
        contract_id="acme",
        title="Checkout",
        started=0,
        report=TriageReport(**{**REPORT, **odd}),
    )
    slack_text, note = client_text(t), client_note(t)
    assert slack_text.startswith("*Checkout: 5xx &gt; 5% &amp; rising &lt;fast&gt;")
    first, *rest = note.splitlines()
    assert first.startswith("Checkout: 5xx > 5% & rising <fast>, token=")
    assert "Root cause (high confidence): " in rest[1] and rest[-2].startswith("1. ")
    for mark in ("*", "#", "&gt;", "&amp;"):
        assert mark not in note, mark
    assert "abcdefghijklmnop1234" not in note and "abcdefghijklmnop1234" not in slack_text
    assert "i1" not in note and "kubernetes_pod_logs" not in note  # no ids, no Evidence
    # the same words: Slack's text without its markup and escaping
    unescaped = slack_text.replace("&gt;", ">").replace("&lt;", "<").replace("&amp;", "&")
    assert note == unescaped.replace("*", "")


@pytest.mark.anyio
async def test_a_note_is_written_as_the_from_user_and_cut_to_size() -> None:
    for status in (200, 201):
        seen: list[httpx.Request] = []
        http = answering(httpx.Response(status, json={"note": {"id": "PWL7QXS"}}), seen)
        await pagerduty.note(http, SecretStr(KEY), SecretStr(FROM), INCIDENT, "x" * 9000)
        (r,) = seen
        assert (r.method, str(r.url)) == (
            "POST",
            "https://api.pagerduty.com/incidents/PGR0VU2/notes",
        )
        assert r.headers["authorization"] == f"Token token={KEY}"
        assert r.headers["accept"] == "application/vnd.pagerduty+json;version=2"
        assert r.headers["from"] == FROM
        assert json.loads(r.content) == {"note": {"content": "x" * 8000}}


@pytest.mark.parametrize(
    ("reply", "error", "said"),
    [
        (httpx.Response(400, json=DENIED), pagerduty.Refused, "HTTP 400, code 2100"),
        (httpx.Response(403, json=DENIED), pagerduty.Refused, "HTTP 403, code 2100"),
        (
            httpx.Response(429, json={"error": {"code": 2020}}),
            pagerduty.Refused,
            "HTTP 429, code 2020",
        ),
        (httpx.Response(204), pagerduty.Refused, "HTTP 204"),
        (httpx.ConnectError("refused"), pagerduty.Refused, "could not connect (ConnectError)"),
        (httpx.ConnectTimeout("slow"), pagerduty.Refused, "could not connect (ConnectTimeout)"),
        # it may be written: never offered again
        (httpx.Response(500, json=DENIED), pagerduty.Unknown, "HTTP 500"),
        (httpx.Response(504), pagerduty.Unknown, "HTTP 504"),
        (httpx.ReadTimeout("slow"), pagerduty.Unknown, "ReadTimeout"),
        (httpx.RemoteProtocolError("cut"), pagerduty.Unknown, "RemoteProtocolError"),
    ],
)
@pytest.mark.anyio
async def test_a_note_refused_can_be_retried_and_one_unanswered_may_be_there(
    reply: Any, error: type[Exception], said: str
) -> None:
    with pytest.raises(error) as e:
        await pagerduty.note(answering(reply, []), SecretStr(KEY), SecretStr(FROM), INCIDENT, "x")
    assert str(e.value) == said
    assert FROM not in str(e.value) and KEY not in str(e.value)  # never PagerDuty's message


@pytest.mark.anyio
async def test_the_health_check_is_one_read_with_the_key() -> None:
    seen: list[httpx.Request] = []
    http = answering(httpx.Response(200, json={"abilities": ["sso"]}), seen)
    assert await pagerduty.reachable(http, SecretStr(KEY)) == ""
    (r,) = seen
    assert (r.method, str(r.url)) == ("GET", "https://api.pagerduty.com/abilities")
    assert r.headers["authorization"] == f"Token token={KEY}"
    refused = answering(httpx.Response(401, json=DENIED), [])
    assert await pagerduty.reachable(refused, SecretStr(KEY)) == "HTTP 401, code 2100"
    down = answering(httpx.ConnectError(f"no route for {KEY}"), [])
    assert await pagerduty.reachable(down, SecretStr(KEY)) == "ConnectError"


PD = {"webhook_secret": "pd-signing", "api_key_secret": "pd-key", "from_secret": "pd-from"}
SECRETS = {"pd-signing": SIGNING, "pd-key": KEY, "pd-from": FROM}


def test_a_contract_names_its_pagerduty_secrets() -> None:
    c = make_contract(pagerduty=PD, secrets=SECRETS)
    assert c.pagerduty is not None and c.secret(c.pagerduty.webhook_secret) == SecretStr(SIGNING)
    assert {"pd-signing", "pd-key", "pd-from"} <= c.secret_refs()  # each checked at load
    only = make_contract(pagerduty={"webhook_secret": "pd-signing"}, secrets=SECRETS)
    assert only.pagerduty is not None
    assert (only.pagerduty.api_key_secret, only.pagerduty.from_secret) == (None, None)
    assert only.secret_refs() >= {"pd-signing"} and "pd-key" not in only.secret_refs()
    assert make_contract().pagerduty is None
    with pytest.raises(ValidationError, match="from_secret needs api_key_secret"):
        make_contract(pagerduty={"webhook_secret": "s", "from_secret": "f"}, secrets=SECRETS)
    with pytest.raises(ValidationError, match="webhook_secret"):
        make_contract(pagerduty={"api_key_secret": "pd-key"}, secrets=SECRETS)
    with pytest.raises(ValidationError, match="region"):  # a typo is refused, not ignored
        make_contract(pagerduty={**PD, "region": "eu"}, secrets=SECRETS)


def test_two_contracts_must_not_share_a_signing_secret() -> None:
    def named(cid: str, signing: str) -> contracts.Contract:
        return make_contract(
            contract_id=cid, pagerduty=PD, secrets={**SECRETS, "pd-signing": signing}
        )

    a, b, c = named("one", SIGNING), named("two", SIGNING), named("three", "its-own")
    (problem,) = contracts._shared_signing(HERE, [a, b, c, make_contract(contract_id="four")])
    assert problem == (
        "contracts: one, two share a PagerDuty signing secret; "
        "one Contract's PagerDuty could post as another"
    )
    assert SIGNING not in problem
    assert contracts._shared_signing(HERE, [a, c]) == []
