All three deployments in `demo` are unhealthy. Each has 0/1 ready and is not Available.

1. **checkout** is crash-looping (exit 1, 3 restarts). Its logs say `FATAL: required env var DATABASE_URL is not set`. Revision 2 rolled out about 48s before the check and removed `DATABASE_URL` from the `app` container. Revision 1 had it. Fix: restore `DATABASE_URL` or roll back to revision 1.

2. **frontend** is stuck in ErrImagePull/ImagePullBackOff. It uses the image `nginx:1.99.99-does-not-exist`, which the registry reports as not found. This has been the case since revision 1, so no earlier working version exists to roll back to. Fix: set a valid nginx tag.

3. **reports** is crash-looping with OOMKilled (exit 137, 3 restarts). The memory limit is 48Mi, and the app logs `building monthly report: loading 200MB of rows`. Fix: raise the memory limit well above 48Mi, or make the report load its rows in batches.

The `FailedScheduling` events on all three pods were from an untolerated node taint about 2 minutes ago. The pods were later scheduled and started, so this is not the current cause.

**Evidence**
- `kubernetes_list_pods`: checkout: Error exit 1, 3 restarts. frontend: ErrImagePull, image nginx:1.99.99-does-not-exist not found. reports: OOMKilled exit 137, 3 restarts.
- `kubernetes_pod_logs`: checkout log: 'FATAL: required env var DATABASE_URL is not set'. reports log: 'building monthly report: loading 200MB of rows'.
- `kubernetes_recent_changes`: checkout revision 2 rollout 48s ago, with env_removed DATABASE_URL from app.
- `kubernetes_describe_deployment`: reports memory limit is 48Mi. checkout revision 1 had env DATABASE_URL, revision 2 has none. frontend only has revision 1, with the bad image.
- `kubernetes_get_events`: BackOff events for checkout and reports. Failed/ErrImagePull/ImagePullBackOff for frontend. FailedScheduling from a node taint about 2m ago.

**Tool calls**
1. `kubernetes_list_pods` {"cluster": "kind", "namespace": "demo"}
2. `kubernetes_list_deployments` {"cluster": "kind", "namespace": "demo"}
3. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "demo", "name": "checkout-869fc5bcc6-db5ps", "tail_lines": 20}
4. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "demo", "name": "reports-55b4fffc66-hmq7f", "tail_lines": 20}
5. `kubernetes_describe_pod` {"cluster": "kind", "namespace": "demo", "name": "reports-55b4fffc66-hmq7f"}
6. `kubernetes_list_pods` {"cluster": "kind", "namespace": "demo"}
7. `kubernetes_recent_changes` {"cluster": "kind"}
8. `read_more` {"result_id": "none"}
9. `kubernetes_describe_deployment` {"cluster": "kind", "namespace": "demo", "name": "frontend"}
10. `kubernetes_describe_deployment` {"cluster": "kind", "namespace": "demo", "name": "reports"}
11. `kubernetes_describe_deployment` {"cluster": "kind", "namespace": "demo", "name": "checkout"}
12. `kubernetes_get_events` {"cluster": "kind", "namespace": "demo"}
13. `read_more` {"result_id": "x"}
