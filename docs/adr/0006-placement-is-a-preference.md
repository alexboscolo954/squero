---
status: accepted
---

# Placement is a preference, never a requirement, and never moves a running pod

**Decision in one line:** Placement only ranks where new pods may go, through a preference and a toleration, and it never moves a running pod.

_Extended by slice 16.2b with one requirement, "not on a spot node", for a Workload's On-demand minimum._

## Context

- Until Phase 15, the Actuator changed how much a pod asks.
- It never changed where a pod runs.

## Decision

**Placement** (15.2) is the first time the Actuator influences where a pod runs.

- Its webhook adds a preference to each new pod.
- The pods it covers belong to a Workload whose pods keep a node from being removed, for a reason set in their spec.
- The preference is for the nodes the client keeps for such pods. These are the cluster's **Pinned nodes**, named by a label in the Contract.
- Where the Contract names a taint for those nodes, the webhook also adds a toleration of that taint.
- Gathered on the Pinned nodes, these pods stop holding the other nodes. The cluster's autoscaler can then remove those other nodes.

## What it may and may not do

### A preference only

- It adds one `preferredDuringSchedulingIgnoredDuringExecution` term of weight 100.
- The term goes after the pod's own terms. Nothing of the pod's affinity is replaced.
- It never adds a `requiredDuringScheduling` term, a `nodeSelector` or a `nodeName`.
- A pod for which the Pinned nodes have no room starts elsewhere, as it would have without the Actuator.
- *Why:* a required rule could leave a pod Pending. That is the one failure an optimizer must not cause.

### A toleration permits and requires nothing

- Only the Contract's own taint is tolerated, and only on the pods placed.
- A tainted pool keeps every other pod off.
- That leaves room in the pool for the pods that can't move.

### Never a running pod

- A pod's affinity can't be changed once it exists.
- These are the pods nothing may evict.
- The Actuator never evicts, restarts or rolls out for Placement.
- A pod moves at its Workload's next rollout or restart.
- The Hub names that restart (for example "Restart shop/Deployment/api to free it"), and a person does it.

### No pod-to-pod affinity

- The Kubernetes docs advise against inter-pod affinity above a few hundred nodes.
- A node label scales.

### No new permission

- Placement is a JSON Patch on a pod's creation, through the webhook the Actuator already runs.
- It writes Events on Workloads, which the Actuator already writes.

## Why

- **The client keeps the decision.** Which nodes are Pinned is their label, set on their node pool. The Contract names it per cluster. No label, no Placement.
- **The Contract's field is the consent**, as for Reflexes (ADR 0005). No approval per Workload.
- **What it can do is bounded.** It ranks nodes the pod could already run on, and it tolerates one named taint.
- **The Kill switch and the pause annotation** stop it with everything else the webhook does.
- **A Workload leaving the list** stops its new pods being placed. Its running pods keep what they have.

## Consequences

- With Karpenter, a preference is treated as required when it picks a node to launch. It is relaxed only when that can't be met.
  - So the label should be on a NodePool's template. Otherwise Karpenter may launch a node for it.
- With Cluster Autoscaler, a preference isn't counted when it picks a node group to grow.
  - Placement only ranks the nodes that exist.
- Spot placement (16.2) adds the one requirement the webhook makes, described below.

## The one requirement: "not on a spot node" (slice 16.2b)

A Workload under a Spot ratio keeps its On-demand minimum off the spot nodes. So does every replica of one that blocks a node.

- A preference can't promise that. A scheduler that finds room only on spot nodes would put them there.
- An interruption would then take the Workload's floor with it.
- So the webhook adds, for those replicas only, a required `NotIn` on the cluster's spot label.
- It adds that term into each of the pod's own required terms. The terms are ORed, so a term beside them would loosen them.

### Why it can't strand a pod the way Placement's could

- It narrows a pod to on-demand nodes.
- Every cluster has on-demand nodes, and the pod could run on them before.
- A pod Pending for it is one that no on-demand node has room for. Its autoscaler grows the cluster for it, as for any pod.
- Placement's requirement would have narrowed a pod to a handful of Pinned nodes.

### Everything else stays a preference

- The spot replicas prefer spot nodes (weight 100).
- They start on-demand when there is no spot room. So a fallback needs no code.

### Still never a running pod

- Never a running pod, an eviction, a restart or a rollout.
- A Workload below its minimum after interruptions is repaired at its next rollout, which the Hub names.

### Left alone

- A pod whose own spec already decides is left alone.
- That means a `nodeSelector`, or a required term on the label's key. The client's choice wins.

### CronJob retries

- A CronJob's retry (slice 16.3b) gets the same requirement.
- A run's pods prefer spot nodes on its first tries.
- Once the run has a failed pod, its retries are required off spot nodes. So a run is interrupted once at most.
- This applies only where its Jobs allow a retry and count an interrupted pod as failed.

### Labels and deletion cost

- The webhook adds a label, `squero.io/capacity`, naming the side it chose. Its count goes by this label.
- It adds `controller.kubernetes.io/pod-deletion-cost: "1"` to on-demand replicas, unless the pod sets its own.
- So a scale-down takes spot replicas first.
- Both are metadata on a new pod, so no new permission is needed.

## Considered Options

- **A required node affinity, or a `nodeSelector`.**
  - Certain to gather the pods.
  - Certain to leave one Pending when the Pinned nodes are full or gone.
- **Evicting the running pods so they land on the Pinned nodes.**
  - These are the pods nothing may evict.
  - Their own annotations or local data say so.
- **Pod-to-pod affinity, gathering such pods with each other.**
  - No label to set.
  - But costly to schedule at size.
  - It gathers them anywhere, rather than where the client keeps room for them.
