# Phase 6: Investigation quality

**In short:** Phase 6 made Investigations better in four ways. It added a timeline of recent changes, handled large tool output, grouped related alerts, and masked identifiers before they reach the model.

*What it adds:* a timeline of changes, paging through large tool output, alert correlation, and masking. Masking keeps identifiers away from the model provider.

**Status:** done (4 slices).

| Slice | What | Status |
|---|---|---|
| [6.1](#61-what-changed) | What changed: rollouts, GitOps and cloud activity | ✅ done |
| [6.2](#62-large-output) | Large output: `read_more` | ✅ done |
| [6.3](#63-alert-correlation-within-a-contract) | Alert correlation within a Contract | ✅ done |
| [6.4](#64-masking) | Masking | ✅ built, checked live |

## 6.1 What changed

**Plan:** one timeline of rollouts, Helm releases, ArgoCD and Flux syncs, and cloud activity logs before the alert.

**Done when:** Scenarios pass where the cause is a change to a different resource.

**Part a, done:**

- `kubernetes_recent_changes` lists rollouts with their diffs, and Helm chart labels.
- Release Secrets stay unreadable.
- The Scenario `dependency-bad-rollout` passes.

**Part b, done:**

- ArgoCD Applications, and Flux Kustomizations and HelmReleases, come from a cluster's `gitops_namespaces`.
- They are shown only when they deploy wholly into the Contract's namespaces.
- There is an opt-in RBAC file: `deploy/gitops/rbac-gitops.yaml`.
- *Tests:* stand-in CRDs on kind. The real controllers were not used.

**Part c, done:** each cloud's guide names its activity or audit log, when the Contract lists that tool.

Slice 6.1 is done.

## 6.2 Large output

**Plan:**

- Select fields and filter JSON at the server.
- Oversized results go to storage, behind a `read_more` tool. They are not cut. This is a HolmesGPT idea.

**Built:**

- An oversized result stays in the Investigation's checkpointed state. It is redacted.
- The Hub's `read_more` pages through it, greps it, or picks fields from its JSON records.
- The filter belongs to the Hub, not to each server. So vendor MCP servers get it too.

**Done:** Scenario `gcp-who-deployed` passes. Its answer is only past the cap.

## 6.3 Alert correlation within a Contract

**Plan:** a new Alert group that overlaps an open Investigation in time and topology joins it. It does not start a Triage.

- This needs a new glossary term. It was decided in a grilling round when the slice was built.

**Part a, done:**

- The new term is **Related alert group**. It was decided in a grilling round on 2026-10-19.
- A group joins an open Triage when both hold:
  - they share a match label;
  - no scope label differs;
  - they are within 30 minutes.
- It is posted in the thread. It costs no investigation.

**Part b, done:**

- A "Triage separately" button on the related group's post starts its own Triage.
- It works once, within the budget.
- The two groups never join again.

## 6.4 Masking

**Plan:** identifiers are replaced with stable placeholders before the LLM call. They are restored in the Report. This is an OpenSRE idea. It was to be built when the first Contract's Data policy required it.

**Grilled 2026-10-23 (all recommended). Built ahead of a Contract that needs it. Off by default.**

- `data_policy.mask` lists terms. A term matches whole words, in any case. A domain covers its subdomains.
- Emails and IPs are masked, unless turned off.
- Kubernetes names are masked only when a term names them.
- One model-call middleware masks each request on the wire. It unmasks the reply too. This includes tool-call arguments and the structured result.
- So the state, the tool calls and the Reports stay plaintext.
- It covers Triage, Questions, Checks and the Teaching drafter.
- The Report embeddings send masked text.
- A placeholder is an HMAC with the Contract's own `mask-key` secret.
  - It is stable within a Contract.
  - It differs across Contracts.
- Values are restored everywhere except at the provider.
- The same middleware redacts secrets for every Contract. This closes a gap for alert labels and Questions.
- The console counts what each Investigation masked.

**Done when:**

- a fake model sees no listed term, email, IP or secret from a Triage, a Question, a Check, the Teaching drafter, or an embedding;
- tools, stored rows and the Report hold the real values;
- the same value masks differently in the two demo Contracts;
- a masked copy of a Scenario, whose clue is a host, passes on Haiku.

**Built (2026-10-23, as grilled):**

- The Scenario `dependency-bad-rollout-masked` passed on Haiku in run 1 (10 calls). Its Evidence was restored to `inventory.orders`.
- But its root cause shortened one placeholder to `<host>`. That cannot be restored.
- The masking Contract's system prompt now says to copy placeholders whole.
- Run 2 passed (10 calls), with every placeholder restored.

**Changes from the grilling:**

- A placeholder hashes the value only, not its kind. Its prefix already says the kind.
- A value written in two casings in one request comes back as first written.
- Tool messages' artifacts are masked too. The providers used never send them.
- A masked term counts as one of the Contract's names for the Shared knowledge check.
- Both demo Contracts mask: `shop.example` and `bank.example`. Each uses its own `mask-key`.

**Checked live (2026-10-24, Mac Hub, v0.7.0):**

- demo-shop masked its kind node's name, its emails and its IPs. It used its own random `mask-key`.
- A Triage of `lab/leaky` on Haiku masked 1 IP and 1 name. The console says so.
- Its Slack Report held only real values.
