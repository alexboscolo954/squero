All three deployments in `demo` are unhealthy. Each one has 0 of 1 replicas ready and the condition `MinimumReplicasUnavailable`.

1. **checkout: crash loop because of a missing env var.** The pod `checkout-869fc5bcc6-db5ps` exits with code 1 and has restarted 3 times. Its log says `FATAL: required env var DATABASE_URL is not set`. Revision 2 of the deployment (ReplicaSet `checkout-869fc5bcc6`, rolled out about 36s before I checked) dropped `DATABASE_URL`. Revision 1 had it; revision 2 has no env vars at all. **Fix:** put `DATABASE_URL` back, or roll back to revision 1.

2. **frontend: the image doesn't exist.** The pod `frontend-6f6b4986b9-hxdf2` is stuck Pending with `ErrImagePull`. The tag `nginx:1.99.99-does-not-exist` returns `not found` from docker.io. **Fix:** point the deployment at a valid nginx tag.

3. **reports: the container runs out of memory.** The pod `reports-55b4fffc66-hmq7f` keeps getting OOMKilled (exit code 137, 3 restarts, in back-off). Its memory limit is only 48Mi, with a 32Mi request. **Fix:** raise the memory limit, or find out why the app needs more than 48Mi.

**Evidence**
- `kubernetes_list_deployments`: checkout, frontend and reports each show 0/1 ready with Available=False, MinimumReplicasUnavailable
- `kubernetes_list_pods`: checkout pod: Error, exit code 1, 3 restarts. frontend pod: Pending with ErrImagePull, 'nginx:1.99.99-does-not-exist: not found'. reports pod: OOMKilled, exit code 137, 3 restarts
- `kubernetes_pod_logs`: checkout-869fc5bcc6-db5ps log: 'FATAL: required env var DATABASE_URL is not set'
- `kubernetes_describe_deployment`: checkout revision 2 (created 36s ago) has env: [], while revision 1 had DATABASE_URL
- `kubernetes_describe_pod`: reports-55b4fffc66-hmq7f: memory limit 48Mi, request 32Mi; container state OOMKilled, BackOff restarting

**Tool calls**
1. `kubernetes_list_pods` {"cluster": "kind", "namespace": "demo"}
2. `kubernetes_list_deployments` {"cluster": "kind", "namespace": "demo"}
3. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "demo", "name": "checkout-869fc5bcc6-db5ps", "tail_lines": 50}
4. `kubernetes_describe_pod` {"cluster": "kind", "namespace": "demo", "name": "reports-55b4fffc66-hmq7f"}
5. `kubernetes_describe_deployment` {"cluster": "kind", "namespace": "demo", "name": "checkout"}
