"""The console app (slice 9.5a): the console's guards hold for /app/ and
/api/v1/ too (identity, approver cookie, CSP, text only), and a Contract's Sizings
never reach another's API."""

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from squero import webapp
from squero.config import Settings
from squero.store import Store, connect
from tests.conftest import make_contract
from tests.test_api import client
from tests.test_console import ACT, AS_OPERATOR, OPERATOR, VIEW
from tests.test_sizing_store import NOW, H, row, stored

ROOT = Path(__file__).parent.parent
PROM = {"name": "prometheus", "url": "http://prometheus"}
INTEGRATIONS = [{"name": "kubernetes"}, PROM]
CSP = "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'"


@pytest.fixture(autouse=True)
def _clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stored runs are at NOW: history's 14 and 400 days count back from it."""
    monkeypatch.setattr("squero.api.now", lambda: NOW)


@pytest.fixture
def app_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text(
        '<!doctype html><script type="module" src="/app/assets/index-a1.js"></script>'
    )
    (tmp_path / "assets" / "index-a1.js").write_text("console.log(1)")
    (tmp_path / "assets" / "index-a1.css").write_text("body{}")
    (tmp_path / "assets" / "font-a1.woff2").write_bytes(b"wOF2")
    (tmp_path / "secret.txt").write_text("not an asset")
    (tmp_path / "assets" / ".hidden").write_text("a dotfile is never served")
    monkeypatch.setattr(webapp, "APP_DIR", tmp_path)
    return tmp_path


def hub(settings: Settings, fake_cluster: Any, database_url: str) -> Any:
    settings.database_url = SecretStr(database_url)
    settings.console_operators = {OPERATOR}
    bank = make_contract(contract_id="bank", integrations=INTEGRATIONS)
    return client(
        settings,
        fake_cluster,
        acme={"integrations": INTEGRATIONS},
        more={"bank": (bank, fake_cluster)},
    )


def seed(database_url: str, *runs: tuple[str, float, dict[str, Any]]) -> None:
    async def go() -> None:
        async with connect(database_url) as pool:
            store = Store(pool)
            await store.migrate()
            for cid, at, r in runs:
                await stored(store, cid, at, {"test": None}, r)

    asyncio.run(go())


# --- the doors ------------------------------------------------------------------------


def test_the_page_and_its_files_need_an_operators_identity(
    settings: Settings, fake_cluster: Any, database_url: str, app_dir: Path
) -> None:
    with hub(settings, fake_cluster, database_url) as c:
        page = c.get("/app/", headers=AS_OPERATOR)  # no token needed for the page
        js = c.get("/app/assets/index-a1.js", headers=AS_OPERATOR)
        css = c.get("/app/assets/index-a1.css", headers=AS_OPERATOR)
        font = c.get("/app/assets/font-a1.woff2", headers=AS_OPERATOR)  # slice 21.5a
        refused = [
            c.get(path, headers=headers)
            for path in ("/app/", "/app/assets/index-a1.js")
            for headers in (ACT, {"Tailscale-User-Login": "mallory@example.com", **ACT})
        ]
        missing = [
            c.get(f"/app/assets/{name}", headers=AS_OPERATOR).status_code
            for name in ("nope.js", "..%2Fsecret.txt", ".hidden", "%2E%2E", "%2E%2E%2Fsecret.txt")
        ]
    assert page.status_code == 200 and "/app/assets/index-a1.js" in page.text
    assert page.headers["cache-control"] == "no-store"
    assert js.headers["content-type"].startswith("text/javascript")
    assert css.headers["content-type"].startswith("text/css")
    assert font.headers["content-type"] == "font/woff2" and font.content == b"wOF2"
    for r in (page, js, css, font):
        assert r.headers["content-security-policy"] == CSP
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["x-content-type-options"] == "nosniff"
    assert [r.status_code for r in refused] == [403] * 4
    assert set(missing) <= {404}


def test_the_api_also_needs_the_approver_cookie(
    settings: Settings, fake_cluster: Any, database_url: str, app_dir: Path
) -> None:
    with hub(settings, fake_cluster, database_url) as c:
        no_cookie = c.get("/api/v1/contracts", headers=AS_OPERATOR)
        bad_cookie = c.get(
            "/api/v1/contracts", headers={**AS_OPERATOR, "Cookie": "squero_actions=x"}
        )
        stranger = c.get("/api/v1/contracts", headers=ACT)
        ok = c.get("/api/v1/contracts", headers=VIEW)
    for r in (no_cookie, bad_cookie):
        assert r.status_code == 401
        assert r.json()["detail"]["token_url"] == "/app/?view=token"
    assert stranger.status_code == 403
    assert ok.status_code == 200 and ok.headers["content-security-policy"] == CSP
    assert ok.json() == [{"id": "acme", "name": "acme"}, {"id": "bank", "name": "bank"}]


def test_the_app_is_off_without_operators(
    settings: Settings, fake_cluster: Any, database_url: str, app_dir: Path
) -> None:
    settings.database_url = SecretStr(database_url)
    with client(settings, fake_cluster) as c:
        assert c.get("/app/", headers=VIEW).status_code == 404
        assert c.get("/api/v1/contracts", headers=VIEW).status_code == 404


def test_an_image_without_the_app_says_so(
    settings: Settings,
    fake_cluster: Any,
    database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(webapp, "APP_DIR", tmp_path / "none")
    with hub(settings, fake_cluster, database_url) as c:
        r = c.get("/app/", headers=AS_OPERATOR)
    assert r.status_code == 404 and "isn't built" in r.text


# --- the data -------------------------------------------------------------------------


def test_workloads_are_the_latest_runs_ranked(
    settings: Settings, fake_cluster: Any, database_url: str, app_dir: Path
) -> None:
    seed(
        database_url,
        ("acme", NOW, row("api", waste={"cpu": 2.0, "memory": 0.0})),
        ("acme", NOW + H, row("api", waste={"cpu": 0.5, "memory": 0.0})),
        ("acme", NOW + 2 * H, row("web", waste={"cpu": 1.0, "memory": 0.0})),
    )
    seed(
        database_url,
        ("acme", NOW + 3 * H, row("api", waste={"cpu": 0.5, "memory": 0.0}, risks=[RISK])),
    )
    with hub(settings, fake_cluster, database_url) as c:
        body = c.get("/api/v1/contracts/acme/workloads", headers=VIEW).json()
        unknown = c.get("/api/v1/contracts/nope/workloads", headers=VIEW)
    assert body["contract"] == "acme"
    assert [w["workload"] for w in body["workloads"]] == ["test/shop/Deployment/api"]
    (api,) = body["workloads"]
    assert api["risks"] == [{**RISK, "container": "app"}]
    assert api["containers"][0]["cpu"] is None and api["waste"]["cpu"] == 0.5
    assert body["clusters"]["test"]["at"] == NOW + 3 * H
    assert unknown.status_code == 404


def test_a_stored_held_request_reaches_the_workloads_route_with_its_change(
    settings: Settings, fake_cluster: Any, database_url: str, app_dir: Path
) -> None:
    """Slice 14.3: what the hourly run stored of a request its HPA divides by is what
    the console reads, the other resource without it."""
    held = {
        "hpa": "api",
        "percent": 50.0,
        "average": 0.5,
        "change": "target:\n  type: AverageValue",
    }
    sized = {"action": "lower", "current": 1.0, "target": 0.115, "basis": 0.1}
    stored = row(
        "api", waste={"cpu": 0.9, "memory": 0.0}, cpu={**sized, "held": held}, memory=sized
    )
    seed(database_url, ("acme", NOW, stored))
    with hub(settings, fake_cluster, database_url) as c:
        body = c.get("/api/v1/contracts/acme/workloads", headers=VIEW).json()
    (app,) = body["workloads"][0]["containers"]
    assert app["cpu"]["held"] == held and app["memory"]["held"] is None
    assert body["workloads"][0]["waste"]["cpu"] == 0.9  # its Waste still counts


RISK = {"kind": "oom_killed", "text": "<img src=x onerror=alert(1)>"}


def test_a_priced_contracts_workloads_carry_their_monthly_cost(
    settings: Settings, fake_cluster: Any, database_url: str, app_dir: Path
) -> None:
    """1 core wasted of 2 requested, over 1 pod, at 0.05 per vCPU-hour (slice 10.1)."""
    asked = {"requests": {"cpu": 2.0, "memory": 0.0}}
    seed(database_url, ("acme", NOW, row("api", **asked)), ("bank", NOW, row("api", **asked)))
    settings.database_url = SecretStr(database_url)
    settings.console_operators = {OPERATOR}
    bank = make_contract(contract_id="bank", integrations=INTEGRATIONS)
    acme = {
        "integrations": INTEGRATIONS,
        "prices": {"currency": "EUR", "cpu": 0.05, "memory": 0.01},
    }
    with client(settings, fake_cluster, acme=acme, more={"bank": (bank, fake_cluster)}) as c:
        priced = c.get("/api/v1/contracts/acme/workloads", headers=VIEW).json()
        plain = c.get("/api/v1/contracts/bank/workloads", headers=VIEW).json()
    assert priced["currency"] == "EUR"
    assert priced["cost"] == pytest.approx({"waste": 36.5, "requests": 73.0})
    assert priced["workloads"][0]["cost"] == pytest.approx({"waste": 36.5, "requests": 73.0})
    assert plain["currency"] is None and plain["cost"] is None  # acme's prices are its own
    assert plain["workloads"][0]["cost"] is None


def test_cost_groups_only_the_contracts_own_namespaces(
    settings: Settings, fake_cluster: Any, database_url: str, app_dir: Path
) -> None:
    """Both Contracts store a run with a Workload in namespace shop: each one's Cost
    page sees only its own (slice 10.2a)."""
    asked = {"requests": {"cpu": 2.0, "memory": 0.0}}
    seed(
        database_url,
        ("acme", NOW, row("api", **asked)),
        ("bank", NOW, row("api", requests={"cpu": 7.0, "memory": 0.0})),
    )
    settings.database_url = SecretStr(database_url)
    settings.console_operators = {OPERATOR}
    bank = make_contract(contract_id="bank", integrations=INTEGRATIONS)
    acme = {"integrations": INTEGRATIONS, "prices": {"cpu": 0.05, "memory": 0.01}}
    with client(settings, fake_cluster, acme=acme, more={"bank": (bank, fake_cluster)}) as c:
        mine = c.get("/api/v1/contracts/acme/cost", headers=VIEW).json()
        theirs = c.get("/api/v1/contracts/bank/cost", headers=VIEW).json()
        wrong = c.get("/api/v1/contracts/acme/cost?by=pods", headers=VIEW)
    (g,) = mine["groups"]
    assert (g["group"], g["workloads"]) == ("test/shop", 1)
    assert g["cost"] == pytest.approx({"requests": 73.0, "waste": 36.5})
    (b,) = theirs["groups"]
    assert b["requests"]["cpu"] == 7.0 and b["cost"] is None and theirs["currency"] is None
    assert wrong.status_code == 422


def test_cost_by_team_and_label_from_the_stored_labels(
    settings: Settings, fake_cluster: Any, database_url: str, app_dir: Path
) -> None:
    """Teams are matched when shown, from the labels stored on each row (slice 10.2b)."""
    asked = {"requests": {"cpu": 2.0, "memory": 0.0}}

    async def go() -> None:  # one run, two rows
        async with connect(database_url) as pool:
            store = Store(pool)
            await store.migrate()
            pay = row("api", labels={"team": "pay"}, **asked)
            await stored(store, "acme", NOW, {"test": None}, pay, row("web", **asked))

    asyncio.run(go())
    settings.database_url = SecretStr(database_url)
    settings.console_operators = {OPERATOR}
    acme = {
        "integrations": INTEGRATIONS,
        "allocation": {
            "labels": ["team"],
            "teams": [{"team": "payments", "labels": {"team": "pay"}}],
        },
    }
    with client(settings, fake_cluster, acme=acme) as c:
        teams = c.get("/api/v1/contracts/acme/cost?by=team", headers=VIEW).json()
        label = c.get("/api/v1/contracts/acme/cost?by=label:team", headers=VIEW).json()
        wrong = c.get("/api/v1/contracts/acme/cost?by=label:owner", headers=VIEW)
    assert {g["group"] for g in teams["groups"]} == {"payments", "(no team)"}
    assert teams["groupings"] == ["namespace", "team", "label:team"] and teams["warnings"] == []
    assert {g["group"] for g in label["groups"]} == {"pay", "(no team)"}
    assert wrong.status_code == 422


def test_history_is_hourly_for_14_days_and_daily_for_400(
    settings: Settings, fake_cluster: Any, database_url: str, app_dir: Path
) -> None:
    day = 86400.0
    times = [NOW - 20 * day + h * H for h in (1, 5)] + [NOW - day + h * H for h in (1, 2)]
    seed(database_url, *[("acme", at, row(pods=float(i))) for i, at in enumerate(times)])
    with hub(settings, fake_cluster, database_url) as c:
        q = "cluster=test&namespace=shop&kind=Deployment&name=api&container=app"
        recent = c.get(f"/api/v1/contracts/acme/history?{q}", headers=VIEW)
        year = c.get(f"/api/v1/contracts/acme/history?{q}&days=400", headers=VIEW)
        week = c.get(f"/api/v1/contracts/acme/history?{q}&days=7", headers=VIEW)
        other = c.get(f"/api/v1/contracts/bank/history?{q}", headers=VIEW)
    assert [p["pods"] for p in recent.json()["points"]] == [2.0, 3.0]
    assert [p["pods"] for p in year.json()["points"]] == [1.0, 3.0]  # the last of each day
    assert week.status_code == 422
    assert other.json() == {"points": []}


def test_a_contracts_sizings_never_reach_anothers_api(
    settings: Settings, fake_cluster: Any, database_url: str, app_dir: Path
) -> None:
    """Same cluster, namespace and Workload names in both Contracts."""
    seed(
        database_url,
        ("acme", NOW, row(policy="acme-only")),
        ("bank", NOW, row(policy="bank-only")),
    )
    q = "cluster=test&namespace=shop&kind=Deployment&name=api&container=app&days=400"
    with hub(settings, fake_cluster, database_url) as c:
        for cid in ("acme", "bank"):
            body = c.get(f"/api/v1/contracts/{cid}/workloads", headers=VIEW).json()
            policies = {x["policy"] for w in body["workloads"] for x in w["containers"]}
            assert policies == {f"{cid}-only"}
            points = c.get(f"/api/v1/contracts/{cid}/history?{q}", headers=VIEW).json()["points"]
            assert len(points) == 1


# --- text only, typed ------------------------------------------------------------------

RAW_HTML = re.compile(
    r"dangerouslySetInnerHTML|innerHTML|outerHTML|insertAdjacentHTML|document\.write"
)


def test_the_apps_source_never_writes_raw_html() -> None:
    sources = [
        p
        for p in (ROOT / "console" / "src").rglob("*")
        if p.suffix in {".ts", ".tsx"} and not p.name.endswith(".test.tsx")
    ]
    assert sources, "no app source found"
    for p in sources:
        assert not RAW_HTML.search(p.read_text()), p


def test_the_committed_openapi_is_the_apis() -> None:
    committed = (ROOT / "console" / "openapi.json").read_text()
    assert committed == webapp.openapi(), "run `make console-types` and commit the result"


def test_the_openapi_has_no_page_routes() -> None:
    paths = json.loads(webapp.openapi())["paths"]
    assert all(p.startswith("/api/v1/") for p in paths)


def test_cost_compares_each_contract_with_its_own_runs(
    settings: Settings, fake_cluster: Any, database_url: str, app_dir: Path
) -> None:
    """acme asked 2 cores a day ago and 1 now; bank has no run that old, and acme's
    old run never dates it (slice 10.3a)."""
    day = 24 * H
    seed(
        database_url,
        ("acme", NOW - day, row("api", requests={"cpu": 2.0, "memory": 0.0})),
        ("acme", NOW, row("api", requests={"cpu": 1.0, "memory": 0.0})),
        ("bank", NOW, row("api", requests={"cpu": 7.0, "memory": 0.0})),
    )
    settings.database_url = SecretStr(database_url)
    settings.console_operators = {OPERATOR}
    bank = make_contract(contract_id="bank", integrations=INTEGRATIONS)
    acme = {"integrations": INTEGRATIONS, "prices": {"cpu": 0.05, "memory": 0.01}}
    with client(settings, fake_cluster, acme=acme, more={"bank": (bank, fake_cluster)}) as c:
        mine = c.get("/api/v1/contracts/acme/cost?compare=1", headers=VIEW).json()
        theirs = c.get("/api/v1/contracts/bank/cost?compare=1", headers=VIEW).json()
        plain = c.get("/api/v1/contracts/acme/cost", headers=VIEW).json()
        bad = [
            c.get(f"/api/v1/contracts/acme/cost?compare={d}", headers=VIEW).status_code
            for d in ("0", "401", "soon")
        ]
    (g,) = mine["groups"]
    assert g["change"]["cost"] == pytest.approx({"requests": -36.5, "waste": 0.0})
    assert mine["compare"]["clusters"] == {"test": {"at": NOW - day, "oldest": NOW - day}}
    assert mine["total"]["cost"] == pytest.approx({"requests": 36.5, "waste": 36.5})
    assert theirs["compare"]["clusters"] == {"test": {"at": None, "oldest": NOW}}
    assert theirs["groups"][0]["change"]["requests"] == {"cpu": 0.0, "memory": 0.0}
    assert plain["compare"] is None and plain["groups"][0]["change"] is None
    assert bad == [422, 422, 422]


def test_the_cost_api_carries_only_the_contracts_own_realized_savings(
    settings: Settings, fake_cluster: Any, database_url: str, app_dir: Path
) -> None:
    """acme lowered api to its Sizing; bank, with the same names, sees none (10.3b)."""
    from squero import sizing
    from tests.test_realized import sized

    async def go() -> None:
        async with connect(database_url) as pool:
            store = Store(pool)
            await store.migrate()
            for cid in ("acme", "bank"):
                await stored(store, cid, NOW - H, {"test": None}, sized(1.0, 0.115))
            before, status = await store.latest_sizings("acme", {"test"})
            await stored(store, "acme", NOW, {"test": None}, sized(0.115, 0.115))
            await stored(store, "bank", NOW, {"test": None}, sized(1.0, 0.115))
            acme = make_contract(integrations=INTEGRATIONS)
            now = [sized(0.115, 0.115)]
            await sizing.realize(store, acme, NOW, before, status, now, {"test": None})

    asyncio.run(go())
    settings.database_url = SecretStr(database_url)
    settings.console_operators = {OPERATOR}
    bank = make_contract(contract_id="bank", integrations=INTEGRATIONS)
    acme = {"integrations": INTEGRATIONS, "prices": {"cpu": 0.04, "memory": 0.005}}
    with client(settings, fake_cluster, acme=acme, more={"bank": (bank, fake_cluster)}) as c:
        mine = c.get("/api/v1/contracts/acme/cost", headers=VIEW).json()["realized"]
        theirs = c.get("/api/v1/contracts/bank/cost", headers=VIEW).json()["realized"]
    (d,) = mine["drops"]
    assert (d["workload"], d["resource"], d["before"], d["after"]) == (
        "test/shop/Deployment/api/app",
        "cpu",
        1.0,
        0.115,
    )
    assert mine["cost"]["rate"] == pytest.approx(0.885 * 3 * 0.04 * 730)
    assert theirs["drops"] == [] and theirs["cost"] is None


def test_a_list_priced_contract_shows_its_prices_and_where_they_come_from(
    settings: Settings, fake_cluster: Any, database_url: str, app_dir: Path
) -> None:
    """One m5.large (2 vCPU, 8 GiB, 0.096 USD/h): a GiB is 0.096 / (7 x 2 + 8), a vCPU 7
    times that. 1 core wasted of 2 requested, over 1 pod (slice 10.4)."""
    from tests.test_listprices import M5, facts

    asked = {"requests": {"cpu": 2.0, "memory": 0.0}}
    seed(database_url, ("acme", NOW, row("api", **asked)), ("bank", NOW, row("api", **asked)))

    async def go() -> None:
        async with connect(database_url) as pool:
            store = Store(pool)
            await store.migrate()
            await store.save_nodes("acme", "test", NOW, facts(M5))
            await store.put_list_price(M5[:3], 0.096, None, NOW)

    asyncio.run(go())
    settings.database_url = SecretStr(database_url)
    settings.console_operators = {OPERATOR}
    bank = make_contract(contract_id="bank", integrations=INTEGRATIONS)
    acme = {"integrations": INTEGRATIONS, "list_prices": {}}
    with client(settings, fake_cluster, acme=acme, more={"bank": (bank, fake_cluster)}) as c:
        work = c.get("/api/v1/contracts/acme/workloads", headers=VIEW).json()
        cost = c.get("/api/v1/contracts/acme/cost", headers=VIEW).json()
        plain = c.get("/api/v1/contracts/bank/workloads", headers=VIEW).json()
    vcpu = 7 * 0.096 / 22
    assert work["currency"] == "USD"
    assert work["cost"] == pytest.approx({"waste": vcpu * 730, "requests": 2 * vcpu * 730})
    assert cost["currency"] == "USD" and cost["total"]["cost"]["requests"] == pytest.approx(
        2 * vcpu * 730
    )
    for body in (work, cost):
        (note,) = body["prices"]
        assert (note["cluster"], note["source"]) == ("test", "list")
        assert "1 x aws m5.large in us-east-1" in note["note"]
    assert plain["currency"] is None and plain["prices"] == []  # acme's list is its own


def test_the_agenda_is_one_contracts_and_needs_the_cookie(
    settings: Settings, fake_cluster: Any, database_url: str
) -> None:
    """Slice 11.4: the panel on the Workloads page."""
    seed(database_url, ("acme", NOW, row("api")), ("bank", NOW, row("web")))
    with hub(settings, fake_cluster, database_url) as c:
        acme = c.get("/api/v1/contracts/acme/agenda", headers=VIEW).json()
        no_cookie = c.get("/api/v1/contracts/acme/agenda", headers=AS_OPERATOR).status_code
        unknown = c.get("/api/v1/contracts/nope/agenda", headers=VIEW).status_code
    assert acme["contract"] == "acme"
    assert [i["workload"] for i in acme["items"]] == ["test/shop/Deployment/api"]
    assert no_cookie == 401 and unknown == 404
