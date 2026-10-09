#!/usr/bin/env bash
# Gateway host: Caliban standalone (router + control plane) with Postgres, Valkey and Qdrant
# from compose/docker-compose.yml, plus compose.testbed-gateway.yml (split-mode keys,
# datastore ports on the private IP, model base_urls pointed at the GPU host when it exists).
set -euo pipefail
# shellcheck source=common.sh
. /opt/caliban-testbed/common.sh
start_log

install_ttl_timer
install_docker
install_compose
fetch_caliban

env_file="$DEPLOY_DIR/compose/.env"
write_compose_env
set_env "$env_file" CALIBAN_SNAPSHOT_SIGNING_KEY "$(snapshot_seed_b64)"
set_env "$env_file" CALIBAN_ROUTER_TOKEN "$(param router-token)"
set_env "$env_file" TESTBED_CALIBAN_TOML "$TB/caliban.toml"

# Config: the deploy repo's file, with the model pools on the GPU host when there is one.
# Postgres is the source of truth after the first start, so this must be right before it.
cp "$DEPLOY_DIR/compose/config/caliban.toml" "$TB/caliban.toml"
if [ "$GATEWAY_USE_GPU_MODELS" = true ] && [ -n "$GPU_IP" ]; then
  sed -i \
    -e "s|http://qwen3-small:8000/v1|http://$GPU_IP:8000/v1|" \
    -e "s|http://qwen3-large:8000/v1|http://$GPU_IP:8000/v1|" \
    -e "s|http://qwen3-moe:8000/v1|http://$GPU_IP:8000/v1|" \
    -e "s|http://gpt-oss:8000/v1|http://$GPU_IP:8003/v1|" \
    -e "s|http://embed:8000/v1|http://$GPU_IP:8001/v1|" \
    -e "s|http://rerank:8000/v1|http://$GPU_IP:8002/v1|" \
    "$TB/caliban.toml"
  log "model providers point at the GPU host $GPU_IP (8000 chat, 8001 embed, 8002 rerank)"
fi
chmod 0644 "$TB/caliban.toml"

cd "$DEPLOY_DIR/compose"
docker compose -f docker-compose.yml -f "$TB/compose.testbed-gateway.yml" up -d --wait --wait-timeout 600
docker compose ps
mark_ready
