"""Prometheus azure and google (slice 19.1c): an Azure Monitor workspace and Google Managed
Prometheus are read with no token in the Contract: the Hub takes one from the cloud with
the azure integration's Entra app or the gcp integration's service account key, read-only,
for these two hosts only. A refused token names the host and the cloud's code, never its
description; one Contract's app or key never asks for another's token; Azure's limits (31
days, labels without case, a workspace named by its host) are the Hub's rules too."""

import json
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastmcp import Client
from fastmcp.exceptions import ToolError
from pydantic import SecretStr, ValidationError

from squero import agent, contracts, usage
from squero.config import Settings
from squero.usage import NEEDS_FOR, Target
from tests.conftest import make_contract
from tests.test_gcp import KEY, PRIVATE, unpad
from tests.test_needs import shop_fake
from tests.test_prometheus_auth import EMPTY, RELAYED
from tests.test_shared_stores import HERE, dns
from tests.test_usage import _prom

TENANT, APP = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d", "1b2c3d4e-5f6a-4b7c-8d9e-0f1a2b3c4d5e"
APP_SECRET = "azure~app~secret"
WORKSPACE = "acme-metrics-a1b2.eastus.prometheus.monitor.azure.com"
OTHER = "other-metrics-c3d4.eastus.prometheus.monitor.azure.com"
AMW = f"https://{WORKSPACE}"
GMP_PATH = "/v1/projects/acme-metrics/location/global/prometheus"
GMP = f"https://monitoring.googleapis.com{GMP_PATH}"
ENTRA, GOOGLE = "login.microsoftonline.com", "oauth2.googleapis.com"
AZURE = {"prometheus_url": AMW, "prometheus_auth": "azure"}
GOOGLED = {"prometheus_url": GMP, "prometheus_auth": "google"}
AZURE_APP = {
    "name": "azure",
    "vendor": "azure",
    "tools": {"subscription_list": "read"},
    "env": {"AZURE_TENANT_ID": TENANT, "AZURE_CLIENT_ID": APP},
    "env_secrets": {"AZURE_CLIENT_SECRET": "az"},
}
GCP_KEY = {
    "name": "gcp",
    "vendor": "gcp",
    "tools": {"list_log_names": "read"},
    "env": {"GOOGLE_CLOUD_PROJECT": "acme-prod"},  # not the project its metrics are read in
    "env_secrets": {"GOOGLE_SERVICE_ACCOUNT_KEY": "gcp-key"},
}
D = 86400


def built(
    cluster: dict[str, Any] | None = None,
    integration: dict[str, Any] | None = None,
    clouds: tuple[dict[str, Any], ...] = (AZURE_APP, GCP_KEY),
    cid: str = "acme",
    secret: str = APP_SECRET,
    key: str = KEY,
    sizing: dict[str, Any] | None = None,
) -> contracts.Contract:
    data = make_contract(contract_id=cid).model_dump()
    data["clusters"][0].update(cluster or {})
    if integration is not None:
        data["integrations"].append({"name": "prometheus", **integration})
    data["integrations"] += clouds
    if sizing:
        data["sizing"] = sizing
    secrets = {"az": secret, "gcp-key": key, "tok": "bearer-secret", "r": "r" * 40, "aws": "w" * 40}
    return contracts.build(data, secrets={k: SecretStr(v) for k, v in secrets.items()})


def cloud(monkeypatch: pytest.MonkeyPatch, token: Any = None, query: Any = None) -> list[Any]:
    """Every httpx.Client made from here on answers from memory, keeping each request: a
    cloud's login gives a new token each time, a store an empty result."""
    seen: list[httpx.Request] = []
    real = httpx.Client

    def answer(r: httpx.Request) -> httpx.Response:
        seen.append(r)
        if r.url.host in (ENTRA, GOOGLE):
            n = sum(1 for s in seen if s.url.host in (ENTRA, GOOGLE))
            fresh = {"access_token": f"minted-{n}", "expires_in": 3600}
            return token(r) if token else httpx.Response(200, json=fresh)
        return query(r) if query else httpx.Response(200, json=EMPTY)

    monkeypatch.setattr(
        httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(answer), **kw)
    )
    return seen


def test_a_clouds_store_is_read_with_a_minted_token_on_a_cluster_or_the_integration() -> None:
    for c in (built(AZURE), built({}, {"url": AMW, "auth": "azure"})):
        (t,) = usage.targets(c)
        assert isinstance(t.signer, usage.Minted) and t.signer.store == "Azure Monitor"
        assert (t.url, t.token, t.user, t.longest) == (AMW, None, None, 31 * D)
    for c in (built(GOOGLED), built({}, {"url": GMP, "auth": "google"})):
        (t,) = usage.targets(c)
        assert isinstance(t.signer, usage.Minted)
        assert t.signer.store == "Google Managed Prometheus"
        assert (t.url, t.token, t.user, t.longest) == (GMP, None, None, None)
    assert contracts.azure_monitor(AMW + "/") and contracts.gmp(GMP + "/")  # the same URL


AMW_URL = "auth: azure needs an Azure Monitor workspace's query endpoint as prometheus_url"
GMP_URL = "auth: google needs Google Managed Prometheus as prometheus_url"
NO_TOKEN = "takes its token with the azure integration's app: no prometheus_user or prometheus_"
THE_APP = r"it needs the azure integration's app \(AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CL"
ONLY = (GCP_KEY,)


@pytest.mark.parametrize(
    ("cluster", "integration", "clouds", "error"),
    [
        (AZURE, None, ONLY, "auth: azure on cluster 'test': " + THE_APP),
        (
            {},
            {"url": AMW, "auth": "azure"},
            ONLY,
            "auth: azure on the prometheus integration: " + THE_APP,
        ),
        # an app without its secret can ask for no token
        (AZURE, None, ({**AZURE_APP, "env_secrets": {}},), THE_APP),
        (AZURE, None, ({**AZURE_APP, "env": {"AZURE_CLIENT_ID": APP}},), THE_APP),
        (GOOGLED, None, (AZURE_APP,), "auth: google on cluster 'test': it needs the gcp integrat"),
        (
            {},
            {"url": GMP, "auth": "google"},
            (AZURE_APP,),
            "auth: google on the prometheus integration: it needs the gcp integration's key",
        ),
        ({**AZURE, "prometheus_url": "https://prometheus.example"}, None, None, AMW_URL),
        ({**AZURE, "prometheus_url": AMW.replace("https", "http")}, None, None, AMW_URL),
        ({**AZURE, "prometheus_url": AMW + "/api/v1"}, None, None, AMW_URL),
        ({**AZURE, "prometheus_url": AMW + ".evil.example"}, None, None, AMW_URL),
        ({**AZURE, "prometheus_url": f"https://evil.example/{WORKSPACE}"}, None, None, AMW_URL),
        ({**AZURE, "prometheus_url": GMP}, None, None, AMW_URL),  # the other cloud's store
        ({"prometheus_auth": "azure"}, None, None, AMW_URL),  # no URL of its own
        ({**GOOGLED, "prometheus_url": "https://prometheus.example"}, None, None, GMP_URL),
        ({**GOOGLED, "prometheus_url": GMP.replace("https", "http")}, None, None, GMP_URL),
        ({**GOOGLED, "prometheus_url": GMP + "/api/v1"}, None, None, GMP_URL),
        ({**GOOGLED, "prometheus_url": GMP.replace("global", "us")}, None, None, GMP_URL),
        (
            {**GOOGLED, "prometheus_url": GMP.replace("googleapis.com", "googleapis.com.evil.io")},
            None,
            None,
            GMP_URL,
        ),
        ({**GOOGLED, "prometheus_url": AMW}, None, None, GMP_URL),
        ({**AZURE, "prometheus_token_secret": "tok"}, None, None, NO_TOKEN),
        ({**AZURE, "prometheus_user": "123456"}, None, None, NO_TOKEN),
        (
            {},
            {"url": GMP, "auth": "google", "token_secret": "tok"},
            None,
            "google takes its token with the gcp integration's key: no user or token_secret",
        ),
        (RELAYED | {"prometheus_auth": "azure"}, None, None, r"the Relay holds.*prometheus_auth"),
        (RELAYED | {"prometheus_auth": "google"}, None, None, r"the Relay holds.*prometheus_auth"),
    ],
)
def test_azure_and_google_need_their_integration_their_own_url_and_no_token(
    cluster: dict[str, Any],
    integration: dict[str, Any] | None,
    clouds: tuple[dict[str, Any], ...] | None,
    error: str,
) -> None:
    with pytest.raises(ValidationError, match=error):
        built(cluster, integration, clouds if clouds is not None else (AZURE_APP, GCP_KEY))


def test_entra_is_asked_once_with_the_app_for_a_read_only_token_and_again_before_it_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = cloud(monkeypatch)
    (t,) = usage.targets(built(AZURE))
    assert isinstance(t.signer, usage.Minted)
    clock = [1000.0]
    t.signer.now = lambda: clock[0]
    prom = usage.Prom(t)
    prom.instant('up{a="b c"}', 5.5)
    prom.instant("up", 0)
    ask, first, second = seen
    assert (ask.method, str(ask.url)) == ("POST", f"https://{ENTRA}/{TENANT}/oauth2/v2.0/token")
    assert parse_qs(ask.content.decode()) == {
        "grant_type": ["client_credentials"],
        "client_id": [APP],
        "client_secret": [APP_SECRET],
        "scope": ["https://prometheus.monitor.azure.com/.default"],
    }
    assert "authorization" not in ask.headers
    # with the reader's own timeout: a login that never answers can't hold the hourly read
    assert (
        ask.extensions["timeout"]
        == first.extensions["timeout"]
        == dict.fromkeys(("connect", "read", "write", "pool"), 30)
    )
    assert (first.method, first.url.host, first.url.path) == ("GET", WORKSPACE, "/api/v1/query")
    assert dict(first.url.params) == {"query": 'up{a="b c"}', "time": "5.5"}
    assert [r.headers["authorization"] for r in (first, second)] == ["Bearer minted-1"] * 2
    clock[0] = 1000.0 + 3600 - 60  # a minute before it expires, to the second: still good
    prom.instant("up", 0)
    clock[0] += 1  # inside its last minute: a new one
    prom.instant("up", 0)
    assert [r.url.host for r in seen[3:]] == [WORKSPACE, ENTRA, WORKSPACE]
    assert seen[-1].headers["authorization"] == "Bearer minted-2"
    for r in seen:  # the secret only in the body of what Entra is asked
        assert APP_SECRET not in str(r.url) and APP_SECRET not in str(r.headers)
        assert (APP_SECRET in r.content.decode()) == (r.url.host == ENTRA)


def test_a_tenant_cant_change_where_the_app_secret_goes(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = cloud(monkeypatch)
    odd = {**AZURE_APP, "env": {"AZURE_TENANT_ID": "a/../b?c#d", "AZURE_CLIENT_ID": APP}}
    (t,) = usage.targets(built(AZURE, clouds=(odd,)))
    usage.Prom(t).instant("up", 0)
    assert seen[0].url.host == ENTRA and seen[0].url.query == b""
    assert seen[0].url.raw_path == b"/a%2F..%2Fb%3Fc%23d/oauth2/v2.0/token"


def test_google_is_asked_with_an_assertion_the_key_signed_for_monitoring_read_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = cloud(monkeypatch)
    (t,) = usage.targets(built(GOOGLED))
    usage.Prom(t).range("up", 0, 60, 60)
    ask, query = seen
    assert (ask.method, str(ask.url)) == ("POST", f"https://{GOOGLE}/token")
    form = parse_qs(ask.content.decode())
    assert form["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
    head, claims, signature = form["assertion"][0].split(".")
    PRIVATE.public_key().verify(
        unpad(signature), f"{head}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256()
    )
    said = json.loads(unpad(claims))
    assert said["iss"] == "squero@acme-prod.iam.gserviceaccount.com"
    assert said["scope"] == "https://www.googleapis.com/auth/monitoring.read"  # no other scope
    assert said["aud"] == f"https://{GOOGLE}/token"  # never the key file's own token_uri
    assert (query.method, query.url.host) == ("GET", "monitoring.googleapis.com")
    assert query.url.path == GMP_PATH + "/api/v1/query_range"  # the project its URL names
    assert query.headers["authorization"] == "Bearer minted-1"
    for r in seen:
        assert "PRIVATE KEY" not in r.content.decode() + str(r.url) + str(r.headers)


def test_a_token_reply_that_arrives_in_pieces_is_read_whole(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = json.dumps({"access_token": "streamed", "expires_in": 3600}).encode()
    pieces = httpx.ByteStream(body)  # not read until asked for, as from a real server
    seen = cloud(monkeypatch, token=lambda r: httpx.Response(200, stream=pieces))
    (t,) = usage.targets(built(AZURE))
    usage.Prom(t).instant("up", 0)
    assert seen[1].headers["authorization"] == "Bearer streamed"


def test_the_health_check_of_these_stores_names_a_metric() -> None:
    """Azure and Google answer only a query that names one; any other store as before."""
    thanos = {"prometheus_url": "http://thanos:9090"}
    for cluster, auth, url, asked in (
        (AZURE, "azure", AMW, "count(up)"),
        (GOOGLED, "google", GMP, "count(up)"),
        (thanos, "azure", AMW, "vector(1)"),  # its own store under an azure integration
        (thanos, "bearer", "http://thanos:9090", "vector(1)"),
    ):
        c = built(cluster, {"url": url, "auth": auth})
        found = [args for label, _, args in agent.probes(c) if label == "prometheus test"]
        assert found == [{"cluster": "test", "promql": asked}]
    amp = "https://aps-workspaces.us-east-2.amazonaws.com/workspaces/ws-1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
    key = {"access_key_id": "AKIASIGNSIGNSIGNSIGN", "secret_access_key_secret": "aws"}
    signed = built(
        {"prometheus_url": amp, "prometheus_auth": "sigv4"},
        {"url": amp, "auth": "sigv4"},
        ({"name": "aws", "regions": ["us-east-2"], **key},),
    )  # Amazon's store answers any query: as before
    assert [a["promql"] for label, _, a in agent.probes(signed) if "prom" in label] == ["vector(1)"]
    for auth, url, asked in (("google", GMP, "count(up)"), ("bearer", "http://p", "vector(1)")):
        alone = contracts.build(
            {
                "id": "cloud",
                "name": "cloud",
                "integrations": [{"name": "prometheus", "url": url, "auth": auth}, GCP_KEY],
            },
            secrets={"gcp-key": SecretStr(KEY)},
        )
        found = [args for label, _, args in agent.probes(alone) if label == "prometheus"]
        assert found == [{"promql": asked}]


def test_a_reply_that_doesnt_say_how_long_its_token_lasts_is_asked_again_in_5_minutes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replies = iter(
        [{"access_token": "a", "expires_in": "3599"}, {"access_token": "b"}, {"access_token": "c"}]
    )
    seen = cloud(monkeypatch, token=lambda r: httpx.Response(200, json=next(replies)))
    (t,) = usage.targets(built(AZURE))
    assert isinstance(t.signer, usage.Minted)
    clock = [0.0]
    t.signer.now = lambda: clock[0]
    prom = usage.Prom(t)
    for at in (0.0, 3538.0, 3540.0, 3779.0, 3781.0):  # Entra's older reply: seconds as text
        clock[0] = at
        prom.instant("up", 0)
    sent = [r.headers["authorization"][7:] for r in seen if r.url.host == WORKSPACE]
    assert sent == ["a", "a", "b", "b", "c"]


DESCRIPTION = f"AADSTS7000215: Invalid client secret provided for app '{APP}' in {TENANT}"


@pytest.mark.parametrize(
    ("reply", "said"),
    [
        (
            httpx.Response(401, json={"error": "invalid_client", "error_description": DESCRIPTION}),
            "HTTP 401 from login.microsoftonline.com: invalid_client",
        ),
        (  # a code that isn't one is not repeated
            httpx.Response(400, json={"error": f"bad secret {APP_SECRET}"}),
            "HTTP 400 from login.microsoftonline.com",
        ),
        (httpx.Response(200, json={"token_type": "Bearer"}), "HTTP 200 from login.microsoftonli"),
        (httpx.Response(401, json={"access_token": "x"}), "HTTP 401 from login.microsoftonline."),
        (httpx.Response(200, json={"access_token": ""}), "HTTP 200 from login.microsoftonline.c"),
        (httpx.Response(200, json=["access_token"]), "HTTP 200 from login.microsoftonline.com"),
        (httpx.Response(502, text=f"<html>{APP_SECRET}</html>"), "HTTP 502 from login.microsofto"),
    ],
)
def test_a_refused_token_names_the_host_and_the_code_never_the_description(
    monkeypatch: pytest.MonkeyPatch, reply: httpx.Response, said: str
) -> None:
    seen = cloud(monkeypatch, token=lambda r: reply)
    (t,) = usage.targets(built(AZURE))
    with pytest.raises(usage.UsageError) as e:
        usage.Prom(t).instant("up", 0)
    assert str(e.value).startswith(f"Prometheus unreachable: no token for Azure Monitor: {said}")
    for secret in (APP_SECRET, APP, TENANT, "AADSTS"):
        assert secret not in str(e.value)
    assert [r.url.host for r in seen] == [ENTRA]  # nothing sent to the store without a token


@pytest.mark.parametrize("key", ["not json", "{}", json.dumps({"client_email": "a@b"}), "[1]"])
def test_a_key_that_isnt_one_stops_only_its_own_read_and_is_never_quoted(
    monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    seen = cloud(monkeypatch)
    bad, good = built(GOOGLED, cid="bad", key=key), built(AZURE, cid="good")
    (t,) = usage.targets(bad)  # read when first asked, never when the Contracts load
    with pytest.raises(usage.UsageError) as e:
        usage.Prom(t).instant("up", 0)
    assert str(e.value) == (
        "Prometheus unreachable: no token for Google Managed Prometheus: the gcp "
        "integration's key isn't a service account key"
    )
    assert seen == []
    usage.ask_stores(HERE, {"bad": bad, "good": good}, httpx.Client(), dns({}))
    (t,) = usage.targets(good)
    usage.Prom(t).instant("up", 0)
    assert [r.url.host for r in seen] == [ENTRA, WORKSPACE]


def test_a_key_of_another_kind_is_refused_the_same_way(monkeypatch: pytest.MonkeyPatch) -> None:
    from cryptography.hazmat.primitives.asymmetric import ec

    pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    key = json.dumps({"client_email": "a@b", "private_key_id": "k", "private_key": pem.decode()})
    cloud(monkeypatch)
    (t,) = usage.targets(built(GOOGLED, key=key))
    with pytest.raises(usage.UsageError, match="isn't a service account key") as e:
        usage.Prom(t).instant("up", 0)
    assert "RSA" not in str(e.value)  # never the parser's own words


def test_the_token_comes_from_where_the_url_is(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = cloud(monkeypatch)
    thanos = {"prometheus_url": "http://thanos:9090", "prometheus_token_secret": "tok"}
    # its own Bearer URL under an azure integration: its own token, none asked of Entra
    (t,) = usage.targets(built(thanos, {"url": AMW, "auth": "azure"}))
    assert t.signer is None and t.token == SecretStr("bearer-secret") and t.longest is None
    usage.Prom(t).instant("up", 0)
    # its own Google store under a Bearer integration: minted, the integration's token left out
    (t,) = usage.targets(built(GOOGLED, {"url": "http://thanos:9090", "token_secret": "tok"}))
    assert isinstance(t.signer, usage.Minted) and t.token is None
    usage.Prom(t).instant("up", 0)
    got = [(r.url.host, r.headers.get("authorization")) for r in seen]
    assert got == [
        ("thanos", "Bearer bearer-secret"),
        (GOOGLE, None),
        ("monitoring.googleapis.com", "Bearer minted-1"),
    ]


def test_one_contracts_app_never_asks_for_anothers_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolation: two Contracts, each its own tenant, app and workspace in one region."""
    token = lambda r: httpx.Response(  # noqa: E731  Entra gives each app its own token
        200, json={"access_token": "for-" + parse_qs(r.content.decode())["client_id"][0]}
    )
    seen = cloud(monkeypatch, token=token)
    theirs = {**AZURE_APP, "env": {"AZURE_TENANT_ID": "b-tenant", "AZURE_CLIENT_ID": "b-app"}}
    a = built(AZURE, cid="a")
    b = built({**AZURE, "prometheus_url": f"https://{OTHER}"}, None, (theirs,), "b", "b" * 40)
    for c in (a, b):
        (t,) = usage.targets(c)
        usage.Prom(t).instant("up", 0)
    got = [(r.url.host, r.url.path.split("/")[1], r.headers.get("authorization")) for r in seen]
    assert got == [
        (ENTRA, TENANT, None),
        (WORKSPACE, "api", f"Bearer for-{APP}"),
        (ENTRA, "b-tenant", None),
        (OTHER, "api", "Bearer for-b-app"),
    ]
    mine, _, others, _ = (r.content.decode() for r in seen)
    assert "b" * 40 not in mine and "b-app" not in mine
    assert "azure~app~secret" not in parse_qs(others)["client_secret"][0] and APP not in others


def test_one_contracts_key_never_signs_anothers_assertion(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolation: two Contracts, each its own service account key, in one Google host."""
    seen = cloud(monkeypatch)
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    other = {"client_email": "b@b-prod.iam", "private_key_id": "k2", "private_key": pem.decode()}
    a = built(GOOGLED, cid="a")
    b = built(
        {**GOOGLED, "prometheus_url": GMP.replace("acme-metrics", "b-metrics")},
        cid="b",
        key=json.dumps(other),
    )
    for c in (a, b):
        (t,) = usage.targets(c)
        usage.Prom(t).instant("up", 0)
    for ask, key, email in ((seen[0], PRIVATE, "squero@acme-prod"), (seen[2], private, "b@b-prod")):
        head, claims, signature = parse_qs(ask.content.decode())["assertion"][0].split(".")
        key.public_key().verify(
            unpad(signature), f"{head}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256()
        )
        assert json.loads(unpad(claims))["iss"].startswith(email)
    assert [r.url.path for r in seen[1::2]] == [
        GMP_PATH + "/api/v1/query",
        "/v1/projects/b-metrics/location/global/prometheus/api/v1/query",
    ]
    assert [r.headers["authorization"] for r in seen[1::2]] == [
        "Bearer minted-1",
        "Bearer minted-2",
    ]


@pytest.mark.anyio
async def test_the_agents_tools_mint_too_and_say_these_stores_serve_no_alerts(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    seen = cloud(monkeypatch)
    integration = {"name": "prometheus", "url": GMP, "auth": "google"}
    alone = contracts.build(
        {"id": "cloud", "name": "cloud", "integrations": [integration, GCP_KEY]},
        secrets={"gcp-key": SecretStr(KEY)},
    )
    for c, args, store in (
        (built(AZURE, {"url": AMW, "auth": "azure"}), {"cluster": "test"}, "Azure Monitor"),
        (alone, {}, "Google Managed Prometheus"),
    ):
        prom = next(i for i in c.integrations if i.name == "prometheus")
        server, _ = agent.integration_server(c, prom, settings)
        async with Client(server) as mcp:
            await mcp.call_tool("query", {"promql": "up", **args})
            with pytest.raises(ToolError) as e:
                await mcp.call_tool("active_alerts", args)
        assert str(e.value).endswith(f"{store} serves no alerts: none can be read from this store")
    got = [(r.url.host, r.url.path, r.headers.get("authorization")) for r in seen]
    assert got == [
        (ENTRA, f"/{TENANT}/oauth2/v2.0/token", None),
        (WORKSPACE, "/api/v1/query", "Bearer minted-1"),
        (GOOGLE, "/token", None),
        ("monitoring.googleapis.com", GMP_PATH + "/api/v1/query", "Bearer minted-2"),
    ]  # and nothing asked of a page neither store has


@pytest.mark.anyio
async def test_a_store_with_its_own_token_still_serves_its_alerts(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    alerts = {"status": "success", "data": {"alerts": []}}
    seen = cloud(monkeypatch, query=lambda r: httpx.Response(200, json=alerts))
    c = built({"prometheus_url": "http://thanos:9090", "prometheus_token_secret": "tok"}, {
        "url": AMW, "auth": "azure"
    })  # fmt: skip
    prom = next(i for i in c.integrations if i.name == "prometheus")
    server, _ = agent.integration_server(c, prom, settings)
    async with Client(server) as mcp:
        await mcp.call_tool("active_alerts", {"cluster": "test"})
    assert [(r.url.host, r.url.path) for r in seen] == [("thanos", "/api/v1/alerts")]


@pytest.mark.anyio
async def test_the_agent_sees_a_refused_token_as_the_operator_does(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    refused = {"error": "invalid_client", "error_description": DESCRIPTION}
    cloud(monkeypatch, token=lambda r: httpx.Response(401, json=refused))
    c = built(AZURE, {"url": AMW, "auth": "azure"})
    prom = next(i for i in c.integrations if i.name == "prometheus")
    server, _ = agent.integration_server(c, prom, settings)
    async with Client(server) as mcp:
        with pytest.raises(ToolError) as e:
            await mcp.call_tool("query", {"promql": "up", "cluster": "test"})
    assert "Prometheus unreachable: no token for Azure Monitor: HTTP 401 from " in str(e.value)
    assert str(e.value).endswith("login.microsoftonline.com: invalid_client")
    assert TENANT not in str(e.value) and APP not in str(e.value)


DENIED = "Permission monitoring.timeSeries.list denied (or the resource may not exist)."


@pytest.mark.parametrize(
    ("body", "said"),
    [
        (  # Google's own shape
            {"error": {"code": 403, "message": DENIED, "status": "PERMISSION_DENIED"}},
            "PERMISSION_DENIED: " + DENIED,
        ),
        (  # Azure's own shape
            {"error": {"code": "AuthorizationFailed", "message": "no Monitoring Data Reader"}},
            "AuthorizationFailed: no Monitoring Data Reader",
        ),
        ({"error": {"code": 403, "message": "m" * 400}}, "403: " + "m" * 295),
        ({"status": "error", "errorType": "bad_data", "error": "parse error"}, "parse error"),
    ],
)
@pytest.mark.anyio
async def test_a_refused_query_says_the_clouds_code_and_message_cut_at_300(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, body: dict[str, Any], said: str
) -> None:
    cloud(monkeypatch, query=lambda r: httpx.Response(403, json=body))
    c = built(GOOGLED, {"url": GMP, "auth": "google"})
    (t,) = usage.targets(c)
    with pytest.raises(usage.UsageError) as e:
        usage.Prom(t).instant("up", 0)
    assert str(e.value) == f"Prometheus error 403: {said}"
    prom = next(i for i in c.integrations if i.name == "prometheus")
    server, _ = agent.integration_server(c, prom, settings)
    async with Client(server) as mcp:
        with pytest.raises(ToolError) as tool:
            await mcp.call_tool("query", {"promql": "up", "cluster": "test"})
    assert str(tool.value).endswith(f"Prometheus error 403: {said}")


def test_these_stores_are_never_asked_who_they_are_and_a_workspace_is_its_host() -> None:
    """Neither serves runtime info. A region's Azure workspaces can share addresses; one
    Google host serves every project."""
    asked: list[str] = []

    def handler(r: httpx.Request) -> httpx.Response:
        asked.append(str(r.url))
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    one = {"192.0.2.10"}
    a = built(AZURE, cid="a")
    b = built({**AZURE, "prometheus_url": f"https://{OTHER}"}, cid="b")
    usage.ask_stores(HERE, {"a": a, "b": b}, client, dns({WORKSPACE: one, OTHER: one}))
    assert not a.shared_stores and not b.shared_stores  # the same addresses, two workspaces
    g = built(GOOGLED, cid="g")
    h = built({**GOOGLED, "prometheus_url": GMP.replace("acme-metrics", "h-metrics")}, cid="h")
    usage.ask_stores(HERE, {"g": g, "h": h}, client, dns({"monitoring.googleapis.com": one}))
    assert not g.shared_stores and not h.shared_stores  # the same host, two projects
    assert asked == []  # no token asked for, and no page neither store has
    # one workspace read by two Contracts is still one store, and one project too
    c, d = built(AZURE, cid="c"), built({**AZURE, "namespaces": ["bank"]}, cid="d")
    usage.ask_stores(HERE, {"c": c, "d": d}, client, dns({WORKSPACE: one}))
    assert c.shared_stores == {"test"} == d.shared_stores
    i, j = built(GOOGLED, cid="i"), built({**GOOGLED, "namespaces": ["bank"]}, cid="j")
    usage.ask_stores(HERE, {"i": i, "j": j}, client, dns({}))
    assert i.shared_stores == {"test"} == j.shared_stores
    # and two stores that aren't Azure's, behind one address, are still marked as before
    thanos = {"prometheus_token_secret": "tok"}
    e = built({**thanos, "prometheus_url": "https://e.example"}, cid="e")
    f = built({**thanos, "prometheus_url": "https://f.example"}, cid="f")
    usage.ask_stores(HERE, {"e": e, "f": f}, client, dns({"e.example": one, "f.example": one}))
    assert e.shared_stores == {"test"} == f.shared_stores


def test_on_azure_labels_that_differ_only_by_case_are_the_same_series() -> None:
    def pair(url: dict[str, Any], mine: str, theirs: str) -> list[str]:
        a = built({**url, "prometheus_labels": {"cluster": mine}}, cid="a")
        b = built({**url, "prometheus_labels": {"cluster": theirs}}, cid="b")
        return contracts._shared_metrics(HERE, [a, b])

    (problem,) = pair(AZURE, "Prod", "prod")  # Azure compares them without case
    assert "a/test and b/test read the same Prometheus with labels that can match" in problem
    assert pair(AZURE, "prod", "staging") == []
    a = built({**AZURE, "prometheus_labels": {"Cluster": "x"}}, cid="a")
    b = built({**AZURE, "prometheus_labels": {"cluster": "y"}}, cid="b")
    assert len(contracts._shared_metrics(HERE, [a, b])) == 1  # two names: never told apart
    thanos = {"prometheus_url": "http://thanos:9090"}
    assert pair(thanos, "Prod", "prod") == []  # any other store tells them apart
    assert pair(GOOGLED, "Prod", "prod") == []


SLOW = {"policies": {"slow": {"base": "production", "window": "32d"}}}
RULE = {"rules": [{"namespace": "shop", "policy": "slow"}]}


def test_a_window_azure_cant_answer_is_refused_where_a_cluster_reads_it() -> None:
    error = "cluster 'test' reads Azure Monitor, which answers 31 days at most: policy 'slow'"
    with pytest.raises(ValidationError, match=error):
        built(AZURE, sizing=SLOW | RULE)
    with pytest.raises(ValidationError, match=error):  # the integration's URL, read by it
        built({}, {"url": AMW, "auth": "azure"}, sizing=SLOW | RULE)
    with pytest.raises(ValidationError, match=error):
        built(AZURE, sizing=SLOW | {"default": "slow"})
    fits = {"policies": {"slow": {"base": "production", "window": "31d"}}}
    built(AZURE, sizing=fits | RULE)  # 31 days fit
    built(AZURE, sizing=SLOW)  # a policy no rule uses is never read
    built(GOOGLED, sizing=SLOW | RULE)  # Google names no such limit
    thanos = {"prometheus_url": "http://thanos:9090"}
    built(thanos, {"url": AMW, "auth": "azure"}, sizing=SLOW | RULE)  # its own store, not Azure's
    built(RELAYED, {"url": AMW, "auth": "azure"}, sizing=SLOW | RULE)  # read by its Relay


def test_on_azure_needs_and_bounds_are_read_31_days_back_at_most(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = shop_fake()
    monkeypatch.setattr(usage, "Prom", lambda t: _prom(fake, t))
    asked: list[tuple[str, float]] = []
    needs, bounds = usage.needs, usage.bounds

    def read_needs(prom: usage.Prom, t: Target, start: float, end: float) -> Any:
        asked.append(("needs", end - start))
        return needs(prom, t, start, end)

    def read_bounds(prom: usage.Prom, t: Target, at: float, back: int) -> Any:
        asked.append(("bounds", back))
        return bounds(prom, t, at, back)

    monkeypatch.setattr(usage, "needs", read_needs)
    monkeypatch.setattr(usage, "bounds", read_bounds)
    now = 1_800_000_000.0
    usage.collect(built(AZURE), [D, 40 * D], now, {})
    usage.collect(built(AZURE), [D], now, {"test": now - 3600})  # a nearer mark is kept
    usage.collect(built(GOOGLED), [D, 40 * D], now, {})
    assert asked == [
        ("needs", 31 * D),
        ("bounds", 31 * D),
        ("needs", 3600),
        ("bounds", D),
        ("needs", NEEDS_FOR),
        ("bounds", 40 * D),
    ]
