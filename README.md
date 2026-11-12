# bottled-tangled

A self-hosted [Tangled](https://tangled.org) **knot** — the server that holds your repositories for Tangled, the AT-Protocol-based social coding platform — packaged for Cloud in a Bottle.

## What this is

Tangled splits a code-hosting platform into pieces that federate over the [AT Protocol](https://atproto.com/) (the same protocol as Bluesky):

- An **AppView** (the hosted `tangled.org`) provides the web UI and the social graph.
- A **knot** holds repositories: their git data and, since Tangled v1.20.0, their issues, pull requests, comments, labels and collaborators. Anyone can run one.

This app runs **your own knot**. Your repos live on your Cloud in a Bottle instance; you keep using `tangled.org` (or any AppView) for the web UI, and it federates with your knot. Your identity is your ATProto DID (e.g. your Bluesky account) — there are no knot-local accounts.

Bundled:

- **knot-server** — upstream's Rust knot, built from source at a pinned release. It serves git over HTTP and over SSH itself (no sshd), and keeps all its state in git plus a sealed key file (no database).
- **knot-migrate** — upstream's offline converter from the old Go knot, run once on installs upgraded from app 1.x (see [Upgrading from app 1.x](#upgrading-from-app-1x)).
- **auth_proxy.py** — the Cloud in a Bottle HTTP seam: health probe, a setup page until you configure your owner DID, and a transparent forward to the knot.

## Setup

1. Deploy this app. On first boot, opening the app URL (while signed in to your Cloud in a Bottle zone) shows a short setup form, because the knot doesn't yet know which ATProto identity owns it.
2. Find your ATProto DID: sign in at [tangled.org](https://tangled.org) with your Bluesky/ATProto account and open [Settings](https://tangled.org/settings) — it looks like `did:plc:xxxxxxxx`.
3. Paste that DID into the setup form and click **Save & start knot**. The knot restarts with your identity and the page becomes your knot's homepage. (The DID is persisted to app data; you only do this once. The form is owner-only — the Cloud in a Bottle router stamps the owner, and the DID cannot be set by anonymous visitors.)
4. In [tangled.org → Settings → Knots](https://tangled.org/settings/knots), add your knot's domain (`tangled.<your-zone>`) and click **verify**.
5. Create repositories on your knot from the Tangled web UI, clone over HTTPS, and push over SSH.

> Note: Cloud in a Bottle doesn't (yet) expose custom app environment variables, so the owner DID is configured through the in-app form above rather than an env var. For a manual deploy, setting `KNOT_OWNER_DID` in the environment takes precedence over the form.

`cloudinabottle.toml` sets `prebuilt_image`, so Cloud in a Bottle pulls our signed prebuilt image (built by CI from each release tag) instead of building the Dockerfile. To fork and modify this app, comment out `prebuilt_image` so your instance builds from source (a Rust build: allow 20–40 minutes).

### Cloning and pushing

- **Clone/fetch (HTTPS):** `git clone https://tangled.<your-zone>/<repo-did>` (the Tangled UI shows each repo's clone URL).
- **Push (SSH):** the knot's SSH server is exposed on the dedicated host port this app maps (`9422` by default; see `[[ports]]` in `cloudinabottle.toml`), so use `ssh://git@tangled.<your-zone>:9422/<repo-did>`. The Tangled UI assumes port 22, so either edit the URL or add a `Host` block with `Port 9422` to `~/.ssh/config`. Auth is by the SSH keys you publish on your ATProto account; the knot reads them from your PDS (cached for up to an hour), and there is no separate knot password.

## The master key

The knot signs its records, and controls each repository's identity, with keys kept in a sealed key store (`knot/sealed-keys`), encrypted with a master key. The app generates the master key on first boot and keeps it in the app data dir as `knot-master-key` (mode 0600), so the platform's backups of the app data dir cover it.

**Losing the master key (or `knot/sealed-keys`; the two are useless without each other) means the knot loses control of every repository's identity.** Your git data stays readable, but the knot can no longer sign for your repos, and recovering means re-creating them. Upstream's advice is to keep a copy of the key off the server: if your app data isn't backed up somewhere other than this server, copy `knot-master-key` somewhere safe. The app never replaces an existing key; if the key file goes missing while the key store exists, the knot refuses to start and the app page says so.

## Upgrading from app 1.x

App 1.x ran upstream's Go knot (v1.16.1-alpha); v2.0.0 runs the Rust knot (v1.20.0), whose data format is different. The first boot of v2.0.0 on an install with Go-knot data migrates it automatically, once:

1. The Go knot's files (`knotserver.db`, `repositories/`, the sshd host keys in `keys/`) are moved, untouched, into `go-knot-backup/` in the app data dir.
2. `knot-migrate` rehearses (`--dry-run`), then converts that data into `knot/`, copying the repositories (so the backup stays exactly as it was) and importing the old SSH host key (so users' `known_hosts` entries keep matching). Its report is in the logs and in `knot-migrate.log`.
3. `knot/.migrated-from-go-knot` records that it's done; later boots skip the migration.

If the migration fails (eg not enough disk for a second copy of the repositories, or a repository `knot-migrate` can't represent), nothing is half-migrated: the partial `knot/` is deleted, the knot stays stopped, the app page tells the owner why (others get a 503), and the next restart retries. The old data stays untouched in `go-knot-backup/`.

After migrating, re-verify the knot at [tangled.org → Settings → Knots](https://tangled.org/settings/knots). Once you're happy with it, you can delete `go-knot-backup/` to reclaim its space. To roll back to app 1.x instead, stop the app, move `go-knot-backup/knotserver.db*`, `repositories/` and `keys/` back to the top of the app data dir, and deploy the 1.x version; changes made on the Rust knot are not carried back.

## Auth / federation model

**Cloud in a Bottle SSO gates configuration; your platform username is inherited.** The one owner-facing action on the knot — setting the ATProto owner DID — is behind Cloud in a Bottle SSO: the setup form is served only to the authenticated zone owner (the router stamps `X-OpenHost-Is-Owner`), greets you by your Cloud in a Bottle username (`OPENHOST_OWNER_USERNAME`), and rejects anonymous attempts to set the owner.

Beyond that, a knot deliberately has **no browser login of its own** — Tangled's identity layer is ATProto (your Bluesky DID), decoupled from any hosting platform:

- **HTTP is public** (`public_paths = ["/"]`). git-over-HTTP clone/fetch, the `/xrpc/*` federation API, the event stream WebSocket at `/xrpc/com.atproto.sync.subscribeRepos`, and the homepage are all machine-to-machine or public by nature and cannot perform a browser login. The knot enforces its own ATProto-based auth: anonymous HTTP is read-only; privileged XRPC calls require service-auth JWTs.
- **Push is SSH + ATProto keys.** The knot's SSH server accepts the keys each user publishes on their PDS, and only for repos they may push to.

The owner DID can't be auto-derived from your Cloud in a Bottle account, because it must be a real ATProto identity (with a PDS and published SSH keys) that the Tangled network can verify.

## Architecture

```
  git client ──HTTPS──▶ OpenHost router (public) ──▶ :8080 auth_proxy
  / AppView /                                          │
  browser                                              ├─ /_healthz → local 200
                                                       ├─ owner DID unset? → setup page
                                                       ├─ knot blocked? → error page (503)
                                                       └─ else → 127.0.0.1:5555  knot-server
                                                                     (git-http, /xrpc,
                                                                      subscribeRepos, homepage)

  git push ──SSH──▶ host:9422 ──▶ container :22  knot-server (built-in SSH,
                                                 keys read from each user's PDS)

  persistent: knot/  knot-master-key  owner-did   (under $OPENHOST_APP_DATA_DIR)
```

## Configuration

`openhost-init.sh` writes the knot's config (`/etc/knot/config.toml`) on every boot, from the platform:

| Setting | Source |
|-----|----------|
| hostname (the knot's identity, `did:web:<hostname>`) | `tangled.<your-zone-domain>` (`$OPENHOST_APP_NAME.$OPENHOST_ZONE_DOMAIN`) |
| admin (owner) | the owner DID from the setup form, or `KNOT_OWNER_DID` |
| contact email (required, published by `org.tangled.knot.describeKnot`) | `<your platform username>@<your-zone-domain>`, or `KNOT_CONTACT_EMAIL` |
| LFS store (required; LFS objects and issue/PR attachments) | `knot/lfs` in the app data dir |
| AppView | `https://tangled.org`, or `KNOT_APPVIEW_ENDPOINT` |
| PLC directory | `https://plc.directory`, or `KNOT_PLC_DIRECTORY` |

New repositories use SHA-1, like the Go knot's, so they can be mirrored to and from GitHub. The knot trusts `X-Forwarded-For` only from the local proxy, so it rate-limits per client.

## Persistence

`$OPENHOST_APP_DATA_DIR` (typically `/data/app_data/tangled/`) holds:

- `knot/` — the knot's whole state: `repos/` (every repository plus its issues, pull requests and ACLs; git is the database), `lfs/`, `sealed-keys`, and `ssh_host_key` (stable across restarts, so `known_hosts` keeps validating). On migrated installs it also holds `repo-signing-keys.json`, the per-repo keys `knot-migrate` exported (in the clear, like the old database held them), which the knot seals into its key store on each start.
- `knot-master-key` — see [The master key](#the-master-key).
- `owner-did` — the owner DID from the setup form.
- `go-knot-backup/`, `knot-migrate.log` — on installs migrated from app 1.x only.

## Known limitations / scope cuts

- **No spindle (CI).** Tangled's CI runner (`spindle`) needs NixOS microVMs and is out of scope for a single Cloud in a Bottle container. Repos, issues, and pull requests all work without it.
- **SSH on a non-standard port.** The Tangled UI's SSH clone URLs assume port 22; see [Cloning and pushing](#cloning-and-pushing).
- **The proxy buffers HTTP bodies** and rejects chunked request bodies, so very large HTTP fetches are limited; use SSH for big repos.
- **Web UI is the AppView's.** This app is the repository backend; you browse and manage repos through `tangled.org` (or any AppView), which federates with your knot.

## Files

- `cloudinabottle.toml` — Cloud in a Bottle manifest (HTTP via router; SSH via `[[ports]]`).
- `Dockerfile` — builds `knot-server` and `knot-migrate` from source at the pinned commit.
- `openhost-init.sh` — every-boot init: hostname/owner, master key, the one-time Go-knot migration, the knot's config.
- `start.sh` — bash supervisor (auth_proxy + knot-server).
- `auth_proxy.py` — health + owner-setup page + error page + transparent forward.
