All three deployments in `demo` are down, each showing **0/1 ready** and `MinimumReplicasUnavailable`. Each has a different cause:

1. **checkout** is in a crash loop: exit code 1, 3 restarts. Its logs say `FATAL: required env var DATABASE_URL is not set`, so the app exits right after it starts. **Fix:** add `DATABASE_URL` to the checkout deployment, either directly or through a Secret or ConfigMap.

2. **frontend** can't start because its image doesn't exist: `ErrImagePull` / NotFound for `nginx:1.99.99-does-not-exist`, so the pod stays Pending. **Fix:** set the deployment back to a real nginx tag, or roll back to the previous revision.

3. **reports** keeps getting killed for running out of memory: exit code 137, 3 restarts, now in BackOff. Its memory limit is only **48Mi** (32Mi requested), and it goes over that. **Fix:** raise the memory limit, or reduce how much memory the app uses.

I didn't check the rollout history (`kubernetes_recent_changes`), so I can't say what change introduced these problems.

**Evidence**
- `kubernetes_list_deployments`: checkout, frontend and reports are all 0/1 ready with Available=False (MinimumReplicasUnavailable); frontend image is nginx:1.99.99-does-not-exist
- `kubernetes_list_pods`: checkout-869fc5bcc6-db5ps terminated Error exit 1 (3 restarts); frontend-6f6b4986b9-hxdf2 Pending with ErrImagePull, docker.io/library/nginx:1.99.99-does-not-exist not found; reports-55b4fffc66-hmq7f OOMKilled exit 137 (3 restarts)
- `kubernetes_pod_logs`: checkout-869fc5bcc6-db5ps logs: 'FATAL: required env var DATABASE_URL is not set'
- `kubernetes_describe_pod`: reports-55b4fffc66-hmq7f container app: memory request 32Mi, limit 48Mi, state OOMKilled exit 137, BackOff restarting failed container

**Tool calls**
1. `kubernetes_list_pods` {"cluster": "kind", "namespace": "demo"}
2. `kubernetes_list_deployments` {"cluster": "kind", "namespace": "demo"}
3. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "demo", "name": "checkout-869fc5bcc6-db5ps", "tail_lines": 50}
4. `kubernetes_describe_pod` {"cluster": "kind", "namespace": "demo", "name": "reports-55b4fffc66-hmq7f"}
