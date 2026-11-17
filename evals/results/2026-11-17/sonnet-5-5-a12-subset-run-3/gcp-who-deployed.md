**oncall@example.com** deployed it, using gcloud (client 589.0.0). The revision is `checkout-00002-nt5` in service `checkout`, us-central1.

- **When:** the revision was created at **2026-10-18 22:22:38 UTC**. It became Ready at 22:24:18 UTC ("Deploying revision succeeded in 1m40.46s"). That is about 10 minutes before the 503s I saw at 22:34 UTC.
- **Who:** `serving.knative.dev/creator` on the revision, and `creator` and `lastModifier` on the service (generation 2), are all oncall@example.com.
- **Why it returns 503:** the deploy set `UPSTREAM_TIMEOUT_MS=1`. The app's default is 2000 ms. Every request to the inventory upstream times out after 1 ms ("inventory lookup failed after 1 ms: URLError: <urlopen error timed out>"), and the app answers 503 "inventory unavailable". All 503s I saw in the logs came from `checkout-00002-nt5`.
- **Fix:** redeploy with a sane `UPSTREAM_TIMEOUT_MS` (for example 2000), or roll traffic back to the previous revision.

**Caveat:** the log filters I used returned mostly unfiltered entries. I did not find an Admin Activity audit entry with `authenticationInfo.principalEmail`. The deployer identity comes from the creator and lastModifier annotations in the system_event audit records. I would treat it as reliable, but it is not confirmed against an Admin Activity entry.

**Evidence**
- `gcp_list_log_entries`: system_event audit record /Services.ReplaceService for revision checkout-00002-nt5: serving.knative.dev/creator=oncall@example.com, client-name gcloud 589.0.0, creationTimestamp 2026-10-18T22:22:38Z, Ready at 22:24:18Z; env UPSTREAM_TIMEOUT_MS=1. The record was read via read_more on the saved result of an audit-log query for resourceName containing 'checkout'.
- `gcp_list_log_entries`: Service checkout audit record (generation 2): creator and lastModifier = oncall@example.com, same operation-id 9bcbd8df-0fc8-416f-a0cf-6e15aa5e74bd. The record was read via read_more on the same saved result.
- `gcp_list_log_entries`: Query for httpRequest.status=503 on cloud_run_revision: 503 responses on revision checkout-00002-nt5 at 22:34:23–22:34:31Z, each paired with the stdout error 'inventory lookup failed after 1 ms: URLError: <urlopen error timed out>' (timeout_ms 1).

**Tool calls**
1. `gcp_list_log_entries` {"resourceNames": ["projects/shop-prod-4f2a"], "filter": "logName=\"projects/shop-prod-4f2a/logs/cloudaudit.googleapis.com%2Factivity\" AND resource.type=\"cloud_run_revision\" OR (logName=\"projects/shop-prod-4f2a/logs/cloudaudit.googleapis.com%2Factivity\" AND protoPayload.resourceName:\"checkout\
2. `gcp_list_log_entries` {"resourceNames": ["projects/shop-prod-4f2a"], "filter": "resource.type=\"cloud_run_revision\" AND httpRequest.status=503", "orderBy": "timestamp desc", "pageSize": 10}
3. `gcp_list_log_entries` {"resourceNames": ["projects/shop-prod-4f2a"], "filter": "logName=\"projects/shop-prod-4f2a/logs/cloudaudit.googleapis.com%2Factivity\" AND protoPayload.resourceName:\"checkout\"", "orderBy": "timestamp desc", "pageSize": 10}
4. `read_more` {"result_id": "r7df791", "grep": "methodName"}
5. `gcp_list_log_entries` {"resourceNames": ["projects/shop-prod-4f2a"], "filter": "protoPayload.serviceName=\"run.googleapis.com\" AND protoPayload.methodName:\"Services\"", "orderBy": "timestamp desc", "pageSize": 10}
6. `read_more` {"result_id": "r7df791", "grep": "principalEmail", "fields": "[\"logName\",\"timestamp\",\"protoPayload.methodName\",\"protoPayload.resourceName\",\"protoPayload.authenticationInfo\"]"}
7. `gcp_list_log_entries` {"resourceNames": ["projects/shop-prod-4f2a"], "filter": "protoPayload.authenticationInfo.principalEmail:\"@\" AND timestamp>=\"2026-10-18T22:00:00Z\" AND timestamp<=\"2026-10-18T22:30:00Z\"", "orderBy": "timestamp asc", "pageSize": 10}
8. `read_more` {"result_id": "r7df791", "grep": "lastModifier", "fields": "[\"timestamp\",\"protoPayload.resourceName\",\"protoPayload.response.metadata.annotations\"]"}
