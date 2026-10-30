"""The SDK against a loopback gateway with its token: the fake by default, the real binary when
decdn's contract run runs this file with `DECDN_CONTRACT`:
https://github.com/decdn/decdn/blob/main/crates/e2e/tests/gateway_sdk.rs

The tests run in file order and share the gateway: content is refused until the capability is
registered. Each flavour registers its own capability for its own host.
"""

import re
from collections.abc import Iterator

import pytest

from decdn.capability import parse_capability
from decdn.client import ByteRange
from decdn.hashing import blake3_hex
from tests.contract.fixture import Contract, contract
from tests.support.flavour import Driver, rejects

pytestmark = pytest.mark.anyio


@pytest.fixture(scope="module", name="contract")
def contract_fixture() -> Iterator[Contract]:
    c = contract("loopback")
    yield c
    c.close()


@pytest.fixture
async def client(contract: Contract, flavour: Driver) -> Driver:
    return await contract.open(flavour)


async def test_refuses_content_no_capability_pays_for_as_plain_text(
    contract: Contract, flavour: Driver, client: Driver
) -> None:
    c = contract
    host, _ = c.entry(flavour)
    e = await rejects(client.get(f"decdn:{c.blob}"), "PAYMENT_REQUIRED")
    assert e.body == f"no capability covers {host}"
    assert e.fallback_uri is None


async def test_registers_a_capability_for_the_host(
    contract: Contract, flavour: Driver, client: Driver
) -> None:
    host, capability = contract.entry(flavour)
    registered = await client.loopback_register(capability=capability, scope=host)
    grant = parse_capability(capability)
    assert registered.signer.lower() == grant.signer.lower()
    assert registered.scope == host
    assert registered.expiry == grant.expiry


async def test_lists_the_capability_it_holds(contract: Contract, flavour: Driver, client: Driver) -> None:
    host, capability = contract.entry(flavour)
    grant = parse_capability(capability)
    held = await client.loopback_capabilities()
    mine = next((h for h in held if h.signer.lower() == grant.signer.lower()), None)
    assert mine is not None
    assert (mine.scope, mine.origin, mine.cap, mine.expiry) == (host, None, grant.spending_cap, grant.expiry)
    assert mine.pool.lower() == grant.pool_id.lower()


async def test_refuses_the_same_capability_twice(contract: Contract, flavour: Driver, client: Driver) -> None:
    host, capability = contract.entry(flavour)
    await rejects(client.loopback_register(capability=capability, scope=host), "CONFLICT")


async def test_streams_a_blob_with_the_token_verified(contract: Contract, client: Driver) -> None:
    c = contract
    r = await client.get(f"decdn:{c.blob}")
    assert r.verified is True
    assert r.headers.get("access-control-allow-origin") is None
    assert blake3_hex(await client.read(r)) == c.blob


async def test_verifies_entries_on_the_bare_host_a_page_byte_exact(
    contract: Contract, client: Driver
) -> None:
    c = contract
    for path in ("a.txt", "index.html", "data.bin"):
        r = await client.get(f"decdn:{c.bundle}/{path}")
        assert r.verified is True, path
        assert await client.read(r) == c.files[path], path


async def test_serves_a_window_of_a_sized_entry(contract: Contract, client: Driver) -> None:
    c = contract
    r = await client.get(f"decdn:{c.bundle}/data.bin", range=ByteRange(offset=10, length=20))
    assert r.status == 206
    assert await client.read(r) == c.files["data.bin"][10:30]


async def test_answers_head_from_the_manifest(contract: Contract, client: Driver) -> None:
    c = contract
    info = await client.head(f"decdn:{c.bundle}/a.txt")
    assert info.size == len(c.files["a.txt"])
    assert info.content_type == "text/plain; charset=utf-8"


async def test_refuses_a_wrong_token_and_a_host_no_capability_covers(
    contract: Contract, flavour: Driver
) -> None:
    c = contract
    wrong = await c.open(flavour, token="00" * 32)
    await rejects(wrong.get(f"decdn:{c.blob}"), "UNAUTHORIZED")
    other = await c.open(flavour, host="other.example")
    await rejects(other.get(f"decdn:{c.blob}"), "PAYMENT_REQUIRED")


async def test_mints_a_delegate(client: Driver) -> None:
    delegate = await client.loopback_delegate()
    assert re.fullmatch(r"0x[0-9a-fA-F]{40}", delegate.address)


async def test_answers_the_api_only_to_the_token_or_a_page(contract: Contract, flavour: Driver) -> None:
    anonymous = await flavour.open(gateway_url=contract.gateway_url, transport=contract.transport(flavour))
    await rejects(anonymous.loopback_capabilities(), "UNAUTHORIZED")
