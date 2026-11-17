**oncall@example.com** deployed the failing revision, `checkout-00002-nt5` (service `checkout`, us-central1), at about **2026-10-18 22:22:38 UTC**.

- **Deploy:** the Admin Activity audit log has a `google.cloud.run.v1.Services.ReplaceService` call by `oncall@example.com` on `services/checkout` at 22:22:37.9Z. It was made through gcloud 589.0.0. The revision was created at 22:22:38 and became Ready at about 22:24:16–22:24:18.
- **Cause of the 503s:** the deploy changed the env var `UPSTREAM_TIMEOUT_MS` from `2000` (the earlier revision `checkout-00001-ldp`, per the earlier service-create log) to `1` (revision `-00002`). The app then fails its upstream call within 1 ms and returns 503 "inventory unavailable". The revision's logs show `inventory lookup failed after 1 ms: URLError: <urlopen error timed out>` alongside 503 responses.
- **Likely fix:** restore `UPSTREAM_TIMEOUT_MS` to `2000`, or roll traffic back to `checkout-00001-ldp`.
- **Limit on the 503 timing:** the log queries returned the newest entries, and I did not see the first 503. The 503s I saw were at 22:34:23–22:34:31Z, all on `checkout-00002-nt5`, about 10 minutes after the deploy. The log filters did not narrow results as expected, so I read the audit entries out of the oversized result instead.

**Evidence**
- `gcp_list_log_entries`: Audit log (cloudaudit activity), principalEmail oncall@example.com, methodName google.cloud.run.v1.Services.ReplaceService on namespaces/shop-prod-4f2a/services/checkout, timestamp 2026-10-18T22:22:37.93Z, client gcloud 589.0.0. Same entry set also has CreateService (22:17:04Z) and SetIamPolicy (22:17:08Z), both by oncall@example.com.
- `gcp_list_log_entries`: system_event log for revision checkout-00002-nt5: serving.knative.dev/creator oncall@example.com, creationTimestamp 2026-10-18T22:22:38Z, 'Deploying revision succeeded in 1m40.46s', Ready 22:24:18Z; env UPSTREAM_TIMEOUT_MS=1. The service-create log for checkout-00001-ldp shows UPSTREAM_TIMEOUT_MS=2000.
- `gcp_list_log_entries`: Request logs on revision checkout-00002-nt5 show HTTP 503 responses (22:34:23–22:34:31Z), with stdout ERROR 'inventory lookup failed after 1 ms: URLError: <urlopen error timed out>' (timeout_ms 1, upstream inventory).

**Tool calls**
1. `gcp_list_log_entries` {"resourceNames": ["projects/shop-prod-4f2a"], "filter": "logName=\"projects/shop-prod-4f2a/logs/cloudaudit.googleapis.com%2Factivity\" AND protoPayload.resourceName:\"checkout\"", "orderBy": "timestamp desc", "pageSize": 20}
2. `gcp_list_log_entries` {"resourceNames": ["projects/shop-prod-4f2a"], "filter": "resource.type=\"cloud_run_revision\" AND httpRequest.status=503", "orderBy": "timestamp desc", "pageSize": 10}
3. `read_more` {"result_id": "r000161", "grep": "methodName", "fields": "[\"timestamp\",\"protoPayload.methodName\",\"protoPayload.resourceName\",\"protoPayload.authenticationInfo.principalEmail\",\"logName\"]"}
4. `gcp_list_log_entries` {"resourceNames": ["projects/shop-prod-4f2a"], "filter": "logName=\"projects/shop-prod-4f2a/logs/cloudaudit.googleapis.com%2Factivity\" AND timestamp>=\"2026-10-18T00:00:00Z\"", "orderBy": "timestamp desc", "pageSize": 20}
5. `read_more` {"result_id": "rb8a941", "grep": "cloudaudit"}
6. `read_more` {"result_id": "rb8a941", "grep": "principalEmail", "fields": "[\"timestamp\",\"logName\",\"protoPayload.methodName\",\"protoPayload.resourceName\",\"protoPayload.authenticationInfo\"]"}
7. `gcp_list_log_entries` {"resourceNames": ["projects/shop-prod-4f2a"], "filter": "protoPayload.methodName:\"ReplaceService\" OR protoPayload.methodName:\"CreateService\" OR protoPayload.methodName:\"UpdateService\"", "orderBy": "timestamp desc", "pageSize": 5}
8. `read_more` {"result_id": "rb8a941", "grep": "cloudaudit.googleapis.com%2Factivity", "fields": "[\"timestamp\",\"logName\",\"protoPayload.methodName\",\"protoPayload.resourceName\",\"protoPayload.authenticationInfo\"]"}
9. `read_more` {"result_id": "rb8a941", "grep": "cloudaudit", "fields": "[\"timestamp\",\"logName\",\"protoPayload.methodName\",\"protoPayload.resourceName\"]"}
10. `gcp_list_log_entries` {"resourceNames": ["projects/shop-prod-4f2a"], "filter": "logName=\"projects/shop-prod-4f2a/logs/cloudaudit.googleapis.com%2Fsystem_event\" AND timestamp>=\"2026-10-18T22:15:00Z\" AND timestamp<=\"2026-10-18T22:30:00Z\"", "orderBy": "timestamp asc", "pageSize": 5}
11. `read_more` {"result_id": "rb8a941", "grep": "oncall@example.com", "start": 7800}
12. `read_more` {"result_id": "rb8a941", "grep": "checkout-00001"}
13. `read_more` {"result_id": "rb8a941", "grep": "ReplaceService", "start": 0, "fields": "[\"timestamp\",\"logName\"]"}
14. `read_more` {"result_id": "rb8a941", "grep": "cloudaudit.googleapis.com%2Factivity", "start": 7800}
15. `read_more` {"result_id": "rb8a941", "grep": "cloudaudit.googleapis.com%2Factivity"}
16. `read_more` {"result_id": "rb8a941", "grep": "checkout-00002-nt5", "start": 0, "fields": "[\"timestamp\"]"}
17. `gcp_list_log_entries` {"resourceNames": ["projects/shop-prod-4f2a"], "filter": "resource.type=\"cloud_run_revision\" AND resource.labels.revision_name=\"checkout-00002-nt5\" AND httpRequest.status=503 AND timestamp<=\"2026-10-18T22:30:00Z\"", "orderBy": "timestamp asc", "pageSize": 1}
18. `read_more` {"result_id": "rb8a941", "grep": "startup|checkout listening", "start": 0}
19. `gcp_list_log_entries` {"resourceNames": ["projects/shop-prod-4f2a"], "filter": "logName=\"projects/shop-prod-4f2a/logs/run.googleapis.com%2Frequests\" AND timestamp>=\"2026-10-18T22:24:00Z\" AND timestamp<=\"2026-10-18T22:30:00Z\"", "orderBy": "timestamp asc", "pageSize": 3}
20. `read_more` {"result_id": "r006ad6", "grep": "\"status\": 503", "fields": "[\"timestamp\"]"}
21. `gcp_list_log_entries` {"resourceNames": ["projects/shop-prod-4f2a"], "filter": "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"checkout\" AND httpRequest.status=503 AND timestamp>=\"2026-10-18T22:24:00Z\"", "orderBy": "timestamp asc", "pageSize": 1}
