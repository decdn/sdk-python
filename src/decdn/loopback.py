"""The loopback gateway's API
(https://github.com/decdn/decdn/blob/main/crates/cli/src/commands/gateway.rs):

`GET /_delegate` returns the gateway's pending delegate key, `PUT /_capability` registers a `dcap1:`
capability issued to a key the gateway holds, with the scope of content it pays for,
`GET /_capability` lists the registered ones in registration order. Keys and
capabilities live in the gateway's memory: a restart drops every one.
"""

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from decdn._http import bool_field, read_json, text_field, u64_field
from decdn.errors import DecdnError

if TYPE_CHECKING:
    from decdn.client import AsyncClient, Client


@dataclass(frozen=True)
class NamespaceScope:
    """A capability scope that pays only for content of `namespaces`.

    The pool's on-chain owner signs EIP-712 `CapabilityScope(address signer,bytes32 poolId,
    uint256[] namespaces)` (domain `decdn-capability-scope`, version `1`, the chain id, and the
    PaymentPool as the verifying contract), with the capability's `signer` and `poolId`
    (https://github.com/decdn/decdn/blob/main/adr/appendix-gateway.md#scope). The SDK sends
    `signature` as given; the gateway checks it.
    """

    # Namespace ids (u64), at least one; a list given here is kept as a tuple.
    namespaces: tuple[int, ...]
    # The owner's 65-byte signature, `0x` hex.
    signature: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "namespaces", tuple(self.namespaces))


CapabilityScope = Literal["any"] | NamespaceScope
"""What a capability pays for: `"any"` content, or only the content of a `NamespaceScope`."""


@dataclass(frozen=True)
class Delegate:
    """A delegate key the gateway holds."""

    address: str


@dataclass(frozen=True)
class Registration:
    """A capability the gateway took."""

    signer: str
    pool: str
    # Unix-seconds expiry.
    expiry: int
    # The namespaces a namespace-scoped capability pays for; `None` for scope `"any"`.
    namespaces: tuple[int, ...] | None = None


@dataclass(frozen=True)
class RegisteredCapability:
    """A capability the gateway holds."""

    signer: str
    pool: str
    # The spending cap, in USDC base units.
    cap: int
    # Unix-seconds expiry.
    expiry: int
    # Whether a pull stopped for funding on it; the gateway pays with it no more.
    spent: bool
    # The namespaces a namespace-scoped capability pays for; `None` for scope `"any"`.
    namespaces: tuple[int, ...] | None = None


def _check(context: str) -> None:
    if context != "loopback":
        raise DecdnError(
            "CONFIG",
            "the SDK offers the delegate and capability API on a loopback client alone: open one on "
            "a localhost or loopback-address URL",
        )


def _headers(has_body: bool) -> dict[str, str]:
    return {"content-type": "application/json"} if has_body else {}


def _registration_body(capability: str, scope: CapabilityScope) -> bytes:
    wire: str | dict[str, Any] = (
        scope
        if isinstance(scope, str)
        else {"namespaces": list(scope.namespaces), "signature": scope.signature}
    )
    return json.dumps({"capability": capability, "scope": wire}).encode()


def _namespaces(value: Any) -> tuple[int, ...] | None:
    """A listing's `namespaces`: absent (`None`) for scope `"any"`, else a list of u64s."""
    if value is None:
        return None
    if not isinstance(value, list):
        raise TypeError(f"namespaces is not a list: {value!r}")
    return tuple(u64_field(n, "a namespace") for n in value)


def _delegate(data: bytes) -> Delegate:
    return read_json("/_delegate", data, lambda p: Delegate(address=text_field(p["address"], "address")))


def _registration(data: bytes) -> Registration:
    return read_json(
        "/_capability",
        data,
        lambda p: Registration(
            signer=text_field(p["signer"], "signer"),
            pool=text_field(p["pool"], "pool"),
            expiry=u64_field(p["expiry"], "expiry"),
            namespaces=_namespaces(p.get("namespaces")),
        ),
    )


def _capabilities(data: bytes) -> list[RegisteredCapability]:
    def read(items: Any) -> list[RegisteredCapability]:
        if not isinstance(items, list):
            raise TypeError(f"expected a list, not {type(items).__name__}")
        return [
            RegisteredCapability(
                signer=text_field(i["signer"], "signer"),
                pool=text_field(i["pool"], "pool"),
                cap=u64_field(i["cap"], "cap"),
                expiry=u64_field(i["expiry"], "expiry"),
                spent=bool_field(i["spent"], "spent"),
                namespaces=_namespaces(i.get("namespaces")),
            )
            for i in items
        ]

    return read_json("/_capability", data, read)


class LoopbackApi:
    """The delegate and capability API of a loopback gateway."""

    def __init__(self, client: "Client") -> None:
        self._client = client

    def _call(self, method: str, path: str, body: bytes | None = None) -> bytes:
        c = self._client
        _check(c.context)
        response = c._send(f"{c._origin}{path}", method, _headers(body is not None), content=body)
        try:
            return response.read()
        finally:
            response.close()

    def delegate(self) -> Delegate:
        """Return the gateway's pending delegate key, the one to issue the next capability to.

        The gateway holds one pending key: every call returns the same address until a capability
        claims it, and the next call after that returns a fresh key.
        """
        return _delegate(self._call("GET", "/_delegate"))

    def register_capability(self, capability: str, scope: CapabilityScope) -> Registration:
        """Register `capability`, a `dcap1:` issued to a key the gateway holds, to pay for `scope`.

        `scope` is `"any"` (any content) or a `NamespaceScope` the pool's owner signed (only the
        content of its namespaces). A key holds one capability per pool: a key the gateway does
        not hold, or a second capability from the same pool on one key, raises `CONFLICT`. A
        malformed scope, or a scope signature that does not recover the pool's owner, raises
        `UNPROCESSABLE`.
        """
        return _registration(self._call("PUT", "/_capability", _registration_body(capability, scope)))

    def capabilities(self) -> list[RegisteredCapability]:
        """Return the capabilities the gateway holds, in registration order.

        A request is paid by the first usable one scoped to the namespace it names, else by the
        first usable `"any"` one.
        """
        return _capabilities(self._call("GET", "/_capability"))


class AsyncLoopbackApi:
    """The delegate and capability API of a loopback gateway: `LoopbackApi`, with coroutines."""

    def __init__(self, client: "AsyncClient") -> None:
        self._client = client

    async def _call(self, method: str, path: str, body: bytes | None = None) -> bytes:
        c = self._client
        _check(c.context)
        response = await c._send(f"{c._origin}{path}", method, _headers(body is not None), content=body)
        try:
            return await response.aread()
        finally:
            await response.aclose()

    async def delegate(self) -> Delegate:
        """Return the gateway's pending delegate key: `LoopbackApi`'s."""
        return _delegate(await self._call("GET", "/_delegate"))

    async def register_capability(self, capability: str, scope: CapabilityScope) -> Registration:
        """Register `capability` to pay for `scope`: `LoopbackApi`'s."""
        return _registration(await self._call("PUT", "/_capability", _registration_body(capability, scope)))

    async def capabilities(self) -> list[RegisteredCapability]:
        """Return the capabilities the gateway holds, in registration order: `LoopbackApi`'s."""
        return _capabilities(await self._call("GET", "/_capability"))
