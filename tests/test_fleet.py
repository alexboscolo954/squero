"""The Fleet (slice 18.4): a row per Contract, each value its own Contract's, totals per
currency never converted, a failing Contract's error on its own row, the parts' versions and
Protocols (18.3), and the route behind the console's identity and cookie."""

from types import SimpleNamespace
from typing import Any

import pytest

from squero import protocol, relay, webapp
from squero.store import Store
from squero.webapp import Cost, IdleGpus, UnrequestedGpus, Waste, Workloads
from tests.test_agenda import _no_prometheus as _no_prometheus
from tests.test_agenda import contract
from tests.test_sizing_store import NOW, row, stored
from tests.test_sizing_store import store as store

ASKED = {"requests": {"cpu": 2.0, "memory": 0.0}}


def priced(cid: str, currency: str, cpu: float) -> Any:
    return contract(cid, prices={"currency": currency, "cpu": cpu, "memory": 0.0})


@pytest.mark.anyio
async def test_each_row_is_its_own_contracts_and_totals_are_per_currency(store: Store) -> None:
    """1 core wasted of 2 asked at 0.05 an hour is 36.5 a month (730 hours)."""
    for cid, cpu in (("acme", 1.0), ("bank", 3.0), ("cafe", 1.0), ("dark", 1.0)):
        await stored(
            store, cid, NOW, {"test": None}, row(waste={"cpu": cpu, "memory": 0.0}, **ASKED)
        )
    await store.turn_on_kill_switch("bank", "console:op", NOW)
    every = [
        priced("cafe", "USD", 0.10),
        priced("bank", "EUR", 0.05),
        priced("acme", "USD", 0.05),
        contract("dark"),  # no prices: no money, out of the totals
    ]
    fleet = await webapp.fleet_of(store, every, NOW)
    rows = {r.id: r for r in fleet.rows}
    assert [r.id for r in fleet.rows] == ["acme", "bank", "cafe", "dark"]
    cost = {r.id: r.cost.model_dump() if r.cost else None for r in fleet.rows}
    assert cost["acme"] == pytest.approx({"waste": 36.5, "requests": 73.0})
    assert cost["bank"] == pytest.approx({"waste": 109.5, "requests": 73.0})  # its own 3 cores
    assert cost["cafe"] == pytest.approx({"waste": 73.0, "requests": 146.0})
    assert (rows["dark"].currency, rows["dark"].cost) == (None, None)
    assert [r.kill_switch for r in fleet.rows] == [False, True, False, False]
    assert rows["acme"].agenda == {"waste": 1}
    assert rows["bank"].agenda == {"kill_switch": 1, "waste": 1}
    totals = {t.currency: t for t in fleet.totals}
    assert list(totals) == ["EUR", "USD"]  # never converted into one
    assert (totals["EUR"].contracts, totals["EUR"].waste) == (1, pytest.approx(109.5))
    assert (totals["USD"].contracts, totals["USD"].waste) == (2, pytest.approx(109.5))
    assert totals["USD"].requests == pytest.approx(219.0)
    assert all(r.error is None for r in fleet.rows)


def gpus_of(idle: float | None, unrequested: float | None) -> tuple[IdleGpus, UnrequestedGpus]:
    return (
        IdleGpus(idle=2, of=8, cost=idle, text="2 of 8"),
        UnrequestedGpus(gpus=1, nodes=1, cost=unrequested),
    )


@pytest.mark.anyio
async def test_gpus_are_summed_where_priced_and_a_failing_contract_keeps_its_own_row(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    gpus = {"acme": gpus_of(100.0, 40.0), "cafe": gpus_of(None, 10.0)}  # cafe: no GPU price

    async def workloads(st: Store, c: Any, now: float) -> Workloads:
        cost = Cost(waste=1.0, requests=2.0)
        return Workloads(
            contract=c.id, clusters={}, total=Waste(), currency="USD", cost=cost,
            workloads=[], learning=[], excluded=[], gpus=gpus.get(c.id, gpus_of(1.0, 1.0))[0],
        )  # fmt: skip

    async def nodes(st: Store, c: Any, now: float) -> Any:
        if c.id == "bad":  # after its cost was read: still never in the totals
            raise RuntimeError("Authorization: Bearer s3cr3tvalue1234567890abcd")
        one = gpus[c.id][1]
        return SimpleNamespace(
            clusters=[SimpleNamespace(unrequested_gpus=x) for x in (one, one, None)]
        )

    monkeypatch.setattr(webapp, "workloads_of", workloads)
    monkeypatch.setattr(webapp, "nodes_of", nodes)
    fleet = await webapp.fleet_of(store, [contract("acme"), contract("bad"), contract("cafe")], NOW)
    rows = {r.id: r for r in fleet.rows}
    assert rows["acme"].unrequested_gpus == UnrequestedGpus(gpus=2, nodes=2, cost=80.0)
    assert rows["cafe"].idle_gpus is not None and rows["cafe"].idle_gpus.cost is None
    (usd,) = fleet.totals
    assert (usd.contracts, usd.idle_gpus, usd.unrequested_gpus) == (2, 100.0, 100.0)
    assert rows["bad"].error == "RuntimeError: Authorization: Bearer [REDACTED]"
    assert "s3cr3t" not in rows["bad"].model_dump_json()


@pytest.mark.anyio
async def test_unrequested_gpus_with_one_unpriced_cluster_have_no_cost(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def nodes(st: Store, c: Any, now: float) -> Any:
        both = (UnrequestedGpus(gpus=1, nodes=1, cost=5.0), UnrequestedGpus(gpus=2, nodes=1))
        return SimpleNamespace(clusters=[SimpleNamespace(unrequested_gpus=x) for x in both])

    monkeypatch.setattr(webapp, "nodes_of", nodes)
    (r,) = (await webapp.fleet_of(store, [priced("acme", "USD", 0.05)], NOW)).rows
    assert r.unrequested_gpus == UnrequestedGpus(gpus=3, nodes=2, cost=None)  # never part of it


def test_each_clusters_relay_and_actuator_with_its_version_and_protocol() -> None:
    relays = relay.Relays(clock=lambda: 1700000000.0)

    async def close() -> None:
        pass

    from pydantic import SecretStr

    from squero import contracts

    clusters = [
        {"name": "test", "context": "test", "namespaces": ["shop"]},  # neither: not listed
        {"name": "edge", "namespaces": ["shop"], "via": "relay", "relay_token_secret": "r1"},
        {"name": "far", "namespaces": ["shop"], "via": "relay", "relay_token_secret": "r2"},
    ]
    for cl in clusters[1:]:
        cl["actuator_token_secret"] = f"a{cl['name']}"
    secrets = {k: SecretStr(k * 40) for k in ("r1", "r2", "aedge", "afar")}
    two = contracts.build(
        {
            "id": "acme",
            "name": "acme",
            "clusters": clusters,
            "integrations": [{"name": "kubernetes"}],
        },
        secrets=secrets,
    )
    relays.live[("acme", "edge")] = relay.Live(None, close, 1700000600.0, None, "0.63.0")  # type: ignore[arg-type]
    protocol.see("acme", "edge", "Relay", "1", 1700000600.0)
    protocol.see("acme", "edge", "Actuator", None, 1700000600.0)  # 0.62.0: no version sent
    protocol.see("acme", "far", "Actuator", "3", 1700000600.0, "0.70.0")
    protocol.see("bank", "far", "Relay", "9", 1700000600.0)  # another Contract's: never here
    got = [(p.cluster, p.part, p.text, p.ok) for p in webapp.parts_of(two, relays)]
    assert got == [
        ("edge", "Relay", "Relay 0.63.0, Protocol 1", True),
        ("edge", "Actuator", "Actuator version unknown, Protocol 1", True),
        (
            "far",
            "Relay",
            "Relay not connected since the Hub started at 2023-11-14 22:13 UTC",
            False,
        ),
        (
            "far",
            "Actuator",
            "Actuator in far speaks Protocol 3, this Hub 1: upgrade the Hub",
            False,
        ),
    ]
    assert webapp.parts_of(contract("acme"), relays) == []  # no Relay, no Actuator


def test_the_fleet_route_needs_the_identity_and_the_cookie(
    settings: Any, fake_cluster: Any, database_url: str
) -> None:
    from tests.test_console import AS_OPERATOR, VIEW
    from tests.test_webapp import hub, seed

    seed(database_url, ("acme", NOW, row("api")), ("bank", NOW, row("web")))
    with hub(settings, fake_cluster, database_url) as c:
        fleet = c.get("/api/v1/fleet", headers=VIEW).json()
        no_cookie = c.get("/api/v1/fleet", headers=AS_OPERATOR).status_code
        nobody = c.get("/api/v1/fleet").status_code
    assert [r["id"] for r in fleet["rows"]] == ["acme", "bank"]
    assert no_cookie == 401 and nobody == 403
