# Phase 0: Foundation: many Contracts, durable state

**In short:** Phase 0 made the Hub work for many Contracts at once. Each Contract gets its own config, tools, alert URL and token. Its state moves into Postgres, and the Hub is deployed with Docker Compose.

*Why it matters:* one Contract's data must never reach another's prompts, tools, storage or notifications.

**Status:** done (5 of 5 slices).

| Slice | What | Status |
|---|---|---|
| [0.1](#01-contract-config) | Contract config: schema, loader, SOPS secrets, two demo Contracts | ✅ done |
| [0.2](#02-contract-scoped-tools) | Contract-scoped tools: one MCP server set per Contract, read or write allowlist | ✅ done |
| [0.3](#03-contract-scoped-intake) | Contract-scoped intake: one webhook token per Contract | ✅ done |
| [0.4](#04-postgres-state) | Postgres state: Investigations, actions, dedup, audit log | ✅ done |
| [0.5](#05-hub-deployment) | Hub deployment: Docker Compose, Caddy, backups | ✅ done |

## 0.1 Contract config

**What:** a pydantic schema and a loader for a contracts directory (`src/aiops_agent/contracts.py`). The command `aiops contracts validate <dir>` checks it.

A Contract file holds:

- its id and name;
- its **clusters**: name, namespaces, and a context. The context is an optional kubeconfig file or secret, or `in_cluster`;
- its **integrations**, as a list. `kubernetes` (`enable_actions`, `max_replicas`), `prometheus` (`url`, `token_secret`) and `runbooks` are builtin entries. Phase 4 needs no migration for them.

These fields mirror today's `Settings`.

**Decided:** unknown keys fail. Each later field arrives in the slice that gives it behaviour. Existing files stay valid.

| Field | Arrives in |
|---|---|
| Tool allowlist in YAML | 4.1 (builtins keep their list in code) |
| Notification targets | 1.1 (the client channel: 1.3) |
| Data policy | 6.4 (1.3 needed none: the Operator reviews each Client report) |
| Model and budget | 2.4 |
| Health probe | 2.5 |
| Reach | 3.2 |
| Vendor source | 4.1 |

**Secrets** sit in `secrets.sops.yaml` and are decrypted with the `sops` binary. The checker:

- refuses plaintext files;
- names a bad key without ever printing its value;
- fails when two Contracts share an age recipient. It reads the recipients from the sops metadata, so it needs no key.

Two synthetic demo Contracts live in `demo/contracts/`. Each has its own test-only age key. The `two_contracts` fixture loads them for every later isolation test.

## 0.2 Contract-scoped tools

**What:** each Contract gets its own MCP server set.

- The kubernetes server takes a `cluster` argument, limited to the Contract's clusters. Each cluster has a namespace allowlist.
- Runbooks come from the Contract's directory, plus Shared knowledge.

**Part 1, done: the allowlist decides read or write.**

- Each builtin server declares its list in code (`ACCESS`). `open_agent` requires it.
- A tool not on the list is hidden.
- A server's annotation can only make a tool stricter.
- *Why:* before this, a server that wrongly marked a write tool as read-only skipped approval. A server that leaves the marker out was already safe, because its tools are gated.

**Part 2, done.** `build_mcp(contract, settings)` builds one Contract's tools. The isolation tests live in `tests/test_isolation.py`.

- The kind demo has the `bank` namespace. A kind-backed test shows demo-bank cannot read demo-shop's namespace.
- Until 0.3, the API, `aiops triage` and `aiops mcp` turn the `AIOPS_*` variables into one Contract named `default`.

**Done when:**

- A's agent sees only A's tools. Naming B's cluster errors. Runbook search never returns B's Runbooks.
- A tool annotated read-only but allowlisted as `write` is gated.
- A tool annotated as a write but allowlisted as `read` is still gated.
- A tool not on the list is invisible.

**Checked:** Scenario `two-clusters-alert-picks-one` (2026-10-20) passes on Haiku. A Contract has two clusters, and the alert's `cluster` label must pick one.

## 0.3 Contract-scoped intake

**What:** `POST /contracts/{id}/alertmanager`, with a webhook token per Contract. Deduplication is keyed by (Contract, `groupKey`). This replaces `/alertmanager` and the global token.

**Decided:**

- The token is stored as its SHA-256 in `contract.yaml`. Create one with `aiops contracts new-token`.
- *Why:* this lets the in-cluster deploy run with no sops yet. The image gains sops in 0.5.
- The `AIOPS_*` cluster settings are gone. `serve`, `triage` and `mcp` refuse to start if one is still set.

**Known limit:** `AIOPS_MAX_CONCURRENT_TRIAGES` is shared. One Contract's alert storm can hold every slot. Revisit with per-Contract queues when it bites.

**Done when:**

- A's token cannot post to B.
- The same `groupKey` in A and B starts two Triages.
- A's group resolving does not make B's Proposed actions Stale.

## 0.4 Postgres state

**What:** the tables for Investigations (with a `kind` column), Proposed and Executed actions, the dedup seen-set, and the audit log. The LangGraph Postgres checkpointer is used.

**Decided:**

- Every tool call's args and raw result are stored. This is stored before the cap and after redaction, so slice 2.6 can turn it into a Scenario.
- Stale deadlines are stored in the database. Timers are re-armed at startup instead of sleeping in memory.

**Built:** `Triage` is renamed to `Investigation` in the code, with a `kind`. Part a is done. The API is now `/investigations`, and each record has `kind: triage`.

**Done when:**

- A pending approval survives a Hub restart and can still be approved.
- A deadline that passes while the Hub is down is made Stale at startup.
- Storage tests run against Postgres in CI.

## 0.5 Hub deployment

**What:** `deploy/hub` is a Docker Compose stack. It runs the Hub, Postgres, Caddy and a backup job.

- Caddy serves TLS. It passes only `POST /contracts/<id>/alertmanager`, with bodies up to 1 MB. Everything else is a 404 there.
- The API listens on `127.0.0.1` only. `tailscale serve` sits in front of it.
- *Why:* never bind it to the Tailscale interface. Any tailnet device could then connect directly and forge the identity header.
- The contracts repo reaches the VM through a read-only deploy key.
- `update-contracts.sh` validates every Contract in the image before restarting. If one is invalid, it rolls the clone back.
- The image ships a pinned `sops`, checksum-verified for amd64 and arm64.

**Tests:** `make hub-smoke` runs in CI. CI has no model key, so it cannot produce a Report. It proves instead:

- the stack is healthy;
- both Contracts decrypt in the image;
- only the alert route is public;
- an alert is stored and fails the way it should without a cluster;
- a backup restores;
- a bad update is refused.

**Known limits:** CI does not cover `tailscale serve`, real certificates, the deploy-key clone, or the arm64 image.
