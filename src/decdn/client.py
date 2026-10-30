"""The clients: `Client` and `AsyncClient`, one of two contexts over a `decdn gateway`.

- `public`: a gateway with `--public-url`; blobs from the bare host, entries from their site
  host, every request paid by the operator.
- `loopback`: the local gateway with its token; every request on the bare host, paid by the
  capability that covers `host`.

Both clients plan with the same sans-I/O core and differ only in how they send.
"""

import asyncio
import time
from types import TracebackType
from typing import Any, Self

import httpx

from decdn._core import (
    ByteRange,
    Context,
    Core,
    HeadInfo,
    Plan,
    Resolved,
    VerifyMode,
    entry_of,
    parse,
    retry_wait,
)
from decdn._http import error_of, is_success, network_error
from decdn._mime import is_rewritten_type
from decdn._verify import GuardOptions, acollect, aguard, collect, guard
from decdn.errors import DecdnError
from decdn.manifest import MAX_MANIFEST_BYTES, BundleManifest, ManifestEntry, parse_manifest
from decdn.pointer import PointerDomain
from decdn.response import AsyncResponse, Response
from decdn.uri import DecdnUri

__all__ = [
    "AsyncClient",
    "ByteRange",
    "Client",
    "Context",
    "HeadInfo",
    "Resolved",
    "VerifyMode",
]


class Client:
    """A synchronous client over a gateway.

    `token` and `host` open the loopback gateway; so does a gateway on `127.0.0.1`, `localhost`
    or `::1`. `transport` stands in for the network (an `httpx.MockTransport` in tests).
    """

    def __init__(
        self,
        gateway_url: str,
        *,
        token: str | None = None,
        host: str | None = None,
        pointer: PointerDomain | None = None,
        transport: httpx.BaseTransport | None = None,
        verify: VerifyMode = "auto",
        manifest_cache_size: int = 64,
        timeout: float | httpx.Timeout | None = 30.0,
    ) -> None:
        self._core = Core(
            gateway_url,
            token=token,
            host=host,
            pointer=pointer,
            verify=verify,
            manifest_cache_size=manifest_cache_size,
        )
        self.context: Context = self._core.context
        # The bare origin: the gateway URL's.
        self.origin: str = self._core.origin
        self.pointer_domain: PointerDomain | None = pointer
        # Namespace pointers (public gateway).
        self.pointer: Any = None
        # The delegate and capability API (loopback gateway).
        self.loopback: Any = None
        self._http = httpx.Client(transport=transport, timeout=timeout, follow_redirects=True)

    def auth_headers(self) -> dict[str, str]:
        """Return the headers every bare-host request carries."""
        return self._core.auth_headers()

    def _send(self, url: str, method: str, headers: dict[str, str]) -> httpx.Response:
        """Send a streamed request; a non-2xx answer is raised as a `DecdnError`."""
        sent = {**self._core.auth_headers(), **headers} if self._core.is_bare(url) else headers
        request = self._http.build_request(method, url, headers=sent)
        try:
            response = self._http.send(request, stream=True)
        except httpx.HTTPError as e:
            raise network_error(url, e) from e
        if is_success(response.status_code):
            return response
        try:
            response.read()
            text = response.text
        except httpx.HTTPError:
            # The status still says what went wrong.
            text = ""
        finally:
            response.close()
        raise error_of(response.status_code, response.headers, text)

    def _fetch_retrying(self, url: str, headers: dict[str, str], retry: bool | float) -> httpx.Response:
        """`GET` `url`, once more after one `Retry-After` when `retry` asks."""
        try:
            return self._send(url, "GET", headers)
        except DecdnError as e:
            wait = retry_wait(e, retry)
            if wait is None:
                raise
            time.sleep(wait)
        return self._send(url, "GET", headers)

    def site_url(self, uri: str | DecdnUri) -> str:
        """Return the site URL of a bundle or an entry."""
        return self._core.site_url(uri)

    def namespace_url(self, namespace: int) -> str:
        """Return the host a namespace's pointer serves at on a public gateway: `https://<ns>.<host>/`."""
        return self._core.namespace_url(namespace)

    def manifest(self, hash_or_uri: str | DecdnUri | None = None) -> BundleManifest:
        """Return the bundle manifest a hash or URI names, parsed and verified; cached by hash."""
        hash_ = self._core.manifest_hash(hash_or_uri)
        cached = self._core.cache_get(hash_)
        if cached is not None:
            return cached
        raw = self._send(self._core.bare_url(DecdnUri(hash=hash_, kind="blob")), "GET", {})
        try:
            data = collect(guard(raw.iter_bytes(), GuardOptions()), MAX_MANIFEST_BYTES)
        finally:
            raw.close()
        manifest = parse_manifest(data, hash_)
        self._core.cache_put(hash_, manifest)
        return manifest

    def _entry_of(self, uri: DecdnUri) -> ManifestEntry:
        return entry_of(self.manifest(uri.hash), uri)

    def resolve(self, uri: str | DecdnUri) -> Resolved:
        """Return what `uri` names: for a bundle or an entry, after reading its manifest."""
        u = parse(uri)
        bare_url = self._core.bare_url(u)
        if u.kind == "blob":
            return Resolved(kind=u.kind, hash=u.hash, bare_url=bare_url)
        site_url = self._core.site_url(u)
        if u.kind == "bundle":
            self.manifest(u.hash)
            return Resolved(kind=u.kind, hash=u.hash, bare_url=bare_url, site_url=site_url)
        entry = self._entry_of(u)
        return Resolved(kind=u.kind, hash=u.hash, bare_url=bare_url, entry=entry, site_url=site_url)

    def _plan(self, uri: DecdnUri, mode: VerifyMode, ranged: bool) -> Plan:
        entry = self._entry_of(uri) if self._core.needs_entry(uri, mode, ranged) else None
        return self._core.plan(uri, mode, ranged, entry)

    def get(
        self,
        uri: str | DecdnUri,
        *,
        range: ByteRange | None = None,
        verify: VerifyMode | None = None,
        retry: bool | float = False,
    ) -> Response:
        """Fetch what `uri` names. Verification follows `verify` (see `Response.verified`).

        `retry` waits out one `Retry-After` on 402, 429 or 503 and tries once more; a number caps
        the wait in seconds.
        """
        mode = verify if verify is not None else self._core.verify
        u = parse(uri)
        plan = self._plan(u, mode, range is not None)
        headers = {} if range is None else {"range": self._core.range_header(range)}
        raw = self._fetch_retrying(plan.url, headers, retry)
        # A site rewrites what it serves as HTML or CSS. `rewritten` picks those entries out by
        # name up front; any other the site answers with such a type comes by its hash, keeping
        # the site's type.
        content_type: str | None = None
        if plan.blob_url is not None and is_rewritten_type(raw.headers.get("content-type")):
            content_type = raw.headers.get("content-type")
            raw.close()
            raw = self._fetch_retrying(plan.blob_url, headers, retry)
        verified, opts = self._core.interpret(plan, raw.status_code, raw.headers, range)
        return Response(raw, guard(raw.iter_bytes(), opts), verified, content_type)

    def head(self, uri: str | DecdnUri) -> HeadInfo:
        """Return what is known about `uri` without its bytes.

        On a public gateway it comes from `HEAD`, elsewhere (where `HEAD` would cost a pull) from
        the manifest. A public `HEAD` of a raw blob whose size the gateway does not know yet pays
        for one chunk group, the proof of its size, and is refused or fails as its `GET` would be.
        """
        u = parse(uri)
        if self.context == "public":
            url = self._core.bare_url(u) if u.kind == "blob" else self._core.site_url(u)
            raw = self._send(url, "HEAD", {})
            raw.close()
            return self._core.head_from_headers(raw.headers)
        if u.kind != "entry":
            return HeadInfo(hash=u.hash)
        return self._core.head_from_entry(self._entry_of(u))

    def close(self) -> None:
        """Close the connections."""
        self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


class AsyncClient:
    """An asynchronous client over a gateway: `Client`, with coroutines."""

    def __init__(
        self,
        gateway_url: str,
        *,
        token: str | None = None,
        host: str | None = None,
        pointer: PointerDomain | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        verify: VerifyMode = "auto",
        manifest_cache_size: int = 64,
        timeout: float | httpx.Timeout | None = 30.0,
    ) -> None:
        self._core = Core(
            gateway_url,
            token=token,
            host=host,
            pointer=pointer,
            verify=verify,
            manifest_cache_size=manifest_cache_size,
        )
        self.context: Context = self._core.context
        # The bare origin: the gateway URL's.
        self.origin: str = self._core.origin
        self.pointer_domain: PointerDomain | None = pointer
        # Namespace pointers (public gateway).
        self.pointer: Any = None
        # The delegate and capability API (loopback gateway).
        self.loopback: Any = None
        self._http = httpx.AsyncClient(transport=transport, timeout=timeout, follow_redirects=True)

    def auth_headers(self) -> dict[str, str]:
        """Return the headers every bare-host request carries."""
        return self._core.auth_headers()

    async def _send(self, url: str, method: str, headers: dict[str, str]) -> httpx.Response:
        """Send a streamed request; a non-2xx answer is raised as a `DecdnError`."""
        sent = {**self._core.auth_headers(), **headers} if self._core.is_bare(url) else headers
        request = self._http.build_request(method, url, headers=sent)
        try:
            response = await self._http.send(request, stream=True)
        except httpx.HTTPError as e:
            raise network_error(url, e) from e
        if is_success(response.status_code):
            return response
        try:
            await response.aread()
            text = response.text
        except httpx.HTTPError:
            # The status still says what went wrong.
            text = ""
        finally:
            await response.aclose()
        raise error_of(response.status_code, response.headers, text)

    async def _fetch_retrying(self, url: str, headers: dict[str, str], retry: bool | float) -> httpx.Response:
        """`GET` `url`, once more after one `Retry-After` when `retry` asks."""
        try:
            return await self._send(url, "GET", headers)
        except DecdnError as e:
            wait = retry_wait(e, retry)
            if wait is None:
                raise
            await asyncio.sleep(wait)
        return await self._send(url, "GET", headers)

    def site_url(self, uri: str | DecdnUri) -> str:
        """Return the site URL of a bundle or an entry."""
        return self._core.site_url(uri)

    def namespace_url(self, namespace: int) -> str:
        """Return the host a namespace's pointer serves at on a public gateway: `https://<ns>.<host>/`."""
        return self._core.namespace_url(namespace)

    async def manifest(self, hash_or_uri: str | DecdnUri | None = None) -> BundleManifest:
        """Return the bundle manifest a hash or URI names, parsed and verified; cached by hash."""
        hash_ = self._core.manifest_hash(hash_or_uri)
        cached = self._core.cache_get(hash_)
        if cached is not None:
            return cached
        raw = await self._send(self._core.bare_url(DecdnUri(hash=hash_, kind="blob")), "GET", {})
        try:
            data = await acollect(aguard(raw.aiter_bytes(), GuardOptions()), MAX_MANIFEST_BYTES)
        finally:
            await raw.aclose()
        manifest = parse_manifest(data, hash_)
        self._core.cache_put(hash_, manifest)
        return manifest

    async def _entry_of(self, uri: DecdnUri) -> ManifestEntry:
        return entry_of(await self.manifest(uri.hash), uri)

    async def resolve(self, uri: str | DecdnUri) -> Resolved:
        """Return what `uri` names: for a bundle or an entry, after reading its manifest."""
        u = parse(uri)
        bare_url = self._core.bare_url(u)
        if u.kind == "blob":
            return Resolved(kind=u.kind, hash=u.hash, bare_url=bare_url)
        site_url = self._core.site_url(u)
        if u.kind == "bundle":
            await self.manifest(u.hash)
            return Resolved(kind=u.kind, hash=u.hash, bare_url=bare_url, site_url=site_url)
        entry = await self._entry_of(u)
        return Resolved(kind=u.kind, hash=u.hash, bare_url=bare_url, entry=entry, site_url=site_url)

    async def _plan(self, uri: DecdnUri, mode: VerifyMode, ranged: bool) -> Plan:
        entry = await self._entry_of(uri) if self._core.needs_entry(uri, mode, ranged) else None
        return self._core.plan(uri, mode, ranged, entry)

    async def get(
        self,
        uri: str | DecdnUri,
        *,
        range: ByteRange | None = None,
        verify: VerifyMode | None = None,
        retry: bool | float = False,
    ) -> AsyncResponse:
        """Fetch what `uri` names. Verification follows `verify` (see `AsyncResponse.verified`).

        `retry` waits out one `Retry-After` on 402, 429 or 503 and tries once more; a number caps
        the wait in seconds.
        """
        mode = verify if verify is not None else self._core.verify
        u = parse(uri)
        plan = await self._plan(u, mode, range is not None)
        headers = {} if range is None else {"range": self._core.range_header(range)}
        raw = await self._fetch_retrying(plan.url, headers, retry)
        # A site rewrites what it serves as HTML or CSS. `rewritten` picks those entries out by
        # name up front; any other the site answers with such a type comes by its hash, keeping
        # the site's type.
        content_type: str | None = None
        if plan.blob_url is not None and is_rewritten_type(raw.headers.get("content-type")):
            content_type = raw.headers.get("content-type")
            await raw.aclose()
            raw = await self._fetch_retrying(plan.blob_url, headers, retry)
        verified, opts = self._core.interpret(plan, raw.status_code, raw.headers, range)
        return AsyncResponse(raw, aguard(raw.aiter_bytes(), opts), verified, content_type)

    async def head(self, uri: str | DecdnUri) -> HeadInfo:
        """Return what is known about `uri` without its bytes: `Client.head`, as a coroutine."""
        u = parse(uri)
        if self.context == "public":
            url = self._core.bare_url(u) if u.kind == "blob" else self._core.site_url(u)
            raw = await self._send(url, "HEAD", {})
            await raw.aclose()
            return self._core.head_from_headers(raw.headers)
        if u.kind != "entry":
            return HeadInfo(hash=u.hash)
        return self._core.head_from_entry(await self._entry_of(u))

    async def close(self) -> None:
        """Close the connections."""
        await self._http.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()
