## The Lambda function checkout is timing out on every invocation. A config update at 02:48:49Z cut its timeout from 10s to 1s, and the function normally runs about 1.2s.

**Root cause** (high confidence, `bad_config`): UpdateFunctionConfiguration by 'deployer' at 02:48:49Z set timeout=1s. The function was created at 02:43:31Z with timeout=10s. Before the change, max Duration was about 1202ms every minute, so a normal run takes longer than 1s. Since the change, every invocation is killed at exactly 1000ms with Status: timeout. Errors went from 0 to about 18 per minute. Memory is not the problem: 37MB used of 128MB. The same request also sent an empty environment {}, but the logs show no environment-related errors.
**Severity:** critical  ·  **Affected:** lambda/function/checkout

**Evidence**
- `aws_lookup_events`: 02:48:49Z UpdateFunctionConfiguration20150331v2 by deployer with timeout=1 and environment {}. The earlier CreateFunction at 02:43:31Z had timeout=10, memorySize=128.
- `aws_query_logs`: Every recent REPORT line shows Duration: 1000.00 ms, Status: timeout, Max Memory Used 37 MB of 128 MB.
- `aws_get_metric_data`: Errors were 0 until 02:47, then 3 at 02:48 and about 18 per minute from 02:49. Max Duration was about 1202ms before 02:48 and a flat 1000ms after.
- `aws_describe_alarms`: checkout-errors is in ALARM. It watches AWS/Lambda Errors (Sum, 60s period) for FunctionName=checkout, with a threshold above 0.

**Recommendations**
1. Restore the checkout function timeout to 10s (or at least a few seconds above the roughly 1.2s typical duration), for example with aws lambda update-function-configuration --timeout 10.
2. Check whether the empty environment {} in the same update dropped any variables the function needs, and restore them if so.
3. Confirm Errors returns to 0 and Duration goes back to about 1.2s.
4. Review the deployer pipeline and add validation so a timeout below the function's observed duration can't be deployed.
5. Look for failed or retried checkout requests from the 02:48–now window and reprocess them if needed.

**Tool calls**
1. `aws_describe_alarms` {"state": "ALARM"}
2. `aws_describe_log_groups` {"prefix": "/aws/lambda/checkout"}
3. `aws_lookup_events` {"resource_name": "checkout"}
4. `aws_query_logs` {"log_groups": ["/aws/lambda/checkout"], "query": "fields @timestamp, @message | sort @timestamp desc | limit 30"}
5. `aws_get_metric_data` {"namespace": "AWS/Lambda", "metric_name": "Duration", "dimensions": {"FunctionName": "checkout"}, "stat": "Maximum", "minutes": 60, "period": 60}
6. `aws_get_metric_data` {"namespace": "AWS/Lambda", "metric_name": "Errors", "dimensions": {"FunctionName": "checkout"}, "minutes": 30, "period": 300}
7. `aws_lookup_events` {"resource_name": "checkout", "minutes": 5, "event_name": "UpdateFunctionConfiguration20150331v2"}
8. `aws_describe_alarms` {"prefix": "checkout"}
