#!/usr/bin/env bash
set -euo pipefail

CLAUDE_REMOTE="${CLAUDE_REMOTE:-claude}"
CLAUDE_URL="https://github.com/pagombin/Benchmarking.git"
CLAUDE_BRANCH="${CLAUDE_BRANCH:-claude/pgbench-harness-live-charts-s3puuw}"
TARGET="${1:-main}"

cd "$(git rev-parse --show-toplevel)"
git remote get-url "$CLAUDE_REMOTE" >/dev/null 2>&1 || git remote add "$CLAUDE_REMOTE" "$CLAUDE_URL"

git fetch origin
git checkout "$TARGET"
git pull --ff-only
git fetch "$CLAUDE_REMOTE" "$CLAUDE_BRANCH"
SRC="$CLAUDE_REMOTE/$CLAUDE_BRANCH"

if git diff --quiet "$TARGET" "$SRC"; then
  echo "✓ Already in sync — $TARGET already matches $SRC."
  exit 0
fi

SYNC_BRANCH="sync-$(date +%Y%m%d-%H%M%S)"
git checkout -b "$SYNC_BRANCH"
git diff --binary "$TARGET" "$SRC" | git apply --index --3way --binary
git commit -m "Sync from pgbench-harness ($(date +%Y-%m-%d))"
git push -u origin "$SYNC_BRANCH"
echo
echo "✓ Pushed $SYNC_BRANCH — review & open the PR into $TARGET:"
echo "  https://github.com/pagombin-do/Benchmarking/compare/${TARGET}...${SYNC_BRANCH}?expand=1"
