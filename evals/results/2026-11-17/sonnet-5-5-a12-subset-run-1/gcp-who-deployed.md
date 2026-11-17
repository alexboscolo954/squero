**oncall@example.com** deployed it at **2026-10-18 22:22:37 UTC**. The deploy was a `google.cloud.run.v1.Services.ReplaceService` call from gcloud 589.0.0. It created revision **checkout-00002-nt5**, which became Ready at 22:24:18 UTC ("Deploying revision succeeded in 1m40.46s").

**Why it returns 503s:** the new revision sets the env var `UPSTREAM_TIMEOUT_MS=1`. The container's startup log says "checkout listening, inventory timeout 1 ms". The code calls the upstream with a timeout of that many milliseconds. Every request therefore fails with `URLError: <urlopen error timed out>` and the service returns 503 "inventory unavailable". The code's default is 2000 ms.

**Timing and impact:**
- The previous revision, checkout-00001-ldp, served 200s up to about 22:24.
- Request metrics show 503s only on checkout-00002-nt5: 113 in the 22:24:27–22:29:27 window and 100 in 22:29:27–22:34:27.
- Revision 00001 had no more traffic after 22:24:27, so 100% of traffic is on the failing revision.
- The latest 503s I saw were at 22:34:30 UTC.

**Caveats:**
- The audit log entry for the deploy does not show the env var. I took `UPSTREAM_TIMEOUT_MS=1` from the revision spec in the Cloud Run system_event audit log.
- The 22:22:37 deploy is the only Replace/update call from a user in the audit log, so I attribute the revision to it. The revision's `serving.knative.dev/creator` annotation also says oncall@example.com.
- My log filters on the 503s were not honoured by the tool, so I could not list the very first 503. The metrics put the first ones in the window after 22:24:27.

**Recommendation:** roll back to checkout-00001-ldp, or redeploy with `UPSTREAM_TIMEOUT_MS` restored to a sane value such as 2000.

**Evidence**
- `gcp_list_log_entries`: Audit log (activity), 2026-10-18T22:22:37.931973Z: google.cloud.run.v1.Services.ReplaceService on namespaces/shop-prod-4f2a/services/checkout, principalEmail oncall@example.com. Earlier entries by the same user are CreateService at 22:17:04 and SetIamPolicy at 22:17:08.
- `gcp_list_log_entries`: Cloud Run system_event audit log: revision checkout-00002-nt5 created 22:22:38 by oncall@example.com (serving.knative.dev/creator), Ready at 22:24:18 ("Deploying revision succeeded in 1m40.46s"). Its spec has env UPSTREAM_TIMEOUT_MS=1. Startup log: "checkout listening, inventory timeout 1 ms".
- `gcp_list_log_entries`: Request and stdout logs: 503 responses from checkout-00002-nt5 (22:34:21–22:34:30) paired with ERROR "inventory lookup failed after 1 ms: URLError: <urlopen error timed out>".
- `gcp_list_timeseries`: request_count for service checkout: revision checkout-00001-ldp had 200s only (37 in the window ending 22:19:27, 83 in the window ending 22:24:27, then 0). checkout-00002-nt5 had 503s only: 113 in the window ending 22:29:27 and 100 in the window ending 22:34:27.

**Tool calls**
1. `gcp_list_log_entries` {"resourceNames": ["projects/shop-prod-4f2a"], "filter": "logName=\"projects/shop-prod-4f2a/logs/cloudaudit.googleapis.com%2Factivity\" AND resource.type=\"cloud_run_revision\" OR (logName=\"projects/shop-prod-4f2a/logs/cloudaudit.googleapis.com%2Factivity\" AND protoPayload.resourceName:\"checkout\
2. `gcp_list_log_entries` {"resourceNames": ["projects/shop-prod-4f2a"], "filter": "resource.type=\"cloud_run_revision\" AND httpRequest.status=503", "orderBy": "timestamp desc", "pageSize": 10}
3. `gcp_list_log_entries` {"resourceNames": ["projects/shop-prod-4f2a"], "filter": "logName=\"projects/shop-prod-4f2a/logs/cloudaudit.googleapis.com%2Factivity\" AND protoPayload.resourceName:\"checkout\"", "orderBy": "timestamp desc", "pageSize": 10}
4. `read_more` {"result_id": "rc1a371", "grep": "methodName"}
5. `read_more` {"result_id": "r8885b7", "grep": "principalEmail"}
6. `read_more` {"result_id": "rc1a371", "grep": "principalEmail", "fields": "timestamp,protoPayload.methodName,protoPayload.authenticationInfo.principalEmail"}
7. `read_more` {"result_id": "r645359", "grep": "checkout-00001", "fields": "timestamp,httpRequest.status,resource.labels.revision_name"}
8. `read_more` {"result_id": "r645359", "grep": "\"status\": 503", "fields": "timestamp,resource.labels.revision_name"}
9. `read_more` {"result_id": "r645359", "grep": "\"status\": 503", "fields": "timestamp,resource.labels.revision_name", "start": 499}
10. `gcp_list_log_entries` {"resourceNames": ["projects/shop-prod-4f2a"], "filter": "resource.type=\"cloud_run_revision\" AND httpRequest.status=503 AND resource.labels.revision_name=\"checkout-00002-nt5\" AND timestamp<=\"2026-10-18T22:34:00Z\"", "orderBy": "timestamp asc", "pageSize": 1}
11. `read_more` {"result_id": "rb144dc", "grep": "\"status\": 503", "fields": "timestamp"}
12. `gcp_list_log_entries` {"resourceNames": ["projects/shop-prod-4f2a"], "filter": "resource.type=\"cloud_run_revision\" AND httpRequest.status=503 AND timestamp>=\"2026-10-18T22:20:00Z\" AND timestamp<=\"2026-10-18T22:34:20Z\"", "orderBy": "timestamp asc", "pageSize": 2}
13. `read_more` {"result_id": "r8e09b0", "grep": "\"status\": 503", "fields": "timestamp,resource.labels.revision_name"}
14. `gcp_list_log_entries` {"resourceNames": ["projects/shop-prod-4f2a"], "filter": "timestamp>=\"2026-10-18T22:24:00Z\" AND timestamp<=\"2026-10-18T22:34:00Z\" AND logName=\"projects/shop-prod-4f2a/logs/run.googleapis.com%2Frequests\"", "orderBy": "timestamp asc", "pageSize": 3}
15. `read_more` {"result_id": "r1ee2e5", "grep": "requests", "fields": "timestamp,httpRequest.status,resource.labels.revision_name"}
16. `read_more` {"result_id": "r1ee2e5", "grep": "checkout-00001", "fields": "timestamp,logName,httpRequest.status"}
17. `gcp_list_timeseries` {"name": "projects/shop-prod-4f2a", "filter": "metric.type=\"run.googleapis.com/request_count\" AND resource.labels.service_name=\"checkout\"", "interval": {"startTime": "2026-10-18T22:15:00Z", "endTime": "2026-10-18T22:40:00Z"}, "view": "FULL", "aggregation": {"alignmentPeriod": "300s", "perSeriesA
18. `read_more` {"result_id": "rc1a371", "grep": "UPSTREAM_TIMEOUT_MS", "fields": "timestamp,protoPayload.methodName,protoPayload.resourceName,logName"}
19. `read_more` {"result_id": "rc1a371", "grep": "checkout-00002-nt5", "fields": "timestamp,protoPayload.response.metadata.annotations", "start": 0}
20. `read_more` {"result_id": "rc1a371", "grep": "\"2026-10-18T22:24:09", "fields": "timestamp,logName,jsonPayload,textPayload"}
21. `gcp_list_log_entries` {"resourceNames": ["projects/shop-prod-4f2a"], "filter": "logName=\"projects/shop-prod-4f2a/logs/cloudaudit.googleapis.com%2Factivity\" AND timestamp>=\"2026-10-18T22:22:30Z\" AND timestamp<=\"2026-10-18T22:22:45Z\"", "pageSize": 3}
22. `read_more` {"result_id": "rc1a371", "grep": "cloudaudit.googleapis.com%2Factivity", "fields": "timestamp,protoPayload.methodName,protoPayload.authenticationInfo.principalEmail,protoPayload.request.service.spec.template.spec.containers"}
23. `read_more` {"result_id": "rc1a371", "grep": "cloudaudit.googleapis.com%2Factivity", "fields": "protoPayload.request.service.spec.template.spec.containers", "start": 0}
24. `read_more` {"result_id": "r8885b7", "grep": "checkout-00002-nt5", "fields": "timestamp,logName"}
25. `gcp_list_log_entries` {"resourceNames": ["projects/shop-prod-4f2a"], "filter": "protoPayload.methodName=\"google.cloud.run.v1.Services.ReplaceService\" AND protoPayload.authenticationInfo.principalEmail:\"@\"", "pageSize": 5, "orderBy": "timestamp desc"}
26. `read_more` {"result_id": "ra8f8dc", "grep": "principalEmail", "fields": "timestamp,protoPayload.methodName,protoPayload.authenticationInfo.principalEmail,protoPayload.resourceName"} (error)
27. `read_more` {"result_id": "rc1a371", "grep": "ReplaceService", "fields": "timestamp,protoPayload.authenticationInfo.principalEmail,logName"} (error)
28. `read_more` {"result_id": "rc1a371", "grep": "principalEmail"} (error)
