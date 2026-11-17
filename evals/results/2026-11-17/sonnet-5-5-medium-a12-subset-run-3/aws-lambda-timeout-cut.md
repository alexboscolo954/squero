## The Lambda function checkout is timing out on every invocation. A config update at 02:48:49Z cut its timeout from 10s to 1s, and the alarm fired on that Errors metric.

**Root cause** (high confidence, `bad_config`): UpdateFunctionConfiguration by 'deployer' at 02:48:49Z set timeout=1s. The function was created at 02:43:31Z with timeout=10s. Invocations now hit the 1s limit (Duration: 1000.00 ms, Status: timeout), while memory use is low (37 MB of 128 MB). The logs show the function needs more than 1s, so the new timeout is too short. The update request also shows environment {}, but the create request had an empty environment too, so that is not a change.
**Severity:** critical  ·  **Affected:** lambda/function/checkout

**Evidence**
- `aws_lookup_events`: 02:48:49Z UpdateFunctionConfiguration by deployer set timeout=1. CreateFunction at 02:43:31Z had timeout=10.
- `aws_query_logs`: Recent REPORT lines all show Duration: 1000.00 ms, Status: timeout, Max Memory Used 37 MB of 128 MB.
- `aws_describe_alarms`: checkout-errors (AWS/Lambda Errors, FunctionName=checkout, Sum, threshold >0) is in ALARM. The datapoint at 02:48 crossed the threshold.

**Recommendations**
1. Restore the checkout timeout to 10s (or another value above the real execution time) with update-function-configuration.
2. Check why the deploy set timeout=1 and fix that in the pipeline or IaC.
3. After the change, confirm the Errors metric returns to 0 and the logs show no more Status: timeout.
4. Add a review check for timeout changes.

**Tool calls**
1. `aws_describe_alarms` {"state": "ALARM"}
2. `aws_lookup_events` {"resource_name": "checkout"}
3. `aws_query_logs` {"log_groups": ["/aws/lambda/checkout"], "query": "fields @timestamp, @message | filter @message like /ERROR|Error|error/ | sort @timestamp desc | limit 20"}
