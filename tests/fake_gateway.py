"""An in-process fake of `decdn gateway`'s HTTP contract.

See https://github.com/decdn/decdn/blob/main/crates/cli/src/commands/gateway.rs and
https://github.com/decdn/decdn/blob/main/adr/appendix-gateway.md: the same hosts, statuses,
headers and JSON bodies, over content held in memory. `handle` answers an `httpx.Request`;
`transport` is an `httpx` transport that answers from the fake, for the SDK's clients. The contract
suite runs the same tests against this fake and against a real gateway, so a drift between the two
fails CI.
"""

import dataclasses
import json
import math
import re
import secrets
import socket
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from Crypto.Hash import keccak

from decdn.base32 import base32_to_hash, hash_to_base32
from decdn.capability import parse_capability
from decdn.errors import DecdnError
from decdn.hashing import blake3_hex
from decdn.manifest import BundleManifest, parse_manifest
from decdn.pointer import Pointer, PointerDomain, recover_pointer_signer
from decdn.uri import DecdnUri, format_uri, parse_uri

FakeMode = Literal["public", "loopback"]


@dataclass
class Seen:
    """One request the fake answered."""

    method: str
    url: str
    headers: httpx.Headers


EXPOSED = (
    "Content-Length, Content-Range, Accept-Ranges, Content-Type, Content-Disposition, ETag, Retry-After, Link"
)
REWRITTEN = re.compile(r"\.(html?|hxt|shtml|css)\Z", re.IGNORECASE | re.ASCII)
TYPES: dict[str, str] = {
    "html": "text/html",
    "htm": "text/html",
    "css": "text/css",
    "txt": "text/plain",
    "json": "application/json",
    "js": "text/javascript",
    "bin": "application/octet-stream",
    "png": "image/png",
}
ACTIVE = (
    "application/xhtml+xml",
    "image/svg+xml",
    "application/xml",
    "application/javascript",
)
"""Types a browser runs or renders as a document; the bare host serves them as text."""

_EXT = re.compile(r"\.([^./]+)\Z")
_RANGE = re.compile(r"bytes=([0-9]*)-([0-9]*)")
_NAMESPACE = re.compile(r"[1-9][0-9]*")
_SCOPE = re.compile(r"\*|(\*\.)?[a-z0-9-]+(\.[a-z0-9-]+)*")
_POINTER_BODY = re.compile(
    r'\s*\{\s*"namespace"\s*:\s*([0-9]+)\s*,\s*"root"\s*:\s*"0x([0-9a-fA-F]{64})"\s*,'
    r'\s*"seq"\s*:\s*([0-9]+)\s*,\s*"signature"\s*:\s*"(0x[0-9a-fA-F]*)"\s*\}\s*'
)
_DECDN_REF = re.compile(r"decdn:([0-9a-fA-F]{64})")
_CSP = "default-src 'self' 'unsafe-inline' 'unsafe-eval' data: blob:; connect-src 'self'"


def _ext_type(name: str) -> str:
    m = _EXT.search(name)
    ext = m.group(1).lower() if m is not None else ""
    return TYPES.get(ext, "application/octet-stream")


def _site_type(name: str) -> str:
    """Return the type a site serves `name` as: its own, text in UTF-8."""
    t = _ext_type(name)
    return f"{t}; charset=utf-8" if t.startswith("text/") else t


def _bare_type(name: str) -> str:
    """Return the type the bare host serves `name` as: every text or active type as plain text."""
    t = _ext_type(name)
    return "text/plain; charset=utf-8" if t.startswith("text/") or t in ACTIVE else t


class _Body(httpx.SyncByteStream, httpx.AsyncByteStream):
    """A response body of `chunks`; with `cut`, it fails after them, as a body whose pull aborts does."""

    def __init__(self, chunks: Sequence[bytes], cut: bool = False) -> None:
        self._chunks = tuple(chunks)
        self._cut = cut

    def __iter__(self) -> Iterator[bytes]:
        yield from self._chunks
        if self._cut:
            raise httpx.ReadError("the pull failed")

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk
        if self._cut:
            raise httpx.ReadError("the pull failed")


def _response(status: int, body: bytes | None, headers: Mapping[str, str]) -> httpx.Response:
    """A response with exactly `headers`: `httpx` adds no length for a streamed body."""
    if body is None:
        return httpx.Response(status, headers=dict(headers))
    return httpx.Response(status, headers=dict(headers), stream=_Body((body,)))


def _text(status: int, body: str, headers: Mapping[str, str] | None = None) -> httpx.Response:
    return _response(
        status,
        None if status in (204, 304) else body.encode(),
        {"content-type": "text/plain; charset=utf-8", **(headers or {})},
    )


def _stringify(value: Any) -> str:
    """`JSON.stringify`: compact, non-ASCII as is."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _json(status: int, body: Any, headers: Mapping[str, str] | None = None) -> httpx.Response:
    return _response(
        status, _stringify(body).encode(), {"content-type": "application/json", **(headers or {})}
    )


def _window(range_: str | None, size: int) -> tuple[int, int] | Literal["unsatisfiable"] | None:
    """A `Range` header naming one window: `(offset, length)`, `"unsatisfiable"`, or `None` to ignore it."""
    m = None if range_ is None else _RANGE.fullmatch(range_.strip())
    if m is None:
        return None
    a, b = m.group(1), m.group(2)
    if a == "" and b == "":
        return None
    if a == "":
        n = min(int(b), size)
        return "unsatisfiable" if n == 0 else (size - n, n)
    start = int(a)
    if start >= size:
        return "unsatisfiable"
    end = size - 1 if b == "" else min(int(b), size - 1)
    if end < start:
        return None
    return (start, end - start + 1)


def _group_span(window: tuple[int, int], size: int) -> int:
    """The bytes a pull of `window` pays for: the window widened to whole chunk groups, within `size`."""
    offset, length = window
    start = offset // CHUNK_GROUP_BYTES * CHUNK_GROUP_BYTES
    end = min(-(-(offset + length) // CHUNK_GROUP_BYTES) * CHUNK_GROUP_BYTES, size)
    return end - start


def _js_length(s: str) -> int:
    """The length of `s` in UTF-16 code units, as JavaScript counts a string."""
    return len(s.encode("utf-16-le", errors="surrogatepass")) // 2


MAX_BODY = 64 * 1024
"""The largest API body the gateway reads (64 KiB)."""
CHUNK_GROUP_BYTES = 16 * 1024
"""The unit a pull pays for: a chunk group (16 KiB)."""
PROOF_BYTES = CHUNK_GROUP_BYTES
"""What a raw blob's size proof spends of the allowance: one chunk group."""


@dataclass
class Registered:
    """A capability registered for a loopback delegate."""

    signer: str
    pool: str
    scope: str
    origin: str | None
    cap: int
    expiry: int


def checksum(address: str) -> str:
    """Return the EIP-55 spelling of a lowercase `0x` address, as the gateway writes addresses."""
    hex_ = address[2:].lower()
    digest = keccak.new(digest_bits=256, data=hex_.encode()).digest()
    out = "0x"
    for i, c in enumerate(hex_):
        byte = digest[i >> 1] if i >> 1 < len(digest) else 0
        nibble = (byte >> (4 if i % 2 == 0 else 0)) & 0xF
        out += c.upper() if nibble >= 8 else c
    return out


@dataclass(frozen=True)
class Listener:
    """A fake served over HTTP: `address` is `127.0.0.1:<port>`; `close` stops the server."""

    address: str
    close: Callable[[], None]


@dataclass(frozen=True)
class _SiteRef:
    """The bundle a site host names, with the namespace it names and the pointer it resolved through."""

    hash: str
    namespace: int | None = None
    pointer: int | None = None


class FakeGateway:
    """An in-memory `decdn gateway`, public or loopback."""

    def __init__(
        self,
        mode: FakeMode,
        *,
        host: str | None = None,
        allowance_bytes: int | None = None,
        token: str | None = None,
    ) -> None:
        """Make a fake.

        `host` is `host[:port]` of the bare host, by default `decdn.test` (public) or
        `127.0.0.1:8484` (loopback). `allowance_bytes` is the bytes the public gateway delivers
        before it answers 402. `token` is the loopback token.
        """
        self.mode: FakeMode = mode
        # `host[:port]` of the bare host; settable so a listener can name the port it got.
        self.host: str = (
            host if host is not None else ("decdn.test" if mode == "public" else "127.0.0.1:8484")
        )
        self.token: str = token if token is not None else "ab" * 32
        self.blobs: dict[str, bytes] = {}
        # The raw blobs whose size a proof here proved.
        self._proven: set[str] = set()
        # Hashes whose pull the pool cannot pay: 503.
        self.unfunded: set[str] = set()
        # Hashes served with one byte flipped.
        self.tampered: set[str] = set()
        # Hashes whose body aborts after its first half.
        self.truncated: set[str] = set()
        # Hashes answered 206 with their first byte, whatever the request asked: a lying gateway.
        self.partial: set[str] = set()
        # Sized hashes answered whole with 200, whatever Range the request carried.
        self.whole: set[str] = set()
        # Loopback capability scopes registered: `example.com`, `*.example.com`, `*`.
        self.scopes: list[str] = []
        # Namespace pointers: namespace id → bundle hash.
        self.pointers: dict[int, str] = {}
        self.seen: list[Seen] = []
        # Loopback delegates by lowercase address, with the capability registered for each.
        self.delegates: dict[str, Registered | None] = {}
        # The deployment pointers are verified on (public).
        self.pointer_domain: PointerDomain | None = None
        # Namespace owners on that deployment, lowercase.
        self.owners: dict[int, str] = {}
        # The highest stored `seq` per namespace and signer.
        self._high_water: dict[str, int] = {}
        self.allowance: int = allowance_bytes if allowance_bytes is not None else 2**53 - 1

    @property
    def url(self) -> str:
        """The bare origin, `http://<host>`."""
        return f"http://{self.host}"

    @property
    def _suffix(self) -> str:
        """The suffix under which site hosts live."""
        if self.mode == "public":
            return self.host
        return f"localhost:{self.host.split(':')[-1]}"

    def add_blob(self, data: bytes | str) -> str:
        """Store `data`, returning its hash."""
        b = data.encode() if isinstance(data, str) else data
        hash_ = blake3_hex(b)
        self.blobs[hash_] = b
        return hash_

    def add_bundle(self, files: dict[str, bytes | str], unsized: Sequence[str] = ()) -> BundleManifest:
        """Store a bundle of `files`, returning its manifest. `unsized` paths carry no `size`."""
        entries: list[dict[str, Any]] = []
        for path, data in files.items():
            hash_ = self.add_blob(data)
            size = len(self.blobs[hash_])
            entries.append(
                {"path": path, "hash": f"b3:{hash_}"}
                if path in unsized
                else {"path": path, "hash": f"b3:{hash_}", "size": size}
            )
        hash_ = self.add_blob(_stringify({"version": 1, "entries": entries}))
        return parse_manifest(self.blobs[hash_])

    def transport(self) -> httpx.MockTransport:
        """An `httpx` transport that answers from this fake in-process, for `Client` and `AsyncClient`.

        `httpx.MockTransport` reads the request body (`read`/`aread`) before it calls `handle`.
        """
        return httpx.MockTransport(self.handle)

    def listen(self) -> Listener:
        """Serve this fake over HTTP on `127.0.0.1` at a free port, from a daemon thread.

        A request's URL is `http://<Host header><path>`, so one listener answers the bare host and
        every site host. A body whose pull fails (a `truncated` hash) ends with the connection
        closed mid-way, as the gateway aborts it.
        """
        server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def close() -> None:
            server.shutdown()
            server.server_close()

        return Listener(f"127.0.0.1:{server.server_address[1]}", close)

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Answer `request`, as the gateway would over HTTP."""
        self.seen.append(Seen(request.method, str(request.url), request.headers))
        response = self._route(request)
        if self.mode == "public":
            response.headers["access-control-allow-origin"] = "*"
            response.headers["access-control-expose-headers"] = EXPOSED
        return response

    def _route(self, req: httpx.Request) -> httpx.Response:
        url = req.url
        host = url.host if url.port is None else f"{url.host}:{url.port}"
        suffix = self._suffix
        labels = host[: -len(suffix) - 1] if host.endswith(f".{suffix}") else None
        bare = host == self.host or (self.mode == "loopback" and host == f"localhost:{suffix.split(':')[1]}")
        site: _SiteRef | None = None
        if labels is not None:
            parts = labels.split(".")
            hash_ = base32_to_hash(parts[-1]) if len(parts) <= 2 else None
            ns = parts[0] if len(parts) == 2 or (len(parts) == 1 and hash_ is None) else None
            ns_ok = ns is None or _NAMESPACE.fullmatch(ns) is not None
            if hash_ is not None and ns_ok:
                site = _SiteRef(hash_) if ns is None else _SiteRef(hash_, namespace=int(ns))
            elif self.mode == "public" and len(parts) == 1 and ns is not None and ns_ok:
                root = self.pointers.get(int(ns))
                if root is None:
                    return _text(404, f"namespace {ns} has no pointer on this gateway\n")
                site = _SiteRef(root, namespace=int(ns), pointer=int(ns))
        if site is None and not bare:
            return _text(403, "unknown Host\n")
        path, search = _path_and_search(url)
        if self.mode == "public" and req.method == "OPTIONS":
            return self._preflight(path, site is None)
        if site is not None:
            return self._site(req, path, search, site)
        return self._bare(req, path, search)

    def _preflight(self, path: str, bare: bool) -> httpx.Response:
        api = path in ("/_delegate", "/_capability", "/_pointer")
        if api and not bare:
            return _text(404, "the gateway API answers on the gateway origin only\n")
        if path in ("/_delegate", "/_capability"):
            return _text(404, "a public gateway has no API\n")
        methods, headers = ("PUT", "content-type") if path == "/_pointer" else ("GET, HEAD", "range")
        return _text(
            204,
            "",
            {
                "access-control-allow-methods": methods,
                "access-control-allow-headers": headers,
                "access-control-max-age": "600",
            },
        )

    def _authorized(self, req: httpx.Request) -> bool:
        token: str | None = req.headers.get("x-decdn-token")
        return token == self.token

    def _covers(self, host: str) -> bool:
        name = host.split(":")[0]
        return any(
            s == "*" or s == name or (s.startswith("*.") and (name == s[2:] or name.endswith(s[1:])))
            for s in self.scopes
        )

    def _bare(self, req: httpx.Request, path: str, search: str) -> httpx.Response:
        api = self._api(req, path)
        if api is not None:
            return api
        if self.mode == "public":
            if path in ("/_delegate", "/_capability"):
                return _text(404, "a public gateway has no API\n")
            if req.method == "HEAD" and path in ("/", "/favicon.ico"):
                return _text(200, "")
        if req.method == "GET" and path == "/":
            return _response(200, b"<h1>decdn gateway</h1>", {"content-type": "text/html; charset=utf-8"})
        if req.method == "HEAD" and self.mode == "loopback":
            return _text(405, "", {"allow": "GET"})
        if req.method not in ("GET", "HEAD"):
            return _text(405, "method not allowed\n")
        try:
            uri = parse_uri(f"decdn:{path[1:]}{search}")
        except DecdnError as e:
            return _text(400, f"{e}\n")
        if self.mode == "public" or not self._authorized(req):
            if uri.kind != "blob":
                return _response(302, None, {"location": self._site_location(uri)})
            if self.mode == "loopback":
                return _text(401, "token required\n")
            if req.method == "HEAD":
                return self._head_blob(uri)
            return self._serve(uri, "bare", req.headers.get("range"))
        page_host = req.headers.get("x-decdn-host")
        if page_host is None:
            return _text(401, "page host required\n")
        if not self._covers(page_host.lower()):
            return _text(402, f"no capability covers {page_host.lower()}\n")
        return self._serve(uri, "bare", req.headers.get("range"))

    def _api(self, req: httpx.Request, path: str) -> httpx.Response | None:
        """The gateway API on the bare host, or `None` for a content path."""
        method = req.method
        if self.mode == "public":
            if path == "/_pointer" and method == "PUT":
                return self._put_pointer(req)
            return None
        origin = req.headers.get("origin")

        def cors(r: httpx.Response) -> httpx.Response:
            if origin is not None:
                r.headers["access-control-allow-origin"] = origin
                r.headers["vary"] = "Origin"
            return r

        if method == "OPTIONS":
            if path not in ("/_delegate", "/_capability"):
                return _text(403, "no cross-origin access\n")
            if origin is None:
                return _text(403, "no Origin\n")
            return cors(
                _text(
                    204,
                    "",
                    {
                        "access-control-allow-methods": "GET, PUT",
                        "access-control-allow-headers": "content-type",
                        "access-control-max-age": "600",
                        "access-control-allow-private-network": "true",
                    },
                )
            )
        page = _hostname(origin) if origin is not None and origin != "null" else None
        authorized = self._authorized(req)
        if path == "/_delegate" and method == "GET":
            if not authorized and page is None:
                return cors(_text(403, "the token or a page origin is required\n"))
            address = f"0x{secrets.token_hex(20)}"
            self.delegates[address] = None
            return cors(_json(200, {"address": checksum(address)}))
        if path == "/_capability" and method == "PUT":
            if not authorized and page is None:
                return cors(_text(403, "the token or a page origin is required\n"))
            return cors(self._register(req, None if authorized else page, origin))
        if path == "/_capability" and method == "GET":
            if not authorized:
                return _text(401, "token required\n")
            listed = [r for r in self.delegates.values() if r is not None]
            body = ",".join(
                f'{{"signer":"{checksum(r.signer)}","pool":"{r.pool}","scope":"{r.scope}",'
                f'"origin":{"null" if r.origin is None else _stringify(r.origin)},'
                f'"cap":{r.cap},"expiry":{r.expiry},"spent":false}}'
                for r in listed
            )
            return _response(200, f"[{body}]".encode(), {"content-type": "application/json"})
        if path == "/_pointer" and method == "PUT":
            return _text(404, "a loopback gateway holds no namespace pointers\n")
        return None

    def _register(self, req: httpx.Request, page: str | None, origin: str | None) -> httpx.Response:
        body = _body_text(req)
        if _js_length(body) > MAX_BODY:
            return _text(413, "body too large\n")
        try:
            request = json.loads(body, parse_constant=_no_constant)
        except (ValueError, RecursionError) as e:
            return _text(422, f'expected {{"capability", "scope"}}: {e}\n')
        capability = request.get("capability") if isinstance(request, dict) else None
        scope = request.get("scope") if isinstance(request, dict) else None
        if not isinstance(capability, str) or not isinstance(scope, str):
            return _text(422, 'expected {"capability", "scope"}\n')
        if _SCOPE.fullmatch(scope) is None:
            return _text(422, f"invalid scope {scope}\n")
        if page is not None and scope != page and scope != f"*.{page}":
            return _text(403, "a page registers only a scope that covers itself\n")
        try:
            grant = parse_capability(capability.strip())
        except DecdnError as e:
            return _text(422, f"{e}\n")
        if grant.expiry <= int(time.time()):
            return _text(422, "the capability has expired\n")
        signer = grant.signer.lower()
        if signer not in self.delegates:
            return _text(
                409,
                "the capability names no delegate this gateway is holding: get one from GET /_delegate\n",
            )
        if self.delegates[signer] is not None:
            return _text(
                409,
                "that delegate already holds a capability: get a new one from GET /_delegate\n",
            )
        self.delegates[signer] = Registered(
            signer=signer,
            pool=grant.pool_id,
            scope=scope,
            origin=None if page is None else origin,
            cap=grant.spending_cap,
            expiry=grant.expiry,
        )
        self.scopes.append(scope)
        return _response(
            201,
            f'{{"signer":"{checksum(signer)}","scope":"{scope}","expiry":{grant.expiry}}}'.encode(),
            {"content-type": "application/json"},
        )

    def _put_pointer(self, req: httpx.Request) -> httpx.Response:
        body = _body_text(req)
        if _js_length(body) > MAX_BODY:
            return _text(413, "body too large\n")
        m = _POINTER_BODY.fullmatch(body)
        if m is None:
            return _text(422, 'expected {"namespace","root","seq","signature"}\n')
        ns_text, root_hex, seq_text, signature = m.groups()
        namespace = int(ns_text)
        seq = int(seq_text)
        root = root_hex.lower()
        if self.pointer_domain is None:
            return _text(502, "no chain\n")
        try:
            signer = recover_pointer_signer(self.pointer_domain, Pointer(namespace, root, seq), signature)
        except Exception:
            return _text(422, "the pointer signature is malformed\n")
        key = f"{namespace}:{signer}"
        held = self._high_water.get(key)
        if held is not None and seq <= held:
            return _response(
                409,
                f'{{"error":"the gateway holds a pointer at or above this seq","seq":{held}}}'.encode(),
                {"content-type": "application/json"},
            )
        owner = self.owners.get(namespace)
        if owner is None:
            return _text(404, f"namespace {namespace} does not exist\n")
        if owner != signer:
            return _text(
                403,
                f"the pointer is signed by {signer}; namespace {namespace} is owned by {owner}\n",
            )
        self._high_water[key] = seq
        self.pointers[namespace] = root
        return _response(
            201,
            f'{{"namespace":{namespace},"root":"{root}","seq":{seq},'
            f'"url":"http://{namespace}.{self.host}/"}}'.encode(),
            {"content-type": "application/json"},
        )

    def _site_location(self, uri: DecdnUri) -> str:
        label = hash_to_base32(uri.hash)
        suffix = self._suffix
        host = f"{label}.{suffix}" if uri.namespace is None else f"{uri.namespace}.{label}.{suffix}"
        path = format_uri(DecdnUri(hash=uri.hash, kind=uri.kind, path=uri.path))[len("decdn:") + 64 :]
        return f"http://{host}{path}"

    def _site(self, req: httpx.Request, path: str, search: str, site: _SiteRef) -> httpx.Response:
        if path in ("/_delegate", "/_capability", "/_pointer"):
            return _text(404, "the gateway API answers on the gateway origin only\n")
        if req.method == "HEAD" and self.mode == "loopback":
            return _text(405, "", {"allow": "GET"})
        if req.method not in ("GET", "HEAD"):
            return _text(405, "method not allowed\n")
        if self.mode == "loopback" and not self._authorized(req):
            fetch_site = req.headers.get("sec-fetch-site")
            if fetch_site is None:
                return _text(401, "token required outside a browser\n")
            if fetch_site not in ("same-origin", "none"):
                return _text(403, "a site answers only its own pages and top-level navigation\n")
        if path.startswith("/_decdn/"):
            try:
                uri = parse_uri(f"decdn:{path[len('/_decdn/') :]}{search}")
            except DecdnError as e:
                return _text(400, f"{e}\n")
            response = (
                self._head_blob(uri)
                if req.method == "HEAD" and uri.kind == "blob"
                else self._serve(uri, "site", req.headers.get("range"))
            )
        else:
            entry = f"{path}index.html" if len(path) > 1 and path.endswith("/") else path
            try:
                uri = parse_uri(f"decdn:{site.hash}{entry}")
            except DecdnError as e:
                return _text(404, f"{e}\n")
            if site.namespace is not None:
                uri = dataclasses.replace(uri, namespace=site.namespace)
            response = (
                self._head_site(uri)
                if req.method == "HEAD"
                else self._serve(uri, "site", req.headers.get("range"))
            )
        response.headers["content-security-policy"] = _CSP
        return response

    def _manifest_of(self, hash_: str) -> BundleManifest | httpx.Response:
        data = self.blobs.get(hash_)
        if data is None:
            return _text(404, "not found\n")
        try:
            return parse_manifest(data)
        except DecdnError as e:
            return _text(404, f"{e}\n")

    def _head_site(self, uri: DecdnUri) -> httpx.Response:
        manifest = self._manifest_of(uri.hash)
        if isinstance(manifest, httpx.Response):
            return manifest
        path = "index.html" if uri.kind == "bundle" else (uri.path or "")
        entry = manifest.entry(path)
        if entry is None:
            return _text(404, f"no entry {path}\n")
        headers = {
            "content-type": _site_type(path),
            "etag": f'"{entry.hash}"',
            "x-content-type-options": "nosniff",
        }
        if entry.size is not None and not REWRITTEN.search(path):
            headers["content-length"] = str(entry.size)
            headers["accept-ranges"] = "bytes"
        return _response(200, None, headers)

    def _refusal(
        self, status: Literal[402, 503], uri: DecdnUri, retry_after: int, why: str
    ) -> httpx.Response:
        canonical = format_uri(uri)
        return _json(
            status,
            {"error": why, "uri": canonical, "retry_after_secs": retry_after},
            {"retry-after": str(retry_after), "link": f'<{canonical}>; rel="alternate"'},
        )

    def _seconds_to_midnight(self) -> int:
        now = time.time()
        return math.ceil(86400 - (now % 86400))

    def _head_blob(self, uri: DecdnUri) -> httpx.Response:
        """`HEAD` of a raw blob on a public gateway.

        It is free for a blob whose size is proven, else refused or failed as its `GET` would be
        before it pays for the size proof.
        """
        headers = {
            "content-type": "application/octet-stream",
            "etag": f'"{uri.hash}"',
            "x-content-type-options": "nosniff",
        }
        data = self.blobs.get(uri.hash)
        # The empty root is empty, with no proof.
        if uri.hash not in self._proven and (data is None or len(data) != 0):
            if self.allowance <= 0:
                return self._refusal(
                    402, uri, self._seconds_to_midnight(), "the gateway's allowance is spent for today"
                )
            if uri.hash in self.unfunded:
                return self._refusal(503, uri, 60, "the gateway's pool cannot pay right now")
            if data is None:
                return _text(404, "not found\n")
            self._prove(uri.hash, len(data))
        if data is not None:
            headers["content-length"] = str(len(data))
            headers["accept-ranges"] = "bytes"
        return _response(200, None, headers)

    def _prove(self, hash_: str, size: int) -> None:
        """Pay for the size proof of the raw blob `hash_` of `size` bytes, once; the empty root needs none."""
        if hash_ in self._proven:
            return
        if size > 0:
            self._charge(PROOF_BYTES)
        self._proven.add(hash_)

    def _serve(self, uri: DecdnUri, present: Literal["bare", "site"], range_: str | None) -> httpx.Response:
        if self.mode == "public" and self.allowance <= 0:
            return self._refusal(
                402, uri, self._seconds_to_midnight(), "the gateway's allowance is spent for today"
            )
        if uri.hash in self.unfunded:
            return self._refusal(503, uri, 60, "the gateway's pool cannot pay right now")
        if uri.kind == "blob":
            # The gateway proves a raw blob's size before it answers (one chunk group of the
            # allowance, once), so a raw blob answers as a sized entry does.
            data = self.blobs.get(uri.hash)
            if data is None:
                return _text(404, "not found\n")
            self._prove(uri.hash, len(data))
            return self._sized(
                uri,
                uri.hash,
                data,
                len(data),
                range_,
                {
                    "content-type": "application/octet-stream",
                    "content-disposition": f'inline; filename="{uri.hash}"',
                    "x-content-type-options": "nosniff",
                },
            )
        manifest = self._manifest_of(uri.hash)
        if isinstance(manifest, httpx.Response):
            return manifest
        self._charge(len(self.blobs.get(uri.hash, b"")))
        if uri.kind == "bundle":
            if present == "site" and manifest.entry("index.html") is not None:
                return self._entry(
                    dataclasses.replace(uri, kind="entry", path="index.html"), manifest, present, None
                )
            links = "".join(f'<li><a href="{e.path}">{e.path}</a></li>' for e in manifest.entries)
            return _response(200, f"<ul>{links}</ul>".encode(), {"content-type": "text/html; charset=utf-8"})
        return self._entry(uri, manifest, present, range_)

    def _entry(
        self,
        uri: DecdnUri,
        manifest: BundleManifest,
        present: Literal["bare", "site"],
        range_: str | None,
    ) -> httpx.Response:
        path = uri.path or ""
        entry = manifest.entry(path)
        if entry is None:
            return _text(404, f"no entry {path} in bundle\n")
        if entry.hash in self.unfunded:
            return self._refusal(503, uri, 60, "the gateway's pool cannot pay right now")
        data = self.blobs.get(entry.hash)
        if data is None:
            return _text(502, "the upstream pull failed\n")
        name = path.split("/")[-1]
        rewrite = present == "site" and REWRITTEN.search(path) is not None
        headers = {
            "content-type": _site_type(name) if present == "site" else _bare_type(name),
            "content-disposition": f'inline; filename="{name}"',
            "x-content-type-options": "nosniff",
        }
        if rewrite:
            out = _DECDN_REF.sub(r"/_decdn/\1", data.decode("utf-8-sig", errors="replace"))
            return self._body(entry.hash, out.encode(), 200, headers)
        if entry.size is None:
            return self._body(entry.hash, data, 200, headers)
        return self._sized(uri, entry.hash, data, entry.size, range_, headers)

    def _sized(
        self,
        uri: DecdnUri,
        hash_: str,
        data: bytes,
        size: int,
        range_: str | None,
        headers: dict[str, str],
    ) -> httpx.Response:
        """The answer for `data` of known `size`.

        One `range_` answers 206, or 416 past the end; else the whole answers with its length. On a
        public gateway it is refused with 402 when what it pays for (the whole, or the window widened
        to chunk groups) does not fit what is left of the allowance, and it is charged that.
        """
        headers["accept-ranges"] = "bytes"
        w = None if hash_ in self.whole else _window(range_, size)
        if w == "unsatisfiable":
            return _text(416, "range not satisfiable\n", {"content-range": f"bytes */{size}"})
        billable = size if w is None else _group_span(w, size)
        if self.mode == "public" and billable > self.allowance:
            return self._refusal(
                402, uri, 0, "the response does not fit what is left of the gateway's allowance today"
            )
        # The pull pays for the aligned span; the body charges the window.
        if w is not None:
            self._charge(billable - w[1])
        if w is None:
            headers["content-length"] = str(size)
            return self._body(hash_, data, 200, headers)
        offset, length = w
        headers["content-length"] = str(length)
        headers["content-range"] = f"bytes {offset}-{offset + length - 1}/{size}"
        return self._body(hash_, data[offset : offset + length], 206, headers)

    def _charge(self, n: int) -> None:
        if self.mode == "public":
            self.allowance -= n

    def _body(self, hash_: str, data: bytes, status: int, headers: dict[str, str]) -> httpx.Response:
        """A streamed body of `data`, in two chunks, honouring the fault sets for `hash_`."""
        self._charge(len(data))
        if hash_ in self.partial and status == 200 and len(data) > 1:
            return _response(206, data[:1], {**headers, "content-range": f"bytes 0-0/{len(data)}"})
        out = data
        if hash_ in self.tampered and len(data) > 0:
            out = bytes([data[0] ^ 0xFF]) + data[1:]
        half = -(-len(out) // 2)
        if hash_ in self.truncated:
            chunks: tuple[bytes, ...] = (out[:half],)
        else:
            chunks = (out[:half], out[half:]) if half < len(out) else (out[:half],)
        return httpx.Response(status, headers=headers, stream=_Body(chunks, cut=hash_ in self.truncated))


def _handler(fake: FakeGateway) -> type[BaseHTTPRequestHandler]:
    """A request handler class that answers from `fake` over HTTP/1.1."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            pass

        def _serve(self) -> None:
            length = int(self.headers.get("content-length") or 0)
            body = self.rfile.read(length) if length > 0 else None
            host = self.headers.get("host") or fake.host
            response = fake.handle(
                httpx.Request(
                    self.command, f"http://{host}{self.path}", headers=self.headers.items(), content=body
                )
            )
            self.send_response_only(response.status_code)
            for key, value in response.headers.multi_items():
                self.send_header(key, value)
            empty = self.command == "HEAD" or response.status_code in (204, 304)
            chunked = not empty and "content-length" not in response.headers
            if chunked:
                self.send_header("transfer-encoding", "chunked")
            self.end_headers()
            if empty:
                response.close()
                return
            stream = response.stream
            assert isinstance(stream, httpx.SyncByteStream)
            try:
                for chunk in stream:
                    if chunked and chunk:
                        self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                    elif chunk:
                        self.wfile.write(chunk)
            except httpx.ReadError:
                # The gateway aborts a body whose pull fails after its first byte.
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            if chunked:
                self.wfile.write(b"0\r\n\r\n")

        do_GET = do_HEAD = do_PUT = do_POST = do_DELETE = do_OPTIONS = do_PATCH = _serve

    return Handler


def _path_and_search(url: httpx.URL) -> tuple[str, str]:
    """A URL's percent-encoded path and its `?query` (empty for none), as a web `URL` spells them."""
    path = url.raw_path.split(b"?", 1)[0].decode("ascii")
    query = url.query.decode("ascii")
    return path, f"?{query}" if query else ""


def _hostname(origin: str) -> str:
    """The hostname of an `Origin`, as `new URL(origin).hostname` reads it; a malformed one raises."""
    hostname = urlsplit(origin).hostname
    if hostname is None:
        raise ValueError(f"invalid Origin {origin}")
    return hostname


def _body_text(req: httpx.Request) -> str:
    """The request body as `Request.text()` reads it: UTF-8, a leading BOM dropped, bad bytes replaced."""
    return req.content.decode("utf-8-sig", errors="replace")


def _no_constant(name: str) -> Any:
    """Refuse `NaN` and `Infinity`, which `JSON.parse` does not read."""
    raise ValueError(f"unexpected token {name}")
