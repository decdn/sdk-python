"""A `dcap1:` capability encoder: the mirror of `decdn.capability.parse_capability`.

A token is `dcap1:` + base64url (no padding) of the postcard encoding of
`{pool_id: [u8;32], signer: [u8;20], spending_cap: u64, expiry: u64, owner_signature: Vec<u8>}`, as
https://github.com/decdn/decdn/blob/main/crates/incentive/src/capability_grant.rs writes it: a byte
string is a varint length and the bytes, an integer a LEB128 varint.
"""

import base64


def _varint(value: int) -> bytes:
    if value < 0 or value >= 1 << 64:
        raise ValueError(f"not a u64: {value}")
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value == 0:
            out.append(b)
            return bytes(out)
        out.append(b | 0x80)


def _bytes(data: bytes) -> bytes:
    return _varint(len(data)) + data


def _fixed(hex_: str, length: int) -> bytes:
    data = bytes.fromhex(hex_.removeprefix("0x"))
    if len(data) != length:
        raise ValueError(f"expected {length} bytes, got {len(data)}: {hex_}")
    return data


def encode_capability(
    pool_id: str, signer: str, spending_cap: int, expiry: int, owner_signature: bytes
) -> str:
    """Return the `dcap1:` token of a grant: `pool_id` and `signer` are `0x` hex."""
    payload = (
        _bytes(_fixed(pool_id, 32))
        + _bytes(_fixed(signer, 20))
        + _varint(spending_cap)
        + _varint(expiry)
        + _bytes(owner_signature)
    )
    return "dcap1:" + base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
