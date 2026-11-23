# Roadmap

**In short:** squero is one Hub that runs SRE for several Contracts at once. It answers alerts, answers questions and runs checks, and never lets one Contract's data reach another. Since Phase 9 it also optimizes each Contract's resources: Sizings, cost, horizontal scaling, nodes, spot and GPUs, applied under the Operator's Automation.

This page says where the project is and what comes next. Each phase has its own page with every decision and slice. Terms are in [`CONTEXT.md`](../CONTEXT.md).

## Where we are (2026-11-23)

- **Latest release:** 0.66.0 (one page pattern on every page of the console), running on the mini PC's Hub.
- **Phases 0 to 8, 10 to 13, 15 and 18:** complete.
- **Phases 9, 14 and 16:** built, with some live checks owed.
- **Phase 17 (GPU and LLM inference):** built on fixtures; the paid GPU check is deferred.
- **Phase 19 (Integrations breadth):** grilled; 7 of 9 slices built (19.1a to 19.1c, 19.2, 19.3, 19.4, 19.5a).
- **Phase 20 (JVM, Spark and batch):** not grilled yet.
- **Phase 21 (Console design):** 21.1 to 21.4 in 0.64.0, 21.5 in 0.65.0 (2026-11-22); 21.6 (the page pattern on every page) in 0.66.0 (2026-11-22).

## Next

1. **19.5b:** the Create ServiceNow incident button, then 19.6, in [Phase 19](roadmap/phase-19.md)'s order.
2. The paid GPU check when the quota allows, and the phase's done-when on real GPUs (see [Phase 17](roadmap/phase-17.md)).

## Live checks still owed

| Check | Slice | Waiting for |
|---|---|---|
| Grafana Cloud with `basic` auth | 19.1a | The owner's free Grafana Cloud account |
| Amazon Managed Prometheus with `sigv4` | 19.1b | A cost estimate (about $1) and the owner's yes |
| Azure Monitor with `azure`, Google Managed Prometheus with `google` | 19.1c | A cost estimate (about $1 each) and the owner's yes |
| PagerDuty's webhook, alerts and note | 19.2 | The owner's free PagerDuty account; for the webhook, Caddy on the mini PC with the owner's OK |
| The Client report in Teams and in Google Chat | 19.4 | A Microsoft 365 account with Workflows, or a Google Workspace one |
| ServiceNow's past incidents, then a recorded Scenario and its eval | 19.5a | The owner's free Personal Developer Instance; for the eval, a cost estimate and the owner's yes |
| Spot list prices on GCP | 16.4a | Left for later by the Operator (procedure in the slice) |
| A Weekly profile's Backtest | 14.4 | Four stored weeks on the lab (12 December if it runs without gaps) |
| The Monday steps of the Need reads (08:00 and 09:00 UTC) | 14.1a | Still to look at |
| Protocol refusal between real parts | 18.3 | A 0.62.0 part meeting a newer Hub |
| The Fleet page on real Contracts | 18.4 | Not checked live yet |
| DCGM labels on a real GPU exporter | Phase 17 | Deferred: the sandbox's GPU quota is 0 |
| Teams and Google Chat | 19.4 | A Microsoft 365 or Google Workspace account |
| The console in the light theme, and its pages with real data | Phase 21 | The owner, on the mini PC's Hub (0.66.0) |

## Phases

| Phase | Theme | Status |
|---|---|---|
| [0](roadmap/phase-00.md) | Foundation: many Contracts, durable state | ✅ done |
| [1](roadmap/phase-01.md) | Slack | ✅ done |
| [2](roadmap/phase-02.md) | Web console, Tailscale only | ✅ done |
| [3](roadmap/phase-03.md) | Relay | ✅ done (0.30.0 to 0.32.1) |
| [4](roadmap/phase-04.md) | Integration framework and first integrations | ✅ done |
| [5](roadmap/phase-05.md) | Clouds, read-only | ✅ done |
| [6](roadmap/phase-06.md) | Investigation quality | ✅ done |
| [7](roadmap/phase-07.md) | Knowledge | ✅ done |
| [8](roadmap/phase-08.md) | Checks | ✅ done |
| [9](roadmap/phase-09.md) | Read-only Sizings | ✅ built |
| [10](roadmap/phase-10.md) | Cost and Savings | ✅ done (0.8.0 to 0.17.0) |
| [11](roadmap/phase-11.md) | The Hub as a source, and the whole console in the app | ✅ done (0.18.0 to 0.29.0) |
| [12](roadmap/phase-12.md) | Actuator | ✅ done (0.33.0 to 0.46.0) |
| [13](roadmap/phase-13.md) | Reflexes | ✅ done |
| [14](roadmap/phase-14.md) | Horizontal | ✅ built; 14.4's live check owed |
| [15](roadmap/phase-15.md) | Nodes | ✅ done (0.54.0, 0.55.0); 15.5 deferred |
| [16](roadmap/phase-16.md) | Spot | ✅ built; 16.4a's GCP check owed |
| [Audit](roadmap/audit.md) | Audit fixes A1 to A14 | ✅ done (A14 in 0.61.0) |
| [17](roadmap/phase-17.md) | GPU and LLM inference | ✅ built on fixtures; paid GPU check deferred |
| [18](roadmap/phase-18.md) | Platform: signed chart, Protocol, Fleet | ✅ done (0.62.0, 0.63.0; 18.1d after 0.63.0) |
| [19](roadmap/phase-19.md) | Integrations breadth | 🔨 7 of 9 slices |
| [20](roadmap/phase-20.md) | JVM, Spark and batch | ⏳ grill before building |
| [21](roadmap/phase-21.md) | Console design: sidebar, Overview, ScaleOps-style pages | ✅ done (0.64.0, 0.65.0, 0.66.0) |

Releases and dates for every version: [releases](roadmap/releases.md). Decisions that shaped the whole project: [decisions](roadmap/decisions.md).

## How every slice ships

A slice is one commit-sized change, about one working session (1 to 3 hours).

1. **Glossary first.** New terms go in `CONTEXT.md` before code. Add an ADR only when the decision is hard to reverse, would surprise a reader, and came from a real trade-off.
2. **Tests for every branch of new logic.** Mutation-check each new test: remove the logic, and the test must fail.
3. **An isolation test** for anything that stores, reads or sends Contract data. It uses the two demo Contracts and proves Contract A's data never reaches Contract B, whether in prompts, tools, storage or notifications.
4. **Rerun the real eval suite** when anything the model sees changes: the prompt, tool names, descriptions or schemas, or the Report schema. Commit the results under `evals/results/`. No Scenario may regress.
5. **Keep lint and the full suite green.** Update the docs (`docs/*.md`) and the config table.
6. **Write a one-line commit subject.** CI must be green before moving on.

**Where checks run.**
- **CI**, in the public repo: lint, unit and isolation tests, the kind demo integration and the deploy smoke test. These run on the two synthetic demo Contracts only.
- **Locally:** the real-LLM evals (they need the API key), and Contract Scenarios from the private contracts repo.

## Deferred

Parked on purpose, each with what would bring it back.

| Item | Why not now | Trigger to revisit |
|---|---|---|
| Full agent inside a client network | A Relay keeps credentials there already | A Contract forbids tool output leaving its network |
| Specialist sub-agents (IncidentFox) | Allowlists and good tools come first | Evals fail on cross-domain Scenarios |
| Anomaly detection (Prophet) | Needs history and tuning. Alerts already cover it | Repeated misses that no alert catches |
| Hierarchical retrieval (RAPTOR) | Full-text search over a small corpus is enough | Runbooks outgrow search plus embeddings |
| Multi-user RBAC, SSO | One Operator | A second person joins |
| Sandbox and credential proxy (IncidentFox, BSL) | A Relay keeps credentials out of the Hub | A Direct Contract's threat model demands it |
| Agent actions that run without approval | Not without months of evidence | Never by default. Sizings under Automation are approved as a policy ([ADR 0004](adr/0004-standing-approval-for-sizing.md)) |
| Fix PRs on GitHub/GitLab (HolmesGPT) | Needs code access per Contract | After Jira, when a Contract grants repo access |
| Splunk and other log sources | Phase 19 covers the metric and paging integrations first | After Phase 19 |
| Opsgenie | Atlassian stopped selling it in June 2025 and ends its support in April 2027 (Phase 19's grill) | A client asks: then Jira Service Management's operations alerts |
| Sizings from Datadog or New Relic metrics | A second metrics engine beside PromQL; Phase 19 adds them for triage only | A client without Prometheus |
| KWOK load test of the Actuator (thousands of fake Workloads) | No Contract has that many Workloads yet | A Contract nears a thousand automated Workloads |
| The Actuator writing a Floor to an HPA or ScaledObject (Phase 14 only recommends) | A wrong minimum costs availability, GitOps puts a patched HPA back, and it is a new Actuator write | Backtests within budget on a real Contract four weeks in a row; then its own grill and an ADR |
| Compaction by the Actuator: evicting pods to empty a node (15.5) | The autoscaler's own drain does it once its setting is right, the default scheduler spreads an evicted pod back, and two controllers emptying nodes can work against each other | A cluster where the proposed autoscaler setting was applied and the nodes still stay |
| Node sizes proposed from list prices ("smaller instances would cost X less") | Karpenter picks sizes itself; on Cluster Autoscaler it means redesigning node groups the Hub can't read (grilled with Phase 16, 2026-11-12) | A Contract on Cluster Autoscaler asks for it |
| A cluster-wide read for the Blockers report, as a manifest a client may apply | It would be the first cluster-wide grant in a client's cluster | "Not visible" pods hide too much of a real report |
| CRDs for the Actuator's state | The Hub is the only source of truth; kubectl shows Events and its ConfigMap | A client wants to read Automation state with kubectl |
| GPU sharing by adopting an existing scheduler, KAI or HAMi (17.6) | The largest and least certain GPU slice; idle GPUs come first (grilled 2026-11-20) | 17.2 flags idle GPUs on a real Contract that time-slicing or MIG would pack |
| Batch inference (17.7) | Needs an inference server's metrics, which 17.3 reads first (grilled 2026-11-20) | 17.3 and 17.4 built on a real Contract |
| Paid GPU check: Phase 17's DCGM labels on a real exporter (a spot g4dn.xlarge on EKS for 2 hours, about 0.90 USD) | The sandbox's G-instance quotas (spot and on-demand) are 0 in us-east-2; skipped 2026-11-20 by the Operator rather than requesting a raise | The quota is raised, or a Contract runs GPUs |
| OpenShift values for the chart (18.2) | No OpenShift sandbox: values written without one would be guesses (grilled 2026-11-21) | A Contract runs OpenShift |
| A watchdog for the Hub itself: an hourly ping to an outside dead man's switch (`SQUERO_HEARTBEAT_URL`), which alerts when the pings stop | A stopped Hub can post nothing, and a ping goes to an outside service the Operator picks; Stale notices (A14) cover the gap seen live, the Hub up and a cluster down (grilled 2026-11-20) | The Hub itself went down unnoticed |
