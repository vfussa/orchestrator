#!/usr/bin/env bash
# Crew Orchestrator — start everything
# Run once after cloning: ./start.sh --install
# Run normally: ./start.sh

set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log()  { echo -e "${GREEN}✓${NC} $*"; }
warn() { echo -e "${YELLOW}!${NC} $*"; }
err()  { echo -e "${RED}✗${NC} $*"; }

# ── Install mode ──────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--install" ]]; then
  echo ""
  echo "  crew-orchestrator setup"
  echo "  ─────────────────────────────"

  # Python deps
  echo "Installing Python dependencies..."
  pip3 install -r requirements.txt -q 2>/dev/null || pip install -r requirements.txt -q
  log "Python deps installed"

  # Caveman for Cursor
  if command -v npx &>/dev/null; then
    npx skills add JuliusBrussee/caveman -a cursor 2>/dev/null && log "Caveman installed for Cursor" || warn "Caveman install skipped"
  fi

  # Compress orchestrator config files
  echo "Compressing config files with Caveman..."
  # caveman:compress is a Cursor skill — compression is handled on next Cursor session
  # Originals are preserved; agents read the compressed versions
  log "Run '/caveman:compress agents.yaml' and '/caveman:compress tasks.yaml' in your next Cursor session"

  # Register MCP globally in Cursor
  MCP_FILE="$HOME/.cursor/mcp.json"
  MCP_ENTRY=$(cat << MCPEOF
{
  "mcpServers": {
    "crew-orchestrator": {
      "command": "python3",
      "args": ["$DIR/mcp_server.py"]
    }
  }
}
MCPEOF
)
  if [ -f "$MCP_FILE" ]; then
    # Merge into existing mcp.json using Python
    python3 - << PYEOF
import json
path = "$MCP_FILE"
with open(path) as f:
    cfg = json.load(f)
cfg.setdefault("mcpServers", {})["crew-orchestrator"] = {
    "command": "python3",
    "args": ["$DIR/mcp_server.py"]
}
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
print("MCP entry merged into existing mcp.json")
PYEOF
  else
    mkdir -p "$HOME/.cursor"
    echo "$MCP_ENTRY" > "$MCP_FILE"
    log "MCP registered at $MCP_FILE"
  fi

  # Check .env
  if [ ! -f "$DIR/.env" ]; then
    cp "$DIR/.env.example" "$DIR/.env"
    warn ".env created from template — fill in your API keys before running crews"
    warn "  $DIR/.env"
  fi

  # Check projects.yaml path
  PROJECT_PATH=$(python3 -c "
import yaml, os
cfg = yaml.safe_load(open('$DIR/projects.yaml'))
for name, p in cfg['projects'].items():
    path = os.path.expanduser(p['path'])
    exists = os.path.isdir(path)
    print(f'{name}: {path} {\"OK\" if exists else \"NOT FOUND\"}')" 2>/dev/null || echo "projects.yaml unreadable")
  echo ""
  echo "  Registered projects:"
  echo "$PROJECT_PATH" | while read line; do
    if echo "$line" | grep -q "NOT FOUND"; then
      warn "  $line — update projects.yaml"
    else
      log "  $line"
    fi
  done

  echo ""
  log "Setup complete. Run './start.sh' to start the orchestrator."
  echo ""
  echo "  Next steps:"
  echo "  1. Fill in ~/crew-orchestrator/.env"
  echo "  2. Update projects.yaml with your planable-app path"
  echo "  3. Restart Cursor to pick up the MCP server"
  echo "  4. ./start.sh"
  echo ""
  exit 0
fi

# ── Runtime ───────────────────────────────────────────────────────────────────

# Validate .env
if [ ! -f "$DIR/.env" ]; then
  err ".env not found. Run './start.sh --install' first."
  exit 1
fi

source "$DIR/.env"
if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  err "ANTHROPIC_API_KEY not set in .env"
  exit 1
fi

echo ""
echo "  crew-orchestrator"
echo "  ─────────────────────────────"

# Start dashboard
echo "Starting dashboard..."
"$DIR/.venv/bin/python3" "$DIR/dashboard/server.py" &
DASHBOARD_PID=$!
sleep 1
log "Dashboard running → http://localhost:8765  (pid $DASHBOARD_PID)"

# Start Linear poller (only if key is set)
if [ -n "${LINEAR_API_KEY:-}" ]; then
  "$DIR/.venv/bin/python3" "$DIR/linear_poller.py" &
  POLLER_PID=$!
  log "Linear poller running (pid $POLLER_PID)"
else
  warn "LINEAR_API_KEY not set — Linear poller disabled"
fi

# Start MCP server (stdio, launched by Cursor — just verify it starts)
"$DIR/.venv/bin/python3" -c "import sys; sys.path.insert(0,'$DIR'); from mcp_server import TOOLS; print(f'MCP server OK — {len(TOOLS)} tools')" && log "MCP server ready"

echo ""
log "All services started."
echo "  Dashboard:   http://localhost:8765"
echo "  MCP:         active in Cursor (restart Cursor if not visible)"
echo "  Linear poll: every 30s"
echo ""
echo "  Press Ctrl+C to stop all services."
echo ""

# Wait and handle shutdown
cleanup() {
  echo ""
  echo "Shutting down..."
  kill $DASHBOARD_PID 2>/dev/null || true
  kill ${POLLER_PID:-} 2>/dev/null || true
  exit 0
}
trap cleanup INT TERM

wait
