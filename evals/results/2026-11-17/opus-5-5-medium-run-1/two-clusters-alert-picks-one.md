## reports in demo (prod-eu) keeps getting OOMKilled (exit 137, 3 restarts). Its memory limit is 48Mi, but it loads about 200MB of rows to build the monthly report.

**Root cause** (high confidence, `resource_limits`): The container's memory limit (48Mi, request 32Mi) is far below what it needs. The log line just before the kill says 'building monthly report: loading 200MB of rows', so the kernel kills the container (OOMKilled, exit 137) on every start. The deployment has only revision 1 (created 2m ago), so this is the original sizing and not a regression from a change. No Prometheus tool was available to measure actual peak memory, so the 200MB figure comes from the app's own log line.
**Severity:** medium  ·  **Affected:** demo/Deployment/reports

**Evidence**
- `kubernetes_list_pods`: reports-55b4fffc66-hmq7f: ready 0/1, restarts 3, last_terminated OOMKilled exit_code 137
- `kubernetes_pod_logs`: The previous instance's logs were gone, so these are from the latest instance: 'building monthly report: loading 200MB of rows'
- `kubernetes_describe_deployment`: Container app (python:3.12-alpine) has limits.memory 48Mi and requests.memory 32Mi; only one revision (reports-55b4fffc66, 2m old); Available=False

**Recommendations**
1. Raise the memory limit and request on deployment reports to fit the about 200MB working set plus headroom (e.g. limit 320Mi, request 256Mi), then re-check usage and size to observed peak plus 25%.
2. Or change the report job to stream or batch rows instead of loading the whole 200MB dataset into memory.
3. Don't restart the pod. It will be OOMKilled again until the limit or the memory use changes.
4. Separately: checkout (exit 1) and frontend (ErrImagePull on nginx:1.99.99-does-not-exist) in the same namespace are also failing. They are unrelated to this alert and need their own triage.

Runbook: `oom-killed`

**Tool calls**
1. `runbooks_runbook_for_alert` {"alertname": "KubePodCrashLooping"}
2. `kubernetes_list_pods` {"cluster": "prod-eu", "namespace": "demo"}
3. `kubernetes_pod_logs` {"cluster": "prod-eu", "namespace": "demo", "name": "reports-55b4fffc66-hmq7f", "previous": true, "tail_lines": 50}
4. `incidents_search` {"keywords": "reports crashloop"}
5. `kubernetes_describe_deployment` {"cluster": "prod-eu", "namespace": "demo", "name": "reports"}
