**The latest rollout of `checkout` (revision 2, ReplicaSet `checkout-869fc5bcc6`, created about 36s ago) removed the `DATABASE_URL` environment variable from the `app` container.** Revision 1 (`checkout-67d9799c78`) set `DATABASE_URL`. Revision 2 has no env vars and no envFrom. The image stayed `python:3.12-alpine` in both revisions.

**What this broke:** the new pod `checkout-869fc5bcc6-db5ps` exits with code 1 and has restarted 3 times. Its logs say `FATAL: required env var DATABASE_URL is not set`. The deployment has 0 of 1 replicas ready and is marked unavailable (`MinimumReplicasUnavailable`). Revision 1 has been scaled to 0 replicas, so nothing is serving checkout right now.

**Recommended fix:** put `DATABASE_URL` back in the deployment spec, or roll back to revision 1 (`kubectl rollout undo deployment/checkout -n demo`). I can't change anything myself.

**Evidence**
- `kubernetes_describe_deployment`: checkout rollout history: rev 2 (checkout-869fc5bcc6, 36s old) has env [] and image python:3.12-alpine; rev 1 (checkout-67d9799c78) had env [DATABASE_URL] and the same image. Ready 0/1, Available=False (MinimumReplicasUnavailable).
- `kubernetes_list_pods`: checkout-869fc5bcc6-db5ps: Running, 0/1 ready, 3 restarts, terminated with Error exit code 1
- `kubernetes_pod_logs`: checkout-869fc5bcc6-db5ps logs: 'FATAL: required env var DATABASE_URL is not set'

**Tool calls**
1. `kubernetes_describe_deployment` {"cluster": "kind", "namespace": "demo", "name": "checkout"}
2. `kubernetes_list_pods` {"cluster": "kind", "namespace": "demo", "label_selector": "app=checkout"}
3. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "demo", "name": "checkout-869fc5bcc6-db5ps", "tail_lines": 30}
