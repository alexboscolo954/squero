## reports in demo is crash looping because the kernel keeps OOM-killing it (exit code 137, 3 restarts). Its 48Mi memory limit is far below what it needs: its log says it is loading 200MB of rows for the monthly report.

**Root cause** (high confidence, `resource_limits`): The container's memory limit is 48Mi and its request is 32Mi. At startup it logs 'building monthly report: loading 200MB of rows', so it needs at least about 200MB, roughly four times the limit. The kernel kills it as it loads, and both the current and last termination show OOMKilled with exit code 137. No rollout is to blame: the deployment is still on revision 1, created about 2 minutes ago, so the limit was too small from the start. The checkout rollout 48s ago only removed DATABASE_URL from checkout and has nothing to do with reports.
**Severity:** medium  ·  **Affected:** demo/Deployment/reports

**Evidence**
- `kubernetes_describe_pod`: reports-55b4fffc66-hmq7f: container app (python:3.12-alpine) has limits.memory=48Mi and requests.memory=32Mi; state terminated OOMKilled with exit code 137, last_state also OOMKilled with exit 137; 3 restarts; BackOff warning events.
- `kubernetes_pod_logs`: reports-55b4fffc66-hmq7f: 'building monthly report: loading 200MB of rows'. Logs from the previous instance were gone, so this line is from the latest one.
- `kubernetes_recent_changes`: reports is only at revision 1 (created 2m ago); the only rollout was checkout rev 2, which removed the DATABASE_URL env var and is unrelated to reports.
- `runbooks_runbook_for_alert`: Runbook pod-crashlooping sends OOMKilled (exit 137) cases to the oom-killed runbook, whose fix is to raise the limit and request to observed peak plus 25%, or fix the leak. Restarting only buys time.

**Recommendations**
1. Raise the reports memory limit to about 256Mi (the 200MB dataset plus Python overhead and about 25% headroom), and raise the request to match, e.g. 256Mi. Then re-check usage, using Prometheus container_memory_working_set_bytes if it is available.
2. Don't restart the pod: under the current limit it will be OOM-killed again.
3. Long term, have the report job stream or batch the rows instead of loading all 200MB into memory, so memory use doesn't grow with data size.
4. Look separately at the checkout rollout (rev 2, which removed DATABASE_URL). It is unrelated to this alert but may break checkout.

Runbook: `oom-killed`

**Tool calls**
1. `runbooks_runbook_for_alert` {"alertname": "KubePodCrashLooping"}
2. `kubernetes_describe_pod` {"cluster": "kind", "namespace": "demo", "name": "reports-55b4fffc66-hmq7f"}
3. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "demo", "name": "reports-55b4fffc66-hmq7f", "previous": true, "tail_lines": 50}
4. `kubernetes_recent_changes` {"cluster": "kind"}
5. `incidents_search` {"keywords": "reports crashloop"}
