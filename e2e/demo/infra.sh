#!/usr/bin/env bash
# The gateway-host commands the demo's terminal segments show; run-demo.sh records them.
#
#   demo/infra.sh up    the e2e stack (MongoDB replica set, seed, model recorder) and the model check
#   demo/infra.sh run   after the console setup: e2e.sh run, every check, and the report's summary
#
# Comments are printed as they would be typed in a shell, so the recording reads on its own.
set -euo pipefail

DEMO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
E2E_DIR="$(dirname "$DEMO_DIR")/health-triage"
DEPLOY_DIR="$(cd "$DEMO_DIR/../.." && pwd)"
E2E_WORK_DIR="${E2E_WORK_DIR:-$E2E_DIR/.work}"

say() { printf '\n\033[1;33m# %s\033[0m\n' "$*"; sleep "${DEMO_TERM_PAUSE:-1}"; }
show() { printf '\033[1;32m$\033[0m \033[1m%s\033[0m\n' "$*"; sleep 0.5; "$@"; }
# A pipeline, shown as typed.
showsh() { printf '\033[1;32m$\033[0m \033[1m%s\033[0m\n' "$1"; sleep 0.5; bash -c "$1"; }

models_check() {
  local host="${E2E_GPU_HOST:-${GPU_IP:-}}"
  if [[ "${E2E_TARGET:-local}" == testbed && -n "$host" ]]; then
    say "Land: the models on the GPU host, OpenAI-compatible (vLLM chat, TEI embeddings, reranker)"
    showsh "curl -s --max-time 10 http://$host:8000/v1/models | python3 -m json.tool | sed -n 1,14p"
    local port
    for port in 8001 8002; do
      showsh "curl -s -o /dev/null --max-time 10 -w '%{http_code}\\n' http://$host:$port/health" || true
    done
  else
    say "Land: locally the e2e's scripted model stands in for the GPU host"
    show "$E2E_DIR/e2e.sh" status
  fi
}

case "${1:-}" in
  up)
    say "Caliban on the gateway host: one compose stack. The e2e adds a MongoDB replica set (primary and an analytics secondary) with synthetic data for the four use cases, a recorder in front of the model, and the bank's refund system as an MCP server."
    show git -C "$DEPLOY_DIR" log -1 --oneline
    show "$E2E_DIR/e2e.sh" up
    models_check
    say "Everything else is set up in the web console, next."
    ;;
  run)
    say "The console set everything up. e2e.sh run sends the requests again and checks routing, masking, the queries MongoDB ran, the answer guard, metering and the audit chain."
    status=0
    show "$E2E_DIR/e2e.sh" run || status=$?
    say "The report"
    show sed -n '1,24p' "$E2E_WORK_DIR/report.md"
    exit "$status"
    ;;
  *) echo "usage: infra.sh up | run" >&2; exit 2 ;;
esac
