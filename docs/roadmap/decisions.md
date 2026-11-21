# Decisions behind the roadmap

**In short:** the goal of the project, the starting point, and the decisions that shaped the whole roadmap. Each decision is a row in a table, with its date where one was made later. Some entries use the old name `aiops`, from before the rename to squero on 2026-11-10. They are kept as they were written.

## The goal

The goal is one Hub that lets the Operator run SRE for several Contracts at once. It answers alerts, answers questions and runs checks, and it never lets one Contract's data reach another.

From Phase 9 it also optimizes each Contract's resources: Sizings for every Workload, cost, horizontal scaling, nodes, spot and GPUs, applied under the Operator's Automation.

**Inspirations:** HolmesGPT, OpenSRE and IncidentFox inspire the agent. ScaleOps (first) and Kubex inspire the optimizer. We take their ideas, not their code.

Terms are defined in [`CONTEXT.md`](../../CONTEXT.md). The decisions behind the shape are in [ADR 0002](../adr/0002-hub-and-relay.md) (Hub and Relay), [ADR 0003](../adr/0003-contract-data-in-private-git.md) (Contract data in a private repo) and [ADR 0004](../adr/0004-standing-approval-for-sizing.md) (an in-cluster Actuator applies Sizings under a standing approval).

## Where we start

| Today | Needed |
|---|---|
| `Settings` is global: one kube context, one namespace list, one Runbooks dir, one Slack webhook, one pair of tokens | Everything except Hub settings is set per Contract |
| `triages` and `recent` live in memory. Dedup is keyed by `groupKey` alone | Postgres, keyed by Contract. Two Contracts can send the same `groupKey` |
| `InMemorySaver` checkpointer. A restart loses pending approvals | Postgres checkpointer. Pending approvals survive a restart |
| `read_only(tool)` trusted the server's `readOnlyHint` | Done in 0.2: the Hub's own list decides read or write, and a server can only make a tool stricter |
| `replay_server` builds tools from our in-process servers | Replay also builds them from saved schema snapshots of vendor servers |
| Alerts only, Slack webhook only, API for approvals | Triage, Question and Check, with Slack and a web console |

## Decisions

| Topic | Decision |
|---|---|
| Who works | One Operator. The Operator receives every Report and makes every Approval decision |
| Reaching a Contract | **Direct** (the Hub holds the credentials) or **Relay** (dials out from the client network) |
| Hub | Any machine you own, a VM or a laptop: docker compose with the Hub and Postgres |
| Operator interfaces | Your own Slack workspace, one channel per Contract, with approve buttons. Also a web console |
| Client delivery | Optional per Contract. A Client report goes when the Operator clicks Send, never with approval controls |
| Data policy | Set per Contract. Default: no restrictions |
| Integrations | Hybrid: vendor MCP servers where they're good, our own where they're missing |
| Contract config | YAML per Contract in one private repo, with SOPS secrets and an age key per Contract |
| Console | View and act. It shows config read-only and is reachable only over Tailscale |
| Investigations | Triage, then Question, then Check |
| Knowledge | Similar incidents and Teachings stay in their own Contract. Teachings need review. Shared knowledge is written by the Operator |
| Optimizer shape (2026-10-21) | The brain in the Hub (metrics aggregates, recommender, Sizing policies), the hands in the client cluster (an Actuator that pulls approved Sizings over the Relay). [ADR 0004](../adr/0004-standing-approval-for-sizing.md) |
| Writes under Automation | Allowed, opt-in per Contract and per Workload, read-only by default, audited and reversible. Reflexes (OOM step-up, burst CPU, startup boost, auto-rollback) act unattended, capped and expiring |
| Metrics for Sizings | Each Contract's own Prometheus; the Hub keeps per-Workload aggregates in Postgres, never raw series. A bundled collector only where a cluster has none |
| Sizing policies | In the contracts repo (`contract.yaml`), validated by `aiops contracts validate`; the Actuator renders them in the cluster |
| Applying Sizings | In-place resize first (Kubernetes 1.33+); webhook plus PDB-safe eviction only behind a per-Contract flag. Never Git write-back to client repos |
| Actuator | Python, same image as the Hub (`aiops actuator`); revisit only if webhook latency shows up in tests |
| Scope | Everything both products do: Workloads, horizontal, node and Karpenter, spot, cloud billing, JVM, Spark and batch, GPU and LLM inference, and as many integrations as possible |
| Testing | kind, KWOK and fake metrics for everything; paid write sandboxes (Karpenter, spot, GPU) small, time-boxed and approved by the Operator per phase |
| Vendor trials | Only after our part of a phase is built, to compare; read the terms first |
| Console (2026-10-21) | A React single-page app for the whole console, served by the Hub, replacing the Jinja pages once each is rebuilt. Same Tailscale identity and approver cookie, same-origin JSON API, strict CSP, model-written text rendered as text (no raw HTML, no live links), built in the image's Docker build with a pinned toolchain, nothing loaded from a CDN |
| Test lab | A persistent kind lab (on the Operator's Mac until 2026-11-14, since on a mini PC of his that stays up, where the Hub runs too): Prometheus, kube-state-metrics and load generators (diurnal, spikes, leaks, OOMs) running continuously so real history builds up, plus synthetic backfill for weeks of data |
| Usage aggregates | Kept 90 days per Contract |
| Hub MCP server | The Operator only, over Tailscale. Reads, plus Approval decisions on Proposed actions and single Sizings, the kill switch, and pull requests to the contracts repo for Automation and policy changes, which the Operator merges. Every write is audited as made through MCP. The Investigation agent never gets these tools |

## Defaults you can veto

| Choice | Default | Why |
|---|---|---|
| Persistence | Postgres 17, `langgraph-checkpoint-postgres`, psycopg 3, numbered `.sql` migrations | One database for state, checkpoints and knowledge |
| Secrets | `sops -d` at load. The Hub holds age keys for Direct Contracts only | No new library. A Relay Contract's credentials never reach the Hub |
| Many clusters | One kubernetes server per Contract, with a `cluster` argument limited to that Contract's clusters | A Contract can have dozens of clusters, and one server per cluster would multiply the tool count |
| Slack | Web API over httpx; Socket Mode over `websockets` (1.2), not slack-bolt | No public Slack endpoint, and no framework for one socket and a few calls |
| Console stack | FastAPI + Jinja + HTMX, with no JS build, until Phase 9.5; then a React app (see Decisions) | The optimizer's charts outgrew server-rendered pages |
| Console login | Tailscale identity headers from `tailscale serve`, with the console on `127.0.0.1`, plus an Operator allowlist | No passwords to manage. `serve` strips a client's copy of the header, but anything else that can reach the port (local processes, other containers on the compose network) could send it |
| Jira | An issue is created only when the Operator clicks | Never write to a client's tracker unasked |
| Budget | Monthly per Contract. Warn at 80%. At 100%, refuse new Investigations but still notify | An alert is never silently dropped |
| Model | One Hub default, overridable per Contract | |
| Similar incidents | Postgres full-text search, merged with pgvector when `AIOPS_EMBEDDING_MODEL` is set (7.4) | Unset, no embedding calls |
| Kubernetes versions | Find out each Contract's before Phase 12; the kind matrix covers 1.33+ for in-place resize | Sets what Phase 12 can rely on |
| Sizing data and Data policy | Per-Workload aggregates count as tool output under each Contract's Data policy and 6.4 masking | They leave the client network |
| Automation granularity | Per Workload, Operator only, in the contracts repo | One clear audit trail |
| In-cluster state | CRDs with our own names and schemas, rendered by the Actuator | Validation and scale over ConfigMaps |
| Clouds and distributions | Follow the Contracts' stacks; manual unit prices for on-prem | No cloud bill on RKE2 |
| Node autoscalers | Recommendations first, NodePool patches later | Patches need write access to node config |
| Cost source | List prices first; billing exports where a client grants read access | Billing needs extra permissions |

## Phases

Phases 0 to 8 were about 36 slices; Phases 9 to 19 add about 64. Phase 3 (Relay) is now a hard prerequisite of Phase 12, because every write to a client cluster goes through the Actuator, and it comes earlier if a new Contract can be reached only through a Relay. Phases 9 to 19 are outlines: each is grilled just before it is built, like every phase before it.

The list of phases with their status is in [the roadmap](../roadmap.md#phases). Each phase has its own page in this folder.
