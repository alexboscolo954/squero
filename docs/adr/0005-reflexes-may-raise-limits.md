---
status: accepted
---

# Reflexes may raise limits, Sizings never do

**Decision in one line:** a Sizing changes requests only, while a Reflex may raise a limit, in bounded, expiring, opt-in steps.

## Context

- A Sizing changes requests only.
- A container's limit is its owner's statement of how much it may ever use.
- So a Sizing keeps the limit, and flags a conflict as a risk ([ADR 0004](0004-standing-approval-for-sizing.md)).
- One failure no Sizing can fix: a container OOM-killed at its memory limit keeps being killed.
- It keeps being killed at 3 a.m. as at noon, until a person raises the limit in its manifest.

## Decision

- A **Reflex** may raise a limit. A Sizing never does.
- The first Reflex is the memory step-up (Phase 13).
- After an OOM kill at a memory limit, the Actuator raises that container's:
  - memory limit, and
  - memory request,
  together and by the same factor. The pod's QoS class stays.
- It does this on every pod of the Workload, in place, and on new pods through its webhook.
- Burst CPU and the startup CPU boost raise a CPU limit under the same rule.

## Guardrails

### Opt-in twice

- Off by default.
- The Contract sets `actuator: reflexes`.
- Only Workloads under Automation (`automate: true`) are touched.
- The Operator turns both on, in the contracts repo.

### Bounded

- Never past `max` times the limit in the Workload's template. The default is 2.
- Never past a LimitRange or a ResourceQuota.
- One step per container every 10 minutes.
- Step-ups and bursts together: at most 10 a cluster an hour.

### Expiring

- A step-up ends after `expires`, unless the need is still measured (slice 13.1c).
- A step-up never becomes the Workload's new normal by itself.

### Never

- Never under the Kill switch.
- Never on a paused Workload.
- Never for a Workload a VPA sizes.
- Never for a container whose memory an HPA scales on.
- Never while the Hub is unreachable.
  - A step-up is a new change, and the Kill switch can't be heard then.
- Never an eviction.

### Writes stay the same

- The Actuator's writes stay what they were:
  - `pods/resize`, which already carries limits;
  - its webhook, at a pod's creation.
- It still never patches a Workload, so nothing drifts from Git.
- The manifest's limit is unchanged.
- The Operator is told what to change in the manifest.

### Audit

- Every step is an Actuation in the Hub's audit trail, and a Kubernetes Event.
- A Revert drops it.

## What the Actuator can and can't know

- The pod reads the same (`OOMKilled`, exit code 137) whichever memory limit caused the kill:
  - the container's own limit;
  - its pod's limit;
  - or the limit the kubelet sets on all pods together, the node's allocatable memory.
- The kubelet's `SystemOOM` Event on the Node marks only a kill that no limit caused.
- So "container-limit" means: a memory limit was reached, and the node was not out of memory.
- A step-up may therefore raise a limit that was not the one reached.

This is accepted, for two reasons:

- Erring the other way leaves a container at its own limit, crash-looping. That is the case the Reflex exists for.
- The step raises the request with the limit. A container killed over its request needs that too.

One exception applies:

- While a Sizing that lowered that container's memory request is in its watch hour, an OOM kill of it is a Rollback.
- It is never a step-up.
- A lowered request makes a container the likelier victim of a kill above its own limit.

## Consequences

- Under Automation with Reflexes on, a container's limit in its manifest is no longer the most it can use.
- The most it can use is `max` times that limit.
- A client must agree to that, as to Automation itself.
- A Guaranteed pod stays Guaranteed, because request and limit move together.
- A Workload nobody automates is never touched.

## Considered Options

- **Never touch a limit: roll the Sizing back and tell the Operator.**
  - A Rollback restores requests, which does nothing for a container at its limit.
  - The kills go on until someone edits the manifest.
- **Let a Sizing raise limits.**
  - It would make every Sizing a possible change of the owner's ceiling.
  - It would need the same bounds on all of them.
  - Keeping limits to Reflexes keeps the standing approval of ADR 0004 narrow.
- **Patch the Workload's limit.**
  - Argo CD or Flux would put it back.
  - The Actuator would gain a permission that can change more than resources.
- **Have the Hub confirm from Prometheus that the container was at its own limit.**
  - A 30-second sample can miss the spike.
  - The step would come a minute or two late.
