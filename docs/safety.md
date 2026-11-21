# Safety model

**In short:** an agent that reads logs and can change a cluster is a prompt-injection target. A log line can say "scale this to zero". So the important controls sit **outside the model**: RBAC, the Hub's own list of reads and writes, a human approval before every write, and server-side checks of every cluster and namespace. The model's own judgement is never the last line.

## The layers at a glance

| Layer | What protects you | Details |
|---|---|---|
| Cluster access | Every cluster is named explicitly; no fallback to your kubectl context | [Cluster access](#cluster-access) |
| RBAC | Read-only, bound per namespace, no Secrets or ConfigMaps | [RBAC](#rbac) |
| Write scope | Writes are opt-in, and only eviction and scale | [Write scope](#write-scope) |
| Actuator | A narrow ServiceAccount that applies only what the Hub approved | [The Actuator](#the-actuator) |
| Contract scope | Every tool checks cluster and namespace against its own Contract | [Contract scope](#contract-scope) |
| Human approval | Every write waits for a person, who sees the full arguments | [Human approval](#human-approval) |
| Secrets in context | Values are never shown; logs and model requests are redacted | [Secrets](#secrets-in-context) |
| Client identifiers | Optional masking before anything reaches the LLM | [Client identifiers](#client-identifiers) |
| Prompt injection | Alerts and tool output are framed as data, and an eval tests it | [Prompt injection](#prompt-injection) |
| Blast radius | Limits on tool calls, model calls, output size and parallel triages | [Blast radius](#blast-radius) |
| Auth | Two tokens with different powers | [Auth](#auth) |
| Audit | Every tool call logged and counted | [Audit](#audit) |

## Cluster access

- Each cluster in a Contract is either `in_cluster: true` or names its `context` explicitly.
- The agent never falls back to your current kubectl context.
- A kubeconfig credential plugin (an `exec` entry) is refused unless the cluster sets `allow_exec`.

## RBAC

- A read-only ClusterRole, bound **per namespace** with RoleBindings.
- No Secrets, no ConfigMaps.
- CI checks this by impersonating the ServiceAccount ([`demo/rbac-check.sh`](../demo/rbac-check.sh)).

## Write scope

- Write tools are opt-in per Contract: the kubernetes integration's `enable_actions`.
- When it is off, the write tools aren't registered at all.
- When it is on, they cover only:
  - `pods/eviction`, which respects PodDisruptionBudgets;
  - `deployments/scale`.
- There is deliberately no `patch deployments`: it would allow changing images and commands.

## The Actuator

Opt-in per cluster (`actuator_token_secret`). It runs in the client's cluster with its own ServiceAccount and token, never the Relay's.

### What it may change

- **Resize pods** (`pods/resize`): requests, and a limit only as a Reflex ([ADR 0005](adr/0005-reflexes-may-raise-limits.md)). Never images or commands.
- **Evict a pod whose resize is Infeasible** (`pods/eviction`, through the Eviction API so PodDisruptionBudgets hold). Only when all of these hold:
  - the Contract has `actuator: eviction` on;
  - it is inside the Contract's eviction windows;
  - one pod at a time, 5 minutes apart;
  - the pod isn't a StatefulSet's, DaemonSet's or Argo Rollout's;
  - the pod isn't in a Deployment with one replica, or one that no PodDisruptionBudget covers.
- **Create Events** and **keep one ConfigMap of its own**, bound per namespace.
- Nothing else.

### What it reads and reports

- Its reads are the Contract's namespaces, with three exceptions a client may each remove:
  - **One Role in `default`** (`actuator.nodeEvents` in the chart): Events there, where a Node's are. It tells a node out of memory from a container at its limit (`reflexes.oom`, which for now only reports OOM kills).
  - **One ClusterRole listing nodes** (`squero-actuator-nodes`). It sends only the values of the label keys its pods' nodeSelector and node affinity name, for the Hub's [nodes that could go](optimizer.md#nodes-that-could-go).
  - **One Role reading its own pod** in its own namespace (`squero-actuator-self`, `get pods`). It sends that pod's facts as it does any pod's.
- It reports the facts of pods that may block a node's removal ([Blockers](client-cluster.md#blockers-what-the-actuator-reports)): names, volumes' names and three annotations. It sends them when they change, and at least hourly.

### What it applies, and when it stops

- Only what the Hub approved: one Sizing at a time, or under Automation. Automation is a Sizing rule with `automate: true`, the Operator's standing approval in the contracts repo ([ADR 0004](adr/0004-standing-approval-for-sizing.md)).
- Never a request above the container's limit.
- Nothing while the Kill switch is on.
- Nothing on a Workload a person paused (`squero-actuator/pause: "true"`).
- A CronJob's pods are never resized in place. Only its runs' new pods get the held requests, from the webhook.
- Three Rollbacks of a Workload in 7 days end its Automation.

### How the Hub treats its reports

An Actuator's reports are data, as a client's alerts are:

- stored only for its own cluster's namespaces (plus its own pod, `itself`, from its own namespace);
- refused when a number isn't a number;
- dated no later than when they arrived;
- shown in Slack as text: no mentions, no links;
- at most 20 Rollbacks of one cluster posted in an hour.

### Revert

The Operator's Revert (`revert_sizing`) puts a container's pods back to their template's requests and stops its Automation until the next approval. It is refused while the Kill switch is on.

### Its pod-creation webhook

- Fails open (`failurePolicy: Ignore`, 2 seconds): a slow or broken Actuator never blocks a pod.
- Sees only the Contract's namespaces.
- Never touches `kube-*` or its own namespace.
- **Placement** (when the Contract turns it on for a cluster): new pods of the Workloads the Hub lists get a *preferred* node affinity for the Pinned nodes and a toleration of their named taint. Never a requirement, never a running pod ([ADR 0006](adr/0006-placement-is-a-preference.md)).
- **Spot** (a cluster's spot nodes plus a Spot ratio): it steers new replicas of the Workloads the Hub lists.
  - Most replicas get a preference for spot nodes.
  - The replicas the On-demand minimum keeps get the one requirement it ever adds, "not on a spot node", with a label, a deletion cost of 1 and the spot taint's toleration.
  - Never a running pod.
- **CronJobs under `spot: true`**: a run prefers spot nodes on its first tries, and is required off them once the run has a failed pod, only where its Jobs allow a retry.

## Contract scope

- Every tool checks its `cluster` and `namespace` against its own Contract server-side, on top of RBAC.
- Each Contract's alerts arrive on their own URL with their own token ([Contracts](contracts.md)).
- The Hub's own Prometheus reads for Sizings name the Contract's namespaces in every query.
- **One exception:** a node's Room ([Nodes](optimizer.md#nodes)) needs every pod on the node. Pods outside the Contract's namespaces are read as one sum and one count per node:
  - added up by Prometheus, never listed by name or namespace;
  - not read at all from a Prometheus another Contract reads without cluster labels.

## Human approval

- The Hub keeps its own list of what each tool does ([ADR 0002](adr/0002-hub-and-relay.md)). A tool not on the list is hidden from the agent.
- `HumanInTheLoopMiddleware` interrupts before:
  - every tool the list marks as a write, **and**
  - every tool whose server doesn't claim `readOnlyHint`.
- A server's annotation can only make a tool stricter. A server that mislabels a write as read-only still waits for approval.
- The reviewer sees the full arguments.

## Secrets in context

- Environment variable *values* are never returned, only their names.
- Logs and events are redacted: tokens, JWTs, keys, `password=`.
- So is every request to the model, alert labels and Questions included.

## Client identifiers

A Contract's Data policy can mask its hostnames, names, emails and IPs. The LLM provider sees placeholders; everyone else sees the real values ([Masking](contracts.md#masking)).

## Prompt injection

- Alerts and tool output are framed as data, never as instructions.
- An eval Scenario plants an instruction in pod logs. It fails if the agent even *attempts* a write.

## Blast radius

- Per-run limits on tool calls and model calls.
- The model sees at most `SQUERO_TOOL_OUTPUT_LIMIT` characters of any tool result at once. A larger result is kept, redacted, in that Investigation's state, and `read_more` pages or filters it.
- The receiver runs a bounded number of triages at once.

## Auth

Two tokens:

| Token | Can do |
|---|---|
| Alertmanager's | Submit alerts, nothing else |
| The approver token | Read Reports and decide on Proposed actions |

## Audit

- Every tool call is logged as one JSON line and counted in `squero_tool_calls_total{tool,status}`.
- A triage's Executed actions are recorded where the tool ran, never taken from the model's own account.
- An approved call that failed is marked `FAILED`.

## Hints versus boundaries

MCP tool annotations are *hints* from the server. The security boundary is:

1. RBAC;
2. the namespace allowlist;
3. tool registration, done on the server side;
4. the Hub's own read/write list.
