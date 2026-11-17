## Pod lab/leaky-8555f7bc7c-mtzbj has restarted 35 times. Its last exit was OOMKilled (exit 137) at its 256Mi memory limit. Nothing was rolled out recently, and the steady pattern of OOM kills points to a memory leak in the app.

**Root cause** (medium confidence, `app_error`): The 'app' container (python:3.12-alpine) has a 256Mi memory limit, and the Hub's Sizing measured its peak at 255Mi, which is the limit itself. It has been OOM-killed in 3 pods. Its env includes LEAK_MIB_PER_MIN, so memory most likely grows at a steady rate until it hits the limit, the kernel kills it, and it restarts. That means the cause is in the code, not a recent change: the deployment has only revision 1, created 1 day ago. Raising the limit (the Sizing suggests 308Mi) only buys more time between kills. Confidence is medium because the leak is inferred from the env var name and the kill pattern; there were no previous-run logs and I did not see a memory-over-time metric.
**Severity:** medium  ·  **Affected:** lab/Deployment/leaky

**Evidence**
- `kubernetes_describe_pod`: Container app: last_state terminated OOMKilled, exit_code 137, restarts 35, limits memory 256Mi, request 128Mi, env includes LEAK_MIB_PER_MIN
- `kubernetes_describe_deployment`: Only rollout revision 1 (leaky-8555f7bc7c, created 1d ago), so no recent change to blame
- `sizing_for_workload`: Memory peak 255Mi against a 256Mi limit; OOM-killed in 3 pods; suggested memory request and limit 308Mi
- `kubernetes_pod_logs`: The previous (crashed) run left no log output

**Recommendations**
1. Find and fix the memory growth in the app. LEAK_MIB_PER_MIN suggests a deliberate or configured leak, so check its value and set it to 0 or remove it if this is not meant as a test workload.
2. As a stopgap, set the memory request and limit to 308Mi, as the Sizing suggests. Expect this only to make OOM kills less frequent while the leak remains.
3. Track container_memory_working_set_bytes for the pod over time to confirm it climbs linearly before each OOM kill.

**Tool calls**
1. `kubernetes_describe_pod` {"cluster": "kind", "namespace": "lab", "name": "leaky-8555f7bc7c-mtzbj"}
2. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "lab", "name": "leaky-8555f7bc7c-mtzbj", "previous": true, "tail_lines": 50}
3. `kubernetes_describe_deployment` {"cluster": "kind", "namespace": "lab", "name": "leaky"}
4. `sizing_for_workload` {"cluster": "kind", "namespace": "lab", "name": "leaky"}
