## inventory revision 2 (which added API_VERSION) now serves /v2/stock, so orders-api's request to /stock gets a 404. orders-api exits with a fatal error at startup and is crash-looping (0/1 ready, 3 restarts).

**Root cause** (high confidence, `bad_rollout`): inventory revision 2, rolled out 48s ago, added the env var API_VERSION. Its log shows it now serves '/v2/stock'. orders-api (version 2.3.0) still calls GET http://inventory.orders:8080/stock, gets HTTP 404 and exits with code 1 at startup, so it crash-loops. The orders-api image finished pulling at the same moment as the rollout (48s ago), so it never had a successful run. Together with the Hub seeing nothing failing before the change, that makes the failure a result of the inventory API change, not an earlier problem.
**Severity:** high  ·  **Affected:** orders/Deployment/orders-api, orders/Deployment/inventory

**Evidence**
- `kubernetes_recent_changes`: inventory revision 2 rolled out 48s ago; diff: env_added app: [API_VERSION]
- `kubernetes_pod_logs`: orders-api-568b7b8d9f-vzp22: 'FATAL: stock sync failed: GET http://inventory.orders:8080/stock: HTTP Error 404: Not Found'
- `kubernetes_pod_logs`: inventory-b775d96fd-qq86l: 'inventory serving /v2/stock'
- `kubernetes_describe_pod`: orders-api container app: terminated with Error, exit code 1, 3 restarts, BackOff; image pull finished 48s ago; env includes INVENTORY_URL
- `kubernetes_list_deployments`: orders-api 0/1 ready, MinimumReplicasUnavailable

**Recommendations**
1. Roll inventory back to revision 1 (kubectl rollout undo deployment/inventory -n orders), or remove the API_VERSION env var, so /stock is served again.
2. Check that the orders-api pod becomes Ready and stops restarting after the rollback.
3. Later, either have inventory keep serving the old /stock path alongside /v2, or update orders-api (INVENTORY_URL or client code) to call /v2/stock before rolling inventory revision 2 out again.

**Tool calls**
1. `kubernetes_recent_changes` {"cluster": "kind", "since_minutes": 60}
2. `kubernetes_list_pods` {"cluster": "kind", "namespace": "orders"}
3. `kubernetes_list_deployments` {"cluster": "kind", "namespace": "orders"}
4. `kubernetes_get_events` {"cluster": "kind", "namespace": "orders"}
5. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "orders", "name": "orders-api-568b7b8d9f-vzp22", "tail_lines": 50}
6. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "orders", "name": "inventory-b775d96fd-qq86l", "tail_lines": 50}
7. `kubernetes_describe_pod` {"cluster": "kind", "namespace": "orders", "name": "orders-api-568b7b8d9f-vzp22"}
8. `incidents_search` {"keywords": "orders-api inventory stock 404"}
