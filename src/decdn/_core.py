"""The sans-I/O core of both clients: context selection, URLs, headers, the request plan of `get`,
and what an answer's status and headers mean for its body.

Nothing here touches the network. `Client` and `AsyncClient` parse, plan with this core, send,
and hand the answer back to it.
"""

import json
import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import Literal

import httpx

from decdn._hex import is_hash
from decdn._mime import rewritten, site_type
from decdn._verify import GuardOptions
from decdn.base32 import hash_to_base32
from decdn.errors import DecdnError
from decdn.manifest import BundleManifest, ManifestEntry
from decdn.pointer import PointerDomain
from decdn.uri import SCHEME, DecdnUri, UriKind, format_uri, parse_uri

Context = Literal["public", "loopback"]
"""Where the client reaches the gateway: a public gateway, or the local one with its token."""

VerifyMode = Literal["auto"] | bool
"""`"auto"` verifies what can be verified; `True` refuses what cannot; `False` verifies nothing."""

Verified = bool | Literal["skipped"]

_DIGITS = re.compile(r"[0-9]+")
_ETAG = re.compile(r'"?([0-9a-fA-F]{64})"?')
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})
_RETRYABLE = frozenset({"PAYMENT_REQUIRED", "POOL_UNFUNDED", "RATE_LIMITED"})


@dataclass(frozen=True)
class ByteRange:
    """A byte window: `length` bytes from `offset`, or to the end without `length`."""

    offset: int
    length: int | None = None


@dataclass(frozen=True)
class HeadInfo:
    """What `head` knows about content without fetching it."""

    size: int | None = None
    hash: str | None = None
    content_type: str | None = None


@dataclass(frozen=True)
class Resolved:
    """What a URI names, once its manifest is read."""

    kind: UriKind
    # The URI's hash: of the blob, or of the bundle manifest.
    hash: str
    # The URI on the gateway's bare host.
    bare_url: str
    # The manifest entry an entry URI names.
    entry: ManifestEntry | None = None
    # The bundle or entry on its site origin.
    site_url: str | None = None


# Public through `decdn.client`, which re-exports them: reprs, docs and pickles name it.
for _cls in (ByteRange, HeadInfo, Resolved):
    _cls.__module__ = "decdn.client"


@dataclass
class Plan:
    """One request `get` sends, and what its body must hash to."""

    url: str
    hash: str | None
    length: int | None
    verified: Verified
    # The entry by its hash, unrewritten: where a verified entry comes from when its site answers
    # with a type the gateway rewrites.
    blob_url: str | None


def config(message: str) -> DecdnError:
    return DecdnError("CONFIG", message)


def _digits(value: str | None) -> int | None:
    """Return `value` as an int when it is ASCII digits only, else `None`."""
    return int(value) if value is not None and _DIGITS.fullmatch(value) else None


def parse(uri: str | DecdnUri) -> DecdnUri:
    """Return `uri` parsed; a `DecdnUri` as is."""
    return parse_uri(uri) if isinstance(uri, str) else uri


def tail(uri: DecdnUri) -> str:
    """Return `uri` without `decdn:` and the hash: `[/<path>][?ns=<id>]`, canonically encoded."""
    return format_uri(uri)[len(SCHEME) + 64 :]


def entry_of(manifest: BundleManifest, uri: DecdnUri) -> ManifestEntry:
    """Return the manifest entry `uri` names; `NOT_FOUND` when the bundle has none."""
    entry = manifest.entry(uri.path or "")
    if entry is None:
        named = json.dumps(uri.path, ensure_ascii=False)
        raise DecdnError("NOT_FOUND", f"bundle {uri.hash} has no entry {named}")
    return entry


def retry_wait(error: DecdnError, retry: bool | float) -> int | None:
    """Return the seconds to wait before the one retry `retry` asks for, or `None` for no retry.

    A float caps the wait: a longer `Retry-After` is not waited out.
    """
    if retry is False or error.code not in _RETRYABLE or error.retry_after_secs is None:
        return None
    if not isinstance(retry, bool) and error.retry_after_secs > retry:
        return None
    return error.retry_after_secs


class Core:
    """A client's configuration, its manifest cache, and everything it decides without I/O."""

    def __init__(
        self,
        gateway_url: str,
        *,
        token: str | None,
        host: str | None,
        pointer: PointerDomain | None,
        verify: VerifyMode,
        manifest_cache_size: int,
    ) -> None:
        try:
            gateway = httpx.URL(gateway_url)
        except httpx.InvalidURL as e:
            raise config(f"not a gateway URL: {gateway_url}") from e
        port = gateway.port
        if gateway.scheme == "" or gateway.raw_host == b"" or (port is not None and not 0 <= port <= 65535):
            raise config(f"not a gateway URL: {gateway_url}")
        hostname = gateway.raw_host.decode("ascii")
        self.context: Context = "loopback" if token is not None or hostname in _LOOPBACK_HOSTS else "public"
        if token is not None and (host is None or host == ""):
            raise config(
                "a loopback token needs `host`: the host whose capability pays, sent as x-decdn-host"
            )
        self.scheme = gateway.scheme
        self.port = port
        bracketed = f"[{hostname}]" if ":" in hostname else hostname
        # `host[:port]` of the gateway, the port only when it is not the scheme's default.
        self.authority = bracketed if port is None else f"{bracketed}:{port}"
        self.origin = f"{self.scheme}://{self.authority}"
        self.token = token
        self.host = host
        self.pointer = pointer
        self.verify: VerifyMode = verify
        self.cache_size = manifest_cache_size
        self.manifests: OrderedDict[str, BundleManifest] = OrderedDict()

    def cache_get(self, hash: str) -> BundleManifest | None:
        """Return the cached manifest of `hash`, now the most recently used, or `None`."""
        manifest = self.manifests.get(hash)
        if manifest is not None:
            self.manifests.move_to_end(hash)
        return manifest

    def cache_put(self, hash: str, manifest: BundleManifest) -> None:
        """Cache `manifest` by `hash`, evicting the least recently used past the cache size."""
        self.manifests[hash] = manifest
        self.manifests.move_to_end(hash)
        while len(self.manifests) > self.cache_size:
            self.manifests.popitem(last=False)

    def auth_headers(self) -> dict[str, str]:
        """Return the headers every bare-host request carries."""
        if self.token is None or self.host is None:
            return {}
        return {"x-decdn-token": self.token, "x-decdn-host": self.host}

    def is_bare(self, url: str) -> bool:
        """Return whether `url` is on the gateway's bare host."""
        return url.startswith(f"{self.origin}/")

    def bare_url(self, uri: DecdnUri) -> str:
        """Return the URI on the bare host."""
        return f"{self.origin}/{uri.hash}{tail(uri)}"

    def site_url(self, uri: str | DecdnUri) -> str:
        """Return the site URL of a bundle or an entry."""
        u = parse(uri)
        if u.kind == "blob":
            raise config("a blob has no site: only a bundle or an entry does")
        label = hash_to_base32(u.hash)
        host = label if u.namespace is None else f"{u.namespace}.{label}"
        path = tail(DecdnUri(hash=u.hash, kind=u.kind, path=u.path))
        if self.context == "loopback":
            port = self.port if self.port is not None else 443 if self.scheme == "https" else 80
            return f"{self.scheme}://{host}.localhost:{port}{path}"
        return f"{self.scheme}://{host}.{self.authority}{path}"

    def namespace_url(self, namespace: int) -> str:
        """Return the host a namespace's pointer serves at on a public gateway: `https://<ns>.<host>/`."""
        if isinstance(namespace, bool) or not isinstance(namespace, int):
            raise config(f"a namespace id is an integer: got {namespace!r}")
        if namespace <= 0:
            raise config("a namespace id is at least 1")
        if self.context == "loopback":
            raise config("a loopback gateway holds no namespace pointers")
        return f"{self.scheme}://{namespace}.{self.authority}/"

    def manifest_hash(self, hash_or_uri: str | DecdnUri | None) -> str:
        """Return the hash a manifest call names."""
        if hash_or_uri is None:
            raise config("manifest needs a hash outside a site page")
        if isinstance(hash_or_uri, str) and is_hash(hash_or_uri):
            return hash_or_uri.lower()
        return parse(hash_or_uri).hash

    def entry_blob_url(self, uri: DecdnUri, entry: ManifestEntry) -> str:
        """Return an entry by its own hash, as a blob: the bytes the manifest names, unrewritten."""
        return self.bare_url(DecdnUri(hash=entry.hash, kind="blob", namespace=uri.namespace))

    def entry_url(self, uri: DecdnUri, entry: ManifestEntry | None) -> str:
        """Return where an entry comes from, given its manifest entry when verifying."""
        if entry is not None and rewritten(uri.path or "") and self.context != "loopback":
            return self.entry_blob_url(uri, entry)
        return self.bare_url(uri) if self.context == "loopback" else self.site_url(uri)

    def needs_entry(self, uri: DecdnUri, mode: VerifyMode, ranged: bool) -> bool:
        """Return whether planning `uri` needs its manifest entry: a verified whole entry does."""
        return uri.kind == "entry" and mode is not False and not ranged

    def plan(self, uri: DecdnUri, mode: VerifyMode, ranged: bool, entry: ManifestEntry | None) -> Plan:
        """Return the request `get` sends for `uri`.

        `entry` is the manifest entry, given exactly when `needs_entry` is true. Raises `CONFIG` for
        what cannot be honoured as asked.
        """
        off: Verified = False if mode is False else "skipped"

        def unverifiable(why: str) -> None:
            if mode is True:
                raise config(f"verify: true, but {why}")

        if uri.kind == "blob":
            # The gateway serves a blob whole: nothing declares its size, so a range would come
            # back as every byte, or as an unverifiable window.
            if ranged:
                raise config("a blob has no declared size to range: fetch it whole, or range a bundle entry")
            if mode is False:
                return Plan(url=self.bare_url(uri), hash=None, length=None, verified=False, blob_url=None)
            return Plan(url=self.bare_url(uri), hash=uri.hash, length=None, verified=True, blob_url=None)
        if uri.kind == "bundle":
            unverifiable("a bundle's index is a page the gateway rewrites")
            url = self.bare_url(uri) if self.context == "loopback" else self.site_url(uri)
            return Plan(url=url, hash=None, length=None, verified=off, blob_url=None)
        if mode is False or ranged:
            if ranged and mode is True:
                raise config("verify: true, but a range cannot be verified")
            return Plan(url=self.entry_url(uri, None), hash=None, length=None, verified=off, blob_url=None)
        if entry is None:
            raise ValueError("a verified entry is planned with its manifest entry")
        url = self.entry_url(uri, entry)
        blob_url = self.entry_blob_url(uri, entry)
        return Plan(
            url=url,
            hash=entry.hash,
            length=entry.size,
            verified=True,
            blob_url=blob_url if url != blob_url and self.context != "loopback" else None,
        )

    def range_header(self, r: ByteRange) -> str:
        """Return the `Range` header of `r`; `CONFIG` unless the offset is at least 0 and the length 1."""
        if (
            isinstance(r.offset, bool)
            or not isinstance(r.offset, int)
            or r.offset < 0
            or (r.length is not None and (isinstance(r.length, bool) or not isinstance(r.length, int)))
            or (r.length is not None and r.length < 1)
        ):
            raise config("a range is a non-negative offset and a positive length")
        end = "" if r.length is None else str(r.offset + r.length - 1)
        return f"bytes={r.offset}-{end}"

    def interpret(
        self, plan: Plan, status: int, headers: httpx.Headers, ranged: ByteRange | None
    ) -> tuple[Verified, GuardOptions]:
        """Return what the answer to `plan` is: its `verified`, and the bounds its body is guarded by."""
        # Only a window the caller asked for goes unhashed: whatever status the gateway answers a
        # whole-body request with, its bytes must hash.
        whole = ranged is None or status == 200
        verified: Verified = "skipped" if plan.verified is True and not whole else plan.verified
        hash_ = plan.hash if verified is True and plan.hash is not None else None
        # The manifest's size bounds a whole entry both ways, and a window holds at most what was
        # asked for. Content-Length bounds a body from below only: a body decoded on the way can
        # run past it.
        if whole and plan.length is not None:
            return verified, GuardOptions(hash=hash_, length=plan.length, max=plan.length)
        length = _digits(headers.get("content-length"))
        max_ = ranged.length if not whole and ranged is not None and ranged.length is not None else None
        return verified, GuardOptions(hash=hash_, length=length, max=max_)

    def head_from_headers(self, headers: httpx.Headers) -> HeadInfo:
        """Return what a public `HEAD` answer says: its size, hash (ETag) and type."""
        size = _digits(headers.get("content-length"))
        etag = _ETAG.fullmatch(headers.get("etag", ""))
        return HeadInfo(
            size=size,
            hash=etag.group(1).lower() if etag is not None else None,
            content_type=headers.get("content-type"),
        )

    def head_from_entry(self, entry: ManifestEntry) -> HeadInfo:
        """Return what the manifest says of an entry."""
        return HeadInfo(size=entry.size, hash=entry.hash, content_type=site_type(entry.path))
