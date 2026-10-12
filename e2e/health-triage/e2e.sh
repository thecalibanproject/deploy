#!/usr/bin/env bash
# Health triage end-to-end test of the whole product flow (see README.md in this directory).
#
#   e2e/health-triage/e2e.sh up       # stack + MongoDB replica set + synthetic seed + model
#   e2e/health-triage/e2e.sh setup    # tenant, key, datasource, ontology, node, evals, gate, intent (admin API)
#   e2e/health-triage/e2e.sh run      # end-to-end requests through caliban/auto, report
#   E2E_RAG=1 e2e/health-triage/e2e.sh setup | run | all   # plus the optional RAG stage (off by default)
#   e2e/health-triage/e2e.sh all      # up, setup, run
#   e2e/health-triage/e2e.sh down     # remove what `up` created (containers, volumes, work dir)
#   e2e/health-triage/e2e.sh status | logs [service]
#   e2e/health-triage/e2e.sh exec COMMAND...   # COMMAND with the e2e's environment (the demo runner)
#
# E2E_TARGET=local (default): its own compose project `caliban-e2e` from compose/docker-compose.yml,
#   the scripted model, no GPU. Secrets are generated into the work directory.
# E2E_TARGET=testbed: on the AWS testbed gateway, on top of the running `caliban` project
#   (compose.testbed-gateway.yml, its compose/.env) with the GPU host's models; see
#   RUN-ON-TESTBED.md. `down` there removes only the e2e containers and volumes and restarts
#   caliban with its own config.
#
# Every setting below can be overridden from the environment.
set -euo pipefail

E2E_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$(cd "$E2E_DIR/../.." && pwd)"
E2E_TARGET="${E2E_TARGET:-local}"
E2E_WORK_DIR="${E2E_WORK_DIR:-$E2E_DIR/.work}"

die() { echo "e2e.sh: $*" >&2; exit 1; }
log() { echo ">> $*"; }
rand() { head -c "$1" /dev/urandom | base64 | tr -d '/+=\n' | cut -c "1-$1"; }

case "$E2E_TARGET" in
  local)
    : "${E2E_PROJECT:=caliban-e2e}"
    : "${E2E_STACK_ENV:=$E2E_WORK_DIR/stack.env}"
    : "${E2E_BASE_CONFIG:=$DEPLOY_DIR/compose/config/caliban.toml}"
    : "${E2E_EXTRA_COMPOSE:=}"
    : "${E2E_UPSTREAM_MODEL:=e2e-scripted}"
    : "${E2E_MODEL_FAMILY:=}"
    : "${E2E_MODEL_UPSTREAM_URL:=}"
    # Embeddings for caliban/auto: the scripted model's hashed vectors, registered in the config.
    : "${E2E_ROUTING_EMBED_MODEL:=e2e/embed}"
    : "${E2E_ROUTING_PRESET:=mock}"
    : "${E2E_EXPECT_NER:=0}"
    # RAG stage: the scripted model embeds; it has no /v1/rerank, so no reranker.
    : "${E2E_RAG_RERANK_MODEL:=}"
    ;;
  testbed)
    : "${E2E_PROJECT:=caliban}"
    : "${E2E_STACK_ENV:=$DEPLOY_DIR/compose/.env}"
    : "${E2E_BASE_CONFIG:=/opt/caliban-testbed/caliban.toml}"
    : "${E2E_EXTRA_COMPOSE:=/opt/caliban-testbed/compose.testbed-gateway.yml}"
    : "${E2E_GPU_HOST:=${GPU_IP:-10.42.0.20}}"
    : "${E2E_UPSTREAM_MODEL:=Qwen/Qwen3.8-27B-FP8}"
    : "${E2E_MODEL_FAMILY:=qwen3.8}"
    # The recorder forwards to vLLM on the GPU host and logs what the model receives.
    : "${E2E_MODEL_UPSTREAM_URL:=http://$E2E_GPU_HOST:8000/v1}"
    # The stack's own embedding model (TEI on the GPU host, port 8001 in the testbed config).
    : "${E2E_ROUTING_EMBED_MODEL:=local/qwen3-embedding-0.6b}"
    : "${E2E_ROUTING_PRESET:=qwen3-embedding}"
    : "${E2E_EXPECT_NER:=0}"
    # RAG stage: the stack's reranker (vLLM on the GPU host, port 8002 in the testbed config).
    : "${E2E_RAG_RERANK_MODEL:=local/qwen3-reranker-0.6b}"
    # The node's model calls run with thinking off (E2E_ASSESS_REASONING, E2E_RECOMMEND_REASONING);
    # the golden bar's p95 and the eval timeout keep room for a run with thinking on.
    : "${E2E_MAX_P95_MS:=300000}"
    : "${E2E_EVAL_TIMEOUT_S:=3600}"
    ;;
  *) die "E2E_TARGET must be local or testbed (got '$E2E_TARGET')" ;;
esac
# The chat model the e2e registers through the admin API: on-prem, so t0_sovereign like every model
# of compose/config/caliban.toml. The node's guardrails.pii masks identifiers on its calls whatever
# the model's tier; E2E_PII_ON_SOVEREIGN=1 makes the tenant mask its other in-boundary calls too
# (README.md, "PII").
: "${E2E_CHAT_MODEL_ID:=e2e/chat}"
: "${E2E_CHAT_BASE_URL:=http://caliban-e2e-model:8000/v1}"
: "${E2E_CHAT_TRUST_TIER:=t0_sovereign}"
: "${E2E_PII_ON_SOVEREIGN:=0}"
: "${E2E_PRICE_IN_PER_MTOK:=0.20}"
: "${E2E_PRICE_OUT_PER_MTOK:=0.80}"
: "${E2E_TENANT:=medmarket}"
: "${E2E_PII_MODE:=mask}"
# The optional RAG stage (README.md, "RAG stage"): off by default; 1 adds the document collection,
# the `careguide` node and its requests to setup and run. Needs a Caliban image with retrieval.
: "${E2E_RAG:=0}"
: "${E2E_RAG_COLLECTION:=care_docs}"
: "${E2E_RAG_EMBED_MODEL:=$E2E_ROUTING_EMBED_MODEL}"
export E2E_RAG E2E_RAG_COLLECTION E2E_RAG_EMBED_MODEL E2E_RAG_RERANK_MODEL
export E2E_DIR E2E_WORK_DIR E2E_TARGET E2E_PROJECT E2E_UPSTREAM_MODEL E2E_MODEL_FAMILY E2E_MODEL_UPSTREAM_URL \
  E2E_CHAT_MODEL_ID E2E_CHAT_BASE_URL E2E_CHAT_TRUST_TIER E2E_PRICE_IN_PER_MTOK E2E_PRICE_OUT_PER_MTOK \
  E2E_TENANT E2E_PII_MODE E2E_PII_ON_SOVEREIGN E2E_EXPECT_NER E2E_ROUTING_EMBED_MODEL
# Reasoning effort of the triage node's two model vertices: off, low, medium or high.
: "${E2E_ASSESS_REASONING:=off}"
: "${E2E_RECOMMEND_REASONING:=off}"
export E2E_ASSESS_REASONING E2E_RECOMMEND_REASONING
[[ -n "${E2E_MAX_P95_MS:-}" ]] && export E2E_MAX_P95_MS
[[ -n "${E2E_EVAL_TIMEOUT_S:-}" ]] && export E2E_EVAL_TIMEOUT_S

# E2E_EXTRA_COMPOSE: overlays between the stack's file and the e2e's, separated by colons or spaces
# (the testbed gateway's file, then any scenario overlays).
EXTRA_FILES=()
for f in ${E2E_EXTRA_COMPOSE//:/ }; do EXTRA_FILES+=(-f "$f"); done
FILES=(-f "$DEPLOY_DIR/compose/docker-compose.yml" "${EXTRA_FILES[@]}")
FILES+=(-f "$E2E_DIR/docker-compose.e2e.yml")
E2E_SERVICES=(caliban-e2e-mongo caliban-e2e-mongo-analytics caliban-e2e-model)

dc() { docker compose -p "$E2E_PROJECT" --env-file "$E2E_STACK_ENV" "${FILES[@]}" "$@"; }
envval() { sed -n "s/^$1=//p" "$E2E_STACK_ENV" | tail -n 1; }

# ─────────────────────────── secrets and config ───────────────────────────

secrets() {
  mkdir -p "$E2E_WORK_DIR/model-log"
  chmod 700 "$E2E_WORK_DIR"
  if [[ "$E2E_TARGET" == local && ! -f "$E2E_STACK_ENV" ]]; then
    log "generating local stack secrets in $E2E_STACK_ENV"
    (umask 077; cat > "$E2E_STACK_ENV") <<EOF
CALIBAN_IMAGE=${CALIBAN_IMAGE:-caliban/caliban}
CALIBAN_VERSION=${CALIBAN_VERSION:-0.1.0}
CALIBAN_ADMIN_TOKEN=$(rand 40)
CALIBAN_KEK=$(head -c 32 /dev/urandom | base64)
POSTGRES_PASSWORD=$(rand 32)
VALKEY_PASSWORD=$(rand 32)
CALIBAN_ROUTER_PORT=${CALIBAN_ROUTER_PORT:-18080}
CALIBAN_CP_PORT=${CALIBAN_CP_PORT:-18081}
POSTGRES_IMAGE=${POSTGRES_IMAGE:-postgres:17.11-bookworm}
EOF
  fi
  [[ -f "$E2E_STACK_ENV" ]] || die "no stack env file at $E2E_STACK_ENV"
  if [[ ! -f "$E2E_WORK_DIR/e2e.env" ]]; then
    (umask 077; cat > "$E2E_WORK_DIR/e2e.env") <<EOF
E2E_MONGO_KEYFILE=$(head -c 756 /dev/urandom | base64 -w0)
E2E_MONGO_ROOT_USER=e2e_root
E2E_MONGO_ROOT_PASSWORD=$(rand 32)
E2E_MONGO_READER_PASSWORD=$(rand 32)
EOF
  fi
  set -a
  # shellcheck source=/dev/null
  . "$E2E_WORK_DIR/e2e.env"
  set +a
  export E2E_CALIBAN_TOML="$E2E_WORK_DIR/caliban.toml" E2E_UID="${E2E_UID:-$(id -u)}" E2E_GID="${E2E_GID:-$(id -g)}"
  export CALIBAN_ADMIN_TOKEN; CALIBAN_ADMIN_TOKEN="${CALIBAN_ADMIN_TOKEN:-$(envval CALIBAN_ADMIN_TOKEN)}"
  [[ -n "$CALIBAN_ADMIN_TOKEN" ]] || die "CALIBAN_ADMIN_TOKEN is not set in $E2E_STACK_ENV"
  local bind router cp
  bind="$(envval CALIBAN_BIND)"; bind="${bind:-127.0.0.1}"; [[ "$bind" == 0.0.0.0 ]] && bind=127.0.0.1
  router="$(envval CALIBAN_ROUTER_PORT)"; cp="$(envval CALIBAN_CP_PORT)"
  export E2E_ROUTER_URL="${E2E_ROUTER_URL:-http://$bind:${router:-8080}}"
  export E2E_CP_URL="${E2E_CP_URL:-http://$bind:${cp:-8081}}"
  export E2E_COMPOSE_CMD; E2E_COMPOSE_CMD="$(printf '%q ' docker compose -p "$E2E_PROJECT" --env-file "$E2E_STACK_ENV" "${FILES[@]}")"
}

# The stack's own config plus the e2e [routing] table (caliban/auto's embedder and kNN settings).
# The tenant's intents and exemplars are not in it: setup sets them through the admin API.
render_config() {
  [[ -f "$E2E_BASE_CONFIG" ]] || die "base config $E2E_BASE_CONFIG not found"
  if grep -qE '^\[routing(\.|\])' "$E2E_BASE_CONFIG"; then
    die "$E2E_BASE_CONFIG already has a [routing] table; add the e2e's [routing] settings to it by hand"
  fi
  local out="$E2E_WORK_DIR/caliban.toml"
  {
    cat "$E2E_BASE_CONFIG"
    echo
    echo "# ═════════════ appended by e2e/health-triage/e2e.sh (target: $E2E_TARGET) ═════════════"
    echo "[routing]"
    echo "embedding_model = \"$E2E_ROUTING_EMBED_MODEL\""
    case "$E2E_ROUTING_PRESET" in
      mock)
        # Hashed bag-of-words vectors from the scripted model: no instruction prefix, a loose
        # out-of-scope gate, a budget that a Python embedder meets.
        cat <<'EOF'
budget_ms = 500
k = 5
temperature = 0.1
abstain_threshold = 0.5
margin_threshold = 0.0
oos_threshold = 0.25
EOF
        ;;
      qwen3-embedding)
        # core config/open-models.example.toml values for Qwen3-Embedding-0.6B.
        cat <<'EOF'
query_prefix = "Instruct: Given a user request, identify the type of task it asks for\nQuery: "
budget_ms = 150
k = 5
temperature = 0.1
abstain_threshold = 0.6
oos_threshold = 0.64
EOF
        ;;
      *) die "E2E_ROUTING_PRESET must be mock or qwen3-embedding" ;;
    esac
    echo "auto_price_in_per_mtok = 3.0"
    echo "auto_price_out_per_mtok = 12.0"
    if [[ "$E2E_ROUTING_PRESET" == mock ]]; then
      cat <<'EOF'

# The scripted model's embeddings (seeded into the store on the first start of a fresh stack).
[[providers]]
id = "e2e-embed"
kind = "openai_compatible"
base_url = "http://caliban-e2e-model:8000/v1"
trust_tier = "t0_sovereign"
cache_salt = false

[[models]]
id = "e2e/embed"
provider = "e2e-embed"
upstream_model = "e2e-embed"
kind = "embedding"
trust_tier = "t0_sovereign"
licence = "apache-2.0"
context_window = 8192
price_in_per_mtok = 0.0
price_out_per_mtok = 0.0
EOF
    fi
  } > "$out.new"
  chmod 0644 "$out.new"
  CONFIG_CHANGED=0
  if [[ -f "$out" ]] && ! cmp -s "$out" "$out.new"; then CONFIG_CHANGED=1; fi
  # Rewritten in place: a single-file bind mount follows the inode, not the path.
  cat "$out.new" > "$out"
  rm -f "$out.new"
  chmod 0644 "$out"   # Caliban reads it as uid 65532
  log "rendered $out"
}

# ─────────────────────────── MongoDB ───────────────────────────

mongo_init() {
  local root="mongodb://$E2E_MONGO_ROOT_USER:$E2E_MONGO_ROOT_PASSWORD@localhost:27017/?authSource=admin"
  if ! dc exec -T caliban-e2e-mongo mongosh --quiet "$root&replicaSet=rs0" --eval 'rs.status().ok' >/dev/null 2>&1; then
    log "MongoDB: replica set rs0 (primary + analytics secondary) and the root user"
    dc exec -T -e E2E_MONGO_ROOT_USER -e E2E_MONGO_ROOT_PASSWORD caliban-e2e-mongo \
      mongosh --quiet --file /e2e/seed/init-replset.js
  fi
  log "MongoDB: synthetic seed, read-only user, profiling"
  dc exec -T caliban-e2e-mongo mongosh --quiet "$root&replicaSet=rs0" --file /e2e/seed/seed.js
  dc exec -T -e E2E_MONGO_READER_PASSWORD caliban-e2e-mongo mongosh --quiet "$root&replicaSet=rs0" --file /e2e/seed/users.js
  dc exec -T caliban-e2e-mongo-analytics mongosh --quiet \
    "mongodb://$E2E_MONGO_ROOT_USER:$E2E_MONGO_ROOT_PASSWORD@localhost:27017/marketplace?authSource=admin&directConnection=true" \
    --eval 'db.setProfilingLevel(2).ok' >/dev/null
}

# ─────────────────────────── commands ───────────────────────────

up() {
  command -v python3 >/dev/null || die "python3 is required"
  secrets
  render_config
  if [[ "$E2E_TARGET" == local ]]; then
    log "starting the stack (project $E2E_PROJECT)"
    dc up -d --wait --wait-timeout 300
  else
    log "starting the e2e services and recreating caliban with the e2e config (project $E2E_PROJECT)"
    dc up -d --wait --wait-timeout 300 "${E2E_SERVICES[@]}" caliban
  fi
  if [[ "$CONFIG_CHANGED" == 1 ]]; then
    # A bind-mounted file that changed does not recreate the container; core reads it at start.
    log "the rendered config changed: recreating caliban"
    dc up -d --wait --wait-timeout 300 --force-recreate caliban
  fi
  mongo_init
  dc ps
}

down() {
  secrets
  if [[ "$E2E_TARGET" == local ]]; then
    log "removing project $E2E_PROJECT with its volumes"
    dc down -v --remove-orphans
  else
    log "removing the e2e services and volumes, restarting caliban with its own config"
    dc rm -s -f -v "${E2E_SERVICES[@]}"
    docker volume rm caliban-e2e-mongo-data caliban-e2e-mongo-analytics-data 2>/dev/null || true
    docker network rm caliban-e2e-out 2>/dev/null || true
    local base=(-f "$DEPLOY_DIR/compose/docker-compose.yml" "${EXTRA_FILES[@]}")
    docker compose -p "$E2E_PROJECT" --env-file "$E2E_STACK_ENV" "${base[@]}" up -d --wait caliban
  fi
  rm -rf "$E2E_WORK_DIR"
}

cmd="${1:-}"
[[ $# -gt 0 ]] && shift
case "$cmd" in
  up) up ;;
  setup) secrets; python3 "$E2E_DIR/lib/setup.py" "$@" ;;
  run) secrets; python3 "$E2E_DIR/lib/run.py" "$@" ;;
  all) up; python3 "$E2E_DIR/lib/setup.py"; python3 "$E2E_DIR/lib/run.py" ;;
  down) down ;;
  status) secrets; dc ps ;;
  logs) secrets; dc logs --no-color --tail "${TAIL:-200}" "$@" ;;
  exec) [[ $# -gt 0 ]] || die "exec needs a command"; secrets; export E2E_DEPLOY_DIR="$DEPLOY_DIR"; exec "$@" ;;
  ""|-h|--help|help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//' ;;
  *) die "unknown command '$cmd' (up | setup | run | all | down | status | logs | exec)" ;;
esac
