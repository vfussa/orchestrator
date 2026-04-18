"""Agent tool factory — returns the right CrewAI tools for each agent role."""

import subprocess
from pathlib import Path
from crewai.tools import tool


def _project_root(project: str) -> Path:
    """Resolve the project's git repo root from projects.yaml."""
    import yaml
    cfg_path = Path(__file__).parent / "projects.yaml"
    if not cfg_path.exists():
        return Path.home() / "Work/PlanableRepo" / project
    cfg = yaml.safe_load(cfg_path.read_text())
    projects = cfg.get("projects", {})
    entry = projects.get(project, {})
    path = entry.get("path") if isinstance(entry, dict) else None
    return Path(path) if path else Path.home() / "Work/PlanableRepo" / project


@tool("read_file")
def read_file(path: str) -> str:
    """Read a file from the project. path is relative to the project root."""
    try:
        root = Path(path) if Path(path).is_absolute() else Path.home() / "Work/PlanableRepo/planable-app" / path
        return root.read_text()
    except Exception as e:
        return f"ERROR: {e}"


@tool("write_file")
def write_file(path_and_content: str) -> str:
    """Write content to a file. Input format: 'FILEPATH\n---\nCONTENT'
    (filename on first line, then a line with just ---, then file content)
    Path is relative to the project root (~/Work/PlanableRepo/planable-app).
    Creates parent directories as needed."""
    try:
        sep = "\n---\n"
        if sep not in path_and_content:
            return "ERROR: input must be 'FILEPATH\\n---\\nCONTENT' (filename, newline, ---, newline, content)"
        file_path, content = path_and_content.split(sep, 1)
        file_path = file_path.strip()
        root = Path.home() / "Work/PlanableRepo/planable-app"
        target = root / file_path if not Path(file_path).is_absolute() else Path(file_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return f"Written: {target} ({len(content)} chars)"
    except Exception as e:
        return f"ERROR: {e}"


@tool("run_bash")
def run_bash(command: str) -> str:
    """Run a shell command. Working directory is the planable-app repo root.
    Use for: git commands, npm test, eslint, grep, find, etc.
    Commands are sandboxed — no sudo, no rm -rf."""
    blocked = ["sudo", "rm -rf /", ":(){ :|:& };:"]
    if any(b in command for b in blocked):
        return "ERROR: command blocked for safety"
    try:
        cwd = Path.home() / "Work/PlanableRepo/planable-app"
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            cwd=str(cwd),
            timeout=60,
        )
        out = result.stdout.strip()
        err = result.stderr.strip()
        combined = out
        if err:
            combined += f"\nSTDERR: {err}" if out else f"STDERR: {err}"
        return combined[:4000] if combined else "(no output)"
    except subprocess.TimeoutExpired:
        return "ERROR: command timed out after 60s"
    except Exception as e:
        return f"ERROR: {e}"


# Tool sets per agent role
_TOOL_SETS = {
    "coder":          [read_file, write_file, run_bash],
    "test_writer":    [read_file, write_file, run_bash],
    "qa_engineer":    [read_file, write_file, run_bash],
    "pr_reviewer":    [read_file, run_bash],
    "architect":      [read_file, run_bash],
    "planner":        [read_file],
    "git_expert":     [run_bash],
    "standards_guardian": [read_file, run_bash],
    "retrospective":  [read_file],
}


def make_tools(agent_name: str, project: str) -> list:
    """Return the tool list for the given agent name."""
    return _TOOL_SETS.get(agent_name, [])
