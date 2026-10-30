"""`decdn:` URIs, the grammar of the Rust client.

See https://github.com/decdn/decdn/blob/main/crates/client/src/uri.rs.

    decdn:<hash>[/<path>][?ns=<id>]

`<hash>` is 64 hex characters (no `b3:`/`0x`), a trailing `/` names a bundle, `<path>` is a manifest
entry path with each segment an RFC 3986 `pchar`, and `ns` is a decimal namespace id of at least 1.
"""

import json
import re
from dataclasses import dataclass
from typing import Literal

from decdn._hex import is_hash
from decdn.errors import DecdnError

SCHEME = "decdn:"
"""The scheme prefix of every `decdn:` URI."""

UriKind = Literal["blob", "bundle", "entry"]

_U64_MAX = (1 << 64) - 1
_PCHAR_PUNCT = frozenset(b"-._~!$&'()*+,;=:@")
_HEX_DIGIT_RE = re.compile(r"[0-9a-fA-F]")
_DECIMAL_RE = re.compile(r"[0-9]+")


@dataclass(frozen=True)
class DecdnUri:
    """A parsed `decdn:` URI."""

    hash: str
    """The BLAKE3 hash, 64 lowercase hex characters: of the blob, or of the bundle manifest."""
    kind: UriKind
    """What the hash names: a raw blob, a whole bundle, or one entry of a bundle."""
    path: str | None = None
    """The decoded manifest path of an entry."""
    namespace: int | None = None
    """The namespace routing hint, never 0."""


def _bad(message: str) -> DecdnError:
    return DecdnError("BAD_URI", message)


def _is_pchar(b: int) -> bool:
    """Return whether byte `b` is an RFC 3986 `pchar` other than `%`: unreserved, sub-delims, `:`, `@`."""
    return 0x30 <= b <= 0x39 or 0x41 <= b <= 0x5A or 0x61 <= b <= 0x7A or b in _PCHAR_PUNCT


def _hex_value(c: str | None) -> int | None:
    return int(c, 16) if c is not None and _HEX_DIGIT_RE.fullmatch(c) else None


def _decode_segment(segment: str) -> str:
    out = bytearray()
    i = 0
    while i < len(segment):
        ch = segment[i]
        c = ord(ch)
        if ch == "%":
            hi = _hex_value(segment[i + 1] if i + 1 < len(segment) else None)
            lo = _hex_value(segment[i + 2] if i + 2 < len(segment) else None)
            if hi is None or lo is None:
                raise _bad("invalid bundle entry path in decdn URI: bad `%` escape")
            out.append((hi << 4) | lo)
            i += 3
        elif c < 0x80 and _is_pchar(c):
            out.append(c)
            i += 1
        else:
            quoted = json.dumps(ch, ensure_ascii=False)
            raise _bad(f"invalid bundle entry path in decdn URI: character {quoted} must be percent-encoded")
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError:
        raise _bad("invalid bundle entry path in decdn URI: not UTF-8 once decoded") from None


def _decode_path(raw: str) -> str:
    segments: list[str] = []
    for segment in raw.split("/"):
        decoded = _decode_segment(segment)
        why: str | None = None
        if decoded == "":
            why = "empty segment (a leading, trailing or doubled `/` inside the path)"
        elif decoded in (".", ".."):
            why = f"`{decoded}` segment"
        elif "/" in decoded:
            why = "encoded `/` (`%2F`) in a segment"
        elif "\0" in decoded:
            why = "NUL byte"
        if why is not None:
            raise _bad(f"invalid bundle entry path in decdn URI: {why}")
        segments.append(decoded)
    return "/".join(segments)


def _bad_namespace(why: str) -> DecdnError:
    return _bad(
        f"invalid namespace in decdn URI ({why}): expected `ns=<id>` with a decimal id of at least 1; "
        "omit `ns` for no namespace"
    )


def _bad_query(why: str) -> DecdnError:
    return _bad(f"invalid query in decdn URI ({why}): the only key is `ns`")


def _parse_query(query: str) -> int:
    namespace: int | None = None
    for pair in query.split("&"):
        key, eq, value = pair.partition("=")
        if not eq:
            raise _bad_query(f"`{pair}` is not `key=value`")
        if key != "ns":
            raise _bad_query(f"unknown key `{key}`")
        if namespace is not None:
            raise _bad_query("`ns` given twice")
        if _DECIMAL_RE.fullmatch(value) is None:
            raise _bad_namespace(f"`{value}` is not a decimal id")
        digits = value.lstrip("0")
        if len(digits) > len(str(_U64_MAX)):
            raise _bad_namespace(f"`{value}` is out of range")
        id_ = int(digits or "0")
        if id_ > _U64_MAX:
            raise _bad_namespace(f"`{value}` is out of range")
        if id_ == 0:
            raise _bad_namespace("0 means no namespace")
        namespace = id_
    if namespace is None:
        raise _bad_query("empty query")
    return namespace


def parse_uri(s: str) -> DecdnUri:
    """Parse a `decdn:` URI.

    Raises a `DecdnError` with code `BAD_URI` for anything the Rust client would refuse.
    """
    if not s.startswith(SCHEME):
        raise _bad("not a decdn URI: expected `decdn:<hash>`")
    rest = s[len(SCHEME) :]
    if rest.startswith("//"):
        raise _bad("a decdn URI has no `//`: write `decdn:<hash>`, not `decdn://<hash>`")
    body, has_query, query = rest.partition("?")
    hash_, has_path, path = body.partition("/")
    if not is_hash(hash_):
        why = f"{len(hash_)} characters" if len(hash_) != 64 else "not hex"
        raise _bad(
            f"invalid hash in decdn URI ({why}): expected 64 hex characters with no `b3:` or `0x` prefix"
        )
    kind: UriKind = "blob"
    decoded_path: str | None = None
    if has_path:
        if path == "":
            kind = "bundle"
        else:
            kind = "entry"
            decoded_path = _decode_path(path)
    namespace = _parse_query(query) if has_query else None
    return DecdnUri(hash=hash_.lower(), kind=kind, path=decoded_path, namespace=namespace)


def _encode_segment(segment: str) -> str:
    return "".join(chr(b) if _is_pchar(b) else f"%{b:02X}" for b in segment.encode("utf-8"))


def format_uri(uri: DecdnUri) -> str:
    """Return the canonical string of `uri`: lowercase hex, uppercase `%XX` escapes."""
    out = SCHEME + uri.hash.lower()
    if uri.kind == "bundle":
        out += "/"
    elif uri.kind == "entry":
        for segment in (uri.path or "").split("/"):
            out += f"/{_encode_segment(segment)}"
    if uri.namespace is not None:
        out += f"?ns={uri.namespace}"
    return out
