"""What the contract suite runs against.

With `DECDN_CONTRACT` set to a JSON file, written by decdn's
https://github.com/decdn/decdn/blob/main/crates/e2e/tests/gateway_sdk.rs, a real `decdn gateway`
serving the content the file describes; without it, the fake gateway over HTTP, serving the same
shapes. Every request goes through a `HostTransport` to the listener, with the URL's host as `Host`.
"""

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TypedDict

import httpx

from decdn.capability import parse_capability
from decdn.pointer import PointerDomain
from tests.conftest import fixture_json
from tests.fake_gateway import FakeGateway
from tests.support.flavour import FLAVOURS, Driver
from tests.support.host_transport import AsyncHostTransport, HostTransport
from tests.support.postcard import encode_capability

Mode = Literal["public", "loopback"]

SECOND_SIGNER = "0x00000000000000000000000000000000000000a2"
"""The delegate the fake's second capability names, for the `async` flavour."""


class CapabilityFile(TypedDict):
    """A `dcap1:` issued to a delegate the gateway holds, not yet registered, and the host it is for."""

    host: str
    capability: str


class PointerFile(TypedDict, total=False):
    """Namespaces the key owns on the registry (public)."""

    chainId: str
    publisherRegistry: str
    namespace: str
    # One namespace per client flavour, `sync` first.
    namespaces: list[str]
    privateKey: str


class FixtureFile(TypedDict, total=False):
    """The fixture file's shape: every byte string hex, every u64 decimal."""

    mode: Mode
    # The URL the SDK opens: `http://decdn.test` (public) or `http://127.0.0.1:<port>`.
    gatewayUrl: str
    # `host:port` every request connects to.
    connect: str
    token: str
    host: str
    capability: str
    # One capability per client flavour, `sync` first (loopback).
    capabilities: list[CapabilityFile]
    # The bundle's manifest hash, and its files.
    bundle: str
    files: dict[str, str]
    # A raw blob, and its bytes.
    blob: str
    blobBytes: str
    pointer: PointerFile


@dataclass(frozen=True)
class PointerFixture:
    """The deployment pointers are signed for, and the namespaces the key owns."""

    chain_id: int
    publisher_registry: str
    namespace: int
    namespaces: list[int]
    private_key: str

    @property
    def domain(self) -> PointerDomain:
        return PointerDomain(chain_id=self.chain_id, publisher_registry=self.publisher_registry)


def _index(flavour: Driver) -> int:
    return FLAVOURS.index(flavour.name)


@dataclass
class Contract:
    """A gateway, real or fake, and the content it serves."""

    real: bool
    mode: Mode
    gateway_url: str
    connect: str
    token: str | None
    host: str | None
    capability: str | None
    capabilities: list[tuple[str, str]]
    bundle: str
    files: dict[str, bytes]
    blob: str
    blob_bytes: bytes
    pointer: PointerFixture | None
    _close: Callable[[], None] | None = field(default=None, repr=False)

    def entry(self, flavour: Driver) -> tuple[str, str]:
        """The `(host, capability)` this flavour registers (loopback)."""
        return self.capabilities[_index(flavour)]

    def namespace(self, flavour: Driver) -> int:
        """The namespace this flavour points (public)."""
        if self.pointer is None:
            raise RuntimeError("the fixture has no pointer")
        return self.pointer.namespaces[_index(flavour)]

    def transport(self, flavour: Driver) -> HostTransport | AsyncHostTransport:
        """A transport of the flavour's kind that connects to the listener."""
        return HostTransport(self.connect) if flavour.name == "sync" else AsyncHostTransport(self.connect)

    async def open(self, flavour: Driver, **extra: Any) -> Driver:
        """A client of the flavour on the gateway with the fixture's credentials; `extra` overrides them.

        On loopback the client's host is the flavour's capability host.
        """
        kw: dict[str, Any] = {"gateway_url": self.gateway_url, "transport": self.transport(flavour)}
        if self.token is not None:
            kw["token"] = self.token
        host = self.entry(flavour)[0] if self.capabilities else self.host
        if host is not None:
            kw["host"] = host
        if self.pointer is not None:
            kw["pointer"] = self.pointer.domain
        kw.update(extra)
        return await flavour.open(**kw)

    def http(self, flavour: Driver) -> httpx.Client | httpx.AsyncClient:
        """A raw HTTP client of the flavour's kind on the listener; it follows no redirect."""
        if flavour.name == "sync":
            return httpx.Client(transport=HostTransport(self.connect), follow_redirects=False)
        return httpx.AsyncClient(transport=AsyncHostTransport(self.connect), follow_redirects=False)

    async def fetch(
        self, flavour: Driver, url: str, *, method: str = "GET", headers: dict[str, str] | None = None
    ) -> httpx.Response:
        """Send one raw request through `http(flavour)` and return the answer, read."""
        http = self.http(flavour)
        if isinstance(http, httpx.Client):
            with http:
                return http.request(method, url, headers=headers)
        async with http:
            return await http.request(method, url, headers=headers)

    def close(self) -> None:
        """Stop the fake's listener; a real gateway is the harness's to stop."""
        if self._close is not None:
            self._close()


def _bytes(length: int, seed: int) -> bytes:
    """Deterministic bytes, distinct per seed (xorshift32)."""
    out = bytearray(length)
    x = seed & 0xFFFFFFFF
    for i in range(length):
        x ^= (x << 13) & 0xFFFFFFFF
        x ^= x >> 17
        x ^= (x << 5) & 0xFFFFFFFF
        out[i] = x & 0xFF
    return bytes(out)


def fake(mode: Mode) -> tuple[FixtureFile, FakeGateway, Callable[[], None]]:
    """A fake gateway over HTTP and the fixture file that describes it, with its listener's `close`."""
    gw = FakeGateway(mode, host="decdn.test" if mode == "public" else "127.0.0.1:8484")
    blob_bytes = _bytes(64 * 1024, 0x0B10B)
    blob = gw.add_blob(blob_bytes)
    files: dict[str, bytes | str] = {
        "a.txt": b"hello from the sdk contract\n",
        "index.html": f'<a href="decdn:{blob}">blob</a>\n'.encode(),
        "data.bin": _bytes(256 * 1024, 0xDA7A),
    }
    manifest = gw.add_bundle(files)
    listener = gw.listen()
    file: FixtureFile = {
        "mode": mode,
        "gatewayUrl": gw.url,
        "connect": listener.address,
        "bundle": manifest.hash,
        "files": {k: (v.encode() if isinstance(v, str) else v).hex() for k, v in files.items()},
        "blob": blob,
        "blobBytes": blob_bytes.hex(),
    }
    if mode == "public":
        p = fixture_json("pointer")
        gw.pointer_domain = PointerDomain(chain_id=p["chainId"], publisher_registry=p["publisherRegistry"])
        gw.owners[7] = p["address"].lower()
        gw.owners[8] = p["address"].lower()
        file["pointer"] = {
            "chainId": str(p["chainId"]),
            "publisherRegistry": p["publisherRegistry"],
            "namespace": "7",
            "namespaces": ["7", "8"],
            "privateKey": p["privateKey"],
        }
    else:
        c = fixture_json("capability")
        second = encode_capability(
            c["poolId"],
            SECOND_SIGNER,
            int(c["spendingCap"]),
            int(c["expiry"]),
            bytes.fromhex(c["ownerSignature"].removeprefix("0x")),
        )
        file["token"] = gw.token
        file["host"] = "example.com"
        file["capability"] = c["token"]
        file["capabilities"] = [
            {"host": "example.com", "capability": c["token"]},
            {"host": "example.net", "capability": second},
        ]
        for entry in file["capabilities"]:
            gw.delegates[parse_capability(entry["capability"]).signer.lower()] = None
    return file, gw, listener.close


def contract(mode: Mode) -> Contract:
    """The gateway the contract suite runs against in `mode`.

    It reads the fixture `DECDN_CONTRACT` names, or builds the fake. A real fixture in the other
    mode is a misconfiguration, and raises: the harness runs one file per mode and counts a skip as
    a failure.
    """
    path = os.environ.get("DECDN_CONTRACT", "")
    real = path != ""
    close: Callable[[], None] | None = None
    if real:
        file: FixtureFile = json.loads(Path(path).read_text())
        if file.get("mode") != mode:
            raise RuntimeError(
                f"DECDN_CONTRACT names a {file.get('mode')} fixture; this file runs the {mode} contract"
            )
    else:
        # A real run sets this, so a lost DECDN_CONTRACT fails instead of quietly testing the fake.
        if os.environ.get("DECDN_CONTRACT_REQUIRED") == "1":
            raise RuntimeError("DECDN_CONTRACT_REQUIRED=1 but DECDN_CONTRACT names no fixture")
        file, _, close = fake(mode)
    p = file.get("pointer")
    pointer = (
        None
        if p is None
        else PointerFixture(
            chain_id=int(p["chainId"]),
            publisher_registry=p["publisherRegistry"],
            namespace=int(p["namespace"]),
            namespaces=[int(n) for n in p.get("namespaces", [])],
            private_key=p["privateKey"],
        )
    )
    out = Contract(
        real=real,
        mode=mode,
        gateway_url=file["gatewayUrl"],
        connect=file["connect"],
        token=file.get("token"),
        host=file.get("host"),
        capability=file.get("capability"),
        capabilities=[(e["host"], e["capability"]) for e in file.get("capabilities", [])],
        bundle=file["bundle"],
        files={k: bytes.fromhex(v) for k, v in file["files"].items()},
        blob=file["blob"],
        blob_bytes=bytes.fromhex(file["blobBytes"]),
        pointer=pointer,
        _close=close,
    )
    need = len(FLAVOURS)
    if mode == "loopback" and len(out.capabilities) < need:
        out.close()
        raise RuntimeError(f"a loopback fixture needs {need} capabilities, one per client flavour")
    if mode == "public" and (pointer is None or len(pointer.namespaces) < need):
        out.close()
        raise RuntimeError(f"a public fixture needs a pointer with {need} namespaces, one per client flavour")
    return out
