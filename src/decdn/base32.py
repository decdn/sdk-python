"""Site labels: a bundle's manifest hash as RFC 4648 base32, lowercase and unpadded.

A label is 52 characters, inside the 63-character DNS label limit, as the gateway writes them
(https://github.com/decdn/decdn/blob/main/crates/cli/src/commands/gateway/site.rs).
"""

from decdn._hex import from_hex, is_hash, to_hex
from decdn.errors import DecdnError

_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"
_LABEL_LEN = 52


def hash_to_base32(hash: str) -> str:
    """Return the site label of a 64-hex-character hash."""
    data = from_hex(hash) if is_hash(hash) else None
    if data is None:
        raise DecdnError("BAD_URI", f"not a 64-hex-character hash: {hash}")
    out: list[str] = []
    buffer = 0
    bits = 0
    for b in data:
        buffer = (buffer << 8) | b
        bits += 8
        while bits >= 5:
            bits -= 5
            out.append(_ALPHABET[(buffer >> bits) & 31])
        buffer &= (1 << bits) - 1
    if bits > 0:
        out.append(_ALPHABET[(buffer << (5 - bits)) & 31])
    return "".join(out)


def base32_to_hash(label: str) -> str | None:
    """Return the hash a canonical site label encodes, as 64 lowercase hex characters.

    Return `None` for anything else: the wrong length, uppercase, or nonzero trailing bits (a
    second spelling the gateway refuses).
    """
    if len(label) != _LABEL_LEN or any(c not in _ALPHABET for c in label):
        return None
    out = bytearray()
    buffer = 0
    bits = 0
    for c in label:
        buffer = (buffer << 5) | _ALPHABET.index(c)
        bits += 5
        if bits >= 8:
            bits -= 8
            out.append((buffer >> bits) & 0xFF)
            buffer &= (1 << bits) - 1
    if buffer != 0:
        return None
    return to_hex(bytes(out))
