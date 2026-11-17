**Passed:** The check passes. Deployment orders/inventory has 1/1 replicas ready. Its only pod, inventory-b775d96fd-qq86l, is Running and Ready with 0 restarts. That pod is only 44s old, so the check covers a short period. Separately, orders-api in the same namespace is unhealthy: 0/1 ready and 3 restarts. The check doesn't cover it.

**Evidence**
- `kubernetes_list_deployments`: inventory: ready 1/1, updated 1, no failing conditions
- `kubernetes_list_pods`: inventory-b775d96fd-qq86l: phase Running, ready 1/1, restarts 0, no last termination, age 44s

**Tool calls**
1. `kubernetes_list_deployments` {"cluster": "kind", "namespace": "orders"}
2. `kubernetes_list_pods` {"cluster": "kind", "namespace": "orders"}
