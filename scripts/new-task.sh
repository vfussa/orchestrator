#!/usr/bin/env bash
# Usage: ./scripts/new-task.sh <project> <task-id> <description>
# Creates a git worktree as a sibling directory of the project.
# Zero files written to the project repo.

set -euo pipefail

PROJECT="${1:?Usage: new-task.sh <project> <task-id> <description>}"
TASK_ID="${2:?task-id required}"
DESCRIPTION="${3:?description required}"

ORCHESTRATOR_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PROJECTS_YAML="$ORCHESTRATOR_DIR/projects.yaml"

# Read project path from projects.yaml via Python
PROJECT_PATH=$(python3 -c "
import yaml, os
cfg = yaml.safe_load(open('$PROJECTS_YAML'))
path = cfg['projects']['$PROJECT']['path']
print(os.path.expanduser(path))
")

if [ ! -d "$PROJECT_PATH" ]; then
  echo "Error: project path not found: $PROJECT_PATH"
  echo "Update projects.yaml with the correct path for '$PROJECT'"
  exit 1
fi

# Build branch name
SLUG=$(echo "$DESCRIPTION" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]/-/g' | sed 's/-\+/-/g' | cut -c1-40 | sed 's/-$//')
BRANCH="feature/${TASK_ID}-${SLUG}"
WORKTREE_PATH="${PROJECT_PATH}-${TASK_ID}"

if [ -d "$WORKTREE_PATH" ]; then
  echo "Worktree already exists: $WORKTREE_PATH"
  exit 0
fi

cd "$PROJECT_PATH"
git worktree add "$WORKTREE_PATH" -b "$BRANCH"

# Install deps in worktree
if [ -f "$WORKTREE_PATH/package.json" ]; then
  echo "Installing npm deps in worktree..."
  (cd "$WORKTREE_PATH" && npm install --silent 2>/dev/null || true)
fi

echo ""
echo "✓ Worktree:  $WORKTREE_PATH"
echo "✓ Branch:    $BRANCH"
echo "✓ Open in Cursor: cursor $WORKTREE_PATH"
