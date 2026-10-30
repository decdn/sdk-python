# AGENTS.md

`decdn` (PyPI): a pure-Python package that speaks HTTP to a `decdn gateway`
(public or loopback), for Python 3.11+. No iroh. It mirrors the TypeScript
SDK, `@decdn/client` (`decdn/sdk-typescript`).

## Source of truth

The gateway's HTTP contract is
[`decdn/adr/appendix-gateway.md`](https://github.com/decdn/decdn/blob/main/adr/appendix-gateway.md),
implemented in decdn's `crates/cli/src/commands/gateway.rs`. Every technical
claim in this repo's docs cites decdn's ADRs by absolute GitHub URL. If the
SDK and the gateway disagree, fix the SDK.

## Commands

```bash
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
scripts/check-package.sh  # after `uv build`: twine check + wheel contents
```

## Rules

- **The fake gateway mirrors the real one.** The fake gateway in `tests/`
  must match the gateway's statuses, headers and JSON shapes the SDK reads.
  The contract suite also runs against the real binary: in decdn's e2e
  harness, at the commit decdn pins, and here against released gateways. A
  change the gateway does not do fails both.
- **Verification is the product.** A whole body named by hash is hashed as
  it streams; only a window the caller asked for, or a page the gateway
  renders, goes unhashed. Never downgrade `verified` on a gateway's say-so.
- **u64 values are `int` and exact.** Namespaces, seq, caps and expiries
  exceed 2^53, and Python ints carry them; never go through `float`.
- **Released gateways and two floors.** `[tool.decdn]` in `pyproject.toml`
  holds two floors. `gateway-floor` (0.3.0) is the oldest gateway whose HTTP
  contract the SDK supports, checked at release. `harness-floor` is the
  first decdn release whose e2e harness runs this SDK's contract suite; it
  is empty until that release exists. The fixture file decdn's harness
  writes is a contract: a suite change must still read what the floor's
  harness writes, or raise the floor.
- **Pinned vectors.** Capability and pointer vectors under `tests/fixtures`
  are pinned by Rust tests in decdn; change both sides together.
- **Present tense.** Comments, docstrings and docs describe what the code
  does, not its history.
- **Conventional commits** (the `commit-msg` pre-commit hook checks commit
  messages; PR titles follow the same format). PRs are squash-merged, so the
  PR title is the changelog entry: the release script renders `CHANGELOG.md`
  sections with git-cliff. Until the first release, user-visible changes go
  in the hand-written `[Unreleased]` section; after it, never edit
  `CHANGELOG.md` by hand or add an `[Unreleased]` section. Releases follow
  RELEASING.md.
