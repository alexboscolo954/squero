"""Idle GPUs (slice 17.2a): each GPU a container requests judged from its busiest hour,
stored with its Sizing, counted per Workload by the share of the window it was held,
priced by a typed `gpu`, beside Waste and never in it; `sizing waste` and the digest."""

import asyncio
import json
from typing import Any

import pytest

from squero import cli, contracts, listprices, sizing
from squero.servers.sizing import container as agent_view
from squero.store import Store, connect
from squero.usage import Gpu
from tests.test_sizing_store import (
    NOW,
    WEEK,
    container,
    lab_fake,
    prometheus_contract,
    row,
    rows_of,
    store,  # noqa: F401
    stored,
)
from tests.test_usage import dcgm

H = 3600.0


def gpus_of(*gpus: tuple[str, Gpu], asked: float = 1.0, hours: int = 48) -> Any:
    """A ready container (48 h of flat usage) that requests `asked` GPUs a pod."""
    u = container(hours=hours)
    u.gpus = asked
    u.gpu = dict(gpus)
    return u


def gpu(busiest: float | None, hours: int = 168, **kw: Any) -> Gpu:
    return Gpu("n1", "api-0", busiest, "engine" if busiest is not None else None,
               {NOW - i * H for i in range(hours)}, **kw)  # fmt: skip


def priced(gpu_price: float | None = 0.5, **cluster: Any) -> contracts.Contract:
    data = prometheus_contract().model_dump()
    data["prices"] = {"cpu": 0.04, "memory": 0.005, "currency": "USD"}
    if gpu_price is not None:
        data["prices"]["gpu"] = gpu_price
    if cluster:
        data["clusters"][0] |= cluster
    return contracts.build(data)


# --- the verdict --------------------------------------------------------------------


def test_each_gpu_gets_its_verdict_and_the_share_of_the_window_it_was_held() -> None:
    u = gpus_of(
        ("idle", gpu(0.049)),
        ("busy", gpu(0.05)),  # 5% is not under 5%
        ("young", gpu(0.0, hours=23)),  # under production's 24 h of history
        ("ready", gpu(0.0, hours=24)),
        ("mig", gpu(0.0, shared=True)),
        ("unread", gpu(None)),
        ("long", gpu(0.0, hours=170)),  # an inclusive range: never over the window
    )
    (r,) = rows_of(u)
    assert r["state"] == "ready" and r["gpus"]["asked"] == 1.0
    got = {g["id"]: (g["verdict"], round(g["held"], 3)) for g in r["gpus"]["gpus"]}
    assert got == {
        "idle": ("idle", 1.0),
        "busy": ("busy", 1.0),
        "young": ("learning", round(23 / 168, 3)),
        "ready": ("idle", round(24 / 168, 3)),
        "mig": ("shared", 1.0),
        "unread": ("unread", 1.0),
        "long": ("idle", 1.0),
    }
    # only idle and busy GPUs count: shared, unread and learning ones are not judged
    judged = sizing.report([r], {"test": {}}, priced())["gpus"]
    assert judged["idle"] == pytest.approx(2 + 24 / 168)
    assert judged["of"] == pytest.approx(3 + 24 / 168)
    # and said: none idle must not read as all busy
    assert judged["not_judged"] == pytest.approx(
        {"learning": 23 / 168, "shared": 1.0, "unread": 1.0}
    )
    assert sizing.gpus_text(judged, "USD").endswith(
        "; 2.1 not judged (0.1 learning, 1 shared, 1 unread)"
    )
    one = next(g for g in r["gpus"]["gpus"] if g["id"] == "idle")
    assert {k: one[k] for k in ("node", "pod", "busiest", "by", "hours")} == {
        "node": "n1",
        "pod": "api-0",
        "busiest": 0.049,
        "by": "engine",
        "hours": 168,
    }


def test_a_container_asking_no_gpu_has_no_gpus_and_an_excluded_one_none_judged() -> None:
    (plain,) = rows_of(container())
    assert "gpus" not in plain
    data = prometheus_contract().model_dump()
    data["sizing"] = {"rules": [{"name": "api", "policy": "off"}]}
    (off,) = rows_of(gpus_of(("a", gpu(0.0))), c=contracts.build(data))
    assert off["state"] == "excluded" and "gpus" not in off


def test_a_learning_containers_gpus_are_stored_but_not_counted() -> None:
    (r,) = rows_of(gpus_of(("a", gpu(0.0)), hours=12))
    assert r["state"] == "learning" and r["gpus"]["gpus"][0]["verdict"] == "idle"
    out = sizing.report([r], {"test": {}}, priced())
    assert "gpus" not in out and out["learning"] == [r]


# --- counted and priced -------------------------------------------------------------


def test_a_pod_that_moved_across_three_gpus_counts_one_gpu_held_in_count_and_money() -> None:
    third = 56
    u = gpus_of(*((k, gpu(0.0, hours=third)) for k in ("g1", "g2", "g3")), ("busy", gpu(0.5)))
    rows = rows_of(u)
    out = sizing.report(rows, {"test": {}}, priced(0.5))
    (w,) = out["workloads"]
    assert w["gpus"]["idle"] == pytest.approx(1.0) and w["gpus"]["of"] == pytest.approx(2.0)
    assert w["gpus"]["cost"] == pytest.approx(1.0 * 0.5 * 730)
    assert out["gpus"] == {
        "idle": pytest.approx(1.0),
        "of": pytest.approx(2.0),
        "cost": pytest.approx(365.0),
        "not_judged": {},
    }
    assert sizing.gpus_text(out["gpus"], "USD") == "1 of 2 GPUs idle, 365.00 USD/month"


def test_idle_gpus_leave_waste_its_cost_and_the_ranking_alone() -> None:
    with_gpus = rows_of(gpus_of(("a", gpu(0.0))))
    without = rows_of(container())
    a = sizing.report(with_gpus, {"test": {}}, priced())
    b = sizing.report(without, {"test": {}}, priced())
    assert a["total"] == b["total"] and a["cost"] == b["cost"]
    assert a["workloads"][0]["cost"] == b["workloads"][0]["cost"]
    assert sizing.rank(a["workloads"][0]) == sizing.rank(b["workloads"][0])


def test_without_a_gpu_price_idle_gpus_are_counted_not_priced() -> None:
    rows = rows_of(gpus_of(("a", gpu(0.0))))
    out = sizing.report(rows, {"test": {}}, priced(None))
    assert out["gpus"] == {"idle": 1.0, "of": 1.0, "cost": None, "not_judged": {}}
    assert sizing.gpus_text(out["gpus"], "USD") == (
        "1 of 1 GPUs idle (no GPU price: set `gpu` in its prices)"
    )
    unpriced = sizing.report(rows, {"test": {}}, prometheus_contract())
    assert unpriced["gpus"]["cost"] is None
    assert sizing.gpus_text(unpriced["gpus"], None) == "1 of 1 GPUs idle"


def test_one_unpriced_cluster_leaves_the_total_unpriced() -> None:
    data = prometheus_contract().model_dump()
    data["prices"] = {"cpu": 0.04, "memory": 0.005, "gpu": 0.5}
    edge = {**data["clusters"][0], "name": "edge", "prices": {"cpu": 0.04, "memory": 0.005}}
    data["clusters"].append(edge)
    c = contracts.build(data)
    assert c.price("edge").gpu == 0.5  # type: ignore[union-attr]  # its own lacks gpu
    data["clusters"][1]["prices"]["gpu"] = 0.9
    assert contracts.build(data).price("edge").gpu == 0.9  # type: ignore[union-attr]
    data["prices"].pop("gpu")
    c = contracts.build(data)
    a = rows_of(gpus_of(("a", gpu(0.0))))
    b = [{**r, "workload": r["workload"].replace("test/", "edge/", 1), "cluster": "edge"}
         for r in rows_of(gpus_of(("b", gpu(0.0))))]  # fmt: skip
    out = sizing.report(a + b, {"test": {}, "edge": {}}, c)
    assert out["gpus"]["idle"] == 2.0 and out["gpus"]["cost"] is None
    assert {w["cluster"]: w["gpus"]["cost"] for w in out["workloads"]} == {
        "test": None,
        "edge": pytest.approx(0.9 * 730),
    }


def test_a_typed_gpu_price_survives_the_spot_blend() -> None:
    cl = priced(0.5, spot={"label": "spot=true", "discount": 0.3}).clusters[0]
    typed = contracts.ClusterPrices(cpu=0.04, memory=0.005, gpu=0.5)
    nodes = [{"vcpu": 4, "gib": 16, "spot": True}, {"vcpu": 4, "gib": 16}]
    facts = {"nodes": nodes, "spot": "spot=true"}
    blended, _, share = listprices._typed(cl, typed, facts, 7.0)
    assert share is not None and share[0] < 1 and blended.gpu == 0.5
    assert blended.cpu == pytest.approx(0.04 * share[0])


def test_a_gpu_price_is_never_negative() -> None:
    data = prometheus_contract().model_dump()
    data["prices"] = {"cpu": 0.04, "memory": 0.005, "gpu": -1}
    with pytest.raises(ValueError, match="gpu"):
        contracts.build(data)


# --- shown --------------------------------------------------------------------------


def test_the_digest_says_idle_gpus_and_only_when_there_are_some() -> None:
    def text(rows: list[dict[str, Any]]) -> str:
        now = sizing.report(rows, {"test": {"at": NOW, "error": None}}, priced())
        return sizing.digest_text("acme", now, now)

    assert "Idle GPUs: 1 of 1 GPUs idle, 365.00 USD/month." in text(
        rows_of(gpus_of(("a", gpu(0.0))))
    )
    assert "Idle GPUs" not in text(rows_of(gpus_of(("a", gpu(0.5)))))
    # GPUs DCGM doesn't cover are said, not taken for busy ones
    assert "Idle GPUs: 0 of 0 GPUs idle; 1 not judged (1 unread)." in text(
        rows_of(gpus_of(("a", gpu(None))))
    )


def test_sizing_waste_names_a_workload_with_idle_gpus_and_no_waste(
    database_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calm = {"waste": {"cpu": 0.0, "memory": 0.0}, "requests": {"cpu": 0.0, "memory": 0.0}}
    held = {"verdict": "idle", "held": 1.0}
    gpu_row = row(**calm, gpus={"asked": 1.0, "gpus": [held]})

    async def put() -> None:
        async with connect(database_url) as pool:
            s = Store(pool)
            await s.migrate()
            await stored(s, "acme", NOW, {"test": None}, gpu_row, row("web", **calm))

    asyncio.run(put())
    monkeypatch.setenv("SQUERO_DATABASE_URL", database_url)
    monkeypatch.setattr(cli, "contract", lambda settings, cid: priced())
    assert cli.main(["sizing", "waste", "--contract", "acme"]) == 0
    out = capsys.readouterr().out
    assert "acme: 1 of 1 GPUs idle, 365.00 USD/month\n" in out
    assert "test/shop/Deployment/api: 0.00 USD/month of" in out
    assert out.count("| 1 of 1 GPUs idle, 365.00 USD/month") == 1
    assert "1 more Workloads: no waste, no risk" in out  # web
    assert cli.main(["sizing", "waste", "--contract", "acme", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["gpus"]["cost"] == 365.0


def test_from_dcgm_to_sizing_waste(
    database_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Phase 17's done-when, on fixtures: an idle GPU Workload flagged with its cost,
    from DCGM's series through a stored run to `sizing waste`."""
    fake = lab_fake(monkeypatch)
    who = {"namespace": "shop", "pod": "api-7d9f-0", "container": "app"}
    fake.add("kube_pod_container_resource_requests", 1, resource="nvidia_com_gpu", **who)
    dcgm(fake, "DCGM_FI_PROF_GR_ENGINE_UTIL_RATIO", [0.01] * 42, "api-7d9f-0")
    monkeypatch.setenv("SQUERO_DATABASE_URL", database_url)
    monkeypatch.setattr(cli, "contract", lambda settings, cid: priced(0.5))
    assert cli.main(["sizing", "refresh", "--contract", "acme"]) == 0
    capsys.readouterr()
    assert cli.main(["sizing", "waste", "--contract", "acme", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    (api,) = [w for w in out["workloads"] if w["name"] == "api"]
    held = 42 / 168  # 42 hours read in production's 7-day window
    assert api["gpus"] == {
        "idle": pytest.approx(held),
        "of": pytest.approx(held),
        "cost": pytest.approx(held * 0.5 * 730),
        "not_judged": {},
    }
    (gpu_row,) = api["containers"][0]["gpus"]["gpus"]
    assert (gpu_row["id"], gpu_row["verdict"], gpu_row["busiest"]) == ("GPU-aaa", "idle", 0.01)


def test_the_agents_tool_never_sees_gpus() -> None:
    """Phase 17's grill: the agent gets GPUs in a slice of its own, with its eval rerun."""
    (r,) = rows_of(gpus_of(("a", gpu(0.0))))
    bare = {k: v for k, v in r.items() if k != "gpus"}
    c = prometheus_contract()
    assert agent_view(r, c) == agent_view(bare, c)


# --- isolation ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_contracts_idle_gpus_never_reach_another(store: Store) -> None:  # noqa: F811
    gpu_row = row(gpus={"asked": 1.0, "gpus": [{"verdict": "idle", "held": 1.0}]})
    await stored(store, "acme", NOW, {"test": None}, gpu_row)
    await stored(store, "bank", NOW, {"test": None}, row())
    for cid, idle in (("acme", True), ("bank", False)):
        c = priced().model_copy(update={"id": cid})
        rows, clusters = await store.latest_sizings(cid, {"test"})
        assert ("gpus" in sizing.report(rows, clusters, c)) is idle
        assert ("Idle GPUs" in await sizing.digest(store, c, NOW + 1, None)) is idle


def test_the_window_is_the_policys() -> None:
    """A 3-day policy holds a GPU read for 72 h whole."""
    data = prometheus_contract().model_dump()
    data["sizing"] = {"policies": {"short": {"base": "production", "window": "3d"}},
                      "rules": [{"name": "api", "policy": "short"}]}  # fmt: skip
    (r,) = rows_of(gpus_of(("a", gpu(0.0, hours=72))), c=contracts.build(data), window=3 * 86400)
    assert r["gpus"]["gpus"][0]["held"] == 1.0
    assert WEEK == 7 * 86400
