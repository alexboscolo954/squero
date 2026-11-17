## Payments (shop namespace) POST /charge errors are caused by its downstream fraud-check service in the risk namespace. Charges fail with 502 'risk check unavailable' because connections to fraud-check.risk.svc.cluster.local:8443 are refused. I don't know yet why fraud-check is down: it doesn't ship logs to Loki, and I had no Kubernetes or metrics tools.

**Root cause** (medium confidence, `dependency_failure`): Over the last hour, payments failed 135 charges with 'failed status=502: risk check unavailable: dial tcp fraud-check.risk.svc.cluster.local:8443: connect: connection refused', against 636 successful 'status=200' charges. A connection refused error means nothing is accepting connections on port 8443 behind that address. Likely reasons are that fraud-check has no ready pods, its pods are crash-looping, or the Service points at the wrong port. Payments itself is working and serves other charges normally. Many fraud-check calls fail; some succeed. That fits partial unavailability, such as some fraud-check endpoints being down or pods restarting. I couldn't tell whether a rollout of fraud-check triggered this, because its logs aren't in Loki (only payments and storefront are) and I had no cluster access.
**Severity:** high  ·  **Affected:** shop/Deployment/payments, risk/Service/fraud-check

**Evidence**
- `grafana_query_loki_patterns`: For {service_name="payments"}, the top error pattern was 'charge order=<_> failed status=502: risk check unavailable: dial tcp fraud-check.risk.svc.cluster.local:8443: connect: connection refused' (135 times), against 636 'charge ... status=200'. (http://localhost:3000/explore?panes=%7B%22a%22%3A%7B%22datasource%22%3A%22loki%22%2C%22queries%22%3A%5B%7B%22datasource%22%3A%7B%22uid%22%3A%22loki%22%7D%2C%22expr%22%3A%22%7Bservice_name%3D%5C%22payments%5C%22%7D%22%2C%22refId%22%3A%22A%22%7D%5D%2C%22range%22%3A%7B%22from%22%3A%22now-1h%22%7D%7D%7D&schemaVersion=1)
- `grafana_query_loki_logs`: Raw payments ERROR lines (service_namespace=shop) show the same connection refused error to fraud-check.risk.svc.cluster.local:8443 mixed in with successful charges, for example order o-c75499fd (trace fb954a35bf708d397bd3ca19b85ee09a).
- `grafana_list_loki_label_values`: Loki has only 'payments' and 'storefront' for service_name, so there are no fraud-check logs to explain why it refuses connections.

**Recommendations**
1. Check fraud-check in the risk namespace: run kubectl -n risk get pods,deploy,endpoints fraud-check. Look for missing or not-ready endpoints, CrashLoopBackOff, or OOMKilled, and check that the Service targets port 8443.
2. Run kubectl -n risk rollout history deploy/fraud-check to see whether a recent revision lines up with when the errors started. If it does, roll back with kubectl -n risk rollout undo deploy/fraud-check.
3. Read the fraud-check pod logs, including previous=true for restarted containers, and its events to find why it's down.
4. If fraud-check can't be restored quickly, consider a fallback in payments for risk-check failures (degrade or queue for review instead of returning a 502), so customers aren't affected.
5. Get fraud-check sending logs to Loki so the next incident can be investigated end to end.

**Tool calls**
1. `grafana_list_datasources` {}
2. `grafana_query_loki_patterns` {"datasourceUid": "loki", "logql": "{service_name=\"payments\"}"}
3. `grafana_query_loki_stats` {"datasourceUid": "loki", "logql": "{service_name=\"payments\"}"}
4. `grafana_list_loki_label_values` {"datasourceUid": "loki", "labelName": "service_name"}
5. `grafana_query_loki_logs` {"datasourceUid": "loki", "logql": "{service_name=\"payments\"} |= \"status=502\"", "limit": 5}
6. `grafana_generate_deeplink` {"resourceType": "explore", "datasourceUid": "loki", "queries": [{"refId": "A", "expr": "{service_name=\"payments\"} |= \"risk check unavailable\""}], "timeRange": {"from": "now-1h", "to": "now"}}
