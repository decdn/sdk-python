#!/usr/bin/env bash
# Publishes `decdn` to PyPI from a signed `v<version>` tag, then makes the tag's
# GitHub release, its notes the tag's CHANGELOG.md section, the PyPI link and
# the two floors.
#
# Like decdn's publish-crates.sh, this is a human step with no counterpart in
# Actions: there is no PyPI token in Actions secrets, and a `push`-triggered
# workflow runs the workflow definition from the pushed ref, so a token
# reachable from a workflow would be reachable from any tag anyone with push
# access could craft. The script refuses to upload from CI.
#
# Usage:  scripts/publish.sh v0.1.0
#
# A prerelease (PEP 440: `v0.2.0rc1`, `v0.2.0a1`, `v0.2.0b1`, `v0.2.0.dev1`)
# is a pre-release on PyPI, which `pip install decdn` skips, and its GitHub
# release is a prerelease, never the latest.
#
# Environment:
#   UV_PUBLISH_TOKEN    a PyPI API token for decdn; or UV_PUBLISH_PASSWORD, or
#                       UV_KEYRING_PROVIDER=subprocess with the token in the
#                       operator's keyring (`uv publish` reads them)
#   DECDN_SIGNING_KEY   key the tag is expected to be signed by (default: any
#                       key in KEYS, vendored from decdn/decdn)
#   DECDN_PYPI_DRY_RUN  set to 1/true/yes to run every check but the
#                       credentials, and `uv publish --dry-run` unless PyPI has
#                       the version, uploading nothing and printing the release
#                       notes instead of releasing
#
# A PyPI file is immutable and can never be uploaded again, even after a
# delete. A re-run uploads only the files PyPI does not have for the version,
# and skips the GitHub release when it exists, so it finishes a run whose
# upload or GitHub step failed.
set -euo pipefail

TAG="${1:?tag required, e.g. v0.1.0}"
SIGNING_KEY="${DECDN_SIGNING_KEY:-}"
REPO=decdn/sdk-python
NAME=decdn

die() { echo "error: $*" >&2; exit 1; }

# `1`/`true`/`yes` enable, `0`/`false`/`no`/empty do not, anything else is a
# typo and stops the script — the same rule as publish-crates.sh.
enabled() {
  local name="$1" value="${2:-}"
  case "${value,,}" in
    ''|0|false|no) return 1 ;;
    1|true|yes)    return 0 ;;
    *) die "$name must be 1/true/yes or 0/false/no, got '$value'" ;;
  esac
}

# PEP 440 in its normalised form, without local or post segments: PyPI
# normalises `0.2.0-rc.1` to `0.2.0rc1`, so two tags would name one version.
PEP440='(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)((a|b|rc)(0|[1-9][0-9]*))?(\.dev(0|[1-9][0-9]*))?'
[[ "$TAG" =~ ^v${PEP440}$ ]] ||
  die "the tag must be v<major>.<minor>.<patch>[{a|b|rc}<n>][.dev<n>], got '$TAG'"
VERSION="${TAG#v}"
PRERELEASE=""
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || PRERELEASE=1

DRY_RUN=""
if enabled DECDN_PYPI_DRY_RUN "${DECDN_PYPI_DRY_RUN:-}"; then
  DRY_RUN=1
fi

# ---- preconditions -------------------------------------------------------

for tool in git gh gpg uv curl python3; do
  command -v "$tool" >/dev/null || die "$tool not found on PATH"
done
python3 -c 'import tomllib' 2>/dev/null || die "python3 has no tomllib: Python 3.11 or later is required"

if [[ -z "$DRY_RUN" ]]; then
  [[ -z "${CI:-}" && -z "${GITHUB_ACTIONS:-}" ]] ||
    die "publish.sh uploads from a maintainer's machine, not in CI (DECDN_PYPI_DRY_RUN=1 runs the checks)"
  [[ -n "${UV_PUBLISH_TOKEN:-}" || -n "${UV_PUBLISH_PASSWORD:-}" || "${UV_KEYRING_PROVIDER:-}" == subprocess ]] ||
    die "no PyPI credentials: set UV_PUBLISH_TOKEN to a PyPI API token for $NAME, or UV_KEYRING_PROVIDER=subprocess with it in your keyring"
fi
# The account's role on $REPO, asked before PyPI is touched: without write
# access the GitHub release would fail only after the upload. A fine-grained
# token's own grant is not part of the answer.
push=$(GH_PROMPT_DISABLED=1 gh api "repos/$REPO" --jq .permissions.push 2>&1) ||
  die "gh cannot read $REPO ($push); run \`gh auth login\` with write access to it"
[[ "$push" == true ]] || die "gh's account has no write access to $REPO"

REPO_ROOT=$(git rev-parse --show-toplevel) || die "not inside a git repository"
KEYS_FILE="$REPO_ROOT/KEYS"
[[ -f "$KEYS_FILE" ]] || die "KEYS not found at $KEYS_FILE"

# A gpg home containing ONLY the published maintainer keys, as in decdn's
# publish-crates.sh: verifying against your own keyring only proves you can
# read a signature you already trust.
KEYS_HOME=$(mktemp -d)
chmod 700 "$KEYS_HOME"
# The release notes and the probes' answers.
SCRATCH=$(mktemp -d)
WORKTREE=""
cleanup() {
  local rc=$? err
  rm -rf "$KEYS_HOME" "$SCRATCH"
  if [[ -n "${WORKTREE:-}" && -d "${WORKTREE:-}" ]]; then
    err=$(git -C "$REPO_ROOT" worktree remove --force "$WORKTREE" 2>&1) || {
      echo "warning: could not remove the worktree at $WORKTREE: $err" >&2
      echo "         run \`git worktree prune\` to clear the stale entry" >&2
    }
  fi
  return $rc
}
trap cleanup EXIT

gpg --homedir "$KEYS_HOME" --import "$KEYS_FILE" >/dev/null 2>&1 ||
  die "KEYS is not a valid OpenPGP keyring"

# ---- the tag must be the one that was pushed, signed ---------------------

# --force: `git fetch --tags` keeps a stale local tag otherwise.
git fetch --tags --force origin >/dev/null 2>&1 ||
  die "cannot reach origin to confirm the tag"
git rev-parse -q --verify "refs/tags/${TAG}" >/dev/null ||
  die "tag $TAG does not exist"

echo "==> Verifying $TAG"
GNUPGHOME="$KEYS_HOME" git verify-tag "$TAG" ||
  die "$TAG is not signed by a key published in KEYS"

if [[ -n "$SIGNING_KEY" ]]; then
  # Primary key fingerprints, compared whole (see decdn's publish-crates.sh for
  # why a VALIDSIG grep is vacuous and why the subkey field is the wrong one).
  sig_fpr=$(GNUPGHOME="$KEYS_HOME" git verify-tag --raw "$TAG" 2>&1 |
    awk '$2 == "VALIDSIG" {print (NF >= 12 ? $NF : $3); exit}')
  [[ -n "$sig_fpr" ]] || die "could not read the signature fingerprint from $TAG"
  mapfile -t want_fprs < <(
    gpg --homedir "$KEYS_HOME" --list-keys --with-colons "$SIGNING_KEY" 2>/dev/null |
      awk -F: '/^pub:/ {want = 1} /^fpr:/ && want {print $10; want = 0}'
  )
  case ${#want_fprs[@]} in
    0) die "DECDN_SIGNING_KEY '$SIGNING_KEY' matches no key published in KEYS" ;;
    1) ;;
    *) die "DECDN_SIGNING_KEY '$SIGNING_KEY' is ambiguous — it matches ${#want_fprs[@]} keys in KEYS.
Use a full fingerprint." ;;
  esac
  [[ "$sig_fpr" == "${want_fprs[0]}" ]] ||
    die "$TAG is signed by $sig_fpr, not by $SIGNING_KEY (${want_fprs[0]})"
fi

LOCAL_COMMIT=$(git rev-parse "refs/tags/${TAG}^{commit}")
# `^{}` asks the remote for the commit an annotated (signed) tag peels to.
REMOTE_COMMIT=$(git ls-remote origin "refs/tags/${TAG}^{}" | awk '{print $1}')
[[ -n "$REMOTE_COMMIT" ]] || die "tag $TAG is not on origin: push it first"
[[ "$LOCAL_COMMIT" == "$REMOTE_COMMIT" ]] || die \
  "local tag $TAG ($LOCAL_COMMIT) differs from origin's ($REMOTE_COMMIT).
PyPI would receive code that was never tagged there. Reconcile the tags first."

# ---- build and publish from the tagged tree ------------------------------

# A detached worktree at the tag, not the current checkout: the working copy
# may carry uncommitted edits, and `uv build` would package whatever is on disk.
WORKTREE=$(mktemp -d)
rmdir "$WORKTREE"
git -C "$REPO_ROOT" worktree add --detach "$WORKTREE" "$TAG" >/dev/null 2>&1 ||
  die "could not create a worktree at $TAG"
cd "$WORKTREE"

# <key>: a string at that dotted path of pyproject.toml, or nothing.
toml() {
  python3 -c 'import sys, tomllib
v = tomllib.load(open("pyproject.toml", "rb"))
for k in sys.argv[1].split("."):
    v = v.get(k) if isinstance(v, dict) else None
if isinstance(v, str):
    print(v)' "$1"
}
PKG_NAME=$(toml project.name) || die "cannot read pyproject.toml at $TAG"
PKG_VERSION=$(toml project.version) || die "cannot read pyproject.toml at $TAG"
[[ "$PKG_NAME" == "$NAME" ]] || die "the package at $TAG is $PKG_NAME, not $NAME"
[[ "$PKG_VERSION" == "$VERSION" ]] ||
  die "pyproject.toml is at $PKG_VERSION, but the tag says $VERSION"
MODULE_VERSION=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' src/decdn/_version.py 2>/dev/null) || MODULE_VERSION=""
[[ "$MODULE_VERSION" == "$VERSION" ]] ||
  die "src/decdn/_version.py at $TAG has __version__ '$MODULE_VERSION', but the tag says $VERSION"
echo "==> $NAME $VERSION${PRERELEASE:+ (a prerelease)}"

# The release notes: the tag's CHANGELOG.md section without its heading, then
# where to get the package and the gateway it needs. A prerelease has no
# section (its commits fall into the next release's), so its notes are the
# footer alone; a release without one stops here, before anything is uploaded.
FLOOR=$(toml tool.decdn.gateway-floor) || die "cannot read pyproject.toml at $TAG"
[[ "$FLOOR" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] ||
  die "pyproject.toml at $TAG has [tool.decdn] gateway-floor '$FLOOR', not X.Y.Z"
HARNESS=$(toml tool.decdn.harness-floor) || die "cannot read pyproject.toml at $TAG"
[[ -z "$HARNESS" || "$HARNESS" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] ||
  die "pyproject.toml at $TAG has [tool.decdn] harness-floor '$HARNESS', not X.Y.Z or empty"
NOTES="$SCRATCH/notes.md"
awk -v head="## [$VERSION] " '
  index($0, head) == 1 { inside = 1; next }
  inside && /^## \[/ { exit }
  inside && NF { while (blanks > 0) { print ""; blanks-- }; started = 1; print; next }
  inside && started { blanks++ }
' CHANGELOG.md >"$NOTES" || die "cannot read CHANGELOG.md at $TAG"
RULE=""
if [[ -s "$NOTES" ]]; then
  RULE=$'\n---\n\n'
elif [[ -z "$PRERELEASE" ]]; then
  die "CHANGELOG.md at $TAG has no '## [$VERSION]' section"
fi
INSTALL="$NAME"
[[ -z "$PRERELEASE" ]] || INSTALL="$NAME==$VERSION"
if [[ -n "$HARNESS" ]]; then
  HARNESS_NOTE=" Its contract suite runs against real gateways in decdn's e2e harness from [v$HARNESS](https://github.com/decdn/decdn/releases/tag/v$HARNESS)."
else
  HARNESS_NOTE=" No decdn release's e2e harness runs its contract suite yet."
fi
cat >>"$NOTES" <<EOF
${RULE}PyPI: [\`$NAME $VERSION\`](https://pypi.org/project/$NAME/$VERSION/) (\`pip install $INSTALL\`). It needs \`decdn gateway\` [v$FLOOR](https://github.com/decdn/decdn/releases/tag/v$FLOOR) or later.$HARNESS_NOTE
EOF

# The worktree's own environment, not one the maintainer's shell has active.
# `env` runs the uv on PATH, not this function.
uv() { env -u VIRTUAL_ENV -u UV_PROJECT_ENVIRONMENT uv "$@"; }
echo "==> Sync, lint, test, build"
uv sync --locked
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen mypy
uv run --frozen pytest
uv build
DIST=(dist/*)
if [[ ${#DIST[@]} -ne 2 || ! -f "dist/$NAME-$VERSION.tar.gz" ]] || ! compgen -G "dist/$NAME-$VERSION-*.whl" >/dev/null; then
  die "uv build made other files than one $NAME-$VERSION sdist and wheel: ${DIST[*]}"
fi
uv run --frozen twine check --strict "${DIST[@]}"

RELEASE=(release create "$TAG" --repo "$REPO" --verify-tag --title "$TAG" --notes-file "$NOTES")
[[ -z "$PRERELEASE" ]] || RELEASE+=(--prerelease --latest=false)
# PyPI refuses a file it already has, so the upload takes only the files it
# lacks, and none when it has them all, as after a run whose GitHub step
# failed. Only PyPI's 404 means it has no file of this version: any other
# failure to ask stops here, before the upload.
ON_PYPI=()
code=$(curl --silent --show-error --location --output "$SCRATCH/pypi.json" --write-out '%{http_code}' \
  "https://pypi.org/pypi/$NAME/$VERSION/json" 2>"$SCRATCH/pypi.err") || code=""
case "$code" in
  200)
    mapfile -t ON_PYPI < <(python3 -c 'import json, sys
for u in json.load(open(sys.argv[1]))["urls"]:
    print(u["filename"], "sha256:" + u["digests"]["sha256"])' "$SCRATCH/pypi.json") ||
      die "cannot read PyPI's answer for $NAME $VERSION" ;;
  404) ;;
  *)
    cat "$SCRATCH/pypi.err" >&2
    die "cannot ask PyPI whether it has $NAME $VERSION${code:+ (HTTP $code)}" ;;
esac
UPLOAD=()
for file in "${DIST[@]}"; do
  printf '%s\n' "${ON_PYPI[@]}" | grep -q "^${file#dist/} " || UPLOAD+=("$file")
done
# `gh release view` answers a draft too: a draft is not a release, and one for
# this tag stops the run, before the upload.
HAS_RELEASE=""
if draft=$(GH_PROMPT_DISABLED=1 gh release view "$TAG" --repo "$REPO" \
  --json isDraft --jq .isDraft 2>"$SCRATCH/gh.err"); then
  [[ "$draft" == false ]] ||
    die "$REPO has a draft release for $TAG: publish or delete it, then run again"
  HAS_RELEASE=1
elif ! grep -qx 'release not found' "$SCRATCH/gh.err"; then
  cat "$SCRATCH/gh.err" >&2
  die "cannot ask GitHub whether $REPO has the release $TAG"
fi

if [[ -n "$DRY_RUN" ]]; then
  echo "==> Dry run"
  if ((${#UPLOAD[@]} == 0)); then
    echo "==> $NAME $VERSION is already on PyPI; it would not be uploaded again"
  else
    uv publish --dry-run "${UPLOAD[@]}"
  fi
  if [[ -n "$HAS_RELEASE" ]]; then
    echo "==> $REPO already has the release $TAG; it would be left as is"
  else
    echo "==> The GitHub release would be: gh ${RELEASE[*]}"
  fi
  echo "--- notes ---"
  cat "$NOTES"
  echo "-------------"
  echo "==> Dry run of $NAME $VERSION passed; nothing was uploaded or released"
  exit 0
fi

if ((${#UPLOAD[@]} == 0)); then
  echo "==> $NAME $VERSION is already on PyPI; not uploading it again"
  echo "    the release adopts PyPI's files:"
  printf '      %s\n' "${ON_PYPI[@]}"
else
  ((${#ON_PYPI[@]} == 0)) || echo "==> PyPI has part of $NAME $VERSION; uploading the rest"
  echo "==> Publishing $NAME $VERSION: ${UPLOAD[*]#dist/}"
  uv publish "${UPLOAD[@]}"
  echo "==> Published. Check: https://pypi.org/project/$NAME/$VERSION/"
fi

if [[ -n "$HAS_RELEASE" ]]; then
  echo "==> $REPO already has the release $TAG; left as is"
  exit 0
fi
echo "==> Creating the GitHub release $TAG"
GH_PROMPT_DISABLED=1 gh "${RELEASE[@]}" || die \
  "PyPI has $NAME $VERSION, but the GitHub release failed (gh's error above).
Once it is fixed, run \`scripts/publish.sh $TAG\` again: the upload is skipped."
echo "==> Released. Check: gh release view $TAG --repo $REPO"
