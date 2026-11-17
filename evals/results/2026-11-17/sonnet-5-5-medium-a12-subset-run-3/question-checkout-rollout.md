The latest rollout of `checkout` is revision 2 (ReplicaSet `checkout-869fc5bcc6`). It removed the `DATABASE_URL` env var from the `app` container. Revision 1 had that variable. The image is `python:3.12-alpine` in both revisions, so it did not change.

The removal is breaking the service. The new pod `checkout-869fc5bcc6-db5ps` is crash-looping: 3 restarts, exit code 1, 0/1 ready. Its log says `FATAL: required env var DATABASE_URL is not set`. The deployment shows 0 of 1 replicas ready and `Available=False`.

**Recommended fix:** restore `DATABASE_URL` on the deployment, or roll back to revision 1.

**Evidence**
- `kubernetes_recent_changes`: demo/checkout revision 2 rollout 48s ago; diff: env_removed app: [DATABASE_URL]
- `kubernetes_describe_deployment`: Revision 2 has env [] and revision 1 has env [DATABASE_URL]. Both use the image python:3.12-alpine. Ready replicas 0/1; Available=False (MinimumReplicasUnavailable).
- `kubernetes_list_pods`: checkout-869fc5bcc6-db5ps is 0/1 ready with 3 restarts. The container terminated with Error, exit code 1.
- `kubernetes_pod_logs`: FATAL: required env var DATABASE_URL is not set

**Tool calls**
1. `kubernetes_describe_deployment` {"cluster": "kind", "namespace": "demo", "name": "checkout"}
2. `kubernetes_recent_changes` {"cluster": "kind"}
3. `kubernetes_list_pods` {"cluster": "kind", "namespace": "demo", "label_selector": "app=checkout"}
4. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "demo", "name": "checkout-869fc5bcc6-db5ps", "tail_lines": 20}
5. `read_more` {"result_id": "none"}
6. `read_more` {"result_id": "none"}
7. `read_more` {"result_id": "none"}
8. `read_more` {"result_id": "none"}
9. `read_more` {"result_id": "none"}
10. `read_more` {"result_id": "none"}
11. `read_more` {"result_id": "none"}
12. `read_more` {"result_id": "none"}
13. `read_more` {"result_id": "none"}
14. `read_more` {"result_id": "none"}
15. `read_more` {"result_id": "none"}
16. `read_more` {"result_id": "none"}
17. `read_more` {"result_id": "none"}
18. `read_more` {"result_id": "none"}
19. `read_more` {"result_id": "none"}
20. `read_more` {"result_id": "none"}
21. `read_more` {"result_id": "none"}
22. `read_more` {"result_id": "none"}
23. `read_more` {"result_id": "none"}
24. `read_more` {"result_id": "none"}
25. `read_more` {"result_id": "none"}
26. `read_more` {"result_id": "none"} (error)
