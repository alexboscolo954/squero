## orders-api-568b7b8d9f-vzp22 is crash looping (3 restarts, exit 1). On startup it calls GET http://inventory.orders:8080/stock and gets a 404. That happens because inventory revision 2, rolled out 48s ago, added API_VERSION and now serves only /v2/stock.

**Root cause** (high confidence, `bad_rollout`): inventory revision 2 (ReplicaSet inventory-b775d96fd, created about 44-48s ago) added the API_VERSION env var, and the service moved its endpoint to /v2/stock (its log says 'inventory serving /v2/stock'). orders-api 2.3.0 still calls the old path /stock at startup. It gets 'HTTP Error 404: Not Found', logs 'FATAL: stock sync failed' and exits with code 1. The image is the same in both revisions (python:3.12-alpine); the only change is the API_VERSION env var. One gap: orders-api is 2m old and has 3 restarts, so some crashes may have happened before revision 2. The logs from the previous run are gone, so I can't confirm that the earlier crashes had the same cause.
**Severity:** high  ·  **Affected:** orders/Deployment/orders-api, orders/Deployment/inventory

**Evidence**
- `kubernetes_list_pods`: orders-api-568b7b8d9f-vzp22 is 0/1 ready with 3 restarts; the app container ended with Error, exit code 1. inventory-b775d96fd-qq86l is Running, 44s old.
- `kubernetes_pod_logs`: orders-api log: 'FATAL: stock sync failed: GET http://inventory.orders:8080/stock: HTTP Error 404: Not Found'. Previous run's logs are gone; this is the latest instance.
- `kubernetes_pod_logs`: inventory-b775d96fd-qq86l log: 'inventory serving /v2/stock'
- `kubernetes_recent_changes`: inventory revision 2 rolled out 48s ago with env_added API_VERSION. orders-api has had no change since it was created 2m ago.
- `kubernetes_describe_deployment`: inventory revision 1 (inventory-8469585669) had no env vars; revision 2 (inventory-b775d96fd) adds API_VERSION. The image python:3.12-alpine is the same in both.

**Recommendations**
1. Roll back inventory to revision 1 (kubectl -n orders rollout undo deployment/inventory --to-revision=1) so /stock is served again. Then check that orders-api starts and becomes Ready.
2. Don't restart orders-api; that won't fix the crash loop while inventory doesn't serve /stock.
3. Before rolling the API_VERSION change out again, have inventory serve both /stock and /v2/stock, or update orders-api to call /v2/stock first.
4. Consider making the orders-api stock sync at startup retry or fail softly instead of exiting with a fatal error when a dependency returns an error.

Runbook: `pod-crashlooping`

**Tool calls**
1. `runbooks_runbook_for_alert` {"alertname": "KubePodCrashLooping"}
2. `kubernetes_list_pods` {"cluster": "kind", "namespace": "orders"}
3. `kubernetes_recent_changes` {"cluster": "kind"}
4. `incidents_search` {"keywords": "orders-api crashloop"}
5. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "orders", "name": "orders-api-568b7b8d9f-vzp22", "previous": true, "tail_lines": 50}
6. `kubernetes_describe_deployment` {"cluster": "kind", "namespace": "orders", "name": "inventory"}
7. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "orders", "name": "inventory-b775d96fd-qq86l", "tail_lines": 30}
