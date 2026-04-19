#!/usr/bin/env python3
"""
MCP server for the crew orchestrator.
Registers globally in ~/.cursor/mcp.json.
Exposes: start_task, task_status, where_are_changes, improve_orchestrator.
"""

import json
import sys
import asyncio
import threading
from pathlib import Path
from datetime import datetime, timezone

# Ensure the orchestrator package is importable
sys.path.insert(0, str(Path(__file__).parent))

from router import infer_task_type
from registry import find_by_query, list_branches, get_branch
from logger import list_runs, count_runs
import logger as run_logger


def _default_project() -> str:
    """Return first project in projects.yaml as the default."""
    import yaml
    cfg = yaml.safe_load((Path(__file__).parent / "projects.yaml").read_text())
    return next(iter(cfg["projects"]))


def tool_start_task(description: str, task_type: str = None, linear_id: str = None, project: str = None, auto_pr: bool = False) -> dict:
    """Dispatch a task to the crew orchestrator."""
    if not project:
        project = _default_project()
    if not task_type:
        task_type = infer_task_type(description)

    task_id = linear_id or datetime.now(timezone.utc).strftime('%m%d-%H%M%S')

    # Run crew in a background thread so MCP doesn't block
    def _run():
        from runner import run_crew
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).parent / ".env")
        try:
            run_crew(project=project, task_id=task_id, description=description,
                     task_type=task_type, linear_id=linear_id, auto_pr=auto_pr)
        except Exception as e:
            run_logger.update_run(project, task_id, outcome=f"error: {e}")

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    return {
        "status": "dispatched",
        "task_id": task_id,
        "project": project,
        "task_type": task_type,
        "description": description,
        "message": f"Crew started for {task_id}. Check status with task_status('{task_id}').",
    }


def tool_task_status(task_id: str, project: str = None) -> dict:
    """Return current status of a running or completed task."""
    if not project:
        project = _default_project()
    branch = get_branch(project, task_id)
    runs = list_runs(project, limit=100)
    run = next((r for r in runs if r["task_id"] == task_id), None)
    return {
        "task_id": task_id,
        "project": project,
        "branch": branch,
        "run": run,
    }


def tool_where_are_changes(query: str, project: str = None) -> dict:
    """Ask the Git Expert where specific changes live."""
    if not project:
        project = _default_project()
    matches = find_by_query(project, query)
    if not matches:
        return {"query": query, "found": False, "message": "No matching branches found."}
    return {
        "query": query,
        "found": True,
        "matches": matches,
    }


def tool_improve_orchestrator(project: str = None) -> dict:
    """Trigger an immediate retrospective run."""
    if not project:
        project = _default_project()

    def _run():
        from retrospective import run_retrospective
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).parent / ".env")
        run_retrospective(project)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return {"status": "retrospective_started", "project": project}


# ── MCP stdio protocol ─────────────────────────────────────────────────────────

TOOLS = {
    "start_task": {
        "description": "Dispatch a task to the crew orchestrator. Starts the full agent pipeline on a new branch.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "What to build or fix"},
                "task_type": {"type": "string", "enum": ["feature", "bug", "hotfix", "refactor", "test", "docs", "chore"], "description": "Task type (inferred if omitted)"},
                "linear_id": {"type": "string", "description": "Linear issue ID e.g. P-12345"},
                "project": {"type": "string", "description": "Project name from projects.yaml (defaults to first project)"},
                "auto_pr": {"type": "boolean", "description": "Open a draft PR automatically (default: false)"},
            },
            "required": ["description"],
        },
    },
    "task_status": {
        "description": "Get the current status of a running or completed task.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "project": {"type": "string"},
            },
            "required": ["task_id"],
        },
    },
    "where_are_changes": {
        "description": "Find which branch and worktree contain changes for a task or description.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Task ID, description, or branch name fragment"},
                "project": {"type": "string"},
            },
            "required": ["query"],
        },
    },
    "improve_orchestrator": {
        "description": "Trigger an immediate retrospective run to analyse recent runs and propose improvements.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
            },
        },
    },
}

TOOL_FNS = {
    "start_task": tool_start_task,
    "task_status": tool_task_status,
    "where_are_changes": tool_where_are_changes,
    "improve_orchestrator": tool_improve_orchestrator,
}


def handle_request(req: dict) -> dict:
    method = req.get("method")
    req_id = req.get("id")

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": req_id, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "crew-orchestrator", "version": "1.0.0"},
        }}

    if method == "tools/list":
        tools = [{"name": k, "description": v["description"], "inputSchema": v["inputSchema"]} for k, v in TOOLS.items()]
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": tools}}

    if method == "tools/call":
        name = req["params"]["name"]
        args = req["params"].get("arguments", {})
        fn = TOOL_FNS.get(name)
        if not fn:
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Unknown tool: {name}"}}
        try:
            result = fn(**args)
            return {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]}}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32603, "message": str(e)}}

    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Unknown method: {method}"}}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle_request(req)
        print(json.dumps(resp), flush=True)


if __name__ == "__main__":
    main()
