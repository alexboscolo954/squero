**Checkout revision 2 removed the `DATABASE_URL` env var from the `app` container, and that broke the deployment.**

- Revision 2 is ReplicaSet `checkout-869fc5bcc6`. It rolled out about 48s before the check.
- It is the only change. The image is still `python:3.12-alpine` and the resources are unchanged.
- Revision 1 (`checkout-67d9799c78`) had `DATABASE_URL`. Revision 2 has no env vars and no `envFrom`.

**Impact:**
- The new pod `checkout-869fc5bcc6-db5ps` is crash-looping. It is 0/1 ready, with 3 restarts and exit code 1.
- Its log says `FATAL: required env var DATABASE_URL is not set`.
- The deployment shows `Available=False` (MinimumReplicasUnavailable).
- Revision 1 is scaled to 0, so nothing is serving checkout.

**Recommended fix:** Roll back to revision 1, or restore `DATABASE_URL` in the deployment config. The crash-looping runbook says a restart won't fix this.

`frontend` (bad image tag) and `reports` (OOMKilled) are also failing, but those problems are unrelated to this rollout.

**Evidence**
- `kubernetes_recent_changes`: demo/checkout revision 2 rollout 48s ago; diff: env_removed {app: [DATABASE_URL]}
- `kubernetes_describe_deployment`: Rev 2 (checkout-869fc5bcc6) has env [] and the same image python:3.12-alpine. Rev 1 (checkout-67d9799c78) had env [DATABASE_URL] and is now at 0 replicas. Available=False, 0/1 ready.
- `kubernetes_pod_logs`: checkout-869fc5bcc6-db5ps: 'FATAL: required env var DATABASE_URL is not set'
- `kubernetes_list_pods`: checkout-869fc5bcc6-db5ps is 0/1 ready with 3 restarts, terminated with Error (exit code 1)
- `runbooks_read_runbook`: pod-crashlooping runbook: if a new revision caused the crash, roll it back; if config is missing, fix the config. Don't propose a restart.

**Tool calls**
1. `kubernetes_describe_deployment` {"cluster": "kind", "namespace": "demo", "name": "checkout"}
2. `kubernetes_recent_changes` {"cluster": "kind"}
3. `kubernetes_list_pods` {"cluster": "kind", "namespace": "demo", "label_selector": "app=checkout"}
4. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "demo", "name": "checkout-869fc5bcc6-db5ps", "tail_lines": 20}
5. `kubernetes_describe_deployment` {"cluster": "kind", "namespace": "demo", "name": "checkout"}
6. `kubernetes_get_events` {"cluster": "kind", "namespace": "demo"}
7. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "demo", "name": "checkout-869fc5bcc6-db5ps", "tail_lines": 5, "previous": true}
8. `kubernetes_describe_pod` {"cluster": "kind", "namespace": "demo", "name": "checkout-869fc5bcc6-db5ps"}
9. `kubernetes_list_deployments` {"cluster": "kind", "namespace": "demo"}
10. `kubernetes_list_pods` {"cluster": "kind", "namespace": "demo", "label_selector": "app=checkout"}
11. `runbooks_search_runbooks` {"keywords": "missing env var config rollback"}
12. `runbooks_read_runbook` {"name": "pod-crashlooping"}
13. `incidents_search` {"keywords": "checkout DATABASE_URL"}
14. `kubernetes_describe_deployment` {"cluster": "kind", "namespace": "demo", "name": "checkout"}
15. `kubernetes_get_events` {"cluster": "kind", "namespace": "demo", "warnings_only": false}
16. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "demo", "name": "checkout-869fc5bcc6-db5ps", "tail_lines": 3}
17. `runbooks_runbook_for_alert` {"alertname": "KubePodCrashLooping"}
18. `read_more` {"result_id": "none"}
