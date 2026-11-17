## checkout-869fc5bcc6-db5ps in demo is crash looping (3 restarts so far, exit code 1). It fails at startup with 'FATAL: required env var DATABASE_URL is not set'. Revision 2 of the checkout deployment, rolled out 48s before the pod state was captured, removed the DATABASE_URL env var from the app container.

**Root cause** (high confidence, `bad_rollout`): Revision 2 of the checkout deployment, rolled out 48s ago, removed the DATABASE_URL env var from the 'app' container (diff env_removed: app: [DATABASE_URL]). Checkout 1.4.0 needs this variable at startup, so it logs 'FATAL: required env var DATABASE_URL is not set' and exits with code 1. That repeats on every restart, which is the crash loop. Revision 1 had been running about 2m before this change.
**Severity:** high  ·  **Affected:** demo/Deployment/checkout

**Evidence**
- `kubernetes_list_pods`: checkout-869fc5bcc6-db5ps: Running, ready 0/1, 3 restarts, last terminated reason Error with exit code 1
- `kubernetes_pod_logs`: checkout pod (the previous run's logs were gone, so these are from the latest run): 'checkout starting, version 1.4.0' followed by 'FATAL: required env var DATABASE_URL is not set'
- `kubernetes_recent_changes`: demo/checkout revision 2 rolled out 48s ago with diff env_removed app: [DATABASE_URL]; revision 1 was created 2m ago
- `runbooks_runbook_for_alert`: pod-crashlooping runbook: an exit with Error usually means missing or wrong config such as an env var; if config is missing, fix the config; a restart does not fix a crash loop

**Recommendations**
1. Restore DATABASE_URL on the checkout 'app' container from secret checkout-db (env valueFrom secretKeyRef) and redeploy checkout. Per the Operator's standing instruction, do not roll back the checkout deployment for this.
2. Watch the new pod reach ready 1/1 and check that its logs no longer show the DATABASE_URL FATAL error.
3. Find out who removed DATABASE_URL in revision 2, and add a CI/manifest check that requires this variable.
4. Don't restart the pod: a restart won't fix it. Also note two unrelated problems in demo: frontend is stuck on ErrImagePull for nginx:1.99.99-does-not-exist, and reports is crash looping with OOMKilled. Triage those separately.

Runbook: `pod-crashlooping`

**Tool calls**
1. `runbooks_runbook_for_alert` {"alertname": "KubePodCrashLooping"}
2. `kubernetes_list_pods` {"cluster": "kind", "namespace": "demo"}
3. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "demo", "name": "checkout-869fc5bcc6-db5ps", "previous": true, "tail_lines": 50}
4. `kubernetes_recent_changes` {"cluster": "kind"}
5. `incidents_search` {"keywords": "checkout crash loop"}
