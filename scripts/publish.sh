#!/usr/bin/env bash
# Publishes `decdn` from a signed `v<version>` tag: dispatches
# .github/workflows/publish.yml on `main` with the tag as its input, using the
# maintainer's own `gh` login, in the manner of decdn's sign-release.sh.
#
# The workflow checks the tag against KEYS on main, builds it, uploads it to
# PyPI through Trusted Publishing (no PyPI token is stored anywhere) and makes
# the tag's GitHub release, its notes the tag's CHANGELOG.md section, the PyPI
# link and the two floors. RELEASING.md describes it.
#
# Usage:  scripts/publish.sh v0.1.0
#
# A prerelease (PEP 440: `v0.2.0rc1`, `v0.2.0a1`, `v0.2.0b1`, `v0.2.0.dev1`)
# is a pre-release on PyPI, which `pip install decdn` skips, and its GitHub
# release is a prerelease, never the latest.
#
# Environment:
#   DECDN_PYPI_DRY_RUN  set to 1/true/yes to dispatch a dry run: every check
#                       and the build, uploading and releasing nothing (the
#                       run's summary shows the release notes either way)
#
# The script returns once the run is dispatched: nothing is on PyPI until the
# run's `publish` job succeeds. A run dispatched again uploads only the files
# PyPI does not have for the version, and skips the GitHub release when it
# exists, so it finishes a run whose upload or GitHub step failed.
set -euo pipefail

TAG="${1:?tag required, e.g. v0.1.0}"
REPO=decdn/sdk-python
WORKFLOW=publish.yml

die() { echo "error: $*" >&2; exit 1; }

# `1`/`true`/`yes` enable, `0`/`false`/`no`/empty do not, anything else is a
# typo and stops the script — the same rule as decdn's sign-release.sh.
enabled() {
  local name="$1" value="${2:-}"
  case "${value,,}" in
    ''|0|false|no) return 1 ;;
    1|true|yes)    return 0 ;;
    *) die "$name must be 1/true/yes or 0/false/no, got '$value'" ;;
  esac
}

# The same form prepare-publish.sh holds the tag to, so a typo fails here
# rather than in the run.
PEP440='(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)((a|b|rc)(0|[1-9][0-9]*))?(\.dev(0|[1-9][0-9]*))?'
[[ "$TAG" =~ ^v${PEP440}$ ]] ||
  die "the tag must be v<major>.<minor>.<patch>[{a|b|rc}<n>][.dev<n>], got '$TAG'"

DRY_RUN=false
if enabled DECDN_PYPI_DRY_RUN "${DECDN_PYPI_DRY_RUN:-}"; then
  DRY_RUN=true
fi

command -v gh >/dev/null || die "gh not found on PATH"
export GH_PROMPT_DISABLED=1

# The run checks out the tag from $REPO: one that is not there yet fails here.
# Only GitHub's 404 means that; any other failure is shown as it is.
err=$(mktemp)
trap 'rm -f "$err"' EXIT
if ! gh api "repos/$REPO/git/ref/tags/$TAG" --silent 2>"$err"; then
  grep -q 'HTTP 404' "$err" &&
    die "$REPO has no tag $TAG: push it first (scripts/release.sh --execute does)"
  cat "$err" >&2
  die "cannot ask GitHub whether $REPO has the tag $TAG (is gh logged in? \`gh auth status\`)"
fi

echo "==> Dispatching $WORKFLOW on main for $TAG$([[ "$DRY_RUN" == false ]] || echo ' (dry run)')"
gh workflow run "$WORKFLOW" --repo "$REPO" --ref main -f tag="$TAG" -f dry_run="$DRY_RUN" ||
  die "could not start $WORKFLOW; nothing was published. Run it by hand:
  gh workflow run $WORKFLOW --repo $REPO --ref main -f tag=$TAG -f dry_run=$DRY_RUN"
echo "==> Dispatched. Nothing is on PyPI until the run's publish job succeeds."
echo "    Follow the run (the newest one, unless another was dispatched since):"
echo "    gh run list --repo $REPO --workflow $WORKFLOW --limit 1"
echo "    gh run watch --repo $REPO <run-id>"
