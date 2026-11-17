## checkout in demo is crash looping (exit 1) because revision 2, rolled out about 36s ago, removed the DATABASE_URL env var. The app exits on startup with "FATAL: required env var DATABASE_URL is not set".

**Root cause** (high confidence, `bad_rollout`): Revision 1 of deployment demo/checkout (ReplicaSet checkout-67d9799c78, about 2m old) set the env var DATABASE_URL. Revision 2 (checkout-869fc5bcc6, created about 36s ago) uses the same image, python:3.12-alpine, but has no env vars and no envFrom. checkout 1.4.0 needs DATABASE_URL at startup, so it logs FATAL and exits with code 1 every time. The pod has restarted 3 times and the deployment has 0 of 1 replicas ready.
**Severity:** high  ·  **Affected:** demo/Deployment/checkout

**Evidence**
- `kubernetes_pod_logs`: checkout-869fc5bcc6-db5ps logs: 'checkout starting, version 1.4.0' then 'FATAL: required env var DATABASE_URL is not set'. These are the latest instance's logs because the previous instance's logs were gone.
- `kubernetes_list_pods`: checkout-869fc5bcc6-db5ps is 0/1 ready with 3 restarts. Last terminated reason is Error, exit code 1.
- `kubernetes_describe_deployment`: Revision 2 (checkout-869fc5bcc6, 36s old) has env [] and envFrom []. Revision 1 (checkout-67d9799c78) had env DATABASE_URL. The image did not change. The Available condition is False with reason MinimumReplicasUnavailable.

**Recommendations**
1. Following the Operator's teaching, do not roll back checkout. Restore the DATABASE_URL env var on deployment demo/checkout from secret checkout-db, e.g. env DATABASE_URL with valueFrom.secretKeyRef name checkout-db. Confirm the key name in the secret before applying. Then redeploy.
2. Check that the new pod starts without the FATAL line and that the deployment reaches 1/1 Available.
3. Find out why the revision 2 change dropped DATABASE_URL (manifest, Helm values or GitOps source) and fix it there so the next sync doesn't remove it again.
4. Do not restart the pod. A restart won't fix a crash loop caused by missing config.
5. Unrelated issues in the same namespace that need their own triage: demo/frontend is stuck in ErrImagePull on image nginx:1.99.99-does-not-exist, and demo/reports is OOMKilled (exit 137).

Runbook: `pod-crashlooping`

**Tool calls**
1. `runbooks_runbook_for_alert` {"alertname": "KubePodCrashLooping"}
2. `kubernetes_list_pods` {"cluster": "kind", "namespace": "demo"}
3. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "demo", "name": "checkout-869fc5bcc6-db5ps", "previous": true, "tail_lines": 50}
4. `kubernetes_describe_deployment` {"cluster": "kind", "namespace": "demo", "name": "checkout"}
5. `incidents_search` {"keywords": "checkout crash DATABASE_URL"}
