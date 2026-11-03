#!/usr/bin/env bash
# Makes the GitHub release of a tag that .github/workflows/publish.yml has
# published to PyPI. The `github-release` job runs it with a `contents: write`
# token and no OIDC access, after the upload.
#
# Usage:  scripts/github-release.sh <tag> <sha> <notes> <prerelease>
#
#   <tag>         the release tag, e.g. v0.1.0
#   <sha>         the commit `prepare` verified and built the tag at
#   <notes>       the release notes prepare-publish.sh rendered
#   <prerelease>  true or false; a prerelease's release is a prerelease,
#                 never the latest
#
# The tag must still name <sha> on GitHub: a tag moved since `prepare` would
# give the release other code than PyPI has, so it stops the run.
set -euo pipefail

TAG="${1:?usage: $0 <tag> <sha> <notes> <prerelease>}"
SHA="${2:?usage: $0 <tag> <sha> <notes> <prerelease>}"
NOTES="${3:?usage: $0 <tag> <sha> <notes> <prerelease>}"
PRERELEASE="${4:?usage: $0 <tag> <sha> <notes> <prerelease>}"
REPO=decdn/sdk-python

die() { echo "error: $*" >&2; exit 1; }

PEP440='(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)((a|b|rc)(0|[1-9][0-9]*))?(\.dev(0|[1-9][0-9]*))?'
[[ "$TAG" =~ ^v${PEP440}$ ]] ||
  die "the tag must be v<major>.<minor>.<patch>[{a|b|rc}<n>][.dev<n>], got '$TAG'"
[[ "$SHA" =~ ^[0-9a-f]{40}$ ]] || die "the commit must be a full SHA-1, got '$SHA'"
[[ "$PRERELEASE" == true || "$PRERELEASE" == false ]] ||
  die "prerelease must be true or false, got '$PRERELEASE'"
[[ -s "$NOTES" ]] || die "no release notes at $NOTES"

command -v gh >/dev/null || die "gh not found on PATH"
export GH_PROMPT_DISABLED=1

# The commit the tag names on GitHub now, peeling annotated tag objects.
object=$(gh api "repos/$REPO/git/ref/tags/$TAG" --jq '.object.type + " " + .object.sha') ||
  die "cannot read the tag $TAG from $REPO"
while [[ "$object" == "tag "* ]]; do
  object=$(gh api "repos/$REPO/git/tags/${object#tag }" --jq '.object.type + " " + .object.sha') ||
    die "cannot read the tag object of $TAG from $REPO"
done
[[ "$object" == "commit "* ]] || die "$TAG on $REPO names a ${object%% *}, not a commit"
[[ "${object#commit }" == "$SHA" ]] || die "$TAG on $REPO now names ${object#commit }, not $SHA,
the commit PyPI has. Restore the tag to $SHA, then dispatch the workflow again."

args=(release create "$TAG" --repo "$REPO" --verify-tag --title "$TAG" --notes-file "$NOTES")
[[ "$PRERELEASE" == false ]] || args+=(--prerelease --latest=false)
echo "==> Creating the GitHub release $TAG"
gh "${args[@]}"
