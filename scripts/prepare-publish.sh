#!/usr/bin/env bash
# Checks a signed `v<version>` tag of `decdn` and builds it for PyPI. The
# `prepare` job of .github/workflows/publish.yml runs it, with no OIDC access:
# the upload is the `publish` job's, and the GitHub release the
# `github-release` job's.
#
# Usage:  scripts/prepare-publish.sh <tag> <src> <keys> <out>
#
#   <tag>   the release tag, e.g. v0.1.0
#   <src>   a checkout of the tag; it is built, never trusted
#   <keys>  the KEYS the tag must be signed by, from the trusted checkout: a
#           KEYS the tag carried would vouch for itself
#   <out>   a new directory for what the later jobs take:
#             dist/      the sdist and the wheel
#             notes.md   the GitHub release's notes: the tag's CHANGELOG.md
#                        section, the PyPI link and the two floors
#             outputs    key=value lines for $GITHUB_OUTPUT:
#                        version, prerelease (true/false), pending (PyPI lacks
#                        a file of the version) and has_release (the GitHub
#                        release exists)
#
# A prerelease (PEP 440: `v0.2.0rc1`, `v0.2.0a1`, `v0.2.0b1`, `v0.2.0.dev1`)
# needs no CHANGELOG.md section; its notes are the footer alone.
#
# A PyPI file is immutable, so everything that could refuse the release refuses
# it here, before the upload: the signature, the version in pyproject.toml and
# src/decdn/_version.py, the CHANGELOG.md section, the floors, the gate, the
# build and a draft release for the tag.
set -euo pipefail

TAG="${1:?usage: $0 <tag> <src> <keys> <out>}"
SRC="${2:?usage: $0 <tag> <src> <keys> <out>}"
KEYS_FILE="${3:?usage: $0 <tag> <src> <keys> <out>}"
OUT="${4:?usage: $0 <tag> <src> <keys> <out>}"
REPO=decdn/sdk-python
NAME=decdn

die() { echo "error: $*" >&2; exit 1; }

# PEP 440 in its normalised form, without local or post segments: PyPI
# normalises `0.2.0-rc.1` to `0.2.0rc1`, so two tags would name one version.
PEP440='(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)((a|b|rc)(0|[1-9][0-9]*))?(\.dev(0|[1-9][0-9]*))?'
[[ "$TAG" =~ ^v${PEP440}$ ]] ||
  die "the tag must be v<major>.<minor>.<patch>[{a|b|rc}<n>][.dev<n>], got '$TAG'"
VERSION="${TAG#v}"
PRERELEASE=false
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || PRERELEASE=true

# ---- preconditions -------------------------------------------------------

for tool in git gh gpg uv curl python3; do
  command -v "$tool" >/dev/null || die "$tool not found on PATH"
done
python3 -c 'import tomllib' 2>/dev/null || die "python3 has no tomllib: Python 3.11 or later is required"

[[ -f "$KEYS_FILE" ]] || die "KEYS not found at $KEYS_FILE"
KEYS_FILE=$(realpath "$KEYS_FILE")
[[ ! -e "$OUT" ]] || die "$OUT exists: the output directory must be new"
mkdir -p "$OUT/dist"
OUT=$(realpath "$OUT")

# A gpg home containing ONLY the published maintainer keys: verifying against
# a keyring that holds other keys proves nothing about KEYS.
KEYS_HOME=$(mktemp -d)
chmod 700 "$KEYS_HOME"
SCRATCH=$(mktemp -d)
trap 'rm -rf "$KEYS_HOME" "$SCRATCH"' EXIT
gpg --homedir "$KEYS_HOME" --import "$KEYS_FILE" >/dev/null 2>&1 ||
  die "KEYS is not a valid OpenPGP keyring"

cd "$SRC" || die "cannot enter $SRC"

# ---- the checkout must be the tag, signed --------------------------------

git rev-parse -q --verify "refs/tags/${TAG}" >/dev/null ||
  die "tag $TAG does not exist in $SRC"
echo "==> Verifying $TAG"
GNUPGHOME="$KEYS_HOME" git verify-tag "$TAG" ||
  die "$TAG is not signed by a key published in KEYS"
[[ "$(git rev-parse "refs/tags/${TAG}^{commit}")" == "$(git rev-parse HEAD)" ]] ||
  die "$SRC is not checked out at $TAG"

# ---- the package at the tag ----------------------------------------------

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
echo "==> $NAME $VERSION$([[ "$PRERELEASE" == false ]] || echo ' (a prerelease)')"

# The release notes: the tag's CHANGELOG.md section without its heading, then
# where to get the package and the gateway it needs. A prerelease has no
# section (its commits fall into the next release's), so its notes are the
# footer alone; a release without one stops here, before anything is built.
FLOOR=$(toml tool.decdn.gateway-floor) || die "cannot read pyproject.toml at $TAG"
[[ "$FLOOR" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] ||
  die "pyproject.toml at $TAG has [tool.decdn] gateway-floor '$FLOOR', not X.Y.Z"
HARNESS=$(toml tool.decdn.harness-floor) || die "cannot read pyproject.toml at $TAG"
[[ -z "$HARNESS" || "$HARNESS" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] ||
  die "pyproject.toml at $TAG has [tool.decdn] harness-floor '$HARNESS', not X.Y.Z or empty"
NOTES="$OUT/notes.md"
awk -v head="## [$VERSION] " '
  index($0, head) == 1 { inside = 1; next }
  inside && /^## \[/ { exit }
  inside && NF { while (blanks > 0) { print ""; blanks-- }; started = 1; print; next }
  inside && started { blanks++ }
' CHANGELOG.md >"$NOTES" || die "cannot read CHANGELOG.md at $TAG"
RULE=""
if [[ -s "$NOTES" ]]; then
  RULE=$'\n---\n\n'
elif [[ "$PRERELEASE" == false ]]; then
  die "CHANGELOG.md at $TAG has no '## [$VERSION]' section"
fi
INSTALL="$NAME"
[[ "$PRERELEASE" == false ]] || INSTALL="$NAME==$VERSION"
if [[ -n "$HARNESS" ]]; then
  HARNESS_NOTE=" Its contract suite runs against real gateways in decdn's e2e harness from [v$HARNESS](https://github.com/decdn/decdn/releases/tag/v$HARNESS)."
else
  HARNESS_NOTE=" No decdn release's e2e harness runs its contract suite yet."
fi
cat >>"$NOTES" <<EOF
${RULE}PyPI: [\`$NAME $VERSION\`](https://pypi.org/project/$NAME/$VERSION/) (\`pip install $INSTALL\`). It needs \`decdn gateway\` [v$FLOOR](https://github.com/decdn/decdn/releases/tag/v$FLOOR) or later.$HARNESS_NOTE
EOF

# ---- gate and build ------------------------------------------------------

# The checkout's own environment, not one the caller's shell has active.
# `env` runs the uv on PATH, not this function.
uv() { env -u VIRTUAL_ENV -u UV_PROJECT_ENVIRONMENT uv "$@"; }
echo "==> Sync, lint, test, build"
uv sync --locked
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen mypy
uv run --frozen pytest
rm -rf dist
uv build
DIST=(dist/*)
if [[ ${#DIST[@]} -ne 2 || ! -f "dist/$NAME-$VERSION.tar.gz" ]] || ! compgen -G "dist/$NAME-$VERSION-*.whl" >/dev/null; then
  die "uv build made other files than one $NAME-$VERSION sdist and wheel: ${DIST[*]}"
fi
uv run --frozen twine check --strict "${DIST[@]}"
cp "${DIST[@]}" "$OUT/dist/"

# ---- what PyPI and GitHub have -------------------------------------------

# PyPI takes the sdist and the wheel as separate uploads, so a failed run can
# leave one. Only PyPI's 404 means it has no file of this version: any other
# failure to ask stops here, before the upload.
ON_PYPI=""
code=$(curl --silent --show-error --location --output "$SCRATCH/pypi.json" --write-out '%{http_code}' \
  "https://pypi.org/pypi/$NAME/$VERSION/json" 2>"$SCRATCH/pypi.err") || code=""
case "$code" in
  200)
    ON_PYPI=$(python3 -c 'import json, sys
for u in json.load(open(sys.argv[1]))["urls"]:
    print(u["filename"])' "$SCRATCH/pypi.json") ||
      die "cannot read PyPI's answer for $NAME $VERSION" ;;
  404) ;;
  *)
    cat "$SCRATCH/pypi.err" >&2
    die "cannot ask PyPI whether it has $NAME $VERSION${code:+ (HTTP $code)}" ;;
esac
PENDING=false
for file in "${DIST[@]}"; do
  if grep -qxF "${file#dist/}" <<<"$ON_PYPI"; then
    echo "==> PyPI has ${file#dist/}"
  else
    echo "==> PyPI lacks ${file#dist/}"
    PENDING=true
  fi
done

# `gh release view` answers a draft too: a draft is not a release, and one for
# this tag stops the run, before the upload.
HAS_RELEASE=false
if draft=$(GH_PROMPT_DISABLED=1 gh release view "$TAG" --repo "$REPO" \
  --json isDraft --jq .isDraft 2>"$SCRATCH/gh.err"); then
  [[ "$draft" == false ]] ||
    die "$REPO has a draft release for $TAG: publish or delete it, then run again"
  HAS_RELEASE=true
  echo "==> $REPO has the release $TAG"
elif ! grep -qx 'release not found' "$SCRATCH/gh.err"; then
  cat "$SCRATCH/gh.err" >&2
  die "cannot ask GitHub whether $REPO has the release $TAG"
fi

cat >"$OUT/outputs" <<EOF
version=$VERSION
prerelease=$PRERELEASE
pending=$PENDING
has_release=$HAS_RELEASE
EOF
echo "==> $NAME $VERSION is ready"
