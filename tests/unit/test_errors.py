from decdn._hex import from_hex, is_hash, to_hex
from decdn.errors import DecdnError


def test_error_carries_code_and_details() -> None:
    err = DecdnError(
        "RANGE_NOT_SATISFIABLE",
        "range starts past the end",
        status=416,
        retry_after_secs=3,
        fallback_uri="decdn:abc",
        size=10,
        seq=7,
        body="gone",
    )
    assert err.code == "RANGE_NOT_SATISFIABLE"
    assert err.status == 416
    assert err.retry_after_secs == 3
    assert err.fallback_uri == "decdn:abc"
    assert err.size == 10
    assert err.seq == 7
    assert err.body == "gone"
    assert str(err) == "range starts past the end"
    assert isinstance(err, Exception)


def test_error_details_default_to_none() -> None:
    err = DecdnError("NETWORK", "unreachable")
    assert err.status is None
    assert err.retry_after_secs is None
    assert err.fallback_uri is None
    assert err.size is None
    assert err.seq is None
    assert err.body is None


def test_to_hex_is_lowercase_and_padded() -> None:
    assert to_hex(b"\x00\xff\x0a") == "00ff0a"


def test_from_hex_rejects_odd_or_nonhex() -> None:
    assert from_hex("abc") is None
    assert from_hex("zz") is None
    assert from_hex("00ff") == b"\x00\xff"
    assert from_hex("00FF") == b"\x00\xff"


def test_is_hash() -> None:
    assert is_hash("a" * 64)
    assert is_hash("AB" * 32)
    assert not is_hash("a" * 63)
    assert not is_hash("b3:" + "a" * 64)
