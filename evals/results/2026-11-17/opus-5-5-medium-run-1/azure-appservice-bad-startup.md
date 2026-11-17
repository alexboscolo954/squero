## Since about 15:53 UTC, every request to App Service shop-web (shop-prod-rg) has returned 5xx (Http5xx 84 of 84 requests in the latest 5-minute bucket). This started minutes after oncall@example.com changed the site's web config at 15:50:54–15:50:56 UTC. Before that change the app returned only 2xx.

**Root cause** (medium confidence, `bad_config`): A change to the site's web config (Microsoft.Web/sites/config/write by oncall@example.com, 15:50:54 started, 15:50:56Z succeeded, with an UpdateWebSite at 15:50:55–56) most likely broke shop-web. The app was created at 15:44:36–15:44:58 and returned only 2xx in the following buckets (20/20 and 110/110 requests). In the first 5-minute bucket after the config write, 61 of 64 requests were 5xx and 3 were 2xx. In the next bucket, 84 of 84 were 5xx. A later site write (Microsoft.Web/sites/write) at 15:54:36 came after the errors started and did not fix them. The activity log names the operation but not the new values, so I don't know which setting changed (for example startup command, runtime stack, or app settings). That is why confidence is medium, not high.
**Severity:** critical  ·  **Affected:** shop-prod-rg/AppService/shop-web

**Evidence**
- `azure_monitor_metrics_query`: shop-web, 5-minute buckets: Requests 20, 110, 64, 84; Http2xx 20, 110, 3, 0; Http5xx 0, 0, 61, 84. Errors start in the bucket right after the config write and reach 100%.
- `azure_monitor_activitylog_list`: Microsoft.Web/sites/config/write (config/web) by oncall@example.com: started 15:50:54.72Z, succeeded 15:50:56.27Z. UpdateWebSite succeeded at 15:50:56Z.
- `azure_monitor_activitylog_list`: CreateWebSite and Microsoft.Web/sites/write at 15:44:36–15:44:58Z, then a publishing profile was fetched at 15:44:59. Another Microsoft.Web/sites/write by oncall@example.com at 15:54:36Z, after errors had started.
- `azure_appservice_webapp_get`: shop-web: state Running, enabled, Linux, Basic SKU, lastModifiedTimeUtc 2026-10-18T15:50:55Z, matching the config write.

**Recommendations**
1. Ask oncall@example.com what they changed in the shop-web web config at 15:50:54 UTC and at 15:54:36 UTC (correlation IDs 1c74213a-b88f-4657-aedd-99dee0d3fe1c and 34d49e3f-5559-438a-8b6f-63747c8acae7).
2. Compare the current site config (startup command, linuxFxVersion/runtime stack, app settings, health check path) with the deployment-time values. If the site has Configuration snapshots, check those too.
3. Restore the previous web config values. This is a production write, so get approval before making it. Then confirm the Http5xx/Requests ratio drops back to 0.
4. If the change can't be identified quickly, check the App Service Log stream or Docker container logs for startup or runtime errors to find which setting fails.
5. To prevent a repeat, route production config changes through IaC or deployment slots with a swap, and only after a health check passes.

**Tool calls**
1. `azure_appservice_webapp_get` {"app": "shop-web", "resource-group": "shop-prod-rg"}
2. `azure_monitor_activitylog_list` {"resource-name": "shop-web", "resource-group": "shop-prod-rg", "hours": 24, "top": 20}
3. `read_more` {"result_id": "r91fddc", "fields": "eventTimestamp,operationName.value,caller,status.value"}
4. `azure_monitor_metrics_query` {"resource": "shop-web", "resource-group": "shop-prod-rg", "resource-type": "Microsoft.Web/sites", "metric-namespace": "Microsoft.Web/sites", "metric-names": "Http5xx,Requests", "aggregation": "Total", "interval": "PT5M", "start-time": "2026-10-18T15:00:00Z"}
5. `read_more` {"result_id": "r91fddc", "start": 7800}
