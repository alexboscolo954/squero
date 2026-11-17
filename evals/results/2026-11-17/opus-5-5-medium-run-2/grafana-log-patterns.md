## Some POST /charge requests in payments (namespace shop) fail with 502 because payments can't reach its fraud-check dependency. Each failed charge logs "risk check unavailable: dial tcp fraud-check.risk.svc.cluster.local:8443: connect: connection refused". Payments itself looks healthy apart from that.

**Root cause** (medium confidence, `dependency_failure`): Payments calls the fraud-check service in the risk namespace (fraud-check.risk.svc.cluster.local:8443) before each charge, and that connection is being refused. The failed-charge pattern ('charge order=<_> failed status=502: risk check unavailable: ... connection refused') appears 135 times in the last hour, against 636 successful charges ('status=200'). Successes and failures are mixed together, so fraud-check probably has some endpoints refusing connections (pods restarting or not ready, or a wrong port) rather than being completely down. That last point is inferred. Fraud-check's own logs aren't in Loki (only payments and storefront are), and I had no Kubernetes access to look at its pods, endpoints or rollout history. So I can't tell why it's refusing connections, or whether a recent fraud-check rollout caused it.
**Severity:** high  ·  **Affected:** shop/Deployment/payments, risk/Service/fraud-check

**Evidence**
- `grafana_query_loki_patterns`: Top payments log patterns in the last hour: 'charge order=<_> status=200' x636, and 'charge order=<_> failed status=502: risk check unavailable: dial tcp fraud-check.risk.svc.cluster.local:8443: connect: connection refused' x135. No other error pattern is present. (http://localhost:3000/explore?panes=%7B%22a%22%3A%7B%22datasource%22%3A%22loki%22%2C%22queries%22%3A%5B%7B%22datasource%22%3A%7B%22uid%22%3A%22loki%22%7D%2C%22expr%22%3A%22%7Bservice_name%3D%5C%22payments%5C%22%7D%22%2C%22refId%22%3A%22A%22%7D%5D%2C%22range%22%3A%7B%22from%22%3A%22now-1h%22%7D%7D%7D&schemaVersion=1)
- `grafana_query_loki_logs`: Raw payments lines (service_namespace=shop) show ERROR 502 'risk check unavailable ... connection refused' mixed in with 200 OK charges, e.g. order o-c75499fd, trace fb954a35bf708d397bd3ca19b85ee09a.
- `grafana_list_loki_label_values`: Loki only has logs for service_name payments and storefront, so fraud-check's logs can't be checked here.

**Recommendations**
1. Check the fraud-check workload in the risk namespace: run kubectl -n risk get pods,endpoints fraud-check and look for restarts, CrashLoopBackOff or NotReady pods, and confirm the endpoints serve port 8443.
2. Check fraud-check's rollout history (kubectl -n risk rollout history deploy/fraud-check). If a recent revision lines up with when the errors started, roll it back.
3. Read fraud-check's pod events and its logs from before the last restart (previous=true) to find out why it refuses connections.
4. Open the example trace fb954a35bf708d397bd3ca19b85ee09a in Tempo to confirm where the failing call goes.
5. For now, consider a short retry or circuit breaker on the risk-check call in payments, or move the alert ownership to the risk team.

**Tool calls**
1. `grafana_list_datasources` {}
2. `grafana_query_loki_patterns` {"datasourceUid": "loki", "logql": "{service_name=\"payments\"}"}
3. `grafana_query_loki_stats` {"datasourceUid": "loki", "logql": "{service_name=\"payments\"}"}
4. `grafana_list_loki_label_values` {"datasourceUid": "loki", "labelName": "service_name"}
5. `grafana_query_loki_logs` {"datasourceUid": "loki", "logql": "{service_name=\"payments\"} |= \"status=502\"", "direction": "forward", "limit": 5}
6. `grafana_generate_deeplink` {"resourceType": "explore", "datasourceUid": "loki", "queries": [{"refId": "A", "expr": "{service_name=\"payments\"} |= \"risk check unavailable\""}], "timeRange": {"from": "now-1h", "to": "now"}}
