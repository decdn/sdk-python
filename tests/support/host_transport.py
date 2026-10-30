"""Transports that send every request to one listener with the URL's host as `Host`, the way a TLS
proxy in front of a gateway does.

Nothing resolves `decdn.test` or a site host under it, so the contract suite connects to the
listener's `host:port` whatever the URL names. httpx sets `Host` from the URL when it builds a
request (and again for each redirect it follows), so the header still names the original host once
the URL is rewritten.
"""

import httpx


def _connect(connect: str) -> tuple[str, int]:
    host, _, port = connect.rpartition(":")
    return (host or "127.0.0.1", int(port) if port else 80)


def _rewrite(request: httpx.Request, host: str, port: int) -> httpx.Request:
    """A copy of `request` addressed to `host:port` over plain HTTP, with the original `Host`."""
    return httpx.Request(
        request.method,
        request.url.copy_with(scheme="http", host=host, port=port),
        headers=request.headers,
        stream=request.stream,
        extensions=request.extensions,
    )


class HostTransport(httpx.HTTPTransport):
    """An `httpx.HTTPTransport` that connects to `connect` (`host:port`) for every request."""

    def __init__(self, connect: str) -> None:
        super().__init__()
        self._host, self._port = _connect(connect)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return super().handle_request(_rewrite(request, self._host, self._port))


class AsyncHostTransport(httpx.AsyncHTTPTransport):
    """An `httpx.AsyncHTTPTransport` that connects to `connect` (`host:port`) for every request."""

    def __init__(self, connect: str) -> None:
        super().__init__()
        self._host, self._port = _connect(connect)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await super().handle_async_request(_rewrite(request, self._host, self._port))
