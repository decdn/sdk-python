import time

import httpx
import pytest

from decdn.errors import DecdnError
from decdn.pointer import (
    Pointer,
    PointerDomain,
    PointerSet,
    PointerTypedData,
    pointer_signing_hash,
    pointer_typed_data,
    recover_pointer_signer,
    sign_pointer,
)
from tests.conftest import fixture_json
from tests.fake_gateway import FakeGateway
from tests.support.flavour import Driver, rejects

fixture = fixture_json("pointer")
domain = PointerDomain(chain_id=fixture["chainId"], publisher_registry=fixture["publisherRegistry"])
pointer = Pointer(namespace=fixture["namespace"], root=fixture["root"], seq=fixture["seq"])
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141


def test_hashes_the_pointer_to_the_rust_signing_hash() -> None:
    assert pointer_signing_hash(domain, pointer) == fixture["signingHash"]


def test_signs_with_a_raw_key_to_the_rust_signature_byte_for_byte() -> None:
    assert sign_pointer(domain, pointer, fixture["privateKey"]) == fixture["signature"]
    assert sign_pointer(domain, pointer, bytes.fromhex(fixture["privateKey"][2:])) == fixture["signature"]


def test_recovers_the_owner_from_the_signature() -> None:
    assert recover_pointer_signer(domain, pointer, fixture["signature"]) == fixture["address"].lower()


def test_describes_the_typed_data_a_wallet_signs() -> None:
    assert pointer_typed_data(domain, pointer) == {
        "domain": {
            "name": "decdn-pointer",
            "version": "1",
            "chainId": fixture["chainId"],
            "verifyingContract": fixture["publisherRegistry"],
        },
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "Pointer": [
                {"name": "namespaceId", "type": "uint256"},
                {"name": "root", "type": "bytes32"},
                {"name": "seq", "type": "uint64"},
            ],
        },
        "primaryType": "Pointer",
        "message": {
            "namespaceId": pointer.namespace,
            "root": f"0x{fixture['root']}",
            "seq": pointer.seq,
        },
    }


def test_normalises_a_wallets_high_s_0_1_v_signature_to_low_s_27_28() -> None:
    sig = fixture["signature"][2:]
    r = sig[:64]
    s = int(sig[64:128], 16)
    v = int(sig[128:], 16)
    high = f"{N - s:064x}"
    flipped = f"{1 if v == 27 else 0:02x}"

    class Wallet:
        def sign_typed_data(self, typed_data: PointerTypedData) -> str:
            return f"0x{r}{high}{flipped}"

    assert sign_pointer(domain, pointer, Wallet()) == fixture["signature"]


def test_recover_rejects_high_s_and_bad_v() -> None:
    sig = fixture["signature"][2:]
    r, s, v = sig[:64], int(sig[64:128], 16), sig[128:]
    assert recover_pointer_signer(domain, pointer, f"0x{sig}") == fixture["address"].lower()
    for bad in (f"0x{r}{N - s:064x}{v}", f"0x{r}{s:064x}00", f"0x{r}{s:064x}1d", f"0x{sig[:128]}"):
        with pytest.raises(DecdnError) as info:
            recover_pointer_signer(domain, pointer, bad)
        assert info.value.code == "UNPROCESSABLE"


def gateway() -> FakeGateway:
    fake = FakeGateway("public")
    fake.pointer_domain = domain
    fake.owners[pointer.namespace] = fixture["address"].lower()
    return fake


async def open_with(flavour: Driver, fake: FakeGateway, *, with_domain: bool = True) -> Driver:
    return await flavour.open(fake, **({"pointer": domain} if with_domain else {}))


@pytest.mark.anyio
async def test_puts_a_signed_pointer_and_the_namespace_host_serves_the_bundle(flavour: Driver) -> None:
    fake = gateway()
    bundle = fake.add_bundle({"index.html": "<p>hi</p>"})
    c = await open_with(flavour, fake)
    done = await c.pointer_set(namespace=7, root=f"decdn:{bundle.hash}/", seq=5, signer=fixture["privateKey"])
    assert done == PointerSet(namespace=7, root=bundle.hash, seq=5, url="http://7.decdn.test/")
    assert fake.seen[0].method == "PUT"
    assert fake.seen[0].url.endswith("/_pointer")
    assert fake.seen[0].headers.get("content-type") == "application/json"
    assert "x-decdn-token" not in fake.seen[0].headers
    r = await c.get(f"decdn:{bundle.hash}/index.html")
    assert await c.text(r) == "<p>hi</p>"
    with httpx.Client(transport=fake.transport()) as http:
        assert http.get("http://7.decdn.test/index.html").text == "<p>hi</p>"


@pytest.mark.anyio
async def test_maps_a_replayed_seq_to_conflict_with_the_held_seq(flavour: Driver) -> None:
    fake = gateway()
    bundle = fake.add_bundle({"a.txt": "a"})
    c = await open_with(flavour, fake)
    signer = fixture["privateKey"]
    await c.pointer_set(namespace=7, root=bundle.hash, seq=9, signer=signer)
    e = await rejects(c.pointer_set(namespace=7, root=bundle.hash, seq=9, signer=signer), "CONFLICT")
    assert e.seq == 9


@pytest.mark.anyio
async def test_refuses_a_signer_that_does_not_own_the_namespace(flavour: Driver) -> None:
    fake = gateway()
    bundle = fake.add_bundle({"a.txt": "a"})
    c = await open_with(flavour, fake)
    await rejects(c.pointer_set(namespace=7, root=bundle.hash, signer="0x" + "33" * 32), "FORBIDDEN")


@pytest.mark.anyio
async def test_needs_the_chain_and_registry_a_public_gateway_and_a_bundle_root(flavour: Driver) -> None:
    fake = gateway()
    signer = fixture["privateKey"]
    root = fixture["root"]
    await rejects(
        (await open_with(flavour, fake, with_domain=False)).pointer_set(
            namespace=7, root=root, signer=signer
        ),
        "CONFIG",
    )
    loop = await flavour.open(gateway_url="http://127.0.0.1:8484", pointer=domain)
    await rejects(loop.pointer_set(namespace=7, root=root, signer=signer), "CONFIG")
    c = await open_with(flavour, fake)
    await rejects(c.pointer_set(namespace=7, root=f"decdn:{root}", signer=signer), "BAD_URI")
    await rejects(c.pointer_set(namespace=7, root=f"decdn:{root}/a.txt", signer=signer), "BAD_URI")
    await rejects(c.pointer_set(namespace=7, root="nothash", signer=signer), "BAD_URI")
    assert len(fake.seen) == 0


@pytest.mark.anyio
async def test_refuses_a_chain_id_namespace_or_seq_that_does_not_encode_exactly(flavour: Driver) -> None:
    for chain_id in (-1, 0, 1.5, 1 << 256, True):
        bad = PointerDomain(chain_id=chain_id, publisher_registry=domain.publisher_registry)  # type: ignore[arg-type]
        for f in (pointer_signing_hash, pointer_typed_data):
            with pytest.raises(DecdnError) as info:
                f(bad, pointer)
            assert info.value.code == "CONFIG"
    widest = PointerDomain(chain_id=(1 << 256) - 1, publisher_registry=domain.publisher_registry)
    assert len(pointer_signing_hash(widest, pointer)) == 66
    fake = gateway()
    c = await open_with(flavour, fake)
    signer = fixture["privateKey"]
    await rejects(c.pointer_set(namespace=1.5, root=fixture["root"], signer=signer), "CONFIG")
    await rejects(c.pointer_set(namespace=7, root=fixture["root"], seq=1.5, signer=signer), "CONFIG")
    with pytest.raises(DecdnError):
        c.client.namespace_url(1.5)  # type: ignore[arg-type]
    assert len(fake.seen) == 0


@pytest.mark.anyio
async def test_defaults_seq_to_the_current_unix_second(flavour: Driver) -> None:
    fake = gateway()
    bundle = fake.add_bundle({"a.txt": "a"})
    c = await open_with(flavour, fake)
    before = int(time.time())
    done = await c.pointer_set(namespace=7, root=bundle.hash, signer=fixture["privateKey"])
    assert before <= done.seq <= before + 2
