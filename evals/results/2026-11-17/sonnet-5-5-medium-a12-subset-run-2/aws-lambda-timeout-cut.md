## The Lambda function checkout is timing out on every invocation. A config update at 02:48:49Z cut its timeout from 10s to 1s.

**Root cause** (high confidence, `bad_config`): UpdateFunctionConfiguration by 'deployer' at 02:48:49Z set timeout=1s. The function was created at 02:43 with timeout=10s. Every invocation now runs the full 1000 ms and ends with Status: timeout, which counts as an Errors datapoint. Memory is not the problem (37 MB used of 128 MB). The logs show the work needs about 2-3 s per request. START-to-START gaps are about 3 s, and the function is cut off at 1 s. The same update also passed an empty environment, so I can't rule out an env change, but the timeout explains the errors.
**Severity:** critical  ·  **Affected:** lambda/function/checkout

**Evidence**
- `aws_lookup_events`: 02:48:49Z UpdateFunctionConfiguration20150331v2 by deployer set timeout 1 and environment {}; the original CreateFunction at 02:43:31Z had timeout 10.
- `aws_query_logs`: Recent REPORT lines show Duration: 1000.00 ms, Status: timeout, Max Memory Used 37 MB of 128 MB, on every invocation.
- `aws_describe_alarms`: Alarm checkout-errors is in ALARM on AWS/Lambda Errors (Sum, threshold >0), first datapoint at 02:48, which matches the config change.

**Recommendations**
1. Restore the checkout timeout to 10s (or an appropriate value) with update-function-configuration.
2. Check whether the empty environment {} in the same update removed required env vars, and restore them if so.
3. Add a guard in the deploy pipeline against timeout reductions below observed p99 duration.
4. Confirm the Errors metric returns to 0 after the fix.

**Tool calls**
1. `aws_describe_alarms` {"state": "ALARM"}
2. `aws_lookup_events` {"resource_name": "checkout"}
3. `aws_query_logs` {"log_groups": ["/aws/lambda/checkout"], "query": "fields @timestamp, @message | filter @message like /ERROR|Error|error/ | sort @timestamp desc | limit 20"}
