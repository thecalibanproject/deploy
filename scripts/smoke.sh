#!/usr/bin/env bash
# Smoke-test a running Caliban: health endpoints, model list and one chat completion
# through the router to the local model.
#
#   scripts/smoke.sh                                # compose defaults on localhost
#   CALIBAN_API_KEY=cal_... scripts/smoke.sh
#
# Env:
#   CALIBAN_URL          data plane      (default http://127.0.0.1:8080)
#   CALIBAN_ADMIN_URL    control plane   (default http://127.0.0.1:8081)
#   CALIBAN_ADMIN_TOKEN  admin bearer token (optional; read from compose/.env if present)
#   CALIBAN_API_KEY      tenant key cal_... (required for /v1 calls; from `caliban keygen`)
#   MINT_KEY=1           no API key? mint one via the admin API for $TENANT (needs admin token)
#   TENANT               tenant id for MINT_KEY (default "default")
#   MODEL                model for the completion (default caliban/auto = tenant "default" route;
#                        or a catalogue id such as local/qwen3.5-9b)
#   TIMEOUT              seconds per request (default 120; CPU inference is slow)
#   SKIP_CHAT=1          only run the health checks
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../compose/.env"

CALIBAN_URL="${CALIBAN_URL:-http://127.0.0.1:8080}"
CALIBAN_ADMIN_URL="${CALIBAN_ADMIN_URL:-http://127.0.0.1:8081}"
MODEL="${MODEL:-caliban/auto}"
TIMEOUT="${TIMEOUT:-120}"
SKIP_CHAT="${SKIP_CHAT:-0}"
MINT_KEY="${MINT_KEY:-0}"
TENANT="${TENANT:-default}"

if [[ -z "${CALIBAN_ADMIN_TOKEN:-}" && -r "$ENV_FILE" ]]; then
  CALIBAN_ADMIN_TOKEN="$(grep -E '^CALIBAN_ADMIN_TOKEN=' "$ENV_FILE" | tail -1 | cut -d= -f2- || true)"
fi

command -v curl >/dev/null || { echo "curl is required" >&2; exit 2; }
HAVE_JQ=false; command -v jq >/dev/null && HAVE_JQ=true

pass=0; fail=0
ok()  { echo "  PASS  $*"; pass=$((pass + 1)); }
bad() { echo "  FAIL  $*"; fail=$((fail + 1)); }

# check NAME URL [curl args...]  -> passes on HTTP 2xx
check() {
  local name="$1" url="$2"; shift 2
  local code
  # curl prints 000 itself via -w when the connection fails.
  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 "$@" "$url" 2>/dev/null)" || true
  if [[ "$code" =~ ^2 ]]; then ok "$name ($code)"; else bad "$name -> HTTP $code ($url)"; fi
}

echo "Caliban smoke test"
echo "  router:        $CALIBAN_URL"
echo "  control plane: $CALIBAN_ADMIN_URL"
echo

echo "[health]"
check "router /healthz"               "$CALIBAN_URL/healthz"
check "control-plane /api/v1/health"  "$CALIBAN_ADMIN_URL/api/v1/health"
# No /metrics check: core has no Prometheus endpoint yet (see the core README).
check "web console /"                 "$CALIBAN_ADMIN_URL/"
if [[ -n "${CALIBAN_ADMIN_TOKEN:-}" ]]; then
  # /api/v1/health is unauthenticated; /api/v1/tenants is not. 401 = wrong token.
  check "admin auth GET /api/v1/tenants" "$CALIBAN_ADMIN_URL/api/v1/tenants" -H "Authorization: Bearer $CALIBAN_ADMIN_TOKEN"
fi

if [[ -z "${CALIBAN_API_KEY:-}" && "$MINT_KEY" == 1 && -n "${CALIBAN_ADMIN_TOKEN:-}" ]]; then
  echo
  echo "[mint] POST /api/v1/tenants/$TENANT/api-keys"
  resp="$(curl -sS --max-time 10 -X POST -H "Authorization: Bearer $CALIBAN_ADMIN_TOKEN" \
           -H 'Content-Type: application/json' -d '{"name":"smoke-test"}' \
           "$CALIBAN_ADMIN_URL/api/v1/tenants/$TENANT/api-keys" 2>/dev/null || true)"
  if [[ "$HAVE_JQ" == true ]]; then
    CALIBAN_API_KEY="$(printf '%s' "$resp" | jq -r '.key // empty' 2>/dev/null || true)"
  else
    CALIBAN_API_KEY="$(printf '%s' "$resp" | grep -o '"key":"cal_[^"]*"' | cut -d'"' -f4 || true)"
  fi
  if [[ -n "$CALIBAN_API_KEY" ]]; then ok "minted key for tenant $TENANT (revoke it after testing)"
  else bad "could not mint a key: ${resp:0:200}"; fi
fi

if [[ "$SKIP_CHAT" == 1 ]]; then
  echo; echo "SKIP_CHAT=1: skipping /v1 checks"
elif [[ -z "${CALIBAN_API_KEY:-}" ]]; then
  echo
  echo "[v1] skipped: set CALIBAN_API_KEY to a tenant key, or run with MINT_KEY=1 (and"
  echo "     CALIBAN_ADMIN_TOKEN) to mint one through the admin API. Keys in the config file are"
  echo "     ignored once Postgres is seeded."
else
  echo
  echo "[v1]"
  check "GET /v1/models" "$CALIBAN_URL/v1/models" -H "Authorization: Bearer $CALIBAN_API_KEY"

  body="$(cat <<JSON
{"model": "$MODEL",
 "messages": [{"role": "user", "content": "Reply with exactly the word: pong"}],
 "max_tokens": 64,
 "temperature": 0,
 "caliban": {"cache": "off"}}
JSON
)"
  hdrs="$(mktemp)"; out="$(mktemp)"
  trap 'rm -f "$hdrs" "$out"' EXIT
  start=$(date +%s)
  code="$(curl -sS -o "$out" -D "$hdrs" -w '%{http_code}' --max-time "$TIMEOUT" \
           -H "Authorization: Bearer $CALIBAN_API_KEY" -H 'Content-Type: application/json' \
           -d "$body" "$CALIBAN_URL/v1/chat/completions" 2>/dev/null)" || true
  secs=$(( $(date +%s) - start ))
  if [[ "$code" =~ ^2 ]]; then
    if [[ "$HAVE_JQ" == true ]]; then
      content="$(jq -r '.choices[0].message.content // empty' "$out")"
    else
      content="$(grep -o '"content":"[^"]*"' "$out" | head -1 | cut -d'"' -f4)"
    fi
    if [[ -n "$content" ]]; then ok "POST /v1/chat/completions ($code, ${secs}s): ${content:0:80}"
    else bad "POST /v1/chat/completions returned no content: $(head -c 300 "$out")"; fi
    grep -i '^x-caliban-' "$hdrs" | sed 's/^/        /' || true
  else
    bad "POST /v1/chat/completions -> HTTP $code: $(head -c 300 "$out")"
  fi
fi

echo
echo "passed: $pass  failed: $fail"
[[ $fail -eq 0 ]]
