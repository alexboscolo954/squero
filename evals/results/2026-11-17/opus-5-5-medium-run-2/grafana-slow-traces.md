## Storefront GET /product/{sku} is slow (2.5–3.3s) because the inventory service is slow. Its `SELECT qty FROM stock WHERE sku = $1 FOR UPDATE` against inventory-db.shop.svc waits on a Postgres row lock (db.wait_event=Lock:tuple) for about 2.5–3s. Catalog and pricing calls finish in 14–21ms. I couldn't see what is holding the lock.

**Root cause** (medium confidence, `dependency_failure`): Row-lock contention in the inventory Postgres database (inventory-db.shop.svc). In the slow traces, nearly all the time goes to inventory's 'SELECT stock' span: 3050ms of a 3103ms request in trace 45d31a48…, and 2470ms of a 2523ms request in trace aa889c27…. That span runs `SELECT qty FROM stock WHERE sku = $1 FOR UPDATE` with db.wait_event=Lock:tuple, so each product-page read waits for another transaction to release the row lock on the stock row. The other downstream calls are fast (catalog 14ms, pricing 21ms). Two things are still unknown: which transaction holds the locks (a long-running transaction, batch job, or hot-SKU writer), and why a read path takes FOR UPDATE at all. Tempo shows no other spans against inventory-db, and the inventory logs show only 'GET <_> 200' lines. I checked no metrics, deployments or rollout history, so a recent change is not ruled out.
**Severity:** medium  ·  **Affected:** shop/Deployment/storefront, shop/Deployment/inventory

**Evidence**
- `grafana_search_tempo_traces`: Searching {resource.service.name="storefront" && duration > 2s} returns many GET /product/{sku} traces at 2483–3284ms. Each one spans storefront, catalog, pricing and inventory (inventory has 2 spans). (http://localhost:3000/explore?panes=%7B%22a%22%3A%7B%22datasource%22%3A%22tempo%22%2C%22queries%22%3A%5B%7B%22datasource%22%3A%7B%22uid%22%3A%22tempo%22%7D%2C%22query%22%3A%22%7Bresource.service.name%3D%5C%22storefront%5C%22+%5Cu0026%5Cu0026+duration+%5Cu003e+2s%7D%22%2C%22queryType%22%3A%22traceql%22%2C%22refId%22%3A%22A%22%7D%5D%2C%22range%22%3A%7B%22from%22%3A%22now-1h%22%7D%7D%7D&schemaVersion=1)
- `grafana_get_tempo_trace`: Trace 45d31a48f074c35561c0353b3bdfe298: storefront 3103ms. Catalog GET /items 14ms, pricing GET /quote 21ms, inventory GET /stock 3056ms. The child span 'SELECT stock' (`SELECT qty FROM stock WHERE sku = $1 FOR UPDATE`, db.wait_event=Lock:tuple, server.address=inventory-db.shop.svc) takes 3050ms.
- `grafana_get_tempo_trace`: Trace aa889c27b4277ffe35150f32673a376f has the same shape: storefront 2523ms, inventory SELECT stock FOR UPDATE 2470ms waiting on Lock:tuple, catalog 14ms, pricing 21ms.
- `grafana_query_loki_patterns`: The only log pattern for {service_name="inventory"} is 'GET <_> 200 <_> <_>' (346 lines). No errors, so the requests are slow but succeed.

**Recommendations**
1. On inventory-db.shop.svc, find what holds the stock-row locks: query pg_stat_activity and pg_locks (or pg_blocking_pids) for long-running or idle-in-transaction sessions. If one is clearly stuck, consider pg_terminate_backend after confirming what it is (requires approval).
2. Check for a recent deploy of inventory, or a batch or migration job writing to the stock table, that started around when the alert began.
3. Change the read path GET /stock/{sku} to stop using SELECT ... FOR UPDATE (use a plain SELECT, or FOR UPDATE SKIP LOCKED/NOWAIT if a lock is truly needed). Also set lock_timeout or statement_timeout on the inventory connection so product pages fail fast instead of blocking.
4. Consider a short timeout plus a fallback (for example 'stock unknown' or a cached value) in storefront's inventory call so page latency doesn't depend on DB lock waits.
5. Confirm recovery: p99 of GET /product/{sku} back under 2s and SELECT stock spans no longer showing Lock:tuple.

**Tool calls**
1. `grafana_list_datasources` {}
2. `grafana_search_tempo_traces` {"datasourceUid": "tempo", "query": "{resource.service.name=\"storefront\" && duration > 2s}"}
3. `grafana_get_tempo_trace` {"datasourceUid": "tempo", "trace_id": "45d31a48f074c35561c0353b3bdfe298"}
4. `grafana_query_loki_patterns` {"datasourceUid": "loki", "logql": "{service_name=\"inventory\"}"}
5. `grafana_search_tempo_traces` {"datasourceUid": "tempo", "query": "{resource.service.name=\"inventory\" && span.db.system=\"postgresql\" && duration > 1s}"}
6. `grafana_search_tempo_traces` {"datasourceUid": "tempo", "query": "{span.server.address=\"inventory-db.shop.svc\" && name!=\"SELECT stock\"}"}
7. `grafana_get_tempo_trace` {"datasourceUid": "tempo", "trace_id": "aa889c27b4277ffe35150f32673a376f"}
8. `grafana_generate_deeplink` {"resourceType": "explore", "datasourceUid": "tempo", "queries": [{"refId": "A", "queryType": "traceId", "query": "45d31a48f074c35561c0353b3bdfe298"}], "timeRange": {"from": "now-1h", "to": "now"}}
9. `grafana_generate_deeplink` {"resourceType": "explore", "datasourceUid": "tempo", "queries": [{"refId": "A", "queryType": "traceql", "query": "{resource.service.name=\"storefront\" && duration > 2s}"}], "timeRange": {"from": "now-1h", "to": "now"}}
