from pathlib import Path

import pytest

from decdn.errors import DecdnError
from decdn.local import default_data_dir, read_loopback_token


def test_reads_the_trimmed_token_the_gateway_wrote_under_its_data_dir(tmp_path: Path) -> None:
    (tmp_path / "token").write_text("ab" * 32 + "\n")
    assert read_loopback_token(tmp_path) == "ab" * 32


def test_fails_with_config_when_there_is_no_token_or_it_is_not_64_hex(tmp_path: Path) -> None:
    with pytest.raises(DecdnError) as missing:
        read_loopback_token(tmp_path)
    assert missing.value.code == "CONFIG"

    (tmp_path / "token").write_text("short")
    with pytest.raises(DecdnError) as malformed:
        read_loopback_token(tmp_path)
    assert malformed.value.code == "CONFIG"


def test_defaults_to_home_decdn_gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert default_data_dir() == tmp_path / ".decdn" / "gateway"
