"""Branch registry — tracks every worktree, branch, and task state."""

import json
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).parent


def _registry_path(project: str) -> Path:
    d = BASE / "registry"
    d.mkdir(exist_ok=True)
    return d / f"{project}.json"


def _load(project: str) -> dict:
    path = _registry_path(project)
    if path.exists():
        return json.loads(path.read_text())
    return {}


def _save(project: str, data: dict) -> None:
    _registry_path(project).write_text(json.dumps(data, indent=2))


def upsert_branch(
    project: str,
    task_id: str,
    branch: str,
    task_type: str,
    description: str,
    status: str = "running",
    worktree_path: str = None,
    staged_files: list = None,
    auto_pr: bool = True,
) -> None:
    data = _load(project)
    existing = data.get(task_id, {})
    data[task_id] = {
        "task_id": task_id,
        "branch": branch,
        "task_type": task_type,
        "description": description,
        "status": status,
        "worktree_path": worktree_path,
        "pr_url": None,
        "commits": existing.get("commits", []),
        "messages": existing.get("messages", []),
        "staged_files": staged_files or existing.get("staged_files", []),
        "auto_pr": auto_pr,
        "created_at": existing.get("created_at", datetime.now(timezone.utc).isoformat()),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _save(project, data)


def update_branch_status(project: str, task_id: str, status: str, pr_url: str = None) -> None:
    data = _load(project)
    if task_id not in data:
        return
    data[task_id]["status"] = status
    data[task_id]["updated_at"] = datetime.now(timezone.utc).isoformat()
    if pr_url:
        data[task_id]["pr_url"] = pr_url
    _save(project, data)


def update_branch_fields(project: str, task_id: str, **kwargs) -> None:
    """Update arbitrary fields on a branch entry."""
    data = _load(project)
    if task_id not in data:
        return
    data[task_id].update(kwargs)
    data[task_id]["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save(project, data)


def add_message(project: str, task_id: str, role: str, content: str) -> None:
    """Append a chat message to a branch's message history."""
    data = _load(project)
    if task_id not in data:
        return
    msg = {
        "role": role,
        "content": content,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    data[task_id].setdefault("messages", []).append(msg)
    data[task_id]["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save(project, data)


def add_commit(project: str, task_id: str, commit_hash: str, message: str, agent: str) -> None:
    data = _load(project)
    if task_id not in data:
        return
    data[task_id]["commits"].append({
        "hash": commit_hash,
        "message": message,
        "agent": agent,
        "at": datetime.now(timezone.utc).isoformat(),
    })
    data[task_id]["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save(project, data)


def get_branch(project: str, task_id: str) -> dict | None:
    return _load(project).get(task_id)


def list_branches(project: str) -> list[dict]:
    return list(_load(project).values())


def find_by_query(project: str, query: str) -> list[dict]:
    """Search by task_id, description, or branch name."""
    q = query.lower()
    return [
        b for b in list_branches(project)
        if q in b["task_id"].lower()
        or q in b["description"].lower()
        or q in b["branch"].lower()
    ]


def get_all_projects_summary() -> dict:
    """Returns {project: [branches]} for the dashboard."""
    registry_dir = BASE / "registry"
    if not registry_dir.exists():
        return {}
    result = {}
    for f in registry_dir.glob("*.json"):
        project = f.stem
        result[project] = list(json.loads(f.read_text()).values())
    return result
