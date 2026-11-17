All three deployments in `demo` are unhealthy. Each has 0/1 ready and shows "Deployment does not have minimum availability".

1. **checkout** is crash-looping (exit code 1, 3 restarts, BackOff).
   - Cause: its revision 2 rollout, about 48s before I checked, removed the `DATABASE_URL` env var from the `app` container.
   - The container logs `FATAL: required env var DATABASE_URL is not set` and exits.
   - Fix: restore `DATABASE_URL` on the deployment, or roll back to revision 1.

2. **frontend** is stuck in ErrImagePull/ImagePullBackOff.
   - Cause: the image `nginx:1.99.99-does-not-exist` doesn't exist (registry returns NotFound).
   - Fix: set a valid nginx tag.
   - I saw no rollout record for frontend beyond its initial creation, so the bad tag was in the deployment from the start.

3. **reports** is crash-looping with OOMKilled (exit 137, 3 restarts).
   - Cause: the memory limit is 48Mi (request 32Mi), and the app logs "building monthly report: loading 200MB of rows". That load is far more than the limit allows.
   - Fix: raise the memory limit well above the working set, or make the job stream or paginate the rows.

The earlier FailedScheduling events (untolerated node taint) were seen about 2m ago and the pods did get scheduled afterwards. They are not the current cause.

**Evidence**
- `kubernetes_list_pods`: checkout 0/1 Error exit 1 with 3 restarts; frontend ErrImagePull for nginx:1.99.99-does-not-exist (not found); reports OOMKilled exit 137 with 3 restarts
- `kubernetes_list_deployments`: checkout, frontend and reports are all 0/1 ready with Available=False (MinimumReplicasUnavailable)
- `kubernetes_pod_logs`: checkout logs: 'FATAL: required env var DATABASE_URL is not set'. Reports logs, from a separate call: 'building monthly report: loading 200MB of rows'.
- `kubernetes_recent_changes`: checkout revision 2 rolled out 48s ago with env_removed app: [DATABASE_URL]. Frontend and reports show only their initial creation.
- `kubernetes_describe_pod`: reports container has memory limit 48Mi (request 32Mi), last state OOMKilled exit 137, BackOff events
- `kubernetes_get_events`: frontend Failed events show ErrImagePull/ImagePullBackOff, NotFound for the nginx tag; BackOff events on checkout and reports pods

**Tool calls**
1. `kubernetes_list_pods` {"cluster": "kind", "namespace": "demo"}
2. `kubernetes_list_deployments` {"cluster": "kind", "namespace": "demo"}
3. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "demo", "name": "checkout-869fc5bcc6-db5ps", "tail_lines": 30}
4. `kubernetes_describe_pod` {"cluster": "kind", "namespace": "demo", "name": "reports-55b4fffc66-hmq7f"}
5. `kubernetes_recent_changes` {"cluster": "kind"}
6. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "demo", "name": "reports-55b4fffc66-hmq7f", "tail_lines": 20}
7. `kubernetes_get_events` {"cluster": "kind", "namespace": "demo"}
