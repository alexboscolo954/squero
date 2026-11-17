## orders-api has 0/1 replicas ready. It crashes on startup because its stock sync to inventory gets HTTP 404. inventory revision 2, rolled out 48s ago, added API_VERSION and now serves only /v2/stock, while orders-api still calls /stock.

**Root cause** (medium confidence, `bad_rollout`): inventory revision 2, rolled out 48s ago, added the env var API_VERSION. The inventory pod now logs 'inventory serving /v2/stock'. orders-api 2.3.0 calls GET http://inventory.orders:8080/stock at startup, gets 404 Not Found, logs FATAL and exits with code 1, which puts it in a crash loop. One caveat: orders-api's image pull finished about 48s ago, the same time as the inventory rollout, so it never ran against inventory revision 1. That means there is no direct proof that it worked with the old inventory. The link rests on the API path mismatch.
**Severity:** high  ·  **Affected:** orders/Deployment/orders-api, orders/Deployment/inventory

**Evidence**
- `kubernetes_pod_logs`: orders-api-568b7b8d9f-vzp22: 'orders-api starting, version 2.3.0' then 'FATAL: stock sync failed: GET http://inventory.orders:8080/stock: HTTP Error 404: Not Found'
- `kubernetes_pod_logs`: inventory-b775d96fd-qq86l: 'inventory serving /v2/stock'
- `kubernetes_recent_changes`: inventory rolled out to revision 2 48s ago, adding env var API_VERSION; orders-api created 2m ago (revision 1)
- `kubernetes_describe_pod`: orders-api pod: container exited with code 1, BackOff restarting failed container, 4 starts; env INVENTORY_URL; image pull finished 48s ago
- `kubernetes_list_deployments`: orders-api ready 0/1 (MinimumReplicasUnavailable); inventory ready 1/1

**Recommendations**
1. Roll back inventory to revision 1 (kubectl rollout undo deployment/inventory -n orders), or remove or change API_VERSION, so inventory serves /stock again. orders-api should then become ready on its next restart.
2. Or, if the move to v2 is intended, point orders-api's INVENTORY_URL or client at /v2/stock and roll out orders-api.
3. Afterwards, confirm orders-api reaches 1/1 ready and its logs show the stock sync succeeding.
4. Make the inventory API version change backward compatible (serve both /stock and /v2/stock) until all callers have moved.

**Tool calls**
1. `kubernetes_list_deployments` {"cluster": "kind", "namespace": "orders"}
2. `kubernetes_list_pods` {"cluster": "kind", "namespace": "orders"}
3. `kubernetes_recent_changes` {"cluster": "kind"}
4. `incidents_search` {"keywords": "orders-api"}
5. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "orders", "name": "orders-api-568b7b8d9f-vzp22", "tail_lines": 50}
6. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "orders", "name": "inventory-b775d96fd-qq86l", "tail_lines": 50}
7. `kubernetes_describe_pod` {"cluster": "kind", "namespace": "orders", "name": "orders-api-568b7b8d9f-vzp22"}
