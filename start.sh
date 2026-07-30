#!/bin/bash
# openhost-tangled supervisor.
#
# Boot order:
#   1. openhost-init.sh  — resolve hostname/owner, lay out persistent
#      dirs, generate sshd host keys, write the env file.
#   2. auth_proxy.py     — the public HTTP seam on 0.0.0.0:8080: serves
#      /_healthz locally, serves a setup page when the owner DID is
#      missing, and otherwise transparently forwards to the knot on
#      127.0.0.1:5555.  Always started (so OpenHost sees a healthy
#      container and the operator gets a helpful page) even if the knot
#      itself can't start yet.
#   3. sshd              — git push transport (only started once the
#      owner DID is set; without repos there's nothing to push to).
#   4. knot server       — the git data server + XRPC federation, on
#      loopback :5555 / internal :5444, as the git user.  Only started
#      when the owner DID is set.
#
# If any started long-running process exits, tear the container down so
# OpenHost restarts it.

set -uo pipefail

/opt/openhost-tangled/openhost-init.sh

# shellcheck disable=SC1091
. /etc/environment-openhost-tangled

# The public HTTP sidecar always runs.
python3 /opt/openhost-tangled/auth_proxy.py &
PROXY_PID=$!

if [[ -f "${OPENHOST_TANGLED_SENTINEL_NO_OWNER}" ]]; then
    echo "[start] KNOT_OWNER_DID not set — knot/sshd NOT started."
    echo "[start] Open the app URL for setup instructions, set KNOT_OWNER_DID, and reload."
    # Keep the container alive on just the proxy so the setup page is
    # reachable; if the proxy dies, exit so OpenHost restarts us.
    wait "$PROXY_PID"
    echo "[start] auth_proxy exited; shutting down"
    exit 1
fi

# Start sshd (foreground child) for git push.
/usr/sbin/sshd -D -e &
SSHD_PID=$!

# Start the knot server as the git user.
su-exec git env \
    KNOT_SERVER_HOSTNAME="${KNOT_SERVER_HOSTNAME}" \
    KNOT_SERVER_OWNER="${KNOT_SERVER_OWNER}" \
    KNOT_SERVER_LISTEN_ADDR="${KNOT_SERVER_LISTEN_ADDR}" \
    KNOT_SERVER_INTERNAL_LISTEN_ADDR="${KNOT_SERVER_INTERNAL_LISTEN_ADDR}" \
    KNOT_SERVER_DB_PATH="${KNOT_SERVER_DB_PATH}" \
    KNOT_REPO_SCAN_PATH="${KNOT_REPO_SCAN_PATH}" \
    KNOT_GIT_USER_NAME="${KNOT_GIT_USER_NAME}" \
    KNOT_GIT_USER_EMAIL="${KNOT_GIT_USER_EMAIL}" \
    APPVIEW_ENDPOINT="${APPVIEW_ENDPOINT}" \
    /usr/bin/knot server &
KNOT_PID=$!

echo "[start] proxy=${PROXY_PID} sshd=${SSHD_PID} knot=${KNOT_PID}"

# If any of the three exits, bring the whole container down.
wait -n "$PROXY_PID" "$SSHD_PID" "$KNOT_PID"
CODE=$?
echo "[start] a child exited (code ${CODE}); shutting down"
kill "$PROXY_PID" "$SSHD_PID" "$KNOT_PID" 2>/dev/null
wait 2>/dev/null
exit "$CODE"
