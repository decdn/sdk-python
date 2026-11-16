"""The SDK against a loopback gateway with its token: the fake by default, the real binary when
decdn's contract run runs this file with `DECDN_CONTRACT`:
https://github.com/decdn/decdn/blob/main/crates/e2e/src/sdk_contract.rs

The tests run in file order and share the gateway: content is refused until a capability is
registered. Every flavour's capability is issued to the gateway's one pending key, each from its
own pool, and each flavour registers its own.
"""

import re
from collections.abc import Iterator

import pytest

from decdn.capability import parse_capability
from decdn.client import ByteRange
from decdn.hashing import blake3_hex
from tests.contract.fixture import Contract, contract, shipped_after
from tests.fake_gateway import NO_CAPABILITY
from tests.support.flavour import FLAVOURS, Driver, rejects

pytestmark = pytest.mark.anyio


@pytest.fixture(scope="module", name="contract")
def contract_fixture() -> Iterator[Contract]:
    c = contract("loopback")
    yield c
    c.close()


@pytest.fixture
async def client(contract: Contract, flavour: Driver) -> Driver:
    return await contract.open(flavour)


async def test_refuses_a_wrong_token(contract: Contract, flavour: Driver) -> None:
    c = contract
    wrong = await c.open(flavour, token="00" * 32)
    await rejects(wrong.get(f"decdn:{c.blob}"), "UNAUTHORIZED")


# The pending delegate key, capabilities without a scope, and payment by the held capabilities in
# registration order: decdn/decdn's gateway after release 0.3.1. A released gateway up to 0.3.1
# keeps a key per call and pays per page host, so it never collects these tests.
if shipped_after("0.3.1"):

    async def test_refuses_content_no_capability_pays_for_as_plain_text(
        contract: Contract, client: Driver
    ) -> None:
        # Nothing pulled the blob through the gateway yet, so its cache does not hold it.
        e = await rejects(client.get(f"decdn:{contract.blob}"), "PAYMENT_REQUIRED")
        assert e.body == NO_CAPABILITY
        assert e.fallback_uri is None

    async def test_returns_the_one_pending_key_until_a_capability_claims_it(
        contract: Contract, flavour: Driver
    ) -> None:
        c = contract
        # The API takes no token.
        anonymous = await flavour.open(gateway_url=c.gateway_url, transport=c.transport(flavour))
        first = await anonymous.loopback_delegate()
        again = await anonymous.loopback_delegate()
        assert re.fullmatch(r"0x[0-9a-fA-F]{40}", first.address)
        assert again.address == first.address
        assert first.address.lower() == parse_capability(c.capability(flavour)).signer.lower()

    async def test_registers_a_capability_from_each_pool_on_the_one_key(
        contract: Contract, flavour: Driver, client: Driver
    ) -> None:
        capability = contract.capability(flavour)
        registered = await client.loopback_register(capability=capability)
        grant = parse_capability(capability)
        assert registered.signer.lower() == grant.signer.lower()
        assert registered.pool.lower() == grant.pool_id.lower()
        assert registered.expiry == grant.expiry

    async def test_lists_the_capabilities_in_registration_order_without_the_token(
        contract: Contract, flavour: Driver
    ) -> None:
        c = contract
        anonymous = await flavour.open(gateway_url=c.gateway_url, transport=c.transport(flavour))
        held = await anonymous.loopback_capabilities()
        grants = [parse_capability(cap) for cap in c.capabilities[: len(FLAVOURS)]]
        wanted = [(g.signer.lower(), g.pool_id.lower()) for g in grants]
        # Every flavour registered its own, in flavour order, before any flavour lists them.
        mine = FLAVOURS.index(flavour.name)
        listed = [
            (h.signer.lower(), h.pool.lower()) for h in held if (h.signer.lower(), h.pool.lower()) in wanted
        ]
        assert listed == wanted
        entry = next(h for h in held if (h.signer.lower(), h.pool.lower()) == wanted[mine])
        assert (entry.cap, entry.expiry, entry.spent) == (
            grants[mine].spending_cap,
            grants[mine].expiry,
            False,
        )

    async def test_refuses_a_second_capability_from_one_pool_on_one_key(
        contract: Contract, flavour: Driver, client: Driver
    ) -> None:
        await rejects(client.loopback_register(capability=contract.capability(flavour)), "CONFLICT")

    async def test_creates_a_fresh_pending_key_once_a_capability_claims_it(
        contract: Contract, flavour: Driver, client: Driver
    ) -> None:
        claimed = parse_capability(contract.capability(flavour)).signer.lower()
        fresh = await client.loopback_delegate()
        assert fresh.address.lower() != claimed
        assert (await client.loopback_delegate()).address == fresh.address

    async def test_answers_no_preflight(contract: Contract, flavour: Driver) -> None:
        c = contract
        r = await c.fetch(
            flavour,
            f"{c.gateway_url}/_capability",
            method="OPTIONS",
            headers={"origin": "https://app.example", "access-control-request-method": "PUT"},
        )
        assert (r.status_code, r.text) == (403, "no cross-origin access\n")
        assert r.headers.get("access-control-allow-origin") is None

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
