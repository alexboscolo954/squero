#!/usr/bin/env bash
# The Hub's compose stack (deploy/hub) on this machine, with the demo Contracts:
# - comes up healthy, and both Contracts decrypt inside the image;
# - only the alert route and the Relays' websocket are public: everything else
#   through Caddy is a 404,
#   the console included, even with a forged Tailscale identity header;
# - the console renders on 127.0.0.1 for the Operator's identity plus the token cookie;
# - an alert is stored and fails as expected (no cluster, no model key in CI);
# - a backup restores, and a bad contracts update never restarts the Hub;
# - each pinned vendor in the image still offers the tools its snapshot has (gcp,
#   remote and unpinned: a warning only).
# Usage: demo/hub-smoke.sh [image]
set -euo pipefail
image=${1:-squero:ci}
root=$(cd "$(dirname "$0")/.." && pwd)
work=$(mktemp -d)
export COMPOSE_PROJECT_NAME=squero-smoke
dc() { (cd "$work" && docker compose "$@"); }
trap 'dc down -v --remove-orphans >/dev/null 2>&1 || true' EXIT
fail() { echo "FAIL: $*" >&2; dc logs hub --tail 50 >&2 || true; exit 1; }
git_() { git -c user.name=smoke -c user.email=smoke@localhost "$@"; }

cp "$root"/deploy/hub/{compose.yaml,Caddyfile,backup.sh,update-contracts.sh} "$work"/

# The contracts repo as the VM has it: a clone of a remote. demo-shop gets a
# webhook token the way an Operator adds one.
for vendor in grafana azure; do
  docker run --rm --read-only --tmpfs /tmp "$image" integrations snapshot "$vendor" --check \
    || { echo "FAIL: $vendor's tools drifted from its snapshot: make snapshots" >&2; exit 1; }
done
# gcp is Google's remote servers, unpinned: their deploys must not fail this push.
docker run --rm --read-only --tmpfs /tmp "$image" integrations snapshot gcp --check \
  || echo "WARNING: gcp's remote tools drifted from its snapshot: make snapshots" >&2
new=$(docker run --rm "$image" contracts new-token 2>/dev/null)
token=$(sed -n 1p <<<"$new" | awk '{print $NF}')
git_ init -q -b main "$work/seed"
cp -R "$root"/demo/contracts/. "$work/seed/"
rm -rf "$work/seed/test-keys"   # keys never live in the contracts repo
sed -n 2p <<<"$new" | sed 's/^contract.yaml: *//' >>"$work/seed/demo-shop/contract.yaml"
git_ -C "$work/seed" add -A && git_ -C "$work/seed" commit -qm seed
git clone -q --bare "$work/seed" "$work/origin.git"
git clone -q "$work/origin.git" "$work/contracts"

# The age keys file exactly as documented: owned by the image's uid 10001, 0400.
cat "$root"/demo/contracts/test-keys/*.agekey >"$work/age-keys.txt"
chmod 0400 "$work/age-keys.txt"
if [[ $(uname) == Linux ]]; then sudo chown 10001 "$work/age-keys.txt"; fi
mkdir -m 0700 "$work/backups"
if [[ $(uname) == Linux ]]; then sudo chown 999 "$work/backups"; fi   # postgres image's uid

cat >"$work/.env" <<EOF
SQUERO_IMAGE=$image
POSTGRES_PASSWORD=smoke-$RANDOM$RANDOM
SQUERO_DOMAIN=localhost
SQUERO_LOCAL_PORT=18000
SQUERO_HTTP_PORT=18081
SQUERO_HTTPS_PORT=18443
EOF
printf 'SQUERO_APPROVER_TOKEN=smoke-approver\nSQUERO_MODEL_API_KEY=not-a-real-key\n' >"$work/hub.env"
echo 'SQUERO_CONSOLE_OPERATORS=["smoke@example.com"]' >>"$work/hub.env"
mcp_token=smoke-mcp-token-0123456789abcdef
echo "SQUERO_MCP_TOKEN=$mcp_token" >>"$work/hub.env"

dc up -d --wait --quiet-pull || fail "stack did not come up healthy"
[[ $(dc port hub 8080) == 127.0.0.1:18000 ]] || fail "hub published on $(dc port hub 8080)"
echo "ok   stack healthy; hub only on 127.0.0.1"

# The Hub MCP server answers on the Hub's own port (what tailscale serve proxies)
mcp=(-H 'Accept: application/json, text/event-stream' -H 'Content-Type: application/json'
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}')
[[ $(curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:18000/mcp/ "${mcp[@]}") == 401 ]] \
  || fail "the Hub MCP server answered without its token"
curl -sf -X POST http://127.0.0.1:18000/mcp/ "${mcp[@]}" -H "Authorization: Bearer $mcp_token" \
  -H 'Tailscale-User-Login: smoke@example.com' | grep -q '"list_contracts"' \
  || fail "the Hub MCP server did not list its tools"
echo "ok   Hub MCP server: refused without its token, lists its tools with it"

public=https://localhost:18443
code() { curl -sk -o /dev/null -w '%{http_code}' "$@"; }
alert="$public/contracts/demo-shop/alertmanager"
json=(-H 'Content-Type: application/json' --data-binary @"$root/examples/alert-crashloop.json")

[[ $(code -X POST "$alert" -H 'Authorization: Bearer wrong' "${json[@]}") == 401 ]] || fail "wrong token"
id=$(curl -sfk -X POST "$alert" -H "Authorization: Bearer $token" "${json[@]}" |
  python3 -c 'import json,sys; print(json.load(sys.stdin)["investigation"])') || fail "alert refused"
echo "ok   alert accepted through Caddy: $id"
grafana=(-H 'Content-Type: application/json' --data-binary @"$root/tests/fixtures/grafana-firing.json")
[[ $(code -X POST "$public/contracts/demo-shop/grafana" -H 'Authorization: Bearer wrong' \
  "${grafana[@]}") == 401 ]] || fail "Grafana route: wrong token"
curl -sfk -X POST "$public/contracts/demo-shop/grafana" -H "Authorization: Bearer $token" \
  "${grafana[@]}" | grep -q '"accepted"' || fail "Grafana alert refused"
echo "ok   Grafana Alerting webhook accepted through Caddy"

for path in /investigations "/investigations/$id" /metrics /healthz /docs /openapi.json \
  /contracts/demo-shop/alertmanager/x /contracts/demo-shop/grafana/x; do
  [[ $(code -H 'Authorization: Bearer smoke-approver' "$public$path") == 404 ]] || fail "GET $path is public"
done
[[ $(code -X POST "$public/investigations/$id/decision" -H 'Authorization: Bearer smoke-approver' \
  -H 'Content-Type: application/json' -d '{"approve": true}') == 404 ]] || fail "decisions are public"
for path in /mcp /mcp/; do  # the Hub MCP server: tailscale serve only
  [[ $(code -X POST "$public$path" -H 'Authorization: Bearer smoke-approver' "${json[@]}") == 404 ]] \
    || fail "POST $path is public"
done
[[ $(code -X GET "$alert" -H "Authorization: Bearer $token") == 404 ]] || fail "GET on the alert route"
for path in /contracts/demo-shop/alertmanager/../../investigations \
  /contracts/demo-shop%2F..%2F..%2Finvestigations/alertmanager \
  /contracts/demo-shop/alertmanager%2F..%2F..%2Finvestigations; do
  got=$(code --path-as-is -X POST "$public$path" -H "Authorization: Bearer $token" "${json[@]}")
  [[ $got == 404 ]] || fail "POST $path reached the Hub ($got)"
done
big=$(code -X POST "$alert" -H "Authorization: Bearer $token" -H 'Content-Type: application/json' \
  --data-binary @<(head -c 2000000 /dev/zero | tr '\0' 'a'))
[[ $big == 413 ]] || fail "a 2 MB body got $big, want 413"
as_operator=(-H 'Tailscale-User-Login: smoke@example.com')
[[ $(code "${as_operator[@]}" "$public/console") == 404 ]] || fail "the console is public"
for path in /app/ /api/v1/contracts; do
  [[ $(code "${as_operator[@]}" "$public$path") == 404 ]] || fail "$path is public"
done
upgrade=(--http1.1 -H 'Connection: Upgrade' -H 'Upgrade: websocket' -H 'Sec-WebSocket-Version: 13'
  -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==')
got=$(code "${upgrade[@]}" "$public/relay")
[[ $got == 403 ]] || fail "a Relay without a token wasn't refused ($got)"
got=$(code "${upgrade[@]}" -H 'Authorization: Bearer nope' "$public/relay")
[[ $got == 403 ]] || fail "a Relay with a wrong token wasn't refused ($got)"
[[ $(code "$public/relay") == 404 ]] || fail "GET /relay without an upgrade is public"
[[ $(code "${upgrade[@]}" "$public/relay/x") == 404 ]] || fail "/relay/x is public"
got=$(code -H 'Authorization: Bearer nope' "$public/actuator/desired")
[[ $got == 401 ]] || fail "an Actuator with a wrong token wasn't refused ($got)"
got=$(code -X POST -H 'Authorization: Bearer nope' -H 'Content-Type: application/json' -d '[]' \
  "$public/actuator/reports")
[[ $got == 401 ]] || fail "an Actuator's report with a wrong token wasn't refused ($got)"
got=$(code -X POST -H 'Authorization: Bearer nope' -H 'Content-Type: application/json' \
  -d '{"at": 1, "namespaces": [], "pods": []}' "$public/actuator/blockers")
[[ $got == 401 ]] || fail "an Actuator's Blockers with a wrong token weren't refused ($got)"
[[ $(code "$public/actuator/x") == 404 ]] || fail "/actuator/x is public"
echo "ok   only the alert route, the Relays' websocket and the Actuators' three routes are public; bodies over 1 MB refused"

local_api=http://127.0.0.1:18000
for _ in $(seq 60); do
  inv=$(curl -sf "$local_api/investigations/$id" -H 'Authorization: Bearer smoke-approver')
  status=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["status"])' "$inv")
  [[ $status == failed ]] && break
  sleep 1
done
# No cluster in the container: the Run fails connecting to demo-shop's kind context.
python3 - "$inv" <<'EOF' || fail "unexpected investigation: $inv"
import json, sys
t = json.loads(sys.argv[1])
assert t["status"] == "failed" and t["contract_id"] == "demo-shop", t
assert t["error"].startswith("ConfigException"), t["error"]
EOF
echo "ok   investigation stored, failed as expected without a cluster"

# The console app, as `tailscale serve` reaches it: its page and built files are in the
# image, behind the identity; its API also wants the token.
# The cookie the token page sets: an HMAC of the approver token (console.py).
key=$(python3 -c 'import hashlib,hmac; print(hmac.new(b"smoke-approver", b"squero-console", hashlib.sha256).hexdigest())')
with_token=("${as_operator[@]}" -H "Cookie: squero_actions=$key")
page=$(curl -sf "${as_operator[@]}" "$local_api/app/") || fail "console app page"
js=$(grep -o '/app/assets/[A-Za-z0-9._-]*\.js' <<<"$page" | head -1)
[[ -n $js && $(code "${as_operator[@]}" "$local_api$js") == 200 ]] || fail "console app files"
curl -sf "${with_token[@]}" "$local_api/api/v1/contracts" | grep -q '"demo-shop"' || fail "console API"
curl -sf "${with_token[@]}" "$local_api/api/v1/investigations/$id" | grep -q 'ConfigException' ||
  fail "console investigation"
curl -sf --max-time 60 "${with_token[@]}" "$local_api/api/v1/contracts/demo-shop/health" |
  grep -q '"ok":false' || fail "console health"   # no cluster in the container: its checks fail
[[ $(code "${as_operator[@]}" "$local_api/api/v1/contracts") == 401 ]] || fail "console API without the token"
[[ $(code "$local_api/app/") == 403 ]] || fail "console app without an identity"
# The Jinja console's old addresses (11.7b) land in the app.
moved=$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' "$local_api/console/investigations/$id")
[[ $moved == "303 $local_api/app/?view=investigation&id=$id" ]] || fail "old console address: $moved"
echo "ok   console app on 127.0.0.1 for the Operator with the token only, old addresses redirect, 404 through Caddy"

dc exec -T backup /backup.sh once >/dev/null || fail "backup"
dc exec -T backup sh -c '
  f=$(ls -t /backups/squero-*.dump | head -1)
  [ "$(stat -c %a "$f")" = 600 ] || { echo "dump mode $(stat -c %a "$f")"; exit 1; }
  createdb restored && pg_restore --no-owner -d restored "$f"
  psql -d restored -tAc "select count(*) from investigations where id = '"'$id'"'"' |
  grep -qx 1 || fail "backup did not restore"
echo "ok   backup is owner-only and restores"

started=$(docker inspect -f '{{.State.StartedAt}}' "$(dc ps -q hub)")
printf 'id: demo-shop\nnaem: typo\n' >"$work/seed/demo-shop/contract.yaml"
git_ -C "$work/seed" commit -qam "bad contract" && git -C "$work/seed" push -q "$work/origin.git" main
good=$(git -C "$work/contracts" rev-parse HEAD)
if (cd "$work" && SQUERO_CONTRACTS=./contracts ./update-contracts.sh) >/dev/null 2>&1; then
  fail "a bad contracts update was applied"
fi
[[ $(git -C "$work/contracts" rev-parse HEAD) == "$good" ]] || fail "clone not rolled back"
[[ $(docker inspect -f '{{.State.StartedAt}}' "$(dc ps -q hub)") == "$started" ]] || fail "hub restarted"
echo "ok   a bad contracts update is refused and rolled back; hub untouched"

echo "hub smoke ok"
