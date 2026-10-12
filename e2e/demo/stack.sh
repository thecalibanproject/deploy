# The shared e2e stack the four recorded scenarios need, as e2e.sh settings. Sourced by run-demo.sh;
# source it yourself before any e2e/health-triage/e2e.sh command (up, exec, down) of a demo stack:
#
#   . e2e/demo/stack.sh && e2e/health-triage/e2e.sh up
#
# What it adds to the health triage stack (each scenario's README, "What the shared stack needs"):
#   fintech    the refund system's MCP server (docker-compose.fintech.yml, service
#              caliban-e2e-fintech-mcp), its work directory, Cedar Bank's quotas (config/limits.toml,
#              appended to the rendered caliban.toml)
#   developer  its work directory (the SDK is installed there); DEV_SDK_SRC names a wheel or a
#              directory the Playwright container can read (DEMO_MOUNTS mounts it read-only)
# A setting already in the environment wins. DEMO_STACK=0 leaves everything as it is.
# shellcheck shell=bash

if [[ "${DEMO_STACK:-1}" == 1 ]]; then
  _demo_e2e="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  if [[ -z "${E2E_EXTRA_COMPOSE+x}" ]]; then
    # e2e.sh's own default on the testbed (the gateway's compose file), then the scenario overlays.
    if [[ "${E2E_TARGET:-local}" == testbed ]]; then
      E2E_EXTRA_COMPOSE="/opt/caliban-testbed/compose.testbed-gateway.yml:"
    else
      E2E_EXTRA_COMPOSE=""
    fi
    E2E_EXTRA_COMPOSE+="$_demo_e2e/fintech/docker-compose.fintech.yml"
  fi
  : "${E2E_EXTRA_CONFIG:=$_demo_e2e/fintech/config/limits.toml}"
  : "${E2E_EXTRA_SERVICES:=caliban-e2e-fintech-mcp}"
  : "${E2E_WORK_SUBDIRS:=fintech developer}"
  export E2E_EXTRA_COMPOSE E2E_EXTRA_CONFIG E2E_EXTRA_SERVICES E2E_WORK_SUBDIRS
  unset _demo_e2e
fi
