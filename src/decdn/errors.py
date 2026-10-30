"""The error type every SDK failure is raised as, and its stable codes."""

from typing import Literal

ErrorCode = Literal[
    # 402: the gateway's allowance is spent, or the content does not fit what is left,
    # or no capability covers the host.
    "PAYMENT_REQUIRED",
    # 503: the gateway's pool cannot pay.
    "POOL_UNFUNDED",
    # 404.
    "NOT_FOUND",
    # 416: the range starts past the end; `size` holds the entry's size.
    "RANGE_NOT_SATISFIABLE",
    # 401: the loopback token is missing or wrong.
    "UNAUTHORIZED",
    # 403.
    "FORBIDDEN",
    # 400, or a `decdn:` URI that does not parse.
    "BAD_URI",
    # 429.
    "RATE_LIMITED",
    # 502: the gateway's pull failed upstream.
    "UPSTREAM",
    # 413.
    "PAYLOAD_TOO_LARGE",
    # 409: a pointer `seq` that is not newer, or a capability already registered.
    "CONFLICT",
    # 422.
    "UNPROCESSABLE",
    # The bytes do not hash to the hash that names them.
    "HASH_MISMATCH",
    # The body is shorter or longer than declared, or the stream failed.
    "TRUNCATED",
    # A bundle manifest that does not parse.
    "MALFORMED_MANIFEST",
    # A `dcap1:` capability token that does not decode.
    "BAD_CAPABILITY",
    # The gateway could not be reached.
    "NETWORK",
    # The client is not set up for the call, or the call cannot be honoured as asked.
    "CONFIG",
    # A status the gateway contract does not name.
    "HTTP",
]


class DecdnError(Exception):
    """Every failure the SDK reports.

    Details the failure does not carry are `None`. The cause, when there is one, is the
    chained exception set with `raise ... from`.
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        status: int | None = None,
        retry_after_secs: int | None = None,
        fallback_uri: str | None = None,
        size: int | None = None,
        seq: int | None = None,
        body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code: ErrorCode = code
        self.status = status
        self.retry_after_secs = retry_after_secs
        self.fallback_uri = fallback_uri
        self.size = size
        self.seq = seq
        self.body = body
