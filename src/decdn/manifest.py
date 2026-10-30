"""Bundle manifests, as `BundleManifest::parse` reads them.

The shape is `{"version":1,"entries":[{"path","hash","size"?,"chunks"?}]}`, with the
version checked before any hash and every hash 64 hex characters after an optional
`b3:` or `0x`.
"""

import json
from dataclasses import dataclass
from typing import Any

from decdn._hex import is_hash
from decdn.errors import DecdnError
from decdn.hashing import blake3_hex

MAX_MANIFEST_BYTES = 64 * 1024 * 1024
"""The largest manifest the gateway pulls, and the SDK reads (64 MiB)."""


@dataclass(frozen=True)
class ManifestChunk:
    """One chunk of an entry: an independently addressed run of its bytes."""

    hash: str
    size: int


@dataclass(frozen=True)
class ManifestEntry:
    """One file of a bundle: its relative POSIX path, whole-file hash, and optional size and chunks."""

    path: str
    hash: str
    size: int | None = None
    chunks: tuple[ManifestChunk, ...] | None = None


@dataclass(frozen=True)
class BundleManifest:
    """A parsed bundle manifest; `hash` is the manifest blob's BLAKE3 hash."""

    hash: str
    entries: tuple[ManifestEntry, ...]

    def entry(self, path: str) -> ManifestEntry | None:
        """Return the entry at `path`, or `None` when the bundle has none."""
        for e in self.entries:
            if e.path == path:
                return e
        return None


def _malformed(message: str) -> DecdnError:
    return DecdnError("MALFORMED_MANIFEST", message)


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _is_size(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 2**53


def _parse_hash(s: Any) -> str | None:
    if not isinstance(s, str):
        return None
    hex_part = s[2:] if s.startswith("0x") else s[3:] if s.startswith("b3:") else s
    return hex_part.lower() if is_hash(hex_part) else None


def _parse_entry(raw: Any, i: int) -> ManifestEntry:
    if not isinstance(raw, dict) or not isinstance(raw.get("path"), str):
        raise _malformed(f"bundle entry {i} has no string `path`")
    path: str = raw["path"]
    named = _dumps(path)
    hash_ = _parse_hash(raw.get("hash"))
    if hash_ is None:
        shown = _dumps(raw["hash"]) if "hash" in raw else "undefined"
        raise _malformed(f"bundle entry {named} has a malformed hash: {shown}")
    size: int | None = None
    if raw.get("size") is not None:
        if not _is_size(raw["size"]):
            raise _malformed(f"bundle entry {named} has a malformed size")
        size = raw["size"]
    chunks: tuple[ManifestChunk, ...] | None = None
    if raw.get("chunks") is not None:
        raw_chunks = raw["chunks"]
        if not isinstance(raw_chunks, list):
            raise _malformed(f"bundle entry {named} has malformed chunks")
        parsed: list[ManifestChunk] = []
        for n, c in enumerate(raw_chunks):
            chunk_hash = _parse_hash(c.get("hash")) if isinstance(c, dict) else None
            if not isinstance(c, dict) or chunk_hash is None:
                raise _malformed(f"bundle entry {named} has a malformed chunk {n} hash")
            if not _is_size(c.get("size")):
                raise _malformed(f"bundle entry {named} has a malformed chunk {n} size")
            parsed.append(ManifestChunk(chunk_hash, c["size"]))
        chunks = tuple(parsed)
    return ManifestEntry(path, hash_, size, chunks)


def parse_manifest(data: bytes, expected_hash: str | None = None) -> BundleManifest:
    """Parse manifest bytes.

    With `expected_hash`, the bytes must hash to it first (`HASH_MISMATCH` otherwise).
    Anything that is not a v1 manifest is `MALFORMED_MANIFEST`.
    """
    hash_ = blake3_hex(data)
    if expected_hash is not None and hash_ != expected_hash.lower():
        raise DecdnError(
            "HASH_MISMATCH",
            f"bundle manifest hashes to {hash_}, not {expected_hash.lower()}",
        )
    try:
        doc = json.loads(data.decode("utf-8"))
    except (ValueError, RecursionError) as e:  # bad UTF-8, bad JSON, or hostile nesting
        raise DecdnError("MALFORMED_MANIFEST", "parse bundle JSON (expected v1 manifest)") from e
    version = doc.get("version") if isinstance(doc, dict) else None
    if (
        not isinstance(doc, dict)
        or isinstance(version, bool)
        or not isinstance(version, int | float)
        or not isinstance(doc.get("entries"), list)
    ):
        raise _malformed("parse bundle JSON (expected v1 manifest): missing `version` or `entries`")
    if version != 1:
        shown = int(version) if isinstance(version, float) and version.is_integer() else version
        raise _malformed(f"unsupported bundle version {shown} (this SDK supports v1)")
    return BundleManifest(hash_, tuple(_parse_entry(e, i) for i, e in enumerate(doc["entries"])))
