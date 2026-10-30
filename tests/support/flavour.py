"""One test body for both clients: a `Driver` drives a `Client` or an `AsyncClient` through the same
`async` calls, so a test parametrised over the `flavour` fixture runs once per client."""

from collections.abc import Awaitable
from typing import Any, Literal

import pytest

from decdn.client import AsyncClient, ByteRange, Client, Context, HeadInfo, Resolved, VerifyMode
from decdn.errors import DecdnError, ErrorCode
from decdn.manifest import BundleManifest
from decdn.response import AsyncResponse, Response
from decdn.uri import DecdnUri
from tests.fake_gateway import FakeGateway

Flavour = Literal["sync", "async"]
FLAVOURS: tuple[Flavour, ...] = ("sync", "async")


class Driver:
    """A client of one flavour, behind `async` pass-throughs.

    The fixture hands out an unopened driver; `open` returns a driver bound to a new client, and
    the fixture closes every client it opened.
    """

    def __init__(
        self,
        name: Flavour,
        client: Client | AsyncClient | None = None,
        opened: list["Driver"] | None = None,
    ) -> None:
        self.name: Flavour = name
        self._client = client
        self._opened: list[Driver] = opened if opened is not None else []

    @property
    def client(self) -> Client | AsyncClient:
        """The client this driver drives."""
        if self._client is None:
            raise RuntimeError("the driver is not open: call `open` first")
        return self._client

    @property
    def context(self) -> Context:
        return self.client.context

    @property
    def origin(self) -> str:
        return self.client.origin

    async def open(self, fake: FakeGateway | None = None, **kw: Any) -> "Driver":
        """Return a driver over a new client of this flavour.

        With `fake`, the client reaches it in-process at `fake.url`; `kw` overrides either.
        """
        if fake is not None:
            kw.setdefault("gateway_url", fake.url)
            kw.setdefault("transport", fake.transport())
        gateway_url: str = kw.pop("gateway_url")
        client: Client | AsyncClient = (
            Client(gateway_url, **kw) if self.name == "sync" else AsyncClient(gateway_url, **kw)
        )
        driver = Driver(self.name, client, self._opened)
        self._opened.append(driver)
        return driver

    async def get(
        self,
        uri: str | DecdnUri,
        *,
        range: ByteRange | None = None,
        verify: VerifyMode | None = None,
        retry: bool | float = False,
    ) -> Response | AsyncResponse:
        c = self.client
        if isinstance(c, Client):
            return c.get(uri, range=range, verify=verify, retry=retry)
        return await c.get(uri, range=range, verify=verify, retry=retry)

    async def head(self, uri: str | DecdnUri) -> HeadInfo:
        c = self.client
        if isinstance(c, Client):
            return c.head(uri)
        return await c.head(uri)

    async def manifest(self, hash_or_uri: str | DecdnUri | None = None) -> BundleManifest:
        c = self.client
        if isinstance(c, Client):
            return c.manifest(hash_or_uri)
        return await c.manifest(hash_or_uri)

    async def resolve(self, uri: str | DecdnUri) -> Resolved:
        c = self.client
        if isinstance(c, Client):
            return c.resolve(uri)
        return await c.resolve(uri)

    async def site_url(self, uri: str | DecdnUri) -> str:
        return self.client.site_url(uri)

    async def namespace_url(self, namespace: int) -> str:
        return self.client.namespace_url(namespace)

    async def read(self, response: Response | AsyncResponse) -> bytes:
        if isinstance(response, Response):
            return response.read()
        return await response.read()

    async def text(self, response: Response | AsyncResponse) -> str:
        if isinstance(response, Response):
            return response.text()
        return await response.text()

    async def iter(self, response: Response | AsyncResponse) -> list[bytes]:
        """Return the body's chunks, streamed with `iter_bytes`."""
        if isinstance(response, Response):
            return list(response.iter_bytes())
        return [chunk async for chunk in response.iter_bytes()]

    async def loopback_delegate(self) -> Any:
        c = self.client
        if isinstance(c, Client):
            return c.loopback.delegate()
        return await c.loopback.delegate()

    async def loopback_register(self, **kw: Any) -> Any:
        c = self.client
        if isinstance(c, Client):
            return c.loopback.register_capability(**kw)
        return await c.loopback.register_capability(**kw)

    async def loopback_capabilities(self) -> Any:
        c = self.client
        if isinstance(c, Client):
            return c.loopback.capabilities()
        return await c.loopback.capabilities()

    async def pointer_set(self, **kw: Any) -> Any:
        c = self.client
        if isinstance(c, Client):
            return c.pointer.set(**kw)
        return await c.pointer.set(**kw)

    async def close(self) -> None:
        c = self._client
        if c is None:
            return
        if isinstance(c, Client):
            c.close()
        else:
            await c.close()

    async def close_all(self) -> None:
        """Close every client opened through this driver's fixture."""
        for driver in self._opened:
            await driver.close()
        self._opened.clear()


async def rejects(awaitable: Awaitable[object], code: ErrorCode) -> DecdnError:
    """Await `awaitable`, which must raise a `DecdnError` with `code`; return the error."""
    with pytest.raises(DecdnError) as info:
        await awaitable
    assert info.value.code == code, str(info.value)
    return info.value
