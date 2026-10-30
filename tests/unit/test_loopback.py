"""The loopback gateway's delegate and capability API, once per client flavour."""

import pytest

from decdn.loopback import Delegate, RegisteredCapability, Registration
from tests.conftest import fixture_json
from tests.fake_gateway import FakeGateway
from tests.support.flavour import Driver, rejects

pytestmark = pytest.mark.anyio

TOKEN = "cd" * 32
capability = fixture_json("capability")


async def open_loop(flavour: Driver, fake: FakeGateway, *, token: str | None = TOKEN) -> Driver:
    if token is None:
        return await flavour.open(fake)
    return await flavour.open(fake, token=token, host="example.com")


async def test_mints_a_delegate_registers_its_capability_and_lists_it(flavour: Driver) -> None:
    fake = FakeGateway("loopback", token=TOKEN)
    c = await open_loop(flavour, fake)
    delegate = await c.loopback_delegate()
    assert isinstance(delegate, Delegate)
    assert len(delegate.address) == 42 and delegate.address.startswith("0x")
    fake.delegates[capability["signer"].lower()] = None
    registered = await c.loopback_register(capability=capability["token"], scope="example.com")
    assert isinstance(registered, Registration)
    assert registered.signer.lower() == capability["signer"]
    assert (registered.scope, registered.expiry) == ("example.com", int(capability["expiry"]))
    listed = await c.loopback_capabilities()
    assert [
        RegisteredCapability(
            signer=r.signer.lower(),
            pool=r.pool,
            scope=r.scope,
            origin=r.origin,
            cap=r.cap,
            expiry=r.expiry,
            spent=r.spent,
        )
        for r in listed
    ] == [
        RegisteredCapability(
            signer=capability["signer"],
            pool=capability["poolId"],
            scope="example.com",
            origin=None,
            cap=int(capability["spendingCap"]),
            expiry=int(capability["expiry"]),
            spent=False,
        )
    ]
    put = next(s for s in fake.seen if s.method == "PUT")
    assert put.headers.get("content-type") == "application/json"
    assert put.headers.get("x-decdn-token") == TOKEN


async def test_maps_the_apis_refusals(flavour: Driver) -> None:
    fake = FakeGateway("loopback", token=TOKEN)
    c = await open_loop(flavour, fake)
    await rejects(c.loopback_register(capability=capability["token"], scope="example.com"), "CONFLICT")
    await rejects(c.loopback_register(capability="dcap1:AAAA", scope="example.com"), "UNPROCESSABLE")
    anonymous = await open_loop(flavour, fake, token=None)
    await rejects(anonymous.loopback_capabilities(), "UNAUTHORIZED")
    await rejects(anonymous.loopback_delegate(), "FORBIDDEN")


async def test_is_a_loopback_gateways_alone(flavour: Driver) -> None:
    fake = FakeGateway("public")
    c = await flavour.open(fake)
    await rejects(c.loopback_delegate(), "CONFIG")
    assert len(fake.seen) == 0
