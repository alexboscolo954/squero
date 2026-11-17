## demo/reports is crash looping: the app container is OOMKilled (exit 137) at its 48Mi memory limit while the monthly report loads about 200MB of rows at once. REPORTS_BATCH_SIZE is not set on the container, which matches past incident OPS-112.

**Root cause** (high confidence, `bad_config`): The reports container has a 48Mi memory limit and no env vars (env: []), so REPORTS_BATCH_SIZE is unset. Its log says 'building monthly report: loading 200MB of rows', so it tries to hold the whole result set in memory and the kernel kills it at 48Mi (OOMKilled, exit 137, 3 restarts so far). The past incident from 2026-09-28 (OPS-112) found the same cause. It says the job loads the whole table when REPORTS_BATCH_SIZE is unset, that setting REPORTS_BATCH_SIZE=1000 keeps it under 40Mi, and that raising the limit on 2026-08-30 only held until the table grew. The only recent rollout is checkout revision 2 (DATABASE_URL removed), and nothing shows reports depends on checkout, so it does not explain this. Note: I couldn't read the code, so the env var's effect comes from the past report. The current tools only confirm that the variable is missing.
**Severity:** medium  ·  **Affected:** demo/Deployment/reports

**Evidence**
- `kubernetes_describe_pod`: reports-55b4fffc66-hmq7f container app: state and last_state terminated OOMKilled exit 137, restarts 3, memory limit 48Mi (request 32Mi), env [] and env_from [], BackOff warnings
- `kubernetes_pod_logs`: Only line before the kill: 'building monthly report: loading 200MB of rows' (previous logs were gone, so this is the latest instance)
- `kubernetes_recent_changes`: reports is still at revision 1 (created 2m ago) and was not changed. The only rollout is checkout rev 2, which removed DATABASE_URL
- `incidents_read`: Past incident 5b0e2c4e (OPS-112, 2026-09-28): same OOMKill at 48Mi. With REPORTS_BATCH_SIZE unset the whole table loads; REPORTS_BATCH_SIZE=1000 keeps it under 40Mi; raising the limit earlier only delayed the next OOM

**Recommendations**
1. Set REPORTS_BATCH_SIZE=1000 on the reports deployment in the team's manifests and roll it out, so rows are streamed in batches.
2. Don't just raise the 48Mi memory limit. The past incident shows that only delayed the next OOM as the table grew. Restarting won't help either.
3. After the rollout, check that the pod stays Running without new OOMKills and, if metrics are available, that memory stays below about 40Mi.
4. Follow up on OPS-112: make the default batch size safe in code so a missing env var can't cause this again.
5. Separately, look at checkout revision 2, which removed DATABASE_URL. It is not related to this alert but may break checkout.

Runbook: `oom-killed`

**Tool calls**
1. `runbooks_runbook_for_alert` {"alertname": "KubePodCrashLooping"}
2. `kubernetes_describe_pod` {"cluster": "kind", "namespace": "demo", "name": "reports-55b4fffc66-hmq7f"}
3. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "demo", "name": "reports-55b4fffc66-hmq7f", "previous": true, "tail_lines": 100}
4. `kubernetes_recent_changes` {"cluster": "kind"}
5. `incidents_search` {"keywords": "reports crashloop"}
6. `incidents_read` {"id": "5b0e2c4e-9a51-4d1e-8f3a-2f6c7d1e0a93"}
