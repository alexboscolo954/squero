"""Vendor MCP servers the Hub may start, by name: a Contract says `vendor: <name>`.

The command lives here, in the Hub's code, never in the contracts repo: whoever can
push there must not be able to run a program next to every Contract's keys (the
reasoning of ADR 0002 for kubeconfig exec plugins). For the same reason a Contract
may set only the variables its vendor's entry lists: `LD_PRELOAD`, `PATH`,
`NODE_OPTIONS` and the like would run code all the same. Each binary ships in the
image, pinned and checksum-verified like sops (versions in the Dockerfile). A
remote vendor (Google's MCP servers) runs nothing here: the Hub calls its fixed
https URLs with a token it mints from the Contract's key. A URL may take a variable
of the Contract's (a vendor's site), only one whose form the registry fixes: a
Contract never names a host its credential goes to (slice 19.6a).

Each vendor's tool schemas are snapshotted into snapshots/<name>.json (slice 4.2):
Scenarios replay against them with no binary, and `squero integrations snapshot
<name> --check` fails when the pinned server's tools drift from the snapshot. A
vendor that lists nothing without its login is snapshotted with its own variables,
from the environment of whoever takes it (19.6a).
"""

import asyncio
import base64
import difflib
import json
import re
import string
import time
from collections.abc import AsyncGenerator, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StdioTransport, StreamableHttpTransport
from fastmcp.server import create_proxy
from fastmcp.tools import Tool, ToolResult
from mcp.types import ToolAnnotations


@dataclass(frozen=True)
class Spec:
    command: tuple[str, ...]  # command[0] an absolute path: PATH can't redirect it; () if remote
    env: frozenset[str]  # the only variables a Contract may set for it
    guide: str = ""  # how to investigate with it, added to the prompt; {name}: the integration
    # Tools whose whole result is a link the Hub may show, built by the vendor from
    # its own base URL, and the guide sentence for each, added when the Contract lists it.
    links: tuple[tuple[str, str], ...] = ()
    # Tools that show what changed (deploys, config writes), and the guide sentence
    # for each, added when the Contract lists it: a guide never names a missing tool.
    changes: tuple[tuple[str, str], ...] = ()
    # Set by the registry on every start, snapshots included, over anything else:
    # telemetry off, which credential to use. A Contract can't set or override them.
    fixed_env: dict[str, str] = field(default_factory=dict)
    # Allowed variables that hold credentials: only from secrets.sops.yaml (env_secrets).
    secret_env: frozenset[str] = frozenset()
    # A remote vendor instead of a command: its https servers, merged into one, and
    # the auth the Hub builds from the Contract's variables for them. A url may hold
    # {VARIABLE}: a required, non-secret one, so its form is checked (url_vars).
    urls: tuple[str, ...] = ()
    auth: Callable[[dict[str, str]], httpx2.Auth] | None = None
    # Variables a Contract must set, and the form each must have (checked at load:
    # a guide or probe built from a bad one fails `squero contracts validate`, not a Run).
    required_env: dict[str, str] = field(default_factory=dict)
    # The health check when the Contract sets none: a tool it must list as read, and
    # its arguments, formatted with the Contract's variables.
    probe: tuple[str, dict[str, str]] | None = None
    # Its servers answer nothing without the login, not even their tool list: a
    # snapshot is taken with the vendor's own variables (login_env).
    login: bool = False

    def __post_init__(self) -> None:
        if bool(self.command) == bool(self.urls):
            raise ValueError("a vendor has a command or remote urls, exactly one")
        if self.command and not Path(self.command[0]).is_absolute():
            raise ValueError(f"vendor command must be an absolute path: {self.command[0]!r}")
        if not all(u.startswith("https://") for u in self.urls):
            raise ValueError(f"remote vendor urls must be https: {self.urls}")
        if overlap := sorted(self.env & set(self.fixed_env)):
            raise ValueError(f"the registry fixes these, a Contract can't set them: {overlap}")
        # A secret never goes into a url, and a host is never a Contract's free text.
        if free := sorted(url_vars(self) - (set(self.required_env) - self.secret_env)):
            raise ValueError(f"a url takes only required, non-secret variables: {free}")
        if self.login and not (self.urls and self.auth):
            raise ValueError("only a remote vendor with an auth lists its tools with a login")


def url_vars(spec: Spec) -> set[str]:
    """The variables a vendor's urls take."""
    fields = (f for u in spec.urls for _, f, _, _ in string.Formatter().parse(u))
    return {f for f in fields if f is not None}


def urls(spec: Spec, env: Mapping[str, str]) -> tuple[str, ...]:
    """A remote vendor's urls for one Contract's variables. Each variable's form is
    checked here too, whatever the caller checked: the credential follows the url."""
    for var in sorted(url_vars(spec)):
        if not re.fullmatch(spec.required_env[var], env.get(var, "")):
            raise ValueError(f"{var}: {env.get(var)!r} doesn't match {spec.required_env[var]}")
    return tuple(u.format(**env) for u in spec.urls)


def login_env(spec: Spec, environ: Mapping[str, str]) -> dict[str, str]:
    """The variables a snapshot of a login vendor is taken with: only the vendor's
    own, out of `environ` (whoever takes it), never another Contract's or the Hub's."""
    env = {k: environ[k] for k in sorted(spec.env) if k in environ}
    if missing := sorted(set(spec.required_env) - set(env)):
        raise ValueError(f"set {', '.join(missing)}: this vendor lists no tools without its login")
    return env


class Bearer(httpx2.Auth):
    """A token the Contract holds, as `Authorization: Bearer`."""

    def __init__(self, token: str) -> None:
        self.token = token

    def auth_flow(self, request: httpx2.Request) -> Any:
        request.headers["Authorization"] = f"Bearer {self.token}"
        yield request


AZURE_TOOLS = (
    "advisor_metadata_get",
    "advisor_metadata_list",
    "advisor_recommendation_list",
    "advisor_recommendation_summary",
    "advisor_remediation_get",
    "aks_cluster_get",
    "aks_nodepool_get",
    "applicationinsights_recommendation_list",
    "appservice_webapp_deployment_get",
    "appservice_webapp_diagnostic_diagnose",
    "appservice_webapp_diagnostic_list",
    "appservice_webapp_get",
    "compute_disk_get",
    "compute_vm_get",
    "compute_vmss_get",
    "containerapps_list",
    "cosmos_list",
    "eventhubs_eventhub_consumergroup_get",
    "eventhubs_eventhub_get",
    "eventhubs_namespace_get",
    "functionapp_get",
    "group_list",
    "group_resource_list",
    "monitor_activitylog_list",
    "monitor_healthmodels_get",
    "monitor_healthmodels_list",
    "monitor_metrics_batchquery",
    "monitor_metrics_definitions",
    "monitor_metrics_query",
    "monitor_resource_log_query",
    "monitor_table_list",
    "monitor_table_type_list",
    "monitor_webtests_get",
    "monitor_workspace_list",
    "monitor_workspace_log_query",
    "monitor_workspace_log_search",
    "mysql_list",
    "mysql_server_config_get",
    "mysql_server_param_get",
    "mysql_table_schema_get",
    "postgres_list",
    "postgres_server_config_get",
    "postgres_server_param_get",
    "postgres_table_schema_get",
    "quota_region_availability_list",
    "quota_usage_check",
    "redis_list",
    "resourcehealth_availability-status_get",
    "resourcehealth_health-events_list",
    "servicebus_queue_details",
    "servicebus_topic_details",
    "servicebus_topic_subscription_details",
    "sql_db_get",
    "sql_elastic-pool_list",
    "sql_server_entra-admin_list",
    "sql_server_firewall-rule_list",
    "sql_server_get",
    "storage_account_get",
    "storage_blob_container_get",
    "storage_blob_get",
    "storage_table_list",
    "subscription_list",
)

# Google's token endpoint, never the key file's own `token_uri`: a key edited in the
# contracts repo must not send a signed assertion elsewhere.
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105
# Read-only scopes: a token can't write even where a role could (cloudtrace.user
# creates trace scopes), nor if Google adds a write tool to these servers.
GOOGLE_SCOPES = " ".join(
    f"https://www.googleapis.com/auth/{s}"
    for s in ("logging.read", "monitoring.read", "trace.readonly")
)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class GoogleKey(httpx2.Auth):
    """Bearer tokens for a service account key (OAuth 2 JWT bearer, RFC 7523), minted
    by the Hub: the key never leaves it. One per integration, so a Run reuses its
    token until a minute before it expires. No assertion or token in any error."""

    def __init__(self, key: str, scopes: str = GOOGLE_SCOPES) -> None:
        self.scopes = scopes  # the MCP servers' read scopes unless the Hub needs another API
        info = json.loads(key)
        self.email, self.key_id = info["client_email"], info["private_key_id"]
        private = serialization.load_pem_private_key(info["private_key"].encode(), None)
        if not isinstance(private, rsa.RSAPrivateKey):
            raise ValueError("a Google service account key is RSA")
        self.private = private
        self.token, self.expires = "", 0.0
        self.lock = asyncio.Lock()  # a Run's three servers start at once: one token

    def assertion(self, now: int) -> str:
        header = {"alg": "RS256", "typ": "JWT", "kid": self.key_id}
        claims = {
            "iss": self.email,
            "scope": self.scopes,
            "aud": GOOGLE_TOKEN_URL,
            "iat": now,
            "exp": now + 600,
        }
        signed = ".".join(_b64(json.dumps(p).encode()) for p in (header, claims))
        signature = self.private.sign(signed.encode(), padding.PKCS1v15(), hashes.SHA256())
        return f"{signed}.{_b64(signature)}"

    async def refresh(self) -> None:
        now = int(time.time())
        grant = {
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": self.assertion(now),
        }
        async with httpx2.AsyncClient(timeout=CALL_SECONDS) as http:
            r = await http.post(GOOGLE_TOKEN_URL, data=grant)
        if r.status_code != 200:  # Google's error names the problem, never the assertion
            error = r.json() if "json" in r.headers.get("content-type", "") else {}
            raise RuntimeError(
                f"Google token for {self.email}: HTTP {r.status_code} "
                f"{error.get('error', '')} {error.get('error_description', '')}".rstrip()
            )
        body = r.json()
        self.token, self.expires = body["access_token"], now + float(body["expires_in"])

    async def async_auth_flow(
        self, request: httpx2.Request
    ) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        async with self.lock:
            if time.time() > self.expires - 60:
                await self.refresh()
        request.headers["Authorization"] = f"Bearer {self.token}"
        yield request


GCP_GUIDE = """\
Google Cloud ({name}_ tools), project {GOOGLE_CLOUD_PROJECT}: name it in every call,
as "projects/{GOOGLE_CLOUD_PROJECT}" (resourceNames, parent, name) or
"{GOOGLE_CLOUD_PROJECT}" (projectId). Logs: {name}_list_log_entries with a filter
that bounds the time and narrows the resource, such as timestamp>="<start>" AND
severity>=ERROR AND resource.type="cloud_run_revision", orderBy "timestamp desc" and
a small pageSize. Metrics: {name}_query_range with location "global" (it fails with
no reason without it) and PromQL, where a metric name's dots and slash become
underscores and a colon, and a Cloud Run one needs its resource:
run_googleapis_com:request_count{{monitored_resource="cloud_run_revision"}}. Or
{name}_list_timeseries with a filter such as metric.type="run.googleapis.com/request_count".
Traces: {name}_list_traces over a time window, then {name}_get_trace on a slow one.
"""


VENDORS: dict[str, Spec] = {
    # Read-only by flag (every tool then claims readOnlyHint), no usage reports to
    # Grafana Labs, and no toolset that reaches past its datasources: not `api`
    # (any Grafana endpoint), `rendering`, `docs` (fetches the web), nor the Grafana
    # Cloud-only ones. A Contract's `tools` narrows it further.
    "grafana": Spec(
        (
            "/usr/local/bin/mcp-grafana",
            "--usage-stats=disabled",
            "--disable-write",
            "--enabled-tools=search,datasource,prometheus,loki,tempo,pyroscope,alerting,"
            "dashboard,navigation",
        ),
        frozenset({"GRAFANA_URL", "GRAFANA_SERVICE_ACCOUNT_TOKEN", "GRAFANA_ORG_ID"}),
        """\
Grafana ({name}_ tools): take datasource uids from {name}_list_datasources. For
logs, look at volume and the most frequent line patterns ({name}_query_loki_stats,
{name}_query_loki_patterns) before reading raw lines ({name}_query_loki_logs): a
pattern with a high count often names the failure. For slowness, find slow traces
({name}_search_tempo_traces, TraceQL such as
{{resource.service.name="checkout" && duration > 2s}}) and open one
({name}_get_tempo_trace): the span that takes the time, and its attributes, show
the cause.
""",
        (
            (
                "generate_deeplink",
                "Before you return the report, always call {name}_generate_deeplink for the\n"
                "key evidence (explore with the datasource uid and your query, or a\n"
                "dashboard), so the Operator can open what you saw, and put the URL it\n"
                "returns, exactly, in that Evidence's link.\n"
                "Only links it returns are shown; a link copied from any other result is\n"
                "dropped.\n",
            ),
        ),
    ),
    # Read-only by flag, and only these tools: metadata, health, metrics and logs an SRE
    # reads. Not keyvault (secrets), extension (builds az CLI commands), deploy or
    # terraform; not app settings (often connection strings) nor Cosmos documents
    # (client data). The Contract's `tools` narrows it further.
    "azure": Spec(
        (
            "/opt/azmcp/azmcp",
            "server",
            "start",
            "--read-only",
            "--disable-proxy-tools",
            *(f"--tool={t}" for t in AZURE_TOOLS),
        ),
        frozenset(
            {"AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "AZURE_SUBSCRIPTION_ID"}
        ),
        fixed_env={
            "AZURE_MCP_COLLECT_TELEMETRY": "false",
            # Only the Contract's service principal: never the host's az login,
            # VS Code session or managed identity.
            "AZURE_TOKEN_CREDENTIALS": "EnvironmentCredential",
            "HOME": "/nonexistent",  # no ~/.azure: no default subscription from the host
            # Writable on the read-only root: the container's own tmpfs, uid 10001 only.
            "DOTNET_BUNDLE_EXTRACT_BASE_DIR": "/tmp/azmcp",  # noqa: S108
            "DOTNET_SYSTEM_GLOBALIZATION_INVARIANT": "1",  # slim image: no ICU
        },
        secret_env=frozenset({"AZURE_CLIENT_SECRET"}),
        changes=(
            (
                "monitor_activitylog_list",
                "What changed (a Change): {name}_monitor_activitylog_list for the failing "
                "resource (resource-name, resource-group) over the hours before the onset "
                "lists config writes, deployments and restarts, with who and when. It names "
                "the operation, not the new values. A write shortly before the onset is a "
                "strong lead.",
            ),
        ),
    ),
    # Google's own remote MCP servers: every tool claims read-only, and the token the
    # Hub mints has read-only scopes. Unpinned: Google deploys them, so drift is a
    # warning (hub-smoke), and only a replay sees an old schema. Error Reporting has
    # no remote server.
    "gcp": Spec(
        (),
        frozenset({"GOOGLE_CLOUD_PROJECT", "GOOGLE_SERVICE_ACCOUNT_KEY"}),
        GCP_GUIDE,
        secret_env=frozenset({"GOOGLE_SERVICE_ACCOUNT_KEY"}),
        urls=tuple(
            f"https://{s}.googleapis.com/mcp" for s in ("logging", "monitoring", "cloudtrace")
        ),
        auth=lambda env: GoogleKey(env["GOOGLE_SERVICE_ACCOUNT_KEY"]),
        required_env={
            "GOOGLE_CLOUD_PROJECT": r"[a-z][a-z0-9-]{4,28}[a-z0-9]",
            "GOOGLE_SERVICE_ACCOUNT_KEY": r".+",
        },
        probe=("list_log_names", {"parent": "projects/{GOOGLE_CLOUD_PROJECT}"}),
        changes=(
            (
                "list_log_entries",
                "What changed (a Change): the audit log, {name}_list_log_entries with "
                'logName="projects/{GOOGLE_CLOUD_PROJECT}/logs/cloudaudit.googleapis.com'
                '%2Factivity" and a time bound before the onset, lists deploys and config '
                "changes (protoPayload.methodName, resourceName), with who and when. A "
                "change shortly before the onset is a strong lead.",
            ),
        ),
    ),
}
START_SECONDS = 30  # a vendor that never finishes MCP's handshake fails the Run
CALL_SECONDS = 60  # each vendor tool call
SNAPSHOTS = Path(__file__).parent / "snapshots"
# What the model and the approval gate see of a tool; _meta and icons are noise.
SNAPSHOT_FIELDS = ("name", "title", "description", "inputSchema", "outputSchema", "annotations")


def client(spec: Spec, env: dict[str, str]) -> Client[StdioTransport]:
    """The vendor as a subprocess with exactly `env` on top of MCP's safe defaults
    (PATH, HOME, ...). keep_alive off: one process per use, none outlives a Run."""
    env = {**env, **spec.fixed_env}  # the registry's last: nothing overrides it
    transport = StdioTransport(spec.command[0], list(spec.command[1:]), env=env, keep_alive=False)
    return Client(transport, timeout=CALL_SECONDS, init_timeout=START_SECONDS)


class Reasons(httpx2.AsyncBaseTransport):
    """Google answers a refused tool call (no permission, billing off) with HTTP 4xx
    around a JSON-RPC result that says why. Passed on as 200, the client reads that
    reason instead of a bare "Server returned an error response"."""

    def __init__(self, inner: httpx2.AsyncBaseTransport | None = None) -> None:
        self.inner = inner or httpx2.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        response = await self.inner.handle_async_request(request)
        if not (400 <= response.status_code < 500) or "json" not in response.headers.get(
            "content-type", ""
        ):
            return response
        body = await response.aread()
        try:
            status = 200 if "result" in json.loads(body) else response.status_code
        except (ValueError, TypeError):
            status = response.status_code
        drop = {"content-encoding", "content-length"}  # aread decoded it
        headers = {k: v for k, v in response.headers.items() if k.lower() not in drop}
        return httpx2.Response(status, headers=headers, content=body, request=request)

    async def aclose(self) -> None:
        await self.inner.aclose()


def http_client(
    headers: dict[str, str] | None = None,
    timeout: httpx2.Timeout | None = None,
    auth: httpx2.Auth | None = None,
    **settings: Any,  # whatever else the MCP client asks for (follow_redirects)
) -> httpx2.AsyncClient:
    """The MCP client's httpx client, through Reasons."""
    return httpx2.AsyncClient(
        headers=headers,
        timeout=timeout or httpx2.Timeout(CALL_SECONDS),
        auth=auth,
        transport=Reasons(),
        **settings,
    )


def remote(url: str, auth: httpx2.Auth | None = None) -> Client[StreamableHttpTransport]:
    transport = StreamableHttpTransport(url, auth=auth, httpx_client_factory=http_client)
    return Client(transport, timeout=CALL_SECONDS, init_timeout=START_SECONDS)


def server(spec: Spec, env: dict[str, str], name: str) -> FastMCP:
    """A proxy to the vendor: its subprocess, or its remote servers merged into one,
    all sharing one auth (one token per Run)."""
    if spec.command:
        return create_proxy(client(spec, env), name=name)
    assert spec.auth
    auth, merged = spec.auth(env), FastMCP(name)
    for url in urls(spec, env):
        merged.mount(create_proxy(remote(url, auth)))
    return merged


async def capture(spec: Spec, environ: Mapping[str, str] | None = None) -> str:
    """The vendor's tool schemas as stable JSON. Started with no Contract and no
    variables at all, so no credential can end up in a file this public repo commits.
    A remote vendor lists its tools without auth; one that needs its login to list
    them gets its own variables out of `environ`, and a snapshot that holds one of
    its secrets is refused."""
    env = login_env(spec, environ or {}) if spec.login else {}
    if spec.command:
        clients: list[Client[Any]] = [client(spec, {})]
    else:
        auth = spec.auth(env) if spec.login and spec.auth else None
        clients = [remote(u, auth) for u in urls(spec, env)]
    tools = []
    for c in clients:
        async with c:
            tools += await c.list_tools()
    names = [t.name for t in tools]
    if clash := sorted({n for n in names if names.count(n) > 1}):
        raise ValueError(f"two of the vendor's servers offer {clash}: which one answers?")
    dumped = [t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in tools]
    kept = [{k: t[k] for k in SNAPSHOT_FIELDS if k in t} for t in dumped]
    text = json.dumps(sorted(kept, key=lambda t: t["name"]), indent=2, sort_keys=True) + "\n"
    if held := sorted(v for v in spec.secret_env if env.get(v) and env[v] in text):
        raise ValueError(f"the vendor's tool list holds {', '.join(held)}: not written")
    return text


async def drift(name: str, environ: Mapping[str, str] | None = None) -> str:
    """A unified diff from the saved snapshot to the running vendor; empty if none."""
    path = SNAPSHOTS / f"{name}.json"
    saved = path.read_text() if path.exists() else ""
    live = await capture(VENDORS[name], environ)
    diff = difflib.unified_diff(
        saved.splitlines(keepends=True), live.splitlines(keepends=True), str(path), "live"
    )
    return "".join(diff)


class Snapshotted(Tool):
    """A vendor tool rebuilt from its snapshot: same name, schemas and annotations
    as the real one, and no implementation. The replay answers every call first."""

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        raise RuntimeError(f"{self.name} is a snapshot: only a replay can answer it")


def snapshot_server(vendor: str, name: str) -> FastMCP:
    """The vendor's tools from snapshots/<vendor>.json, for replaying a Scenario."""
    path = SNAPSHOTS / f"{vendor}.json"
    if not path.exists():
        raise ValueError(
            f"no schema snapshot for vendor {vendor!r}: run `squero integrations snapshot {vendor}`"
        )
    server = FastMCP(name)
    for t in json.loads(path.read_text()):
        annotations = t.get("annotations")
        server.add_tool(
            Snapshotted(
                name=t["name"],
                title=t.get("title"),
                description=t.get("description"),
                parameters=t["inputSchema"],
                output_schema=t.get("outputSchema"),
                annotations=ToolAnnotations(**annotations) if annotations else None,
            )
        )
    return server
