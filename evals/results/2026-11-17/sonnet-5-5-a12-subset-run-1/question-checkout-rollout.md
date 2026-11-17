**The latest rollout (revision 2) removed the `DATABASE_URL` env var from the `app` container. That is what is breaking checkout.**

- Revision 2 is ReplicaSet `checkout-869fc5bcc6`. It was created about 36–48s before I checked.
- The only change from revision 1 (`checkout-67d9799c78`) is `env_removed: app: [DATABASE_URL]`. The image is `python:3.12-alpine` in both revisions, and the memory request (32Mi) and limit (64Mi) are the same.
- **Impact:** The new pod `checkout-869fc5bcc6-db5ps` is Running but 0/1 ready. It has exited with code 1 and restarted 3 times, and events show BackOff. Its logs say `FATAL: required env var DATABASE_URL is not set`. The deployment reports Available=False (MinimumReplicasUnavailable), with 0 of 1 replicas ready.
- **Fix:** Roll back to revision 1, or restore `DATABASE_URL` in the deployment spec. The crash-loop runbook says a rollout that causes a crash loop should be rolled back, and that a restart won't help.
- I found no past Reports about this.

**Evidence**
- `kubernetes_recent_changes`: checkout revision 2 rollout 48s ago with diff env_removed: {app: [DATABASE_URL]}. Revision 1 was the creation about 2m ago.
- `kubernetes_describe_deployment`: Revision 2 (checkout-869fc5bcc6) has env [] and revision 1 (checkout-67d9799c78) has env [DATABASE_URL]. The image is python:3.12-alpine in both. Ready is 0/1 and Available=False.
- `kubernetes_pod_logs`: checkout-869fc5bcc6-db5ps logs: 'FATAL: required env var DATABASE_URL is not set'.
- `kubernetes_list_pods`: checkout-869fc5bcc6-db5ps is 0/1 ready with 3 restarts and last terminated Error (exit code 1).
- `kubernetes_get_events`: Warning BackOff: restarting failed container app in pod checkout-869fc5bcc6-db5ps.
- `runbooks_read_runbook`: The pod-crashlooping runbook lists missing env config as the most common cause. It says to roll back a revision that caused the crash loop and not to restart.

**Tool calls**
1. `kubernetes_describe_deployment` {"cluster": "kind", "namespace": "demo", "name": "checkout"}
2. `kubernetes_recent_changes` {"cluster": "kind"}
3. `kubernetes_list_pods` {"cluster": "kind", "namespace": "demo", "label_selector": "app=checkout"}
4. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "demo", "name": "checkout-869fc5bcc6-db5ps", "tail_lines": 20}
5. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "demo", "name": "checkout-869fc5bcc6-db5ps", "tail_lines": 1, "previous": true}
6. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "demo", "name": "checkout-869fc5bcc6-db5ps", "tail_lines": 5}
7. `kubernetes_get_events` {"cluster": "kind", "namespace": "demo"}
8. `read_more` {"result_id": "none"}
9. `runbooks_search_runbooks` {"keywords": "missing env var crashloop"}
10. `kubernetes_list_deployments` {"cluster": "kind", "namespace": "demo"}
11. `kubernetes_describe_pod` {"cluster": "kind", "namespace": "demo", "name": "checkout-869fc5bcc6-db5ps"}
12. `runbooks_read_runbook` {"name": "pod-crashlooping"}
13. `incidents_search` {"keywords": "checkout DATABASE_URL"}
14. `kubernetes_get_events` {"cluster": "kind", "namespace": "demo", "warnings_only": false}
15. `kubernetes_describe_pod` {"cluster": "kind", "namespace": "demo", "name": "checkout-869fc5bcc6-db5ps"}
16. `runbooks_read_runbook` {"name": "deployment-unavailable"}
17. `kubernetes_describe_deployment` {"cluster": "kind", "namespace": "demo", "name": "checkout"}
18. `kubernetes_recent_changes` {"cluster": "kind", "since_minutes": 30}
19. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "demo", "name": "checkout-869fc5bcc6-db5ps", "container": "app", "tail_lines": 50}
20. `runbooks_search_runbooks` {"keywords": "rollback"}
21. `kubernetes_list_pods` {"cluster": "kind", "namespace": "demo", "label_selector": "app=checkout"}
22. `runbooks_read_runbook` {"name": "pod-crashlooping"}
23. `kubernetes_describe_deployment` {"cluster": "kind", "namespace": "demo", "name": "checkout"}
24. `incidents_search` {"keywords": "checkout crashloop env var missing"}
25. `kubernetes_recent_changes` {"cluster": "kind", "since_minutes": 10}
26. `runbooks_read_runbook` {"name": "deployment-unavailable"} (error)
