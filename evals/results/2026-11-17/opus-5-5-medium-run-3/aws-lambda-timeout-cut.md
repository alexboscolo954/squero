## The checkout Lambda's timeout was cut from 10 s to 1 s at 02:48:49Z, by user 'deployer' in an UpdateFunctionConfiguration call. The function normally needs about 1.2 s per invocation, so since then every invocation we looked at hits the 1 s limit, times out and counts as an error.

**Root cause** (high confidence, `bad_config`): A config change at 2026-10-19T02:48:49Z (UpdateFunctionConfiguration by 'deployer') set checkout's timeout to 1 s. The function was created at 02:43:31Z with a 10 s timeout. Before the change, the longest invocation each minute (02:43–02:48) was about 1202 ms. From 02:49 onward every invocation stops at exactly 1000.00 ms with 'Status: timeout'. Memory isn't the problem: 37 MB used of 128 MB. The new 1 s limit is below the roughly 1.2 s the handler needs, so the timeouts show up as Lambda Errors and set off the checkout-errors alarm.
**Severity:** critical  ·  **Affected:** aws/lambda/checkout

**Evidence**
- `aws_describe_alarms`: checkout-errors is in ALARM: AWS/Lambda Errors (Sum, 60 s period) for FunctionName=checkout was above 0 (value 1.0 at 02:48).
- `aws_lookup_events`: 02:48:49Z UpdateFunctionConfiguration by 'deployer' set checkout timeout to 1. The function was created at 02:43:31Z with timeout 10 and memory 128.
- `aws_get_metric_data`: Maximum Duration was about 1202 ms each minute from 02:43 to 02:48, then exactly 1000.0 ms every minute from 02:49 to 02:58.
- `aws_query_logs`: In /aws/lambda/checkout, every REPORT line we looked at, from about 02:58:37 to 02:59:37, shows 'Duration: 1000.00 ms ... Max Memory Used: 37 MB Status: timeout'.

**Recommendations**
1. Set the checkout timeout back to 10 s, its value at creation (or at least comfortably above the ~1.2 s it needs). This needs human approval.
2. Check with 'deployer' or the owning team why the timeout was changed to 1 s, and fix the source config (IaC or pipeline) so the next deploy doesn't set it again.
3. After the change, confirm that Errors returns to 0, Duration returns to about 1.2 s, and the checkout-errors alarm goes back to OK.
4. Add a check to the deploy pipeline that blocks timeout values below the function's observed p99 duration plus a margin.

**Tool calls**
1. `aws_describe_alarms` {"state": "ALARM"}
2. `aws_lookup_events` {"resource_name": "checkout"}
3. `aws_query_logs` {"log_groups": ["/aws/lambda/checkout"], "query": "fields @timestamp, @message | filter @message like /(?i)error|timed out|REPORT/ | sort @timestamp desc | limit 30"}
4. `aws_get_metric_data` {"namespace": "AWS/Lambda", "metric_name": "Duration", "dimensions": {"FunctionName": "checkout"}, "period": 60, "stat": "Maximum", "minutes": 30}
