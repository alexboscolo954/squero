## Pod lab/leaky-8555f7bc7c-mtzbj (Deployment leaky) has restarted 35 times. Each time it is OOMKilled (exit 137) at its 256Mi memory limit. The most likely cause is a memory leak: the container has a LEAK_MIB_PER_MIN env var, and nothing in the cluster was rolled out recently.

**Root cause** (medium confidence, `app_error`): Container 'app' (python:3.12-alpine) has a 256Mi memory limit and a 128Mi request. The kernel kills it when it reaches the limit: last state is OOMKilled with exit code 137, and the Hub measured peak memory of 255Mi, right at the limit. The container's env includes PROFILE, MEMORY_MIB and LEAK_MIB_PER_MIN, which suggests memory grows steadily over time rather than the workload simply needing more. It has been OOM-killed in 3 pods so far. No deployment rollouts happened in the last 6h, so a recent change is not the cause. Confidence is medium: I couldn't see the env var values (the tool doesn't return them), and there were no previous-run logs to confirm memory growth.
**Severity:** medium  ·  **Affected:** lab/Deployment/leaky

**Evidence**
- `kubernetes_describe_pod`: Container app: limits memory 256Mi, requests 128Mi; last_state terminated OOMKilled exit_code 137; restarts 35; env names PROFILE, CPU, MEMORY_MIB, LEAK_MIB_PER_MIN
- `sizing_for_workload`: Deployment leaky: memory peak 255Mi against a 256Mi limit; 'OOM-killed in 3 pods: its peak is its limit, not its demand'; suggested memory request and limit 308Mi
- `kubernetes_recent_changes`: No rollouts or changes in the last 360 minutes
- `kubernetes_pod_logs`: Previous-run logs of leaky-8555f7bc7c-mtzbj were empty, so there is no app-level error output

**Recommendations**
1. Check the LEAK_MIB_PER_MIN and MEMORY_MIB values in the leaky Deployment spec. If the leak is configured on purpose (this is a lab namespace), set it to 0 or remove it.
2. Short-term: per the Sizing, raise the memory request and limit to 308Mi. If it is a real leak, this only delays the OOM kills.
3. Fix the leak in the application code, then watch memory over time to confirm it levels off below the limit.

**Tool calls**
1. `kubernetes_describe_pod` {"cluster": "kind", "namespace": "lab", "name": "leaky-8555f7bc7c-mtzbj"}
2. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "lab", "name": "leaky-8555f7bc7c-mtzbj", "previous": true, "tail_lines": 50}
3. `kubernetes_recent_changes` {"cluster": "kind"}
4. `sizing_for_workload` {"cluster": "kind", "namespace": "lab", "name": "leaky-8555f7bc7c-mtzbj"}
