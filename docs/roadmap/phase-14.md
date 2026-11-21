# Phase 14: Horizontal

**In short:** The Hub works out how few replicas a Workload needed, hour by hour, and recommends a minimum for its autoscaler. It also builds a weekly profile and tests it with a backtest. Nothing in this phase writes to an HPA or a ScaledObject. The client's GitOps applies every change.

**Status:** built, with one live check owed. 14.1a, 14.1b, 14.1c, 14.2, 14.3 and 14.4 are built. The backtest's live check (14.4) waits for four stored weeks without gaps, due 12 December.

**Goal:** recommend HPA and KEDA minimum replicas from what each Workload needed, and show a weekly profile only when a backtest says it predicts the load.

**Done when:** backtests pre-scale before a synthetic Monday peak, and a request cut does not change HPA replica counts.

| Slice | What | Status |
|---|---|---|
| [14.1a](#141a-hpas-and-each-workloads-need) | HPAs read from kube-state-metrics; each Workload's hourly Need stored | built and checked live 2026-11-13 |
| [14.3](#143-sizings-held-by-an-hpa) | Sizings held by an HPA, with the `AverageValue` change | built and checked live 2026-11-13 |
| [14.1b](#141b-the-floor) | The Floor: its saving, its risks, the console and the digest | built 2026-11-13, checked live 2026-11-14 |
| [14.1c](#141c-realized-saving-once-the-minimum-is-lowered) | Realized saving once the HPA's minimum is lowered | built 2026-11-13, checked live 2026-11-14 |
| [14.2](#142-weekly-profile) | Weekly profile, `sizing.timezone`, `sizing.lead_time`, KEDA `cron` triggers | built and checked live 2026-11-14 |
| [14.4](#144-backtest-with-an-error-budget) | Backtest with an error budget | built 2026-11-14, not checked live |

## Decisions (grill, 2026-11-12)

Three rounds, all recommended. The later grills (14.1b, 14.1c, 14.2, 14.4) are in their slices.

- **Read-only.**
  - The Hub recommends; the client's GitOps applies. Nothing writes to an HPA or a ScaledObject, and no permission is added.
  - *Why:* Argo CD with self-heal, and Flux, both put a patched HPA back unless told to ignore the field.
  - Only Workloads with an HPA or a KEDA ScaledObject are considered. A fixed replica count gets nothing yet.
  - Everything is read from Prometheus, through kube-state-metrics' HPA series. KEDA is read through the HPA it creates (`keda-hpa-<name>`), since KEDA has no metric for a ScaledObject's minimum, maximum or target.
  - Writing a minimum is Deferred. It needs its own grill and an ADR, as a new Actuator write.

- **Need.**
  - The HPA's "desired replicas" never reads below its minimum, so the Hub computes what a Workload needed: current replicas x current value / target value.
  - This covers any metric the HPA scales on (resource, per-container, pods, object, external). With several, the largest counts.
  - Unknown where series are missing, for a Workload with two HPAs, or for a per-container metric on kube-state-metrics older than v2.19.0 (no `container` label).
  - The Hub stores each Workload's peak Need per hour, in one table kept 8 weeks, filled first from what Prometheus still has.
  - *Why:* a weekly profile needs more weeks than most Prometheus keeps.

- **Floor (14.1).**
  - A low percentile of the hourly Need over the policy's window: the 10th under `production`, the 25th under `high-availability`, a policy setting. None under `off`.
  - Never below 2, unless today's minimum already is. A minimum of 0 (KEDA's scale to zero) is left alone.
  - Shown against today's minimum: a lower Floor is a saving, a higher one a risk.
  - The saving is its own line: replica-hours removed, priced at the Sizing's requests. It adds to Waste without counting a replica twice. It is available only, and realized once the HPA's minimum is lowered (14.1c).
  - One risk line when the Need was above the HPA's maximum ("needed more than its maximum in 6% of hours"). No maximum is recommended.

- **Requests under an HPA (14.3).**
  - A Sizing of a request an HPA divides by (`Utilization`, cpu or memory) is marked "held by its HPA". Until the change below, the Hub shows it as available and the Actuator skips it.
  - The change that frees it: the target as `AverageValue`, with the same threshold as an absolute number. That is the target percentage times the pod's summed requests, or one container's requests for a per-container metric.
  - After it, requests play no part in the HPA and the Actuator resizes freely. It only ever skipped `Utilization` targets.
  - For KEDA's HPA, the same change goes on the ScaledObject's trigger (`metricType: AverageValue`). A wrong guess about whose HPA it is shows only the wrong snippet.
  - Its Waste still counts. One manifest line recovers it.
  - The [Phase 13](phase-13.md) rule stays: no OOM step-up for a container whose memory an HPA scales on. The Actuator never patches a target percentage.

- **Weekly profile (14.2).**
  - For each hour of the week, the median of that hour's Need across the stored weeks. One odd week changes nothing. Never below the Floor.
  - Shown as blocks of hours with one Floor each, in `sizing.timezone` (UTC by default). Each block starts `sizing.lead_time` early (15 minutes by default).
  - Measuring the lead time per Workload, from its pods' time to Ready, can come later.
  - For a KEDA Workload, a ready `cron` trigger is given per block, to paste into its ScaledObject. KEDA takes the trigger asking for most replicas.
  - A plain HPA has no schedule of its own, so it gets the profile with a note saying so. No generated CronJobs that patch `minReplicas`.

- **Backtest (14.4).**
  - The newest full week is replayed hour by hour, against a profile built from the weeks before it: all those kept, three to seven. *Changed 2026-11-14* from "the three before it". So a backtest needs 4 stored weeks.
  - The error is the replica-hours off, too few plus too many, as a share of the replica-hours needed. Both parts are shown, with the cost of the too many.
  - Budget: 20%, fixed. It becomes a setting once real backtests show what is normal.
  - Over budget, the Workload shows "load isn't weekly enough" and keeps only its Floor.
  - A gap under 10% is nothing the HPA acts on. That matches its tolerance, both ways.

- **Order.** 14.3 is built early, because it frees savings on autoscaled Workloads at once. The lab's `weekly` load generator starts in 14.1a, for 14.4's live check four weeks on.
- **Seen.** The console's Sizing views (the Floor beside each autoscaled Workload, its profile, its backtest, and the manifest change to copy), and one line in the weekly digest. The Triage agent's sizing tool is unchanged, so no eval rerun.

- **Terms.** *Floor*: the minimum replicas the Hub computes for a Workload with an autoscaler. *Weekly profile*: its Floors by hour of the week. A Sizing stays per replica.

**Looked up before the grill (2026-11-12, Kubernetes v1.34, kube-state-metrics source, KEDA, Argo CD and Flux docs):**
- Utilization is the pods' total usage over their total requests. A pod's request is the sum over its containers, sidecars included. One container without a request fails it.
- `AverageValue` reads no requests and compares the pods' mean usage. A target above 100% is valid. The tolerance is 0.1 both ways.
- The series: `kube_horizontalpodautoscaler_spec_min_replicas`, `_spec_max_replicas`, `_status_current_replicas`, `_status_desired_replicas` and `_status_target_metric` (stable); `_info` and `_spec_target_metric` (experimental, to confirm on the lab). The last two cover all five metric kinds, with `metric_name`, `metric_target_type` (`value`, `utilization` or `average`) and `container`.
- Not found in a primary source: whether KEDA puts back a direct edit of its HPA.

## 14.1a HPAs and each Workload's Need

**What:** `usage.needs` reads each cluster's HPAs from kube-state-metrics, and the hourly run stores each Workload's Need. Nothing is shown and no API changed.

**Built (2026-11-13):**
- The four series are read in the Contract's namespaces and with its labels, like every read. Through a Relay they go as they are, as the Hub's own PromQL.
- Each Workload's Need is stored as every UTC hour's peak, in table `needs` (migration 0034), kept 8 weeks.
- The lab gets `weekly`: a Deployment under an HPA (CPU 50%, 2 to 5 replicas), whose load follows the wall clock and is split between its pods. Over the week it needs 1, 3 and 7 replicas. metrics-server is added to `make lab`.

**Changed from the grill, confirmed 2026-11-13:**
- With no replicas the Need is 0, not unknown. Nothing ran and nothing was asked. A scale-to-zero Workload's profile gets its idle hours.
- The replicas counted are the smaller of a step's and the step before's, never across a gap. *Why:* the HPA reports a scale-up's new count at once, with the utilization of the pods that already have metrics (`replica_calculator.go`). So 3 replicas at 83% going to 5 read as needing 8, in the same hours every week, and a profile's median would keep that. This does not help while new pods take longer than a step to report.
- A per-container metric on kube-state-metrics before 2.19 is not unknown. Its current value still pairs with its own target.
- **Known limit:** on such a version, an HPA with two per-container metrics of one resource reads as one metric.

**Chosen beyond the grill:**
- The rules hold per step. Two HPAs count only while both exist. An HPA moved to another Workload counts for each in its own time. If one of several targets has no current value, the step is unknown, never a guess. A target of zero is unknown.
- The first read goes 8 weeks back, at a 9-minute step. Each later read starts from the newest hour stored, at a 1-minute step, so a Hub that was down catches up.
- An hour read twice keeps the larger. A failed read is logged and never fails the Sizing run.
- A cluster without an HPA costs one query an hour. One whose HPAs never give a Need is read 8 weeks back each hour (marked in the code).
- Samples are instants, so a peak between two samples is missed.

**Tests:** isolation tested with both demo Contracts on one Prometheus; 45 mutations, each caught.

**Checked live (2026-11-13, on the lab; kube-state-metrics 2.20.0, Kubernetes 1.37, metrics-server 0.9.0):**
- The HPA held 2 pods at 25% to 26% of their request, against 50%. The pods count their peers' load.
- The read gave a Need of 1.00 to 1.04 at every step, below the HPA's minimum of 2, where the HPA's own desired replicas read 2.
- All four series were present. The read takes a resource metric's `utilization` or `average` series, matching the target's type. The minute before the HPA had a utilization gave no Need.
- One stored run (throwaway database, no Hub, no model calls) applied migration 0034, and the next read started at that hour.
- The Mac Hub (0.47.0) reads no Needs before the next release.

**Not checked yet** (the load was at its base until Monday 08:00 UTC): a Need between the minimum and the maximum, one above the maximum (Mondays 09:00 to 12:00), and the hour of a scale-up.

## 14.3 Sizings held by an HPA

**What:** `usage.scaled` reads each cluster's HPA `Utilization` targets on cpu or memory. `sizing.rows` marks a ready Sizing that would change such a request as `held`, with the manifest change to make.

**Built (2026-11-13):**
- Targets are read as they are at the run. The selectors are plain, so a switched target is gone at the next run.
- A held row names the HPA and its percentage, the same threshold as an absolute value, and the change as text: a plain HPA's `target`, or for `keda-hpa-<name>` the ScaledObject's trigger, in KEDA 2.21's form (`metricType` beside `type`, `metadata.value` as a quoted quantity, `metadata.containerName`).
- Shown by `sizing show`, the console's container detail and the Hub MCP's `sizings`. Stored in the run's rows. Waste, risks and the Actuator are untouched. No digest line: the phase's one comes with 14.1b.

**Chosen beyond the grill:**
- Only a Sizing that changes the request is held. A kept one has nothing to free.
- A per-container metric holds its container alone. A pod-wide metric holds every container of the pod. The sum takes every container's request, learning and excluded ones included.
- A `container` label naming no container of the Workload comes from the scrape target (kube-state-metrics scraped without `honor_labels`, or before 2.19). The metric then reads as the whole pod's, the known wrong form the grill named.
- The comment above the change names the requests the value came from, and says to apply it before any request changes.
- With no request to go by, the Sizing is held with no value. Two HPAs on a Workload show the first, by name. A failed read marks nothing.

**Tests:** isolation tested with both demo Contracts on one Prometheus; 32 mutations, each caught. The Hub MCP's `sizing` shares the Triage agent's renderer, and a test holds that the agent's tool gives the same for a held row.

**Checked live (2026-11-13), with no Hub, no database and no model calls:**
- On the lab, read-only, with `weekly` on a one-hour-history policy: its CPU Sizing (100m to 30m) read as held by its HPA at 50%, shown as `averageValue: 50m` (50% of the pod's 100m), its Waste still counted.
- The change applied to the HPA as printed. The next read, 90 seconds later, held nothing, since the `utilization` series was gone from Prometheus at once. The Need still read 1.04 against the `AverageValue` target, and the replica count stayed at 2. The HPA was put back, and the request read as held again.
- On a throwaway kind cluster (Kubernetes 1.37, metrics-server, a stand-in Hub), `weekly` at three times the load held 3 pods at 50m each. An approved Sizing of 60m was skipped ("cpu left to its HPA, which scales on its utilization"). After the printed change was applied, a newer run resized 3 of 3 pods in place to 60m (the canary's 10 minutes, then one a minute), with no restart. The HPA then read 3 replicas and 3 desired, at 49m to 52m a pod, in all 46 samples 20 seconds apart, from the first resize to 6 minutes after the last, with no rescale event.

**Known limits:** not checked live: KEDA's form on a ScaledObject, a per-container metric, and the console page against a real Hub.

## 14.1b The Floor

**What:** `sizing.floors` computes each autoscaled Workload's Floor from its stored Needs, with its saving and two risks. The Floor shows in the console, `sizing show`, the Hub MCP and the digest.

**Decided (grill, 2026-11-13, all recommended):**
- The percentile of the Needs is rounded up, as the HPA rounds. So 1.04 is 2.
- With containers under different policies, the highest percentile decides. A Workload with every container `off` gets no Floor.
- The setting is `replicas: {percentile}`, beside `cpu` and `memory`:

| Policy | `replicas.percentile` (range 0 to 0.5) |
|---|---|
| `production`, `batch` | 0.10 |
| `high-availability` | 0.25 |

- A first Floor needs the policy's `history` of stored Needs. The hours it goes by are shown.
- The saving, per stored hour: the minimum less the larger of the Floor and that hour's Need (rounded up), when positive, averaged. Multiplied by the pod's requests at their Sizings (a container without one counts its request). Shown per Workload and as one Contract total, in the console and the digest. Not yet in the Cost page's groups.

**Built (2026-11-13):**
- `Policy.replicas.percentile`. `usage.bounds` reads each HPA's minimum and maximum as they are at the run.
- Floors are stored per cluster in table `floors` (migration 0035). Each run replaces the last. An HPA gone takes its Floor with it. A failed read keeps the last.
- Shown by `sizing show` (which reads the Needs Prometheus still has, having no database), the console's Workloads page (Autoscaled Workloads, with the Contract's total), the Hub MCP's `sizings` (`floors`, masked and filtered like the rest), and one digest line. The digest line is said only when a minimum can come down or is below its Floor.
- The Triage agent's tool reads stored rows, not this, so no eval rerun.

**Changed from the grill, confirmed 2026-11-13:**
- KEDA gives the HPA of a ScaledObject with `minReplicaCount: 0` a minimum of 1 (`GetHPAMinReplicas`), and it scales to zero itself. So "a minimum of 0 is left alone" never shows for KEDA.
- So a Workload whose HPA had 0 replicas at some time in the Contract's longest policy window is taken to scale to zero, and is left alone. That needs one more read each run: `min_over_time` of `_status_current_replicas`.
- A Need of 0 is no such sign. An idle Workload held at its minimum stores one too.
- The grill's "minimum of history is covered time, by 9.7's helper" does not fit a Need ([Phase 9](phase-09.md)). A missing hour is unknown, not idle, and no replicas is stored as 0. So only the Workload's own stored hours count, each as an hour. The hour under way is never counted, since its peak is partial. A gap counts for nothing.

**Chosen beyond the grill:**
- The window and history are those of the policy whose percentile decides. A Floor looks back 8 weeks at most, and asks for 4 weeks of history at most.
- Two HPAs, or an unknown minimum, give no Floor, and the reason is given. A Floor equal to the minimum says nothing.
- The risks belong to the Floor, not the Workload.

**Tests:** isolation tested with both demo Contracts on one Prometheus; 68 mutations, each caught. Merging `replicas` into its base's is an equivalent mutant while the setting has one field.

**Checked live (2026-11-14, on the lab; no Hub, no model calls; `weekly` on a one-hour-history policy):**
- Its HPA's minimum of 2 and maximum of 5 were read. Its replicas were never at 0.
- From 5 complete hours of Needs (1.04 each), its Floor was 2, equal to its minimum: no saving, no risk, no digest line.
- One stored run (throwaway database, migration 0035) gave the same Floor back, with its run's time.
- With the HPA's minimum raised to 3 (the HPA went to 3 pods), the Floor was 2 against a minimum of 3. The saving read "1 replica fewer on average: 0.03 cores, 0.06 GiB", priced at the pod's Sizing of 30m, not its 100m request. The digest read `Floors: 1 can lower their HPA minimum (1.0 replicas fewer on average, 0.03 cores, 0.06 GiB).`
- With the minimum back at 2, the saving was gone at the next read.

**Known limits:** not checked live: the two risk lines (the one above the maximum waits for Monday 09:00 UTC), a priced Contract, a Workload that scales to zero, and the console page against a real Hub.

## 14.1c Realized saving once the minimum is lowered

**What:** `sizing.realize_floors` records each drop in table `floor_drops`, and `sizing.savings` adds its worth to the Realized Savings totals.

**Decided (grill, 2026-11-13, all recommended):**
- A drop is an HPA's minimum read lower than at the previous run, while that run's Floor was below the old minimum. A request drop needs a Sizing that it followed.
- It counts down to that Floor, and no further.
- Its worth, per stored hour since the drop: the old minimum less the larger of what it counts down to and that hour's Need (rounded up), when positive. Averaged over the last 7 days, and multiplied by the pod's requests as they are now. The available saving is at the Sizings, so it adds to Waste: this is what a removed replica stops reserving.
- Priced at today's prices as a monthly rate, accrued as that rate times the hours since.
- A minimum raised again takes the drop back, as a rise does a request drop.
- An HPA or Workload gone, or a second HPA, ends the drop. A failed read ends nothing.
- It counts inside the Realized Savings totals (the Cost page, `sizing savings`, the Hub MCP's `savings`, the digest's *Realized this week*), listed apart from request drops. No backfill.

**Built (2026-11-13, as grilled):** `sizing.realize_floors` runs in the hourly run, against the Floors the last run stored, before they are replaced (table `floor_drops`, migration 0036). `sizing.savings` lists them as `floors`, and `sizing savings`, the Cost page's realized line and the API show them.

**Chosen beyond the grill:**
- A rise takes the newest drop first and never lowers what a drop counted. A minimum going from 2 back to its Floor of 3 changes nothing.
- The hour a drop was seen in counts once complete, never the hour under way. A drop is worth nothing until an hour has passed since it was stored.
- An unknown minimum ends nothing. A Workload whose rows are gone is not shown.

**Tests:** isolation tested: one Contract's drops are never read, raised or ended by another's run. 31 mutations, each caught.

**Checked live (2026-11-14, on the lab; no Hub, no model calls; three stored runs in a throwaway database, migration 0036):**
- With the minimum raised to 3, the first run stored a Floor of 2 under it: 1 replica, 0.03 cores available.
- With the minimum back at 2, the second run stored one drop (from 3, counted down to 2). It was worth nothing yet.
- After that hour was complete, the third run read it as 1 replica fewer on average: 0.10 cores and 0.06 GiB, at the pod's real requests (100m, 64Mi). The digest read `Realized this week: 0.10 cores, 0.06 GiB`.
- The lab ended as it was: minimum 2, 2 replicas.

**Known limits:** not checked live: a rise taking a drop back, a drop stopping at a Floor above the new minimum, a priced Contract, and the Cost page against a real Hub.

## 14.2 Weekly profile

**What:** `sizing.profile` builds each Workload's weekly profile from its stored Needs. `sizing.scheduled` gives KEDA's HPA a `cron` trigger per block.

**Decided (grill, 2026-11-14, all recommended):**
- A first profile needs 3 weeks of the Workload's own stored Needs: the fewest where a median ignores one odd week. The backtest adds its verdict at 4 weeks.
- Consecutive hours asking the same are one block. Hours at the Floor are the baseline and are not listed.
- A block starts `lead_time` early when it asks more than the hour before it, and ends on the hour. No smoothing.
- A week equal to its Floor in every hour says "no weekly pattern".
- Shown in the console under the Floor, in `sizing show` and the Hub MCP's `sizings`. No digest line.

| Setting | Default | What it does |
|---|---|---|
| `sizing.timezone` | UTC | IANA name per Contract; blocks are shown in it |
| `sizing.lead_time` | 15 minutes | How early a block starts; 0 to 2 hours |

**Built (2026-11-14, as grilled):** `sizing.profile` computes it from all 8 weeks of Needs kept, not the policy's window, and `sizing.floors` stores it on the Floor. `sizing.scheduled` gives KEDA's HPA a `cron` trigger per block, in KEDA 2.21's form (`timezone`, `start` and `end` as 5-field cron expressions that must differ, `desiredReplicas` quoted). A plain HPA gets a note instead.

**Chosen beyond the grill:**
- One trigger per block, with a single weekday, Sunday as 0. The docs do not say whether the weekday field takes lists or ranges.
- The median of an even count of weeks is the mean of the middle two, rounded up.
- An hour stored in fewer than 3 weeks is at the Floor. The three weeks are counted in hours: with gaps, an hour could rest on one week, which would break "one odd week changes nothing". Put to the Operator and confirmed 2026-11-14.
- A week whose every hour asks the same above its Floor is flat too. One block the whole week would need a trigger whose start equals its end, which KEDA refuses.
- The lead time is whole minutes, because cron has no seconds. In a zone on the half hour, blocks start and end on it. Blocks are listed by when they start in the week.

No eval reruns: the Triage agent's tool does not read Floors.

**Tests:** 40 mutations, each caught.

**Checked live (2026-11-14; the Needs were synthetic, since the lab has only a day of real ones; no Hub, no model calls):**
- **Hub side:** four synthetic weeks of `weekly`'s load pattern went into a throwaway database, and one stored run read the lab's Prometheus. With its real HPA (minimum 2, maximum 5) the Floor was 2, with `needed more than its maximum of 5 in 2% of hours`. The profile was the seven blocks expected: Monday 08:45 to 12:00 at 7, weekdays 07:45 to 18:00 at 3. It carried the plain-HPA note.
- **KEDA side:** on a throwaway kind cluster with KEDA 2.21.0, a ScaledObject with all 10 triggers of a generated profile (UTC, a 45-minute lead, a Sunday block as day 0) was accepted, went Ready, and made its HPA.
  - Inside a block of 4, the Deployment held 4 replicas.
  - 23 seconds after that block ended at 04:00, it was back at its minimum of 2 (a 30-second scale-down window was set for the check).
  - At 04:15:10, the early start of the block beginning at 05:00, it went to 6.

**Known limits:** not checked live: a profile from real stored weeks (the lab has them from 4 December if it runs without gaps), a zone other than UTC on KEDA, and the console page against a real Hub.

## 14.4 Backtest with an error budget

**What:** `sizing.backtest` replays the newest week against the profile built from the weeks before it. `sizing.floors` stores the verdict on the Floor's profile.

**Decided (grill, 2026-11-14, all recommended):**
- In an hour, the Workload needed the larger of its Floor and that hour's Need, rounded up: what its HPA would run with the Floor as its minimum. So the profile is judged only on what it adds above the Floor.
- The profile under test is built from all the stored weeks before the newest one (three to seven).
- Over budget, the blocks and triggers are hidden, and one line shows instead, for example: "load isn't weekly enough: off by 34% last week (too few 21%, too many 13%); it keeps its Floor". Under budget, the same figures show beside the profile, with the monthly cost of the too many, priced at the Sizings like the Floor's saving.
- Before four weeks, the three-week profile keeps showing, marked "not backtested yet (needs four weeks)".
- Grilled earlier, 2026-11-12: the error and its two parts, the 20% budget and the 10% tolerance.

**Built (2026-11-14, as grilled):**
- `sizing.backtest` replays the newest 168 complete hours against `sizing._slots` (the profile's own hours of the week, now shared), using the Needs kept before them.
- `sizing.floors` stores the verdict as `backtest` on the profile: `waiting`, `within` or `over`, with `error`, `few`, `many`, the hours replayed, and the too many at the Sizings.
- Over budget, the profile is stored as `irregular` with no blocks, and `sizing.scheduled` is never called. The rule sits at the source, not in each renderer.
- `floors_report` prices the too many with a priced Contract. That cost stays outside `floors_total`.
- Shown by `sizing show`, the API (`Backtest` on `Profile`), the console and the Hub MCP's `sizings`. The Triage agent's `sizing` tool is unchanged, so no eval rerun.
- No migration: Floors are stored as JSON. A Floor stored by 14.2 shows without a backtest until the next hourly run replaces it.

**Chosen beyond the grill:**
- The newest week is the 168 complete hours before the run, not a calendar week.
- A verdict needs 504 stored hours before that week and 84 in it (half). An hour never stored is skipped, never replayed as idle.
- The 10% tolerance is per hour, against what that hour needed, in whole numbers. So it applies only from 11 replicas up. The lab (2 to 5) never exercises it.
- The lead time plays no part in an hour-by-hour replay. An error of exactly 20% is within the budget.
- The Floor on both sides is today's, which has seen the newest week. The Floor is not what is under test.
- A flat profile, or one still learning, has no backtest. The too many's cost is stored over the budget too, but shown only within it.

**Tests:** 52 mutations. 51 were killed after four tests were sharpened. One equivalent mutant: the tolerance as `0.1 * needed` in floats, equal for every multiple of ten.

**Known limits:**
- One replayed week. One unusual newest week fails a good profile until the next one.
- The verdict says how the weeks before predicted the newest. It does not say how the shown profile will do.

**Live check owed:** once the lab has four stored weeks without gaps (12 December).
- The lab's `weekly` repeats exactly and has a plain HPA, so that check can show a profile within its budget only, with a note.
- The over-budget line and the hidden KEDA triggers need a week broken on purpose (the load generator's pattern changed for the newest week), or stay covered by tests only.

## The lab moves to a host that stays up

**Decided (2026-11-14):** the Mac's sleeps leave gaps in its history, and 14.4 needs four weeks without them. So the lab moves to a host that stays up.

**Built, step 1 (2026-11-14):**
- `make lab-remote` creates a kind cluster `lab` on another host over ssh. Its API server and Prometheus are on one private address only. Its node is capped at 2.5 CPUs and 6 GiB, and restarts with Docker.
- `make lab LAB=small` installs the lab for a 4-CPU host (`demo/lab/small`): the full lab asks 7.9 to 8.2 CPUs of its node with Kubernetes' own, the small one 3.3 to 3.6, the big Workloads asking and burning a quarter to a third. Every ratio of request to load is kept except `idle`'s (100 to 25). `weekly` and all memory are unchanged.
- No address or host name is in the repository.

**Built, steps 2 to 6 (2026-11-14),** on a mini PC of the Operator's (4 cores, 15 GiB, Debian, running his own services):
- All 29 pods ran, none Pending. The node used 0.8 of its 2.5-core cap and 3.6 of 6 GiB, with 3.07 CPUs requested of 4. The host kept 9 GiB available, at a load of 1.1.
- `weekly`'s HPA read 26% of 50% at 2 replicas, as on the Mac. The hourly read took a second and gave `weekly` a Need of 1.04.
- The Hub reads it with a read-only kubeconfig (`demo/lab/hub-read.yaml`): the shipped role in namespace `lab` only. Secrets, ConfigMaps, writes and other namespaces are refused. It is kept in the Contract's SOPS file; demo-shop has it as its lab cluster, beside `kind`.

**Built, the Hub moved (2026-11-14, 0.48.0)**, because its battery cost on the Mac was the reason:
- Its compose folder, settings, age key and contracts clone were copied. The database was restored from a final dump, with every table's row count equal.
- The alert and Actuator ports are on the private address only. The console and the MCP endpoint are served by `tailscale serve`, on their own port. Contract updates use a read-only deploy key.
- The first run failed with `Prometheus unreachable: timed out`: a container cannot reach another's published port through the host's own address. The fix: the Hub joins kind's Docker network, and demo-shop names the node (`lab-control-plane`) in its Prometheus URL and in the read-only kubeconfig's server. The next run stored the lab's 10 containers, and the Hub listed the lab's pods and was refused its secrets.
- demo-shop's `kind` cluster on the Mac (the Triage demo) is no longer a cluster of the Hub. Its move is its own step. Then the Hub and that cluster on the Mac were stopped, their data kept as the way back; only the test Postgres of the unit suite runs there.

**Built, the rest (2026-11-14):**
- Step 7: the Mac Hub (0.48.0) read both clusters with no error at 05:55 UTC, storing the lab cluster's 10 containers and `weekly`'s Need for the 05:00 hour (1.04).
- Step 8: the lab's Workloads and namespace were deleted from the Mac's cluster, whose node went from 1.17 cores to 0.25. Its `demo` and `orders` namespaces and its Prometheus stay.
- The Triage demo followed (`make demo CLUSTER=lab`), with its five Workloads broken as designed. demo-shop's lab cluster gained `demo`, `orders` and the two GitOps namespaces.
- demo-bank had named a cluster on the Mac and never had data. It is now a real second Contract on the same cluster and Prometheus: a Workload `ledger` in namespace `bank` (`demo/lab/bank.yaml`), and an account bound there only (`demo/lab/hub-read-demo.yaml`), with its actions switched off, since the account is read-only.

**Checked:**
- Each Contract's run stored its own namespaces only: demo-shop `lab`, `demo` and `orders`; demo-bank `bank`.
- `kubectl auth can-i` and the Hub itself were refused the other Contract's pods (403). The Hub MCP listed both, with nothing stale.
- A Question through the Hub MCP checked the moved demo end to end, with one model call. Asked why `checkout` crash loops, the agent read the rollout's change, the pod's log and its restarts, and answered that the rollout had removed `DATABASE_URL`, citing two earlier Reports of the same.

**Known limits:** not checked: a Triage from an alert.

**To watch:** the node's memory against its cap as Prometheus fills, and the host's low inotify limit.
