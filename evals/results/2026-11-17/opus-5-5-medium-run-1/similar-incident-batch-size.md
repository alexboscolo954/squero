## reports in demo is crash looping because it gets OOMKilled (exit 137) at its 48Mi memory limit. The monthly report loads about 200MB of rows into memory at once because REPORTS_BATCH_SIZE isn't set. A past incident (OPS-112) found the same cause.

**Root cause** (high confidence, `bad_config`): The reports container (python:3.12-alpine) has a 48Mi memory limit and no env vars at all (env: []), so REPORTS_BATCH_SIZE is not set. The log line just before the kill says 'building monthly report: loading 200MB of rows'. Loading about 200MB against a 48Mi limit gets the container OOMKilled with exit 137, which has happened 3 times so far. The past report (OPS-112) says that without REPORTS_BATCH_SIZE the job loads the whole table at once, and that setting REPORTS_BATCH_SIZE=1000 streams the rows and keeps memory under 40Mi. It also says raising the limit on 2026-08-30 only helped until the table grew. That fix is not in place: there are no env vars on the pod, and the deployment is still at revision 1, created 2m ago.
**Severity:** medium  ·  **Affected:** demo/Deployment/reports

**Evidence**
- `kubernetes_describe_pod`: reports-55b4fffc66-hmq7f container app: state and last_state terminated OOMKilled exit 137, 3 restarts, memory limit 48Mi, env: [], BackOff events
- `kubernetes_pod_logs`: Latest instance logs (previous ones are gone): 'building monthly report: loading 200MB of rows'
- `kubernetes_recent_changes`: reports is still at revision 1, created 2m ago, with no later rollout. The only rollout is checkout rev 2 (DATABASE_URL removed), which is a different workload and not related to the reports OOM
- `incidents_read`: Past report 5b0e2c4e (OPS-112) found the same OOM at 48Mi from REPORTS_BATCH_SIZE being unset. Fix: set REPORTS_BATCH_SIZE=1000; a limit raise on 2026-08-30 only delayed the next OOM

**Recommendations**
1. Set REPORTS_BATCH_SIZE=1000 on deploy/reports in the team's manifests so the report streams rows instead of loading the whole table.
2. Don't just raise the memory limit. Per OPS-112, that only delayed the next OOM last time.
3. Don't restart the pod: a restart won't stop the crash loop.
4. After the change rolls out, confirm in describe_pod that the pod stays Running with no OOMKilled terminations.
5. Separately, check the checkout rollout (rev 2 removed DATABASE_URL), since it may cause its own alert.

Runbook: `oom-killed`

**Tool calls**
1. `runbooks_runbook_for_alert` {"alertname": "KubePodCrashLooping"}
2. `kubernetes_describe_pod` {"cluster": "kind", "namespace": "demo", "name": "reports-55b4fffc66-hmq7f"}
3. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "demo", "name": "reports-55b4fffc66-hmq7f", "previous": true, "tail_lines": 100}
4. `kubernetes_recent_changes` {"cluster": "kind"}
5. `incidents_search` {"keywords": "reports crashloop"}
6. `incidents_read` {"id": "5b0e2c4e-9a51-4d1e-8f3a-2f6c7d1e0a93"}
