## The only pod of demo/reports, reports-55b4fffc66-hmq7f, is crash looping because it keeps getting OOMKilled (exit 137, 3 restarts). Its memory limit is 48Mi, but its last log line says it is loading about 200MB of rows to build the monthly report. The deployment has 0 of 1 replicas ready.

**Root cause** (high confidence, `resource_limits`): The 'app' container's memory limit (48Mi, request 32Mi) is far below what the job needs. The container logs 'building monthly report: loading 200MB of rows', and the kernel kills it each time it goes over 48Mi. Both the current and the previous termination are OOMKilled with exit code 137. The deployment is new: revision 1 was created about 2 minutes ago and there is no earlier revision, so a bad change or regression is not the cause. The limit was set too low from the start for a workload that loads about 200MB in memory. I have no memory metrics to measure the real peak.
**Severity:** high  ·  **Affected:** demo/Deployment/reports

**Evidence**
- `kubernetes_describe_pod`: Container 'app' (python:3.12-alpine) has limits memory 48Mi and requests 32Mi. State and last_state are both terminated: OOMKilled, exit_code 137. 3 restarts, with BackOff warning events.
- `kubernetes_pod_logs`: Only log line: 'building monthly report: loading 200MB of rows'. The previous instance's logs were gone, so this is the latest instance.
- `kubernetes_describe_deployment`: Only revision 1 (reports-55b4fffc66), created 2m ago, with limits memory 48Mi. Available=False (MinimumReplicasUnavailable), 0/1 ready.
- `runbooks_runbook_for_alert`: KubePodCrashLooping maps to the pod-crashlooping runbook. With last terminated reason OOMKilled, that runbook points to oom-killed: raise the limit to observed peak plus 25% or fix the memory use, because a restart only buys time.

**Recommendations**
1. Raise the memory limit and request on Deployment demo/reports to well above the ~200MB working set. Without metrics, a starting point is a limit of about 320Mi (peak plus 25% plus Python overhead) and a request of about 256Mi. Then watch whether OOMKills stop.
2. Once metrics are available, check the real peak with max by (pod)(container_memory_working_set_bytes{namespace="demo",container="app"}) and adjust the limit to observed peak plus 25%.
3. Over the longer term, have the report job stream or batch its rows instead of loading all 200MB into memory.
4. Don't restart the pod: it will be OOMKilled again until the limit changes.

Runbook: `oom-killed`

**Tool calls**
1. `runbooks_runbook_for_alert` {"alertname": "KubePodCrashLooping"}
2. `kubernetes_describe_pod` {"cluster": "prod-eu", "namespace": "demo", "name": "reports-55b4fffc66-hmq7f"}
3. `kubernetes_pod_logs` {"cluster": "prod-eu", "namespace": "demo", "name": "reports-55b4fffc66-hmq7f", "previous": true, "tail_lines": 100}
4. `incidents_search` {"keywords": "reports crashloop"}
5. `kubernetes_describe_deployment` {"cluster": "prod-eu", "namespace": "demo", "name": "reports"}
