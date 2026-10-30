import tomllib
from pathlib import Path

import decdn

EXPECTED = [
    "__version__",
    # base32
    "base32_to_hash",
    "hash_to_base32",
    # capability
    "Capability",
    "parse_capability",
    # client
    "AsyncClient",
    "ByteRange",
    "Client",
    "Context",
    "HeadInfo",
    "Resolved",
    "VerifyMode",
    # errors
    "DecdnError",
    "ErrorCode",
    # hashing
    "Blake3Hasher",
    "blake3_hex",
    "create_blake3",
    # loopback
    "AsyncLoopbackApi",
    "Delegate",
    "LoopbackApi",
    "RegisteredCapability",
    "Registration",
    # manifest
    "MAX_MANIFEST_BYTES",
    "BundleManifest",
    "ManifestChunk",
    "ManifestEntry",
    "parse_manifest",
    # pointer
    "AsyncPointerApi",
    "Pointer",
    "PointerApi",
    "PointerDomain",
    "PointerSet",
    "PointerSigner",
    "PointerTypedData",
    "TypedDataSigner",
    "pointer_signing_hash",
    "pointer_typed_data",
    "recover_pointer_signer",
    "sign_pointer",
    # response
    "AsyncResponse",
    "ContentRange",
    "Response",
    # uri
    "SCHEME",
    "DecdnUri",
    "UriKind",
    "format_uri",
    "parse_uri",
]


def test_version_is_pyprojects() -> None:
    # scripts/release.sh bumps both together.
    pyproject = tomllib.loads((Path(__file__).resolve().parents[2] / "pyproject.toml").read_text())
    assert decdn.__version__ == pyproject["project"]["version"]


def test_all_names_resolve() -> None:
    for name in decdn.__all__:
        assert hasattr(decdn, name), name


def test_mirrors_the_typescript_index() -> None:
    assert set(decdn.__all__) == set(EXPECTED)
    assert len(decdn.__all__) == len(set(decdn.__all__))
