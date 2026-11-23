import json
import time

import httpx
import pytest

from decdn.base32 import hash_to_base32
from decdn.client import Client
from decdn.errors import DecdnError
from decdn.pointer import PointerDomain
from tests.conftest import fixture_json
from tests.fake_gateway import (
    CANNOT_PAY,
    CROSS_ORIGIN,
    CROSS_SITE,
    EXPIRY_MARGIN_SECS,
    EXPOSED,
    NO_CAPABILITY,
    NO_FETCH_SITE,
    FakeGateway,
    checksum,
)
from tests.support.host_transport import AsyncHostTransport, HostTransport
from tests.support.postcard import encode_capability

CSP = "default-src 'self' 'unsafe-inline' 'unsafe-eval' data: blob:; connect-src 'self'"


def client(fake: FakeGateway, *, follow: bool = False) -> httpx.Client:
    return httpx.Client(transport=fake.transport(), follow_redirects=follow)


def funded_public() -> FakeGateway:
    """A public fake holding one capability, so its pulls are paid."""
    fake = FakeGateway("public")
    fake.grant()
    return fake


def test_checksum_spells_addresses_eip55() -> None:
    assert (
        checksum("0x5aaeb6053f3e94c9b9a09f33669435e7ef1beaed") == "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"
    )
    assert checksum(fixture_json("pointer")["address"].lower()) == fixture_json("pointer")["address"]


def test_public_blob_200_with_cors() -> None:
    fake = funded_public()
    h = fake.add_blob("hello")
    with client(fake) as c:
        r = c.get(f"{fake.url}/{h}")
        assert r.status_code == 200
        assert r.content == b"hello"
        assert r.headers["content-type"] == "application/octet-stream"
        assert r.headers["content-length"] == "5"
        assert r.headers["accept-ranges"] == "bytes"
        assert r.headers["content-disposition"] == f'inline; filename="{h}"'
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["access-control-allow-origin"] == "*"
        assert r.headers["access-control-expose-headers"] == EXPOSED
        preflight = c.options(f"{fake.url}/{h}")
        assert preflight.status_code == 204
        assert preflight.headers["access-control-allow-methods"] == "GET, HEAD"
        assert preflight.headers["access-control-allow-headers"] == "range"
        assert preflight.headers["access-control-max-age"] == "600"
        pointer = c.options(f"{fake.url}/_pointer")
        assert pointer.headers["access-control-allow-methods"] == "PUT"
        assert pointer.headers["access-control-allow-headers"] == "content-type"
        assert c.options(f"{fake.url}/_delegate").status_code == 204
    assert [s.method for s in fake.seen] == ["GET", "OPTIONS", "OPTIONS", "OPTIONS"]
    assert fake.seen[0].url == f"{fake.url}/{h}"


def test_public_bundle_redirects_to_site() -> None:
    fake = funded_public()
    m = fake.add_bundle({"index.html": "<p>hi</p>", "a b.txt": "x"})
    label = hash_to_base32(m.hash)
    with client(fake) as c:
        r = c.get(f"{fake.url}/{m.hash}/")
        assert r.status_code == 302
        assert r.headers["location"] == f"http://{label}.decdn.test/"
        r = c.get(f"{fake.url}/{m.hash}/a%20b.txt?ns=7")
        assert r.headers["location"] == f"http://7.{label}.decdn.test/a%20b.txt"
        assert c.get(f"{fake.url}/{m.hash}/", follow_redirects=True).text == "<p>hi</p>"


def test_site_serves_entry_with_type() -> None:
    fake = funded_public()
    m = fake.add_bundle({"a.txt": "x", "d/data.json": "{}", "blob.bin": b"\x00\x01"})
    site = f"http://{hash_to_base32(m.hash)}.decdn.test"
    with client(fake) as c:
        r = c.get(f"{site}/a.txt")
        assert r.status_code == 200
        assert r.text == "x"
        assert r.headers["content-type"] == "text/plain; charset=utf-8"
        assert r.headers["content-disposition"] == 'inline; filename="a.txt"'
        assert r.headers["content-length"] == "1"
        assert r.headers["content-security-policy"] == CSP
        assert c.get(f"{site}/d/data.json").headers["content-type"] == "application/json"
        assert c.get(f"{site}/blob.bin").content == b"\x00\x01"
        listing = c.get(f"{site}/")
        assert listing.headers["content-type"] == "text/html; charset=utf-8"
        assert '<li><a href="a.txt">a.txt</a></li>' in listing.text
        missing = c.get(f"{site}/nope.txt")
        assert (missing.status_code, missing.text) == (404, "no entry nope.txt in bundle\n")
        assert c.get(f"{site}/_delegate").status_code == 404
    # The loopback bare host serves every text or active type as plain text.
    loopback = FakeGateway("loopback")
    m = loopback.add_bundle({"page.html": "<p>x</p>"})
    loopback.grant()
    with client(loopback) as c:
        bare = c.get(f"{loopback.url}/{m.hash}/page.html")
        assert (bare.status_code, bare.text) == (200, "<p>x</p>")
        assert bare.headers["content-type"] == "text/plain; charset=utf-8"
        assert "content-security-policy" not in bare.headers


def test_site_rewrites_html_and_sets_csp() -> None:
    fake = funded_public()
    img = fake.add_blob(b"\x89PNG")
    m = fake.add_bundle({"index.html": f'<img src="decdn:{img}">', "s/style.css": f"a{{b:url(decdn:{img})}}"})
    site = f"http://{hash_to_base32(m.hash)}.decdn.test"
    with client(fake) as c:
        r = c.get(f"{site}/")
        assert r.status_code == 200
        assert r.text == f'<img src="/_decdn/{img}">'
        assert r.headers["content-type"] == "text/html; charset=utf-8"
        assert r.headers["content-security-policy"] == CSP
        assert "content-length" not in r.headers
        css = c.get(f"{site}/s/style.css")
        assert css.text == f"a{{b:url(/_decdn/{img})}}"
        assert css.headers["content-type"] == "text/css; charset=utf-8"
        blob = c.get(f"{site}/_decdn/{img}")
        assert blob.content == b"\x89PNG"
        assert blob.headers["content-security-policy"] == CSP
        head = c.head(f"{site}/")
        assert head.headers["etag"] == f'"{m.entry("index.html").hash}"'  # type: ignore[union-attr]
        assert "content-length" not in head.headers


def test_range_206_and_416() -> None:
    fake = funded_public()
    h = fake.add_blob("0123456789")
    with client(fake) as c:
        r = c.get(f"{fake.url}/{h}", headers={"range": "bytes=2-4"})
        assert r.status_code == 206
        assert r.content == b"234"
        assert r.headers["content-range"] == "bytes 2-4/10"
        assert r.headers["content-length"] == "3"
        assert r.headers["accept-ranges"] == "bytes"
        assert c.get(f"{fake.url}/{h}", headers={"range": "bytes=-3"}).content == b"789"
        assert c.get(f"{fake.url}/{h}", headers={"range": "bytes=7-"}).content == b"789"
        assert c.get(f"{fake.url}/{h}", headers={"range": "bytes=5-2"}).status_code == 200
        r = c.get(f"{fake.url}/{h}", headers={"range": "bytes=10-"})
        assert r.status_code == 416
        assert r.headers["content-range"] == "bytes */10"
        assert r.text == "range not satisfiable\n"
        assert c.get(f"{fake.url}/{h}", headers={"range": "bytes=-0"}).status_code == 416


def test_head_etag() -> None:
    fake = funded_public()
    h = fake.add_blob("hello")
    m = fake.add_bundle({"a.txt": "x"})
    with client(fake) as c:
        r = c.head(f"{fake.url}/{h}")
        assert r.status_code == 200
        assert r.headers["etag"] == f'"{h}"'
        assert r.headers["content-length"] == "5"
        assert r.headers["accept-ranges"] == "bytes"
        site = c.head(f"http://{hash_to_base32(m.hash)}.decdn.test/a.txt")
        assert site.headers["etag"] == f'"{m.entries[0].hash}"'
        assert site.headers["content-length"] == "1"
        assert c.head(f"{fake.url}/").status_code == 200
    loopback = FakeGateway("loopback")
    with client(loopback) as c:
        r = c.head(f"{loopback.url}/{h}")
        assert (r.status_code, r.headers["allow"]) == (405, "GET")


def test_site_directory_redirects_and_serves_its_index() -> None:
    fake = funded_public()
    m = fake.add_bundle({"docs/index.html": "<p>docs</p>", "a.txt": "x"})
    label = hash_to_base32(m.hash)
    site = f"http://{label}.decdn.test"
    page = {"sec-fetch-mode": "cors"}
    with client(fake) as c:
        for base, q in ((site, "?tab=2"), (f"{site}/_decdn/{m.hash}", "?ns=7")):
            r = c.get(f"{base}/docs{q}", headers=page)
            assert (r.status_code, r.headers["location"]) == (302, f"docs/{q}")
            assert c.get(f"{base}/docs/{q}", headers=page).text == "<p>docs</p>"
        head = c.head(f"{site}/docs?tab=2")
        assert (head.status_code, head.headers["location"]) == (302, "docs/?tab=2")
        index = c.head(f"{site}/docs/")
        assert (index.status_code, index.headers["etag"]) == (200, f'"{m.entries[0].hash}"')
        # The bundle root under `/_decdn/` is not a directory in the bundle: it lists the bundle.
        root = c.get(f"{site}/_decdn/{m.hash}/", headers=page)
        assert (root.status_code, '<a href="docs/index.html">' in root.text) == (200, True)
        assert c.get(f"{site}/_decdn/{m.hash}/nope/", headers=page).status_code == 404
        bare = c.get(f"{fake.url}/{m.hash}/docs")
        assert (bare.status_code, bare.headers["location"]) == (302, f"{site}/docs")


def test_navigation_of_a_decdn_bundle_redirects_to_its_site() -> None:
    fake = FakeGateway("public")
    m = fake.add_bundle({"docs/index.html": "<p>docs</p>"})
    label = hash_to_base32(m.hash)
    site = f"http://{label}.decdn.test"
    with client(fake) as c:
        for mode in ({}, {"sec-fetch-mode": "navigate"}):
            r = c.get(f"{site}/_decdn/{m.hash}/docs/?ns=7", headers=mode)
            assert (r.status_code, r.headers["location"]) == (302, f"http://7.{label}.decdn.test/docs/")
        r = c.get(f"{site}/_decdn/{m.hash}/docs")
        assert (r.status_code, r.headers["location"]) == (302, f"{site}/docs")


def test_head_of_a_decdn_directory_redirects_to_its_site_at_no_pull() -> None:
    # The fake holds no capability, so anything that pulls answers 503.
    fake = FakeGateway("public")
    m = fake.add_bundle({"docs/index.html": "<p>docs</p>", "a.txt": "x"})
    label = hash_to_base32(m.hash)
    site = f"http://{label}.decdn.test"
    with client(fake) as c:
        r = c.head(f"{site}/_decdn/{m.hash}/docs/?ns=7")
        assert (r.status_code, r.headers["location"]) == (302, f"http://7.{label}.decdn.test/docs/")
        entry = c.head(f"{site}/_decdn/{m.hash}/a.txt")
        assert (entry.status_code, entry.headers["location"]) == (302, f"{site}/a.txt")


def test_unknown_host_403() -> None:
    fake = funded_public()
    with client(fake) as c:
        r = c.get("http://other.test/")
        assert (r.status_code, r.text) == (403, "unknown Host\n")
        assert r.headers["content-type"] == "text/plain; charset=utf-8"
        r = c.get(f"http://9.{fake.host}/")
        assert (r.status_code, r.text) == (404, "namespace 9 has no pointer on this gateway\n")
        assert c.get(f"{fake.url}/").text == "<h1>decdn gateway</h1>"
        assert c.post(f"{fake.url}/x").status_code == 405
        assert c.get(f"{fake.url}/nothex").status_code == 400


def test_loopback_browser_header_gate() -> None:
    fake = FakeGateway("loopback")
    h = fake.add_blob("hello")
    m = fake.add_bundle({"a.txt": "x"})
    site = f"http://{hash_to_base32(m.hash)}.localhost:8484"
    with client(fake) as c:
        # The gate refuses before anything pays: with no capability, a refused request is a 403.
        r = c.get(f"{fake.url}/{h}", headers={"sec-fetch-site": "cross-site"})
        assert (r.status_code, r.text) == (403, f"{CROSS_SITE}\n")
        assert c.get(f"{fake.url}/{h}").status_code == 402
    fake.grant()
    with client(fake) as c:
        assert fake.url == "http://127.0.0.1:8484"
        # A client outside a browser is served, on the bare host and on a site.
        r = c.get(f"{fake.url}/{h}")
        assert (r.status_code, r.content) == (200, b"hello")
        assert "access-control-allow-origin" not in r.headers
        assert c.get(f"{site}/a.txt").text == "x"
        for url in (f"{fake.url}/{h}", f"{site}/a.txt", f"{fake.url}/_delegate", f"{fake.url}/_capability"):
            r = c.get(url, headers={"sec-fetch-site": "cross-site"})
            assert (r.status_code, r.text) == (403, f"{CROSS_SITE}\n"), url
            r = c.get(url, headers={"origin": "http://app.example", "sec-fetch-site": "same-site"})
            assert (r.status_code, r.text) == (403, f"{CROSS_ORIGIN}\n"), url
            for browser in ({"user-agent": "Mozilla/5.0"}, {"origin": site}):
                r = c.get(url, headers=browser)
                assert (r.status_code, r.text) == (403, f"{NO_FETCH_SITE}\n"), url
        # The gateway's own origins pass: its bare host, its sites and their namespaced spellings.
        for own in (fake.url, "http://localhost:8484", site, f"http://7.{site[len('http://') :]}"):
            r = c.get(f"{fake.url}/{h}", headers={"origin": own, "sec-fetch-site": "same-site"})
            assert r.status_code == 200, own
        for other in ("http://localhost:9999", "https://127.0.0.1:8484", "null", f"http://x.{site[7:]}"):
            r = c.get(f"{fake.url}/{h}", headers={"origin": other, "sec-fetch-site": "same-site"})
            assert r.text == f"{CROSS_ORIGIN}\n", other
        # A site spends only for its own pages, the user and a top-level navigation.
        assert c.get(f"{site}/a.txt", headers={"sec-fetch-site": "same-site"}).status_code == 403
        same = {"sec-fetch-site": "same-site", "sec-fetch-mode": "navigate"}
        assert c.get(f"{site}/a.txt", headers=same).text == "x"
        assert c.get(f"{site}/a.txt", headers={"sec-fetch-site": "same-origin"}).text == "x"
        # A navigation to a bundle on the bare host goes to its site; anything else is served there.
        r = c.get(
            f"{fake.url}/{m.hash}/a.txt", headers={"sec-fetch-mode": "navigate", "sec-fetch-site": "none"}
        )
        assert (r.status_code, r.headers["location"]) == (302, f"{site}/a.txt")
        assert c.get(f"{fake.url}/{m.hash}/a.txt").text == "x"
        assert c.get("http://localhost:8484/").text == "<h1>decdn gateway</h1>"


def test_loopback_pays_with_the_first_usable_capability_in_registration_order() -> None:
    fake = FakeGateway("loopback")
    h = fake.add_blob("hello")
    with client(fake) as c:
        r = c.get(f"{fake.url}/{h}")
        assert (r.status_code, r.text) == (402, f"{NO_CAPABILITY}\n")
        assert r.headers["content-type"] == "text/plain; charset=utf-8"
        # Inside the expiry margin, spent, then one whose pool stops for funding: the fourth pays.
        near = fake.grant("0x" + "01" * 32, expiry=int(time.time()) + EXPIRY_MARGIN_SECS)
        spent = fake.grant("0x" + "02" * 32)
        spent.spent = True
        drained = fake.grant("0x" + "03" * 32)
        fake.unfunded_pools.add(drained.pool)
        paying = fake.grant("0x" + "04" * 32)
        r = c.get(f"{fake.url}/{h}")
        assert (r.status_code, r.content) == (200, b"hello")
        assert [g.spent for g in (near, spent, drained, paying)] == [False, True, True, False]
        paying.spent = True
        assert c.get(f"{fake.url}/{h}").status_code == 402
        # A blob the cache holds whole costs no pull, so it needs no capability.
        fake.cached.add(h)
        r = c.get(f"{fake.url}/{h}")
        assert (r.status_code, r.content) == (200, b"hello")


def test_loopback_pays_a_namespace_with_a_capability_scoped_to_it_first() -> None:
    fake = FakeGateway("loopback")
    h = fake.add_blob("hello")
    with client(fake) as c:
        scoped = fake.grant("0x" + "01" * 32, namespaces=[7])
        # A capability scoped to other namespaces pays for nothing else.
        assert c.get(f"{fake.url}/{h}").status_code == 402
        assert c.get(f"{fake.url}/{h}?ns=8").status_code == 402
        assert c.get(f"{fake.url}/{h}?ns=7").content == b"hello"
        # Registered after the unscoped one, the scoped one still pays first for its namespace: a
        # funding stop on its pool retires it, and the unscoped one takes the request.
        scoped.spent = False
        anything = fake.grant("0x" + "02" * 32)
        fake.capabilities.reverse()
        fake.unfunded_pools.add(scoped.pool)
        assert c.get(f"{fake.url}/{h}?ns=7").content == b"hello"
        assert (scoped.spent, anything.spent) == (True, False)


def test_public_pays_with_a_scoped_capability_only_where_its_pointer_serves() -> None:
    fake = FakeGateway("public")
    m = fake.add_bundle({"a.txt": "x"})
    other = fake.add_blob("other")
    fake.pointers[7] = m.hash
    fake.grant(namespaces=[7])
    label = hash_to_base32(m.hash)
    page = {"sec-fetch-mode": "cors"}
    with client(fake) as c:
        assert c.get("http://7.decdn.test/a.txt").text == "x"
        assert c.get(f"http://7.decdn.test/_decdn/{m.hash}/a.txt", headers=page).text == "x"
        # The pointer's namespace pays for its own bundle alone, and only on its host.
        for url in (
            f"http://7.decdn.test/_decdn/{other}",
            f"{fake.url}/{other}",
            f"http://{label}.decdn.test/a.txt",
            f"http://7.{label}.decdn.test/a.txt",
        ):
            assert c.get(url, headers=page).status_code == 503, url


def test_public_nothing_pays_503_json_and_retry_after() -> None:
    fake = FakeGateway("public")
    h = fake.add_blob("0123456789")
    m = fake.add_bundle({"a.txt": "x"})
    site = f"http://{hash_to_base32(m.hash)}.decdn.test"
    with client(fake) as c:
        for r in (c.get(f"{fake.url}/{h}"), c.head(f"{fake.url}/{h}"), c.get(f"{site}/a.txt")):
            assert r.status_code == 503
            assert r.headers["retry-after"] == "60"
            assert r.headers["content-type"] == "application/json"
        r = c.get(f"{fake.url}/{h}")
        assert r.text == f'{{"error":"{CANNOT_PAY}","retry_after_secs":60,"uri":"decdn:{h}"}}'
        assert r.headers["link"] == f'<decdn:{h}>; rel="alternate"'
        # A blob the cache holds whole is served with nothing that can pay.
        fake.cached.add(h)
        assert c.get(f"{fake.url}/{h}").content == b"0123456789"
        held = fake.grant()
        assert c.get(f"{site}/a.txt").text == "x"
        # A pull no capability can pay leaves the capabilities usable.
        fake.unfunded.add(h)
        fake.cached.clear()
        assert c.get(f"{fake.url}/{h}").status_code == 503
        assert held.spent is False


def test_public_capability_api_on_the_bare_host() -> None:
    cap = fixture_json("capability")
    fake = FakeGateway("public")
    with client(fake) as c:
        r = c.get(f"{fake.url}/_delegate")
        assert r.status_code == 200
        assert r.headers["access-control-allow-origin"] == "*"
        fake.pend(cap["signer"])
        r = c.put(f"{fake.url}/_capability", content=json.dumps({"capability": cap["token"], "scope": "any"}))
        assert r.status_code == 201
        listed = c.get(f"{fake.url}/_capability").json()
        assert [e["pool"] for e in listed] == [cap["poolId"]]
        assert c.get(f"{fake.url}/_status").json() == {"capabilities": listed}
        preflight = c.options(f"{fake.url}/_capability")
        assert preflight.status_code == 204
        assert preflight.headers["access-control-allow-methods"] == "GET, PUT"
        site = f"http://{hash_to_base32('ab' * 32)}.decdn.test"
        for path in ("/_delegate", "/_capability", "/_status"):
            assert c.get(f"{site}{path}").status_code == 404
            assert c.options(f"{site}{path}").status_code == 404


def test_delegate_register_list_409s() -> None:
    cap = fixture_json("capability")
    signer = cap["signer"]
    fake = FakeGateway("loopback")
    body = json.dumps({"capability": cap["token"], "scope": "any"})
    with client(fake) as c:
        # The API takes no token.
        r = c.get(f"{fake.url}/_delegate")
        address = r.json()["address"]
        assert address == checksum(address.lower())
        assert fake.pending == address.lower()
        assert c.get(f"{fake.url}/_delegate").json() == {"address": address}
        r = c.put(f"{fake.url}/_capability", content=body)
        assert (r.status_code, r.text) == (
            409,
            "the capability names no key this gateway holds: get one from GET /_delegate\n",
        )
        fake.pend(signer)
        r = c.put(f"{fake.url}/_capability", content=body)
        assert r.status_code == 201
        assert r.text == f'{{"expiry":1900000000,"pool":"{cap["poolId"]}","signer":"{checksum(signer)}"}}'
        assert fake.pending is None
        r = c.put(f"{fake.url}/_capability", content=body)
        assert (r.status_code, r.text) == (
            409,
            "that key already holds a capability from this pool: get a new key from GET /_delegate\n",
        )
        # The same key takes a capability from another pool, scoped to namespaces it echoes.
        other = issue(cap, pool="0x" + "c4" * 32)
        scope = {"namespaces": [7, 2**64 - 1], "signature": "0x" + "ab" * 65}
        r = c.put(f"{fake.url}/_capability", content=json.dumps({"capability": other, "scope": scope}))
        assert (r.status_code, r.text) == (
            201,
            f'{{"expiry":1900000000,"namespaces":[7,18446744073709551615],"pool":"0x{"c4" * 32}",'
            f'"signer":"{checksum(signer)}"}}',
        )
        listed = c.get(f"{fake.url}/_capability")
        assert listed.text == (
            f'[{{"cap":10000000,"expiry":1900000000,"pool":"{cap["poolId"]}",'
            f'"signer":"{checksum(signer)}","spent":false}},'
            f'{{"cap":10000000,"expiry":1900000000,"namespaces":[7,18446744073709551615],'
            f'"pool":"0x{"c4" * 32}","signer":"{checksum(signer)}","spent":false}}]'
        )
        # Expired capabilities drop out of the list.
        fake.capabilities[0].expiry = int(time.time())
        assert len(c.get(f"{fake.url}/_capability").json()) == 1
        fresh = c.get(f"{fake.url}/_delegate").json()["address"]
        assert fresh.lower() not in (signer, address.lower())


def test_register_422s() -> None:
    cap = fixture_json("capability")
    fake = FakeGateway("loopback")
    fake.pend(cap["signer"])

    def put(body: str) -> httpx.Response:
        with client(fake) as c:
            return c.put(f"{fake.url}/_capability", content=body)

    # No scope.
    r = put(json.dumps({"capability": cap["token"]}))
    assert (r.status_code, r.text) == (422, 'expected {"capability","scope"}\n')
    r = put(json.dumps({"capability": cap["token"], "scope": "any", "extra": 1}))
    assert (r.status_code, r.text) == (422, 'expected {"capability","scope"}\n')
    assert put('{"scope":"any"}').status_code == 422
    assert put('{"capability":7,"scope":"any"}').status_code == 422
    assert put("nope").status_code == 422
    sig = "0x" + "ab" * 65
    for scope in (
        "example.com",
        None,
        {"namespaces": [], "signature": sig},
        {"namespaces": [2**64], "signature": sig},
        {"namespaces": [True], "signature": sig},
        {"namespaces": ["7"], "signature": sig},
        {"namespaces": [7], "signature": "0x" + "ab" * 64},
        {"namespaces": [7], "signature": "ab" * 65},
        {"namespaces": [7]},
        {"namespaces": [7], "signature": sig, "extra": 1},
    ):
        r = put(json.dumps({"capability": cap["token"], "scope": scope}))
        assert (r.status_code, r.text) == (
            422,
            'expected a scope of "any" or {"namespaces","signature"}\n',
        ), scope
    assert put(json.dumps({"capability": "dcap1:AAAA", "scope": "any"})).status_code == 422
    r = put(json.dumps({"capability": issue(cap, cap_=0), "scope": "any"}))
    assert (r.status_code, r.text) == (422, "the capability's cap is 0\n")
    soon = int(time.time()) + EXPIRY_MARGIN_SECS
    r = put(json.dumps({"capability": issue(cap, expiry=soon), "scope": "any"}))
    assert (r.status_code, r.text) == (422, "the capability expires inside the expiry margin\n")
    assert put(json.dumps({"capability": cap["token"] + " " * 70_000, "scope": "any"})).status_code == 413
    assert fake.capabilities == []


def test_loopback_answers_no_preflight() -> None:
    fake = FakeGateway("loopback")
    page = {"origin": "http://app.example.com", "access-control-request-method": "PUT"}
    with client(fake) as c:
        for path in ("/_capability", "/_delegate", "/x"):
            # A page's preflight stops at the gate; any other OPTIONS gets no CORS either.
            r = c.options(f"{fake.url}{path}", headers=page)
            assert (r.status_code, r.text) == (403, f"{CROSS_ORIGIN}\n")
            assert "access-control-allow-origin" not in r.headers
            r = c.options(f"{fake.url}{path}")
            assert (r.status_code, r.text) == (403, "no cross-origin access\n")


def issue(
    cap: dict[str, str], *, pool: str | None = None, cap_: int | None = None, expiry: int | None = None
) -> str:
    """The fixture capability with `pool`, `cap_` or `expiry` replaced."""
    return encode_capability(
        pool or cap["poolId"],
        cap["signer"],
        int(cap["spendingCap"]) if cap_ is None else cap_,
        int(cap["expiry"]) if expiry is None else expiry,
        bytes.fromhex(cap["ownerSignature"].removeprefix("0x")),
    )


def test_put_pointer_201_409_403() -> None:
    p = fixture_json("pointer")
    body = json.dumps(
        {"namespace": p["namespace"], "root": f"0x{p['root']}", "seq": p["seq"], "signature": p["signature"]}
    )
    fake = funded_public()
    with client(fake) as c:
        assert (c.put(f"{fake.url}/_pointer", content=body).status_code) == 502
        fake.pointer_domain = PointerDomain(chain_id=p["chainId"], publisher_registry=p["publisherRegistry"])
        r = c.put(f"{fake.url}/_pointer", content=body)
        assert (r.status_code, r.text) == (404, "namespace 7 does not exist\n")
        fake.owners[7] = p["address"].lower()
        r = c.put(f"{fake.url}/_pointer", content=body)
        assert r.status_code == 201
        assert r.text == f'{{"namespace":7,"root":"{p["root"]}","seq":3,"url":"http://7.decdn.test/"}}'
        assert fake.pointers[7] == p["root"]
        r = c.put(f"{fake.url}/_pointer", content=body)
        assert r.status_code == 409
        assert r.text == '{"error":"the gateway holds a pointer at or above this seq","seq":3}'
        assert c.put(f"{fake.url}/_pointer", content="{}").status_code == 422
        r = c.get("http://7.decdn.test/")
        assert (r.status_code, r.text) == (404, "not found\n")
    other = FakeGateway("public")
    other.pointer_domain = fake.pointer_domain
    other.owners[7] = "0x" + "11" * 20
    with client(other) as c:
        r = c.put(f"{other.url}/_pointer", content=body)
        assert (r.status_code, r.text) == (
            403,
            f"the pointer is signed by {p['address'].lower()}; namespace 7 is owned by 0x{'11' * 20}\n",
        )
    loopback = FakeGateway("loopback")
    with client(loopback) as c:
        r = c.put(f"{loopback.url}/_pointer", content=body)
        assert (r.status_code, r.text) == (404, "a loopback gateway holds no namespace pointers\n")


def test_truncated_and_tampered_blobs() -> None:
    fake = funded_public()
    cut = fake.add_blob("0123456789")
    flipped = fake.add_blob("abc")
    lying = fake.add_blob("hello")
    fake.truncated.add(cut)
    fake.tampered.add(flipped)
    fake.partial.add(lying)
    with client(fake) as c:
        with pytest.raises(httpx.ReadError, match="the pull failed"):
            c.get(f"{fake.url}/{cut}")
        with c.stream("GET", f"{fake.url}/{cut}") as r:
            chunks: list[bytes] = []
            with pytest.raises(httpx.ReadError):
                for chunk in r.iter_bytes():
                    chunks.append(chunk)
            assert b"".join(chunks) == b"01234"
        assert c.get(f"{fake.url}/{flipped}").content == bytes([ord("a") ^ 0xFF]) + b"bc"
        r = c.get(f"{fake.url}/{lying}")
        assert (r.status_code, r.content) == (206, b"h")
        assert r.headers["content-range"] == "bytes 0-0/5"


@pytest.mark.anyio
async def test_transport_serves_an_async_client() -> None:
    fake = funded_public()
    h = fake.add_blob("hello")
    cut = fake.add_blob("0123456789")
    fake.truncated.add(cut)
    body = json.dumps({"namespace": 1, "root": "0x" + "00" * 32, "seq": 1, "signature": "0x"})
    async with httpx.AsyncClient(transport=fake.transport()) as c:
        r = await c.get(f"{fake.url}/{h}")
        assert (r.status_code, r.content) == (200, b"hello")
        with pytest.raises(httpx.ReadError):
            await c.get(f"{fake.url}/{cut}")
        # The request body reaches the sync handler under the async client.
        assert (await c.put(f"{fake.url}/_pointer", content=body)).text == "no chain\n"


def test_listen_serves_over_a_socket() -> None:
    fake = funded_public()
    h = fake.add_blob("hello")
    cut = fake.add_blob("0123456789")
    fake.truncated.add(cut)
    bundle = fake.add_bundle({"unsized.txt": "no size here"}, ["unsized.txt"])
    cut_entry = bundle.entry("unsized.txt")
    assert cut_entry is not None
    fake.truncated.add(cut_entry.hash)
    listener = fake.listen()
    try:
        assert listener.address.startswith("127.0.0.1:")
        with httpx.Client(transport=HostTransport(listener.address)) as c:
            r = c.get(f"{fake.url}/{h}")
            assert (r.status_code, r.content) == (200, b"hello")
            assert r.headers["content-length"] == "5"
            assert r.headers["access-control-allow-origin"] == "*"
            assert r.headers["content-disposition"] == f'inline; filename="{h}"'
            assert str(r.request.url) == f"{fake.url}/{h}"
            head = c.head(f"{fake.url}/{h}")
            assert (head.status_code, head.content, head.headers["content-length"]) == (200, b"", "5")
            assert c.get("http://evil.example/").status_code == 403
            with pytest.raises(httpx.TransportError):
                c.get(f"{fake.url}/{cut}")
            site = f"http://{hash_to_base32(bundle.hash)}.{fake.host}/unsized.txt"
            with pytest.raises(httpx.TransportError):
                c.get(site)
            # The connection the cut body closed does not poison the next request.
            assert c.get(f"{fake.url}/{h}").content == b"hello"
        with (
            Client(fake.url, transport=HostTransport(listener.address)) as client_,
            pytest.raises(DecdnError) as info,
        ):
            client_.get(f"decdn:{cut}").read()
        assert info.value.code == "TRUNCATED"
    finally:
        listener.close()


@pytest.mark.anyio
async def test_listen_serves_an_async_client() -> None:
    fake = FakeGateway("loopback")
    h = fake.add_blob("hello")
    listener = fake.listen()
    try:
        async with httpx.AsyncClient(transport=AsyncHostTransport(listener.address)) as c:
            r = await c.get(f"{fake.url}/{h}")
            assert r.status_code == 402
            fake.grant()
            r = await c.get(f"{fake.url}/{h}")
            assert (r.status_code, r.content) == (200, b"hello")
            r = await c.put(
                f"{fake.url}/_capability", content=json.dumps({"capability": "nope", "scope": "any"})
            )
            assert r.status_code == 422
    finally:
        listener.close()
