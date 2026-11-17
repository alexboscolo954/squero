## Payments POST /charge returns 502 because the call to its fraud-check dependency fails. The logs show connection refused to fraud-check.risk.svc.cluster.local:8443. That accounts for 135 failed charges against 636 successful ones in the last hour, which fits the 8% 5xx alert.

**Root cause** (medium confidence, `dependency_failure`): The payments service (namespace shop) can't connect to the fraud-check service in the risk namespace on port 8443. Every failing charge logs 'failed status=502: risk check unavailable: dial tcp fraud-check.risk.svc.cluster.local:8443: connect: connection refused'. A connection refused (rather than a timeout) means the address resolves but nothing is listening. Likely causes are fraud-check pods being down or not ready, endpoints missing for some replicas, or a port change. The failures are partial (135 of 771 charge lines), so some fraud-check replicas or endpoints may be unhealthy rather than the whole service. I had no Kubernetes or fraud-check log access, so I couldn't check fraud-check's own state or recent rollouts. If a fraud-check rollout caused this, the category would be bad_rollout instead.
**Severity:** high  ·  **Affected:** shop/Deployment/payments, risk/Service/fraud-check

**Evidence**
- `grafana_query_loki_patterns`: In the last hour of {service_name="payments"}: 'charge order=<_> failed status=502: risk check unavailable: dial tcp fraud-check.risk.svc.cluster.local:8443: connect: connection refused' x135, against 'charge order=<_> status=200' x636. (http://localhost:3000/explore?panes=%7B%22a%22%3A%7B%22datasource%22%3A%22loki%22%2C%22queries%22%3A%5B%7B%22datasource%22%3A%7B%22uid%22%3A%22loki%22%7D%2C%22expr%22%3A%22%7Bservice_name%3D%5C%22payments%5C%22%7D%22%2C%22refId%22%3A%22A%22%7D%5D%2C%22range%22%3A%7B%22from%22%3A%22now-1h%22%7D%7D%7D&schemaVersion=1)
- `grafana_query_loki_logs`: Raw payments logs (service_namespace=shop) show ERROR lines with trace IDs mixed in with successful charges, e.g. order o-c75499fd, trace fb954a35bf708d397bd3ca19b85ee09a: 'risk check unavailable ... connection refused'.
- `grafana_list_loki_label_values`: Loki only has logs for payments and storefront, so fraud-check's own logs weren't available to check.

**Recommendations**
1. Check fraud-check in the risk namespace: run kubectl -n risk get pods,endpoints fraud-check and look for pods that are not ready, crashlooping or recently restarted, or endpoints missing.
2. Run kubectl -n risk rollout history deploy/fraud-check and look at recent events. If a recent revision lines up with when the errors started, roll it back.
3. Confirm the Service port and targetPort for 8443 match the port the fraud-check container actually listens on.
4. Open trace fb954a35bf708d397bd3ca19b85ee09a in Tempo to see when the failing dependency calls happen and how long they take.
5. For the longer term, add a fraud-check availability alert, and consider retries or a circuit breaker in payments for transient risk-check failures, if the business policy allows it.

**Tool calls**
1. `grafana_list_datasources` {}
2. `grafana_query_loki_patterns` {"datasourceUid": "loki", "logql": "{service_name=\"payments\"}"}
3. `grafana_query_loki_stats` {"datasourceUid": "loki", "logql": "{service_name=\"payments\"}"}
4. `grafana_list_loki_label_values` {"datasourceUid": "loki", "labelName": "service_name"}
5. `grafana_query_loki_logs` {"datasourceUid": "loki", "logql": "{service_name=\"payments\"} |= \"status=502\"", "limit": 5}
6. `grafana_generate_deeplink` {"resourceType": "explore", "datasourceUid": "loki", "queries": [{"refId": "A", "expr": "{service_name=\"payments\"} |= \"risk check unavailable\""}], "timeRange": {"from": "now-1h", "to": "now"}}
