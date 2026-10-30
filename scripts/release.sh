#!/usr/bin/env bash
# Cuts a release of `decdn` on main, in the manner of `cargo release`: bumps
# `version` with `uv version` (pyproject.toml, then uv.lock with `uv lock`, and
# src/decdn/_version.py), writes the CHANGELOG.md section with git-cliff, then
# makes a signed release commit and a signed `vX.Y.Z` tag and pushes both.
# There is no release PR. Publishing is the next, separate step,
# `scripts/publish.sh vX.Y.Z`: a PyPI version is immutable, so nothing is
# uploaded before the tag is on origin and a human runs it.
#
#   scripts/release.sh [auto|patch|minor|major|X.Y.Z]
#                      [--execute] [--no-verify] [--allow-unconventional]
#
# Without --execute it is a dry run: it prints the new tag and the diff it would
# commit, and changes no file. The level defaults to `auto`, the bump git-cliff
# derives (cliff.toml) from the conventional commits since the last `vX.Y.Z`
# tag.
# Before 1.0, a feature (or, from 0.1 on, a breaking change) bumps the minor and
# any other commit kept in the changelog bumps the patch. The section is
# git-cliff's rendering of those commits, under a `## [X.Y.Z] — YYYY-MM-DD`
# heading. A commit there that git-cliff cannot parse as a Conventional Commit
# would be dropped with only a count in git-cliff's log, so the script has
# git-cliff list them (require_conventional) and refuses them unless
# --allow-unconventional.
#
# The first release (no tag yet, pyproject.toml at the 0.0.0 placeholder) names
# its level and releases the changelog's hand-written `## [Unreleased]` section
# instead: without a previous tag, git-cliff would render the whole history.
#
# Every release needs the decdn gateway release pyproject.toml names as its
# floor (`[tool.decdn] gateway-floor`) to be published: an SDK whose floor names
# an unreleased gateway does not ship (RELEASING.md).
#
# --execute runs the gate (`ruff check`, `ruff format --check`, `mypy`,
# `pytest`), `uv build` and scripts/check-package.sh on the result unless
# --no-verify, so a release that fails them is never committed. If any of that
# or the commit fails, the files are restored. Run it on an up-to-date, clean
# main, with git configured to sign with an OpenPGP key published in KEYS
# (user.signingkey; publish.sh verifies the tag against KEYS), and with a bypass
# of the main ruleset: the release commit goes straight to main.
set -euo pipefail

usage() {
  echo "usage: $0 [auto|patch|minor|major|X.Y.Z] [--execute] [--no-verify] [--allow-unconventional]" >&2
  exit 2
}
die() { echo "release: $*" >&2; exit 1; }

level="" execute=0 verify=1 allow_unconventional=0
for arg in "$@"; do
  case "$arg" in
    --execute) execute=1 ;;
    --no-verify) verify=0 ;;
    --allow-unconventional) allow_unconventional=1 ;;
    -*) usage ;;
    *)
      [[ -z "$level" ]] || usage
      level="$arg" ;;
  esac
done
level="${level:-auto}"
[[ "$level" =~ ^(auto|patch|minor|major|[0-9]+\.[0-9]+\.[0-9]+)$ ]] || usage

tools=(git python3 uv gh)
((execute == 0)) || tools+=(gpg)
for tool in "${tools[@]}"; do
  command -v "$tool" >/dev/null || die "$tool not found on PATH"
done
python3 -c 'import tomllib' 2>/dev/null || die "python3 has no tomllib: Python 3.11 or later is required"

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"
prefix="v"
manifest="pyproject.toml"
lockfile="uv.lock"
version_file="src/decdn/_version.py"
changelog="CHANGELOG.md"
# What the release commit changes.
files=("$manifest" "$lockfile" "$version_file" "$changelog")
for f in "${files[@]}"; do
  [[ -f "$f" ]] || die "$f is missing"
done

tmp="$(mktemp -d)"
written=0 committed=0 pushed=0 reported=0
cleanup() {
  # Once --execute has written the files, any failure before the release commit
  # exists puts them back. Nothing is restored before that: a refusal never
  # touches the maintainer's own edits.
  if ((written == 1 && committed == 0)); then
    git checkout -q HEAD -- "${files[@]}" \
      || echo "release: could not restore ${files[*]}: run git checkout HEAD -- ${files[*]}" >&2
  fi
  # Stopped (Ctrl-C, say) between the release commit and the end of the push.
  if ((committed == 1 && pushed == 0 && reported == 0)); then
    echo "release: stopped after the release commit, before the push finished. Check what origin has:" >&2
    echo "  git ls-remote origin refs/heads/main refs/tags/$tag" >&2
    echo "then, if it has neither, undo locally: git tag -d $tag; git reset --hard origin/main" >&2
  fi
  rm -rf "$tmp"
}
trap cleanup EXIT

# --- preflight ---------------------------------------------------------------
branch="$(git symbolic-ref --quiet --short HEAD || true)"
[[ "$branch" == "main" ]] || die "releases are cut on main, not '${branch:-a detached HEAD}'"
status="$(git status --porcelain)" || die "git status failed"
[[ -z "$status" ]] || die "the working tree is not clean (git status)"
git fetch --quiet --tags origin main || die "git fetch origin failed"
head="$(git rev-parse HEAD)" || die "git rev-parse HEAD failed"
upstream="$(git rev-parse origin/main)" || die "git rev-parse origin/main failed"
[[ "$head" == "$upstream" ]] || die "main is not origin/main: pull or push first"
if ((execute == 1)); then
  signingkey="$(git config user.signingkey || true)"
  [[ -n "$signingkey" ]] || die "git has no user.signingkey: the release commit and tag are signed"
  format="$(git config gpg.format || true)"
  [[ -z "$format" || "$format" == "openpgp" ]] \
    || die "gpg.format is $format: publish.sh verifies the tag against KEYS, an OpenPGP keyring"
  # Sign a throwaway commit object now, not after the checks, and verify it
  # against KEYS alone, as publish.sh verifies the tag: a key that cannot sign,
  # or one KEYS does not hold, would otherwise fail only after the push.
  probe="$(git commit-tree -S -p HEAD -m "release.sh signing check" "HEAD^{tree}" 2>"$tmp/sign.err")" \
    || { cat "$tmp/sign.err" >&2; die "git cannot sign with user.signingkey"; }
  [[ -f KEYS ]] || die "KEYS is missing"
  mkdir -m 700 "$tmp/keys"
  gpg --homedir "$tmp/keys" --batch --import KEYS >/dev/null 2>&1 || die "KEYS is not a valid OpenPGP keyring"
  GNUPGHOME="$tmp/keys" git verify-commit "$probe" >/dev/null 2>&1 \
    || die "user.signingkey ($signingkey) signs with a key that is not in KEYS: publish.sh would refuse the tag"
fi

# <key>: a string at that dotted path of pyproject.toml, or nothing.
toml() {
  python3 -c 'import sys, tomllib
v = tomllib.load(open("pyproject.toml", "rb"))
for k in sys.argv[1].split("."):
    v = v.get(k) if isinstance(v, dict) else None
if isinstance(v, str):
    print(v)' "$1"
}
name="$(toml project.name)" || die "cannot read $manifest"
[[ "$name" == "decdn" ]] || die "$manifest does not name decdn"
current="$(toml project.version)" || die "cannot read $manifest"
[[ "$current" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] \
  || die "$manifest has version '${current}', not X.Y.Z (after a prerelease, version goes back to the last release: RELEASING.md)"
module_version="$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$version_file")"
[[ "$module_version" == "$current" ]] \
  || die "$version_file has __version__ '${module_version}', not $manifest's $current"
floor="$(toml tool.decdn.gateway-floor)" || die "cannot read $manifest"
[[ "$floor" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "$manifest has [tool.decdn] gateway-floor '${floor}', not X.Y.Z"
# `gh release view` answers a published release, and a draft only to an account
# that can push to decdn/decdn; a draft is refused all the same.
rc=0
draft="$(GH_PROMPT_DISABLED=1 gh release view "v$floor" -R decdn/decdn --json isDraft --jq .isDraft 2>"$tmp/gh.err")" \
  || rc=$?
if ((rc != 0)); then
  grep -qx 'release not found' "$tmp/gh.err" \
    && die "the gateway floor, decdn v$floor, has no published release: release decdn v$floor first"
  cat "$tmp/gh.err" >&2
  die "could not ask GitHub for decdn v$floor's release (gh release view failed: is gh logged in?)"
fi
[[ "$draft" == "false" ]] || die "the gateway floor, decdn v$floor, is a draft release: publish it first"

ver_gt() { [[ "$1" != "$2" && "$(printf '%s\n' "$1" "$2" | sort -V | tail -n1)" == "$1" ]]; }
tag_re="^${prefix}[0-9]+[.][0-9]+[.][0-9]+$"
# <tag names on stdin>: the highest X.Y.Z among the release tags, or nothing.
highest() { { grep -E "$tag_re" || (($? == 1)); } | sed "s/^$prefix//" | sort -V | tail -n1; }
# The last release is origin's highest tag. git-cliff's boundary is the nearest
# release tag in the history, so every local one must be origin's, the same
# tag object: a stray one (left by a failed push, say), even below origin's
# highest, would move the boundary.
remote_refs="$(git ls-remote --tags --refs origin "refs/tags/${prefix}*")" || die "git ls-remote origin failed"
remote_refs="$(awk '{sub(/^refs\/tags\//, "", $2); print $1, $2}' <<<"$remote_refs")"
local_refs="$(git for-each-ref --format='%(objectname) %(refname:short)' "refs/tags/${prefix}*")" || die "git for-each-ref failed"
remote_tags="$(awk '{print $2}' <<<"$remote_refs")"
prev="$(highest <<<"$remote_tags")"
stray="$(awk -v re="$tag_re" 'NR == FNR {seen[$0]; next} $2 ~ re && !($0 in seen) {print $2}' \
  <(printf '%s\n' "$remote_refs") <(printf '%s\n' "$local_refs"))"
[[ -z "$stray" ]] \
  || die "local tags not on origin, or not origin's: $(xargs <<<"$stray"). Delete them with git tag -d (git fetch --tags then brings back origin's)"
today="$(date -u +%F)"

# --- version and section -----------------------------------------------------
# Keep a Changelog's link definitions would go stale: no section has one.
! grep -q '^\[[^]]*\]: ' "$changelog" \
  || die "$changelog has link definitions ('[X]: <url>' lines), which the script does not maintain: drop them"

if [[ -z "$prev" ]]; then
  # First release: the hand-written [Unreleased] section becomes the release's.
  [[ "$current" == "0.0.0" ]] || die "no ${prefix}X.Y.Z tag yet, but $manifest is $current, not the 0.0.0 placeholder"
  case "$level" in
    auto) die "the first release names its level: minor for 0.1.0" ;;
    patch) version="0.0.1" ;;
    minor) version="0.1.0" ;;
    major) version="1.0.0" ;;
    *) version="$level" ;;
  esac
  ver_gt "$version" "0.0.0" || die "$version is not above 0.0.0"
  grep -qx '## \[Unreleased\]' "$changelog" \
    || die "$changelog has no '## [Unreleased]' section for the first release"
  awk -v h="## [$version] — $today" '$0 == "## [Unreleased]" {print h; next} {print}' \
    "$changelog" > "$tmp/changelog"
  awk -v h="## [$version] — $today" '
    $0 == h {on=1; next}
    on && (/^## \[/ || /^<!--/ || /^\[[^]]+\]: /) {exit}
    on' "$tmp/changelog" > "$tmp/section"
  grep -q '^- ' "$tmp/section" || die "$changelog: the [Unreleased] section has no '- ' entries"
else
  # The git-cliff uv.lock pins (`uv sync` installs it), else one on PATH.
  cliff_bin="$repo/.venv/bin/git-cliff"
  [[ -x "$cliff_bin" ]] || cliff_bin="$(command -v git-cliff || true)"
  [[ -n "$cliff_bin" ]] || die "git-cliff is not installed (uv sync installs it)"
  cliff_version="$("$cliff_bin" --version)" || die "git-cliff --version failed"
  cliff_version="$(awk '{print $2}' <<<"$cliff_version")"
  # 2.9 added require_conventional, which the unconventional-commit refusal
  # below needs.
  ver_gt "2.9.0" "$cliff_version" && die "git-cliff 2.9 or later is required, not ${cliff_version:-unknown}"
  [[ "$current" == "$prev" ]] \
    || die "$manifest is $current, but the last tag is $prefix$prev: fix the manifest first"
  if grep -q '^## \[Unreleased\]' "$changelog"; then
    die "$changelog has an [Unreleased] section: sections are generated since the first release, so turn its entries into commit subjects or drop it"
  fi

  # Every run is --unreleased: the commits since the last tag. (git-cliff's
  # require_conventional would otherwise judge the whole history.)
  cliff() {
    "$cliff_bin" --config "$repo/cliff.toml" --tag-pattern "$tag_re" --unreleased "$@" 2>"$tmp/cliff.err" \
      || { cat "$tmp/cliff.err" >&2; die "git-cliff failed"; }
  }
  # git-cliff drops a commit it cannot parse as a Conventional Commit
  # (cliff.toml, filter_unconventional) and only says so in a log line, so the
  # bump and the section would leave it out unseen: an empty scope, say, or a
  # body right under the subject. With require_conventional it fails instead,
  # naming each one.
  if ! GIT_CLIFF__GIT__REQUIRE_CONVENTIONAL=true "$cliff_bin" --config "$repo/cliff.toml" --tag-pattern "$tag_re" \
      --unreleased --bumped-version >/dev/null 2>"$tmp/cliff.err"; then
    esc="$(printf '\033')"
    sed "s/$esc\[[0-9;]*m//g" "$tmp/cliff.err" > "$tmp/cliff.plain" # git-cliff colours its log even off a tty
    grep -q 'UnconventionalCommitsError' "$tmp/cliff.plain" || { cat "$tmp/cliff.err" >&2; die "git-cliff failed"; }
    # "Commit <hash> is not conventional:", then the message, one "| " line per line.
    awk '/Commit [0-9a-f]+ is not conventional:/ {match($0, /Commit [0-9a-f]+/); h=substr($0, RSTART+7, RLENGTH-7); next}
         h != "" && sub(/^[[:space:]]*\| /, "") {print "  " h " " $0; h=""}' "$tmp/cliff.plain" > "$tmp/unconventional"
    # An empty list would let --allow-unconventional drop commits unnamed.
    [[ -s "$tmp/unconventional" ]] \
      || { cat "$tmp/cliff.err" >&2; die "git-cliff refused unconventional commits, but its list of them could not be read"; }
    echo "release: git-cliff cannot parse these commits as Conventional Commits, so the changelog and the bump leave them out:" >&2
    cat "$tmp/unconventional" >&2
    ((allow_unconventional == 1)) || die "refusing; re-run with --allow-unconventional to release without them"
  fi
  case "$level" in
    auto) bumped="$(cliff --bumped-version)" ;;
    patch|minor|major) bumped="$(cliff --bump "$level" --bumped-version)" ;;
    *) bumped="$prefix$level" ;;
  esac
  [[ "$bumped" =~ $tag_re ]] || die "git-cliff proposed '$bumped', not a ${prefix}X.Y.Z tag"
  version="${bumped#"$prefix"}"
  # Squeeze git-cliff's blank lines and trim the ends.
  cliff --strip all | cat -s | sed '/./,$!d' | sed '${/^$/d;}' > "$tmp/section"
  bad="$(grep '^### ' "$tmp/section" | grep -vxE '### (Added|Changed|Deprecated|Removed|Fixed|Security)' || true)"
  [[ -z "$bad" ]] || die "git-cliff rendered a heading that is not a Keep a Changelog kind: $bad (fix cliff.toml's commit_parsers)"
  if [[ "$level" == [0-9]* ]]; then
    ver_gt "$version" "$prev" || die "$version is not above the last release, $prev"
  fi
  if [[ "$version" == "$prev" ]] || ! grep -q '^- ' "$tmp/section"; then
    die "nothing to release: no commit since $prefix$prev makes a changelog entry (ci, test, style, build and release commits do not, nor, with --allow-unconventional, unconventional ones)"
  fi
  ver_gt "$version" "$prev" || die "$version is not above the last release, $prev"
  awk -v h="## [$version] — $today" -v s="$tmp/section" '
    !done && /^## \[/ {
      print h; print ""
      while ((getline l < s) > 0) print l
      print ""; done=1
    }
    {print}
    END {
      if (!done) {
        print ""; print h; print ""
        while ((getline l < s) > 0) print l
      }
    }' "$changelog" > "$tmp/changelog"
fi
tag="$prefix$version"
git rev-parse -q --verify "refs/tags/$tag" >/dev/null && die "tag $tag already exists"
! grep -qx "$tag" <<<"$remote_tags" || die "tag $tag already exists on origin"

# What `uv version` writes: the [project] table's `version` line, nothing else.
awk -v v="$version" '
  /^\[/ {table=$0}
  table == "[project]" && !n && /^version = "/ {print "version = \"" v "\""; n=1; next}
  {print}
  END {exit !n}' "$manifest" > "$tmp/manifest" || die "could not set the version in $manifest"
# What `uv lock` writes: the decdn package's `version` line, nothing else.
awk -v v="$version" '
  $0 == "[[package]]" {pkg=1; mine=0}
  pkg && $0 == "name = \"decdn\"" {mine=1}
  mine && /^version = "/ {print "version = \"" v "\""; mine=0; pkg=0; n++; next}
  {print}
  END {exit n != 1}' "$lockfile" > "$tmp/lockfile" || die "could not set decdn's version in $lockfile"
sed "s/^__version__ = \".*\"$/__version__ = \"$version\"/" "$version_file" > "$tmp/version_file" \
  || die "could not set the version in $version_file"

show_diff() { # <file> <new copy>
  local rc=0
  diff -u --label "a/$1" --label "b/$1" "$1" "$2" || rc=$?
  ((rc <= 1)) || die "diff failed on $1"
}
echo "release decdn: ${prev:+$prefix$prev -> }$tag"
echo
show_diff "$manifest" "$tmp/manifest"
show_diff "$lockfile" "$tmp/lockfile"
show_diff "$version_file" "$tmp/version_file"
show_diff "$changelog" "$tmp/changelog"
echo

if ((execute == 0)); then
  echo "dry run: no file changed. --execute would run:"
  echo "  uv version $version --frozen && uv lock, and set __version__ in $version_file"
  ((verify == 1)) && echo "  uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest && uv build && scripts/check-package.sh"
  echo "  git commit -S -m 'chore(release): $tag' -- ${files[*]}"
  echo "  git tag -s $tag -m 'decdn $version'"
  echo "  git push --atomic origin main $tag"
  exit 0
fi

# --- execute -----------------------------------------------------------------
written=1
# On the tree the preflight found clean, before the changelog is written.
uv version "$version" --frozen >/dev/null || die "uv version $version failed: nothing was committed"
uv lock --quiet || die "uv lock failed: nothing was committed"
cp "$tmp/version_file" "$version_file"
cp "$tmp/changelog" "$changelog"
if ((verify == 1)); then
  uv run --frozen ruff check . || die "uv run ruff check . failed: nothing was committed"
  uv run --frozen ruff format --check . || die "uv run ruff format --check . failed: nothing was committed"
  uv run --frozen mypy || die "uv run mypy failed: nothing was committed"
  uv run --frozen pytest || die "uv run pytest failed: nothing was committed"
  uv build || die "uv build failed: nothing was committed"
  scripts/check-package.sh || die "scripts/check-package.sh failed: nothing was committed"
fi
# The checks write dist/ and .venv/ only, which git ignores; anything else is
# theirs to explain before a release commit.
excludes=()
for f in "${files[@]}"; do
  excludes+=(":(exclude)$f")
done
status="$(git status --porcelain -- . "${excludes[@]}")" || die "git status failed"
[[ -z "$status" ]] || die "the checks left changes besides ${files[*]}: nothing was committed"$'\n'"$status"
# What is committed is the diff shown above, byte for byte.
cmp -s "$manifest" "$tmp/manifest" \
  || die "uv version wrote a $manifest other than the one shown: nothing was committed"$'\n'"$(diff -u "$tmp/manifest" "$manifest" || true)"
cmp -s "$lockfile" "$tmp/lockfile" \
  || die "uv lock wrote a $lockfile other than the one shown: nothing was committed"$'\n'"$(diff -u "$tmp/lockfile" "$lockfile" || true)"
cmp -s "$version_file" "$tmp/version_file" || die "$version_file changed during the checks: nothing was committed"
cmp -s "$changelog" "$tmp/changelog" || die "$changelog changed during the checks: nothing was committed"

git commit --quiet -S -m "chore(release): $tag" -- "${files[@]}" \
  || die "git commit failed (a commit hook, or signing): nothing was committed"
committed=1
# The pre-commit hook may rewrite what it commits.
for f in manifest lockfile version_file changelog; do
  git show "HEAD:${!f}" | cmp -s - "$tmp/$f" \
    || { reported=1; die "the release commit's ${!f} is not the one shown (a commit hook rewrote it?). To undo: git reset --hard origin/main"; }
done
git tag -s "$tag" -m "decdn $version" \
  || { reported=1; die "tagging failed after the release commit. To undo: git reset --hard origin/main"; }
if ! git push --atomic origin "HEAD:refs/heads/main" "refs/tags/$tag"; then
  # --atomic makes a rejected push all-or-nothing, but a dropped connection can
  # leave it unknown: look before suggesting an undo.
  if seen="$(git ls-remote origin refs/heads/main "refs/tags/$tag")" \
     && ! grep -q "refs/tags/$tag$" <<<"$seen" \
     && ! grep -q "^$(git rev-parse HEAD)[[:space:]]refs/heads/main$" <<<"$seen"; then
    echo "release: the push failed; nothing reached origin. To undo locally:" >&2
    echo "  git tag -d $tag && git reset --hard origin/main" >&2
  else
    echo "release: the push failed, and origin may have the release commit or $tag:" >&2
    echo "  check git ls-remote origin before undoing anything" >&2
  fi
  reported=1
  exit 1
fi
pushed=1
echo "tagged $tag and pushed it with the release commit. Publish it:"
echo "  DECDN_PYPI_DRY_RUN=1 scripts/publish.sh $tag"
echo "  scripts/publish.sh $tag"
