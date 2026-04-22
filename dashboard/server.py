#!/usr/bin/env python3
"""Dashboard server — FastAPI + WebSocket, serves the visual interface at localhost:8765."""

import json
import asyncio
import sys
import subprocess
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, PlainTextResponse
import uvicorn

from registry import get_all_projects_summary
from logger import list_runs, count_runs

app = FastAPI()
connected_clients: list[WebSocket] = []

# In-memory per-task logs — cleared automatically on server restart
_task_logs: dict[str, list[str]] = {}


def _append_log(task_id: str, line: str):
    if task_id not in _task_logs:
        _task_logs[task_id] = []
    _task_logs[task_id].append(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {line}")


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
async def get_task_output(project: str, task_id: str):
    lines = _task_logs.get(task_id)
    if lines:
        return {"output": "\n".join(lines)}
    return {"output": "(no logs — logs are in-memory and clear on server restart)"}


@app.get("/api/runs/{project}/{task_id}/output/download")
async def download_task_output(project: str, task_id: str):
    lines = _task_logs.get(task_id, [])
    content = "\n".join(lines) if lines else "(no logs)"
    return PlainTextResponse(content, headers={
        "Content-Disposition": f"attachment; filename={task_id}.txt"
    })


@app.post("/dispatch")
async def dispatch(body: dict):
    """HTTP dispatch endpoint — used by Claude mobile and Cloudflare tunnel."""
    from runner import run_crew
    import threading
    project = body.get("project") or _default_project()
    task_id = body.get("linear_id") or datetime.now(timezone.utc).strftime('%m%d-%H%M%S')

    def _run():
        _append_log(task_id, f"Task dispatched: {body.get('description', '')}")
        _append_log(task_id, f"Project: {project} | Type: {body.get('task_type', 'auto')}")
        try:
            result = run_crew(
                project=project,
                task_id=task_id,
                description=body["description"],
                task_type=body.get("task_type"),
                linear_id=body.get("linear_id"),
                auto_pr=body.get("auto_pr", False),
            )
            _append_log(task_id, f"Crew complete — outcome: {result.get('pr_url') and 'draft_pr_created' or 'awaiting-review'}")
            if result.get("staged_files"):
                for f in result["staged_files"]:
                    _append_log(task_id, f"  staged: {f}")
            if result.get("pr_url"):
                _append_log(task_id, f"PR: {result['pr_url']}")
            asyncio.run(broadcast({"type": "run_complete", "task_id": task_id}))
        except Exception as e:
            import traceback as _tb
            _append_log(task_id, f"ERROR: {e}")
            _append_log(task_id, _tb.format_exc())
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
                              description=body.get("description", ""), status="error")
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
    """Restart the dashboard server itself via a detached subprocess."""
    import subprocess, sys, os, signal
    orch = Path(__file__).parent.parent
    venv_python = orch / ".venv/bin/python3"
    server_script = Path(__file__)
    # Launch new server, then kill self
    subprocess.Popen(
        [str(venv_python), str(server_script)],
        cwd=str(orch),
        start_new_session=True,
        stdout=open("/tmp/crew-server.log", "w"),
        stderr=subprocess.STDOUT,
    )
    async def _kill():
        await asyncio.sleep(1)
        os.kill(os.getpid(), signal.SIGTERM)
    asyncio.create_task(_kill())
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
    _task_logs.clear()
    await broadcast({"type": "state", "data": _get_state()})
    return {"status": "reset"}


@app.post("/api/cancel/{project}/{task_id}")
async def cancel_task(project: str, task_id: str):
    from registry import upsert_branch, get_branch
    b = get_branch(project, task_id)
    if b:
        upsert_branch(project, task_id, branch=b.get("branch", ""), task_type=b.get("task_type", ""),
                      description=b.get("description", ""), status="cancelled")
    _append_log(task_id, "Task cancelled by user")
    await broadcast({"type": "state", "data": _get_state()})
    return {"status": "cancelled"}


@app.post("/api/cancel/{project}/{task_id}/logs")
async def cancel_task_with_logs(project: str, task_id: str):
    from registry import upsert_branch, get_branch
    b = get_branch(project, task_id)
    if b:
        upsert_branch(project, task_id, branch=b.get("branch", ""), task_type=b.get("task_type", ""),
                      description=b.get("description", ""), status="cancelled")
    _task_logs.pop(task_id, None)
    await broadcast({"type": "state", "data": _get_state()})
    return {"status": "cancelled_with_logs"}


@app.post("/api/cancel-all")
async def cancel_all():
    import subprocess
    subprocess.run(["pkill", "-f", "crew-orchestrator/runner.py"], capture_output=True)
    await broadcast({"type": "state", "data": _get_state()})
    return {"status": "cancelled_all"}


@app.post("/api/revise/{project}/{task_id}")
async def revise_task(project: str, task_id: str, body: dict):
    from runner import run_revision
    import threading

    def _run():
        try:
            result = run_revision(project=project, task_id=task_id, feedback=body.get("feedback", ""))
            asyncio.run(broadcast({"type": "revision_complete", "task_id": task_id, "result": result}))
        except Exception as e:
            asyncio.run(broadcast({"type": "revision_failed", "task_id": task_id, "error": str(e)}))

    threading.Thread(target=_run, daemon=True).start()
    asyncio.create_task(broadcast({"type": "revision_started", "task_id": task_id}))
    return {"status": "revision_started"}


@app.post("/api/commit/{project}/{task_id}")
async def commit_task(project: str, task_id: str, body: dict):
    """Commit staged files for a task and push."""
    from registry import get_branch, upsert_branch
    import subprocess
    b = get_branch(project, task_id)
    if not b:
        return {"error": "task not found"}
    staged = b.get("staged_files", [])
    branch = b.get("branch", "")
    description = b.get("description", "")
    task_type = b.get("task_type", "chore")

    from runner import resolve_project_path
    root = Path(resolve_project_path(project)).expanduser()
    cwd = str(root)

    def run(cmd):
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)

    run(["git", "checkout", "-b", branch])
    for f in staged:
        run(["git", "add", f])
    msg = f"{task_type}: {description[:60]}"
    run(["git", "commit", "-m", msg])
    push = run(["git", "push", "-u", "origin", branch])

    pr_result = run(["gh", "pr", "create", "--title", f"{description[:60]} [{task_id}]",
                     "--body", f"Task: {description}\n\nFiles:\n" + "\n".join(f"- {f}" for f in staged),
                     "--draft", "--head", branch])
    pr_url = pr_result.stdout.strip() if pr_result.returncode == 0 else None
    upsert_branch(project, task_id, branch=branch, task_type=task_type,
                  description=description, status="draft_pr_created", pr_url=pr_url)
    return {"pr_url": pr_url, "error": pr_result.stderr if not pr_url else None}


@app.post("/api/chat/{project}/{task_id}")
async def chat_task(project: str, task_id: str, body: dict):
    from registry import get_branch, _load, _save
    message = body.get("message", "")
    role = body.get("role", "user")
    data = _load(project)
    if task_id in data:
        if "messages" not in data[task_id]:
            data[task_id]["messages"] = []
        data[task_id]["messages"].append({
            "role": role, "content": message,
            "timestamp": datetime.now(timezone.utc).isoformat()
        })
        _save(project, data)
    return {"status": "ok"}


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


def _get_version() -> str:
    try:
        orch = Path(__file__).parent.parent
        count = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=str(orch), capture_output=True, text=True
        ).stdout.strip()
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(orch), capture_output=True, text=True
        ).stdout.strip()
        return f"0.0.{count} ({sha})"
    except Exception:
        return "0.0.0"


def _get_state() -> dict:
    branches = get_all_projects_summary()
    all_runs = {}
    for project in branches:
        all_runs[project] = list_runs(project, limit=20)
    return {
        "branches": branches,
        "runs": all_runs,
        "version": _get_version(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


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
