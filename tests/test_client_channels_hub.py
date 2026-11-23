# ruff: noqa: F811 - sock is test_slack's fixture, imported for these tests
"""Client channels in the Hub (slice 19.4): one press of Send to client posts the Client
report to the Contract's Teams and Google Chat channels too, after Slack's client channel
and under the same single claim; never on its own; the dialog names every destination; a
refusal is named or retried by the same rule as PagerDuty's note; and one Contract's
report never reaches another's channel."""

import json
from typing import Any

import httpx
from pydantic import SecretStr

from squero import clientchannels
from squero.config import Settings
from tests.conftest import REPORT, call, make_contract, make_fake_cluster
from tests.test_api import (  # noqa: F401 - _database: every test here needs Postgres
    ALERT,
    BANK_HOOK,
    BANK_URL,
    HOOK,
    URL,
    _database,
    client,
    offline,
    wait_for,
)
from tests.test_client_channels import CHANNELS, CHAT_URL, SECRETS, TEAMS_URL, WIDE
from tests.test_pagerduty import PD, event
from tests.test_pagerduty import SECRETS as PD_SECRETS
from tests.test_pagerduty_hub import FakePagerDuty, deliver
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

CHATS = {"client_channels": CHANNELS, "secrets": SECRETS}
ONE = call("TriageReport", "r1", **REPORT)
TEAMS, CHAT = "prod-07.westus.logic.azure.com", "chat.googleapis.com"


class Channels:
    """Teams' and Google Chat's ends, in the Hub's one outbound client: each request
    kept; `answers` overrides the next replies, in the order they are asked."""

    def __init__(self) -> None:
        self.seen: list[httpx.Request] = []
        self.answers: list[httpx.Response] = []

    def __call__(self, r: httpx.Request) -> httpx.Response:
        self.seen.append(r)
        return self.answers.pop(0) if self.answers else httpx.Response(202)

    def hosts(self) -> list[str]:
        return [r.url.host for r in self.seen]

    def to(self, host: str) -> list[Any]:
        return [json.loads(r.content) for r in self.seen if r.url.host == host]


def routed(chats: Channels, slack: Any = None) -> httpx.MockTransport:
    ours = ("logic.azure.com", "googleapis.com")
    return httpx.MockTransport(
        lambda r: chats(r) if (r.url.host or "").endswith(ours) else (slack or offline)(r)
    )


def confirm(slack: FakeSlack) -> str:
    text: str = buttons(slack)["send_to_client"]["confirm"]["text"]["text"]
    return text


def lines(card: Any) -> list[str]:
    return [b["text"] for b in card["attachments"][0]["content"]["body"]]


def test_one_press_sends_the_report_to_teams_and_google_chat_once_never_on_its_own(
    settings: Settings, fake_cluster: Any, sock: FakeSocket, database_url: str
) -> None:
    settings.slack_bot_token = SecretStr(TOKEN)
    slack, chats = FakeSlack(sock.url), Channels()
    with client(
        settings, fake_cluster, ONE, slack=routed(chats, slack), acme_channel=ACME, acme=CHATS
    ) as c:
        tid = c.post(URL, json=ALERT, headers=HOOK).json()["investigation"]
        wait_for(c, tid, "done")
        until(lambda: "send_to_client" in buttons(slack))  # with no Slack client channel
        assert confirm(slack) == (
            "The Report, without Evidence, goes to Teams (teams-url) and to Google Chat (chat-url)."
        )  # every destination, by name: the click is the consent
        assert chats.seen == []  # nothing until the click
        value = buttons(slack)["send_to_client"]["value"]
        sock.send(click("send_to_client", value))
        said = (
            ":outbox_tray: Sent to the client by <@U0OPERATOR1>. "
            "Accepted by Teams (teams-url), Google Chat (chat-url)."
        )
        until(lambda: said in replies(slack))
        assert buttons(slack) == {}
        sock.send(click("send_to_client", value))  # a second click
        until(lambda: ephemerals(slack) == [("U0OPERATOR1", "Already sent, or nothing to send.")])
    assert chats.hosts() == [TEAMS, CHAT]  # in the Contract's order, once each
    teams, chat = chats.seen
    assert str(teams.url) == TEAMS_URL.replace(":443", "") and str(chat.url) == CHAT_URL
    (card,), (message,) = chats.to(TEAMS), chats.to(CHAT)
    text = message["text"]
    assert text.splitlines()[0] == f"KubePodCrashLooping shop: {REPORT['summary']}"
    assert lines(card) == [line for line in text.split("\n") if line.strip()]  # the same words
    assert "Raise the memory limit to 512Mi" in text and "Root cause (high confidence)" in text
    for internal in ("kubernetes_pod_logs", "OutOfMemoryError", "Evidence", tid, "*", "&gt;"):
        assert internal not in text, internal
    assert sent_by(database_url) == [(tid, "slack:U0OPERATOR1")]
    posted = [p for p in to(slack, ACME) if "thread_ts" not in p]
    assert len(posted) == 1  # the parent alone: the report is posted nowhere in Slack


def test_what_the_model_wrote_can_make_no_link_and_no_mention_in_the_clients_channel(
    settings: Settings, fake_cluster: Any, sock: FakeSocket
) -> None:
    settings.slack_bot_token = SecretStr(TOKEN)
    slack, chats = FakeSlack(sock.url), Channels()
    planted = "checkout fails, see https://evil.example/login?d=shop and <users/all>"
    step = call("TriageReport", "r1", **{**REPORT, "summary": planted})
    with client(
        settings, fake_cluster, step, slack=routed(chats, slack),
        acme_channel=ACME, acme_client=ACME_CLIENT, acme=CHATS,
    ) as c:  # fmt: skip
        tid = c.post(URL, json=ALERT, headers=HOOK).json()["investigation"]
        wait_for(c, tid, "done")
        until(lambda: "send_to_client" in buttons(slack))
        assert confirm(slack).endswith(
            f"goes to the client channel {ACME_CLIENT} and to Teams (teams-url) and to "
            "Google Chat (chat-url)."
        )
        sock.send(click("send_to_client", buttons(slack)["send_to_client"]["value"]))
        until(lambda: len(chats.seen) == 2)
        until(lambda: any("Accepted by Teams (teams-url)" in r for r in replies(slack)))
    safe = f"see https[:]//evil[.]example/login?d=shop and {WIDE}users/all>"
    (card,), (message,) = chats.to(TEAMS), chats.to(CHAT)
    assert safe in message["text"] and safe in lines(card)[0]
    for body in (message["text"], "\n".join(lines(card))):
        assert "://" not in body and "<" not in body
    (slack_copy,) = to(slack, ACME_CLIENT)  # Slack's own rule, as before: a URL as code
    assert "`https://evil.example/login?d=shop`" in slack_copy["text"]
    assert "&lt;users/all&gt;" in slack_copy["text"]


def test_with_nothing_sent_yet_a_refusal_can_be_retried(
    settings: Settings, fake_cluster: Any, sock: FakeSocket, database_url: str
) -> None:
    settings.slack_bot_token = SecretStr(TOKEN)
    slack, chats = FakeSlack(sock.url), Channels()
    with client(
        settings, fake_cluster, ONE, slack=routed(chats, slack), acme_channel=ACME, acme=CHATS
    ) as c:
        tid = c.post(URL, json=ALERT, headers=HOOK).json()["investigation"]
        wait_for(c, tid, "done")
        until(lambda: "send_to_client" in buttons(slack))
        value = buttons(slack)["send_to_client"]["value"]
        chats.answers = [httpx.Response(400, text=f"bad card for {TEAMS_URL}")]
        sock.send(click("send_to_client", value))
        refused = "Teams (teams-url) refused the report (HTTP 400): nothing was sent."
        until(lambda: ephemerals(slack) == [("U0OPERATOR1", refused)])
        assert sent_by(database_url) == [(tid, None)] and chats.hosts() == [TEAMS]
        assert "send_to_client" in buttons(slack)  # released: it can be retried
        sock.send(click("send_to_client", value))
        until(
            lambda: any("Accepted by Teams (teams-url), Google Chat" in r for r in replies(slack))
        )
    assert chats.hosts() == [TEAMS, TEAMS, CHAT]
    assert sent_by(database_url) == [(tid, "slack:U0OPERATOR1")]
    assert "TEAMS-SIGNATURE" not in json.dumps([p for _, p, _ in slack.calls])


def test_after_something_was_sent_a_refusal_is_named_and_the_rest_still_go(
    settings: Settings, fake_cluster: Any, sock: FakeSocket, database_url: str
) -> None:
    settings.slack_bot_token = SecretStr(TOKEN)
    slack, chats = FakeSlack(sock.url), Channels()
    with client(
        settings, fake_cluster, ONE, slack=routed(chats, slack),
        acme_channel=ACME, acme_client=ACME_CLIENT, acme=CHATS,
    ) as c:  # fmt: skip
        tid = c.post(URL, json=ALERT, headers=HOOK).json()["investigation"]
        wait_for(c, tid, "done")
        until(lambda: "send_to_client" in buttons(slack))
        denied = {"error": {"code": 403, "message": "no", "status": "PERMISSION_DENIED"}}
        chats.answers = [httpx.Response(400), httpx.Response(403, json=denied)]
        sock.send(click("send_to_client", buttons(slack)["send_to_client"]["value"]))
        named = (
            ":outbox_tray: Sent to the client by <@U0OPERATOR1>. "
            "Teams (teams-url) refused the report (HTTP 400). "
            "Google Chat (chat-url) refused the report (HTTP 403 PERMISSION_DENIED)."
        )
        until(lambda: named in replies(slack))
        assert buttons(slack) == {} and ephemerals(slack) == []
    assert len(to(slack, ACME_CLIENT)) == 1 and chats.hosts() == [TEAMS, CHAT]  # nothing twice
    assert sent_by(database_url) == [(tid, "slack:U0OPERATOR1")]


def test_a_channel_that_doesnt_answer_may_have_it_and_is_never_sent_again(
    settings: Settings, fake_cluster: Any, sock: FakeSocket, database_url: str
) -> None:
    settings.slack_bot_token = SecretStr(TOKEN)
    slack, chats = FakeSlack(sock.url), Channels()
    with client(
        settings, fake_cluster, ONE, slack=routed(chats, slack), acme_channel=ACME, acme=CHATS
    ) as c:
        tid = c.post(URL, json=ALERT, headers=HOOK).json()["investigation"]
        wait_for(c, tid, "done")
        until(lambda: "send_to_client" in buttons(slack))
        value = buttons(slack)["send_to_client"]["value"]
        chats.answers = [httpx.Response(502), httpx.Response(400)]
        sock.send(click("send_to_client", value))
        maybe = (
            ":outbox_tray: Sent to the client by <@U0OPERATOR1>. "
            "Teams (teams-url) didn't answer (HTTP 502): it may have the report. Check the "
            "channel. Google Chat (chat-url) refused the report (HTTP 400)."
        )
        until(lambda: maybe in replies(slack))
        assert sent_by(database_url) == [(tid, "slack:U0OPERATOR1")] and buttons(slack) == {}
        sock.send(click("send_to_client", value))
        until(lambda: len(ephemerals(slack)) == 1)
    assert chats.hosts() == [TEAMS, CHAT]  # the one that may have it counts as sent


def test_one_contracts_report_never_reaches_anothers_channel(
    settings: Settings, fake_cluster: Any, sock: FakeSocket
) -> None:
    """Isolation: bank has its own Teams channel; acme's press and a forged click."""
    settings.slack_bot_token = SecretStr(TOKEN)
    slack, chats = FakeSlack(sock.url), Channels()
    theirs = TEAMS_URL.replace("prod-07", "prod-99")
    bank = make_contract(
        contract_id="bank",
        webhook_token="bank-token",
        client_channels=[{"kind": "teams", "url_secret": "bank-teams"}],
        secrets={"bank-teams": theirs},
    )
    two = (ONE, call("TriageReport", "r2", **REPORT))
    with client(
        settings, fake_cluster, *two, slack=routed(chats, slack), acme_channel=ACME, acme=CHATS,
        more={"bank": (bank, make_fake_cluster())},
    ) as c:  # fmt: skip
        other = c.post(BANK_URL, json=ALERT, headers=BANK_HOOK).json()["investigation"]
        wait_for(c, other, "done")
        tid = c.post(URL, json=ALERT, headers=HOOK).json()["investigation"]
        wait_for(c, tid, "done")
        until(lambda: "send_to_client" in buttons(slack))
        sock.send(click("send_to_client", json.dumps({"id": other})))  # forged: bank's id
        until(lambda: len(ephemerals(slack)) == 1)
        assert chats.seen == []
        sock.send(click("send_to_client", buttons(slack)["send_to_client"]["value"]))
        until(lambda: any("Accepted by Teams (teams-url)" in r for r in replies(slack)))
    assert chats.hosts() == [TEAMS, CHAT]  # acme's own two: bank's host was never asked
    assert "prod-99" not in "".join(str(r.url) for r in chats.seen)


def test_with_a_pagerduty_incident_the_thread_says_each_in_its_own_words(
    settings: Settings, fake_cluster: Any, sock: FakeSocket
) -> None:
    settings.slack_bot_token = SecretStr(TOKEN)
    slack, chats, pd = FakeSlack(sock.url), Channels(), FakePagerDuty()
    transport = httpx.MockTransport(
        lambda r: pd(r) if r.url.host == "api.pagerduty.com" else routed(chats, slack).handler(r)
    )
    both = {"pagerduty": PD, "client_channels": CHANNELS, "secrets": {**PD_SECRETS, **SECRETS}}
    with client(settings, fake_cluster, ONE, slack=transport, acme_channel=ACME, acme=both) as c:
        tid = deliver(c, event()).json()["investigation"]
        wait_for(c, tid, "done")
        until(lambda: "send_to_client" in buttons(slack))
        assert confirm(slack) == (
            "The Report, without Evidence, goes on its PagerDuty incident as a note and to "
            "Teams (teams-url) and to Google Chat (chat-url)."
        )
        sock.send(click("send_to_client", buttons(slack)["send_to_client"]["value"]))
        said = (
            ":outbox_tray: Sent to the client by <@U0OPERATOR1>. A note is on its PagerDuty "
            "incident. Accepted by Teams (teams-url), Google Chat (chat-url)."
        )
        until(lambda: said in replies(slack))
    assert pd.paths()[-1] == "POST /incidents/PGR0VU2/notes" and chats.hosts() == [TEAMS, CHAT]


def test_no_client_channels_and_no_slack_channel_no_button(
    settings: Settings, fake_cluster: Any, sock: FakeSocket
) -> None:
    settings.slack_bot_token = SecretStr(TOKEN)
    slack, chats = FakeSlack(sock.url), Channels()
    with client(settings, fake_cluster, ONE, slack=routed(chats, slack), acme_channel=ACME) as c:
        tid = c.post(URL, json=ALERT, headers=HOOK).json()["investigation"]
        wait_for(c, tid, "done")
        slack.until(3)
        assert buttons(slack) == {}
        sock.send(click("send_to_client", json.dumps({"id": tid})))
        until(lambda: ephemerals(slack) == [("U0OPERATOR1", "Already sent, or nothing to send.")])
    assert chats.seen == [] and clientchannels.SHAPES.keys() == {"teams", "google_chat"}
