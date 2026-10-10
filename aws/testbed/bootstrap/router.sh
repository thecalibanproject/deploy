#!/usr/bin/env bash
# Router host: `caliban router` in split mode. No config file: it polls the gateway's control
# plane for signed snapshots (fail-static, cached on disk) and shares the gateway's Valkey.
set -euo pipefail
# shellcheck source=common.sh
. /opt/caliban-testbed/common.sh
start_log

install_ttl_timer
install_docker
fetch_caliban

umask 077
cat > /etc/caliban-testbed/router.env <<ENV
CALIBAN_SNAPSHOT_PUBLIC_KEY=$(snapshot_pub_b64)
CALIBAN_ROUTER_TOKEN=$(param router-token)
CALIBAN_KEK=$(param kek)
CALIBAN_VALKEY_URL=redis://:$(param valkey-password)@$GATEWAY_IP:6379/0
# Qdrant REST port (6334 is gRPC, which Caliban does not use).
CALIBAN_QDRANT_URL=http://$GATEWAY_IP:6333
CALIBAN_LOG=info
# Nagle plus delayed ACK added 24 to 50 ms to the first streamed token on Linux.
CALIBAN_TCP_NODELAY=1
ENV
umask 022

install -d -o 65532 -g 65532 -m 0700 /var/lib/caliban
docker rm -f caliban-router >/dev/null 2>&1 || true
docker run -d --name caliban-router --restart unless-stopped \
  --read-only --cap-drop ALL --security-opt no-new-privileges:true --user 65532:65532 \
  --tmpfs /tmp:size=64m --log-driver local --log-opt max-size=20m \
  -v /var/lib/caliban:/var/lib/caliban \
  -p 0.0.0.0:8080:8080 \
  --env-file /etc/caliban-testbed/router.env \
  "$CALIBAN_IMAGE_REF" router \
  --control-plane-url "http://$GATEWAY_IP:8081" \
  --snapshot-cache /var/lib/caliban/snapshot.json

# A new router waits for its first snapshot before it listens.
for _ in $(seq 1 90); do
  if docker exec caliban-router /usr/local/bin/caliban healthcheck --addr 127.0.0.1:8080 --path /healthz >/dev/null 2>&1; then
    log "router serving"
    break
  fi
  sleep 10
done
mark_ready
