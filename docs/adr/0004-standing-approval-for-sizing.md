---
status: accepted
---

# An in-cluster Actuator may apply Sizings under a standing approval

**Decision in one line:** a Contract may turn on Automation, so an Actuator in the client cluster applies the Sizings of an approved policy without asking for each change.

## Context

- The Hub becomes a resource optimizer as well as an SRE agent (Phases 9 to 19).
- The optimizer is inspired by ScaleOps and Kubex: their ideas, not their code.
- Continuous rightsizing cannot wait for a click on every change.
- Neither can the reflexes that make it safe, such as an OOM step-up at 3 a.m.

## Decision

- A Contract may turn on **Automation** for chosen workloads.
- The Operator approves a Sizing policy once.
- From then on, the Actuator applies the Sizings that policy produces, without asking again.

### Who does what

The brain stays in the Hub:

- metrics aggregates;
- the recommender;
- the Sizing policies, which come from the contracts repo.

The hands live in the client cluster, as an **Actuator**:

- It is a new role in the same image (`squero actuator`).
- It pulls approved Sizings from the Hub over HTTPS, with its own token. The token is bound to one Contract's cluster.
- It applies them in place first (Kubernetes 1.33+).
- A pod-creation webhook keeps them on new pods.
- PDB-safe eviction works only behind a per-Contract flag.
- It never writes to client Git repositories. It changes pods, not manifests.

*Amended 2026-11-09 (Phase 12 grilling): its own connection and token rather than the Relay's, so the Relay keeps a read-only identity; the webhook is part of Automation, only eviction is behind the flag.*

## What this supersedes

- **ADR 0002's rule** that every write is a Proposed action approved per call.
  - That rule still holds for everything the agent proposes.
  - It no longer holds for Sizings under Automation, which the Operator approved as a policy.
- **The roadmap's rule** that every write to a client cluster is a tool call.
  - The Actuator reaches the Hub on its own.
  - ADR 0002's Relays still only run tools, read-only unless the client turns writes on.
- **The roadmap's deferred row** "Actions that run without approval: never by default".
  - Automation is still never on by default.

## Guardrails

- **Off by default.**
  - Automation is opt-in per Contract, then per workload.
  - Only the Operator turns it on, in the contracts repo.
- **Bounded and recorded.**
  - Every applied Sizing is bounded by its policy's minimums and maximums.
  - Each one is recorded in the Hub's audit trail and as a Kubernetes event.
  - Each one can be rolled back.
- **Auto-rollback.** It undoes a Sizing that made pods unhealthy.
- **Reflexes are capped and expire.**
  - Reflexes act unattended: OOM memory step-up, burst CPU headroom, startup CPU boost, auto-rollback.
  - Each is capped per Contract and expires.
- **Fails open.** If the Actuator is down, pods are created unchanged by the webhook.
- **Kill switch.** A per-Contract Kill switch stops all Automation at once.
- **The agent cannot widen any of this.**
  - A Triage may propose a Sizing.
  - That Sizing stays an ordinary Proposed action.

## Consequences

- A client that runs the Actuator grants it write RBAC on its workloads.
- With the eviction flag on, the client also grants a mutating webhook.
- Each client must agree to that.
- A Contract that doesn't agree keeps read-only Sizings.
- The Actuator needs only an outbound HTTPS connection to the Hub, like a Relay.
- The safety model (now `docs/safety.md`) changes when Phase 12 ships, not before.

## Considered Options

- **Keep per-change approval for every Sizing.**
  - Safest.
  - But continuous rightsizing becomes a queue of clicks, and reflexes become impossible.
- **Everything in the client cluster.**
  - The recommender, metrics and UI run per cluster, as ScaleOps ships.
  - It works with the Hub down.
  - But it means N copies of the brain to upgrade, and per-workload data spread across sites.
  - Held back: the Hub already holds every Contract's other state.
- **Pull requests to the client's Git repositories.**
  - Fully reviewable.
  - But it needs client Git credentials, and it is too slow for reflexes.
