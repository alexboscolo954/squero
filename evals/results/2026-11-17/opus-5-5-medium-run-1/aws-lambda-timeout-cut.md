## Every checkout Lambda invocation has timed out since 02:48:49Z. At that moment the 'deployer' user changed the function's timeout from 10s to 1s, but the function normally takes about 1.2s per request, so each call is cut off at exactly 1000 ms.

**Root cause** (high confidence, `bad_config`): CloudTrail shows UpdateFunctionConfiguration on checkout at 2026-10-19T02:48:49Z by 'deployer' with timeout: 1. The function was created at 02:43:31Z with timeout: 10. From 02:43 to 02:48 the Lambda Duration metric peaked at about 1202 ms per minute, so the function needs roughly 1.2s. Since 02:49 the peak has been exactly 1000.0 ms. Every REPORT line now reads 'Duration: 1000.00 ms ... Status: timeout'. Memory is not the cause: 37 MB used of 128 MB. The 1s timeout is shorter than the function's normal ~1.2s run time, so every invocation is cut off and counted as an error. That is what tripped the checkout-errors alarm (Errors Sum > 0, first breach at 02:48).
**Severity:** critical  ·  **Affected:** aws/lambda/checkout

**Evidence**
- `aws_describe_alarms`: checkout-errors is in ALARM: AWS/Lambda Errors (FunctionName=checkout) Sum 1.0 at 02:48 crossed the threshold of 0.
- `aws_lookup_events`: UpdateFunctionConfiguration by 'deployer' at 2026-10-19T02:48:49Z set checkout timeout to 1. CreateFunction at 02:43:31Z had set timeout to 10 and memorySize to 128.
- `aws_get_metric_data`: Max Duration for checkout was about 1202 ms per minute from 02:43 to 02:48, then exactly 1000.0 ms every minute from 02:49 to 02:58, i.e. capped by the new timeout.
- `aws_query_logs`: Recent /aws/lambda/checkout REPORT lines (02:58–02:59) all show 'Duration: 1000.00 ms ... Max Memory Used: 37 MB Status: timeout'. Grepping the result for 'Status: success' found no matches.

**Recommendations**
1. Set checkout's timeout back to 10s, or to at least several seconds above the ~1.2s it normally runs (e.g. aws lambda update-function-configuration --function-name checkout --timeout 10). This change needs human approval.
2. Check that the Errors metric returns to 0 and that REPORT lines show successful invocations of about 1.2s again.
3. Ask the 'deployer' pipeline or owner why the timeout was set to 1s, and fix the source config (IaC or deploy script) so the next deploy doesn't bring it back.
4. Add a guard in the deploy pipeline that blocks a timeout lower than the function's observed p99 duration plus some headroom.
5. Find out whether callers retried or dropped checkout requests during the outage, which started at 02:48:49Z, and follow up on any failed orders.

**Tool calls**
1. `aws_describe_alarms` {"state": "ALARM"}
2. `aws_lookup_events` {"resource_name": "checkout"}
3. `aws_query_logs` {"log_groups": ["/aws/lambda/checkout"], "query": "fields @timestamp, @message | filter @message like /ERROR|Error|Task timed out|error/ | sort @timestamp desc | limit 20"}
4. `aws_query_logs` {"log_groups": ["/aws/lambda/checkout"], "query": "fields @timestamp, @message | filter @message like /REPORT/ | sort @timestamp asc | limit 15"}
5. `aws_get_metric_data` {"namespace": "AWS/Lambda", "metric_name": "Duration", "dimensions": {"FunctionName": "checkout"}, "minutes": 30, "period": 60, "stat": "Maximum"}
6. `read_more` {"result_id": "r88e1c7", "grep": "Status: success"}
