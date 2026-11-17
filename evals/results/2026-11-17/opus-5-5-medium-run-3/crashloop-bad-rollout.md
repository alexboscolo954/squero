## checkout in demo is crash looping because rollout revision 2, about 48s before this check, removed the DATABASE_URL env var. The app exits on startup with 'FATAL: required env var DATABASE_URL is not set', and checkout has 0 of 1 replicas ready.

**Root cause** (high confidence, `bad_rollout`): Deployment checkout revision 2, rolled out about 48s before this check, dropped the env var DATABASE_URL from container 'app' (recent_changes diff: env_removed app: [DATABASE_URL]). The new pod's env list is empty. The app (checkout 1.4.0) needs this variable and exits with code 1 right after starting ('FATAL: required env var DATABASE_URL is not set'), so the pod has restarted 3 times and is in BackOff. The previous run's logs were gone, so the FATAL line comes from the latest run, which failed the same way (exit 1). No checkout replica is available. Revision 1 had DATABASE_URL set.
**Severity:** high  ·  **Affected:** demo/Deployment/checkout

**Evidence**
- `kubernetes_recent_changes`: checkout revision 2 rollout about 48s before this check, diff env_removed: {app: [DATABASE_URL]}; revision 1 created 2m before this check
- `kubernetes_pod_logs`: checkout-869fc5bcc6-db5ps (previous run's logs gone, so this is the latest run): 'checkout starting, version 1.4.0' / 'FATAL: required env var DATABASE_URL is not set'
- `kubernetes_describe_pod`: container app terminated Error, exit code 1, 3 restarts, BackOff events; env: [] and env_from: [], so DATABASE_URL is not set
- `kubernetes_list_deployments`: checkout ready 0/1, MinimumReplicasUnavailable

**Recommendations**
1. Roll checkout back to revision 1 (kubectl rollout undo deployment/checkout -n demo --to-revision=1), or add DATABASE_URL back to the deployment spec, using its original Secret or ConfigMap source.
2. Check that the new pod starts and checkout reaches 1/1 ready.
3. Find out why revision 2 removed DATABASE_URL (manifest or Helm change), fix it in source control so the next deploy doesn't remove it again, and consider a pre-deploy check that required env vars are present.
4. Don't just restart the pod: it will keep crashing until DATABASE_URL is restored.
5. Not the cause of this alert, but frontend (image nginx:1.99.99-does-not-exist) and reports are also at 0/1 ready in demo and need their own triage.

Runbook: `pod-crashlooping`

**Tool calls**
1. `runbooks_runbook_for_alert` {"alertname": "KubePodCrashLooping"}
2. `kubernetes_describe_pod` {"cluster": "kind", "namespace": "demo", "name": "checkout-869fc5bcc6-db5ps"}
3. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "demo", "name": "checkout-869fc5bcc6-db5ps", "previous": true, "tail_lines": 50}
4. `kubernetes_recent_changes` {"cluster": "kind"}
5. `incidents_search` {"keywords": "checkout crashloop"}
6. `kubernetes_list_deployments` {"cluster": "kind", "namespace": "demo"}
