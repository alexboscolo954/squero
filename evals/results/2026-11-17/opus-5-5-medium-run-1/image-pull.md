## The frontend pod frontend-6f6b4986b9-hxdf2 can't start because its image tag nginx:1.99.99-does-not-exist doesn't exist on Docker Hub (ErrImagePull: not found). This is revision 1 of the deployment, so no older ReplicaSet is serving traffic and frontend has 0 of 1 replicas ready.

**Root cause** (high confidence, `image_unavailable`): Deployment demo/frontend was created about 2 minutes ago at revision 1 with image nginx:1.99.99-does-not-exist. The registry says the reference is missing: "docker.io/library/nginx:1.99.99-does-not-exist: not found". The tag is wrong or was never pushed, so the container stays in ErrImagePull and the pod never becomes Ready. There is no earlier revision to fall back to, so the frontend is completely down.
**Severity:** high  ·  **Affected:** demo/Deployment/frontend

**Evidence**
- `kubernetes_list_pods`: frontend-6f6b4986b9-hxdf2 is Pending, 0/1 ready, with container web waiting in ErrImagePull: 'failed to resolve reference "docker.io/library/nginx:1.99.99-does-not-exist": ... not found'
- `kubernetes_describe_deployment`: frontend has one revision only (ReplicaSet frontend-6f6b4986b9, created 2m ago) with image nginx:1.99.99-does-not-exist. Available=False (MinimumReplicasUnavailable), 0 of 1 replicas ready.
- `kubernetes_recent_changes`: frontend was created at revision 1 about 2m ago, with no later rollouts. checkout (DATABASE_URL removed in revision 2) and reports (OOMKilled) are failing in the same namespace for separate reasons.

**Recommendations**
1. Change the frontend image to a tag that exists, such as a real released nginx version. Check that the tag is in the registry first.
2. Apply the fix (for example kubectl -n demo set image deployment/frontend web=nginx:<valid-tag>), then confirm the new pod pulls its image and becomes Ready.
3. Find out why the deployment was created with a tag that doesn't exist (manifest typo or a CI step that didn't push), and add a check that the image exists before deploying.
4. Triage the unrelated failures in demo separately: checkout is crash looping after DATABASE_URL was removed in revision 2, and reports is being OOMKilled.

Runbook: `image-pull`

**Tool calls**
1. `runbooks_runbook_for_alert` {"alertname": "KubePodNotReady"}
2. `kubernetes_list_pods` {"cluster": "kind", "namespace": "demo"}
3. `kubernetes_recent_changes` {"cluster": "kind"}
4. `incidents_search` {"keywords": "frontend"}
5. `kubernetes_describe_deployment` {"cluster": "kind", "namespace": "demo", "name": "frontend"}
