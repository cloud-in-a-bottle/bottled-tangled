# openhost-tangled

A self-hosted [Tangled](https://tangled.org) **knot** — the git data
server for Tangled, the AT-Protocol-based social coding platform —
packaged for OpenHost.

## What this is

Tangled splits a code-hosting platform into pieces that federate over
the [AT Protocol](https://atproto.com/) (the same protocol as Bluesky):

- An **AppView** (the hosted `tangled.org`) provides the web UI, the
  social graph, and issues/pull-requests stored as portable ATProto
  records.
- A **knot** holds the actual git repositories and serves them. Anyone
  can run one.

This app runs **your own knot**. Your repos live on your OpenHost
instance; you keep using `tangled.org` (or any AppView) for the web UI,
and it federates with your knot. Your identity is your ATProto DID
(e.g. your Bluesky account) — there are no knot-local accounts.

Bundled:

- **knot** — the upstream Go git data server (built from source, pinned
  release).
- **sshd** — for `git push` (the knot only accepts pushes over SSH).
- **auth_proxy.py** — the OpenHost HTTP seam: health probe, a setup
  page until you configure your owner DID, and a transparent forward to
  the knot.

## Setup

1. Deploy this app. On first boot, opening the app URL (while signed in
   to your OpenHost zone) shows a short setup form, because the knot
   doesn't yet know which ATProto identity owns it.
2. Find your ATProto DID: sign in at
   [tangled.org](https://tangled.org) with your Bluesky/ATProto account
   and open [Settings](https://tangled.org/settings) — it looks like
   `did:plc:xxxxxxxx`.
3. Paste that DID into the setup form and click **Save & start knot**.
   The knot restarts with your identity and the page becomes your
   knot's MOTD. (The DID is persisted to app data; you only do this
   once. The form is owner-only — the OpenHost router stamps the owner,
   and the DID cannot be set by anonymous visitors.)
4. In [tangled.org → Settings → Knots](https://tangled.org/settings/knots),
   add your knot's domain (`tangled.<your-zone>`) and click **verify**.
   This publishes a record to your PDS announcing the knot, federating
   it with the network.
5. Create repositories on your knot from the Tangled web UI, clone over
   HTTPS, and push over SSH.

> Note: OpenHost doesn't (yet) expose custom app environment variables,
> so the owner DID is configured through the in-app form above rather
> than an env var. If a future OpenHost version does inject env vars, or
> for a manual deploy, setting `KNOT_OWNER_DID` in the environment takes
> precedence over the form.

### Cloning and pushing

- **Clone/fetch (HTTPS):**
  `git clone https://tangled.<your-zone>/<your-handle>/<repo>`
- **Push (SSH):** pushes go over SSH on the dedicated port this app
  exposes (host port `9422` by default; see `[[ports]]` in
  `openhost.toml`). The Tangled UI and the knot's own error messages
  give you the exact `git@…` remote to use. Auth is by the SSH keys you
  registered with your ATProto account — the knot fetches them for your
  DID; there is no separate knot password.

## Auth / federation model

There is no OpenHost SSO here because a knot has no browser login:

- **HTTP is public** (`public_paths = ["/"]`). git-over-HTTP
  clone/fetch, the `/xrpc/*` federation API, the `/events` oplog
  WebSocket, and the MOTD are all machine-to-machine or public by
  nature and cannot perform a browser login. The knot enforces its own
  ATProto-based auth: anonymous HTTP is read-only; privileged XRPC calls
  require AppView service-auth JWTs.
- **Push is SSH + ATProto keys.** The knot's `AuthorizedKeysCommand`
  (`knot keys`) fetches the pushing user's registered SSH public keys
  from the AppView by DID, so only keys tied to authorized ATProto
  identities can push. No knot-local accounts or passwords.

## Architecture

```
  git client ──HTTPS──▶ OpenHost router (public) ──▶ :8080 auth_proxy
  / AppView /                                          │
  browser                                              ├─ /_healthz → local 200
                                                       ├─ owner DID unset? → setup page
                                                       └─ else → 127.0.0.1:5555  knot
                                                                     (git-http, /xrpc,
                                                                      /events, MOTD)

  git push ──SSH──▶ host:9422 ──▶ container sshd ──▶ knot guard
                    (AuthorizedKeysCommand = `knot keys`, keyed by ATProto DID)

  persistent: repositories/  knotserver.db  keys/   (under $OPENHOST_APP_DATA_DIR)
```

## Configuration

The owner DID is normally set through the in-app setup form (see
Setup). If your OpenHost version supports app environment variables, or
for a manual deploy, these are also honored (env takes precedence over
the form):

| Var | Required | Description |
|-----|----------|-------------|
| `KNOT_OWNER_DID` | via form or env | Your ATProto DID (`did:plc:…` or `did:web:…`). The knot won't start until this is set. |
| `KNOT_APPVIEW_ENDPOINT` | no | AppView to federate with (default `https://tangled.org`). |

The knot's public hostname is derived automatically as
`tangled.<your-zone-domain>`.

## Persistence

`$OPENHOST_APP_DATA_DIR` (typically `/data/app_data/tangled/`) holds:

- `repositories/` — your bare git repos (the durable data; back this up).
- `knotserver.db` — knot metadata (members, collaborators, SSH-key
  cache, federation cursor).
- `keys/` — sshd host keys (stable across restarts, so `known_hosts`
  keeps validating).

No passwords or long-lived secrets are written to disk.

## Known limitations / scope cuts

- **No spindle (CI).** Tangled's CI runner (`spindle`) needs NixOS
  microVMs and is out of scope for a single OpenHost container. Repos,
  issues, and pull requests all work without it.
- **Alpha software.** Tangled is alpha; the knot protocol still changes
  between releases (see the pinned `KNOT_TAG` in the Dockerfile).
- **Web UI is the AppView's.** This app is the git backend; you browse
  and manage repos through `tangled.org` (or any AppView), which
  federates with your knot.

## Files

- `openhost.toml` — OpenHost manifest (HTTP via router; SSH via `[[ports]]`).
- `Dockerfile` — builds the knot binary; installs sshd + git.
- `openhost-init.sh` — first-boot init: hostname/owner, persistent dirs,
  sshd host keys, env file.
- `start.sh` — bash supervisor (auth_proxy + sshd + knot).
- `auth_proxy.py` — health + owner-setup page + transparent forward.
- `sshd_tangled.conf` — sshd drop-in wiring `knot keys` for push auth.
