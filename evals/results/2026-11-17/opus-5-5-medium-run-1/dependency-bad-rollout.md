## orders-api is crash looping (exit 1, 3 restarts) because it can't sync stock at startup. Its call to GET http://inventory.orders:8080/stock returns 404. The inventory rollout to revision 2, about 48s ago, added API_VERSION, and inventory now serves only /v2/stock.

**Root cause** (high confidence, `bad_rollout`): Inventory revision 2 (ReplicaSet inventory-b775d96fd, created 44s ago) added the env var API_VERSION. Revision 1 had no env vars. The new inventory pod logs 'inventory serving /v2/stock', so it no longer serves the /stock path. orders-api 2.3.0 still calls /stock at startup, gets 'HTTP Error 404: Not Found', logs FATAL and exits with code 1. This causes the CrashLoopBackOff. I can't see the value of API_VERSION (the tools never return env values). The link between the env change and the path change is inferred from the timing and the new log line.
**Severity:** high  ·  **Affected:** orders/Deployment/orders-api, orders/Deployment/inventory

**Evidence**
- `kubernetes_list_pods`: orders-api-568b7b8d9f-vzp22 is 0/1 ready with 3 restarts, last terminated with Error, exit code 1. The inventory pod is 44s old and running.
- `kubernetes_pod_logs`: orders-api logs 'FATAL: stock sync failed: GET http://inventory.orders:8080/stock: HTTP Error 404: Not Found'. The inventory pod logs 'inventory serving /v2/stock'.
- `kubernetes_recent_changes`: inventory revision 2 was rolled out 48s ago with env_added API_VERSION. orders-api has had no rollout since it was created.
- `kubernetes_describe_deployment`: inventory revision 2 has env [API_VERSION]. Revision 1 had no env vars and is now scaled to 0.

**Recommendations**
1. Roll back inventory to revision 1: kubectl -n orders rollout undo deployment/inventory --to-revision=1. This should bring back the /stock endpoint.
2. Check that inventory serves /stock again, then watch orders-api recover from CrashLoopBackOff on its own. A restart alone won't fix it.
3. Before rolling out API_VERSION again, either have inventory keep serving the old /stock path alongside /v2, or move orders-api to /v2/stock first.
4. Consider having orders-api retry or degrade gracefully when the stock sync fails at startup, rather than exiting.

Runbook: `pod-crashlooping`

**Tool calls**
1. `runbooks_runbook_for_alert` {"alertname": "KubePodCrashLooping"}
2. `kubernetes_list_pods` {"cluster": "kind", "namespace": "orders"}
3. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "orders", "name": "orders-api-568b7b8d9f-vzp22", "previous": true, "tail_lines": 50}
4. `kubernetes_recent_changes` {"cluster": "kind"}
5. `incidents_search` {"keywords": "orders-api crash"}
6. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "orders", "name": "inventory-b775d96fd-qq86l", "tail_lines": 30}
7. `kubernetes_describe_deployment` {"cluster": "kind", "namespace": "orders", "name": "inventory"}
