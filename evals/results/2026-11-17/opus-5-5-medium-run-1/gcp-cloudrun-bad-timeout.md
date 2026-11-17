## Since revision checkout-00002-nt5 rolled out at about 22:24 UTC, checkout returns 503 on its requests. The new revision starts with an inventory timeout of 1 ms instead of 2000 ms, so every inventory lookup times out. oncall@example.com deployed it with a ReplaceService call at 22:22:37 UTC.

**Root cause** (high confidence, `bad_rollout`): The new revision checkout-00002-nt5 (deployed by oncall@example.com through gcloud ReplaceService at 22:22:37Z, serving at about 22:24Z) lowered the inventory upstream timeout from 2000 ms to 1 ms. Revision checkout-00001-ldp logged 'checkout listening, inventory timeout 2000 ms'. The new revision logs 'checkout listening, inventory timeout 1 ms'. Since then every request logs 'inventory lookup failed after 1 ms: URLError: <urlopen error timed out>' and returns 503 in about 25 ms. A network call to another service can't finish in 1 ms, so every inventory call fails. The logs don't show how long inventory normally takes, but the previous revision ran with a 2000 ms budget. The audit fields I read don't show which setting changed (the env var or flag name). The startup logs only show the effective timeout value.
**Severity:** critical  ·  **Affected:** shop-prod-4f2a/CloudRunService/checkout

**Evidence**
- `gcp_list_log_entries`: Audit log: google.cloud.run.v1.Services.ReplaceService on namespaces/shop-prod-4f2a/services/checkout by oncall@example.com at 2026-10-18T22:22:37Z created revision checkout-00002-nt5. Revision checkout-00001-ldp was created at 22:17:04Z.
- `gcp_list_log_entries`: Startup logs: checkout-00001-ldp at 22:17:15Z says 'checkout listening, inventory timeout 2000 ms'. checkout-00002-nt5 at 22:24:09Z says 'checkout listening, inventory timeout 1 ms'.
- `gcp_list_log_entries`: On checkout-00002-nt5 (22:34Z), ERROR logs repeat 'inventory lookup failed after 1 ms: URLError: <urlopen error timed out>' (upstream=inventory, timeout_ms=1). Each one goes with a GET / that returns 503 after about 0.024–0.028 s.

**Recommendations**
1. Roll checkout traffic back to the last good revision: gcloud run services update-traffic checkout --region us-central1 --to-revisions checkout-00001-ldp=100. This needs approval.
2. Fix the inventory timeout setting in the service config, setting it back to 2000 ms or another value that fits inventory's real latency, and redeploy.
3. Ask oncall@example.com why the timeout was changed. It may have been a units mistake (seconds vs ms).
4. Add validation or a minimum for the timeout config, and use a canary or tagged-revision rollout so a change like this is caught before it takes 100% of traffic.
5. Check that the 5xx rate goes back to baseline after the rollback, using run.googleapis.com/request_count grouped by response_code_class.

**Tool calls**
1. `gcp_list_log_entries` {"resourceNames": ["projects/shop-prod-4f2a"], "filter": "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"checkout\" AND severity>=ERROR", "orderBy": "timestamp desc", "pageSize": 10}
2. `gcp_list_log_entries` {"resourceNames": ["projects/shop-prod-4f2a"], "filter": "logName=\"projects/shop-prod-4f2a/logs/cloudaudit.googleapis.com%2Factivity\"", "orderBy": "timestamp desc", "pageSize": 10}
3. `read_more` {"result_id": "rcf6a85", "grep": "methodName", "fields": "timestamp,protoPayload.methodName,protoPayload.resourceName,protoPayload.authenticationInfo,protoPayload.request"}
4. `read_more` {"result_id": "rcf6a85", "grep": "timeout_ms", "fields": "timestamp,resource.labels.revision_name,jsonPayload"}
5. `gcp_list_log_entries` {"resourceNames": ["projects/shop-prod-4f2a"], "filter": "resource.type=\"cloud_run_revision\" AND resource.labels.revision_name=\"checkout-00001-ldp\" AND timestamp>=\"2026-10-18T22:17:00Z\"", "orderBy": "timestamp desc", "pageSize": 8}
6. `read_more` {"result_id": "r485177", "grep": "checkout-00001-ldp", "fields": "timestamp,severity,jsonPayload,httpRequest.status,httpRequest.latency"}
