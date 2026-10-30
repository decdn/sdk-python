"""The gateway's answer: a stream that ends with an error rather than finishing when its bytes
fail verification."""

import json
import re
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Literal, Self

import httpx

from decdn._verify import acollect, collect

_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+)")
_DIGITS = re.compile(r"\d+")


@dataclass(frozen=True)
class ContentRange:
    """The window of an entry a ranged answer holds."""

    offset: int
    length: int
    size: int


class _Head:
    """The answer's status line and headers, as fields."""

    def __init__(
        self,
        raw: httpx.Response,
        verified: bool | Literal["skipped"],
        content_type: str | None,
    ) -> None:
        self._raw = raw
        self.status: int = raw.status_code
        self.headers: httpx.Headers = raw.headers
        self.url: str = str(raw.url)
        self.content_type: str = content_type or raw.headers.get("content-type") or "application/octet-stream"
        length = raw.headers.get("content-length")
        self.content_length: int | None = (
            int(length) if length is not None and _DIGITS.fullmatch(length) else None
        )
        self.content_range: ContentRange | None = None
        m = _RANGE.fullmatch(raw.headers.get("content-range", ""))
        if m is not None:
            a, b, size = (int(g) for g in m.groups())
            self.content_range = ContentRange(offset=a, length=b - a + 1, size=size)
        # `True`: the body is hashed as it streams, and ends with `HASH_MISMATCH` instead of
        # finishing unless it hashes to the hash that names it. `"skipped"`: the bytes cannot be
        # verified (a range, a rewritten page, a bundle listing). `False`: verification was
        # turned off.
        self.verified: bool | Literal["skipped"] = verified


class Response(_Head):
    """The gateway's answer, streamed synchronously."""

    def __init__(
        self,
        raw: httpx.Response,
        body: Iterator[bytes],
        verified: bool | Literal["skipped"],
        content_type: str | None = None,
    ) -> None:
        super().__init__(raw, verified, content_type)
        self._body = body
        self._cache: bytes | None = None

    def iter_bytes(self) -> Iterator[bytes]:
        """Return the body's chunks. Raises `HASH_MISMATCH` or `TRUNCATED` at the end."""
        return self._body

    def read(self) -> bytes:
        """Return the whole body, drained once and cached. Raises `HASH_MISMATCH` or `TRUNCATED`."""
        if self._cache is None:
            self._cache = collect(self._body)
        return self._cache

    def text(self) -> str:
        """Return the body decoded as UTF-8."""
        return self.read().decode("utf-8")

    def json(self) -> Any:
        """Return the body parsed as JSON."""
        return json.loads(self.text())

    def close(self) -> None:
        """Release the connection."""
        self._raw.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


class AsyncResponse(_Head):
    """The gateway's answer, streamed asynchronously."""

    def __init__(
        self,
        raw: httpx.Response,
        body: AsyncIterator[bytes],
        verified: bool | Literal["skipped"],
        content_type: str | None = None,
    ) -> None:
        super().__init__(raw, verified, content_type)
        self._body = body
        self._cache: bytes | None = None

    def iter_bytes(self) -> AsyncIterator[bytes]:
        """Return the body's chunks. Raises `HASH_MISMATCH` or `TRUNCATED` at the end."""
        return self._body

    async def read(self) -> bytes:
        """Return the whole body, drained once and cached. Raises `HASH_MISMATCH` or `TRUNCATED`."""
        if self._cache is None:
            self._cache = await acollect(self._body)
        return self._cache

    async def text(self) -> str:
        """Return the body decoded as UTF-8."""
        return (await self.read()).decode("utf-8")

    async def json(self) -> Any:
        """Return the body parsed as JSON."""
        return json.loads(await self.text())

    async def close(self) -> None:
        """Release the connection."""
        await self._raw.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()
