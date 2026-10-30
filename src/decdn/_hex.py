"""Hex encoding helpers shared across the SDK."""

import re

_HEX_RE = re.compile(r"[0-9a-fA-F]*")
_HASH_RE = re.compile(r"[0-9a-fA-F]{64}")


def to_hex(data: bytes) -> str:
    """Return the lowercase hex spelling of `data`."""
    return data.hex()


def from_hex(text: str) -> bytes | None:
    """Return the bytes of an even-length hex string, or `None` if it is not hex."""
    if len(text) % 2 != 0 or _HEX_RE.fullmatch(text) is None:
        return None
    return bytes.fromhex(text)


def is_hash(text: str) -> bool:
    """Return whether `text` is a BLAKE3 hash spelled as 64 hex characters."""
    return _HASH_RE.fullmatch(text) is not None
