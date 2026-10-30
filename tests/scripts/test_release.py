"""Gates and runs of scripts/release.sh.

The argument gates run the real script with a PATH that holds no tool at all. The rest
run it in the `repo` fixture with the real cliff.toml, git, git-cliff and gpg, and shims:
- `uv` logs each call; `version` sets pyproject.toml's version (and writes another line
  too when $RS_UV_EXTRA is set), `lock` sets decdn's version in uv.lock (and another
  line when $RS_LOCK_EXTRA is set), and `run <check>` and `build` fail when the check
  (`ruff`, `mypy`, `pytest`, `build`) is named in $RS_FAIL; `build` also changes
  cliff.toml when $RS_DIRTY is set;
- `scripts/check-package.sh` logs itself and fails the same way;
- `gh` answers the gateway floor's release as $RS_GH says.
"""

import datetime
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.scripts.conftest import FIRST_TOOL_CHECK, ROOT, Keys, Repo, Shim, gate, require

pytestmark = pytest.mark.timeout(120)

UV_SHIM = r"""
set_lock() {
  python3 - "$1" <<'EOF'
import re, sys
s = open("uv.lock").read()
s = re.sub(r'(\[\[package\]\]\nname = "decdn"\nversion = ")[^"]*', lambda m: m.group(1) + sys.argv[1], s)
open("uv.lock", "w").write(s)
EOF
}
check=""
case "$1" in
  version)
    sed -i "0,/^version = \".*\"$/s//version = \"$2\"/" pyproject.toml
    [[ -z "${RS_UV_EXTRA:-}" ]] || echo "# extra" >> pyproject.toml ;;
  lock)
    set_lock "$(sed -n 's/^version = "\(.*\)"$/\1/p' pyproject.toml | head -n1)"
    [[ -z "${RS_LOCK_EXTRA:-}" ]] || echo "# extra" >> uv.lock ;;
  run)
    shift
    [[ "$1" != --frozen ]] || shift
    check="$1" ;;
  build)
    [[ -z "${RS_DIRTY:-}" ]] || echo "# dirty" >> cliff.toml
    check=build ;;
  *) exit 1 ;;
esac
[[ -z "$check" || " ${RS_FAIL:-} " != *" $check "* ]]
"""
CHECK_PACKAGE_STUB = """#!/usr/bin/env bash
echo check-package >> "$SHIM_LOG"
[[ " ${RS_FAIL:-} " != *" check-package "* ]]
"""
GH_SHIM = """
[[ "$*" == "release view v0.3.0 -R decdn/decdn"* ]] || exit 1
case "${RS_GH:-false}" in
  404) echo "release not found" >&2; exit 1 ;;
  401) echo "HTTP 401: Bad credentials (https://api.github.com/graphql)" >&2; exit 1 ;;
  *) echo "${RS_GH:-false}" ;;
esac
"""
# Shaped like CHANGELOG.md before its first release: an intro, then a hand-written
# [Unreleased] section with wrapped entries. Not the file itself, which loses its
# [Unreleased] section at the first release.
CHANGELOG = """# Changelog

All notable changes to `decdn` are documented in this file. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
package follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **The client.** A pure-Python package that speaks HTTP to a gateway
  and hashes what it fetches.

### Fixed

- **Aborts surface as aborts.** Closing a stream mid-body raises
  `ABORTED`.
"""
VERSION_PY = "src/decdn/_version.py"


def today() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")


def pyproject(version: str) -> str:
    """The real pyproject.toml at `version`."""
    return re.sub(
        r'^version = ".*"$',
        f'version = "{version}"',
        (ROOT / "pyproject.toml").read_text(),
        count=1,
        flags=re.M,
    )


def lockfile(version: str) -> str:
    """The real uv.lock with decdn at `version`."""
    return re.sub(
        r'(\[\[package\]\]\nname = "decdn"\nversion = ")[^"]*',
        lambda m: m.group(1) + version,
        (ROOT / "uv.lock").read_text(),
    )


def version_py(version: str) -> str:
    return f'__version__ = "{version}"\n'


def at(version: str) -> dict[str, str]:
    """pyproject.toml and _version.py at `version`."""
    return {"pyproject.toml": pyproject(version), VERSION_PY: version_py(version)}


def project_version(repo: Repo) -> str:
    found = re.search(r'^version = "(.*)"$', repo.file("pyproject.toml"), re.M)
    assert found
    return found.group(1)


# ---- arguments ---------------------------------------------------------------


@pytest.mark.parametrize(
    "args", [["1.2"], ["v1.2.3"], ["1.2.3rc1"], ["next"], ["minor", "patch"], ["--force"]], ids=str
)
def test_stops_before_any_tool_runs(tmp_path: Path, args: list[str]) -> None:
    r = gate("release.sh", tmp_path, *args)
    assert r.returncode == 2
    assert "usage:" in r.stderr
    assert FIRST_TOOL_CHECK not in r.stderr


@pytest.mark.parametrize(
    "args",
    [[], ["auto"], ["minor"], ["1.2.3"], ["patch", "--execute", "--no-verify", "--allow-unconventional"]],
    ids=str,
)
def test_takes_arguments_to_the_tool_checks(tmp_path: Path, args: list[str]) -> None:
    r = gate("release.sh", tmp_path, *args)
    assert r.returncode == 1
    assert FIRST_TOOL_CHECK in r.stderr


# ---- the fixture -------------------------------------------------------------


class Release:
    def __init__(self, repo: Repo) -> None:
        self.repo = repo

    def __call__(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        return self.repo.run([str(self.repo.path / "scripts/release.sh"), *args], **env)

    def first(self) -> None:
        """The first release, executed: tags v0.1.0 on origin."""
        r = self("minor", "--execute", "--no-verify")
        assert r.returncode == 0, r.stderr

    def untouched(self, head: str) -> None:
        """Nothing committed or pushed, and the tree as it was."""
        assert self.repo.head() == head
        assert self.repo.git("status", "--porcelain") == ""
        assert self.repo.origin_git("rev-parse", "main").strip() == head


@pytest.fixture
def release(repo: Repo, shim: Shim, bin_dir: Path) -> Release:
    cliff = ROOT / ".venv/bin/git-cliff"
    if not cliff.exists():
        require("git-cliff")
        cliff = Path(shutil.which("git-cliff") or "git-cliff")
    (bin_dir / "git-cliff").symlink_to(cliff)
    shim("uv", UV_SHIM)
    shim("gh", GH_SHIM)
    script = repo.path / "scripts/release.sh"
    script.parent.mkdir()
    shutil.copy(ROOT / "scripts/release.sh", script)
    repo.write("scripts/check-package.sh", CHECK_PACKAGE_STUB)
    (repo.path / "scripts/check-package.sh").chmod(0o755)
    shutil.copy(ROOT / "cliff.toml", repo.path / "cliff.toml")
    repo.write("CHANGELOG.md", CHANGELOG)
    repo.write("uv.lock", lockfile("0.0.0"))
    for path, content in at("0.0.0").items():
        repo.write(path, content)
    repo.git("add", "-A")
    repo.git("commit", "-qm", "chore: the release fixture")
    repo.git("push", "-q", "origin", "main")
    return Release(repo)


# ---- the first release -------------------------------------------------------


def test_first_release_dry_runs_without_changing_a_file(release: Release, repo: Repo) -> None:
    head = repo.head()
    r = release("minor")
    assert r.returncode == 0, r.stderr
    assert "release decdn: v0.1.0" in r.stdout
    assert f"+## [0.1.0] — {today()}" in r.stdout
    assert '+version = "0.1.0"' in r.stdout
    assert '+__version__ = "0.1.0"' in r.stdout
    assert "dry run: no file changed" in r.stdout
    release.untouched(head)
    assert repo.git("tag") == ""
    assert not re.search(r"^uv (version|lock)", repo.calls(), re.M)


def test_first_release_names_its_level(release: Release) -> None:
    r = release()
    assert r.returncode == 1
    assert "the first release names its level" in r.stderr


def test_first_release_releases_unreleased_signed_and_pushed(
    release: Release, repo: Repo, keys: Keys
) -> None:
    r = release("minor", "--execute")
    assert r.returncode == 0, r.stderr
    assert "scripts/publish.sh v0.1.0" in r.stdout
    assert project_version(repo) == "0.1.0"
    assert repo.file(VERSION_PY) == version_py("0.1.0")
    assert repo.file("uv.lock") == lockfile("0.1.0")
    assert repo.file("CHANGELOG.md") == CHANGELOG.replace(
        "\n## [Unreleased]\n", f"\n## [0.1.0] — {today()}\n"
    )
    assert repo.git("status", "--porcelain") == ""
    assert repo.git("log", "-1", "--format=%s") == "chore(release): v0.1.0\n"
    assert repo.git("show", "--name-only", "--format=", "HEAD") == (
        "CHANGELOG.md\npyproject.toml\nsrc/decdn/_version.py\nuv.lock\n"
    )
    for verify, ref in {"verify-commit": "HEAD", "verify-tag": "v0.1.0"}.items():
        v = repo.run(["git", verify, "--raw", ref])
        assert v.returncode == 0, v.stderr
        assert f"VALIDSIG {keys.signer}" in v.stderr
    assert "decdn 0.1.0" in repo.git("cat-file", "-p", "v0.1.0")
    head = repo.head()
    assert repo.origin_git("rev-parse", "main", "v0.1.0^{commit}") == f"{head}\n{head}\n"
    assert repo.origin_git("tag") == "v0.1.0\n"
    assert (
        "uv version 0.1.0 --frozen\nuv lock --quiet\n"
        "uv run --frozen ruff check .\nuv run --frozen ruff format --check .\n"
        "uv run --frozen mypy\nuv run --frozen pytest\nuv build\ncheck-package\n"
    ) in repo.calls()


@pytest.mark.parametrize(
    ("files", "message"),
    [
        pytest.param(at("0.3.0"), "not the 0.0.0 placeholder", id="a manifest past the placeholder"),
        pytest.param(
            at("0.2.0rc1"),
            "after a prerelease, version goes back to the last release",
            id="a prerelease manifest",
        ),
        pytest.param(
            {VERSION_PY: version_py("0.1.0")},
            "src/decdn/_version.py has __version__ '0.1.0', not pyproject.toml's 0.0.0",
            id="a _version.py out of step",
        ),
        pytest.param(
            {"CHANGELOG.md": CHANGELOG.replace("## [Unreleased]", "## Next")},
            "has no '## [Unreleased]' section",
            id="no [Unreleased] section",
        ),
        pytest.param(
            {"CHANGELOG.md": "# Changelog\n\n## [Unreleased]\n"},
            "the [Unreleased] section has no '- ' entries",
            id="an empty [Unreleased] section",
        ),
        pytest.param(
            {"CHANGELOG.md": f"{CHANGELOG}\n[Unreleased]: https://example.org/compare\n"},
            "has link definitions",
            id="a link definition",
        ),
    ],
)
def test_first_release_refuses(release: Release, repo: Repo, files: dict[str, str], message: str) -> None:
    repo.replace(files)
    r = release("minor")
    assert r.returncode == 1
    assert message in r.stderr


# ---- a later release ---------------------------------------------------------


@pytest.fixture
def released(release: Release) -> Release:
    release.first()
    return release


def test_later_release_generates_its_section_and_pushes_it(released: Release, repo: Repo) -> None:
    repo.commit("src/a.py", "fix(site): keep the query (#3)")
    repo.commit("src/b.py", "feat: open a bundle (#4)")
    repo.commit("tests/a.py", "test: cover it (#5)")
    repo.commit(".github/ci.yml", "ci: pin an action (#6)")
    r = released("--execute", "--no-verify")
    assert r.returncode == 0, r.stderr
    assert project_version(repo) == "0.2.0"
    assert repo.file(VERSION_PY) == version_py("0.2.0")
    changelog = repo.file("CHANGELOG.md")
    assert (
        "package follows [Semantic Versioning](https://semver.org/).\n\n"
        f"## [0.2.0] — {today()}\n\n### Added\n\n- Open a bundle (#4)\n\n"
        "### Fixed\n\n- **site**: Keep the query (#3)\n\n## [0.1.0]"
    ) in changelog
    assert not re.search("Cover it|Pin an action", changelog)
    head = repo.head()
    assert repo.origin_git("rev-parse", "main", "v0.2.0^{commit}") == f"{head}\n{head}\n"
    assert repo.git("status", "--porcelain") == ""


def test_later_release_renders_every_type_under_a_kind(released: Release, repo: Repo) -> None:
    for message in [
        "perf: faster hashing",
        "docs: document open",
        "refactor: split http",
        "revert: drop the retry",
        "security: check the origin",
        "style: format",
        "chore(deps): bump httpx",
        "chore(release): v9.9.9",
        "ci!: drop python 3.10",
        "Feat: a capitalised type",
        "deps: an unlisted type",
        "build: a build change",
    ]:
        repo.commit("src/a.py", message)
    r = released()
    assert r.returncode == 0, r.stderr
    assert "v0.1.0 -> v0.2.0" in r.stdout
    section = [line for line in r.stdout.split("\n") if re.match(r"^\+(###|- )", line)]
    assert section == [
        "+### Added",
        "+- A capitalised type",
        "+### Changed",
        "+- An unlisted type",
        "+- **Breaking:** Drop python 3.10",
        "+- **deps**: Bump httpx",
        "+- Split http",
        "+- Document open",
        "+- Faster hashing",
        "+### Removed",
        "+- Drop the retry",
        "+### Security",
        "+- Check the origin",
    ]


def test_later_release_refuses_a_heading_that_is_not_a_kind(released: Release, repo: Repo) -> None:
    repo.replace(
        {"cliff.toml": re.sub(r'^ {2}\{ message = "\.\*".*\n', "", repo.file("cliff.toml"), flags=re.M)}
    )
    repo.commit("src/a.py", "deps: an unlisted type")
    r = released()
    assert r.returncode == 1
    assert "not a Keep a Changelog kind" in r.stderr


@pytest.mark.parametrize(
    ("messages", "tag"),
    [
        pytest.param(["fix: a fix"], "v0.1.1", id="a fix"),
        pytest.param(["fix: a fix", "feat: a feature"], "v0.2.0", id="a feature"),
        pytest.param(["refactor!: drop X"], "v0.2.0", id="a breaking change"),
        pytest.param(["fix: a fix\n\nBREAKING CHANGE: it breaks"], "v0.2.0", id="a breaking footer"),
    ],
)
def test_later_release_bumps(released: Release, repo: Repo, messages: list[str], tag: str) -> None:
    for message in messages:
        repo.commit("src/a.py", message)
    r = released()
    assert r.returncode == 0, r.stderr
    assert f"v0.1.0 -> {tag}" in r.stdout


def test_later_release_marks_a_breaking_change(released: Release, repo: Repo) -> None:
    repo.commit("src/a.py", "refactor!: drop X")
    assert "+- **Breaking:** Drop X" in released().stdout


@pytest.mark.parametrize(("level", "tag"), [("minor", "v0.2.0"), ("major", "v1.0.0"), ("0.4.0", "v0.4.0")])
def test_later_release_takes_the_level(released: Release, repo: Repo, level: str, tag: str) -> None:
    repo.commit("src/a.py", "fix: a fix")
    r = released(level)
    assert r.returncode == 0, r.stderr
    assert f"v0.1.0 -> {tag}" in r.stdout


@pytest.mark.parametrize("level", ["0.0.5", "0.1.0"])
def test_later_release_refuses_a_version_not_above_the_last(
    released: Release, repo: Repo, level: str
) -> None:
    repo.commit("src/a.py", "feat: a feature")
    r = released(level)
    assert r.returncode == 1
    assert f"{level} is not above the last release, 0.1.0" in r.stderr


def test_later_release_refuses_when_nothing_makes_an_entry(released: Release, repo: Repo) -> None:
    repo.commit("tests/a.py", "test: more tests")
    repo.commit(".github/ci.yml", "ci: a job")
    r = released("patch")
    assert r.returncode == 1
    assert "nothing to release" in r.stderr


def test_later_release_refuses_unconventional_commits_unless_allowed(released: Release, repo: Repo) -> None:
    repo.commit("src/a.py", "Update the client")
    repo.commit("src/b.py", "feat(): an empty scope")
    repo.commit("src/c.py", "fix(x): a glued body\nright under the subject")
    repo.commit("src/d.py", "fix: a fix")
    r = released()
    assert r.returncode == 1
    assert "cannot parse these commits" in r.stderr
    for subject in [" Update the client", " feat(): an empty scope", " fix(x): a glued body"]:
        assert subject in r.stderr
    r = released("--allow-unconventional")
    assert r.returncode == 0, r.stderr
    assert " Update the client" in r.stderr
    assert "v0.1.0 -> v0.1.1" in r.stdout
    assert not re.search(r"^\+- .*(Update the client|An empty scope|A glued body)", r.stdout, re.M)


def test_later_release_refuses_an_unreleased_section(released: Release, repo: Repo) -> None:
    repo.commit("src/a.py", "fix: a fix")
    changelog = repo.file("CHANGELOG.md").replace(
        "## [0.1.0]", "## [Unreleased]\n\n- a hand edit\n\n## [0.1.0]"
    )
    repo.replace({"CHANGELOG.md": changelog})
    r = released()
    assert r.returncode == 1
    assert "has an [Unreleased] section" in r.stderr


def test_later_release_refuses_a_manifest_that_is_not_the_last_release(released: Release, repo: Repo) -> None:
    repo.replace(at("0.5.0"))
    repo.commit("src/a.py", "fix: a fix")
    r = released()
    assert r.returncode == 1
    assert "fix the manifest first" in r.stderr


# A stray tag on a newer commit, below origin's highest or above it, would move
# git-cliff's boundary.
@pytest.mark.parametrize("tag", ["v0.0.9", "v0.9.0"])
def test_later_release_refuses_a_local_tag_origin_does_not_have(
    released: Release, repo: Repo, tag: str
) -> None:
    repo.commit("src/a.py", "fix: a fix")
    repo.git("tag", tag)
    repo.commit("src/b.py", "fix: another fix")
    r = released("0.2.0")
    assert r.returncode == 1
    assert f"local tags not on origin, or not origin's: {tag}." in r.stderr


def test_later_release_takes_a_tag_that_is_not_a_release_tag(released: Release, repo: Repo) -> None:
    repo.commit("src/a.py", "fix: a fix")
    repo.git("tag", "v0.2.0rc1")
    repo.git("tag", "vendor-snapshot")
    r = released()
    assert r.returncode == 0, r.stderr
    assert "v0.1.0 -> v0.1.1" in r.stdout


def test_later_release_pushes_all_or_nothing_and_its_undo_works(released: Release, repo: Repo) -> None:
    repo.commit("src/a.py", "fix: a fix")
    before = repo.origin_git("rev-parse", "main").strip()
    hook = repo.origin / "hooks/update"
    hook.write_text('#!/bin/sh\ncase "$1" in refs/tags/*) exit 1 ;; esac\n')
    hook.chmod(0o755)
    r = released("--execute", "--no-verify")
    assert r.returncode == 1
    assert "nothing reached origin" in r.stderr
    assert repo.origin_git("rev-parse", "main").strip() == before
    assert repo.origin_git("tag") == "v0.1.0\n"
    assert repo.git("log", "-1", "--format=%s") == "chore(release): v0.1.1\n"
    assert "main is not origin/main" in released().stderr
    hook.unlink()
    repo.git("tag", "-d", "v0.1.1")
    repo.git("reset", "-q", "--hard", "origin/main")
    r = released("--execute", "--no-verify")
    assert r.returncode == 0, r.stderr
    assert repo.origin_git("tag") == "v0.1.0\nv0.1.1\n"


# ---- refusals ----------------------------------------------------------------


def test_refuses_another_branch_and_a_detached_head(release: Release, repo: Repo) -> None:
    repo.git("switch", "-qc", "topic")
    r = release("minor")
    assert r.returncode == 1
    assert "releases are cut on main, not 'topic'" in r.stderr
    repo.git("switch", "-q", "--detach", "main")
    r = release("minor")
    assert r.returncode == 1
    assert "not 'a detached HEAD'" in r.stderr


def test_refuses_a_dirty_tree_and_keeps_the_edit(release: Release, repo: Repo) -> None:
    with (repo.path / "CHANGELOG.md").open("a") as f:
        f.write("# my edit\n")
    r = release("minor", "--execute")
    assert r.returncode == 1
    assert "the working tree is not clean" in r.stderr
    assert repo.file("CHANGELOG.md") == f"{CHANGELOG}# my edit\n"


def test_refuses_a_main_ahead_of_or_behind_origin(release: Release, repo: Repo) -> None:
    repo.git("commit", "-q", "--allow-empty", "-m", "chore: local only")
    r = release("minor")
    assert r.returncode == 1
    assert "main is not origin/main" in r.stderr
    repo.git("push", "-q", "origin", "main")
    repo.git("reset", "-q", "--hard", "HEAD~1")
    r = release("minor")
    assert r.returncode == 1
    assert "main is not origin/main" in r.stderr


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        ("404", "decdn v0.3.0, has no published release"),
        ("401", "is gh logged in?"),
        ("true", "decdn v0.3.0, is a draft release"),
    ],
)
def test_refuses_the_gateway_floor(release: Release, answer: str, message: str) -> None:
    r = release("minor", RS_GH=answer)
    assert r.returncode == 1
    assert message in r.stderr
    if answer == "401":
        assert "Bad credentials" in r.stderr


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        pytest.param(
            ["config", "--unset", "user.signingkey"], "git has no user.signingkey", id="no signing key"
        ),
        pytest.param(["config", "gpg.format", "ssh"], "gpg.format is ssh", id="an ssh signing format"),
        pytest.param(None, "signs with a key that is not in KEYS", id="a key KEYS does not hold"),
    ],
)
def test_refuses_a_signing_setup_before_anything_runs(
    release: Release, repo: Repo, keys: Keys, setup: list[str] | None, message: str
) -> None:
    repo.git(*(setup or ["config", "user.signingkey", keys.stranger]))
    head = repo.head()
    r = release("minor", "--execute")
    assert r.returncode == 1
    assert message in r.stderr
    assert re.findall(r"^(?:uv|check-package).*$", repo.calls(), re.M) == []
    release.untouched(head)
    assert repo.git("tag") == ""


# ---- restores the files ------------------------------------------------------


# A failed --execute leaves the tree as it was, and commits, tags and pushes nothing.
@pytest.mark.parametrize(
    ("extra", "message"),
    [
        pytest.param({"RS_FAIL": "ruff"}, "uv run ruff check . failed", id="ruff fails"),
        pytest.param(
            {"RS_FAIL": "check-package"}, "scripts/check-package.sh failed", id="check-package fails"
        ),
        pytest.param(
            {"RS_DIRTY": "1"},
            "the checks left changes besides pyproject.toml uv.lock src/decdn/_version.py CHANGELOG.md",
            id="a check leaves other changes",
        ),
        pytest.param(
            {"RS_UV_EXTRA": "1"},
            "uv version wrote a pyproject.toml other than the one shown",
            id="uv version writes another pyproject.toml",
        ),
        pytest.param(
            {"RS_LOCK_EXTRA": "1"},
            "uv lock wrote a uv.lock other than the one shown",
            id="uv lock writes another uv.lock",
        ),
    ],
)
def test_restores_the_files_when(release: Release, repo: Repo, extra: dict[str, str], message: str) -> None:
    head = repo.head()
    r = release("minor", "--execute", **extra)
    assert r.returncode == 1
    assert f"{message}: nothing was committed" in r.stderr
    # The stray change the check made is the test's to drop.
    repo.git("checkout", "-q", "--", "cliff.toml")
    release.untouched(head)
    assert project_version(repo) == "0.0.0"
    assert repo.file(VERSION_PY) == version_py("0.0.0")
    assert repo.git("tag") == ""
    if extra.get("RS_FAIL") == "ruff":
        assert not re.search(r"(mypy|pytest|build)$", repo.calls(), re.M)


def test_restores_the_files_when_a_commit_hook_fails(release: Release, repo: Repo) -> None:
    hook = repo.path / ".git/hooks/pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    head = repo.head()
    r = release("minor", "--execute", "--no-verify")
    assert r.returncode == 1
    assert "git commit failed" in r.stderr
    release.untouched(head)
    assert project_version(repo) == "0.0.0"
    assert repo.git("tag") == ""
