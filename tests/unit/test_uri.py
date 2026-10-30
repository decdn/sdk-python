"""Cases ported from the Rust client's URI tests.

See https://github.com/decdn/decdn/blob/main/crates/client/src/uri/tests.rs: the SDK accepts and
rejects exactly what the Rust `decdn:` grammar does.
"""

import re

import pytest

from decdn.errors import DecdnError
from decdn.uri import DecdnUri, format_uri, parse_uri

HEX = "ce88d839ce93b3d62c500567602ea372a9b6b043866bdf327e508f7226efe06e"


def bad_uri(s: str) -> str:
    with pytest.raises(DecdnError) as info:
        parse_uri(s)
    assert info.value.code == "BAD_URI"
    return str(info.value)


def test_parses_every_target_kind() -> None:
    assert parse_uri(f"decdn:{HEX}") == DecdnUri(hash=HEX, kind="blob")
    assert parse_uri(f"decdn:{HEX}/") == DecdnUri(hash=HEX, kind="bundle")
    assert parse_uri(f"decdn:{HEX}/a/b.txt") == DecdnUri(hash=HEX, kind="entry", path="a/b.txt")


@pytest.mark.parametrize("suffix", ["", "/", "/a/b.txt"])
def test_parses_the_namespace_hint_on_every_target_kind(suffix: str) -> None:
    assert parse_uri(f"decdn:{HEX}{suffix}?ns=42").namespace == 42


@pytest.mark.parametrize(
    "s",
    [
        f"decdn:{HEX}",
        f"decdn:{HEX}/",
        f"decdn:{HEX}/a/b.txt",
        f"decdn:{HEX}/a/b.txt?ns=42",
        f"decdn:{HEX}/?ns=1",
        f"decdn:{HEX}/my%20model/weights%5B0%5D.bin",
        f"decdn:{HEX}/caf%C3%A9.txt",
    ],
)
def test_round_trips_the_canonical_form(s: str) -> None:
    uri = parse_uri(s)
    assert format_uri(uri) == s
    assert parse_uri(format_uri(uri)) == uri


def test_parses_uppercase_hex_and_prints_lowercase() -> None:
    uri = parse_uri(f"decdn:{HEX.upper()}")
    assert uri.hash == HEX
    assert format_uri(uri) == f"decdn:{HEX}"


def test_decodes_percent_encoded_segments_to_the_literal_manifest_path() -> None:
    assert parse_uri(f"decdn:{HEX}/dir%20one/%5Bx%5D*%3F%7B%7D.bin").path == "dir one/[x]*?{}.bin"


def test_keeps_a_namespace_beyond_2_53_exact() -> None:
    assert parse_uri(f"decdn:{HEX}?ns=18446744073709551615").namespace == 2**64 - 1


def test_rejects_a_missing_scheme_or_an_authority() -> None:
    assert re.search(r"not a decdn URI", bad_uri(HEX))
    assert re.search(r"no `//`", bad_uri(f"decdn://{HEX}"))


@pytest.mark.parametrize(
    "bad",
    [
        f"decdn:b3:{HEX}",
        f"decdn:0x{HEX}",
        f"decdn:{HEX[:63]}",
        f"decdn:{HEX}0",
        f"decdn:{HEX[:63]}g",
    ],
)
def test_rejects_prefixed_short_long_or_non_hex_hashes(bad: str) -> None:
    assert re.search(r"invalid hash", bad_uri(bad))


@pytest.mark.parametrize(
    "bad",
    ["ns=0", "ns=", "ns=-1", "ns=+1", "ns=1.5", "ns=x", "ns=18446744073709551616"],
)
def test_rejects_bad_namespaces(bad: str) -> None:
    assert re.search(r"invalid namespace", bad_uri(f"decdn:{HEX}?{bad}"))


@pytest.mark.parametrize("bad", ["ns=1&x=2", "x=1", "ns=1&ns=2", "", "ns"])
def test_rejects_an_unknown_duplicate_or_empty_query(bad: str) -> None:
    assert re.search(r"invalid query", bad_uri(f"decdn:{HEX}?{bad}"))


@pytest.mark.parametrize(
    "bad",
    [
        "/../x",
        "/a/../b",
        "/./a",
        "//a",
        "/a//b",
        "/a/",
        "/a%2Fb",
        "/a%00b",
        "/a b",
        "/%zz",
        "/%4",
        "/%FF",
        "/a#b",
    ],
)
def test_rejects_unsafe_or_malformed_entry_paths(bad: str) -> None:
    assert re.search(r"invalid bundle entry path", bad_uri(f"decdn:{HEX}{bad}"))


def test_rejects_a_trailing_newline_in_the_hash() -> None:
    assert re.search(r"invalid hash", bad_uri(f"decdn:{HEX[:63]}\n"))


def test_encodes_what_is_not_a_pchar() -> None:
    uri = DecdnUri(hash=HEX, kind="entry", path="dir one/[x]?#%.bin")
    assert format_uri(uri) == f"decdn:{HEX}/dir%20one/%5Bx%5D%3F%23%25.bin"


def test_writes_a_namespace_hint() -> None:
    assert format_uri(DecdnUri(hash=HEX, kind="bundle", namespace=7)) == f"decdn:{HEX}/?ns=7"


def test_rejects_a_namespace_too_long_to_parse() -> None:
    assert re.search(r"out of range", bad_uri(f"decdn:{HEX}?ns=" + "9" * 5000))
