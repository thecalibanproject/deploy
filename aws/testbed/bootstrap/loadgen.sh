#!/usr/bin/env bash
# Load generator host: Rust toolchain and the bench crate from core (bench_ref), or a
# prebuilt binary from s3://<bucket>/tools/<arch>/caliban-bench on hosts without internet.
set -euo pipefail
# shellcheck source=common.sh
. /opt/caliban-testbed/common.sh
start_log

install_ttl_timer
retry dnf install -y -q git gcc gcc-c++ make cmake openssl-devel pkgconf-pkg-config perl python3 jq tar gzip

# Higher limits for many concurrent connections.
cat > /etc/sysctl.d/90-caliban-loadgen.conf <<'SYS'
net.ipv4.ip_local_port_range = 10240 65535
net.ipv4.tcp_tw_reuse = 1
net.core.somaxconn = 65535
SYS
sysctl --system >/dev/null
cat > /etc/security/limits.d/90-caliban-loadgen.conf <<'LIM'
*    soft nofile 1048576
*    hard nofile 1048576
LIM

if [ "$CONNECTED" = true ]; then
  export RUSTUP_HOME=/opt/rust CARGO_HOME=/opt/cargo
  if [ ! -x /opt/cargo/bin/cargo ]; then
    retry curl -fsSL -o /tmp/rustup-init.sh https://sh.rustup.rs
    sh /tmp/rustup-init.sh -y --no-modify-path --profile minimal --default-toolchain stable
  fi
  cat > /etc/profile.d/rust.sh <<'RS'
export RUSTUP_HOME=/opt/rust CARGO_HOME=/opt/cargo PATH=/opt/cargo/bin:$PATH
RS
  export PATH=/opt/cargo/bin:$PATH

  for c in $CARGO_TOOLS; do
    cargo install --locked --quiet "$c" || log "warning: cargo install $c failed"
  done

  mkdir -p "$SRC"
  if git ls-remote --exit-code --heads "https://github.com/$GITHUB_ORG/core" "$BENCH_REF" >/dev/null 2>&1; then
    clone core "$BENCH_REF"
    # To a file, not `| grep -q`: under pipefail an early grep exit kills cargo with SIGPIPE.
    (cd "$SRC/core" && cargo metadata --no-deps --format-version 1 > /tmp/core-metadata.json)
    if grep -q "\"name\":\"$BENCH_PACKAGE\"" /tmp/core-metadata.json; then
      (cd "$SRC/core" && cargo build --release --locked -p "$BENCH_PACKAGE")
      for b in "$SRC/core/target/release/$BENCH_PACKAGE" "$SRC/core/target/release/caliban-bench"; do
        if [ -x "$b" ]; then install -m 0755 "$b" /usr/local/bin/caliban-bench; break; fi
      done
    else
      log "core@$BENCH_REF has no package $BENCH_PACKAGE yet; build it later (README scenario a)"
    fi
  else
    log "core has no branch $BENCH_REF on GitHub yet; build the bench crate later (README scenario a)"
  fi
  chmod -R a+rwX /opt/cargo /opt/rust "$SRC" 2>/dev/null || true
fi

if [ ! -x /usr/local/bin/caliban-bench ] && s3exists "$TOOLS_PREFIX/caliban-bench"; then
  s3get "$TOOLS_PREFIX/caliban-bench" /usr/local/bin/caliban-bench
  chmod 0755 /usr/local/bin/caliban-bench
  log "caliban-bench from s3://$BUCKET/$TOOLS_PREFIX/caliban-bench"
fi
[ -x /usr/local/bin/caliban-bench ] || log "no caliban-bench binary yet"

mark_ready
