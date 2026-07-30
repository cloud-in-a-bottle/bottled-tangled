# openhost-tangled
#
# A Tangled "knot" — the self-hostable git data server for Tangled
# (https://tangled.org), the AT-Protocol-based social coding platform.
#
# A knot stores your git repositories and federates with a Tangled
# AppView (tangled.org by default): the AppView provides the web UI,
# social graph, issues and pull-requests as ATProto records, while the
# knot holds the actual git data and serves it.  It is NOT a website
# you log into — identity is your ATProto DID (e.g. your Bluesky
# account), and there is no knot-local account system.
#
# Transports the knot exposes:
#   * HTTP (:5555) — git clone/fetch over HTTP, the ATProto XRPC API
#     used for federation, a /events oplog WebSocket, and a MOTD at /.
#     Git PUSH is NOT allowed over HTTP (the knot returns "Pushes are
#     only supported over SSH").
#   * SSH  (:22)   — git push, authenticated by SSH keys that the knot
#     fetches from the AppView for the pushing user's DID (via
#     `knot keys` as sshd's AuthorizedKeysCommand).
#
# On OpenHost we route HTTP through the normal app URL (public, since
# git/federation are machine-to-machine and can't do browser SSO) and
# expose SSH on a dedicated host port via the manifest's [[ports]].
#
# We build the `knot` binary from source (a static CGO Go build, the
# same approach the community knot-docker uses) and supervise sshd +
# the knot server with a small bash script (no s6 — bash + `wait -n`
# is the OpenHost-preferred, rootless-podman-friendly approach).

# ---- build stage: compile the knot binary --------------------------------
FROM golang:1.25-alpine AS builder
ENV CGO_ENABLED=1
ARG KNOT_TAG=v1.16.1-alpha
WORKDIR /app
RUN apk add --no-cache git gcc musl-dev
RUN git clone -b ${KNOT_TAG} https://tangled.org/@tangled.org/core . \
 && go build -o /usr/bin/knot -ldflags '-s -w -extldflags "-static"' ./cmd/knot

# ---- runtime stage --------------------------------------------------------
FROM alpine:3.20

# git + openssh for the SSH push path; python3 for the health/landing
# sidecar; su-exec to drop privileges to the git user; bash for start.sh;
# curl/ca-certificates for outbound federation calls to the AppView/PDS.
RUN apk add --no-cache \
      git openssh openssh-server \
      python3 bash curl ca-certificates su-exec shadow

# The knot stores and serves repositories as the unprivileged `git`
# user.  UID/GID 1000 matches the community image's default.
ARG UID=1000
ARG GID=1000
RUN addgroup -g ${GID} git \
 && adduser -u ${UID} -G git -D -h /home/git git \
 && mkdir -p /home/git/repositories \
 && chown -R git:git /home/git

COPY --from=builder /usr/bin/knot /usr/bin/knot

# sshd config: host keys live on the persistent volume (generated on
# first boot by start.sh), password auth off, and git pushes are
# authorized by `knot keys` (fetches the pusher's SSH keys from the
# AppView by DID).  git-dir is the persistent repositories path.
COPY sshd_tangled.conf /etc/ssh/sshd_config.d/tangled.conf

COPY start.sh          /opt/openhost-tangled/start.sh
COPY auth_proxy.py     /opt/openhost-tangled/auth_proxy.py
COPY openhost-init.sh  /opt/openhost-tangled/openhost-init.sh
RUN chmod 0755 /opt/openhost-tangled/start.sh \
      /opt/openhost-tangled/auth_proxy.py \
      /opt/openhost-tangled/openhost-init.sh

# 8080 = auth_proxy (the OpenHost-routed HTTP port, forwards to the
#        knot on loopback :5555).
# 22   = sshd for git push (exposed on the host via manifest [[ports]]).
EXPOSE 8080
EXPOSE 22

CMD ["/opt/openhost-tangled/start.sh"]
