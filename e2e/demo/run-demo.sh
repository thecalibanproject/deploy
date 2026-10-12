#!/usr/bin/env bash
# Records the Caliban product demo on the gateway host (see README.md in this directory).
#
#   e2e/demo/run-demo.sh all                  # every scenario, the whole flow below
#   e2e/demo/run-demo.sh health-triage law    # the whole flow for these scenarios
#   e2e/demo/run-demo.sh STEP [SCENARIO...]   # one step:
#       pull       the pinned images (images.env)
#       fonts      copies the Playwright image's fonts for the terminal renderer
#       infra      records the gateway host: e2e.sh up and the model check (term/infra-up.mp4)
#       seed       e2e/<scenario>/seed.py, when the scenario has one
#       record     the console walkthroughs (<scenario>.webm, .timeline.json, shots/)
#       check      e2e/<scenario>/check.py, recorded (term/check-<scenario>.mp4)
#       cards      the cards, the health results segment, the telemetry report and segment
#       stitch     <scenario>.mp4 and caliban-demo.mp4
#       upload     copies $DEMO_OUT to s3://$CALIBAN_TESTBED_BUCKET/results/demo-<UTC time>/
#
# The whole flow: pull, fonts, infra, then per scenario seed, record, check; cards, stitch, and
# upload when DEMO_UPLOAD=1. A scenario whose recording or check fails is reported and the others
# go on; the exit status is non-zero then. The environment is the shared e2e stack's
# (e2e/health-triage/e2e.sh exec); E2E_TARGET=testbed on the testbed.
#
# DEMO_INFRA=0 leaves the gateway-host recording out (the stack must be up already).
# DEMO_LOCAL=1 runs without Docker: DEMO_PYTHON (a Python with playwright==1.63.0 and Chromium),
# DEMO_AGG and DEMO_FFMPEG (local binaries).
set -euo pipefail

DEMO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
E2E_ROOT="$(dirname "$DEMO_DIR")"
DEPLOY_DIR="$(dirname "$E2E_ROOT")"
E2E_SH="$E2E_ROOT/health-triage/e2e.sh"
export E2E_WORK_DIR="${E2E_WORK_DIR:-$E2E_ROOT/health-triage/.work}"
export DEMO_OUT="${DEMO_OUT:-$E2E_WORK_DIR/demo}"
# shellcheck source=e2e/demo/images.env
. "$DEMO_DIR/images.env"
export DEMO_PLAYWRIGHT_IMAGE DEMO_AGG_IMAGE DEMO_FFMPEG_IMAGE

die() { echo "run-demo.sh: $*" >&2; exit 1; }
log() { echo ">> $*"; }
local_mode() { [[ "${DEMO_LOCAL:-0}" == 1 ]]; }

scenarios() {
  if [[ $# -eq 0 || "$1" == all ]]; then
    local d
    for d in "$E2E_ROOT"/*/demo/scenario.py; do
      [[ -f "$d" ]] && basename "$(dirname "$(dirname "$d")")"
    done
  else
    printf '%s\n' "$@"
  fi
}

# The rest of the steps need the shared stack's environment: re-run through e2e.sh exec once.
with_env() {
  if [[ -n "${DEMO_IN_ENV:-}" ]]; then "$@"; else DEMO_IN_ENV=1 "$E2E_SH" exec "$0" "$@"; fi
}

pull() {
  local_mode && { log "DEMO_LOCAL=1: nothing to pull"; return; }
  local img
  for img in "$DEMO_PLAYWRIGHT_IMAGE" "$DEMO_AGG_IMAGE" "$DEMO_FFMPEG_IMAGE"; do
    log "pulling $img"
    docker pull -q "$img"
  done
}

fonts() {
  mkdir -p "$DEMO_OUT/fonts"
  if local_mode; then
    [[ -n "${DEMO_FONTS_DIR:-}" ]] && cp -r "$DEMO_FONTS_DIR"/. "$DEMO_OUT/fonts/"
    return 0
  fi
  docker run --rm --user "$(id -u):$(id -g)" -v "$DEMO_OUT/fonts:/out" "$DEMO_PLAYWRIGHT_IMAGE" \
    bash -c 'cp -r /usr/share/fonts/truetype/liberation /usr/share/fonts/truetype/freefont /out/ 2>/dev/null;
             find /usr/share/fonts -iname "unifont*.otf" -o -iname "unifont*.ttf" | head -n 1 | xargs -r -I{} cp {} /out/; true'
}

# Python in the pinned Playwright image (or DEMO_PYTHON locally), with the e2e's environment:
# E2E_*, DEMO_*, CALIBAN_ADMIN_TOKEN and the names in DEMO_PASS_ENV, never the MongoDB root
# credentials or key file.
pyrun() {
  if local_mode; then
    "${DEMO_PYTHON:-python3}" "$@"
    return
  fi
  local args=(--rm --init --ipc=host --network host --user "$(id -u):$(id -g)" -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1
    -v "$DEPLOY_DIR:$DEPLOY_DIR:ro" -v "$E2E_WORK_DIR:$E2E_WORK_DIR" -w "$DEMO_DIR")
  [[ "$DEMO_OUT" == "$E2E_WORK_DIR"/* ]] || args+=(-v "$DEMO_OUT:$DEMO_OUT")
  local name
  while IFS= read -r name; do
    case "$name" in
      E2E_MONGO_KEYFILE|E2E_MONGO_ROOT_*|E2E_COMPOSE_CMD) ;;
      E2E_*|DEMO_*|CALIBAN_ADMIN_TOKEN) args+=(-e "$name") ;;
    esac
  done < <(compgen -e)
  for name in ${DEMO_PASS_ENV:-}; do args+=(-e "$name"); done
  docker run "${args[@]}" "$DEMO_PLAYWRIGHT_IMAGE" python3 "$@"
}

infra() {
  log "recording the gateway host (e2e.sh up, the model check)"
  "$DEMO_DIR/record-term.sh" infra-up "$DEMO_DIR/infra.sh" up
}

seed() {
  local s
  for s in "$@"; do
    if [[ -f "$E2E_ROOT/$s/seed.py" ]]; then
      log "seeding $s"
      python3 "$E2E_ROOT/$s/seed.py"
    fi
  done
}

record() {
  mkdir -p "$DEMO_OUT"
  local s status=0
  for s in "$@"; do
    log "recording $s"
    pyrun "$DEMO_DIR/framework.py" record "$s" || { status=1; FAILED+=("$s: recording"); }
  done
  return "$status"
}

check() {
  local s status=0
  for s in "$@"; do
    [[ -f "$E2E_ROOT/$s/check.py" ]] || { log "$s has no check.py"; continue; }
    log "checking $s (recorded)"
    "$DEMO_DIR/record-term.sh" "check-$s" python3 "$E2E_ROOT/$s/check.py" || { status=1; FAILED+=("$s: check"); }
  done
  return "$status"
}

cards() {
  pyrun "$DEMO_DIR/cards.py" cards
  if [[ -f "$DEMO_OUT/health-triage.timeline.json" ]] && compgen -G "$E2E_WORK_DIR/results-*.json" >/dev/null; then
    pyrun "$DEMO_DIR/cards.py" results
  fi
  python3 "$DEMO_DIR/telemetry.py" report "$@"
  pyrun "$DEMO_DIR/cards.py" telemetry
}

stitch() {
  if local_mode; then
    [[ -n "${DEMO_FFMPEG:-}" ]] || die "DEMO_LOCAL=1 needs DEMO_FFMPEG"
  fi
  python3 "$DEMO_DIR/stitch.py" "$@"
}

upload() {
  [[ -n "${CALIBAN_TESTBED_BUCKET:-}" ]] || die "CALIBAN_TESTBED_BUCKET is not set (testbed hosts set it in /etc/profile.d)"
  local dest
  dest="s3://$CALIBAN_TESTBED_BUCKET/results/demo-$(date -u +%Y%m%dT%H%M%SZ)/"
  log "uploading $DEMO_OUT to $dest (needs results_upload = true)"
  aws s3 cp --recursive --only-show-errors --exclude 'video/*' --exclude 'parts/*' --exclude 'fonts/*' \
    "$DEMO_OUT" "$dest"
  aws s3 cp --only-show-errors "$E2E_WORK_DIR/report.md" "$dest" 2>/dev/null || true
}

FAILED=()
everything() {
  mapfile -t list < <(scenarios "$@")
  [[ ${#list[@]} -gt 0 ]] || die "no scenario found under $E2E_ROOT/*/demo/scenario.py"
  pull
  fonts
  if [[ "${DEMO_INFRA:-1}" == 1 ]]; then
    infra || die "the gateway host is not up (term/infra-up.mp4 shows why)"
  fi
  with_env _steps "${list[@]}"
}

# The scenario steps, with the stack's environment.
_steps() {
  local s
  for s in "$@"; do
    seed "$s" || { FAILED+=("$s: seed"); continue; }
    record "$s" || true
    check "$s" || true
  done
  cards "$@" || FAILED+=("cards")
  stitch || FAILED+=("stitch")
  [[ "${DEMO_UPLOAD:-0}" == 1 ]] && upload
  log "outputs in $DEMO_OUT: caliban-demo.mp4, <scenario>.mp4, telemetry.md, shots/, timelines"
  if [[ ${#FAILED[@]} -gt 0 ]]; then
    printf 'run-demo.sh: failed: %s\n' "${FAILED[@]}" >&2
    return 1
  fi
}

cmd="${1:-}"
[[ $# -gt 0 ]] && shift
case "$cmd" in
  pull) pull ;;
  fonts) fonts ;;
  infra) infra ;;
  seed) mapfile -t l < <(scenarios "$@"); with_env seed "${l[@]}" ;;
  record) mapfile -t l < <(scenarios "$@"); with_env record "${l[@]}" ;;
  check) mapfile -t l < <(scenarios "$@"); with_env check "${l[@]}" ;;
  cards) with_env cards "$@" ;;
  stitch) stitch "$@" ;;
  upload) upload ;;
  _steps) _steps "$@" ;;
  ""|-h|--help|help) sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//' ;;
  all) everything all ;;
  *) everything "$cmd" "$@" ;;
esac
