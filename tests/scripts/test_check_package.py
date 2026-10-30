import subprocess
from pathlib import Path


def test_check_package_passes() -> None:
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(["bash", "scripts/check-package.sh"], cwd=root, check=False)
    assert result.returncode == 0
