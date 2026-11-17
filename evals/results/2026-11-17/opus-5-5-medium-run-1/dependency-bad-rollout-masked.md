## orders-api-568b7b8d9f-vzp22 is crash looping (exit 1, 3 restarts) because its startup stock sync gets HTTP 404 from GET http://inventory.orders:8080/stock. The likely cause is inventory revision 2, rolled out about 48s ago: it added the API_VERSION env var, and inventory now says it serves /v2/stock.

**Root cause** (medium confidence, `bad_rollout`): inventory revision 2 (ReplicaSet inventory-b775d96fd, created about 44-48s ago) added the env var API_VERSION. Revision 1 had no env vars. The new inventory pod logs 'inventory serving /v2/stock'. orders-api, whose only env var is INVENTORY_URL, still calls the old path: 'FATAL: stock sync failed: GET http://inventory.orders:8080/stock: HTTP Error 404: Not Found'. It exits with code 1 and goes into back-off. Two gaps: I didn't confirm that inventory.orders resolves to the inventory service, and I have no proof that revision 1 served /stock. The orders-api container first started about 48s ago, the same time inventory revision 2 went out, so it never ran against revision 1. That makes the cause a strong inference rather than a confirmed fact.
**Severity:** high  ·  **Affected:** orders/Deployment/orders-api, orders/Deployment/inventory

**Evidence**
- `kubernetes_list_pods`: orders-api-568b7b8d9f-vzp22 is 0/1 ready with 3 restarts, last terminated Error exit_code 1. inventory-b775d96fd-qq86l is Running, age 44s.
- `kubernetes_pod_logs`: orders-api: 'FATAL: stock sync failed: GET http://inventory.orders:8080/stock: HTTP Error 404: Not Found'. inventory-b775d96fd-qq86l: 'inventory serving /v2/stock'.
- `kubernetes_recent_changes`: inventory revision 2 rolled out 48s ago and added env API_VERSION. orders-api has been unchanged since it was created (revision 1, 2m ago).
- `kubernetes_describe_deployment`: inventory revision 1 (inventory-8469585669) had no env vars; revision 2 (inventory-b775d96fd) adds API_VERSION and is now the live revision.
- `kubernetes_describe_pod`: orders-api has only the INVENTORY_URL env var and no probes. Events show BackOff restarting the failed container app, 3 times.

**Recommendations**
1. Roll back inventory to revision 1 (kubectl -n orders rollout undo deployment/inventory --to-revision=1), or remove API_VERSION so it serves /stock again, then check that orders-api starts cleanly.
2. Confirm that inventory.orders in INVENTORY_URL is the inventory service.
3. Before moving inventory to /v2 again, update orders-api to call /v2/stock, or have inventory keep serving /stock alongside /v2 during the migration.
4. Make orders-api retry or degrade gracefully when the startup stock sync fails, instead of exiting fatally.
5. Don't just restart orders-api: it will keep crashing until the endpoints match.

Runbook: `pod-crashlooping`

**Tool calls**
1. `runbooks_runbook_for_alert` {"alertname": "KubePodCrashLooping"}
2. `kubernetes_list_pods` {"cluster": "kind", "namespace": "orders"}
3. `kubernetes_recent_changes` {"cluster": "kind"}
4. `incidents_search` {"keywords": "orders-api crashloop"}
5. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "orders", "name": "orders-api-568b7b8d9f-vzp22", "previous": true, "tail_lines": 50}
6. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "orders", "name": "inventory-b775d96fd-qq86l", "tail_lines": 50}
7. `kubernetes_describe_pod` {"cluster": "kind", "namespace": "orders", "name": "orders-api-568b7b8d9f-vzp22"}
8. `kubernetes_describe_deployment` {"cluster": "kind", "namespace": "orders", "name": "inventory"}
