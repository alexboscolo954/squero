## demo/reports is crash looping because it gets OOMKilled (exit 137) at its 48Mi memory limit. It loads about 200MB of rows for the monthly report, and the env var that would make it load rows in batches (REPORTS_BATCH_SIZE) isn't set. This matches past incident OPS-112.

**Root cause** (high confidence, `bad_config`): The reports container has no env vars set (env: [], env_from: []), so REPORTS_BATCH_SIZE is missing. The logs show 'building monthly report: loading 200MB of rows', and 200MB is far above the 48Mi limit (request 32Mi), so the kernel kills the container with OOMKilled, exit 137, on every start. A past report (OPS-112) found this same cause. It also says raising the limit on 2026-08-30 only held until the table grew, and that REPORTS_BATCH_SIZE=1000 keeps the job under 40Mi. I could not check those two claims with current tools. Reports has only one revision (created about 2m ago), so no rollout of reports is to blame. The checkout rollout that removed DATABASE_URL is a separate change and not related to this crash.
**Severity:** medium  ·  **Affected:** demo/deployment/reports

**Evidence**
- `kubernetes_describe_pod`: reports-55b4fffc66-hmq7f container app: state and last_state terminated OOMKilled, exit code 137, 3 restarts, memory limit 48Mi, env [] and env_from [], BackOff events
- `kubernetes_pod_logs`: Logs before the kill: 'building monthly report: loading 200MB of rows' (the tool said previous logs were gone and showed the latest instance instead)
- `kubernetes_recent_changes`: reports revision 1 was created about 2m ago with no later rollout. The only rollout is checkout rev 2, which removed DATABASE_URL and is not related.
- `incidents_read`: Past report 5b0e2c4e (OPS-112): same OOM at 48Mi from REPORTS_BATCH_SIZE being unset. Raising the limit before only delayed the next OOM. REPORTS_BATCH_SIZE=1000 keeps the job under 40Mi.

**Recommendations**
1. Set REPORTS_BATCH_SIZE=1000 on deployment demo/reports in the team's manifests (GitOps/source of truth) and roll it out.
2. Don't raise the memory limit as the fix. A past incident showed that only delays the next OOM as the table grows.
3. Don't restart the pod: a restart won't fix this crash loop.
4. After the rollout, confirm the pod stays Running with no new OOMKilled restarts and stays under the 48Mi limit.
5. Separately, look into the checkout rev 2 rollout that removed DATABASE_URL. It may cause its own alert.

Runbook: `oom-killed`

**Tool calls**
1. `runbooks_runbook_for_alert` {"alertname": "KubePodCrashLooping"}
2. `kubernetes_describe_pod` {"cluster": "kind", "namespace": "demo", "name": "reports-55b4fffc66-hmq7f"}
3. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "demo", "name": "reports-55b4fffc66-hmq7f", "previous": true, "tail_lines": 100}
4. `kubernetes_recent_changes` {"cluster": "kind"}
5. `incidents_search` {"keywords": "reports crashloop"}
6. `incidents_read` {"id": "5b0e2c4e-9a51-4d1e-8f3a-2f6c7d1e0a93"}
