"""The clients over the fake gateway, each test run once per flavour (`Client`, `AsyncClient`)."""

import asyncio
import gzip
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

from decdn.base32 import hash_to_base32
from decdn.client import AsyncClient, ByteRange, HeadInfo, Resolved
from decdn.errors import DecdnError
from decdn.manifest import BundleManifest, ManifestEntry
from decdn.response import ContentRange
from decdn.uri import DecdnUri
from tests.fake_gateway import FakeGateway
from tests.support.flavour import Driver, rejects

pytestmark = pytest.mark.anyio

TOKEN = "cd" * 32


def public_fake(allowance_bytes: int | None = None) -> tuple[FakeGateway, BundleManifest, str]:
    fake = FakeGateway("public", allowance_bytes=allowance_bytes)
    bundle = fake.add_bundle(
        {
            "a.txt": "abc",
            "index.html": "<p>hi decdn:" + "ee" * 32 + "</p>",
            "css/site.css": "a { color: red }",
            "dir one/b.bin": bytes([1, 2, 3, 4, 5, 6, 7, 8]),
            "unsized.txt": "no size",
        },
        ["unsized.txt"],
    )
    blob = fake.add_blob("other bytes")
    return fake, bundle, blob


def entry_of(bundle: BundleManifest, path: str) -> ManifestEntry:
    entry = bundle.entry(path)
    assert entry is not None
    return entry


def paths(fake: FakeGateway) -> list[str]:
    out: list[str] = []
    for s in fake.seen:
        u = httpx.URL(s.url)
        host = u.host if u.port is None else f"{u.host}:{u.port}"
        out.append(f"{s.method} {host}{u.raw_path.decode('ascii')}")
    return out


def via(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
    """A transport answering from `handler`, for either client flavour."""
    return httpx.MockTransport(handler)


def body(status: int, data: bytes, headers: dict[str, str] | httpx.Headers) -> httpx.Response:
    """A streamed answer with exactly `headers`."""
    return httpx.Response(status, headers=headers, stream=httpx.ByteStream(data))


def served_as(fake: FakeGateway, type_: str) -> httpx.MockTransport:
    """A transport that serves the site URL of `a.txt` as `type_`, rewritten."""

    def handle(request: httpx.Request) -> httpx.Response:
        r = fake.handle(request)
        if request.url.path != "/a.txt":
            return r
        return body(200, b"rewritten", {"content-type": type_})

    return via(handle)


def on_sleep(
    monkeypatch: pytest.MonkeyPatch, flavour: Driver, then: Callable[[float], None] = lambda _: None
) -> list[float]:
    """Make the flavour's sleep return at once, calling `then`; return the waits asked for."""
    waits: list[float] = []
    if flavour.name == "sync":

        def sleep(secs: float) -> None:
            waits.append(secs)
            then(secs)

        monkeypatch.setattr(time, "sleep", sleep)
    else:
        original = asyncio.sleep

        async def asleep(secs: float, result: Any = None) -> Any:
            # The event loop's own zero-second yields pass through.
            if secs == 0:
                return await original(0, result)
            waits.append(secs)
            then(secs)
            return result

        monkeypatch.setattr(asyncio, "sleep", asleep)
    return waits


# DecdnClient.open


async def test_picks_the_context_from_the_gateway_url_and_token(flavour: Driver) -> None:
    fake, _, _ = public_fake()
    assert (await flavour.open(fake)).context == "public"
    loop = FakeGateway("loopback")
    c = await flavour.open(gateway_url="http://127.0.0.1:8484", transport=loop.transport())
    assert c.context == "loopback"
    c = await flavour.open(gateway_url="https://gw.example", token=TOKEN, host="example.com")
    assert c.context == "loopback"


async def test_refuses_a_token_without_a_host_and_no_gateway_outside_a_site_page(flavour: Driver) -> None:
    await rejects(flavour.open(gateway_url="http://127.0.0.1:8484", token=TOKEN), "CONFIG")
    await rejects(flavour.open(gateway_url=""), "CONFIG")
    e = await rejects(flavour.open(gateway_url="not a url"), "CONFIG")
    assert str(e) == "not a gateway URL: not a url"


# public context


async def test_streams_a_blob_from_the_bare_host_and_verifies_it(flavour: Driver) -> None:
    fake, _, blob = public_fake()
    c = await flavour.open(fake)
    r = await c.get(f"decdn:{blob}")
    assert r.status == 200
    assert r.verified is True
    assert r.content_type == "application/octet-stream"
    assert r.content_length == 11
    assert await c.text(r) == "other bytes"
    assert paths(fake) == [f"GET decdn.test/{blob}"]


async def test_fails_a_tampered_blob_with_hash_mismatch_as_a_stream_error_and_on_bytes(
    flavour: Driver,
) -> None:
    fake, _, blob = public_fake()
    fake.tampered.add(blob)
    c = await flavour.open(fake)
    await rejects(c.read(await c.get(f"decdn:{blob}")), "HASH_MISMATCH")
    await rejects(c.iter(await c.get(f"decdn:{blob}")), "HASH_MISMATCH")


async def test_still_hashes_a_whole_body_request_the_gateway_answers_with_a_window(flavour: Driver) -> None:
    fake, bundle, blob = public_fake()
    fake.partial.add(blob)
    fake.partial.add(entry_of(bundle, "a.txt").hash)
    fake.partial.add(entry_of(bundle, "unsized.txt").hash)
    c = await flavour.open(fake)
    u = await c.get(f"decdn:{bundle.hash}/unsized.txt")
    assert u.verified is True
    await rejects(c.read(u), "HASH_MISMATCH")
    for uri in (f"decdn:{blob}", f"decdn:{bundle.hash}/a.txt"):
        r = await c.get(uri)
        assert r.verified is True
        await rejects(c.read(r), "TRUNCATED")


async def test_fails_a_body_the_gateway_cuts_with_truncated(flavour: Driver) -> None:
    fake, _, blob = public_fake()
    fake.truncated.add(blob)
    c = await flavour.open(fake)
    await rejects(c.read(await c.get(f"decdn:{blob}")), "TRUNCATED")
    await rejects(c.read(await c.get(f"decdn:{blob}", verify=False)), "TRUNCATED")


async def test_fetches_an_entry_through_its_site_and_verifies_it_against_the_manifest(
    flavour: Driver,
) -> None:
    fake, bundle, _ = public_fake()
    c = await flavour.open(fake)
    r = await c.get(f"decdn:{bundle.hash}/dir%20one/b.bin")
    assert r.verified is True
    assert r.content_length == 8
    assert await c.read(r) == bytes([1, 2, 3, 4, 5, 6, 7, 8])
    label = hash_to_base32(bundle.hash)
    assert paths(fake) == [f"GET decdn.test/{bundle.hash}", f"GET {label}.decdn.test/dir%20one/b.bin"]


async def test_fetches_a_page_or_a_stylesheet_by_its_entry_hash_unrewritten(flavour: Driver) -> None:
    fake, bundle, _ = public_fake()
    c = await flavour.open(fake)
    page = entry_of(bundle, "index.html")
    r = await c.get(f"decdn:{bundle.hash}/index.html")
    assert r.verified is True
    assert "decdn:" + "ee" * 32 in await c.text(r)
    assert paths(fake) == [f"GET decdn.test/{bundle.hash}", f"GET decdn.test/{page.hash}"]


async def test_fails_a_tampered_entry_with_hash_mismatch(flavour: Driver) -> None:
    fake, bundle, _ = public_fake()
    fake.tampered.add(entry_of(bundle, "a.txt").hash)
    c = await flavour.open(fake)
    await rejects(c.read(await c.get(f"decdn:{bundle.hash}/a.txt")), "HASH_MISMATCH")


async def test_skips_the_manifest_when_verification_is_off(flavour: Driver) -> None:
    fake, bundle, _ = public_fake()
    c = await flavour.open(fake, verify=False)
    r = await c.get(f"decdn:{bundle.hash}/a.txt")
    assert r.verified is False
    assert await c.text(r) == "abc"
    assert paths(fake) == [f"GET {hash_to_base32(bundle.hash)}.decdn.test/a.txt"]


async def test_answers_a_range_with_206_and_no_verification(flavour: Driver) -> None:
    fake, bundle, _ = public_fake()
    c = await flavour.open(fake)
    r = await c.get(f"decdn:{bundle.hash}/dir%20one/b.bin", range=ByteRange(offset=2, length=3))
    assert r.status == 206
    assert r.verified == "skipped"
    assert r.content_range == ContentRange(offset=2, length=3, size=8)
    assert await c.read(r) == bytes([3, 4, 5])
    assert len(fake.seen) == 1
    assert fake.seen[0].headers.get("range") == "bytes=2-4"
    await rejects(c.get(f"decdn:{bundle.hash}/a.txt", range=ByteRange(offset=1), verify=True), "CONFIG")


async def test_refuses_a_range_on_a_blob_before_any_request(flavour: Driver) -> None:
    fake, _, blob = public_fake()
    fake.partial.add(blob)
    c = await flavour.open(fake)
    for verify in ("auto", True, False):
        await rejects(c.get(f"decdn:{blob}", range=ByteRange(offset=0, length=1), verify=verify), "CONFIG")
    assert len(fake.seen) == 0


async def test_maps_a_range_past_the_end_to_range_not_satisfiable_with_the_size(flavour: Driver) -> None:
    fake, bundle, _ = public_fake()
    c = await flavour.open(fake)
    e = await rejects(c.get(f"decdn:{bundle.hash}/a.txt", range=ByteRange(offset=3)), "RANGE_NOT_SATISFIABLE")
    assert e.size == 3
    assert e.status == 416


async def test_maps_a_spent_allowance_to_payment_required_with_the_fallback_uri(flavour: Driver) -> None:
    fake, _, blob = public_fake(0)
    c = await flavour.open(fake)
    e = await rejects(c.get(f"decdn:{blob}?ns=7"), "PAYMENT_REQUIRED")
    assert e.status == 402
    assert e.fallback_uri == f"decdn:{blob}?ns=7"
    assert e.retry_after_secs is not None and e.retry_after_secs > 0


async def test_maps_a_pool_that_cannot_pay_to_pool_unfunded(flavour: Driver) -> None:
    fake, _, blob = public_fake()
    fake.unfunded.add(blob)
    c = await flavour.open(fake)
    e = await rejects(c.get(f"decdn:{blob}"), "POOL_UNFUNDED")
    assert e.retry_after_secs == 60
    assert e.fallback_uri == f"decdn:{blob}"


async def test_retries_once_after_retry_after_when_asked(
    flavour: Driver, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake, _, blob = public_fake()
    fake.unfunded.add(blob)
    waits = on_sleep(monkeypatch, flavour, lambda _: fake.unfunded.discard(blob))
    c = await flavour.open(fake)
    r = await c.get(f"decdn:{blob}", retry=True)
    assert await c.text(r) == "other bytes"
    assert waits == [60]
    assert len(fake.seen) == 2


async def test_retry_cap_refuses_long_wait(flavour: Driver, monkeypatch: pytest.MonkeyPatch) -> None:
    fake, _, blob = public_fake(0)
    monkeypatch.setattr(fake, "_seconds_to_midnight", lambda: 3600)
    waits = on_sleep(monkeypatch, flavour)
    c = await flavour.open(fake)
    e = await rejects(c.get(f"decdn:{blob}", retry=1.0), "PAYMENT_REQUIRED")
    assert e.retry_after_secs == 3600
    assert waits == []
    assert len(fake.seen) == 1


async def test_a_ranged_request_answered_200_is_a_whole_body(flavour: Driver) -> None:
    fake = FakeGateway("public")
    data = bytes(range(40))
    bundle = fake.add_bundle({"big.bin": data})
    fake.whole.add(entry_of(bundle, "big.bin").hash)
    c = await flavour.open(fake)
    r = await c.get(f"decdn:{bundle.hash}/big.bin", range=ByteRange(offset=10, length=20))
    assert r.status == 200
    # A ranged plan carries no hash (it reads no manifest), so the whole body comes back unhashed.
    assert r.verified == "skipped"
    assert await c.read(r) == data


async def test_retry_never_sleeps_without_retry_after(
    flavour: Driver, monkeypatch: pytest.MonkeyPatch
) -> None:
    for retry in (True, 5.0):
        fake, _, blob = public_fake(0)

        def handle(request: httpx.Request, fake: FakeGateway = fake) -> httpx.Response:
            fake.handle(request)
            return body(402, b"pay", {})

        waits = on_sleep(monkeypatch, flavour)
        c = await flavour.open(fake, transport=via(handle))
        await rejects(c.get(f"decdn:{blob}", retry=retry), "PAYMENT_REQUIRED")
        assert waits == []
        assert len(fake.seen) == 1


async def test_float_cap_retries_when_retry_after_fits(
    flavour: Driver, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake, _, blob = public_fake()
    fake.unfunded.add(blob)
    waits = on_sleep(monkeypatch, flavour, lambda _: fake.unfunded.discard(blob))
    c = await flavour.open(fake)
    r = await c.get(f"decdn:{blob}", retry=120.0)
    assert await c.text(r) == "other bytes"
    assert waits == [60]
    assert len(fake.seen) == 2


async def test_fails_a_window_that_runs_past_its_declared_length_with_truncated(flavour: Driver) -> None:
    fake, bundle, _ = public_fake()

    def handle(request: httpx.Request) -> httpx.Response:
        if request.headers.get("range") is None:
            return fake.handle(request)
        return body(206, bytes(8), {"content-length": "2", "content-range": "bytes 0-1/8"})

    c = await flavour.open(fake, transport=via(handle))
    r = await c.get(f"decdn:{bundle.hash}/dir%20one/b.bin", range=ByteRange(offset=0, length=2))
    assert r.content_length == 2
    await rejects(c.read(r), "TRUNCATED")


async def test_fails_a_whole_entry_that_runs_past_its_manifest_size_with_truncated(flavour: Driver) -> None:
    fake, bundle, _ = public_fake()

    def handle(request: httpx.Request) -> httpx.Response:
        r = fake.handle(request)
        if not str(request.url).endswith("/a.txt"):
            return r
        return body(200, b"abcdef", r.headers)

    c = await flavour.open(fake, transport=via(handle))
    r = await c.get(f"decdn:{bundle.hash}/a.txt")
    assert r.verified is True
    e = await rejects(c.read(r), "TRUNCATED")
    assert "3 bytes" in str(e)


async def test_takes_a_body_longer_than_its_content_length_as_one_decoded_on_the_way(
    flavour: Driver,
) -> None:
    _, _, blob = public_fake()
    decoded = b"decoded, longer than sent " * 20
    sent = gzip.compress(decoded)
    assert len(sent) < len(decoded)

    def handle(request: httpx.Request) -> httpx.Response:
        return body(200, sent, {"content-length": str(len(sent)), "content-encoding": "gzip"})

    c = await flavour.open(gateway_url="http://decdn.test", transport=via(handle))
    r = await c.get(f"decdn:{blob}", verify=False)
    assert await c.read(r) == decoded


@pytest.mark.parametrize("type_", ["text/html; charset=utf-8", "TEXT/CSS"])
async def test_fetches_an_entry_by_its_hash_when_its_site_serves_it_as(flavour: Driver, type_: str) -> None:
    fake, bundle, _ = public_fake()
    c = await flavour.open(fake, transport=served_as(fake, type_))
    r = await c.get(f"decdn:{bundle.hash}/a.txt?ns=9")
    assert r.verified is True
    assert r.content_type == type_
    assert await c.text(r) == "abc"
    entry = entry_of(bundle, "a.txt")
    assert paths(fake) == [
        f"GET decdn.test/{bundle.hash}",
        f"GET 9.{hash_to_base32(bundle.hash)}.decdn.test/a.txt",
        f"GET decdn.test/{entry.hash}?ns=9",
    ]


class _Unread(httpx.SyncByteStream, httpx.AsyncByteStream):
    """A page body that fails the test if anything reads it."""

    def __iter__(self) -> Iterator[bytes]:
        raise AssertionError("the page was read")

    async def __aiter__(self) -> Any:
        raise AssertionError("the page was read")
        yield b""  # pragma: no cover


async def test_fetches_by_hash_without_waiting_on_a_page_whose_clone_is_kept_unread(flavour: Driver) -> None:
    fake, bundle, _ = public_fake()

    def handle(request: httpx.Request) -> httpx.Response:
        r = fake.handle(request)
        if request.url.path != "/a.txt":
            return r
        return httpx.Response(200, headers={"content-type": "text/html"}, stream=_Unread())

    c = await flavour.open(fake, transport=via(handle))
    r = await c.get(f"decdn:{bundle.hash}/a.txt")
    assert await c.text(r) == "abc"


async def test_takes_a_rewritten_type_as_served_when_verification_is_off(flavour: Driver) -> None:
    fake, bundle, _ = public_fake()
    c = await flavour.open(fake, transport=served_as(fake, "text/html"), verify=False)
    assert await c.text(await c.get(f"decdn:{bundle.hash}/a.txt")) == "rewritten"
    assert len(fake.seen) == 1


async def test_retries_the_fetch_by_hash_after_retry_after_when_asked(
    flavour: Driver, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake, bundle, _ = public_fake()
    fake.unfunded.add(entry_of(bundle, "a.txt").hash)
    waits = on_sleep(monkeypatch, flavour, lambda _: fake.unfunded.clear())
    c = await flavour.open(fake, transport=served_as(fake, "text/html"))
    r = await c.get(f"decdn:{bundle.hash}/a.txt", retry=True)
    assert await c.text(r) == "abc"
    assert waits == [60]
    assert len(fake.seen) == 4


async def test_knows_an_entry_the_manifest_lacks_is_not_found_without_asking_for_it(flavour: Driver) -> None:
    fake, bundle, _ = public_fake()
    c = await flavour.open(fake)
    await rejects(c.get(f"decdn:{bundle.hash}/missing.txt"), "NOT_FOUND")
    assert len(fake.seen) == 1


async def test_caches_a_manifest_by_hash(flavour: Driver) -> None:
    fake, bundle, _ = public_fake()
    c = await flavour.open(fake)
    m = await c.manifest(bundle.hash)
    assert "a.txt" in [e.path for e in m.entries]
    assert await c.manifest(f"decdn:{bundle.hash}/") is m
    assert len(fake.seen) == 1


async def test_keeps_a_manifest_read_again_after_an_evicted_read_of_it_fails(flavour: Driver) -> None:
    fake, bundle, _ = public_fake()
    other = fake.add_bundle({"b.txt": "b"})
    first = True

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal first
        if first and str(request.url).endswith(f"/{bundle.hash}"):
            first = False
            return body(404, b"gone\n", {"content-type": "text/plain"})
        return fake.handle(request)

    c = await flavour.open(fake, manifest_cache_size=1, transport=via(handle))
    await rejects(c.manifest(bundle.hash), "NOT_FOUND")
    await c.manifest(other.hash)
    m = await c.manifest(bundle.hash)
    assert await c.manifest(bundle.hash) is m
    assert len([s for s in fake.seen if s.url.endswith(f"/{bundle.hash}")]) == 1


async def test_refuses_a_blob_that_is_not_a_manifest(flavour: Driver) -> None:
    fake, _, blob = public_fake()
    c = await flavour.open(fake)
    await rejects(c.manifest(blob), "MALFORMED_MANIFEST")


async def test_resolves_an_entry_to_its_hash_and_urls(flavour: Driver) -> None:
    fake, bundle, _ = public_fake()
    c = await flavour.open(fake)
    entry = entry_of(bundle, "a.txt")
    r = await c.resolve(f"decdn:{bundle.hash}/a.txt?ns=9")
    assert r == Resolved(
        kind="entry",
        hash=bundle.hash,
        entry=ManifestEntry(path="a.txt", hash=entry.hash, size=3),
        site_url=f"http://9.{hash_to_base32(bundle.hash)}.decdn.test/a.txt",
        bare_url=f"http://decdn.test/{bundle.hash}/a.txt?ns=9",
    )
    await rejects(c.resolve(f"decdn:{bundle.hash}/nope"), "NOT_FOUND")


async def test_builds_site_and_namespace_urls(flavour: Driver) -> None:
    c = await flavour.open(gateway_url="https://decdn.link")
    h = "ab" * 32
    assert await c.site_url(f"decdn:{h}/") == f"https://{hash_to_base32(h)}.decdn.link/"
    assert (
        await c.site_url(f"decdn:{h}/my%20dir/x.txt?ns=4")
        == f"https://4.{hash_to_base32(h)}.decdn.link/my%20dir/x.txt"
    )
    assert await c.namespace_url(42) == "https://42.decdn.link/"
    await rejects(c.namespace_url(0), "CONFIG")
    await rejects(c.site_url(f"decdn:{h}"), "CONFIG")


async def test_site_url_encodes_path(flavour: Driver) -> None:
    fake, _, _ = public_fake()
    # The fetched entry's own name is ASCII: the fake names it in Content-Disposition.
    bundle = fake.add_bundle({"dir one/ü.txt": "x", "dir one/ü/y.txt": "y"})
    label = hash_to_base32(bundle.hash)
    c = await flavour.open(fake)
    uri = DecdnUri(hash=bundle.hash, kind="entry", path="dir one/ü.txt")
    assert await c.site_url(uri) == f"http://{label}.decdn.test/dir%20one/%C3%BC.txt"
    assert await c.site_url(f"decdn:{bundle.hash}/dir%20one/%c3%bc.txt") == await c.site_url(uri)
    fetched = DecdnUri(hash=bundle.hash, kind="entry", path="dir one/ü/y.txt")
    assert await c.text(await c.get(fetched)) == "y"
    assert paths(fake)[-1] == f"GET {label}.decdn.test/dir%20one/%C3%BC/y.txt"


async def test_reads_an_entrys_size_and_hash_from_head(flavour: Driver) -> None:
    fake, bundle, _ = public_fake()
    c = await flavour.open(fake)
    entry = entry_of(bundle, "a.txt")
    assert await c.head(f"decdn:{bundle.hash}/a.txt") == HeadInfo(
        size=3, hash=entry.hash, content_type="text/plain; charset=utf-8"
    )
    assert [s.method for s in fake.seen] == ["HEAD"]


async def test_reads_a_raw_blobs_size_and_hash_from_head(flavour: Driver) -> None:
    fake, _, blob = public_fake()
    c = await flavour.open(fake)
    assert await c.head(f"decdn:{blob}") == HeadInfo(
        size=11, hash=blob, content_type="application/octet-stream"
    )
    assert [s.method for s in fake.seen] == ["HEAD"]


async def test_maps_a_raw_blobs_head_refusals_as_its_gets(flavour: Driver) -> None:
    fake, _, blob = public_fake(0)
    c = await flavour.open(fake)
    await rejects(c.head(f"decdn:{blob}"), "PAYMENT_REQUIRED")
    funded, _, funded_blob = public_fake()
    funded.unfunded.add(funded_blob)
    d = await flavour.open(funded)
    await rejects(d.head(f"decdn:{funded_blob}"), "POOL_UNFUNDED")


def test_pays_a_raw_blobs_proof_once_under_a_sites_decdn_too() -> None:
    fake, bundle, blob = public_fake(1 << 20)
    site = f"http://{hash_to_base32(bundle.hash)}.decdn.test"
    with httpx.Client(transport=fake.transport()) as http:
        for _ in range(2):
            r = http.head(f"{site}/_decdn/{blob}")
            assert r.status_code == 200
            assert r.headers.get("content-length") == "11"
    assert fake.allowance == (1 << 20) - 16 * 1024


async def test_answers_the_empty_blob_with_no_proof(flavour: Driver) -> None:
    fake, _, _ = public_fake(1)
    empty = fake.add_blob("")
    c = await flavour.open(fake)
    assert (await c.head(f"decdn:{empty}")).size == 0
    r = await c.get(f"decdn:{empty}")
    assert r.content_length == 0
    assert await c.read(r) == b""
    assert fake.allowance == 1


def test_admits_a_window_of_a_raw_blob_by_its_chunk_group_span() -> None:
    fake, _, _ = public_fake(32 * 1024)
    big = fake.add_blob(bytes(64 * 1024))
    with httpx.Client(transport=fake.transport()) as http:
        r = http.get(f"http://decdn.test/{big}", headers={"range": "bytes=0-0"})
    assert r.status_code == 206
    assert r.headers.get("content-range") == f"bytes 0-0/{64 * 1024}"
    assert fake.allowance == 0


async def test_serves_a_bundle_as_its_site_index_unverified(flavour: Driver) -> None:
    fake, bundle, _ = public_fake()
    c = await flavour.open(fake)
    r = await c.get(f"decdn:{bundle.hash}/")
    assert r.verified == "skipped"
    assert "/_decdn/" in await c.text(r)


async def test_maps_a_failed_fetch_to_network(flavour: Driver) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("fetch failed", request=request)

    c = await flavour.open(gateway_url="https://decdn.test", transport=via(handle))
    e = await rejects(c.get(f"decdn:{'ab' * 32}"), "NETWORK")
    assert isinstance(e.__cause__, httpx.ConnectError)


async def test_rejects_a_bad_uri_before_any_request(flavour: Driver) -> None:
    fake, _, _ = public_fake()
    c = await flavour.open(fake)
    await rejects(c.get("decdn:nope"), "BAD_URI")
    assert len(fake.seen) == 0


# loopback context


def loopback_fake() -> tuple[FakeGateway, BundleManifest, str]:
    fake = FakeGateway("loopback", token=TOKEN)
    bundle = fake.add_bundle({"a.txt": "abc", "index.html": "<p>x</p>"})
    blob = fake.add_blob("loop bytes")
    fake.scopes.append("example.com")
    return fake, bundle, blob


async def open_loop(
    flavour: Driver, fake: FakeGateway, token: str = TOKEN, host: str = "example.com", **kw: Any
) -> Driver:
    return await flavour.open(fake, token=token, host=host, **kw)


async def test_sends_the_token_and_host_and_verifies_a_blob(flavour: Driver) -> None:
    fake, _, blob = loopback_fake()
    c = await open_loop(flavour, fake)
    r = await c.get(f"decdn:{blob}")
    assert r.verified is True
    assert await c.text(r) == "loop bytes"
    assert fake.seen[0].headers.get("x-decdn-token") == TOKEN
    assert fake.seen[0].headers.get("x-decdn-host") == "example.com"


async def test_fetches_an_entry_on_the_bare_host_byte_exact_and_verifies_it(flavour: Driver) -> None:
    fake, bundle, _ = loopback_fake()
    c = await open_loop(flavour, fake)
    r = await c.get(f"decdn:{bundle.hash}/index.html")
    assert r.verified is True
    assert await c.text(r) == "<p>x</p>"
    assert paths(fake) == [
        f"GET 127.0.0.1:8484/{bundle.hash}",
        f"GET 127.0.0.1:8484/{bundle.hash}/index.html",
    ]


async def test_maps_a_wrong_token_to_unauthorized_and_an_uncovered_host_to_payment_required(
    flavour: Driver,
) -> None:
    fake, _, blob = loopback_fake()
    await rejects((await open_loop(flavour, fake, "00" * 32)).get(f"decdn:{blob}"), "UNAUTHORIZED")
    e = await rejects(
        (await open_loop(flavour, fake, TOKEN, "other.org")).get(f"decdn:{blob}"), "PAYMENT_REQUIRED"
    )
    assert e.body == "no capability covers other.org"
    assert e.fallback_uri is None


async def test_answers_head_from_the_manifest_since_a_loopback_gateway_refuses_head(flavour: Driver) -> None:
    fake, bundle, _ = loopback_fake()
    c = await open_loop(flavour, fake)
    entry = entry_of(bundle, "a.txt")
    assert await c.head(f"decdn:{bundle.hash}/a.txt") == HeadInfo(
        size=3, hash=entry.hash, content_type="text/plain; charset=utf-8"
    )
    assert [s.method for s in fake.seen] == ["GET"]


async def test_does_not_refetch_by_hash_where_the_bare_host_serves_entries_unrewritten(
    flavour: Driver,
) -> None:
    fake, bundle, _ = loopback_fake()

    def handle(request: httpx.Request) -> httpx.Response:
        r = fake.handle(request)
        if not str(request.url).endswith("/a.txt"):
            return r
        return body(200, b"abc!", {"content-type": "text/html"})

    c = await open_loop(flavour, fake, transport=via(handle))
    await rejects(c.read(await c.get(f"decdn:{bundle.hash}/a.txt")), "TRUNCATED")
    assert len(fake.seen) == 2


async def test_builds_site_urls_under_localhost_on_the_gateways_port(flavour: Driver) -> None:
    fake, bundle, _ = loopback_fake()
    c = await open_loop(flavour, fake)
    assert (
        await c.site_url(f"decdn:{bundle.hash}/a.txt")
        == f"http://{hash_to_base32(bundle.hash)}.localhost:8484/a.txt"
    )
    await rejects(c.namespace_url(1), "CONFIG")


async def test_manifest_needs_a_hash_outside_a_site_page(flavour: Driver) -> None:
    fake, _, _ = public_fake()
    c = await flavour.open(fake)
    e = await rejects(c.manifest(), "CONFIG")
    assert str(e) == "manifest needs a hash outside a site page"


# The async client's shared manifest reads


def manifest_reads(fake: FakeGateway, bundle: BundleManifest) -> int:
    return len([s for s in fake.seen if s.url.endswith(f"/{bundle.hash}")])


def yielding(fake: FakeGateway, gate: asyncio.Event | None = None) -> tuple[httpx.MockTransport, list[str]]:
    """A transport that yields to the event loop (behind `gate`, when given) before every answer,
    as a real one does; it lists every URL asked for, answered or not."""
    asked: list[str] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        await asyncio.sleep(0)
        if gate is not None:
            await gate.wait()
        return fake.handle(request)

    return httpx.MockTransport(handle), asked


async def test_async_concurrent_manifest_reads_share_one_request() -> None:
    fake, bundle, _ = public_fake()
    transport, _ = yielding(fake)
    async with AsyncClient(fake.url, transport=transport) as c:
        a, b = await asyncio.gather(
            c.get(f"decdn:{bundle.hash}/a.txt"), c.get(f"decdn:{bundle.hash}/unsized.txt")
        )
        assert await a.text() == "abc"
        assert await b.text() == "no size"
    assert manifest_reads(fake, bundle) == 1


async def test_async_failed_shared_read_fails_every_waiter_and_caches_nothing() -> None:
    fake, bundle, _ = public_fake()
    fake.tampered.add(bundle.hash)
    transport, _ = yielding(fake)
    async with AsyncClient(fake.url, transport=transport) as c:
        outcomes = await asyncio.gather(
            c.manifest(bundle.hash), c.get(f"decdn:{bundle.hash}/a.txt"), return_exceptions=True
        )
        assert [isinstance(o, DecdnError) and o.code for o in outcomes] == ["HASH_MISMATCH"] * 2
        assert manifest_reads(fake, bundle) == 1
        fake.tampered.discard(bundle.hash)
        m = await c.manifest(bundle.hash)
        assert m.hash == bundle.hash
        assert await c.manifest(bundle.hash) is m
    assert manifest_reads(fake, bundle) == 2


async def test_async_cancelled_waiter_leaves_the_shared_read_running() -> None:
    fake, bundle, _ = public_fake()
    gate = asyncio.Event()
    transport, asked = yielding(fake, gate)
    async with AsyncClient(fake.url, transport=transport) as c:
        first = asyncio.ensure_future(c.manifest(bundle.hash))
        second = asyncio.ensure_future(c.manifest(bundle.hash))
        for _ in range(5):
            await asyncio.sleep(0)
        first.cancel()
        gate.set()
        m = await second
        assert m.hash == bundle.hash
        with pytest.raises(asyncio.CancelledError):
            await first
        assert await c.manifest(bundle.hash) is m
    assert asked == [f"{fake.url}/{bundle.hash}"]


async def test_loopback_client_ignores_proxy_env(flavour: Driver, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    fake, _, blob = loopback_fake()
    c = await open_loop(flavour, fake)
    assert await c.text(await c.get(f"decdn:{blob}")) == "loop bytes"
    # `_http` is the client's private httpx client: its `trust_env` decides whether proxy variables apply.
    assert c.client._http.trust_env is False
    public, _, _ = public_fake()
    assert (await flavour.open(public)).client._http.trust_env is True
