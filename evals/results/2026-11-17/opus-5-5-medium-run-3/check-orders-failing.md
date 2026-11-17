## orders-api (0/1 ready) is crash looping. At startup it calls http://inventory.orders:8080/stock and gets a 404. inventory was rolled out to revision 2 48s ago and now only serves /v2/stock.

**Root cause** (high confidence, `bad_rollout`): inventory revision 2, rolled out 48s ago, added the env var API_VERSION. Inventory now logs 'inventory serving /v2/stock'. orders-api 2.3.0 still requests GET http://inventory.orders:8080/stock at startup, gets 'HTTP Error 404: Not Found', logs FATAL and exits with code 1, which causes the crash loop. orders-api itself has not changed since it was created as revision 1. The likely trigger is the new API_VERSION value in inventory's rollout. Env var values can't be read with these tools, so that last link is inferred.
**Severity:** high  ·  **Affected:** orders/Deployment/orders-api, orders/Deployment/inventory

**Evidence**
- `kubernetes_pod_logs`: orders-api-568b7b8d9f-vzp22 logs: 'FATAL: stock sync failed: GET http://inventory.orders:8080/stock: HTTP Error 404: Not Found'
- `kubernetes_pod_logs`: inventory-b775d96fd-qq86l logs: 'inventory serving /v2/stock'
- `kubernetes_recent_changes`: inventory revision 2 rolled out 48s ago and added the env var API_VERSION to container app; orders-api has only revision 1 (created 2m ago)
- `kubernetes_list_pods`: orders-api pod is 0/1 ready with 3 restarts and exit code 1 (Error)
- `kubernetes_list_deployments`: orders-api ready 0/1 with MinimumReplicasUnavailable

**Recommendations**
1. Roll back inventory to revision 1 (kubectl rollout undo deployment/inventory -n orders), or remove or revert its API_VERSION env var, so it serves /stock again.
2. Confirm the orders-api pod becomes ready after the next restart, and confirm both deployments are back to 1/1.
3. Long term, have inventory keep serving the old /stock path during an API version change, or update orders-api to call /v2/stock before inventory moves. Also make orders-api retry this startup call or handle the failure without exiting.
4. Don't restart orders-api: per the pod-crashlooping runbook, a restart won't fix a crash loop.

Runbook: `pod-crashlooping`

**Tool calls**
1. `kubernetes_list_deployments` {"cluster": "kind", "namespace": "orders"}
2. `kubernetes_list_pods` {"cluster": "kind", "namespace": "orders"}
3. `kubernetes_recent_changes` {"cluster": "kind"}
4. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "orders", "name": "orders-api-568b7b8d9f-vzp22", "tail_lines": 50}
5. `incidents_search` {"keywords": "orders-api inventory"}
6. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "orders", "name": "inventory-b775d96fd-qq86l", "tail_lines": 30}
7. `runbooks_runbook_for_alert` {"alertname": "KubePodCrashLooping"}
