# openhost-tangled
#
# A Tangled "knot" — the self-hostable git data server for Tangled
# (https://tangled.org), the AT-Protocol-based social coding platform.
#
# A knot stores your git repositories, and since v1.20.0 also their
# issues, pull requests, comments and collaborators, and federates with
# a Tangled AppView (tangled.org by default), which provides the web UI.
# It is NOT a website you log into — identity is your ATProto DID (e.g.
# your Bluesky account), and there is no knot-local account system.
#
# Since v1.20.0 upstream ships only the Rust knot ("knot2"): one
# binary, `knot-server`, with no database (git is the store, plus a key
# file sealed with KNOT_MASTER_KEY) and its own in-process SSH server,
# so there is no sshd, AuthorizedKeysCommand or unix git account any
# more.  `knot-migrate` converts the old Go knot's data offline;
# openhost-init.sh runs it on the first boot of an install that has
# Go-knot data.
#
# Transports the knot exposes:
#   * HTTP (:5555, loopback) — git clone/fetch over HTTP, the XRPC API
#     used for federation, the event stream WebSocket at
#     /xrpc/com.atproto.sync.subscribeRepos, and a homepage at /.
#   * SSH (:22) — git push/fetch, authenticated by the SSH keys each
#     user publishes on their PDS (the knot reads them live).
#
# On OpenHost we route HTTP through the normal app URL (public, since
# git/federation are machine-to-machine and can't do browser SSO) and
# expose SSH on a dedicated host port via the manifest's [[ports]].

# ---- build stage: compile knot-server and knot-migrate --------------------
# Mirrors upstream's knot2/Containerfile.
FROM docker.io/library/rust:1.96-slim-trixie AS builder
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates build-essential cmake perl pkg-config clang mold git \
    && rm -rf /var/lib/apt/lists/*
ENV RUSTFLAGS="-C linker=clang -C link-arg=-fuse-ld=mold"
# cargo's built-in git client is very slow on the tangled.org-hosted git
# dependencies (pinned by rev in Cargo.lock); the git CLI is quick.
ENV CARGO_NET_GIT_FETCH_WITH_CLI=true
# v1.20.0.  Fetched by commit, not tag, so a moved tag can't change
# what we build.
ARG KNOT_REF=2096d06d938e680297adeadd71b8320c9f3491d4
WORKDIR /src
RUN git init -q . \
 && git fetch --depth 1 https://tangled.org/@tangled.org/core ${KNOT_REF} \
 && git checkout -q FETCH_HEAD
RUN cargo build --release --locked --package knot-server --package knot-migrate
RUN strip target/release/knot-server target/release/knot-migrate

# ---- runtime stage --------------------------------------------------------
FROM docker.io/library/debian:trixie-slim

# python3 for the health/setup/forward proxy; ca-certificates for the
# knot's outbound calls (PLC directory, PDSes, AppView, relays).
# setpriv (util-linux, in the base image) drops the knot to the git user.
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# The knot runs as the unprivileged `git` user (uid/gid 1000, as
# upstream's image), which owns its data.
ARG UID=1000
ARG GID=1000
RUN groupadd -g ${GID} git \
 && useradd -u ${UID} -g git -M -d /nonexistent -s /usr/sbin/nologin git

COPY --from=builder /src/target/release/knot-server  /usr/local/bin/knot-server
COPY --from=builder /src/target/release/knot-migrate /usr/local/bin/knot-migrate

COPY start.sh          /opt/openhost-tangled/start.sh
COPY auth_proxy.py     /opt/openhost-tangled/auth_proxy.py
COPY openhost-init.sh  /opt/openhost-tangled/openhost-init.sh
RUN chmod 0755 /opt/openhost-tangled/start.sh \
      /opt/openhost-tangled/auth_proxy.py \
      /opt/openhost-tangled/openhost-init.sh

# 8080 = auth_proxy (the OpenHost-routed HTTP port, forwards to the
#        knot on loopback :5555).
# 22   = the knot's built-in SSH server (exposed on the host via the
#        manifest's [[ports]]).
EXPOSE 8080
EXPOSE 22

CMD ["/opt/openhost-tangled/start.sh"]
