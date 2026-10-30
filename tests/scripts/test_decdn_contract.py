"""Gates of scripts/decdn-contract.sh.

Arguments it refuses stop it before any tool runs, and arguments it takes reach the
first tool check: those run the real script with a PATH that holds no tool at all. The
harness floor is checked once the tools are found, so its test runs a copy of the script
beside a fixture pyproject.toml, with shims for the tools a run needs, none of which a
refusal reaches.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.scripts.conftest import FIRST_TOOL_CHECK, ROOT, Shim, gate

pytestmark = pytest.mark.timeout(120)


def run(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return gate("decdn-contract.sh", tmp_path, *args)


@pytest.mark.parametrize("args", [[], ["--tag", "v0.3.0", "--ref", "main"]], ids=str)
def test_takes_exactly_one_of_tag_or_ref(tmp_path: Path, args: list[str]) -> None:
    r = run(tmp_path, *args)
    assert r.returncode == 1
    assert "pass exactly one of --tag or --ref" in r.stderr
    assert FIRST_TOOL_CHECK not in r.stderr


@pytest.mark.parametrize("arg", ["--bogus", "--tag", "--ref", "--decdn-dir"])
def test_stops_on_a_malformed_argument(tmp_path: Path, arg: str) -> None:
    r = run(tmp_path, arg)
    assert r.returncode == 1
    assert "usage: scripts/decdn-contract.sh" in r.stderr


@pytest.mark.parametrize("tag", ["0.3.0", "v0.3", "v03.0.0", "sdk-v0.3.0", "v0.3.0+build"])
def test_stops_on_the_tag_before_any_tool_runs(tmp_path: Path, tag: str) -> None:
    r = run(tmp_path, "--tag", tag)
    assert r.returncode == 1
    assert "the tag must be v<major>.<minor>.<patch>" in r.stderr
    assert FIRST_TOOL_CHECK not in r.stderr


def test_stops_on_a_ref_that_reads_as_an_option(tmp_path: Path) -> None:
    r = run(tmp_path, "--ref", "--upload-pack=x")
    assert r.returncode == 1
    assert "not a git ref" in r.stderr
    assert FIRST_TOOL_CHECK not in r.stderr


@pytest.mark.parametrize(
    "args",
    [
        ["--tag", "v0.3.0"],
        ["--tag", "v1.2.3-rc.1"],
        ["--ref", "main"],
        ["--ref", "feat/python-sdk", "--decdn-dir", "/tmp/decdn"],
    ],
    ids=str,
)
def test_takes_arguments_to_the_tool_checks(tmp_path: Path, args: list[str]) -> None:
    r = run(tmp_path, *args)
    assert r.returncode == 1
    assert FIRST_TOOL_CHECK in r.stderr


@pytest.mark.parametrize(
    ("floor", "args", "code", "message"),
    [
        pytest.param(
            "",
            ["--tag", "v0.5.0"],
            2,
            "decdn v0.5.0 has no Python SDK harness; the harness floor is unset",
            id="no floor",
        ),
        pytest.param(
            "0.5.0",
            ["--tag", "v0.4.9"],
            2,
            "decdn v0.4.9 has no Python SDK harness; the harness floor is 0.5.0",
            id="a tag below the floor",
        ),
        pytest.param(
            "0.5.0",
            ["--tag", "v0.10.0"],
            1,
            "install this repo's dependencies first",
            id="a tag above the floor",
        ),
        pytest.param(
            "0.5.0", ["--tag", "v0.5.0"], 1, "install this repo's dependencies first", id="the floor"
        ),
        pytest.param(
            "", ["--ref", "main"], 1, "install this repo's dependencies first", id="a ref, no floor"
        ),
    ],
)
def test_tag_below_harness_floor_is_refused(
    tmp_path: Path, shim: Shim, bin_dir: Path, floor: str, args: list[str], code: int, message: str
) -> None:
    sdk = tmp_path / "sdk"
    (sdk / "scripts").mkdir(parents=True)
    shutil.copy(ROOT / "scripts/decdn-contract.sh", sdk / "scripts/decdn-contract.sh")
    (sdk / "pyproject.toml").write_text(
        f'[project]\nname = "decdn"\nversion = "0.0.0"\n\n'
        f'[tool.decdn]\ngateway-floor = "0.3.0"\nharness-floor = "{floor}"\n'
    )
    for tool in ["git", "cargo", "cargo-nextest", "anvil", "forge", "uv", "gpg", "curl"]:
        shim(tool, "exit 1\n")
    log = tmp_path / "log"
    r = subprocess.run(
        ["bash", str(sdk / "scripts/decdn-contract.sh"), *args],
        cwd=tmp_path,
        env={"HOME": str(tmp_path), "PATH": f"{bin_dir}:/usr/bin:/bin", "SHIM_LOG": str(log)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == code, r.stderr
    assert message in r.stderr
    assert not log.exists()
