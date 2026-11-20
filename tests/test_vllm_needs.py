"""vLLM Needs (slice 17.4a): a vLLM Workload's hourly Need from its own metrics, the
replicas that reported times the busiest one's KV cache peak over 0.8, rounded up, and one
replica more in an hour when requests waited; in place of its HPA's, so its Floor, Weekly
profile and Backtest come from them."""

import logging
from typing import Any

import pytest

from squero import usage
from squero.usage import Target, UsageError, Workload
from tests.fake_prometheus import FakePrometheus
from tests.test_needs import API, NOW, SHOP, shop_fake
from tests.test_sizing_store import prometheus_contract
from tests.test_usage import _prom, deployment, gpu_pod

H = 3600.0
KV, WAITING = "vllm:kv_cache_usage_perc", "vllm:num_requests_waiting"


def served(fake: FakePrometheus, pod: str, kv: list[float], waiting: list[float],
           ns: str = "shop") -> None:  # fmt: skip
    """A vLLM pod's hourly peaks: its KV cache and its queue."""
    where = {"namespace": ns, "pod": pod, "container": "app", "model_name": "m"}
    fake.add(KV, kv, **where)
    fake.add(WAITING, waiting, **where)


def read(fake: FakePrometheus, t: Target = SHOP) -> dict[Workload, dict[float, float]]:
    """Two hours: the peaks of the hours starting at NOW - 3 H and NOW - 2 H."""
    return usage.vllm_needs(_prom(fake, t), t, NOW - 2 * H, NOW)


def test_a_need_is_the_replicas_times_the_busiest_peak_over_eighty_percent() -> None:
    fake = FakePrometheus()
    a, b = deployment(fake, "api", pods=2)
    served(fake, a, [0.5, 0.6], [0, 0])
    served(fake, b, [0.3, 0.2], [0, 0])
    assert read(fake) == {API: {NOW - 3 * H: 2.0, NOW - 2 * H: 2.0}}  # 1.25 and 1.5 up


def test_an_hour_when_requests_waited_needs_one_replica_more() -> None:
    fake = FakePrometheus()
    a, b = deployment(fake, "api", pods=2)
    served(fake, a, [0.5, 0.95], [3, 0])
    served(fake, b, [0.3, 0.2], [0, 1])
    # 2 x 0.5 / 0.8 = 1.25: 2, and waited: 3; 2 x 0.95 / 0.8 = 2.4: 3, waited: still 3
    assert read(fake) == {API: {NOW - 3 * H: 3.0, NOW - 2 * H: 3.0}}


def test_a_replica_exactly_at_the_target_needs_itself_and_no_more() -> None:
    fake = FakePrometheus()
    (a,) = deployment(fake, "api", pods=1)
    served(fake, a, [0.8, 0.0], [0, 0])
    assert read(fake) == {API: {NOW - 3 * H: 1.0, NOW - 2 * H: 0.0}}


def test_only_the_contracts_namespaces_and_its_own_pods_count() -> None:
    """Isolation: bank's vLLM pod has the same name in its own namespace."""
    fake = FakePrometheus()
    (a,) = deployment(fake, "api", pods=1)
    served(fake, a, [0.4, 0.4], [0, 0])
    served(fake, a, [1.0, 1.0], [9, 9], ns="bank")
    served(fake, "nobody-0", [1.0, 1.0], [9, 9])  # no Workload: not counted
    assert read(fake) == {API: {NOW - 3 * H: 1.0, NOW - 2 * H: 1.0}}
    for q in (q for q in fake.queries if "vllm" in q):
        assert not [s for s in fake.select(q) if s.get("namespace") == "bank"], q


# --- in place of its HPA's -------------------------------------------------------------


def test_a_vllm_workloads_needs_replace_its_hpas_and_are_read_only_with_gpus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = shop_fake()  # api: an HPA at 2 replicas, twice its target: a Need of 4
    monkeypatch.setattr(usage, "Prom", lambda t: _prom(fake, t))
    c = prometheus_contract()
    hpa_only = usage.collect(c, [86400], NOW, {})
    assert hpa_only.needs["test"][API] and not any("vllm" in q for q in fake.queries)
    gpu_pod(fake, "api-7d9f-0")
    served(fake, "api-7d9f-0", [0.4] * 3, [0] * 3)
    found = usage.collect(c, [86400], NOW, {})
    assert set(found.needs["test"][API].values()) == {1.0}  # its vLLM's, not its HPA's 4


def test_a_failed_vllm_read_keeps_the_hpas_needs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fake = shop_fake()
    gpu_pod(fake, "api-7d9f-0")
    monkeypatch.setattr(usage, "Prom", lambda t: _prom(fake, t))

    def broken(*args: Any) -> Any:
        raise UsageError("Prometheus error 422: too many samples")

    monkeypatch.setattr(usage, "vllm_needs", broken)
    with caplog.at_level(logging.WARNING):
        found = usage.collect(prometheus_contract(), [86400], NOW, {})
    assert set(found.needs["test"][API].values()) == {4.0}
    assert "acme/test: its vLLM Needs not read: Prometheus error 422" in caplog.text
