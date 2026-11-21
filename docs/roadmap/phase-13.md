# Phase 13: Reflexes

**In short:** A Reflex is a fast, bounded change the Actuator makes on its own. It raises a memory limit after an OOM kill, lifts a CPU limit for a throttled container, or boosts CPU at startup. Each one expires unless the Hub renews it. Reflexes may raise limits; Sizings never do ([ADR 0005](../adr/0005-reflexes-may-raise-limits.md)).

**Status:** built. Slices 13.1a to 13.4 are built. They are checked live on kind, except the parts listed as not checked; 13.3a was checked read-only.

**Goal:** a container that is OOM-killed gets more memory within a minute, and the extra memory expires on schedule. Throttled and starting containers get a temporary CPU limit, which also expires.

**Done when:** an OOM-killed pod's next replica gets more memory within a minute, and the step-up expires on schedule.

| Slice | What | Status |
|---|---|---|
| 13.1a | Attribution and reporting of OOM kills, no change | built 2026-11-12, checked live 2026-11-12 (container-limit half) |
| 13.2 | Node-level versus container-limit attribution | built inside 13.1a, from the source; node-level not seen live |
| 13.1b | Step-up: the memory limit and request go up | built 2026-11-12, checked live 2026-11-12 |
| 13.1c | Expiry, Slack, Automation page, Triage line | built 2026-11-12, checked live 2026-11-12 (Actuator side) |
| 13.4 | Startup CPU boost | built 2026-11-12, checked live 2026-11-13 |
| 13.3a | Burst: the Hub's hourly throttling read | built 2026-11-13, checked read-only 2026-11-13 |
| 13.3b | Burst in the Actuator | built 2026-11-13, checked live 2026-11-13 |

## Decisions (grill, 2026-11-12)

Three rounds. All answers were recommended, except burst CPU: the Operator chose to build it on the Hub's side rather than defer it.

- **Every Reflex.**
  - Only for Workloads under Automation (`automate: true`). Never under the Kill switch, on a paused Workload, or for a Workload under a VPA in any mode but `Off`. Never an eviction.
  - Switched on per Contract, refused without a cluster that runs an Actuator. Absent means off:

    ```yaml
    actuator:
      reflexes:
        oom: {max: 2.0, expires: 7d}
        burst: {max: 2.0, expires: 24h}
    ```

  - The startup boost is set by a factor on a Sizing rule: `startup_boost: 2`, from 1.5 to 4, refused without `automate`.
  - Step-ups and bursts together: at most 10 a cluster an hour. The rest are said once and not done.
  - Raising a limit is new: `pods/resize` already allows it. [ADR 0005](../adr/0005-reflexes-may-raise-limits.md) records the rule.
- **OOM step-up (13.1).**
  - Limit and request rise together by the same factor, so the QoS class stays. The new value is the larger of x1.2 and +100 MiB (VPA's numbers). Never past `max` times the template's limit.
  - Every pod, in place and at once. A pod whose node has no room keeps its values. New pods get it through the webhook, and a CronJob's next runs too.
  - One step per container every 10 minutes.
  - The Hub lists the cluster's automated containers, so the Actuator also watches those it holds nothing for. The decision is the Actuator's, within a minute.
  - Not while the Hub is unreachable: the Kill switch cannot be heard then. Not when a LimitRange or ResourceQuota would refuse it (skipped and said), or for a container whose memory an HPA scales on ([Phase 14](phase-14.md) keeps this rule).
  - At its cap and still OOM-killed: no further step, no Rollback, Automation goes on, and one Slack line a day: "still OOM-killed at 2x its limit: raise it in its manifest or look for a leak".
- **Attribution (13.2).**
  - An OOM kill with a kubelet `SystemOOM` Event on its node around then is node-level. Any other OOM kill of a container with a memory limit is container-limit. The pod reads `OOMKilled`, exit 137, in both.
  - Container-limit: a step-up, and no Rollback. The kill must not count toward the three that end Automation.
  - Node-level: a Rollback if the Actuator had lowered that container's memory request, otherwise only said.
  - Events are read in `default`, where Node Events are. Without that read, every OOM kill of a limited container counts as container-limit. With the Reflex off, as today.
- **Expiry.**
  - After `expires`, unless the Hub renews it. The Hub renews while the container's measured memory peak is above its template's limit, so a step-up never brings a kill on purpose.
  - New pods start from the template. Running pods are lowered in place where Kubernetes allows: 1.33 refuses to lower a memory limit in place; 1.34 tries, and leaves the resize in progress while usage is above the new value.
  - The Reflex's memory wins over a held Sizing's while it lasts. At expiry the container returns to the Sizing's request, or the template's. A Revert drops it with the Sizing. A person changing the template's memory gets the container back.
- **Startup CPU boost (13.4).**
  - Only the CPU limit changes, times the rule's factor, at pod creation. It goes back when the pod is Ready or 5 minutes after it started, whichever is first. No boost on a container's restart.
  - Not for a Guaranteed pod, a container with no CPU limit, or a CronJob.
  - Requests are untouched, so no pod is made unschedulable and no HPA or Cluster Autoscaler is misled. The pitfalls in kube-startup-cpu-boost come from raising requests.
  - Goes on while the Hub is unreachable. A JVM sizes its thread pools from the boosted CPU count and keeps them; the guide says so.
- **Burst CPU (13.3), on the Hub's side.**
  - The Actuator cannot see throttling. Prometheus, or the kubelet through `nodes/proxy`, would be too wide a permission.
  - Hourly, the Hub reads throttling. An automated container with a CPU limit, not Guaranteed, throttled in more than 25% of its periods gets its CPU limit raised by 25% of the template's limit, one step at a time, at most `max` times the template's limit. Every pod in place; new pods through the webhook. Requests untouched; CronJobs excluded.
  - Expires after `expires` unless the Hub renews it, which it does while the CPU peak stays above 90% of the template's limit.
- **Seen.**
  - A step-up and a burst each give an Event on the Workload, an Actuation on the Automation page (where Revert works), and one Slack line saying what to change in the manifest, under [12.9c](phase-12.md#129c-the-hubs-side-of-the-reports)'s cap. Their expiry is an Actuation, with no Slack.
  - A startup boost gives an Event on the pod only, at every start.
  - A Triage Report links a Reflex through a line the Hub adds to the Report's Slack thread. The model never sees it, so no eval is rerun.

**Terms** (see [`CONTEXT.md`](../../CONTEXT.md)): *Reflex*, *Step-up*, *Burst*, *Startup boost*.

**Looked up before the grill (2026-11-12):** VPA's OOM bump (`--oom-bump-up-ratio` 1.2, `--oom-min-bump-up-bytes` 100 MiB); kube-startup-cpu-boost, which raises requests and limits by a percentage and reverts on Ready or after a fixed time. No primary source was found for how the kubelet's `SystemOOM` Event reads on the pod. To confirm on kind.

## Attribution: the gap that was resolved

**Resolved 2026-11-12**, the Operator's "resolve it", after 13.1a, for 13.1b to build.

- The facts (kubelet v1.34.0, containerd v2.0.0, cadvisor v0.52.1, the kernel's cgroup v2 document): a kill at the node's or a pod's memory limit is a cgroup's kill, so the kubelet emits no `SystemOOM`. containerd marks it `OOMKilled` either way. Nothing on the Pod or in an Event tells it from a kill at the container's own limit. Nor does `container_oom_events_total`, which cadvisor counts on the killed task's cgroup; only the container's usage would tell, and Prometheus has it by samples.
- Decision: a kill with no `SystemOOM`, of a container with a limit, still gets the step-up. *Why:* the other way would leave a container at its own limit crash-looping, the case the Reflex exists for.
  - The step also raises the request, since a container killed over its request needs it (its `oom_score_adj` falls as its request rises). Bounded and said.
  - One rule added: while a Sizing that lowered that container's memory request is in its watch hour, an OOM kill is a Rollback, never a step-up. *Why:* a lowered request can bring a kill above the container's limit.
- Not chosen: the Hub confirming from Prometheus that the container was at its limit. A 30-second sample can miss the spike, and the step-up would come late, past the done-when.
- 13.1a's report says what is known, for example: "with a memory limit of 256Mi, its node not out of memory (container-limit)".

## 13.1a Attribution and reporting, no change

**What:** the Hub lists the automated containers. The Actuator sees their OOM kills, attributes them and reports them. It changes nothing.

**Built (2026-11-12):**
- `actuator: reflexes: {oom: {}}` in the Contract (refused without a cluster that runs an Actuator). `max` and `expires` were refused until 13.1b and 13.1c gave them behaviour.
- `/actuator/desired`'s `oom` lists the cluster's containers under Automation (a Sizing ready or not), except a Workload whose Automation ended, a container reverted since its approval, a kind the Actuator didn't name, and anything under the Kill switch. An Actuator of 0.46.0 or older ignores the field.
- Each minute the Actuator reads each listed namespace's pods and takes the OOM kills of the last 10 minutes, running again or ended for good. A CronJob's run counts.
- Attribution: node-level when the container has no memory limit, or its node has a `SystemOOM` Event within 2 minutes. The Events are read in `default`, through a Role in `deploy/actuator/rbac.yaml`, which ships by default (a client may remove it). Otherwise container-limit, including where the Events cannot be read; the report says so, and it is logged once.
- One Actuation `oom_killed` per container (migration 0031): limit, up to five pods, each pod's kind, with its pods killed since its last report (a pod shows its last kill only, so pods are counted, not kills). At most one every 10 minutes per container, and 50 containers a minute. None while reports the Hub has not taken are waiting, so sightings cannot push a Rollback's report out of the 200 kept across a restart. The last-report time is in memory only, so a restart repeats the report once, stored once; a report with a newer kill is a second row.
- Nothing changes in the cluster. An OOM kill in a watch hour is still a Rollback. The Hub stores the sighting and tells no one; the Automation page and Revert ignore it, and a container only ever seen has no row.

**Known limits:**
- The pause annotation is not read for a listed Workload that nothing is held for. 13.1b had to read it before acting.
- A kill the runtime reports as `Error`, exit 137, is not seen. No Event on the Workload.
- Until 13.1c, a sighting is visible only in the Actuator's log and the `actuations` table.
- A kill at a limit above the container's own is container-limit too; nothing the Actuator reads can tell the two apart.

**Tests:** isolation (two Contracts each get only their own containers; one without the Reflex gets none). 28 mutations, all caught.

**Checked live 2026-11-12**, container-limit half only: a throwaway kind cluster (Kubernetes 1.37, one worker, the Actuator on the control plane), a stand-in Hub, no model calls, no cloud.
- Deployment `leaky` (2 pods, limit 64Mi) got one report in the first minute the Actuator could list its pods, both container-limit. `other`, killed as often but not listed, got nothing.
- No report the minute after, though the kills went on; every pod stayed at its 50m and 32Mi. No `SystemOOM` Event appeared in `default`, as the source says for a kill at a cgroup's limit.
- With the RoleBinding deleted, the report came within seconds of the Actuator's restart, both pods read container-limit. The API server's refusal to list Events was then said once per report, not once per pod. No other report came in the two minutes after.
- Not checked live: node-level (a `SystemOOM` on kind means exhausting the Docker VM that the Operator's Hub runs in), and the Hub's side (unit-tested).

## 13.1b Step-up

**What:** a container-limit OOM kill is answered by a Step-up. The memory limit and request go up on every pod, in place and at once.

**Built (2026-11-12):**
- `reflexes.oom.max` in the Contract: 2, more than 1, at most 4. Sent as `oom_max`; an Actuator given none only reports.
- The memory limit goes to the larger of 1.2 times and 100 MiB more, in whole Mi. The request goes up by the same factor (equal ones stay equal). Never past `max` times the template's limit.
- Applied to every pod in place and at once. One pod's refusal leaves the others; a lost pod is untouched.
- One Step-up per container every 10 minutes, and 10 per cluster an hour, kept in `state.json`.
- Only a kill of a pod at the current step earns the next. A pod below it is brought up to it. A kill from before a step every pod has is only said.
- Not made, and said with the kill: a paused Workload; a VPA, or an HPA on memory; a `resizePolicy` that restarts for memory; no memory limit in the template; a LimitRange or ResourceQuota (`limits.memory` too).
- The Sizing beside it: the step starts from the larger of the template's request and a Sizing's, never a lowered one. The hold gives up its memory and keeps its cpu. No Sizing sets that container's memory while the step-up lasts (its Actuation says so; a Sizing that asks only for memory is skipped, once). A Revert drops it.
- Rollback interplay: an OOM-killed pod is left to the Reflex when it looks this minute (the Hub answered with `oom_max`, the container is listed, and no report is waiting), unless the Sizing lowered its memory request (a template with only a limit asks that much; a test found it). A node-level kill rolls back a hold that lowered memory, at any time. A kill answered by a Rollback earns no step. Where the Reflex does not look, a Rollback is as before. A crash that is not an OOM kill stays the Sizing's.
- New pods start at the step from the webhook, when the pod asks what the template asked then, with its QoS class kept. Not under the Kill switch or when paused. A DaemonSet's pod is raised in place the minute after, as [12.8a](phase-12.md#128a-statefulsets-and-daemonsets) does. Each minute, pods below their step are raised. A step whose template memory a person changed, or whose Workload is gone, is handed back and said.
- A CronJob's step-up is for its next runs (their parallelism counted against a quota); an OOM kill in its watched first run is the Reflex's, on the same terms.
- Pinned by review: only a kill since the pod got its requests is the Reflex's; a pod is at a step by the limit its status says it was given; a pod is only ever raised; a resize a node cannot fit stays pending on 1.33 and 1.34, is put back the minute after and left for good (otherwise a later Sizing's rollout would evict it: a Reflex never causes an eviction); a step-up's end has its own outcome, `step_ended`, so it does not remove the container's Revert button while a Sizing's cpu is held.
- Outcome `stepped_up` (migration 0032), Event `MemorySteppedUp`.

**Not built here, moved to 13.1c:** expiry, the Slack line, the Automation page, and lowering running pods at a Revert, a hand-back or when the Reflex is switched off. Until then, the Reflex should not run on a real cluster.

**Tests:** the Hub's part is one number beside 13.1a's list, tested there with two Contracts. 89 mutations, all caught.

**Checked live 2026-11-12**, on kind (Kubernetes 1.37, two workers, the Actuator on the control plane), a stand-in Hub sending `oom_max` 2. No model calls, no cloud.
- **The step:** Deployment `leaky` and DaemonSet `agent` were stepped within the minute, from 128Mi to 228Mi and request 64Mi to 114Mi: "2 of 2 pods in place". On the roomy worker the kubelet gave both pods 228Mi and 114Mi in place, with no restart but the kill's own.
- **No room:** on the full worker both resizes stayed `PodResizePending`. This version refuses at once only what a node could never fit. The minute after, both pods went back to 128Mi and 64Mi, and were not tried again while still being killed. This is Deferred.
- **New pods:** the Deployment's replacement started at 228Mi and 114Mi from the webhook. The DaemonSet's replacement started at 128Mi and was raised in place within the minute.
- Kept: both steps, the hour's two, and the left pods were in `state.json`.
- Found: a left pod's later kill was said as "before its last Step-up". It is now said for what it is.

**Known limits:**
- Not checked live (unit-tested): a further step and the cap, a CronJob's runs, a Guaranteed pod, a resize the API server refuses at once, the interplay with a Sizing's Rollback.

## 13.1c Expiry, Slack, Automation page and Triage line

**Built 2026-11-12.**

- **The term:** `reflexes.oom.expires`, 7 days by default, from 1 hour to 90 days. Sent as `oom_expires`, with each container's memory peak over its Sizing's window (`peak`).
  - It runs from the last step or the last renewal. A step-up is kept another term while its peak is above its template's limit, or the peak is unknown.
  - It ends (`step_ended`, Event `StepUpExpired`) when the peak is within the limit, or the Hub no longer lists the container.
  - Without a term from the Hub, a step-up never expires.
- **Its pods** go back to the template's memory in place only where the peak says they fit, so no resize hangs above what a pod uses and none is killed. Otherwise they wait until they restart. A 1.33 refusal is said pod by pod. A Revert does the same.
- **Slack:** a step-up made, and a container still OOM-killed at its cap (once a day), go through 12.9c's teller, cap and retries. Pods only brought up, a kill only seen, and an expiry are not posted. A step-up after three Rollbacks does not say Automation ended again.
- **The Automation page:** a row's step-up limit. A container with only a step-up gets a row. Revert is offered and accepted. Another cluster's or Contract's step-ups are never shown.
- **A Triage's thread:** one line above the Report for each step-up made from an hour before the Triage began, on a Workload its Alerts name (a kind's label, or a `pod` label beginning with the Workload's name and a dash). The Hub adds it, so no eval is rerun. Console types were regenerated.
- Pinned by review: a step-up reported without its limit is still said; a renewal is `stepped_up`, not told, so the page still knows a step-up older than the 90 days Actuations are kept; a stored Sizing's `memory.basis` is the peak the term reads.
- Choices the grill left open, made here: a peak not known keeps a step-up. A container no longer listed ends at its term, without its pods being touched.
- Fixed 2026-11-13 (found building 13.3b): with `reflexes.oom` switched off the Hub sends no `oom_expires`, and the Actuator read the term from that alone, so a Step-up never ended, though the guide said it did. Its term is now kept with each Step-up, as a Burst's is; one that never heard a term still never expires. 4 mutations, all caught.

**Known limits:**
- A step-up brought back in place is tried once. A line is said again if a Triage ends twice.
- Not checked live: a peak above the limit renewing, a 1.33 refusal, and the Hub's side (unit-tested). Reports made up for a check must not reach the Operator's Hub.

**Tests:** 43 mutations, all caught.

**Checked live 2026-11-12**, Actuator side only: a throwaway kind cluster (Kubernetes 1.37, one worker), a stand-in Hub sending `oom_max` 2 and a term of 150 seconds. No model calls, no cloud.
- **A Revert** of `b`, with the Hub no longer listing it: its pod went back to 128Mi and 64Mi in place, decided by the peak of the minute before (100Mi).
- **The term:** `a`'s peak was unknown, so it was kept and said. `c`, no longer listed, ended and its pod was left at 228Mi. When `a`'s peak was 100Mi, it ended at its next term: "2 of 2 pods back to memory limit 128Mi, request 64Mi in place". Neither pod restarted.
- One pod's resize showed in progress for some seconds, then both were given 128Mi. Neither restarted.
- Found: a term of minutes was said as "0 hours". It is now said in days, hours or minutes.

## 13.4 Startup CPU boost

**Built 2026-11-12.** The glossary's *Startup boost* comes first.

- `startup_boost` on a Sizing rule, from 1.5 to 4. Refused without `automate`.
- `/actuator/desired`'s `boosts` lists the automated containers whose rule sets a factor (never a CronJob's), each Contract's own. Nothing under the Kill switch.
- The webhook (`boosted`) multiplies a new pod's CPU limit and writes the container's own limit into the annotation `squero-actuator/startup-boost`.
- No boost for a Guaranteed pod (including one Guaranteed by its limits alone), a container with no CPU limit, a CronJob's run, a paused Workload, anything under the Kill switch, or a pod already carrying the annotation.
- Each minute, Hub reachable or not, a boosted container gets its own limit back in place when its pod is Ready, when it has run for 5 minutes, or when it restarted. The limit comes back up to a minute after the pod is Ready (the Actuator's interval). Event `StartupBoostEnded` is raised on the pod, with its UID. No Actuation, and nothing the model sees.

**Chosen beyond the grill**, each as the safer reading:
- The 5 minutes count from the container's own start, not the pod's. `status.startTime` predates the image pull and init containers.
- A container that restarts while boosted loses the boost at once. This also bounds a crash loop that never gets Ready.
- No boost for a pod with pod-level resources (`spec.resources`), whose limit a container's may not pass. A Revert of a container, or Automation ended by three Rollbacks, stops its boost too (the Hub lists only what is under Automation now).
- No boost in a namespace whose LimitRange sets a CPU maximum or limit-to-request ratio, or whose ResourceQuota counts `limits.cpu`. The whole namespace, whatever room it leaves, is skipped, said once in the log.
- No boost for a container whose `resizePolicy` restarts it for CPU. Giving the limit back would restart it.
- A VPA is read each minute, not at creation. The webhook has 2 seconds and reads nothing.
- A container with a CPU limit and no request gets that limit written as its request, so the request cannot follow the boost.
- The annotation is read as anyone's word. It can only lower a limit to it, never under the request.
- Under the Kill switch or a pause, a boosted pod keeps its higher limit.
- A namespace is read one minute more after its last boost was wanted, then until no pod in it still carries a boost, so a rule removed mid-start still gets its limit back.

**Tests:** 69 mutations, all caught. Five survived first, each given its own test.

**Checked live 2026-11-13**, a throwaway kind cluster (Kubernetes 1.37, one worker), a stand-in Hub sending `boosts` with factor 2. No model calls, no cloud.
- **At creation:** pods with cpu 100m under a limit of 200m started under 400m, annotated `{"app": "200m"}`. A container with a limit and no request started with request 200m under 400m. A Guaranteed pod, a CronJob's runs, and pods in the LimitRange and ResourceQuota namespaces kept their own limit.
- **Given back:** pods Ready after 90 seconds had their limit returned at the next minute, with no restart. A pod never Ready 5 minutes after its start was given back at 5 minutes. A container that exited after 25 seconds was given back at its first restart.
- **The Hub stopped:** a new pod was boosted and given back, and the restarted Actuator boosted the next from its ConfigMap.
- **The Kill switch:** a new pod started at its own limit. The pod boosted before kept 400m for two minutes, and got 200m back when the switch went off.
- **The rule removed** while a pod was boosted: its limit came back when it was Ready, the namespace then left the Actuator's state, and the next pod was not boosted.
- No report reached the Hub, and there was no error in the Actuator's log.

**Known limits:**
- Not checked live: a pause, a VPA, pod-level resources, a `resizePolicy` that restarts for CPU, and whether the API server's defaulting or the webhook wrote a limit-only container's request. The result is the same either way.

## 13.3a Burst: the Hub's hourly read

**Built 2026-11-13.** The glossary's *Burst* comes first.

- `actuator: reflexes: {burst: {expires: 24h}}` in the Contract: 1 hour to 7 days. Refused without an Actuator cluster. `max` waits for 13.3b.
- The hourly Sizing run also reads each container's share of CFS periods throttled over the last hour, and its CPU peak over the last `expires`. Both are stored in the row's `burst`.
- `/actuator/desired` gets `burst`: `throttled` (the share, only when over a quarter), `peak`, and `burst_run`. It excludes CronJobs and containers without the Reflex or under the Kill switch.
- The Actuator reads them and does nothing yet.

**Chosen beyond the grill:**
- The trigger is the hour before the read, not the policy's window. Over 7 or 14 days, a container throttled once last week would be stepped to its cap, an hour at a time.
- A container with fewer than 600 CFS periods in that hour is not told.
- A read more than two hours old tells no Burst and no peak.
- The peak covers the last `expires`, so 7 days at most. `burst_run` makes one read count as one Burst.
- The Actuator, not the Hub, compares the peak with 90% of the template's limit. The Hub only sees the pods' limit, which a Burst has raised.

**Known, and said in the guide:** a Workload's pods are pooled, so one hot pod among many reads low. The peak is of 5-minute rates, so a spiky container's Burst would end and be made again.

**For 13.3b:** two runs can be under an hour apart (`squero sizing refresh`), so the Actuator keeps to one Burst a container an hour. `Throttled`'s four required fields are a commitment once an Actuator with it is released, so 13.3b may only add optional fields.

**Tests:** 34 mutations, all caught.

**Checked read-only 2026-11-13.** The hourly read ran against a throwaway kind cluster's Prometheus (Kubernetes 1.37, kube-prometheus-stack 92.11.0, the lab's workloads and three more with a CPU limit), with a synthetic Contract setting `reflexes.burst`. No Hub, database, Actuator or model calls.
- The one-hour expression returns a series only for containers with a CPU limit. One without has no CFS periods, so it is never told.
- `throttled` (wants 0.5 cores under 200m): 82% of periods throttled. Told, peak 0.19 cores.
- `spiky-capped` (a minute at 0.9 cores every 15, under 200m): 3.6%. Kept, not told.
- `blip` (3 seconds of a full core every 5 minutes, under 100m): 92% throttled, but held back by the floor (about 400 periods an hour). Its peak read 0.001 cores: the 5-minute rate hides the spike.
- Seen: the floor counts a Workload's pods together, so two replicas of `blip` would pass it. This is in the guide.
- A container that only sleeps had no periods.
- Every row was still learning, with minutes of history, and carried its numbers all the same.

**Known limits:**
- Not checked live: the stored row and `/actuator/desired` (unit tests only), a Workload of several pods, a read through a Relay.

## 13.3b Burst in the Actuator

**Built 2026-11-13.** The last slice of Phase 13.

- `reflexes.burst.max` in the Contract: 2, more than 1, at most 4. Sent as `burst_max`, with `burst_expires`. An Actuator given no `burst_max` makes no Burst and reports none.

**The step** (`burst`), once per read, for a listed container the read says is throttled:
- its CPU limit goes up by a quarter of the template's limit, in whole millicores, never past `max` times the template's limit;
- applied to every live pod in place, only ever raised; requests untouched;
- kept in `state.json` before any pod is touched; undone and not counted if every pod refused.

**Not made**, each said as an Actuation `throttled` with its reason (a standing reason once a day): no CPU limit in the template; a paused Workload; a Guaranteed pod; a `resizePolicy` that restarts for CPU; a VPA; a Sizing still rolling out; a LimitRange's maximum or ratio; a ResourceQuota that counts `limits.cpu`; its cap or the hour's budget; no live pod; a CronJob.

**New pods** start at the Burst's limit from the webhook. A Startup boost goes on top, and is given back to the Burst's limit.

**Each minute** (`keep_bursts`):
- a pod below the Burst is brought up to it;
- the Burst is dropped, touching no pod, when a person changed the template's CPU limit or the Workload is gone;
- at its term it is kept while the Hub's peak is above 90% of the template's limit, or unknown. Otherwise it ends and every pod above the template's limit goes back in place (`burst_back`);
- a Revert drops it and lowers its pods.

**The Hub's side:** migration 0033, outcomes `burst`, `burst_ended` and `throttled`. One Slack line when a Burst is made, saying what to set in the manifest, and one a day for a container still throttled at its cap (its own day count). The Automation page shows and reverts a Burst's CPU limit. A Triage's thread names a Burst. Nothing the model sees.

**Chosen beyond the grill:**
- A container's Bursts are at least 45 minutes apart on the Actuator's clock. A full hour would decline every other read, since a step lands up to a minute after its read.
- The last read handled per container is kept across a restart, a declined one too. The term is kept with each Burst, so one still ends after the Reflex is switched off. A Burst that ends is not made again from the read it handled.
- At most 5 of the cluster's 10 an hour are Bursts. OOM kills are looked at first.
- No Burst in a namespace whose ResourceQuota counts `limits.cpu`: an HPA answering the same load makes pods that each ask the Burst's limit.
- Declined while a Sizing of that container rolls out, and tried again each minute of that read.
- Reads handled are forgotten by read, not by listing, so a Revert followed by an approval within one read makes no second Burst.
- An HPA on CPU does not decline it, since requests are untouched.
- The 50 a minute count containers looked at, not reports made.

**Tests:** 120 mutations, all caught.

**Checked live 2026-11-13**, the Actuator's side: a throwaway kind cluster (Kubernetes 1.37, one worker), a stand-in Hub sending `burst` with `burst_max` 2 and a term of 3 minutes. No model calls, no cloud, no Prometheus; the shares and peaks were the stand-in's.
- **The step:** `hot` (two pods, cpu 100m under 200m) and `warm` both went to 250m in place in the first minute, with no restart. An Event `CpuBurst` was raised on each. A Guaranteed pod, a container with no CPU limit, and one in a ResourceQuota namespace each got a `throttled` report, said once.
- **New pods:** `warm` has a Startup boost of 2. Its replacement started at 500m and was given back to 250m when Ready.
- **A second read** a minute later said "No Burst: burst 1 minutes ago: one a container every 45 minutes", and the standing reasons were not said again.
- **The term:** after 3 minutes `hot` (peak 50m) ended and both pods went back to 200m in place (`BurstExpired`). `warm` (peak 190m, above 90% of 200m) was kept for another term.
- **A Revert** of `warm` dropped its Burst and put its pod back to 200m.
- **A template limit set by hand** to the 250m the report named dropped a new Burst ("its template's CPU limit changed: 200m to 250m", `HandedBack`). The rollout's pods went to their template's 250m, and no new Burst came from that read.
- The ConfigMap held the Bursts and the reads handled. There was no error in the Actuator's log.

**Known limits:**
- Not checked live: the Hub's side (migration 0033, the Slack lines, the Automation page, the Triage line), which is unit-tested only. Also the Kill switch, a pause, a LimitRange, the hour's budget, and the whole path from real throttling in Prometheus to a Burst. 13.3a's read and this Actuator check were each checked on their own.
