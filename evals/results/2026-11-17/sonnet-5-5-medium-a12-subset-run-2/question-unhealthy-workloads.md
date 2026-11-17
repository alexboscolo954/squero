All three deployments in `demo` are unhealthy. Each shows 0/1 ready and Available=False (MinimumReplicasUnavailable).

1. **checkout** is crash-looping (Error, exit 1, 3 restarts).
   - The container logs `FATAL: required env var DATABASE_URL is not set`.
   - Rollout revision 2, about 48s before the check, removed the `DATABASE_URL` env var from the `app` container. Revision 1 had it.
   - Fix: restore `DATABASE_URL` or roll back to revision 1.

2. **frontend** is stuck in ErrImagePull / ImagePullBackOff.
   - It uses image `nginx:1.99.99-does-not-exist`, which the registry reports as not found.
   - It has only ever had revision 1, so the bad tag was there from creation.
   - Fix: set a valid nginx tag.

3. **reports** is crash-looping with OOMKilled (exit 137, 3 restarts).
   - Its memory limit is 48Mi, and the app logs `building monthly report: loading 200MB of rows`.
   - Fix: raise the memory limit well above that, or make the job load less data at once.

All three pods also had an early FailedScheduling event (the node had an untolerated taint) about 2m ago. They were scheduled afterwards, so it isn't the current cause.

**Evidence**
- `kubernetes_list_deployments`: checkout, frontend and reports are all 0/1 ready with Available=False (MinimumReplicasUnavailable); frontend image is nginx:1.99.99-does-not-exist.
- `kubernetes_pod_logs`: checkout pod logs: 'FATAL: required env var DATABASE_URL is not set'. reports pod logs: 'building monthly report: loading 200MB of rows'.
- `kubernetes_recent_changes`: checkout revision 2 rolled out 48s ago with env_removed: DATABASE_URL on the app container.
- `kubernetes_describe_pod`: reports pod: OOMKilled exit 137 with a 48Mi memory limit. frontend pod: ErrImagePull / ImagePullBackOff, 'nginx:1.99.99-does-not-exist: not found'.

**Tool calls**
1. `kubernetes_list_pods` {"cluster": "kind", "namespace": "demo"}
2. `kubernetes_list_deployments` {"cluster": "kind", "namespace": "demo"}
3. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "demo", "name": "checkout-869fc5bcc6-db5ps", "tail_lines": 20}
4. `kubernetes_describe_pod` {"cluster": "kind", "namespace": "demo", "name": "reports-55b4fffc66-hmq7f"}
5. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "demo", "name": "reports-55b4fffc66-hmq7f", "tail_lines": 10}
6. `kubernetes_describe_pod` {"cluster": "kind", "namespace": "demo", "name": "frontend-6f6b4986b9-hxdf2"}
7. `kubernetes_get_events` {"cluster": "kind", "namespace": "demo", "warnings_only": true}
8. `kubernetes_recent_changes` {"cluster": "kind"}
9. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "demo", "name": "checkout-869fc5bcc6-db5ps", "tail_lines": 5, "previous": true}
10. `kubernetes_list_pods` {"cluster": "kind", "namespace": "demo"}
11. `kubernetes_list_deployments` {"cluster": "kind", "namespace": "demo"}
12. `kubernetes_get_events` {"cluster": "kind", "namespace": "demo", "warnings_only": false}
13. `read_more` {"result_id": "none"}
14. `kubernetes_describe_deployment` {"cluster": "kind", "namespace": "demo", "name": "frontend"}
15. `kubernetes_describe_deployment` {"cluster": "kind", "namespace": "demo", "name": "checkout"}
16. `kubernetes_describe_deployment` {"cluster": "kind", "namespace": "demo", "name": "reports"}
17. `kubernetes_list_pods` {"cluster": "kind", "namespace": "demo", "label_selector": "app=checkout"}
18. `kubernetes_get_events` {"cluster": "kind", "namespace": "demo"}
19. `read_more` {"result_id": "x"}
