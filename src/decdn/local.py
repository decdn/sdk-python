"""Local helpers: the loopback gateway's data dir and token."""

import re
from pathlib import Path

from decdn.errors import DecdnError

_TOKEN_RE = re.compile(r"[0-9a-fA-F]{64}")


def default_data_dir() -> Path:
    """Return the loopback gateway's default data dir, `~/.decdn/gateway`."""
    return Path.home() / ".decdn" / "gateway"


def read_loopback_token(data_dir: str | Path | None = None) -> str:
    """Return the token a loopback `decdn gateway` keeps in `<data_dir>/token`.

    `data_dir` is the gateway's `--data-dir`; it defaults to `default_data_dir()`. The token is
    stripped of surrounding whitespace and must be 64 hex characters.
    """
    path = (Path(data_dir) if data_dir is not None else default_data_dir()) / "token"
    try:
        token = path.read_bytes().decode("utf-8", errors="replace").strip()
    except OSError as e:
        raise DecdnError(
            "CONFIG",
            f"cannot read the gateway token at {path}: start `decdn gateway` first",
        ) from e
    if _TOKEN_RE.fullmatch(token) is None:
        raise DecdnError("CONFIG", f"{path} does not hold a gateway token (64 hex characters)")
    return token
