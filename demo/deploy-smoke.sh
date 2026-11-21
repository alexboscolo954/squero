#!/usr/bin/env bash
# Run the real image on kind with the shipped manifests: pod starts under the
# hardened securityContext, health and auth behave, a webhook is accepted; and a
# Relay installed from the chart, after its namespace and token Secret, connects (18.1b).
set -euo pipefail
ctx=$1
k() { kubectl --context "$ctx" "$@"; }
# The demo Contracts' synthetic test key (never for a real secret) holds the Relay's token.
agekey=demo/contracts/test-keys/demo-shop.agekey
k create namespace squero --dry-run=client -o yaml | k apply -f -
k apply -f demo/postgres.yaml
k -n squero rollout status deploy/squero-postgres --timeout=3m
k -n squero create secret generic squero-secrets \
  --from-literal=SQUERO_APPROVER_TOKEN=smoke-approver --from-literal=SQUERO_MODEL_API_KEY=not-a-real-key \
  --from-literal=SQUERO_DATABASE_URL=postgresql://postgres:smoke@squero-postgres.squero:5432/postgres \
  --from-file=SOPS_AGE_KEY="$agekey" \
  --dry-run=client -o yaml | k apply -f -
# The shipped Contract has no webhook token: add one, as an Operator would.
new=$(uv run squero contracts new-token 2>/dev/null)
token=$(sed -n 1p <<<"$new" | awk '{print $NF}')
work=$(mktemp -d); cp -R deploy/base "$work/base"
sed -n 2p <<<"$new" | sed 's/^contract.yaml: *//' >> "$work/base/contract.yaml"
# A second cluster reached through a Relay, its token in the Contract's secrets file.
awk '/^integrations:/{print "  - name: relayed\n    via: relay\n    namespaces: [demo]\n    relay_token_secret: relay-token"}1' \
  "$work/base/contract.yaml" >"$work/contract.yaml" && mv "$work/contract.yaml" "$work/base/contract.yaml"
relay_token=$(openssl rand -hex 32)
printf 'relay-token: %s\n' "$relay_token" >"$work/base/secrets.sops.yaml"
sops --encrypt --in-place --age "$(grep -o 'age1[a-z0-9]*' "$agekey")" "$work/base/secrets.sops.yaml"
sed -i.bak 's/files: \[contract.yaml\]/files: [contract.yaml, secrets.sops.yaml]/' "$work/base/kustomization.yaml"
mkdir -p "$work/check/in-cluster"
cp "$work/base/contract.yaml" "$work/base/secrets.sops.yaml" "$work/check/in-cluster/"
SOPS_AGE_KEY_FILE=$agekey uv run squero contracts validate "$work/check"  # fails here, not as a rollout
kubectl kustomize "$work/base" \
  | sed 's#ghcr.io/alexboscolo954/squero:0.61.0#squero:ci#' \
  | k apply -f -
k -n squero rollout status deploy/squero --timeout=3m
# kubectl itself, not the k() wrapper: $! must be kubectl's pid for the trap to stop it.
kubectl --context "$ctx" -n squero port-forward svc/squero 18080:8080 >/dev/null 2>&1 & pf=$!
trap 'kill $pf' EXIT; sleep 3
curl -sf localhost:18080/healthz
url=localhost:18080/contracts/in-cluster/alertmanager
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST $url \
  -H 'Authorization: Bearer wrong' -H 'Content-Type: application/json' -d @examples/alert-crashloop.json)
[[ $code == 401 ]] || { echo "expected 401, got $code"; exit 1; }
curl -sf -X POST $url -H "Authorization: Bearer $token" \
  -H 'Content-Type: application/json' -d @examples/alert-crashloop.json
curl -sf localhost:18080/metrics | grep -q squero_triages_total
# The Relay, as a client installs it: its namespace and token Secret, then one helm install.
k create namespace squero-relay --dry-run=client -o yaml | k apply -f -
k -n squero-relay create secret generic squero-relay --from-literal=SQUERO_RELAY_TOKEN="$relay_token" \
  --dry-run=client -o yaml | k apply -f -
helm upgrade --install squero charts/squero -n squero-relay --kube-context "$ctx" \
  --set relay.hub=ws://squero.squero:8080/relay --set relay.cluster=relayed \
  --set 'namespaces={demo}' --set image.repository=squero --set image.tag=ci
k -n squero-relay rollout status deploy/squero-relay --timeout=3m
# The Hub logs it only once the Relay's MCP session is open; it dials again after 1 s, doubling.
last() { k -n squero logs deploy/squero | grep 'relay in-cluster/relayed ' | tail -1; }
for _ in $(seq 45); do
  [[ $(last) == *connected && $(last) != *disconnected ]] && break
  sleep 2
done
if [[ $(last) != *connected || $(last) == *disconnected ]]; then
  echo "FAIL the Hub doesn't see the Relay connected"; k -n squero-relay logs deploy/squero-relay --tail=50; exit 1
fi
echo "ok   a Relay installed from the chart is connected to the Hub"
helm uninstall squero -n squero-relay --kube-context "$ctx"  # left in place when it fails, for its logs
echo; echo "deploy smoke ok"
