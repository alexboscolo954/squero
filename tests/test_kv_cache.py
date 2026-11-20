"""The KV cache reservation (slice 17.3): a vLLM container whose KV cache peaks under half
of its blocks over its Sizing policy's window gets a `kv_cache_light` Risk, saying what was
used, what vLLM reserves and what that suggests; nothing computed, nothing applied."""

from typing import Any

import httpx

from squero import usage
from squero.servers.sizing import container as agent_view
from squero.usage import Target
from tests.fake_prometheus import FakePrometheus
from tests.test_sizing_store import container, prometheus_contract, rows_of
from tests.test_usage import DAY, SHOP, cpu, deployment, gpu_pod

KV, CONFIG = "vllm:kv_cache_usage_perc", "vllm:cache_config_info"
SERVED = {"model_name": "meta-llama/Llama-3.1-8B-Instruct"}


def vllm(fake: FakePrometheus, pod: str, peaks: list[float | None], **labels: str) -> None:
    where = {"namespace": "shop", "pod": pod, "container": "app", **SERVED, **labels}
    fake.add(KV, peaks, **where)
    fake.add(CONFIG, 1, gpu_memory_utilization="0.9", num_gpu_blocks="4096", **where)


def read(fake: FakePrometheus, t: Target = SHOP) -> usage.Usage:
    return usage.usage(usage.Prom(t, fake.client()), t, DAY, end=DAY)


# --- the read ----------------------------------------------------------------------------


def test_a_vllm_containers_kv_cache_is_its_highest_hourly_peak_with_its_reservation() -> None:
    fake = FakePrometheus()
    (a,) = deployment(fake, pods=1)
    gpu_pod(fake, a)
    vllm(fake, a, [0.1, 0.3, None, 0.2])
    found = read(fake)
    (row,) = found.rows
    assert row.kv is not None
    assert (row.kv["peak"], len(row.kv["hours"])) == (0.3, 3)
    assert (row.kv["reserved"], row.kv["blocks"]) == ("0.9", "4096")
    assert found.gpu_note == "no GPU use from DCGM for 1 containers that request GPUs"
    q = next(q for q in fake.queries if q.startswith(f"max_over_time({KV}"))
    assert q.endswith("[3600s])") and 'namespace=~"shop"' in q


def test_a_series_without_its_container_goes_to_the_pods_one_gpu_container() -> None:
    fake = FakePrometheus()
    (a,) = deployment(fake, pods=1)
    gpu_pod(fake, a)
    cpu(fake, a, [0.1], container="proxy")  # a sidecar asking no GPU
    vllm(fake, a, [0.2], container="")
    vllm(fake, a, [0.4], container="proxy")  # named, but asking none: the GPU one's
    rows = {r.container: r for r in read(fake).rows}
    assert rows["app"].kv is not None and rows["app"].kv["peak"] == 0.4
    assert rows["proxy"].kv is None


def test_with_two_gpu_containers_an_unnamed_series_goes_to_neither() -> None:
    fake = FakePrometheus()
    (a,) = deployment(fake, pods=1)
    gpu_pod(fake, a)
    gpu_pod(fake, a, container="second")
    vllm(fake, a, [0.2], container="")
    assert all(r.kv is None for r in read(fake).rows)


def test_no_vllm_query_where_no_container_requests_a_gpu() -> None:
    fake = FakePrometheus()
    (a,) = deployment(fake, pods=1)
    cpu(fake, a, [0.1])
    vllm(fake, a, [0.2])
    assert read(fake).rows[0].kv is None and not any("vllm" in q for q in fake.queries)


def test_a_failed_read_costs_only_the_kv_cache() -> None:
    fake = FakePrometheus()
    (a,) = deployment(fake, pods=1)
    gpu_pod(fake, a)
    vllm(fake, a, [0.2])

    def handler(request: httpx.Request) -> httpx.Response:
        if CONFIG in request.url.params["query"]:
            return httpx.Response(503, json={"status": "error", "error": "boom"})
        return fake.handler(request)

    http = httpx.Client(base_url="http://prometheus", transport=httpx.MockTransport(handler))
    found = usage.usage(usage.Prom(SHOP, http), SHOP, DAY, end=DAY)
    (row,) = found.rows
    assert row.cpu and row.kv is None
    assert "vLLM's KV cache not read: Prometheus error 503: boom" in (found.gpu_note or "")


def test_another_namespaces_vllm_is_never_read() -> None:
    """Isolation: bank's vLLM pod has the same name in its own namespace."""
    fake = FakePrometheus()
    (a,) = deployment(fake, pods=1)
    gpu_pod(fake, a)
    vllm(fake, a, [0.9], namespace="bank")
    assert read(fake).rows[0].kv is None
    for q in (q for q in fake.queries if "vllm" in q):
        assert not [s for s in fake.select(q) if s.get("namespace") == "bank"], q


# --- the Risk ----------------------------------------------------------------------------


def vllm_row(peak: float, hours: int = 48, **kv: Any) -> dict[str, Any]:
    u = container()
    u.gpus = 1.0
    u.kv = {"peak": peak, "hours": {i * 3600.0 for i in range(hours)}, "reserved": "0.9",
            "blocks": "4096", **kv}  # fmt: skip
    (row,) = rows_of(u)
    return row


def light(row: dict[str, Any]) -> list[str]:
    return [r["text"] for r in row["risks"] if r["kind"] == "kv_cache_light"]


def test_a_cache_peaking_under_half_its_blocks_is_a_risk_that_says_why() -> None:
    assert light(vllm_row(0.3)) == [
        "the KV cache peaked at 30% of its 4,096 blocks in the window, vLLM reserving 0.9 of "
        "the GPU: a lower gpu_memory_utilization or a smaller GPU or MIG slice would likely "
        "hold it"
    ]
    assert light(vllm_row(0.5)) == []  # half is not under half


def test_without_its_policys_history_there_is_no_risk_yet() -> None:
    assert light(vllm_row(0.1, hours=23)) == []  # production needs a day
    assert light(vllm_row(0.1, hours=24)) != []


def test_without_its_config_the_risk_says_what_it_knows() -> None:
    (text,) = light(vllm_row(0.25, reserved=None, blocks=None))
    assert text.startswith("the KV cache peaked at 25% of its cache in the window: a lower")


def test_the_risk_shows_wherever_risks_show_the_agents_tool_too() -> None:
    """Phase 17's grill (Q13): a Risk like any other, its text data, no schema changed."""
    row = vllm_row(0.3)
    assert light(row)[0] in agent_view(row, prometheus_contract())["risks"]
