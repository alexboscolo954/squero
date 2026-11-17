## frontend pod frontend-6f6b4986b9-hxdf2 is stuck Pending with ErrImagePull. The image tag nginx:1.99.99-does-not-exist doesn't exist on Docker Hub. This is revision 1 of the deployment, so no older ReplicaSet is serving traffic and frontend has 0/1 ready replicas.

**Root cause** (high confidence, `image_unavailable`): The frontend deployment, created 2m ago as revision 1, sets container 'web' to image nginx:1.99.99-does-not-exist. The registry says it is "not found" (failed to resolve reference docker.io/library/nginx:1.99.99-does-not-exist), so the container can't start. There is no earlier revision to fall back on.
**Severity:** high  ·  **Affected:** demo/Deployment/frontend

**Evidence**
- `kubernetes_list_pods`: frontend-6f6b4986b9-hxdf2 is Pending, 0/1 ready, container web waiting ErrImagePull: 'docker.io/library/nginx:1.99.99-does-not-exist: not found'
- `kubernetes_describe_deployment`: frontend has one revision only (ReplicaSet frontend-6f6b4986b9, created 2m ago) with image nginx:1.99.99-does-not-exist; Available=False, MinimumReplicasUnavailable, 0/1 ready
- `kubernetes_recent_changes`: frontend was created (revision 1) 2m ago with no later rollout, so there is no previous working image to roll back to

**Recommendations**
1. Change the frontend deployment's web container image to a valid nginx tag, e.g. `kubectl -n demo set image deployment/frontend web=nginx:<valid-tag>`, or fix the tag in its source manifest or GitOps repo.
2. Find out why CI or the manifest used a tag that doesn't exist, and add a check that the image exists before deploying.
3. Confirm the new pod pulls the image and becomes Ready, and that the KubePodNotReady alert clears.
4. Separate from this alert: checkout is in a crash loop since its revision 2 rollout 48s ago removed the DATABASE_URL env var, and reports is being OOMKilled. Triage those on their own.

Runbook: `image-pull`

**Tool calls**
1. `kubernetes_list_pods` {"cluster": "kind", "namespace": "demo"}
2. `runbooks_runbook_for_alert` {"alertname": "KubePodNotReady"}
3. `kubernetes_recent_changes` {"cluster": "kind"}
4. `incidents_search` {"keywords": "frontend"}
5. `kubernetes_describe_deployment` {"cluster": "kind", "namespace": "demo", "name": "frontend"}
