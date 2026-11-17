**oncall@example.com** deployed it. The 503s come from revision `checkout-00002-nt5` of the `checkout` service in us-central1.

- **Deploy time:** the `Services.ReplaceService` audit entry was logged at **2026-10-18 22:22:37Z**, with principal `oncall@example.com` (run via gcloud 589.0.0). The revision was created at 22:22:38Z and became Ready at 22:24:18Z, so it has been serving since about 22:24Z.
- **Likely cause:** the previous revision `checkout-00001-ldp` (created 22:17Z, also by oncall@example.com) had `UPSTREAM_TIMEOUT_MS=2000`. Revision 00002 has `UPSTREAM_TIMEOUT_MS=1`. Every request then fails with "inventory lookup failed after 1 ms: URLError: timed out" and returns 503. Raising the timeout back (for example to 2000) and redeploying should fix it.
- **Gap:** I did not pin down the timestamp of the first 503. The only 503s I saw were from 00002, and the newest ones were at 22:34Z. Treat 22:24Z as the earliest they could have started.

**Evidence**
- `gcp_list_log_entries`: Activity audit log, methodName google.cloud.run.v1.Services.ReplaceService on services/checkout, principalEmail oncall@example.com, timestamp 2026-10-18T22:22:37.931973Z
- `gcp_list_log_entries`: system_event audit entry for revisions/checkout-00002-nt5: creator oncall@example.com, created 22:22:38Z, 'Deploying revision succeeded in 1m40.46s' at 22:24:18Z, env UPSTREAM_TIMEOUT_MS=1. Revision checkout-00001-ldp had UPSTREAM_TIMEOUT_MS=2000.
- `gcp_list_log_entries`: Cloud Run request logs show HTTP 503 from checkout-00002-nt5 (latest 22:34:30Z), with stdout errors 'inventory lookup failed after 1 ms: URLError: <urlopen error timed out>'

**Tool calls**
1. `gcp_list_log_entries` {"resourceNames": ["projects/shop-prod-4f2a"], "filter": "logName=\"projects/shop-prod-4f2a/logs/cloudaudit.googleapis.com%2Factivity\" AND resource.type=\"cloud_run_revision\" OR protoPayload.serviceName=\"run.googleapis.com\"", "orderBy": "timestamp desc", "pageSize": 20}
2. `gcp_list_log_entries` {"resourceNames": ["projects/shop-prod-4f2a"], "filter": "resource.type=\"cloud_run_revision\" AND httpRequest.status=503", "orderBy": "timestamp desc", "pageSize": 10}
3. `gcp_list_log_entries` {"resourceNames": ["projects/shop-prod-4f2a"], "filter": "logName=\"projects/shop-prod-4f2a/logs/cloudaudit.googleapis.com%2Factivity\" AND protoPayload.serviceName=\"run.googleapis.com\"", "orderBy": "timestamp desc", "pageSize": 10}
4. `read_more` {"result_id": "rda12f4", "grep": "methodName"}
5. `gcp_list_log_entries` {"resourceNames": ["projects/shop-prod-4f2a"], "filter": "logName=\"projects/shop-prod-4f2a/logs/cloudaudit.googleapis.com%2Factivity\" AND timestamp>=\"2026-10-18T22:15:00Z\"", "orderBy": "timestamp asc", "pageSize": 5}
6. `read_more` {"result_id": "rf81d85", "grep": "principalEmail", "fields": "[\"timestamp\",\"protoPayload.methodName\",\"protoPayload.resourceName\",\"protoPayload.authenticationInfo.principalEmail\"]"}
7. `read_more` {"result_id": "rda12f4", "grep": "authenticationInfo", "fields": "[\"timestamp\",\"logName\",\"protoPayload.methodName\",\"protoPayload.authenticationInfo\"]"}
8. `gcp_list_log_entries` {"resourceNames": ["projects/shop-prod-4f2a"], "filter": "protoPayload.methodName:\"Services\" AND timestamp>=\"2026-10-18T22:15:00Z\" AND timestamp<=\"2026-10-18T22:30:00Z\"", "orderBy": "timestamp asc", "pageSize": 5}
9. `read_more` {"result_id": "rda12f4", "grep": "serving.knative.dev/creator", "fields": "[\"timestamp\",\"protoPayload.methodName\",\"protoPayload.resourceName\"]"}
10. `read_more` {"result_id": "rda12f4", "grep": "methodName", "fields": "[\"timestamp\",\"protoPayload.methodName\",\"protoPayload.resourceName\",\"protoPayload.response.metadata.annotations\"]"}
11. `read_more` {"result_id": "rda12f4", "grep": "lastModifier", "start": 7800}
12. `read_more` {"result_id": "rda12f4", "grep": "lastModifier", "start": 0, "fields": "[\"timestamp\",\"protoPayload.methodName\"]"}
13. `gcp_list_log_entries` {"resourceNames": ["projects/shop-prod-4f2a"], "filter": "logName=\"projects/shop-prod-4f2a/logs/cloudaudit.googleapis.com%2Factivity\" AND protoPayload.methodName=\"google.cloud.run.v1.Services.ReplaceService\"", "orderBy": "timestamp desc", "pageSize": 1}
14. `read_more` {"result_id": "rf21c8f", "grep": "principalEmail"}
15. `gcp_list_log_entries` {"resourceNames": ["projects/shop-prod-4f2a"], "filter": "resource.type=\"cloud_run_revision\" AND httpRequest.status=503 AND resource.labels.revision_name=\"checkout-00002-nt5\"", "orderBy": "timestamp asc", "pageSize": 1}
16. `read_more` {"result_id": "re447ca", "grep": "\\\"status\\\": 503", "fields": "[\"timestamp\"]"}
17. `read_more` {"result_id": "re447ca", "grep": "503", "fields": "[\"timestamp\",\"httpRequest.status\"]"}
18. `read_more` {"result_id": "re447ca", "start": 45000, "grep": "\"status\": 503"}
19. `read_more` {"result_id": "re447ca", "start": 44000}
20. `read_more` {"result_id": "re447ca", "start": 0, "grep": "checkout-00001-ldp", "fields": "[\"timestamp\"]"}
21. `read_more` {"result_id": "re447ca", "start": 36000, "grep": "\"requests\""}
22. `gcp_list_log_entries` {"resourceNames": ["projects/shop-prod-4f2a"], "filter": "logName=\"projects/shop-prod-4f2a/logs/run.googleapis.com%2Frequests\" AND httpRequest.status=503 AND timestamp>=\"2026-10-18T22:24:00Z\"", "orderBy": "timestamp asc", "pageSize": 1}
23. `read_more` {"result_id": "r5f1560", "start": 44000}
24. `read_more` {"result_id": "r5f1560", "start": 0, "grep": "stdout", "fields": "[\"timestamp\",\"jsonPayload.message\"]"}
25. `read_more` {"result_id": "r5f1560", "start": 36000, "grep": "Services.ReplaceService", "fields": "[\"timestamp\"]"}
26. `read_more` {"result_id": "r5f1560", "start": 0, "grep": "checkout listening"} (error)
27. `gcp_list_log_entries` {"resourceNames": ["projects/shop-prod-4f2a"], "filter": "x"} (error)
28. `read_more` {"result_id": "rda12f4", "grep": "x"} (error)
