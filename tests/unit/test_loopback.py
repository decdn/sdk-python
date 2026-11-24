"""The loopback gateway's delegate and capability API, once per client flavour."""

import json

import httpx
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
    scope = NamespaceScope(namespaces=(7, 2**64 - 1), signature=SCOPE_SIGNATURE)
    second = await c.loopback_register(issued(OTHER_POOL), scope)
    assert (second.pool, second.namespaces) == (OTHER_POOL, (7, 2**64 - 1))
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
        for pool, namespaces in ((capability["poolId"], None), (OTHER_POOL, (7, 2**64 - 1)))
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
        c.loopback_register(capability["token"], NamespaceScope(namespaces=(), signature=SCOPE_SIGNATURE)),
        "UNPROCESSABLE",
    )
    await c.loopback_register(capability["token"], "any")
    # A second capability from one pool on one key.
    await rejects(c.loopback_register(capability["token"], "any"), "CONFLICT")
    await rejects(c.loopback_register("dcap1:AAAA", "any"), "UNPROCESSABLE")


async def test_raises_http_for_an_answer_outside_the_contract(flavour: Driver) -> None:
    bodies: dict[str, bytes] = {}

    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=bodies[request.url.path])

    c = await flavour.open(gateway_url="http://127.0.0.1:8484", transport=httpx.MockTransport(answer))
    signer = {"signer": "0x" + "22" * 20, "pool": OTHER_POOL, "expiry": 1}
    held = {**signer, "cap": 1, "spent": False}
    for path, body, call in (
        ("/_delegate", b"not json", c.loopback_delegate),
        ("/_delegate", b"{}", c.loopback_delegate),
        ("/_capability", b'{"signer":"0x"}', lambda: c.loopback_register(capability["token"], "any")),
        (
            "/_capability",
            json.dumps({**signer, "namespaces": ["7"]}).encode(),
            lambda: c.loopback_register(capability["token"], "any"),
        ),
        ("/_capability", b"{}", c.loopback_capabilities),
        ("/_capability", json.dumps([{**held, "namespaces": 7}]).encode(), c.loopback_capabilities),
        # Fields of the wrong type.
        ("/_delegate", b'{"address":42}', c.loopback_delegate),
        ("/_delegate", b"[]", c.loopback_delegate),
        (
            "/_capability",
            json.dumps({**signer, "expiry": "1"}).encode(),
            lambda: c.loopback_register(capability["token"], "any"),
        ),
        (
            "/_capability",
            json.dumps({**signer, "expiry": 1.5}).encode(),
            lambda: c.loopback_register(capability["token"], "any"),
        ),
        (
            "/_capability",
            json.dumps({**signer, "namespaces": [2**64]}).encode(),
            lambda: c.loopback_register(capability["token"], "any"),
        ),
        ("/_capability", json.dumps([{**held, "spent": "false"}]).encode(), c.loopback_capabilities),
        ("/_capability", json.dumps([{**held, "cap": -1}]).encode(), c.loopback_capabilities),
        ("/_capability", json.dumps([{**held, "cap": True}]).encode(), c.loopback_capabilities),
        ("/_capability", json.dumps([{**held, "pool": 7}]).encode(), c.loopback_capabilities),
        ("/_capability", b'["x"]', c.loopback_capabilities),
    ):
        bodies[path] = body
        e = await rejects(call(), "HTTP")
        assert str(e).startswith(f"the gateway's answer to {path} is not the JSON its contract names: ")
        assert e.body == body.decode()


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
    e = await rejects(c.loopback_delegate(), "CONFIG")
    assert str(e) == (
        "the SDK offers the delegate and capability API on a loopback client alone: open one on "
        "a localhost or loopback-address URL"
    )
    assert len(fake.seen) == 0
    await rejects(c.get("decdn:nope"), "BAD_URI")
    assert len(fake.seen) == 0
