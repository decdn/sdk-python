"""The SDK against a loopback gateway: the fake by default, the real binary when
decdn's contract run runs this file with `DECDN_CONTRACT`:
https://github.com/decdn/decdn/blob/main/crates/e2e/src/sdk_contract.rs

The tests run in file order and share the gateway: content is refused until a capability is
registered. Every flavour's capability is issued to the gateway's one pending key, each from its
own pool, and each flavour registers its own.
"""

import re
from collections.abc import Iterator

import httpx
import pytest

from decdn.base32 import hash_to_base32
from decdn.capability import parse_capability
from decdn.client import ByteRange
from decdn.hashing import blake3_hex
from tests.contract.fixture import Contract, contract, shipped_after
from tests.fake_gateway import CROSS_ORIGIN, CROSS_SITE, NO_CAPABILITY, NO_FETCH_SITE
from tests.support.flavour import FLAVOURS, Driver, rejects

pytestmark = pytest.mark.anyio


@pytest.fixture(scope="module", name="contract")
def contract_fixture() -> Iterator[Contract]:
    c = contract("loopback")
    yield c
    c.close()


@pytest.fixture
async def client(contract: Contract, flavour: Driver) -> Driver:
    return await contract.open(flavour)


async def test_refuses_an_unknown_host(contract: Contract, flavour: Driver) -> None:
    r = await contract.fetch(flavour, "http://evil.example/")
    assert r.status_code == 403


# No token, a browser-header gate on content, the pending delegate key, capabilities without a
# scope, and payment by the held capabilities in registration order: decdn/decdn's gateway after
# release 0.3.1. A released gateway up to 0.3.1 wants a token and a page host, so it never collects
# these tests.
if shipped_after("0.3.1"):

    async def test_refuses_content_no_capability_pays_for_as_plain_text(
        contract: Contract, client: Driver
    ) -> None:
        # Nothing pulled the blob through the gateway yet, so its cache does not hold it.
        e = await rejects(client.get(f"decdn:{contract.blob}"), "PAYMENT_REQUIRED")
        assert e.body == NO_CAPABILITY
        assert e.fallback_uri is None

    async def test_returns_the_one_pending_key_until_a_capability_claims_it(
        contract: Contract, client: Driver
    ) -> None:
        first = await client.loopback_delegate()
        again = await client.loopback_delegate()
        assert re.fullmatch(r"0x[0-9a-fA-F]{40}", first.address)
        assert again.address == first.address
        assert first.address.lower() == parse_capability(contract.capability(client)).signer.lower()

    async def test_registers_a_capability_from_each_pool_on_the_one_key(
        contract: Contract, flavour: Driver, client: Driver
    ) -> None:
        capability = contract.capability(flavour)
        registered = await client.loopback_register(capability=capability)
        grant = parse_capability(capability)
        assert registered.signer.lower() == grant.signer.lower()
        assert registered.pool.lower() == grant.pool_id.lower()
        assert registered.expiry == grant.expiry

    async def test_lists_the_capabilities_in_registration_order(
        contract: Contract, flavour: Driver, client: Driver
    ) -> None:
        c = contract
        held = await client.loopback_capabilities()
        grants = [parse_capability(cap) for cap in c.capabilities[: len(FLAVOURS)]]
        wanted = [(g.signer.lower(), g.pool_id.lower()) for g in grants]
        # Every flavour registered its own, in flavour order, before any flavour lists them.
        mine = FLAVOURS.index(flavour.name)
        listed = [
            (h.signer.lower(), h.pool.lower()) for h in held if (h.signer.lower(), h.pool.lower()) in wanted
        ]
        assert listed == wanted
        entry = next(h for h in held if (h.signer.lower(), h.pool.lower()) == wanted[mine])
        assert (entry.cap, entry.expiry, entry.spent) == (
            grants[mine].spending_cap,
            grants[mine].expiry,
            False,
        )

    async def test_refuses_a_second_capability_from_one_pool_on_one_key(
        contract: Contract, flavour: Driver, client: Driver
    ) -> None:
        await rejects(client.loopback_register(capability=contract.capability(flavour)), "CONFLICT")

    async def test_creates_a_fresh_pending_key_once_a_capability_claims_it(
        contract: Contract, flavour: Driver, client: Driver
    ) -> None:
        claimed = parse_capability(contract.capability(flavour)).signer.lower()
        fresh = await client.loopback_delegate()
        assert fresh.address.lower() != claimed
        assert (await client.loopback_delegate()).address == fresh.address

    async def test_answers_no_preflight(contract: Contract, flavour: Driver) -> None:
        c = contract
        r = await c.fetch(
            flavour,
            f"{c.gateway_url}/_capability",
            method="OPTIONS",
            headers={"origin": "https://app.example", "access-control-request-method": "PUT"},
        )
        assert r.status_code == 403
        assert r.headers.get("access-control-allow-origin") is None

    async def test_refuses_a_page_on_another_site_or_origin_and_a_browser_with_no_fetch_site(
        contract: Contract, flavour: Driver
    ) -> None:
        c = contract
        # The gate stands in front of every route: content and the API alike.
        for url in (f"{c.gateway_url}/{c.blob}", f"{c.gateway_url}/_capability"):
            cross_site = await c.fetch(flavour, url, headers={"sec-fetch-site": "cross-site"})
            assert (cross_site.status_code, cross_site.text) == (403, f"{CROSS_SITE}\n"), url
            foreign = await c.fetch(
                flavour, url, headers={"origin": "https://app.example", "sec-fetch-site": "same-site"}
            )
            assert (foreign.status_code, foreign.text) == (403, f"{CROSS_ORIGIN}\n"), url
            browser = await c.fetch(flavour, url, headers={"user-agent": "Mozilla/5.0"})
            assert (browser.status_code, browser.text) == (403, f"{NO_FETCH_SITE}\n"), url

    async def test_serves_a_page_on_one_of_its_own_sites(
        contract: Contract, flavour: Driver, client: Driver
    ) -> None:
        c = contract
        site = httpx.URL(await client.site_url(f"decdn:{c.bundle}/"))
        own = {"origin": f"http://{site.netloc.decode('ascii')}", "sec-fetch-site": "same-site"}
        r = await c.fetch(flavour, f"{c.gateway_url}/{c.blob}", headers=own)
        assert r.status_code == 200
        assert r.content == c.blob_bytes

    async def test_serves_content_to_a_client_outside_a_browser(contract: Contract, flavour: Driver) -> None:
        c = contract
        r = await c.fetch(flavour, f"{c.gateway_url}/{c.blob}")
        assert r.status_code == 200
        assert r.content == c.blob_bytes

    async def test_redirects_a_navigation_to_a_bundle_to_its_site(
        contract: Contract, flavour: Driver
    ) -> None:
        c = contract
        navigate = {"sec-fetch-mode": "navigate", "sec-fetch-site": "none"}
        r = await c.fetch(flavour, f"{c.gateway_url}/{c.bundle}/a.txt", headers=navigate)
        assert r.status_code == 302
        location = r.headers.get("location") or ""
        assert location.startswith(f"http://{hash_to_base32(c.bundle)}.localhost:")
        assert location.endswith("/a.txt")

    async def test_streams_a_blob_verified(contract: Contract, client: Driver) -> None:
        c = contract
        r = await client.get(f"decdn:{c.blob}")
        assert r.verified is True
        assert r.headers.get("access-control-allow-origin") is None
        assert blake3_hex(await client.read(r)) == c.blob

    async def test_verifies_entries_on_the_bare_host_a_page_byte_exact(
        contract: Contract, client: Driver
    ) -> None:
        c = contract
        for path in ("a.txt", "index.html", "data.bin"):
            r = await client.get(f"decdn:{c.bundle}/{path}")
            assert r.verified is True, path
            assert await client.read(r) == c.files[path], path

    async def test_serves_a_window_of_a_sized_entry(contract: Contract, client: Driver) -> None:
        c = contract
        r = await client.get(f"decdn:{c.bundle}/data.bin", range=ByteRange(offset=10, length=20))
        assert r.status == 206
        assert await client.read(r) == c.files["data.bin"][10:30]

    async def test_answers_head_from_the_manifest(contract: Contract, client: Driver) -> None:
        c = contract
        info = await client.head(f"decdn:{c.bundle}/a.txt")
        assert info.size == len(c.files["a.txt"])
        assert info.content_type == "text/plain; charset=utf-8"
