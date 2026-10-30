"""Cases ported from the Rust `parse_manifest_*` tests: the SDK accepts the manifests
`BundleManifest::parse` accepts and refuses the rest."""

import json
from typing import Any

import pytest

from decdn.errors import DecdnError
from decdn.hashing import blake3_hex
from decdn.manifest import ManifestChunk, ManifestEntry, parse_manifest

AB = "ab" * 32
CD = "cd" * 32


def encode(value: Any) -> bytes:
    return json.dumps(value).encode()


def malformed(data: bytes) -> str:
    with pytest.raises(DecdnError) as info:
        parse_manifest(data)
    assert info.value.code == "MALFORMED_MANIFEST"
    return str(info.value)


def test_accepts_v1_with_an_optional_size() -> None:
    m = parse_manifest(
        encode(
            {
                "version": 1,
                "entries": [
                    {"path": "a.txt", "hash": f"b3:{AB}", "size": 4},
                    {"path": "b", "hash": f"b3:{CD}"},
                ],
            }
        )
    )
    assert m.entries == (ManifestEntry("a.txt", AB, size=4), ManifestEntry("b", CD))
    assert m.entry("b") == ManifestEntry("b", CD)
    assert m.entry("nope") is None


def test_keeps_the_blob_hash() -> None:
    data = b'{"version":1,"entries":[]}'
    assert parse_manifest(data).hash == blake3_hex(data)


def test_accepts_a_chunked_entry_in_order() -> None:
    m = parse_manifest(
        encode(
            {
                "version": 1,
                "entries": [
                    {
                        "path": "m.bin",
                        "hash": f"b3:{'ff' * 32}",
                        "size": 100,
                        "chunks": [
                            {"hash": f"b3:{'c0' * 32}", "size": 60},
                            {"hash": f"b3:{'c1' * 32}", "size": 40},
                        ],
                    }
                ],
            }
        )
    )
    chunks = m.entries[0].chunks
    assert chunks == (ManifestChunk("c0" * 32, 60), ManifestChunk("c1" * 32, 40))


@pytest.mark.parametrize("spelling", [f"b3:{AB}", f"0x{AB}", AB, f"b3:{AB.upper()}"])
def test_accepts_every_hash_spelling(spelling: str) -> None:
    m = parse_manifest(encode({"version": 1, "entries": [{"path": "a", "hash": spelling}]}))
    assert m.entries[0].hash == AB


@pytest.mark.parametrize("bad", ["zz", "", f"b3:{AB[:62]}", f"b3:{AB}ab", f"B3:{AB}"])
def test_rejects_a_malformed_entry_hash_and_names_the_entry(bad: str) -> None:
    why = malformed(
        encode(
            {
                "version": 1,
                "entries": [
                    {"path": "ok", "hash": f"b3:{AB}"},
                    {"path": "bad", "hash": bad},
                ],
            }
        )
    )
    assert 'bundle entry "bad" has a malformed hash' in why


def test_rejects_a_malformed_chunk_hash_and_names_the_entry() -> None:
    why = malformed(
        encode(
            {
                "version": 1,
                "entries": [
                    {
                        "path": "bad",
                        "hash": f"b3:{AB}",
                        "size": 4,
                        "chunks": [
                            {"hash": f"b3:{AB}", "size": 2},
                            {"hash": "b3:zz", "size": 2},
                        ],
                    }
                ],
            }
        )
    )
    assert 'bundle entry "bad" has a malformed chunk 1 hash' in why


def test_checks_the_version_before_the_hashes() -> None:
    why = malformed(b'{"version":2,"entries":[{"path":"a","hash":"sha256:00"}]}')
    assert "unsupported bundle version 2" in why


@pytest.mark.parametrize(
    "bad",
    [
        "not json",
        "[]",
        "{}",
        '{"version":1}',
        '{"entries":[]}',
        '{"version":"1","entries":[]}',
        '{"version":1,"entries":{}}',
        '{"version":1,"entries":[{"hash":"' + AB + '"}]}',
        '{"version":1,"entries":[{"path":"a","hash":"' + AB + '","size":-1}]}',
        '{"version":1,"entries":[{"path":"a","hash":"' + AB + '","size":1.5}]}',
    ],
)
def test_rejects_what_is_not_a_manifest(bad: str) -> None:
    malformed(bad.encode())


def test_refuses_bytes_that_do_not_hash_to_the_expected_manifest_hash() -> None:
    data = encode({"version": 1, "entries": []})
    with pytest.raises(DecdnError) as info:
        parse_manifest(data, AB)
    assert info.value.code == "HASH_MISMATCH"
    assert parse_manifest(data, blake3_hex(data)).entries == ()


def test_rejects_hostile_nesting_as_malformed() -> None:
    malformed(b"[" * 100_000)
