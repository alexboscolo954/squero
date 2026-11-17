**oncall@example.com** deployed it at **2026-10-18 22:22:37.9 UTC**. The audit log shows a `google.cloud.run.v1.Services.ReplaceService` call on `namespaces/shop-prod-4f2a/services/checkout`. It created revision **checkout-00002-nt5**, and system events for that revision follow at 22:22:38 and 22:24:16–22:24:18.

The earlier revision, checkout-00001-ldp, was created by the same account at 22:17:04 (`CreateService`).

Every 503 I saw came from checkout-00002-nt5, the newest at 22:34:30. The 503s I read are about 10 minutes after the deploy. My log queries returned unfiltered, newest-first results, so I did not find the first 503 and cannot say exactly when they began.

The revision's stdout logs show `inventory lookup failed after 1 ms: URLError: <urlopen error timed out>` with `timeout_ms: 1`. That suggests the new revision set an inventory timeout of 1 ms. I did not confirm this in the revision's config.

If that is the cause, roll back to checkout-00001-ldp or raise the timeout.

**Evidence**
- `gcp_list_log_entries`: Activity audit log: 2026-10-18T22:22:37.931973Z google.cloud.run.v1.Services.ReplaceService on services/checkout, principalEmail oncall@example.com. System events at 22:22:38 name revision checkout-00002-nt5. Also CreateService by oncall@example.com at 22:17:04 (checkout-00001-ldp).
- `gcp_list_log_entries`: HTTP 503 request logs, latest 22:34:30.98Z, all from revision checkout-00002-nt5. Stdout shows 'inventory lookup failed after 1 ms: URLError: timed out', timeout_ms 1, upstream inventory.

**Tool calls**
1. `gcp_list_log_entries` {"resourceNames": ["projects/shop-prod-4f2a"], "filter": "logName=\"projects/shop-prod-4f2a/logs/cloudaudit.googleapis.com%2Factivity\" AND resource.type=\"cloud_run_revision\" OR (logName=\"projects/shop-prod-4f2a/logs/cloudaudit.googleapis.com%2Factivity\" AND protoPayload.resourceName:\"checkout\
2. `gcp_list_log_entries` {"resourceNames": ["projects/shop-prod-4f2a"], "filter": "resource.type=\"cloud_run_revision\" AND httpRequest.status=503", "orderBy": "timestamp desc", "pageSize": 5}
3. `gcp_list_log_entries` {"resourceNames": ["projects/shop-prod-4f2a"], "filter": "logName=\"projects/shop-prod-4f2a/logs/cloudaudit.googleapis.com%2Factivity\" AND protoPayload.methodName:\"Services\"", "orderBy": "timestamp desc", "pageSize": 5}
4. `read_more` {"result_id": "r78183f", "grep": "methodName", "fields": "timestamp,protoPayload.methodName,protoPayload.resourceName,protoPayload.authenticationInfo.principalEmail,logName"}
5. `read_more` {"result_id": "r78183f", "grep": "ReplaceService", "fields": "timestamp,protoPayload.request.spec.template.spec.containers,protoPayload.request.spec.template.metadata"}
6. `gcp_list_log_entries` {"resourceNames": ["projects/shop-prod-4f2a"], "filter": "resource.type=\"cloud_run_revision\" AND httpRequest.status=503 AND resource.labels.revision_name=\"checkout-00002-nt5\"", "orderBy": "timestamp asc", "pageSize": 1}
7. `gcp_list_log_entries` {"resourceNames": ["projects/shop-prod-4f2a"], "filter": "resource.type=\"cloud_run_revision\" AND httpRequest.status=503 AND timestamp<=\"2026-10-18T22:30:00Z\"", "orderBy": "timestamp asc", "pageSize": 1}
8. `read_more` {"result_id": "r57b139", "grep": "\"status\": 503", "fields": "timestamp,resource.labels.revision_name"}
9. `gcp_list_log_entries` {"resourceNames": ["projects/shop-prod-4f2a"], "filter": "logName=\"projects/shop-prod-4f2a/logs/run.googleapis.com%2Frequests\" AND timestamp>=\"2026-10-18T22:17:00Z\" AND timestamp<=\"2026-10-18T22:25:00Z\"", "orderBy": "timestamp asc", "pageSize": 3}
10. `read_more` {"result_id": "rc0ce86", "grep": "\"status\"", "fields": "timestamp,httpRequest.status,resource.labels.revision_name"}
11. `gcp_list_log_entries` {"resourceNames": ["projects/shop-prod-4f2a"], "filter": "timestamp>=\"2026-10-18T22:22:00Z\" AND timestamp<=\"2026-10-18T22:23:00Z\" AND logName=\"projects/shop-prod-4f2a/logs/cloudaudit.googleapis.com%2Factivity\"", "pageSize": 3}
12. `read_more` {"result_id": "r161509", "grep": "ReplaceService", "fields": "timestamp,protoPayload.methodName,protoPayload.authenticationInfo.principalEmail,logName"}
