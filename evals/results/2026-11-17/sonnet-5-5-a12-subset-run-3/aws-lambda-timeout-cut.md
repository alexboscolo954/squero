## The checkout Lambda is timing out on every invocation. A config update at 02:48:49Z cut the timeout from 10s to 1s, but the function normally runs for about 1.2s.

**Root cause** (high confidence, `bad_config`): UpdateFunctionConfiguration by 'deployer' at 02:48:49Z set timeout=1s. The function was created at 02:43:31Z with timeout=10s. Before the change its max Duration was about 1202ms, which is more than 1s. Since the change, every invocation is killed at 1000ms with Status: timeout. Errors went from 0 to about 18 per minute. The same request also sent an empty environment ({}), but the create call had an empty environment too, so that isn't the cause.
**Severity:** critical  ·  **Affected:** lambda/function/checkout

**Evidence**
- `aws_lookup_events`: 02:48:49Z UpdateFunctionConfiguration20150331v2 by deployer with timeout=1. The CreateFunction at 02:43:31Z had timeout=10, memorySize=128.
- `aws_query_logs`: Every recent invocation ends with 'REPORT ... Duration: 1000.00 ms ... Max Memory Used: 37 MB Status: timeout'. Memory is not the limit.
- `aws_get_metric_data`: Duration max was about 1202ms from 02:43 to 02:48, then a flat 1000ms from 02:49. The function needs more than 1s, so a 1s timeout always kills it. The Errors metric went from 0 to 17-18 per minute from 02:49.

**Recommendations**
1. Restore the checkout timeout to the previous 10s with aws lambda update-function-configuration --timeout 10, or roll back the deployer change.
2. Confirm that Errors drop to 0 and Duration returns to about 1.2s.
3. Review the deployer pipeline or IaC that set timeout=1. Add validation so the timeout can't go below the function's observed p99 duration.
4. Check for failed or retried checkout requests during the 02:48 to now window and reprocess them if needed.

**Tool calls**
1. `aws_describe_alarms` {"state": "ALARM"}
2. `aws_describe_log_groups` {"prefix": "/aws/lambda/checkout"}
3. `aws_lookup_events` {"resource_name": "checkout"}
4. `aws_query_logs` {"log_groups": ["/aws/lambda/checkout"], "query": "fields @timestamp, @message | sort @timestamp desc | limit 30", "minutes": 120}
5. `aws_get_metric_data` {"namespace": "AWS/Lambda", "metric_name": "Duration", "dimensions": {"FunctionName": "checkout"}, "stat": "Maximum", "minutes": 60, "period": 60}
6. `aws_get_metric_data` {"namespace": "AWS/Lambda", "metric_name": "Errors", "dimensions": {"FunctionName": "checkout"}, "minutes": 30, "period": 300}
