# Phase 5: Clouds, read-only

**In short:** Phase 5 gave the agent read-only access to Azure, AWS and GCP. Each cloud has a documented read-only role, an allowlist, a schema snapshot, and one Scenario recorded from a sandbox account.

*Why it matters:* the agent can read the cloud side of a problem. Every cloud role is read-only, and every tool is on an allowlist.

**Status:** done (3 slices). The plan was "each through its official MCP server". Azure and GCP use their vendors' servers. AWS is a builtin (see 5.2a).

| Slice | What | Status |
|---|---|---|
| [5.1](#51-azure) | Azure, through `azmcp` | ✅ done |
| [5.2](#52-aws) | AWS, a builtin server | ✅ done |
| [5.3](#53-gcp) | GCP, through Google's remote MCP servers | ✅ done |

## What every cloud gets

- A documented read-only role: Reader, ReadOnlyAccess, or Viewer.
- An allowlist of read tools.
- A schema snapshot.
- One Scenario, recorded from a sandbox account. Examples: a node pool at its max, database storage full, a quota hit.

*Note:* recording needs a sandbox account per cloud. This costs a little money.

## 5.1 Azure

**Part a, done:** registry entry, pinned binary, snapshot, and CI check.

**Part b, done:** a Scenario recorded from a sandbox subscription.

### 5.1a: the registry entry

- `azmcp` 3.0.0 comes from npm's `@azure/mcp-linux-<arch>` package. Its sha512 is verified. It sits at `/opt/azmcp/azmcp`.
- It runs with `--read-only --disable-proxy-tools`.
- It uses an explicit `--tool` list of 62 tools. `--namespace` was not used, because it cannot drop single tools.
- *Why:* the read-only namespaces offered app settings and Cosmos DB documents.
- A registry entry can now fix variables for every run and snapshot (`fixed_env`).
- It can also mark credentials as `env_secrets`-only (`secret_env`).

**Azure's fixed variables:**

| Variable | Setting |
|---|---|
| Telemetry | off |
| `AZURE_TOKEN_CREDENTIALS=EnvironmentCredential` | Only the Contract's service principal is used |
| `HOME=/nonexistent` | No host `~/.azure` is read |
| .NET bundle | extracted to `/tmp`, because the Hub's root is read-only |
| Globalization | invariant, because the slim image has no ICU |

The Contract sets only the tenant, client and subscription ids. The secret comes through `env_secrets`.

**Tests:** `hub-smoke` checks both vendors' snapshots inside the image, read-only.

**Checked in the image (read-only root, tmpfs `/tmp`):**

- A real tool call reaches Entra with the Contract's service principal only.
- The process starts in about 1 s. A call takes 2.6 to 4.7 s, including the sign-in.
- The unpacked bundle is 6.7 MB of tmpfs.
- Two cold starts at once do not clash.
- One process per call is fine for now. *Upgrade path:* keep one process per Run, if Azure Runs feel slow.

**Why 3.0.0:** it was 5 days old when chosen. The 2.x line is the previous major version. Its snapshot would churn at the next upgrade.

### 5.1b: the Scenario

- `evals/record_azure.py` runs in the Hub image. It uses the pinned binary and `fixed_env`, as in production. It uses a Reader service principal.
- It records `azure-appservice-bad-startup`. This is a working Linux web app whose startup command was changed to one that fails.
- Each tool keeps one result, matched on the resource name only.
- The free F1 tier was dropped. A crash-looping container used up its daily CPU quota within minutes. The app then went to `QuotaExceeded`, a second cause.
- Basic B1 for under an hour costs cents of trial credit.
- A new subscription needs `Microsoft.Web`, `Microsoft.Insights` and `Microsoft.ResourceHealth` registered.
- Reader cannot read deployments. `appservice_webapp_deployment_get` returns Unauthorized.
- The diagnostics detectors returned empty tables. So neither is offered.
- The scrub replaces ids and names with fixed fakes. `AIOPS_SCRUB` covers the caller's identity. `claims` and `httpRequest` are dropped. Nothing is written if a real value survives. A test covers the scrub.
- The Scenario passes on Haiku in 4 calls.

## 5.2 AWS

**Part a, done:** a builtin server, with a live check in the image.

**Part b, done:** a Scenario recorded from the sandbox account.

### 5.2a: a builtin, not AWS's official servers

This was the Operator's choice. It goes against this phase's plan for "official MCP servers".

- awslabs' `cloudwatch-mcp-server` 0.3.1 annotates none of its 19 tools. So every call would need approval.
- It installs 249 MB (scipy, statsmodels, pandas).
- The remote AWS MCP Server (GA 2026-05) offers no `call_aws`, even when signed in.
- Its data path is `run_script`, which claims destructive.

**The builtin:**

- It signs with SigV4, from the standard library. The signatures were checked against botocore's.
- It speaks the JSON protocols of CloudWatch (1.0), Logs and CloudTrail (1.1).
- It has six read tools.
- It reads only the Contract's regions. The first region is the default.
- Errors show AWS's reason. They never show the signature.
- The Contract names an IAM user's key (`AKIA...`). A temporary `ASIA...` key would expire.
- The health check reads alarms.
- The image is unchanged.

### 5.2b: the Scenario

- `evals/record_aws.py` records `aws-lambda-timeout-cut` in the Hub image.
- It is a Lambda function with inline code. Its timeout was cut below its run time. It also has an Errors alarm.
- The sandbox is on AWS's Free plan. Its AWS-managed organization denies Lambda and alarm writes outside us-east-2. So the recording ran there. The recording names us-east-1, as a replayed aws Contract does.
- Only always-free usage was used. Everything was deleted after the recording.
- The scrub masks every access key id. CloudTrail names session keys, and an id encodes its account.
- The Scenario passes on Haiku in 5 calls.

## 5.3 GCP

**Part a, done:** registry entry, snapshot, and a live check in the image.

**Part b, done:** a Scenario recorded from the sandbox project.

### 5.3a: Google's remote MCP servers

- The integration uses Google's remote MCP servers: Logging, Monitoring and Trace.
- It does not use the local `@google-cloud/observability-mcp`.
- *Why not the local one:* it loads `googleapis` (194 MB) at runtime. With Node, that would add about 350 MB to the image. None of its tools claims read-only, so every call would need approval.
- The remote servers' 17 tools all claim read-only.
- `tools/list` needs no auth. So the snapshot and its check work as they do for the pinned vendors.

**Built:**

- A registry entry is now either a command or `urls`. Several URLs merge into one server, with one auth.
- `GoogleKey` signs a JWT with the key (RFC 7523). It uses `cryptography`, now a direct dependency. It trades the JWT at the pinned token URL for a read-only-scoped token.
- The token is reused until a minute before it expires.
- Narrow scopes were tested against all three servers.
- A registry entry can require variables with a form (`required_env`). The project id is checked at load, because it goes into the guide.
- A registry entry can name a default health probe. It is formatted with the Contract's variables.
- Scenarios now carry those required plain variables. So a gcp Scenario's guide names its project.

**Found:**

- A docs summary listed `roles/monitoring.admin` for Monitoring's server. Viewer roles plus `roles/mcp.toolUser` read fine.
- Google answers a refused call (no permission, billing off) with an HTTP 4xx. The 4xx comes around a JSON-RPC result that says why.
- The Hub's transport passes that on as 200. So the health page and the model both see the reason.
- `list_timeseries` and `query_range` need billing on the project. Logs and traces do not.
- The image grew by 1 MB.

### 5.3b: the Scenario

- `evals/record_gcp.py` records `gcp-cloudrun-bad-timeout` in the Hub image.
- It is a Cloud Run service. Its new revision set a 1 ms upstream timeout.
- It was deployed from Docker Hub's `python:3.12-slim`, with the server inline in its args. So there was no build and no registry.
- Billing was linked only while recording, with a $1 budget alert.
- It cost nothing, because of Cloud Run's free tier. The service was deleted, and billing unlinked, afterwards.
- The two recorders share `evals/scrub.py`.
- Request logs keep their status. They drop only IPs and user agents.
- Google's `query_range` fails with an empty message unless `location` is "global". Cloud Run's PromQL needs a `monitored_resource` matcher. The guide says both.
- The Scenario passes on Haiku in 2 calls.
