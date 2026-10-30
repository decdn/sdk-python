"""The loopback gateway's API
(https://github.com/decdn/decdn/blob/main/crates/cli/src/commands/gateway.rs):

`GET /_delegate` mints a delegate key, `PUT /_capability` registers a `dcap1:` capability issued
to it for a scope, `GET /_capability` lists the registered ones (token only).
"""

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from decdn.errors import DecdnError

if TYPE_CHECKING:
    from decdn.client import AsyncClient, Client


@dataclass(frozen=True)
class Delegate:
    """A delegate key the gateway holds."""

    address: str


@dataclass(frozen=True)
class Registration:
    """A capability the gateway took."""

    signer: str
    scope: str
    # Unix-seconds expiry.
    expiry: int


@dataclass(frozen=True)
class RegisteredCapability:
    """A capability the gateway holds."""

    signer: str
    pool: str
    scope: str
    # The page origin that registered it, or `None` for the token holder.
    origin: str | None
    # The spending cap, in USDC base units.
    cap: int
    # Unix-seconds expiry.
    expiry: int
    # Whether a pull found it spent.
    spent: bool


def _check(context: str) -> None:
    if context != "loopback":
        raise DecdnError("CONFIG", "the delegate and capability API is a loopback gateway's alone")


def _headers(has_body: bool) -> dict[str, str]:
    return {"content-type": "application/json"} if has_body else {}


def _registration_body(capability: str, scope: str) -> bytes:
    return json.dumps({"capability": capability, "scope": scope}).encode()


def _delegate(data: bytes) -> Delegate:
    return Delegate(address=json.loads(data)["address"])


def _registration(data: bytes) -> Registration:
    parsed = json.loads(data)
    return Registration(signer=parsed["signer"], scope=parsed["scope"], expiry=parsed["expiry"])


def _capabilities(data: bytes) -> list[RegisteredCapability]:
    items: list[dict[str, Any]] = json.loads(data)
    return [
        RegisteredCapability(
            signer=i["signer"],
            pool=i["pool"],
            scope=i["scope"],
            origin=i["origin"],
            cap=i["cap"],
            expiry=i["expiry"],
            spent=i["spent"],
        )
        for i in items
    ]


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
        """Return a fresh delegate key the gateway holds for 15 minutes unless a capability claims it."""
        return _delegate(self._call("GET", "/_delegate"))

    def register_capability(self, *, capability: str, scope: str) -> Registration:
        """Register `capability` (a `dcap1:` issued to a delegate) for `scope`.

        `scope` is `example.com`, `*.example.com` or `*`.
        """
        return _registration(self._call("PUT", "/_capability", _registration_body(capability, scope)))

    def capabilities(self) -> list[RegisteredCapability]:
        """Return the capabilities the gateway holds. Needs the token."""
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
        """Return a fresh delegate key the gateway holds for 15 minutes unless a capability claims it."""
        return _delegate(await self._call("GET", "/_delegate"))

    async def register_capability(self, *, capability: str, scope: str) -> Registration:
        """Register `capability` (a `dcap1:` issued to a delegate) for `scope`: `LoopbackApi`'s."""
        return _registration(await self._call("PUT", "/_capability", _registration_body(capability, scope)))

    async def capabilities(self) -> list[RegisteredCapability]:
        """Return the capabilities the gateway holds. Needs the token."""
        return _capabilities(await self._call("GET", "/_capability"))
