#!/usr/bin/env bash
# Records a command in a terminal and renders it as a 1920x1080 video, for the demo's
# infrastructure segments:
#
#   demo/record-term.sh NAME COMMAND [ARGS...]
#
# 1. `script` (util-linux, on every gateway host) runs the command on a pty with a fixed size and
#    writes a typescript and a classic timing file;
# 2. cast.py converts them to an asciicast v2 file (replayable with asciinema-player);
# 3. agg (pinned image) renders the cast as a GIF, idle stretches capped;
# 4. ffmpeg (pinned image) turns the GIF into an H.264 MP4 at 1920x1080, 30 fps.
#
# Outputs in $DEMO_OUT/term (default $E2E_WORK_DIR/demo/term): NAME.cast, NAME.gif, NAME.mp4, and
# NAME.status with the command's exit status (the recording is kept whatever the status).
#
# DEMO_AGG and DEMO_FFMPEG replace the containers with local binaries (testing without Docker).
set -euo pipefail

DEMO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
E2E_DIR="$(dirname "$DEMO_DIR")"
E2E_WORK_DIR="${E2E_WORK_DIR:-$E2E_DIR/.work}"
DEMO_OUT="${DEMO_OUT:-$E2E_WORK_DIR/demo}"
# shellcheck source=e2e/demo/images.env
. "$DEMO_DIR/images.env"
COLS="${DEMO_TERM_COLS:-110}"
ROWS="${DEMO_TERM_ROWS:-30}"

die() { echo "record-term.sh: $*" >&2; exit 1; }
[[ $# -ge 2 ]] || die "usage: record-term.sh NAME COMMAND [ARGS...]"
name="$1"; shift
command -v script >/dev/null || die "script (util-linux) is required"
out="$DEMO_OUT/term"
mkdir -p "$out"

# The command line, quoted for the shell `script` starts; the pty gets the recording's size.
cmd="stty cols $COLS rows $ROWS 2>/dev/null; export COLUMNS=$COLS LINES=$ROWS TERM=xterm-256color;"
cmd+=" $(printf '%q ' "$@")"
status=0
SHELL=/bin/bash script -q -e -m classic -T "$out/$name.timing" -O "$out/$name.typescript" -c "$cmd" \
  </dev/null || status=$?
echo "$status" > "$out/$name.status"
python3 "$DEMO_DIR/cast.py" "$out/$name.typescript" "$out/$name.timing" "$out/$name.cast" \
  --cols "$COLS" --rows "$ROWS" --title "Caliban demo: $name"

# The fonts come from the Playwright image (run-demo.sh fonts): agg's image has none.
fonts="$DEMO_OUT/fonts"
agg_args=(--font-size "${DEMO_TERM_FONT_SIZE:-26}" --theme "${DEMO_TERM_THEME:-monokai}"
  --idle-time-limit "${DEMO_TERM_IDLE_S:-1.5}" --speed "${DEMO_TERM_SPEED:-1}" --last-frame-duration 3
  --fps-cap 15 --no-loop --quiet)
if [[ -d "$fonts" ]]; then
  agg_args+=(--font-dir "$fonts" --font-family "${DEMO_TERM_FONTS:-Liberation Mono,FreeMono,Unifont}")
fi
if [[ -n "${DEMO_AGG:-}" ]]; then
  "$DEMO_AGG" "${agg_args[@]}" "$out/$name.cast" "$out/$name.gif"
else
  docker run --rm --user "$(id -u):$(id -g)" -v "$DEMO_OUT:$DEMO_OUT" -w "$out" \
    "$DEMO_AGG_IMAGE" "${agg_args[@]}" "$out/$name.cast" "$out/$name.gif"
fi

vf="scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=0x272822"
vf+=",fps=30,format=yuv420p"
ff_args=(-hide_banner -loglevel error -y -i "$out/$name.gif" -vf "$vf" -c:v libx264 -preset veryfast -crf 20
  -movflags +faststart "$out/$name.mp4")
if [[ -n "${DEMO_FFMPEG:-}" ]]; then
  "$DEMO_FFMPEG" "${ff_args[@]}"
else
  docker run --rm --user "$(id -u):$(id -g)" -v "$DEMO_OUT:$DEMO_OUT" -w "$out" "$DEMO_FFMPEG_IMAGE" "${ff_args[@]}"
fi
echo "record-term.sh: $out/$name.mp4 (the command exited $status)"
exit "$status"
