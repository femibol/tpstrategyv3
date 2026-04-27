#!/bin/bash
# Stop hook: warn if HANDOFF.md is stale relative to commits on this branch.
# Triggers when the current branch has >=2 commits ahead of main where the
# most recent of them did NOT touch HANDOFF.md. Prints to stderr; never blocks.
set -euo pipefail

cd "${CLAUDE_PROJECT_DIR:-$PWD}" 2>/dev/null || exit 0

# Skip if not in a git repo
git rev-parse --git-dir >/dev/null 2>&1 || exit 0

branch=$(git branch --show-current 2>/dev/null || echo "")
[ -z "$branch" ] && exit 0
[ "$branch" = "main" ] || [ "$branch" = "master" ] && exit 0

# Resolve base ref (prefer local main, fall back to origin/main)
base=""
if git rev-parse --verify --quiet main >/dev/null 2>&1; then
  base=main
elif git rev-parse --verify --quiet origin/main >/dev/null 2>&1; then
  base=origin/main
else
  exit 0
fi

ahead=$(git rev-list --count "$base..HEAD" 2>/dev/null || echo 0)
[ "$ahead" -lt 2 ] && exit 0

# Look for HANDOFF.md in any commit on this branch ahead of base.
# If absent across all of them, warn.
if ! git log "$base..HEAD" --name-only --pretty=format: 2>/dev/null | grep -qx "HANDOFF.md"; then
  >&2 echo ""
  >&2 echo "  ⚠️  HANDOFF.md hasn't been updated on this branch ($branch, $ahead commits ahead of $base)."
  >&2 echo "      Run /handoff to refresh it before the next session picks up cold."
  >&2 echo ""
fi

exit 0
