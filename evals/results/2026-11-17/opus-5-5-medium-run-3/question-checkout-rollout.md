The latest rollout of `checkout` (namespace `demo`) is **revision 2**, ReplicaSet `checkout-869fc5bcc6`, created about 36s before I checked. It replaced revision 1 (`checkout-67d9799c78`, about 2m old).

**What changed:** the `app` container no longer has the env var **`DATABASE_URL`**. Revision 1 set it; revision 2 has no env vars at all. The image stayed the same (`python:3.12-alpine`), and the rollout history shows no other change between the two revisions.

**Current state:** the deployment has 0 of 1 replicas ready, and its `Available` condition is False (`MinimumReplicasUnavailable`). Revision 2's ReplicaSet has 1 replica and revision 1's has been scaled to 0, so no copy of checkout is serving right now. I didn't check the pod logs, so I haven't confirmed that the missing `DATABASE_URL` is why the pod isn't ready. It is the only change, though, and an app that needs a database URL would likely fail without it.

**Suggested next step:** check the checkout pod's logs to confirm. If it is the cause, put `DATABASE_URL` back, or roll back to revision 1 (`kubectl rollout undo deployment/checkout -n demo`).

**Evidence**
- `kubernetes_describe_deployment`: checkout rollout_history: rev 2 (checkout-869fc5bcc6, 36s, image python:3.12-alpine, env: []) vs rev 1 (checkout-67d9799c78, 2m, image python:3.12-alpine, env: [DATABASE_URL]); replicas desired 1, ready 0; Available=False MinimumReplicasUnavailable

**Tool calls**
1. `kubernetes_describe_deployment` {"cluster": "kind", "namespace": "demo", "name": "checkout"}
