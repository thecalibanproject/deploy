#!/usr/bin/env bash
# Verify and load a Caliban offline bundle on the AIR-GAPPED side.
#
#   airgap/load.sh caliban-bundle-<v>.tar [--pubkey FILE] [--allow-unsigned]
#                  [--registry HOST[:PORT]] [--models-dest DIR] [--workdir DIR] [--env-out FILE]
#
#   1. checks <bundle>.tar.sha256 if it sits next to the tarball
#   2. extracts, verifies MANIFEST.sha256's signature (cosign .sig or minisign .minisig)
#      with the public key you received OUT OF BAND (never trust a key shipped in the bundle)
#   3. verifies every file against MANIFEST.sha256
#   4. docker load's the images; with --registry also tags + pushes them to your mirror
#      (path kept: <registry>/library/postgres:..., <registry>/caliban/caliban:...)
#   5. copies model weights to --models-dest and writes compose env overrides (--env-out)
#
# Needs: tar, docker, sha256sum or shasum; cosign or minisign to verify signatures.
set -euo pipefail

BUNDLE=""
PUBKEY=""
ALLOW_UNSIGNED=false
REGISTRY=""
MODELS_DEST=""
WORKDIR=""
ENV_OUT=""

die() { echo "load.sh: error: $*" >&2; exit 1; }
log() { echo ">> $*"; }
usage() { sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pubkey)         PUBKEY="$2"; shift 2 ;;
    --allow-unsigned) ALLOW_UNSIGNED=true; shift ;;
    --registry)       REGISTRY="${2%/}"; shift 2 ;;
    --models-dest)    MODELS_DEST="$2"; shift 2 ;;
    --workdir)        WORKDIR="$2"; shift 2 ;;
    --env-out)        ENV_OUT="$2"; shift 2 ;;
    -h|--help)        usage 0 ;;
    -*) die "unknown option: $1" ;;
    *)  [[ -z "$BUNDLE" ]] || die "only one bundle"; BUNDLE="$1"; shift ;;
  esac
done
[[ -n "$BUNDLE" ]] || usage 1
command -v docker >/dev/null || die "docker not found"

if command -v sha256sum >/dev/null; then SHA256=(sha256sum)
elif command -v shasum >/dev/null; then SHA256=(shasum -a 256)
else die "need sha256sum or shasum"; fi

# ───────────── 1. outer checksum + extract ─────────────
if [[ -f "$BUNDLE" ]]; then
  if [[ -f "$BUNDLE.sha256" ]]; then
    log "checking $(basename "$BUNDLE").sha256"
    ( cd "$(dirname "$BUNDLE")" && "${SHA256[@]}" -c "$(basename "$BUNDLE").sha256" ) \
      || die "tarball checksum mismatch"
  fi
  WORKDIR="${WORKDIR:-$(dirname "$BUNDLE")}"
  mkdir -p "$WORKDIR"
  top="$(tar -tf "$BUNDLE" | head -1 | cut -d/ -f1)"
  [[ "$top" =~ ^caliban-bundle-[A-Za-z0-9._+-]+$ ]] || die "unexpected bundle layout ($top)"
  # Refuse absolute paths / traversal before extracting.
  if tar -tf "$BUNDLE" | grep -Eq '(^/|(^|/)\.\.(/|$))'; then die "unsafe paths in bundle"; fi
  log "extracting to $WORKDIR/$top"
  tar -C "$WORKDIR" -xf "$BUNDLE"
  DIR="$WORKDIR/$top"
elif [[ -d "$BUNDLE" ]]; then
  DIR="$BUNDLE"
else
  die "not found: $BUNDLE"
fi
DIR="$(cd "$DIR" && pwd)"
VERSION="$(cat "$DIR/VERSION")"
log "bundle version $VERSION"

# ───────────── 2. signature ─────────────
cd "$DIR"
if [[ -f MANIFEST.sha256.sig ]]; then
  command -v cosign >/dev/null || die "bundle is cosign-signed but cosign is not installed"
  [[ -f "$PUBKEY" ]] || die "--pubkey <cosign.pub> required"
  log "verifying cosign signature"
  cosign verify-blob --key "$PUBKEY" --signature MANIFEST.sha256.sig \
    --insecure-ignore-tlog=true MANIFEST.sha256 || die "SIGNATURE INVALID"
elif [[ -f MANIFEST.sha256.minisig ]]; then
  command -v minisign >/dev/null || die "bundle is minisign-signed but minisign is not installed"
  [[ -f "$PUBKEY" ]] || die "--pubkey <minisign.pub> required"
  log "verifying minisign signature"
  minisign -V -p "$PUBKEY" -m MANIFEST.sha256 -x MANIFEST.sha256.minisig || die "SIGNATURE INVALID"
elif [[ "$ALLOW_UNSIGNED" == true ]]; then
  echo "   WARNING: bundle is unsigned; continuing because --allow-unsigned was given" >&2
else
  die "bundle is unsigned (pass --allow-unsigned to accept it anyway)"
fi

# ───────────── 3. file checksums ─────────────
log "verifying MANIFEST.sha256 (this reads every file, model weights included)"
"${SHA256[@]}" -c --quiet MANIFEST.sha256 || die "checksum mismatch"
# Anything present but not listed in the manifest is suspicious.
extra="$(comm -13 \
  <(awk '{p=$2; sub(/^\*/, "", p); print p}' MANIFEST.sha256 | LC_ALL=C sort) \
  <(find . -type f ! -name 'MANIFEST.sha256*' | LC_ALL=C sort))"
[[ -z "$extra" ]] || die "files not covered by the manifest:"$'\n'"$extra"

# ───────────── 4. images ─────────────
# Strip the registry host and normalise Docker Hub short names, keeping the path:
#   postgres:17 -> library/postgres:17 ; ghcr.io/ggml-org/llama.cpp:x -> ggml-org/llama.cpp:x
mirror_path() {
  local ref="$1" first="${1%%/*}"
  if [[ "$ref" == */* && ( "$first" == *.* || "$first" == *:* || "$first" == localhost ) ]]; then
    ref="${ref#*/}"
  fi
  [[ "$ref" == */* ]] || ref="library/$ref"
  echo "$ref"
}

env_lines=()
while read -r var ref file _id _digest; do
  [[ -n "${var:-}" ]] || continue
  log "docker load $ref"
  docker load -q -i "$file" >/dev/null
  if [[ -n "$REGISTRY" ]]; then
    target="$REGISTRY/$(mirror_path "$ref")"
    log "  push $target"
    docker tag "$ref" "$target"
    docker push -q "$target" >/dev/null
    pushed="$(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$target" \
      | grep -F "${target%:*}@" | head -1 || true)"
    [[ -n "$pushed" ]] && echo "     digest: $pushed"
  else
    target="$ref"
  fi
  if [[ "$var" == CALIBAN_IMAGE ]]; then
    env_lines+=("CALIBAN_IMAGE=${target%:*}" "CALIBAN_VERSION=${target##*:}")
  else
    env_lines+=("$var=$target")
  fi
done < images.txt

# ───────────── 5. models + env overrides ─────────────
if [[ -n "$MODELS_DEST" ]]; then
  mkdir -p "$MODELS_DEST"
  for d in models/*/; do
    [[ -d "$d" ]] || continue
    name="$(basename "$d")"
    log "model $name -> $MODELS_DEST/$name"
    rm -rf "${MODELS_DEST:?}/$name.tmp"
    cp -R "$d" "$MODELS_DEST/$name.tmp"
    rm -rf "${MODELS_DEST:?}/$name"
    mv "$MODELS_DEST/$name.tmp" "$MODELS_DEST/$name"
  done
  env_lines+=("MODELS_DIR=$(cd "$MODELS_DEST" && pwd)")
fi

if [[ -n "$ENV_OUT" ]]; then
  {
    echo "# Written by airgap/load.sh for bundle $VERSION on $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf '%s\n' ${env_lines[@]+"${env_lines[@]}"}
  } > "$ENV_OUT"
  log "compose overrides written to $ENV_OUT (append to compose/.env)"
fi

echo
echo "Loaded Caliban bundle $VERSION from $DIR"
printf '  %s\n' ${env_lines[@]+"${env_lines[@]}"}
if [[ -n "$REGISTRY" ]]; then
  echo
  echo "Helm:  --set global.imageRegistry=$REGISTRY   (pin image.digest to the caliban digest printed above)"
fi
echo "Deploy assets: $DIR/deploy   (see deploy/README.md)"
