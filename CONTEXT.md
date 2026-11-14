# Alert triage

An agent investigates alerts, questions and scheduled checks for several Contracts, and backs every conclusion with evidence. It changes a Contract's systems only after the Operator approves: each change, or, under Automation, a Sizing policy once ([ADR 0004](docs/adr/0004-standing-approval-for-sizing.md)).

## Language

### Contracts

**Contract**:
One client engagement the agent works for: its clusters and integrations, its credentials, its Runbooks, its Data policy and where its Reports go. Nothing from one Contract (Alerts, tool results, Reports, knowledge) is ever visible while working on another.
_Avoid_: Client, tenant, customer

**Operator**:
The person who runs the agent across all Contracts. The Operator receives every Report and makes every Approval decision. If a Contract has a client channel, the Operator can send a Client report there, one Report at a time.
_Avoid_: User, admin

**Data policy**:
A Contract's rules on what may be sent to the LLM. A Contract has no restrictions unless its Data policy says otherwise.

**Integration**:
One outside system a Contract turns on, such as its clusters, Prometheus or Runbooks. Its tools are what the agent can use while working for that Contract.
_Avoid_: Toolset, plugin

**Hub**:
The one always-on service that runs every Investigation for every Contract. It receives alerts and serves the Operator in Slack and the web console.
_Avoid_: Server, backend

**Hub MCP server**:
The Operator's own MCP endpoint on the Hub, for an MCP client such as Claude Code on the Operator's devices. Each call is about one Contract and follows that Contract's Data policy. The Investigation agent never gets its tools.
_Avoid_: Operator API, admin MCP

**Agenda**:
What the Operator should look at now in one Contract, ranked without a model in a fixed order of urgency: the Kill switch while it is on, pauses waiting for an Approval decision (soonest deadline first), failing Checks, data the Hub can't read (oldest first; a Relay that is offline counts), Bills outside their tolerance (biggest gap first), running Investigations, and the three Workloads whose waste costs most. Across Contracts it gives only the counts.
_Avoid_: Inbox, to-do, dashboard

**Proposed change**:
A pull request the Hub opens on the contracts repo, from the Hub MCP server, editing one Contract's Sizing policy (a rule, a policy, the default) and nothing else. It is checked against the Contract the Hub loaded and validated before it opens, never touches `main`, and changes nothing until the Operator merges it and the Hub loads it again.
_Avoid_: Proposed action (that is a paused tool call), patch

**Direct**:
A way to reach a Contract's cluster or integration where the Hub holds the credentials and calls its APIs itself. The default; a cluster reached through a Relay is never reached Direct.

**Relay**:
A small process inside a client's network that serves one of a Contract's clusters (the cluster is reached `via: relay`): it holds that cluster's credentials and runs its Kubernetes tools and its Prometheus queries, for the agent and for the Hub's own reads. It connects out to the Hub with a token bound to that Contract and cluster, so the client network accepts no inbound connections. The agent's reasoning, the allowlist and the approval gate stay in the Hub; cloud and vendor integrations stay Direct. Only tool calls and their results pass through the Relay. A Run that calls a Relay that isn't connected, or doesn't answer within the call timeout, ends Failed ("Relay offline"): its Report would otherwise read as if the cluster had been seen. The Actuator has its own token and connection, so the Relay never holds write access.
_Avoid_: Satellite, agent

### Input

**Alert**:
One firing instance of an alerting rule, identified by its labels.
_Avoid_: Using "alert" for a whole notification

**Alert group**:
One Alertmanager notification, holding one or more Alerts that Alertmanager grouped together. The Alert group is the unit that gets triaged. It belongs to exactly one Contract, so two Contracts never share an Alert group. A re-sent Alert group starts a new Triage only when an Alert not seen before starts firing. A group that shrinks, or comes back unchanged, is a duplicate.
_Avoid_: Notification, alert batch

**Related alert group**:
An Alert group that joined an open Triage of the same Contract instead of starting its own. It joins when the Triage's own Alert group still fires, the Triage started within the window (30 minutes by default) and did not fail, the two share a match label (such as `service` or `app`), and no scope label (`cluster`, `namespace`) differs. It is posted in the Triage's thread and costs no investigation. The Operator can still have it triaged separately: it then becomes that new Triage's own Alert group, and the two groups don't join each other again within the window. Only the Triage's own Alert group makes its Proposed actions Stale.
_Avoid_: Incident, duplicate (a duplicate is a re-sent Alert group with nothing new)

**Alert severity**:
The severity label the rule author set on an Alert. The agent never changes it.

### Investigation

**Investigation**:
Work the agent does for one Contract. It is one of three kinds: a Triage, a Question or a Check.

**Triage**:
The Investigation of one Alert group, from receipt to final Report, including any pauses for approval.
_Avoid_: Using "triage" for a single agent invocation

**Question**:
An Investigation the Operator asks for in their own words, in Slack or the web console. A Question only reads: it never proposes an action. A follow-up in the same thread, or on its console page, continues the same Question; each follow-up is kept with the Answer it replaced, so the Question's whole exchange can be shown.

**Check**:
An Investigation that runs on a schedule or after a deploy, without an alert. The Operator writes what healthy means in the Check's prompt, and the agent's verdict is pass or fail. A passing Check is recorded quietly. A failing Check produces a Report and notifies the Operator like a Triage does, including Proposed actions. A Check whose Run fails without a verdict counts as failing.

**Open failure**:
A Check's failure that the Operator has been told about and that hasn't passed since. Each later failing Run of that Check replies *still failing* in its thread instead of starting a new one, unless it needs an Approval decision. The first passing Run replies *recovered* and ends it.

**Deploy verification**:
A Check that the Hub starts by itself after a deploy it sees in a Contract's cluster (a rollout, an ArgoCD sync, a Flux release), once a delay has passed, asking whether what was deployed is healthy and whether anything else in that namespace started failing since. Deploys in one namespace close together are verified in one Run. Opt-in per Contract. It has no Open failure: each deploy is verified once.

**Run**:
One uninterrupted stretch of agent work inside an Investigation. An Investigation without a Proposed action has one Run. Each Approval decision or Stale rejection starts another Run.

**Report**:
The structured result of a Triage or a failing Check: summary, Category, root cause, Evidence, Assessed severity and Recommendations.
_Avoid_: Triage (for the output)

**Client report**:
The copy of a Report that goes to the Contract's client channel, only when the Operator clicks to send it. It holds the summary, root cause, Assessed severity, what is affected and the Recommendations. It never holds Evidence, Executed actions, the Runbook used, or any approval control.
_Avoid_: Client delivery (for the message)

**Answer**:
The result of a Question: a direct reply backed by Evidence, with no Category or Assessed severity. A Question that finds a fault still ends with an Answer, which says so.

**Failed investigation**:
An Investigation that ended without its Report or Answer because the agent could not finish. The Operator is told explicitly. Nothing partial is delivered.
_Avoid_: Failed triage

**Evidence**:
A finding from a tool result that supports a conclusion. A claim in a Report or an Answer without Evidence is not allowed.

**Change**:
Something that modified a Contract's systems before an alert: a rollout, a Helm release, a GitOps sync or a cloud activity log entry. The agent looks for Changes across the Contract's allowed scope, because the cause is often a Change to a different resource than the one alerting.
_Avoid_: Deploy (for a Change that isn't one)

**Runbook**:
A written procedure mapped to one or more alert names. One alert name can map to several Runbooks. A Runbook belongs to one Contract, or is part of Shared knowledge.

**Category**:
The class of the root cause, not the failure mode (the alert name already says how it fails). When a rollout introduced the fault after an earlier revision that worked, the Category is `bad_rollout`, even if the fault itself is bad config or a bad image, and even when the rollout was of another workload the alerting one depends on. A workload broken since its first revision has no rollout to blame.
Values: `bad_rollout`, `bad_config` (wrong or missing config, command or probe), `app_error` (a bug in the code, including memory leaks), `resource_limits` (limits too low for a healthy workload), `image_unavailable`, `capacity` (the cluster can't fit the workload), `node_problem`, `dependency_failure`, `unknown`.

**Assessed severity**:
The agent's judgment of actual impact, backed by evidence. It can disagree with the Alert severity.

### Acting

**Recommendation**:
A next step written for a human. It is advice and never runs.
_Avoid_: Action, recommended action

**Proposed action**:
A tool call that would change a Contract's systems, paused until the Operator approves or rejects it.
_Avoid_: Pending action, write

**Stale**:
A Proposed action is Stale once its Alert group resolves, or when no Approval decision arrives within the decision timeout. A Stale action is rejected automatically, and the Investigation still finishes.

**Approval decision**:
The Operator's approve or reject, covering every Proposed action in one pause together. A rejection can carry a reason, and the agent can propose a narrower action in the next Run.

**Executed action**:
A Proposed action that the Operator approved and that the system then ran. The system records it from the tool calls that actually ran, marked as failed when the call errored. The agent never reports it.
_Avoid_: Action taken

**Kill switch**:
A Contract's stop for every change to its systems. While it is on, approving a Proposed action is refused (rejecting still works), and a change already approved is refused where it would run. The Operator turns it on from the Hub MCP server or the console, and only the console turns it off. Under Automation it also stops the Actuator ([ADR 0004](docs/adr/0004-standing-approval-for-sizing.md)).
_Avoid_: Pause, freeze

### Knowledge

**Similar incident**:
A past Report from the same Contract that the agent finds, by its words or by meaning, and uses as a lead. It never comes from another Contract.

**Feedback**:
The Operator's verdict on a finished Triage Report: right, or wrong with a correction. A correction, like a reason the Operator typed when rejecting a Proposed action, makes the agent propose a Teaching.

**Teaching**:
A lesson for future Investigations of one Contract, at most 500 characters, optionally for some alert names only. The agent proposes it from a correction or a rejection reason; the Operator approves (possibly editing it), rejects it, or writes one by hand. Only an approved Teaching is ever used, and it always stays in its own Contract. One that says what an approved Teaching already says is not proposed; one that contradicts an approved Teaching retires it when approved. The Operator can retire one at any time.
_Avoid_: Memory, learning (for a single lesson)

**Shared knowledge**:
Knowledge the Operator writes that holds no client data, such as general SRE lessons and generic Runbooks. Every Contract can use it. The agent never writes to it, and a file that names a Contract is refused.

### Optimization (planned, Phase 9+)

**Workload**:
What a Sizing is for: a Deployment, StatefulSet, DaemonSet, CronJob, Job or Rollout (or a pod with no owner), found by following each pod's owners. Its pods' usage is pooled per container, never summed, so a Sizing is per replica whatever the replica count.

**Need**:
The replicas a Workload with an autoscaler (an HPA, KEDA's included) needed at a time: its replicas then (those already there a sample before, after a scale-up), times the current value of the metric its HPA scales on over that metric's target, the largest over its metrics. Unlike the HPA's own desired replicas it reads below the HPA's minimum and above its maximum. The Hub keeps each hour's peak for 8 weeks. 0 with no replicas. Unknown where kube-state-metrics has no target or current value for it, and for a Workload with two HPAs.
_Avoid_: Demand, desired replicas (that is the HPA's own figure, never below its minimum)

**Floor**:
The minimum replicas the Hub computes for a Workload with an autoscaler: a low percentile of its hourly Needs over its Sizing policy's window (the 10th under production), rounded up, never below 2 unless the HPA's minimum already is. Shown against the HPA's minimum today: a saving when lower (the replicas it would no longer hold, at their Sizings), a risk when higher. A Workload that scales to zero gets none. Advice for the client's manifests: the Hub never writes an HPA. Once the client lowers the minimum toward a Floor the Hub showed, what it no longer holds is realized Savings, down to that Floor and no further.
_Avoid_: minReplicas (that is the HPA's own setting), baseline

**Weekly profile**:
A Workload's Floors by hour of the week: for each hour, the median of that hour's Need across the stored weeks (three at least), never below its Floor, shown as blocks of hours in the Contract's `sizing.timezone`, each starting `sizing.lead_time` early, and checked by its Backtest. For a KEDA Workload each block comes as a `cron` trigger to paste; an HPA has no schedule of its own.
_Avoid_: schedule (the Hub schedules nothing), forecast

**Backtest**:
The check of a Weekly profile against a week it has not seen: the newest stored week replayed hour by hour against a profile built from the weeks before it (three at least, so four stored weeks in all). In each hour the Workload needed the larger of its Floor and its Need rounded up; the error is the replica-hours the profile was off by, too few plus too many, as a share of the replica-hours needed. Within the budget (20%) the profile shows with its error and what the too many would cost; over it the Workload shows "load isn't weekly enough" and keeps only its Floor. Before four weeks the profile shows as not backtested yet.
_Avoid_: forecast accuracy, validation

**Room**:
What a node has left: its allocatable CPU and memory less the requests of the pods on it, Running or Pending, in every namespace. The pods outside the Contract's namespaces count in it as one sum and one count per node, "not visible": never named, never split by namespace. A node's Room *after its DaemonSets* is its allocatable less only its DaemonSets' pods, the most a pod could ask there: no Sizing asks more than the largest such Room in its cluster (a cordoned node aside), and one that needs more carries the risk "needs a larger node". The cap never lowers a request.
_Avoid_: free capacity, headroom (a Sizing policy's margin over usage)

**Sizing**:
The CPU and memory requests the Hub computes for one container of a Workload from its usage under a Sizing policy, with the risks it can't fix (a limit too low, throttling or OOM kills hiding real demand). Limits are kept. A container with less *history* than its policy needs (a day under production) is still *learning* and has none; its history is the time its cluster's Prometheus has usage for, from the container's first sample to its last in the policy's window, so a gap in Prometheus counts for nothing and a CronJob between its runs still counts. A Sizing whose history falls under what its policy needs (after a long gap) is learning again. Read-only unless the Operator approves it or the Workload is under Automation. A Sizing that would change a request an HPA divides by (a `Utilization` target on cpu or memory) is *held by its HPA*: the Actuator leaves that request alone, since changing it would change the replica count, until the HPA's target is an `AverageValue`, which the Hub gives as the same threshold in absolute terms. Its Waste still counts.
_Avoid_: Recommendation (that is advice for a human), rightsizing recommendation

**Approved Sizing**:
One container's Sizing that the Operator approved from the Hub MCP server, kept with the exact requests approved, for the Actuator to apply. It is current, waiting to be applied, until a later Sizing of that container is stored, which supersedes it whatever its values: one superseded before the Actuator applied it never is. Once applied, the Actuator holds its requests until a later Approved Sizing of that container, Automation or a Revert; a later one that changes only one resource keeps the other's held value. Refused for a container that is learning or has no change, and while the Contract's Kill switch is on. Approving changes nothing in a cluster until the Actuator applies it.
_Avoid_: Applied Sizing, Executed action (nothing ran)

**Sizing policy**:
How Sizings are computed: the percentile, the look-back window, headroom, minimums and maximums, and the limit strategy. Built-ins (production, high-availability, batch) mean the same in every Contract; a Contract's own policies, each a built-in with some settings changed, and the rules that pick one per container are in its `contract.yaml`. The policy `off` gives a container no Sizing.

**Waste**:
The requests above a Workload's Sizings, times its average pods: what the Sizings would free with headroom kept. A container that is learning, needs more, or is held by throttling or an OOM kill has none. Stored hourly with the Sizings and their risks.
_Avoid_: Savings (that is waste priced, Phase 10), over-provisioning (for the number)

**Automation**:
The standing approval for one Workload: the Operator approves its Sizing policy once, in the contracts repo (`automate: true` on the rule that picks the policy, in a Contract whose cluster runs an Actuator), and the Actuator then applies its Sizings without asking: each run's newest, a change under 10% ignored, an increase at once, a decrease at most once a day per container. Three Rollbacks of the Workload in 7 days end it until the oldest of them is 7 days old; the Operator hears of it in Slack. A person stops it on one Workload with the pause annotation. Off by default.
_Avoid_: Auto mode, Proposed action (a Sizing under Automation never pauses)

**Actuator**:
The process in a client cluster that applies approved Sizings, in place first, and reports what it did. It runs only what the Hub approved: every minute it pulls its cluster's desired state from the Hub with its own token, bound to one Contract's cluster, and nothing while the Kill switch is on. What it applied it holds: new pods of that Workload get the same requests until a person changes them in its template. Pods start unchanged when it is down. While the Hub is unreachable it keeps what it holds and changes nothing new. A Workload annotated `squero-actuator/pause: "true"` is left alone, as under the Kill switch. It resizes Deployments, StatefulSets, DaemonSets and Argo Rollouts. A CronJob's pods it never resizes: its webhook gives its next run's pods the held requests, and that first run is watched until it ends (7 days at most), a pod of it Failed or OOM-killed being a Rollback. A Job of no CronJob runs once, so it is never sent.
_Avoid_: Agent, controller (alone)

**Actuation**:
One outcome the Actuator reports for one container of a Workload: applied (every live pod of the Workload has the new requests, allocated by the kubelet; a pod whose node is gone is not counted), skipped, failed, rolled back (see Rollback), or handed back (a person changed the template's requests it held, or the Workload is gone: the Actuator lets go), with the reason and how many pods; or, changing nothing, an OOM kill it saw (`oom_killed`, see OOM kill); or a Step-up it made (`stepped_up`) or that ended (`step_ended`); or a Burst it made (`burst`) or that ended (`burst_ended`), or a container the Hub says is throttled of which it made none (`throttled`). The Hub keeps them as the Actuator's audit trail, each dated by the Actuator's clock but never later than the Hub received it.
_Avoid_: Executed action (that is a Proposed action the Operator approved)

**Rollback**:
The Actuator's own undo of a Sizing that made pods unhealthy within an hour of its resize (a container with its requests restarted or was OOM-killed, or a pod it resized stayed NotReady for 2 minutes; never a pod whose node is gone, which is NotReady or Failed through no Sizing, nor a container its node's own restart started again): every such pod back to its template's requests, in place and at once. Then no new Sizing of that container for 24 hours, doubling with each rollback within 7 days. Not while the Kill switch is on; the Operator hears of it in Slack at once.
_Avoid_: Revert (that is the Operator's undo)

**Revert**:
The Operator's undo of what the Actuator holds for a container, asked from the console's Automation page or the Hub MCP server (`revert_sizing`): its pods go back to their template's requests, in place, and the Actuator stops holding it. Automation of that container stops until the Operator approves one of its Sizings. Refused while the Kill switch is on. It drops the container's Step-up and its Burst with it, and is offered for a container that has only one of those.
_Avoid_: Rollback (that is the Actuator's own undo of a Sizing that made pods unhealthy)

**Eviction window**:
The days and hours, in the Contract's own timezone (its `actuator:` block), when the Actuator may evict a pod whose in-place resize is Infeasible: its node can never fit the new requests (the kubelet says so on Kubernetes 1.33 and 1.34; from 1.35 the API server refuses the resize at once), so that it starts again elsewhere with them. Eviction is off unless the Contract turns it on, and then needs at least one window. It goes through the Eviction API, so PodDisruptionBudgets hold; one pod at a time, at least 5 minutes apart, never another pod to make room, never a StatefulSet's, DaemonSet's or Argo Rollout's pod, never a pod of a Deployment with one replica or that no PodDisruptionBudget covers. Outside a window the rollout waits on that pod. A replacement not Ready within 10 minutes is a Rollback.
_Avoid_: Maintenance window (it gates eviction only; resizes in place go on at any time)

**Reflex**:
A bounded, expiring change the Actuator makes on its own under Automation: memory step-up after an OOM kill, burst CPU headroom, startup CPU boost, rollback of a Sizing that made pods unhealthy.

**OOM kill**:
A container ended by the kernel for want of memory, of two kinds the pod itself doesn't tell apart (`OOMKilled`, exit code 137 in both). *Node-level*: its node ran out of memory, whatever the container's limit (the kubelet says so in a `SystemOOM` Event on the node), or the container has no memory limit. *Container-limit*: any other, of a container with a memory limit: it reached its own limit or, which nothing on a pod or in an Event tells apart, one above it (its pod's, or the node's allocatable memory that all pods share). Where the Contract sets `reflexes.oom`, the Actuator tells which each kill of an automated container was and reports it as an Actuation, changing nothing; the memory step-up acts on it.

**Step-up**:
The Reflex for OOM kills: after a container-limit OOM kill of an automated container, the Actuator raises its memory limit and its memory request together, by one step (to the larger of 1.2 times and 100 MiB more, the same factor for both, so the pod's QoS class stays) on every pod of the Workload in place. Never past `max` times the limit in the Workload's template; one step a container every 10 minutes; a pod killed below the current step gets that step, not a further one. It takes the container's memory from any Sizing held for it, and a Sizing never undoes it. It ends by itself: `expires` after its last step unless the container's memory peak is still above its template's limit; or by a Revert, or a person's change to the template's memory. With the Startup boost and the Burst, the only times the Actuator changes a limit ([ADR 0005](docs/adr/0005-reflexes-may-raise-limits.md)).
_Avoid_: bump, boost (that is the Startup boost)

**Startup boost**:
The Reflex for a slow start: a new pod of an automated container whose Sizing rule sets `startup_boost` starts with its CPU limit times that factor (1.5 to 4), given by the Actuator's webhook at the pod's creation, and gets its own limit back in place once the pod is Ready, the container has run for 5 minutes, or it restarted, whichever is first. The CPU limit only: requests are untouched, so the pod is scheduled, and an HPA or the Cluster Autoscaler reads it, as without the boost. Not for a Guaranteed pod, a container without a CPU limit, or a CronJob's run; a restart never gets one.
_Avoid_: Step-up (that is the memory Reflex for OOM kills), Burst (that is the CPU Reflex for throttling)

**Burst**:
The Reflex for CPU throttling: an automated container with a CPU limit, not Guaranteed, that was throttled in more than a quarter of its CPU periods over the hour before the Hub's hourly read gets its CPU limit raised by a quarter of its template's limit, a step each such read, on every pod in place and on new pods; requests untouched, never a CronJob's. Never past `max` times the limit in the Workload's template; a container's steps at least 45 minutes apart; at most 5 of the cluster's 10 Reflex steps an hour. It ends `expires` after its last step unless the container's CPU peak over that time stays above 90% of its template's limit, its pods then back to that limit in place; or by a Revert, or a person's change to the template's CPU limit. The Hub decides the trigger and reads the peak (the Actuator can't see throttling); the Actuator makes the change.
_Avoid_: Startup boost (that is for a pod's start), Step-up (that is memory, after an OOM kill)

**List price**:
What a cloud charges on demand per hour for an instance type in a region, from its public price list (USD unless the Contract asks for another currency, which only Azure's and GCP's catalogs give; no discounts). A cluster with no typed prices gets its nodes' list prices, split into a CPU and a GiB price by one weight, so they add back up to the nodes' price.

**Bill**:
What the cloud says a cluster's compute cost each UTC day, read from its billing API (AWS Cost Explorer, Azure Cost Management or GCP's billing export in BigQuery, usage before credits, lines carrying the cluster's `billing` tag). The Hub sets it beside the estimate (the cluster's node-hours at its unit prices): within 5% it reconciles, else it is flagged; when fewer than 90% of the window's hours were sampled it gives the gap but no verdict. A Bill never changes a shown cost.

**Savings**:
What Sizings would save (available: the waste's cost) or saved (realized), in the Contract's own unit prices, or in cores and GiB when it has none.

### Evaluation

**Scenario**:
A recorded Alert group or Question, the tool results captured from a real cluster, and the Expectation a correct Triage or Answer must meet.

**Expectation**:
What a passing Triage must contain: an allowed Category, root-cause keywords, a tool-call budget, and the Proposed actions it may make (none, by default). For a Question: keywords in the Answer, the tools its Evidence must cite, and a tool-call budget.
