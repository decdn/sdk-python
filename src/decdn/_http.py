"""Gateway answers as errors: every non-2xx answer becomes a `DecdnError` with the fields its
contract carries (Retry-After, the `Link: <decdn:...>; rel="alternate"` fallback, the 402/503
JSON body, the 416 size, the pointer 409 `seq`)."""

import json
import re
from collections.abc import Callable
from typing import Any, TypeVar

import httpx

from decdn.errors import DecdnError, ErrorCode

_T = TypeVar("_T")

CODES: dict[int, ErrorCode] = {
    400: "BAD_URI",
    402: "PAYMENT_REQUIRED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    409: "CONFLICT",
    413: "PAYLOAD_TOO_LARGE",
    416: "RANGE_NOT_SATISFIABLE",
    422: "UNPROCESSABLE",
    429: "RATE_LIMITED",
    502: "UPSTREAM",
    503: "PAYMENT_REQUIRED",
}

_LINK = re.compile(r'<(decdn:[^>]+)>\s*;\s*rel="?alternate"?')
_SEQ = re.compile(r'"seq"\s*:\s*([0-9]+)')
_DIGITS = re.compile(r"[0-9]+")
_RANGE_SIZE = re.compile(r"bytes \*/([0-9]+)")


def is_success(status: int) -> bool:
    """Return whether `status` is a 2xx answer."""
    return 200 <= status <= 299


def read_json(path: str, body: bytes, read: Callable[[Any], _T]) -> _T:
    """Return what `read` makes of the JSON body of the gateway's success answer to `path`.

    A body that is not JSON, or JSON `read` cannot take (a field missing or of the wrong type),
    raises `HTTP`: the gateway answered outside its contract.
    """
    try:
        return read(json.loads(body))
    except (ValueError, KeyError, TypeError, RecursionError) as e:
        raise DecdnError(
            "HTTP",
            f"the gateway's answer to {path} is not the JSON its contract names: {e!r}",
            body=body.decode("utf-8", errors="replace"),
        ) from e


_U64_MAX = 2**64 - 1


def text_field(value: Any, name: str) -> str:
    """Return `value`, a JSON answer's string field `name`; `TypeError` for any other type."""
    if not isinstance(value, str):
        raise TypeError(f"{name} is not a string: {value!r}")
    return value


def u64_field(value: Any, name: str) -> int:
    """Return `value`, a JSON answer's u64 field `name`; `TypeError` for anything else.

    `json` reads an integer without a fraction or exponent as an exact `int`, so a `float` here is
    no u64.
    """
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _U64_MAX:
        raise TypeError(f"{name} is not a u64: {value!r}")
    return value


def bool_field(value: Any, name: str) -> bool:
    """Return `value`, a JSON answer's boolean field `name`; `TypeError` for any other type."""
    if not isinstance(value, bool):
        raise TypeError(f"{name} is not a boolean: {value!r}")
    return value


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
            if isinstance(secs, int) and not isinstance(secs, bool) and secs >= 0:
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
