#!/bin/bash
ORCH="$HOME/crew-orchestrator"
cd "$ORCH"

WORKSPACE="$HOME/Work/PlanableRepo/planable-app"

echo "=== Apply & Push — crew-orchestrator ==="
echo "Workspace: $WORKSPACE"
echo ""

for pair in "crew-orchestrator-index.html:dashboard/index.html" "crew-orchestrator-server.py:dashboard/server.py" "crew-orchestrator-runner.py:runner.py"; do
  SRC="${pair%%:*}"; DST="${pair##*:}"
  if [ -f "$WORKSPACE/$SRC" ]; then
    cp "$WORKSPACE/$SRC" "$ORCH/$DST"
    echo "✓ $SRC → $DST"
  fi
done

echo ""
git add dashboard/index.html dashboard/server.py runner.py 2>/dev/null || true
COUNT=$(git rev-list --count HEAD 2>/dev/null || echo "0")
if git diff --cached --quiet 2>/dev/null; then
  echo "(nothing new to commit)"
else
  git commit -m "🛠 v0.0.$((COUNT + 1)): dashboard update"
  git push origin main && echo "✓ Pushed" || echo "⚠ Push failed"
fi

echo ""
pkill -f "crew-orchestrator/dashboard/server" 2>/dev/null || true
sleep 1
source "$ORCH/.env" 2>/dev/null || true
PYTHON="$ORCH/.venv/bin/python3"; [ -f "$PYTHON" ] || PYTHON="$(which python3)"
nohup "$PYTHON" "$ORCH/dashboard/server.py" >> /tmp/crew-server.log 2>&1 &
sleep 2
curl -sf --max-time 3 http://localhost:8765/api/state > /dev/null && echo "✅ Server running at http://localhost:8765" || echo "⚠ Still starting..."
echo ""
read -p "Press Enter to close..."
