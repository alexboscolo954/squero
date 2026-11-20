"""Floors without an HPA (slice 17.4b): a GPU Workload with vLLM Needs and no HPA gets a
Floor against its replica count, its saving as Phase 14's (what an autoscaler holding the
Floor would save) with its GPUs at the GPU price, a Weekly profile with the cron triggers a
KEDA ScaledObject would need, and no realized Savings for a count lowered by hand."""

from typing import Any

import pytest

from squero import contracts, sizing, usage, webapp
from squero.usage import Bounds, Workload
from tests.fake_prometheus import FakePrometheus
from tests.test_floors import API, NOW, SHOP, floor_of, row
from tests.test_needs import shop_fake
from tests.test_sizing_store import prometheus_contract, store  # noqa: F401
from tests.test_usage import _prom, deployment, gpu_pod

FIXED = [Bounds("", 4.0, 4.0, fixed=True)]


def gpu_row(gpus: float = 1.0) -> dict[str, Any]:
    return {**row(), "gpus": {"asked": gpus, "gpus": []}}


def fixed_floor(values: list[float], **kw: Any) -> dict[str, Any]:
    return floor_of(values, hpas=kw.pop("hpas", FIXED), rows=kw.pop("rows", [gpu_row()]), **kw)


# --- the replica count -------------------------------------------------------------------


def test_a_deployments_and_a_statefulsets_replica_count_in_their_namespaces() -> None:
    fake = FakePrometheus()
    fake.add("kube_deployment_spec_replicas", 4, namespace="shop", deployment="api")
    fake.add("kube_deployment_spec_replicas", 9, namespace="bank", deployment="api")
    fake.add("kube_statefulset_replicas", 0, namespace="shop", statefulset="db")
    db, ro = Workload("shop", "StatefulSet", "db"), Workload("shop", "Rollout", "web")
    got = usage.fixed(_prom(fake, SHOP), SHOP, NOW, 86400, {API, db, ro})
    assert got[API] == [Bounds("", 4.0, 4.0, False, fixed=True)]  # not bank's 9
    assert got[db] == [Bounds("", 0.0, 0.0, True, fixed=True)]  # at 0: scales to zero
    assert got[ro] == [Bounds("", None, None, fixed=True)]  # a kind it doesn't count
    assert all('namespace=~"shop"' in q for q in fake.queries)


def test_a_gpu_workload_without_an_hpa_is_read_by_its_replica_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakePrometheus()
    (p,) = deployment(fake, "api", pods=1)
    gpu_pod(fake, p)
    fake.add("kube_deployment_spec_replicas", 3, namespace="shop", deployment="api")
    monkeypatch.setattr(usage, "Prom", lambda t: _prom(fake, t))
    found = usage.collect(prometheus_contract(), [86400], NOW, {})
    assert found.bounds["test"][API] == [Bounds("", 3.0, 3.0, False, fixed=True)]


def test_an_hpa_wins_and_a_workload_without_gpus_gets_no_replica_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = shop_fake()  # api: an HPA
    gpu_pod(fake, "api-7d9f-0")
    monkeypatch.setattr(usage, "Prom", lambda t: _prom(fake, t))
    found = usage.collect(prometheus_contract(), [86400], NOW, {})
    assert not found.bounds["test"][API][0].fixed
    plain = FakePrometheus()
    (p,) = deployment(plain, "api", pods=1)
    plain.add("container_cpu_usage_seconds_total", [0.1], namespace="shop", pod=p, container="app")
    monkeypatch.setattr(usage, "Prom", lambda t: _prom(plain, t))
    assert usage.collect(prometheus_contract(), [86400], NOW, {}).bounds["test"] == {}
    assert not any("spec_replicas" in q for q in plain.queries)


# --- the Floor ---------------------------------------------------------------------------


def test_a_fixed_workload_gets_a_floor_only_with_stored_needs() -> None:
    needs: dict[str, Any] = {"test": {}}
    got = sizing.floors(prometheus_contract(), [gpu_row()], needs, {"test": {API: FIXED}}, NOW)
    assert got == {"test": []}  # a GPU Workload no vLLM ever reported for


def test_its_saving_is_what_an_autoscaler_holding_the_floor_would_save_with_its_gpus() -> None:
    f = fixed_floor([1, 1, 1, 3] * 6, rows=[gpu_row(2.0)])
    assert (f["state"], f["floor"], f["fixed"]) == ("ready", 2, True)
    # per hour: 4 less the larger of the Floor (2) and the Need: 2, 2, 2, 1 -> 1.75
    assert f["replicas"] == 1.75 and f["gpu_saving"] == pytest.approx(1.75 * 2)
    assert f["saving"]["cpu"] == pytest.approx(1.75 * 0.1)  # its Sizing, as Phase 14's


def test_a_workload_with_an_hpa_and_gpus_saves_its_gpus_too() -> None:
    f = floor_of([1.0] * 24, minimum=4.0, rows=[gpu_row(1.0)])
    assert "fixed" not in f and f["gpu_saving"] == pytest.approx(2.0)


def test_its_risks_and_why_none_say_replicas_not_an_hpas_minimum() -> None:
    below = fixed_floor([5.0] * 24, hpas=[Bounds("", 2.0, 2.0, fixed=True)])
    texts = [r["text"] for r in below["risks"]]
    assert texts == [
        "its 2 replicas are below the 5 replicas it needed in 100% of hours",
        "needed more than its 2 replicas in 100% of hours",
    ]
    unknown = fixed_floor([1.0] * 24, hpas=[Bounds("", None, None, fixed=True)])
    assert unknown["why"] == "its replica count is not known"


# --- priced, said and scheduled ------------------------------------------------------------


def priced(gpu: float | None) -> contracts.Contract:
    data = prometheus_contract().model_dump()
    data["prices"] = {"cpu": 0.04, "memory": 0.005, **({"gpu": gpu} if gpu is not None else {})}
    return contracts.build(data)


def test_a_floors_gpus_are_priced_with_it_and_never_left_out_of_its_cost() -> None:
    f = fixed_floor([1, 1, 1, 3] * 6, rows=[gpu_row(2.0)])
    got = sizing.floors_report([f], priced(0.5))
    cpu_mem = sizing.monthly(priced(0.5).prices, **f["saving"])  # type: ignore[arg-type]
    assert got["floors"][0]["cost"] == pytest.approx(cpu_mem + 3.5 * 0.5 * 730)
    total = got["floors_total"]
    assert (total["workloads"], total["fixed"], total["gpus"]) == (1, 1, pytest.approx(3.5))
    unpriced = sizing.floors_report([f], priced(None))
    assert unpriced["floors"][0]["cost"] is None and unpriced["floors_total"]["cost"] is None
    line = sizing.floors_line(unpriced, "USD")
    assert line == (
        "Floors: 1 can run fewer replicas (1.8 replicas fewer on average, 0.18 cores, "
        "0.22 GiB, 3.50 GPUs)."
    )


def test_the_digest_line_says_which_kind_of_floor() -> None:
    hpa = floor_of([1.0] * 24, minimum=4.0)
    fixed = fixed_floor([1.0] * 24)
    both = sizing.floors_report([hpa, fixed], priced(0.5))
    assert "2 can lower their HPA minimum or run fewer replicas" in str(
        sizing.floors_line(both, "USD")
    )
    only = sizing.floors_report([hpa], priced(0.5))
    assert "1 can lower their HPA minimum (" in str(sizing.floors_line(only, "USD"))


def test_its_line_says_it_needs_an_autoscaler() -> None:
    f = fixed_floor([1, 1, 1, 3] * 6)
    text = sizing.floor_text(f)
    assert "Floor 2 (no HPA: 4 replicas; production: 10% of 24 h of Needs)" in text
    assert text.endswith("(with an autoscaler holding the Floor, such as KEDA)")
    assert "HPA :" not in sizing.floor_text(f)


def test_its_weekly_profile_gives_the_triggers_a_scaledobject_would_need() -> None:
    f = {"namespace": "shop", "kind": "Deployment", "name": "api", "hpa": "", "fixed": True}
    weekly = {"timezone": "UTC", "blocks": [{"start": "0 8 * * 1", "end": "0 12 * * 1",
                                             "replicas": 3}]}  # fmt: skip
    got = sizing.scheduled(f, weekly)
    assert got["triggers"].startswith("# ScaledObject shop/api (KEDA): cron triggers")
    assert got["note"] == (
        "needs a KEDA ScaledObject for Deployment api; until then its replica count is the "
        "Floor's or the busiest block's"
    )


def test_a_backtests_too_many_are_priced_with_their_gpus() -> None:
    test = {"state": "over", "replicas": 1.0, "excess": {"cpu": 0.1, "memory": 0.0},
            "excess_gpus": 2.0}  # fmt: skip
    f = {**fixed_floor([1.0] * 24), "profile": {"state": "irregular", "backtest": test}}
    got = sizing.floors_report([f], priced(0.5))["floors"][0]["profile"]["backtest"]
    assert got["cost"] == pytest.approx(0.1 * 0.04 * 730 + 2.0 * 0.5 * 730)
    unpriced = sizing.floors_report([f], priced(None))["floors"][0]["profile"]["backtest"]
    assert unpriced["cost"] is None


def test_the_api_takes_a_floor_without_an_hpa() -> None:
    f = sizing.floors_report([fixed_floor([1, 1, 1, 3] * 6)], priced(0.5))["floors"][0]
    shown = webapp.Floor.model_validate({**f, "at": NOW})
    assert shown.fixed and shown.hpa == "" and shown.gpu_saving == pytest.approx(1.75)


# --- never realized ---------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_replica_count_lowered_by_hand_is_never_realized_savings(store: Any) -> None:  # noqa: F811
    shown = [{**fixed_floor([1, 1, 1, 3] * 6), "minimum": 4.0}]
    lowered = {"test": {API: [Bounds("", 2.0, 2.0, fixed=True)]}}
    await sizing.realize_floors(store, prometheus_contract(), NOW, shown, lowered)
    assert await store.floor_drops("acme") == []
    hpa = [{**floor_of([1.0] * 24, minimum=4.0), "minimum": 4.0}]
    await sizing.realize_floors(
        store, prometheus_contract(), NOW, hpa, {"test": {API: [Bounds("api", 2.0, 5.0)]}}
    )
    assert len(await store.floor_drops("acme")) == 1  # an HPA's minimum lowered: realized


def test_a_backtests_too_many_replicas_hold_their_gpus() -> None:
    """Three weeks busy on Monday mornings, the newest not: the profile asks too many."""
    from datetime import UTC, datetime

    values = {}
    for i in range(5 * 168):
        hour = NOW - (i + 1) * 3600.0
        when = datetime.fromtimestamp(hour, UTC)
        busy = when.weekday() == 0 and 9 <= when.hour < 12 and i >= 168
        values[hour] = 3.0 if busy else 1.0
    f = floor_of(values, hpas=FIXED, rows=[gpu_row(2.0)])
    test = f["profile"]["backtest"]
    assert test["replicas"] > 0 and test["excess_gpus"] == pytest.approx(test["replicas"] * 2)
