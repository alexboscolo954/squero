"""Protocol (slice 18.3): each part's number, the Hub accepting its own and the one before,
a 426 carrying the Hub's, the part saying which side to upgrade, an Agenda item for a refused
one, and an Actuator's version and Protocol in Health. Never another Contract's."""

import logging
import socket
from typing import Any
from unittest.mock import MagicMock

import anyio
import httpx
import pytest
import uvicorn
import websockets
from fastapi.testclient import TestClient
from fastmcp import FastMCP
from pydantic import SecretStr
from websockets.datastructures import Headers
from websockets.http11 import Response

from squero import actuator, agenda, protocol, relay
from squero.config import Settings
from squero.store import Store
from tests.test_actuator import A_TOKEN as ACT_A
from tests.test_actuator import B_TOKEN as ACT_B
from tests.test_actuator import actuated, make
from tests.test_actuator import hub as actuator_hub
from tests.test_agenda import _no_prometheus as _no_prometheus
from tests.test_agenda import contract
from tests.test_relay import A_TOKEN, B_TOKEN, relayed, settings_of, tools_server, until
from tests.test_relay import hub as relay_hub
from tests.test_sizing_store import NOW, row, stored
from tests.test_sizing_store import store as store

# --- the rule and its words -------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "n"),
    [
        (None, 1),
        ("2", 2),
        (" 3 ", 3),
        ("x", None),
        ("-1", None),
        ("1234567", None),
        ("", None),
        ("\u00b2", None),  # a digit to str.isdigit, not to int(): never a 500
    ],
)
def test_a_parts_header_is_its_protocol_and_none_is_todays(header: str | None, n: int) -> None:
    assert protocol.spoken(header) == n


@pytest.mark.parametrize(
    ("own", "said", "accepted"),
    [
        (5, 3, False),
        (5, 4, True),  # the one before
        (5, 5, True),
        (5, 6, False),
        (5, None, False),
        (1, 0, False),  # there is no Protocol 0
        (1, 1, True),
        (1, 2, False),
    ],
)
def test_the_hub_accepts_its_own_and_the_one_before(
    monkeypatch: pytest.MonkeyPatch, own: int, said: int | None, accepted: bool
) -> None:
    monkeypatch.setitem(protocol.OWN, "Actuator", own)
    assert protocol.accepts("Actuator", said) is accepted


def test_what_a_side_accepts_in_words() -> None:
    assert (protocol.needs(1), protocol.needs(2), protocol.needs(5)) == ("1", "1 or 2", "4 or 5")


def test_a_refusal_says_which_side_to_upgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(protocol.OWN, "Actuator", 5)
    monkeypatch.setitem(protocol.OWN, "Relay", 5)
    assert (
        protocol.refusal("Actuator", "eu", 3)
        == "Actuator in eu speaks Protocol 3, the Hub needs 4 or 5: upgrade it"
    )
    assert (
        protocol.refusal("Relay", "eu", 6)
        == "Relay in eu speaks Protocol 6, this Hub 5: upgrade the Hub"
    )
    assert (
        protocol.refusal("Relay", "eu", None)
        == "Relay in eu sends no readable Protocol, the Hub needs 4 or 5"
    )
    monkeypatch.setitem(protocol.OWN, "Actuator", 1)
    assert protocol.refusal("Actuator", "eu", 2).endswith("this Hub 1: upgrade the Hub")


def test_a_part_told_426_says_which_side_to_upgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(protocol.OWN, "Relay", 6)
    assert protocol.told("Relay", "5") == (
        "the Hub speaks Protocol 4 or 5, this Relay 6: upgrade the Hub"
    )
    monkeypatch.setitem(protocol.OWN, "Relay", 3)
    assert protocol.told("Relay", "5") == (
        "the Hub speaks Protocol 4 or 5, this Relay 3: upgrade this Relay"
    )
    assert protocol.told("Relay", None) == "the Hub refused this Relay's Protocol 3"  # not a Hub
    assert protocol.told("Relay", "x") == "the Hub refused this Relay's Protocol 3"


def test_what_a_part_says_is_noted_its_version_cut() -> None:
    seen = protocol.see("acme", "eu", "Actuator", "9", 10.0, "v" * 100)
    assert (seen.protocol, seen.at, seen.refused) == (9, 10.0, True)
    assert seen.version == "v" * 40
    assert protocol.SEEN[("acme", "eu", "Actuator")] is seen
    protocol.see("acme", "eu", "Actuator", None, 11.0)  # its next accepted call clears it
    assert protocol.SEEN[("acme", "eu", "Actuator")].refused is False


# --- the Relay ------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_relay_of_another_protocol_gets_426_after_its_token(
    settings: Settings, database_url: str
) -> None:
    app = relay_hub(settings, database_url, relayed("acme", A_TOKEN), relayed("bank", B_TOKEN))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    url = f"ws://127.0.0.1:{sock.getsockname()[1]}/relay"
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    relays: relay.Relays = app.state.relays
    async with anyio.create_task_group() as tg:
        tg.start_soon(lambda: server.serve(sockets=[sock]))
        await until(lambda: server.started)
        answers = []
        for headers in (
            {"Authorization": "Bearer nope", protocol.HEADER: "9"},  # the token first
            {"Authorization": f"Bearer {A_TOKEN}", protocol.HEADER: "9"},
            {"Authorization": f"Bearer {A_TOKEN}", protocol.HEADER: "x"},
        ):
            with pytest.raises(websockets.InvalidStatus) as e:
                await websockets.connect(url, additional_headers=headers)
            r = e.value.response
            answers.append((r.status_code, r.headers.get(protocol.HEADER)))
        assert answers == [(403, None), (426, "1"), (426, "1")]
        assert relays.live == {}
        old = await websockets.connect(
            url, additional_headers={"Authorization": f"Bearer {A_TOKEN}"}
        )
        await old.close()  # 0.62.0 sends no Protocol: 1, accepted
        await until(lambda: relays.live == {})
        assert not protocol.SEEN[("acme", "edge", "Relay")].refused
        protocol.see("acme", "edge", "Relay", "9", 0.0)
        assert list(protocol.SEEN) == [("acme", "edge", "Relay")]  # never the bad token's
        assert protocol.SEEN[("acme", "edge", "Relay")].refused
        tg.start_soon(relay.dial, url, A_TOKEN, tools_server())  # it sends 1: accepted
        await until(lambda: relays.get("acme", "edge") is not None)
        assert protocol.SEEN[("acme", "edge", "Relay")].protocol == 1
        assert not protocol.SEEN[("acme", "edge", "Relay")].refused
        server.should_exit = True
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_a_relay_told_426_says_which_side_to_upgrade(
    caplog: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(protocol.OWN, "Relay", 3)

    async def connect(url: str, token: str, server: Any) -> None:
        hub = Headers({protocol.HEADER: "5"})
        raise websockets.InvalidStatus(Response(426, "Upgrade Required", hub))

    async def sleep(seconds: float) -> None:
        raise anyio.get_cancelled_exc_class()

    with pytest.raises(anyio.get_cancelled_exc_class()):
        await relay.run(settings_of(), FastMCP("x"), connect, sleep, lambda: 0.0)
    assert "the Hub speaks Protocol 4 or 5, this Relay 3: upgrade this Relay" in caplog.text
    assert "check the token" not in caplog.text


def test_the_relay_sends_its_protocol() -> None:
    sent: dict[str, Any] = {}

    class Stop(Exception):
        pass

    def connect(url: str, additional_headers: dict[str, str], **_: Any) -> Any:
        sent.update(additional_headers)
        raise Stop

    with pytest.MonkeyPatch.context() as m:
        m.setattr(relay.websockets, "connect", connect)
        with pytest.raises(Stop):
            anyio.run(relay.dial, "ws://hub/relay", A_TOKEN, FastMCP("x"))
    assert sent == {"Authorization": f"Bearer {A_TOKEN}", protocol.HEADER: "1"}


# --- the Actuator ---------------------------------------------------------------------------


def as_actuator(token: str, **headers: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", **headers}


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/actuator/desired", None),
        ("POST", "/actuator/reports", []),
        ("POST", "/actuator/blockers", {"pods": [], "namespaces": []}),
    ],
)
def test_an_actuator_of_another_protocol_gets_426_on_every_call(
    settings: Settings, database_url: str, method: str, path: str, body: Any
) -> None:
    app = actuator_hub(settings, database_url, actuated("acme", ACT_A), actuated("bank", ACT_B))
    with TestClient(app) as c:
        newer = c.request(
            method, path, json=body, headers=as_actuator(ACT_A, **{protocol.HEADER: "2"})
        )
        wrong = c.request(
            method, path, json=body, headers=as_actuator(ACT_A + "x", **{protocol.HEADER: "2"})
        )
    assert newer.status_code == 426
    assert newer.headers[protocol.HEADER] == "1"
    assert newer.json()["detail"] == "this Hub speaks Protocol 1"
    assert wrong.status_code == 401  # the token first: nothing noted of it
    assert list(protocol.SEEN) == [("acme", "test", "Actuator")]


def test_an_actuators_version_and_protocol_are_noted_and_the_hubs_sent_back(
    settings: Settings, database_url: str
) -> None:
    app = actuator_hub(settings, database_url, actuated("acme", ACT_A))
    with TestClient(app) as c:
        old = c.get("/actuator/desired", headers=as_actuator(ACT_A))  # 0.62.0: says nothing
        assert protocol.SEEN[("acme", "test", "Actuator")].version is None
        new = c.get(
            "/actuator/desired",
            headers=as_actuator(ACT_A, **{protocol.HEADER: "1", protocol.VERSION: "0.63.0"}),
        )
        seen = protocol.SEEN[("acme", "test", "Actuator")]
        reported = c.post("/actuator/reports", json=[], headers=as_actuator(ACT_A))
        told = {"at": 1.0, "namespaces": ["shop"], "pods": []}
        blocking = c.post("/actuator/blockers", json=told, headers=as_actuator(ACT_A))
    assert old.status_code == new.status_code == 200
    assert old.headers[protocol.HEADER] == new.headers[protocol.HEADER] == "1"
    assert reported.status_code == blocking.status_code == 204  # every answer carries it
    assert reported.headers[protocol.HEADER] == blocking.headers[protocol.HEADER] == "1"
    assert (seen.protocol, seen.version, seen.refused) == (1, "0.63.0", False)


def test_the_actuator_sends_its_protocol_and_version() -> None:
    import importlib.metadata

    assert actuator.hub_headers(SecretStr(ACT_A)) == {
        "Authorization": f"Bearer {ACT_A}",
        protocol.HEADER: "1",
        protocol.VERSION: importlib.metadata.version("squero"),
    }


def refusing(code: int) -> httpx.Client:
    def answer(r: httpx.Request) -> httpx.Response:
        return httpx.Response(code, headers={protocol.HEADER: "2"}, json={"detail": "no"})

    return httpx.Client(base_url="https://hub", transport=httpx.MockTransport(answer))


def test_an_actuator_told_426_keeps_its_reports_and_blockers_and_says_why(
    caplog: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_actuator import actuation

    caplog.set_level(logging.INFO)
    a = make(refusing(426), MagicMock(), MagicMock())
    a.unsent = [actuation(1), actuation(2)]
    a.report()
    assert [x.run_id for x in a.unsent] == [1, 2]  # sent again once one side is upgraded
    assert a.pull() is None
    assert "the Hub speaks Protocol 1 or 2, this Actuator 1: upgrade this Actuator" in caplog.text
    a = make(refusing(422), MagicMock(), MagicMock())  # a verdict on what was sent: dropped
    a.unsent = [actuation(1)]
    a.report()
    assert a.unsent == []
    assert actuator.RESEND == (401, 403, 426, 429)


# --- the Agenda and Health ------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_refused_part_is_on_its_own_contracts_agenda_after_stale_data(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def bills(*_: Any, **__: Any) -> list[dict[str, Any]]:
        span = {"start": "2026-11-01", "end": "2026-11-07"}
        return [{"cluster": "edge", "within": False, "gap": 0.2, **span}]

    monkeypatch.setattr("squero.billing.report", bills)
    c = contract("acme")  # clusters test and edge, read from Prometheus
    protocol.see("acme", "test", "Actuator", "7", NOW - 60, "0.70.0")  # refused
    protocol.see("acme", "edge", "Relay", "1", NOW)  # accepted: nothing to do
    protocol.see("acme", "gone", "Actuator", "7", NOW)  # no longer the Contract's
    protocol.see("bank", "test", "Actuator", "7", NOW)  # B's never on A's
    await stored(store, "acme", NOW, {"test": "Prometheus unreachable"}, row())  # stale data
    a = await agenda.of(store, c, NOW)
    got = [(i.kind, i.cluster) for i in a.items]
    assert got == [("stale", "test"), ("protocol", "test"), ("bill", "edge")]
    item = a.items[1]
    assert item.title == "Actuator in test speaks Protocol 7, this Hub 1: upgrade the Hub"
    assert item.at == NOW - 60
    assert agenda.counts(a) == {"stale": 1, "protocol": 1, "bill": 1}
    b = await agenda.of(store, contract("bank"), NOW)
    assert [(i.kind, i.cluster) for i in b.items] == [("protocol", "test"), ("bill", "edge")]


def test_health_shows_each_actuators_version_and_protocol(
    settings: Settings, database_url: str
) -> None:
    from squero import agent
    from squero.api import create_app
    from tests.conftest import scripted
    from tests.test_console import OPERATOR, VIEW
    from tests.test_relay import local

    settings.database_url = SecretStr(database_url)
    settings.console_operators = {OPERATOR}
    settings.approver_token = SecretStr("approver-token")
    by_id = {c.id: c for c in (actuated("acme", ACT_A), actuated("bank", ACT_B))}

    def mcp(c: Any) -> agent.Toolbox:
        return agent.build_mcp(c, settings, k8s_apis=local(), relays=relay.HUB)

    def lines(c: TestClient, cid: str) -> dict[str, Any]:
        got = c.get(f"/api/v1/contracts/{cid}/health", headers=VIEW).json()["integrations"]
        return {p["name"]: p for p in got}

    with TestClient(create_app(settings, scripted(), mcp, by_id)) as c:
        unseen = lines(c, "acme")["actuator test"]
        protocol.see("acme", "test", "Actuator", "1", 1700000600.0, "0.63.0")
        seen = lines(c, "acme")["actuator test"]
        theirs = lines(c, "bank")["actuator test"]  # A's Actuator is never B's
        protocol.see("acme", "test", "Actuator", "3", 1700000600.0, "0.70.0")
        every = lines(c, "acme")
        refused = every["actuator test"]
    assert unseen["ok"] is True
    assert unseen["detail"].startswith("not seen since the Hub started at ")
    assert seen == {
        "name": "actuator test",
        "ok": True,
        "ms": 0,
        "detail": "seen at 2023-11-14 22:23 UTC, version 0.63.0, Protocol 1",
    }
    assert theirs["detail"].startswith("not seen since the Hub started at ")
    assert refused["ok"] is False
    assert refused["detail"] == (
        "seen at 2023-11-14 22:23 UTC, version 0.70.0; "
        "Actuator in test speaks Protocol 3, this Hub 1: upgrade the Hub"
    )
    assert "actuator edge" not in every  # edge runs no Actuator


def test_health_shows_a_connected_relays_protocol(settings: Settings, database_url: str) -> None:
    from squero import agent
    from squero.api import create_app
    from tests.conftest import scripted
    from tests.test_console import OPERATOR, VIEW
    from tests.test_relay import local

    settings.database_url = SecretStr(database_url)
    settings.console_operators = {OPERATOR}
    settings.approver_token = SecretStr("approver-token")
    by_id = {"acme": relayed("acme", A_TOKEN)}

    def mcp(c: Any) -> agent.Toolbox:
        return agent.build_mcp(c, settings, k8s_apis=local(), relays=relay.HUB)

    async def close() -> None:
        pass

    relay.HUB.live[("acme", "edge")] = relay.Live(None, close, 1700000600.0, None, "0.63.0")  # type: ignore[arg-type]
    protocol.see("acme", "edge", "Relay", "1", 1700000600.0)
    try:
        with TestClient(create_app(settings, scripted(), mcp, by_id)) as c:
            got = c.get("/api/v1/contracts/acme/health", headers=VIEW).json()["integrations"]
    finally:
        del relay.HUB.live[("acme", "edge")]
    (line,) = [p for p in got if p["name"] == "relay edge"]
    assert line["detail"] == "connected since 2023-11-14 22:23 UTC, version 0.63.0, Protocol 1"
