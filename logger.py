"""Run logger — writes structured JSON logs to runs/{project}/YYYY-MM-DD-{task_id}.json."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path


BASE = Path(__file__).parent


def _run_path(project: str, task_id: str) -> Path:
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    directory = BASE / "runs" / project
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{date}-{task_id}.json"


def _output_path(project: str, task_id: str) -> Path:
    directory = BASE / "runs" / project
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{task_id}.output.txt"


def start_run(project: str, task_id: str, task_type: str, description: str, agents_used: list[str]) -> dict:
    run = {
        "task_id": task_id,
        "project": project,
        "task_type": task_type,
        "description": description,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": None,
        "duration_seconds": None,
        "agents_used": agents_used,
        "token_usage": {},
        "flags": [],
        "pr_url": None,
        "branch": None,
        "files_written": [],
        "outcome": "in_progress",
    }
    path = _run_path(project, task_id)
    path.write_text(json.dumps(run, indent=2))
    return run


def update_run(project: str, task_id: str, **kwargs) -> dict:
    path = _run_path(project, task_id)
    if not path.exists():
        raise FileNotFoundError(f"Run log not found: {path}")
    run = json.loads(path.read_text())
    run.update(kwargs)
    path.write_text(json.dumps(run, indent=2))
    return run


def complete_run(project: str, task_id: str, outcome: str = "draft_pr_created", pr_url: str = None) -> dict:
    path = _run_path(project, task_id)
    run = json.loads(path.read_text())
    started = datetime.fromisoformat(run["started_at"])
    now = datetime.now(timezone.utc)
    run["completed_at"] = now.isoformat()
    run["duration_seconds"] = int((now - started).total_seconds())
    run["outcome"] = outcome
    if pr_url:
        run["pr_url"] = pr_url
    path.write_text(json.dumps(run, indent=2))
    return run


def add_flag(project: str, task_id: str, flag: str) -> None:
    path = _run_path(project, task_id)
    run = json.loads(path.read_text())
    if flag not in run["flags"]:
        run["flags"].append(flag)
    path.write_text(json.dumps(run, indent=2))


def record_tokens(project: str, task_id: str, agent: str, input_tokens: int, output_tokens: int) -> None:
    path = _run_path(project, task_id)
    run = json.loads(path.read_text())
    run["token_usage"][agent] = {"input": input_tokens, "output": output_tokens}
    path.write_text(json.dumps(run, indent=2))


def save_output(project: str, task_id: str, output: str) -> None:
    """Save the full crew output text to a companion .output.txt file."""
    _output_path(project, task_id).write_text(output)


def get_output(project: str, task_id: str) -> str | None:
    """Return the saved crew output text, or None if not found."""
    path = _output_path(project, task_id)
    return path.read_text() if path.exists() else None


def list_runs(project: str, limit: int = 50) -> list[dict]:
    directory = BASE / "runs" / project
    if not directory.exists():
        return []
    files = sorted(directory.glob("*.json"), reverse=True)[:limit]
    return [json.loads(f.read_text()) for f in files]


def count_runs(project: str) -> int:
    directory = BASE / "runs" / project
    if not directory.exists():
        return 0
    return len(list(directory.glob("*.json")))
