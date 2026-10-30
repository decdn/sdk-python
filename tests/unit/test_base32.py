"""Site labels: base32 encoding of a manifest hash."""

import re

import pytest

from decdn.base32 import base32_to_hash, hash_to_base32
from decdn.errors import DecdnError

EMPTY = "af1349b9f5f9a1a6a0404dea36dcc9499bcb25c9adc112b7cc9a93cae41f3262"
ABC = "6437b3ac38465133ffb63b75273a8db548c558465d79db03fd359c6cd5bd9d85"


def test_encode_a_hash_as_52_lowercase_base32_characters_and_back() -> None:
    for h in ["00" * 32, "ff" * 32, EMPTY, ABC]:
        label = hash_to_base32(h)
        assert re.fullmatch(r"[a-z2-7]{52}", label)
        assert base32_to_hash(label) == h


def test_match_the_gateways_label_for_a_known_hash() -> None:
    # RFC 4648 base32 of 32 zero bytes, unpadded.
    assert hash_to_base32("00" * 32) == "a" * 52
    assert hash_to_base32("ff" * 32) == "7" * 51 + "q"


def test_decode_only_a_canonical_label() -> None:
    label = hash_to_base32(ABC)
    for bad in [
        label.upper(),
        label[:51],
        label + "a",
        label[:51] + "1",
        # Nonzero trailing bits: a second spelling of the same hash.
        "7" * 51 + "r",
    ]:
        assert base32_to_hash(bad) is None, bad


def test_hash_to_base32_rejects_a_non_hash() -> None:
    with pytest.raises(DecdnError) as info:
        hash_to_base32("nope")
    assert info.value.code == "BAD_URI"
