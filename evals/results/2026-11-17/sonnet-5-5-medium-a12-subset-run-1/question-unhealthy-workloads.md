All three deployments in `demo` are unhealthy (0/1 ready, MinimumReplicasUnavailable):

1. **checkout**: crash-looping (exit 1, 3 restarts). The app logs `FATAL: required env var DATABASE_URL is not set`. Revision 2 rolled out about 48s ago and removed the `DATABASE_URL` env var from the `app` container. Fix: restore `DATABASE_URL` or roll back to revision 1.
2. **frontend**: `ErrImagePull`. The image `nginx:1.99.99-does-not-exist` doesn't exist (not found on docker.io). Fix: set a valid image tag.
3. **reports**: `OOMKilled` (exit 137, 3 restarts, in back-off). The memory limit is 48Mi and the app logs `loading 200MB of rows`. Fix: raise the memory limit well above 200MB, or make the job stream or batch its data.

**Evidence**
- `kubernetes_list_pods`: checkout: Error exit 1, 3 restarts; frontend: ErrImagePull, image not found; reports: OOMKilled exit 137, 3 restarts
- `kubernetes_pod_logs`: checkout: 'FATAL: required env var DATABASE_URL is not set'
- `kubernetes_recent_changes`: checkout revision 2 rollout 48s ago: env_removed DATABASE_URL from app
- `kubernetes_describe_pod`: reports container has memory limit 48Mi, last state OOMKilled, BackOff events
- `kubernetes_list_deployments`: all three deployments 0/1 ready; frontend image nginx:1.99.99-does-not-exist

**Tool calls**
1. `kubernetes_list_pods` {"cluster": "kind", "namespace": "demo"}
2. `kubernetes_list_deployments` {"cluster": "kind", "namespace": "demo"}
3. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "demo", "name": "checkout-869fc5bcc6-db5ps", "tail_lines": 20}
4. `kubernetes_describe_pod` {"cluster": "kind", "namespace": "demo", "name": "reports-55b4fffc66-hmq7f"}
5. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "demo", "name": "reports-55b4fffc66-hmq7f", "tail_lines": 10}
6. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "demo", "name": "frontend-6f6b4986b9-hxdf2", "tail_lines": 5} (error)
7. `kubernetes_recent_changes` {"cluster": "kind", "since_minutes": 60}
8. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "demo", "name": "checkout-869fc5bcc6-db5ps", "tail_lines": 1}
9. `kubernetes_list_deployments` {"cluster": "kind", "namespace": "demo"}
