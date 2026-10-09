#!/usr/bin/env bash
# Build (and optionally push) the Caliban image, or prepare offline build inputs.
#
#   images/build.sh [build] [--version V] [--image NAME] [--registry HOST] [--platform P]
#                           [--offline] [--push] [--sbom]
#   images/build.sh vendor          # connected machine: vendor crates, pnpm store, pnpm release
#
# The build context is the directory that contains core/ and web/ (default: two levels up
# from this script, i.e. ~/caliban). Override with CALIBAN_SRC=/path/to/caliban.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="${CALIBAN_SRC:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
DOCKERFILE="$SCRIPT_DIR/caliban.Dockerfile"

VERSION="${VERSION:-}"
IMAGE="${IMAGE:-caliban/caliban}"
REGISTRY="${REGISTRY:-}"
PLATFORM="${PLATFORM:-}"
OFFLINE=false
PUSH=false
SBOM=false
EXTRA_ARGS=()

usage() { sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
die() { echo "build.sh: $*" >&2; exit 1; }

cmd="build"
if [[ $# -gt 0 && "$1" != -* ]]; then cmd="$1"; shift; fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version)  VERSION="$2"; shift 2 ;;
    --image)    IMAGE="$2"; shift 2 ;;
    --registry) REGISTRY="$2"; shift 2 ;;
    --platform) PLATFORM="$2"; shift 2 ;;
    --offline)  OFFLINE=true; shift ;;
    --push)     PUSH=true; shift ;;
    --sbom)     SBOM=true; shift ;;
    --build-arg) EXTRA_ARGS+=(--build-arg "$2"); shift 2 ;;
    -h|--help)  usage 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

[[ -d "$SRC/core" ]] || die "core/ not found under $SRC (set CALIBAN_SRC)"
[[ -d "$SRC/web" ]]  || die "web/ not found under $SRC (set CALIBAN_SRC)"

vendor() {
  command -v cargo >/dev/null || die "cargo is required for 'vendor'"
  command -v pnpm  >/dev/null || die "pnpm is required for 'vendor'"
  command -v corepack >/dev/null || die "corepack is required for 'vendor'"

  echo ">> cargo vendor -> core/vendor"
  ( cd "$SRC/core" && cargo vendor --locked --versioned-dirs vendor > .cargo-vendor-config.toml )

  echo ">> pnpm fetch -> web/.pnpm-store"
  ( cd "$SRC/web" && pnpm fetch --store-dir .pnpm-store )

  echo ">> corepack pack -> web/.corepack/corepack.tgz"
  ( cd "$SRC/web" && mkdir -p .corepack && corepack pack -o .corepack/corepack.tgz )

  cat <<EOF
Offline inputs ready. Do NOT commit them; add to the source repos' .gitignore:
  core/vendor/  core/.cargo-vendor-config.toml  web/.pnpm-store/  web/.corepack/
Then, on the offline builder:  images/build.sh --offline --version <v>
EOF
}

build() {
  if [[ -z "$VERSION" ]]; then
    VERSION="$(git -C "$SRC/core" describe --tags --always --dirty 2>/dev/null || echo 0.0.0-dev)"
  fi
  local revision created ref
  revision="$(git -C "$SRC/core" rev-parse HEAD 2>/dev/null || echo unknown)"
  # Reproducible timestamp: last core commit time, else epoch.
  created="$(git -C "$SRC/core" log -1 --format=%cI 2>/dev/null || echo 1970-01-01T00:00:00Z)"
  ref="${REGISTRY:+$REGISTRY/}$IMAGE:$VERSION"

  local args=(
    -f "$DOCKERFILE"
    -t "$ref"
    --build-arg "VERSION=$VERSION"
    --build-arg "REVISION=$revision"
    --build-arg "CREATED=$created"
    --build-arg "OFFLINE=$OFFLINE"
  )
  [[ -n "$PLATFORM" ]] && args+=(--platform "$PLATFORM")
  [[ "$OFFLINE" == true ]] && args+=(--network=none --pull=false)
  if [[ ${#EXTRA_ARGS[@]} -gt 0 ]]; then args+=("${EXTRA_ARGS[@]}"); fi

  if [[ "$PUSH" == true ]]; then
    args+=(--push)
    # SBOM + provenance attestations are attached to the pushed manifest.
    [[ "$SBOM" == true ]] && args+=(--sbom=true --provenance=mode=max)
  else
    args+=(--load)
    [[ "$SBOM" == true ]] && echo "note: --sbom only applies with --push (attestations need a registry)" >&2
  fi

  echo ">> building $ref (offline=$OFFLINE) from $SRC"
  SOURCE_DATE_EPOCH="$(git -C "$SRC/core" log -1 --format=%ct 2>/dev/null || echo 0)" \
    docker buildx build "${args[@]}" "$SRC"
  echo ">> done: $ref"
}

case "$cmd" in
  build)  build ;;
  vendor) vendor ;;
  *) die "unknown command: $cmd (build|vendor)" ;;
esac
