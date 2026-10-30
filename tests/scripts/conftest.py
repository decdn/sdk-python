"""Fixtures for the script tests: throwaway OpenPGP keys, shimmed tools and a fixture repo.

`repo` is a git repository on `main` with a bare `origin`, the host's git config kept
out, `user.signingkey` set to a throwaway key that its `KEYS` holds, and `commit.gpgsign`
off: the scripts sign with `-S` and `-s` themselves. `keys.stranger` is a second key that
`KEYS` does not hold. `shim(name, body)` puts an executable `name` first on the repo's
PATH; each shim appends `<name> <args>` to `repo.log` before its body runs.

Signing needs a real `gpg`: without one the fixture runs skip, except under CI, where
they fail.
"""

import os
import re
import shutil
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
# Resolved before a gate empties PATH.
BASH = shutil.which("bash") or "/bin/bash"
FIRST_TOOL_CHECK = "git not found on PATH"

Shim = Callable[[str, str], Path]


def gate(
    script: str, home: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run `scripts/<script>` with a PATH that holds no tool at all."""
    return subprocess.run(
        [BASH, str(ROOT / "scripts" / script), *args],
        cwd=home,
        env={"HOME": str(home), "PATH": str(home / "no-tools"), **(env or {})},
        capture_output=True,
        text=True,
        check=False,
    )


def require(*tools: str) -> None:
    """Skip, or under CI fail, when a tool the fixture runs is not on PATH."""
    missing = [tool for tool in tools if shutil.which(tool) is None]
    if missing:
        message = f"not on PATH: {' '.join(missing)}"
        if os.environ.get("CI"):
            pytest.fail(message)
        pytest.skip(message)


@dataclass(frozen=True)
class Keys:
    home: Path
    signer: str
    stranger: str
    keys: bytes


@pytest.fixture(scope="session")
def keys(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Keys]:
    """A gpg home with two throwaway signing keys, and an armoured `KEYS` holding the first."""
    require("gpg")
    home = tmp_path_factory.mktemp("gpg")
    home.chmod(0o700)

    def gpg(*args: str) -> bytes:
        return subprocess.run(
            ["gpg", "--homedir", str(home), "--batch", *args],
            capture_output=True,
            check=True,
        ).stdout

    def key(uid: str) -> str:
        gpg("--passphrase", "", "--quick-gen-key", uid, "ed25519", "sign")
        listing = gpg("--with-colons", "--list-secret-keys", uid).decode()
        found = re.search(r"^fpr:+([0-9A-F]+):", listing, re.MULTILINE)
        assert found, f"gpg made no key for {uid}"
        return found.group(1)

    signer = key("Release Test <release@example.org>")
    stranger = key("Stranger <stranger@example.org>")
    yield Keys(home, signer, stranger, gpg("--armor", "--export", signer))
    subprocess.run(["gpgconf", "--homedir", str(home), "--kill", "all"], check=False)


@pytest.fixture
def bin_dir(tmp_path: Path) -> Path:
    path = tmp_path / "bin"
    path.mkdir()
    return path


@pytest.fixture
def shim(bin_dir: Path) -> Shim:
    """Write an executable fake `name` onto the fixture PATH; it logs its call, then runs `body`."""

    def write(name: str, body: str) -> Path:
        path = bin_dir / name
        path.write_text(f'#!/usr/bin/env bash\nset -euo pipefail\necho "{name} $*" >> "$SHIM_LOG"\n{body}')
        path.chmod(0o755)
        return path

    return write


@dataclass
class Repo:
    path: Path
    origin: Path
    log: Path
    env: dict[str, str] = field(default_factory=dict)

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.path), *args], env=self.env, capture_output=True, text=True, check=True
        ).stdout

    def origin_git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "--git-dir", str(self.origin), *args],
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    def run(self, command: list[str], **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command, cwd=self.path, env={**self.env, **extra}, capture_output=True, text=True, check=False
        )

    def file(self, path: str) -> str:
        return (self.path / path).read_text()

    def write(self, path: str, content: str) -> None:
        target = self.path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    def calls(self) -> str:
        return self.log.read_text() if self.log.exists() else ""

    def head(self) -> str:
        return self.git("rev-parse", "HEAD").strip()

    def commit(self, path: str, message: str) -> None:
        """Change `path`, commit it with `message` and push."""
        target = self.path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a") as f:
            f.write(f"{os.urandom(8).hex()}\n")
        self.git("add", "-A")
        self.git("commit", "-qm", message)
        self.git("push", "-q", "origin", "main")

    def replace(self, files: dict[str, str]) -> None:
        """Replace each path's content, commit and push."""
        for path, content in files.items():
            self.write(path, content)
        self.git("add", "-A")
        self.git("commit", "-qm", f"chore: rewrite {' '.join(files)}")
        self.git("push", "-q", "origin", "main")


@pytest.fixture
def repo(tmp_path: Path, keys: Keys, bin_dir: Path) -> Repo:
    """A repository on `main`, pushed to a bare `origin`, holding only `KEYS`."""
    path = tmp_path / "repo"
    origin = tmp_path / "origin.git"
    log = tmp_path / "log"
    env = {
        "HOME": str(tmp_path),
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GNUPGHOME": str(keys.home),
        "SHIM_LOG": str(log),
        "LC_ALL": "C.UTF-8",
    }
    r = Repo(path, origin, log, env)
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], env=env, check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], env=env, check=True)
    for key, value in {
        "user.name": "Release Test",
        "user.email": "release@example.org",
        "user.signingkey": keys.signer,
        "commit.gpgsign": "false",
    }.items():
        r.git("config", key, value)
    (path / "KEYS").write_bytes(keys.keys)
    r.git("add", "-A")
    r.git("commit", "-qm", "chore: the fixture")
    r.git("remote", "add", "origin", str(origin))
    r.git("push", "-q", "-u", "origin", "main")
    return r
