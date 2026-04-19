#!/usr/bin/env python3
"""Dashboard server — FastAPI + WebSocket, serves the visual interface at localhost:8765."""

import json
import asyncio
import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from registry import get_all_projects_summary
from logger import list_runs, count_runs, get_output

VERSION = "0.0.1"


def get_version() -> str:
    """Compute version from git: 0.0.{commit_count} ({short_hash})"""
    import subprocess
    try:
        repo = Path(__file__).parent.parent
        count = subprocess.check_output(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=repo, stderr=subprocess.DEVNULL
        ).decode().strip()
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo, stderr=subprocess.DEVNULL
        ).decode().strip()
        return f"0.0.{count} ({sha})"
    except Exception:
        return "0.0.0"

app = FastAPI()
connected_clients: list[WebSocket] = []


async def broadcast(data: dict):
    dead = []
    for ws in connected_clients:
        try:
            await ws.send_text(json.dumps(data))
        except Exception:
            dead.append(ws)
    for ws in dead:
        connected_clients.remove(ws)


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    html = (Path(__file__).parent / "index.html").read_text()
    return HTMLResponse(html)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    connected_clients.append(ws)
    # Send current state immediately on connect
    await ws.send_text(json.dumps({"type": "state", "data": _get_state()}))
    try:
        while True:
            await ws.receive_text()  # keep alive
    except WebSocketDisconnect:
        connected_clients.remove(ws)


@app.get("/api/state")
async def get_state():
    return _get_state()


@app.get("/api/runs/{project}")
async def get_runs(project: str, limit: int = 20):
    return list_runs(project, limit=limit)


@app.post("/dispatch")
async def dispatch(body: dict):
    """HTTP dispatch endpoint — used by Claude mobile and Cloudflare tunnel."""
    from runner import run_crew
    import threading
    project = body.get("project") or _default_project()
    task_id = body.get("linear_id") or datetime.now(timezone.utc).strftime('%m%d-%H%M%S')

    def _run():
        try:
            run_crew(
                project=project,
                task_id=task_id,
                description=body["description"],
                task_type=body.get("task_type"),
                linear_id=body.get("linear_id"),
            )
            asyncio.run(broadcast({"type": "run_complete", "task_id": task_id}))
        except Exception as e:
            import traceback as _tb
            print(f"Runner error [{task_id}]: {e}")
            _tb.print_exc()
            try:
                import logger as _rl
                _rl.complete_run(project, task_id, outcome=f"error: {e}")
            except Exception:
                pass
            try:
                from registry import upsert_branch
                upsert_branch(project, task_id, branch=f"error/{task_id}", task_type="error",
                              description=body.get("description",""), status="error")
            except Exception:
                pass

    threading.Thread(target=_run, daemon=True).start()
    asyncio.create_task(broadcast({"type": "run_started", "task_id": task_id, "description": body["description"]}))
    return {"status": "dispatched", "task_id": task_id}


@app.get("/api/llm-test")
async def llm_test():
    """Fire a single minimal LLM call and return the result — for diagnosing API issues."""
    import threading, time
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")

    result = {}

    def _call():
        try:
            from crewai import Agent, Task, Crew, Process
            a = Agent(role="Tester", goal="Reply with OK", backstory="You reply with OK.", llm="groq/llama-3.3-70b-versatile", max_iter=1, verbose=False)
            t = Task(description="Reply with the word OK and nothing else.", expected_output="OK", agent=a)
            c = Crew(agents=[a], tasks=[t], process=Process.sequential, verbose=False)
            r = c.kickoff()
            result["output"] = str(r)
            result["ok"] = True
        except Exception as e:
            result["error"] = str(e)
            result["ok"] = False

    th = threading.Thread(target=_call)
    th.start()
    th.join(timeout=30)
    if th.is_alive():
        return {"ok": False, "error": "LLM call timed out after 30s — API key missing or network issue"}
    return result


@app.post("/api/apply")
async def apply_changes():
    """Copy latest files from the workspace folder into crew-orchestrator."""
    import shutil
    workspace = Path.home() / "Work/PlanableRepo/planable-app"
    orch = Path(__file__).parent.parent
    copies = [
        (workspace / "crew-orchestrator-index.html",  orch / "dashboard/index.html"),
        (workspace / "crew-orchestrator-server.py",   orch / "dashboard/server.py"),
        (workspace / "crew-orchestrator-runner.py",   orch / "runner.py"),
        (workspace / "crew-orchestrator-tasks.yaml",  orch / "tasks.yaml"),
        (workspace / "crew-orchestrator-agents.yaml", orch / "agents.yaml"),
    ]
    applied = []
    for src, dst in copies:
        if src.exists():
            shutil.copy2(src, dst)
            applied.append(dst.name)
    return {"applied": applied}



@app.post("/api/shell")
async def run_shell(body: dict):
    """Run a trusted maintenance command in the crew-orchestrator directory.
    Localhost-only — never expose this server to the public internet."""
    import subprocess
    script = body.get("script", "")
    if not script:
        return {"error": "no script provided"}
    orch = Path(__file__).parent.parent
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True, text=True,
        cwd=str(orch), timeout=120
    )
    return {
        "stdout": result.stdout,
        "stderr": result.stderr,
        "returncode": result.returncode,
        "ok": result.returncode == 0,
    }


@app.post("/api/restart")
async def restart_server():
    """Restart via a wrapper script — avoids port-binding race condition."""
    import subprocess, os
    pid = os.getpid()
    venv_py = str(Path(__file__).parent.parent / ".venv/bin/python3")
    srv = str(Path(__file__))
    with open("/tmp/crew-restart.sh", "w") as f:
        f.write("#!/bin/bash\n")
        f.write("kill %d\n" % pid)
        f.write("while lsof -ti:8765 > /dev/null 2>&1; do sleep 0.3; done\n")
        f.write("nohup %s %s > /tmp/crew-server.log 2>&1 &\n" % (venv_py, srv))
    os.chmod("/tmp/crew-restart.sh", 0o755)
    subprocess.Popen(["bash", "/tmp/crew-restart.sh"], start_new_session=True)
    return {"status": "restarting"}

@app.post("/api/reset")
async def reset_orchestrator():
    """Kill running task processes, clear registry and run logs. Server stays up."""
    import subprocess
    orch = Path(__file__).parent.parent
    subprocess.run(["pkill", "-f", "crew-orchestrator/runner.py"], capture_output=True)
    subprocess.run(["pkill", "-f", "crew-orchestrator/linear_poller.py"], capture_output=True)
    for f in (orch / "registry").glob("*.json"):
        f.unlink(missing_ok=True)
    for f in (orch / "runs").glob("**/*.json"):
        f.unlink(missing_ok=True)
    await broadcast({"type": "state", "data": _get_state()})
    return {"status": "reset"}


@app.get("/status/{task_id}")
async def task_status(task_id: str, project: str = None):
    from registry import get_branch
    if not project:
        project = _default_project()
    return {"branch": get_branch(project, task_id), "runs": list_runs(project, limit=100)}


@app.get("/where")
async def where_are_changes(q: str, project: str = None):
    from registry import find_by_query
    if not project:
        project = _default_project()
    return find_by_query(project, q)


def _default_project() -> str:
    import yaml
    cfg = yaml.safe_load((Path(__file__).parent.parent / "projects.yaml").read_text())
    return next(iter(cfg["projects"]))


def _get_state() -> dict:
    branches = get_all_projects_summary()
    all_runs = {}
    for project in branches:
        all_runs[project] = list_runs(project, limit=20)
    return {"branches": branches, "runs": all_runs, "version": VERSION, "timestamp": datetime.now(timezone.utc).isoformat(), "version": get_version()}


# Push state updates every 5 seconds
async def periodic_push():
    while True:
        await asyncio.sleep(5)
        if connected_clients:
            await broadcast({"type": "state", "data": _get_state()})



async def _clear_stale_runs():
    """On startup, mark any runs still in_progress as failed (server was restarted)."""
    import yaml
    from datetime import datetime, timezone
    import logger as run_logger
    try:
        cfg = yaml.safe_load((Path(__file__).parent.parent / "projects.yaml").read_text())
        for project in cfg["projects"]:
            for run in run_logger.list_runs(project, limit=100):
                if run.get("outcome") == "in_progress" or (run.get("outcome") and "error" in run["outcome"] and not run.get("completed_at")):
                    run_logger.update_run(project, run["task_id"],
                        outcome="error: server restarted — task was interrupted",
                        completed_at=datetime.now(timezone.utc).isoformat())
        # Also clear the registry entry so dashboard doesn't show zombie
        try:
            from registry import _registry_path
            rp = _registry_path(project, run["task_id"])
            if rp.exists():
                rp.unlink()
        except Exception:
            pass
    except Exception as e:
        print(f"[startup] stale run cleanup: {e}")

@app.on_event("startup")
async def startup():
    asyncio.create_task(periodic_push())
    await _clear_stale_runs()


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8765, log_level="warning")
