## orders-api (pod orders-api-568b7b8d9f-vzp22) is crash looping: it exits with code 1 at startup because its stock sync gets HTTP 404 from GET http://inventory.orders:8080/stock. The likely cause is the inventory rollout to revision 2 about 48s ago, which added API_VERSION, after which inventory serves only /v2/stock.

**Root cause** (medium confidence, `bad_rollout`): About 48s ago inventory rolled out revision 2 (ReplicaSet inventory-b775d96fd). The only change from revision 1 is a new env var, API_VERSION. The new inventory pod logs 'inventory serving /v2/stock'. orders-api 2.3.0 still calls the old path /stock on inventory.orders:8080 at startup, gets 'HTTP Error 404: Not Found' and exits fatally (exit code 1, 3 restarts). The API change in inventory breaks the service that depends on it. Two gaps: I assumed inventory.orders is the inventory service and did not check it, and the previous run's logs were gone, so I can't rule out that some earlier restarts happened before the rollout.
**Severity:** high  ·  **Affected:** orders/Deployment/orders-api, orders/Deployment/inventory

**Evidence**
- `kubernetes_list_pods`: orders-api-568b7b8d9f-vzp22 is 0/1 ready with 3 restarts; last terminated reason Error, exit code 1. inventory-b775d96fd-qq86l is healthy, age 44s.
- `kubernetes_pod_logs`: orders-api: 'orders-api starting, version 2.3.0' then 'FATAL: stock sync failed: GET http://inventory.orders:8080/stock: HTTP Error 404: Not Found'. inventory: 'inventory serving /v2/stock'.
- `kubernetes_recent_changes`: inventory revision 2 rolled out 48s ago with env_added API_VERSION. orders-api has had no change since it was created 2m ago.
- `kubernetes_describe_deployment`: inventory revision 2 (inventory-b775d96fd) adds env API_VERSION; revision 1 (inventory-8469585669) had no env vars. The image python:3.12-alpine did not change.

**Recommendations**
1. Roll back inventory to revision 1 (kubectl -n orders rollout undo deployment/inventory --to-revision=1), or remove API_VERSION, so that /stock is served again. This needs approval.
2. Check that inventory.orders:8080 is the inventory service and that GET /stock returns 200 after the rollback.
3. Watch orders-api-568b7b8d9f-vzp22 come out of back-off and become Ready. Do not restart it, because that won't fix the crash.
4. Before moving inventory to the v2 API again, update orders-api to call /v2/stock or make inventory serve both paths, and roll out the consumers first.
5. Optionally, have orders-api retry or degrade gracefully when the stock sync fails, instead of exiting at startup.

Runbook: `pod-crashlooping`

**Tool calls**
1. `runbooks_runbook_for_alert` {"alertname": "KubePodCrashLooping"}
2. `kubernetes_list_pods` {"cluster": "kind", "namespace": "orders"}
3. `kubernetes_recent_changes` {"cluster": "kind"}
4. `incidents_search` {"keywords": "orders-api crash"}
5. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "orders", "name": "orders-api-568b7b8d9f-vzp22", "previous": true, "tail_lines": 50}
6. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "orders", "name": "inventory-b775d96fd-qq86l", "tail_lines": 50}
7. `kubernetes_describe_deployment` {"cluster": "kind", "namespace": "orders", "name": "inventory"}
