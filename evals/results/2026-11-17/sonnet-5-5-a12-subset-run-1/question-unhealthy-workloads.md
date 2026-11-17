All three deployments in `demo` are unhealthy. Each has 0/1 ready replicas and the condition Available=False (MinimumReplicasUnavailable). Each fails for a different reason.

1. **checkout** is crash-looping (Error, exit code 1, 3 restarts). Its logs say `FATAL: required env var DATABASE_URL is not set`. Revision 2 rolled out about 48s ago and removed the `DATABASE_URL` env var from the `app` container. Revision 1 had it. The fix is to restore `DATABASE_URL` or roll back to revision 1.
2. **frontend** can't start (ErrImagePull / ImagePullBackOff). It uses the image `nginx:1.99.99-does-not-exist`, and the registry returns "not found" for that tag. The fix is to set a valid nginx tag. It has only ever had revision 1, so there is nothing to roll back to.
3. **reports** is crash-looping with OOMKilled (exit code 137, 3 restarts). Its memory limit is 48Mi, and its log says `building monthly report: loading 200MB of rows`. The fix is to raise the memory limit well above 48Mi, or make the job load its rows in smaller batches.

The early FailedScheduling events (untolerated taint) were from about 2 minutes ago. The pods were scheduled and started afterwards, so that is not the current cause.

**Evidence**
- `kubernetes_list_pods`: checkout 0/1, Error exit 1, 3 restarts; frontend Pending with ErrImagePull for nginx:1.99.99-does-not-exist (not found); reports 0/1, OOMKilled exit 137, 3 restarts.
- `kubernetes_list_deployments`: checkout, frontend and reports all 0/1 ready with Available=False MinimumReplicasUnavailable.
- `kubernetes_pod_logs`: checkout log: 'FATAL: required env var DATABASE_URL is not set'.
- `kubernetes_recent_changes`: checkout revision 2 rolled out 48s ago with env_removed: app: [DATABASE_URL].
- `kubernetes_describe_pod`: reports container has memory limit 48Mi and was OOMKilled (exit 137), with BackOff restarts. Its earlier FailedScheduling taint event was followed by a successful schedule.
- `kubernetes_get_events`: BackOff events for checkout and reports; Failed/ErrImagePull/ImagePullBackOff for frontend with 'not found' for the nginx tag.

**Tool calls**
1. `kubernetes_list_pods` {"cluster": "kind", "namespace": "demo"}
2. `kubernetes_list_deployments` {"cluster": "kind", "namespace": "demo"}
3. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "demo", "name": "checkout-869fc5bcc6-db5ps", "tail_lines": 30}
4. `kubernetes_describe_pod` {"cluster": "kind", "namespace": "demo", "name": "reports-55b4fffc66-hmq7f"}
5. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "demo", "name": "reports-55b4fffc66-hmq7f", "tail_lines": 20}
6. `kubernetes_recent_changes` {"cluster": "kind", "since_minutes": 60}
7. `kubernetes_list_pods` {"cluster": "kind", "namespace": "demo"}
8. `kubernetes_get_events` {"cluster": "kind", "namespace": "demo"}
9. `read_more` {"result_id": "none"}
10. `kubernetes_describe_deployment` {"cluster": "kind", "namespace": "demo", "name": "frontend"}
11. `kubernetes_describe_deployment` {"cluster": "kind", "namespace": "demo", "name": "checkout"}
12. `kubernetes_describe_deployment` {"cluster": "kind", "namespace": "demo", "name": "reports"}
13. `kubernetes_get_events` {"cluster": "kind", "namespace": "demo", "warnings_only": false}
