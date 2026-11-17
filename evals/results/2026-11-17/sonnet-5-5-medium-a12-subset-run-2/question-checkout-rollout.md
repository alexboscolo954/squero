The latest rollout of `checkout` (revision 2, about 48s ago) removed the `DATABASE_URL` env var from the `app` container. The image did not change: it is `python:3.12-alpine` in both revisions. Revision 1 had `DATABASE_URL`.

The change broke the deployment. The new pod `checkout-869fc5bcc6-db5ps` is 0/1 ready, has exited with code 1 and restarted 3 times. Its log says `FATAL: required env var DATABASE_URL is not set`. The deployment is Available=False with 0 of 1 replicas ready.

**Recommended fix:** restore `DATABASE_URL` on the deployment, or roll back to revision 1.

**Evidence**
- `kubernetes_recent_changes`: checkout revision 2 rolled out 48s ago with diff env_removed: app: [DATABASE_URL]
- `kubernetes_describe_deployment`: Revision 2 (checkout-869fc5bcc6) has env [] and image python:3.12-alpine. Revision 1 had env DATABASE_URL and the same image. Ready 0/1, Available=False.
- `kubernetes_pod_logs`: checkout-869fc5bcc6-db5ps: 'FATAL: required env var DATABASE_URL is not set'
- `kubernetes_list_pods`: checkout-869fc5bcc6-db5ps is 0/1, terminated with Error (exit code 1), 3 restarts

**Tool calls**
1. `kubernetes_describe_deployment` {"cluster": "kind", "namespace": "demo", "name": "checkout"}
2. `kubernetes_recent_changes` {"cluster": "kind"}
3. `kubernetes_list_pods` {"cluster": "kind", "namespace": "demo", "label_selector": "app=checkout"}
4. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "demo", "name": "checkout-869fc5bcc6-db5ps", "tail_lines": 20}
