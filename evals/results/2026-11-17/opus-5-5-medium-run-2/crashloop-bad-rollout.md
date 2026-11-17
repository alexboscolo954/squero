## checkout in demo is crash looping because revision 2 of the deployment, rolled out about 48s ago, removed the DATABASE_URL env var. The app exits with 'FATAL: required env var DATABASE_URL is not set'. No working checkout pod is left, so checkout is down.

**Root cause** (high confidence, `bad_rollout`): checkout revision 2 (rolled out about 48s before the alert) removed DATABASE_URL from the 'app' container: recent_changes shows env_removed app: [DATABASE_URL]. The pod now has no env vars at all (env: [], env_from: []). Version 1.4.0 needs this variable at startup, logs 'FATAL: required env var DATABASE_URL is not set' and exits with code 1 (reason Error, 3 restarts, BackOff). It is not a memory problem: the reason is Error, not OOMKilled. No revision-1 checkout pod is still running, so the service has no ready replicas.
**Severity:** high  ·  **Affected:** demo/Deployment/checkout

**Evidence**
- `kubernetes_pod_logs`: checkout-869fc5bcc6-db5ps: 'checkout starting, version 1.4.0' then 'FATAL: required env var DATABASE_URL is not set'. These are the latest instance's logs, since the previous instance's logs were gone.
- `kubernetes_recent_changes`: demo/checkout revision 2 rolled out 48s ago with diff env_removed {app: [DATABASE_URL]}; revision 1 was created 2m ago.
- `kubernetes_describe_pod`: Container app has env [] and env_from []. It is terminated with Error, exit_code 1, has 3 restarts, and shows a BackOff warning event.
- `kubernetes_list_pods`: The only checkout pod is checkout-869fc5bcc6-db5ps at 0/1 ready, so no old-revision replica is serving. Unrelated in the same namespace: frontend is stuck in ErrImagePull on nginx:1.99.99-does-not-exist, and reports is being OOMKilled.

**Recommendations**
1. Roll checkout back to revision 1 (kubectl rollout undo deployment/checkout -n demo --to-revision=1), or add DATABASE_URL back to the deployment spec. Needs approval.
2. Confirm the new pod starts, gets past the startup check and becomes Ready.
3. Find out why DATABASE_URL was removed in revision 2 and fix the source manifest or chart so the next deploy doesn't remove it again.
4. Don't just restart the pod. It will keep crashing until the env var is back.
5. Triage the separate problems in demo: frontend has a nonexistent image tag (nginx:1.99.99-does-not-exist) and reports is being OOMKilled.

Runbook: `pod-crashlooping`

**Tool calls**
1. `runbooks_runbook_for_alert` {"alertname": "KubePodCrashLooping"}
2. `kubernetes_describe_pod` {"cluster": "kind", "namespace": "demo", "name": "checkout-869fc5bcc6-db5ps"}
3. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "demo", "name": "checkout-869fc5bcc6-db5ps", "previous": true, "tail_lines": 50}
4. `kubernetes_recent_changes` {"cluster": "kind"}
5. `incidents_search` {"keywords": "checkout crashloop"}
6. `kubernetes_list_pods` {"cluster": "kind", "namespace": "demo", "label_selector": "app=checkout"}
