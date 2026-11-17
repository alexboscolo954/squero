**oncall@example.com deployed it on 2026-10-18 at 22:22:37 UTC** (`google.cloud.run.v1.Services.ReplaceService` on `services/checkout`, run from gcloud 589.0.0).

- **Revision:** that deploy created **`checkout-00002-nt5`**, created at 22:22:38 UTC with `serving.knative.dev/creator: oncall@example.com`. It finished deploying and became Ready and serving at **22:24:18 UTC**. All the 503s I looked at come from this revision.
- **What changed:** this deploy set the env var **`UPSTREAM_TIMEOUT_MS=1`** (1 ms). If the variable is unset, the code defaults to 2000 ms. Now every inventory lookup times out (`inventory lookup failed after 1 ms: URLError: <urlopen error timed out>`) and the handler returns 503 `inventory unavailable`. The same deploy also set the revision's `maxScale` to 1.
- **Earlier activity:** the same person created the service (revision 1) at 22:17:04 UTC and changed its access policy (`SetIamPolicy`) at 22:17:08 UTC.

**What I couldn't confirm:** the 503 logs I read are the most recent ones, from about 22:34 UTC. I didn't find the first 503, so I can't say exactly when they started. They would only have begun once this revision went live at 22:24:18.

**Suggested fix (I can only read, so I haven't changed anything):** move traffic back to the previous revision, or redeploy with `UPSTREAM_TIMEOUT_MS` unset or set to something sensible like 2000.

**Evidence**
- `gcp_list_log_entries`: Activity audit log: 2026-10-18T22:22:37.931973Z google.cloud.run.v1.Services.ReplaceService on namespaces/shop-prod-4f2a/services/checkout by principalEmail oncall@example.com (earlier: CreateService 22:17:04Z, SetIamPolicy 22:17:08Z, same principal)
- `gcp_list_log_entries`: System-event audit log for revision checkout-00002-nt5: creator oncall@example.com, creationTimestamp 2026-10-18T22:22:38Z, client gcloud 589.0.0, env UPSTREAM_TIMEOUT_MS=1, Ready 'Deploying revision succeeded in 1m40.46s' at 22:24:18Z
- `gcp_list_log_entries`: checkout requests log: status 503 on revision checkout-00002-nt5 (e.g. 22:34:30Z), paired with stdout ERROR 'inventory lookup failed after 1 ms: URLError: <urlopen error timed out>', timeout_ms=1

**Tool calls**
1. `gcp_list_log_entries` {"resourceNames": ["projects/shop-prod-4f2a"], "filter": "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"checkout\" AND httpRequest.status=503", "orderBy": "timestamp asc", "pageSize": 5}
2. `gcp_list_log_entries` {"resourceNames": ["projects/shop-prod-4f2a"], "filter": "logName=\"projects/shop-prod-4f2a/logs/cloudaudit.googleapis.com%2Factivity\" AND protoPayload.resourceName:\"checkout\"", "orderBy": "timestamp desc", "pageSize": 10}
3. `gcp_list_log_entries` {"resourceNames": ["projects/shop-prod-4f2a"], "filter": "logName=\"projects/shop-prod-4f2a/logs/cloudaudit.googleapis.com%2Factivity\" AND timestamp>=\"2026-10-17T00:00:00Z\" AND timestamp<=\"2026-10-18T23:00:00Z\"", "orderBy": "timestamp desc", "pageSize": 10}
4. `read_more` {"result_id": "r7fbc54", "grep": "cloudaudit"}
5. `read_more` {"result_id": "r7fbc54", "grep": "principalEmail", "fields": "timestamp,logName,protoPayload.methodName,protoPayload.resourceName,protoPayload.authenticationInfo.principalEmail"}
