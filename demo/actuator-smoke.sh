#!/usr/bin/env bash
# The Actuator's real image and shipped chart on kind (slice 12.2; Phase 18.1a: installed
# with Helm, its namespace and token Secret created first, as a client does): it starts,
# puts its certificate in its own webhook, and a new pod of a Deployment it holds is
# created with the held requests; a `helm upgrade` leaves that certificate in place; scaled
# to zero, the webhook fails open and pods start from their template. No Hub: it pulls in
# vain and keeps what its ConfigMap holds. Cleans up after.
set -euo pipefail
ctx=$1
k() { kubectl --context "$ctx" "$@"; }
release() {
  helm --kube-context "$ctx" upgrade --install squero charts/squero -n squero-actuator \
    --set relay.enabled=false --set actuator.enabled=true \
    --set actuator.hub=https://hub.example.com --set 'namespaces={orders}' \
    --set image.repository=squero --set image.tag=ci >/dev/null
}
trap 'helm --kube-context "$ctx" uninstall squero -n squero-actuator >/dev/null 2>&1 || true
  k delete namespace squero-actuator --ignore-not-found --wait=false >/dev/null' EXIT
k create namespace squero-actuator --dry-run=client -o yaml | k apply -f - >/dev/null
k -n squero-actuator create secret generic squero-actuator \
  --from-literal=SQUERO_ACTUATOR_TOKEN=smoke-token-not-the-hubs-0123456789 \
  --dry-run=client -o yaml | k apply -f - >/dev/null
# What it would hold had it applied an Approved Sizing to inventory (template: cpu 10m).
held='{"orders/Deployment/inventory/app": {"run_id": 1, "outcome": "applied", "at": 0,
  "requests": {"cpu": "37m"}, "template": {"cpu": "10m"}}}'
k -n squero-actuator create configmap squero-actuator --from-literal=applied.json="$held" \
  --dry-run=client -o yaml | k apply -f - >/dev/null
release
k -n squero-actuator rollout status deploy/squero-actuator --timeout=3m
for _ in $(seq 60); do
  ca=$(k get mutatingwebhookconfiguration squero-actuator -o jsonpath='{.webhooks[0].clientConfig.caBundle}')
  ready=$(k -n squero-actuator get endpoints squero-actuator -o jsonpath='{.subsets[0].addresses[0].ip}')
  [[ -n $ca && -n $ready ]] && break
  sleep 2
done
[[ -n $ca ]] || { echo "the Actuator never put its certificate in its webhook"; exit 1; }
release  # an upgrade that changes nothing of the pod: the certificate must stay
after=$(k get mutatingwebhookconfiguration squero-actuator -o jsonpath='{.webhooks[0].clientConfig.caBundle}')
[[ $after == "$ca" ]] || { echo "helm upgrade cleared the webhook's certificate: it would fail open"; exit 1; }
echo "ok   a helm upgrade keeps the Actuator's certificate in its webhook"

replaced_cpu() {  # inventory's pod replaced: the cpu its replacement was created with
  old=$(k -n orders get pods -l app=inventory -o jsonpath='{.items[0].metadata.name}')
  k -n orders delete pod "$old" --wait=true >/dev/null
  k -n orders rollout status deploy/inventory --timeout=2m >/dev/null
  k -n orders get pods -l app=inventory -o jsonpath='{.items[0].spec.containers[0].resources.requests.cpu}'
}
cpu=$(replaced_cpu)
[[ $cpu == 37m ]] || { echo "a new pod got cpu $cpu, not the held 37m"; k -n squero-actuator logs deploy/squero-actuator --tail=30; exit 1; }
echo "ok   a new pod of a held Deployment is created with the held requests"
k -n squero-actuator scale deploy/squero-actuator --replicas=0
k -n squero-actuator wait --for=delete pod -l app=squero-actuator --timeout=2m
cpu=$(replaced_cpu)
[[ $cpu == 10m ]] || { echo "with the Actuator at zero a new pod got cpu $cpu, not its template's 10m"; exit 1; }
echo "ok   with the Actuator scaled to zero, pods start from their template"
