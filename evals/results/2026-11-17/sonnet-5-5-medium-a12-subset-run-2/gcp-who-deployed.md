**oncall@example.com** deployed the checkout revision that returns 503s, **checkout-00002-nt5**. The audit log records the `ReplaceService` call (the gcloud deploy) at **2026-10-18 22:22:37.9Z**. The revision was Ready at 22:24:18Z and its container began listening at 22:24:09Z.

**Cause:** the deploy set the env var `UPSTREAM_TIMEOUT_MS=1`. The original revision, checkout-00001-ldp, was created by the same user at 22:17:04Z and ran with a 2000 ms timeout. With a 1 ms timeout, the inventory lookup times out on every request. The handler then returns 503 "inventory unavailable" and logs `inventory lookup failed after 1 ms: URLError: <urlopen error timed out>`. The 503s I saw start at 22:34:21Z, and all of them are on checkout-00002-nt5.

**Caveats:** my log filters were not applied and I only saw the newest entries. So I can't say when the 503s began. I also didn't see the revision's earliest requests or any requests on checkout-00001-ldp.

**Recommendation:** roll traffic back to checkout-00001-ldp, or redeploy with a sane `UPSTREAM_TIMEOUT_MS` such as 2000.

**Evidence**
- `gcp_list_log_entries`: Audit activity log: google.cloud.run.v1.Services.ReplaceService on services/checkout at 2026-10-18T22:22:37.93Z, principalEmail oncall@example.com. Earlier CreateService (22:17:04Z) and SetIamPolicy (22:17:08Z) by the same principal.
- `gcp_list_log_entries`: The system_event ReplaceService response shows revision checkout-00002-nt5 (created 22:22:38Z, Ready 22:24:18Z) with env UPSTREAM_TIMEOUT_MS=1. The startup log reads 'checkout listening, inventory timeout 1 ms' at 22:24:09Z, versus 2000 ms on checkout-00001-ldp.
- `gcp_list_log_entries`: Requests logs for checkout-00002-nt5 show HTTP 503 from 22:34:21Z onward. Stdout shows 'inventory lookup failed after 1 ms: URLError: <urlopen error timed out>'.

**Tool calls**
1. `gcp_list_log_entries` {"resourceNames": ["projects/shop-prod-4f2a"], "filter": "logName=\"projects/shop-prod-4f2a/logs/cloudaudit.googleapis.com%2Factivity\" AND resource.type=\"cloud_run_revision\" OR protoPayload.serviceName=\"run.googleapis.com\"", "orderBy": "timestamp desc", "pageSize": 20}
2. `gcp_list_log_entries` {"resourceNames": ["projects/shop-prod-4f2a"], "filter": "resource.type=\"cloud_run_revision\" AND httpRequest.status=503", "orderBy": "timestamp desc", "pageSize": 5}
3. `gcp_list_log_entries` {"resourceNames": ["projects/shop-prod-4f2a"], "filter": "logName=\"projects/shop-prod-4f2a/logs/cloudaudit.googleapis.com%2Factivity\" AND protoPayload.serviceName=\"run.googleapis.com\"", "orderBy": "timestamp desc", "pageSize": 10}
4. `read_more` {"result_id": "r770fbd", "grep": "methodName", "fields": "timestamp,protoPayload.methodName,protoPayload.resourceName,protoPayload.authenticationInfo.principalEmail,logName"}
5. `gcp_list_log_entries` {"resourceNames": ["projects/shop-prod-4f2a"], "filter": "timestamp>=\"2026-10-18T22:00:00Z\" AND resource.type=\"cloud_run_revision\" AND httpRequest.status=503 AND resource.labels.revision_name=\"checkout-00001-ldp\"", "pageSize": 1}
6. `gcp_list_log_entries` {"resourceNames": ["projects/shop-prod-4f2a"], "filter": "timestamp>=\"2026-10-18T22:22:00Z\" AND timestamp<=\"2026-10-18T22:23:00Z\"", "pageSize": 1}
7. `read_more` {"result_id": "rdb9935", "grep": "ReplaceService", "fields": "timestamp,protoPayload.methodName,protoPayload.authenticationInfo.principalEmail,protoPayload.request.spec.template.spec.containers,protoPayload.request.spec.template.metadata"}
8. `read_more` {"result_id": "rdb9935", "grep": "principalEmail", "fields": "timestamp,protoPayload.methodName,protoPayload.response.status.latestCreatedRevisionName,protoPayload.request"}
9. `read_more` {"result_id": "rdb9935", "grep": "checkout-00002-nt5", "fields": "timestamp,protoPayload.methodName,protoPayload.authenticationInfo.principalEmail,protoPayload.response"}
10. `read_more` {"result_id": "rdb9935", "grep": "principalEmail", "fields": "timestamp,protoPayload.methodName,protoPayload.authenticationInfo.principalEmail"}
11. `read_more` {"result_id": "rdb9935", "grep": "\"status\": 503", "fields": "timestamp", "start": 0}
12. `read_more` {"result_id": "rdb9935", "grep": "checkout listening", "fields": "timestamp,jsonPayload.message,resource.labels.revision_name"}
13. `read_more` {"result_id": "rdb9935", "grep": "checkout-00001-ldp", "fields": "timestamp,httpRequest.status"}
14. `read_more` {"result_id": "rdb9935", "grep": "checkout-00001-ldp", "fields": "timestamp,httpRequest.status,resource.labels.revision_name,protoPayload.methodName", "start": 0}
15. `read_more` {"result_id": "rdb9935", "grep": "status", "fields": "timestamp,httpRequest.status", "start": 0}
16. `read_more` {"result_id": "rdb9935", "grep": "xxxx_none"}
