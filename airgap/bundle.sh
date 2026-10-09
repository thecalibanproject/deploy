#!/usr/bin/env bash
# Build a signed, offline Caliban bundle on a CONNECTED machine, or fetch the model weights for it.
#
#   airgap/bundle.sh fetch [--profile P] [--model ID]... [--models-dir DIR]
#                          [--allow-licence ID]... [--allow-unpinned] [--pin]
#   airgap/bundle.sh [bundle] --version 0.1.0 [--profile P] [--model ID]... [--models-dir DIR]
#                    [--platform linux/amd64] [--out DIR] [--sign none|cosign|minisign]
#                    [--key FILE] [--skip-models] [--allow-licence ID]...
#
#   --profile P   all (default) | none | comma-separated compose profiles: qwen3-small,
#                 qwen3-large, qwen3-moe, gpt-oss, embeddings, reranker, cpu, ollama, k8s.
#                 Legacy aliases: gpu = qwen3-small,gpt-oss,embeddings,reranker.
#   --model ID    select this models.lock entry even if it has bundle: false (repeatable).
#                 Without an explicit --profile, ONLY the listed models are selected.
#
# fetch   downloads the selected weights from models.lock.yaml into --models-dir with
#         `hf download --revision <pinned commit>` (or `huggingface-cli download`), applies the
#         licence gate, computes each directory's tree sha256 and records it in
#         <models-dir>/models.sha256. --pin also writes it into models.lock.yaml.
#         Refuses "TODO-pin-commit" revisions unless --allow-unpinned (then fetches main).
# bundle  (default) produces <out>/caliban-bundle-<version>.tar (+ .tar.sha256) containing:
#         VERSION, images/*.tar, images.txt, models/<dir>/..., deploy/ (compose, helm, scripts),
#         MANIFEST.sha256 and, if signed, MANIFEST.sha256.sig (cosign) or .minisig (minisign).
#
# Prerequisites: tar, sha256sum or shasum; docker and cosign/minisign for `bundle`;
# hf (pip install -U huggingface_hub) and curl for `fetch`.
# The caliban image must exist locally (images/build.sh) or be pullable.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

MODE="bundle"
VERSION=""
PROFILE="all"
PROFILE_SET=false
MODELS_DIR="${MODELS_DIR:-$REPO_DIR/compose/models}"
PLATFORM="linux/amd64"
OUT="$REPO_DIR/dist"
SIGN="none"
KEY=""
SKIP_MODELS=false
ALLOW_UNPINNED=false
PIN=false
EXTRA_MODELS=()
ALLOWED_LICENCES=(apache-2.0 mit)
IMAGES_LOCK="${IMAGES_LOCK:-$SCRIPT_DIR/images.lock}"
MODELS_LOCK="${MODELS_LOCK:-$SCRIPT_DIR/models.lock.yaml}"

die()  { echo "bundle.sh: error: $*" >&2; exit 1; }
log()  { echo ">> $*"; }
usage() { sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

case "${1:-}" in
  fetch)  MODE="fetch"; shift ;;
  bundle) MODE="bundle"; shift ;;
esac

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version)       VERSION="$2"; shift 2 ;;
    --profile)       PROFILE="$2"; PROFILE_SET=true; shift 2 ;;
    --model)         EXTRA_MODELS+=("$2"); shift 2 ;;
    --allow-unpinned) ALLOW_UNPINNED=true; shift ;;
    --pin)           PIN=true; shift ;;
    --models-dir)    MODELS_DIR="$2"; shift 2 ;;
    --platform)      PLATFORM="$2"; shift 2 ;;
    --out)           OUT="$2"; shift 2 ;;
    --sign)          SIGN="$2"; shift 2 ;;
    --key)           KEY="$2"; shift 2 ;;
    --skip-models)   SKIP_MODELS=true; shift ;;
    --allow-licence) ALLOWED_LICENCES+=("$2"); shift 2 ;;
    -h|--help)       usage 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

KNOWN_PROFILES=" qwen3-small qwen3-large qwen3-moe gpt-oss embeddings reranker cpu ollama k8s "
case "$PROFILE" in
  all|none) ;;
  gpu) PROFILE="qwen3-small,gpt-oss,embeddings,reranker" ;;
  *)
    IFS=',' read -ra _profiles <<< "$PROFILE"
    for _p in "${_profiles[@]}"; do
      [[ "$KNOWN_PROFILES" == *" $_p "* ]] || die "unknown profile '$_p' (known:$KNOWN_PROFILES)"
    done ;;
esac
# --model without --profile: only the listed models (no images beyond core either).
if [[ ${#EXTRA_MODELS[@]} -gt 0 && "$PROFILE_SET" == false ]]; then PROFILE="none"; fi

if [[ "$MODE" == bundle ]]; then
  [[ -n "$VERSION" ]] || die "--version is required"
  [[ "$VERSION" =~ ^[A-Za-z0-9._+-]+$ ]] || die "invalid version: $VERSION"
  case "$SIGN" in
    none) ;;
    cosign)   command -v cosign   >/dev/null || die "cosign not found"; [[ -f "$KEY" ]] || die "--key <cosign.key> required" ;;
    minisign) command -v minisign >/dev/null || die "minisign not found"; [[ -f "$KEY" ]] || die "--key <minisign.key> required" ;;
    *) die "--sign must be none|cosign|minisign" ;;
  esac
  command -v docker >/dev/null || die "docker not found"
fi

if command -v sha256sum >/dev/null; then SHA256=(sha256sum)
elif command -v shasum >/dev/null; then SHA256=(shasum -a 256)
else die "need sha256sum or shasum"; fi

# $1 = comma-separated groups of an images.lock line or a models.lock entry's `profiles`.
group_selected() {
  local g
  [[ "$1" == core ]] && return 0
  [[ "$PROFILE" == all ]] && return 0
  [[ "$PROFILE" == none ]] && return 1
  IFS=',' read -ra _groups <<< "$1"
  for g in "${_groups[@]}"; do
    [[ ",$PROFILE," == *",$g,"* ]] && return 0
  done
  return 1
}

model_requested() {  # $1 = model id given with --model?
  local m
  for m in ${EXTRA_MODELS[@]+"${EXTRA_MODELS[@]}"}; do [[ "$m" == "$1" ]] && return 0; done
  return 1
}

model_selected() {  # $1 id, $2 profiles, $3 bundle flag
  model_requested "$1" && return 0
  [[ "$3" == true ]] && group_selected "$2"
}

licence_allowed() {
  local l
  for l in "${ALLOWED_LICENCES[@]}"; do [[ "$1" == "$l" ]] && return 0; done
  return 1
}

# Deterministic hash of a directory tree: sha256 over the sorted "hash  ./path" listing.
# ./.cache/ (hf download bookkeeping) is excluded.
tree_hash() {
  ( cd "$1" && find . -type f ! -path './.cache/*' -print0 | LC_ALL=C sort -z | xargs -0 "${SHA256[@]}" ) \
    | "${SHA256[@]}" | awk '{print $1}'
}

# models.lock.yaml -> "id|dir|licence|profiles|bundle|sha256|source|revision|include|exclude"
# (flat YAML, see file header)
parse_models_lock() {
  awk '
    function flush() {
      if (id != "") print id "|" dir "|" lic "|" prof "|" bun "|" sha "|" src "|" rev "|" inc "|" exc
      id=dir=lic=prof=bun=sha=src=rev=inc=exc=""
    }
    function val(s) { sub(/^[^:]*:[ \t]*/, "", s); sub(/[ \t]+#.*$/, "", s); gsub(/^"|"$/, "", s); return s }
    /^  - id:/        { flush(); id = val($0); next }
    /^    dir:/       { dir  = val($0); next }
    /^    licence:/   { lic  = val($0); next }
    /^    profiles:/  { prof = val($0); next }
    /^    bundle:/    { bun  = val($0); next }
    /^    sha256:/    { sha  = val($0); next }
    /^    source:/    { src  = val($0); next }
    /^    revision:/  { rev  = val($0); next }
    /^    include:/   { inc  = val($0); next }
    /^    exclude:/   { exc  = val($0); next }
    END { flush() }
  ' "$MODELS_LOCK"
}

# Rewrite the sha256 field of one models.lock.yaml entry in place.
pin_sha() {  # $1 id, $2 sha
  local tmp
  tmp="$(mktemp)"
  awk -v id="$1" -v sha="$2" '
    /^  - id:/ { cur = $0; sub(/^  - id:[ \t]*/, "", cur); sub(/[ \t]+#.*$/, "", cur); gsub(/^"|"$/, "", cur) }
    /^    sha256:/ && cur == id { print "    sha256: \"" sha "\""; next }
    { print }
  ' "$MODELS_LOCK" > "$tmp"
  cat "$tmp" > "$MODELS_LOCK"
  rm -f "$tmp"
}

# ───────────── fetch (connected side): download pinned weights ─────────────
fetch_models() {
  local hf_bin="" id dir lic prof bun sha src rev inc exc dest got n=0 g repo name out req=""
  local -a args globs
  if command -v hf >/dev/null; then hf_bin=hf
  elif command -v huggingface-cli >/dev/null; then hf_bin=huggingface-cli
  fi
  mkdir -p "$MODELS_DIR"
  MODELS_DIR="$(cd "$MODELS_DIR" && pwd)"
  [[ ${#EXTRA_MODELS[@]} -eq 0 ]] || req=", models: ${EXTRA_MODELS[*]}"
  log "fetch into $MODELS_DIR (profile=$PROFILE$req)"
  # Gate first, download second: never start a multi-GB download for a set that will fail.
  while IFS='|' read -r id dir lic prof bun sha src rev inc exc; do
    model_selected "$id" "$prof" "$bun" || continue
    licence_allowed "$lic" || die "model $id has licence '$lic' which is not allowed (use --allow-licence $lic after legal review)"
    [[ "$dir" =~ ^[A-Za-z0-9._-]+$ ]] || die "model $id: bad dir '$dir'"
    if [[ "$src" == hf://* && ( -z "$rev" || "$rev" == TODO* ) && "$ALLOW_UNPINNED" != true ]]; then
      die "model $id: revision not pinned in models.lock.yaml (set it, or pass --allow-unpinned to fetch main)"
    fi
  done < <(parse_models_lock)
  while IFS='|' read -r id dir lic prof bun sha src rev inc exc; do
    model_selected "$id" "$prof" "$bun" || continue
    dest="$MODELS_DIR/$dir"
    n=$((n + 1))
    case "$src" in
      hf://*)
        [[ -n "$hf_bin" ]] || die "need the Hugging Face CLI: pip install -U huggingface_hub (provides 'hf')"
        repo="${src#hf://}"
        if [[ -z "$rev" || "$rev" == TODO* ]]; then
          echo "   WARNING: $id is not pinned; fetching main. Pin the commit before bundling."
          rev="main"
        fi
        log "  $id@$rev ($lic) -> $dest"
        args=(download "$repo" --revision "$rev" --local-dir "$dest")
        if [[ -n "$inc" ]]; then read -ra globs <<< "$inc"; for g in "${globs[@]}"; do args+=(--include "$g"); done; fi
        if [[ -n "$exc" ]]; then read -ra globs <<< "$exc"; for g in "${globs[@]}"; do args+=(--exclude "$g"); done; fi
        HF_HUB_DISABLE_TELEMETRY=1 "$hf_bin" "${args[@]}" >/dev/null
        rm -rf "$dest/.cache"
        ;;
      https://*)
        command -v curl >/dev/null || die "curl not found"
        log "  $id ($lic) -> $dest"
        mkdir -p "$dest"
        # curl URL globbing: {a,b} in the URL, #1 in the output name.
        name="${src##*/}"
        out="${name/\{*\}/#1}"
        curl -fsSL --retry 3 -o "$dest/$out" "$src"
        ;;
      ollama://*)
        echo "   $id: Ollama models are not fetched by this script. On a connected host run:"
        echo "     OLLAMA_MODELS=$dest ollama serve &   then: ollama pull <tag> for ${src#ollama://}"
        echo "   (or import the pinned GGUF with a Modelfile). Then re-run with --model $id --pin."
        [[ -d "$dest" ]] || continue
        ;;
      *) die "model $id: unsupported source '$src'" ;;
    esac
    got="$(tree_hash "$dest")"
    printf '%s  %s  %s@%s\n' "$got" "$dir" "$id" "$rev" >> "$MODELS_DIR/models.sha256"
    if [[ -n "$sha" && "$sha" != TODO* && "$sha" != "$got" ]]; then
      die "model $id: tree sha256 mismatch: models.lock has $sha, downloaded $got"
    fi
    if [[ "$PIN" == true ]]; then
      pin_sha "$id" "$got"
      echo "     tree sha256 = $got   (pinned in $(basename "$MODELS_LOCK"))"
    else
      echo "     tree sha256 = $got   (recorded in models.sha256; --pin writes it to models.lock.yaml)"
    fi
  done < <(parse_models_lock)
  [[ $n -gt 0 ]] || die "no model selected (check --profile / --model and the bundle flags)"
  log "done: $n model(s). Bundle them with: airgap/bundle.sh --version <v> --models-dir $MODELS_DIR"
}

if [[ "$MODE" == fetch ]]; then
  fetch_models
  exit 0
fi

NAME="caliban-bundle-$VERSION"
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
STAGE="$OUT/$NAME"
[[ ! -e "$STAGE" ]] || die "$STAGE already exists; remove it first"
mkdir -p "$STAGE/images" "$STAGE/models"
trap 'echo "bundle.sh: failed; partial output left in $STAGE" >&2' ERR

echo "$VERSION" > "$STAGE/VERSION"

# ───────────── images ─────────────
# With the containerd image store, `docker save` must be told the platform, otherwise it
# tries to export every platform of a multi-arch image (whose layers were never pulled).
SAVE_PLATFORM=()
if docker save --help 2>/dev/null | grep -q -- '--platform'; then SAVE_PLATFORM=(--platform "$PLATFORM"); fi

log "images (profile=$PROFILE, platform=$PLATFORM)"
: > "$STAGE/images.txt"
while read -r group var ref; do
  [[ -z "${group:-}" || "$group" == \#* ]] && continue
  group_selected "$group" || continue
  ref="${ref//@VERSION@/$VERSION}"
  if docker image inspect "$ref" >/dev/null 2>&1 && [[ "$var" == CALIBAN_IMAGE ]]; then
    log "  using local $ref"
  else
    log "  pull $ref"
    docker pull --quiet --platform "$PLATFORM" "$ref" >/dev/null
  fi
  file="$(echo "$ref" | tr '/:@' '___').tar"
  docker save ${SAVE_PLATFORM[@]+"${SAVE_PLATFORM[@]}"} -o "$STAGE/images/$file" "$ref"
  id="$(docker image inspect --format '{{.Id}}' "$ref")"
  digest="$(docker image inspect --format '{{if .RepoDigests}}{{index .RepoDigests 0}}{{else}}local-build{{end}}' "$ref")"
  printf '%s %s images/%s %s %s\n' "$var" "$ref" "$file" "$id" "$digest" >> "$STAGE/images.txt"
done < "$IMAGES_LOCK"

# ───────────── models ─────────────
if [[ "$SKIP_MODELS" == true || ( "$PROFILE" == none && ${#EXTRA_MODELS[@]} -eq 0 ) ]]; then
  log "models: skipped"
else
  log "models from $MODELS_DIR"
  [[ -d "$MODELS_DIR" ]] || die "models dir not found: $MODELS_DIR (or use --skip-models)"
  while IFS='|' read -r id dir lic prof bun sha _src _rev _inc _exc; do
    model_selected "$id" "$prof" "$bun" || continue
    licence_allowed "$lic" || die "model $id has licence '$lic' which is not allowed in the bundle (use --allow-licence $lic after legal review)"
    [[ "$dir" =~ ^[A-Za-z0-9._-]+$ ]] || die "model $id: bad dir '$dir'"
    src="$MODELS_DIR/$dir"
    [[ -d "$src" ]] || die "model $id: $src missing. Download it first: airgap/bundle.sh fetch (airgap/README.md)."
    log "  $id ($lic) -> models/$dir"
    cp -R "$src" "$STAGE/models/$dir"
    rm -rf "$STAGE/models/$dir/.cache"
    got="$(tree_hash "$STAGE/models/$dir")"
    if [[ -z "$sha" || "$sha" == TODO* ]]; then
      echo "     tree sha256 = $got   (not pinned: set sha256 in models.lock.yaml)"
    elif [[ "$got" != "$sha" ]]; then
      die "model $id: tree sha256 mismatch: expected $sha, got $got"
    else
      echo "     tree sha256 OK"
    fi
  done < <(parse_models_lock)
fi
cp "$MODELS_LOCK" "$STAGE/models.lock.yaml"

# ───────────── deployment assets ─────────────
log "deploy assets"
mkdir -p "$STAGE/deploy"
for p in README.md compose helm airgap scripts; do
  [[ -e "$REPO_DIR/$p" ]] && cp -R "$REPO_DIR/$p" "$STAGE/deploy/"
done
# Never ship local secrets or downloaded weights twice.
rm -f "$STAGE/deploy/compose/.env"
rm -rf "$STAGE/deploy/compose/models"

# ───────────── manifest + signature ─────────────
log "MANIFEST.sha256"
( cd "$STAGE" && find . -type f ! -name 'MANIFEST.sha256*' -print0 | LC_ALL=C sort -z \
    | xargs -0 "${SHA256[@]}" > MANIFEST.sha256 )

case "$SIGN" in
  cosign)
    log "signing with cosign (offline, no transparency log)"
    # cosign v2 flags. Keep the matching cosign.pub with the customer (out of band).
    COSIGN_PASSWORD="${COSIGN_PASSWORD:-}" cosign sign-blob --yes --key "$KEY" --tlog-upload=false \
      --output-signature "$STAGE/MANIFEST.sha256.sig" "$STAGE/MANIFEST.sha256"
    ;;
  minisign)
    log "signing with minisign"
    minisign -S -s "$KEY" -m "$STAGE/MANIFEST.sha256" -x "$STAGE/MANIFEST.sha256.minisig" \
      -t "caliban-bundle $VERSION"
    ;;
  none) echo "   WARNING: bundle is NOT signed (load.sh will require --allow-unsigned)" ;;
esac

# ───────────── tarball ─────────────
log "tar"
tar -C "$OUT" -cf "$OUT/$NAME.tar" "$NAME"
( cd "$OUT" && "${SHA256[@]}" "$NAME.tar" > "$NAME.tar.sha256" )
rm -rf "$STAGE"
trap - ERR

log "done: $OUT/$NAME.tar"
cat "$OUT/$NAME.tar.sha256"
