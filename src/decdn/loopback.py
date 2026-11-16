"""The loopback gateway's API
(https://github.com/decdn/decdn/blob/main/crates/cli/src/commands/gateway.rs):

`GET /_delegate` returns the gateway's pending delegate key, `PUT /_capability` registers a `dcap1:`
capability issued to a key the gateway holds, `GET /_capability` lists the registered ones in the
order the gateway pays with them. Keys and capabilities live in the gateway's memory: a restart
drops every one.
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
    pool: str
    # Unix-seconds expiry.
    expiry: int


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


def _check(context: str) -> None:
    if context != "loopback":
        raise DecdnError("CONFIG", "the delegate and capability API is a loopback gateway's alone")


def _headers(has_body: bool) -> dict[str, str]:
    return {"content-type": "application/json"} if has_body else {}


def _registration_body(capability: str) -> bytes:
    return json.dumps({"capability": capability}).encode()


def _delegate(data: bytes) -> Delegate:
    return Delegate(address=json.loads(data)["address"])


def _registration(data: bytes) -> Registration:
    parsed = json.loads(data)
    return Registration(signer=parsed["signer"], pool=parsed["pool"], expiry=parsed["expiry"])


def _capabilities(data: bytes) -> list[RegisteredCapability]:
    items: list[dict[str, Any]] = json.loads(data)
    return [
        RegisteredCapability(
            signer=i["signer"],
            pool=i["pool"],
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
        """Return the gateway's pending delegate key, the one to issue the next capability to.

        The gateway holds one pending key: every call returns the same address until a capability
        claims it, and the next call after that returns a fresh key.
        """
        return _delegate(self._call("GET", "/_delegate"))

    def register_capability(self, capability: str) -> Registration:
        """Register `capability`, a `dcap1:` issued to a key the gateway holds.

        A key holds one capability per pool: a key the gateway does not hold, or a second
        capability from the same pool on one key, raises `CONFLICT`.
        """
        return _registration(self._call("PUT", "/_capability", _registration_body(capability)))

    def capabilities(self) -> list[RegisteredCapability]:
        """Return the capabilities the gateway holds, in the order it pays with them."""
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

    async def register_capability(self, capability: str) -> Registration:
        """Register `capability`, a `dcap1:` issued to a key the gateway holds: `LoopbackApi`'s."""
        return _registration(await self._call("PUT", "/_capability", _registration_body(capability)))

    async def capabilities(self) -> list[RegisteredCapability]:
        """Return the capabilities the gateway holds, in the order it pays with them."""
        return _capabilities(await self._call("GET", "/_capability"))
