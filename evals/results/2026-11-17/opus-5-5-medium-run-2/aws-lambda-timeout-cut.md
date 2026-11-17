## The checkout Lambda times out on every call. At 02:48:49Z the deployer user changed its timeout from 10s to 1s. Each call makes an inventory lookup that takes about 1.2s, so all calls now hit the 1000 ms limit.

**Root cause** (high confidence, `bad_config`): CloudTrail shows UpdateFunctionConfiguration on checkout at 2026-10-19T02:48:49Z by the deployer user, setting timeout to 1 (seconds). The function was created at 02:43:31Z with a 10s timeout. The function's own logs show 'inventory lookup took 1.2s' on every call up to 02:48:47, so each call needs more than 1s. Since the change, every REPORT line shows 'Duration: 1000.00 ms ... Status: timeout'. Memory is not the problem: 37 MB used of 128 MB. The Errors alarm went over its threshold of 0 in the 02:48 minute, when the change was made.
**Severity:** critical  ·  **Affected:** aws/lambda/checkout

**Evidence**
- `aws_describe_alarms`: checkout-errors is in ALARM on AWS/Lambda Errors for FunctionName=checkout: 1 datapoint at 02:48:00 was over the threshold of 0.
- `aws_lookup_events`: UpdateFunctionConfiguration20150331v2 on checkout at 2026-10-19T02:48:49Z by deployer with request timeout: 1. The CreateFunction event at 02:43:31Z had timeout: 10 and memorySize: 128.
- `aws_query_logs`: Logs Insights on /aws/lambda/checkout: from 02:58 to 02:59 every REPORT line shows 'Duration: 1000.00 ms ... Max Memory Used: 37 MB Status: timeout'. Earlier lines, 02:45–02:48:47, show 'inventory lookup took 1.2s' on each call.

**Recommendations**
1. Set the checkout timeout back to 10s, the value it was created with and well above the ~1.2s inventory lookup: aws lambda update-function-configuration --function-name checkout --timeout 10. This needs approval from a human.
2. After the change, check that the AWS/Lambda Errors metric for checkout falls to 0 and that REPORT lines no longer show Status: timeout.
3. Ask the deployer, or whatever pipeline uses that user, why the timeout was set to 1s. Fix the source of the setting (IaC/pipeline config) so the next deploy doesn't bring it back.
4. Optionally add a guard such as a Duration p99 alarm or an IaC policy check, so the timeout can't be set below the function's normal latency.

**Tool calls**
1. `aws_describe_alarms` {"state": "ALARM"}
2. `aws_lookup_events` {"resource_name": "checkout"}
3. `aws_query_logs` {"log_groups": ["/aws/lambda/checkout"], "query": "fields @timestamp, @message | filter @message like /ERROR|Error|Task timed out|Exception/ | sort @timestamp desc | limit 20"}
4. `aws_query_logs` {"log_groups": ["/aws/lambda/checkout"], "query": "fields @timestamp, @message | filter @message like /REPORT/ | sort @timestamp asc | limit 15", "minutes": 120}
5. `read_more` {"result_id": "r7c521e", "grep": "02:4"}
