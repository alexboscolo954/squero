## Cloud Run service checkout returns 503 on its requests because revision checkout-00002-nt5 went out at 22:22–22:24 UTC with UPSTREAM_TIMEOUT_MS=1. Every inventory lookup now times out after 1 ms. The previous revision, checkout-00001-ldp, started with a 2000 ms timeout.

**Root cause** (high confidence, `bad_rollout`): At 2026-10-18T22:22:37Z oncall@example.com ran a gcloud ReplaceService on checkout. That created revision checkout-00002-nt5 with env UPSTREAM_TIMEOUT_MS="1" (the code's default is 2000), and the revision became Ready at 22:24:18Z. The handler calls the inventory upstream with timeout=T/1000, which is 0.001 s. No network round trip finishes in 1 ms, so every request fails with "URLError: <urlopen error timed out>" and the handler returns 503 "inventory unavailable" in about 24–28 ms. Revision checkout-00001-ldp logged "checkout listening, inventory timeout 2000 ms" at startup. So this change cut the timeout from 2000 ms to 1 ms, and nothing points to a real outage of the inventory upstream. Caveat: I did not pull request metrics, so I have not confirmed that 00001 actually served 200s or measured the exact 5xx ratio.
**Severity:** critical  ·  **Affected:** shop-prod-4f2a/CloudRunService/checkout

**Evidence**
- `gcp_list_log_entries`: checkout-00002-nt5 stdout, repeated through 22:34:31Z: ERROR "inventory lookup failed after 1 ms: URLError: <urlopen error timed out>" timeout_ms=1 upstream=inventory
- `gcp_list_log_entries`: checkout-00002-nt5 request log: GET / returns status 503 in about 0.024–0.028 s for every request sampled (22:34:21Z–22:34:30Z)
- `gcp_list_log_entries`: Audit log: google.cloud.run.v1.Services.ReplaceService by oncall@example.com at 2026-10-18T22:22:37Z creates checkout-00002-nt5 with env UPSTREAM_TIMEOUT_MS="1"; the code reads T = int(os.environ.get("UPSTREAM_TIMEOUT_MS", "2000")) and calls urlopen(timeout=T/1000); revision Ready at 22:24:18Z
- `gcp_list_log_entries`: Previous revision checkout-00001-ldp (created 22:17Z) logged "checkout listening, inventory timeout 2000 ms" at startup

**Recommendations**
1. Send 100% of checkout's traffic back to the previous revision now: gcloud run services update-traffic checkout --region us-central1 --to-revisions checkout-00001-ldp=100 (needs human approval).
2. Or deploy a fix that removes UPSTREAM_TIMEOUT_MS or sets it back to 2000: gcloud run services update checkout --region us-central1 --update-env-vars UPSTREAM_TIMEOUT_MS=2000.
3. Check that the 5xx ratio falls in run.googleapis.com/request_count (by response_code_class) and that the 'inventory lookup failed' errors stop.
4. Ask oncall@example.com why the timeout was set to 1. If it was meant to be seconds, the variable is in milliseconds. Add validation or a minimum value (for example at least 100 ms) for UPSTREAM_TIMEOUT_MS.
5. Think about gradual or tagged rollouts (--no-traffic and then a canary) for checkout so a change like this is caught before it takes all the traffic.

**Tool calls**
1. `gcp_list_log_entries` {"resourceNames": ["projects/shop-prod-4f2a"], "filter": "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"checkout\" AND severity>=ERROR", "orderBy": "timestamp desc", "pageSize": 10}
2. `gcp_list_log_entries` {"resourceNames": ["projects/shop-prod-4f2a"], "filter": "logName=\"projects/shop-prod-4f2a/logs/cloudaudit.googleapis.com%2Factivity\"", "orderBy": "timestamp desc", "pageSize": 10}
3. `read_more` {"result_id": "rfa41e6", "grep": "methodName"}
4. `gcp_list_log_entries` {"resourceNames": ["projects/shop-prod-4f2a"], "filter": "resource.type=\"cloud_run_revision\" AND resource.labels.revision_name=\"checkout-00001\" OR (resource.labels.service_name=\"checkout\" AND jsonPayload.message:\"listening\")", "orderBy": "timestamp desc", "pageSize": 5}
5. `read_more` {"result_id": "rfa41e6", "grep": "00001", "fields": "timestamp,resource.labels.revision_name,protoPayload.methodName,jsonPayload.message,httpRequest.status,protoPayload.authenticationInfo.principalEmail"}
