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
    task_id = body.get("linear_id") or f"TASK-{datetime.now(timezone.utc).strftime('%H%M%S')}"

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
            print(f"Runner error: {e}")

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
    return {"branches": branches, "runs": all_runs, "version": VERSION, "timestamp": datetime.now(timezone.utc).isoformat()}


# Push state updates every 5 seconds
async def periodic_push():
    while True:
        await asyncio.sleep(5)
        if connected_clients:
            await broadcast({"type": "state", "data": _get_state()})


@app.on_event("startup")
async def startup():
    asyncio.create_task(periodic_push())


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8765, log_level="warning")
