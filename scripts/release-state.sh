#!/usr/bin/env bash
# Asks GitHub whether decdn/sdk-python has a release for a tag, before the
# upload. The `release-state` job of .github/workflows/publish.yml runs it
# with a `contents: write` token: GitHub lists draft releases only to callers
# with push access, and to a read-only token a draft is no release at all.
# The job runs none of the tag's code.
#
# Usage:  scripts/release-state.sh <tag>
#
# Prints `has_release=true` or `has_release=false` on stdout, for
# $GITHUB_OUTPUT. A draft release for the tag stops it: a draft is not a
# release, and `gh release create` beside it would make a second one.
set -euo pipefail

TAG="${1:?usage: $0 <tag>}"
REPO=decdn/sdk-python

die() { echo "error: $*" >&2; exit 1; }

# The tag is interpolated into the jq filter below, so its form is checked
# first: the PEP 440 rule prepare-publish.sh applies.
PEP440='(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)((a|b|rc)(0|[1-9][0-9]*))?(\.dev(0|[1-9][0-9]*))?'
[[ "$TAG" =~ ^v${PEP440}$ ]] ||
  die "the tag must be v<major>.<minor>.<patch>[{a|b|rc}<n>][.dev<n>], got '$TAG'"

command -v gh >/dev/null || die "gh not found on PATH"

# One line per release for the tag, `true` for a draft. Listing, not
# `releases/tags/<tag>`, because that endpoint never answers a draft.
drafts=$(GH_PROMPT_DISABLED=1 gh api "repos/$REPO/releases" --paginate \
  --jq ".[] | select(.tag_name == \"$TAG\") | .draft") ||
  die "cannot ask GitHub whether $REPO has a release for $TAG"
if grep -qx true <<<"$drafts"; then
  die "$REPO has a draft release for $TAG: publish or delete it, then run again"
elif grep -qx false <<<"$drafts"; then
  echo "==> $REPO has the release $TAG" >&2
  echo "has_release=true"
elif [[ -z "$drafts" ]]; then
  echo "==> $REPO has no release for $TAG" >&2
  echo "has_release=false"
else
  die "cannot read GitHub's answer for the releases of $TAG: $drafts"
fi
