<!-- markdownlint-disable-file MD033 MD041 -->
<p align="center">
  <a href="https://decdn.org">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/decdn/decdn/main/.github/assets/wordmark-dark.svg">
      <img alt="deCDN" src="https://raw.githubusercontent.com/decdn/decdn/main/.github/assets/wordmark-light.svg" width="150">
    </picture>
  </a>
</p>

<h1 align="center">decdn</h1>

<h3 align="center">Python client for deCDN: verified fetches, bundles, loopback capabilities and namespace pointers.</h3>

<p align="center">
  <a href="https://github.com/decdn/sdk-python/actions/workflows/ci.yml?query=branch%3Amain"><img src="https://img.shields.io/github/actions/workflow/status/decdn/sdk-python/ci.yml?branch=main&label=CI&logo=github" alt="CI"></a>
  <a href="https://github.com/decdn/sdk-python/actions/workflows/codeql.yml?query=branch%3Amain"><img src="https://img.shields.io/github/actions/workflow/status/decdn/sdk-python/codeql.yml?branch=main&label=CodeQL&logo=github" alt="CodeQL"></a>
  <a href="https://pypi.org/project/decdn/"><img src="https://img.shields.io/pypi/v/decdn?logo=pypi&logoColor=white" alt="PyPI"></a>
  <a href="https://pypi.org/project/decdn/"><img src="https://img.shields.io/pypi/pyversions/decdn?logo=python&logoColor=white" alt="Python versions"></a>
  <a href="#compatibility"><img src="https://img.shields.io/badge/dynamic/toml?url=https%3A%2F%2Fraw.githubusercontent.com%2Fdecdn%2Fsdk-python%2Fmain%2Fpyproject.toml&query=%24.tool.decdn.gateway-floor&label=decdn%20gateway&prefix=%E2%89%A5%20v" alt="decdn gateway floor"></a>
  <a href="#license"><img src="https://img.shields.io/badge/license-MIT%20OR%20Apache--2.0-blue" alt="License: MIT OR Apache-2.0"></a>
  <a href="https://www.conventionalcommits.org"><img src="https://img.shields.io/badge/Conventional%20Commits-1.0.0-yellow" alt="Conventional Commits"></a>
  <a href="https://discord.gg/vVbNswKVGX"><img src="https://img.shields.io/badge/chat-Discord-5865F2?logo=discord&logoColor=white" alt="Discord"></a>
</p>

<h3 align="center">
  <a href="https://decdn.org">Website</a> ·
  <a href="https://docs.decdn.org">Docs</a> ·
  <a href="https://github.com/decdn/decdn/blob/main/adr/appendix-gateway.md">Gateway spec</a> ·
  <a href="https://github.com/decdn/decdn">deCDN</a> ·
  <a href="https://discord.gg/vVbNswKVGX">Discord</a>
</h3>

Python client for [deCDN](https://github.com/decdn/decdn), for Python 3.11+.
It speaks HTTP to a `decdn gateway`, which pays for every pull and verifies
it, and it hashes the bytes again with BLAKE3 wherever it holds a whole blob
named by its hash. No iroh, no wallet library. `Client` is synchronous and
`AsyncClient` is its `asyncio` twin; both take the same arguments and return
the same shapes.

- **Verified streaming.** A blob, or a whole bundle entry, is hashed with
  BLAKE3 as it streams; a body that does not hash to its name raises
  `HASH_MISMATCH` instead of ending cleanly. Ranges and rendered pages are
  `"skipped"` ([Verification](#verification)).
- **Bundles.** Entries are checked against the bundle manifest, which is
  itself verified and cached per client.
- **Byte ranges** of sized entries, for large files such as model weights. A
  window does not hash to anything, so its `verified` is `"skipped"`.
- **Loopback gateways.** Read the pending delegate key and register
  capabilities on a local `decdn gateway`.
- **Namespace pointers.** Point a `PublisherRegistry` namespace at a bundle on
  a public gateway, signing EIP-712 typed data with a private key or an
  `eth-account`-style `sign_typed_data` signer.
- **Small and typed.** Fully annotated (`py.typed`); namespaces, seq, caps and
  expiries are exact `int`s.

## Contents

- [Install](#install)
- [Gateways](#gateways)
- [Compatibility](#compatibility)
- [Public gateway](#public-gateway)
- [Loopback gateway](#loopback-gateway)
- [Verification](#verification)
- [Errors](#errors)
- [Namespace pointers](#namespace-pointers)
- [API at a glance](#api-at-a-glance)
- [Development](#development)
- [License](#license)

## Install

```sh
pip install decdn
# or
uv add decdn
```

## Gateways

The client talks to one of two kinds of gateway:

| Gateway                                                  | Where it runs                          | Who pays                                          |
| -------------------------------------------------------- | -------------------------------------- | ------------------------------------------------- |
| Public (`decdn gateway --public-url https://decdn.link`) | Anywhere with network access           | The capabilities registered with the gateway      |
| Loopback (`decdn gateway`, `http://127.0.0.1:8484`)      | The same machine, outside a browser      | The capabilities registered with the gateway      |

The gateway's HTTP contract is
[appendix-gateway](https://github.com/decdn/decdn/blob/main/adr/appendix-gateway.md);
bundles and their manifests are
[appendix-bundles](https://github.com/decdn/decdn/blob/main/adr/appendix-bundles.md).

## Compatibility

Python 3.11 or later. Python 3.14 is not yet supported: coincurve ships no 3.14 wheels; the matrix grows when it does.

Two floors in `pyproject.toml` (`[tool.decdn]`) say which `decdn gateway`
releases the SDK works against:

- `gateway-floor` (`0.3.0`) is the oldest gateway whose HTTP contract the SDK
  supports, checked at release. It is the release that carries
  [cross-origin reads](https://github.com/decdn/decdn/blob/main/adr/appendix-gateway.md#cross-origin-reads),
  which a public gateway needs. That floor covers a public client alone: a
  loopback client speaks the pending-key capability API and takes no token,
  which only decdn releases after 0.3.1 carry, so against 0.3.0 or 0.3.1 every
  loopback request fails with `HTTP` (the gateway's 401). The floor rises to the
  first release that carries the API before the SDK's next release.
- `harness-floor` is the first decdn release whose e2e harness runs this SDK's
  contract suite ([CONTRIBUTING.md](CONTRIBUTING.md#the-contract-suite-fake-and-real-gateway)).
  It is empty until that release exists: the released-gateway run starts with
  the first decdn release that ships the Python harness.

## Public gateway

```python
from decdn import AsyncClient, ByteRange, Client

with Client("https://decdn.link") as client:
    # A blob, streamed and hashed as it arrives.
    blob = client.get("decdn:<64 hex>")
    blob.verified  # True
    data = blob.read()  # raises HASH_MISMATCH on bad bytes

    # One file of a bundle, checked against the bundle's manifest.
    page = client.get("decdn:<manifest hash>/docs/index.html")
    text = page.text()

    # A window of a sized entry: 206, not verifiable.
    part = client.get("decdn:<manifest hash>/model.bin", range=ByteRange(offset=0, length=1 << 20))
    part.content_range  # ContentRange(offset=0, length=1048576, size=...)

# The same calls, awaited.
async with AsyncClient("https://decdn.link") as client:
    blob = await client.get("decdn:<64 hex>")
    data = await blob.read()

    # Or stream it: the mismatch raises at the end of the iteration.
    streamed = await client.get("decdn:<64 hex>")
    async for chunk in streamed.iter_bytes():
        ...
```

A public gateway pays with the capabilities its operator registered on it, in
the order they were registered; a capability scoped to namespaces pays only on
the host of a namespace whose pointer serves the request
([appendix-gateway § Scope](https://github.com/decdn/decdn/blob/main/adr/appendix-gateway.md#scope)). When none can pay, a request the gateway must
pull fails with `PAYMENT_REQUIRED`, carrying `retry_after_secs` and the
`fallback_uri` to fetch with a paying client of your own; a response its cache
holds whole still comes, at no cost.

`ByteRange` takes an `offset` and, optionally, a `length`. A response is a context manager
too; leaving the block closes the connection if the body was not read.

## Loopback gateway

A loopback gateway pays with capabilities a pool owner issued to its delegate
keys. The gateway holds one pending key: `delegate()` returns the same address
until a capability claims it, and a fresh key after that. Have the owner
assign the pending key, then register the capability with the scope it pays
for:

```python
from decdn import AsyncClient, Client, NamespaceScope

with Client("http://127.0.0.1:8484") as client:
    delegate = client.loopback.delegate()
    # The pool owner: decdn pool assign --pool <id> --signer <delegate.address> ...
    client.loopback.register_capability("dcap1:...", "any")
    r = client.get("decdn:<64 hex>")

async with AsyncClient("http://127.0.0.1:8484") as client:
    delegate = await client.loopback.delegate()
    scope = NamespaceScope(namespaces=(7, 9), signature="0x...")
    await client.loopback.register_capability("dcap1:...", scope)
    r = await client.get("decdn:<64 hex>?ns=7")
```

The scope is required. `"any"` pays for any content. A `NamespaceScope` pays
only for requests that name one of its namespaces (`?ns=<id>` in the `decdn:`
URI, or a site host `<ns>.<label>.localhost`), and carries the pool owner's
EIP-712 signature over `CapabilityScope(address signer,bytes32 poolId,uint256[] namespaces)`
(domain `decdn-capability-scope`, version `1`, the chain id, and the
PaymentPool as the verifying contract;
[appendix-gateway § Scope](https://github.com/decdn/decdn/blob/main/adr/appendix-gateway.md#scope)). The SDK sends the signature as given;
the gateway checks it and refuses a malformed scope or a signature that does
not recover the pool's owner with `UNPROCESSABLE`. A request that names no
namespace is paid only by `"any"` capabilities.

A key holds one capability per pool, and capabilities from several pools.
`client.loopback.capabilities()` lists what the gateway holds in registration
order. A request goes to the first registered one that is not spent, not near
its expiry, and scoped to the namespace it names; failing that, to the first
such `"any"` one. When none can pay, a request the gateway must pull fails with
`PAYMENT_REQUIRED`; a blob its cache holds whole still comes, at no cost. The
gateway keeps keys and capabilities in memory, so after a restart you register
again. A `dcap1:` token decodes offline with `parse_capability`.

A loopback gateway takes no credentials. It answers what runs outside a
browser on the same machine and refuses a web page from another site or origin
with `FORBIDDEN`; the SDK sends no `Origin`, no `Sec-Fetch-*` headers and no
browser user agent, so it is served.

The SDK does not start the gateway. Run it yourself (`decdn gateway`, or the
`decdn/decdn` Docker image). A `localhost` URL, or one on a loopback address
(`127.0.0.0/8` or `::1`), opens a loopback client; a public gateway you run locally needs another host name,
such as `decdn.localhost`.

## Verification

`get` takes `verify="auto" | True | False` (default `"auto"`, or the client's
own default from its constructor). `response.verified` says what happened:

| Request                     | `verified` | How                                                                                                                |
| --------------------------- | ---------- | ------------------------------------------------------------------------------------------------------------------ |
| A blob by hash              | `True`     | The whole body hashes to the URI's hash.                                                                           |
| A bundle entry              | `True`     | The client reads the bundle manifest (cached per client, itself verified) and the body hashes to the entry's hash. |
| A page or stylesheet entry  | `True`     | A site rewrites HTML and CSS as it serves them, so the client fetches these by their entry hash instead.           |
| A `range` of a bundle entry | `"skipped"` | A window does not hash to anything. A blob has no declared size, so a range on one is refused with `CONFIG`.       |
| A bundle's index or listing | `"skipped"` | The gateway renders it.                                                                                            |
| Any, with `verify=False`    | `False`    | Nothing is hashed and no manifest is read.                                                                         |

`verify=True` refuses a request that cannot be verified with `CONFIG`.

A verified body is hashed as it streams, and a body is consumed once: by
`read()` (also `text()` and `json()`) or by `iter_bytes()`, not both. A
mismatch raises `HASH_MISMATCH` (or `TRUNCATED` when the gateway cuts the
stream) from `read()`, or from the iteration at its end, and the same error
again on every later call. Chunks read before the end are not yet verified;
act on them only once the iteration finishes. Ranges are never verified.

## Errors

Every failure is a `DecdnError` with a stable `code`:

| Code                    | When                                                                                                |
| ----------------------- | --------------------------------------------------------------------------------------------------- |
| `PAYMENT_REQUIRED`      | 402 (loopback) or 503 (public): no capability the gateway holds can pay                             |
| `NOT_FOUND`             | 404                                                                                                 |
| `RANGE_NOT_SATISFIABLE` | 416: the range starts past the end; `size` holds the entry's size                                   |
| `FORBIDDEN`             | 403                                                                                                 |
| `BAD_URI`               | A `decdn:` URI that does not parse, or 400                                                          |
| `RATE_LIMITED`          | 429                                                                                                 |
| `UPSTREAM`              | 502: the gateway's pull failed upstream                                                             |
| `PAYLOAD_TOO_LARGE`     | 413                                                                                                 |
| `CONFLICT`              | 409: a pointer `seq` that is not newer, or a capability already registered                          |
| `UNPROCESSABLE`         | 422                                                                                                 |
| `HASH_MISMATCH`         | The bytes do not hash to the hash that names them                                                   |
| `TRUNCATED`             | The body is shorter or longer than declared, or the stream failed                                   |
| `MALFORMED_MANIFEST`    | A bundle manifest that does not parse                                                               |
| `BAD_CAPABILITY`        | A `dcap1:` token that does not decode                                                               |
| `NETWORK`               | The gateway could not be reached                                                                    |
| `CONFIG`                | The client is not set up for the call, or the call cannot be honoured as asked                      |
| `HTTP`                  | A status the gateway contract does not name                                                         |

The error carries the details its failure has, and `None` for the rest:
`status`, `body`, `retry_after_secs` and `fallback_uri` (402 and 503), `size`
(416) and `seq` (409). The cause, when there is one, is the chained exception.

`fallback_uri` is the canonical `decdn:` URI: fetch it with a paying client of
your own (`decdn get <uri>`), or wait `retry_after_secs`. The SDK retries
nothing on its own; `get(uri, retry=True)` waits out one `Retry-After` and
tries once more, and `retry=30.0` does so only when the wait is at most 30
seconds.

## Namespace pointers

On a public gateway, the owner of a namespace on the `PublisherRegistry`
points it at a bundle, and `https://<ns>.<host>/` serves that bundle:

```python
from decdn import AsyncClient, Client, PointerDomain

domain = PointerDomain(chain_id=421614, publisher_registry="0x...")

with Client("https://decdn.link", pointer=domain) as client:
    result = client.pointer.set(
        namespace=42,
        root="decdn:<manifest hash>/",
        signer="0x...",  # a raw secp256k1 key: hex or 32 bytes
    )
    result.url  # https://42.decdn.link/

async with AsyncClient("https://decdn.link", pointer=domain) as client:
    result = await client.pointer.set(namespace=42, root="decdn:<manifest hash>/", signer="0x...")
```

A wallet signs instead: any object with
`sign_typed_data(typed_data) -> str` is a `TypedDataSigner`. For an
`eth-account` `LocalAccount`, whose `sign_typed_data` returns a
`SignedMessage`, a three-line adapter does it:

```python
class AccountSigner:
    def __init__(self, account):
        self.account = account

    def sign_typed_data(self, typed_data):
        return "0x" + self.account.sign_typed_data(full_message=typed_data).signature.hex()
```

then `signer=AccountSigner(account)`.

The pointer is EIP-712 typed data (`pointer_typed_data` gives it to a wallet),
`seq` defaults to the current Unix second, and the gateway takes only a `seq`
above the last one it holds from the same signer. The chain id and registry
are the deployment the gateway verifies against.

## API at a glance

| Export                                                                                  | What it does                                                                                                                                                                  |
| --------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `Client(gateway_url, ...)`, `AsyncClient(gateway_url, ...)`                             | A client on a public or loopback gateway (`pointer`, `transport`, `verify`, `manifest_cache_size`, `timeout`); a context manager                              |
| `client.get(uri, range=, verify=, retry=)`                                              | Fetches a blob, a bundle, a bundle entry or a range as a `Response` / `AsyncResponse` (`verified`, `iter_bytes`, `read`, `text`, `json`)                                      |
| `client.head(uri)`                                                                      | The size, hash and content type of what a URI names, without its bytes                                                                                                        |
| `client.resolve(uri)`                                                                   | A URI's kind, hash, bare and site URLs, and its manifest entry (reads the manifest for a bundle or entry)                                                                     |
| `client.manifest(hash_or_uri)`                                                          | A bundle's verified manifest, cached per client                                                                                                                               |
| `client.site_url(uri)`, `client.namespace_url(ns)`                                      | The site URL of a bundle or entry; the URL a namespace's pointer serves at (`https://<ns>.<host>/`). Neither makes a request                                                  |
| `client.loopback.delegate()`                                                            | The loopback gateway's pending delegate key: the same address until a capability claims it                                                                                    |
| `client.loopback.register_capability(capability, scope)`, `.capabilities()`             | Registers a `dcap1:` capability issued to a key the gateway holds, for scope `"any"` or a `NamespaceScope`; lists the capabilities the gateway holds, in registration order, with the `namespaces` of a scoped one |
| `client.pointer.set(namespace=, root=, seq=, signer=)`                                  | Signs and publishes a namespace pointer on a public gateway                                                                                                                   |
| `pointer_typed_data`, `pointer_signing_hash`, `sign_pointer`, `recover_pointer_signer`  | The pointer's EIP-712 typed data and signing hash, its signature, and the signer a signature recovers to                                                                      |
| `parse_uri`, `format_uri`                                                               | Parse and canonically format a `decdn:` URI, following the Rust client's grammar exactly (`BAD_URI` on anything it refuses)                                                   |
| `hash_to_base32`, `base32_to_hash`                                                      | A hash to and from its site label                                                                                                                                             |
| `blake3_hex`, `create_blake3`                                                           | BLAKE3, one-shot and incremental                                                                                                                                              |
| `parse_manifest`, `parse_capability`                                                    | Decodes a v1 bundle manifest, checking its hash when given one; decodes a `dcap1:` token without checking its signature                                                       |
| `DecdnError`                                                                            | Every failure, with a stable `code` ([Errors](#errors))                                                                                                                       |

Namespaces, sequence numbers, caps and expiries are `int`.

## Development

```sh
uv sync --frozen
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest        # unit tests and the contract suite against the fake gateway
uv build             # dist/: sdist and wheel
```

`tests/fake_gateway.py` reproduces the gateway's HTTP contract. The contract
suite in `tests/contract` runs against it by default, and against a real
gateway in decdn's CI
([`sdk_contract.rs`](https://github.com/decdn/decdn/blob/main/crates/e2e/src/sdk_contract.rs)),
so the two cannot drift apart. See [CONTRIBUTING.md](CONTRIBUTING.md).

Releases are cut from signed `v*` tags ([RELEASING.md](RELEASING.md)); the
keys that sign them are in [`KEYS`](KEYS) (`gpg --import KEYS`, then
`git verify-tag vX.Y.Z`).

## License

Dual-licensed under either [MIT](LICENSE-MIT) or [Apache-2.0](LICENSE-APACHE),
at your option.
