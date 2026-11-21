"""Prometheus auth (slice 19.1a): Bearer as before, Basic with a user id (Grafana Cloud)
only over https, the token and user always from where the URL is, one helper for every
read (Sizings, the agent's tools, asking a store who it is), and one Contract's
credentials never sent to another's store."""

import base64
from typing import Any

import httpx
import pytest
from fastmcp import Client
from pydantic import SecretStr, ValidationError

from squero import agent, contracts, usage
from squero.config import Settings
from tests.conftest import make_contract
from tests.test_shared_stores import HERE, dns, stores
from tests.test_shared_stores import contract as store_contract

GRAFANA = "https://prometheus-prod-01.grafana.example/api/prom"
BASIC = "Basic " + base64.b64encode(b"123456:glc_secret").decode()
EMPTY = {"status": "success", "data": {"resultType": "vector", "result": []}}
SECRETS = {"tok": "glc_secret", "other": "thanos-secret", "r": "r" * 40}


def built(cluster: dict[str, Any], integration: dict[str, Any] | None = None) -> Any:
    data = make_contract().model_dump()
    data["clusters"][0].update(cluster)
    if integration is not None:
        data["integrations"].append({"name": "prometheus", **integration})
    return contracts.build(data, secrets={k: SecretStr(v) for k, v in SECRETS.items()})


def basic(url: str = GRAFANA) -> dict[str, Any]:
    return {"prometheus_url": url, "prometheus_token_secret": "tok"} | {
        "prometheus_auth": "basic",
        "prometheus_user": "123456",
    }


def recorder(monkeypatch: pytest.MonkeyPatch, status: int = 200) -> list[httpx.Request]:
    """Every httpx.Client made from here on answers from memory, keeping each request."""
    seen: list[httpx.Request] = []
    real = httpx.Client

    def answer(r: httpx.Request) -> httpx.Response:
        seen.append(r)
        return httpx.Response(status, json=EMPTY if status == 200 else {"error": "denied"})

    monkeypatch.setattr(
        httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(answer), **kw)
    )
    return seen


RELAYED = {"via": "relay", "relay_token_secret": "r", "context": None}


@pytest.mark.parametrize(
    ("cluster", "integration", "error"),
    [
        (
            {
                "prometheus_url": GRAFANA,
                "prometheus_token_secret": "tok",
                "prometheus_auth": "basic",
            },
            None,
            "prometheus_auth: basic needs prometheus_user and prometheus_token_secret",
        ),
        (
            {"prometheus_url": GRAFANA, "prometheus_auth": "basic", "prometheus_user": "123456"},
            None,
            "prometheus_auth: basic needs prometheus_user and prometheus_token_secret",
        ),
        (basic("http://grafana.example/api/prom"), None, "needs an https:// prometheus_url"),
        (
            {"prometheus_url": GRAFANA, "prometheus_token_secret": "tok", "prometheus_user": "1"},
            None,
            "prometheus_user is for prometheus_auth: basic",
        ),
        (
            {"prometheus_auth": "basic", "prometheus_user": "123456"},
            None,
            "needs prometheus_user and prometheus_token_secret",
        ),
        (RELAYED | {"prometheus_user": "123456"}, None, r"the Relay holds these.*prometheus_user"),
        (RELAYED | {"prometheus_auth": "basic"}, None, r"the Relay holds these.*prometheus_auth"),
        ({}, {"url": GRAFANA, "token_secret": "tok", "auth": "basic"}, "basic needs user"),
        (
            {},
            {"url": "http://g", "token_secret": "tok", "auth": "basic", "user": "1"},
            "needs an https:// url",
        ),
        ({}, {"url": GRAFANA, "token_secret": "tok", "user": "1"}, "user is for auth: basic"),
    ],
)
def test_basic_needs_a_user_a_token_and_https_and_a_relay_holds_its_own(
    cluster: dict[str, Any], integration: dict[str, Any] | None, error: str
) -> None:
    with pytest.raises(ValidationError, match=error):
        built(cluster, integration)


def test_basic_goes_with_its_user_to_the_path_under_the_url_and_bearer_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = recorder(monkeypatch)
    (grafana,) = usage.targets(built(basic()))
    usage.Prom(grafana).instant("up", 0)
    (thanos,) = usage.targets(
        built({"prometheus_url": "http://thanos:9090", "prometheus_token_secret": "tok"})
    )
    usage.Prom(thanos).instant("up", 0)
    (none,) = usage.targets(built({"prometheus_url": "http://open:9090"}))
    usage.Prom(none).instant("up", 0)
    got = [(r.url.path, r.headers.get("authorization")) for r in seen]
    assert got == [
        ("/api/prom/api/v1/query", BASIC),  # Grafana Cloud's query URL has a path
        ("/api/v1/query", "Bearer glc_secret"),
        ("/api/v1/query", None),
    ]


def test_the_token_and_user_come_from_where_the_url_is() -> None:
    integration = {"url": GRAFANA, "token_secret": "tok", "auth": "basic", "user": "123456"}
    own = {"prometheus_url": "http://thanos:9090", "prometheus_token_secret": "other"}
    (t,) = usage.targets(built(own, integration))
    assert (t.url, t.token, t.user) == ("http://thanos:9090", SecretStr("thanos-secret"), None)
    (t,) = usage.targets(built({}, integration))  # no URL of its own: the integration's
    assert (t.url, t.user) == (GRAFANA, "123456")


@pytest.mark.anyio
async def test_the_agents_tools_send_it_with_and_without_clusters(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    seen = recorder(monkeypatch)
    integration = {"url": GRAFANA, "token_secret": "tok", "auth": "basic", "user": "123456"}
    clustered = built({}, integration)
    alone = contracts.build(
        {"id": "cloud", "name": "cloud", "integrations": [{"name": "prometheus", **integration}]},
        secrets={"tok": SecretStr("glc_secret")},
    )
    for c, args in ((clustered, {"cluster": "test"}), (alone, {})):
        prom = next(i for i in c.integrations if i.name == "prometheus")
        server, _ = agent.integration_server(c, prom, settings)
        async with Client(server) as mcp:
            await mcp.call_tool("query", {"promql": "up", **args})
    assert [(r.url.path, r.headers["authorization"]) for r in seen] == [
        ("/api/prom/api/v1/query", BASIC)
    ] * 2


def test_asking_who_a_store_is_sends_each_contract_its_own_credentials_only() -> None:
    """Isolation: one shared client asks both stores; A's user and token reach only A's."""
    a = store_contract("a", GRAFANA, ["shop"], prometheus_auth="basic", prometheus_user="123456")
    b = store_contract("b", "http://thanos:9090", ["web"])
    seen: list[str] = []
    answers = {"prometheus-prod-01.grafana.example": "t1", "thanos": "t2"}
    usage.ask_stores(HERE, {"a": a, "b": b}, stores(answers, seen), dns({}))
    a_basic = "Basic " + base64.b64encode(b"123456:a-secret").decode()
    assert sorted(seen) == sorted(
        [f"prometheus-prod-01.grafana.example {a_basic}", "thanos Bearer b-secret"]
    )


def test_a_refusal_never_carries_the_token(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder(monkeypatch, status=401)
    (t,) = usage.targets(built(basic()))
    with pytest.raises(usage.UsageError) as e:
        usage.Prom(t).instant("up", 0)
    assert str(e.value) == "Prometheus error 401: denied"
    assert "glc_secret" not in str(e.value) and BASIC.split()[1] not in str(e.value)
