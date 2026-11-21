# Phase 4: Integration framework and first integrations

**In short:** Phase 4 built the framework for vendor integrations. A vendor's tool server runs as a pinned program, and its tool schemas are saved as a snapshot. The first integrations were Grafana and Jira.

*Why it matters:* a Contract names a vendor, never a command. The command comes from the Hub's registry, so it never comes from the contracts repo.

**Status:** done (4 slices).

| Slice | What | Status |
|---|---|---|
| [4.1](#41-vendor-sources) | Vendor sources: pinned subprocesses and the registry | ✅ done |
| [4.2](#42-schema-snapshots) | Schema snapshots, checked in CI | ✅ done |
| [4.3](#43-grafana-stack) | Grafana stack through `mcp-grafana`, parts a to d | ✅ done |
| [4.4](#44-jira) | Jira: search (part a) and the "Create Jira issue" button (part b) | ✅ done |

## 4.1 Vendor sources

**What:**

- Vendor servers run as pinned stdio subprocesses, or as remote HTTP servers.
- They sit behind the same allowlist enforcement as slice 0.2.

**Done when:** the slice 0.2 allowlist tests also pass against a vendor subprocess.

**Decided:**

- A Contract integration has this shape: `{name, vendor, env, env_secrets, tools, probe}`.
- `vendor` names an entry in the Hub's registry (`servers/vendors.py`). So the command never comes from the contracts repo. This follows the reasoning in [ADR 0002](../adr/0002-hub-and-relay.md) for kubeconfig exec plugins.
- Each binary ships in the image, pinned.
- `tools` is the Hub's read and write list for the vendor. It is the YAML allowlist that was planned for 4.1.
- The 0.2 allowlist tests run against an in-process server, and against a real vendor subprocess.

**Process rules:**

- The subprocess gets MCP's safe default environment, plus its own `env` and secrets. It never gets the Hub's environment. This is tested with the demo Contracts.
- A Contract may set only the variables its vendor's registry entry lists.
- *Why:* variables such as `LD_PRELOAD`, `PATH` and `NODE_OPTIONS` would run code.
- Registry commands are absolute paths.
- One process runs per tool call. None outlives a Run.
- A handshake timeout drops a vendor that never answers. The Run goes on.

**Health:** a `probe` tool (it must be listed as `read`). If there is no probe, the vendor starts and offers every listed tool.

**Deviations:**

- stdio only. Remote HTTP waits for 4.4, where the Jira choice decides whether it is needed.
- Unannotated vendor tools stay gated. Grafana annotates its reads.
- Replaying a Scenario that uses a vendor is refused until the snapshots of 4.2 exist.

## 4.2 Schema snapshots

**What:** `aiops integrations snapshot <name>` saves a pinned vendor server's tool schemas into this repo. The snapshot holds schemas only, no data. Replay builds the tools from the snapshot.

**Done when:** a test diffs the snapshot against the real pinned server. So a vendor that renames a tool fails a test, instead of silently breaking an eval.

**As built:**

- The snapshot is `src/aiops_agent/servers/snapshots/<vendor>.json`.
- It keeps each tool's name, title, description, input and output schemas, and annotations. These are what the model and the approval gate see.
- The file is sorted, so a diff reads well.
- *Why no config in the snapshot:* a vendor starts with no Contract and no variables at all. A vendor that writes its config into a description would put it into a file this public repo commits.
- `--check` prints the diff and fails.
- A saved Scenario lists a vendor as `{name, vendor, tools}`. It never lists its env, secrets or probe.
- Replay offers the vendor's tools from the snapshot, without starting it.

**Deviations:**

- The registry was still empty. So the diff test ran against the fixture vendor (`tests/fixtures/snapshots/fixture.json`). CI checks it against the live fixture on every run.
- For a real vendor, the check is `aiops integrations snapshot <name> --check`. It runs where that vendor's pinned binary is (4.3).
- A vendor saved by name only, in a draft from before 4.2, still refuses to replay.
- A Scenario from a Contract with no clusters now replays with none. Before, it got a made-up `kind` cluster.

## 4.3 Grafana stack

**What:** the Grafana integration, through `mcp-grafana`. Its parts are a, b, c and d.

**Scope:**

- Covers Prometheus and Mimir, and Loki.
- For logs, stats and patterns come before raw logs. This is an IncidentFox idea.
- Also covers Tempo traces, Pyroscope, and dashboard deep links in Reports.
- Grafana Alerting becomes a second alert source. It is normalized into an Alert group.
- The kind demo gains a Grafana LGTM stack.

### Part a: registry, snapshot and CI check

- A registry entry, plus `snapshots/grafana.json`.
- `hub-smoke` runs `aiops integrations snapshot grafana --check` inside the image. That is where the pinned binary is. So a renamed Grafana tool fails CI.

**Checked (v2.0.2):**

- It lists all its tools with no variables.
- Toolsets are command flags.
- `--disable-write` leaves only the tools that claim read-only.
- Tempo is a static toolset (`tempo`).
- It reports usage statistics to Grafana Labs by default. So the registry passes `--usage-stats=disabled`.

**Why the toolset goes in the command:** a snapshot is taken with no variables. So mcp-grafana must list its tools without `GRAFANA_URL`. The toolset choice must therefore live in the registry's command args, not in the env. Otherwise the snapshot would not match what a Contract's Run offers.

### Part b: the kind demo and two Scenarios

- `make lgtm` runs `grafana/otel-lgtm` in namespace `observability`. It is not in CI, because replay needs no Grafana.
  - Loki's pattern ingester is on.
  - Chunks are flushed within minutes, so log stats see them.
  - There is no WAL. Nothing persists. A nearly full disk made the WAL throttle every write.
- `demo/telemetry.py` uses only the standard library (OTLP/HTTP JSON). It runs as `payments` and `storefront`, in namespace `shop`. It is kept apart from `demo`, so the Kubernetes Scenarios' recordings do not change.
- `evals/record_grafana.py` records through the Hub's real path (`build_mcp`). It keeps one result per tool, matched on its datasource. A trace is also matched on its id.
- A vendor's registry entry may carry a prompt guide. The guide is added only when the vendor is offered.
- Both Scenarios pass on Haiku. The model calls patterns and traces first.

**Checked first:** the snapshot was taken with Grafana unreachable. mcp-grafana reads `/api/frontend/settings` at startup. The snapshot was then captured again with the registry flags, and with `GRAFANA_URL` set to the demo Grafana. The result was identical to `grafana.json`. So the snapshot taken with no Grafana holds.

**Deviations in part b:**

- Deep links moved to part c. They need an `Evidence` field, and a check that a link came from a tool result. That would change every Scenario's output.
- Grafana Alerting moved to part d.
- The Scenarios were recorded by a script, not from a live Investigation. `build_scenarios.py` records the Kubernetes ones the same way.

### Part c: deep links in Reports

- `Evidence.link` is shown after the finding, in Slack and the console. It never appears in the Client report.
- A registry entry names its link tools. For Grafana, that is `generate_deeplink`. Its result is only a URL built from `GRAFANA_URL`.
- A middleware records what the link tools return, and where they ran.
- The Hub drops any Evidence link that no link tool returned exactly.
- *Why:* a URL in a log line, a span attribute, or a trace result's `grafanaUrl` is client data or another tool's. So it never shows.
- Scenarios can expect `links: N`. Results count the dropped links.
- On Haiku, the slow-trace Scenario first cited a trace result's link, and failed. The guide now says other links are dropped. Both Grafana Scenarios then passed with a kept link.

### Part d: Grafana Alerting as an alert source

- `POST /contracts/<id>/grafana` is public through Caddy. The Contract's webhook token is sent as Grafana's Bearer credentials.
- Grafana's webhook is Alertmanager's payload, plus each rule's query values (`valueString`). The values are kept as the `query_values` annotation. The rendered `title` and `message` are dropped.
- Fingerprints and resolved notifications are Alertmanager's. So deduplication and stale-on-resolve need nothing new.
- Group keys get a `grafana:` prefix. *Why:* an Alertmanager grouping by the same labels sends the same key. One source resolving must not stale the other's actions.
- Firing and resolved payloads were captured from Grafana 13.2 on the kind demo. They are the test fixtures. `hub-smoke` posts one through Caddy.
- Not handled specially: Grafana's DatasourceNoData and DatasourceError alerts arrive as ordinary alerts, under those names. These were not captured yet.

**Done when:** two new recorded Scenarios pass, one with the root cause in log patterns and one in traces.

## 4.4 Jira

**What:** part a is search, as Evidence. Part b is the "Create Jira issue" button.

### Part a: search as Evidence

- Integration shape: `{name: jira, url (https), email, token_secret, project}`.
- Tools: `jira_search_issues` and `jira_get_issue`. Both are read-only.
- Built on REST v3 (`POST /rest/api/3/search/jql`).
- The server writes the JQL: `project = "<KEY>" AND text ~ "<words>"`.
- The model passes words only. Non-word characters become spaces.
- `get_issue` checks the project of the issue that comes back.
- A prompt guide is added only when the Contract has Jira.
- Health searches one day. Save as Scenario and replay handle it.

**Tests:** against synthetic responses in the documented shapes. There is no live Jira yet.

**Decided (the Operator, 2026-10-17):** a small builtin server with an API token, not Atlassian's remote MCP.

- Any Jira Cloud user can make a token. There is no client admin step.
- The remote MCP's API-token access must be enabled by each client's org admin.
- The token fits the Contract's static SOPS secrets.
- It needs no remote-HTTP vendor transport.

### Part b: the "Create Jira issue" button

- A finished Report gets the button on its Slack parent, and on its console page, when the Contract has Jira.
- `file_jira` is the one path for both surfaces. It does this in order:
  1. claims the Report (`jira_at`, migration 0009);
  2. creates the issue with the Contract's own Basic auth, on the request alone (the HTTP client is shared with Slack);
  3. records the key;
  4. updates the Slack thread.
- A refusal frees the claim. A refusal is a 4xx (Jira's `errors` are shown), or a connection that never opened.
- A timeout, a 5xx, or a 201 without a key of the project keeps the claim. Jira may have created the issue. Both surfaces say to check the project.
- The only reset is clearing `jira_at` in the database.
- Empty sections are left out, because ADF refuses an empty list or text.
- The documents were checked once against Atlassian's published ADF schema.

**Decided (the Operator, 2026-10-17):**

- The issue holds the full Report, Evidence and executed actions. All are redacted.
- It is built as ADF text nodes. So markup from tool output stays inert.
- Only kept Evidence links become links.
- The summary is one line of at most 255 characters.
- `issue_type` (default Task) is set per Contract.
- Creating an issue is never an agent tool.

**Tests:** against a fake Jira. There is no live Jira yet.
