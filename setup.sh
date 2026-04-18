#!/bin/bash
set -euo pipefail

ORCH="$(cd "$(dirname "$0")" && pwd)"

echo ""
echo "╔══════════════════════════════════════╗"
echo "║       Crew Orchestrator Setup        ║"
echo "╚══════════════════════════════════════╝"
echo ""

# ── Python env ────────────────────────────────────────────────────────────────
if [ ! -d "$ORCH/.venv" ]; then
  echo "→ Creating Python virtual environment..."
  python3 -m venv "$ORCH/.venv"
fi

echo "→ Installing dependencies..."
"$ORCH/.venv/bin/pip" install -q -r "$ORCH/requirements.txt"
echo "  ✓ Dependencies installed"
echo ""

# ── Groq API key ──────────────────────────────────────────────────────────────
if [ ! -f "$ORCH/.env" ]; then
  echo "You need a free Groq API key."
  echo "Get one at: https://console.groq.com → API Keys"
  echo ""
  read -p "Paste your GROQ_API_KEY: " GROQ_KEY
  echo "GROQ_API_KEY=$GROQ_KEY" > "$ORCH/.env"
  echo "  ✓ .env written"
else
  echo "  ✓ .env already exists — skipping"
fi
echo ""

# ── Project path ──────────────────────────────────────────────────────────────
if [ ! -f "$ORCH/projects.yaml" ]; then
  echo "Which local project should the crew work on?"
  echo "Enter the absolute path to your project's root directory."
  echo "(Example: /Users/you/dev/my-app)"
  echo ""
  read -p "Project path: " PROJECT_PATH
  PROJECT_PATH="${PROJECT_PATH/#\~/$HOME}"  # expand tilde

  # Derive a short name from the folder
  PROJECT_NAME="$(basename "$PROJECT_PATH")"

  cat > "$ORCH/projects.yaml" << YAML
projects:
  $PROJECT_NAME:
    path: $PROJECT_PATH
YAML
  echo "  ✓ projects.yaml written (project: $PROJECT_NAME)"
else
  echo "  ✓ projects.yaml already exists — skipping"
fi
echo ""

# ── Precondition checks ───────────────────────────────────────────────────────
echo "Checking preconditions..."

if ! command -v gh &>/dev/null; then
  echo "  ⚠  gh CLI not found — PR creation will be skipped"
  echo "     Install: https://cli.github.com"
else
  if gh auth status &>/dev/null; then
    echo "  ✓ gh CLI authenticated"
  else
    echo "  ⚠  gh CLI not authenticated — run: gh auth login"
  fi
fi

if ! git config --global user.name &>/dev/null; then
  echo "  ⚠  Git user not configured — run: git config --global user.name 'Your Name'"
else
  echo "  ✓ Git configured"
fi
echo ""

# ── Start server ──────────────────────────────────────────────────────────────
echo "Starting dashboard server..."
"$ORCH/.venv/bin/python3" "$ORCH/dashboard/server.py" &
SERVER_PID=$!
sleep 2

if kill -0 $SERVER_PID 2>/dev/null; then
  echo "  ✓ Server running (PID $SERVER_PID)"
  echo ""
  echo "╔══════════════════════════════════════╗"
  echo "║   Dashboard: http://localhost:8765   ║"
  echo "╚══════════════════════════════════════╝"
  echo ""
  open http://localhost:8765 2>/dev/null || true
else
  echo "  ✗ Server failed to start — check output above"
  exit 1
fi
