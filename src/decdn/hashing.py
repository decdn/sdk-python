"""BLAKE3 hashing, one-shot and incremental."""

import blake3


def blake3_hex(data: bytes) -> str:
    """Return the BLAKE3 hash of `data` as 64 lowercase hex characters."""
    return blake3.blake3(data).hexdigest()


class Blake3Hasher:
    """An incremental BLAKE3 hasher, for bytes that arrive in chunks."""

    def __init__(self) -> None:
        self._state = blake3.blake3()

    def update(self, chunk: bytes) -> None:
        """Feed `chunk` into the hash."""
        self._state.update(chunk)

    def hex(self) -> str:
        """Return the hash of every chunk so far, as 64 lowercase hex characters."""
        return self._state.hexdigest()


def create_blake3() -> Blake3Hasher:
    """Return a new incremental BLAKE3 hasher."""
    return Blake3Hasher()
