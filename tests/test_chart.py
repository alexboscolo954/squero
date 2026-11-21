"""The squero chart (Phase 18.1a): it replaces the Relay's and the Actuator's kustomize
directories, so rendered with their values it must give back exactly their objects (the
two Namespaces aside: the client creates those, with their token Secrets), each grant no
wider; one list of namespaces for every grant and setting; never a Secret; typos refused.
`tests/fixtures/rbac-0.61/` is what those directories rendered, kept as the reference.
A missing helm fails these tests: the RBAC guard must never quietly disappear."""

import json
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).parent.parent
CHART = ROOT / "charts/squero"
BEFORE = Path(__file__).parent / "fixtures/rbac-0.61"
RELAY = {"hub": "wss://hub.example.com/relay", "cluster": "demo"}
ACTUATOR = {"enabled": True, "hub": "https://hub.example.com"}


def render(**values: Any) -> list[dict[str, Any]]:
    """The chart's objects with these values (`namespaces` defaults to [demo])."""
    helm = shutil.which("helm")
    assert helm, "helm is needed to render the chart (CI installs it)"
    values = {"namespaces": ["demo"], **values}
    out = subprocess.run(  # noqa: S603  helm, from PATH, on the repo's own chart
        [helm, "template", "squero", str(CHART), "--values", "-"],
        input=json.dumps(values),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def refused(**values: Any) -> str:
    try:
        render(**values)
    except subprocess.CalledProcessError as e:
        return str(e.stderr)
    raise AssertionError("rendered")


def keyed(docs: list[dict[str, Any]]) -> dict[tuple[str, str, str], dict[str, Any]]:
    out = {}
    for d in docs:
        meta = d["metadata"]
        out[(d["kind"], meta.get("namespace", ""), meta["name"])] = d
    return out


def before(*names: str) -> dict[tuple[str, str, str], dict[str, Any]]:
    """What the kustomize directories rendered, their Namespaces left out."""
    docs = [d for n in names for d in yaml.safe_load_all((BEFORE / n).read_text()) if d]
    return {k: v for k, v in keyed(docs).items() if k[0] != "Namespace"}


RBAC = {"ServiceAccount", "ClusterRole", "Role", "ClusterRoleBinding", "RoleBinding"}


def rbac(objs: dict[tuple[str, str, str], dict[str, Any]]) -> dict[Any, Any]:
    return {k: v for k, v in objs.items() if k[0] in RBAC}


# --- the same objects as before ------------------------------------------------------


def test_the_relay_is_what_deploy_relay_was() -> None:
    assert keyed(render(relay=RELAY)) == before("relay.yaml")


def test_the_relays_writes_are_what_deploy_relay_actions_was() -> None:
    assert keyed(render(relay={**RELAY, "writes": True})) == before("relay-actions.yaml")


def test_the_relays_gitops_reads_are_what_rbac_gitops_was() -> None:
    gitops = ["argocd", "flux-system"]
    got = keyed(render(relay={**RELAY, "gitopsNamespaces": gitops}))
    assert rbac(got) == rbac(before("relay.yaml", "relay-gitops.yaml"))
    (c,) = got[("Deployment", "squero-relay", "squero-relay")]["spec"]["template"]["spec"][
        "containers"
    ]
    env = {e["name"]: e.get("value") for e in c["env"]}
    assert json.loads(env["SQUERO_RELAY_GITOPS_NAMESPACES"]) == gitops  # once set by hand


def test_the_actuator_is_what_deploy_actuator_was() -> None:
    got = keyed(render(relay={"enabled": False}, actuator=ACTUATOR))
    assert got == before("actuator.yaml")


@pytest.mark.parametrize(
    ("toggle", "gone"),
    [
        ("nodes", {"squero-actuator-nodes"}),
        ("nodeEvents", {"squero-actuator-node-events"}),
        ("self", {"squero-actuator-self"}),
    ],
)
def test_each_optional_actuator_grant_goes_alone(toggle: str, gone: set[str]) -> None:
    got = keyed(render(relay={"enabled": False}, actuator={**ACTUATOR, toggle: False}))
    want = {k: v for k, v in before("actuator.yaml").items() if k[2] not in gone}
    assert got == want


def test_both_together_are_each_as_before() -> None:
    got = keyed(render(relay=RELAY, actuator=ACTUATOR))
    assert got == {**before("relay.yaml"), **before("actuator.yaml")}


# --- one list of namespaces, never a Secret, nothing unknown ------------------------------


def test_one_list_of_namespaces_feeds_every_grant_and_setting() -> None:
    spaces = ["orders", "shop"]
    got = keyed(render(namespaces=spaces, relay={**RELAY, "writes": True}, actuator=ACTUATOR))
    for name in ("squero-relay-read", "squero-relay-actions", "squero-actuator-resize"):
        bound = sorted(k[1] for k in got if k[0] == "RoleBinding" and k[2] == name)
        assert bound == spaces, name
    actions = sorted(k[1] for k in got if k[0] == "Role" and k[2] == "squero-relay-actions")
    assert actions == spaces
    (c,) = got[("Deployment", "squero-relay", "squero-relay")]["spec"]["template"]["spec"][
        "containers"
    ]
    env = {e["name"]: e.get("value") for e in c["env"]}
    assert json.loads(env["SQUERO_RELAY_NAMESPACES"]) == spaces
    (hook,) = got[("MutatingWebhookConfiguration", "", "squero-actuator")]["webhooks"]
    assert hook["namespaceSelector"]["matchExpressions"][0]["values"] == spaces
    assert "caBundle" not in hook["clientConfig"]  # the Actuator's own, set at its start


def test_the_parts_own_namespaces_are_followed_everywhere() -> None:
    got = keyed(
        render(
            relay={**RELAY, "namespace": "ops-relay"},
            actuator={**ACTUATOR, "namespace": "ops-actuator"},
        )
    )
    for kind, ns, name in got:
        d = got[(kind, ns, name)]
        for s in d.get("subjects", []):
            want = "ops-relay" if s["name"] == "squero-relay" else "ops-actuator"
            assert s["namespace"] == want, (kind, name)
    (hook,) = got[("MutatingWebhookConfiguration", "", "squero-actuator")]["webhooks"]
    assert hook["clientConfig"]["service"]["namespace"] == "ops-actuator"


@pytest.mark.parametrize(
    ("values", "said"),
    [
        ({"namespaces": [], "relay": RELAY}, "namespaces:"),
        ({"namespaces": [], "relay": {"enabled": False}, "actuator": ACTUATOR}, "namespaces:"),
        ({"relay": {"cluster": "demo"}}, "relay.hub"),
        ({"relay": {"hub": "wss://h/relay"}}, "relay.cluster"),
        ({"relay": {"enabled": False}, "actuator": {"enabled": True}}, "actuator.hub"),
    ],
)
def test_what_it_needs_is_asked_for(values: dict[str, Any], said: str) -> None:
    assert said in refused(**values)


@pytest.mark.parametrize(
    "values",
    [
        {"relay": {**RELAY, "token": "s3cret"}},  # a token is never a value
        {"relay": {**RELAY, "env": {"SQUERO_RELAY_TOKEN": "s3cret"}}},
        {"actuator": {**ACTUATOR, "tokn": "x"}, "relay": RELAY},
        {"relay": RELAY, "namespace": ["demo"]},
    ],
)
def test_an_unknown_value_is_refused(values: dict[str, Any]) -> None:
    assert "not allowed" in refused(**values)


def test_no_secret_is_ever_rendered() -> None:
    every = render(relay={**RELAY, "writes": True, "gitopsNamespaces": ["argocd"]},
                   actuator=ACTUATOR)  # fmt: skip
    assert "Secret" not in {d["kind"] for d in every}
    for d in every:
        for r in d.get("rules", []):
            assert "secrets" not in r["resources"], d["metadata"]["name"]


def test_its_settings_reach_the_relay_and_the_actuator() -> None:
    got = keyed(
        render(
            relay={**RELAY, "prometheusUrl": "http://prometheus.monitoring:9090",
                   "alertsPort": 9095, "callSeconds": 45},
            actuator={**ACTUATOR, "interval": 30},
        )
    )  # fmt: skip

    def env(kind_ns_name: tuple[str, str, str]) -> dict[str, Any]:
        (c,) = got[kind_ns_name]["spec"]["template"]["spec"]["containers"]
        return {e["name"]: e.get("value") for e in c["env"]}

    relay = env(("Deployment", "squero-relay", "squero-relay"))
    assert relay["SQUERO_RELAY_PROMETHEUS_URL"] == "http://prometheus.monitoring:9090"
    assert (relay["SQUERO_RELAY_ALERTS_PORT"], relay["SQUERO_RELAY_CALL_SECONDS"]) == ("9095", "45")
    (c,) = got[("Deployment", "squero-relay", "squero-relay")]["spec"]["template"]["spec"][
        "containers"
    ]
    assert c["ports"] == [{"name": "alerts", "containerPort": 9095}]
    assert (
        env(("Deployment", "squero-actuator", "squero-actuator"))["SQUERO_ACTUATOR_INTERVAL"]
        == "30"
    )


# --- its version ------------------------------------------------------------------------


def test_the_chart_is_the_releases_version_and_its_image_by_default() -> None:
    chart = yaml.safe_load((CHART / "Chart.yaml").read_text())
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert chart["version"] == chart["appVersion"] == version
    got = keyed(render(relay=RELAY))
    (c,) = got[("Deployment", "squero-relay", "squero-relay")]["spec"]["template"]["spec"][
        "containers"
    ]
    assert c["image"] == f"ghcr.io/alexboscolo954/squero:{version}"
    pinned = keyed(render(relay=RELAY, image={"tag": "0.1.0"}))
    (c,) = pinned[("Deployment", "squero-relay", "squero-relay")]["spec"]["template"]["spec"][
        "containers"
    ]
    assert c["image"] == "ghcr.io/alexboscolo954/squero:0.1.0"
