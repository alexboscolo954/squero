All three deployments in `demo` are unhealthy. Each shows 0/1 ready and `MinimumReplicasUnavailable`.

1. **checkout** is crash-looping (exit code 1, 3 restarts).
   - **Cause:** the container logs `FATAL: required env var DATABASE_URL is not set`.
   - **Trigger:** revision 2 rolled out about 48s ago and removed the `DATABASE_URL` env var from the `app` container.
   - **Fix:** restore `DATABASE_URL` on the deployment, or roll back to revision 1.

2. **frontend** is stuck in `ErrImagePull` / `ImagePullBackOff`.
   - **Cause:** it uses the image `nginx:1.99.99-does-not-exist`, and the registry returns "not found" for that tag.
   - **Fix:** point it at a valid nginx tag.

3. **reports** is crash-looping because it is `OOMKilled` (exit 137, 3 restarts).
   - **Cause:** the memory limit is 48Mi (request 32Mi), which is too low for the container.
   - **Fix:** raise the memory limit, or reduce the app's memory use.
   - **Not checked:** I didn't look at the reports container's logs or its actual memory usage, so the right new limit is unknown.

The `FailedScheduling` events (untolerated node taint) were seen about 2 minutes ago. The pods have since been scheduled, so this is not the current cause.

**Evidence**
- `kubernetes_list_pods`: checkout: Error exit 1, 3 restarts. frontend: ErrImagePull for nginx:1.99.99-does-not-exist (not found). reports: OOMKilled exit 137, 3 restarts. All pods 0/1 ready.
- `kubernetes_list_deployments`: checkout, frontend and reports are all 0/1 ready with Available=False (MinimumReplicasUnavailable).
- `kubernetes_pod_logs`: checkout pod log: 'FATAL: required env var DATABASE_URL is not set'.
- `kubernetes_recent_changes`: checkout revision 2 rolled out 48s ago with env_removed: app: [DATABASE_URL].
- `kubernetes_describe_pod`: reports pod: memory limit 48Mi (request 32Mi), last state OOMKilled exit 137, BackOff restarts.
- `kubernetes_get_events`: frontend: Failed to pull image nginx:1.99.99-does-not-exist (not found), ErrImagePull and ImagePullBackOff. BackOff events for checkout and reports. FailedScheduling due to a taint was seen only about 2m ago.

**Tool calls**
1. `kubernetes_list_pods` {"cluster": "kind", "namespace": "demo"}
2. `kubernetes_list_deployments` {"cluster": "kind", "namespace": "demo"}
3. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "demo", "name": "checkout-869fc5bcc6-db5ps", "tail_lines": 30}
4. `kubernetes_describe_pod` {"cluster": "kind", "namespace": "demo", "name": "reports-55b4fffc66-hmq7f"}
5. `kubernetes_recent_changes` {"cluster": "kind"}
6. `kubernetes_get_events` {"cluster": "kind", "namespace": "demo"}
