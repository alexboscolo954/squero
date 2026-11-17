## In demo, the reports pod reports-55b4fffc66-hmq7f is crash looping because the kernel keeps killing it for using too much memory (OOMKilled, exit code 137, 3 restarts). Its job loads about 200MB of rows, but its container memory limit is 48Mi.

**Root cause** (high confidence, `resource_limits`): The reports container (image python:3.12-alpine) has a memory limit of 48Mi and a request of 32Mi. Its startup log says 'building monthly report: loading 200MB of rows', so the job needs several times the memory it is allowed. The current run and the previous run were both OOMKilled with exit code 137. No rollout caused this: reports is at revision 1, created about 2 minutes before the alert. The limit has been too small from the start. The rollout of checkout (DATABASE_URL removed) changes a different workload and does not explain these OOM kills. The ~200MB figure comes from the log line only; Prometheus memory data was not available to confirm the real peak.
**Severity:** medium  ·  **Affected:** demo/Deployment/reports

**Evidence**
- `runbooks_runbook_for_alert`: KubePodCrashLooping maps to pod-crashlooping, which sends OOMKilled (exit 137) cases to the oom-killed runbook: raise the limit to observed peak + 25% or fix the leak; a restart only buys time.
- `kubernetes_describe_pod`: Pod reports-55b4fffc66-hmq7f, container app: limits.memory 48Mi, requests 32Mi; current state and last state both terminated OOMKilled with exit code 137; 3 restarts; BackOff warning events.
- `kubernetes_pod_logs`: The previous run's logs are gone. The latest run logged 'building monthly report: loading 200MB of rows' just before being killed.
- `kubernetes_recent_changes`: reports has only revision 1 (created 2m ago), so no reports rollout changed it. The only rollout is checkout revision 2, which removed DATABASE_URL and is unrelated to this OOM.

**Recommendations**
1. Raise the reports memory limit to about 256Mi, enough for the ~200MB data load plus Python overhead. Set the request to match, or at least to about 200Mi. Change it in the deployment manifest.
2. After rollout, watch the pod's memory use (container_memory_working_set_bytes, if Prometheus is available). Then set the limit to the observed peak plus 25%.
3. Long term, have the report job stream or batch the rows instead of loading all ~200MB into memory.
4. Do not restart the pod as a fix: it will be OOMKilled again until the limit or memory use changes.
5. Separately, check checkout revision 2 (DATABASE_URL removed). It is unrelated to this alert but may cause its own failures.

Runbook: `oom-killed`

**Tool calls**
1. `runbooks_runbook_for_alert` {"alertname": "KubePodCrashLooping"}
2. `kubernetes_describe_pod` {"cluster": "kind", "namespace": "demo", "name": "reports-55b4fffc66-hmq7f"}
3. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "demo", "name": "reports-55b4fffc66-hmq7f", "previous": true, "tail_lines": 100}
4. `kubernetes_recent_changes` {"cluster": "kind"}
5. `incidents_search` {"keywords": "reports crashloop"}
