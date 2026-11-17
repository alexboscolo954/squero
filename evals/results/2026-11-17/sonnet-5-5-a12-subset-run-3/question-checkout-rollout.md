**The latest rollout of `checkout` (revision 2) removed the `DATABASE_URL` env var from the `app` container, and the deployment is now broken.**

- **Revision 1** (`checkout-67d9799c78`) had `DATABASE_URL` set. It is now scaled to 0.
- **Revision 2** (`checkout-869fc5bcc6`) has no env vars at all. It rolled out about 48s before I checked.
- **Unchanged:** the image is `python:3.12-alpine` in both revisions. The memory request (32Mi) and limit (64Mi) are the same too.

**Effect:** the new pod `checkout-869fc5bcc6-db5ps` is in CrashLoopBackOff. It has restarted 3 times and exits with code 1. Its log says `FATAL: required env var DATABASE_URL is not set`. The deployment shows 0/1 ready and `Available=False` (MinimumReplicasUnavailable).

**Recommended fix:** roll back to revision 1, or restore `DATABASE_URL` in the deployment spec. The `pod-crashlooping` runbook says a restart won't help here. No past Report matches this.

**Unrelated issues in the same namespace:**
- `frontend` can't pull its image, `nginx:1.99.99-does-not-exist` (ErrImagePull).
- `reports` is being OOMKilled against its 48Mi memory limit.

**Evidence**
- `kubernetes_recent_changes`: checkout revision 2 rollout about 48s ago, diff: env_removed {app: [DATABASE_URL]}. Revision 1 was just 'created'.
- `kubernetes_describe_deployment`: Revision 2 (checkout-869fc5bcc6) has env [] and image python:3.12-alpine. Revision 1 (checkout-67d9799c78) had env [DATABASE_URL] and is at 0 replicas. The deployment is 0/1 ready and Available=False.
- `kubernetes_pod_logs`: checkout-869fc5bcc6-db5ps: 'FATAL: required env var DATABASE_URL is not set' (checkout starting, version 1.4.0)
- `kubernetes_list_pods`: checkout-869fc5bcc6-db5ps is 0/1 ready with 3 restarts and a last termination of Error, exit code 1. frontend shows ErrImagePull. reports was OOMKilled.
- `kubernetes_get_events`: BackOff warning: back-off restarting failed container app in pod checkout-869fc5bcc6-db5ps.
- `runbooks_read_runbook`: The pod-crashlooping runbook lists missing config such as an env var as the most common cause. It says the fix is a rollback or restoring the config, and that a restart does not fix a crash loop.

**Tool calls**
1. `kubernetes_describe_deployment` {"cluster": "kind", "namespace": "demo", "name": "checkout"}
2. `kubernetes_recent_changes` {"cluster": "kind"}
3. `kubernetes_list_pods` {"cluster": "kind", "namespace": "demo", "label_selector": "app=checkout"}
4. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "demo", "name": "checkout-869fc5bcc6-db5ps", "tail_lines": 20}
5. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "demo", "name": "checkout-869fc5bcc6-db5ps", "tail_lines": 1}
6. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "demo", "name": "checkout-869fc5bcc6-db5ps", "tail_lines": 5}
7. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "demo", "name": "checkout-869fc5bcc6-db5ps", "tail_lines": 10}
8. `kubernetes_describe_deployment` {"cluster": "kind", "namespace": "demo", "name": "frontend"}
9. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "demo", "name": "checkout-869fc5bcc6-db5ps", "previous": true}
10. `kubernetes_get_events` {"cluster": "kind", "namespace": "demo"}
11. `kubernetes_describe_pod` {"cluster": "kind", "namespace": "demo", "name": "checkout-869fc5bcc6-db5ps"}
12. `read_more` {"result_id": "none"}
13. `kubernetes_list_deployments` {"cluster": "kind", "namespace": "demo"}
14. `runbooks_search_runbooks` {"keywords": "missing env var crashloop"}
15. `runbooks_read_runbook` {"name": "pod-crashlooping"}
16. `incidents_search` {"keywords": "checkout DATABASE_URL"}
17. `kubernetes_get_events` {"cluster": "kind", "namespace": "demo", "warnings_only": false}
18. `kubernetes_recent_changes` {"cluster": "kind", "since_minutes": 10}
19. `kubernetes_list_pods` {"cluster": "kind", "namespace": "demo"}
20. `kubernetes_describe_deployment` {"cluster": "kind", "namespace": "demo", "name": "checkout"}
21. `runbooks_read_runbook` {"name": "deployment-unavailable"}
22. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "demo", "name": "checkout-869fc5bcc6-db5ps", "container": "app"}
23. `incidents_search` {"keywords": "checkout crashloop env var missing"}
24. `kubernetes_list_deployments` {"cluster": "kind", "namespace": "demo"}
25. `kubernetes_describe_deployment` {"cluster": "kind", "namespace": "demo", "name": "reports"}
26. `kubernetes_recent_changes` {"cluster": "kind", "since_minutes": 60} (error)
27. `read_more` {"result_id": "x"} (error)
