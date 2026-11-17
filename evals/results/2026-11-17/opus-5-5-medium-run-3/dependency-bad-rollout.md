## orders-api is crash looping (exit 1, 3 restarts) because it fails at startup when it calls GET http://inventory.orders:8080/stock and gets a 404. The inventory deployment was rolled out to revision 2 about 48s ago, adding the API_VERSION env var, and inventory now logs that it serves /v2/stock.

**Root cause** (high confidence, `bad_rollout`): Inventory revision 2 (ReplicaSet inventory-b775d96fd, created 44s ago) added the env var API_VERSION, which revision 1 did not have. The new inventory pod logs 'inventory serving /v2/stock', so the API path appears to have moved. orders-api 2.3.0 still calls /stock at startup, gets 'HTTP Error 404: Not Found', logs FATAL and exits with code 1. That makes this a breaking API change from a dependency's rollout. Two things are not confirmed: the crashed run's logs were gone, so the cited log line comes from the latest instance; and the timeline does not prove whether the earlier restarts came after the inventory rollout.
**Severity:** high  ·  **Affected:** orders/Deployment/orders-api, orders/Deployment/inventory

**Evidence**
- `kubernetes_list_pods`: orders-api-568b7b8d9f-vzp22 is Running but 0/1 ready, with 3 restarts; its last termination was Error, exit code 1. inventory-b775d96fd-qq86l is 1/1 ready and 44s old.
- `kubernetes_pod_logs`: Latest orders-api instance (previous logs gone): 'orders-api starting, version 2.3.0' then 'FATAL: stock sync failed: GET http://inventory.orders:8080/stock: HTTP Error 404: Not Found'.
- `kubernetes_pod_logs`: inventory-b775d96fd-qq86l logs 'inventory serving /v2/stock'.
- `kubernetes_recent_changes`: inventory revision 2 rolled out 48s ago with env_added API_VERSION. orders-api has only revision 1, created 2m ago, and no later change.
- `kubernetes_describe_deployment`: inventory revision 1 (inventory-8469585669) had no env vars. Revision 2 (inventory-b775d96fd) adds API_VERSION.

**Recommendations**
1. Roll back inventory to revision 1 (kubectl -n orders rollout undo deployment/inventory --to-revision=1) so /stock is served again. This needs approval.
2. Once inventory is rolled back, check that orders-api's next start completes the stock sync and the pod becomes Ready. It is already restarting on its own backoff, so there is no need to restart it.
3. Coordinate the API version change. Either make orders-api call /v2/stock, or have inventory serve both /stock and /v2/stock during the migration, before rolling out API_VERSION again.
4. Consider making orders-api retry or degrade gracefully when the stock sync fails at startup, instead of exiting fatally.

Runbook: `pod-crashlooping`

**Tool calls**
1. `runbooks_runbook_for_alert` {"alertname": "KubePodCrashLooping"}
2. `kubernetes_list_pods` {"cluster": "kind", "namespace": "orders"}
3. `kubernetes_recent_changes` {"cluster": "kind"}
4. `incidents_search` {"keywords": "orders-api crash"}
5. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "orders", "name": "orders-api-568b7b8d9f-vzp22", "previous": true, "tail_lines": 50}
6. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "orders", "name": "inventory-b775d96fd-qq86l", "tail_lines": 50}
7. `kubernetes_describe_deployment` {"cluster": "kind", "namespace": "orders", "name": "inventory"}
