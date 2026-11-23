"""A remote vendor behind a login (slice 19.6a): its url takes a variable of the
Contract's whose form the registry fixes, the Contract's token goes as a Bearer
header to that url only, and its schema snapshot is taken with the vendor's own
variables, never written with a secret in it."""

import json
import logging
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastmcp import Client, FastMCP
from pydantic import SecretStr

from squero import agent, cli, contracts
from squero.servers import vendors

SITES = r"one\.example|two\.example"
URL = "https://mcp.{SITE}/v1/mcp?toolsets=core"


def spec(**changes: Any) -> vendors.Spec:
    base: dict[str, Any] = {
        "command": (),
        "env": frozenset({"SITE", "TOKEN"}),
        "secret_env": frozenset({"TOKEN"}),
        "urls": (URL,),
        "auth": lambda env: vendors.Bearer(env["TOKEN"]),
        "required_env": {"SITE": SITES, "TOKEN": r".+"},
        "login": True,
    }
    return vendors.Spec(**{**base, **changes})


def contract(cid: str, site: str, token: str, **changes: Any) -> contracts.Contract:
    vendor = {
        "name": "obs",
        "vendor": "siteful",
        "env": {"SITE": site},
        "env_secrets": {"TOKEN": "obs-token"},
        "tools": {"whoami": "read"},
        **changes,
    }
    data = {"id": cid, "name": cid, "integrations": [vendor]}
    return contracts.build(data, secrets={"obs-token": SecretStr(token)})


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(vendors.VENDORS, "siteful", spec())


@pytest.fixture
def remotes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    """Every remote the registry opens, as (url, auth), answered by a server in
    memory whose one tool says which url it stands for."""
    opened: list[tuple[str, Any]] = []

    def remote(url: str, auth: Any = None) -> Client[Any]:
        opened.append((url, auth))
        server = FastMCP("remote")

        @server.tool
        def whoami() -> str:
            return url

        return Client(server)

    monkeypatch.setattr(vendors, "remote", remote)
    return opened


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"urls": ("https://mcp.{REGION}/v1/mcp",)}, r"non-secret variables: \['REGION'\]"),
        ({"urls": ("https://mcp.example/{TOKEN}",)}, r"non-secret variables: \['TOKEN'\]"),
        ({"urls": ("https://mcp.{}/v1/mcp",)}, r"non-secret variables: \[''\]"),
        ({"required_env": {"TOKEN": r".+"}}, r"non-secret variables: \['SITE'\]"),
        ({"auth": None}, "only a remote vendor with an auth"),
        ({"command": ("/bin/x",), "urls": (), "required_env": {}}, "only a remote vendor"),
    ],
)
def test_a_url_takes_only_a_variable_the_registry_checks(
    changes: dict[str, Any], error: str
) -> None:
    with pytest.raises(ValueError, match=error):
        spec(**changes)


def test_a_fixed_url_and_a_checked_variable_are_both_fine() -> None:
    assert vendors.url_vars(spec()) == {"SITE"}
    assert vendors.url_vars(vendors.VENDORS["gcp"]) == set()
    assert vendors.urls(vendors.VENDORS["gcp"], {}) == vendors.VENDORS["gcp"].urls
    env = {"SITE": "two.example", "TOKEN": "t"}
    assert vendors.urls(spec(), env) == ("https://mcp.two.example/v1/mcp?toolsets=core",)


@pytest.mark.parametrize(
    "site",
    ["evil.example", "one.example/x", "one.example@evil.example", "one.example\n", "", None],
)
def test_the_url_is_never_built_from_a_site_of_another_form(site: str | None) -> None:
    env = {"TOKEN": "t"} if site is None else {"SITE": site, "TOKEN": "t"}
    with pytest.raises(ValueError, match=r"SITE: .* doesn't match"):
        vendors.urls(spec(), env)
    with pytest.raises(ValueError, match="doesn't match"):
        vendors.server(spec(), env, "obs")


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"env": {"SITE": "evil.example"}}, "SITE: 'evil.example' doesn't match"),
        ({"env": {}}, "needs SITE"),
        (
            {"env": {}, "env_secrets": {"TOKEN": "obs-token", "SITE": "obs-token"}},
            "SITE go into the vendor's url: set them in env",
        ),
        ({"env": {"SITE": "one.example", "TOKEN": "t"}}, "TOKEN hold credentials"),
        ({"env_secrets": {}}, "needs TOKEN"),
    ],
)
def test_a_contract_names_one_of_the_registrys_sites_in_plain_env(
    registry: None, changes: dict[str, Any], error: str
) -> None:
    assert contract("acme", "one.example", "tok-acme").secret_refs() == {"obs-token"}
    with pytest.raises(Exception, match=error):
        contract("acme", "one.example", "tok-acme", **changes)


async def authorize(auth: httpx2.Auth) -> str:
    flow = auth.async_auth_flow(httpx2.Request("POST", "https://mcp.one.example/v1/mcp"))
    return (await flow.__anext__()).headers["Authorization"]


@pytest.mark.anyio
async def test_the_token_goes_as_a_bearer_header() -> None:
    assert await authorize(vendors.Bearer("tok-acme")) == "Bearer tok-acme"
    sync = vendors.Bearer("tok-bank").sync_auth_flow(httpx2.Request("GET", "https://x.example"))
    assert next(sync).headers["Authorization"] == "Bearer tok-bank"


@pytest.mark.anyio
async def test_each_contract_reaches_its_own_site_with_its_own_token(
    registry: None, remotes: list[tuple[str, Any]]
) -> None:
    acme = contract("acme", "one.example", "tok-acme")
    bank = contract("bank", "two.example", "tok-bank")
    answers = {}
    for c in (acme, bank):
        vendor = c.integrations[0]
        assert isinstance(vendor, contracts.Vendor)
        async with Client(agent.vendor_server(c, vendor)) as client:
            answers[c.id] = (await client.call_tool("whoami", {})).data
    assert answers == {
        "acme": "https://mcp.one.example/v1/mcp?toolsets=core",
        "bank": "https://mcp.two.example/v1/mcp?toolsets=core",
    }
    assert [(url, auth.token) for url, auth in remotes] == [
        ("https://mcp.one.example/v1/mcp?toolsets=core", "tok-acme"),
        ("https://mcp.two.example/v1/mcp?toolsets=core", "tok-bank"),
    ]


def refusing(monkeypatch: pytest.MonkeyPatch) -> list[httpx2.Request]:
    """The vendor's own client code, over a transport that refuses every login."""
    seen: list[httpx2.Request] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(401, json={"error": "invalid_token"})

    monkeypatch.setattr(httpx2, "AsyncHTTPTransport", lambda: httpx2.MockTransport(answer))
    return seen


@pytest.mark.anyio
async def test_the_header_reaches_the_vendors_url_and_a_refusal_never_shows_the_token(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    seen = refusing(monkeypatch)
    env = {"SITE": "one.example", "TOKEN": "tok-sentinel-1"}
    with caplog.at_level(logging.DEBUG):
        async with Client(vendors.server(spec(), env, "obs")) as client:
            assert await client.list_tools() == []  # refused: nothing is offered
        with pytest.raises(Exception) as e:
            await vendors.capture(spec(), env)
    assert seen and {str(r.url) for r in seen} == {"https://mcp.one.example/v1/mcp?toolsets=core"}
    assert {r.headers["Authorization"] for r in seen} == {"Bearer tok-sentinel-1"}
    assert "tok-sentinel-1" not in str(e.value) and "tok-sentinel-1" not in caplog.text
    assert "error response" in caplog.text  # the refusal was logged, without it


@pytest.mark.anyio
async def test_a_redirect_to_another_host_never_carries_the_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[httpx2.Request] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        if request.url.host == "mcp.one.example":
            return httpx2.Response(307, headers={"Location": "https://evil.example/v1/mcp"})
        return httpx2.Response(401, json={"error": "invalid_token"})

    monkeypatch.setattr(httpx2, "AsyncHTTPTransport", lambda: httpx2.MockTransport(answer))
    env = {"SITE": "one.example", "TOKEN": "tok-sentinel-1"}
    async with Client(vendors.server(spec(), env, "obs")) as client:
        assert await client.list_tools() == []
    # The MCP client doesn't follow it: nothing leaves for the other host at all.
    assert seen and {r.url.host for r in seen} == {"mcp.one.example"}


ENVIRON = {
    "SITE": "one.example",
    "TOKEN": "tok-owner",
    "SQUERO_SLACK_BOT_TOKEN": "xoxb-sentinel-123",
    "GOOGLE_SERVICE_ACCOUNT_KEY": "another vendor's",
}


def test_a_snapshots_variables_are_only_the_vendors_own() -> None:
    assert vendors.login_env(spec(), ENVIRON) == {"SITE": "one.example", "TOKEN": "tok-owner"}
    with pytest.raises(ValueError, match="set SITE, TOKEN: this vendor lists no tools") as e:
        vendors.login_env(spec(), {"SQUERO_SLACK_BOT_TOKEN": "xoxb-sentinel-123"})
    assert "xoxb" not in str(e.value)
    with pytest.raises(ValueError, match="set TOKEN:"):
        vendors.login_env(spec(), {"SITE": "one.example"})


@pytest.mark.anyio
async def test_a_login_vendor_is_snapshotted_with_its_own_login(
    remotes: list[tuple[str, Any]],
) -> None:
    seen: list[dict[str, str]] = []

    def auth(env: dict[str, str]) -> Any:
        seen.append(dict(env))
        return vendors.Bearer(env["TOKEN"])

    snapshot = json.loads(await vendors.capture(spec(auth=auth), ENVIRON))
    assert [t["name"] for t in snapshot] == ["whoami"]
    assert seen == [{"SITE": "one.example", "TOKEN": "tok-owner"}]  # nothing of the Hub's
    assert [(url, a.token) for url, a in remotes] == [
        ("https://mcp.one.example/v1/mcp?toolsets=core", "tok-owner")
    ]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("environ", "error"),
    [
        (None, "set SITE, TOKEN"),
        ({"SITE": "one.example"}, "set TOKEN"),
        ({"SITE": "evil.example", "TOKEN": "tok-owner"}, "SITE: 'evil.example' doesn't match"),
    ],
)
async def test_no_snapshot_without_the_login_or_at_another_site(
    remotes: list[tuple[str, Any]], environ: dict[str, str] | None, error: str
) -> None:
    with pytest.raises(ValueError, match=error) as e:
        await vendors.capture(spec(), environ)
    assert remotes == [] and "tok-owner" not in str(e.value)  # refused before any call


@pytest.mark.anyio
async def test_a_vendor_without_a_login_is_still_snapshotted_with_nothing(
    remotes: list[tuple[str, Any]],
) -> None:
    open_spec = spec(login=False, urls=("https://mcp.fixed.example/mcp",))
    await vendors.capture(open_spec, ENVIRON)
    await vendors.capture(open_spec)  # and needs none of its variables set
    assert remotes == [("https://mcp.fixed.example/mcp", None)] * 2


@pytest.mark.anyio
async def test_a_snapshot_that_holds_the_token_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    echo = FastMCP("echo")

    @echo.tool(description="signed in with tok-owner at one.example")
    def whoami() -> str:
        return ""

    monkeypatch.setattr(vendors, "remote", lambda url, auth=None: Client(echo))
    with pytest.raises(ValueError, match="the vendor's tool list holds TOKEN: not written") as e:
        await vendors.capture(spec(), ENVIRON)
    assert "tok-owner" not in str(e.value)
    # the site is no secret: a description may name it
    assert "whoami" in await vendors.capture(spec(), {**ENVIRON, "TOKEN": "another"})


def test_the_command_takes_the_login_from_its_environment(
    registry: None,
    remotes: list[tuple[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(vendors, "SNAPSHOTS", tmp_path)
    for name in ("SITE", "TOKEN"):
        monkeypatch.delenv(name, raising=False)
    assert cli.main(["integrations", "snapshot", "siteful"]) == 1
    assert "siteful: set SITE, TOKEN" in capfd.readouterr().err
    assert remotes == [] and not (tmp_path / "siteful.json").exists()

    monkeypatch.setenv("SITE", "two.example")
    monkeypatch.setenv("TOKEN", "tok-owner")
    assert cli.main(["integrations", "snapshot", "siteful"]) == 0
    saved = (tmp_path / "siteful.json").read_text()
    assert '"name": "whoami"' in saved and "tok-owner" not in saved
    assert cli.main(["integrations", "snapshot", "siteful", "--check"]) == 0
    assert [(url, a.token) for url, a in remotes] == [
        ("https://mcp.two.example/v1/mcp?toolsets=core", "tok-owner")
    ] * 2

    monkeypatch.setenv("SITE", "evil.example")
    assert cli.main(["integrations", "snapshot", "siteful", "--check"]) == 1
    assert "doesn't match" in capfd.readouterr().err and len(remotes) == 2
