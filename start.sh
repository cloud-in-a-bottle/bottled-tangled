#!/bin/bash
# openhost-tangled supervisor.
#
# Boot order:
#   1. openhost-init.sh  — resolve hostname/owner, generate the master
#      key on first boot, migrate Go-knot data once, write the knot's
#      config and the env file.
#   2. auth_proxy.py     — the public HTTP seam on 0.0.0.0:8080: serves
#      /_healthz locally, serves the setup page while the owner DID is
#      missing (or an error page if the knot can't start), and
#      otherwise forwards to the knot on 127.0.0.1:5555.  Always started
#      so OpenHost sees a healthy container and the operator gets a
#      helpful page, even if the knot itself can't start.
#   3. knot-server       — git over HTTP and SSH, XRPC, the event
#      stream, as the git user.  Only started when the owner DID is set
#      and nothing blocks it.
#
# If either long-running process exits, tear the container down so
# OpenHost restarts it.

set -uo pipefail

if ! /opt/openhost-tangled/openhost-init.sh; then
    echo "[start] openhost-init.sh failed; see above. Exiting so the app restarts."
    exit 1
fi

# shellcheck disable=SC1091
. /etc/environment-openhost-tangled

# The public HTTP sidecar always runs.
python3 /opt/openhost-tangled/auth_proxy.py &
PROXY_PID=$!

proxy_only() {
    # Keep the container alive on just the proxy so its page is
    # reachable; if the proxy dies, exit so OpenHost restarts us.
    wait "$PROXY_PID"
    echo "[start] auth_proxy exited; shutting down"
    exit 1
}

if [[ -f "${OPENHOST_TANGLED_SENTINEL_FAILED}" ]]; then
    echo "[start] knot NOT started: $(cat "${OPENHOST_TANGLED_SENTINEL_FAILED}")"
    proxy_only
fi
if [[ -f "${OPENHOST_TANGLED_SENTINEL_NO_OWNER}" ]]; then
    echo "[start] owner DID not set — knot NOT started."
    echo "[start] Open the app URL while signed in to set it."
    proxy_only
fi

# Start the knot as the git user.  It listens for SSH on container port
# 22 (the manifest's [[ports]] maps it to the host), so it keeps just
# CAP_NET_BIND_SERVICE as an ambient capability.  The master key goes in
# the environment only, never into a world-readable file.
# NO_COLOR keeps ANSI colour codes out of the app logs.
NO_COLOR=1 KNOT_MASTER_KEY="$(cat "${OPENHOST_TANGLED_MASTER_KEY_FILE}")" \
    setpriv --reuid=git --regid=git --clear-groups \
        --inh-caps=-all,+net_bind_service --ambient-caps=+net_bind_service \
        /usr/local/bin/knot-server --config /etc/knot/config.toml &
KNOT_PID=$!

echo "[start] proxy=${PROXY_PID} knot=${KNOT_PID}"

# If either exits, bring the whole container down.
wait -n "$PROXY_PID" "$KNOT_PID"
CODE=$?
echo "[start] a child exited (code ${CODE}); shutting down"
kill "$PROXY_PID" "$KNOT_PID" 2>/dev/null
wait 2>/dev/null
exit "$CODE"
