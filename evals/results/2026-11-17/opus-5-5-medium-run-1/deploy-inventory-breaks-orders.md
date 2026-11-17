## orders-api is crashlooping since the inventory revision 2 rollout. inventory now serves /v2/stock, while orders-api still calls /stock and gets 404 when it starts up.

**Root cause** (high confidence, `bad_rollout`): inventory revision 2 (48s ago) added the API_VERSION env var, and the new pod logs 'inventory serving /v2/stock'. Since then orders-api (version 2.3.0) calls GET http://inventory.orders:8080/stock, gets HTTP 404 and exits with code 1. It has restarted 3 times. orders-api has had only one revision, and the Hub saw nothing failing just before the change, so the inventory API path change is what broke it.
**Severity:** high  ·  **Affected:** orders/Deployment/orders-api, orders/Deployment/inventory

**Evidence**
- `kubernetes_recent_changes`: inventory revision 2 added the env var API_VERSION 48s ago. orders-api has had no change since it was created 2m ago.
- `kubernetes_pod_logs`: orders-api: 'FATAL: stock sync failed: GET http://inventory.orders:8080/stock: HTTP Error 404: Not Found'. inventory: 'inventory serving /v2/stock'.
- `kubernetes_list_pods`: orders-api-568b7b8d9f-vzp22 is 0/1 with 3 restarts and exit code 1.
- `kubernetes_describe_deployment`: orders-api: 0 of 1 replicas ready, Available=False (MinimumReplicasUnavailable). Only revision 1 exists, with env var INVENTORY_URL.
- `kubernetes_get_events`: BackOff restarting failed container app in orders-api-568b7b8d9f-vzp22 (count 3).

**Recommendations**
1. Roll inventory back to revision 1 (kubectl rollout undo deployment/inventory -n orders), or remove the API_VERSION env var so it serves /stock again. This needs human approval.
2. Check that orders-api becomes ready and stops restarting.
3. Before rolling out the v2 API again, keep /stock working alongside /v2/stock, or update orders-api (INVENTORY_URL or its client code) to call /v2/stock first.

**Tool calls**
1. `kubernetes_recent_changes` {"cluster": "kind", "since_minutes": 60}
2. `kubernetes_list_pods` {"cluster": "kind", "namespace": "orders"}
3. `kubernetes_get_events` {"cluster": "kind", "namespace": "orders"}
4. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "orders", "name": "orders-api-568b7b8d9f-vzp22", "tail_lines": 50}
5. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "orders", "name": "inventory-b775d96fd-qq86l", "tail_lines": 50}
6. `kubernetes_describe_deployment` {"cluster": "kind", "namespace": "orders", "name": "orders-api"}
7. `incidents_search` {"keywords": "orders-api inventory stock 404"}
