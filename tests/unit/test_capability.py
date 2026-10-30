import pytest

from decdn.capability import Capability, parse_capability
from decdn.errors import DecdnError
from tests.conftest import fixture_json
from tests.support.postcard import encode_capability

fixture = fixture_json("capability")
PREFIX = "dcap1:"


def bad(token: str) -> DecdnError:
    with pytest.raises(DecdnError) as info:
        parse_capability(token)
    return info.value


def test_decodes_the_token_the_rust_encoder_pins() -> None:
    assert parse_capability(fixture["token"]) == Capability(
        pool_id=fixture["poolId"],
        signer=fixture["signer"],
        spending_cap=int(fixture["spendingCap"]),
        expiry=int(fixture["expiry"]),
        owner_signature=fixture["ownerSignature"],
    )
    cap = parse_capability(fixture["token"])
    assert cap.spending_cap == 10000000
    assert cap.expiry == 1900000000


def test_rejects_a_missing_or_foreign_prefix() -> None:
    assert bad(fixture["token"][len(PREFIX) :]).code == "BAD_CAPABILITY"
    assert "dcap1:" in str(bad("dcap2:whatever"))


def test_rejects_a_body_that_is_not_base64url() -> None:
    assert "base64url" in str(bad("dcap1:not*base64*"))
    assert "base64url" in str(bad("dcap1:IBEi="))


def test_rejects_a_truncated_or_padded_payload() -> None:
    body = fixture["token"][len(PREFIX) :]
    assert bad(f"{PREFIX}{body[: len(body) // 2]}").code == "BAD_CAPABILITY"
    assert "malformed" in str(bad(f"{fixture['token']}AA"))


def test_encode_round_trips_the_pinned_token() -> None:
    signature = bytes.fromhex(fixture["ownerSignature"][2:])
    token = encode_capability(
        fixture["poolId"],
        fixture["signer"],
        int(fixture["spendingCap"]),
        int(fixture["expiry"]),
        signature,
    )
    assert token == fixture["token"]
    assert parse_capability(token) == parse_capability(fixture["token"])
