"""BLAKE3 hashing against the reference test vectors."""

from decdn.hashing import blake3_hex, create_blake3

# BLAKE3 of the empty input and of "abc", from the reference test vectors.
EMPTY = "af1349b9f5f9a1a6a0404dea36dcc9499bcb25c9adc112b7cc9a93cae41f3262"
ABC = "6437b3ac38465133ffb63b75273a8db548c558465d79db03fd359c6cd5bd9d85"


def test_blake3_hex_matches_the_reference_vectors() -> None:
    assert blake3_hex(b"") == EMPTY
    assert blake3_hex(b"abc") == ABC


def test_incremental_hasher_matches_one_shot() -> None:
    hasher = create_blake3()
    hasher.update(b"a")
    hasher.update(b"bc")
    assert hasher.hex() == ABC
