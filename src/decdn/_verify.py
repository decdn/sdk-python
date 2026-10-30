"""The body guard: a stream that passes the gateway's bytes through, hashes them as they go when
the request named the bytes by hash, and ends with an error instead of finishing when they do not
hash to it, when fewer bytes arrive than declared, or when more arrive than the body may hold."""

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass

import httpx

from decdn.errors import DecdnError
from decdn.hashing import create_blake3

_READ_ERRORS = (httpx.HTTPError, httpx.StreamError)


@dataclass(frozen=True)
class GuardOptions:
    """What a guarded body must satisfy."""

    # The hash the whole body must have, or `None` to skip the check.
    hash: str | None = None
    # The fewest bytes the body holds: the manifest's size, or Content-Length.
    length: int | None = None
    # The most bytes the body may hold: the manifest's size, or the window asked for.
    max: int | None = None


class _State:
    """The running count and hash of a guarded body."""

    def __init__(self, opts: GuardOptions) -> None:
        self.opts = opts
        self.hasher = create_blake3() if opts.hash is not None else None
        self.delivered = 0

    def accept(self, chunk: bytes) -> None:
        """Count `chunk`, or raise `TRUNCATED` when it runs past `max`."""
        self.delivered += len(chunk)
        if self.opts.max is not None and self.delivered > self.opts.max:
            raise DecdnError("TRUNCATED", f"the body ran past the {self.opts.max} bytes it may hold")
        if self.hasher is not None:
            self.hasher.update(chunk)

    def finish(self) -> None:
        """Raise when the body ended short or hashes to something else."""
        length = self.opts.length
        if length is not None and self.delivered < length:
            raise DecdnError("TRUNCATED", f"the body ended after {self.delivered} of {length} bytes")
        if self.hasher is not None and self.opts.hash is not None:
            got = self.hasher.hex()
            if got != self.opts.hash:
                raise DecdnError("HASH_MISMATCH", f"the body hashes to {got}, not {self.opts.hash}")


def guard(chunks: Iterator[bytes], opts: GuardOptions) -> Iterator[bytes]:
    """Yield `chunks`, guarded.

    A failed read is `TRUNCATED`, a short or long body `TRUNCATED`, a wrong hash `HASH_MISMATCH`.
    """
    state = _State(opts)
    try:
        while True:
            try:
                chunk = next(chunks)
            except StopIteration:
                break
            except _READ_ERRORS as e:
                raise DecdnError("TRUNCATED", "the body ended early") from e
            state.accept(chunk)
            yield chunk
        state.finish()
    finally:
        close = getattr(chunks, "close", None)
        if close is not None:
            close()


async def aguard(chunks: AsyncIterator[bytes], opts: GuardOptions) -> AsyncIterator[bytes]:
    """Yield `chunks`, guarded: the async form of `guard`."""
    state = _State(opts)
    try:
        while True:
            try:
                chunk = await anext(chunks)
            except StopAsyncIteration:
                break
            except _READ_ERRORS as e:
                raise DecdnError("TRUNCATED", "the body ended early") from e
            state.accept(chunk)
            yield chunk
        state.finish()
    finally:
        aclose = getattr(chunks, "aclose", None)
        if aclose is not None:
            await aclose()


def collect(chunks: Iterator[bytes], limit: int | None = None) -> bytes:
    """Return every byte of `chunks`, joined."""
    parts: list[bytes] = []
    total = 0
    for chunk in chunks:
        total += len(chunk)
        if limit is not None and total > limit:
            raise DecdnError("MALFORMED_MANIFEST", f"the body is larger than {limit} bytes")
        parts.append(chunk)
    return b"".join(parts)


async def acollect(chunks: AsyncIterator[bytes], limit: int | None = None) -> bytes:
    """Return every byte of `chunks`, joined: the async form of `collect`."""
    parts: list[bytes] = []
    total = 0
    async for chunk in chunks:
        total += len(chunk)
        if limit is not None and total > limit:
            raise DecdnError("MALFORMED_MANIFEST", f"the body is larger than {limit} bytes")
        parts.append(chunk)
    return b"".join(parts)
