## reports in demo (prod-eu) is crash looping because it gets OOMKilled (exit 137) on every start. Its 48Mi memory limit is far below the roughly 200MB of rows it loads at startup. The deployment has never been ready (0/1).

**Root cause** (high confidence, `resource_limits`): The app container has a memory limit of 48Mi (request 32Mi). Its own log says it is 'building monthly report: loading 200MB of rows', which is more than four times the limit, so the kernel kills it each time it loads the data (OOMKilled, exit 137, 3 restarts so far). Rollout history has only revision 1, created about 2 minutes ago. This is a new deployment that has never run, not a regression from an earlier working revision. Most likely the limit was simply set too low for the workload. No Prometheus is available here, so the actual peak memory use was not measured. The ~200MB figure comes from the app's log.
**Severity:** medium  ·  **Affected:** demo/Deployment/reports

**Evidence**
- `kubernetes_describe_pod`: Pod reports-55b4fffc66-hmq7f container app: state and last_state terminated OOMKilled, exit_code 137, restarts 3; limits memory 48Mi, requests 32Mi; BackOff warning events
- `kubernetes_pod_logs`: Latest instance log (previous logs were gone): 'building monthly report: loading 200MB of rows', just before the kill
- `kubernetes_describe_deployment`: reports has only revision 1 (reports-55b4fffc66, created 2m ago), limit 48Mi, 0/1 ready, Available=False (MinimumReplicasUnavailable)
- `runbooks_runbook_for_alert`: The pod-crashlooping runbook sends OOMKilled/137 to the oom-killed runbook, whose fix is to raise the limit and request to observed peak plus 25%, or fix the leak. Restarting only buys time.

**Recommendations**
1. Raise the memory limit on deployment demo/reports (in its manifest or Helm values) to at least ~256Mi, enough for the 200MB dataset plus Python overhead and about 25% headroom. Raise the request to match, e.g. 256Mi.
2. Once memory metrics are available, check the real peak working set and size the limit to that peak plus 25%.
3. Longer term, have the report job stream or batch its rows instead of loading all 200MB into memory at once.
4. Don't restart the pod: each new run will get OOMKilled again until the limit is changed.

Runbook: `oom-killed`

**Tool calls**
1. `runbooks_runbook_for_alert` {"alertname": "KubePodCrashLooping"}
2. `kubernetes_describe_pod` {"cluster": "prod-eu", "namespace": "demo", "name": "reports-55b4fffc66-hmq7f"}
3. `kubernetes_pod_logs` {"cluster": "prod-eu", "namespace": "demo", "name": "reports-55b4fffc66-hmq7f", "previous": true, "tail_lines": 100}
4. `incidents_search` {"keywords": "reports crashloop"}
5. `kubernetes_describe_deployment` {"cluster": "prod-eu", "namespace": "demo", "name": "reports"}
