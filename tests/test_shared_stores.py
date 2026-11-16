"""One Prometheus under two names (slice A1): the Hub asks each store who it is, and
a store that can't say is compared by the addresses its host resolves to."""

import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from squero import contracts, usage
from tests.conftest import make_contract

HERE = Path("contracts")


def contract(cid: str, url: str, namespaces: list[str], **extra: Any) -> contracts.Contract:
    data = make_contract(contract_id=cid, clusters={"main": namespaces}).model_dump()
    data["clusters"][0].update(prometheus_url=url, prometheus_token_secret="tok", **extra)
    return contracts.build(data, secrets={"tok": SecretStr(f"{cid}-secret")})


def stores(answers: dict[str, Any], seen: list[str] | None = None) -> httpx.Client:
    """Each host's reply to runtimeinfo: a startTime, an HTTP status, or a body."""

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path.endswith("/api/v1/status/runtimeinfo")
        if seen is not None:
            seen.append(f"{req.url.host} {req.headers.get('authorization', '-')}")
        got = answers[req.url.host]
        if isinstance(got, int):
            return httpx.Response(got, json={"status": "error"})
        if isinstance(got, str):
            return httpx.Response(200, json={"status": "success", "data": {"startTime": got}})
        return httpx.Response(200, json=got)

    return httpx.Client(transport=httpx.MockTransport(handler))


def dns(table: dict[str, set[str]]) -> Any:
    return lambda host, port: table.get(host, set())


def ask(cs: list[contracts.Contract], answers: dict[str, Any], table: Any = None) -> None:
    usage.ask_stores(HERE, {c.id: c for c in cs}, stores(answers), dns(table or {}))


T1, T2 = "2026-11-16T10:00:00.123456789Z", "2026-11-16T10:00:00.987654321Z"


def test_two_names_one_identity_with_namespaces_in_common_refuse_the_contracts() -> None:
    a = contract("a", "http://prom-a", ["shop"])
    b = contract("b", "http://10.0.0.5:80/", ["shop"])
    with pytest.raises(contracts.ContractError) as e:
        ask([a, b], {"prom-a": T1, "10.0.0.5": T1})
    assert "a/main and b/main read the same Prometheus" in e.value.problems[0]


def test_two_names_one_identity_in_other_namespaces_are_marked_shared() -> None:
    a, b = contract("a", "http://prom-a", ["shop"]), contract("b", "http://prom-b", ["bank"])
    ask([a, b], {"prom-a": T1, "prom-b": T1})
    assert a.shared_stores == {"main"} and b.shared_stores == {"main"}


def test_two_identities_behind_one_address_stay_apart() -> None:
    """One ingress address fronting two stores: both said who they are, and differ."""
    a, b = contract("a", "http://prom-a", ["shop"]), contract("b", "http://prom-b", ["shop"])
    ask([a, b], {"prom-a": T1, "prom-b": T2}, {"prom-a": {"10.0.0.5"}, "prom-b": {"10.0.0.5"}})
    assert not a.shared_stores and not b.shared_stores


def test_a_store_that_cant_say_is_compared_by_address_and_only_marked() -> None:
    a, b = contract("a", "http://prom-a", ["shop"]), contract("b", "http://prom-b", ["shop"])
    ask([a, b], {"prom-a": 404, "prom-b": T1}, {"prom-a": {"10.0.0.5"}, "prom-b": {"10.0.0.5"}})
    assert a.shared_stores == {"main"} and b.shared_stores == {"main"}


@pytest.mark.parametrize(
    ("url_b", "table"),
    [
        ("http://prom-b", {"prom-a": {"10.0.0.5"}, "prom-b": {"10.0.0.6"}}),  # other address
        ("http://prom-b/other", {"prom-a": {"10.0.0.5"}, "prom-b": {"10.0.0.5"}}),  # other path
        ("http://prom-b:9090", {"prom-a": {"10.0.0.5"}, "prom-b": {"10.0.0.5"}}),  # other port
        ("https://prom-b:80", {"prom-a": {"10.0.0.5"}, "prom-b": {"10.0.0.5"}}),  # other scheme
        ("http://prom-b", {}),  # neither resolves
    ],
)
def test_stores_that_cant_say_and_differ_in_address_port_path_or_scheme_stay_apart(
    url_b: str, table: dict[str, set[str]]
) -> None:
    a, b = contract("a", "http://prom-a", ["shop"]), contract("b", url_b, ["shop"])
    ask([a, b], {"prom-a": 404, "prom-b": 503}, table)
    assert not a.shared_stores and not b.shared_stores


def test_one_address_in_common_is_enough() -> None:
    a, b = contract("a", "http://prom-a", ["shop"]), contract("b", "http://prom-b", ["bank"])
    ask(
        [a, b],
        {"prom-a": 404, "prom-b": 404},
        {"prom-a": {"1.1.1.1", "2.2.2.2"}, "prom-b": {"2.2.2.2"}},
    )
    assert a.shared_stores == {"main"}


@pytest.mark.parametrize(
    "reply",
    [
        {"status": "success", "data": {}},
        {"status": "success", "data": {"startTime": ""}},
        {"status": "success", "data": {"startTime": 5}},
        {"status": "success"},
        ["not", "a", "dict"],
    ],
)
def test_a_reply_without_a_start_time_cant_say(reply: Any) -> None:
    """Either store answering oddly falls back to addresses, never to a shared identity."""
    a, b = contract("a", "http://prom-a", ["shop"]), contract("b", "http://prom-b", ["shop"])
    ask(
        [a, b], {"prom-a": reply, "prom-b": reply}, {"prom-a": {"10.0.0.5"}, "prom-b": {"10.0.0.6"}}
    )
    assert not a.shared_stores


def test_a_non_json_reply_cant_say() -> None:
    a, b = contract("a", "http://prom-a", ["shop"]), contract("b", "http://prom-b", ["shop"])
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>"))
    )
    table = {"prom-a": {"10.0.0.5"}, "prom-b": {"10.0.0.5"}}
    usage.ask_stores(HERE, {"a": a, "b": b}, client, dns(table))
    assert a.shared_stores == {"main"}  # by address, so marked and not refused


def test_an_unreachable_store_cant_say() -> None:
    def down(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    a, b = contract("a", "http://prom-a", ["shop"]), contract("b", "http://prom-b", ["bank"])
    table = {"prom-a": {"10.0.0.5"}, "prom-b": {"10.0.0.5"}}
    usage.ask_stores(
        HERE, {"a": a, "b": b}, httpx.Client(transport=httpx.MockTransport(down)), dns(table)
    )
    assert a.shared_stores == {"main"}


def test_asking_never_splits_a_url_written_the_same() -> None:
    """Replicas behind one name answer each for itself: the text still joins them."""
    a = contract("a", "http://prom", ["shop"])
    b = contract("b", "http://PROM:80/", ["bank"])
    answers = iter([T1, T2])
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, json={"status": "success", "data": {"startTime": next(answers)}}
            )
        )
    )
    usage.ask_stores(HERE, {"a": a, "b": b}, client, dns({}))
    assert a.shared_stores == {"main"} and b.shared_stores == {"main"}


def test_a_chain_of_names_is_one_store() -> None:
    """a and b said the same; c couldn't say and shares b's address: all three one store."""
    a = contract("a", "http://prom-a", ["shop"])
    b = contract("b", "http://prom-b", ["bank"])
    c = contract("c", "http://prom-c", ["shop"])
    ask(
        [a, b, c],
        {"prom-a": T1, "prom-b": T1, "prom-c": 404},
        {"prom-b": {"10.0.0.7"}, "prom-c": {"10.0.0.7"}},
    )
    assert a.shared_stores == b.shared_stores == c.shared_stores == {"main"}


def test_each_contracts_token_goes_only_to_its_own_store() -> None:
    a, b = contract("a", "http://prom-a", ["shop"]), contract("b", "http://prom-b", ["bank"])
    seen: list[str] = []
    usage.ask_stores(HERE, {"a": a, "b": b}, stores({"prom-a": T1, "prom-b": T2}, seen), dns({}))
    assert sorted(seen) == ["prom-a Bearer a-secret", "prom-b Bearer b-secret"]


def test_a_failure_is_logged_without_its_token(caplog: pytest.LogCaptureFixture) -> None:
    a = contract("a", "http://prom-a", ["shop"])
    with caplog.at_level(logging.INFO, logger="squero.usage"):
        ask([a], {"prom-a": 401})
    assert "a/main: its Prometheus didn't say who it is (HTTP 401 from prom-a)" in caplog.text
    assert "a-secret" not in caplog.text


def test_a_relays_cluster_and_a_contract_without_prometheus_ask_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    relayed = make_contract(contract_id="r").model_dump()
    relayed["clusters"] = [
        {"name": "edge", "namespaces": ["shop"], "via": "relay", "relay_token_secret": "rt"}
    ]
    relayed["integrations"].append({"name": "prometheus", "url": "http://central"})
    r = contracts.build(relayed, secrets={"rt": SecretStr("relay-secret")})
    plain = make_contract(contract_id="p")

    def never(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"asked {req.url}")

    client = httpx.Client(transport=httpx.MockTransport(never))
    with caplog.at_level(logging.INFO, logger="squero.usage"):
        usage.ask_stores(HERE, {"r": r, "p": plain}, client, dns({}))
    assert not caplog.text


class Asked(Exception):
    pass


def asked(monkeypatch: pytest.MonkeyPatch) -> dict[str, contracts.Contract]:
    loaded = {"a": contract("a", "http://prom-a", ["shop"])}
    monkeypatch.setattr(contracts, "load", lambda directory: loaded)

    def spy(directory: Path, got: dict[str, contracts.Contract]) -> None:
        assert got is loaded
        raise Asked

    monkeypatch.setattr(usage, "ask_stores", spy)
    return loaded


def test_the_hub_asks_its_stores_when_it_loads_the_contracts(
    monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    from squero.api import create_app
    from tests.conftest import scripted

    asked(monkeypatch)
    settings.approver_token = SecretStr("approver-token")
    settings.contracts_dir = HERE
    with pytest.raises(Asked):
        create_app(settings, scripted())


def test_the_cli_asks_its_stores_when_it_loads_a_contract(
    monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    from squero import cli

    asked(monkeypatch)
    settings.contracts_dir = HERE
    with pytest.raises(Asked):
        cli.contract(settings, "a")


def test_links_found_in_any_order_join_one_store() -> None:
    """a and c said the same, b couldn't say and shares c's address: a, met first, still
    joins b's store through c."""
    a = contract("a", "http://prom-a", ["shop"])
    b = contract("b", "http://prom-b", ["bank"])
    c = contract("c", "http://prom-c", ["other"])
    ask(
        [a, b, c],
        {"prom-a": T1, "prom-b": 404, "prom-c": T1},
        {"prom-b": {"10.0.0.7"}, "prom-c": {"10.0.0.7"}},
    )
    assert a.shared_stores == b.shared_stores == c.shared_stores == {"main"}


def test_stores_joined_by_asking_are_logged_by_cluster(caplog: pytest.LogCaptureFixture) -> None:
    a, b = contract("a", "http://prom-a", ["shop"]), contract("b", "http://prom-b", ["bank"])
    c = contract("c", "http://prom-c", ["other"])
    with caplog.at_level(logging.INFO, logger="squero.usage"):
        ask([a, b, c], {"prom-a": T1, "prom-b": T1, "prom-c": T2})
    lines = [r.getMessage() for r in caplog.records if "several URLs" in r.getMessage()]
    assert lines == ["one Prometheus under several URLs: a/main, b/main"]


def test_a_redirect_cant_say_and_is_not_followed() -> None:
    """A redirect would carry the token to wherever it points."""
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.host)
        return httpx.Response(
            302, headers={"location": "http://elsewhere/api/v1/status/runtimeinfo"}
        )

    a, b = contract("a", "http://prom-a", ["shop"]), contract("b", "http://prom-b", ["shop"])
    client = httpx.Client(transport=httpx.MockTransport(handler))
    usage.ask_stores(HERE, {"a": a, "b": b}, client, dns({}))
    assert sorted(seen) == ["prom-a", "prom-b"] and not a.shared_stores


def test_an_invalid_url_cant_say() -> None:
    def invalid(req: httpx.Request) -> httpx.Response:
        raise httpx.InvalidURL("bad")

    a = contract("a", "http://prom-a", ["shop"])
    usage.ask_stores(HERE, {"a": a}, httpx.Client(transport=httpx.MockTransport(invalid)), dns({}))
    assert not a.shared_stores


def test_addresses_resolves_a_host_and_gives_none_when_it_cant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket

    def fake(host: str, port: int, type: int = 0) -> list[Any]:
        if host == "prom":
            return [(2, 1, 6, "", ("10.0.0.5", port)), (10, 1, 6, "", ("fd00::5", port, 0, 0))]
        if host == "bad..label":
            raise UnicodeError("label empty")
        raise socket.gaierror("unknown")

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    assert usage.addresses("prom", 9090) == {"10.0.0.5", "fd00::5"}
    assert usage.addresses("nowhere", 9090) == set()
    assert usage.addresses("bad..label", 9090) == set()
