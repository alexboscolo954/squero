## The frontend pod frontend-6f6b4986b9-hxdf2 in demo never became ready because its image tag nginx:1.99.99-does-not-exist does not exist on Docker Hub. The pod is stuck in ErrImagePull / ImagePullBackOff.

**Root cause** (high confidence, `image_unavailable`): The frontend deployment was created 2 minutes ago as revision 1 with the image nginx:1.99.99-does-not-exist. The registry returns 'docker.io/library/nginx:1.99.99-does-not-exist: not found' (NotFound), so the 'web' container can never start. This is revision 1, so no earlier working revision exists to fall back to, and frontend may have no ready replicas at all; I did not list the deployment to confirm that. A FailedScheduling event about an untolerated node taint appeared before the pod was scheduled, but the pod did get scheduled to squero-control-plane, so that is not the blocker. The checkout rollout 48s ago removed DATABASE_URL, which is unrelated to this frontend image failure.
**Severity:** medium  ·  **Affected:** demo/Deployment/frontend

**Evidence**
- `kubernetes_describe_pod`: Pod frontend-6f6b4986b9-hxdf2 is Pending, Ready=False, and container 'web' is waiting with ErrImagePull: failed to resolve reference docker.io/library/nginx:1.99.99-does-not-exist: not found. Events show repeated Failed/BackOff while pulling the image.
- `kubernetes_recent_changes`: Deployment frontend revision 1 was created 2m ago and has no earlier revision. The only other recent rollout is checkout rev 2 (DATABASE_URL removed), which does not involve frontend.
- `runbooks_runbook_for_alert`: The image-pull runbook says 'manifest unknown / not found' means the tag does not exist (a typo, or CI never pushed it), and the fix is to correct the tag or roll back.

**Recommendations**
1. Set the frontend deployment's 'web' container image to a valid tag (for example a real nginx release such as nginx:1.27) in its source manifest, then apply it: kubectl -n demo set image deployment/frontend web=nginx:<valid-tag>.
2. Check that the new pod pulls the image and becomes Ready, then confirm the KubePodNotReady alert resolves.
3. Rollback is not possible because frontend has only revision 1. Fix the tag at its source (CI or the manifest) so a redeploy does not bring the bad image back.
4. Separately, look into the checkout rollout 48s ago that removed the DATABASE_URL env var; it may cause its own alerts.

Runbook: `image-pull`

**Tool calls**
1. `runbooks_runbook_for_alert` {"alertname": "KubePodNotReady"}
2. `kubernetes_describe_pod` {"cluster": "kind", "namespace": "demo", "name": "frontend-6f6b4986b9-hxdf2"}
3. `kubernetes_recent_changes` {"cluster": "kind"}
4. `incidents_search` {"keywords": "frontend"}
