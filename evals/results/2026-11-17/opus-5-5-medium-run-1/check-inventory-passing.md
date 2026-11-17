**Passed:** The Check holds. Deployment orders/inventory has 1/1 replicas ready, and its only pod, inventory-b775d96fd-qq86l, is Running and 1/1 ready with 0 restarts. Separately, orders-api in the same namespace is failing (0/1 ready, 3 restarts, exit code 1), but this Check does not cover it.

**Evidence**
- `kubernetes_list_deployments`: Deployment inventory in orders: ready 1/1, updated 1, no problem conditions.
- `kubernetes_list_pods`: Pod inventory-b775d96fd-qq86l: phase Running, ready 1/1, restarts 0, running for 44s. It is a fresh pod, so a restart-free record covers only that time.
- `kubernetes_list_pods`: Outside this Check: pod orders-api-568b7b8d9f-vzp22 is 0/1 ready with 3 restarts and the app container exits with code 1.

**Tool calls**
1. `kubernetes_list_deployments` {"cluster": "kind", "namespace": "orders"}
2. `kubernetes_list_pods` {"cluster": "kind", "namespace": "orders"}
