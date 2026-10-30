from collections.abc import AsyncIterator, Iterator

import httpx
import pytest

from decdn._verify import GuardOptions, guard
from decdn.errors import DecdnError
from decdn.response import AsyncResponse, ContentRange, Response


def _raw(headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(206, headers=headers or {}, request=httpx.Request("GET", "http://h/x"))


def _it(chunks: list[bytes]) -> Iterator[bytes]:
    yield from chunks


async def _ait(chunks: list[bytes]) -> AsyncIterator[bytes]:
    for c in chunks:
        yield c


def test_fields() -> None:
    r = Response(
        _raw({"content-type": "text/plain", "content-length": "5", "content-range": "bytes 2-6/10"}),
        _it([b"abcde"]),
        True,
    )
    assert (r.status, r.url, r.content_type, r.content_length, r.verified) == (
        206,
        "http://h/x",
        "text/plain",
        5,
        True,
    )
    assert r.content_range == ContentRange(offset=2, length=5, size=10)


def test_defaults_and_override() -> None:
    r = Response(_raw({"content-length": "5x"}), _it([]), "skipped")
    assert r.content_type == "application/octet-stream"
    assert r.content_length is None and r.content_range is None
    assert Response(_raw({"content-type": "a/b"}), _it([]), False, "c/d").content_type == "c/d"


def test_read_caches_and_decodes() -> None:
    r = Response(_raw(), _it([b'{"a":', b"1}"]), True)
    assert r.read() == b'{"a":1}'
    assert r.read() == b'{"a":1}'
    assert r.json() == {"a": 1}
    assert r.text() == '{"a":1}'


def test_guard_error_propagates() -> None:
    r = Response(_raw(), guard(_it([b"ab"]), GuardOptions(length=5)), True)
    with pytest.raises(DecdnError):
        r.text()


def test_context_manager_closes() -> None:
    raw = _raw()
    with Response(raw, _it([]), True):
        pass
    assert raw.is_closed


@pytest.mark.anyio
async def test_async_response() -> None:
    raw = _raw({"content-range": "bytes 0-1/2"})
    async with AsyncResponse(raw, _ait([b"h", b"i"]), True) as r:
        assert r.content_range == ContentRange(0, 2, 2)
        assert await r.read() == b"hi"
        assert await r.text() == "hi"
    assert raw.is_closed


@pytest.mark.anyio
async def test_async_iter_and_json() -> None:
    r = AsyncResponse(_raw(), _ait([b"[1]"]), "skipped")
    assert [c async for c in r.iter_bytes()] == [b"[1]"]
    r2 = AsyncResponse(_raw(), _ait([b"[1]"]), True)
    assert await r2.json() == [1]


def _bad() -> Response:
    return Response(_raw(), guard(_it([b"ab"]), GuardOptions(hash="0" * 64)), True)


def test_read_after_hash_mismatch_raises_again() -> None:
    r = _bad()
    with pytest.raises(DecdnError) as first:
        r.read()
    with pytest.raises(DecdnError) as second:
        r.read()
    assert second.value is first.value and first.value.code == "HASH_MISMATCH"
    with pytest.raises(DecdnError) as third:
        r.iter_bytes()
    assert third.value is first.value


def test_read_after_partial_iteration_is_refused() -> None:
    r = Response(_raw(), _it([b"a", b"b"]), True)
    assert next(r.iter_bytes()) == b"a"
    with pytest.raises(DecdnError) as e:
        r.read()
    assert e.value.code == "CONFIG"
    assert str(e.value) == "the body is already being streamed by iter_bytes()"


def test_iter_after_read_is_refused() -> None:
    r = Response(_raw(), _it([b"a"]), True)
    assert r.read() == b"a"
    with pytest.raises(DecdnError) as e:
        r.iter_bytes()
    assert (e.value.code, str(e.value)) == ("CONFIG", "the body is already consumed")


def test_iter_error_is_remembered_by_read() -> None:
    r = _bad()
    with pytest.raises(DecdnError) as first:
        list(r.iter_bytes())
    with pytest.raises(DecdnError) as second:
        r.read()
    assert second.value is first.value


async def _abad() -> AsyncResponse:
    from decdn._verify import aguard

    return AsyncResponse(_raw(), aguard(_ait([b"ab"]), GuardOptions(hash="0" * 64)), True)


@pytest.mark.anyio
async def test_async_read_after_hash_mismatch_raises_again() -> None:
    r = await _abad()
    with pytest.raises(DecdnError) as first:
        await r.read()
    with pytest.raises(DecdnError) as second:
        await r.read()
    assert second.value is first.value and first.value.code == "HASH_MISMATCH"


@pytest.mark.anyio
async def test_async_read_after_partial_iteration_is_refused() -> None:
    r = AsyncResponse(_raw(), _ait([b"a", b"b"]), True)
    assert await anext(r.iter_bytes()) == b"a"
    with pytest.raises(DecdnError) as e:
        await r.read()
    assert e.value.code == "CONFIG"


@pytest.mark.anyio
async def test_async_iter_after_read_is_refused() -> None:
    r = AsyncResponse(_raw(), _ait([b"a"]), True)
    assert await r.read() == b"a"
    with pytest.raises(DecdnError) as e:
        r.iter_bytes()
    assert e.value.code == "CONFIG"


@pytest.mark.anyio
async def test_async_iter_error_is_remembered_by_read() -> None:
    r = await _abad()
    with pytest.raises(DecdnError) as first:
        [c async for c in r.iter_bytes()]
    with pytest.raises(DecdnError) as second:
        await r.read()
    assert second.value is first.value
