"""The loopback gateway's delegate and capability API, once per client flavour."""

import json

import pytest

from decdn.loopback import Delegate, NamespaceScope, RegisteredCapability, Registration
from tests.conftest import fixture_json
from tests.fake_gateway import FakeGateway
from tests.support.flavour import Driver, rejects
from tests.support.postcard import encode_capability

pytestmark = pytest.mark.anyio

capability = fixture_json("capability")
OTHER_POOL = "0x" + "b3" * 32
# A well-formed scope signature; the fake checks its shape alone.
SCOPE_SIGNATURE = "0x" + "5c" * 65


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
    registered = await c.loopback_register(capability=capability["token"], scope="any")
    assert isinstance(registered, Registration)
    assert registered.signer.lower() == capability["signer"]
    assert (registered.pool, registered.expiry) == (capability["poolId"], int(capability["expiry"]))
    assert registered.namespaces is None
    # The claimed key takes a capability from another pool too, scoped to namespaces.
    scope = NamespaceScope(namespaces=[7, 2**64 - 1], signature=SCOPE_SIGNATURE)
    second = await c.loopback_register(issued(OTHER_POOL), scope)
    assert (second.pool, second.namespaces) == (OTHER_POOL, [7, 2**64 - 1])
    listed = await c.loopback_capabilities()
    assert [
        RegisteredCapability(
            signer=r.signer.lower(),
            pool=r.pool,
            cap=r.cap,
            expiry=r.expiry,
            spent=r.spent,
            namespaces=r.namespaces,
        )
        for r in listed
    ] == [
        RegisteredCapability(
            signer=capability["signer"],
            pool=pool,
            cap=int(capability["spendingCap"]),
            expiry=int(capability["expiry"]),
            spent=False,
            namespaces=namespaces,
        )
        for pool, namespaces in ((capability["poolId"], None), (OTHER_POOL, [7, 2**64 - 1]))
    ]
    puts = [s for s in fake.seen if s.method == "PUT"]
    assert all(put.headers.get("content-type") == "application/json" for put in puts)
    assert [json.loads(put.content) for put in puts] == [
        {"capability": capability["token"], "scope": "any"},
        {
            "capability": issued(OTHER_POOL),
            "scope": {"namespaces": [7, 2**64 - 1], "signature": SCOPE_SIGNATURE},
        },
    ]
    # A claimed key leaves no pending one: the next call creates a fresh key, and keeps it.
    fresh = await c.loopback_delegate()
    assert fresh.address.lower() != capability["signer"]
    assert (await c.loopback_delegate()) == fresh


async def test_maps_the_apis_refusals(flavour: Driver) -> None:
    fake = FakeGateway("loopback")
    c = await flavour.open(fake)
    # A key the gateway does not hold.
    await rejects(c.loopback_register(capability["token"], "any"), "CONFLICT")
    fake.pend(capability["signer"])
    # A malformed scope: the SDK passes it through, and the gateway refuses it.
    await rejects(
        c.loopback_register(capability["token"], NamespaceScope(namespaces=[], signature=SCOPE_SIGNATURE)),
        "UNPROCESSABLE",
    )
    await c.loopback_register(capability["token"], "any")
    # A second capability from one pool on one key.
    await rejects(c.loopback_register(capability["token"], "any"), "CONFLICT")
    await rejects(c.loopback_register("dcap1:AAAA", "any"), "UNPROCESSABLE")


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
