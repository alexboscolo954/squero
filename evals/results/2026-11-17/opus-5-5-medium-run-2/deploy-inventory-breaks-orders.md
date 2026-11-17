## The inventory revision 2 rollout added API_VERSION, and inventory now serves /v2/stock instead of /stock. orders-api, which depends on it, gets a 404 at startup and is in CrashLoopBackOff.

**Root cause** (high confidence, `bad_rollout`): inventory revision 2 (ReplicaSet inventory-b775d96fd, rolled out 48s ago) added the env var API_VERSION, and inventory now logs 'inventory serving /v2/stock'. orders-api 2.3.0 calls GET http://inventory.orders:8080/stock at startup, gets HTTP 404 and exits with code 1. Its container first came up about when the rollout happened (image pulled 48s ago) and has failed on every start since: 3 restarts, 0/1 ready. The Hub saw nothing failing just before the change.
**Severity:** high  ·  **Affected:** orders/Deployment/orders-api, orders/Deployment/inventory

**Evidence**
- `kubernetes_recent_changes`: inventory revision 2 rolled out 48s ago. The only change is the new env var API_VERSION.
- `kubernetes_describe_deployment`: inventory revision 2 (inventory-b775d96fd) has env API_VERSION. Revision 1 (inventory-8469585669) had no env vars and is now at 0 replicas.
- `kubernetes_pod_logs`: orders-api: 'FATAL: stock sync failed: GET http://inventory.orders:8080/stock: HTTP Error 404: Not Found'. inventory: 'inventory serving /v2/stock'.
- `kubernetes_describe_pod`: orders-api-568b7b8d9f-vzp22: Ready=False, 3 restarts, exit code 1. Its image was pulled 48s ago and it has hit BackOff since.
- `kubernetes_list_pods`: orders-api is 0/1 ready with 3 restarts. inventory is 1/1 ready with 0 restarts.

**Recommendations**
1. Roll inventory back to revision 1 (kubectl rollout undo deployment/inventory -n orders), or remove API_VERSION, so it serves /stock again.
2. Confirm orders-api becomes ready after its next restart.
3. Before rolling out API_VERSION again, either have inventory serve both /stock and /v2/stock, or move orders-api (its INVENTORY_URL setting or code) to /v2/stock first.

**Tool calls**
1. `kubernetes_recent_changes` {"cluster": "kind", "since_minutes": 60}
2. `kubernetes_list_pods` {"cluster": "kind", "namespace": "orders"}
3. `kubernetes_get_events` {"cluster": "kind", "namespace": "orders"}
4. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "orders", "name": "orders-api-568b7b8d9f-vzp22", "tail_lines": 50}
5. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "orders", "name": "inventory-b775d96fd-qq86l", "tail_lines": 50}
6. `kubernetes_describe_deployment` {"cluster": "kind", "namespace": "orders", "name": "inventory"}
7. `incidents_search` {"keywords": "orders-api inventory stock 404"}
8. `kubernetes_describe_pod` {"cluster": "kind", "namespace": "orders", "name": "orders-api-568b7b8d9f-vzp22"}
