"""Namespace pointers.

A pointer is an owner-signed EIP-712 record
(https://github.com/decdn/decdn/blob/main/crates/cli/src/commands/gateway/pointer.rs)

    Pointer(uint256 namespaceId,bytes32 root,uint64 seq)

under the domain {name: "decdn-pointer", version: "1", chainId, verifyingContract:
PublisherRegistry}. The gateway takes a 65-byte `r‖s‖v` signature with a low `s` only.
"""

import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, TypedDict

import coincurve
from Crypto.Hash import keccak

from decdn._hex import from_hex, is_hash, to_hex
from decdn.errors import DecdnError
from decdn.uri import SCHEME, parse_uri

if TYPE_CHECKING:
    from decdn.client import AsyncClient, Client


@dataclass(frozen=True)
class PointerDomain:
    """The chain a pointer is signed for: the EIP-712 domain of `PUT /_pointer`."""

    chain_id: int
    # The `PublisherRegistry` contract address.
    publisher_registry: str


@dataclass(frozen=True)
class Pointer:
    """A pointer from a namespace to the bundle whose manifest hash is `root`."""

    namespace: int
    # The bundle's manifest hash, 64 hex characters (optional `0x`).
    root: str
    seq: int


class _TypedField(TypedDict):
    name: str
    type: str


class _Eip712Domain(TypedDict):
    name: Literal["decdn-pointer"]
    version: Literal["1"]
    chainId: int
    verifyingContract: str


class _PointerTypes(TypedDict):
    EIP712Domain: list[_TypedField]
    Pointer: list[_TypedField]


class _PointerMessage(TypedDict):
    namespaceId: int
    root: str
    seq: int


class PointerTypedData(TypedDict):
    """The EIP-712 typed data of a pointer, as `eth_account`'s `full_message` takes it."""

    domain: _Eip712Domain
    types: _PointerTypes
    primaryType: Literal["Pointer"]
    message: _PointerMessage


class TypedDataSigner(Protocol):
    """Anything that signs EIP-712 typed data: a wallet behind the same call shape."""

    def sign_typed_data(self, typed_data: PointerTypedData) -> str: ...


# A signer: a typed-data signer, or a raw secp256k1 key (`0x` hex, bare hex, or 32 bytes).
PointerSigner = TypedDataSigner | str | bytes

_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_U64_MAX = (1 << 64) - 1
_U256_MAX = (1 << 256) - 1


def _bad(message: str) -> DecdnError:
    return DecdnError("CONFIG", message)


def _keccak(data: bytes) -> bytes:
    return keccak.new(digest_bits=256, data=data).digest()


_DOMAIN_TYPE = _keccak(b"EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)")
_POINTER_TYPE = _keccak(b"Pointer(uint256 namespaceId,bytes32 root,uint64 seq)")


def _integer(value: int, what: str) -> int:
    """Return `value`; `CONFIG` for anything that is not an int (a `bool` is not)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise _bad(f"{what} is an integer: got {value!r}")
    return value


def _chain_id_of(domain: PointerDomain) -> int:
    """The domain's chain id: an integer of at least 1 that fits a uint256."""
    chain_id = _integer(domain.chain_id, "a chain id")
    if chain_id < 1 or chain_id > _U256_MAX:
        raise _bad("a chain id is a uint256 of at least 1")
    return chain_id


def _word(n: int) -> bytes:
    """`n` as a 32-byte big-endian word."""
    if n < 0 or n > _U256_MAX:
        raise _bad(f"{n} does not fit a uint256")
    return n.to_bytes(32, "big")


def _address(text: str) -> bytes:
    raw = from_hex(text[2:]) if isinstance(text, str) and text.startswith("0x") else None
    if raw is None or len(raw) != 20:
        raise _bad(f"not an address: {text}")
    return raw


def _checked(pointer: Pointer) -> bytes:
    """Validate `pointer` and return its root as 32 bytes."""
    namespace = _integer(pointer.namespace, "a namespace id")
    seq = _integer(pointer.seq, "a pointer seq")
    if namespace < 1 or namespace > _U64_MAX:
        raise _bad("a namespace id is a u64 of at least 1")
    if seq < 0 or seq > _U64_MAX:
        raise _bad("a pointer seq is a u64")
    root = pointer.root[2:] if pointer.root.startswith("0x") else pointer.root
    if not is_hash(root):
        raise _bad(f"not a manifest hash: {pointer.root}")
    return bytes.fromhex(root)


def pointer_typed_data(domain: PointerDomain, pointer: Pointer) -> PointerTypedData:
    """The typed data a wallet signs for `pointer`."""
    root = _checked(pointer)
    _address(domain.publisher_registry)
    return {
        "domain": {
            "name": "decdn-pointer",
            "version": "1",
            "chainId": _chain_id_of(domain),
            "verifyingContract": domain.publisher_registry,
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
        "message": {"namespaceId": pointer.namespace, "root": f"0x{to_hex(root)}", "seq": pointer.seq},
    }


def _digest(domain: PointerDomain, pointer: Pointer) -> bytes:
    root = _checked(pointer)
    separator = _keccak(
        _DOMAIN_TYPE
        + _keccak(b"decdn-pointer")
        + _keccak(b"1")
        + _word(_chain_id_of(domain))
        + bytes(12)
        + _address(domain.publisher_registry)
    )
    struct = _keccak(_POINTER_TYPE + _word(pointer.namespace) + root + _word(pointer.seq))
    return _keccak(b"\x19\x01" + separator + struct)


def pointer_signing_hash(domain: PointerDomain, pointer: Pointer) -> str:
    """The EIP-712 signing hash of `pointer`, `0x` + 64 hex."""
    return f"0x{to_hex(_digest(domain, pointer))}"


def _normalised(signature: str) -> str:
    """A 65-byte `r‖s‖v` signature with a low `s` and `v` of 27 or 28, as the gateway takes it."""
    hex_ = signature[2:] if signature.startswith("0x") else signature
    raw = from_hex(hex_)
    if raw is None or len(raw) != 65:
        raise _bad("the signer returned no 65-byte signature")
    r = raw[:32]
    s = int.from_bytes(raw[32:64], "big")
    parity = raw[64]
    if parity >= 27:
        parity -= 27
    if parity not in (0, 1):
        raise _bad("the signature's v is not 0, 1, 27 or 28")
    if s > _N // 2:
        s = _N - s
        parity ^= 1
    return f"0x{to_hex(r)}{s:064x}{27 + parity:02x}"


def _key_of(private_key: str | bytes) -> bytes:
    if isinstance(private_key, str):
        raw = from_hex(private_key[2:] if private_key.startswith("0x") else private_key)
    else:
        raw = private_key
    if raw is None or len(raw) != 32:
        raise _bad("a private key is 32 bytes")
    return raw


def sign_pointer(domain: PointerDomain, pointer: Pointer, signer: PointerSigner) -> str:
    """Sign `pointer` for `domain` with `signer`; a 65-byte `0x` hex signature."""
    if isinstance(signer, str | bytes):
        key = _key_of(signer)
        digest = _digest(domain, pointer)
        try:
            sig = coincurve.PrivateKey(key).sign_recoverable(digest, hasher=None)
        except ValueError as error:
            raise _bad("a private key is a valid secp256k1 scalar") from error
        return _normalised(f"{to_hex(sig[:64])}{27 + sig[64]:02x}")
    return _normalised(signer.sign_typed_data(pointer_typed_data(domain, pointer)))


def recover_pointer_signer(domain: PointerDomain, pointer: Pointer, signature: str) -> str:
    """The lowercase address that signed `pointer`; `UNPROCESSABLE` for a malformed or high-`s` signature."""
    malformed = DecdnError("UNPROCESSABLE", "the pointer signature is malformed")
    hex_ = signature[2:] if signature.startswith("0x") else signature
    raw = from_hex(hex_) if len(hex_) == 130 else None
    if raw is None or raw[64] not in (27, 28):
        raise malformed
    if int.from_bytes(raw[32:64], "big") > _N // 2:
        raise malformed
    try:
        public = coincurve.PublicKey.from_signature_and_message(
            raw[:64] + bytes([raw[64] - 27]), _digest(domain, pointer), hasher=None
        )
    except ValueError as error:
        raise malformed from error
    return f"0x{to_hex(_keccak(public.format(compressed=False)[1:])[12:])}"


@dataclass(frozen=True)
class PointerSet:
    """A pointer the gateway took."""

    namespace: int
    # The bundle's manifest hash.
    root: str
    seq: int
    # The namespace's host on the gateway.
    url: str


def _request(
    context: str,
    domain: PointerDomain | None,
    namespace: int,
    root: str,
    seq: int | None,
    signer: PointerSigner,
) -> bytes:
    """Validate a `set` call, sign the pointer and return the `PUT` body."""
    if context != "public":
        raise _bad("namespace pointers live on a public gateway")
    if domain is None:
        raise _bad("pointer.set needs Client(pointer=PointerDomain(chain_id, publisher_registry))")
    bare = root[2:] if root.startswith("0x") else root
    if bare.startswith(SCHEME):
        uri = parse_uri(bare)
        if uri.kind != "bundle":
            raise DecdnError(
                "BAD_URI",
                "a pointer names a bundle: pass `decdn:<hash>/`, the manifest hash with its trailing slash",
            )
        bare = uri.hash
    elif not is_hash(bare):
        raise DecdnError("BAD_URI", f"not a manifest hash: {root}")
    pointer = Pointer(
        namespace=_integer(namespace, "a namespace id"),
        root=bare.lower(),
        seq=int(time.time()) if seq is None else _integer(seq, "a pointer seq"),
    )
    signature = sign_pointer(domain, pointer, signer)
    return (
        f'{{"namespace":{pointer.namespace},"root":"0x{pointer.root}","seq":{pointer.seq},'
        f'"signature":"{signature}"}}'
    ).encode()


def _pointer_set(body: bytes) -> PointerSet:
    parsed = json.loads(body)
    return PointerSet(
        namespace=parsed["namespace"], root=parsed["root"], seq=parsed["seq"], url=parsed["url"]
    )


class PointerApi:
    """Namespace pointers on a public gateway."""

    def __init__(self, client: "Client") -> None:
        self._client = client

    def set(self, *, namespace: int, root: str, seq: int | None = None, signer: PointerSigner) -> PointerSet:
        """Sign a pointer from `namespace` to the bundle `root` and `PUT` it.

        `root` is `decdn:<hash>/` or its manifest hash. `seq` defaults to the current Unix
        second; the gateway takes only a `seq` above the one it holds from the same signer. The
        signer must own the namespace on the `PublisherRegistry` of the client's `pointer` domain.
        """
        c = self._client
        body = _request(c.context, c._pointer_domain, namespace, root, seq, signer)
        response = c._send(f"{c._origin}/_pointer", "PUT", {"content-type": "application/json"}, content=body)
        try:
            return _pointer_set(response.read())
        finally:
            response.close()


class AsyncPointerApi:
    """Namespace pointers on a public gateway: `PointerApi`, with coroutines."""

    def __init__(self, client: "AsyncClient") -> None:
        self._client = client

    async def set(
        self, *, namespace: int, root: str, seq: int | None = None, signer: PointerSigner
    ) -> PointerSet:
        """Sign a pointer from `namespace` to the bundle `root` and `PUT` it: `PointerApi.set`."""
        c = self._client
        body = _request(c.context, c._pointer_domain, namespace, root, seq, signer)
        response = await c._send(
            f"{c._origin}/_pointer", "PUT", {"content-type": "application/json"}, content=body
        )
        try:
            return _pointer_set(await response.aread())
        finally:
            await response.aclose()
