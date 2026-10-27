import time
from collections.abc import Callable

import requests
from openhost_test_harness import OpenhostStack

# Matches auth_proxy.py's did:plc pattern (24 base32 chars). Not a real identity: the knot only stores and reports
# its owner DID at startup; resolving it (federation) is best-effort and not needed here.
OWNER_DID = "did:plc:upstreamcoveragetest2345"


def _poll(
    stack: OpenhostStack, what: str, attempt: Callable[[], requests.Response], ok: Callable[[requests.Response], bool]
) -> requests.Response:
    deadline = time.monotonic() + 180
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
            raise AssertionError(f"{what}; last response {last}\n\napp logs:\n{stack.app_logs()[-5000:]}")
        time.sleep(3)


def test_anonymous_visitors_reach_the_knot_but_cannot_configure_it(stack: OpenhostStack) -> None:
    """public_paths = ["/"]: git/XRPC clients reach the knot anonymously, but only the owner can set its owner DID."""
    page = _poll(
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

    # The proxy exits so the container restarts with the DID; the knot then reports it over XRPC.
    owner = _poll(
        stack,
        "knot did not come up reporting the owner DID",
        lambda: stack.owner_session.get(f"{stack.url}/xrpc/sh.tangled.owner", timeout=30),
        lambda r: r.status_code == 200,
    )
    assert owner.json() == {"owner": OWNER_DID}, owner.text[:300]

    # Federation (the AppView, other knots) and git clients call these anonymously.
    for path in ("/xrpc/sh.tangled.owner", "/"):
        public = requests.get(f"{stack.url}{path}", allow_redirects=False, timeout=30)
        assert public.status_code == 200 and "setup" not in public.text.lower(), (
            f"anonymous GET {path} on the running knot: {public.status_code}: {public.text[:500]}"
        )

    # Owner identity and knot state live in the app data dir, so they survive redeploys.
    assert (stack.data_dir / "owner-did").read_text().strip() == OWNER_DID
    assert (stack.data_dir / "knotserver.db").exists(), sorted(p.name for p in stack.data_dir.iterdir())
