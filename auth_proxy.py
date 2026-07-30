"""OpenHost front proxy for the Tangled knot.

A knot has no browser login — identity is your ATProto DID, and all of
its HTTP surface (git-over-HTTP clone/fetch, the XRPC federation API,
the /events oplog WebSocket, the MOTD at /) is machine-to-machine or
public by nature.  So this sidecar does NOT do an SSO auto-login dance.
Its jobs are:

  * Serve ``/_healthz`` locally with a static 200 so OpenHost's
    readiness probe flips to "alive" the moment the proxy binds,
    independent of the knot's own startup.

  * When the operator hasn't set their owner DID yet (the knot can't
    start without it), serve a clear setup page on every HTML
    navigation explaining how to set ``KNOT_OWNER_DID``.  Controlled by
    the ``OPENHOST_TANGLED_SENTINEL_NO_OWNER`` env var written by
    openhost-init.sh.

  * Otherwise transparently forward everything to the knot on
    127.0.0.1:5555, including the ``/events`` WebSocket (tunnelled) and
    git-over-HTTP requests (buffered, then re-sent with a correct
    Content-Length; capped at MAX_BODY_BYTES).

The whole app is public in openhost.toml (git + federation clients
can't perform OpenHost's browser SSO), so we don't gate anything here;
we only add health, the missing-owner setup page, and defensive
stripping of any client-supplied ``X-OpenHost-*`` trust headers.

Adapted from openhost-unciv/auth_proxy.py.
"""

from __future__ import annotations

import http.client
import logging
import os
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import AbstractSet, Iterable

ALWAYS_STRIP_HEADERS = frozenset(
    h.lower() for h in ("X-OpenHost-Is-Owner", "X-OpenHost-User")
)

HOP_BY_HOP_HEADERS = frozenset(
    h.lower()
    for h in (
        "Connection",
        "Keep-Alive",
        "Proxy-Authenticate",
        "Proxy-Authorization",
        "TE",
        "Trailer",
        "Transfer-Encoding",
        "Upgrade",
        "Host",
        "Content-Length",
    )
)

CLIENT_READ_TIMEOUT_SECONDS = 60

# Git packs can be large (pushing/cloning big repos).  Cap generously.
MAX_BODY_BYTES = 512 * 1024 * 1024

HEALTH_PATH = "/_healthz"

# Owner-gated config endpoint: the setup form POSTs the ATProto owner
# DID here.  Only requests the OpenHost router stamps as the owner
# (X-OpenHost-Is-Owner: true) are accepted; the router sets this header
# afresh on every request, and we strip client-supplied copies before
# ever forwarding upstream, so it can't be forged.
OWNER_HEADER_NAME = "X-OpenHost-Is-Owner"
SET_OWNER_PATH = "/_openhost/set-owner"

# A DID we accept for the knot owner.  Deliberately conservative:
# did:plc:<base32ish> or did:web:<host...>.
import re as _re  # noqa: E402

_DID_RE = _re.compile(r"^did:(plc:[a-z2-7]{24}|web:[A-Za-z0-9.\-:%]+)$")

logging.basicConfig(
    level=os.environ.get("AUTH_PROXY_LOG_LEVEL", "INFO"),
    format="[tangled-proxy] %(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("tangled_proxy")


def _strip_headers(
    headers: Iterable[tuple[str, str]], drop: AbstractSet[str]
) -> list[tuple[str, str]]:
    drop_lower = {h.lower() for h in drop}
    return [(k, v) for k, v in headers if k.lower() not in drop_lower]


_STYLE = (
    "<style>body{background:#0f1117;color:#e1e4e8;font-family:system-ui,sans-serif;"
    "max-width:660px;margin:3rem auto;padding:0 1.2rem;line-height:1.6}"
    "h1{font-size:1.5rem}code{background:#21262d;padding:.15em .4em;border-radius:6px;"
    "font-family:ui-monospace,monospace}a{color:#58a6ff}ol{padding-left:1.2rem}"
    "li{margin:.5rem 0}.box{background:#161b22;border:1px solid #30363d;border-radius:8px;"
    "padding:1rem 1.2rem;margin:1rem 0}input[type=text]{width:100%;padding:.6em;"
    "border-radius:6px;border:1px solid #30363d;background:#0d1117;color:#e1e4e8;"
    "font-family:ui-monospace,monospace}button{margin-top:.8rem;padding:.6em 1.2em;"
    "border-radius:6px;border:1px solid #2ea043;background:#238636;color:#fff;"
    "font-size:1rem;cursor:pointer}.err{color:#f85149}.muted{color:#8b949e;font-size:.9rem}"
    "</style>"
)


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _setup_html(hostname: str, is_owner: bool, error: str = "", username: str = "") -> bytes:
    safe = _esc(hostname or "your-knot-domain")
    user = _esc(username or "owner")
    err_html = f"<p class=\"err\">{_esc(error)}</p>" if error else ""
    if is_owner:
        # Owner sees an interactive form to set the DID right here.
        # We greet them by the platform username inherited from OpenHost
        # (OPENHOST_OWNER_USERNAME) so the SSO identity is visible.
        body_inner = (
            "<h1>Set your Tangled owner DID</h1>"
            f"<p>Signed in as <b>{user}</b> via OpenHost SSO. "
            "This is your self-hosted Tangled <b>knot</b> (git data server) at "
            f"<code>{safe}</code>. Tell it which ATProto identity owns it to start it.</p>"
            f"{err_html}"
            "<div class=\"box\">"
            "<p>Find your DID: sign in at <a href=\"https://tangled.org\">tangled.org</a> "
            "with your Bluesky/ATProto account, open "
            "<a href=\"https://tangled.org/settings\">Settings</a>; it looks like "
            "<code>did:plc:xxxxxxxxxxxxxxxxxxxxxxxx</code>.</p>"
            f"<form method=\"POST\" action=\"{SET_OWNER_PATH}\">"
            "<label for=\"did\">Owner DID</label>"
            "<input type=\"text\" id=\"did\" name=\"did\" placeholder=\"did:plc:...\" "
            "autocomplete=\"off\" spellcheck=\"false\" required>"
            "<button type=\"submit\">Save & start knot</button>"
            "</form></div>"
            "<p class=\"muted\">After saving, the knot restarts and this page "
            "becomes your knot's MOTD. Then add the knot at "
            "<a href=\"https://tangled.org/settings/knots\">tangled.org → Settings → "
            f"Knots</a> (domain <code>{safe}</code>) and click <b>verify</b> to "
            "federate it.</p>"
        )
    else:
        # Non-owner visitor: no form, just an explanation.
        body_inner = (
            "<h1>Tangled knot — not yet configured</h1>"
            f"<p>This is a self-hosted Tangled knot at <code>{safe}</code>. Its owner "
            "hasn't finished setting it up yet. If this is your knot, open it while "
            "signed in to your OpenHost zone to configure it.</p>"
        )
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>Tangled knot — setup</title>{_STYLE}</head><body>"
        f"{body_inner}</body></html>"
    ).encode("utf-8")


def _saved_html(hostname: str) -> bytes:
    safe = _esc(hostname or "your-knot-domain")
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>Tangled knot — starting</title>"
        "<meta http-equiv=\"refresh\" content=\"8\">"
        f"{_STYLE}</head><body>"
        "<h1>Owner DID saved — starting your knot…</h1>"
        "<p>The knot is (re)starting with your identity. This page refreshes "
        "automatically; in a few seconds it will become your knot's MOTD.</p>"
        "<div class=\"box\"><p>Next: add this knot at "
        "<a href=\"https://tangled.org/settings/knots\">tangled.org → Settings → "
        f"Knots</a> (domain <code>{safe}</code>) and click <b>verify</b> to "
        "federate it, then create repositories from the Tangled web UI.</p></div>"
        "</body></html>"
    ).encode("utf-8")


class KnotProxyHandler(BaseHTTPRequestHandler):
    upstream_host: str = "127.0.0.1"
    upstream_port: int = 5555
    sentinel_no_owner: str = ""
    knot_hostname: str = ""
    owner_did_file: str = ""
    owner_username: str = "owner"

    def log_message(self, format: str, *args) -> None:  # noqa: A002, N802
        log.info("%s - " + format, self.address_string(), *args)

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch()

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch()

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch()

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch()

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch()

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._dispatch()

    def _safe_send_error(self, code: int, message: str) -> None:
        try:
            self.send_error(code, message)
        except OSError as exc:
            log.debug("client disconnected before error response: %s", exc)

    def _path_only(self) -> str:
        return self.path.split("?", 1)[0]

    def _owner_missing(self) -> bool:
        return bool(self.sentinel_no_owner) and os.path.exists(self.sentinel_no_owner)

    def _is_owner(self) -> bool:
        return self.headers.get(OWNER_HEADER_NAME, "").lower() == "true"

    def _dispatch(self) -> None:
        try:
            self.connection.settimeout(CLIENT_READ_TIMEOUT_SECONDS)
        except OSError:
            pass

        path = self._path_only()

        if path == HEALTH_PATH:
            self._serve_health()
            return

        # Owner submits their DID here.  Owner-only, and only meaningful
        # while the knot is unconfigured.
        if path == SET_OWNER_PATH:
            self._handle_set_owner()
            return

        # If the owner DID isn't configured, the knot isn't running.
        # Serve the 200 setup page (an owner-gated form) for the app
        # root and any GET/HEAD navigation.  This also satisfies
        # OpenHost's readiness probe, which polls GET / with Accept:
        # */*.  Return 503 only for clearly machine/git/XRPC requests,
        # where a 503 correctly signals "backend not up yet".
        if self._owner_missing():
            is_git_or_api = (
                path.startswith("/xrpc")
                or path.startswith("/admin")
                or path.endswith("/info/refs")
                or "git-upload-pack" in path
                or "git-receive-pack" in path
                or "git-upload-archive" in path
            )
            if self.command in ("GET", "HEAD") and not is_git_or_api:
                self._serve_setup()
            else:
                self._safe_send_error(503, "knot not configured: set owner DID")
            return

        # WebSocket (/events) → tunnel.
        upgrade = self.headers.get("Upgrade", "").lower().strip()
        if upgrade == "websocket":
            self._proxy_websocket()
            return

        self._proxy()

    def _handle_set_owner(self) -> None:
        # Only the OpenHost-authenticated owner may set the DID.  The
        # router stamps X-OpenHost-Is-Owner fresh; forged copies are
        # stripped before any upstream forward, so this is trustworthy.
        if self.command != "POST":
            self._safe_send_error(405, "method not allowed")
            return
        if not self._is_owner():
            self._safe_send_error(403, "owner only")
            return
        # Read a small form body.
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            self._safe_send_error(400, "invalid Content-Length")
            return
        if length <= 0 or length > 4096:
            self._safe_send_error(400, "invalid body")
            return
        try:
            raw = self.rfile.read(length).decode("utf-8", "replace")
        except (OSError, TimeoutError):
            self._safe_send_error(400, "body read failed")
            return
        # application/x-www-form-urlencoded: did=...
        import urllib.parse

        did = urllib.parse.parse_qs(raw).get("did", [""])[0].strip()
        if not _DID_RE.match(did):
            body = _setup_html(
                self.knot_hostname, is_owner=True,
                error=f"That doesn't look like a valid DID: {did!r}. "
                "Expected did:plc:... or did:web:...",
                username=self.owner_username,
            )
            self._send_html(400, body)
            return
        # Persist the DID for openhost-init to pick up on next start.
        try:
            if not self.owner_did_file:
                raise OSError("owner_did_file not configured")
            with open(self.owner_did_file, "w", encoding="utf-8") as fh:
                fh.write(did + "\n")
        except OSError as exc:
            log.error("failed to persist owner DID: %s", exc)
            self._send_html(
                500,
                _setup_html(self.knot_hostname, is_owner=True,
                            error="Failed to save the DID on the server. Check logs.",
                            username=self.owner_username),
            )
            return
        log.info("owner DID set to %s; requesting knot start", did)
        # Show a friendly "starting" page, then exit the proxy so the
        # supervisor (start.sh) restarts the whole container, which
        # re-runs openhost-init, reads the persisted DID, and launches
        # the knot.
        self._send_html(200, _saved_html(self.knot_hostname))
        # Give the response time to flush before we exit.
        import threading

        def _bail():
            import time
            time.sleep(1.0)
            log.info("exiting proxy to trigger container restart with owner DID")
            os._exit(0)

        threading.Thread(target=_bail, daemon=True).start()

    def _send_html(self, code: int, body: bytes) -> None:
        try:
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except OSError as exc:
            log.debug("client disconnected during html response: %s", exc)

    def _serve_health(self) -> None:
        body = b"ok\n"
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except OSError as exc:
            log.debug("client disconnected during health response: %s", exc)

    def _serve_setup(self) -> None:
        # Serve the setup page with a 200, not a 5xx.  The container is
        # genuinely alive and this page is the correct, actionable
        # response until the owner DID is set; a 5xx here would make
        # OpenHost's readiness gate fail the deploy forever (the DID can
        # only be set after a successful deploy).  Owners get an
        # interactive form; non-owners get a plain explanation.
        body = _setup_html(self.knot_hostname, is_owner=self._is_owner(), username=self.owner_username)
        self._send_html(200, body)

    def _proxy_websocket(self) -> None:
        cleaned_headers = _strip_headers(self.headers.items(), ALWAYS_STRIP_HEADERS)
        try:
            up = socket.create_connection(
                (self.upstream_host, self.upstream_port), timeout=15
            )
        except OSError as exc:
            log.warning("ws: upstream connect failed: %s", exc)
            self._safe_send_error(502, "Bad Gateway")
            return

        up_file = None
        try:
            try:
                request = f"{self.command} {self.path} HTTP/1.1\r\n"
                for k, v in cleaned_headers:
                    request += f"{k}: {v}\r\n"
                request += "\r\n"
                up.sendall(request.encode("latin-1"))
            except OSError as exc:
                log.warning("ws: upstream send failed: %s", exc)
                self._safe_send_error(502, "Bad Gateway")
                return

            up_file = up.makefile("rb")
            status_line = up_file.readline()
            if not status_line:
                self._safe_send_error(502, "Bad Gateway")
                return
            try:
                self.wfile.write(status_line)
            except OSError:
                return

            while True:
                line = up_file.readline()
                if not line:
                    return
                try:
                    self.wfile.write(line)
                except OSError:
                    return
                if line in (b"\r\n", b"\n"):
                    break

            import threading

            def _copy(src, dst):
                try:
                    while True:
                        data = src.recv(65536)
                        if not data:
                            break
                        dst.sendall(data)
                except OSError:
                    pass

            t1 = threading.Thread(target=_copy, args=(self.connection, up), daemon=True)
            t2 = threading.Thread(target=_copy, args=(up, self.connection), daemon=True)
            t1.start()
            t2.start()
            t1.join()
            t2.join()
        finally:
            if up_file is not None:
                try:
                    up_file.close()
                except OSError:
                    pass
            try:
                up.close()
            except OSError:
                pass

    def _proxy(self) -> None:
        cleaned_headers = _strip_headers(
            self.headers.items(), HOP_BY_HOP_HEADERS | ALWAYS_STRIP_HEADERS
        )
        # Preserve the client's Host so the knot builds correct absolute
        # URLs / git-remote hints.  Fall back to the configured public
        # hostname, then loopback.
        forwarded_host = self.headers.get("X-Forwarded-Host", "").strip()
        if not forwarded_host:
            forwarded_host = self.headers.get("Host", "").strip()
        if not forwarded_host:
            forwarded_host = self.knot_hostname or f"{self.upstream_host}:{self.upstream_port}"
        cleaned_headers.append(("Host", forwarded_host))

        transfer_encoding = self.headers.get("Transfer-Encoding", "").lower().strip()
        if transfer_encoding and transfer_encoding != "identity":
            self._safe_send_error(501, "Transfer-Encoding not supported")
            return

        body: bytes | None = None
        content_length_header = self.headers.get("Content-Length")
        if content_length_header:
            try:
                length = int(content_length_header)
            except ValueError:
                self._safe_send_error(400, "invalid Content-Length")
                return
            if length < 0:
                self._safe_send_error(400, "negative Content-Length")
                return
            if length > MAX_BODY_BYTES:
                self._safe_send_error(413, "request body too large")
                return
            if length > 0:
                try:
                    body = self.rfile.read(length)
                except (OSError, TimeoutError) as exc:
                    log.info("client read error: %s", exc)
                    self._safe_send_error(400, "request body read failed")
                    return
                if len(body) != length:
                    log.info("short read: expected %d, got %d", length, len(body))
                    self._safe_send_error(400, "incomplete request body")
                    return
            else:
                body = b""
        elif self.command in ("POST", "PUT", "PATCH", "DELETE"):
            body = b""

        conn = http.client.HTTPConnection(
            self.upstream_host, self.upstream_port, timeout=300
        )
        try:
            try:
                conn.putrequest(
                    self.command, self.path, skip_host=True, skip_accept_encoding=True
                )
                for key, value in cleaned_headers:
                    conn.putheader(key, value)
                if body is not None:
                    conn.putheader("Content-Length", str(len(body)))
                conn.endheaders(message_body=body)
                upstream = conn.getresponse()
            except (OSError, http.client.HTTPException) as exc:
                log.warning("upstream error: %s", exc)
                self._serve_cold_start_placeholder()
                return

            try:
                payload = upstream.read(MAX_BODY_BYTES + 1)
            except (OSError, http.client.HTTPException) as exc:
                log.warning("upstream read error: %s", exc)
                self._serve_cold_start_placeholder()
                try:
                    upstream.close()
                except Exception as close_exc:  # noqa: BLE001
                    log.debug("upstream.close() raised: %s", close_exc)
                return
            try:
                upstream.close()
            except Exception as exc:  # noqa: BLE001
                log.debug("upstream.close() raised (ignored): %s", exc)
            if len(payload) > MAX_BODY_BYTES:
                log.warning("upstream response exceeded %d bytes; 502", MAX_BODY_BYTES)
                self._safe_send_error(502, "upstream response too large")
                return

            reason = upstream.reason or ""
            try:
                self.send_response(upstream.status, reason)
                for key, value in upstream.getheaders():
                    if key.lower() in HOP_BY_HOP_HEADERS:
                        continue
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(payload)
            except OSError as exc:
                log.debug("client disconnected mid-response: %s", exc)
        finally:
            conn.close()

    def _serve_cold_start_placeholder(self) -> None:
        body = b"Tangled knot is starting; please retry shortly.\n"
        try:
            self.send_response(503)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Retry-After", "3")
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except OSError as exc:
            log.debug("client disconnected during cold-start placeholder: %s", exc)


class IPv4ThreadingServer(ThreadingHTTPServer):
    address_family = socket.AF_INET
    allow_reuse_address = True
    daemon_threads = True


def _port_from_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        port = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name}={raw!r} is not an integer: {exc}") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"{name}={raw!r} is out of range (1-65535)")
    return port


def main() -> int:
    try:
        listen_port = _port_from_env("AUTH_PROXY_LISTEN_PORT", 8080)
        upstream_port = _port_from_env("AUTH_PROXY_UPSTREAM_PORT", 5555)
    except ValueError as exc:
        log.error("invalid port configuration: %s", exc)
        return 1

    KnotProxyHandler.upstream_host = os.environ.get(
        "AUTH_PROXY_UPSTREAM_HOST", "127.0.0.1"
    ).strip()
    KnotProxyHandler.upstream_port = upstream_port
    KnotProxyHandler.sentinel_no_owner = os.environ.get(
        "OPENHOST_TANGLED_SENTINEL_NO_OWNER", ""
    ).strip()
    KnotProxyHandler.knot_hostname = os.environ.get(
        "OPENHOST_TANGLED_HOSTNAME", ""
    ).strip()
    KnotProxyHandler.owner_did_file = os.environ.get(
        "OPENHOST_TANGLED_OWNER_DID_FILE", ""
    ).strip()
    KnotProxyHandler.owner_username = (
        os.environ.get("OPENHOST_TANGLED_OWNER_USERNAME", "").strip() or "owner"
    )

    try:
        server = IPv4ThreadingServer(("0.0.0.0", listen_port), KnotProxyHandler)
    except OSError as exc:
        log.error("failed to bind listener on 0.0.0.0:%d: %s", listen_port, exc)
        return 1
    log.info(
        "listening on 0.0.0.0:%d -> %s:%d",
        listen_port,
        KnotProxyHandler.upstream_host,
        upstream_port,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
