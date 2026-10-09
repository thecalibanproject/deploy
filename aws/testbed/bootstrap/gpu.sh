#!/usr/bin/env bash
# GPU host (Deep Learning Base AMI, Ubuntu): the compose model profiles (default
# qwen3-large embeddings reranker = the README's 1 x 48 GB tier), published on the private IP
# by compose.testbed-models.yml. Weights come from Hugging Face (connected) or from the S3
# bundle (isolated). With GPU_RUN_CALIBAN=true, Caliban and the datastores run here too.
set -euo pipefail
# shellcheck source=common.sh
. /opt/caliban-testbed/common.sh
start_log

install_ttl_timer
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv
install_docker
docker info --format '{{json .Runtimes}}' | grep -q nvidia || log "warning: nvidia runtime not registered with Docker"
if [ "$CONNECTED" = true ]; then
  export DEBIAN_FRONTEND=noninteractive
  retry apt-get update -qq
  retry apt-get install -y -qq python3-venv git curl
fi
install_compose

models="$STACK/models"
mkdir -p "$models"
if [ "$IMAGE_SOURCE" = s3 ]; then
  # Images (vLLM, TEI, and the core images) and weights from the bundle.
  load_bundle --models-dest "$models"
else
  if [ "$GPU_RUN_CALIBAN" = true ]; then build_image; else fetch_deploy_tree; fi
  python3 -m venv /opt/hf
  retry /opt/hf/bin/pip install -q -U huggingface_hub
  export PATH=/opt/hf/bin:$PATH HF_HUB_DISABLE_TELEMETRY=1
  fetch_args=()
  read -r -a fetch_args <<< "$GPU_FETCH_ARGS"
  [ "$HF_ALLOW_UNPINNED" = true ] && fetch_args+=(--allow-unpinned)
  log "fetching weights: bundle.sh fetch ${fetch_args[*]}"
  "$DEPLOY_DIR/airgap/bundle.sh" fetch "${fetch_args[@]}" --models-dir "$models"
fi

write_compose_env
cd "$DEPLOY_DIR/compose"

profile_args=()
for p in $GPU_PROFILES; do profile_args+=(--profile "$p"); done
files=(-f docker-compose.yml -f "$TB/compose.testbed-models.yml")

services=()
if [ "$GPU_RUN_CALIBAN" != true ]; then
  # Model services only: everything the profiles enable except Caliban and its datastores.
  while read -r s; do
    case "$s" in caliban | postgres | qdrant | valkey | "") ;; *) services+=("$s") ;; esac
  done < <(docker compose "${files[@]}" "${profile_args[@]}" config --services)
fi

# vLLM needs a few minutes to load 31 GB of weights; do not block cloud-init on it.
docker compose "${files[@]}" "${profile_args[@]}" up -d "${services[@]}"
docker compose "${files[@]}" "${profile_args[@]}" ps
log "model servers starting; watch with: cd $DEPLOY_DIR/compose && sudo docker compose ps"
mark_ready
