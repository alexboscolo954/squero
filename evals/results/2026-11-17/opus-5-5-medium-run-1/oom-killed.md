## reports (demo) is in a crash loop because it gets OOMKilled (exit 137). It logs that it is loading about 200MB of rows into a container whose memory limit is 48Mi. It has done this since it was first deployed (revision 1). No rollout caused it.

**Root cause** (high confidence, `resource_limits`): The container 'app' (python:3.12-alpine) has a 48Mi memory limit and a 32Mi request. Its startup log says 'building monthly report: loading 200MB of rows', which is roughly 4x the limit. The kernel kills it each time it loads the data (OOMKilled, exit 137, 3 restarts). The deployment is still at revision 1, created about 2m ago. The only rollout in the namespace was checkout revision 2, which removed DATABASE_URL, and reports does not appear to use it. So the limit has been too small since creation; nothing broke it. I have no memory metrics, so I can't say whether the load needs about 200MB or more than that.
**Severity:** medium  ·  **Affected:** demo/Deployment/reports

**Evidence**
- `kubernetes_describe_pod`: Pod reports-55b4fffc66-hmq7f: container app with limits memory 48Mi, requests 32Mi. State and last state both terminated OOMKilled, exit code 137, 3 restarts, BackOff events.
- `kubernetes_pod_logs`: Latest instance logs 'building monthly report: loading 200MB of rows' (previous instance's logs were gone).
- `kubernetes_recent_changes`: reports exists only at revision 1 (created 2m ago) and has had no rollout. The only rollout was checkout revision 2, which removed the DATABASE_URL env var.

**Recommendations**
1. Raise the reports memory limit to above the working set the 200MB load needs. Start around 256Mi to 320Mi and set the request to match, then adjust to observed peak plus 25% as the oom-killed runbook says.
2. Once memory metrics are available, check container_memory_working_set_bytes for reports to confirm the peak and set the final limit from it.
3. Over the longer term, have the report job stream or batch rows instead of loading all 200MB into memory.
4. Don't restart the pod: it will be OOMKilled again until the limit or the memory use changes.

Runbook: `oom-killed`

**Tool calls**
1. `runbooks_runbook_for_alert` {"alertname": "KubePodCrashLooping"}
2. `kubernetes_describe_pod` {"cluster": "kind", "namespace": "demo", "name": "reports-55b4fffc66-hmq7f"}
3. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "demo", "name": "reports-55b4fffc66-hmq7f", "previous": true, "tail_lines": 100}
4. `kubernetes_recent_changes` {"cluster": "kind"}
5. `incidents_search` {"keywords": "reports crashloop"}
