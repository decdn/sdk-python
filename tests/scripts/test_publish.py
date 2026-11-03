"""Gates and runs of the release scripts behind .github/workflows/publish.yml:

- scripts/prepare-publish.sh checks and builds a release tag (the `prepare` job);
- scripts/release-state.sh asks GitHub for the tag's release (the `release-state` job);
- scripts/github-release.sh makes the release (the `github-release` job);
- scripts/publish.sh dispatches the workflow.

A PyPI file cannot be uploaded twice, so what prepare-publish.sh and release-state.sh
refuse must stop the run before the `publish` job.

The gates run the real scripts with a PATH that holds no tool at all: a tag that is not
`v<version>`, and a typo in publish.sh's dry-run switch, stop before the first tool check,
and input they accept gets as far as it.

The prepare runs take a signed tag in the `repo` fixture, with git and gpg, as the workflow
does: from a trusted root holding its own KEYS, with the tag's checkout at `src/`. Shims
share a state directory:
- `uv` logs each call and fails the call naming $PS_UV (`pytest`, `mypy`, ...); `build`
  writes an empty sdist and wheel into `dist/`, with a third file when $PS_BUILD is
  `extra`, or for version 9.9.9 when it is `other`;
- `curl` answers PyPI's JSON API from the stub file list in `pypi`, each file's digest
  that of an empty file unless $PS_PYPI is `differs`; with a 404 when it lacks the
  version, failing to reach it when $PS_PYPI is `down`, answering 503 when it is `503`,
  or 200 with a body that is not JSON when it is `garbled`.
The other scripts run against a `gh` shim each, described where it is defined.
"""

import hashlib
import os
import re
import subprocess
from pathlib import Path

import pytest

from tests.scripts.conftest import BASH, FIRST_TOOL_CHECK, ROOT, Keys, Repo, Shim, gate

pytestmark = pytest.mark.timeout(120)

SCRIPTS = ROOT / "scripts"
PREPARE = SCRIPTS / "prepare-publish.sh"
GH_CHECK = "gh not found on PATH"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()

UV_SHIM = r"""
[[ -z "${PS_UV:-}" || " $* " != *" $PS_UV "* ]] || { echo "$PS_UV failed" >&2; exit 1; }
case "$1" in
  sync|run) ;;
  build)
    v="$(sed -n 's/^version = "\(.*\)"$/\1/p' pyproject.toml | head -n1)"
    [[ "${PS_BUILD:-}" != other ]] || v=9.9.9
    mkdir -p dist
    : > "dist/decdn-$v.tar.gz"
    : > "dist/decdn-$v-py3-none-any.whl"
    [[ "${PS_BUILD:-}" != extra ]] || : > "dist/decdn-$v-py3-none-manylinux.whl" ;;
  *) exit 1 ;;
esac
"""
CURL_SHIM = r"""
args=("$@")
out=""
for ((i = 0; i < ${#args[@]}; i++)); do
  [[ "${args[i]}" != --output ]] || out="${args[i + 1]}"
done
[[ "${PS_PYPI:-}" != down ]] || { echo "curl: (7) Failed to connect to pypi.org port 443" >&2; exit 7; }
[[ "${args[-1]}" =~ ^https://pypi\.org/pypi/decdn/([^/]+)/json$ ]] || exit 1
version="${BASH_REMATCH[1]}"
files="$(grep -E "^decdn-${version//./\\.}[-.]" "$PS_STATE/pypi" 2>/dev/null || true)"
if [[ "${PS_PYPI:-}" == 503 ]]; then
  echo "unavailable" > "$out"
  printf 503
elif [[ "${PS_PYPI:-}" == garbled ]]; then
  echo "not json" > "$out"
  printf 200
elif [[ -n "$files" ]]; then
  python3 -c 'import json, sys
digest = "0" * 64 if sys.argv[1] == "differs" else sys.argv[2]
urls = [{"filename": f, "digests": {"sha256": digest}} for f in sys.argv[3:]]
print(json.dumps({"urls": urls}))' "${PS_PYPI:-}" "$EMPTY_SHA256" $files > "$out"
  printf 200
else
  echo '{"message": "Not Found"}' > "$out"
  printf 404
fi
"""
SECTION = """### Fixed

- **Aborts surface as aborts.** Closing a stream mid-body raises
  `ABORTED`."""
FIRST_SECTION = """### Added

- **The client.**"""
CHANGELOG = f"""# Changelog

Intro.

## [0.2.0] — 2026-11-01

{SECTION}

## [0.1.0] — 2026-10-30

{FIRST_SECTION}
"""

TAGS = ["v0.1.0", "v1.20.3", "v0.2.0rc1", "v0.2.0a1", "v0.2.0b2", "v0.2.0.dev3"]
BAD_TAGS = [
    "sdk-v0.1.0",
    "0.1.0",
    "v0.1",
    "v01.0.0",
    "v0.1.0+build",
    "v0.1.0-",
    "x",
    "v0.2.0-rc.1",
    "v0.2.0rc",
    "v0.1.0.post1",
    'v0.1.0"',
]
TAG_ERROR = "the tag must be v<major>.<minor>.<patch>"
SHA = "a" * 40


def footer(version: str, install: str, harness: str = "") -> str:
    floors = (
        f" Its contract suite runs against real gateways in decdn's e2e harness from"
        f" [v{harness}](https://github.com/decdn/decdn/releases/tag/v{harness})."
        if harness
        else " No decdn release's e2e harness runs its contract suite yet."
    )
    return (
        f"PyPI: [`decdn {version}`](https://pypi.org/project/decdn/{version}/) (`pip install {install}`)."
        " It needs `decdn gateway` [v0.3.0](https://github.com/decdn/decdn/releases/tag/v0.3.0) or later."
        f"{floors}\n"
    )


# ---- gates -------------------------------------------------------------------


GATED = {
    "prepare-publish.sh": (("src", "KEYS", "out"), FIRST_TOOL_CHECK),
    "publish.sh": ((), GH_CHECK),
    "release-state.sh": ((), GH_CHECK),
    "github-release.sh": ((SHA, "/dev/null", "false"), None),
}


@pytest.mark.parametrize("script", list(GATED))
@pytest.mark.parametrize("tag", BAD_TAGS)
def test_stops_on_the_tag_before_any_tool_runs(tmp_path: Path, script: str, tag: str) -> None:
    rest, _ = GATED[script]
    r = gate(script, tmp_path, tag, *rest)
    assert r.returncode == 1
    assert TAG_ERROR in r.stderr
    assert "not found on PATH" not in r.stderr


@pytest.mark.parametrize("script", [s for s, (_, check) in GATED.items() if check])
@pytest.mark.parametrize("tag", TAGS)
def test_takes_the_tag_to_the_tool_checks(tmp_path: Path, script: str, tag: str) -> None:
    rest, check = GATED[script]
    r = gate(script, tmp_path, tag, *rest)
    assert r.returncode == 1
    assert check is not None and check in r.stderr


@pytest.mark.parametrize("value", ["maybe", "2", "on"])
def test_publish_stops_on_a_dry_run_typo_before_any_tool_runs(tmp_path: Path, value: str) -> None:
    r = gate("publish.sh", tmp_path, "v0.1.0", env={"DECDN_PYPI_DRY_RUN": value})
    assert r.returncode == 1
    assert f"DECDN_PYPI_DRY_RUN must be 1/true/yes or 0/false/no, got '{value}'" in r.stderr
    assert GH_CHECK not in r.stderr


@pytest.mark.parametrize(
    ("args", "message"),
    [
        pytest.param(
            ("v0.1.0", "abc", "notes", "false"), "the commit must be a full SHA-1", id="a short sha"
        ),
        pytest.param(("v0.1.0", SHA, "notes", "yes"), "prerelease must be true or false", id="a bad switch"),
        pytest.param(("v0.1.0", SHA, "/nonexistent", "false"), "no release notes at", id="no notes"),
    ],
)
def test_github_release_stops_on_bad_input_before_any_tool_runs(
    tmp_path: Path, args: tuple[str, ...], message: str
) -> None:
    r = gate("github-release.sh", tmp_path, *args)
    assert r.returncode == 1
    assert message in r.stderr
    assert GH_CHECK not in r.stderr


# ---- prepare-publish.sh from a fixture tag -------------------------------------


def pyproject(version: str, harness: str = "", floor: str = "0.3.0") -> str:
    """The real pyproject.toml at `version`, its floors `floor` and `harness`."""
    text = re.sub(
        r'^version = ".*"$',
        f'version = "{version}"',
        (ROOT / "pyproject.toml").read_text(),
        count=1,
        flags=re.M,
    )
    text = re.sub(r'^gateway-floor = ".*"$', f'gateway-floor = "{floor}"', text, flags=re.M)
    return re.sub(r'^harness-floor = ".*"$', f'harness-floor = "{harness}"', text, flags=re.M)


class Prepare:
    """prepare-publish.sh as publish.yml runs it: from a trusted root holding KEYS (the
    signer's key alone), with the tag's checkout at `src/` and relative paths."""

    def __init__(self, repo: Repo, state: Path, trusted: Path) -> None:
        self.repo = repo
        self.state = state
        self.trusted = trusted
        self.out = trusted / "out"

    def __call__(self, tag: str, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(PREPARE), tag, "src", "KEYS", str(self.out)],
            cwd=self.trusted,
            env={**self.repo.env, **env},
            capture_output=True,
            text=True,
            check=False,
        )

    def tag(
        self, version: str, harness: str = "", version_py: str = "", key: str = "", floor: str = "0.3.0"
    ) -> None:
        """pyproject.toml, _version.py and CHANGELOG.md at `version`, committed and tagged
        v<version>, signed by `key` (default: the signer KEYS holds)."""
        self.repo.write("pyproject.toml", pyproject(version, harness, floor))
        self.repo.write("src/decdn/_version.py", f'__version__ = "{version_py or version}"\n')
        self.repo.write("CHANGELOG.md", CHANGELOG)
        self.repo.git("add", "-A")
        self.repo.git("commit", "-qm", f"chore(release): v{version}")
        self.repo.git("tag", *(["-u", key] if key else ["-s"]), f"v{version}", "-m", f"decdn {version}")

    def outputs(self) -> dict[str, str]:
        lines = (self.out / "outputs").read_text().splitlines()
        return dict(line.split("=", 1) for line in lines)

    def notes(self) -> str:
        return (self.out / "notes.md").read_text()

    def dist(self) -> list[str]:
        return sorted(p.name for p in (self.out / "dist").iterdir())

    def stopped(self) -> bool:
        """No outputs, so the later jobs have nothing to act on."""
        return not (self.out / "outputs").exists()

    def built(self) -> bool:
        return bool(re.search(r"^uv ", self.repo.calls(), re.M))


@pytest.fixture
def prepare(repo: Repo, shim: Shim, tmp_path: Path, keys: Keys) -> Prepare:
    state = tmp_path / "state"
    state.mkdir()
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    (trusted / "KEYS").write_bytes(keys.keys)
    (trusted / "src").symlink_to(repo.path)
    shim("uv", UV_SHIM)
    shim("curl", CURL_SHIM)
    repo.env["PS_STATE"] = str(state)
    repo.env["EMPTY_SHA256"] = EMPTY_SHA256
    return Prepare(repo, state, trusted)


def test_builds_a_release_and_renders_its_notes(prepare: Prepare) -> None:
    prepare.tag("0.2.0")
    r = prepare("v0.2.0")
    assert r.returncode == 0, r.stderr
    assert prepare.outputs() == {
        "version": "0.2.0",
        "sha": prepare.repo.head(),
        "prerelease": "false",
        "pending": "true",
    }
    assert prepare.dist() == ["decdn-0.2.0-py3-none-any.whl", "decdn-0.2.0.tar.gz"]
    assert prepare.notes() == f"{SECTION}\n\n---\n\n{footer('0.2.0', 'decdn')}"
    log = prepare.repo.calls()
    steps = [
        "uv sync --locked",
        "ruff check",
        "ruff format --check",
        "mypy",
        "pytest",
        "uv build",
        "twine check",
    ]
    positions = [log.index(step) for step in steps]
    assert positions == sorted(positions)


# The first release's section is the changelog's last: it runs to the end of the file.
def test_renders_the_last_section_of_the_changelog(prepare: Prepare) -> None:
    prepare.tag("0.1.0")
    r = prepare("v0.1.0")
    assert r.returncode == 0, r.stderr
    assert prepare.notes() == f"{FIRST_SECTION}\n\n---\n\n{footer('0.1.0', 'decdn')}"


def test_names_the_harness_floor_in_the_notes(prepare: Prepare) -> None:
    prepare.tag("0.2.0", harness="0.5.0")
    r = prepare("v0.2.0")
    assert r.returncode == 0, r.stderr
    assert prepare.notes() == f"{SECTION}\n\n---\n\n{footer('0.2.0', 'decdn', '0.5.0')}"


@pytest.mark.parametrize("version", ["0.3.0rc1", "0.3.0a1", "0.3.0b2", "0.3.0.dev1"])
def test_marks_a_prerelease_its_notes_the_footer(prepare: Prepare, version: str) -> None:
    prepare.tag(version)
    r = prepare(f"v{version}")
    assert r.returncode == 0, r.stderr
    assert prepare.outputs()["prerelease"] == "true"
    assert prepare.notes() == footer(version, f"decdn=={version}")


# PyPI takes a release file by file: a run stopped between the two uploads leaves one.
@pytest.mark.parametrize(
    ("on_pypi", "pending"),
    [
        pytest.param("decdn-0.2.0.tar.gz\n", "true", id="one file"),
        pytest.param("decdn-0.2.0-py3-none-any.whl\ndecdn-0.2.0.tar.gz\n", "false", id="both files"),
    ],
)
def test_reports_whether_pypi_lacks_a_file(prepare: Prepare, on_pypi: str, pending: str) -> None:
    prepare.tag("0.2.0")
    (prepare.state / "pypi").write_text(on_pypi)
    r = prepare("v0.2.0")
    assert r.returncode == 0, r.stderr
    assert prepare.outputs()["pending"] == pending


def test_stops_on_a_file_pypi_has_with_other_contents(prepare: Prepare) -> None:
    prepare.tag("0.2.0")
    (prepare.state / "pypi").write_text("decdn-0.2.0.tar.gz\n")
    r = prepare("v0.2.0", PS_PYPI="differs")
    assert r.returncode == 1
    assert f"PyPI has decdn-0.2.0.tar.gz with sha256 {'0' * 64}, but v0.2.0 builds {EMPTY_SHA256}" in r.stderr
    assert prepare.stopped()


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        pytest.param(
            {"PS_PYPI": "down"}, "cannot ask PyPI whether it has decdn 0.2.0", id="a PyPI it cannot reach"
        ),
        pytest.param(
            {"PS_PYPI": "503"}, "cannot ask PyPI whether it has decdn 0.2.0 (HTTP 503)", id="a PyPI error"
        ),
        pytest.param(
            {"PS_PYPI": "garbled"},
            "cannot read PyPI's answer for decdn 0.2.0",
            id="unreadable JSON from PyPI",
        ),
        pytest.param(
            {"PS_BUILD": "extra"},
            "uv build made other files than one decdn-0.2.0 sdist and wheel",
            id="a third file",
        ),
        pytest.param(
            {"PS_BUILD": "other"},
            "uv build made other files than one decdn-0.2.0 sdist and wheel",
            id="files of another version",
        ),
    ],
)
def test_stops_without_outputs(prepare: Prepare, extra: dict[str, str], message: str) -> None:
    prepare.tag("0.2.0")
    r = prepare("v0.2.0", **extra)
    assert r.returncode == 1
    assert message in r.stderr
    assert prepare.stopped()


@pytest.mark.parametrize("step", ["--locked", "ruff", "--check", "mypy", "pytest", "build", "twine"])
def test_stops_on_a_failing_gate_or_build_step(prepare: Prepare, step: str) -> None:
    prepare.tag("0.2.0")
    r = prepare("v0.2.0", PS_UV=step)
    assert r.returncode == 1
    assert f"{step} failed" in r.stderr
    assert prepare.stopped()
    assert prepare.dist() == []


# The tag's tree carries a KEYS of its own that holds the stranger's key: only the
# trusted KEYS counts.
def test_ignores_the_keys_the_tag_carries(prepare: Prepare, keys: Keys) -> None:
    stranger = subprocess.run(
        ["gpg", "--homedir", str(keys.home), "--batch", "--armor", "--export", keys.stranger],
        capture_output=True,
        check=True,
    ).stdout
    prepare.repo.write("KEYS", (keys.keys + stranger).decode())
    prepare.tag("0.2.0", key=keys.stranger)
    r = prepare("v0.2.0")
    assert r.returncode == 1
    assert "v0.2.0 is not signed by a key published in KEYS" in r.stderr
    assert not prepare.built()


def test_stops_on_an_unsigned_tag(prepare: Prepare) -> None:
    prepare.tag("0.2.0")
    prepare.repo.git("tag", "-f", "-a", "v0.2.0", "-m", "unsigned")
    r = prepare("v0.2.0")
    assert r.returncode == 1
    assert "v0.2.0 is not signed by a key published in KEYS" in r.stderr
    assert not prepare.built()


# A tag object signed as v0.1.0, at a commit of version 0.2.0, put at refs/tags/v0.2.0.
def test_stops_on_a_tag_signed_under_another_name(prepare: Prepare) -> None:
    prepare.tag("0.2.0")
    prepare.repo.git("tag", "-s", "v0.1.0", "-m", "decdn 0.1.0")
    other = prepare.repo.git("rev-parse", "refs/tags/v0.1.0").strip()
    prepare.repo.git("update-ref", "refs/tags/v0.2.0", other)
    r = prepare("v0.2.0")
    assert r.returncode == 1
    assert "the tag object at refs/tags/v0.2.0 was signed as 'v0.1.0', not v0.2.0" in r.stderr
    assert not prepare.built()


def test_stops_on_a_checkout_that_is_not_the_tag(prepare: Prepare) -> None:
    prepare.tag("0.2.0")
    prepare.repo.commit("later.txt", "chore: after the tag")
    r = prepare("v0.2.0")
    assert r.returncode == 1
    assert "is not checked out at v0.2.0" in r.stderr
    assert not prepare.built()


def test_stops_on_a_release_without_a_changelog_section_before_the_build(prepare: Prepare) -> None:
    prepare.tag("0.4.0")
    r = prepare("v0.4.0")
    assert r.returncode == 1
    assert "CHANGELOG.md at v0.4.0 has no '## [0.4.0]' section, or it is empty" in r.stderr
    assert not prepare.built()


def test_stops_on_a_version_file_out_of_step_before_the_build(prepare: Prepare) -> None:
    prepare.tag("0.2.0", version_py="0.1.0")
    r = prepare("v0.2.0")
    assert r.returncode == 1
    assert "src/decdn/_version.py at v0.2.0 has __version__ '0.1.0', but the tag says 0.2.0" in r.stderr
    assert not prepare.built()


def test_stops_on_a_tag_naming_another_version(prepare: Prepare) -> None:
    prepare.tag("0.2.0")
    prepare.repo.git("tag", "-s", "v0.2.1", "-m", "decdn 0.2.1")
    r = prepare("v0.2.1")
    assert r.returncode == 1
    assert "pyproject.toml is at 0.2.0, but the tag says 0.2.1" in r.stderr


@pytest.mark.parametrize(
    ("floors", "message"),
    [
        pytest.param({"floor": "0.3"}, "gateway-floor '0.3', not X.Y.Z", id="a short gateway floor"),
        pytest.param({"floor": ""}, "gateway-floor '', not X.Y.Z", id="no gateway floor"),
        pytest.param(
            {"harness": "next"}, "harness-floor 'next', not X.Y.Z or empty", id="a bad harness floor"
        ),
    ],
)
def test_stops_on_a_malformed_floor_before_the_build(
    prepare: Prepare, floors: dict[str, str], message: str
) -> None:
    prepare.tag("0.2.0", **floors)
    r = prepare("v0.2.0")
    assert r.returncode == 1
    assert message in r.stderr
    assert not prepare.built()


def test_stops_on_an_output_directory_that_exists(prepare: Prepare) -> None:
    prepare.tag("0.2.0")
    prepare.out.mkdir()
    r = prepare("v0.2.0")
    assert r.returncode == 1
    assert "exists: the output directory must be new" in r.stderr


# ---- the gh-only scripts -------------------------------------------------------


def run(
    script: str, bin_dir: Path, tmp_path: Path, *args: str, **env: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [BASH, str(SCRIPTS / script), *args],
        cwd=tmp_path,
        env={
            "HOME": str(tmp_path),
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "SHIM_LOG": str(tmp_path / "log"),
            **env,
        },
        capture_output=True,
        text=True,
        check=False,
    )


def calls(tmp_path: Path, prefix: str) -> list[str]:
    log = tmp_path / "log"
    lines = log.read_text().split("\n") if log.exists() else []
    return [line for line in lines if line.startswith(prefix)]


# `gh api .../releases` prints $PS_RELEASES, the `.draft` of each release for the tag,
# or fails when it is `down`.
RELEASES_SHIM = r"""
[[ "$1 $2" == "api repos/decdn/sdk-python/releases" ]] || exit 1
[[ "${PS_RELEASES:-}" != down ]] || { echo "HTTP 502" >&2; exit 1; }
printf '%b' "${PS_RELEASES:-}"
"""


@pytest.mark.parametrize(
    ("releases", "output"),
    [
        pytest.param("", "has_release=false", id="no release"),
        pytest.param("false\n", "has_release=true", id="a release"),
    ],
)
def test_release_state_reports_the_release(
    shim: Shim, bin_dir: Path, tmp_path: Path, releases: str, output: str
) -> None:
    shim("gh", RELEASES_SHIM)
    r = run("release-state.sh", bin_dir, tmp_path, "v0.2.0", PS_RELEASES=releases)
    assert r.returncode == 0, r.stderr
    assert r.stdout == f"{output}\n"
    [call] = calls(tmp_path, "gh api")
    assert '.tag_name == "v0.2.0"' in call
    assert "--paginate" in call


@pytest.mark.parametrize(
    ("releases", "message"),
    [
        pytest.param("true\n", "has a draft release for v0.2.0", id="a draft"),
        pytest.param("false\ntrue\n", "has a draft release for v0.2.0", id="a release and a draft"),
        pytest.param("down", "cannot ask GitHub whether decdn/sdk-python has a release", id="GitHub down"),
        pytest.param("null\n", "cannot read GitHub's answer", id="an answer it cannot read"),
    ],
)
def test_release_state_stops_without_an_answer(
    shim: Shim, bin_dir: Path, tmp_path: Path, releases: str, message: str
) -> None:
    shim("gh", RELEASES_SHIM)
    r = run("release-state.sh", bin_dir, tmp_path, "v0.2.0", PS_RELEASES=releases)
    assert r.returncode == 1
    assert message in r.stderr
    assert "has_release" not in r.stdout


# `gh api .../git/ref/tags/<tag>` answers $PS_REF (`<type> <sha>`); `.../git/tags/<sha>`
# peels an annotated tag to `commit $PS_COMMIT`; `release create` records its arguments.
RELEASE_SHIM = r"""
case "$1 $2" in
  "api repos/decdn/sdk-python/git/ref/tags/"*) echo "$PS_REF" ;;
  "api repos/decdn/sdk-python/git/tags/"*) echo "commit $PS_COMMIT" ;;
  "release create") ;;
  *) exit 1 ;;
esac
"""


@pytest.fixture
def notes(tmp_path: Path) -> Path:
    path = tmp_path / "notes.md"
    path.write_text("notes\n")
    return path


@pytest.mark.parametrize(
    ("prerelease", "flags"),
    [("false", ""), ("true", " --prerelease --latest=false")],
)
def test_github_release_makes_the_release_of_the_verified_commit(
    shim: Shim, bin_dir: Path, tmp_path: Path, notes: Path, prerelease: str, flags: str
) -> None:
    shim("gh", RELEASE_SHIM)
    r = run(
        "github-release.sh",
        bin_dir,
        tmp_path,
        "v0.2.0",
        SHA,
        str(notes),
        prerelease,
        PS_REF="tag " + "b" * 40,
        PS_COMMIT=SHA,
    )
    assert r.returncode == 0, r.stderr
    assert calls(tmp_path, "gh release create") == [
        "gh release create v0.2.0 --repo decdn/sdk-python --verify-tag --title v0.2.0"
        f" --notes-file {notes}{flags}"
    ]


@pytest.mark.parametrize(
    ("ref", "commit", "message"),
    [
        pytest.param(
            "tag " + "b" * 40, "c" * 40, f"v0.2.0 on decdn/sdk-python now names {'c' * 40}", id="moved"
        ),
        pytest.param("commit " + "c" * 40, "", f"now names {'c' * 40}, not {SHA}", id="made lightweight"),
        pytest.param("tree " + "c" * 40, "", "names a tree, not a commit", id="a tree"),
    ],
)
def test_github_release_stops_on_a_tag_that_moved(
    shim: Shim, bin_dir: Path, tmp_path: Path, notes: Path, ref: str, commit: str, message: str
) -> None:
    shim("gh", RELEASE_SHIM)
    r = run(
        "github-release.sh",
        bin_dir,
        tmp_path,
        "v0.2.0",
        SHA,
        str(notes),
        "false",
        PS_REF=ref,
        PS_COMMIT=commit,
    )
    assert r.returncode == 1
    assert message in r.stderr
    assert calls(tmp_path, "gh release create") == []


# `gh api .../git/ref/tags/<tag>` succeeds unless $PS_TAG is `missing` (a 404) or `denied`
# (a 401); `workflow run` fails when $PS_GH is `refuse`.
DISPATCH_SHIM = r"""
case "$1 $2" in
  "api repos/decdn/sdk-python/git/ref/tags/"*)
    [[ "${PS_TAG:-}" != missing ]] || { echo "gh: Not Found (HTTP 404)" >&2; exit 1; }
    [[ "${PS_TAG:-}" != denied ]] || { echo "gh: Bad credentials (HTTP 401)" >&2; exit 1; } ;;
  "workflow run")
    [[ "${PS_GH:-}" != refuse ]] || { echo "HTTP 422" >&2; exit 1; } ;;
  *) exit 1 ;;
esac
"""


@pytest.mark.parametrize(
    ("value", "dry_run"),
    [("", "false"), ("0", "false"), ("no", "false"), ("1", "true"), ("TRUE", "true"), ("yes", "true")],
)
def test_dispatches_the_workflow_on_main(
    shim: Shim, bin_dir: Path, tmp_path: Path, value: str, dry_run: str
) -> None:
    shim("gh", DISPATCH_SHIM)
    r = run("publish.sh", bin_dir, tmp_path, "v0.2.0", DECDN_PYPI_DRY_RUN=value)
    assert r.returncode == 0, r.stderr
    assert calls(tmp_path, "gh api") == ["gh api repos/decdn/sdk-python/git/ref/tags/v0.2.0 --silent"]
    assert calls(tmp_path, "gh workflow run") == [
        f"gh workflow run publish.yml --repo decdn/sdk-python --ref main -f tag=v0.2.0 -f dry_run={dry_run}"
    ]
    assert "Nothing is on PyPI until the run's publish job succeeds" in r.stdout


@pytest.mark.parametrize(
    ("tag", "message"),
    [
        pytest.param("missing", "decdn/sdk-python has no tag v0.2.0: push it first", id="no tag"),
        pytest.param(
            "denied", "cannot ask GitHub whether decdn/sdk-python has the tag v0.2.0", id="bad credentials"
        ),
    ],
)
def test_stops_before_the_dispatch_when_github_has_no_tag(
    shim: Shim, bin_dir: Path, tmp_path: Path, tag: str, message: str
) -> None:
    shim("gh", DISPATCH_SHIM)
    r = run("publish.sh", bin_dir, tmp_path, "v0.2.0", PS_TAG=tag)
    assert r.returncode == 1
    assert message in r.stderr
    assert calls(tmp_path, "gh workflow run") == []


def test_shows_githubs_error_when_it_is_not_a_404(shim: Shim, bin_dir: Path, tmp_path: Path) -> None:
    shim("gh", DISPATCH_SHIM)
    r = run("publish.sh", bin_dir, tmp_path, "v0.2.0", PS_TAG="denied")
    assert "Bad credentials (HTTP 401)" in r.stderr
    assert "push it first" not in r.stderr


def test_says_how_to_dispatch_by_hand_when_it_fails(shim: Shim, bin_dir: Path, tmp_path: Path) -> None:
    shim("gh", DISPATCH_SHIM)
    r = run("publish.sh", bin_dir, tmp_path, "v0.2.0", PS_GH="refuse")
    assert r.returncode == 1
    assert "nothing was published" in r.stderr
    assert "gh workflow run publish.yml --repo decdn/sdk-python --ref main -f tag=v0.2.0" in r.stderr
