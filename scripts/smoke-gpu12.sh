#!/usr/bin/env bash
# End-to-end smoke test of the gpu-12gb compose profile on one CUDA host (README "1 × 12 GB"):
# fetch the pinned weights (sha256-verified), start the stack, then through the Caliban gateway
# run chat (latency, tokens/s, thinking, tool call), embeddings and rerank, check that the model
# servers have no route off the host, record GPU memory per stage, and tear everything down.
#
#   scripts/smoke-gpu12.sh
#   SKIP_FETCH=1 KEEP=1 scripts/smoke-gpu12.sh
#
# Needs: docker with the NVIDIA runtime (docker run --gpus all works), docker compose v2,
# nvidia-smi, curl, jq, openssl, and for the fetch the Hugging Face CLI
# (pip install -U huggingface_hub). The Caliban image must exist locally (images/build.sh).
#
# Env:
#   MODELS_DIR       weights, kept between runs (default /srv/caliban/models as root, else
#                    ~/.cache/caliban-models). The containers run as other users (Caliban as
#                    65532), so the script makes the tree world-readable and every parent
#                    directory must be traversable by others (o+x): a 0700 or 0750 home is not.
#   WORK_DIR         generated env file and results (default ~/.cache/caliban-gpu12)
#   PROJECT          compose project name (default caliban-gpu12)
#   CALIBAN_IMAGE    image repository (default caliban/caliban)
#   CALIBAN_VERSION  image tag (default 0.1.0)
#   GPU              GPU index (default 0)
#   NEED_VRAM_MIB    free GPU memory to wait for before starting (default 10240). Another job on
#                    the GPU is waited for (polled), never stopped.
#   VRAM_WAIT_SECS   how long to wait for it (default 1800)
#   SKIP_FETCH=1     weights already in MODELS_DIR (they are still checked against the lock)
#   PII=0            run without the PII NER model (default: use or fetch it, see below)
#   ML_DIR           checkout of the ml repo, for the PII NER fetch (default ../../ml)
#   KEEP=1           leave the stack running; default is `down -v` (MODELS_DIR is kept)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# HOME is unset under SSM Run Command and some systemd units.
HOME_DIR="${HOME:-$(getent passwd "$(id -u)" | cut -d: -f6)}"
HOME_DIR="${HOME_DIR:-/root}"
export HOME="$HOME_DIR"   # for docker, hf and python, which look for config under it
if [[ "$(id -u)" == 0 ]]; then DEFAULT_MODELS_DIR=/srv/caliban/models
else DEFAULT_MODELS_DIR="$HOME_DIR/.cache/caliban-models"; fi
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE_DIR="$REPO/compose"

MODELS_DIR="${MODELS_DIR:-$DEFAULT_MODELS_DIR}"
WORK_DIR="${WORK_DIR:-$HOME_DIR/.cache/caliban-gpu12}"
PROJECT="${PROJECT:-caliban-gpu12}"
CALIBAN_IMAGE="${CALIBAN_IMAGE:-caliban/caliban}"
CALIBAN_VERSION="${CALIBAN_VERSION:-0.1.0}"
GPU="${GPU:-0}"
NEED_VRAM_MIB="${NEED_VRAM_MIB:-10240}"
VRAM_WAIT_SECS="${VRAM_WAIT_SECS:-1800}"
SKIP_FETCH="${SKIP_FETCH:-0}"
PII="${PII:-1}"
ML_DIR="${ML_DIR:-$REPO/../ml}"
KEEP="${KEEP:-0}"

ROUTER=http://127.0.0.1:8080
ADMIN=http://127.0.0.1:8081
CHAT_MODEL=local/qwen3.5-9b-q4
PII_REL=pii/nym-pii-multilingual-small-int8/3.0.0
# models.lock.yaml entries of the profile (revision and tree sha256 pinned there).
GGUF_MODELS=(unsloth/Qwen3.5-9B-GGUF Qwen/Qwen3-Embedding-0.6B-GGUF ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF)

die() { echo "smoke-gpu12: $*" >&2; exit 1; }
for c in docker nvidia-smi curl jq openssl; do command -v "$c" >/dev/null || die "$c is required"; done
docker compose version >/dev/null 2>&1 || die "docker compose v2 is required"

mkdir -p "$MODELS_DIR" "$WORK_DIR"
MODELS_DIR="$(cd "$MODELS_DIR" && pwd)"
ENV_FILE="$WORK_DIR/smoke.env"
RESULTS="$WORK_DIR/results-$(date -u +%Y%m%dT%H%M%SZ).txt"
: > "$RESULTS"

pass=0; fail=0
say()  { echo "$*" | tee -a "$RESULTS"; }
ok()   { say "  PASS  $*"; pass=$((pass + 1)); }
bad()  { say "  FAIL  $*"; fail=$((fail + 1)); }
note() { say "        $*"; }

dc() {
  docker compose -p "$PROJECT" --env-file "$ENV_FILE" -f "$COMPOSE_DIR/docker-compose.yml" \
    --profile gpu-12gb "$@"
}
vram() {  # used free total (MiB)
  nvidia-smi -i "$GPU" --query-gpu=memory.used,memory.free,memory.total --format=csv,noheader,nounits \
    | tr -d ' ' | tr ',' ' '
}

# ── 1. Weights ──
say "[weights] $MODELS_DIR"
if [[ "$SKIP_FETCH" != 1 ]]; then
  command -v hf >/dev/null || command -v huggingface-cli >/dev/null \
    || die "the Hugging Face CLI is required for the fetch: pip install -U huggingface_hub"
  args=(); for m in "${GGUF_MODELS[@]}"; do args+=(--model "$m"); done
  # Downloads each entry at its pinned revision and fails on a tree sha256 mismatch.
  "$REPO/airgap/bundle.sh" fetch "${args[@]}" --models-dir "$MODELS_DIR"
fi
for f in qwen3.5-9b-gguf/Qwen3.5-9B-Q4_K_M.gguf qwen3-embedding-0.6b-gguf/Qwen3-Embedding-0.6B-Q8_0.gguf \
    qwen3-reranker-0.6b-gguf/qwen3-reranker-0.6b-q8_0.gguf; do
  [[ -s "$MODELS_DIR/$f" ]] || die "missing $MODELS_DIR/$f (run without SKIP_FETCH=1)"
done
# The lock pins a tree hash per directory; check it also when the fetch was skipped.
while read -r dir want; do
  got="$(cd "$MODELS_DIR/$dir" && find . -type f ! -path './.cache/*' -print0 | LC_ALL=C sort -z \
    | xargs -0 sha256sum | sha256sum | awk '{print $1}')"
  if [[ "$got" == "$want" ]]; then ok "sha256 $dir"; else bad "sha256 $dir: got $got, lock has $want"; fi
done < <(awk '
  /^  - id:/ { id = $3 }
  /^    dir:/ { dir = $2 }
  /^    profiles:/ { prof = $2 }
  /^    sha256:/ { s = $2; gsub(/"/, "", s); if (prof ~ /gpu-12gb/ && s != "") print dir, s }
' "$REPO/airgap/models.lock.yaml")

PII_DIR=""
if [[ "$PII" != 0 ]]; then
  if [[ ! -f "$MODELS_DIR/$PII_REL/manifest.json" && -f "$ML_DIR/scripts/fetch_pii_ner.py" ]]; then
    # Exit 3 = files fetched and hash-checked, licence review still pending (fine for a test).
    python3 -I "$ML_DIR/scripts/fetch_pii_ner.py" --out "$MODELS_DIR/pii" || [[ $? == 3 ]]
  fi
  if [[ -f "$MODELS_DIR/$PII_REL/manifest.json" ]]; then PII_DIR="/models/$PII_REL"
  else note "PII NER model not found (set ML_DIR or PII=0); starting without it"; fi
fi
[[ "$fail" == 0 ]] || die "weights do not match airgap/models.lock.yaml"
# Files fetched under umask 077 (or by fetch_pii_ner.py's temp files) are 0600; the model
# servers and Caliban (uid 65532) read them through a read-only bind mount.
chmod -R a+rX "$MODELS_DIR"
d="$MODELS_DIR"
while [[ "$d" != / ]]; do
  [[ "$(stat -c %A "$d")" == *[xt] ]] \
    || die "$d is not traversable by other users, so the containers cannot read $MODELS_DIR: chmod o+x it, or set MODELS_DIR elsewhere (e.g. /srv/caliban/models)"
  d="$(dirname "$d")"
done

# ── 2. Env file (secrets generated once, kept in WORK_DIR, mode 600) ──
if [[ ! -f "$ENV_FILE" ]]; then
  (
    umask 077
    grep -vE '^(CALIBAN_ADMIN_TOKEN|CALIBAN_KEK|POSTGRES_PASSWORD|VALKEY_PASSWORD)=' "$COMPOSE_DIR/.env.example"
    printf 'CALIBAN_ADMIN_TOKEN=%s\nCALIBAN_KEK=%s\nPOSTGRES_PASSWORD=%s\nVALKEY_PASSWORD=%s\n' \
      "$(openssl rand -hex 32)" "$(openssl rand -base64 32)" "$(openssl rand -hex 24)" "$(openssl rand -hex 24)"
  ) > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
fi
set_env() {  # KEY VALUE: replace the line (or append)
  local tmp; tmp="$(mktemp)"
  awk -v k="$1" -v v="$2" 'BEGIN { done = 0 } $0 ~ "^" k "=" { print k "=" v; done = 1; next } { print }
    END { if (!done) print k "=" v }' "$ENV_FILE" > "$tmp"
  cat "$tmp" > "$ENV_FILE"; rm -f "$tmp"
}
set_env MODELS_DIR "$MODELS_DIR"
set_env CALIBAN_PII_MODEL_DIR "$MODELS_DIR/pii"
set_env CALIBAN_PII_NER_DIR "$PII_DIR"
set_env CALIBAN_IMAGE "$CALIBAN_IMAGE"
set_env CALIBAN_VERSION "$CALIBAN_VERSION"
set_env GPU12_GPU "$GPU"
ADMIN_TOKEN="$(grep -E '^CALIBAN_ADMIN_TOKEN=' "$ENV_FILE" | cut -d= -f2-)"
docker image inspect "$CALIBAN_IMAGE:$CALIBAN_VERSION" >/dev/null 2>&1 \
  || die "image $CALIBAN_IMAGE:$CALIBAN_VERSION not found: build it with images/build.sh --version $CALIBAN_VERSION"

cleanup() {
  if [[ "$KEEP" == 1 ]]; then
    echo "KEEP=1: stack left running. Stop it with:"
    echo "  docker compose -p $PROJECT --env-file $ENV_FILE -f $COMPOSE_DIR/docker-compose.yml --profile gpu-12gb down -v"
  else
    dc down -v --remove-orphans >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

# ── 3. Wait for GPU memory (poll; never stop another job) ──
say "[gpu] $(nvidia-smi -i "$GPU" --query-gpu=name,driver_version --format=csv,noheader)"
deadline=$(( $(date +%s) + VRAM_WAIT_SECS ))
while :; do
  read -r used free total < <(vram)
  (( free >= NEED_VRAM_MIB )) && break
  (( $(date +%s) < deadline )) || die "only $free MiB free on GPU $GPU after ${VRAM_WAIT_SECS}s (need $NEED_VRAM_MIB)"
  echo "  waiting for GPU memory: $free MiB free, need $NEED_VRAM_MIB"
  sleep 30
done
note "before start: used $used MiB, free $free MiB of $total MiB"
base_used=$used

# ── 4. Start: embed, then rerank, then the chat pool and Caliban (one at a time, to measure) ──
say "[start] project $PROJECT"
t0=$(date +%s)
prev=$base_used
for s in embed-gpu12 rerank-gpu12; do
  dc up -d --wait --wait-timeout 600 "$s"
  read -r used free total < <(vram)
  note "$s loaded: +$((used - prev)) MiB (free $free MiB)"
  prev=$used
done
small_used=$used
dc up -d --wait --wait-timeout 900
read -r used free total < <(vram)
ok "stack healthy in $(( $(date +%s) - t0 ))s"
note "chat pool loaded: +$((used - small_used)) MiB; stack total +$((used - base_used)) MiB; free $free MiB of $total MiB"
for s in qwen3-gpu12 embed-gpu12 rerank-gpu12; do
  dc logs --no-color "$s" 2>/dev/null \
    | grep -E 'buffer size' | sed "s/^/        $s: /" | tee -a "$RESULTS" || true
done

# ── 5. Tenant key ──
say "[key]"
KEY="$(curl -sS --max-time 10 -X POST -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"smoke-gpu12"}' "$ADMIN/api/v1/tenants/default/api-keys" | jq -r '.key // empty')"
[[ -n "$KEY" ]] || die "could not mint a tenant key"
ok "minted a key for tenant default (removed with the stack)"

# post PATH JSON -> sets BODY, CODE, SECS, HDRS
post() {
  local out hdr
  out="$(mktemp)"; hdr="$(mktemp)"
  read -r CODE SECS < <(curl -sS -o "$out" -D "$hdr" -w '%{http_code} %{time_total}\n' --max-time 300 \
    -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d "$2" "$ROUTER$1" || echo "000 0")
  BODY="$(cat "$out")"; HDRS="$(cat "$hdr")"; rm -f "$out" "$hdr"
}

# ── 6. Chat through the gateway ──
say "[chat] $CHAT_MODEL"
post /v1/chat/completions "{\"model\":\"$CHAT_MODEL\",\"reasoning_effort\":\"none\",\"max_tokens\":16,\"temperature\":0,
  \"messages\":[{\"role\":\"user\",\"content\":\"Reply with exactly the word: pong\"}],\"caliban\":{\"cache\":\"off\"}}"
if [[ "$CODE" == 200 ]]; then ok "short completion in ${SECS}s: $(jq -r '.choices[0].message.content' <<<"$BODY" | head -c 60)"
else bad "short completion -> HTTP $CODE: ${BODY:0:300}"; fi

post /v1/chat/completions "{\"model\":\"$CHAT_MODEL\",\"reasoning_effort\":\"none\",\"max_tokens\":300,\"temperature\":0,
  \"messages\":[{\"role\":\"user\",\"content\":\"Write about 250 words on how a sovereign AI gateway protects personal data.\"}],
  \"caliban\":{\"cache\":\"off\"}}"
if [[ "$CODE" == 200 ]]; then
  n="$(jq -r '.usage.completion_tokens' <<<"$BODY")"
  ok "300-token completion: $n tokens in ${SECS}s = $(awk -v n="$n" -v s="$SECS" 'BEGIN { printf "%.1f", n / s }') tokens/s end to end (prefill included)"
else bad "long completion -> HTTP $CODE: ${BODY:0:300}"; fi

# Engine-side decode and prefill rates, straight from llama.cpp (timings are not passed through).
eng="$(dc exec -T qwen3-gpu12 curl -sS --max-time 300 http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' -d '{"max_tokens":300,"temperature":0,"chat_template_kwargs":{"enable_thinking":false},
  "messages":[{"role":"user","content":"Write about 250 words on how a sovereign AI gateway protects personal data."}]}' || true)"
if jq -e '.timings.predicted_per_second' <<<"$eng" >/dev/null 2>&1; then
  ok "llama.cpp decode $(jq -r '.timings.predicted_per_second | floor' <<<"$eng") tokens/s, prefill $(jq -r '.timings.prompt_per_second | floor' <<<"$eng") tokens/s (one stream)"
else bad "llama.cpp timings missing: ${eng:0:200}"; fi

ttfb="$(curl -sS -o /dev/null -w '%{time_starttransfer}' --max-time 120 -N -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' "$ROUTER/v1/chat/completions" -d "{\"model\":\"$CHAT_MODEL\",\"stream\":true,
  \"reasoning_effort\":\"none\",\"max_tokens\":32,\"messages\":[{\"role\":\"user\",\"content\":\"Say hello.\"}],
  \"caliban\":{\"cache\":\"off\"}}" || echo fail)"
note "streamed: first byte after ${ttfb}s"

post /v1/chat/completions "{\"model\":\"$CHAT_MODEL\",\"reasoning_effort\":\"high\",\"max_tokens\":1024,
  \"messages\":[{\"role\":\"user\",\"content\":\"What is 17 * 23? Answer with the number.\"}],\"caliban\":{\"cache\":\"off\"}}"
if [[ "$CODE" == 200 ]] && jq -e '.choices[0].message | (.reasoning // .reasoning_content // "") | length > 0' <<<"$BODY" >/dev/null; then
  ok "thinking on: reasoning returned separately, answer $(jq -r '.choices[0].message.content' <<<"$BODY" | tr -d '\n' | head -c 40) (${SECS}s)"
else bad "thinking on -> HTTP $CODE, no reasoning field: ${BODY:0:300}"; fi

post /v1/chat/completions "{\"model\":\"$CHAT_MODEL\",\"reasoning_effort\":\"none\",\"max_tokens\":256,
  \"messages\":[{\"role\":\"user\",\"content\":\"What is the weather in Beirut?\"}],
  \"tools\":[{\"type\":\"function\",\"function\":{\"name\":\"get_weather\",\"description\":\"Current weather for a city\",
  \"parameters\":{\"type\":\"object\",\"properties\":{\"city\":{\"type\":\"string\"}},\"required\":[\"city\"]}}}],
  \"caliban\":{\"cache\":\"off\"}}"
if [[ "$CODE" == 200 ]] && jq -e '.choices[0].message.tool_calls[0].function.name == "get_weather"' <<<"$BODY" >/dev/null; then
  ok "tool call: $(jq -c '.choices[0].message.tool_calls[0].function' <<<"$BODY" | head -c 100)"
else bad "tool call -> HTTP $CODE: ${BODY:0:300}"; fi

post /v1/chat/completions '{"model":"caliban/auto","reasoning_effort":"none","max_tokens":16,
  "messages":[{"role":"user","content":"Hello"}],"caliban":{"cache":"off"}}'
# Every tenant route lists local/qwen3.5-9b-q4, and it is the only chat pool running.
routed="$(grep -i '^x-caliban-routed-model:' <<<"$HDRS" | tr -d '\r' | cut -d' ' -f2- || true)"
intent="$(grep -i '^x-caliban-intent:' <<<"$HDRS" | tr -d '\r' | cut -d' ' -f2- || true)"
if [[ "$CODE" == 200 && "$routed" == "$CHAT_MODEL" ]]; then ok "caliban/auto -> $routed (intent ${intent:-none}, ${SECS}s)"
else bad "caliban/auto -> HTTP $CODE, x-caliban-routed-model '${routed}' (want $CHAT_MODEL): ${BODY:0:200}"; fi

# ── 7. Embeddings and rerank through the gateway ──
say "[embeddings, rerank]"
post /v1/embeddings '{"model":"local/qwen3-embedding-0.6b","input":["Caliban keeps data on-prem.","Le modèle tourne sur site."]}'
if [[ "$CODE" == 200 ]] && [[ "$(jq '.data[0].embedding | length' <<<"$BODY")" == 1024 ]]; then
  ok "embeddings: 2 inputs, 1024-d, ${SECS}s"
else bad "embeddings -> HTTP $CODE: ${BODY:0:300}"; fi

post /v1/rerank '{"model":"local/qwen3-reranker-0.6b","query":"What is the capital of France?",
  "documents":["Bananas are yellow.","Paris is the capital of France.","Berlin is in Germany."]}'
if [[ "$CODE" == 200 ]] && [[ "$(jq '.results[0].index' <<<"$BODY")" == 1 ]]; then
  ok "rerank: relevant document first, scores $(jq -c '[.results[] | {i: .index, s: (.relevance_score * 1000 | round / 1000)}]' <<<"$BODY") (${SECS}s)"
else bad "rerank -> HTTP $CODE: ${BODY:0:300}"; fi

# ── 8. Zero egress ──
say "[egress]"
if [[ "$(docker network inspect -f '{{.Internal}}' "${PROJECT}_backend")" == true ]]; then ok "backend network is internal"
else bad "backend network is not internal"; fi
if [[ "$(docker network inspect -f '{{index .Options "com.docker.network.bridge.enable_ip_masquerade"}}' "${PROJECT}_edge")" == false ]]; then
  ok "edge network has no masquerade"
else bad "edge network masquerades"; fi
for s in qwen3-gpu12 embed-gpu12 rerank-gpu12; do
  if dc exec -T "$s" curl -sS -o /dev/null --max-time 5 https://huggingface.co 2>/dev/null; then bad "$s reached huggingface.co"
  else ok "$s has no route out"; fi
done
if dc logs --no-color qwen3-gpu12 embed-gpu12 rerank-gpu12 caliban 2>/dev/null | grep -iE 'huggingface\.co|downloading' >/dev/null; then
  bad "a service tried to download something (see docker compose logs)"
else ok "no download attempts in the logs"; fi

say
say "passed: $pass  failed: $fail   (results: $RESULTS)"
[[ $fail -eq 0 ]]
