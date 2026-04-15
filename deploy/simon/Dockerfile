# syntax=docker/dockerfile:1.6
#
# Simon TUI agent — multi-stage build.
#
# Stage 1 compiles the Rust binary from workspace sources under src-rust/.
# Stage 2 ships a minimal debian:bookworm-slim runtime image with only
# ca-certificates for TLS to the LLM provider. The binary is owned by root
# mode 0755, and Simon runs as non-root uid 1000.
#
# Build context: the repo root (so this Dockerfile must be referenced from
# deploy/simon/Dockerfile with context=. in docker-compose.yml).

# ---------- Stage 1: build ----------
FROM rust:1-bullseye AS builder

WORKDIR /app

# Install build deps needed by rusqlite (bundled), openssl-sys optional paths,
# and anything else the workspace pulls in. Kept minimal.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        pkg-config \
        libssl-dev \
        ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Copy the whole workspace. We do NOT try to do a cargo-chef style dependency
# cache here — it's fragile across workspace moves and the parallel Rust
# thread is still churning. A plain full-copy build is fine for the demo.
COPY src-rust/ /app/src-rust/

WORKDIR /app/src-rust

# The --no-default-features flag is load-bearing: several crates gate
# heavy features (sixel, xcap, etc.) behind defaults that only work inside
# an interactive terminal with GPU / X11 access. The demo runs headless.
RUN cargo build --release --package simon --no-default-features

# ---------- Stage 2: runtime ----------
#
# IMPORTANT: must match the builder's glibc + libssl soname. The builder
# runs on rust:1-bullseye (Debian 11, libssl1.1). Using bookworm-slim
# (libssl3) here produces a binary that fails to start with "libssl.so.1.1:
# cannot open shared object file". Keep both on bullseye, or upgrade the
# builder to rust:1-bookworm and this to debian:bookworm-slim together.
FROM debian:bullseye-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates \
        tini \
 && rm -rf /var/lib/apt/lists/*

# Non-root demo user. uid 1000 matches the default host user on most
# Docker Desktop installs so bind-mounted files are writable without chown.
RUN groupadd --system --gid 1000 simon \
 && useradd  --system --uid 1000 --gid 1000 --home /home/simon --shell /bin/bash --create-home simon

# Binary: owned by root, world-executable, in a PATH location.
COPY --from=builder /app/src-rust/target/release/simon /usr/local/bin/simon
RUN chown root:root /usr/local/bin/simon \
 && chmod 0755      /usr/local/bin/simon

# Directories the running process needs.
#   /workspace  — bind mount target for the corpus volume, Simon's cwd
#   /etc/simon  — read-only config (acl.json), owned by root
#   /var/simon  — writable audit log / state
RUN install -d -o simon -g simon -m 0755 /workspace \
 && install -d -o root  -g root  -m 0755 /etc/simon \
 && install -d -o simon -g simon -m 0755 /var/simon

USER simon
WORKDIR /workspace

# tini reaps zombies and forwards signals so Ctrl-C from the host tears
# Simon down cleanly.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/simon"]
