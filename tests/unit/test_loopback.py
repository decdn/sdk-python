"""The loopback gateway's delegate and capability API, once per client flavour."""

import json

import pytest

from decdn.loopback import Delegate, RegisteredCapability, Registration
from tests.conftest import fixture_json
from tests.fake_gateway import FakeGateway
from tests.support.flavour import Driver, rejects
from tests.support.postcard import encode_capability

pytestmark = pytest.mark.anyio

capability = fixture_json("capability")
OTHER_POOL = "0x" + "b3" * 32


def issued(pool: str, signer: str = capability["signer"], expiry: int = int(capability["expiry"])) -> str:
    """The fixture capability, from `pool` to `signer`."""
    return encode_capability(
        pool,
        signer,
        int(capability["spendingCap"]),
        expiry,
        bytes.fromhex(capability["ownerSignature"].removeprefix("0x")),
    )


async def test_returns_the_pending_key_registers_capabilities_on_it_and_lists_them(flavour: Driver) -> None:
    fake = FakeGateway("loopback")
    c = await flavour.open(fake)
    delegate = await c.loopback_delegate()
    assert isinstance(delegate, Delegate)
    assert len(delegate.address) == 42 and delegate.address.startswith("0x")
    assert (await c.loopback_delegate()) == delegate
    fake.pend(capability["signer"])
    registered = await c.loopback_register(capability=capability["token"])
    assert isinstance(registered, Registration)
    assert registered.signer.lower() == capability["signer"]
    assert (registered.pool, registered.expiry) == (capability["poolId"], int(capability["expiry"]))
    # The claimed key takes a capability from another pool too.
    second = await c.loopback_register(issued(OTHER_POOL))
    assert second.pool == OTHER_POOL
    listed = await c.loopback_capabilities()
    assert [
        RegisteredCapability(signer=r.signer.lower(), pool=r.pool, cap=r.cap, expiry=r.expiry, spent=r.spent)
        for r in listed
    ] == [
        RegisteredCapability(
            signer=capability["signer"],
            pool=pool,
            cap=int(capability["spendingCap"]),
            expiry=int(capability["expiry"]),
            spent=False,
        )
        for pool in (capability["poolId"], OTHER_POOL)
    ]
    put = next(s for s in fake.seen if s.method == "PUT")
    assert put.headers.get("content-type") == "application/json"
    assert json.loads(put.content) == {"capability": capability["token"]}
    # A claimed key leaves no pending one: the next call creates a fresh key, and keeps it.
    fresh = await c.loopback_delegate()
    assert fresh.address.lower() != capability["signer"]
    assert (await c.loopback_delegate()) == fresh


async def test_maps_the_apis_refusals(flavour: Driver) -> None:
    fake = FakeGateway("loopback")
    c = await flavour.open(fake)
    # A key the gateway does not hold.
    await rejects(c.loopback_register(capability["token"]), "CONFLICT")
    fake.pend(capability["signer"])
    await c.loopback_register(capability["token"])
    # A second capability from one pool on one key.
    await rejects(c.loopback_register(capability["token"]), "CONFLICT")
    await rejects(c.loopback_register("dcap1:AAAA"), "UNPROCESSABLE")


async def test_passes_the_browser_header_gate_as_a_client_outside_a_browser(flavour: Driver) -> None:
    fake = FakeGateway("loopback")
    c = await flavour.open(fake)
    delegate = await c.loopback_delegate()
    assert delegate.address.lower() == fake.pending
    assert await c.loopback_capabilities() == []
    for seen in fake.seen:
        assert seen.headers.get("origin") is None
        assert seen.headers.get("sec-fetch-site") is None
        assert not (seen.headers.get("user-agent") or "").startswith("Mozilla/")


async def test_is_a_loopback_gateways_alone(flavour: Driver) -> None:
    fake = FakeGateway("public")
    c = await flavour.open(fake)
    await rejects(c.loopback_delegate(), "CONFIG")
    assert len(fake.seen) == 0
    await rejects(c.get("decdn:nope"), "BAD_URI")
    assert len(fake.seen) == 0
