#!/usr/bin/env bash
# Shared helpers for the Caliban AWS testbed bootstrap. Installed by cloud-init at
# /opt/caliban-testbed/common.sh and sourced by the role scripts (gateway.sh, router.sh,
# loadgen.sh, gpu.sh). Settings come from /etc/caliban-testbed/testbed.env (rendered by
# OpenTofu, no secrets); secrets come from SSM parameters at boot.
#
# Logs: /var/log/caliban-testbed.log. Markers: /var/lib/caliban-testbed/{ready,failed}.
# shellcheck shell=bash
set -euo pipefail

# shellcheck source=/dev/null
. /etc/caliban-testbed/testbed.env

export AWS_DEFAULT_REGION="$REGION" AWS_REGION="$REGION"
TB=/opt/caliban-testbed           # testbed files from cloud-init
STACK=/srv/caliban                # deploy tree, models, env files
SRC=/opt/caliban/src              # core, web, deploy clones (build mode)
STATE=/var/lib/caliban-testbed
CALIBAN_IMAGE_REF="caliban/caliban:${CALIBAN_VERSION}"
DEPLOY_DIR=""

mkdir -p "$STACK" "$STATE"

on_exit() {
  local rc=$?
  if [ "$rc" -ne 0 ]; then
    log "FAILED (exit $rc); see /var/log/caliban-testbed.log"
    touch "$STATE/failed"
  fi
}

start_log() {
  exec > >(tee -a /var/log/caliban-testbed.log) 2>&1
  trap on_exit EXIT
  log "bootstrap $ROLE ($HOST_KEY) tier=$TIER connected=$CONNECTED image=$IMAGE_SOURCE ip=$SELF_IP"
}

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] caliban-testbed: $*"; }
die() { log "error: $*"; exit 1; }

retry() {
  local n=0
  until "$@"; do
    n=$((n + 1))
    [ "$n" -lt 5 ] || return 1
    log "retry $n: $*" >&2
    sleep $((n * 10))
  done
}

# shellcheck source=/dev/null
os_id() { (. /etc/os-release && echo "$ID"); }
uname_arch() { uname -m; } # aarch64 | x86_64

# Secret from SSM Parameter Store (SecureString, decrypted with the aws/ssm managed key).
param() {
  retry aws ssm get-parameter --with-decryption --name "$PARAM_PREFIX/$1" \
    --query Parameter.Value --output text
}

s3get() { retry aws s3 cp --only-show-errors "s3://$BUCKET/$1" "$2"; }
s3exists() { aws s3api head-object --bucket "$BUCKET" --key "$1" >/dev/null 2>&1; }

# ── OS TTL timer: power off TTL_HOURS after every boot (on-demand hosts only) ──
install_ttl_timer() {
  [ "$OS_TTL_TIMER" = true ] || { log "OS TTL timer skipped (spot host; the scheduler stops it)"; return 0; }
  cat > /etc/systemd/system/caliban-ttl.service <<'EOF'
[Unit]
Description=Caliban testbed TTL: power off (instance stops)

[Service]
Type=oneshot
ExecStart=/usr/bin/systemctl poweroff
EOF
  cat > /etc/systemd/system/caliban-ttl.timer <<EOF
[Unit]
Description=Caliban testbed TTL: power off ${TTL_HOURS}h after boot

[Timer]
OnBootSec=${TTL_HOURS}h
Unit=caliban-ttl.service

[Install]
WantedBy=timers.target
EOF
  systemctl daemon-reload
  systemctl enable --now caliban-ttl.timer
  log "OS TTL timer armed: poweroff ${TTL_HOURS}h after boot (sudo systemctl stop caliban-ttl.timer to extend)"
}

# ── Docker ──
install_docker() {
  case "$(os_id)" in
    amzn)
      # AL2023 repos are served from S3, so this also works in the isolated subnet
      # (s3_endpoint_allow_al2023_repos = true).
      retry dnf install -y -q docker tar gzip openssl git
      ;;
    ubuntu)
      command -v docker >/dev/null || die "Docker is missing from the GPU AMI"
      if ! command -v aws >/dev/null; then
        [ "$CONNECTED" = true ] || die "AWS CLI is missing from the GPU AMI"
        retry snap install aws-cli --classic
      fi
      ;;
    *) die "unsupported OS $(os_id)" ;;
  esac
  systemctl enable --now docker
  usermod -aG docker ssm-user 2>/dev/null || true
}

install_plugin() { # $1 = plugin name, $2 = URL, $3 = checksum URL ("" = none), $4 = file name in checksum list
  local dir=/usr/local/lib/docker/cli-plugins tmp
  tmp="$(mktemp -d)"
  mkdir -p "$dir"
  retry curl -fsSL -o "$tmp/$4" "$2"
  if [ -n "$3" ]; then
    retry curl -fsSL -o "$tmp/sums" "$3"
    (cd "$tmp" && grep -E "[ *]$4\$" sums | sha256sum -c -) || die "checksum mismatch for $4"
  fi
  install -m 0755 "$tmp/$4" "$dir/$1"
  rm -rf "$tmp"
}

install_compose() {
  if docker compose version >/dev/null 2>&1; then return 0; fi
  local a f
  a="$(uname_arch)"
  if [ "$CONNECTED" = true ]; then
    f="docker-compose-linux-$a"
    install_plugin docker-compose \
      "https://github.com/docker/compose/releases/download/$COMPOSE_VERSION/$f" \
      "https://github.com/docker/compose/releases/download/$COMPOSE_VERSION/$f.sha256" "$f"
  else
    mkdir -p /usr/local/lib/docker/cli-plugins
    s3get "$TOOLS_PREFIX/docker-compose" /usr/local/lib/docker/cli-plugins/docker-compose
    chmod 0755 /usr/local/lib/docker/cli-plugins/docker-compose
  fi
  docker compose version
}

install_buildx() {
  if docker buildx version >/dev/null 2>&1; then return 0; fi
  [ "$CONNECTED" = true ] || die "buildx is needed for image_source=build, which needs internet access"
  local f="buildx-$BUILDX_VERSION.linux-$DOCKER_ARCH"
  install_plugin docker-buildx \
    "https://github.com/docker/buildx/releases/download/$BUILDX_VERSION/$f" \
    "https://github.com/docker/buildx/releases/download/$BUILDX_VERSION/checksums.txt" "$f"
  docker buildx version
}

# ── Source checkouts (build mode) ──
clone() { # $1 = repo, $2 = ref
  local dest="$SRC/$1"
  rm -rf "$dest"
  retry git clone --quiet --depth 1 --branch "$2" "https://github.com/$GITHUB_ORG/$1" "$dest"
  log "cloned $1@$2 ($(git -C "$dest" rev-parse --short HEAD))"
}

# Deploy tree only (compose files, bundle.sh), e.g. for a GPU host that runs models only.
fetch_deploy_tree() {
  mkdir -p "$SRC"
  clone deploy "$DEPLOY_REF"
  DEPLOY_DIR="$SRC/deploy"
}

build_image() {
  install_buildx
  mkdir -p "$SRC"
  clone core "$CORE_REF"
  clone web "$WEB_REF"
  [ -d "$SRC/deploy" ] || clone deploy "$DEPLOY_REF"
  DEPLOY_DIR="$SRC/deploy"
  log "building $CALIBAN_IMAGE_REF (this takes a while)"
  CALIBAN_SRC="$SRC" "$DEPLOY_DIR/images/build.sh" --version "$CALIBAN_VERSION"
}

# ── Offline bundle (s3 mode) ──
# Arguments are passed on to load.sh (e.g. --models-dest DIR). The bundle is verified with the testbed's
# own copy of load.sh (from this repo, via cloud-init), not with the one inside the bundle.
load_bundle() {
  local work=/var/tmp/caliban-bundle name args=()
  name="$(basename "$BUNDLE_KEY")"
  rm -rf "$work" && mkdir -p "$work"
  log "fetching s3://$BUCKET/$BUNDLE_KEY"
  s3get "$BUNDLE_KEY" "$work/$name"
  if s3exists "$BUNDLE_KEY.sha256"; then s3get "$BUNDLE_KEY.sha256" "$work/$name.sha256"; fi

  case "$BUNDLE_VERIFY" in
    none) args+=(--allow-unsigned) ;;
    minisign | cosign)
      if ! command -v "$BUNDLE_VERIFY" >/dev/null; then
        s3get "$TOOLS_PREFIX/$BUNDLE_VERIFY" "/usr/local/bin/$BUNDLE_VERIFY"
        chmod 0755 "/usr/local/bin/$BUNDLE_VERIFY"
      fi
      [ -f /etc/caliban-testbed/bundle.pub ] || die "bundle_pubkey is not set"
      args+=(--pubkey /etc/caliban-testbed/bundle.pub)
      ;;
  esac

  "$TB/load.sh" "$work/$name" "${args[@]}" --workdir "$work/x" --env-out "$STACK/images.env" "$@"

  rm -rf "$STACK/deploy"
  cp -R "$work/x/caliban-bundle-$CALIBAN_VERSION/deploy" "$STACK/deploy"
  DEPLOY_DIR="$STACK/deploy"
  rm -rf "$work"
  log "bundle loaded; deploy tree at $DEPLOY_DIR"
}

# Image (+ deploy tree) for a host that runs Caliban. Arguments go to load_bundle.
# shellcheck disable=SC2120 # arguments are optional
fetch_caliban() {
  case "$IMAGE_SOURCE" in
    build) build_image ;;
    s3) load_bundle "$@" ;;
    *) die "unknown IMAGE_SOURCE $IMAGE_SOURCE" ;;
  esac
  docker image inspect "$CALIBAN_IMAGE_REF" >/dev/null || die "image $CALIBAN_IMAGE_REF not present"
}

# ── compose .env ──
set_env() { # $1 = file, $2 = key, $3 = value (no '|' or newline)
  if grep -qE "^$2=" "$1"; then
    sed -i "s|^$2=.*|$2=$3|" "$1"
  else
    printf '%s=%s\n' "$2" "$3" >> "$1"
  fi
}

# Base .env for the compose stack: secrets from SSM, ports on all interfaces (the security
# groups admit only the VPC), PII NER off (its artifact is fetched separately; see README).
write_compose_env() {
  local env="$DEPLOY_DIR/compose/.env"
  cp "$DEPLOY_DIR/compose/.env.example" "$env"
  chmod 600 "$env"
  set_env "$env" CALIBAN_ADMIN_TOKEN "$(param admin-token)"
  set_env "$env" CALIBAN_KEK "$(param kek)"
  set_env "$env" POSTGRES_PASSWORD "$(param postgres-password)"
  set_env "$env" VALKEY_PASSWORD "$(param valkey-password)"
  set_env "$env" CALIBAN_VERSION "$CALIBAN_VERSION"
  set_env "$env" CALIBAN_BIND 0.0.0.0
  set_env "$env" CALIBAN_PII_NER_DIR ""
  set_env "$env" MODELS_DIR "$STACK/models"
  set_env "$env" TESTBED_BIND "$SELF_IP"
  if [ -f "$STACK/images.env" ]; then
    grep -vE '^(#|MODELS_DIR=)' "$STACK/images.env" >> "$env" || true
  fi
  mkdir -p "$STACK/models/pii"
}

# Raw base64 keys from the Ed25519 PEMs (PKCS#8 private: last 32 DER bytes = seed;
# SPKI public: last 32 DER bytes = key).
snapshot_seed_b64() { param snapshot-signing-pem | openssl pkey -outform DER | tail -c 32 | base64 -w0; }
snapshot_pub_b64() {
  aws ssm get-parameter --name "$PARAM_PREFIX/snapshot-public-pem" --query Parameter.Value --output text |
    openssl pkey -pubin -outform DER | tail -c 32 | base64 -w0
}

# Shell profile for SSM sessions: where things are.
write_profile() {
  cat > /etc/profile.d/caliban-testbed.sh <<EOF
export AWS_DEFAULT_REGION=$REGION
export CALIBAN_TESTBED_BUCKET=$BUCKET
export GATEWAY_IP=$GATEWAY_IP GPU_IP=$GPU_IP LOADGEN_IP=$LOADGEN_IP ROUTER_IPS="$ROUTER_IPS"
export CALIBAN_DEPLOY_DIR=$DEPLOY_DIR
EOF
}

mark_ready() {
  write_profile
  touch "$STATE/ready"
  log "ready"
}
