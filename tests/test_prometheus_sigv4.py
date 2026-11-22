"""Prometheus sigv4 (slice 19.1b): an Amazon Managed Prometheus workspace is read with no
token: each request is signed with the Contract's aws key, for the region its URL names,
which the aws integration must list. The query that is sent is the one that is signed, the
signature is dated per request and sits in a header, the signer comes from where the URL
is, and one Contract's key never signs another's request."""

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from pydantic import SecretStr, ValidationError

from squero import agent, contracts, usage
from squero.config import Settings
from squero.servers import aws
from tests.conftest import make_contract
from tests.test_prometheus_auth import RELAYED, recorder
from tests.test_shared_stores import HERE, dns

HOST = "aps-workspaces.us-east-2.amazonaws.com"
WORKSPACE = "/workspaces/ws-1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
AMP = f"https://{HOST}{WORKSPACE}"
OTHER = f"https://{HOST}/workspaces/ws-1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5e"
KEY, SECRET = "AKIASIGNSIGNSIGNSIGN", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
SIGNED = {"prometheus_url": AMP, "prometheus_auth": "sigv4"}
WHEN = datetime(2026, 11, 22, 12, 0, 0, tzinfo=UTC)
DATE = "20261122T120000Z"


def built(
    cluster: dict[str, Any] | None = None,
    integration: dict[str, Any] | None = None,
    regions: tuple[str, ...] | None = ("us-east-2",),
    cid: str = "acme",
    key: str = KEY,
    secret: str = SECRET,
) -> contracts.Contract:
    data = make_contract(contract_id=cid).model_dump()
    data["clusters"][0].update(cluster or {})
    if integration is not None:
        data["integrations"].append({"name": "prometheus", **integration})
    if regions is not None:
        key_ = {"access_key_id": key, "secret_access_key_secret": "aws"}
        data["integrations"].append({"name": "aws", "regions": list(regions), **key_})
    secrets = {"aws": secret, "tok": "bearer-secret", "r": "r" * 40}
    return contracts.build(data, secrets={k: SecretStr(v) for k, v in secrets.items()})


def test_a_workspace_is_signed_for_its_urls_region_on_a_cluster_or_the_integration() -> None:
    for c in (built(SIGNED), built({}, {"url": AMP, "auth": "sigv4"})):
        (t,) = usage.targets(c)
        assert isinstance(t.signer, usage.SigV4)
        assert (t.url, t.token, t.user) == (AMP, None, None)
        assert (t.signer.key_id, t.signer.region, t.signer.service) == (KEY, "us-east-2", "aps")
    assert contracts.amp_region(AMP + "/") == "us-east-2"  # a trailing slash is the same URL
    assert contracts.amp_region("https://prometheus.example/workspaces/ws-1") is None


AMP_URL = "needs an Amazon Managed Prometheus workspace prometheus_url"
NO_TOKEN = "sigv4 signs with the aws integration's key: no prometheus_user or prometheus_token"
IN_EAST = r"its workspace is in us-east-2, not one of the aws integration's regions \['eu-west-1'\]"


@pytest.mark.parametrize(
    ("cluster", "integration", "regions", "error"),
    [
        (SIGNED, None, None, "auth: sigv4 on cluster 'test': it needs the aws integration's key"),
        (
            {},
            {"url": AMP, "auth": "sigv4"},
            None,
            "auth: sigv4 on the prometheus integration: it needs the aws integration's key",
        ),
        (
            {**SIGNED, "prometheus_url": "https://prometheus.example/api"},
            None,
            ("us-east-2",),
            AMP_URL,
        ),
        ({**SIGNED, "prometheus_url": AMP.replace("https", "http")}, None, ("us-east-2",), AMP_URL),
        ({**SIGNED, "prometheus_url": AMP + "/api/v1"}, None, ("us-east-2",), AMP_URL),
        ({"prometheus_auth": "sigv4"}, None, ("us-east-2",), AMP_URL),  # no URL of its own
        ({**SIGNED, "prometheus_token_secret": "tok"}, None, ("us-east-2",), NO_TOKEN),
        ({**SIGNED, "prometheus_user": "123456"}, None, ("us-east-2",), NO_TOKEN),
        (
            {},
            {"url": AMP, "auth": "sigv4", "token_secret": "tok"},
            ("us-east-2",),
            "sigv4 signs with the aws integration's key: no user or token_secret",
        ),
        (
            {},
            {"url": "https://prometheus.example", "auth": "sigv4"},
            ("us-east-2",),
            "workspace url",
        ),
        (SIGNED, None, ("eu-west-1",), "cluster 'test': " + IN_EAST + ": add it there"),
        (
            {},
            {"url": AMP, "auth": "sigv4"},
            ("eu-west-1",),
            "the prometheus integration: " + IN_EAST,
        ),
        (
            RELAYED | {"prometheus_auth": "sigv4"},
            None,
            ("us-east-2",),
            r"the Relay holds these.*prometheus_auth",
        ),
    ],
)
def test_sigv4_needs_the_aws_key_a_workspace_url_in_a_listed_region_and_no_token(
    cluster: dict[str, Any],
    integration: dict[str, Any] | None,
    regions: tuple[str, ...] | None,
    error: str,
) -> None:
    with pytest.raises(ValidationError, match=error):
        built(cluster, integration, regions)


# As AWS reads a query back: sorted, a space %20, only A-Za-z0-9-_.~ unescaped.
PROMQL = 'sum(rate(x{a="b c",d=~"e|f"}[5m])) / 2 + 1'
QUERY = (
    "query=sum%28rate%28x%7Ba%3D%22b%20c%22%2Cd%3D~%22e%7Cf%22%7D%5B5m%5D%29%29%20%2F%202%20%2B%201"
    "&time=1700000000.5"
)


def test_what_is_sent_is_what_is_signed_in_a_header_dated_per_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = recorder(monkeypatch)
    (t,) = usage.targets(built(SIGNED))
    assert isinstance(t.signer, usage.SigV4)
    clock = [WHEN]
    t.signer.now = lambda: clock[0]
    prom = usage.Prom(t)
    prom.instant(PROMQL, 1700000000.5)
    first = seen[0]
    assert first.url.path == WORKSPACE + "/api/v1/query"
    assert first.url.query.decode() == QUERY
    assert first.headers["x-amz-date"] == DATE
    # the Hub's own signer over the canonical request written by hand: host and date signed
    expected = aws.authorization(
        "GET", HOST, WORKSPACE + "/api/v1/query", QUERY, {"host": HOST, "x-amz-date": DATE},
        b"", "us-east-2", "aps", KEY, SECRET, DATE,
    )  # fmt: skip
    assert first.headers["authorization"] == expected
    assert expected.startswith(
        f"AWS4-HMAC-SHA256 Credential={KEY}/20261122/us-east-2/aps/aws4_request, "
        "SignedHeaders=host;x-amz-date, Signature="
    )
    clock[0] = WHEN + timedelta(minutes=20)  # the same client, later: AWS refuses an old date
    prom.instant("up", 0)
    assert seen[1].headers["x-amz-date"] == "20261122T122000Z"
    assert seen[1].headers["authorization"] != expected
    for r in seen:  # the secret in no URL and no header; nothing signed into the query
        assert SECRET not in str(r.url) and "X-Amz-" not in str(r.url)
        assert not [v for v in r.headers.values() if SECRET in v]


def test_repeated_names_are_kept_and_sorted_by_value() -> None:
    signer = usage.SigV4(KEY, SecretStr(SECRET), "us-east-2", now=lambda: WHEN)
    request = httpx.Request(
        "GET", AMP + "/api/v1/series", params=[("match[]", "b"), ("match[]", "a")]
    )
    sent = next(signer.auth_flow(request))
    assert sent.url.query.decode() == "match%5B%5D=a&match%5B%5D=b"


def test_the_signer_comes_from_where_the_url_is(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = recorder(monkeypatch)
    thanos = {"prometheus_url": "http://thanos:9090", "prometheus_token_secret": "tok"}
    # its own Bearer URL under a signed integration: never signed, never the aws key
    (t,) = usage.targets(built(thanos, {"url": AMP, "auth": "sigv4"}))
    assert t.signer is None and t.token == SecretStr("bearer-secret")
    usage.Prom(t).instant("up", 0)
    # its own workspace under a Bearer integration: signed, the integration's token left out
    (t,) = usage.targets(built(SIGNED, {"url": "http://thanos:9090", "token_secret": "tok"}))
    assert isinstance(t.signer, usage.SigV4) and t.token is None
    usage.Prom(t).instant("up", 0)
    got = [
        (r.url.host, r.headers["authorization"].split()[0], "x-amz-date" in r.headers) for r in seen
    ]
    assert got == [("thanos", "Bearer", False), (HOST, "AWS4-HMAC-SHA256", True)]


@pytest.mark.anyio
async def test_the_agents_tools_sign_with_and_without_clusters(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    seen = recorder(monkeypatch)
    integration = {"name": "prometheus", "url": AMP, "auth": "sigv4"}
    key = {"regions": ["us-east-2"], "access_key_id": KEY, "secret_access_key_secret": "aws"}
    alone = contracts.build(
        {"id": "cloud", "name": "cloud", "integrations": [integration, {"name": "aws", **key}]},
        secrets={"aws": SecretStr(SECRET)},
    )
    for c, args in (
        (built(SIGNED, {"url": AMP, "auth": "sigv4"}), {"cluster": "test"}),
        (alone, {}),
    ):
        prom = next(i for i in c.integrations if i.name == "prometheus")
        server, _ = agent.integration_server(c, prom, settings)
        async with Client(server) as mcp:
            await mcp.call_tool("query", {"promql": "up", **args})
    assert [r.url.path for r in seen] == [WORKSPACE + "/api/v1/query"] * 2
    for r in seen:
        assert r.headers["authorization"].startswith(f"AWS4-HMAC-SHA256 Credential={KEY}/")


def test_one_contracts_key_never_signs_anothers_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolation: two Contracts, each its own aws key and workspace in one region."""
    seen = recorder(monkeypatch)
    a = built(SIGNED, cid="a")
    b = built(
        {**SIGNED, "prometheus_url": OTHER}, cid="b", key="AKIAOTHEROTHEROTHERO", secret="b" * 40
    )
    for c in (a, b):
        (t,) = usage.targets(c)
        usage.Prom(t).instant("up", 0)
    got = [
        (r.url.path.split("/")[2][-1], r.headers["authorization"].split("=")[1].split("/")[0])
        for r in seen
    ]
    assert got == [("d", KEY), ("e", "AKIAOTHEROTHEROTHERO")]
    assert KEY not in seen[1].headers["authorization"]
    for r in seen:
        assert SECRET not in str(r.headers) and "b" * 40 not in str(r.headers)


def test_a_workspace_is_never_asked_who_it_is_and_two_in_a_region_stay_apart() -> None:
    """AMP serves no runtime info; one host serves every workspace of a region."""
    asked: list[str] = []

    def handler(r: httpx.Request) -> httpx.Response:
        asked.append(str(r.url))
        return httpx.Response(404)

    a = built(SIGNED, cid="a")
    b = built({**SIGNED, "prometheus_url": OTHER}, cid="b", key="AKIAOTHEROTHEROTHERO")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    usage.ask_stores(HERE, {"a": a, "b": b}, client, dns({HOST: {"192.0.2.10"}}))
    assert asked == []  # no signed request to a path AMP doesn't have
    assert not a.shared_stores and not b.shared_stores  # the same host and addresses, two stores


def refusing(monkeypatch: pytest.MonkeyPatch) -> None:
    """AWS's refusal of a signature quotes the request it expected."""
    real = httpx.Client
    said = {"message": "The request signature we calculated does not match: 'GET\n/workspaces'"}
    kind = {"x-amzn-errortype": "InvalidSignatureException:http://internal.amazon.com/coral/"}
    answer = lambda r: httpx.Response(403, json=said, headers=kind)  # noqa: E731
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(answer), **kw)
    )


def test_a_refusal_is_named_by_its_type_never_by_awss_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refusing(monkeypatch)
    (t,) = usage.targets(built(SIGNED))
    with pytest.raises(usage.UsageError) as e:
        usage.Prom(t).instant("up", 0)
    assert str(e.value) == "Prometheus error 403: InvalidSignatureException"
    assert SECRET not in str(e.value) and KEY not in str(e.value)


@pytest.mark.anyio
async def test_the_agent_sees_the_refusals_type_only(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    refusing(monkeypatch)
    c = built(SIGNED, {"url": AMP, "auth": "sigv4"})
    prom = next(i for i in c.integrations if i.name == "prometheus")
    server, _ = agent.integration_server(c, prom, settings)
    async with Client(server) as mcp:
        with pytest.raises(ToolError) as e:
            await mcp.call_tool("query", {"promql": "up", "cluster": "test"})
    assert "Prometheus error 403: InvalidSignatureException" in str(e.value)
    assert "signature we calculated" not in str(e.value)


def test_a_store_that_is_not_aws_says_its_own_error_as_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder(monkeypatch, status=401)
    (t,) = usage.targets(
        built({"prometheus_url": "http://thanos:9090", "prometheus_token_secret": "tok"})
    )
    with pytest.raises(usage.UsageError) as e:
        usage.Prom(t).instant("up", 0)
    assert str(e.value) == "Prometheus error 401: denied"


@pytest.mark.anyio
async def test_a_refusal_with_no_type_is_refused_never_awss_words(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    real = httpx.Client
    said = {"message": "The Canonical String for this request should have been 'GET\n/workspaces'"}
    answer = lambda r: httpx.Response(403, json=said)  # noqa: E731  no x-amzn-errortype
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(answer), **kw)
    )
    c = built(SIGNED, {"url": AMP, "auth": "sigv4"})
    prom = next(i for i in c.integrations if i.name == "prometheus")
    server, _ = agent.integration_server(c, prom, settings)
    async with Client(server) as mcp:
        with pytest.raises(ToolError) as e:
            await mcp.call_tool("query", {"promql": "up", "cluster": "test"})
    assert "Prometheus error 403: refused" in str(e.value)
    assert "Canonical String" not in str(e.value)
    (t,) = usage.targets(c)
    with pytest.raises(usage.UsageError) as u:
        usage.Prom(t).instant("up", 0)
    assert str(u.value) == "Prometheus error 403: "  # the Hub's own read never echoed it


@pytest.mark.anyio
async def test_a_url_with_a_trailing_slash_reaches_the_same_path_and_targets_stay_equal(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    seen = recorder(monkeypatch)
    c = built({**SIGNED, "prometheus_url": AMP + "/"}, {"url": AMP + "/", "auth": "sigv4"})
    (t,) = usage.targets(c)
    usage.Prom(t).instant("up", 0)
    prom = next(i for i in c.integrations if i.name == "prometheus")
    server, _ = agent.integration_server(c, prom, settings)
    async with Client(server) as mcp:
        await mcp.call_tool("query", {"promql": "up", "cluster": "test"})
    assert [r.url.path for r in seen] == [WORKSPACE + "/api/v1/query"] * 2
    assert usage.targets(c) == usage.targets(c)  # a signer is not part of what a Target is
