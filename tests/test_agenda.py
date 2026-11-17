"""The Agenda (slice 11.4): every kind of item from what the Hub stores, in its fixed
order of urgency, within one Contract only."""

from typing import Any

import pytest
from pydantic import SecretStr

from squero import agenda, contracts
from squero.models import CheckResult, Evidence, Investigation
from squero.store import Store
from tests.conftest import REPORT, make_contract
from tests.test_sizing_store import NOW, row, stored
from tests.test_sizing_store import store as store
from tests.test_webapp import INTEGRATIONS

H = 3600.0
PRICES = {"prices": {"cpu": 0.04, "memory": 0.005}}


def contract(cid: str = "acme", **extra: Any) -> Any:
    clusters = {"test": ["shop"], "edge": ["shop"]}
    return make_contract(contract_id=cid, clusters=clusters, integrations=INTEGRATIONS, **extra)


def paused(cid: str, iid: str, deadline: float, actions: int = 1) -> Investigation:
    return Investigation(
        id=iid,
        contract_id=cid,
        title=f"alert {iid}",
        status="awaiting_approval",
        started=NOW - H,
        deadline=deadline,
        pending=[{"name": "kubernetes_evict_pod"}] * actions,
    )


async def failed_check(
    st: Store, cid: str, name: str, at: float, done: bool = True, errored: bool = False
) -> None:
    """done=False: its Run is still going, so it has no verdict yet. errored: its Run
    failed, so it has none at all."""
    result = CheckResult(
        passed=False, summary="no", evidence=[Evidence(source="t", finding="f")], report=REPORT
    )
    t = Investigation(
        id=f"{cid}-{name}", kind="check", contract_id=cid, title=name,
        status="failed" if errored else "done" if done else "running", started=at,
        check_result=result if done and not errored else None,
    )  # fmt: skip
    await st.sync_checks(cid, {name: ("0 * * * *", at)})
    assert await st.claim_check(t, name, at, NOW + H, None) == "started"
    await st.save(t)


async def no_bill(*_: Any, **__: Any) -> list[dict[str, Any]]:
    return []


@pytest.fixture(autouse=True)
def _no_prometheus(monkeypatch: pytest.MonkeyPatch) -> None:
    """billing.report reads node-hours from Prometheus: its rows are given instead."""
    monkeypatch.setattr("squero.billing.report", no_bill)


@pytest.mark.anyio
async def test_every_kind_comes_in_its_order_of_urgency(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = contract(**PRICES)
    # waste, ranked by its cost: four wasting Workloads, one not
    big, mid, small, tiny = (
        row(n, waste={"cpu": w, "memory": 0.0})
        for n, w in (("big", 3.0), ("mid", 2.0), ("small", 1.0), ("tiny", 0.5))
    )
    await stored(
        store, "acme", NOW, {"test": None, "edge": None}, big, mid, small, tiny,
        row("none", waste={"cpu": 0.0, "memory": 0.0}),
    )  # fmt: skip
    # edge's reads then fail
    await stored(store, "acme", NOW + H, {"edge": "Prometheus unreachable"})
    await store.fail_bill("acme", "test", "HTTP 403 from ce.us-east-1.amazonaws.com", NOW - H)
    await store.fail_bill("acme", "gone", "a cluster no longer in the Contract", NOW)
    await store.insert(paused("acme", "later", NOW + 3 * H, actions=2))
    await store.insert(paused("acme", "sooner", NOW + 2 * H))
    await store.insert(Investigation(id="run1", contract_id="acme", title="disk", started=NOW))
    await failed_check(store, "acme", "older", NOW - 2 * H)
    await failed_check(store, "acme", "newer", NOW - H)
    await failed_check(store, "acme", "going", NOW + 60, done=False)  # running: no verdict
    await failed_check(store, "acme", "broke", NOW - 3 * H, errored=True)
    await store.turn_on_kill_switch("acme", "console:op", NOW - H)

    async def bills(*_: Any, **__: Any) -> list[dict[str, Any]]:
        span = {"start": "2026-11-01", "end": "2026-11-07"}
        return [
            {"cluster": "test", "within": False, "gap": -0.08, **span},
            {"cluster": "edge", "within": False, "gap": 0.20, **span},
            {"cluster": "far", "within": True, "gap": 0.01, **span},
            {"cluster": "none", "within": None, "gap": None, **span},
        ]

    monkeypatch.setattr("squero.billing.report", bills)
    a = await agenda.of(store, c, NOW + 2 * H)
    got = [(i.kind, i.id or i.workload or i.cluster) for i in a.items]
    assert got == [
        ("kill_switch", None),
        ("approval", "sooner"),
        ("approval", "later"),
        ("check", "acme-newer"),
        ("check", "acme-older"),
        ("check", "acme-broke"),  # its Run errored: no verdict, still failing
        ("stale", "test"),  # bill reads failing since NOW - H
        ("stale", "edge"),  # Sizing reads failing since NOW + H
        ("bill", "edge"),  # the bigger gap first
        ("bill", "test"),
        ("investigation", "run1"),
        ("investigation", "acme-going"),  # a Check's Run still going is no failing Check
        ("waste", "test/shop/Deployment/big"),
        ("waste", "test/shop/Deployment/mid"),
        ("waste", "test/shop/Deployment/small"),
    ]
    titles = [i.title for i in a.items]
    assert titles[0] == "The Kill switch is on (console:op): approving is refused"
    assert titles[1] == "1 Proposed action waits for a decision: alert sooner"
    assert titles[2] == "2 Proposed actions wait for a decision: alert later"
    assert titles[3] == "Check newer failed"
    assert titles[5] == "Check broke could not run"
    assert titles[6] == "Bill reads failing: HTTP 403 from ce.us-east-1.amazonaws.com"
    assert titles[7] == "Sizing reads failing: Prometheus unreachable"
    assert titles[8] == "Bill +20.0% from the estimate (2026-11-01 to 2026-11-07), outside 5%"
    assert titles[10] == "Running: disk"
    assert titles[12].startswith("Waste: ") and " USD a month, 3000m and 0Mi above" in titles[12]
    assert agenda.counts(a) == {
        "kill_switch": 1,
        "approval": 2,
        "check": 3,
        "stale": 2,
        "bill": 2,
        "investigation": 2,
        "waste": 3,
    }


@pytest.mark.anyio
async def test_waste_without_prices_is_in_cores_and_mib(store: Store) -> None:
    idle = row("idle", waste={"cpu": 0.0, "memory": 0.0})
    await stored(store, "acme", NOW, {"test": None}, row(waste={"cpu": 1.5, "memory": 2**30}), idle)
    a = await agenda.of(store, contract(), NOW)
    assert [i.title for i in a.items] == ["Waste: 1500m and 1024Mi above its Sizings"]


@pytest.mark.anyio
async def test_a_quiet_contract_has_an_empty_agenda(store: Store) -> None:
    a = await agenda.of(store, contract(), NOW)
    assert a.items == [] and agenda.counts(a) == {}


@pytest.mark.anyio
async def test_another_contracts_items_never_appear(store: Store) -> None:
    await stored(store, "bank", NOW, {"test": "down"}, row())
    await store.fail_bill("bank", "test", "boom", NOW)
    await store.insert(paused("bank", "theirs", NOW + H))
    await store.insert(Investigation(id="their-run", contract_id="bank", title="x", started=NOW))
    await failed_check(store, "bank", "theirs", NOW)
    await store.turn_on_kill_switch("bank", "console:op", NOW)
    a = await agenda.of(store, contract(), NOW)
    assert a.items == []
    assert len((await agenda.of(store, contract("bank"), NOW)).items) == 6


@pytest.mark.anyio
async def test_an_offline_relay_is_data_the_hub_cant_read(store: Store) -> None:
    from squero import relay
    from tests.test_relay import A_TOKEN, B_TOKEN, relayed

    async def close() -> None:
        pass

    offline = await agenda.of(store, relayed("acme", A_TOKEN), NOW)
    relay.HUB.live[("bank", "edge")] = relay.Live(None, close, NOW, None)  # type: ignore[arg-type]
    try:
        still = await agenda.of(store, relayed("acme", A_TOKEN), NOW)  # B's Relay isn't A's
        up = await agenda.of(store, relayed("bank", B_TOKEN), NOW)
    finally:
        del relay.HUB.live[("bank", "edge")]
    (item,) = offline.items
    started = relay.HUB.started
    assert item.kind == "stale" and item.cluster == "edge" and item.at == started
    assert item.title.startswith("Relay not connected since the Hub started at ")
    assert item.title.endswith(": the Hub can't read cluster edge")
    assert still.items == offline.items and up.items == []


# --- audit A6b: an approved Workload leaves the waste ranking ---------------------------------


def actuated(cid: str = "acme") -> Any:
    """contract(cid), its cluster "test" running an Actuator, "edge" not."""
    data = contract(cid).model_dump()
    data["clusters"][0]["actuator_token_secret"] = "act"
    return contracts.build(data, secrets={"act": SecretStr("a" * 40)})


def wasting(name: str, cpu: float, cluster: str = "test", container: str = "app") -> Any:
    where = f"{cluster}/shop/Deployment/{name}/{container}"
    return row(name, cluster, container=container, workload=where, waste={"cpu": cpu, "memory": 0})


async def approve(st: Store, cid: str, run: int, *rows: dict[str, Any]) -> None:
    for r in rows:
        assert await st.approve_sizing(cid, run, r, "mcp:op", NOW) == "approved"


def ranked(a: agenda.Agenda) -> list[str | None]:
    return [i.workload if i.kind == "waste" else i.title for i in a.items]


@pytest.mark.anyio
async def test_an_approved_workload_leaves_the_waste_ranking_where_an_actuator_runs(
    store: Store,
) -> None:
    """big and mid (one container of two, then both) approved on "test": the next ones rise,
    counted as 2 Workloads. edgy approved on "edge", which runs no Actuator: still ranked.
    idle approved without waste: never ranked, never counted."""
    big, mid, side = wasting("big", 3.0), wasting("mid", 2.0), wasting("mid", 2.0, container="s")
    small, tiny, edgy = wasting("small", 1.0), wasting("tiny", 0.5), wasting("edgy", 0.7, "edge")
    idle = wasting("idle", 0.0)
    run = await stored(
        store, "acme", NOW, {"test": None, "edge": None}, big, mid, side, small, tiny, edgy, idle
    )
    await approve(store, "acme", run, big, mid, edgy, idle)
    a = await agenda.of(store, actuated(), NOW)
    assert ranked(a) == [
        "test/shop/Deployment/small",
        "edge/shop/Deployment/edgy",
        "test/shop/Deployment/tiny",
        "2 Workloads approved: their waste is read again within the hour",
    ]
    assert a.items[-1].kind == "approved" and a.items[-1].workload is None
    assert agenda.counts(a) == {"waste": 3}  # the line counts Workloads, not an item
    await approve(store, "acme", run, side)  # mid's other container: still one Workload
    assert ranked(await agenda.of(store, actuated(), NOW))[-1].startswith("2 Workloads")


@pytest.mark.anyio
async def test_one_approved_workload_is_said_once_and_back_after_the_next_run(
    store: Store,
) -> None:
    big, small = wasting("big", 3.0), wasting("small", 1.0)
    run = await stored(store, "acme", NOW, {"test": None, "edge": None}, big, small)
    await approve(store, "acme", run, big)
    assert ranked(await agenda.of(store, actuated(), NOW)) == [
        "test/shop/Deployment/small",
        "1 Workload approved: its waste is read again within the hour",
    ]
    await stored(store, "acme", NOW + H, {"test": None, "edge": None}, big, small)  # supersedes
    assert ranked(await agenda.of(store, actuated(), NOW + H)) == [
        "test/shop/Deployment/big",
        "test/shop/Deployment/small",
    ]


@pytest.mark.anyio
async def test_another_contracts_approval_never_hides_a_workload(store: Store) -> None:
    big, small = wasting("big", 3.0), wasting("small", 1.0)
    for cid in ("acme", "bank"):
        run = await stored(store, cid, NOW, {"test": None, "edge": None}, big, small)
    await approve(store, "bank", run, big)  # the same cluster, namespace and name, in bank
    assert ranked(await agenda.of(store, actuated(), NOW)) == [
        "test/shop/Deployment/big",
        "test/shop/Deployment/small",
    ]
    assert ranked(await agenda.of(store, actuated("bank"), NOW)) == [
        "test/shop/Deployment/small",
        "1 Workload approved: its waste is read again within the hour",
    ]
