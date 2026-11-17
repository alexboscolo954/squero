## checkout in demo is crash looping (exit 1, 3 restarts) because revision 2, rolled out about 48s ago, removed the DATABASE_URL env var. The app exits at startup with 'FATAL: required env var DATABASE_URL is not set'.

**Root cause** (high confidence, `bad_rollout`): Deployment checkout revision 2, rolled out about 48s ago, removed the env var DATABASE_URL from the 'app' container. Revision 1 was created about 2m ago. Checkout 1.4.0 needs this variable at startup and exits with code 1 without it ('FATAL: required env var DATABASE_URL is not set'), which causes the CrashLoopBackOff. The tools showed no logs from revision 1 running healthily. Still, removing the variable is the only change listed, and the log names that exact variable.
**Severity:** high  ·  **Affected:** demo/Deployment/checkout

**Evidence**
- `kubernetes_recent_changes`: checkout revision 2 rollout 48s ago with diff env_removed: {app: [DATABASE_URL]}; revision 1 created 2m ago
- `kubernetes_pod_logs`: checkout-869fc5bcc6-db5ps: 'checkout starting, version 1.4.0' then 'FATAL: required env var DATABASE_URL is not set' (previous logs gone, so this is the latest instance)
- `kubernetes_list_pods`: checkout-869fc5bcc6-db5ps ready 0/1, 3 restarts, app container terminated Error exit_code 1

**Recommendations**
1. Roll back deployment demo/checkout to revision 1 (kubectl rollout undo deployment/checkout -n demo), or add DATABASE_URL back to the app container's env.
2. Find out who removed DATABASE_URL in revision 2 and why, and fix the manifest or GitOps source so the next sync doesn't remove it again.
3. Don't restart the pod. It will keep crashing until the env var is back.
4. These other demo problems were seen but not investigated and don't explain the checkout crash: frontend is in ErrImagePull on the nonexistent tag nginx:1.99.99-does-not-exist, and reports is OOMKilled. Triage them on their own.

Runbook: `pod-crashlooping`

**Tool calls**
1. `runbooks_runbook_for_alert` {"alertname": "KubePodCrashLooping"}
2. `kubernetes_list_pods` {"cluster": "kind", "namespace": "demo"}
3. `kubernetes_recent_changes` {"cluster": "kind"}
4. `incidents_search` {"keywords": "checkout"}
5. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "demo", "name": "checkout-869fc5bcc6-db5ps", "previous": true, "tail_lines": 30}
