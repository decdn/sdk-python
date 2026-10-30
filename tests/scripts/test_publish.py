"""Gates and runs of scripts/publish.sh. A PyPI file cannot be uploaded twice, so what the
script refuses must stop it before the upload.

The gates run the real script with a PATH that holds no tool at all: a tag that is not
`v<version>` and a typo in a switch stop before the first tool check, and input it
accepts gets as far as it.

The runs take a signed tag in the `repo` fixture, with git and gpg, and shims that share
a state directory:
- `uv` logs each call; `build` writes an empty sdist and wheel into `dist/`, and
  `publish` without `--dry-run` puts each file it is given on the stub PyPI;
- `curl` answers PyPI's JSON API from that stub, with a 404 when it lacks the version,
  or fails to reach it when $PS_PYPI is `down`;
- `gh` answers the repo's push permission with $PS_PUSH, `release view` from the stub
  releases (a draft when $PS_GH is `draft`, a failure when it is `down`), and
  `release create` records the release and its notes (or fails when $PS_GH is
  `refuse`).
"""

import re
import subprocess
from pathlib import Path

import pytest

from tests.scripts.conftest import FIRST_TOOL_CHECK, ROOT, Repo, Shim, gate

pytestmark = pytest.mark.timeout(120)

SCRIPT = ROOT / "scripts/publish.sh"

UV_SHIM = r"""
case "$1" in
  sync|run) ;;
  build)
    v="$(sed -n 's/^version = "\(.*\)"$/\1/p' pyproject.toml | head -n1)"
    mkdir -p dist
    : > "dist/decdn-$v.tar.gz"
    : > "dist/decdn-$v-py3-none-any.whl" ;;
  publish)
    [[ " $* " != *" --dry-run "* ]] || exit 0
    for file in "${@:2}"; do
      [[ "$file" == -* ]] || basename "$file" >> "$PS_STATE/pypi"
    done ;;
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
if [[ -n "$files" ]]; then
  python3 -c 'import json, sys
urls = [{"filename": f, "digests": {"sha256": "stub-" + f}} for f in sys.argv[1:]]
print(json.dumps({"urls": urls}))' $files > "$out"
  printf 200
else
  echo '{"message": "Not Found"}' > "$out"
  printf 404
fi
"""
GH_SHIM = r"""
case "$1 $2" in
  "api repos/decdn/sdk-python") echo "${PS_PUSH:-true}" ;;
  "release view")
    [[ "${PS_GH:-}" != down ]] || { echo "HTTP 502" >&2; exit 1; }
    [[ "${PS_GH:-}" != draft ]] || { echo true; exit 0; }
    [[ -f "$PS_STATE/release-$3" ]] || { echo "release not found" >&2; exit 1; }
    cat "$PS_STATE/release-$3" ;;
  "release create")
    [[ "${PS_GH:-}" != refuse ]] || { echo "HTTP 422" >&2; exit 1; }
    args=("$@")
    for ((i = 0; i < ${#args[@]}; i++)); do
      [[ "${args[i]}" != --notes-file ]] || cp "${args[i + 1]}" "$PS_STATE/notes-$3"
    done
    echo false > "$PS_STATE/release-$3" ;;
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


def gate_run(tmp_path: Path, tag: str, **env: str) -> subprocess.CompletedProcess[str]:
    return gate("publish.sh", tmp_path, tag, env=env)


@pytest.mark.parametrize(
    "tag",
    [
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
    ],
)
def test_stops_on_the_tag_before_any_tool_runs(tmp_path: Path, tag: str) -> None:
    r = gate_run(tmp_path, tag)
    assert r.returncode == 1
    assert "the tag must be v<major>.<minor>.<patch>" in r.stderr
    assert FIRST_TOOL_CHECK not in r.stderr


@pytest.mark.parametrize("tag", ["v0.1.0", "v1.20.3", "v0.2.0rc1", "v0.2.0a1", "v0.2.0b2", "v0.2.0.dev3"])
def test_takes_the_tag_to_the_tool_checks(tmp_path: Path, tag: str) -> None:
    r = gate_run(tmp_path, tag)
    assert r.returncode == 1
    assert FIRST_TOOL_CHECK in r.stderr


@pytest.mark.parametrize("value", ["maybe", "2", "on"])
def test_stops_on_a_dry_run_typo_before_any_tool_runs(tmp_path: Path, value: str) -> None:
    r = gate_run(tmp_path, "v0.1.0", DECDN_PYPI_DRY_RUN=value)
    assert r.returncode == 1
    assert f"DECDN_PYPI_DRY_RUN must be 1/true/yes or 0/false/no, got '{value}'" in r.stderr
    assert FIRST_TOOL_CHECK not in r.stderr


@pytest.mark.parametrize("value", ["", "0", "no", "1", "TRUE", "yes"])
def test_takes_the_dry_run_switch_to_the_tool_checks(tmp_path: Path, value: str) -> None:
    r = gate_run(tmp_path, "v0.1.0", DECDN_PYPI_DRY_RUN=value)
    assert r.returncode == 1
    assert FIRST_TOOL_CHECK in r.stderr


# ---- from a fixture tag ------------------------------------------------------


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


class Publish:
    def __init__(self, repo: Repo, state: Path) -> None:
        self.repo = repo
        self.state = state

    def __call__(self, tag: str, **env: str) -> subprocess.CompletedProcess[str]:
        return self.repo.run(["bash", str(SCRIPT), tag], **env)

    def tag(self, version: str, harness: str = "", version_py: str = "") -> None:
        """pyproject.toml, _version.py and CHANGELOG.md at `version`, committed, tagged
        v<version> (signed) and pushed."""
        self.repo.write("pyproject.toml", pyproject(version, harness))
        self.repo.write("src/decdn/_version.py", f'__version__ = "{version_py or version}"\n')
        self.repo.write("CHANGELOG.md", CHANGELOG)
        self.repo.git("add", "-A")
        self.repo.git("commit", "-qm", f"chore(release): v{version}")
        self.repo.git("tag", "-s", f"v{version}", "-m", f"decdn {version}")
        self.repo.git("push", "-q", "origin", "main", f"v{version}")

    def read(self, name: str) -> str:
        path = self.state / name
        return path.read_text() if path.exists() else ""

    def uploaded(self) -> list[str]:
        return [line for line in self.repo.calls().split("\n") if line.startswith("uv publish")]

    def created(self) -> list[str]:
        return [line for line in self.repo.calls().split("\n") if line.startswith("gh release create")]


@pytest.fixture
def publish(repo: Repo, shim: Shim, tmp_path: Path) -> Publish:
    state = tmp_path / "state"
    state.mkdir()
    shim("uv", UV_SHIM)
    shim("curl", CURL_SHIM)
    shim("gh", GH_SHIM)
    repo.env["PS_STATE"] = str(state)
    repo.env["UV_PUBLISH_TOKEN"] = "pypi-stub"
    return Publish(repo, state)


def test_uploads_a_release_then_makes_its_github_release(publish: Publish) -> None:
    publish.tag("0.2.0")
    r = publish("v0.2.0")
    assert r.returncode == 0, r.stderr
    assert publish.uploaded() == ["uv publish dist/decdn-0.2.0-py3-none-any.whl dist/decdn-0.2.0.tar.gz"]
    [create] = publish.created()
    assert re.fullmatch(
        r"gh release create v0\.2\.0 --repo decdn/sdk-python --verify-tag --title v0\.2\.0 --notes-file \S+",
        create,
    )
    assert publish.read("notes-v0.2.0") == f"{SECTION}\n\n---\n\n{footer('0.2.0', 'decdn')}"
    log = publish.repo.calls()
    assert log.index("uv publish") < log.index("gh release create")


def test_names_the_harness_floor_in_the_notes(publish: Publish) -> None:
    publish.tag("0.2.0", harness="0.5.0")
    r = publish("v0.2.0")
    assert r.returncode == 0, r.stderr
    assert publish.read("notes-v0.2.0") == f"{SECTION}\n\n---\n\n{footer('0.2.0', 'decdn', '0.5.0')}"


def test_makes_a_prereleases_release_a_prerelease_its_notes_the_footer(publish: Publish) -> None:
    publish.tag("0.3.0rc1")
    r = publish("v0.3.0rc1")
    assert r.returncode == 0, r.stderr
    assert publish.uploaded() == [
        "uv publish dist/decdn-0.3.0rc1-py3-none-any.whl dist/decdn-0.3.0rc1.tar.gz"
    ]
    assert publish.created()[0].endswith(" --prerelease --latest=false")
    assert publish.read("notes-v0.3.0rc1") == footer("0.3.0rc1", "decdn==0.3.0rc1")


def test_finishes_a_run_whose_github_release_failed(publish: Publish) -> None:
    publish.tag("0.2.0")
    failed = publish("v0.2.0", PS_GH="refuse")
    assert failed.returncode == 1
    assert "PyPI has decdn 0.2.0, but the GitHub release failed" in failed.stderr
    assert len(publish.uploaded()) == 1
    assert not (publish.state / "release-v0.2.0").exists()

    r = publish("v0.2.0")
    assert r.returncode == 0, r.stderr
    assert "decdn 0.2.0 is already on PyPI; not uploading it again" in r.stdout
    assert "stub-decdn-0.2.0.tar.gz" in r.stdout
    assert len(publish.uploaded()) == 1
    assert publish.read("release-v0.2.0") == "false\n"


# PyPI takes a release file by file: a run stopped between the two uploads leaves one.
def test_uploads_only_the_files_pypi_lacks(publish: Publish) -> None:
    publish.tag("0.2.0")
    (publish.state / "pypi").write_text("decdn-0.2.0.tar.gz\n")
    r = publish("v0.2.0")
    assert r.returncode == 0, r.stderr
    assert publish.uploaded() == ["uv publish dist/decdn-0.2.0-py3-none-any.whl"]
    assert len(publish.created()) == 1


def test_leaves_an_existing_release_as_it_is(publish: Publish) -> None:
    publish.tag("0.2.0")
    (publish.state / "pypi").write_text("decdn-0.2.0-py3-none-any.whl\ndecdn-0.2.0.tar.gz\n")
    (publish.state / "release-v0.2.0").write_text("false\n")
    r = publish("v0.2.0")
    assert r.returncode == 0, r.stderr
    assert "already has the release v0.2.0; left as is" in r.stdout
    assert publish.uploaded() == []
    assert publish.created() == []


def test_dry_runs_without_uploading_or_releasing_printing_the_notes(publish: Publish) -> None:
    publish.tag("0.2.0")
    r = publish("v0.2.0", DECDN_PYPI_DRY_RUN="1", UV_PUBLISH_TOKEN="")
    assert r.returncode == 0, r.stderr
    assert publish.uploaded() == [
        "uv publish --dry-run dist/decdn-0.2.0-py3-none-any.whl dist/decdn-0.2.0.tar.gz"
    ]
    assert publish.created() == []
    assert "The GitHub release would be: gh release create v0.2.0" in r.stdout
    assert footer("0.2.0", "decdn") in r.stdout
    assert publish.read("pypi") == ""


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        pytest.param({"PS_GH": "draft"}, "has a draft release for v0.2.0", id="a draft release for the tag"),
        pytest.param(
            {"PS_PYPI": "down"}, "cannot ask PyPI whether it has decdn 0.2.0", id="a PyPI it cannot ask"
        ),
        pytest.param(
            {"PS_GH": "down"},
            "cannot ask GitHub whether decdn/sdk-python has the release v0.2.0",
            id="a GitHub it cannot ask",
        ),
        pytest.param(
            {"PS_PUSH": "false"},
            "gh's account has no write access to decdn/sdk-python",
            id="an account without write access",
        ),
        pytest.param({"UV_PUBLISH_TOKEN": ""}, "no PyPI credentials", id="no PyPI credentials"),
        pytest.param({"GITHUB_ACTIONS": "true"}, "not in CI", id="a run in CI"),
    ],
)
def test_stops_before_the_upload(publish: Publish, extra: dict[str, str], message: str) -> None:
    publish.tag("0.2.0")
    r = publish("v0.2.0", **extra)
    assert r.returncode == 1
    assert message in r.stderr
    assert publish.uploaded() == []
    assert publish.created() == []


def test_stops_on_a_release_without_a_changelog_section_before_the_build(publish: Publish) -> None:
    publish.tag("0.4.0")
    r = publish("v0.4.0")
    assert r.returncode == 1
    assert "CHANGELOG.md at v0.4.0 has no '## [0.4.0]' section" in r.stderr
    assert not re.search(r"^uv ", publish.repo.calls(), re.M)


def test_stops_on_a_version_file_out_of_step_before_the_build(publish: Publish) -> None:
    publish.tag("0.2.0", version_py="0.1.0")
    r = publish("v0.2.0")
    assert r.returncode == 1
    assert "src/decdn/_version.py at v0.2.0 has __version__ '0.1.0', but the tag says 0.2.0" in r.stderr
    assert not re.search(r"^uv ", publish.repo.calls(), re.M)
