"""Python client for deCDN over a decdn gateway."""

from decdn._version import __version__
from decdn.base32 import (
    base32_to_hash,
    hash_to_base32,
)
from decdn.capability import (
    Capability,
    parse_capability,
)
from decdn.client import (
    AsyncClient,
    ByteRange,
    Client,
    Context,
    HeadInfo,
    Resolved,
    VerifyMode,
)
from decdn.errors import (
    DecdnError,
    ErrorCode,
)
from decdn.hashing import (
    Blake3Hasher,
    blake3_hex,
    create_blake3,
)
from decdn.loopback import (
    AsyncLoopbackApi,
    Delegate,
    LoopbackApi,
    RegisteredCapability,
    Registration,
)
from decdn.manifest import (
    MAX_MANIFEST_BYTES,
    BundleManifest,
    ManifestChunk,
    ManifestEntry,
    parse_manifest,
)
from decdn.pointer import (
    AsyncPointerApi,
    Pointer,
    PointerApi,
    PointerDomain,
    PointerSet,
    PointerSigner,
    PointerTypedData,
    TypedDataSigner,
    pointer_signing_hash,
    pointer_typed_data,
    recover_pointer_signer,
    sign_pointer,
)
from decdn.response import (
    AsyncResponse,
    ContentRange,
    Response,
)
from decdn.uri import (
    SCHEME,
    DecdnUri,
    UriKind,
    format_uri,
    parse_uri,
)

__all__ = [
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
