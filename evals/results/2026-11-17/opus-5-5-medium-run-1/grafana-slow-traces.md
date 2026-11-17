## Storefront GET /product/{sku} is slow because its call to the inventory service is waiting on a Postgres row lock. In the trace I opened, inventory's `SELECT qty FROM stock WHERE sku = $1 FOR UPDATE` on inventory-db spent 3050ms waiting on a row lock (`Lock:tuple`), which was almost all of the 3103ms request. Only some requests are affected: most finish in about 60ms.

**Root cause** (medium confidence, `dependency_failure`): Row-lock contention in inventory-db (shop namespace). In trace 45d31a48f074c35561c0353b3bdfe298, storefront's GET /product/{sku} took 3103ms. Inside it, the catalog call took 14ms and the pricing call 21ms. The inventory call GET /stock/{sku} took 3056ms, and 3050ms of that was its query `SELECT qty FROM stock WHERE sku = $1 FOR UPDATE` against inventory-db.shop.svc, with db.wait_event=Lock:tuple. A read-only product page takes an exclusive row lock (FOR UPDATE), so it waits whenever another transaction holds the lock on that sku's stock row. Storefront logs show a two-level pattern: most requests take about 55-62ms, while others take 2.5-3.1s and are marked 'slow'. That fits lock waits rather than general load. I could not tell from the data what is holding the locks: Tempo only lists storefront, catalog, pricing and inventory, and Loki has no inventory or DB logs. I also could not check for a recent deploy, so a rollout of inventory is not ruled out.
**Severity:** medium  ·  **Affected:** shop/Deployment/storefront, shop/Deployment/inventory

**Evidence**
- `grafana_search_tempo_traces`: Searching `{resource.service.name="storefront" && duration > 2s}` found many GET /product/{sku} traces of 2.5-3.3s, each with 2 inventory spans and 1 span each from catalog and pricing. (http://localhost:3000/explore?panes=%7B%22a%22%3A%7B%22datasource%22%3A%22tempo%22%2C%22queries%22%3A%5B%7B%22datasource%22%3A%7B%22uid%22%3A%22tempo%22%7D%2C%22query%22%3A%22%7Bresource.service.name%3D%5C%22storefront%5C%22+%5Cu0026%5Cu0026+duration+%5Cu003e+2s%7D%22%2C%22queryType%22%3A%22traceql%22%2C%22refId%22%3A%22A%22%7D%5D%2C%22range%22%3A%7B%22from%22%3A%22now-1h%22%7D%7D%7D&schemaVersion=1)
- `grafana_get_tempo_trace`: Trace 45d31a48f074c35561c0353b3bdfe298: storefront 3103ms; catalog 14ms; pricing 21ms; inventory GET /stock/{sku} 3056ms; its child span 'SELECT stock' took 3050ms with db.query.text='SELECT qty FROM stock WHERE sku = $1 FOR UPDATE', db.wait_event=Lock:tuple, server.address=inventory-db.shop.svc.
- `grafana_query_loki_logs`: Storefront log lines are either fast or slow, e.g. 'GET /product/sku-1046 200 57ms ok' next to 'GET /product/sku-1077 200 3131ms slow' and 'GET /product/sku-1010 200 2606ms slow'. All return 200, so there are no errors, only latency. (http://localhost:3000/explore?panes=%7B%22a%22%3A%7B%22datasource%22%3A%22loki%22%2C%22queries%22%3A%5B%7B%22datasource%22%3A%7B%22uid%22%3A%22loki%22%7D%2C%22expr%22%3A%22%7Bservice_name%3D%5C%22storefront%5C%22%7D%22%2C%22refId%22%3A%22A%22%7D%5D%2C%22range%22%3A%7B%22from%22%3A%22now-1h%22%7D%7D%7D&schemaVersion=1)
- `grafana_query_loki_patterns`: Loki has only one pattern for storefront, 'GET <_> 200 <_> <_>' (346 lines), and no error patterns.

**Recommendations**
1. In inventory-db, find the transactions holding the locks: query pg_stat_activity together with pg_locks (or pg_blocking_pids) and look for long-running or 'idle in transaction' sessions on the stock table.
2. If a stuck or idle-in-transaction session is the blocker, end it with pg_terminate_backend, after checking with the inventory or DB owner.
3. Check whether inventory or anything else that writes to the stock table was deployed recently, or whether a batch or reconciliation job is running. Roll it back or pause it if it lines up with when the latency started.
4. Long-term fix: remove FOR UPDATE from the read-only stock lookup in inventory's GET /stock/{sku} (use a plain SELECT), or use NOWAIT/SKIP LOCKED with a short lock_timeout. Also set idle_in_transaction_session_timeout on inventory-db.
5. Add DB lock-wait metrics or dashboards for inventory-db, and add inventory logs to Loki, so the blocking session can be seen next time.

**Tool calls**
1. `grafana_list_datasources` {}
2. `grafana_search_tempo_traces` {"datasourceUid": "tempo", "query": "{resource.service.name=\"storefront\" && duration > 2s}"}
3. `grafana_get_tempo_trace` {"datasourceUid": "tempo", "trace_id": "45d31a48f074c35561c0353b3bdfe298"}
4. `grafana_query_loki_patterns` {"datasourceUid": "loki", "logql": "{service_name=\"inventory\"}"}
5. `grafana_list_loki_label_values` {"datasourceUid": "loki", "labelName": "service_name"}
6. `grafana_query_loki_logs` {"datasourceUid": "loki", "logql": "{service_name=\"inventory\"}", "limit": 5}
7. `grafana_search_tempo_traces` {"datasourceUid": "tempo", "query": "{span.server.address=\"inventory-db.shop.svc\" && duration > 1s && resource.service.name!=\"inventory\"}"}
8. `grafana_list_tempo_attribute_values` {"datasourceUid": "tempo", "name": "resource.service.name"}
9. `grafana_query_loki_patterns` {"datasourceUid": "loki", "logql": "{service_name=\"payments\"}"}
10. `grafana_query_loki_patterns` {"datasourceUid": "loki", "logql": "{service_name=\"storefront\"}"}
11. `grafana_generate_deeplink` {"resourceType": "explore", "datasourceUid": "tempo", "queries": [{"refId": "A", "queryType": "traceId", "query": "45d31a48f074c35561c0353b3bdfe298"}], "timeRange": {"from": "now-1h", "to": "now"}}
12. `grafana_generate_deeplink` {"resourceType": "explore", "datasourceUid": "loki", "queries": [{"refId": "A", "expr": "{service_name=\"storefront\"} |= \"slow\""}], "timeRange": {"from": "now-1h", "to": "now"}}
