import json

import httpx
import pytest

from decdn.base32 import hash_to_base32
from decdn.pointer import PointerDomain
from tests.conftest import fixture_json
from tests.fake_gateway import PROOF_BYTES, FakeGateway, checksum

EXPOSED = (
    "Content-Length, Content-Range, Accept-Ranges, Content-Type, Content-Disposition, ETag, Retry-After, Link"
)
CSP = "default-src 'self' 'unsafe-inline' 'unsafe-eval' data: blob:; connect-src 'self'"
TOKEN = "ab" * 32


def client(fake: FakeGateway, *, follow: bool = False) -> httpx.Client:
    return httpx.Client(transport=fake.transport(), follow_redirects=follow)


def test_checksum_spells_addresses_eip55() -> None:
    assert (
        checksum("0x5aaeb6053f3e94c9b9a09f33669435e7ef1beaed") == "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"
    )
    assert checksum(fixture_json("pointer")["address"].lower()) == fixture_json("pointer")["address"]


def test_public_blob_200_with_cors() -> None:
    fake = FakeGateway("public")
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
        assert c.options(f"{fake.url}/_delegate").text == "a public gateway has no API\n"
    assert [s.method for s in fake.seen] == ["GET", "OPTIONS", "OPTIONS", "OPTIONS"]
    assert fake.seen[0].url == f"{fake.url}/{h}"


def test_public_bundle_redirects_to_site() -> None:
    fake = FakeGateway("public")
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
    fake = FakeGateway("public")
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
    loopback.scopes.append("*")
    with client(loopback) as c:
        bare = c.get(
            f"{loopback.url}/{m.hash}/page.html", headers={"x-decdn-token": TOKEN, "x-decdn-host": "a.test"}
        )
        assert (bare.status_code, bare.text) == (200, "<p>x</p>")
        assert bare.headers["content-type"] == "text/plain; charset=utf-8"
        assert "content-security-policy" not in bare.headers


def test_site_rewrites_html_and_sets_csp() -> None:
    fake = FakeGateway("public")
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
    fake = FakeGateway("public")
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
    fake = FakeGateway("public")
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


def test_unknown_host_403() -> None:
    fake = FakeGateway("public")
    with client(fake) as c:
        r = c.get("http://other.test/")
        assert (r.status_code, r.text) == (403, "unknown Host\n")
        assert r.headers["content-type"] == "text/plain; charset=utf-8"
        r = c.get(f"http://9.{fake.host}/")
        assert (r.status_code, r.text) == (404, "namespace 9 has no pointer on this gateway\n")
        assert c.get(f"{fake.url}/").text == "<h1>decdn gateway</h1>"
        assert c.post(f"{fake.url}/x").status_code == 405
        assert c.get(f"{fake.url}/nothex").status_code == 400


def test_loopback_needs_token_401() -> None:
    fake = FakeGateway("loopback")
    h = fake.add_blob("hello")
    m = fake.add_bundle({"a.txt": "x"})
    site = f"http://{hash_to_base32(m.hash)}.localhost:8484"
    with client(fake) as c:
        assert fake.url == "http://127.0.0.1:8484"
        r = c.get(f"{fake.url}/{h}")
        assert (r.status_code, r.text) == (401, "token required\n")
        r = c.get(f"{fake.url}/{h}", headers={"x-decdn-token": TOKEN})
        assert (r.status_code, r.text) == (401, "page host required\n")
        r = c.get(f"{site}/a.txt")
        assert (r.status_code, r.text) == (401, "token required outside a browser\n")
        assert c.get(f"{site}/a.txt", headers={"sec-fetch-site": "cross-site"}).status_code == 403
        assert c.get(f"{site}/a.txt", headers={"sec-fetch-site": "same-origin"}).text == "x"
        assert c.get("http://localhost:8484/").text == "<h1>decdn gateway</h1>"
        assert "access-control-allow-origin" not in r.headers


def test_loopback_uncovered_host_402_text() -> None:
    fake = FakeGateway("loopback")
    h = fake.add_blob("hello")
    headers = {"x-decdn-token": TOKEN, "x-decdn-host": "App.Example.com"}
    with client(fake) as c:
        r = c.get(f"{fake.url}/{h}", headers=headers)
        assert (r.status_code, r.text) == (402, "no capability covers app.example.com\n")
        assert r.headers["content-type"] == "text/plain; charset=utf-8"
        fake.scopes.append("*.example.com")
        r = c.get(f"{fake.url}/{h}", headers=headers)
        assert (r.status_code, r.content) == (200, b"hello")
        assert fake.allowance == 2**53 - 1


def test_allowance_402_json_and_retry_after() -> None:
    fake = FakeGateway("public", allowance_bytes=PROOF_BYTES + 10)
    h = fake.add_blob("0123456789")
    with client(fake) as c:
        assert c.get(f"{fake.url}/{h}").status_code == 200
        assert fake.allowance == 0
        r = c.get(f"{fake.url}/{h}")
        assert r.status_code == 402
        body = r.json()
        assert body["error"] == "the gateway's allowance is spent for today"
        assert body["uri"] == f"decdn:{h}"
        assert 0 < body["retry_after_secs"] <= 86400
        assert r.headers["retry-after"] == str(body["retry_after_secs"])
        assert r.headers["link"] == f'<decdn:{h}>; rel="alternate"'
        assert r.headers["content-type"] == "application/json"
    small = FakeGateway("public", allowance_bytes=PROOF_BYTES + 5)
    h2 = small.add_blob("0123456789")
    with client(small) as c:
        r = c.get(f"{small.url}/{h2}")
        assert r.status_code == 402
        assert r.json()["retry_after_secs"] == 0
        assert r.headers["retry-after"] == "0"
    unfunded = FakeGateway("public")
    h3 = unfunded.add_blob("x")
    unfunded.unfunded.add(h3)
    with client(unfunded) as c:
        r = c.get(f"{unfunded.url}/{h3}")
        assert r.status_code == 503
        assert r.json() == {
            "error": "the gateway's pool cannot pay right now",
            "uri": f"decdn:{h3}",
            "retry_after_secs": 60,
        }
        assert r.headers["retry-after"] == "60"


def test_delegate_register_list_409s() -> None:
    cap = fixture_json("capability")
    signer = cap["signer"]
    fake = FakeGateway("loopback")
    auth = {"x-decdn-token": TOKEN}
    body = json.dumps({"capability": cap["token"], "scope": "example.com"})
    with client(fake) as c:
        assert c.get(f"{fake.url}/_delegate").status_code == 403
        r = c.get(f"{fake.url}/_delegate", headers=auth)
        address = r.json()["address"]
        assert address == checksum(address.lower())
        assert address.lower() in fake.delegates
        r = c.put(f"{fake.url}/_capability", headers=auth, content=body)
        assert (r.status_code, r.text) == (
            409,
            "the capability names no delegate this gateway is holding: get one from GET /_delegate\n",
        )
        fake.delegates[signer] = None
        r = c.put(f"{fake.url}/_capability", headers=auth, content=body)
        assert r.status_code == 201
        assert r.text == f'{{"signer":"{checksum(signer)}","scope":"example.com","expiry":1900000000}}'
        assert fake.scopes == ["example.com"]
        r = c.put(f"{fake.url}/_capability", headers=auth, content=body)
        assert (r.status_code, r.text) == (
            409,
            "that delegate already holds a capability: get a new one from GET /_delegate\n",
        )
        listed = c.get(f"{fake.url}/_capability", headers=auth)
        assert listed.text == (
            f'[{{"signer":"{checksum(signer)}","pool":"{cap["poolId"]}","scope":"example.com",'
            f'"origin":null,"cap":10000000,"expiry":1900000000,"spent":false}}]'
        )
        assert c.get(f"{fake.url}/_capability").status_code == 401
        page = {"origin": "http://app.example.com"}
        r = c.put(f"{fake.url}/_capability", headers=page, content=body)
        assert (r.status_code, r.text) == (403, "a page registers only a scope that covers itself\n")
        assert r.headers["access-control-allow-origin"] == "http://app.example.com"
        assert r.headers["vary"] == "Origin"
        r = c.put(f"{fake.url}/_capability", headers=auth, content='{"scope":"x"}')
        assert (r.status_code, r.text) == (422, 'expected {"capability", "scope"}\n')
        bad_scope = json.dumps({"capability": cap["token"], "scope": "Bad_Scope"})
        assert (
            c.put(f"{fake.url}/_capability", headers=auth, content=bad_scope).text
            == "invalid scope Bad_Scope\n"
        )
        preflight = c.options(f"{fake.url}/_capability", headers=page)
        assert preflight.status_code == 204
        assert preflight.headers["access-control-allow-private-network"] == "true"
        assert preflight.headers["access-control-allow-methods"] == "GET, PUT"
        assert c.options(f"{fake.url}/x", headers=page).text == "no cross-origin access\n"


def test_put_pointer_201_409_403() -> None:
    p = fixture_json("pointer")
    body = json.dumps(
        {"namespace": p["namespace"], "root": f"0x{p['root']}", "seq": p["seq"], "signature": p["signature"]}
    )
    fake = FakeGateway("public")
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
    fake = FakeGateway("public")
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
    fake = FakeGateway("public")
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
