"""`dcap1:` capability tokens, as the Rust encoder writes them.

The encoder is
https://github.com/decdn/decdn/blob/main/crates/incentive/src/capability_grant.rs. A token
is `dcap1:` + base64url (no padding) of the postcard encoding of
`{pool_id: [u8;32], signer: [u8;20], spending_cap: u64, expiry: u64,
owner_signature: Vec<u8>}`. Postcard writes a byte string as a varint length and the
bytes, and an integer as a LEB128 varint.
"""

from dataclasses import dataclass

from decdn._hex import to_hex
from decdn.errors import DecdnError

_PREFIX = "dcap1:"
_B64URL = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


@dataclass(frozen=True)
class Capability:
    """A pool owner's spending grant to a delegate signer."""

    pool_id: str
    """The on-chain pool, `0x` + 64 hex."""
    signer: str
    """The delegate voucher-signing address, `0x` + 40 hex."""
    spending_cap: int
    """Cumulative spend ceiling in USDC base units."""
    expiry: int
    """Unix-seconds expiry."""
    owner_signature: str
    """The owner's raw EIP-712 signature, `0x` + hex."""


def _bad(message: str) -> DecdnError:
    return DecdnError("BAD_CAPABILITY", message)


def _truncated() -> DecdnError:
    return _bad("capability token payload is malformed (truncated or corrupt)")


def _from_base64url(body: str) -> bytes:
    out = bytearray()
    buffer = 0
    bits = 0
    for c in body:
        v = _B64URL.find(c)
        if v < 0:
            raise _bad("capability token body is not valid base64url")
        buffer = ((buffer << 6) | v) & 0xFFFF
        bits += 6
        if bits >= 8:
            bits -= 8
            out.append((buffer >> bits) & 0xFF)
    if len(body) % 4 == 1 or (buffer & ((1 << bits) - 1)) != 0:
        raise _bad("capability token body is not valid base64url")
    return bytes(out)


class _Reader:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._at = 0

    def varint(self) -> int:
        value = 0
        for shift in range(0, 70, 7):
            if self._at >= len(self._data):
                raise _truncated()
            b = self._data[self._at]
            self._at += 1
            value |= (b & 0x7F) << shift
            if b & 0x80 == 0:
                if value >= 1 << 64:
                    raise _truncated()
                return value
        raise _truncated()

    def bytes_of(self, length: int | None = None) -> bytes:
        n = self.varint()
        if length is not None and n != length:
            raise _truncated()
        if self._at + n > len(self._data):
            raise _truncated()
        out = self._data[self._at : self._at + n]
        self._at += n
        return out

    def done(self) -> bool:
        return self._at == len(self._data)


def parse_capability(token: str) -> Capability:
    """Decode a `dcap1:` token. Raises `BAD_CAPABILITY` for anything else."""
    if not token.startswith(_PREFIX):
        raise _bad("not a dcap1 capability token (missing the `dcap1:` prefix)")
    r = _Reader(_from_base64url(token[len(_PREFIX) :]))
    capability = Capability(
        pool_id=f"0x{to_hex(r.bytes_of(32))}",
        signer=f"0x{to_hex(r.bytes_of(20))}",
        spending_cap=r.varint(),
        expiry=r.varint(),
        owner_signature=f"0x{to_hex(r.bytes_of())}",
    )
    if not r.done():
        raise _truncated()
    return capability
