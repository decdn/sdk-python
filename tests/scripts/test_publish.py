"""Gates and runs of scripts/prepare-publish.sh, which checks and builds a release tag in
the `prepare` job of .github/workflows/publish.yml, and of scripts/publish.sh, which
dispatches that workflow. A PyPI file cannot be uploaded twice, so what prepare-publish.sh
refuses must stop the run before the `publish` job.

The gates run the real scripts with a PATH that holds no tool at all: a tag that is not
`v<version>` and a typo in a switch stop before the first tool check, and input they
accept gets as far as it.

The prepare runs take a signed tag in the `repo` fixture, with git and gpg, and shims that
share a state directory:
- `uv` logs each call; `build` writes an empty sdist and wheel into `dist/`;
- `curl` answers PyPI's JSON API from the stub file list in `pypi`, with a 404 when it
  lacks the version, fails to reach it when $PS_PYPI is `down`, or answers 200 with a
  body that is not JSON when it is `garbled`;
- `gh release view` answers from the stub releases (a draft when $PS_GH is `draft`, a
  failure when it is `down`).
The dispatch runs shim `gh`: the tag on GitHub exists unless $PS_TAG is `missing`, and
`workflow run` fails when $PS_GH is `refuse`.
"""

import re
import subprocess
from pathlib import Path

import pytest

from tests.scripts.conftest import FIRST_TOOL_CHECK, ROOT, Keys, Repo, Shim, gate

pytestmark = pytest.mark.timeout(120)

PREPARE = ROOT / "scripts/prepare-publish.sh"
PUBLISH = ROOT / "scripts/publish.sh"
# publish.sh needs gh alone.
GH_CHECK = "gh not found on PATH"

UV_SHIM = r"""
case "$1" in
  sync|run) ;;
  build)
    v="$(sed -n 's/^version = "\(.*\)"$/\1/p' pyproject.toml | head -n1)"
    mkdir -p dist
    : > "dist/decdn-$v.tar.gz"
    : > "dist/decdn-$v-py3-none-any.whl" ;;
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
if [[ "${PS_PYPI:-}" == garbled ]]; then
  echo "not json" > "$out"
  printf 200
elif [[ -n "$files" ]]; then
  python3 -c 'import json, sys
urls = [{"filename": f, "digests": {"sha256": "stub-" + f}} for f in sys.argv[1:]]
print(json.dumps({"urls": urls}))' $files > "$out"
  printf 200
else
  echo '{"message": "Not Found"}' > "$out"
  printf 404
fi
"""
GH_PREPARE_SHIM = r"""
case "$1 $2" in
  "release view")
    [[ "${PS_GH:-}" != down ]] || { echo "HTTP 502" >&2; exit 1; }
    [[ "${PS_GH:-}" != draft ]] || { echo true; exit 0; }
    [[ -f "$PS_STATE/release-$3" ]] || { echo "release not found" >&2; exit 1; }
    cat "$PS_STATE/release-$3" ;;
  *) exit 1 ;;
esac
"""
GH_DISPATCH_SHIM = r"""
case "$1 $2" in
  "api repos/decdn/sdk-python/git/ref/tags/"*)
    [[ "${PS_TAG:-}" != missing ]] || { echo "HTTP 404" >&2; exit 1; } ;;
  "workflow run")
    [[ "${PS_GH:-}" != refuse ]] || { echo "HTTP 422" >&2; exit 1; } ;;
  *) exit 1 ;;
esac
"""
SECTION = """### Fixed

- **Aborts surface as aborts.** Closing a stream mid-body raises
  `ABORTED`."""
CHANGELOG = f"""# Changelog

Intro.

## [0.2.0] — 2026-11-01

{SECTION}

## [0.1.0] — 2026-10-30

### Added

- **The client.**
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
]
TAG_ERROR = "the tag must be v<major>.<minor>.<patch>"


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


def prepare_gate(tmp_path: Path, tag: str) -> subprocess.CompletedProcess[str]:
    return gate("prepare-publish.sh", tmp_path, tag, "src", "KEYS", "out")


def publish_gate(tmp_path: Path, tag: str, **env: str) -> subprocess.CompletedProcess[str]:
    return gate("publish.sh", tmp_path, tag, env=env)


@pytest.mark.parametrize("tag", BAD_TAGS)
def test_prepare_stops_on_the_tag_before_any_tool_runs(tmp_path: Path, tag: str) -> None:
    r = prepare_gate(tmp_path, tag)
    assert r.returncode == 1
    assert TAG_ERROR in r.stderr
    assert FIRST_TOOL_CHECK not in r.stderr


@pytest.mark.parametrize("tag", TAGS)
def test_prepare_takes_the_tag_to_the_tool_checks(tmp_path: Path, tag: str) -> None:
    r = prepare_gate(tmp_path, tag)
    assert r.returncode == 1
    assert FIRST_TOOL_CHECK in r.stderr


@pytest.mark.parametrize("tag", BAD_TAGS)
def test_publish_stops_on_the_tag_before_any_tool_runs(tmp_path: Path, tag: str) -> None:
    r = publish_gate(tmp_path, tag)
    assert r.returncode == 1
    assert TAG_ERROR in r.stderr
    assert GH_CHECK not in r.stderr


@pytest.mark.parametrize("tag", TAGS)
def test_publish_takes_the_tag_to_the_tool_checks(tmp_path: Path, tag: str) -> None:
    r = publish_gate(tmp_path, tag)
    assert r.returncode == 1
    assert GH_CHECK in r.stderr


@pytest.mark.parametrize("value", ["maybe", "2", "on"])
def test_publish_stops_on_a_dry_run_typo_before_any_tool_runs(tmp_path: Path, value: str) -> None:
    r = publish_gate(tmp_path, "v0.1.0", DECDN_PYPI_DRY_RUN=value)
    assert r.returncode == 1
    assert f"DECDN_PYPI_DRY_RUN must be 1/true/yes or 0/false/no, got '{value}'" in r.stderr
    assert GH_CHECK not in r.stderr


@pytest.mark.parametrize("value", ["", "0", "no", "1", "TRUE", "yes"])
def test_publish_takes_the_dry_run_switch_to_the_tool_checks(tmp_path: Path, value: str) -> None:
    r = publish_gate(tmp_path, "v0.1.0", DECDN_PYPI_DRY_RUN=value)
    assert r.returncode == 1
    assert GH_CHECK in r.stderr


# ---- prepare-publish.sh from a fixture tag -------------------------------------


def pyproject(version: str, harness: str = "") -> str:
    """The real pyproject.toml at `version`, its floors 0.3.0 and `harness`."""
    text = re.sub(
        r'^version = ".*"$',
        f'version = "{version}"',
        (ROOT / "pyproject.toml").read_text(),
        count=1,
        flags=re.M,
    )
    text = re.sub(r'^gateway-floor = ".*"$', 'gateway-floor = "0.3.0"', text, flags=re.M)
    return re.sub(r'^harness-floor = ".*"$', f'harness-floor = "{harness}"', text, flags=re.M)


class Prepare:
    def __init__(self, repo: Repo, state: Path, out: Path) -> None:
        self.repo = repo
        self.state = state
        self.out = out

    def __call__(self, tag: str, **env: str) -> subprocess.CompletedProcess[str]:
        keys = self.repo.path / "KEYS"
        return self.repo.run(
            ["bash", str(PREPARE), tag, str(self.repo.path), str(keys), str(self.out)], **env
        )

    def tag(self, version: str, harness: str = "", version_py: str = "", key: str = "") -> None:
        """pyproject.toml, _version.py and CHANGELOG.md at `version`, committed and tagged
        v<version>, signed by `key` (default: the signer KEYS holds)."""
        self.repo.write("pyproject.toml", pyproject(version, harness))
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


@pytest.fixture
def prepare(repo: Repo, shim: Shim, tmp_path: Path) -> Prepare:
    state = tmp_path / "state"
    state.mkdir()
    shim("uv", UV_SHIM)
    shim("curl", CURL_SHIM)
    shim("gh", GH_PREPARE_SHIM)
    repo.env["PS_STATE"] = str(state)
    return Prepare(repo, state, tmp_path / "out")


def test_builds_a_release_and_renders_its_notes(prepare: Prepare) -> None:
    prepare.tag("0.2.0")
    r = prepare("v0.2.0")
    assert r.returncode == 0, r.stderr
    assert prepare.outputs() == {
        "version": "0.2.0",
        "prerelease": "false",
        "pending": "true",
        "has_release": "false",
    }
    assert prepare.dist() == ["decdn-0.2.0-py3-none-any.whl", "decdn-0.2.0.tar.gz"]
    assert prepare.notes() == f"{SECTION}\n\n---\n\n{footer('0.2.0', 'decdn')}"
    log = prepare.repo.calls()
    for step in [
        "uv sync --locked",
        "ruff check",
        "ruff format --check",
        "mypy",
        "pytest",
        "uv build",
        "twine check",
    ]:
        assert step in log


def test_names_the_harness_floor_in_the_notes(prepare: Prepare) -> None:
    prepare.tag("0.2.0", harness="0.5.0")
    r = prepare("v0.2.0")
    assert r.returncode == 0, r.stderr
    assert prepare.notes() == f"{SECTION}\n\n---\n\n{footer('0.2.0', 'decdn', '0.5.0')}"


def test_marks_a_prerelease_its_notes_the_footer(prepare: Prepare) -> None:
    prepare.tag("0.3.0rc1")
    r = prepare("v0.3.0rc1")
    assert r.returncode == 0, r.stderr
    assert prepare.outputs()["prerelease"] == "true"
    assert prepare.notes() == footer("0.3.0rc1", "decdn==0.3.0rc1")


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


def test_reports_an_existing_release(prepare: Prepare) -> None:
    prepare.tag("0.2.0")
    (prepare.state / "release-v0.2.0").write_text("false\n")
    r = prepare("v0.2.0")
    assert r.returncode == 0, r.stderr
    assert prepare.outputs()["has_release"] == "true"


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        pytest.param({"PS_GH": "draft"}, "has a draft release for v0.2.0", id="a draft release for the tag"),
        pytest.param(
            {"PS_PYPI": "down"}, "cannot ask PyPI whether it has decdn 0.2.0", id="a PyPI it cannot ask"
        ),
        pytest.param(
            {"PS_PYPI": "garbled"},
            "cannot read PyPI's answer for decdn 0.2.0",
            id="unreadable JSON from PyPI",
        ),
        pytest.param(
            {"PS_GH": "down"},
            "cannot ask GitHub whether decdn/sdk-python has the release v0.2.0",
            id="a GitHub it cannot ask",
        ),
    ],
)
def test_stops_without_outputs(prepare: Prepare, extra: dict[str, str], message: str) -> None:
    prepare.tag("0.2.0")
    r = prepare("v0.2.0", **extra)
    assert r.returncode == 1
    assert message in r.stderr
    assert not (prepare.out / "outputs").exists()


def test_stops_on_a_tag_signed_by_a_key_not_in_keys(prepare: Prepare, keys: Keys) -> None:
    prepare.tag("0.2.0", key=keys.stranger)
    r = prepare("v0.2.0")
    assert r.returncode == 1
    assert "v0.2.0 is not signed by a key published in KEYS" in r.stderr
    assert not re.search(r"^uv ", prepare.repo.calls(), re.M)


def test_stops_on_a_checkout_that_is_not_the_tag(prepare: Prepare) -> None:
    prepare.tag("0.2.0")
    prepare.repo.commit("later.txt", "chore: after the tag")
    r = prepare("v0.2.0")
    assert r.returncode == 1
    assert "is not checked out at v0.2.0" in r.stderr
    assert not re.search(r"^uv ", prepare.repo.calls(), re.M)


def test_stops_on_a_release_without_a_changelog_section_before_the_build(prepare: Prepare) -> None:
    prepare.tag("0.4.0")
    r = prepare("v0.4.0")
    assert r.returncode == 1
    assert "CHANGELOG.md at v0.4.0 has no '## [0.4.0]' section" in r.stderr
    assert not re.search(r"^uv ", prepare.repo.calls(), re.M)


def test_stops_on_a_version_file_out_of_step_before_the_build(prepare: Prepare) -> None:
    prepare.tag("0.2.0", version_py="0.1.0")
    r = prepare("v0.2.0")
    assert r.returncode == 1
    assert "src/decdn/_version.py at v0.2.0 has __version__ '0.1.0', but the tag says 0.2.0" in r.stderr
    assert not re.search(r"^uv ", prepare.repo.calls(), re.M)


def test_stops_on_a_tag_naming_another_version(prepare: Prepare) -> None:
    prepare.tag("0.2.0")
    prepare.repo.git("tag", "-s", "v0.2.1", "-m", "decdn 0.2.1")
    r = prepare("v0.2.1")
    assert r.returncode == 1
    assert "pyproject.toml is at 0.2.0, but the tag says 0.2.1" in r.stderr


# ---- publish.sh: the dispatch --------------------------------------------------


@pytest.fixture
def dispatch(repo: Repo, shim: Shim) -> Repo:
    shim("gh", GH_DISPATCH_SHIM)
    return repo


def dispatched(repo: Repo) -> list[str]:
    return [line for line in repo.calls().split("\n") if line.startswith("gh workflow run")]


@pytest.mark.parametrize(("value", "dry_run"), [("", "false"), ("1", "true")])
def test_dispatches_the_workflow_on_main(dispatch: Repo, value: str, dry_run: str) -> None:
    r = dispatch.run(["bash", str(PUBLISH), "v0.2.0"], DECDN_PYPI_DRY_RUN=value)
    assert r.returncode == 0, r.stderr
    assert dispatched(dispatch) == [
        f"gh workflow run publish.yml --repo decdn/sdk-python --ref main -f tag=v0.2.0 -f dry_run={dry_run}"
    ]


def test_stops_before_the_dispatch_on_a_tag_github_lacks(dispatch: Repo) -> None:
    r = dispatch.run(["bash", str(PUBLISH), "v0.2.0"], PS_TAG="missing")
    assert r.returncode == 1
    assert "decdn/sdk-python has no tag v0.2.0: push it first" in r.stderr
    assert dispatched(dispatch) == []


def test_says_how_to_dispatch_by_hand_when_it_fails(dispatch: Repo) -> None:
    r = dispatch.run(["bash", str(PUBLISH), "v0.2.0"], PS_GH="refuse")
    assert r.returncode == 1
    assert "nothing was published" in r.stderr
    assert "gh workflow run publish.yml --repo decdn/sdk-python --ref main -f tag=v0.2.0" in r.stderr
