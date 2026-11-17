**The latest rollout (revision 2, ReplicaSet `checkout-869fc5bcc6`, about 36s old) removed the `DATABASE_URL` environment variable from the `app` container.** The image is the same in both revisions (`python:3.12-alpine`). Revision 1 (`checkout-67d9799c78`) set `DATABASE_URL`. Revision 2 sets no env vars at all.

**Effect:** the deployment has 0 of 1 replicas ready. The new pod `checkout-869fc5bcc6-db5ps` exits with code 1 and has already restarted 3 times. Its log says `FATAL: required env var DATABASE_URL is not set`.

**Recommended fix:** add `DATABASE_URL` back to the deployment (from the same source revision 1 used), or roll back to revision 1 with `kubectl rollout undo deployment/checkout -n demo`.

**Evidence**
- `kubernetes_describe_deployment`: checkout rollout history: rev 2 (checkout-869fc5bcc6, 36s) image python:3.12-alpine, env []; rev 1 (checkout-67d9799c78, 2m) image python:3.12-alpine, env [DATABASE_URL]; Available=False, ready 0/1
- `kubernetes_list_pods`: checkout-869fc5bcc6-db5ps ready 0/1, 3 restarts, terminated Error exit_code 1
- `kubernetes_pod_logs`: checkout-869fc5bcc6-db5ps: 'FATAL: required env var DATABASE_URL is not set'

**Tool calls**
1. `kubernetes_describe_deployment` {"cluster": "kind", "namespace": "demo", "name": "checkout"}
2. `kubernetes_list_pods` {"cluster": "kind", "namespace": "demo", "label_selector": "app=checkout"}
3. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "demo", "name": "checkout-869fc5bcc6-db5ps", "tail_lines": 30}
