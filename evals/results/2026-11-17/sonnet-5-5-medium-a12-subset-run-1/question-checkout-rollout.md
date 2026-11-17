The latest rollout of `checkout` is revision 2 (ReplicaSet `checkout-869fc5bcc6`). It started about 48s ago, and its only change was removing the `DATABASE_URL` env var from the `app` container. The image is unchanged (`python:3.12-alpine`), and revision 1 had `DATABASE_URL` set.

The rollout broke the deployment. The new pod `checkout-869fc5bcc6-db5ps` is crash-looping: 0/1 ready, 3 restarts, exit code 1. Its logs say `FATAL: required env var DATABASE_URL is not set`. The deployment shows Available=False (MinimumReplicasUnavailable).

To fix it, restore `DATABASE_URL` in the deployment spec, or roll back to revision 1. I couldn't see where revision 1 got the value from, so you will need to check that source.

**Evidence**
- `kubernetes_recent_changes`: checkout revision 2 rollout 48s ago: env_removed app: [DATABASE_URL]
- `kubernetes_describe_deployment`: Revision 2 has no env and the same image python:3.12-alpine. Revision 1 had env DATABASE_URL. Ready 0/1, Available=False.
- `kubernetes_list_pods`: checkout-869fc5bcc6-db5ps is 0/1 ready, 3 restarts, terminated with Error exit code 1
- `kubernetes_pod_logs`: FATAL: required env var DATABASE_URL is not set

**Tool calls**
1. `kubernetes_describe_deployment` {"cluster": "kind", "namespace": "demo", "name": "checkout"}
2. `kubernetes_recent_changes` {"cluster": "kind"}
3. `kubernetes_list_pods` {"cluster": "kind", "namespace": "demo", "label_selector": "app=checkout"}
4. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "demo", "name": "checkout-869fc5bcc6-db5ps", "tail_lines": 20}
5. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "demo", "name": "checkout-869fc5bcc6-db5ps", "tail_lines": 5, "previous": true}
