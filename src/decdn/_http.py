"""Gateway answers as errors: every non-2xx answer becomes a `DecdnError` with the fields its
contract carries (Retry-After, the `Link: <decdn:...>; rel="alternate"` fallback, the 402/503
JSON body, the 416 size, the pointer 409 `seq`)."""

import json
import re
from typing import Any

import httpx

from decdn.errors import DecdnError, ErrorCode

CODES: dict[int, ErrorCode] = {
    400: "BAD_URI",
    401: "UNAUTHORIZED",
    402: "PAYMENT_REQUIRED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    409: "CONFLICT",
    413: "PAYLOAD_TOO_LARGE",
    416: "RANGE_NOT_SATISFIABLE",
    422: "UNPROCESSABLE",
    429: "RATE_LIMITED",
    502: "UPSTREAM",
    503: "POOL_UNFUNDED",
}

_LINK = re.compile(r'<(decdn:[^>]+)>\s*;\s*rel="?alternate"?')
_SEQ = re.compile(r'"seq"\s*:\s*([0-9]+)')
_DIGITS = re.compile(r"[0-9]+")
_RANGE_SIZE = re.compile(r"bytes \*/([0-9]+)")


def is_success(status: int) -> bool:
    """Return whether `status` is a 2xx answer."""
    return 200 <= status <= 299


def network_error(url: str, exc: Exception) -> DecdnError:
    """Return the `NETWORK` error for a failure to reach the gateway at `url`.

    The caller raises it `from exc`.
    """
    u = httpx.URL(url)
    port = f":{u.port}" if u.port is not None else ""
    return DecdnError("NETWORK", f"cannot reach {u.scheme}://{u.host}{port}: {exc}")


def error_of(status: int, headers: httpx.Headers, body: str) -> DecdnError:
    """Return the error for a gateway answer that is not a success.

    `body` is the answer's text, read by the caller.
    """
    code: ErrorCode = CODES.get(status, "HTTP")
    message = body.strip()
    details: dict[str, Any] = {"status": status}
    if message != "":
        details["body"] = message
    retry_after = headers.get("retry-after")
    if retry_after is not None and _DIGITS.fullmatch(retry_after.strip()):
        details["retry_after_secs"] = int(retry_after.strip())
    link = _LINK.search(headers.get("link") or "")
    if link is not None:
        details["fallback_uri"] = link.group(1)
    if headers.get("content-type", "").startswith("application/json"):
        try:
            parsed = json.loads(body)
        except ValueError:
            # Not JSON after all: the text stands.
            parsed = None
        if isinstance(parsed, dict):
            if isinstance(parsed.get("error"), str):
                message = parsed["error"]
            if isinstance(parsed.get("uri"), str):
                details["fallback_uri"] = parsed["uri"]
            secs = parsed.get("retry_after_secs")
            if isinstance(secs, int) and not isinstance(secs, bool):
                details["retry_after_secs"] = secs
        if parsed is not None:
            # A u64 `seq` is read from its digits, as the gateway wrote it.
            seq = _SEQ.search(body)
            if seq is not None:
                details["seq"] = int(seq.group(1))
    if status == 416:
        size = _RANGE_SIZE.fullmatch(headers.get("content-range", ""))
        if size is not None:
            details["size"] = int(size.group(1))
    return DecdnError(code, message or f"gateway answered {status}", **details)
