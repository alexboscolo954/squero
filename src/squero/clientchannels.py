"""Client channels beside Slack (slice 19.4): Microsoft Teams and Google Chat, through
each product's own incoming webhook. They get the Client report when the Operator sends
it, and nothing else: no buttons, no other message, never on their own.

The report is the model's own prose, and neither product documents a way to keep a URL
from becoming a link or `<users/all>` from pinging a space. So the text is rewritten in
the two ways that can't be parsed back, whatever the product does: a URL's dots and colon
in brackets, and `<` as its full-width look-alike. A URL holds a key: only the status of
a reply is ever shown, never its text or an error's."""

import asyncio
import json
import re
from typing import Any

import httpx
from pydantic import SecretStr

from squero.slack import TRAILING, URL

SECONDS = 10.0  # one destination, in all: a press sends to several in a row
# What each product takes (Teams: 28 KB; Chat: 32,000 bytes), with room for its envelope
MAX_BYTES = {"teams": 24_000, "google_chat": 28_000}
CUT = "\n[cut: the full Report is with your Operator]"
WIDE_LESS = "\uff1c"  # FULLWIDTH LESS-THAN SIGN: reads as `<`, opens no syntax
CODE = re.compile(r"[A-Z][A-Z_]{2,40}")  # Google's status word, such as PERMISSION_DENIED


class Refused(Exception):
    """The channel answered no: nothing was posted, a retry may work."""


class Unknown(Exception):
    """No answer (timeout, connection lost): the channel may have the report."""


def safe(text: str) -> str:
    """The text with nothing a chat product can turn into a link or a mention: every URL
    with its dots and its colon in brackets (grafana[.]acme[.]example), and every `<` as
    its full-width look-alike (Chat's link and mention syntax opens with one).
    ponytail: as in Slack, a bare domain with no path (x.evil.com) is not a URL here."""

    def defang(m: re.Match[str]) -> str:
        url = m[0].rstrip(TRAILING)
        return url.replace("://", "[:]//").replace(".", "[.]") + m[0][len(url) :]

    return URL.sub(defang, text).replace("<", WIDE_LESS)


def teams(text: str) -> dict[str, Any]:
    """A Workflows webhook's body: one Adaptive Card, a wrapping text block per line (a
    block folds single line breaks, and clips to one line unless it wraps), the first
    one bold by its weight, not by markup."""
    blocks: list[dict[str, Any]] = [
        {"type": "TextBlock", "text": line, "wrap": True}
        for line in text.split("\n")
        if line.strip()
    ]
    if blocks:
        blocks[0]["weight"] = "Bolder"
    card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.2",
        "body": blocks,
    }
    attachment = {
        "contentType": "application/vnd.microsoft.card.adaptive",
        "contentUrl": None,
        "content": card,
    }
    return {"type": "message", "attachments": [attachment]}


def google_chat(text: str) -> dict[str, Any]:
    return {"text": text}


SHAPES = {"teams": teams, "google_chat": google_chat}


def body(kind: str, text: str) -> bytes:
    """The request's bytes for this product, the text made safe and cut until they fit."""
    shown, cut = safe(text), ""
    while True:
        encoded = json.dumps(SHAPES[kind](shown + cut), ensure_ascii=False).encode()
        if len(encoded) <= MAX_BYTES[kind] or not shown:
            return encoded
        shown, cut = shown[: int(len(shown) * 0.9)], CUT


async def send(http: httpx.AsyncClient, kind: str, url: SecretStr, text: str) -> None:
    """The Client report to one channel. Refused: nothing was posted. Unknown: it may
    have been. A Teams workflow's 2xx says it took the request, not that it posted."""
    try:
        async with asyncio.timeout(SECONDS):
            resp = await http.post(
                url.get_secret_value().strip(),
                content=body(kind, text),
                headers={"Content-Type": "application/json; charset=UTF-8"},
                timeout=SECONDS,
                follow_redirects=False,
            )
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:  # nothing was sent
        raise Refused(f"could not connect ({type(e).__name__})") from None
    except httpx.InvalidURL:  # not an HTTPError: checked at load, never sent here either
        raise Refused("its URL is not a URL") from None
    except (httpx.HTTPError, TimeoutError) as e:
        raise Unknown(type(e).__name__) from None  # never its text: it can quote the URL
    if resp.status_code >= 500:  # a gateway timeout or a late failure: maybe posted
        raise Unknown(f"HTTP {resp.status_code}")
    if not 200 <= resp.status_code < 300:
        try:
            word = resp.json()["error"]["status"]
        except (ValueError, KeyError, TypeError):
            word = None
        named = f" {word}" if isinstance(word, str) and CODE.fullmatch(word) else ""
        raise Refused(f"HTTP {resp.status_code}{named}")
