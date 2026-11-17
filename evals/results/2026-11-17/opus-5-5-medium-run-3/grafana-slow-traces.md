## GET /product/{sku} on storefront is slow (2.5–3.3s) because its call to inventory GET /stock/{sku} waits about 3s on a Postgres row lock. The query is `SELECT ... FOR UPDATE` against inventory-db, with wait_event Lock:tuple. Storefront itself and its other dependencies, catalog and pricing, respond in milliseconds.

**Root cause** (medium confidence, `dependency_failure`): In the slowest trace I opened (45d31a48…, 3103ms total), catalog GET /items/{sku} took 14ms and pricing GET /quote took 21ms. Inventory GET /stock/{sku} took 3056ms, and 3050ms of that was the span `SELECT qty FROM stock WHERE sku = $1 FOR UPDATE` on postgresql inventory-db.shop.svc, with db.wait_event=Lock:tuple. So nearly all the latency is the inventory query waiting for a row lock on the stock table. Another transaction is holding locks on those stock rows. It could be long-running or idle-in-transaction, or there could be heavy contention on hot SKUs. A read path (product page) also takes a FOR UPDATE lock it probably doesn't need. I could not identify the transaction holding the lock: Loki has no inventory or database logs, and I found no rollout data. That limits confidence.
**Severity:** medium  ·  **Affected:** shop/Deployment/storefront, shop/Deployment/inventory

**Evidence**
- `grafana_search_tempo_traces`: Many storefront 'GET /product/{sku}' traces in the last hour took 2483–3284ms. Each one involves catalog (1 span), pricing (1 span) and inventory (2 spans). (http://localhost:3000/explore?panes=%7B%22a%22%3A%7B%22datasource%22%3A%22tempo%22%2C%22queries%22%3A%5B%7B%22datasource%22%3A%7B%22uid%22%3A%22tempo%22%7D%2C%22query%22%3A%22%7Bresource.service.name%3D%5C%22storefront%5C%22+%5Cu0026%5Cu0026+duration+%5Cu003e+2s%7D%22%2C%22queryType%22%3A%22traceql%22%2C%22refId%22%3A%22A%22%7D%5D%2C%22range%22%3A%7B%22from%22%3A%22now-1h%22%7D%7D%7D&schemaVersion=1)
- `grafana_get_tempo_trace`: In trace 45d31a48f074c35561c0353b3bdfe298 (3103ms total): catalog took 14ms and pricing 21ms. Inventory GET /stock/{sku} took 3056ms, of which the child span 'SELECT qty FROM stock WHERE sku = $1 FOR UPDATE' took 3050ms with db.wait_event=Lock:tuple on inventory-db.shop.svc.
- `grafana_query_loki_patterns`: The only log pattern for service_name=inventory is 'GET <_> 200 <_> <_>' (346 occurrences). There are no inventory or database errors to show what is holding the lock.

**Recommendations**
1. On inventory-db, check pg_stat_activity and pg_locks (or pg_blocking_pids) for the sessions holding locks on the stock table, especially long-running or 'idle in transaction' sessions.
2. If one stuck session is blocking the rest, have a DBA terminate it with pg_terminate_backend after confirming it is safe. Consider setting idle_in_transaction_session_timeout and lock_timeout on the inventory role.
3. Check whether a recent inventory or batch job change (for example a stock sync or reservation job) started holding long transactions on stock rows, and roll it back if so.
4. Change the inventory GET /stock/{sku} read path to a plain SELECT without FOR UPDATE (or use SKIP LOCKED/NOWAIT) so product page views don't queue behind writers.
5. Confirm the storefront p99 drops below 2s once the lock contention is cleared.

**Tool calls**
1. `grafana_list_datasources` {}
2. `grafana_search_tempo_traces` {"datasourceUid": "tempo", "query": "{resource.service.name=\"storefront\" && duration > 2s}"}
3. `grafana_get_tempo_trace` {"datasourceUid": "tempo", "trace_id": "45d31a48f074c35561c0353b3bdfe298"}
4. `grafana_query_loki_patterns` {"datasourceUid": "loki", "logql": "{service_name=\"inventory\"}"}
5. `grafana_list_loki_label_values` {"datasourceUid": "loki", "labelName": "service_name"}
6. `grafana_search_tempo_traces` {"datasourceUid": "tempo", "query": "{resource.service.name=\"inventory\" && span.db.wait_event != \"\"}"}
7. `grafana_generate_deeplink` {"resourceType": "explore", "datasourceUid": "tempo", "queries": [{"refId": "A", "queryType": "traceId", "query": "45d31a48f074c35561c0353b3bdfe298"}]}
8. `grafana_generate_deeplink` {"resourceType": "explore", "datasourceUid": "tempo", "queries": [{"refId": "A", "queryType": "traceql", "query": "{resource.service.name=\"storefront\" && duration > 2s}"}]}
