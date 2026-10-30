from collections.abc import AsyncIterator, Iterator

import httpx
import pytest

from decdn._verify import GuardOptions, acollect, aguard, collect, guard
from decdn.errors import DecdnError
from decdn.hashing import blake3_hex

DATA = b"hello world" * 10
CHUNKS = [DATA[:7], DATA[7:50], DATA[50:]]
HASH = blake3_hex(DATA)


def _it(chunks: list[bytes], fail: Exception | None = None) -> Iterator[bytes]:
    yield from chunks
    if fail is not None:
        raise fail


async def _ait(chunks: list[bytes], fail: Exception | None = None) -> AsyncIterator[bytes]:
    for c in chunks:
        yield c
    if fail is not None:
        raise fail


def test_guard_passes_matching_hash() -> None:
    out = b"".join(guard(_it(CHUNKS), GuardOptions(hash=HASH, length=len(DATA), max=len(DATA))))
    assert out == DATA


def test_guard_hash_mismatch_at_end() -> None:
    got: list[bytes] = []
    with pytest.raises(DecdnError) as e:
        for c in guard(_it(CHUNKS), GuardOptions(hash="0" * 64)):
            got.append(c)
    assert e.value.code == "HASH_MISMATCH"
    assert str(e.value) == f"the body hashes to {HASH}, not {'0' * 64}"
    assert b"".join(got) == DATA


def test_guard_short_body() -> None:
    with pytest.raises(DecdnError) as e:
        list(guard(_it(CHUNKS), GuardOptions(length=len(DATA) + 1)))
    assert e.value.code == "TRUNCATED"
    assert str(e.value) == f"the body ended after {len(DATA)} of {len(DATA) + 1} bytes"


def test_guard_long_body() -> None:
    got: list[bytes] = []
    with pytest.raises(DecdnError) as e:
        for c in guard(_it(CHUNKS), GuardOptions(max=50)):
            got.append(c)
    assert e.value.code == "TRUNCATED"
    assert str(e.value) == "the body ran past the 50 bytes it may hold"
    assert got == CHUNKS[:2]


def test_guard_wraps_read_error_as_truncated() -> None:
    cause = httpx.ReadError("reset")
    with pytest.raises(DecdnError) as e:
        list(guard(_it(CHUNKS[:1], cause), GuardOptions()))
    assert e.value.code == "TRUNCATED"
    assert str(e.value) == "the body ended early"
    assert e.value.__cause__ is cause


def test_collect_limit() -> None:
    assert collect(_it(CHUNKS)) == DATA
    assert collect(_it(CHUNKS), len(DATA)) == DATA
    with pytest.raises(DecdnError) as e:
        collect(_it(CHUNKS), 10)
    assert e.value.code == "MALFORMED_MANIFEST"
    assert str(e.value) == "the body is larger than 10 bytes"


@pytest.mark.anyio
async def test_aguard_passes_matching_hash() -> None:
    out = b"".join([c async for c in aguard(_ait(CHUNKS), GuardOptions(hash=HASH, length=len(DATA)))])
    assert out == DATA


@pytest.mark.anyio
async def test_aguard_hash_mismatch_at_end() -> None:
    with pytest.raises(DecdnError) as e:
        [c async for c in aguard(_ait(CHUNKS), GuardOptions(hash="0" * 64))]
    assert e.value.code == "HASH_MISMATCH"


@pytest.mark.anyio
async def test_aguard_short_body() -> None:
    with pytest.raises(DecdnError) as e:
        [c async for c in aguard(_ait(CHUNKS), GuardOptions(length=len(DATA) + 1))]
    assert e.value.code == "TRUNCATED"


@pytest.mark.anyio
async def test_aguard_long_body() -> None:
    got: list[bytes] = []
    with pytest.raises(DecdnError) as e:
        async for c in aguard(_ait(CHUNKS), GuardOptions(max=50)):
            got.append(c)
    assert e.value.code == "TRUNCATED"
    assert got == CHUNKS[:2]


@pytest.mark.anyio
async def test_aguard_wraps_read_error_as_truncated() -> None:
    cause = httpx.ReadError("reset")
    with pytest.raises(DecdnError) as e:
        [c async for c in aguard(_ait(CHUNKS[:1], cause), GuardOptions())]
    assert e.value.code == "TRUNCATED"
    assert e.value.__cause__ is cause


@pytest.mark.anyio
async def test_acollect_limit() -> None:
    assert await acollect(_ait(CHUNKS)) == DATA
    with pytest.raises(DecdnError) as e:
        await acollect(_ait(CHUNKS), 10)
    assert e.value.code == "MALFORMED_MANIFEST"
