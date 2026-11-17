## Checkout is returning 503 on nearly every request. Revision checkout-00002-nt5 was deployed at 22:22 UTC by oncall@example.com and set UPSTREAM_TIMEOUT_MS to 1 ms, down from the 2000 ms default. Each inventory call now times out before it can finish.

**Root cause** (high confidence, `bad_rollout`): oncall@example.com used gcloud to run a ReplaceService on Cloud Run service checkout at 2026-10-18T22:22:37Z. That created revision checkout-00002-nt5 with env UPSTREAM_TIMEOUT_MS="1". The code reads this as the inventory call timeout (urlopen timeout=T/1000, so 0.001 s) and defaults to 2000 ms. The previous revision checkout-00001-ldp logged "checkout listening, inventory timeout 2000 ms". Since the new revision went live, every request logs "inventory lookup failed after 1 ms: URLError: <urlopen error timed out>" (timeout_ms=1) and returns 503 in about 24–28 ms. A real outbound HTTPS call cannot finish in 1 ms, so the new value fails every time. The inventory service itself is not shown to be failing. I did not confirm that revision 00001 served 200s, because the log page I pulled had no 200 responses. I also did not query request-count metrics to measure the 5xx rate.
**Severity:** critical  ·  **Affected:** shop-prod-4f2a/CloudRunService/checkout

**Evidence**
- `gcp_list_log_entries`: Revision checkout-00002-nt5 logs repeat "inventory lookup failed after 1 ms: URLError: <urlopen error timed out>" (upstream=inventory, timeout_ms=1), each paired with a GET / that returns status 503 in about 0.024–0.028 s (22:34 UTC).
- `gcp_list_log_entries`: Audit log: oncall@example.com (gcloud 589.0.0) ran Services.ReplaceService on checkout at 2026-10-18T22:22:37Z, creating revision checkout-00002-nt5 with env UPSTREAM_TIMEOUT_MS="1". The code default is 2000, and the timeout is passed as urlopen(timeout=T/1000). The revision became Ready at 22:24:18Z.
- `gcp_list_log_entries`: The previous revision checkout-00001-ldp (created 22:17 by CreateService) logged "checkout listening, inventory timeout 2000 ms" at 22:17:15Z.

**Recommendations**
1. Roll traffic back to the last good revision: gcloud run services update-traffic checkout --region us-central1 --to-revisions checkout-00001-ldp=100. This needs human approval.
2. Or deploy a fixed revision with UPSTREAM_TIMEOUT_MS set back to 2000 (or removed so the default applies).
3. Confirm recovery: the 503s and the 'inventory lookup failed after 1 ms' errors should stop, and run_googleapis_com:request_count{monitored_resource="cloud_run_revision",service_name="checkout"} should shift back to 2xx.
4. Ask oncall@example.com why the timeout was changed, probably a ms/s unit mix-up. Add validation or a minimum for UPSTREAM_TIMEOUT_MS. Also note the new revision sets autoscaling maxScale=1; check that this was intended.

**Tool calls**
1. `gcp_list_log_entries` {"resourceNames": ["projects/shop-prod-4f2a"], "filter": "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"checkout\" AND severity>=ERROR", "orderBy": "timestamp desc", "pageSize": 10}
2. `gcp_list_log_entries` {"resourceNames": ["projects/shop-prod-4f2a"], "filter": "logName=\"projects/shop-prod-4f2a/logs/cloudaudit.googleapis.com%2Factivity\"", "orderBy": "timestamp desc", "pageSize": 10}
3. `read_more` {"result_id": "r5ccbb3", "grep": "methodName"}
4. `gcp_list_log_entries` {"resourceNames": ["projects/shop-prod-4f2a"], "filter": "logName=\"projects/shop-prod-4f2a/logs/cloudaudit.googleapis.com%2Factivity\" AND protoPayload.methodName:\"Service\"", "orderBy": "timestamp desc", "pageSize": 5}
5. `read_more` {"result_id": "r5ccbb3", "grep": "checkout-00001", "fields": "timestamp,resource.labels.revision_name,jsonPayload.message,httpRequest.status,protoPayload.methodName"}
6. `read_more` {"result_id": "r5ccbb3", "grep": "\"status\": 200", "fields": "timestamp,resource.labels.revision_name,httpRequest.status,httpRequest.latency"}
