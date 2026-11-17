All three deployments in `demo` are unhealthy. Each shows **0/1 ready** with `MinimumReplicasUnavailable`.

1. **checkout** (pod `checkout-869fc5bcc6-db5ps`) is crash-looping. The app exits with code 1 after 3 restarts. Its log says `FATAL: required env var DATABASE_URL is not set`. **Fix:** add `DATABASE_URL` to the deployment, either directly or from a Secret or ConfigMap.

2. **frontend** (pod `frontend-6f6b4986b9-hxdf2`) is stuck in Pending with `ErrImagePull`. The image `nginx:1.99.99-does-not-exist` doesn't exist on docker.io (`not found`). **Fix:** set the image to a real nginx tag, or roll back to the previous revision.

3. **reports** (pod `reports-55b4fffc66-hmq7f`) is crash-looping because it runs out of memory. It is killed with `OOMKilled` (exit 137), has restarted 3 times and is in back-off. Its memory limit is only **48Mi** (it asks for 32Mi). **Fix:** raise the memory limit, or find out why the app uses more than 48Mi.

I didn't check what changed recently, so I can't say which rollout caused these.

**Evidence**
- `kubernetes_list_deployments`: checkout, frontend and reports are all 0/1 ready with Available=False, MinimumReplicasUnavailable; frontend image is nginx:1.99.99-does-not-exist
- `kubernetes_list_pods`: checkout pod Error exit 1 (3 restarts); frontend pod Pending ErrImagePull 'docker.io/library/nginx:1.99.99-does-not-exist: not found'; reports pod OOMKilled exit 137 (3 restarts)
- `kubernetes_pod_logs`: checkout-869fc5bcc6-db5ps logs: 'FATAL: required env var DATABASE_URL is not set'
- `kubernetes_describe_pod`: reports-55b4fffc66-hmq7f container app: memory request 32Mi, limit 48Mi; state OOMKilled exit 137; BackOff restarting failed container

**Tool calls**
1. `kubernetes_list_pods` {"cluster": "kind", "namespace": "demo"}
2. `kubernetes_list_deployments` {"cluster": "kind", "namespace": "demo"}
3. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "demo", "name": "checkout-869fc5bcc6-db5ps", "tail_lines": 50}
4. `kubernetes_describe_pod` {"cluster": "kind", "namespace": "demo", "name": "reports-55b4fffc66-hmq7f"}
