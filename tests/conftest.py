import json
from pathlib import Path
from typing import Any

import pytest

_FIXTURES = Path(__file__).parent / "fixtures"


def fixture_json(name: str) -> dict[str, Any]:
    """Return the parsed contents of `tests/fixtures/<name>.json`."""
    data: dict[str, Any] = json.loads((_FIXTURES / f"{name}.json").read_text())
    return data


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"
