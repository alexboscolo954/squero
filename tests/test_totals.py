"""The Overview's data (slice 21.2): a Contract's CPU and memory over 14 days, its ready
containers summed times their pods, one point per hourly run, never another Contract's;
and each container's Automation, from its Sizing rule and its cluster's Actuator."""

from typing import Any

import pytest
from pydantic import SecretStr

from squero import contracts, webapp
from squero.store import Store
from tests.conftest import make_contract
from tests.test_sizing_store import NOW, row, stored
from tests.test_sizing_store import store as store

GI = 2**30
DAY = 86_400.0


def sized(name: str, pods: float, cpu: tuple[float, float, float], **extra: Any) -> dict[str, Any]:
    """cpu: request, Sizing and basis per pod; memory the same times 1 GiB."""
    res = lambda f: {"action": "lower", "current": f[0], "target": f[1], "basis": f[2]}  # noqa: E731
    return row(name, pods=pods, cpu=res(cpu), memory=res(tuple(v * GI for v in cpu)), **extra)


@pytest.mark.anyio
async def test_totals_sum_ready_containers_times_their_pods_per_run(store: Store) -> None:
    await stored(store, "acme", NOW - 15 * DAY, {"test": None}, sized("old", 1, (9, 9, 9)))
    await stored(
        store,
        "acme",
        NOW - 3600,
        {"test": None},
        sized("api", 2, (1.0, 0.25, 0.2)),
        sized("web", 3, (0.5, 0.5, 0.1)),
        sized("new", 4, (7, 7, 7), state="learning"),  # not ready: not in the sums
        {**sized("bare", 5, (1, 1, 1)), "cpu": None, "memory": None},  # no resource
    )
    await stored(store, "acme", NOW, {"test": None}, sized("api", 1, (1.0, 0.25, 0.2)))
    await stored(store, "bank", NOW, {"test": None}, sized("api", 10, (5, 5, 5)))
    got = await webapp.totals_of(store, make_contract(contract_id="acme"), NOW)
    assert [p.at for p in got.points] == [NOW - 3600, NOW]  # 15 days ago is out
    first, last = got.points
    assert first.cpu.model_dump() == pytest.approx({"requests": 3.5, "sizings": 2.0, "usage": 0.7})
    assert first.memory.requests == pytest.approx(3.5 * GI)
    assert last.cpu.model_dump() == pytest.approx({"requests": 1.0, "sizings": 0.25, "usage": 0.2})
    bank = await webapp.totals_of(store, make_contract(contract_id="bank"), NOW)
    assert [p.cpu.requests for p in bank.points] == [50.0]  # its own, never acme's


@pytest.mark.anyio
async def test_a_container_is_automated_by_its_rule_only_where_an_actuator_runs(
    store: Store,
) -> None:
    data = make_contract(
        contract_id="acme", clusters={"test": ["shop"], "edge": ["shop"]}
    ).model_dump()
    data["integrations"].append({"name": "prometheus", "url": "http://prometheus"})
    data["clusters"][0]["actuator_token_secret"] = "act"
    data["sizing"] = {"rules": [{"name": "api", "policy": "production", "automate": True}]}
    c = contracts.build(data, secrets={"act": SecretStr("a" * 40)})
    await stored(
        store,
        "acme",
        NOW,
        {"test": None, "edge": None},
        sized("api", 1, (1, 0.5, 0.4)),
        sized("web", 1, (1, 0.5, 0.4)),
        sized("api", 1, (1, 0.5, 0.4), cluster="edge", workload="edge/shop/Deployment/api/app"),
        sized(
            "api",
            1,
            (1, 0.5, 0.4),
            state="learning",
            container="side",
            workload="test/shop/Deployment/api/side",
        ),
    )
    got = await webapp.workloads_of(store, c, NOW)
    flags = {
        (x.cluster, x.name, x.container): x.automated for w in got.workloads for x in w.containers
    } | {(x.cluster, x.name, x.container): x.automated for x in got.learning}
    assert flags == {
        ("test", "api", "app"): True,
        ("test", "web", "app"): False,  # no rule automates it
        ("edge", "api", "app"): False,  # its cluster has no Actuator
        ("test", "api", "side"): True,  # learning: automated all the same
    }


def test_the_totals_route_needs_the_identity_and_names_a_known_contract(
    settings: Any, fake_cluster: Any, database_url: str
) -> None:
    from tests.test_console import AS_OPERATOR, VIEW
    from tests.test_webapp import hub, seed

    seed(database_url, ("acme", NOW, row("api")))
    with hub(settings, fake_cluster, database_url) as c:
        ok = c.get("/api/v1/contracts/acme/totals", headers=VIEW)
        unknown = c.get("/api/v1/contracts/nobody/totals", headers=VIEW).status_code
        no_cookie = c.get("/api/v1/contracts/acme/totals", headers=AS_OPERATOR).status_code
    assert ok.status_code == 200 and isinstance(ok.json()["points"], list)
    assert unknown == 404 and no_cookie == 401
