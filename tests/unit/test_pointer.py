from decdn.pointer import (
    Pointer,
    PointerDomain,
    PointerTypedData,
    pointer_signing_hash,
    pointer_typed_data,
    recover_pointer_signer,
    sign_pointer,
)
from tests.conftest import fixture_json

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
