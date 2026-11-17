## The checkout Lambda is timing out on every invocation. A config update at 02:48:49Z cut the timeout from 10s to 1s.

**Root cause** (high confidence, `bad_config`): UpdateFunctionConfiguration by 'deployer' at 02:48:49Z set timeout=1 and environment={}. The function was created at 02:43:31Z with timeout=10 and 128MB. Every recent invocation now ends with 'Duration: 1000.00 ms ... Status: timeout' and uses only 37MB of 128MB. Memory is not the constraint, so the 1s limit is cutting off normal work. I did not find pre-change durations, so the exact time the function needs is not confirmed. The same update also sent an empty environment, which may have dropped env vars the function needs.
**Severity:** critical  ·  **Affected:** lambda/function/checkout

**Evidence**
- `aws_lookup_events`: 02:48:49Z UpdateFunctionConfiguration20150331v2 by deployer: timeout 1, environment {}. The original CreateFunction at 02:43:31Z had timeout 10, memorySize 128.
- `aws_query_logs`: Recent REPORT lines all show Duration: 1000.00 ms, Status: timeout, Max Memory Used 37 MB of 128 MB, about one every 3 seconds.
- `aws_describe_alarms`: checkout-errors (AWS/Lambda Errors, Sum, 60s, threshold >0) is in ALARM, with its first datapoint at 02:48, right when the config change was made.

**Recommendations**
1. Restore the timeout to 10s: aws lambda update-function-configuration --function-name checkout --timeout 10.
2. Check whether the empty environment {} removed required env vars, and restore them if so.
3. Check the IaC/deploy pipeline that pushed timeout=1 and environment={}, and fix it so it can't repeat.
4. After the fix, confirm the Errors metric returns to 0 and that durations are normal.
5. Add a guard such as a review or policy check on timeout changes.

**Tool calls**
1. `aws_describe_alarms` {"state": "ALARM"}
2. `aws_lookup_events` {"resource_name": "checkout"}
3. `aws_query_logs` {"log_groups": ["/aws/lambda/checkout"], "query": "fields @timestamp, @message | filter @message like /ERROR|Error|error/ | sort @timestamp desc | limit 20"}
4. `aws_query_logs` {"log_groups": ["/aws/lambda/checkout"], "query": "fields @timestamp, @message | filter @message like /REPORT/ and @message not like /timeout/ | sort @timestamp desc | limit 5"}
5. `aws_query_logs` {"log_groups": ["/aws/lambda/checkout"], "query": "fields @timestamp, @message | filter @message like /Duration/ and @message like /Status: timeout/ | sort @timestamp asc | limit 1", "minutes": 240}
