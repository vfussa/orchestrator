"""Context loader — resolves project paths from projects.yaml."""

import yaml
from pathlib import Path

BASE = Path(__file__).parent


def resolve_project_path(project: str) -> str | None:
    """Return the filesystem path for a project from projects.yaml."""
    cfg_path = BASE / "projects.yaml"
    if not cfg_path.exists():
        return None
    try:
        cfg = yaml.safe_load(cfg_path.read_text())
        return cfg.get("projects", {}).get(project, {}).get("path")
    except Exception:
        return None


def load_context_for_agent(project: str, agent_name: str) -> str:
    """Load any static context files relevant to a specific agent (extensible)."""
    return ""
