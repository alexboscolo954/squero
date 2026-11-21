# Phase 8: Checks

**In short:** Phase 8 added Checks. A Check is a scheduled Investigation. It runs on a cron schedule, and it reports only when it fails. A deploy can also start a Check of the new revision.

*Why it matters:* a Check runs on a schedule, so the Hub can look between alerts. A passing Check is recorded quietly.

**Status:** built (2 slices).

| Slice | What | Status |
|---|---|---|
| [8.1](#81-scheduled-checks) | Scheduled Checks | ✅ built |
| [8.2](#82-deploy-verification) | Deploy verification | ✅ built, checked live |

## 8.1 Scheduled Checks

**Plan:**

- Per Contract, a cron schedule and a check prompt, in YAML.
- A passing Check is recorded quietly.
- A failing Check produces a Report and notifies the Operator.
- The schedule lives in Postgres. So a restart never fires a Check twice.

**Grilled 2026-10-20 (all recommended):**

- A CheckResult holds: passed, a summary, Evidence, and a Report when it failed.
- The schedule is 5-field cron, read with `cronsim`.
- Each Check has its own time zone.
- A Check runs at most every 15 minutes.
- Missed fire times run once.
- An Open failure keeps one thread. The thread says *still failing*, then *recovered*.
- Proposed actions work as in a Triage.
- A Check is skipped at 100% of the budget.

**Parts:**

- Part a: config, schedule, the runner, Slack and the console.
- Part b: Scenario support, and two Scenarios.

**Built:** both parts on 2026-10-20. The two Scenarios pass on Haiku.

## 8.2 Deploy verification

**Plan:** a rollout or an ArgoCD sync starts a Check of the new revision, after a set delay.

**Grilled 2026-10-20 (all recommended):**

- The Hub polls the Contract's `recent_changes` every 2 minutes.
- Deploy checks are opt-in, with `deploy_checks:` and a delay.
- The prompt is written by the Hub. It holds only identities: the object and the revision. It also asks about the namespace.
- Deploys that are close together get one Run per namespace. This is capped at twice the delay.
- The first poll is the baseline.
- A pass is quiet. A failure starts a thread. There is no Open failure.

**Built (2026-10-20).**

**Checked live:**

- The live check showed the model cannot tell what was failing before a deploy.
- So the Hub's poll records what was failing before the deploy, and the prompt names it.
- The Operator chose this over a prompt-only fix.

**Tests:**

- Scenario `deploy-inventory-breaks-orders` must fail. It passes on Haiku.
- Scenario `deploy-unrelated-next-to-crashloop` must pass. It passes on Haiku.
- The live check also passed.
