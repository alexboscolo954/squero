## orders-api has crash looped 3 times (exit 1). At startup it gets HTTP 404 from GET http://inventory.orders:8080/stock. This started right after inventory revision 2, which added the API_VERSION env var, and inventory now says it serves /v2/stock. A rollback of inventory is the likely fix.

**Root cause** (medium confidence, `bad_rollout`): About 48 seconds before the crashes, the inventory deployment rolled out revision 2. The only change was a new env var, API_VERSION. The new inventory pod logs 'inventory serving /v2/stock', which suggests the endpoint moved from /stock to /v2/stock. orders-api 2.3.0 still calls GET http://inventory.orders:8080/stock at startup, gets 'HTTP Error 404: Not Found', logs FATAL and exits with code 1. The host name is masked, so I'm assuming it is the inventory service, which listens on 8080. orders-api itself is still on its only revision (1), so the break most likely comes from the inventory change. Two points are inferred: I didn't check what revision 1 of inventory served, and the host is masked.
**Severity:** high  ·  **Affected:** orders/Deployment/orders-api, orders/Deployment/inventory

**Evidence**
- `kubernetes_list_pods`: orders-api-568b7b8d9f-vzp22 is 0/1 ready with 3 restarts; last terminated 'Error' with exit code 1. The inventory pod is new (44s old) and Running.
- `kubernetes_pod_logs`: orders-api: 'FATAL: stock sync failed: GET http://inventory.orders:8080/stock: HTTP Error 404: Not Found'. inventory: 'inventory serving /v2/stock'.
- `kubernetes_recent_changes`: inventory revision 2 rolled out 48s ago and only added env API_VERSION. orders-api has had no change since it was created 2m ago (revision 1).
- `kubernetes_describe_deployment`: inventory rollout history: revision 1 (inventory-8469585669) had no env vars; revision 2 (inventory-b775d96fd) added API_VERSION. Same image python:3.12-alpine in both.

**Recommendations**
1. Roll back inventory to revision 1 (kubectl -n orders rollout undo deployment/inventory --to-revision=1), or remove or correct API_VERSION so inventory serves /stock again. This needs approval.
2. Check that GET /stock on inventory returns 200 again and that orders-api goes Ready. It should recover on its next restart after the backoff, so no manual restart is needed.
3. Confirm that inventory.orders is the inventory service.
4. If moving to /v2 is intended, coordinate it: update orders-api to call /v2/stock first, or have inventory serve both /stock and /v2/stock during the migration.
5. Make orders-api's startup stock sync retry and fail softly instead of exiting fatally on an error from a dependency.

Runbook: `pod-crashlooping`

**Tool calls**
1. `runbooks_runbook_for_alert` {"alertname": "KubePodCrashLooping"}
2. `kubernetes_list_pods` {"cluster": "kind", "namespace": "orders"}
3. `kubernetes_recent_changes` {"cluster": "kind"}
4. `incidents_search` {"keywords": "orders-api crash"}
5. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "orders", "name": "orders-api-568b7b8d9f-vzp22", "previous": true, "tail_lines": 50}
6. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "orders", "name": "inventory-b775d96fd-qq86l", "tail_lines": 50}
7. `kubernetes_describe_deployment` {"cluster": "kind", "namespace": "orders", "name": "inventory"}
