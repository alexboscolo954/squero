**Passed:** The audit deployment (revision 1, created 2m ago) is healthy: 1/1 replicas ready on the new revision, 0 restarts and no warning events. The log tool returned no data for its pod, which runs only the pause:3.10 image. orders-api is still in CrashLoopBackOff (exit code 1, 17 restarts, pod 1h old). It was already failing before the change, in the same way, so the Check doesn't fail for it. Nothing else in the namespace started failing after the change.

**Evidence**
- `kubernetes_recent_changes`: audit revision 1 was created 2m ago with no diff. The only other change was the inventory revision 5 rollout 14m ago, which added the env vars API_VERSION and BUILD and came before the audit change.
- `kubernetes_list_deployments`: audit is 1/1 ready with 1 updated (image registry.k8s.io/pause:3.10) and no problems. inventory is 1/1. orders-api is 0/1 with MinimumReplicasUnavailable.
- `kubernetes_list_pods`: audit-6cfdd68f8c-2xr5p is Running and 1/1 ready with 0 restarts, age 2m. inventory-b775d96fd-rmlvd is 1/1 with 0 restarts. orders-api-79cd5d49dc-28wnk is in CrashLoopBackOff with 17 restarts, last exit Error code 1, age 1h.
- `kubernetes_get_events`: There are no warning events for audit. orders-api has a BackOff warning seen 55 times, which is the same failure that existed before the change. The inventory readiness-probe warnings were last seen 11m and 14m ago, during inventory's own rollout and before audit was created.
- `kubernetes_pod_logs`: Returned 'no recorded data' for audit-6cfdd68f8c-2xr5p. The pause image normally writes no logs, and pod status shows nothing failing.

**Tool calls**
1. `kubernetes_recent_changes` {"cluster": "kind", "since_minutes": 60}
2. `kubernetes_list_pods` {"cluster": "kind", "namespace": "orders"}
3. `kubernetes_get_events` {"cluster": "kind", "namespace": "orders"}
4. `kubernetes_list_deployments` {"cluster": "kind", "namespace": "orders"}
5. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "orders", "name": "audit-6cfdd68f8c-2xr5p", "tail_lines": 50} (error)
6. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "orders", "name": "orders-api-79cd5d49dc-28wnk", "tail_lines": 30} (error)
