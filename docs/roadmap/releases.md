# Releases

**In short:** every version the roadmap names, from 0.2 to 0.63.0, newest first, with its date and what it shipped. Where the roadmap gives no release date, the date is the day the slice was built or checked live, and the Notes column says so. Events that were not releases, such as the rename and the license change, are in [Notable events](#notable-events).

## Releases

| Version | Date | What it shipped | Notes |
|---|---|---|---|
| 0.63.0 | 2026-11-21 | 18.3 Protocol (a Relay or an Actuator of another Protocol is refused, and the Agenda says who must upgrade) and 18.4 the Fleet page | Runs on the mini PC's Hub |
| 0.62.0 | 2026-11-21 | 18.1: the `squero` Helm chart, published to ghcr and signed with the image | Runs on the mini PC's Hub. The first signed chart; checked in its tag run |
| 0.61.0 | 2026-11-21 | A14: Stale notices (migration 0047) | |
| 0.60.0 | 2026-11-20 | Phase 17's slices so far (17.1 to 17.5) | Runs on the mini PC's Hub. Built on fixtures; the paid GPU check is Deferred |
| 0.59.0 | not in the roadmap | Not stated | The Hub's version in the AWS Part A check (2026-11-16) |
| 0.57.0 | not in the roadmap | Not stated | Named as an Actuator that takes `workloads` for Workloads with a ratio and ignores `cronjobs` |
| 0.55.0 | 2026-11-15 | 15.2a and 15.2b: Placement (the Hub's side, and the Actuator's webhook under ADR 0006) | Runs on the mini PC's Hub |
| 0.54.0 | 2026-11-15 | 15.3b and 15.3c: each node type's Sizing applied by the Actuator, and the money by type | Runs on the mini PC's Hub |
| 0.48.0 | 2026-11-14 | Not stated | The Mac Hub's version at step 7 of the lab move, the day the Hub moved to the mini PC |
| 0.47.0 | by 2026-11-13 | Not stated | The Mac Hub's version at the 14.1a check (2026-11-13) |
| 0.46.0 | 2026-11-12 | 12.9a, 12.9b and 12.9c: the Actuator's audit fixes (what can harm a Workload, then the narrower ones) and the Hub hardened against a compromised Actuator's reports | Migration 0030 on the Mac Hub: 3 Actuations, none untold |
| 0.45.0 | 2026-11-11 | Phase 12 complete: 12.8c, CronJobs through the webhook only | Date of its last slice; the release date is not recorded |
| 0.44.0 | 2026-11-11 | 12.8a: the Actuator resizes StatefulSets and DaemonSets | Date of the build. An integration test still calling `pods_of` without its kind was fixed first |
| 0.43.0 | 2026-11-11 | 12.7 and 12.7b: PDB-safe eviction behind the Contract's flag, in windows; never a pod of a one-replica Deployment or one no PodDisruptionBudget covers | Date of the build |
| 0.42.0 | 2026-11-10 | 12.6b: the console's Automation page | Date of the build |
| 0.41.0 | 2026-11-10 | 12.5: Automation on a Sizing rule; 12.6a: Revert (migration 0029) | Date of the build |
| 0.38.0 | 2026-11-10 | Not a named slice | The build the 12.1 to 12.4 live check ran on (Mac Hub and the kind lab) |
| 0.37.0 | 2026-11-10 | The rename from aiops-sre-agent to squero | See [Notable events](#notable-events) |
| 0.36.0 | 2026-11-10 | 12.4: each resized pod's restart count is kept | Date of the build |
| 0.35.0 | 2026-11-10 | 12.3: pre-checks before any resize, each a skip with its reason | Date of the build |
| 0.34.0 | 2026-11-09 | 12.2: the Actuator's pod-creation webhook | Date of the build |
| 0.33.0 | 2026-11-09 | 12.1: a cluster's `actuator_token_secret` (32 or more characters, never a Relay's) | Date of the build |
| 0.32.1 | 2026-11-09 | Fix to 3.4: the Relay's port setting is `AIOPS_RELAY_ALERTS_PORT` | Found by 3.4's live check. A 3.4 log fix the same day: a Relay the Hub refuses says "check the token" only for 401 or 403 (version not stated) |
| 0.32.0 | 2026-11-09 | 3.4: the Relay takes its cluster's Alertmanager webhook | Checked live 2026-11-09 on the kind lab |
| 0.31.0 | 2026-11-09 | 3.3b: a Relay's cluster is a Sizing target | Checked live 2026-11-09 |
| 0.30.0 | 2026-11-09 | 3.3a: a Relay's cluster is in the Hub's own Kubernetes and Prometheus tools | Checked live 2026-11-09 |
| 0.29.0 | 2026-11-08 | 11.7b: the Jinja console is gone; the React app is the whole console | Checked live 2026-11-08 |
| 0.28.1 | 2026-11-08 | 11.7a follow-up (migration 0025) | Checked live 2026-11-08 |
| 0.28.0 | 2026-11-08 | 11.7a: asking a Question from the console app | Checked live 2026-11-08 |
| 0.27.0 | 2026-11-08 | 11.6b: the Checks page | Checked live 2026-11-08 |
| 0.26.0 | 2026-11-08 | 11.6a: the Teachings page | Checked live 2026-11-08 |
| 0.25.0 | 2026-11-08 | 11.5b: Investigation decisions and feedback in the app | Checked live 2026-11-08 |
| 0.24.0 | 2026-11-08 | 11.5a: the Investigations list and page | Checked live 2026-11-08 |
| 0.23.0 | 2026-11-08 | 11.4: the Agenda, through the Hub MCP's `now()` | Checked live 2026-11-08 |
| 0.22.0 | 2026-11-07 | 11.3: Proposed changes as pull requests (`propose_change`) | Checked live 2026-11-07 with the private contracts repo |
| 0.21.1 | 2026-11-07 | 11.2b: Approved Sizings (migration 0024) | Also: oauthlib 4.0.1 (Dependabot #23). The console's dev-only minimatch alert (#24) was dismissed as tolerable risk |
| 0.20.0 | 2026-11-07 | 11.2a: the Kill switch through the Hub MCP server (migration 0023) | Checked live 2026-11-07 |
| 0.19.0 | 2026-11-06 | 11.1b: the Hub MCP's read tools (`cost`, `savings`, `bill`, `investigations`) | Checked live 2026-11-06 |
| 0.18.1 | 2026-11-06 | Fix: `serve` gives the `aiops` loggers a stderr handler at INFO | Found by 11.1a's live check |
| 0.18.0 | 2026-11-06 | 11.1a: the Hub MCP server for Claude Code | Checked live 2026-11-06 on the Mac Hub |
| 0.17.0 | 2026-10-31 | 10.5c: GCP's bill check | Shipped on fixtures; live check 2026-11-02 reconciled at -1.0% |
| 0.16.0 | 2026-10-31 | 10.5d: node-hour coverage in the bill check (from 10.5b's live finding) | |
| 0.15.0 | 2026-10-26 | 10.5b: Azure's bill check | |
| 0.14.0 | 2026-10-26 | 10.5a: the AWS bill check | Checked live 2026-10-26 against Cost Explorer |
| 0.13.0 | 2026-10-26 | 10.4: cloud list prices | Checked live 2026-10-25 (the local v0.13.0 image) |
| 0.12.1 | 2026-10-25 | Dependency alert fixes | |
| 0.12.0 | 2026-10-25 | 10.3b: Realized Savings | Checked live 2026-10-25 |
| 0.11.0 | 2026-10-24 | 10.3a: Savings and compare periods | Checked live 2026-10-24 |
| 0.10.0 | 2026-10-24 | 10.2b: cost by team and label | Checked live 2026-10-24 |
| 0.9.0 | 2026-10-24 | 10.2a: cost by namespace | Checked live 2026-10-24 |
| 0.8.0 | 2026-10-24 | 10.1: unit prices | Checked live 2026-10-24 |
| 0.7.0 | 2026-10-23 | 6.4: Masking | Checked live 2026-10-24 on the Mac Hub |
| 0.6.0 | 2026-10-23 | 9.6a: the agent gets Sizings; 9.6b: per-cluster PromQL | The roadmap names a live Triage on the Mac Hub after it; no result is recorded |
| 0.5.0 | 2026-10-23 | 9.5b: the weekly Sizing digest | |
| 0.4.0 | 2026-10-23 | 9.5a: the console app | |
| 0.3.0 | 2026-10-23 | 9.1 to 9.4: Sizings stored hourly on the Mac Hub | 9.1 to 9.4 built and checked on kind |
| 0.2 | not recorded | The Hub's own list decides read or write (`read_only`) | Named only as "Done in 0.2" in [Where we start](decisions.md#where-we-start) |

Versions that the roadmap never names (0.1, 0.39.0, 0.40.0, 0.49.0 to 0.53.0, 0.56.0 and 0.58.0) have no row.

## Notable events

Events that were not releases, in date order.

| Date | Event |
|---|---|
| Before 2026-10-21 | Phases 0 to 8 are done. Phase 3 (Relay) was the last of them. |
| 2026-10-21 | The Operator chose to grow the Hub into a resource optimizer as well (Phases 9 to 19, [ADR 0004](../adr/0004-standing-approval-for-sizing.md)). |
| 2026-10-23 | 9.1 to 9.4 built and checked on kind. |
| 2026-11-02 | The GCP sandbox's bill for 2026-11-02 reconciled at -1.0%. Phase 10 is done. |
| 2026-11-08 | Phase 11 is done: the Hub MCP server for Claude Code, Approval decisions, Approved Sizings and the Kill switch through it, Proposed changes as pull requests, the Agenda, and the whole console in the React app. |
| 2026-11-09 | Phase 3 is done. [ADR 0002](../adr/0002-hub-and-relay.md) accepted with one cluster per Relay. Phase 12 (Actuator) grilled into 8 slices. |
| 2026-11-10 | **Rename.** The project was renamed from aiops-sre-agent to **squero** (the Venetian yard where gondolas are built and repaired; the logo is a gondola's ferro). The repo, image, Python package, `squero` command, `SQUERO_*` settings, `squero_*` metrics and Kubernetes names changed in one release, 0.37.0. The eval recordings' node and resource names were renamed too (`squero-control-plane`). |
| 2026-11-10 | The 18 Scenarios that carried those names were rerun on Haiku (`evals/results/2026-11-10/`): 16 passed. `aws-lambda-timeout-cut` failed, as on its last Haiku run. `teaching-restores-from-secret` chose `bad_config` once, then passed twice, as it does with the old names. |
| 2026-11-10 | **License.** The license changed from MIT to PolyForm Noncommercial 1.0.0 (no commercial use by others). Releases up to 0.37.0 stay MIT. `TRADEMARKS.md` reserves the name and the ferro logo. |
| 2026-11-10 | The README became a short overview, with six guides under `docs/`. |
| 2026-11-10 | Phase 12's live check on kind found two issues, built as 12.4b. |
| 2026-11-10 | **Model comparison and effort sweep:** Opus 5.5 at medium effort passed 66 of 66. [Details](#model-comparison-and-effort-sweep-2026-11-10). |
| 2026-11-10 | **Bug found by that comparison.** At the tool call limit, an answer given as a tool got two results, and Anthropic refused the conversation. The limit no longer counts or blocks the answer (`ToolBudget`). Decided the same day, and built in audit A13: one line in the weekly Sizing digest summing the week's Actuations ("Actuator this week: 5 resized, 1 rolled back"). |
| 2026-11-11 | An audit of the Actuator (three reviews: its rollout state, eviction and webhook, and the Hub's side) found no cross-Contract leak and no eviction against its rules. It found failures-and-restarts bugs: the five that can harm a Workload were fixed in 12.9a, the narrower ones in 12.9b. Both were built and checked live, which found two more, fixed. 12.9c hardened the Hub against a compromised Actuator's reports. Released as 0.46.0. |
| 2026-11-12 | Phases 13 (Reflexes) grilled into 6 slices. Phase 14 (Horizontal) grilled into 5. Phase 15 (Nodes) and Phase 16 (Spot) each grilled into 6. |
| 2026-11-12 | 12.10 (a vanished node is never a Rollback; found in Phase 16's grill) was built and checked live on kind. The check found one more false Rollback, a node that restarts and comes back. Fixed in 12.11, built and checked live. |
| Undated in the roadmap | A race in the Kubernetes server's `pod_logs` failed a CI integration job; fixed. [Details](#the-pod_logs-race). |
| 2026-11-14 | **The lab moved to a mini PC.** [Details](#the-lab-moved-to-a-mini-pc-2026-11-14). |
| 2026-11-14 | **The Hub moved to the mini PC** the same day (0.48.0). Its battery cost on the Mac was the reason. |
| 2026-11-16 | **AWS, Part A.** A throwaway EKS 1.37 cluster in a sandbox account, 04:12 to 12:32 UTC, about 2.3 USD of the Operator's credits (ceiling 5). Phases 15 and 16 were checked live on it. Torn down and swept empty. |
| 2026-11-17 to 2026-11-21 | **AWS, Part B.** The bill checks on a fresh cluster, about 50 hours, about 8.5 USD of the Operator's 15. 16.1b reconciled at +1.2%. 16.4b's spot lines and 16.4a's spot read were confirmed on AWS. The cluster was deleted after its snapshot. |
| 2026-11-20 | The paid GPU check was skipped by the Operator rather than requesting a quota raise. The sandbox's G-instance quotas are 0 in us-east-2. It is in [Deferred](../roadmap.md#deferred). |
| 2026-11-21 | **0.63.0's tag run hung.** The arm64 build's `npm ci` for the console sat silent under QEMU for 75 minutes (0.62.0's whole build took 3.5). The publish job had no time limit, so it was cancelled with nothing pushed, then rerun: 4 minutes, the image and chart signed, verified and public with no stored login. The publish job now stops after 30 minutes (`6f48ddb`). |
| 2026-11-21 | 18.1d added from that hang: the console stage builds on the runner's own platform. |
| 2026-11-21 | Phase 18 complete (18.1, 18.3, 18.4). |
| 2026-11-21 | Phase 19 (Integrations breadth) grilled into 8 slices. JVM, Spark and batch moved to Phase 20. Opsgenie moved to Deferred. |
| 2026-11-21 | 19.1a built: `bearer` and `basic` Prometheus auth through one helper. Its Grafana Cloud check is owed. |

## Event details

### Model comparison and effort sweep (2026-11-10)

- **New settings:**
  - `SQUERO_MODEL_EFFORT`: the model's reasoning effort.
  - `SQUERO_EMBEDDING_BASE_URL` and `_API_KEY`: embeddings on their own gateway, so the model can be called directly on Anthropic, which has no embeddings API. When set, the model's key and headers never reach it.
- **Run:** the evals, three runs each, paid from one-off Anthropic credits.
- **Results** ([`docs/development.md`](../development.md), Model comparison):

  | Model and effort | Passed (of 66) |
  |---|---|
  | Opus 5.5, medium | 66 |
  | Opus 5.5, high | 64 |
  | Opus 5.5, low | 62 |
  | Sonnet 5.5 | 58 |
  | Haiku 5.5 | 38 (through an answer tool, LangChain not knowing it yet) |

### The `pod_logs` race

- A CI integration job failed on a race in the Kubernetes server's `pod_logs`.
- For a crashed run whose log file the kubelet had already removed, the answer was `failed to try resolving symlinks in path "/var/log/pods/..."` in place of logs, and it reached the caller as the pod's logs.
- It now counts as the same race as `unable to retrieve container logs`: tried again, then the latest run's logs with a note.
- The tool's name, description and schema are unchanged, so no eval rerun.

### The lab moved to a mini PC (2026-11-14)

- The test lab had run on the Operator's Mac until 2026-11-14.
- `make lab-remote` creates the kind lab `lab` on another host over ssh:
  - its API server and Prometheus on one private address only;
  - its node capped (2.5 CPUs, 6 GiB) and restarting with Docker.
- The small lab (`make lab LAB=small`) was installed on a 4-CPU mini PC of the Operator's. All 29 pods ran.
- The Hub reads it with a read-only kubeconfig.
- Step 7: the Mac Hub's hourly run read both clusters with no error.
- Step 8: the lab's Workloads and namespace were deleted from the Mac's cluster.
