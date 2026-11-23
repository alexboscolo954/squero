"""Client channels beside Slack, on their own (slice 19.4): the text made safe for a chat
product (no link, no mention, whatever it parses), each product's documented body, cut to
its size by bytes, one request with its outcome by status only, and the Contract's rules:
a URL that is its product's, never quoted, and never one channel in two Contracts."""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from squero import cli, clientchannels, contracts
from squero.models import Investigation, TriageReport
from squero.slack import destinations, parent_blocks
from tests.conftest import REPORT as TRIAGE
from tests.conftest import make_contract, use_age_keys
from tests.test_contracts import BASE, encrypt, problems, write
from tests.test_shared_stores import HERE

TEAMS_URL = (
    "https://prod-07.westus.logic.azure.com:443/workflows/0a1b2c3d4e5f/triggers/manual/paths/"
    "invoke?api-version=2016-06-01&sp=%2Ftriggers%2Fmanual%2Frun&sv=1.0&sig=TEAMS-SIGNATURE"
)
CHAT_URL = (
    "https://chat.googleapis.com/v1/spaces/AAAAabcdEFG/messages?key=CHAT-KEY&token=CHAT-TOKEN"
)
CHANNELS = [
    {"kind": "teams", "url_secret": "teams-url"},
    {"kind": "google_chat", "url_secret": "chat-url"},
]
SECRETS = {"teams-url": TEAMS_URL, "chat-url": CHAT_URL}
WIDE = "\uff1c"  # the full-width look-alike a `<` becomes
REPORT = (
    "Checkout is failing: 5xx > 5%\n\nRoot cause (high confidence): a bad rollout\n1. Roll back"
)


def test_a_url_can_never_become_a_link() -> None:
    cases = {
        "see https://grafana.acme.example/d/1?x=1.5 now": (
            "see https[:]//grafana[.]acme[.]example/d/1?x=1[.]5 now"
        ),
        "open http://evil.example.": "open http[:]//evil[.]example.",  # its full stop stays one
        "at www.evil.example/login, then": "at www[.]evil[.]example/login, then",
        "from grafana.acme.example/d/1": "from grafana[.]acme[.]example/d/1",
        "[click here](https://evil.example/x?d=secret)": (
            "[click here](https[:]//evil[.]example/x?d=secret)"
        ),
        "two: https://a.example/1 and https://b.example/2": (
            "two: https[:]//a[.]example/1 and https[:]//b[.]example/2"
        ),
    }
    for text, made in cases.items():
        assert clientchannels.safe(text) == made
        assert "://" not in made


def test_nothing_can_open_a_mention_or_a_chat_link() -> None:
    assert clientchannels.safe("ping <users/all> now") == f"ping {WIDE}users/all> now"
    linked = clientchannels.safe("<https://evil.example/x|your dashboard>")
    assert linked == f"{WIDE}https[:]//evil[.]example/x|your dashboard>"
    assert clientchannels.safe("p99 < 200ms, 5xx > 5% & rising") == (
        f"p99 {WIDE} 200ms, 5xx > 5% & rising"  # the others open nothing: left as they are
    )
    for text in ("<users/all>", "<<a>>", "a<b", "<https://x.example|y>"):
        assert "<" not in clientchannels.safe(text)
    plain = "Raise the memory limit to 512Mi (was 64Mi). It restarted 3 times."
    assert clientchannels.safe(plain) == plain  # nothing else is touched


def test_teams_gets_the_documented_card_a_wrapping_block_per_line() -> None:
    assert clientchannels.teams(REPORT) == {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.2",
                    "body": [
                        {
                            "type": "TextBlock",
                            "text": "Checkout is failing: 5xx > 5%",
                            "wrap": True,
                            "weight": "Bolder",  # by its weight, never by markup
                        },
                        {
                            "type": "TextBlock",
                            "text": "Root cause (high confidence): a bad rollout",
                            "wrap": True,
                        },
                        {"type": "TextBlock", "text": "1. Roll back", "wrap": True},
                    ],
                },
            }
        ],
    }
    assert clientchannels.teams("")["attachments"][0]["content"]["body"] == []
    assert clientchannels.google_chat(REPORT) == {"text": REPORT}


@pytest.mark.parametrize(("kind", "limit"), [("teams", 24_000), ("google_chat", 28_000)])
def test_a_long_report_is_cut_by_its_bytes_to_what_the_product_takes(kind: str, limit: int) -> None:
    small = clientchannels.body(kind, REPORT)
    assert json.loads(small) == clientchannels.SHAPES[kind](REPORT) and b"[cut" not in small
    long = "Résumé: " + "é" * 60_000  # two bytes each: 20,000 characters would not fit
    cut = clientchannels.body(kind, long)
    assert len(cut) <= limit < len(json.dumps(clientchannels.SHAPES[kind](long)).encode())
    assert len(cut) > limit * 0.8  # cut to fit, not to nothing
    text = json.dumps(json.loads(cut), ensure_ascii=False)
    assert "[cut: the full Report is with your Operator]" in text and "Résumé: é" in text
    assert clientchannels.MAX_BYTES == {"teams": 24_000, "google_chat": 28_000}


def answering(reply: Any, seen: list[httpx.Request]) -> httpx.AsyncClient:
    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(r)
        if isinstance(reply, Exception):
            raise reply
        return reply  # type: ignore[no-any-return]

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize(("kind", "url"), [("teams", TEAMS_URL), ("google_chat", CHAT_URL)])
@pytest.mark.parametrize("status", [200, 202, 204])
@pytest.mark.anyio
async def test_the_report_is_one_post_of_the_products_body_to_its_url(
    kind: str, url: str, status: int
) -> None:
    seen: list[httpx.Request] = []
    text = "Checkout: see https://evil.example/x <users/all>"
    await clientchannels.send(answering(httpx.Response(status), seen), kind, SecretStr(url), text)
    (r,) = seen
    # the URL whole, its key and its token (a default :443 is dropped on the way)
    assert (r.method, str(r.url)) == ("POST", url.replace(":443", ""))
    assert r.headers["content-type"] == "application/json; charset=UTF-8"
    assert "authorization" not in r.headers  # a Teams workflow that takes "Anyone" wants none
    assert r.content == clientchannels.body(kind, text)
    sent = r.content.decode()
    assert "evil[.]example" in sent and "://" not in sent.replace("http://adaptivecards.io", "")
    assert "<users/all>" not in sent and r.extensions["timeout"]["read"] == 10


DENIED = {
    "error": {"code": 403, "message": f"bad token in {CHAT_URL}", "status": "PERMISSION_DENIED"}
}


@pytest.mark.parametrize(
    ("reply", "error", "said"),
    [
        (httpx.Response(400, text="Bad card"), clientchannels.Refused, "HTTP 400"),
        (httpx.Response(401), clientchannels.Refused, "HTTP 401"),
        (httpx.Response(403, json=DENIED), clientchannels.Refused, "HTTP 403 PERMISSION_DENIED"),
        (
            httpx.Response(404, json={"error": {"status": "see https://evil.example"}}),
            clientchannels.Refused,
            "HTTP 404",  # a word that isn't one is not repeated
        ),
        (httpx.Response(404, json={"error": {"status": 7}}), clientchannels.Refused, "HTTP 404"),
        (httpx.Response(413, json=["x"]), clientchannels.Refused, "HTTP 413"),
        (httpx.Response(429, json={"error": {"status": "RESOURCE_EXHAUSTED"}}),
         clientchannels.Refused, "HTTP 429 RESOURCE_EXHAUSTED"),
        (httpx.Response(302, headers={"location": "https://evil.example/"}),
         clientchannels.Refused, "HTTP 302"),
        (httpx.Response(199), clientchannels.Refused, "HTTP 199"),
        (httpx.ConnectError(f"no route to {CHAT_URL}"), clientchannels.Refused,
         "could not connect (ConnectError)"),
        (httpx.ConnectTimeout("slow"), clientchannels.Refused,
         "could not connect (ConnectTimeout)"),
        # it may be posted: never offered again
        (httpx.Response(500, json=DENIED), clientchannels.Unknown, "HTTP 500"),
        (httpx.Response(503), clientchannels.Unknown, "HTTP 503"),
        (httpx.ReadTimeout(f"slow {CHAT_URL}"), clientchannels.Unknown, "ReadTimeout"),
        (httpx.RemoteProtocolError("cut"), clientchannels.Unknown, "RemoteProtocolError"),
    ],
)  # fmt: skip
@pytest.mark.anyio
async def test_a_refusal_can_be_retried_one_unanswered_may_be_there_by_status_only(
    reply: Any, error: type[Exception], said: str
) -> None:
    seen: list[httpx.Request] = []
    with pytest.raises(error) as e:
        await clientchannels.send(answering(reply, seen), "google_chat", SecretStr(CHAT_URL), "x")
    assert str(e.value) == said
    assert len(seen) == 1  # a redirect is never followed, and nothing is tried twice
    for secret in ("CHAT-TOKEN", "CHAT-KEY", "AAAAabcdEFG", "evil.example"):
        assert secret not in str(e.value)


@pytest.mark.anyio
async def test_a_channel_has_ten_seconds_in_all(monkeypatch: pytest.MonkeyPatch) -> None:
    assert clientchannels.SECONDS == 10
    monkeypatch.setattr(clientchannels, "SECONDS", 0.05)

    async def slow(r: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200)

    http = httpx.AsyncClient(transport=httpx.MockTransport(slow))
    with pytest.raises(clientchannels.Unknown, match="TimeoutError"):
        await asyncio.wait_for(
            clientchannels.send(http, "teams", SecretStr(TEAMS_URL), "x"), timeout=2
        )


def test_a_contract_lists_its_client_channels_each_named_by_its_secret() -> None:
    c = make_contract(client_channels=CHANNELS, secrets=SECRETS)
    assert [ch.label for ch in c.client_channels] == ["Teams (teams-url)", "Google Chat (chat-url)"]
    assert {"teams-url", "chat-url"} <= c.secret_refs()  # each checked at load
    assert make_contract().client_channels == []
    for bad, error in (
        ({"kind": "slack", "url_secret": "x"}, "kind"),
        ({"kind": "teams"}, "url_secret"),
        ({"kind": "teams", "url": TEAMS_URL}, "url"),  # a plain URL is refused, not ignored
        ({"kind": "teams", "url_secret": "x", "name": "ops"}, "name"),
    ):
        with pytest.raises(ValidationError, match=error):
            make_contract(client_channels=[bad], secrets=SECRETS)


BROKEN = "its URL has a space or a line break in it"
CHAT_FORM = (
    "its URL must be a Google Chat space's webhook: "
    "https://chat.googleapis.com/v1/spaces/<space>/messages?key=...&token=..."
)


@pytest.mark.parametrize(
    ("kind", "url", "refusal"),
    [
        ("google_chat", CHAT_URL, None),
        ("google_chat", f" {CHAT_URL}\n", None),
        ("google_chat", CHAT_URL.replace("https", "http"), CHAT_FORM),
        ("google_chat", CHAT_URL.replace("googleapis.com", "googleapis.com.evil.io"), CHAT_FORM),
        ("google_chat", CHAT_URL.replace("chat.", "evil-chat."), CHAT_FORM),
        ("google_chat", CHAT_URL.replace("/v1/spaces/", "/v1/rooms/"), CHAT_FORM),
        ("google_chat", CHAT_URL.replace("/messages", "/messages/x"), CHAT_FORM),
        ("google_chat", CHAT_URL.split("?")[0], CHAT_FORM),  # no key and no token
        ("google_chat", CHAT_URL.split("&token")[0], CHAT_FORM),
        ("google_chat", CHAT_URL.replace("https://", "https://u:p@"), CHAT_FORM),
        ("google_chat", TEAMS_URL, CHAT_FORM),
        ("google_chat", "", CHAT_FORM),
        ("teams", TEAMS_URL, None),
        ("teams", "https://tenant.environment.api.example/workflows/x?sig=y", None),  # any host
        ("teams", TEAMS_URL.replace("https", "http"), "its URL must be an https:// URL"),
        (
            "teams",
            "https://u:p@prod.logic.azure.com/x",
            "its URL must not carry a user or a password",
        ),
        ("teams", "", "its URL must be an https:// URL"),
        # a long URL wrapped in the secrets file: the load says so, not the first send
        ("teams", TEAMS_URL.replace("/triggers", "\n/triggers"), BROKEN),
        ("teams", TEAMS_URL.replace("&sv", "\t&sv"), BROKEN),
        ("teams", TEAMS_URL.replace("/triggers", " /triggers"), BROKEN),
        ("google_chat", CHAT_URL.replace("&token", "\n&token"), CHAT_FORM),
    ],
)
def test_a_channels_url_is_its_products_and_a_refusal_never_quotes_it(
    kind: str, url: str, refusal: str | None
) -> None:
    assert contracts.client_channel_refusal(kind, url) == refusal


def test_an_outbound_webhooks_url_with_a_line_break_is_refused_too() -> None:
    assert contracts.webhook_refusal("https://hooks.acme.example/in\n/9f8e") == BROKEN
    assert contracts.webhook_refusal("https://hooks.acme.example/in\x00") == BROKEN
    assert contracts.webhook_refusal("\n https://hooks.acme.example/in \n") is None  # around it


@pytest.mark.anyio
async def test_a_url_the_http_client_refuses_is_a_refusal_never_a_crash() -> None:
    seen: list[httpx.Request] = []
    broken = SecretStr(TEAMS_URL.replace("/triggers", "\n/triggers"))
    with pytest.raises(clientchannels.Refused) as e:
        await clientchannels.send(answering(httpx.Response(200), seen), "teams", broken, "x")
    assert str(e.value) == "its URL is not a URL" and seen == []


def test_the_confirm_dialog_names_what_fits_in_slacks_300_characters() -> None:
    few = ["to the client channel C0ACMECLI01", "to Teams (teams-url)"]
    assert destinations(few, 265) == "to the client channel C0ACMECLI01 and to Teams (teams-url)"
    names = [f"to Teams (teams-acme-oncall-rotation-{n}-of-the-platform-group)" for n in range(6)]
    said = destinations(names, 265)
    assert len(said) <= 265 and said.endswith(" and 3 more")  # three named, three counted
    assert said.startswith(names[0]) and names[2] in said and names[3] not in said
    assert len(" and ".join([*names[:4], "2 more"])) > 265  # a fourth would not fit
    assert destinations(["x" * 400], 265) == "x" * 265  # one name longer than the dialog
    assert destinations([], 265) == ""
    t = Investigation(
        id="i-1",
        contract_id="acme",
        title="KubePodCrashLooping shop",
        started=0,
        status="done",
        report=TriageReport(**TRIAGE),
    )
    labels = [n.removeprefix("to ") for n in names]
    blocks = parent_blocks(t, True, "C0ACMECLI01", None, True, labels)
    (send,) = [e for b in blocks if b["type"] == "actions" for e in b["elements"]]
    text = send["confirm"]["text"]["text"]
    assert len(text) <= 300 and text.endswith(" more.")  # over it, Slack refuses the message
    assert text.startswith(
        "The Report, without Evidence, goes to the client channel C0ACMECLI01 and on its "
        "PagerDuty incident as a note and to Teams (teams-acme-oncall-rotation-0"
    )


def test_one_channel_is_never_two_contracts_nor_twice_ones() -> None:
    def named(cid: str, **secrets: str) -> contracts.Contract:
        return make_contract(
            contract_id=cid, client_channels=CHANNELS, secrets={**SECRETS, **secrets}
        )

    other = CHAT_URL.replace("AAAAabcdEFG", "BBBBabcdEFG")
    a = named("one")
    b = named("two", **{"teams-url": TEAMS_URL + "2", "chat-url": f" {CHAT_URL} "})  # as written
    c = named("three", **{"teams-url": TEAMS_URL + "3", "chat-url": other})
    twice = named("four", **{"teams-url": TEAMS_URL + "4", "chat-url": TEAMS_URL + "4"})
    found = contracts._shared_client_channels(HERE, [a, b, c, twice, make_contract()])
    assert found == [
        "contracts: one, two share a client channel's URL; one channel per Contract, once",
        "contracts: four, four share a client channel's URL; one channel per Contract, once",
    ]
    assert "CHAT-TOKEN" not in "".join(found)
    assert contracts._shared_client_channels(HERE, [a, c]) == []


def test_a_channels_url_is_checked_when_the_contracts_load_never_quoted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    use_age_keys(monkeypatch, tmp_path, "demo-shop")
    root = tmp_path / "contracts"
    path = write(root, {**BASE, "id": "demo-shop", "client_channels": CHANNELS})
    wrong = {"teams-url": TEAMS_URL, "chat-url": CHAT_URL.replace("chat.", "evil.")}
    encrypt(path.parent / "secrets.sops.yaml", wrong)
    found = problems(root)
    assert any(p.endswith(f"client_channels.1: {CHAT_FORM}") for p in found), found
    assert not any("client_channels.0" in p for p in found)
    assert "CHAT-TOKEN" not in "\n".join(found)
    encrypt(path.parent / "secrets.sops.yaml", {"teams-url": TEAMS_URL})  # the other missing
    missing = problems(root)
    assert any("secret 'chat-url' is not in" in p for p in missing)
    assert not any("client_channels" in p for p in missing)  # said once, as a missing secret
    encrypt(path.parent / "secrets.sops.yaml", SECRETS)
    assert len(contracts.load(root)["demo-shop"].client_channels) == 2


def test_two_contracts_with_one_channel_are_refused_when_they_load(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    use_age_keys(monkeypatch, tmp_path, "demo-shop", "demo-bank")
    root = tmp_path / "contracts"
    only_chat = [CHANNELS[1]]
    for cid in ("demo-shop", "demo-bank"):
        path = write(root, {**BASE, "id": cid, "client_channels": only_chat})
        encrypt(path.parent / "secrets.sops.yaml", {"chat-url": CHAT_URL}, cid)
    found = problems(root)
    assert any("demo-bank, demo-shop share a client channel's URL" in p for p in found), found
    assert "CHAT-TOKEN" not in "\n".join(found)


@pytest.mark.anyio
async def test_the_http_librarys_own_log_line_never_prints_a_url(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """httpx logs every request's whole URL at INFO, and these URLs hold a key (slices
    19.3 and 19.4): the Hub keeps that logger at WARNING, whatever the root is set to."""
    hub, client = logging.getLogger("squero"), logging.getLogger("httpx")
    before = (hub.handlers[:], hub.level, client.level)  # a setup left behind would hide
    try:  # the next one's handler: the Hub adds its own only where there is none
        cli.hub_logging()
        assert client.level == logging.WARNING
        with caplog.at_level(logging.INFO):  # the root at INFO, as a careless setup would
            http = answering(httpx.Response(200), [])
            await clientchannels.send(http, "google_chat", SecretStr(CHAT_URL), "x")
    finally:
        hub.handlers[:], hub.level = before[:2]
        client.setLevel(before[2])
    assert "CHAT-TOKEN" not in caplog.text and "chat.googleapis.com" not in caplog.text
