import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from tests.support.flavour import FLAVOURS, Driver

_FIXTURES = Path(__file__).parent / "fixtures"


def fixture_json(name: str) -> dict[str, Any]:
    """Return the parsed contents of `tests/fixtures/<name>.json`."""
    data: dict[str, Any] = json.loads((_FIXTURES / f"{name}.json").read_text())
    return data


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(params=FLAVOURS, ids=FLAVOURS)
async def flavour(request: pytest.FixtureRequest) -> AsyncIterator[Driver]:
    """A `Driver` of each client flavour, `sync` first; it closes the clients it opened."""
    driver = Driver(request.param)
    yield driver
    await driver.close_all()
