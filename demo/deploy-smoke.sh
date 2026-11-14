#!/usr/bin/env bash
# Run the real image on kind with the shipped manifests: pod starts under the
# hardened securityContext, health and auth behave, a webhook is accepted.
set -euo pipefail
ctx=$1
k() { kubectl --context "$ctx" "$@"; }
k create namespace squero --dry-run=client -o yaml | k apply -f -
k apply -f demo/postgres.yaml
k -n squero rollout status deploy/squero-postgres --timeout=3m
k -n squero create secret generic squero-secrets \
  --from-literal=SQUERO_APPROVER_TOKEN=smoke-approver --from-literal=SQUERO_MODEL_API_KEY=not-a-real-key \
  --from-literal=SQUERO_DATABASE_URL=postgresql://postgres:smoke@squero-postgres.squero:5432/postgres \
  --dry-run=client -o yaml | k apply -f -
# The shipped Contract has no webhook token: add one, as an Operator would.
new=$(uv run squero contracts new-token 2>/dev/null)
token=$(sed -n 1p <<<"$new" | awk '{print $NF}')
work=$(mktemp -d); cp -R deploy/base "$work/base"
sed -n 2p <<<"$new" | sed 's/^contract.yaml: *//' >> "$work/base/contract.yaml"
kubectl kustomize "$work/base" \
  | sed 's#ghcr.io/alexboscolo954/squero:0.50.0#squero:ci#' \
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
echo; echo "deploy smoke ok"
