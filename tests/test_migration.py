"""An install upgraded from app 1.x (the Go knot) migrates its data to the Rust knot on first boot, once."""

import sqlite3
import stat
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
import requests
from conftest import DEPLOY_TIMEOUT, OWNER_DID, knot_hostname, ssh_host_keys, wait_for_knot
from openhost_test_harness import OpenhostStack

FIXTURE_SQL = Path(__file__).parent / "fixtures" / "go-knot-v1.16.1.sql"
REPO_DID = "did:plc:migrationfixturerepo2345"


def _git(*args: str, cwd: Path | None = None) -> str:
    env = {
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "HOME": "/nonexistent",
        "GIT_CONFIG_NOSYSTEM": "1",
        "PATH": "/usr/bin:/bin:/usr/local/bin",
    }
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


def _seed_go_knot_data(data_dir: Path, scratch: Path) -> str:
    """Lays out the data dir as app 1.0.0 left it; returns the seeded repo's head commit."""
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "owner-did").write_text(OWNER_DID + "\n")
    with sqlite3.connect(data_dir / "knotserver.db") as db:
        db.executescript(FIXTURE_SQL.read_text())

    # The Go knot kept each repository at repositories/<repo did>.
    bare = data_dir / "repositories" / REPO_DID
    _git("init", "-q", "--bare", "-b", "main", str(bare))
    work = scratch / "work"
    _git("init", "-q", "-b", "main", str(work))
    (work / "README").write_text("migrated from the Go knot\n")
    _git("add", "README", cwd=work)
    _git("commit", "-q", "-m", "first", cwd=work)
    _git("push", "-q", str(bare), "main", cwd=work)

    # openhost-init.sh in app 1.x generated sshd host keys here on every boot.
    keys = data_dir / "keys"
    keys.mkdir()
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(keys / "ssh_host_ed25519_key")], check=True)
    return _git("rev-parse", "HEAD", cwd=work)


@pytest.fixture(scope="module")
def legacy(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[OpenhostStack, str]]:
    scratch = tmp_path_factory.mktemp("legacy")
    head: list[str] = []

    def seed(s: OpenhostStack) -> None:
        head.append(_seed_go_knot_data(s.data_dir, scratch))

    with OpenhostStack(pre_deploy=seed, deploy_timeout=DEPLOY_TIMEOUT) as s:
        yield s, head[0]


def test_go_knot_data_migrates_on_first_boot(legacy: tuple[OpenhostStack, str]) -> None:
    stack, head = legacy
    data = stack.data_dir
    seeded_host_key = (data / "go-knot-backup" / "keys" / "ssh_host_ed25519_key.pub").read_text().split()[1]

    wait_for_knot(stack)

    # The old data was moved aside untouched, and the migration was recorded.
    assert not (data / "knotserver.db").exists() and not (data / "repositories").exists()
    assert (data / "go-knot-backup" / "knotserver.db").is_file()
    assert (data / "go-knot-backup" / "repositories" / REPO_DID / "HEAD").is_file()
    assert (data / "knot" / ".migrated-from-go-knot").is_file()
    assert stat.S_IMODE((data / "knot-master-key").stat().st_mode) == 0o600

    # Same knot identity, and the repository is served from the new knot.
    described = requests.get(f"{stack.url}/xrpc/org.tangled.knot.describeKnot", timeout=30).json()
    assert described["did"] == f"did:web:{knot_hostname(stack)}", described
    repos = requests.get(f"{stack.url}/xrpc/com.atproto.sync.listRepos", timeout=30)
    assert REPO_DID in repos.text, f"{repos.status_code}: {repos.text[:1000]}"
    refs = requests.get(f"{stack.url}/{REPO_DID}/info/refs?service=git-upload-pack", timeout=30)
    assert refs.status_code == 200 and head in refs.text, f"{refs.status_code}: {refs.text[:1000]}"

    # The Go knot's SSH host key came across, so users' known_hosts entries still match.
    assert seeded_host_key in ssh_host_keys(), stack.app_logs()[-5000:]


def test_migration_runs_once(legacy: tuple[OpenhostStack, str]) -> None:
    stack, _ = legacy
    marker = stack.data_dir / "knot" / ".migrated-from-go-knot"
    migrated_at = marker.read_text()
    subprocess.run(["podman", "restart", stack.container_name], check=True, capture_output=True)
    wait_for_knot(stack)
    # The marker holds the migration's time (to the second), so a second migration would have rewritten it.
    assert marker.read_text() == migrated_at
