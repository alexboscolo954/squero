"""Nodes (slice 15.3c): the money of a DaemonSet applied per instance type. Where its
cluster's Actuator applies the types, its Waste, the cost of its requests and its realized
Savings go by each type's pods; its pooled Sizing is shown, not applied."""

from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from squero import actuator, blockers, sizing, usage, webapp
from squero.config import Settings
from squero.store import Store, connect
from tests.fake_prometheus import FakePrometheus
from tests.test_actuator import A_TOKEN, AUTO, B_TOKEN, as_actuator, automating, hub, run, sql
from tests.test_actuator import sized as stored_row
from tests.test_could_go import node, pod, read_of, sizing_row
from tests.test_prices import GI, priced
from tests.test_realized import PRICES, hourly, sized, store  # noqa: F401
from tests.test_sizing_store import NOW, H
from tests.test_types import SHOP, _prom, a_node, acting, agent, fleet

WEEK = 7 * 86400


def typed_rows(fake: FakePrometheus, typed: set[str] | None) -> dict[str, Any]:
    types = usage.pod_types(_prom(fake, SHOP), SHOP, WEEK, NOW)
    (u,) = usage.usage(_prom(fake, SHOP), SHOP, WEEK, NOW, types=types).rows
    (row,) = sizing.rows(acting(), {WEEK: [("test", u)]}, NOW, typed=typed)
    return row


# --- Waste and the pooled line ------------------------------------------------------------


def test_where_its_actuator_applies_the_types_its_waste_is_theirs() -> None:
    fake = fleet()  # two idle m5.large pods asking 500m, a busy m5.4xlarge one asking 2 cores
    pooled, by_type = typed_rows(fake, None), typed_rows(fake, {"test"})
    assert "by_type" not in pooled and by_type["by_type"] is True
    assert typed_rows(fake, {"elsewhere"}).get("by_type") is None  # another cluster's Actuator
    for g in by_type["groups"]:
        assert g["live"] is True and g["share"] == g["pods"]  # every type there all along
        assert g["waste"] == pytest.approx(
            {
                res: max(0.0, g[res]["current"] - g[res]["target"]) * g["share"]
                for res in ("cpu", "memory")
            }
        )
    assert by_type["waste"] == {
        res: sum(g["waste"][res] for g in by_type["groups"]) for res in ("cpu", "memory")
    }
    # the pooled Waste counts every pod against the busy pod's 2 cores; by type it doesn't
    assert pooled["waste"]["cpu"] > by_type["waste"]["cpu"]
    for key in ("cpu", "memory", "state", "pods", "requests"):  # the pooled line is kept
        assert pooled[key] == by_type[key], key


def test_a_type_still_learning_has_no_waste() -> None:
    fake = FakePrometheus()
    for i in range(29):
        a_node(fake, f"s{i}", "m5.large")
        agent(fake, f"agent-s{i}", f"s{i}")
    a_node(fake, "big", "m5.4xlarge")
    agent(fake, "agent-big", "big", cores=1.0, peak=900.0 * 2**20)
    a_node(fake, "new", "c5.large")
    agent(fake, "agent-new", "new", samples=5)  # minutes of history
    by_type = typed_rows(fake, {"test"})
    learning = next(g for g in by_type["groups"] if g["type"] == "c5.large")
    assert learning["state"] == "learning" and "waste" not in learning
    # its pod ran minutes of the window: counted for that time, so the types add up to the
    # container's pods and its requests cost no more than its pods asked (found live)
    assert learning["pods"] == 1.0 and 0.0 < learning["share"] < 0.01  # shown, and counted
    assert sum(g["share"] for g in by_type["groups"]) == pytest.approx(by_type["pods"], abs=0.01)
    cost = sizing.requested(by_type)[0]
    assert cost == pytest.approx(
        sum(g["requests"]["cpu"] * g["share"] for g in by_type["groups"]), abs=0.01
    )
    assert by_type["waste"]["cpu"] == pytest.approx(
        sum(g["waste"]["cpu"] for g in by_type["groups"] if g["state"] == "ready")
    )


def group(kind: str, pods: float, cpu: float | None, gib: float = 1.0, **more: Any) -> Any:
    return {
        "type": kind,
        "pods": pods,
        "state": "ready",
        "requests": {"cpu": cpu, "memory": gib * GI},
    } | more


def test_its_requests_cost_what_each_types_pods_ask() -> None:
    groups = [group("", 1.0, 0.01), group("m5.4xlarge", 1.0, 0.349), group("c5.large", 1.0, 0.5)]
    row = {"pods": 4.0, "requests": {"cpu": 0.5, "memory": 2 * GI}, "groups": groups}
    assert sizing.requested(row) == (2.0, 8 * GI)  # the largest request, every pod
    cpu, memory = sizing.requested({**row, "by_type": True})
    assert cpu == pytest.approx(0.01 + 0.349 + 0.5 + 0.5)  # one pod on no known node: 500m
    assert memory == pytest.approx(3 * GI + 2 * GI)
    rounded = {**row, "by_type": True, "pods": 2.9}  # rounded parts: never fewer than none
    assert sizing.requested(rounded)[0] == pytest.approx(0.01 + 0.349 + 0.5)
    unset = [group("a", 2.0, None), group("b", 1.0, 0.2)]  # a type whose pods ask no cpu
    assert sizing.requested({**row, "by_type": True, "pods": 3.0, "groups": unset})[0] == 0.2


def test_the_hub_mcp_and_the_api_say_what_is_applied() -> None:
    from squero import hubmcp

    g = {"type": "m5.large", "pods": 2.0, "state": "learning", "history_h": 1.0}
    base = {"workload": "w", "cluster": "test", "namespace": "shop", "kind": "DaemonSet"}
    base |= {"name": "agent", "container": "app", "policy": "p", "state": "ready", "groups": [g]}
    plain = webapp.Container.model_validate(base)
    typed = webapp.Container.model_validate({**base, "by_type": True})
    assert (plain.by_type, typed.by_type) == (False, True)
    assert hubmcp._by_type(plain)["applied"] == "the one Sizing, to every pod"
    assert hubmcp._by_type(typed)["applied"] == "by_instance_type"
    drop = webapp.Drop.model_validate(
        {"workload": "w", "resource": "cpu", "at": 0, "before": 1, "after": 0.1, "sizing": 0.1}
        | {"pods": 2, "saved": 1.8, "type": ""}
    )
    assert drop.type == ""  # nodes without the label: a type, not none


# --- the Hub remembers what its Actuator applies --------------------------------------------


def test_each_pull_says_whether_its_actuator_applies_types_stored_only_when_it_changes(
    settings: Settings, database_url: str
) -> None:
    acme, bank = automating("acme", A_TOKEN, AUTO), automating("bank", B_TOKEN, AUTO)
    kinds = {"kinds": ",".join(actuator.KINDS)}

    def stored_flags() -> list[Any]:
        return sql(
            database_url,
            ("select contract_id, cluster, types, at from cluster_actuators order by 1", ()),
        )

    with TestClient(hub(settings, database_url, acme, bank)) as c:
        c.get("/actuator/desired", headers=as_actuator(A_TOKEN), params=kinds | {"types": "1"})
        c.get("/actuator/desired", headers=as_actuator(B_TOKEN), params=kinds)
        first = stored_flags()
        c.get("/actuator/desired", headers=as_actuator(A_TOKEN), params=kinds | {"types": "1"})
        again = stored_flags()
        c.get("/actuator/desired", headers=as_actuator(A_TOKEN), params=kinds)  # downgraded
        older = stored_flags()
    assert [(r[0], r[1], r[2]) for r in first] == [("acme", "test", True), ("bank", "test", False)]
    assert again == first  # the same said again: kept as it was, its time too
    assert [(r[0], r[2]) for r in older] == [("acme", False), ("bank", False)]


@pytest.mark.anyio
async def test_the_clusters_whose_actuator_applies_types_are_each_contracts_own(
    store: Store,  # noqa: F811
) -> None:
    await store.actuator_types("acme", "test", True, 1.0)
    await store.actuator_types("acme", "edge", False, 1.0)
    await store.actuator_types("bank", "test", False, 1.0)
    assert await store.typed_clusters("acme") == {"test"}
    assert await store.typed_clusters("bank") == set()
    await store.actuator_types("bank", "test", True, 2.0)
    await store.actuator_types("acme", "test", True, 3.0)  # the same: its time kept
    assert await store.typed_clusters("bank") == {"test"}
    assert await store.typed_clusters("acme") == {"test"}
    at = await store._all(
        "select contract_id, cluster, at from cluster_actuators order by 1, 2", ()
    )
    assert [(r["contract_id"], r["cluster"], r["at"]) for r in at] == [
        ("acme", "edge", 1.0),
        ("acme", "test", 1.0),
        ("bank", "test", 2.0),
    ]


@pytest.mark.anyio
async def test_the_hourly_run_sizes_by_type_where_its_actuator_applies_them(
    store: Store,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Any] = []

    def rows(*args: Any) -> list[Any]:
        seen.append(args[5] if len(args) > 5 else None)
        return []

    unread: list[Any] = []

    async def realize(*args: Any) -> None:
        unread.append(args[-1])

    found = usage.Collected({WEEK: []}, {"test": None}, untyped={"test"})
    monkeypatch.setattr(usage, "collect", lambda *a, **k: found)
    monkeypatch.setattr(sizing, "rows", rows)
    monkeypatch.setattr(sizing, "realize", realize)
    c = acting()
    await store.actuator_types(c.id, "test", True, 1.0)
    await store.actuator_types("other", "test", True, 1.0)
    await sizing.store_run(store, c, NOW)
    await store.actuator_types(c.id, "test", False, 2.0)
    await sizing.store_run(store, c, NOW + H)
    assert seen == [{"test"}, set()]
    assert unread == [{"test"}, {"test"}]  # its types unread: realize is told


def test_a_typed_actuator_gets_nothing_while_every_type_is_learning(
    settings: Settings, database_url: str
) -> None:
    acme = automating("acme", A_TOKEN, AUTO)
    learning = [{"type": t, "state": "learning", "pods": 1, "history_h": 1} for t in ("a", "b")]
    with TestClient(hub(settings, database_url, acme)) as c:
        sql(
            database_url,
            run("acme", '{"test": null}'),
            stored_row("acme", 1, "agent", kind="DaemonSet", groups=learning),
        )
        kinds = {"kinds": ",".join(actuator.KINDS)}
        knows = c.get(
            "/actuator/desired", headers=as_actuator(A_TOKEN), params=kinds | {"types": "1"}
        )
        older = c.get("/actuator/desired", headers=as_actuator(A_TOKEN), params=kinds)
    assert knows.json()["automated"] == []  # each pod keeps its template's
    assert [w["name"] for w in older.json()["automated"]] == ["agent"]  # its one Sizing


# --- nodes that could go, after the Sizings -------------------------------------------------


def test_after_the_sizings_a_daemonsets_pod_asks_its_nodes_types_sizing() -> None:
    def daemon(name: str) -> Any:
        return pod(name, 0.5, workload=("DaemonSet", "agent"))

    small, big, new = node("a", daemon("a1")), node("b", daemon("b1")), node("c", daemon("c1"))
    small["type"], big["type"], new["type"] = "m5.large", "m5.4xlarge", "c5.large"
    groups = [
        {
            "type": "m5.large",
            "state": "ready",
            "cpu": {"target": 0.05},
            "memory": {"target": 0.25 * GI},
        },
        {"type": "m5.4xlarge", "state": "ready", "cpu": {"target": 0.4}},
        {"type": "c5.large", "state": "learning"},
    ]
    row = sizing_row(kind="DaemonSet", name="agent", cpu=0.3, groups=groups)
    after = blockers.after(read_of(small, big, new), [{**row, "by_type": True}])["nodes"]
    assert [(n["own"][0]["cpu"], n["own"][0]["memory"]) for n in after] == [
        (0.05, 0.25 * GI),  # its type's
        (0.4, GI),  # its type's cpu; memory, which that type keeps, as it is
        (0.5, GI),  # a type still learning: as it is
    ]
    pooled = blockers.after(read_of(small, big, new), [row])["nodes"]  # not applied by type
    assert [n["own"][0]["cpu"] for n in pooled] == [0.3, 0.3, 0.3]


# --- realized Savings ------------------------------------------------------------------------


def daemon(groups: list[dict[str, Any]], live: bool = True, pods: float = 3.0) -> dict[str, Any]:
    """A stored row of DaemonSet agent applied by type, its pooled request the largest."""
    asks = max((g["requests"]["cpu"] or 0.0) for g in groups) if groups else 1.0
    r = sized(asks, 0.3, pods=pods, name="agent", live=live)
    return r | {
        "kind": "DaemonSet",
        "workload": "test/shop/DaemonSet/agent/app",
        "by_type": True,
        "groups": groups,
    }


def kind(
    name: str, cpu: float, target: float, pods: float = 1.0, live: bool = True, **more: Any
) -> dict[str, Any]:
    g = group(name, pods, cpu, live=live, **more)
    return g | {"cpu": {"action": "lower", "current": cpu, "target": target}}


def drops(st: Store) -> Any:
    return st.realized("acme")


@pytest.mark.anyio
async def test_each_types_pods_realize_against_that_types_sizing(store: Store) -> None:  # noqa: F811
    c = priced(PRICES)
    await hourly(
        store, c, NOW, daemon([kind("m5.large", 0.5, 0.06, pods=2.0), kind("big", 2.0, 1.2)])
    )
    # its two pods, one of them on a node that came during the window: 1.5 of the container's
    lowered = kind("m5.large", 0.06, 0.06, pods=2.0, share=1.5)
    await hourly(store, c, NOW + H, daemon([lowered, kind("big", 2.0, 1.2)]))
    (e,) = await drops(store)
    assert (e["type"], e["resource"], e["before"], e["after"], e["sizing"]) == (
        "m5.large",
        "cpu",
        0.5,
        0.06,
        0.06,
    )
    out = await sizing.savings(store, c, NOW + 2 * H)
    (d,) = out["drops"]
    assert (d["type"], d["pods"]) == ("m5.large", 1.5)  # that type's share, not the row's 3
    assert d["saved"] == pytest.approx(0.44 * 1.5)
    # its pods' request rises: the drop gives it back; back at before, it ends
    await hourly(
        store,
        c,
        NOW + 2 * H,
        daemon([kind("m5.large", 0.2, 0.06, pods=2.0), kind("big", 2.0, 1.2)]),
    )
    assert [e["after"] for e in await drops(store)] == [0.2]
    await hourly(
        store,
        c,
        NOW + 3 * H,
        daemon([kind("m5.large", 0.5, 0.06, pods=2.0), kind("big", 2.0, 1.2)]),
    )
    assert await drops(store) == []


@pytest.mark.anyio
async def test_a_types_drop_ends_with_its_type_or_when_types_are_no_longer_applied(
    store: Store,  # noqa: F811
) -> None:
    c = priced(PRICES)
    both = [kind("a", 0.5, 0.06), kind("b", 0.5, 0.06)]
    low = [kind("a", 0.06, 0.06), kind("b", 0.06, 0.06)]
    await hourly(store, c, NOW, daemon(both))
    await hourly(store, c, NOW + H, daemon(low))
    assert sorted(e["type"] for e in await drops(store)) == ["a", "b"]
    # a type whose requests are not its live pods' (the window's largest, the old one): kept
    await hourly(store, c, NOW + 2 * H, daemon([kind("a", 0.5, 0.06, live=False), low[1]]))
    assert [e["after"] for e in await drops(store)] == [0.06, 0.06]
    await hourly(store, c, NOW + 3 * H, daemon([low[1]]))  # type a gone
    assert [e["type"] for e in await drops(store)] == ["b"]
    plain = {k: v for k, v in daemon(low).items() if k != "by_type"}  # its groups still shown
    await hourly(store, c, NOW + 4 * H, plain)  # its Actuator no longer applies types
    assert await drops(store) == []


def pooled(*groups: dict[str, Any], cpu: float = 1.0, live: bool = True) -> dict[str, Any]:
    """A DaemonSet's row with its groups shown (stored since 15.3a), its one Sizing applied."""
    r = {k: v for k, v in daemon(list(groups)).items() if k != "by_type"}
    return r | {"requests": {"cpu": cpu, "memory": GI}, "live": live}


async def held_at_300m(store: Store, c: Any) -> None:  # noqa: F811
    """A pooled drop in place: every pod from 1 core to its one Sizing's 300m."""
    await hourly(store, c, NOW, pooled(kind("a", 1.0, 0.06, pods=2.0), kind("big", 1.0, 0.3)))
    held = [kind("a", 0.3, 0.06, pods=2.0), kind("big", 0.3, 0.3)]
    await hourly(store, c, NOW + H, pooled(*held, cpu=0.3))
    (e,) = await store.realized("acme")
    assert (e["type"], e["before"], e["after"]) == (None, 1.0, 0.3)


@pytest.mark.anyio
async def test_a_containers_drop_becomes_one_per_type_counted_once(store: Store) -> None:  # noqa: F811
    c = priced(PRICES)
    await held_at_300m(store, c)
    # its Actuator applies types: the quiet type's pods from 300m to 60m (its Sizing), a
    # learning one at 1 core, and a type whose pods are not live now
    learning = group("new", 1.0, 1.0, live=True) | {"state": "learning"}
    gone = kind("old", 0.06, 0.06, live=False)
    now = [kind("a", 0.06, 0.06, pods=2.0), kind("big", 0.3, 0.3), learning, gone]
    await hourly(store, c, NOW + 2 * H, daemon(now, pods=5.0))
    found = sorted(await drops(store), key=lambda e: e["type"])
    assert [(e["type"], e["before"], e["after"], e["sizing"]) for e in found] == [
        ("a", 1.0, 0.06, 0.3),  # from the same run and request, at what its pods ask
        ("big", 1.0, 0.3, 0.3),
    ]  # the learning type asks what it did: nothing saved there; the type not live, none
    # the quiet type's own 300m -> 60m since the last run is in the drop made: counted once
    out = await sizing.savings(store, c, NOW + 3 * H)
    assert out["rate"]["cpu"] == pytest.approx(0.94 * 2 + 0.7)
    await hourly(store, c, NOW + 3 * H, daemon(now, pods=5.0))  # the next run adds nothing
    assert len(await drops(store)) == 2


@pytest.mark.anyio
async def test_a_containers_drop_waits_while_none_of_its_pods_is_live(store: Store) -> None:  # noqa: F811
    c = priced(PRICES)
    await held_at_300m(store, c)
    asleep = [kind("a", 0.06, 0.06, pods=2.0, live=False), kind("big", 0.3, 0.3, live=False)]
    row = daemon(asleep, live=False) | {"requests": {"cpu": 0.3, "memory": GI}}
    await hourly(store, c, NOW + 2 * H, row)  # applied by type, nothing read from live pods
    assert [(e["type"], e["after"]) for e in await drops(store)] == [(None, 0.3)]


@pytest.mark.anyio
async def test_type_drops_are_stored_apart_by_type_and_contract(store: Store) -> None:  # noqa: F811
    keys = {
        "cluster": "test",
        "namespace": "shop",
        "kind": "DaemonSet",
        "name": "agent",
        "container": "app",
    }
    e = keys | {
        "resource": "cpu",
        "at": NOW,
        "prev_run": 1,
        "before": 1.0,
        "after": 0.1,
        "sizing": 0.1,
    }
    await store.save_realized(
        "acme", [e | {"type": "a"}, e | {"type": ""}, e | {"type": None}], {}, set()
    )
    await store.save_realized("acme", [e | {"type": "a"}, e | {"type": None}], {}, set())  # again
    await store.save_realized("bank", [e | {"type": "a"}], {}, set())
    found = sorted("-" if d["type"] is None else d["type"] for d in await store.realized("acme"))
    assert found == ["", "-", "a"]
    assert [d["type"] for d in await store.realized("bank")] == ["a"]


@pytest.mark.anyio
async def test_a_drop_stored_before_types_keeps_its_place(store: Store, database_url: str) -> None:  # noqa: F811
    """Migration 0040: a drop stored before it is the container's (type null)."""
    with psycopg.connect(database_url) as conn:
        conn.execute(
            "insert into realized (contract_id, cluster, namespace, kind, name, container, "
            "resource, at, prev_run, before, after, sizing) values "
            "('acme', 'test', 'shop', 'Deployment', 'api', 'app', 'cpu', 1, 1, 1, 0.1, 0.1)"
        )
    (d,) = await store.realized("acme")
    assert d["type"] is None


@pytest.mark.anyio
async def test_a_chain_of_drops_converted_still_adds_up_to_the_first_request_less_the_types(
    store: Store,  # noqa: F811
) -> None:
    """Each lowering is a drop of its own (1 core to 500m, then 500m to 300m): converted,
    only the lowest goes down to what a type asks, so the chain is counted once."""
    c = priced(PRICES)
    keys = dict(zip(sizing.KEYS, ("test", "shop", "DaemonSet", "agent", "app"), strict=True))
    chain = [
        keys | {"resource": "cpu", "at": NOW - 2 * H, "prev_run": 1, "before": 1.0, "after": 0.5},
        keys | {"resource": "cpu", "at": NOW - H, "prev_run": 2, "before": 0.5, "after": 0.3},
    ]
    await store.save_realized("acme", [e | {"sizing": e["after"]} for e in chain], {}, set())
    now = [kind("a", 0.06, 0.06, pods=2.0), kind("big", 0.4, 0.4), kind("busy", 0.7, 0.7)]
    await hourly(store, c, NOW, daemon(now, pods=4.0))
    found = sorted((e["type"], e["before"], e["after"]) for e in await drops(store))
    assert found == [
        ("a", 0.5, 0.06),  # the lowest drop, down to what the type asks
        ("a", 1.0, 0.5),  # the other, never below its own after
        ("big", 0.5, 0.4),
        ("big", 1.0, 0.5),
        ("busy", 1.0, 0.7),  # asks more than the lowest drop's before: only the first saves
    ]
    out = await sizing.savings(store, c, NOW + H)
    assert out["rate"]["cpu"] == pytest.approx((1.0 - 0.06) * 2 + (1.0 - 0.4) + (1.0 - 0.7))


@pytest.mark.anyio
async def test_a_failed_read_of_the_types_leaves_a_daemonsets_drops_as_they_are(
    store: Store,  # noqa: F811
) -> None:
    c = priced(PRICES)
    steady = kind("c", 0.5, 0.5)  # a type that keeps 500m: the row's request, the largest
    await hourly(store, c, NOW, daemon([kind("a", 0.5, 0.06), kind("b", 0.5, 0.06), steady]))
    await hourly(store, c, NOW + H, daemon([kind("a", 0.06, 0.06), kind("b", 0.06, 0.06), steady]))
    assert len(await drops(store)) == 2
    # the next run can't read the pods' types: its row has no groups and isn't by type, and
    # its pooled request reads 300m, its pooled Sizing shown before: no drop is made of it
    blind = {k: v for k, v in daemon([]).items() if k not in ("by_type", "groups")}
    blind |= {"requests": {"cpu": 0.3, "memory": GI}}
    before, status = await store.latest_sizings(c.id, {"test"})
    run_id = await store.claim_sizing_run(c.id, NOW + 2 * H, 0)
    assert run_id is not None
    await store.finish_sizing_run(run_id, c.id, {"test": None}, [blind])
    await sizing.realize(store, c, NOW + 2 * H, before, status, [blind], {"test": None}, {"test"})
    assert sorted(e["type"] for e in await drops(store)) == ["a", "b"]  # kept, none added


def test_sizing_savings_names_a_drops_type(
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import asyncio

    from squero import cli

    c = priced(PRICES)
    keys = dict(zip(sizing.KEYS, ("test", "shop", "DaemonSet", "agent", "app"), strict=True))
    e = keys | {"resource": "cpu", "at": NOW, "prev_run": 1, "before": 0.5, "after": 0.06}
    rows = [daemon([kind("m5.large", 0.06, 0.06), kind("", 0.06, 0.06)])]

    async def seed() -> None:
        async with connect(database_url) as pool:
            st = Store(pool)
            await st.migrate()
            await st.save_realized(
                "acme", [e | {"sizing": 0.06, "type": t} for t in ("m5.large", "")], {}, set()
            )
            run_id = await st.claim_sizing_run("acme", NOW, 0)
            assert run_id is not None
            await st.finish_sizing_run(run_id, "acme", {"test": None}, rows)

    asyncio.run(seed())
    monkeypatch.setenv("SQUERO_DATABASE_URL", database_url)
    monkeypatch.setattr(cli, "contract", lambda settings, cid: c)
    monkeypatch.setattr("time.time", lambda: NOW + H)
    assert cli.main(["sizing", "savings", "--contract", "acme"]) == 0
    lines = capsys.readouterr().out.splitlines()[1:]
    assert [line.split(" cpu:")[0] for line in sorted(lines)] == [
        "test/shop/DaemonSet/agent/app on m5.large",
        "test/shop/DaemonSet/agent/app on nodes with no instance type",
    ]
