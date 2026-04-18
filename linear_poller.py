#!/usr/bin/env python3
"""
Linear poller — watches for issues labelled 'agent-queue' and dispatches them.
Runs as a background service. Poll interval: 30 seconds.
"""

import os
import sys
import time
import json
import hashlib
from pathlib import Path
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
load_dotenv(Path(__file__).parent / ".env")

DISPATCHED_FILE = Path(__file__).parent / "registry" / "_dispatched_linear.json"
POLL_INTERVAL = 30  # seconds


def _load_dispatched() -> set:
    if DISPATCHED_FILE.exists():
        return set(json.loads(DISPATCHED_FILE.read_text()))
    return set()


def _save_dispatched(ids: set) -> None:
    DISPATCHED_FILE.parent.mkdir(exist_ok=True)
    DISPATCHED_FILE.write_text(json.dumps(list(ids)))


def _default_project() -> str:
    import yaml
    cfg = yaml.safe_load((Path(__file__).parent / "projects.yaml").read_text())
    return next(iter(cfg["projects"]))


def fetch_queued_issues(api_key: str) -> list[dict]:
    try:
        import httpx
        headers = {"Authorization": api_key, "Content-Type": "application/json"}
        query = """
        query {
          issues(filter: { labels: { name: { eq: "agent-queue" } } state: { type: { neq: "completed" } } }) {
            nodes { id identifier title description }
          }
        }"""
        r = httpx.post("https://api.linear.app/graphql", json={"query": query}, headers=headers, timeout=10)
        return r.json().get("data", {}).get("issues", {}).get("nodes", [])
    except Exception as e:
        print(f"[poller] Linear fetch error: {e}")
        return []


def mark_in_progress(api_key: str, issue_id: str) -> None:
    """Remove agent-queue label to prevent re-dispatch."""
    try:
        import httpx
        headers = {"Authorization": api_key, "Content-Type": "application/json"}
        # Get label ID for agent-queue
        q = """query { issueLabels(filter: { name: { eq: "agent-queue" } }) { nodes { id } } }"""
        r = httpx.post("https://api.linear.app/graphql", json={"query": q}, headers=headers, timeout=10)
        label_ids = [n["id"] for n in r.json().get("data", {}).get("issueLabels", {}).get("nodes", [])]
        if label_ids:
            mutation = """
            mutation($id: String!, $labelIds: [String!]!) {
              issueUpdate(id: $id, input: { removeLabelIds: $labelIds }) { success }
            }"""
            httpx.post("https://api.linear.app/graphql",
                       json={"query": mutation, "variables": {"id": issue_id, "labelIds": label_ids}},
                       headers=headers, timeout=10)
    except Exception as e:
        print(f"[poller] Mark in-progress error: {e}")


def dispatch_issue(issue: dict, project: str) -> None:
    from runner import run_crew
    task_id = issue["identifier"]  # e.g. P-15301
    description = issue["title"]
    print(f"[poller] Dispatching {task_id}: {description}")
    try:
        run_crew(project=project, task_id=task_id, description=description, linear_id=task_id)
    except Exception as e:
        print(f"[poller] Runner error for {task_id}: {e}")


def poll_loop():
    api_key = os.getenv("LINEAR_API_KEY", "")
    if not api_key:
        print("[poller] LINEAR_API_KEY not set — poller disabled")
        return

    project = _default_project()
    print(f"[poller] Started. Watching Linear for 'agent-queue' issues → project: {project}")

    dispatched = _load_dispatched()

    while True:
        issues = fetch_queued_issues(api_key)
        for issue in issues:
            if issue["id"] in dispatched:
                continue
            dispatched.add(issue["id"])
            _save_dispatched(dispatched)
            mark_in_progress(api_key, issue["id"])
            dispatch_issue(issue, project)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    poll_loop()
