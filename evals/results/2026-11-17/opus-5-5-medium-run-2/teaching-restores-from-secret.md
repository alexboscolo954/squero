## The checkout pod in demo is crash looping (exit 1, 3 restarts) because revision 2 of the checkout deployment, rolled out about 48s ago, removed the DATABASE_URL env var. The app exits at startup with 'FATAL: required env var DATABASE_URL is not set'.

**Root cause** (high confidence, `bad_rollout`): Revision 2 of deployment demo/checkout removed DATABASE_URL from container 'app' (env_removed: [DATABASE_URL]). Revision 1 was created 2m ago and did not have this problem. Checkout v1.4.0 needs this variable at startup and exits with code 1 without it ('FATAL: required env var DATABASE_URL is not set'), so pod checkout-869fc5bcc6-db5ps is in a crash loop.
**Severity:** high  ·  **Affected:** demo/Deployment/checkout

**Evidence**
- `kubernetes_list_pods`: checkout-869fc5bcc6-db5ps: ready 0/1, 3 restarts, last terminated reason Error with exit code 1.
- `kubernetes_pod_logs`: checkout pod logs: 'checkout starting, version 1.4.0' then 'FATAL: required env var DATABASE_URL is not set'. The previous instance's logs were gone, so these are the latest instance's logs.
- `kubernetes_recent_changes`: About 48s ago, checkout revision 2 rolled out with diff env_removed {app: [DATABASE_URL]}. Revision 1 was created 2m ago.
- `runbooks_runbook_for_alert`: The pod-crashlooping runbook says to fix missing config rather than restart; a restart does not fix a crash loop.

**Recommendations**
1. Do not roll back the checkout deployment. Following the Operator's instruction, restore DATABASE_URL on the checkout 'app' container from secret checkout-db (env valueFrom secretKeyRef, secret checkout-db) and redeploy.
2. Watch the rollout and confirm the new checkout pod becomes Ready 1/1 with no further restarts, and that the logs no longer show the DATABASE_URL fatal error.
3. Find out why revision 2 dropped DATABASE_URL (manifest or Helm values change) and fix it in source control so the next deploy doesn't remove it again.
4. Separately, other pods in demo are also failing and are not part of this alert: frontend has ErrImagePull because the image tag nginx:1.99.99-does-not-exist doesn't exist, and reports is being OOMKilled. These need their own triage.

Runbook: `pod-crashlooping`

**Tool calls**
1. `runbooks_runbook_for_alert` {"alertname": "KubePodCrashLooping"}
2. `kubernetes_list_pods` {"cluster": "kind", "namespace": "demo"}
3. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "demo", "name": "checkout-869fc5bcc6-db5ps", "previous": true, "tail_lines": 50}
4. `kubernetes_recent_changes` {"cluster": "kind"}
5. `incidents_search` {"keywords": "checkout crash loop"}
