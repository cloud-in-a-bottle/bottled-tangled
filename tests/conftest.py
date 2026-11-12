import socket
import subprocess
import time
from collections.abc import Callable, Iterator
from urllib.parse import urlsplit

import pytest
import requests
from openhost_test_harness import OpenhostStack

# The Rust knot is a large cargo build; the first deploy in a CI job builds it, later ones hit podman's layer cache.
DEPLOY_TIMEOUT = 3600

# Matches auth_proxy.py's did:plc pattern (24 base32 chars). Not a real identity: the knot stores its owner DID at
# startup; resolving it (federation) is best-effort and not needed here. The legacy fixture's owner is the same DID.
OWNER_DID = "did:plc:upstreamcoveragetest2345"

# The manifest's fixed [[ports]] host port for the knot's SSH server.
SSH_HOST_PORT = 9422


@pytest.fixture(scope="module")
def stack() -> Iterator[OpenhostStack]:
    """Builds the Dockerfile and deploys a fresh install through the real router; returns once the app is running.

    Module-scoped, not session-scoped: the manifest pins the SSH host port, so only one stack can run at a time.
    """
    with OpenhostStack(deploy_timeout=DEPLOY_TIMEOUT) as s:
        yield s


def poll(
    stack: OpenhostStack,
    what: str,
    attempt: Callable[[], requests.Response],
    ok: Callable[[requests.Response], bool],
    timeout: float = 300,
) -> requests.Response:
    deadline = time.monotonic() + timeout
    response: requests.Response | None = None
    while True:
        try:
            response = attempt()
            if ok(response):
                return response
        except requests.ConnectionError:
            pass
        if time.monotonic() > deadline:
            last = f"{response.status_code}: {response.text[:500]}" if response is not None else "no response"
            raise AssertionError(f"{what}; last response {last}\n\napp logs:\n{stack.app_logs()[-8000:]}")
        time.sleep(3)


def wait_for_knot(stack: OpenhostStack) -> None:
    poll(
        stack,
        "knot did not come up",
        lambda: requests.get(f"{stack.url}/xrpc/_health", timeout=30),
        lambda r: r.status_code == 200 and "knot" in r.text,
    )


def knot_hostname(stack: OpenhostStack) -> str:
    """The knot's hostname: the app's zone host without the dev zone's port (a knot hostname can't carry one)."""
    return f"{stack.app_name or stack.manifest.name}.{stack.zone_domain.split(':')[0]}"


def ssh_host_keys(timeout: float = 60) -> str:
    """The ed25519 host key the knot's SSH server offers on the manifest's host port, as `ssh-keyscan` prints it."""
    deadline = time.monotonic() + timeout
    while True:
        result = subprocess.run(
            ["ssh-keyscan", "-T", "10", "-t", "ed25519", "-p", str(SSH_HOST_PORT), "127.0.0.1"],
            capture_output=True,
            text=True,
        )
        if "ssh-ed25519" in result.stdout or time.monotonic() > deadline:
            return result.stdout
        time.sleep(3)


def websocket_upgrade_status(stack: OpenhostStack, path: str) -> str:
    """Opens a WebSocket handshake to `path` through the router, anonymously; returns the response's status line."""
    url = urlsplit(stack.url)
    assert url.port is not None, stack.url
    with socket.create_connection(("127.0.0.1", url.port), timeout=30) as conn:
        conn.sendall(
            (
                f"GET {path} HTTP/1.1\r\nHost: {url.netloc}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        return conn.makefile("rb").readline().decode("latin-1").strip()
