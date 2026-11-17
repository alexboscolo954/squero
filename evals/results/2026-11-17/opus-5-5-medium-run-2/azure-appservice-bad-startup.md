## Since about 15:53 UTC almost every request to App Service shop-web/shop-prod-rg returns 5xx (61 of 64 requests, then 84 of 84). The likeliest cause is a site config write (Microsoft.Web/sites/config/write) by oncall@example.com at 15:50:55, about 3 minutes before onset. The activity log doesn't show what was changed, so this is a strong lead, not proven.

**Root cause** (medium confidence, `bad_config`): The app was created at 15:44:36–15:44:58 and served only 2xx afterwards: 20 then 110 successful requests in the 15:43 and 15:48 buckets, with zero 5xx. At 15:50:54–15:50:56 oncall@example.com wrote the site's web config (sites/config/write, plus an UpdateWebSite). The site's lastModifiedTimeUtc of 15:50:55 matches that write. In the next 5-minute bucket (15:53–15:58), 5xx jumped to 61 of 64 requests, and the bucket after that had 84 of 84 failing. The only other write in the log, a sites/write at 15:54:36, shares correlationId 34d49e3f with the 15:44 site creation. It is probably a late entry for that creation, not a separate change. The activity log only names operations, not the new values, so I can't tell which setting broke the app (startup command, runtime stack, app settings, health check path, etc.). I didn't read app or container logs, so the exact error behind the 5xx is still unknown. Confidence is medium.
**Severity:** critical  ·  **Affected:** shop-prod-rg/Microsoft.Web/sites/shop-web

**Evidence**
- `azure_monitor_activitylog_list`: Microsoft.Web/sites/config/write (config/web) by oncall@example.com at 2026-10-18T15:50:54–15:50:56Z, Succeeded; UpdateWebSite at 15:50:55–15:50:56Z
- `azure_monitor_activitylog_list`: Site created at 15:44:36–15:44:58Z (CreateWebSite, sites/write, correlationId 34d49e3f); the later sites/write at 15:54:36Z has the same correlationId 34d49e3f
- `azure_monitor_metrics_query`: 5-min buckets: Requests 20/110/64/84, Http2xx 20/110/3/0, Http5xx 0/0/61/84. Errors start in the 15:53–15:58 bucket, right after the 15:50:55 config write
- `azure_appservice_webapp_get`: shop-web is in state Running, enabled, Linux, Basic SKU; lastModifiedTimeUtc 2026-10-18T15:50:55Z, matching the config write

**Recommendations**
1. Ask oncall@example.com what they changed in the web config at 15:50:55 UTC, e.g. startup command, linuxFxVersion/runtime, app settings, or health check path.
2. Compare the current site config with the pre-change values (az webapp config show / appsettings list, or a deployment-slot or ARM template baseline). Revert the changed setting to its pre-15:50 value.
3. Read the application/container logs (az webapp log tail or Log stream / Kudu docker logs) to confirm the error behind the 5xx, e.g. container failing to start or a wrong port or command.
4. After reverting, watch Http5xx and Http2xx in 5-minute buckets to confirm recovery, then clear the alert.

**Tool calls**
1. `azure_appservice_webapp_get` {"app": "shop-web", "resource-group": "shop-prod-rg"}
2. `azure_monitor_activitylog_list` {"resource-name": "shop-web", "resource-group": "shop-prod-rg", "hours": 24, "top": 20}
3. `read_more` {"result_id": "r8d8018", "fields": "eventTimestamp,operationName.value,caller,status.value"}
4. `azure_monitor_metrics_query` {"resource": "shop-web", "resource-group": "shop-prod-rg", "resource-type": "Microsoft.Web/sites", "metric-namespace": "Microsoft.Web/sites", "metric-names": "Http5xx,Requests", "aggregation": "Total", "interval": "PT5M", "start-time": "2026-10-18T15:00:00Z"}
5. `read_more` {"result_id": "r8d8018", "start": 7800}
