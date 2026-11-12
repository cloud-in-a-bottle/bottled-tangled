#!/bin/bash
# openhost-tangled every-boot init.
#
# Responsibilities:
#   1. Compute the knot's public HOSTNAME from the OpenHost zone
#      ($OPENHOST_APP_NAME.$OPENHOST_ZONE_DOMAIN, e.g.
#      tangled.andrew-1.selfhost.imbue.com).  The knot's identity is
#      did:web:<hostname>, so it must be the real public host.
#   2. Resolve the operator's ATProto owner DID (env, else the file the
#      setup form writes).  The knot refuses to start without an owner,
#      and it can't be auto-generated — it's the operator's identity.
#   3. Generate KNOT_MASTER_KEY on first boot and keep it in the app
#      data dir (mode 0600).  It unseals the knot's signing keys.
#   4. On the first boot after upgrading from the Go knot (app 1.x),
#      back up the old data and migrate it with knot-migrate (see
#      migrate_go_knot below).
#   5. Write the knot's config (/etc/knot/config.toml, regenerated
#      every boot) and /etc/environment-openhost-tangled for start.sh.
#
# Persistent layout under $OPENHOST_APP_DATA_DIR:
#   knot/             the knot's whole state: repos/ (every repo plus the
#                     knot's ACL and repo data; git is the database),
#                     lfs/ (LFS objects and issue/PR attachments),
#                     sealed-keys, ssh_host_key
#   knot-master-key   KNOT_MASTER_KEY (0600); useless without
#                     knot/sealed-keys and vice versa
#   owner-did         the owner DID saved by the setup form
#   go-knot-backup/   only on installs migrated from app 1.x: the Go
#                     knot's data, untouched, for rolling back
#
# Missing owner DID or a failed migration don't fail the container;
# instead a sentinel file tells start.sh not to start the knot and the
# auth_proxy to serve a page saying why.  That gives the operator an
# actionable message instead of a crash loop.

set -euo pipefail

PERSIST="${OPENHOST_APP_DATA_DIR:-/data/app_data/tangled}"
APP_NAME="${OPENHOST_APP_NAME:-tangled}"
ZONE_DOMAIN="${OPENHOST_ZONE_DOMAIN:-localhost}"

KNOT_DIR="$PERSIST/knot"
MASTER_KEY_FILE="$PERSIST/knot-master-key"
OWNER_DID_FILE="$PERSIST/owner-did"
BACKUP_DIR="$PERSIST/go-knot-backup"
MIGRATED_MARKER="$KNOT_DIR/.migrated-from-go-knot"
MIGRATION_LOG="$PERSIST/knot-migrate.log"
SENTINEL_NO_OWNER="$PERSIST/.needs-owner-did"
SENTINEL_FAILED="$PERSIST/.knot-start-blocked"
CONFIG_FILE=/etc/knot/config.toml
ENV_FILE=/etc/environment-openhost-tangled

# A hostname can't carry a port (the knot rejects one); only dev/test
# zones have one.
KNOT_HOSTNAME="${APP_NAME}.${ZONE_DOMAIN%%:*}"

# The platform owner's username (OPENHOST_OWNER_USERNAME, default
# "owner") greets the owner on the setup page and makes the contact
# email.
OWNER_USERNAME="${OPENHOST_OWNER_USERNAME:-owner}"

# Operator contact email, now required by the knot and published by
# org.tangled.knot.describeKnot.  Derived from the owner and zone like
# other apps' admin emails; KNOT_CONTACT_EMAIL overrides it.
SAFE_USERNAME="$(printf '%s' "$OWNER_USERNAME" | tr -c 'A-Za-z0-9._-' '-')"
CONTACT_EMAIL="${KNOT_CONTACT_EMAIL:-${SAFE_USERNAME}@${ZONE_DOMAIN%%:*}}"

PLC_DIRECTORY="${KNOT_PLC_DIRECTORY:-https://plc.directory}"
APPVIEW_ENDPOINT="${KNOT_APPVIEW_ENDPOINT:-https://tangled.org}"

log() { echo "[openhost-init] $*"; }

rm -f "$SENTINEL_NO_OWNER" "$SENTINEL_FAILED"

# Stop the knot from starting, with a reason for the logs and the page.
block_start() {
    local reason="$1"
    log "ERROR: $reason"
    log "The knot will NOT start until this is fixed."
    printf '%s\n' "$reason" > "$SENTINEL_FAILED"
}

# ----------------------------------------------------------------------
# Owner DID
# ----------------------------------------------------------------------
# OpenHost has no user-env-var mechanism, so the DID comes, in priority
# order, from KNOT_OWNER_DID / KNOT_SERVER_OWNER / OWNER (a manual
# deploy) or from the owner-did file the setup form writes.
OWNER_DID="${KNOT_OWNER_DID:-${KNOT_SERVER_OWNER:-${OWNER:-}}}"
if [[ -z "$OWNER_DID" && -f "$OWNER_DID_FILE" ]]; then
    OWNER_DID="$(head -n1 "$OWNER_DID_FILE" | tr -d '[:space:]')"
fi
if [[ -z "$OWNER_DID" || ! "$OWNER_DID" =~ ^did:[a-z]+:[A-Za-z0-9._:%-]+$ ]]; then
    log "WARNING: owner DID is not set to a valid DID (got: '${OWNER_DID}')."
    log "Open the app URL while signed in to set it; the knot starts once it's set."
    OWNER_DID=""
    : > "$SENTINEL_NO_OWNER"
fi

# ----------------------------------------------------------------------
# Master key
# ----------------------------------------------------------------------
# The sealed key store (knot/sealed-keys) holds the knot's signing key
# and every repo's; the master key unseals it.  Lose the key and the
# knot loses control of its repos' identities, so never silently make a
# new one for an existing store.
mkdir -p "$KNOT_DIR"
if [[ ! -s "$MASTER_KEY_FILE" ]]; then
    if [[ -e "$KNOT_DIR/sealed-keys" ]]; then
        block_start "$MASTER_KEY_FILE is missing but $KNOT_DIR/sealed-keys exists. Restore the master key from a backup; a new key can't unseal the existing keys."
    else
        (umask 077; head -c 32 /dev/urandom | base64 > "$MASTER_KEY_FILE.tmp")
        mv "$MASTER_KEY_FILE.tmp" "$MASTER_KEY_FILE"
        log "generated KNOT_MASTER_KEY in $MASTER_KEY_FILE (back up this file together with the app data)"
    fi
fi
chown root:root "$MASTER_KEY_FILE" 2>/dev/null || true
chmod 0600 "$MASTER_KEY_FILE" 2>/dev/null || true

# ----------------------------------------------------------------------
# One-time migration from the Go knot (app 1.x)
# ----------------------------------------------------------------------
# The Go knot kept knotserver.db, repositories/ and sshd host keys in
# keys/ at the top of the data dir.  On the first boot of this version:
#   1. move them, untouched, into go-knot-backup/ (renames on the same
#      filesystem: instant, no extra space),
#   2. run knot-migrate from the backup into knot/ in its default copy
#      mode, which only reads the source (the database read-only), so
#      the backup stays exactly as the old knot left it,
#   3. mark knot/ as migrated.
# A failed or interrupted migration leaves no marker: the partial knot/
# is deleted (it holds only copies) and the migration is retried on the
# next boot, while the knot stays stopped.  The migration needs room for
# a second copy of the repositories; knot-migrate checks before copying.
legacy_items=(knotserver.db knotserver.db-wal knotserver.db-shm repositories keys)

has_legacy_data() {
    [[ -f "$PERSIST/knotserver.db" || -f "$BACKUP_DIR/knotserver.db" ]]
}

migrate_go_knot() {
    log "found data from the Go knot (app 1.x); migrating it to the Rust knot."
    log "this happens once; the old data is kept in $BACKUP_DIR."

    mkdir -p "$BACKUP_DIR"
    local item
    for item in "${legacy_items[@]}"; do
        if [[ -e "$PERSIST/$item" ]]; then
            if [[ -e "$BACKUP_DIR/$item" ]]; then
                block_start "both $PERSIST/$item and $BACKUP_DIR/$item exist; refusing to pick one. Move the one that isn't the Go knot's latest data out of the way and restart the app."
                return
            fi
            mv "$PERSIST/$item" "$BACKUP_DIR/$item"
        fi
    done
    mkdir -p "$BACKUP_DIR/repositories"

    # Anything in knot/ is left over from an interrupted attempt (there's
    # no marker): only copies, so start over.
    find "$KNOT_DIR" -mindepth 1 -maxdepth 1 -exec rm -rf {} +

    if [[ ! -f "$BACKUP_DIR/keys/ssh_host_ed25519_key" ]]; then
        block_start "the Go knot's SSH host key $BACKUP_DIR/keys/ssh_host_ed25519_key is missing; knot-migrate needs it to keep the key your users already trust."
        return
    fi

    # The env file lets knot-migrate cross-check the owner and hostname
    # against the old database.
    local legacy_env="$BACKUP_DIR/knot-migrate.env"
    cat > "$legacy_env" <<EOF
KNOT_SERVER_HOSTNAME=${KNOT_HOSTNAME}
KNOT_SERVER_OWNER=${OWNER_DID}
KNOT_REPO_SCAN_PATH=${BACKUP_DIR}/repositories
EOF
    local args=(
        --source-db "$BACKUP_DIR/knotserver.db"
        --env-file "$legacy_env"
        --host-key "$BACKUP_DIR/keys/ssh_host_ed25519_key"
        --plc-url "$PLC_DIRECTORY"
        --contact-email "$CONTACT_EMAIL"
        --target "$KNOT_DIR"
    )

    local key
    key="$(cat "$MASTER_KEY_FILE")"
    {
        echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) knot-migrate (dry run) ==="
    } >> "$MIGRATION_LOG"
    if ! KNOT_MASTER_KEY="$key" /usr/local/bin/knot-migrate "${args[@]}" --dry-run 2>&1 | tee -a "$MIGRATION_LOG"; then
        find "$KNOT_DIR" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
        block_start "migrating from the Go knot failed its rehearsal (knot-migrate --dry-run); knot-migrate's report is in the app logs and $MIGRATION_LOG. Your old data is untouched in $BACKUP_DIR."
        return
    fi
    echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) knot-migrate ===" >> "$MIGRATION_LOG"
    if ! KNOT_MASTER_KEY="$key" /usr/local/bin/knot-migrate "${args[@]}" 2>&1 | tee -a "$MIGRATION_LOG"; then
        find "$KNOT_DIR" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
        block_start "migrating from the Go knot failed (knot-migrate); knot-migrate's report is in the app logs and $MIGRATION_LOG. Your old data is untouched in $BACKUP_DIR; the next restart retries."
        return
    fi

    # knot-migrate wrote config.toml as a record of what it chose; the
    # knot reads /etc/knot/config.toml (below).
    date -u +%Y-%m-%dT%H:%M:%SZ > "$MIGRATED_MARKER"
    log "migration from the Go knot complete."
}

if has_legacy_data && [[ ! -f "$MIGRATED_MARKER" && ! -f "$SENTINEL_FAILED" ]]; then
    if [[ -n "$OWNER_DID" ]]; then
        migrate_go_knot
    else
        # The setup page asks for the DID; the migration runs on the
        # restart after it's saved.
        log "the Go knot's data will be migrated once the owner DID is set."
    fi
fi

# ----------------------------------------------------------------------
# Knot state
# ----------------------------------------------------------------------
mkdir -p "$KNOT_DIR/repos" "$KNOT_DIR/lfs"
# The knot runs as the git user; it must own its state.  The master key
# stays root-only: start.sh hands it to the knot in the environment.
chown -R git:git "$KNOT_DIR"

mkdir -p /etc/knot
cat > "$CONFIG_FILE" <<EOF
# Generated by openhost-init.sh on every boot.  Do not edit.
[server]
hostname = "${KNOT_HOSTNAME}"
admins = ["${OWNER_DID:-did:plc:unset}"]
listen_addr = "127.0.0.1:5555"
ssh_listen_addr = "0.0.0.0:22"
ssh_host_key_file = "${KNOT_DIR}/ssh_host_key"
appview_endpoint = "${APPVIEW_ENDPOINT}"

[repo]
scan_path = "${KNOT_DIR}/repos"

[git]
# SHA-1, as the Go knot's repos are, so repos stay mirrorable to and
# from GitHub and other SHA-1 hosts.
object_format = "sha1"

[secrets]
sealed_key_file = "${KNOT_DIR}/sealed-keys"
master_key_env = "KNOT_MASTER_KEY"

[atproto]
plc_directory = "${PLC_DIRECTORY}"

[lfs]
store_path = "${KNOT_DIR}/lfs"

[contact]
email = "${CONTACT_EMAIL}"

[xrpc]
# auth_proxy.py forwards the router's X-Forwarded-For (the real client
# address) from loopback, so the knot rate-limits per client.
trusted_proxy_header = "x-forwarded-for"
trusted_proxies = ["127.0.0.1/32"]
EOF
chmod 0644 "$CONFIG_FILE"

cat > "$ENV_FILE" <<EOF
# Generated by openhost-init.sh; sourced by start.sh.  Do not edit.
export OPENHOST_TANGLED_SENTINEL_NO_OWNER='${SENTINEL_NO_OWNER}'
export OPENHOST_TANGLED_SENTINEL_FAILED='${SENTINEL_FAILED}'
export OPENHOST_TANGLED_MASTER_KEY_FILE='${MASTER_KEY_FILE}'
export OPENHOST_TANGLED_HOSTNAME='${KNOT_HOSTNAME}'
export OPENHOST_TANGLED_OWNER_DID_FILE='${OWNER_DID_FILE}'
export OPENHOST_TANGLED_OWNER_USERNAME='${OWNER_USERNAME}'
EOF
chmod 0644 "$ENV_FILE"

log "knot hostname: ${KNOT_HOSTNAME}"
log "owner DID:     ${OWNER_DID:-<unset>}"
log "contact email: ${CONTACT_EMAIL}"
log "state dir:     ${KNOT_DIR}"
log "appview:       ${APPVIEW_ENDPOINT}"
log "done"
