#!/usr/bin/env bash
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# Operate the AML server stack (docker-compose.yml in this directory).
#
#   ./aml-server.sh up            preflight checks, build, start, wait until the adapter is healthy
#   ./aml-server.sh check         end-to-end check: health, auth, Add, Search, error codes
#   ./aml-server.sh durability    write a memory, restart every service, confirm it survived
#   ./aml-server.sh status        container states and health
#   ./aml-server.sh logs [svc]    follow logs (all services, or e.g. adapter)
#   ./aml-server.sh down          stop everything, keep the data
#   ./aml-server.sh wipe          stop everything and DELETE all stored data (asks first)
#
# Settings come from deploy/aml/.env (see README.md in this directory).
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$(cd "$DIR/.." && pwd)/.env"

say() { printf '%s\n' "$*"; }
ok() { printf '  [ OK ] %s\n' "$*"; }
warn() { printf '  [WARN] %s\n' "$*"; }
bad() { printf '  [FAIL] %s\n' "$*"; FAILED=1; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# Value of KEY in the env file (last occurrence, quotes stripped); never printed by this script.
envval() {
  [[ -f "$ENV_FILE" ]] || return 0
  local line
  line="$(grep -E "^[[:space:]]*$1[[:space:]]*=" "$ENV_FILE" | tail -n 1 || true)"
  line="${line#*=}"; line="${line%$'\r'}"
  line="${line#"${line%%[![:space:]]*}"}"; line="${line%"${line##*[![:space:]]}"}"
  line="${line%\"}"; line="${line#\"}"; line="${line%\'}"; line="${line#\'}"
  printf '%s' "$line"
}

compose() {
  local profile=()
  [[ -n "$(envval AML_DOMAIN)" ]] && profile=(--profile https)
  docker compose --env-file "$ENV_FILE" -f "$DIR/docker-compose.yml" "${profile[@]}" "$@"
}

PORT="$(envval AML_PORT)"; PORT="${PORT:-8200}"
BIND="$(envval AML_BIND_ADDRESS)"; BIND="${BIND:-127.0.0.1}"
[[ "$BIND" == "0.0.0.0" ]] && BIND=127.0.0.1
BASE="http://$BIND:$PORT"

http_code() { curl -s -o /dev/null -w '%{http_code}' -m 30 "$@" || true; }

wait_healthy() {  # seconds
  local deadline=$((SECONDS + $1))
  printf 'waiting for the adapter at %s ' "$BASE"
  while (( SECONDS < deadline )); do
    if [[ "$(http_code "$BASE/health")" == "200" ]]; then printf ' up\n'; return 0; fi
    printf '.'; sleep 5
  done
  printf '\n'
  compose ps
  say "the adapter did not become healthy; recent logs:"
  compose logs --tail=40 adapter rerank elasticsearch milvus || true
  return 1
}

preflight() {
  FAILED=0
  say "preflight"
  command -v docker >/dev/null 2>&1 || die "docker is not installed (see README.md, step 1)"
  docker info >/dev/null 2>&1 || die "cannot talk to the Docker daemon (is it running? is this user in the docker group?)"
  docker compose version >/dev/null 2>&1 || die "the docker compose plugin is missing (see README.md, step 1)"
  ok "docker $(docker version --format '{{.Server.Version}}' 2>/dev/null), $(docker compose version --short 2>/dev/null)"
  command -v curl >/dev/null 2>&1 || die "curl is not installed"
  [[ -f "$ENV_FILE" ]] || die "missing $ENV_FILE (copy deploy/aml/.env.example and fill it in)"
  ok "settings file $ENV_FILE"
  [[ -n "$(envval API_KEY)" ]] && ok "API_KEY set" || bad "API_KEY is empty (gpt-4o-mini key)"
  [[ -n "$(envval EMBED_API_KEY)" ]] && ok "EMBED_API_KEY set" || bad "EMBED_API_KEY is empty (DashScope key)"
  local key; key="$(envval AML_ADAPTER_KEY)"
  if (( ${#key} >= 32 )) && [[ "$key" != "local-dev-key" ]]; then ok "AML_ADAPTER_KEY set (${#key} characters)"
  else bad "AML_ADAPTER_KEY must be 32+ random characters (see README.md, step 3)"; fi
  local mmc; mmc="$(cat /proc/sys/vm/max_map_count 2>/dev/null || echo 0)"
  if (( mmc >= 262144 )); then ok "vm.max_map_count=$mmc"
  else warn "vm.max_map_count=$mmc; Elasticsearch wants 262144: sudo sysctl -w vm.max_map_count=262144"; fi
  local mem_kb; mem_kb="$(awk '/MemTotal/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)"
  if (( mem_kb >= 7000000 )); then ok "memory $((mem_kb / 1024 / 1024)) GB"
  else warn "memory $((mem_kb / 1024 / 1024)) GB; 8 GB or more is recommended"; fi
  local free_gb; free_gb="$(df -Pk "$DIR" | awk 'NR==2 {print int($4/1024/1024)}')"
  if (( free_gb >= 20 )); then ok "disk ${free_gb} GB free"; else warn "disk ${free_gb} GB free; 20 GB or more is recommended"; fi
  [[ -n "$(envval AML_DOMAIN)" ]] && ok "HTTPS front enabled for $(envval AML_DOMAIN)"
  (( FAILED == 0 )) || die "fix the [FAIL] items above, then run ./aml-server.sh up again"
}

add_body() {  # request_id user_id text
  printf '{"request_id":"%s","user_id":"%s","session_id":"check-session","messages":[{"role":"user","content":"%s","timestamp":1730000000000},{"role":"assistant","content":"Noted.","timestamp":1730000000000}]}' "$1" "$2" "$3"
}

search_body() { printf '{"query":"%s","user_id":"%s","top_k":5}' "$1" "$2"; }

post() {  # path body -> sets BODY and CODE (call directly, not in $(...), so both are visible)
  local out; out="$(curl -s -m 1800 -w '\n%{http_code}' -X POST "$BASE$1" \
    -H "Authorization: Bearer $(envval AML_ADAPTER_KEY)" -H "Content-Type: application/json" -d "$2" || true)"
  CODE="${out##*$'\n'}"; BODY="${out%$'\n'*}"
}

cmd_check() {
  FAILED=0
  say "end-to-end check against $BASE"
  [[ "$(http_code "$BASE/health")" == "200" ]] && ok "health" || { bad "health: adapter not reachable (./aml-server.sh status)"; return 1; }
  local code
  code="$(http_code -X POST "$BASE/search" -H "Content-Type: application/json" -d "$(search_body x x)")"
  [[ "$code" == "401" ]] && ok "request without key refused (401)" || bad "request without key returned $code, expected 401"
  code="$(http_code -X POST "$BASE/nope" -H "Authorization: Bearer $(envval AML_ADAPTER_KEY)" -d '{}')"
  [[ "$code" == "404" ]] && ok "unknown path (404)" || bad "unknown path returned $code, expected 404"
  local user body
  user="check-$(date +%s)"
  post /add "$(add_body "$user-1" "$user" "I just adopted a beagle named Biscuit last Saturday.")"; body="$BODY"
  if [[ "$CODE" == "200" && "$body" == *'"success": true'* && "$body" == *"\"$user-1\""* ]]; then ok "Add stored and echoed the IDs"
  else bad "Add returned $CODE: ${body:0:300}"; fi
  post /search "$(search_body "What is the name of my dog?" "$user")"; body="$BODY"
  if [[ "$CODE" == "200" && "$body" == *Biscuit* ]]; then ok "Search found the memory"
  else bad "Search returned $CODE: ${body:0:300}"; fi
  post /search "$(search_body "What is the name of my dog?" "$user-other")"; body="$BODY"
  [[ "$CODE" == "200" && "$body" == *'"data": []'* ]] && ok "another user_id sees nothing" || bad "isolation: ${body:0:200}"
  local domain; domain="$(envval AML_DOMAIN)"
  if [[ -n "$domain" ]]; then
    [[ "$(http_code "https://$domain/health")" == "200" ]] && ok "https://$domain/health" || bad "https://$domain/health not reachable (DNS, ports 80/443, ./aml-server.sh logs caddy)"
  fi
  (( FAILED == 0 )) && say "all checks passed" || { say "some checks failed"; return 1; }
}

cmd_durability() {
  FAILED=0
  say "durability check: write, restart every service, read back"
  [[ "$(http_code "$BASE/health")" == "200" ]] || die "adapter not reachable; run ./aml-server.sh up first"
  local user body
  user="durable-$(date +%s)"
  post /add "$(add_body "$user-1" "$user" "My locker at the gym is number 4417.")"; body="$BODY"
  [[ "$CODE" == "200" ]] && ok "Add stored" || { bad "Add returned $CODE: ${body:0:300}"; return 1; }
  say "restarting all services (takes a minute or two)"
  compose restart
  wait_healthy 900 || { bad "stack did not come back"; return 1; }
  post /search "$(search_body "What is my gym locker number?" "$user")"; body="$BODY"
  [[ "$CODE" == "200" && "$body" == *4417* ]] && ok "memory survived the restart" || bad "memory missing after restart: ${body:0:300}"
  local t0=$SECONDS
  post /add "$(add_body "$user-1" "$user" "My locker at the gym is number 4417.")"; body="$BODY"
  if [[ "$CODE" == "200" ]] && (( SECONDS - t0 < 5 )); then ok "retried Add answered from the ledger (not stored twice)"
  else bad "retried Add returned $CODE after $((SECONDS - t0)) s"; fi
  (( FAILED == 0 )) && say "durability check passed" || { say "durability check failed"; return 1; }
}

case "${1:-help}" in
  up)
    preflight
    say "building and starting (the first build downloads several GB and can take 10-20 minutes)"
    compose up -d --build
    wait_healthy 1200
    say "ready: $BASE (next: ./aml-server.sh check)"
    ;;
  check) cmd_check ;;
  durability) cmd_durability ;;
  status) compose ps ;;
  logs) shift; compose logs -f --tail=200 "$@" ;;
  down) compose down ;;
  wipe)
    say "This deletes ALL stored memories, indexes and the request ledger (every volume of this stack)."
    read -r -p "Type WIPE to continue: " answer
    [[ "$answer" == "WIPE" ]] || die "cancelled"
    compose down -v
    say "all data deleted"
    ;;
  help|-h|--help) sed -n '2,13p' "$0" ;;
  *) die "unknown command '$1' (try ./aml-server.sh help)" ;;
esac
