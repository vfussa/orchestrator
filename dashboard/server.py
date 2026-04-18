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


@app.get("/api/runs/{project}/{task_id}/output")
async def get_run_output(project: str, task_id: str):
    text = get_output(project, task_id)
    if text is None:
        return {"output": None, "message": "No output saved for this run yet."}
    return {"output": text}


@app.post("/dispatch")
async def dispatch(body: dict):
    """HTTP dispatch endpoint — used by Claude mobile and Cloudflare tunnel."""
    from runner import run_crew
    import threading
    project = body.get("project") or _default_project()
    task_id = body.get("linear_id") or f"TASK-{datetime.now(timezone.utc).strftime('%H%M%S')}"

    auto_pr = body.get("auto_pr", True)

    def _run():
        try:
            run_crew(
                project=project,
                task_id=task_id,
                description=body["description"],
                task_type=body.get("task_type"),
                linear_id=body.get("linear_id"),
                auto_pr=auto_pr,
            )
            asyncio.run(broadcast({"type": "run_complete", "task_id": task_id}))
        except Exception as e:
            print(f"Runner error: {e}")
            try:
                from registry import update_branch_status
                from logger import complete_run
                update_branch_status(project, task_id, "failed")
                complete_run(project, task_id, outcome="failed")
            except Exception:
                pass
            asyncio.run(broadcast({"type": "run_failed", "task_id": task_id, "error": str(e)}))

    threading.Thread(target=_run, daemon=True).start()
    asyncio.create_task(broadcast({"type": "run_started", "task_id": task_id, "description": body["description"]}))
    return {"status": "dispatched", "task_id": task_id}


@app.post("/api/commit/{project}/{task_id}")
async def commit_staged(project: str, task_id: str, body: dict = {}):
    """Commit staged files and create draft PR for an awaiting-review task."""
    from registry import get_branch, update_branch_status
    from context_loader import resolve_project_path
    from runner import _create_branch_and_pr

    branch_info = get_branch(project, task_id)
    if not branch_info:
        return {"error": "task not found"}

    staged_files = branch_info.get("staged_files", [])
    if not staged_files:
        return {"error": "no staged files"}

    project_root = resolve_project_path(project)
    if not project_root:
        return {"error": "project path not resolved"}

    try:
        git_result = _create_branch_and_pr(
            Path(project_root),
            branch_info["branch"],
            task_id,
            branch_info.get("task_type", "feature"),
            branch_info.get("description", ""),
            staged_files,
            linear_id=branch_info.get("linear_id"),
        )
        pr_url = git_result.get("pr_url")
        update_branch_status(project, task_id, "review-ready", pr_url=pr_url)
        await broadcast({"type": "state", "data": _get_state()})
        return {"status": "draft_pr_created" if pr_url else "committed", "pr_url": pr_url}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/revise/{project}/{task_id}")
async def revise_task_endpoint(project: str, task_id: str, body: dict):
    """Run targeted revision: analyze feedback → simple revise or complex re-plan+code."""
    from runner import run_revision
    import threading

    feedback = body.get("feedback", "").strip()
    if not feedback:
        return {"error": "no feedback provided"}

    def _run():
        try:
            result = run_revision(project, task_id, feedback)
            asyncio.run(broadcast({"type": "revision_complete", "task_id": task_id, "result": result}))
            asyncio.run(broadcast({"type": "state", "data": _get_state()}))
        except Exception as e:
            print(f"Revision error: {e}")
            from registry import update_branch_status
            update_branch_status(project, task_id, "awaiting-review")
            asyncio.run(broadcast({"type": "revision_failed", "task_id": task_id, "error": str(e)}))
            asyncio.run(broadcast({"type": "state", "data": _get_state()}))

    # Flip to running immediately so UI shows it in Active Crews right away
    from registry import update_branch_status
    update_branch_status(project, task_id, "running")
    threading.Thread(target=_run, daemon=True).start()
    asyncio.create_task(broadcast({"type": "state", "data": _get_state()}))
    return {"status": "revision_started", "task_id": task_id}


@app.post("/api/chat/{project}/{task_id}")
async def send_chat(project: str, task_id: str, body: dict):
    """Add a message to a task's chat history."""
    from registry import add_message
    content = body.get("message", "").strip()
    if not content:
        return {"error": "empty message"}
    role = body.get("role", "user")
    add_message(project, task_id, role, content)
    await broadcast({"type": "state", "data": _get_state()})
    return {"status": "ok"}


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
        (workspace / "crew-orchestrator-index.html",       orch / "dashboard/index.html"),
        (workspace / "crew-orchestrator-server.py",        orch / "dashboard/server.py"),
        (workspace / "crew-orchestrator-runner.py",        orch / "runner.py"),
        (workspace / "crew-orchestrator-router.py",        orch / "router.py"),
        (workspace / "crew-orchestrator-context_loader.py", orch / "context_loader.py"),
        (workspace / "crew-orchestrator-tasks.yaml",       orch / "tasks.yaml"),
        (workspace / "crew-orchestrator-agents.yaml",      orch / "agents.yaml"),
        (workspace / "crew-orchestrator-tools.py",         orch / "tools.py"),
        (workspace / "crew-orchestrator-logger.py",        orch / "logger.py"),
        (workspace / "crew-orchestrator-registry.py",      orch / "registry.py"),
    ]
    applied = []
    for src, dst in copies:
        if src.exists():
            shutil.copy2(src, dst)
            applied.append(dst.name)
    return {"applied": applied}


@app.post("/api/reset")
async def reset_orchestrator():
    """Kill running task processes, clear registry AND run logs. Server stays up."""
    import subprocess
    orch = Path(__file__).parent.parent
    subprocess.run(["pkill", "-f", "crew-orchestrator/runner.py"], capture_output=True)
    subprocess.run(["pkill", "-f", "crew-orchestrator/linear_poller.py"], capture_output=True)
    for f in (orch / "registry").glob("*.json"):
        f.unlink(missing_ok=True)
    for f in (orch / "runs").glob("**/*.json"):
        f.unlink(missing_ok=True)
    for f in (orch / "runs").glob("**/*.cancelled"):
        f.unlink(missing_ok=True)
    await broadcast({"type": "state", "data": _get_state()})
    return {"status": "reset"}


@app.post("/api/cancel-all")
async def cancel_all_tasks():
    """Cancel all running tasks. Logs are kept."""
    import subprocess
    subprocess.run(["pkill", "-f", "crew-orchestrator/runner.py"], capture_output=True)
    from registry import get_all_projects_summary, update_branch_status
    branches = get_all_projects_summary()
    for project, bs in branches.items():
        for b in bs:
            if b.get("status") == "running":
                update_branch_status(project, b["task_id"], "cancelled")
    await broadcast({"type": "state", "data": _get_state()})
    return {"status": "cancelled_all"}


@app.post("/api/cancel/{project}/{task_id}")
async def cancel_task(project: str, task_id: str):
    """Cancel a specific running task. Log is kept."""
    from registry import update_branch_status
    update_branch_status(project, task_id, "cancelled")
    # Write cancel marker — runner checks this before final status write
    orch = Path(__file__).parent.parent
    marker_dir = orch / "runs" / project
    marker_dir.mkdir(parents=True, exist_ok=True)
    (marker_dir / f"{task_id}.cancelled").touch()
    await broadcast({"type": "state", "data": _get_state()})
    return {"status": "cancelled", "task_id": task_id}


@app.post("/api/cancel/{project}/{task_id}/logs")
async def cancel_task_and_clear_logs(project: str, task_id: str):
    """Cancel a specific running task AND delete its run log."""
    from registry import update_branch_status
    update_branch_status(project, task_id, "cancelled")
    orch = Path(__file__).parent.parent
    marker_dir = orch / "runs" / project
    marker_dir.mkdir(parents=True, exist_ok=True)
    (marker_dir / f"{task_id}.cancelled").touch()
    # Delete the run log file (search by task_id glob)
    for f in marker_dir.glob(f"*-{task_id}.json"):
        f.unlink(missing_ok=True)
    await broadcast({"type": "state", "data": _get_state()})
    return {"status": "cancelled_and_cleared", "task_id": task_id}


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
