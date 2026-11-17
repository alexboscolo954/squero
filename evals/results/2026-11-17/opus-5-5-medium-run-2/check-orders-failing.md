## orders-api (0/1 ready) crash loops on startup. It calls inventory at /stock and gets a 404. Inventory was rolled out 48s ago (revision 2, added API_VERSION) and now serves only /v2/stock.

**Root cause** (high confidence, `bad_rollout`): inventory rolled out revision 2 about 48s ago, adding the env var API_VERSION. Its log now reads 'inventory serving /v2/stock'. orders-api 2.3.0 still calls GET http://inventory.orders:8080/stock, gets HTTP 404 and exits with code 1. That has happened 3 times so far. orders-api itself was not changed: its revision 1 was created 2m ago.
**Severity:** high  ·  **Affected:** orders/Deployment/orders-api, orders/Deployment/inventory

**Evidence**
- `kubernetes_list_deployments`: orders-api ready 0/1, MinimumReplicasUnavailable
- `kubernetes_list_pods`: orders-api-568b7b8d9f-vzp22 restarts 3, terminated Error exit_code 1
- `kubernetes_pod_logs`: orders-api pod: 'FATAL: stock sync failed: GET http://inventory.orders:8080/stock: HTTP Error 404: Not Found'
- `kubernetes_pod_logs`: inventory-b775d96fd-qq86l: 'inventory serving /v2/stock'
- `kubernetes_recent_changes`: inventory revision 2 rollout 48s ago, env_added app: API_VERSION; orders-api only created (rev 1) 2m ago

**Recommendations**
1. Roll inventory back to revision 1 (kubectl rollout undo deployment/inventory -n orders), or remove the API_VERSION env var, so /stock is served again. This needs human approval.
2. Once inventory is back on /stock, check that orders-api becomes 1/1 ready. If it stays in CrashLoopBackOff, delete the pod so it restarts.
3. Before inventory moves to /v2 again, change orders-api to call /v2/stock, or have inventory serve both /stock and /v2/stock.

Runbook: `deployment-unavailable`

**Tool calls**
1. `kubernetes_list_deployments` {"cluster": "kind", "namespace": "orders"}
2. `kubernetes_list_pods` {"cluster": "kind", "namespace": "orders"}
3. `kubernetes_recent_changes` {"cluster": "kind"}
4. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "orders", "name": "orders-api-568b7b8d9f-vzp22", "tail_lines": 50}
5. `incidents_search` {"keywords": "orders-api inventory"}
6. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "orders", "name": "inventory-b775d96fd-qq86l", "tail_lines": 50}
7. `runbooks_runbook_for_alert` {"alertname": "KubeDeploymentReplicasMismatch"}
