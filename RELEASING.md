# Releasing decdn (Python)

Releases are cut locally by `scripts/release.sh` and published from its signed
tag by `scripts/publish.sh`, which uploads to PyPI with `uv publish` and makes
the GitHub release. CI builds and tests, but holds no PyPI token and publishes
nothing: a `push`-triggered workflow runs the workflow definition from the
pushed ref, so a token reachable from a workflow would be reachable from any
tag anyone with push access could craft.

The SDK speaks HTTP to a `decdn gateway`. It has its own semver; the gateway
release it needs is a compatibility floor, not a shared version.

## One-time setup

Before the first release, confirm that you own the `decdn` project on PyPI (or
can create it) and add a second maintainer, so the package outlives one
account. `publish.sh` uploads with `uv publish` and needs PyPI credentials for
the project: a PyPI API token in `UV_PUBLISH_TOKEN`, or `UV_PUBLISH_PASSWORD`,
or `UV_KEYRING_PROVIDER=subprocess` with the token in your keyring. It also
needs `gh auth login`, with an account that has write access to
`decdn/sdk-python` to make the GitHub release. It runs on a maintainer's
machine and refuses to upload in CI (`CI` or `GITHUB_ACTIONS` set).

`KEYS` holds the maintainer keys a release tag may be signed by. It is a
verbatim copy of [decdn's `KEYS`](https://github.com/decdn/decdn/blob/main/KEYS):
a key added or revoked there is added or revoked here in the same sitting.

## Cutting a version

A version is cut on `main`, with no release PR, by
[`scripts/release.sh`](scripts/release.sh), a wrapper around `uv version` and
[git-cliff](https://git-cliff.org) in the manner of `cargo release`. Publishing
is the separate step after it:

```bash
scripts/release.sh                    # dry run: the new tag and the diff
scripts/release.sh --execute          # bump, changelog, check, commit, tag, push
DECDN_PYPI_DRY_RUN=1 scripts/publish.sh v0.2.0   # rehearse, print the notes
scripts/publish.sh v0.2.0                        # PyPI, then GitHub release
```

The argument is the level: `auto` (the default), `patch`, `minor`, `major` or
an explicit `X.Y.Z`. Without `--execute`, `release.sh` prints the new tag and
the diff it would commit, and changes no file. It:

1. **Checks the tree, the key and the floor.** It runs on a clean `main` equal
   to `origin/main`, with every local `vX.Y.Z` tag on origin, and
   `pyproject.toml` at the last release's version. With `--execute`, it signs
   a throwaway commit and verifies it against `KEYS` alone, as `publish.sh`
   verifies the tag. It refuses unless the decdn release `gateway-floor` names
   is published (see below), and tells a release GitHub does not have from a
   `gh` that cannot ask.
2. **Refuses unconventional commits.** git-cliff drops a commit it cannot parse
   as a Conventional Commit, from the changelog and from the bump, and only
   logs a count: an unconventional subject, but also an empty scope
   (`feat(): …`) and a body right under the subject with no blank line. The
   script has git-cliff name every such commit since the last tag
   (`require_conventional`) and stops; `--allow-unconventional` releases
   without them.
3. **Picks the version.** `auto` is the bump git-cliff derives (`cliff.toml`)
   from the commits since the last tag. Before 1.0, a feature bumps the minor,
   and so does a breaking change from 0.1 on (at 0.0.x it bumps the patch); any
   other commit kept in the changelog bumps the patch. It refuses when none of
   them makes a changelog entry, whatever the level.
4. **Writes the changelog section.** It renders the same commits into a dated
   `## [X.Y.Z] — YYYY-MM-DD` section at the top of `CHANGELOG.md`: `feat`
   under Added, `fix` under Fixed, `revert` under Removed, `security` under
   Security, and `perf`, `refactor`, `docs`, `chore` and any other type under
   Changed. `ci`, `test`, `style` and `build` commits are left out unless
   breaking (those go under Changed); release commits always are. A breaking
   commit (`!` or a `BREAKING CHANGE:` footer) starts with **Breaking:**. An
   entry is the subject without its type, the scope in bold and the first
   letter capitalised: `fix(client): keep the query (#12)` becomes
   `- **client**: Keep the query (#12)`. PRs are squash-merged with the PR
   title as the subject, so the PR title is the changelog entry.
5. **Bumps and checks** (`--execute` only): `uv version X.Y.Z --frozen`
   (`pyproject.toml`), `uv lock` (`uv.lock`) and a rewrite of `__version__`
   in `src/decdn/_version.py`, then the gate (`ruff check`,
   `ruff format --check`, `mypy`, `pytest`), `uv build` and
   `scripts/check-package.sh` (`--no-verify` skips the checks). The commit
   holds four files, those three and `CHANGELOG.md`, each the diff it showed,
   byte for byte, or it stops.
6. **Commits** (`--execute` only) `chore(release): vX.Y.Z` and tags `vX.Y.Z`
   (`decdn X.Y.Z`), both signed, and pushes them with
   `git push --atomic origin main vX.Y.Z`: origin gets both or neither. If a
   check or the commit fails, the files are restored and nothing is committed.
   If tagging or the push fails, or the script is stopped after the commit, it
   says how to undo: after a failed push, once it has checked what origin got;
   when stopped, with the command that checks it. A refusal never touches the
   working tree.

**Prerequisites.**

- [git-cliff](https://git-cliff.org) 2.14 or later, for `require_conventional`
  (`uv sync` installs it), [uv](https://docs.astral.sh/uv/), and `gh`, logged
  in.
- `gpg`, and git configured to sign with an OpenPGP key published in `KEYS`
  (`user.signingkey`, `gpg.format` unset or `openpgp`). The script verifies a
  throwaway signature against `KEYS` before the checks, so a key that cannot
  sign, or one `publish.sh` would refuse, fails before anything is pushed.
- A bypass of the `main` ruleset (an org admin), because the release commit
  goes straight to `main`, where the ruleset's pull-request rule and required
  status checks apply to a direct push too.
- The repository settings the changelog relies on: squash merges only, with
  the PR title as the squash commit's subject (_Settings → General → Pull
  Requests_), and `pr-title` a required status check.
- CI green on the commit.

**The first release.** `pyproject.toml` starts at the `0.0.0` placeholder, and
the first release names its level: `scripts/release.sh minor --execute` cuts
`0.1.0`. Without a tag, git-cliff would render the whole history, so the first
release does not use it: it renames the changelog's hand-written
`## [Unreleased]` section to the dated version instead, so until then a
user-visible PR adds its entry there by hand. After that, no `[Unreleased]`
section is kept: the script refuses one, because every later section is
generated.

**A prerelease** follows PEP 440 (`0.2.0rc1`, tagged `v0.2.0rc1`) and is not
cut by `release.sh`. In a PR, set `version` to it (`uv version 0.2.0rc1
--frozen`), set `__version__` in `src/decdn/_version.py` to match and refresh
`uv.lock` (`uv lock`); tag the merged commit by hand with `git tag -s` and push
the tag, publish it, then set all three back to the last release in the next
PR: `release.sh` refuses a `pyproject.toml` that is not at the last `vX.Y.Z`
tag, and a `_version.py` out of step with it. Prerelease tags are not changelog
boundaries, so the commits up to the prerelease fall into the next release's
generated section.

`publish.sh` refuses a tag that is not `v<version>`, verifies the tag against
`KEYS` (and against `DECDN_SIGNING_KEY` when set) and against `origin`, checks
that gh's account can write to `decdn/sdk-python`, checks the tag out into a
detached worktree, checks that `pyproject.toml` names `decdn` at the tag's
version and that `CHANGELOG.md` has its section, and runs sync, the gate and
`uv build` there before `uv publish`. It then makes the tag's GitHub release,
titled with the tag, its notes the tag's `CHANGELOG.md` section followed by
the PyPI link and the two floors (`gateway-floor`, `harness-floor`). A
prerelease goes to PyPI as a pre-release, which `pip install` skips unless
asked (`--pre`); its GitHub release is a prerelease, never the latest, and
with no changelog section its notes are the PyPI link and floors alone. A
draft release for the tag stops the script before the upload: publish or
delete it first.

**A PyPI version is published once.** It is immutable: a filename can never be
uploaded again, even after a delete. A broken release is fixed by the next
patch version and a yank of the broken one. PyPI takes the sdist and the
wheel as separate uploads, so running `publish.sh` again uploads only the
files PyPI lacks for the version (none when it has both) and skips the GitHub
release when it exists: it finishes a run whose upload or GitHub step failed.
An existing release's notes are left as they are.

## The two floors

`[tool.decdn]` in `pyproject.toml` holds two floors, and the README names
them:

- **`gateway-floor`** is the oldest `decdn gateway` release whose HTTP
  contract the SDK supports. Raise it in the same PR as any change that
  relies on newer gateway behaviour, and check it before every release: an
  SDK whose floor names an unreleased gateway does not ship. The first floor
  is the release that carries
  [cross-origin reads](https://github.com/decdn/decdn/blob/main/adr/appendix-gateway.md#cross-origin-reads).
- **`harness-floor`** is the first decdn release whose e2e harness runs this
  SDK's contract suite. It is empty until that release exists, and while it
  is empty the released-gateway run does not start. Set it to the first decdn
  release that ships the Python harness, and raise it when a suite change no
  longer reads what that harness writes.
