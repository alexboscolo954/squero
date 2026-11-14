"""Contracts: one directory per Contract in the private contracts repo (ADR 0003).

    <contracts-dir>/<id>/contract.yaml       what the Contract runs; secret *names* only
    <contracts-dir>/<id>/secrets.sops.yaml   SOPS-encrypted map: secret name -> value
    <contracts-dir>/<id>/runbooks/*.md       this Contract's Runbooks
    <contracts-dir>/shared/runbooks/*.md     Shared knowledge, for every Contract;
                                             never naming one (load() refuses it)

`load()` reads every Contract, decrypts its secrets with the `sops` binary and
reports every problem at once as `path: field: message`. Secret values never
appear in errors, logs or repr.
"""

import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import shutil
import subprocess
from collections import defaultdict
from collections.abc import Callable, Sequence
from datetime import datetime
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Discriminator,
    Field,
    PrivateAttr,
    SecretStr,
    Tag,
    ValidationError,
    model_validator,
)

from squero import checks

SECRETS_FILE = "secrets.sops.yaml"
SHARED = "shared"

# Becomes a URL path segment (/contracts/{id}/...), so a lowercase slug.
Slug = Annotated[str, Field(pattern=r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$", max_length=63)]


class Strict(BaseModel):
    # A typo must fail, not be ignored. Fields are added when a slice gives them behaviour.
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class ClusterPrices(Strict):
    """Unit prices (slice 10.1), in the Contract's currency."""

    cpu: float = Field(ge=0)  # per vCPU-hour
    memory: float = Field(ge=0)  # per GiB-hour


class Prices(ClusterPrices):
    currency: str = Field("USD", pattern=r"^[A-Z]{3}$")  # never converted


# A GCP label: lowercase letters, digits, _ and -, starting with a letter; key up to 63.
GCP_LABEL = re.compile(r"[a-z][a-z0-9_-]{0,62}=[a-z0-9_-]{0,63}")


class Billing(Strict):
    """Which bill lines are this cluster's (slices 10.5a to 10.5c): the ones carrying this
    tag, `key=value`. In AWS the tag must be activated in Billing; Azure needs nothing;
    GCP reads a label from the billing export's BigQuery `table` (project.dataset.table),
    which GCP clusters must name and the others must not."""

    tag: str = Field(pattern=r"^[A-Za-z0-9_.:/+@ -]{1,128}=[A-Za-z0-9_.:/+@ -]{1,256}$")
    # The table goes into the query's SQL (a table can't be a parameter): a plain name only.
    table: str | None = Field(
        None, pattern=r"^[a-z][a-z0-9-]{4,28}[a-z0-9]\.[A-Za-z0-9_]{1,128}\.[A-Za-z0-9_]{1,128}$"
    )


# What a Relay cluster's own Relay holds: set on the Hub, they'd reach it Direct.
RELAY_HOLDS = (
    "in_cluster",
    "context",
    "kubeconfig_file",
    "kubeconfig_secret",
    "allow_exec",
    "prometheus_url",
    "prometheus_token_secret",
)


class Cluster(Strict):
    name: Slug
    namespaces: list[str] = Field(min_length=1)
    # Where ArgoCD Applications and Flux Kustomizations/HelmReleases live (argocd,
    # flux-system). Only recent_changes reads there, and only objects that deploy
    # into `namespaces`; no other tool may name these namespaces.
    gitops_namespaces: list[str] = []
    in_cluster: bool = False
    context: str | None = None
    kubeconfig_file: Path | None = None  # default: ~/.kube/config
    kubeconfig_secret: str | None = None
    # A kubeconfig credential plugin runs as a command on the Hub, next to every other
    # Contract's secrets. Refused unless set here, where review of the contracts repo sees it.
    allow_exec: bool = False
    # Where this cluster's metrics are for Sizings: its own Prometheus (else the
    # Contract's prometheus integration), and labels that pick its series out of a
    # store shared by several clusters (Thanos, Mimir), added to every query.
    prometheus_url: str | None = None
    prometheus_token_secret: str | None = None
    prometheus_labels: dict[Annotated[str, Field(pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$")], str] = {}
    prices: ClusterPrices | None = None  # else the Contract's
    provider: Literal["aws", "azure", "gcp"] | None = None  # else read from kube_node_info
    # What removes this cluster's nodes (slice 15.1a): named, never guessed from which
    # metrics exist, since their rules differ. Shown with its nodes; none by default.
    autoscaler: Literal["cluster-autoscaler", "karpenter"] | None = None
    billing: Billing | None = None  # else no bill to reconcile with
    # Reached through a Relay inside the client's network (slice 3.1), which holds this
    # cluster's credentials and dials the Hub with this secret's value: the Hub never
    # reaches it Direct, so it names no kube context, kubeconfig or Prometheus here.
    via: Literal["direct", "relay"] = "direct"
    relay_token_secret: str | None = None
    # This cluster runs an Actuator (slice 12.1), which pulls with this secret's value.
    # Its own token, never the Relay's: the Relay stays read-only. Direct or Relay alike.
    actuator_token_secret: str | None = None

    @model_validator(mode="after")
    def _one_source(self) -> "Cluster":
        if self.via == "relay":
            if not self.relay_token_secret:
                raise ValueError("via: relay needs relay_token_secret")
            direct = [f for f in RELAY_HOLDS if getattr(self, f) not in (None, False, {})]
            if direct:
                raise ValueError(f"via: relay: the Relay holds these, not the Hub: {direct}")
            return self
        if self.relay_token_secret:
            raise ValueError("relay_token_secret is for via: relay")
        if self.prometheus_token_secret and not self.prometheus_url:
            raise ValueError("prometheus_token_secret needs prometheus_url")
        if (
            self.in_cluster
            + (self.kubeconfig_file is not None)
            + (self.kubeconfig_secret is not None)
            > 1
        ):
            raise ValueError("set only one of in_cluster, kubeconfig_file, kubeconfig_secret")
        if not self.in_cluster and not self.context:
            raise ValueError("context is required unless in_cluster (no silent default context)")
        return self


class Kubernetes(Strict):
    name: Literal["kubernetes"]
    enable_actions: bool = False
    max_replicas: int = Field(10, ge=1)


class Prometheus(Strict):
    name: Literal["prometheus"]
    url: str
    token_secret: str | None = None


class Jira(Strict):
    """The Contract's Jira Cloud: its past issues as Evidence, its project for new ones."""

    name: Literal["jira"]
    url: str = Field(pattern=r"^https://[^/\s]+/?$")  # Basic auth: never over plain http
    email: str  # the API token's owner
    token_secret: str
    project: str = Field(pattern=r"^[A-Z][A-Z0-9_]+$")  # every search stays inside it
    issue_type: str = "Task"  # for the Create Jira issue button: as the project names it


class Aws(Strict):
    """The Contract's AWS account, read-only: CloudWatch metrics, alarms and logs, and
    CloudTrail's management events, with an IAM user's key."""

    name: Literal["aws"]
    regions: list[Annotated[str, Field(pattern=r"^[a-z]{2}(-[a-z]+)+-[0-9]$")]] = Field(
        min_length=1
    )  # every call goes to one of these; the first is the default
    access_key_id: str = Field(pattern=r"^AKIA[A-Z0-9]{16}$")  # an IAM user's long-term key
    secret_access_key_secret: str


class Runbooks(Strict):
    name: Literal["runbooks"]  # reads <contract dir>/runbooks


class Incidents(Strict):
    name: Literal["incidents"]  # the Contract's own past Reports, from the Hub's database
    # false: its Reports never go to the embedding model (SQUERO_EMBEDDING_MODEL); words only
    embeddings: bool = True


class Sizings(Strict):
    name: Literal["sizing"]  # the agent reads the Hub's stored Sizings (slice 9.6a)


class Probe(Strict):
    tool: str  # one of the vendor's tools listed `read`
    args: dict[str, Any] = {}


class Vendor(Strict):
    """A vendor MCP server, started by the Hub from its registry (servers/vendors.py).
    Its tools appear as <name>_<tool>; only those in `tools` are visible, with the
    Hub's own read or write (ADR 0002). Secrets reach it as environment variables."""

    name: Slug
    vendor: str
    env: dict[str, str] = {}
    env_secrets: dict[str, str] = {}  # variable -> secret name in secrets.sops.yaml
    tools: dict[str, Literal["read", "write"]] = Field(min_length=1)
    probe: Probe | None = None  # health; default: start it and check `tools` are offered

    @model_validator(mode="before")
    @classmethod
    def _is_vendor(cls, data: Any) -> Any:
        if isinstance(data, dict) and "vendor" not in data:
            raise ValueError(
                f"unknown integration {data.get('name')!r}: the builtins are "
                f"{', '.join(BUILTINS)}; a vendor integration needs `vendor:`"
            )
        return data

    @model_validator(mode="after")
    def _known(self) -> "Vendor":
        from squero.servers.vendors import VENDORS

        if self.vendor not in VENDORS:
            known = ", ".join(sorted(VENDORS)) or "none yet"
            raise ValueError(f"vendor {self.vendor!r} isn't in the Hub's registry ({known})")
        allowed = VENDORS[self.vendor].env
        if refused := sorted((set(self.env) | set(self.env_secrets)) - allowed):
            raise ValueError(
                f"vendor {self.vendor!r} takes only these variables: "
                f"{', '.join(sorted(allowed)) or 'none'}; refused: {', '.join(refused)}"
            )
        if plain := sorted(set(self.env) & VENDORS[self.vendor].secret_env):
            raise ValueError(
                f"{', '.join(plain)} hold credentials: set them in env_secrets "
                "(a name in secrets.sops.yaml), never in plain env"
            )
        if self.probe and self.tools.get(self.probe.tool) != "read":
            raise ValueError(f"probe tool {self.probe.tool!r} must be listed in tools as read")
        spec = VENDORS[self.vendor]
        if missing := sorted(set(spec.required_env) - set(self.env) - set(self.env_secrets)):
            raise ValueError(f"vendor {self.vendor!r} needs {', '.join(missing)}")
        for var, form in spec.required_env.items():
            if var in self.env and not re.fullmatch(form, self.env[var]):
                raise ValueError(f"{var}: {self.env[var]!r} doesn't match {form}")
        if spec.probe and not self.probe and self.tools.get(spec.probe[0]) != "read":
            raise ValueError(
                f"list {spec.probe[0]!r} in tools as read: it's the {self.vendor} health check"
            )
        return self


BUILTINS = ("kubernetes", "prometheus", "runbooks", "jira", "aws", "incidents", "sizing")


def _kind(value: Any) -> str:
    name = value.get("name") if isinstance(value, dict) else getattr(value, "name", None)
    return name if name in BUILTINS else "vendor"


Integration = Annotated[
    Annotated[Kubernetes, Tag("kubernetes")]
    | Annotated[Prometheus, Tag("prometheus")]
    | Annotated[Runbooks, Tag("runbooks")]
    | Annotated[Jira, Tag("jira")]
    | Annotated[Aws, Tag("aws")]
    | Annotated[Incidents, Tag("incidents")]
    | Annotated[Sizings, Tag("sizing")]
    | Annotated[Vendor, Tag("vendor")],
    Discriminator(_kind),
]


class Slack(Strict):
    # A channel ID (C0123ABCD), not a name: names can be renamed. The bot must be
    # a member. One Contract per channel, so a channel always names its Contract.
    operator_channel: str = Field(pattern=r"^[CG][A-Z0-9]{8,}$")
    # Optional: where the Operator can send Client reports (a Slack Connect channel
    # shared with the client). Never gets buttons.
    client_channel: str | None = Field(None, pattern=r"^[CG][A-Z0-9]{8,}$")

    @model_validator(mode="after")
    def _apart(self) -> "Slack":
        if self.client_channel == self.operator_channel:
            raise ValueError("client_channel must not be the operator_channel: clients see it")
        return self


class Related(Strict):
    """When a new Alert group joins an open Triage (a Related alert group): a scope
    label both have must be equal, and at least one match label must be shared.
    No match labels: never joins. window_minutes: the Hub's default unless set."""

    scope_labels: list[str] = ["cluster", "namespace"]
    match_labels: list[str] = ["service", "service_name", "app", "deployment"]
    window_minutes: int | None = Field(None, ge=1)


class Check(Strict):
    """A scheduled Check: the prompt says what healthy means, the schedule when to
    look (5-field cron in `timezone`, at most once every 15 minutes)."""

    name: Slug
    schedule: str
    timezone: str = "UTC"
    prompt: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def _schedule(self) -> "Check":
        checks.validate(self.schedule, self.timezone)
        return self


class DeployChecks(Strict):
    """Deploy verification: a Check of each deploy the Hub sees, `delay_minutes` after
    it. namespaces: only these (of the clusters'); empty: all. prompt: a sentence
    added to the Hub's."""

    delay_minutes: int = Field(10, ge=1, le=120)
    namespaces: list[str] = []
    prompt: str | None = Field(None, min_length=1, max_length=1000)


def duration_s(text: str) -> int:
    """`90m`, `6h`, `7d` in seconds."""
    m = re.fullmatch(r"([1-9][0-9]*)([mhd])", text)
    if not m:
        raise ValueError(f"{text!r}: use a number and m, h or d, like 6h or 7d")
    return int(m.group(1)) * {"m": 60, "h": 3600, "d": 86400}[m.group(2)]


def _duration(lo: str, hi: str) -> Callable[[Any], Any]:
    """Seconds, written `72h` or `7d` in YAML (a number: seconds), from lo to hi."""

    def parse(v: Any) -> Any:
        if isinstance(v, str):
            v = duration_s(v)
        if (
            isinstance(v, int)
            and not isinstance(v, bool)
            and not (duration_s(lo) <= v <= duration_s(hi))
        ):
            raise ValueError(f"must be from {lo} to {hi}")
        return v

    return parse


def _quantity(v: Any) -> Any:
    """A Kubernetes quantity (`500m`, `16Gi`) as a number: cores or bytes."""
    if isinstance(v, str):
        from kubernetes.utils import parse_quantity

        try:
            return float(parse_quantity(v))  # type: ignore[no-untyped-call]
        except ValueError as e:
            raise ValueError(f"{v!r} is not a Kubernetes quantity like 500m or 16Gi") from e
    return v


Quantity = Annotated[float, BeforeValidator(_quantity), Field(gt=0)]


class ResourcePolicy(Strict):
    headroom: float = Field(ge=0, le=3)  # 0.15: 15% above the basis
    floor: Quantity
    max: Quantity | None = None  # capped there, and listed as a risk


class CpuPolicy(ResourcePolicy):
    percentile: float = Field(ge=0.5, le=1.0)  # of the decay-weighted samples


class ReplicasPolicy(Strict):
    """Of a Workload with an autoscaler (slice 14.1b)."""

    percentile: float = Field(ge=0, le=0.5)  # of its hourly Needs: its Floor


class Policy(Strict):
    """How a Sizing is computed (CONTEXT.md, Sizing policy). Memory is always the peak:
    a maximum can't be weighted. The safety holds (throttling, OOM kills) are not here:
    no policy may turn them off."""

    base: Literal["production", "high-availability", "batch"] | None = None  # yours: required
    window: Annotated[int, BeforeValidator(_duration("1h", "90d"))]
    half_life: Annotated[int, BeforeValidator(_duration("1h", "30d"))]
    history: Annotated[int, BeforeValidator(_duration("1h", "90d"))]  # before a first Sizing
    threshold: float = Field(ge=0, le=0.5)  # relative change below which a request is kept
    limits: Literal["keep"] = "keep"  # other strategies when the Actuator writes (Phase 12)
    cpu: CpuPolicy
    memory: ResourcePolicy
    replicas: ReplicasPolicy = ReplicasPolicy(percentile=0.10)

    @model_validator(mode="after")
    def _consistent(self) -> "Policy":
        if self.history > self.window:
            raise ValueError("history must fit in the window")
        for name, r in (("cpu", self.cpu), ("memory", self.memory)):
            if r.max is not None and r.floor > r.max:
                raise ValueError(f"{name} floor is above its max")
        return self


H, D, MI = 3600, 86400, 2**20
OFF = "off"  # a Workload (or container) that gets no Sizing
BUILTIN = {
    "production": Policy(
        window=7 * D, half_life=24 * H, history=24 * H, threshold=0.10,
        cpu=CpuPolicy(percentile=0.95, headroom=0.15, floor=0.010),
        memory=ResourcePolicy(headroom=0.20, floor=32 * MI),
    ),
    "high-availability": Policy(
        window=14 * D, half_life=72 * H, history=72 * H, threshold=0.10,
        cpu=CpuPolicy(percentile=0.99, headroom=0.30, floor=0.050),
        memory=ResourcePolicy(headroom=0.35, floor=64 * MI),
        replicas=ReplicasPolicy(percentile=0.25),
    ),
    "batch": Policy(
        window=14 * D, half_life=24 * H, history=24 * H, threshold=0.10,
        cpu=CpuPolicy(percentile=0.90, headroom=0.10, floor=0.010),
        memory=ResourcePolicy(headroom=0.15, floor=32 * MI),
    ),
}  # fmt: skip
BATCH_KINDS = ("Job", "CronJob")


def _off(v: Any) -> Any:
    """YAML reads a bare `off` as false."""
    return OFF if v is False else v


PolicyName = Annotated[str, BeforeValidator(_off)]


class SizingRule(Strict):
    """Which policy a Workload's containers get. Every field set must match; namespace
    and name take globs (`checkout-*`)."""

    cluster: str | None = None
    namespace: str | None = None
    kind: str | None = None  # Deployment, StatefulSet, DaemonSet, CronJob, Job, Rollout, ...
    name: str | None = None
    container: str | None = None
    policy: PolicyName
    # Automation (slice 12.5, ADR 0004): the Actuator applies this rule's Sizings unasked
    automate: bool = False
    # the Startup boost (slice 13.4): its containers' new pods start with their CPU limit
    # times this, until the pod is Ready
    startup_boost: float | None = Field(None, ge=1.5, le=4)

    @model_validator(mode="after")
    def _matches_something(self) -> "SizingRule":
        if not {"cluster", "namespace", "kind", "name", "container"} & self.model_fields_set:
            raise ValueError("a rule needs cluster, namespace, kind, name or container")
        if self.automate and self.policy == OFF:
            raise ValueError("automate: a rule with policy off has no Sizings to apply")
        if self.startup_boost and not self.automate:
            raise ValueError("startup_boost: only on a rule with automate")
        return self

    def matches(self, cluster: str, namespace: str, kind: str, name: str, container: str) -> bool:
        return (
            self.cluster in (None, cluster)
            and (self.namespace is None or fnmatchcase(namespace, self.namespace))
            and self.kind in (None, kind)
            and (self.name is None or fnmatchcase(name, self.name))
            and self.container in (None, container)
        )


class Digest(Strict):
    """The weekly Sizing digest's time: 5-field cron in `timezone` (slice 9.5b)."""

    schedule: str = "0 9 * * 1"  # Monday 09:00
    timezone: str = "UTC"

    @model_validator(mode="after")
    def _schedule(self) -> "Digest":
        checks.validate(self.schedule, self.timezone)
        return self


def _lead(v: Any) -> Any:
    """`15m` in YAML (a number: seconds), whole minutes from 0 to 2h."""
    if isinstance(v, str):
        v = duration_s(v)
    if isinstance(v, int) and not isinstance(v, bool) and not (0 <= v <= 7200 and v % 60 == 0):
        raise ValueError("must be whole minutes from 0 to 2h")
    return v


def _timezone(v: Any) -> Any:
    if isinstance(v, str):
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(f"{v!r} is not an IANA time zone like Europe/Rome") from e
    return v


class SizingConfig(Strict):
    """Sizing policies: your own, each a built-in (`base:`) with changes, and the
    rules that pick one per container. The rules first, top to bottom, then a Job or
    CronJob gets `batch`, then `default`. Built-in names mean the same in every
    Contract: a standing approval names a policy (ADR 0004)."""

    default: PolicyName = "production"
    policies: dict[Slug, Policy] = {}
    rules: list[SizingRule] = []
    # To the operator channel only, when the Contract has one and a Prometheus.
    digest: Annotated[Digest | Literal["off"], BeforeValidator(_off)] = Digest()
    # The Weekly profile (slice 14.2): the zone its hours are shown and scheduled in,
    # and how early a block that asks for more starts.
    timezone: Annotated[str, BeforeValidator(_timezone)] = "UTC"
    lead_time: Annotated[int, BeforeValidator(_lead)] = 900

    @model_validator(mode="before")
    @classmethod
    def _bases(cls, data: Any) -> Any:
        """Each policy is its base's settings with the ones it names replaced."""
        if not isinstance(data, dict) or not isinstance(data.get("policies"), dict):
            return data
        merged: dict[str, Any] = {}
        for name, p in data["policies"].items():
            if isinstance(p, Policy):
                merged[name] = p
                continue
            if not isinstance(p, dict) or p.get("base") not in BUILTIN:
                raise ValueError(f"policy {name}: base must be one of {', '.join(BUILTIN)}")
            out = BUILTIN[p["base"]].model_dump()
            for key, value in p.items():
                if key in ("cpu", "memory", "replicas") and isinstance(value, dict):
                    out[key] = {**out[key], **value}
                else:
                    out[key] = value
            merged[name] = out
        return {**data, "policies": merged}

    @model_validator(mode="after")
    def _known(self) -> "SizingConfig":
        if taken := sorted(set(self.policies) & {*BUILTIN, OFF}):
            raise ValueError(f"policy names taken by the built-ins: {taken}")
        known = {*self.policies, *BUILTIN, OFF}
        for where, name in [("default", self.default)] + [
            (f"rule {i + 1}", r.policy) for i, r in enumerate(self.rules)
        ]:
            if name not in known:
                raise ValueError(f"{where}: unknown policy {name!r}")
        return self

    def policy_for(self, cluster: str, namespace: str, kind: str, name: str, container: str) -> str:
        for r in self.rules:
            if r.matches(cluster, namespace, kind, name, container):
                return r.policy
        return "batch" if kind in BATCH_KINDS else self.default

    def automated(self, cluster: str, namespace: str, kind: str, name: str, container: str) -> bool:
        """Whether the rule that picks this container's policy automates it (slice 12.5)."""
        rule = next(
            (r for r in self.rules if r.matches(cluster, namespace, kind, name, container)), None
        )
        return rule is not None and rule.automate

    def startup_boost(
        self, cluster: str, namespace: str, kind: str, name: str, container: str
    ) -> float | None:
        """The Startup boost of the rule that picks this container's policy (slice 13.4)."""
        rule = next(
            (r for r in self.rules if r.matches(cluster, namespace, kind, name, container)), None
        )
        return rule.startup_boost if rule else None

    def policy(self, name: str) -> Policy | None:
        """The settings for a policy name; None for `off`."""
        return self.policies.get(name) or BUILTIN.get(name)

    def windows(self) -> list[int]:
        """Every window a rule, the default or the batch rule can need, shortest first."""
        names = {self.default, "batch", *(r.policy for r in self.rules)}
        return sorted({p.window for n in names if (p := self.policy(n))})


MASK_KEY = "mask-key"  # the secret the placeholders are keyed with (masking.py)


class Mask(Strict):
    """Identifiers replaced with placeholders before the LLM sees them (masking.py).
    A term is matched case-insensitively as a whole word; a domain covers its
    subdomains. Kubernetes names are never matched unless a term says so."""

    terms: list[Annotated[str, Field(min_length=3)]] = []
    emails: bool = True
    ips: bool = True

    @model_validator(mode="after")
    def _something(self) -> "Mask":
        if not (self.terms or self.emails or self.ips):
            raise ValueError("mask masks nothing: list terms or turn on emails or ips")
        return self


class DataPolicy(Strict):
    """What may be sent to the LLM. No Data policy, no restrictions (CONTEXT.md)."""

    mask: Mask | None = None


# A Kubernetes label key: an optional DNS prefix and a name (app.kubernetes.io/team)
LabelKey = Annotated[
    str,
    Field(pattern=r"^([a-z0-9]([-a-z0-9.]*[a-z0-9])?/)?[A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?$"),
]
NO_TEAM = "(no team)"


def ksm_label(key: str) -> str:
    """kube-state-metrics' name for a pod label on kube_pod_labels."""
    return "label_" + re.sub(r"[^a-zA-Z0-9_]", "_", key)


class TeamRule(Strict):
    """Which team a Workload's cost goes to (slice 10.2b). Every field set must match;
    namespace takes a glob, labels are values of the allocation's labels."""

    team: str = Field(min_length=1, pattern=r"^[^(]")  # "(no team)" is for none
    cluster: str | None = None
    namespace: str | None = None
    labels: dict[str, str] = {}

    @model_validator(mode="after")
    def _matches_something(self) -> "TeamRule":
        if self.cluster is None and self.namespace is None and not self.labels:
            raise ValueError("a team rule needs cluster, namespace or labels")
        return self

    def matches(self, cluster: str, namespace: str, labels: dict[str, str]) -> bool:
        return (
            self.cluster in (None, cluster)
            and (self.namespace is None or fnmatchcase(namespace, self.namespace))
            and all(labels.get(k) == v for k, v in self.labels.items())
        )


class Allocation(Strict):
    """Cost by label and team (slice 10.2b): the pod labels the Hub reads from
    kube-state-metrics' kube_pod_labels (which must allowlist them), and the team
    rules, first match wins."""

    labels: list[LabelKey] = []
    teams: list[TeamRule] = []

    @model_validator(mode="after")
    def _known(self) -> "Allocation":
        names = [ksm_label(k) for k in self.labels]
        if clash := sorted({k for k in self.labels if names.count(ksm_label(k)) > 1}):
            raise ValueError(f"labels {clash} are one label in kube-state-metrics")
        for i, r in enumerate(self.teams):
            if unknown := sorted(set(r.labels) - set(self.labels)):
                raise ValueError(f"team rule {i + 1}: labels {unknown} are not in labels")
        return self

    def team(self, cluster: str, namespace: str, labels: dict[str, str]) -> str:
        return next((r.team for r in self.teams if r.matches(cluster, namespace, labels)), NO_TEAM)


DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
Day = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
Clock = Annotated[str, Field(pattern=r"^(([01]\d|2[0-3]):[0-5]\d|24:00)$")]


def minutes(clock: str) -> int:
    hours, mins = clock.split(":")
    return int(hours) * 60 + int(mins)


class Window(Strict):
    """An Eviction window (slice 12.7): from `start` to `end` on each of `days`; an end at or
    before its start runs past midnight, the night belonging to the day it starts."""

    days: list[Day] = Field(min_length=1)
    start: Clock
    end: Clock

    @model_validator(mode="after")
    def _spans(self) -> "Window":
        if self.start == "24:00" or self.start == self.end:
            raise ValueError(f"a window from {self.start} to {self.end} is no window")
        return self

    def holds(self, day: int, minute: int) -> bool:
        """Whether this window is open on weekday `day` (0 Monday) at `minute` past midnight."""
        s, e = minutes(self.start), minutes(self.end)
        if s < e:
            return DAYS[day] in self.days and s <= minute < e
        return (DAYS[day] in self.days and minute >= s) or (
            DAYS[day - 1] in self.days and minute < e
        )


class OomReflex(Strict):
    """The Reflex for OOM kills, on by being there (slice 13.1a): the Actuator tells each
    OOM kill of an automated container as container-limit or node-level, reports it, and
    answers a container-limit one with a Step-up (slice 13.1b, ADR 0005), never past `max`
    times the memory limit in the Workload's template. A Step-up ends `expires` after its
    last step, unless the container's memory peak is still above that limit (slice 13.1c)."""

    max: float = Field(2.0, gt=1, le=4)
    expires: Annotated[int, BeforeValidator(_duration("1h", "90d"))] = 7 * 86400


class BurstReflex(Strict):
    """The Reflex for CPU throttling, on by being there (slice 13.3a): each hour the Hub
    tells the cluster's Actuator which automated containers were throttled in more than a
    quarter of their CPU periods over the last hour (a Burst is due), and each one's CPU
    peak over the last `expires`, which is what keeps a Burst past its term. The Actuator
    makes it (slice 13.3b), never past `max` times the CPU limit in the Workload's template."""

    max: float = Field(2.0, gt=1, le=4)
    expires: Annotated[int, BeforeValidator(_duration("1h", "7d"))] = 86400


class Reflexes(Strict):
    """The Contract's Reflexes (Phase 13), each off unless set."""

    oom: OomReflex | None = None
    burst: BurstReflex | None = None


class ActuatorConfig(Strict):
    """What the Contract's Actuators may do beyond resizing in place (slice 12.7): evict a
    pod whose resize is Infeasible, only inside one of its windows, in its timezone; and
    its Reflexes (slice 13.1a)."""

    eviction: bool = False
    timezone: str = "UTC"
    windows: list[Window] = []
    reflexes: Reflexes = Reflexes()

    @model_validator(mode="after")
    def _usable(self) -> "ActuatorConfig":
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"actuator: no timezone {self.timezone!r}") from None
        if self.eviction and not self.windows:
            raise ValueError("actuator: eviction needs at least one window")
        return self

    def evicting(self, at: datetime) -> Literal["off", "closed", "open"]:
        """Whether its Actuators may evict at `at`, as the Hub tells them."""
        if not self.eviction:
            return "off"
        local = at.astimezone(ZoneInfo(self.timezone))
        minute = local.hour * 60 + local.minute
        return "open" if any(w.holds(local.weekday(), minute) for w in self.windows) else "closed"


class ListPrices(Strict):
    """Cloud list prices (slice 10.4): a cluster with no typed prices gets its nodes'
    on-demand list price, in USD unless `currency` says otherwise (Azure's catalog only;
    slice 10.5b). `weight`: how many GiB one vCPU is worth when a
    node's price is split; `discount`: what a deal takes off the list price."""

    currency: str = Field("USD", pattern=r"^[A-Z]{3}$")  # never converted
    weight: float = Field(7.0, gt=0)
    discount: float = Field(1.0, gt=0, le=1)
    gcp_key_secret: str | None = None  # GCP's catalog needs an API key; AWS and Azure don't


class Contract(Strict):
    id: Slug
    name: str
    # SHA-256 of the bearer token this Contract's Alertmanager sends. A hash, not a
    # credential, so it lives in plain YAML (`squero contracts new-token` makes one).
    webhook_token_sha256: str | None = Field(None, pattern=r"^[0-9a-f]{64}$")
    clusters: list[Cluster] = []
    integrations: list[Integration] = Field(min_length=1)
    slack: Slack | None = None  # where the Operator follows this Contract's Investigations
    # The Hub's SQUERO_MODEL unless set; through the Hub's gateway and key (same prefix).
    model: str | None = None
    monthly_budget_usd: float | None = Field(None, gt=0)  # calendar month, UTC
    related: Related = Related()
    checks: list[Check] = []
    deploy_checks: DeployChecks | None = None  # off unless set
    sizing: SizingConfig = SizingConfig()
    data_policy: DataPolicy = DataPolicy()
    prices: Prices | None = None  # none: Sizings in cores and GiB, no money
    list_prices: ListPrices | None = None  # else typed prices only
    allocation: Allocation = Allocation()
    actuator: ActuatorConfig = ActuatorConfig()  # eviction (slice 12.7)

    _dir: Path = PrivateAttr(default=Path())
    _secrets: dict[str, SecretStr] = PrivateAttr(default_factory=dict)
    _runbook_dirs: list[Path] = PrivateAttr(default_factory=list)
    # Its clusters whose Prometheus another loaded Contract reads too: no PromQL there
    _shared_stores: set[str] = PrivateAttr(default_factory=set)
    # Where each cluster's price comes from (typed, list, or why none), set by listprices
    _price_notes: list[dict[str, str]] = PrivateAttr(default_factory=list)

    @model_validator(mode="after")
    def _consistent(self) -> "Contract":
        groups: dict[str, list[str]] = {
            "cluster": [c.name for c in self.clusters],
            "integration": [i.name for i in self.integrations],
            "check": [c.name for c in self.checks],
        }
        for kind, names in groups.items():
            dupes = sorted({n for n in names if names.count(n) > 1})
            if dupes:
                raise ValueError(f"duplicate {kind} names: {dupes}")
        has_k8s = any(i.name == "kubernetes" for i in self.integrations)
        if has_k8s != bool(self.clusters):
            raise ValueError("the kubernetes integration and clusters go together")
        if self.deploy_checks:
            if not has_k8s:
                raise ValueError("deploy_checks needs the kubernetes integration")
            spaces = {n for c in self.clusters for n in c.namespaces}
            if outside := sorted(set(self.deploy_checks.namespaces) - spaces):
                raise ValueError(f"deploy_checks namespaces not in any cluster: {outside}")
        actuated = {c.name for c in self.clusters if c.actuator_token_secret}
        for i, r in enumerate(self.sizing.rules):
            if r.cluster is not None and r.cluster not in groups["cluster"]:
                raise ValueError(f"sizing rule {i + 1}: no cluster {r.cluster!r}")
            if r.automate and not (actuated if r.cluster is None else actuated & {r.cluster}):
                where = f"cluster {r.cluster!r} has" if r.cluster else "no cluster has"
                raise ValueError(f"sizing rule {i + 1} automates: {where} no actuator_token_secret")
        if self.actuator.eviction and not actuated:
            raise ValueError("actuator: eviction, but no cluster has an actuator_token_secret")
        if (self.actuator.reflexes.oom or self.actuator.reflexes.burst) and not actuated:
            raise ValueError("actuator: reflexes, but no cluster has an actuator_token_secret")
        for i, t in enumerate(self.allocation.teams):
            if t.cluster is not None and t.cluster not in groups["cluster"]:
                raise ValueError(f"team rule {i + 1}: no cluster {t.cluster!r}")
        shared = any(isinstance(i, Prometheus) for i in self.integrations)
        if any(isinstance(i, Sizings) for i in self.integrations) and not any(
            shared or c.prometheus_url for c in self.clusters
        ):
            raise ValueError("the sizing integration needs a cluster with a Prometheus")
        if self.list_prices and self.prices:
            # the Contract's prices already price every cluster: nothing for the list to fill
            raise ValueError("list_prices and prices: a Contract prices by one of them")
        if (
            not self.prices
            and not self.list_prices
            and (priced := [c.name for c in self.clusters if c.prices])
        ):
            # one cluster in money and another by the 4 GiB weighting can't be ranked
            raise ValueError(f"clusters {priced} set prices: set the Contract's prices too")
        if (
            self.list_prices
            and not any(isinstance(i, Prometheus) for i in self.integrations)
            and not any(c.prometheus_url for c in self.clusters)
        ):
            raise ValueError("list_prices reads node labels: it needs a cluster with a Prometheus")
        if (
            self.list_prices
            and self.list_prices.currency != "USD"
            and (other := [c.name for c in self.clusters if c.provider not in ("azure", "gcp")])
        ):
            raise ValueError(
                f"list_prices currency is {self.list_prices.currency}: only Azure's and GCP's "
                f"catalogs offer it, so clusters {other} need provider: azure or gcp"
            )
        if billed := [c for c in self.clusters if c.billing]:
            for c in billed:
                if self.bill_provider(c) is None:
                    raise ValueError(
                        f"clusters {[c.name]} set billing: it reads the aws, azure or gcp "
                        "integration (set provider: when the Contract has several)"
                    )
                gcp = self.bill_provider(c) == "gcp"
                assert c.billing is not None
                if gcp and not c.billing.table:
                    raise ValueError(f"cluster {c.name} reads a GCP bill: set billing.table")
                if not gcp and c.billing.table:
                    raise ValueError(f"cluster {c.name} sets billing.table: only GCP bills use it")
                if gcp and not GCP_LABEL.fullmatch(c.billing.tag):
                    raise ValueError(
                        f"cluster {c.name}: a GCP label is key=value in lowercase letters, "
                        "digits, _ and -, the key starting with a letter"
                    )
            shared = any(isinstance(i, Prometheus) for i in self.integrations)
            if lacking := [c.name for c in billed if not (shared or c.prometheus_url)]:
                raise ValueError(f"clusters {lacking} set billing: they need a Prometheus")
        return self

    def azure_vendor(self) -> "Vendor | None":
        return next(
            (i for i in self.integrations if isinstance(i, Vendor) and i.vendor == "azure"), None
        )

    def gcp_vendor(self) -> "Vendor | None":
        return next(
            (i for i in self.integrations if isinstance(i, Vendor) and i.vendor == "gcp"), None
        )

    def bill_provider(self, cluster: Cluster) -> Literal["aws", "azure", "gcp"] | None:
        """Whose bill a billed cluster reads: its `provider`, else the only cloud
        integration the Contract holds; None when that integration is missing or the
        choice is open (several, no `provider`)."""
        held: list[Literal["aws", "azure", "gcp"]] = []
        if any(isinstance(i, Aws) for i in self.integrations):
            held.append("aws")
        if self.azure_vendor():
            held.append("azure")
        if self.gcp_vendor():
            held.append("gcp")
        want = cluster.provider or (held[0] if len(held) == 1 else None)
        return want if want in held else None

    def price(self, cluster: str) -> ClusterPrices | None:
        """A cluster's unit prices: its own, else the Contract's; None when unpriced."""
        own = next((c.prices for c in self.clusters if c.name == cluster), None)
        return own or self.prices

    @property
    def dir(self) -> Path:
        return self._dir

    @property
    def direct_clusters(self) -> list["Cluster"]:
        """The clusters the Hub reaches itself; a Relay serves the rest (slice 3.1)."""
        return [c for c in self.clusters if c.via == "direct"]

    @property
    def shared_stores(self) -> set[str]:
        """Clusters whose Prometheus another Contract also reads (set by load())."""
        return self._shared_stores

    @property
    def runbook_dirs(self) -> list[Path]:
        """Shared knowledge first, then the Contract's own: its Runbook wins on a name clash."""
        return self._runbook_dirs

    def secret(self, name: str) -> SecretStr:
        return self._secrets[name]

    def has_secret(self, name: str) -> bool:
        return name in self._secrets

    def secret_refs(self) -> set[str]:
        """Every `*_secret` field and every value of a `*_secrets` map, so a new
        integration's credentials are checked without being listed here."""
        refs: set[str] = set()
        for item in (*self.clusters, *self.integrations):
            for field in type(item).model_fields:
                value = getattr(item, field)
                if field.endswith("_secret") and value is not None:
                    refs.add(value)
                elif field.endswith("_secrets"):
                    refs.update(value.values())
        if self.data_policy.mask:
            refs.add(MASK_KEY)
        return refs


class ContractError(Exception):
    """Every problem found, one `path: field: message` line each."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("\n".join(problems))
        self.problems = problems


def load(directory: Path) -> dict[str, Contract]:
    if not directory.is_dir():
        raise ContractError([f"{directory}: not a directory"])
    contracts: dict[str, Contract] = {}
    owners: dict[str, list[str]] = defaultdict(list)  # age recipient -> Contract ids
    problems: list[str] = []
    for path in sorted(directory.glob("*/contract.yaml")):
        try:
            contract, recipients = _load_one(path)
        except ContractError as e:
            problems += e.problems
            continue
        contracts[contract.id] = contract
        for r in recipients:
            owners[r].append(contract.id)
    by_token: dict[str, list[str]] = defaultdict(list)
    for c in contracts.values():
        if c.webhook_token_sha256:
            by_token[c.webhook_token_sha256].append(c.id)
    for ids in by_token.values():
        if len(ids) > 1:
            problems.append(
                f"{directory}: {', '.join(ids)} share a webhook token; "
                "one Contract's Alertmanager could post as another"
            )
    by_channel: dict[str, list[str]] = defaultdict(list)
    for c in contracts.values():
        if c.slack:  # operator and client channels alike: a channel names one Contract
            by_channel[c.slack.operator_channel].append(c.id)
            if c.slack.client_channel:
                by_channel[c.slack.client_channel].append(c.id)
    for channel, ids in by_channel.items():
        if len(ids) > 1:
            problems.append(
                f"{directory}: {', '.join(ids)} share Slack channel {channel}; "
                "one channel per Contract"
            )
    for recipient, ids in owners.items():
        if len(ids) > 1:
            problems.append(
                f"{directory}: {', '.join(ids)} share age recipient {recipient}; "
                "each Contract needs its own key"
            )
    problems += _shared_metrics(directory, contracts.values())
    _mark_shared_stores(contracts.values())
    problems += _client_data_in_shared(directory / SHARED, contracts.values())
    if not contracts and not problems:
        problems.append(f"{directory}: no */contract.yaml found")
    if problems:
        raise ContractError(problems)
    return contracts


def _store(url: str) -> str:
    """One Prometheus, however its URL is written: scheme and host in lower case, the
    default port made explicit, no trailing slash. ponytail: two DNS names or an IP for
    the same store still look different; nothing short of asking it can tell."""
    u = urlsplit(url.strip())
    scheme = u.scheme.lower()
    port = u.port or {"http": 80, "https": 443}.get(scheme)
    return f"{scheme}://{u.hostname or ''}:{port}{u.path.rstrip('/')}"


def _could_match_the_same(a: dict[str, str], b: dict[str, str]) -> bool:
    """Two sets of label matchers select disjoint series only when they give one label
    two different values; {cluster: eu} also matches every {cluster: eu, env: prod} series."""
    return all(a[k] == b[k] for k in a.keys() & b.keys())


def _mark_shared_stores(loaded: Any) -> None:
    """Each Contract's clusters whose Prometheus another Contract reads too, even in
    other namespaces: the agent's free PromQL there could read that Contract's series
    (slice 9.6b), where the Hub's own queries are scoped."""
    readers: dict[str, set[str]] = defaultdict(set)
    for c in loaded:
        shared = next((i.url for i in c.integrations if isinstance(i, Prometheus)), None)
        for cl in c.direct_clusters:
            if url := cl.prometheus_url or shared:
                readers[_store(url)].add(c.id)
    for c in loaded:
        shared = next((i.url for i in c.integrations if isinstance(i, Prometheus)), None)
        c._shared_stores = {
            cl.name
            for cl in c.direct_clusters
            if (url := cl.prometheus_url or shared) and len(readers[_store(url)]) > 1
        }


def _shared_metrics(directory: Path, loaded: Any) -> list[str]:
    """Two clusters, in one Contract or two, reading the same Prometheus with label
    matchers that can match the same series and a namespace in common would get each
    other's usage: Sizings scope by namespace and labels only."""
    seen: list[tuple[str, dict[str, str], set[str], str]] = []
    problems = []
    for c in loaded:
        shared = next((i.url for i in c.integrations if isinstance(i, Prometheus)), None)
        for cl in c.direct_clusters:
            url = cl.prometheus_url or shared
            if not url:
                continue
            here = (_store(url), cl.prometheus_labels, set(cl.namespaces), f"{c.id}/{cl.name}")
            for store, labels, spaces, name in seen:
                if store == here[0] and _could_match_the_same(labels, here[1]) and spaces & here[2]:
                    problems.append(
                        f"{directory}: {name} and {here[3]} read the same Prometheus with "
                        f"labels that can match the same series and share namespaces "
                        f"{sorted(spaces & here[2])}; give one label different values in "
                        "their prometheus_labels (like cluster: <name>)"
                    )
            seen.append(here)
    return problems


def identifiers(c: Contract) -> dict[str, str]:
    """What names this Contract, as term -> what it is: its id and name, kube
    contexts, the hosts of its integration URLs, Jira project, Slack channels, AWS
    key id, vendor settings and the terms it masks. Namespaces and cluster names are
    left out: generic names like `prod` would match everywhere. ponytail: terms under
    4 characters too."""
    found = {c.id: "id", c.name: "name"}
    found |= {cl.context: "kube context" for cl in c.clusters if cl.context}
    if c.data_policy.mask:
        found |= {t: "masked term" for t in c.data_policy.mask.terms}
    if c.slack:
        found[c.slack.operator_channel] = "operator channel"
        if c.slack.client_channel:
            found[c.slack.client_channel] = "client channel"
    for cl in c.clusters:
        if cl.prometheus_url:
            found[_own_host(cl.prometheus_url)] = "Prometheus host"
    for i in c.integrations:
        if isinstance(i, Prometheus | Jira):
            found[_own_host(i.url)] = f"{i.name} host"
        if isinstance(i, Jira):
            found[i.project] = "Jira project"
        if isinstance(i, Aws):
            found[i.access_key_id] = "AWS access key id"
        if isinstance(i, Vendor):
            for var, value in i.env.items():
                found[_own_host(value) if "://" in value else value] = f"{i.name} {var}"
    return {term: what for term, what in found.items() if len(term) >= 4}


def _own_host(url: str) -> str:
    """A URL's host when it can name one client: not `prometheus`, `localhost`, an IP
    or a cluster-local service, which every client's setup can share. Else ""."""
    host = urlsplit(url).hostname or ""
    try:
        ipaddress.ip_address(host)
        return ""
    except ValueError:
        pass
    local = host.endswith((".svc", ".cluster.local", ".local", ".internal"))
    return host if "." in host and not local else ""


def _client_data_in_shared(shared: Path, loaded: Any) -> list[str]:
    """Shared knowledge holds no client data: every Contract reads it. A file that
    names a Contract is refused, by line and by what it names, never the value."""
    terms = [
        (re.compile(rf"(?<![\w.-]){re.escape(term)}(?![\w-]|\.\w)", re.IGNORECASE), c.id, what)
        for c in loaded
        for term, what in identifiers(c).items()
    ]
    problems = []
    for file in sorted(f for f in shared.rglob("*") if f.is_file()) if shared.is_dir() else []:
        lines = file.read_text(errors="replace").splitlines()
        for n, line in enumerate(lines, 1):
            for pattern, contract_id, what in terms:
                if pattern.search(line):
                    problems.append(
                        f"{file}:{n}: names Contract {contract_id!r} (its {what}): Shared "
                        "knowledge is read by every Contract and must hold no client data"
                    )
    return problems


def _load_one(path: Path) -> tuple[Contract, set[str]]:
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise ContractError([f"{path}: invalid YAML: {e}"]) from None
    try:
        contract = Contract.model_validate(raw)
    except ValidationError as e:
        raise ContractError(_lines(str(path), e)) from None
    problems = []
    if contract.id != path.parent.name:
        problems.append(
            f"{path}: id: {contract.id!r} must match its directory {path.parent.name!r}"
        )
    if (path.parent / "secrets.yaml").exists():
        problems.append(
            f"{path.parent / 'secrets.yaml'}: plaintext secrets; encrypt as {SECRETS_FILE}"
        )
    secrets, recipients = {}, set()
    secrets_path = path.parent / SECRETS_FILE
    if secrets_path.exists():
        recipients = _recipients(secrets_path)
        secrets = _decrypt(secrets_path)
    for name in sorted(contract.secret_refs() - secrets.keys()):
        problems.append(f"{path}: secret {name!r} is not in {SECRETS_FILE}")
    for i, cluster in enumerate(contract.clusters):
        if cluster.kubeconfig_secret in secrets:
            kubeconfig = _yaml_or_none(secrets[cluster.kubeconfig_secret])
        elif cluster.kubeconfig_file and cluster.kubeconfig_file.expanduser().is_file():
            kubeconfig = read_kubeconfig(cluster.kubeconfig_file)
        else:
            continue  # the default kubeconfig is the Hub's, checked when it connects
        if kubeconfig is None:
            problems.append(f"{path}: clusters.{i}: kubeconfig is not valid YAML")
        elif refusal := refuse_plugin(cluster, kubeconfig):
            problems.append(f"{path}: clusters.{i}: {refusal}")
    if problems:
        raise ContractError(problems)
    contract._dir = path.parent
    contract._secrets = {k: SecretStr(v) for k, v in secrets.items()}
    contract._runbook_dirs = [path.parent.parent / SHARED / "runbooks", path.parent / "runbooks"]
    return contract, recipients


def _yaml_or_none(text: str) -> Any:
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return None  # never echo the text: it is a credential


def read_kubeconfig(path: Path | None) -> dict[str, Any]:
    """The contexts and users the kubernetes client would see: `path`, or else
    KUBECONFIG / ~/.kube/config, merged first-wins like kubectl."""
    env = os.environ.get("KUBECONFIG", "~/.kube/config")
    paths = [path] if path else [Path(p) for p in env.split(os.pathsep) if p]
    merged: dict[str, Any] = {"contexts": [], "users": []}
    seen = set()
    for p in paths:
        p = p.expanduser()
        if not p.is_file():
            continue
        data = yaml.safe_load(p.read_text()) or {}
        for key in ("contexts", "users"):
            for entry in data.get(key) or []:
                if isinstance(entry, dict) and (key, entry.get("name")) not in seen:
                    seen.add((key, entry.get("name")))
                    merged[key].append(entry)
    return merged


def credential_plugin(kubeconfig: Any, context: str | None) -> str | None:
    """The command `context`'s user would run on the Hub to get credentials, if any:
    an exec plugin, or an auth-provider (gcp's can run a cmd-path)."""
    if not isinstance(kubeconfig, dict):
        return None

    def named(key: str, name: Any) -> dict[str, Any]:
        for entry in kubeconfig.get(key) or []:
            if isinstance(entry, dict) and entry.get("name") == name:
                inner = entry.get(key[:-1])  # contexts[].context, users[].user
                return inner if isinstance(inner, dict) else {}
        return {}

    user = named("users", named("contexts", context).get("user"))
    if "exec" in user:
        return f"exec plugin {(user['exec'] or {}).get('command')!r}"
    if "auth-provider" in user:
        return f"auth-provider {(user['auth-provider'] or {}).get('name')!r}"
    return None


def refuse_plugin(cluster: Cluster, kubeconfig: Any) -> str | None:
    """Why this cluster's kubeconfig may not be used, or None."""
    plugin = None if cluster.allow_exec else credential_plugin(kubeconfig, cluster.context)
    if plugin is None:
        return None
    return (
        f"cluster {cluster.name!r}: its kubeconfig runs {plugin} on the Hub; "
        "set allow_exec: true on the cluster to permit it"
    )


def _lines(where: str, e: ValidationError) -> list[str]:
    # loc and msg only: pydantic's `input` could echo a value.
    return [
        f"{where}: {'.'.join(map(str, err['loc'])) or '<root>'}: {err['msg']}" for err in e.errors()
    ]


def build(
    data: dict[str, Any],
    *,
    secrets: dict[str, SecretStr] | None = None,
    runbook_dirs: Sequence[Path] = (),
) -> Contract:
    """A Contract from data already in memory (tests, evals, the env settings)."""
    contract = Contract.model_validate(data)
    contract._secrets = dict(secrets or {})
    contract._runbook_dirs = list(runbook_dirs)
    return contract


def _recipients(path: Path) -> set[str]:
    """Read from the file's sops metadata: no key needed. Refuses unencrypted files."""
    try:
        meta = (yaml.safe_load(path.read_text()) or {}).get("sops")
    except (yaml.YAMLError, AttributeError):
        meta = None
    if not isinstance(meta, dict):
        raise ContractError([f"{path}: not encrypted with sops; refusing to read it"])
    return {a["recipient"] for a in meta.get("age") or [] if "recipient" in a}


def _decrypt(path: Path) -> dict[str, str]:
    sops = shutil.which("sops")
    if sops is None:
        raise ContractError([f"{path}: sops is not on PATH (brew install sops)"])
    proc = subprocess.run(  # noqa: S603 - fixed binary and arguments, no shell
        [sops, "--decrypt", "--output-type", "json", str(path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        reason = (proc.stderr.strip().splitlines() or ["unknown error"])[0]
        raise ContractError([f"{path}: sops could not decrypt: {reason}"])
    data: Any = json.loads(proc.stdout)
    if not isinstance(data, dict):
        raise ContractError([f"{path}: must be a map of secret name to value"])
    bad = sorted(str(k) for k, v in data.items() if not isinstance(v, str))
    if bad:
        # Name the keys, never the values.
        raise ContractError([f"{path}: {k}: value must be a string" for k in bad])
    return data


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> tuple[str, str]:
    """A fresh webhook token and the hash that goes in contract.yaml."""
    token = secrets.token_urlsafe(32)
    return token, token_hash(token)


# Compared when the Contract is unknown, so a wrong id costs the same as a wrong token.
_NO_MATCH = token_hash(secrets.token_urlsafe(32))


def token_matches(contract: Contract | None, token: str) -> bool:
    expected = (contract and contract.webhook_token_sha256) or _NO_MATCH
    return hmac.compare_digest(token_hash(token), expected)
