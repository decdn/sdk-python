"""MIME rules for site entries, mirroring the TypeScript SDK's `mime.ts`.

What the gateway does with an entry by the extension of its name: whether a
site rewrites it as it streams (the gateway rewrites the types mime_guess
maps these extensions to:
https://github.com/decdn/decdn/blob/main/crates/cli/src/commands/gateway/rewrite.rs),
and the type a site serves it as, for the common web types. When it
verifies off loopback, the client fetches these entries by their hash up
front, and fetches by hash again any other entry a site answers with a
type it rewrites.
"""

import re

# Extensions a site serves as `text/html` or `text/css`, rewritten as they stream.
_REWRITTEN_RE = re.compile(r"\.(html?|hxt|shtml|css)\Z", re.IGNORECASE)
_EXTENSION_RE = re.compile(r"\.([^./]+)\Z")

_TYPES: dict[str, str] = {
    "html": "text/html",
    "htm": "text/html",
    "css": "text/css",
    "js": "text/javascript",
    "mjs": "text/javascript",
    "json": "application/json",
    "txt": "text/plain",
    "md": "text/markdown",
    "csv": "text/csv",
    "xml": "text/xml",
    "svg": "image/svg+xml",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "avif": "image/avif",
    "ico": "image/x-icon",
    "wasm": "application/wasm",
    "pdf": "application/pdf",
    "zip": "application/zip",
    "gz": "application/gzip",
    "tar": "application/x-tar",
    "mp4": "video/mp4",
    "webm": "video/webm",
    "mp3": "audio/mpeg",
    "ogg": "audio/ogg",
    "woff": "font/woff",
    "woff2": "font/woff2",
}


def rewritten(path: str) -> bool:
    """Return whether a site rewrites the entry at `path`, so its bytes need not hash to the entry hash."""
    return _REWRITTEN_RE.search(path) is not None


def site_type(path: str) -> str:
    """Return the type a site serves the entry at `path` as: text in UTF-8, bytes for an unknown extension."""
    match = _EXTENSION_RE.search(path)
    ext = match.group(1).lower() if match is not None else ""
    content_type = _TYPES.get(ext, "application/octet-stream")
    return f"{content_type}; charset=utf-8" if content_type.startswith("text/") else content_type


def is_rewritten_type(content_type: str | None) -> bool:
    """Return whether a response content type is one a site rewrites: `text/html` or `text/css`."""
    essence = (content_type or "").split(";")[0].strip().lower()
    return essence in ("text/html", "text/css")
