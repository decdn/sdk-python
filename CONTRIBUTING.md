# Contributing to decdn (Python)

The SDK is a pure-Python client over the `decdn gateway`'s HTTP contract
([appendix-gateway](https://github.com/decdn/decdn/blob/main/adr/appendix-gateway.md)).
The contract, not this repo, is the source of truth: when the SDK and the
gateway disagree, the gateway's spec decides.

## Setup

Python 3.11+ and [uv](https://docs.astral.sh/uv/):

```bash
uv sync --frozen
uv run ruff check .          # lint
uv run ruff format --check . # formatting (line length 110)
uv run mypy                  # strict type check of src and tests
uv run pytest                # unit tests, script tests, contract suite against the fake gateway
uv run pytest --cov          # the same, with line and branch coverage of src/decdn
uv build                     # dist/: sdist and wheel
scripts/check-package.sh     # twine check and a wheel smoke test in a fresh venv
```

The gate is `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest`.

Commits and PR titles follow [Conventional Commits](https://www.conventionalcommits.org/).
PRs are squash-merged with the PR title as the commit subject, and
`scripts/release.sh` renders those subjects into `CHANGELOG.md` with git-cliff
at release (see [RELEASING.md](RELEASING.md)): write a title a user can read as
a changelog entry. Until the first release, a user-visible PR adds its entry
to the hand-written `## [Unreleased]` section of `CHANGELOG.md`; after it,
never edit `CHANGELOG.md` by hand.

Install the git hooks once with `uv run pre-commit install --install-hooks
--hook-type pre-commit --hook-type commit-msg`.
`pre-commit` runs ruff and ruff-format on the staged files and zizmor on the
workflows; `commit-msg` checks the message is a Conventional Commit
(`.pre-commit-config.yaml`). `git commit --no-verify` skips them for one
commit.

## CI

`.github/workflows/ci.yml` runs on every PR and push to `main`:

| Job       | What it runs                                                                                                                                     | Locally                                                                 |
| --------- | ------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------- |
| `test`    | lint, mypy, a git-cliff parse check, tests with coverage and build on Python 3.11, 3.12 and 3.13; 3.13 posts the coverage to GitHub Code Quality | `uv run ruff check . && uv run mypy && uv run pytest --cov && uv build` |
| `package` | twine check and the wheel's contents                                                                                                             | `scripts/check-package.sh`                                              |
| `lint-ci` | zizmor (policy in `zizmor.yml`) and shellcheck                                                                                                   | `uvx zizmor .github && shellcheck scripts/*.sh`                         |

Separate workflows check the PR title (`PR title`) and run CodeQL on the
Python and the workflows (`CodeQL`).

## The contract suite: fake and real gateway

`tests/fake_gateway.py` reproduces the gateway's statuses, headers and JSON
bodies. The contract suite (`tests/contract`) runs against it here, and
against the real binary in decdn's e2e harness. So:

- A gateway change the SDK can observe needs a PR here first, which updates
  the fake and the suite.
- A contract change here must pass against the real gateway before it merges.

Each file runs one mode: `tests/contract/test_public.py` the public gateway,
`tests/contract/test_loopback.py` the loopback one. `DECDN_CONTRACT` names the
JSON fixture file decdn's harness writes; the file's `mode` must match the
file under test, and a missing `DECDN_CONTRACT` runs the fake. A real run also
sets `DECDN_CONTRACT_REQUIRED=1`, so a lost `DECDN_CONTRACT` fails instead of
quietly testing the fake:

```bash
DECDN_CONTRACT=/path/to/public.json DECDN_CONTRACT_REQUIRED=1 uv run pytest tests/contract/test_public.py
DECDN_CONTRACT=/path/to/loopback.json DECDN_CONTRACT_REQUIRED=1 uv run pytest tests/contract/test_loopback.py
```

The fixture carries extended fields the suite reads when present:
`capabilities` (loopback: a `dcap1:` token issued to a delegate the gateway
holds, with the host it is for) and `pointer.namespaces` (public: the
namespaces the signing key owns on the registry). The fixture is a contract
between decdn's harness and this suite: a suite change must still read what
the floor's harness writes, or raise `harness-floor` (see
[RELEASING.md](RELEASING.md#the-two-floors)).

The vectors in `tests/fixtures` are pinned on the Rust side too: change both
together.

## Releases

See [RELEASING.md](RELEASING.md).
