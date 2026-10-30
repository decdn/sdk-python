"""The SDK against a public gateway: the fake by default, the real binary when decdn's contract run
runs this file with `DECDN_CONTRACT`:
https://github.com/decdn/decdn/blob/main/crates/e2e/tests/gateway_sdk.rs
"""

from collections.abc import Iterator

import httpx
import pytest

from decdn.base32 import hash_to_base32
from decdn.client import ByteRange, HeadInfo
from decdn.hashing import blake3_hex
from decdn.response import ContentRange
from tests.contract.fixture import Contract, contract
from tests.support.flavour import Driver, rejects

pytestmark = pytest.mark.anyio


@pytest.fixture(scope="module", name="contract")
def contract_fixture() -> Iterator[Contract]:
    c = contract("public")
    yield c
    c.close()


@pytest.fixture
async def client(contract: Contract, flavour: Driver) -> Driver:
    return await contract.open(flavour)


async def test_streams_a_blob_from_the_bare_host_verified_readable_by_any_origin(
    contract: Contract, client: Driver
) -> None:
    c = contract
    r = await client.get(f"decdn:{c.blob}")
    assert r.status == 200
    assert r.verified is True
    assert r.headers.get("access-control-allow-origin") == "*"
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert blake3_hex(await client.read(r)) == c.blob


async def test_verifies_an_entry_fetched_through_its_site(contract: Contract, client: Driver) -> None:
    c = contract
    r = await client.get(f"decdn:{c.bundle}/a.txt")
    assert r.verified is True
    assert r.content_type == "text/plain; charset=utf-8"
    assert r.content_length == len(c.files["a.txt"])
    assert await client.read(r) == c.files["a.txt"]


async def test_verifies_a_page_by_its_entry_hash_unrewritten(contract: Contract, client: Driver) -> None:
    c = contract
    r = await client.get(f"decdn:{c.bundle}/index.html")
    assert r.verified is True
    assert await client.read(r) == c.files["index.html"]


async def test_rewrites_the_page_on_its_site(contract: Contract, client: Driver) -> None:
    c = contract
    r = await client.get(f"decdn:{c.bundle}/index.html", verify=False)
    assert "connect-src 'self'" in (r.headers.get("content-security-policy") or "")
    assert f"/_decdn/{c.blob}" in await client.text(r)


async def test_serves_a_window_of_a_sized_entry_as_206(contract: Contract, client: Driver) -> None:
    c = contract
    r = await client.get(f"decdn:{c.bundle}/data.bin", range=ByteRange(offset=1000, length=5000))
    assert r.status == 206
    assert r.verified == "skipped"
    size = len(c.files["data.bin"])
    assert r.content_range == ContentRange(offset=1000, length=5000, size=size)
    assert r.headers.get("accept-ranges") == "bytes"
    assert await client.read(r) == c.files["data.bin"][1000:6000]


async def test_refuses_a_window_past_the_end_with_the_size(contract: Contract, client: Driver) -> None:
    c = contract
    size = len(c.files["a.txt"])
    e = await rejects(
        client.get(f"decdn:{c.bundle}/a.txt", range=ByteRange(offset=size)), "RANGE_NOT_SATISFIABLE"
    )
    assert e.size == size


async def test_answers_head_on_an_entry_from_the_manifest(contract: Contract, client: Driver) -> None:
    c = contract
    m = await client.manifest(c.bundle)
    entry = m.entry("data.bin")
    assert entry is not None
    assert await client.head(f"decdn:{c.bundle}/data.bin") == HeadInfo(
        size=entry.size, hash=entry.hash, content_type="application/octet-stream"
    )


async def test_reads_the_manifest_the_bundle_hash_names(contract: Contract, client: Driver) -> None:
    c = contract
    m = await client.manifest(f"decdn:{c.bundle}/")
    assert m.hash == c.bundle
    assert sorted(e.path for e in m.entries) == sorted(c.files)


async def test_answers_a_ranged_preflight_from_any_origin(
    contract: Contract, flavour: Driver, client: Driver
) -> None:
    c = contract
    site = await client.site_url(f"decdn:{c.bundle}/data.bin")
    r = await c.fetch(
        flavour,
        site,
        method="OPTIONS",
        headers={
            "origin": "https://app.example",
            "access-control-request-method": "GET",
            "access-control-request-headers": "range",
        },
    )
    assert r.status_code == 204
    assert r.headers.get("access-control-allow-origin") == "*"
    assert r.headers.get("access-control-allow-methods") == "GET, HEAD"
    assert r.headers.get("access-control-allow-headers") == "range"
    assert "Content-Range" in (r.headers.get("access-control-expose-headers") or "")


async def test_answers_a_pointer_preflight_on_the_bare_host_only(
    contract: Contract, flavour: Driver, client: Driver
) -> None:
    c = contract
    preflight = {
        "origin": "https://app.example",
        "access-control-request-method": "PUT",
        "access-control-request-headers": "content-type",
    }
    bare = await c.fetch(flavour, f"{c.gateway_url}/_pointer", method="OPTIONS", headers=preflight)
    assert bare.status_code == 204
    assert bare.headers.get("access-control-allow-methods") == "PUT"
    assert bare.headers.get("access-control-allow-headers") == "content-type"
    site = httpx.URL(await client.site_url(f"decdn:{c.bundle}/"))
    origin = f"{site.scheme}://{site.netloc.decode('ascii')}"
    off = await c.fetch(flavour, f"{origin}/_pointer", method="OPTIONS", headers=preflight)
    assert off.status_code == 404


async def test_lets_any_origin_read_a_refusal(contract: Contract, flavour: Driver, client: Driver) -> None:
    c = contract
    size = len(c.files["a.txt"])
    r = await c.fetch(
        flavour,
        await client.site_url(f"decdn:{c.bundle}/a.txt"),
        headers={"origin": "https://app.example", "range": f"bytes={size}-"},
    )
    assert r.status_code == 416
    assert r.headers.get("access-control-allow-origin") == "*"
    assert "Content-Range" in (r.headers.get("access-control-expose-headers") or "")


async def test_refuses_an_unknown_host(contract: Contract, flavour: Driver) -> None:
    r = await contract.fetch(flavour, "http://evil.example/")
    assert r.status_code == 403


async def test_redirects_a_bundle_on_the_bare_host_to_its_site(contract: Contract, flavour: Driver) -> None:
    c = contract
    r = await c.fetch(flavour, f"{c.gateway_url}/{c.bundle}/a.txt")
    assert r.status_code == 302
    gateway = httpx.URL(c.gateway_url)
    assert r.headers.get("location") == (
        f"{gateway.scheme}://{hash_to_base32(c.bundle)}.{gateway.netloc.decode('ascii')}/a.txt"
    )


async def test_stores_a_pointer_the_namespaces_owner_signs_and_its_host_serves_the_bundle(
    contract: Contract, flavour: Driver, client: Driver
) -> None:
    c = contract
    p = c.pointer
    if p is None:
        raise RuntimeError("the fixture has no pointer")
    namespace = c.namespace(flavour)
    signer = p.private_key
    set_ = await client.pointer_set(namespace=namespace, root=f"decdn:{c.bundle}/", signer=signer)
    assert set_.root == c.bundle
    assert set_.url == await client.namespace_url(namespace)
    page = await c.fetch(flavour, f"{set_.url}a.txt")
    assert page.status_code == 200
    assert page.content == c.files["a.txt"]
    replay = await rejects(
        client.pointer_set(namespace=namespace, root=c.bundle, seq=set_.seq, signer=signer), "CONFLICT"
    )
    assert replay.seq == set_.seq
    await rejects(
        client.pointer_set(namespace=namespace, root=c.bundle, seq=set_.seq + 1, signer=f"0x{'33' * 32}"),
        "FORBIDDEN",
    )
