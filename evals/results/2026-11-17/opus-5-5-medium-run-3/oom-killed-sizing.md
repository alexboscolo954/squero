## Pod lab/leaky-8555f7bc7c-mtzbj has restarted 35 times. Each time it is OOMKilled (exit 137) when memory reaches its 256Mi limit. The container sets a LEAK_MIB_PER_MIN env var and nothing has been rolled out since the deployment was created a day ago, so this looks like a memory leak rather than a limit that is simply too low.

**Root cause** (medium confidence, `app_error`): The 'app' container (python:3.12-alpine) is OOMKilled again and again (last state OOMKilled, exit code 137, 35 restarts) at its 256Mi memory limit. The Hub measured peak memory at 255Mi, which is the limit itself, so the container's real demand is above what the limit allows. The deployment has only revision 1 (1 day old), so no rollout caused this. The container's env includes LEAK_MIB_PER_MIN, which suggests a configured, steady memory leak. I'm inferring that from the variable's name only: env values aren't returned and the previous container's logs were empty. A leak would keep growing past any limit, so raising the limit only spaces out the restarts.
**Severity:** medium  ·  **Affected:** lab/Deployment/leaky

**Evidence**
- `kubernetes_describe_pod`: Container app: restarts 35, last_state terminated OOMKilled exit_code 137 27m ago; limits memory 256Mi, requests 128Mi; env includes LEAK_MIB_PER_MIN
- `kubernetes_describe_deployment`: Only revision 1 (ReplicaSet leaky-8555f7bc7c, created 1d ago); no recent rollout
- `sizing_for_workload`: Memory peak 255Mi at 256Mi limit; 'OOM-killed in 3 pods: its peak is its limit, not its demand'; suggested memory request/limit 308Mi/308Mi
- `kubernetes_pod_logs`: Previous container logs returned no output, so there is no crash message from the app itself

**Recommendations**
1. Find and fix the memory growth in the app. Check what LEAK_MIB_PER_MIN is set to (it looks like a deliberate leak setting) and set it to 0 or remove it if leaking isn't intended.
2. As a stopgap, apply the Hub Sizing suggestion: set memory request and limit to 308Mi. Expect this only to delay OOM kills if memory keeps growing.
3. Watch the container's memory over time after the change. If it keeps rising linearly it is a leak; if it levels off below 308Mi, the old limit was just too low.

**Tool calls**
1. `kubernetes_describe_pod` {"cluster": "kind", "namespace": "lab", "name": "leaky-8555f7bc7c-mtzbj"}
2. `kubernetes_pod_logs` {"cluster": "kind", "namespace": "lab", "name": "leaky-8555f7bc7c-mtzbj", "previous": true, "tail_lines": 50}
3. `kubernetes_describe_deployment` {"cluster": "kind", "namespace": "lab", "name": "leaky"}
4. `sizing_for_workload` {"cluster": "kind", "namespace": "lab", "name": "leaky-8555f7bc7c-mtzbj"}
