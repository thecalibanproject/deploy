# NOTE: deliberately no `# syntax=` directive. It would make BuildKit pull the
# docker/dockerfile frontend image from Docker Hub, which breaks air-gapped builds.
# The frontend built into Docker Engine >= 24 supports everything used here
# (RUN --mount=type=cache, COPY --chmod).
#
# Caliban: one image, one binary (`caliban router | control-plane | standalone | keygen`)
# plus the static admin console.
#
# Build context = the PARENT directory that holds the sibling repos (~/caliban):
#
#   ~/caliban/core   Rust workspace  -> /usr/local/bin/caliban  (builder rust:1-trixie + runtime distroless cc-debian13: the `ner` feature links the prebuilt ONNX Runtime, built with GCC 14, so both stages need GCC 14 libstdc++)
#   ~/caliban/web    Vite app        -> /usr/share/caliban/web
#
#   docker build -f deploy/images/caliban.Dockerfile -t caliban/caliban:dev ~/caliban
#   (or use deploy/images/build.sh)
#
# Air-gapped builds (no network during `docker build`):
#   1. On a connected machine run `deploy/images/build.sh vendor`. It produces
#        core/vendor/ + core/.cargo-vendor-config.toml   (cargo vendor)
#        web/.pnpm-store/                                  (pnpm fetch)
#        web/.corepack/corepack.tgz                        (corepack pack: the pnpm release)
#   2. Carry the source tree (with those directories) plus the three base images below.
#   3. Build with `--build-arg OFFLINE=true --network=none`.
#
# Base images are build args so they can point at a private mirror. Pin by digest for
# reproducible builds, e.g. --build-arg RUST_IMAGE=rust:1-trixie@sha256:...
ARG NODE_IMAGE=node:24-slim
ARG RUST_IMAGE=rust:1-trixie
ARG RUNTIME_IMAGE=gcr.io/distroless/cc-debian13:nonroot

# ───────────────────────────── web console ─────────────────────────────
FROM ${NODE_IMAGE} AS web
ARG OFFLINE=false
ENV CI=true \
    PNPM_HOME=/pnpm \
    PATH=/pnpm:$PATH \
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \
    NEXT_TELEMETRY_DISABLED=1 \
    DO_NOT_TRACK=1
WORKDIR /src/web

# Dependency layer first (cache-friendly). The pnpm version comes from the
# `packageManager` field of web/package.json; corepack resolves it.
# The `[x]` globs make optional files optional; package.json is listed in every COPY
# so that at least one source always matches (BuildKit rejects an all-empty COPY).
COPY web/package.json web/pnpm-lock.yaml web/.npmr[c] web/pnpm-workspace.yam[l] ./
# Offline inputs (only present after `build.sh vendor`).
COPY web/package.json web/.corepac[k] ./.corepack/
COPY web/package.json web/.pnpm-stor[e] ./.pnpm-store/
RUN set -eu; \
    if [ "$OFFLINE" = "true" ]; then \
      export COREPACK_ENABLE_NETWORK=0; \
      test -f .corepack/corepack.tgz || { echo "OFFLINE=true needs web/.corepack/corepack.tgz (run build.sh vendor)" >&2; exit 1; }; \
      corepack enable; \
      corepack install -g --cache-only ./.corepack/corepack.tgz; \
      pnpm config set store-dir /src/web/.pnpm-store; \
      pnpm install --frozen-lockfile --offline; \
    else \
      corepack enable; \
      pnpm install --frozen-lockfile; \
    fi

COPY web/ ./
RUN pnpm build && test -f dist/index.html

# ───────────────────────────── Rust binary ─────────────────────────────
FROM ${RUST_IMAGE} AS core
ARG OFFLINE=false
# Extra Debian packages for the build (e.g. "pkg-config protobuf-compiler"). Leave empty
# for air-gapped builds unless your RUST_IMAGE mirror already contains them.
ARG EXTRA_APT_PACKAGES=""
ARG CARGO_PROFILE=release
# Cargo features. `ner` compiles in the in-process PII NER model runtime (ONNX Runtime, fetched by
# the `ort` crate at build time). For --network=none builds either provide ONNX Runtime via
# ORT_LIB_LOCATION (see core/crates/caliban-pii/MODELS.md) or build with CARGO_FEATURES="".
ARG CARGO_FEATURES=ner
ENV CARGO_TERM_COLOR=never \
    CARGO_NET_RETRY=5 \
    CARGO_INCREMENTAL=0
WORKDIR /src/core

RUN set -eu; \
    if [ -n "$EXTRA_APT_PACKAGES" ]; then \
      apt-get update; \
      apt-get install -y --no-install-recommends $EXTRA_APT_PACKAGES; \
      rm -rf /var/lib/apt/lists/*; \
    fi

COPY core/ ./

# Cache mounts speed up repeated builds; the binary is copied out of the cache to /out.
RUN --mount=type=cache,id=caliban-cargo-registry,target=/usr/local/cargo/registry \
    --mount=type=cache,id=caliban-cargo-git,target=/usr/local/cargo/git \
    --mount=type=cache,id=caliban-target,target=/src/core/target \
    set -eu; \
    if [ "$OFFLINE" = "true" ]; then \
      test -d vendor || { echo "OFFLINE=true needs core/vendor (run build.sh vendor)" >&2; exit 1; }; \
      if [ -f .cargo-vendor-config.toml ]; then \
        cp .cargo-vendor-config.toml "$CARGO_HOME/config.toml"; \
      else \
        printf '[source.crates-io]\nreplace-with = "vendored-sources"\n\n[source.vendored-sources]\ndirectory = "vendor"\n' > "$CARGO_HOME/config.toml"; \
      fi; \
      cargo build --profile "$CARGO_PROFILE" --locked --offline -p caliban --features "$CARGO_FEATURES"; \
    else \
      cargo build --profile "$CARGO_PROFILE" --locked -p caliban --features "$CARGO_FEATURES"; \
    fi; \
    mkdir -p /out; \
    cp "target/$CARGO_PROFILE/caliban" /out/caliban; \
    /out/caliban --version || true

# ───────────────────────────── runtime ─────────────────────────────
FROM ${RUNTIME_IMAGE}
ARG VERSION=0.0.0-dev
ARG REVISION=unknown
ARG CREATED=1970-01-01T00:00:00Z

LABEL org.opencontainers.image.title="caliban" \
      org.opencontainers.image.description="Caliban AI gateway: router, control plane and admin console" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.created="${CREATED}" \
      org.opencontainers.image.base.name="gcr.io/distroless/cc-debian13:nonroot"

COPY --from=core --chown=0:0 --chmod=0755 /out/caliban /usr/local/bin/caliban
COPY --from=web  --chown=0:0 /src/web/dist /usr/share/caliban/web

# Telemetry is OFF unless OTEL_EXPORTER_OTLP_ENDPOINT is set by the operator.
ENV CALIBAN_CONFIG=/etc/caliban/caliban.toml \
    CALIBAN_WEB_DIR=/usr/share/caliban/web \
    CALIBAN_LOG=info

# distroless "nonroot" = uid/gid 65532. The root filesystem can be mounted read-only.
USER 65532:65532
EXPOSE 8080 8081
ENTRYPOINT ["/usr/local/bin/caliban"]
CMD ["standalone"]
