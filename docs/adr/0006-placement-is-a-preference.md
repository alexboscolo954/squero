---
status: accepted
---

# Placement is a preference, never a requirement, and never moves a running pod

_Extended by slice 16.2b with one requirement, "not on a spot node", for a Workload's On-demand minimum._

Until Phase 15 the Actuator changed how much a pod asks and never where it runs. **Placement** (15.2) is the first time it influences where: its webhook adds, to each new pod of a Workload whose pods keep a node from being removed for a reason set in their spec, a preference for the nodes the client keeps for such pods (the cluster's **Pinned nodes**, named by a label in the Contract), and a toleration of their taint where the Contract names one. Gathered there, they stop holding the other nodes, which the cluster's autoscaler can then remove.

## What it may and may not do

- **A preference only.** One `preferredDuringSchedulingIgnoredDuringExecution` term of weight 100, after the pod's own terms; nothing of the pod's affinity is replaced, and no `requiredDuringScheduling` term, `nodeSelector` or `nodeName` is ever added. A pod for which the Pinned nodes have no room starts elsewhere, as it would have without the Actuator. A required rule could leave a pod Pending, the one failure an optimizer must not cause.
- **A toleration permits and requires nothing.** Only the Contract's own taint, only on the pods placed: a tainted pool keeps every other pod off, which is what leaves room there for the ones that can't move.
- **Never a running pod.** A pod's affinity can't be changed once it exists, and these are the pods nothing may evict. The Actuator never evicts, restarts or rolls out for Placement: a pod moves at its Workload's next rollout or restart, which the Hub names ("Restart shop/Deployment/api to free it") and a person does.
- **No pod-to-pod affinity.** The Kubernetes docs advise against inter-pod affinity above a few hundred nodes; a node label scales.
- **No new permission.** It is a JSON Patch on a pod's creation, through the webhook the Actuator already runs, and Events on Workloads, which it already writes.

## Why

- The client keeps the decision: which nodes are Pinned is their label, set on their node pool, and the Contract names it per cluster. No label, no Placement.
- The Contract's field is the consent, as for Reflexes (ADR 0005): no approval per Workload. What it can do is bounded to a ranking among nodes the pod could already run on, plus a toleration of one named taint.
- The Kill switch and the pause annotation stop it with everything else the webhook does. A Workload leaving the list stops its new pods being placed; its running pods keep what they have.

## Consequences

- With Karpenter a preference is treated as required when it picks a node to launch, and relaxed only when that can't be met: the label should be on a NodePool's template, or Karpenter may launch a node for it.
- On Cluster Autoscaler a preference isn't counted when it picks a node group to grow; Placement only ranks the nodes that exist.
- Spot placement (16.2) adds the one requirement the webhook makes, below.

## The one requirement: "not on a spot node" (slice 16.2b)

A Workload under a Spot ratio keeps its On-demand minimum off the spot nodes (and every replica of one that blocks a node). A preference can't promise that: a scheduler that finds room only on spot nodes would put them there, and an interruption would take the Workload's floor with it. So the webhook adds, for those replicas only, a required `NotIn` on the cluster's spot label, into each of the pod's own required terms (they are ORed: a term beside them would loosen them).

- **Why it can't strand a pod the way Placement's could.** It narrows a pod to on-demand nodes, which every cluster has and which the pod could run on before; a pod Pending for it is one no on-demand node has room for, which its autoscaler grows as it would for any pod. Placement's requirement would have narrowed a pod to a handful of Pinned nodes.
- **Everything else stays a preference**: the spot replicas prefer spot nodes (weight 100) and start on-demand when there is no spot room, so a fallback needs no code.
- **Still never a running pod**, never an eviction, a restart or a rollout: a Workload below its minimum after interruptions is repaired at its next rollout, which the Hub names.
- **Left alone**: a pod whose own spec already decides (a `nodeSelector` or a required term on the label's key): the client's choice wins.
- It also adds a label (`squero.io/capacity`, the side it chose, which its count goes by) and `controller.kubernetes.io/pod-deletion-cost: "1"` to on-demand replicas, unless the pod sets its own, so a scale-down takes spot replicas first. Both are metadata on a new pod: no new permission.

## Considered Options

- **A required node affinity, or a `nodeSelector`.** Certain to gather the pods, and certain to leave one Pending when the Pinned nodes are full or gone.
- **Evicting the running pods so they land on the Pinned nodes.** These are the pods nothing may evict: their own annotations or local data say so.
- **Pod-to-pod affinity, gathering such pods with each other.** No label to set, but costly to schedule at size, and it gathers them anywhere rather than where the client keeps room for them.
