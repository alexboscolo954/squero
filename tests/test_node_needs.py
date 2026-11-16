"""What a pod needs of a node (audit A3, the Hub's side): its Actuator reports each pod's
nodeSelector and node affinity with the nodes' labels, and a pod that would move fits only
on a node meeting them: Part A's three refusals by Karpenter first."""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from squero import blockers, sizing
from squero.config import Settings
from tests.test_actuator import A_TOKEN, B_TOKEN, actuated, as_actuator, hub, sql
from tests.test_blockers import a_pod, a_report
from tests.test_could_go import FRESH, node, pod, read_of
from tests.test_sizing_store import NOW

KARPENTER, CA = "karpenter", "cluster-autoscaler"
NEEDS = ("constraints", "needs", "needed", "labels")
BARE = {k: v for k, v in FRESH.items() if k not in NEEDS}  # an Actuator from before A3
SPOT = "karpenter.sh/capacity-type"


def expr(key: str, op: str, *values: str, field: bool = False) -> dict[str, Any]:
    return {"key": key, "operator": op, "values": list(values), "field": field}


def need(
    selector: dict[str, str] | None = None,
    required: list[list[dict[str, Any]]] | None = None,
    preferred: list[tuple[int, list[dict[str, Any]]]] | None = None,
) -> dict[str, Any]:
    prefs = [{"weight": w, "match": m} for w, m in preferred or []]
    return {"selector": selector or {}, "required": required or [], "preferred": prefs}


def told(
    needs: dict[str, dict[str, Any]], labels: dict[str, dict[str, str]], **more: Any
) -> dict[str, Any]:
    """A fresh report whose pods (`shop/<pod>`) need these, with the nodes' labels."""
    distinct = list(needs.values())
    needed = {f"shop/{p}": distinct.index(n) for p, n in needs.items()}
    body = {"constraints": "read", "needs": distinct, "needed": needed, "labels": labels}
    return FRESH | body | more


def go(nodes: list[dict[str, Any]], report: dict[str, Any], autoscaler: str | None) -> Any:
    verdict = {n["node"]: {"state": "clear"} for n in nodes}
    return blockers.could_go(read_of(*nodes), verdict, blockers.fit(report, autoscaler))


# --- Part A's three -----------------------------------------------------------------------


def test_a_pod_preferring_spot_keeps_its_spot_node_under_karpenter_only() -> None:
    """Karpenter holds the heaviest preference as required while a new spot node could
    meet it: "SpotToSpotConsolidation is disabled" kept the spot node. Cluster Autoscaler
    places by requirements alone."""
    spot = node("spot-1", pod("web-1", 0.5))
    on_demand = node("od-1", pod("web-2", 3.0))
    report = told(
        {"web-1": need(preferred=[(1, [expr(SPOT, "In", "spot")])])},
        {"spot-1": {SPOT: "spot"}, "od-1": {SPOT: "on-demand"}},
    )
    assert go([spot, on_demand], report, KARPENTER) == ["od-1"]  # web-2 needs nothing
    assert go([spot, on_demand], report, CA) == ["spot-1"]
    another = node("spot-2", pod("web-3", 3.0))  # an existing spot node with room: deleted
    with_spot = told(report_needs(report), report["labels"] | {"spot-2": {SPOT: "spot"}})
    assert go([spot, on_demand, another], with_spot, KARPENTER) == ["spot-1"]


def report_needs(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {key.partition("/")[2]: report["needs"][i] for key, i in report["needed"].items()}


def test_a_node_selector_keeps_pods_off_nodes_without_its_label() -> None:
    """web's `karpenter.sh/nodepool: default`: "Can't replace with a cheaper node"."""
    pool = "karpenter.sh/nodepool"
    od = node("od-1", pod("web-1", 0.5))
    system = node("sys-1", pod("dns", 0.5))
    report = told({"web-1": need({pool: "default"})}, {"od-1": {pool: "default"}, "sys-1": {}})
    assert go([od, system], report, KARPENTER) == ["sys-1"]  # dns needs nothing: it moves
    labelled = told({"web-1": need({pool: "default"})}, {"sys-1": {pool: "default"}})
    assert go([od, system], labelled, KARPENTER) == ["od-1"]  # then dns has nowhere left


def test_a_required_not_in_keeps_a_pod_off_the_nodes_it_names() -> None:
    """keeper's `role NotIn [system]`: never onto a system node, onto any other."""
    off_system = need(required=[[expr("role", "NotIn", "system")]])
    a, system = node("a", pod("keeper", 0.5)), node("sys", pod("s1", 1.0))
    report = told({"keeper": off_system}, {"a": {}, "sys": {"role": "system"}})
    assert go([a, system], report, CA) == ["sys"]  # s1 needs nothing: it moves to a
    worker = node("w", pod("w1", 3.0))
    report = told({"keeper": off_system}, {"a": {}, "sys": {"role": "system"}, "w": {}})
    assert go([a, system, worker], report, CA) == ["a"]  # keeper onto w


# --- the scheduler's reading ------------------------------------------------------------


def holds(e: dict[str, Any], labels: dict[str, str], name: str = "n1") -> bool:
    return blockers._holds(e, labels, name)


@pytest.mark.parametrize(
    ("e", "labels", "expected"),
    [
        (expr("k", "In", "a", "b"), {"k": "b"}, True),
        (expr("k", "In", "a"), {"k": "b"}, False),
        (expr("k", "In", "a"), {}, False),
        (expr("k", "NotIn", "a"), {"k": "b"}, True),
        (expr("k", "NotIn", "a"), {"k": "a"}, False),
        (expr("k", "NotIn", "a"), {}, True),  # a label it lacks meets NotIn
        (expr("k", "Exists"), {"k": ""}, True),
        (expr("k", "Exists"), {}, False),
        (expr("k", "DoesNotExist"), {}, True),
        (expr("k", "DoesNotExist"), {"k": "x"}, False),
        (expr("k", "Gt", "4"), {"k": "5"}, True),
        (expr("k", "Gt", "5"), {"k": "5"}, False),
        (expr("k", "Lt", "5"), {"k": "4"}, True),
        (expr("k", "Lt", "4"), {"k": "4"}, False),
        (expr("k", "Gt", "4"), {}, False),  # no label: no number
        (expr("k", "Lt", "4"), {}, False),  # nor taken as 0
        (expr("k", "Gt", "4"), {"k": "big"}, False),
        (expr("k", "Gt"), {"k": "5"}, False),  # no value given
        (expr("metadata.name", "In", "n1", field=True), {"metadata.name": "n2"}, True),
        (expr("metadata.name", "NotIn", "n1", field=True), {}, False),
    ],
)
def test_each_operator_reads_as_the_scheduler_reads_it(
    e: dict[str, Any], labels: dict[str, str], expected: bool
) -> None:
    assert holds(e, labels) is expected


def fits_on(n: dict[str, Any], labels: dict[str, str], autoscaler: str | None = CA) -> bool:
    report = told({"p": n}, {"n1": labels})
    check = blockers.fit(report, autoscaler)
    assert not isinstance(check, str)
    return check({"namespace": "shop", "pod": "p"}, "n1")


def test_a_selector_and_a_term_both_hold_and_one_term_of_several_is_enough() -> None:
    both = need({"zone": "a"}, [[expr("disk", "In", "ssd")], [expr("gpu", "Exists")]])
    assert fits_on(both, {"zone": "a", "disk": "ssd"})
    assert fits_on(both, {"zone": "a", "gpu": "1"})
    assert not fits_on(both, {"zone": "b", "disk": "ssd"})  # the selector holds in every term
    assert not fits_on(both, {"zone": "a"})
    every = need(required=[[expr("disk", "In", "ssd"), expr("zone", "In", "a")]])
    assert not fits_on(every, {"disk": "ssd"})  # every expression of a term


def test_an_empty_required_term_meets_no_node() -> None:
    assert not fits_on(need(required=[[]]), {})
    assert fits_on(need(required=[[], [expr("k", "Exists")]]), {"k": "x"})


def test_karpenter_holds_every_one_of_the_heaviest_preferences_and_no_lighter_one() -> None:
    """It sorts them unstably: of two as heavy, which one it holds isn't fixed."""
    prefs = [
        (10, [expr("zone", "In", "a")]),
        (50, [expr("disk", "In", "ssd")]),
        (50, [expr("gpu", "Exists")]),  # as heavy
    ]
    p = need(preferred=prefs)
    assert fits_on(p, {"disk": "ssd", "gpu": "1"}, KARPENTER)  # no zone needed
    assert not fits_on(p, {"disk": "ssd"}, KARPENTER)
    assert not fits_on(p, {"gpu": "1", "zone": "a"}, KARPENTER)
    assert fits_on(p, {}, CA) and fits_on(p, {}, None)  # preferences place nothing there
    held = need(
        {"pool": "x"}, [[expr("role", "NotIn", "system")]], [(1, [expr(SPOT, "In", "spot")])]
    )
    assert fits_on(held, {"pool": "x", SPOT: "spot"}, KARPENTER)
    assert not fits_on(held, {"pool": "x", SPOT: "on-demand"}, KARPENTER)
    assert not fits_on(held, {"pool": "x", SPOT: "spot", "role": "system"}, KARPENTER)


def test_a_pod_with_no_need_lands_anywhere_and_one_with_a_need_only_on_a_node_with_labels() -> None:
    report = told({"p": need({"k": "v"})}, {"n1": {"k": "v"}})
    check = blockers.fit(report, CA)
    assert not isinstance(check, str)
    assert check({"namespace": "shop", "pod": "free"}, "unlabelled")
    assert check({"namespace": "shop", "pod": "p"}, "n1")
    assert not check({"namespace": "shop", "pod": "p"}, "unlabelled")  # not seen: no match
    assert check({"namespace": "bank", "pod": "p"}, "unlabelled")  # keyed by namespace too


# --- when it can't be told -------------------------------------------------------------


@pytest.mark.parametrize(
    ("constraints", "said"),
    [
        (None, "its Actuator doesn't report what its pods need of a node"),
        ("nodes_refused", "its Actuator may not read the nodes' labels"),
        ("cut", "its Actuator had more pods' needs, nodes or labels than one report holds"),
        ("nodes_unread", "its Actuator's last read of the nodes' labels failed"),
    ],
)
def test_no_node_goes_where_what_pods_need_is_not_known_and_the_page_says_why(
    constraints: str | None, said: str
) -> None:
    report = BARE | ({"constraints": constraints} if constraints else {})
    c = actuated("acme", A_TOKEN)
    read = read_of(node("a", pod("a1")), node("b", pod("b1")))
    (shown, *_) = sizing.nodes_report({"test": read}, c, {"test": report}, NOW)
    assert shown["could_go"]["now"]["nodes"] == shown["could_go"]["after"]["nodes"] == []
    assert shown["could_go"]["note"].startswith(said)
    assert shown["judged"]["clear"] == 2  # judged, and still none counted
    known = FRESH | {"constraints": "read", "needs": [], "needed": {}, "labels": {}}
    (fine, *_) = sizing.nodes_report({"test": read}, c, {"test": known}, NOW)
    assert fine["could_go"]["now"]["nodes"] == ["a"] and fine["could_go"]["note"] is None


def test_no_usable_report_lets_any_pod_land_anywhere() -> None:
    check = blockers.fit(None, KARPENTER)
    assert not isinstance(check, str) and check({"namespace": "shop", "pod": "p"}, "n")


# --- restart advice ----------------------------------------------------------------------


def test_a_restart_is_not_advised_when_a_pod_needs_what_no_pinned_node_has() -> None:
    pins = {"pin"}
    pinned = node("pin", cores=8.0, gib=32.0)
    moving = node("a", pod("db-1", 1.0, workload=("StatefulSet", "db")))
    read = read_of(pinned, moving)
    found = [{"namespace": "shop", "pod": "db-1", "placement": "shop/StatefulSet/db"}]
    selector = told({"db-1": need({"disk": "ssd"})}, {"pin": {}, "a": {"disk": "ssd"}})
    got = blockers._restart(found, read, pins, "pool=pinned", blockers.fit(selector, CA))
    assert got == {
        "workloads": [],
        "why_not": "pod shop/db-1 needs of a node what no pinned node has (its nodeSelector "
        "or node affinity)",
    }
    met = told({"db-1": need({"disk": "ssd"})}, {"pin": {"disk": "ssd"}})
    ok = blockers._restart(found, read, pins, "pool=pinned", blockers.fit(met, CA))
    assert ok == {"workloads": ["shop/StatefulSet/db"], "why_not": None}
    unknown = blockers._restart(found, read, pins, "pool=pinned", blockers.fit(BARE, CA))
    assert unknown["why_not"] == (
        "its Actuator doesn't report what its pods need of a node (one from before that, "
        "or a Hub that didn't ask)"
    )


# --- the report ------------------------------------------------------------------------


def test_the_hub_asks_for_what_pods_need_under_the_kill_switch_too(
    settings: Settings, database_url: str
) -> None:
    with TestClient(hub(settings, database_url, actuated("acme", A_TOKEN))) as c:
        got = c.get("/actuator/desired", headers=as_actuator(A_TOKEN)).json()
        sql(database_url, ("insert into kill_switches values ('acme', 't', 1)", ()))
        killed = c.get("/actuator/desired", headers=as_actuator(A_TOKEN)).json()
    assert got["constraints"] is True and killed["constraints"] is True


GOOD = {
    "constraints": "read",
    "needs": [
        need(
            {"karpenter.sh/nodepool": "default"},
            [[expr("role", "NotIn", "system"), expr("metadata.name", "In", "n1", field=True)]],
            [(100, [expr("squero.io/pinned", "In", "yes")])],
        )
    ],
    "needed": {"shop/api-1": 0},
    "labels": {"n1": {"karpenter.sh/nodepool": "default", "role": "worker"}},
}


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"needed": {"shop/api-1": 1}}, "needed"),  # no such need
        ({"needed": {"shop/api-1": -1}}, "needed"),
        ({"needed": {"shop/" + "p" * 254: 0}}, "needed"),
        ({"labels": {"n" * 254: {}}}, "labels"),
        (
            {"needs": [need(required=[[expr("metadata.name", "In", "n" * 254, field=True)]])]},
            "needs",
        ),
        ({"needed": {"shop/API": 0}}, "needed"),
        ({"labels": {"N1": {}}}, "labels"),
        ({"labels": {"n1": {"bad key!": "x"}}}, "labels"),
        ({"labels": {"n1": {"k": "-x"}}}, "labels"),
        ({"labels": {"n1": {f"k{i}": "v" for i in range(101)}}}, "labels"),
        ({"needs": [need({"k": "v" * 64})]}, "needs"),
        ({"needs": [need({".".join(["a" * 60] * 5) + "/" + "k" * 20: "v"})]}, "needs"),  # 325
        ({"needs": [need(required=[[expr("bad key!", "Exists")]])]}, "needs"),
        ({"needs": [need(required=[[expr("labels.x", "In", "n1", field=True)]])]}, "needs"),
        ({"needs": [need(required=[[expr("metadata.name", "In", "N1", field=True)]])]}, "needs"),
        ({"needs": [need(preferred=[(1, [expr("k", "In", "-v")])])]}, "needs"),
    ],
)
def test_a_need_no_actuator_sends_is_refused_and_named(
    settings: Settings, database_url: str, change: dict[str, Any], field: str
) -> None:
    with TestClient(hub(settings, database_url, actuated("acme", A_TOKEN))) as c:
        post = lambda body: c.post(  # noqa: E731
            "/actuator/blockers", headers=as_actuator(A_TOKEN), json=body
        )
        assert post(a_report(a_pod(), **GOOD)).status_code == 204
        odd = post(a_report(a_pod(), **(GOOD | change)))
    assert odd.status_code == 422 and odd.json()["detail"] == f"not what an Actuator sends: {field}"


def test_a_need_of_a_pod_outside_the_clusters_namespaces_is_refused(
    settings: Settings, database_url: str
) -> None:
    with TestClient(hub(settings, database_url, actuated("acme", A_TOKEN))) as c:
        r = c.post(
            "/actuator/blockers",
            headers=as_actuator(A_TOKEN),
            json=a_report(**(GOOD | {"needed": {"bank/api-1": 0}})),
        )
    assert (
        r.status_code == 422 and r.json()["detail"] == "namespaces not in cluster 'test': ['bank']"
    )


def test_needs_are_sent_only_when_read_so_an_older_hub_takes_the_report() -> None:
    plain = blockers.Report(at=NOW, namespaces=["shop"], pods=[])
    assert not {"constraints", "needs", "needed", "labels"} & set(blockers.sent(plain))
    full = blockers.Report.model_validate(a_report(**GOOD))
    assert blockers.sent(full)["needed"] == {"shop/api-1": 0}


def test_each_contracts_needs_stay_its_own(settings: Settings, database_url: str) -> None:
    """Two Contracts' Actuators on clusters of one name: each report, with its needs and
    its nodes' labels, is stored as its own token's only."""
    acme, bank = actuated("acme", A_TOKEN), actuated("bank", B_TOKEN)
    theirs = GOOD | {"labels": {"n1": {"bank-only": "yes"}}}
    with TestClient(hub(settings, database_url, acme, bank)) as c:
        for token, body in ((A_TOKEN, GOOD), (B_TOKEN, theirs)):
            c.post("/actuator/blockers", headers=as_actuator(token), json=a_report(**body))
    rows = sql(database_url, ("select contract_id, report from cluster_blockers order by 1", ()))
    assert [(cid, r["labels"]) for cid, r in rows] == [
        ("acme", GOOD["labels"]),
        ("bank", theirs["labels"]),
    ]


def test_a_nodes_restart_advice_goes_by_what_its_pods_need() -> None:
    """Through the verdicts: a placed pod needing a label no pinned node has is never
    advised onto one."""
    from tests.test_placement import KEEP, facts, pinned, restart
    from tests.test_placement import report as placing_report

    rep = placing_report(facts("api-1", "b", annotations=KEEP), placing=True)
    a, b = pinned(node("a")), node("b", pod("api-1"))
    assert restart(rep, a, b) == {"workloads": ["shop/Deployment/api"], "why_not": None}
    needy = rep | {"needs": [need({"disk": "ssd"})], "needed": {"shop/api-1": 0}}
    needy |= {"labels": {"a": {}, "b": {"disk": "ssd"}}}
    assert restart(needy, a, b)["why_not"].startswith("pod shop/api-1 needs of a node")


def test_restart_advice_goes_by_requirements_alone_under_karpenter_too() -> None:
    """A restarted pod is placed by the scheduler, where its own preference only scores:
    a zone it prefers that no pinned node is in doesn't stop the advice."""
    from tests.test_placement import facts, pinned, verdict
    from tests.test_placement import report as placing_report

    kept = {blockers.DO_NOT_DISRUPT: "true"}  # Karpenter's Blocker
    rep = placing_report(facts("api-1", "b", annotations=kept), placing=True)
    zoned = rep | {"needs": [need(preferred=[(100, [expr("zone", "In", "a")])])]}
    zoned |= {"needed": {"shop/api-1": 0}, "labels": {"a": {}, "b": {"zone": "a"}}}
    a, b = pinned(node("a")), node("b", pod("api-1"))
    got = verdict(zoned, a, b, autoscaler=KARPENTER)["b"]["restart"]
    assert got == {"workloads": ["shop/Deployment/api"], "why_not": None}


# --- the Actuator's side (A3b) -----------------------------------------------------------


def k_expr(key: str, op: str, *values: str) -> Any:
    from kubernetes import client as k

    return k.V1NodeSelectorRequirement(key=key, operator=op, values=list(values) or None)


def affined(
    name: str,
    selector: dict[str, str] | None = None,
    required: list[tuple[list[Any], list[Any]]] | None = None,
    preferred: list[tuple[int, list[Any]]] | None = None,
    **kw: Any,
) -> Any:
    """A pod with a nodeSelector, required terms (expressions, fields) and preferences."""
    from kubernetes import client as k

    from tests.test_blockers import kpod

    p = kpod(name, **kw)
    p.spec.node_selector = selector
    na = k.V1NodeAffinity()
    if required is not None:
        terms = [
            k.V1NodeSelectorTerm(match_expressions=e or None, match_fields=f or None)
            for e, f in required
        ]
        na.required_during_scheduling_ignored_during_execution = k.V1NodeSelector(
            node_selector_terms=terms
        )
    if preferred is not None:
        na.preferred_during_scheduling_ignored_during_execution = [
            k.V1PreferredSchedulingTerm(
                weight=w, preference=k.V1NodeSelectorTerm(match_expressions=e)
            )
            for w, e in preferred
        ]
    if required is not None or preferred is not None:
        p.spec.affinity = k.V1Affinity(node_affinity=na)
    return p


def nodes_api(core: Any, pages: list[Any]) -> list[Any]:
    """`core.list_node` gives these pages of {name: labels} in turn, or raises one."""
    from kubernetes import client as k

    asked: list[Any] = []

    def list_node(limit: int, _continue: str | None = None) -> Any:
        asked.append(_continue)
        i = 0 if _continue is None else int(_continue)
        if isinstance(pages[i], Exception):
            raise pages[i]
        items = [
            k.V1Node(metadata=k.V1ObjectMeta(name=n, labels=labels))
            for n, labels in pages[i].items()
        ]
        more = str(i + 1) if i + 1 < len(pages) else None
        return k.V1NodeList(items=items, metadata=k.V1ListMeta(_continue=more))

    core.list_node.side_effect = list_node
    return asked


def test_a_pods_need_is_read_from_its_spec_as_written() -> None:
    from squero import actuator

    p = affined(
        "web-1",
        {"karpenter.sh/nodepool": "default"},
        [([k_expr("role", "NotIn", "system")], [k_expr("metadata.name", "In", "n1")])],
        [(100, [k_expr("pool", "In", "pinned")]), (1, [k_expr(SPOT, "In", "spot")])],
    )
    got = actuator.need_of(p)
    assert got is not None and got.model_dump() == need(
        {"karpenter.sh/nodepool": "default"},
        [[expr("role", "NotIn", "system"), expr("metadata.name", "In", "n1", field=True)]],
        [(100, [expr("pool", "In", "pinned")]), (1, [expr(SPOT, "In", "spot")])],
    )
    exists = actuator.need_of(affined("x", required=[([k_expr("gpu", "Exists")], [])]))
    assert exists and exists.required[0][0].values == []
    assert actuator.need_of(affined("plain")) is None


def test_the_actuator_reports_each_need_once_and_the_labels_it_names_of_every_node() -> None:
    from squero import actuator
    from tests.test_blockers import apis, kpod

    pool = {"karpenter.sh/nodepool": "default"}
    pods = [
        affined("web-1", pool),
        affined("web-2", pool),  # the same need: sent once
        affined("keeper", required=[([k_expr("role", "NotIn", "system")], [])]),
        affined("spotty", preferred=[(1, [k_expr(SPOT, "In", "spot")])]),  # a preference only
        kpod("free"),  # needs nothing: not listed
        affined("ds", pool, by="DaemonSet"),  # stays with its node
        affined("done", pool, phase="Succeeded"),
        affined("new", pool, node=None, phase="Pending"),
    ]
    core, policy = apis({"shop": [pods[:4], pods[4:]]})
    asked = nodes_api(
        core,
        [
            {"n1": {"karpenter.sh/nodepool": "default", "zone": "a", SPOT: "spot"}},
            {"n2": {"role": "system", "kubernetes.io/hostname": "n2"}, "n3": None},
        ],
    )
    report = actuator.blocking(core, policy, ["shop"], NOW, constraints=True)
    assert report.constraints == "read" and asked == [None, "1"]  # a page at a time
    assert report.needed == {
        "shop/keeper": 0,
        "shop/spotty": 1,
        "shop/web-1": 2,
        "shop/web-2": 2,
    }
    assert [n.model_dump() for n in report.needs] == [
        need(required=[[expr("role", "NotIn", "system")]]),
        need(preferred=[(1, [expr(SPOT, "In", "spot")])]),
        need(pool),
    ]
    assert report.labels == {  # only the keys the needs name
        "n1": {"karpenter.sh/nodepool": "default", SPOT: "spot"},
        "n2": {"role": "system"},
        "n3": {},
    }
    assert blockers.unreal(report) is None
    # the Hub reads it as Part A's: web's pods only onto a node of the NodePool
    check = blockers.fit(report.model_dump(), KARPENTER)
    assert not isinstance(check, str)
    assert check({"namespace": "shop", "pod": "web-1"}, "n1")
    assert not check({"namespace": "shop", "pod": "web-1"}, "n2")
    assert not check({"namespace": "shop", "pod": "keeper"}, "n2")
    assert check({"namespace": "shop", "pod": "keeper"}, "n3")


def test_nothing_is_read_or_sent_of_needs_unless_the_hub_asks() -> None:
    from squero import actuator
    from tests.test_blockers import apis

    core, policy = apis({"shop": [[affined("web-1", {"k": "v"})]]})
    report = actuator.blocking(core, policy, ["shop"], NOW)
    assert report.constraints is None and not core.list_node.called
    assert not {"constraints", "needs", "needed", "labels"} & set(blockers.sent(report))


def test_no_need_reads_no_node() -> None:
    from squero import actuator
    from tests.test_blockers import apis, kpod

    core, policy = apis({"shop": [[kpod("free")]]})
    report = actuator.blocking(core, policy, ["shop"], NOW, constraints=True)
    assert (report.constraints, report.needs, report.labels) == ("read", [], {})
    assert not core.list_node.called


@pytest.mark.parametrize(
    ("status", "code"), [(403, "nodes_refused"), (500, "nodes_unread"), (0, "nodes_unread")]
)
def test_nodes_it_may_not_or_could_not_read_are_said_with_a_code(status: int, code: str) -> None:
    from kubernetes.client.exceptions import ApiException

    from squero import actuator
    from tests.test_blockers import apis

    core, policy = apis({"shop": [[affined("web-1", {"k": "v"})]]})
    nodes_api(core, [{"n1": {}}, ApiException(status=status, reason="x")])
    report = actuator.blocking(core, policy, ["shop"], NOW, constraints=True)
    assert (report.constraints, report.needs, report.needed, report.labels) == (code, [], {}, {})
    said = blockers.fit(report.model_dump(), CA)
    assert isinstance(said, str)


def test_a_namespace_whose_read_failed_sends_none_of_its_needs() -> None:
    from kubernetes.client.exceptions import ApiException

    from squero import actuator
    from tests.test_blockers import apis

    pages = [[affined("web-1", {"k": "v"})], ApiException(status=500, reason="x")]
    core, policy = apis({"shop": pages, "bank": [[affined("ledger", {"k": "v"}, ns="bank")]]})
    nodes_api(core, [{"n1": {"k": "v"}}])
    report = actuator.blocking(core, policy, ["bank", "shop"], NOW, constraints=True)
    assert report.namespaces == ["bank"] and report.needed == {"bank/ledger": 0}


def test_more_than_a_report_holds_is_cut_and_said(monkeypatch: pytest.MonkeyPatch) -> None:
    from squero import actuator
    from tests.test_blockers import apis

    def built(*pods: Any, nodes: dict[str, Any] | None = None) -> Any:
        core, policy = apis({"shop": [list(pods)]})
        nodes_api(core, [nodes or {"n1": {}}])
        return actuator.blocking(core, policy, ["shop"], NOW, constraints=True)

    two = [affined("a", {"k": "1"}), affined("b", {"k": "2"})]
    assert built(*two).constraints == "read"
    monkeypatch.setattr(blockers, "MAX_NEEDED", 1)
    assert built(*two).constraints == "cut"
    monkeypatch.setattr(blockers, "MAX_NEEDED", 10)
    monkeypatch.setattr(blockers, "MAX_NEEDS", 1)
    assert built(*two).constraints == "cut"
    monkeypatch.setattr(blockers, "MAX_NEEDS", 10)
    many = affined(
        "m",
        {f"k{i}": "v" for i in range(50)},
        preferred=[(1, [k_expr(f"p{i}", "Exists") for i in range(50)])],
    )
    assert built(many).constraints == "read"  # 100 keys
    terms = [([k_expr(f"r{i}", "Exists") for i in range(j, j + 25)], []) for j in (0, 25)]
    hundred = affined("m", {f"k{i}": "v" for i in range(50)}, required=terms)
    assert built(hundred).constraints == "read"
    terms[1][0].append(k_expr("r50", "Exists"))
    one_more = affined("m", {f"k{i}": "v" for i in range(50)}, required=terms)
    assert built(one_more).constraints == "cut"  # 101 keys, beyond what a node's labels hold
    huge = affined(
        "h", required=[([k_expr("k", "Exists")], [])] * 51
    )  # more terms than a need holds
    assert built(huge, *two).constraints == "cut"
    monkeypatch.setattr(blockers, "MAX_NODES", 1)
    assert built(*two, nodes={"n1": {}, "n2": {}}).constraints == "cut"
    monkeypatch.setattr(blockers, "MAX_NODES", 10)
    whole = built(*two)
    monkeypatch.setattr(blockers, "MAX_BYTES", len(whole.model_dump_json()) - 1)
    small = built(*two)
    assert (small.constraints, small.needs, small.needed, small.labels) == ("cut", [], {}, {})


def test_the_actuator_reports_needs_when_its_hub_asks() -> None:
    import json

    from squero import actuator
    from tests.test_blockers import an_actuator

    seen: list[Any] = []
    a = an_actuator([204, 204], seen)
    a.core.list_namespaced_pod.side_effect = None
    a.core.list_namespaced_pod.return_value = __import__("kubernetes").client.V1PodList(
        items=[affined("web-1", {"k": "v"})], metadata=None
    )
    nodes_api(a.core, [{"n1": {"k": "v"}}])
    a.blocked(["shop"], NOW)
    assert "constraints" not in json.loads(seen[0].content)
    a.blocked(["shop"], NOW + actuator.BLOCKERS_EVERY, constraints=True)
    sent = json.loads(seen[1].content)
    assert (sent["constraints"], sent["needed"], sent["labels"]) == (
        "read",
        {"shop/web-1": 0},
        {"n1": {"k": "v"}},
    )


def test_the_node_read_is_its_own_clusterrole_listing_nodes_and_nothing_else() -> None:
    from pathlib import Path

    import yaml

    docs = list(
        yaml.safe_load_all((Path(__file__).parent.parent / "deploy/actuator/rbac.yaml").read_text())
    )
    (role,) = [
        d
        for d in docs
        if d["metadata"]["name"] == "squero-actuator-nodes" and d["kind"] == "ClusterRole"
    ]
    assert role["rules"] == [{"apiGroups": [""], "resources": ["nodes"], "verbs": ["list"]}]
    (bound,) = [
        d
        for d in docs
        if d["kind"] == "ClusterRoleBinding" and d["metadata"]["name"] == "squero-actuator-nodes"
    ]
    assert bound["roleRef"]["name"] == "squero-actuator-nodes"
    assert bound["subjects"] == [
        {"kind": "ServiceAccount", "name": "squero-actuator", "namespace": "squero-actuator"}
    ]
    elsewhere = [
        r
        for d in docs
        if d["metadata"]["name"] != "squero-actuator-nodes" and d["kind"] in ("ClusterRole", "Role")
        for r in d["rules"]
    ]
    assert "nodes" not in str(elsewhere)
