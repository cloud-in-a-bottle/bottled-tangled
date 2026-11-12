import stat
import subprocess

import requests
from conftest import OWNER_DID, knot_hostname, poll, ssh_host_keys, wait_for_knot, websocket_upgrade_status
from openhost_test_harness import OpenhostStack


def test_anonymous_visitors_reach_the_knot_but_cannot_configure_it(stack: OpenhostStack) -> None:
    """public_paths = ["/"]: git/XRPC clients reach the knot anonymously, but only the owner can set its owner DID."""
    page = poll(
        stack,
        "anonymous GET / did not serve the app's setup page",
        lambda: requests.get(f"{stack.url}/", headers={"Accept": "text/html"}, timeout=30),
        lambda r: r.status_code == 200 and r.url.startswith(stack.url),
    )
    assert "not yet configured" in page.text and "<form" not in page.text, page.text[:1000]

    forged = requests.post(
        f"{stack.url}/_openhost/set-owner",
        data={"did": OWNER_DID},
        headers={"X-OpenHost-Is-Owner": "true"},
        timeout=30,
    )
    assert forged.status_code == 403, f"anonymous set-owner: {forged.status_code}: {forged.text[:300]}"
    assert not (stack.data_dir / "owner-did").exists(), "anonymous set-owner wrote the owner DID"


def test_owner_sets_the_did_and_the_knot_starts(stack: OpenhostStack) -> None:
    """The owner gets the setup form (greeted by their platform username); saving a DID starts the knot as that owner."""
    setup = stack.owner_session.get(f"{stack.url}/", headers={"Accept": "text/html"}, timeout=30)
    assert setup.status_code == 200, f"owner GET /: {setup.status_code}: {setup.text[:300]}"
    assert f"Signed in as <b>{stack.owner_username}</b>" in setup.text and "/_openhost/set-owner" in setup.text, (
        setup.text[:1000]
    )

    saved = stack.owner_session.post(f"{stack.url}/_openhost/set-owner", data={"did": OWNER_DID}, timeout=30)
    assert saved.status_code == 200 and "Owner DID saved" in saved.text, f"{saved.status_code}: {saved.text[:500]}"

    # The proxy exits so the container restarts with the DID, and the knot starts.
    wait_for_knot(stack)

    # The knot runs with the saved DID as its owner (no public XRPC reports it since v1.20.0).
    config = subprocess.run(
        ["podman", "exec", stack.container_name, "cat", "/etc/knot/config.toml"], capture_output=True, text=True
    ).stdout
    assert f'admins = ["{OWNER_DID}"]' in config, config

    # The knot's identity comes from the zone, and its required contact email from the platform owner and zone.
    zone_host = stack.zone_domain.split(":")[0]
    described = requests.get(f"{stack.url}/xrpc/org.tangled.knot.describeKnot", timeout=30)
    assert described.status_code == 200, f"{described.status_code}: {described.text[:300]}"
    assert described.json()["did"] == f"did:web:{knot_hostname(stack)}", described.text[:500]
    assert described.json()["contact"]["email"] == f"{stack.owner_username}@{zone_host}", described.text[:500]

    # Federation (the AppView, other knots, indexers) and git clients call these anonymously.
    for path in ("/xrpc/_health", "/"):
        public = requests.get(f"{stack.url}{path}", allow_redirects=False, timeout=30)
        assert public.status_code == 200 and "set-owner" not in public.text, (
            f"anonymous GET {path} on the running knot: {public.status_code}: {public.text[:500]}"
        )
    # The event stream moved from /events to subscribeRepos in v1.20.0; the proxy tunnels the WebSocket.
    status = websocket_upgrade_status(stack, "/xrpc/com.atproto.sync.subscribeRepos")
    assert status.startswith("HTTP/1.1 101"), status

    # The knot serves SSH itself on the manifest's host port.
    assert "ssh-ed25519" in ssh_host_keys(), stack.app_logs()[-5000:]

    # Owner identity, the knot's state and its master key live in the app data dir, so they survive redeploys.
    assert (stack.data_dir / "owner-did").read_text().strip() == OWNER_DID
    assert (stack.data_dir / "knot" / "repos").is_dir(), sorted(p.name for p in (stack.data_dir / "knot").iterdir())
    master_key = stack.data_dir / "knot-master-key"
    assert stat.S_IMODE(master_key.stat().st_mode) == 0o600, oct(master_key.stat().st_mode)
    assert not (stack.data_dir / "go-knot-backup").exists(), "a fresh install made a migration backup"
