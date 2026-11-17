## The checkout Lambda is timing out on every invocation. A config update at 02:48:49Z cut the timeout from 10s to 1s, and the function normally needs about 1.2s.

**Root cause** (high confidence, `bad_config`): UpdateFunctionConfiguration by 'deployer' at 02:48:49Z set timeout=1s. The function was created at 02:43:31Z with timeout=10s. Its normal max duration before the change was about 1202ms, so it can't finish inside 1s. Since the change, every invocation ends at exactly 1000ms with Status: timeout. Memory is not the cause: 37MB used of 128MB. The update request also showed environment {}, but the function was created with an empty environment too, so that isn't a change.
**Severity:** critical  ·  **Affected:** lambda/checkout

**Evidence**
- `aws_lookup_events`: 02:48:49Z UpdateFunctionConfiguration20150331v2 by deployer with timeout=1. The function was created at 02:43:31Z with timeout=10.
- `aws_query_logs`: Every recent REPORT line shows Duration: 1000.00 ms, Status: timeout, Max Memory Used 37 MB of 128 MB.
- `aws_get_metric_data`: Max Duration was about 1202ms from 02:43 to 02:48. From 02:49 onward it is a flat 1000ms, which is the new timeout cap.
- `aws_describe_alarms`: checkout-errors (AWS/Lambda Errors, Sum, threshold >0) is in ALARM, with its first datapoint at 02:48.

**Recommendations**
1. Restore the checkout timeout to 10s (or at least 3-5s, to leave headroom over the ~1.2s normal runtime) with update-function-configuration.
2. Check what the deployer pipeline or IaC change intended with timeout=1, and fix the source so it doesn't revert.
3. Check for failed or partial checkouts during the roughly 10 minutes of timeouts, and replay them if needed.
4. Add a guard in the pipeline against timeouts below the observed p99 duration.

**Tool calls**
1. `aws_describe_alarms` {"state": "ALARM"}
2. `aws_describe_log_groups` {"prefix": "/aws/lambda/checkout"}
3. `aws_lookup_events` {"resource_name": "checkout"}
4. `aws_query_logs` {"log_groups": ["/aws/lambda/checkout"], "query": "fields @timestamp, @message | sort @timestamp desc | limit 30"}
5. `aws_get_metric_data` {"namespace": "AWS/Lambda", "metric_name": "Duration", "dimensions": {"FunctionName": "checkout"}, "stat": "Maximum", "minutes": 60, "period": 60}
