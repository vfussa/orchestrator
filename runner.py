"""Crew runner — builds and executes a CrewAI crew for a given task."""

import os
import re
import subprocess
import yaml
from collections import defaultdict
from pathlib import Path
from dotenv import load_dotenv

from router import infer_task_type
from context_loader import (
    resolve_project_path, scan_cursor_folder,
    build_rules_block, build_skills_index_block, build_agent_context_block,
)
from registry import upsert_branch, update_branch_status
import logger as run_logger

load_dotenv(Path(__file__).parent / ".env")

# Patch CrewAI's interpolate_only to tolerate unknown template vars (e.g. {featureName}
# in injected skill docs). We replace with "" rather than raising KeyError.
# Patch both the module AND crewai.task's module global (imported via `from ... import`).
try:
    import crewai.utilities.string_utils as _su
    import crewai.task as _ct

    _VARIABLE_PATTERN = _su._VARIABLE_PATTERN

    def _safe_interpolate(text, inputs, *a, **kw):
        if not text or ("{" not in text and "}" not in text):
            return text or ""
        # Build a complete inputs dict that has all vars found in the text,
        # defaulting missing ones to "" so the `var not in inputs` check passes.
        variables = _VARIABLE_PATTERN.findall(text)
        safe_inputs = dict(inputs)
        for var in variables:
            if var not in safe_inputs:
                safe_inputs[var] = ""
        return _su.__dict__["_orig_interpolate"](text, safe_inputs, *a, **kw)

    _su._orig_interpolate = _su.interpolate_only
    _su.interpolate_only = _safe_interpolate
    _ct.interpolate_only = _safe_interpolate
    print("[runner] interpolate_only patched OK")
except Exception as e:
    print(f"[runner] interpolate_only patch FAILED: {e}")

BASE = Path(__file__).parent
CAVEMAN_PROMPT = (
    "Terse like caveman. Technical substance exact. Only fluff die. "
    "Drop: articles, filler, pleasantries, hedging. Fragments OK. "
    "Short synonyms. Code unchanged. Pattern: [thing] [action] [reason]. [next step]. "
    "ACTIVE EVERY RESPONSE. No revert. No filler drift."
)

# Keyword → skill name: auto-inject full skill content when description matches
SKILL_AUTO_KEYWORDS: dict[str, list[str]] = {
    "ui-implementation":      ["component", "tsx", "jsx", "button", "modal", "form", "tailwind", "ui", "render", "style", "dialog", "dropdown"],
    "react-component-tests":  ["component", "tsx", "hook", "render", "rtl", "testing-library"],
    "e2e-tests":              ["e2e", "playwright", "visual test", "end-to-end", "automation", "spec", "pom", "test", "tests", "coverage", "flake", "flaky"],
    "unit-tests":             ["unit test", "node:assert", "sinon", "trpc", "meteor method"],
    "mongodb":                ["mongo", "collection", "query", "aggregate", "db.", "database"],
    "feature-development":    ["feature", "new screen", "new page", "new flow", "implement"],
    "github-pull-requests":   ["pr", "pull request", "review", "merge"],
}

TASK_MAP = {
    "feature":  ["plan_task", "code_task", "git_task"],
    "bug":      ["plan_task", "code_task", "git_task"],
    "hotfix":   ["code_task", "git_task"],
    "refactor": ["plan_task", "code_task", "git_task"],
    "test":     ["plan_task", "code_task", "git_task"],
    "docs":     ["plan_task", "code_task", "git_task"],
    "chore":    ["code_task", "git_task"],
}


def _load_yaml(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def _extract_section(text: str, heading: str) -> str:
    """Extract a ## heading section from markdown text."""
    pattern = rf"(?m)^##\s+{re.escape(heading)}\s*\n(.*?)(?=\n##\s|\Z)"
    m = re.search(pattern, text, re.DOTALL)
    return m.group(1).strip() if m else ""


def _load_file_stripped(path: Path, max_lines: int = 200) -> str:
    """Load a file, strip YAML frontmatter, truncate to max_lines."""
    if not path.exists():
        return ""
    text = path.read_text(errors="ignore")
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:]
    lines = text.splitlines()
    if len(lines) > max_lines:
        text = "\n".join(lines[:max_lines]) + f"\n... [{len(lines) - max_lines} lines truncated]"
    return text.strip()


def _build_skill_block(cfg: dict, description: str, project_root: str) -> str:
    """Build explicit + auto-detected skill content for injection."""
    if not project_root:
        return ""
    root = Path(project_root).expanduser()
    parts = []
    explicit_skills = cfg.get("skills", [])
    skill_sections = cfg.get("skill_sections", {})
    desc_lower = description.lower()
    auto_skills = [
        s for s, kws in SKILL_AUTO_KEYWORDS.items()
        if s not in explicit_skills and any(kw in desc_lower for kw in kws)
    ]
    for skill_name in explicit_skills + auto_skills:
        skill_path = root / ".cursor" / "skills" / skill_name / "SKILL.md"
        if not skill_path.exists():
            continue
        text = _load_file_stripped(skill_path, max_lines=80)
        sections = skill_sections.get(skill_name, [])
        if sections:
            extracted = [f"### {s}\n{_extract_section(text, s)}" for s in sections if _extract_section(text, s)]
            if extracted:
                parts.append(f"## SKILL: {skill_name}\n" + "\n\n".join(extracted))
        else:
            parts.append(f"## SKILL: {skill_name}\n{text}")
    return "\n\n---\n\n".join(parts)


def _build_agent(name: str):
    from crewai import Agent
    from crewai.llm import LLM
    agents_cfg = _load_yaml(BASE / "agents.yaml")
    cfg = agents_cfg[name]
    backstory = cfg["backstory"].format(caveman=CAVEMAN_PROMPT)
    model = cfg.get("llm", "groq/llama-3.3-70b-versatile")
    llm = LLM(model=model, max_retries=6)
    return Agent(
        role=cfg["role"],
        goal=cfg["goal"],
        backstory=backstory,
        llm=llm,
        max_iter=cfg.get("max_iter", 3),
        verbose=True,
    )


def _build_task(task_name: str, agent, context_tasks: list, inputs: dict,
                project_root: str = None, cursor_ctx: dict = None, agent_name: str = None,
                callback=None):
    from crewai import Task
    tasks_cfg = _load_yaml(BASE / "tasks.yaml")
    cfg = tasks_cfg[task_name]
    # Use defaultdict so any unknown {var} in template becomes "" instead of KeyError.
    # Second regex pass neutralizes any {var} patterns introduced by substituted values
    # (e.g. project_context containing {featureName} naming conventions).
    _fmt = defaultdict(str, {k: v or "" for k, v in inputs.items()})
    description = re.sub(r'\{(\w+)\}', r'[\1]',
                         cfg["description"].format_map(_fmt))
    expected_output = re.sub(r'\{(\w+)\}', r'[\1]',
                             cfg["expected_output"].format_map(_fmt))

    injections = []

    # 1. Agent-specific rules from .cursor/rules/
    if cursor_ctx and agent_name:
        rules_block = build_rules_block(agent_name, cursor_ctx)
        if rules_block:
            injections.append(rules_block)

    # 2. Skills index into planner so it knows what exists
    if cursor_ctx and agent_name == "planner":
        skills_block = build_skills_index_block(cursor_ctx)
        if skills_block:
            injections.append(skills_block)

    # 3. Specialist agent context from .cursor/agents/ (keyword-matched)
    if cursor_ctx:
        agent_ctx = build_agent_context_block(inputs.get("description", ""), cursor_ctx)
        if agent_ctx:
            injections.append(agent_ctx)

    # 4. Explicit + auto-detected full skill content
    if project_root:
        skill_block = _build_skill_block(cfg, inputs.get("description", ""), project_root)
        if skill_block:
            injections.append(skill_block)

    if injections:
        # Replace {varName} patterns in injected docs with [varName] so CrewAI doesn't
        # treat naming conventions like {featureName} as unresolved template variables.
        safe = [re.sub(r'\{(\w+)\}', r'[\1]', s) for s in injections]
        description = description + "\n\n---\n\n" + "\n\n---\n\n".join(safe)

    return Task(
        description=description,
        expected_output=expected_output,
        agent=agent,
        context=context_tasks or [],
        callback=callback,
    )


def _extract_git_artifacts(output_text: str) -> dict:
    result = {"commit_msg": None, "pr_title": None, "pr_body": None, "files": []}
    in_pr_body = False
    pr_body_lines = []
    for line in output_text.splitlines():
        if line.startswith("COMMIT_MSG:"):
            result["commit_msg"] = line[len("COMMIT_MSG:"):].strip()
            in_pr_body = False
        elif line.startswith("PR_TITLE:"):
            result["pr_title"] = line[len("PR_TITLE:"):].strip()
            in_pr_body = False
        elif line.startswith("PR_BODY:"):
            in_pr_body = True
            first = line[len("PR_BODY:"):].strip()
            if first:
                pr_body_lines.append(first)
        elif line.startswith("FILES:"):
            in_pr_body = False
            result["files"] = [f.strip() for f in line[len("FILES:"):].split(",") if f.strip()]
        elif line.strip() == "DONE":
            in_pr_body = False
        elif in_pr_body:
            pr_body_lines.append(line)
    if pr_body_lines:
        result["pr_body"] = "\n".join(pr_body_lines)
    return result


def _write_extracted_files(project_root: Path, task_outputs: list[str]) -> list[str]:
    written = []
    pattern = re.compile(r"```(?:\w+\n)?([^\n`][^\n]*)\n(.*?)```", re.DOTALL)
    for output in task_outputs:
        for m in pattern.finditer(output):
            rel_path = m.group(1).strip().lstrip("/").lstrip("#").strip()
            content = m.group(2)
            if "/" not in rel_path and "." not in rel_path:
                continue
            if rel_path.startswith("/") or not rel_path:
                continue
            full_path = project_root / rel_path
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content)
            written.append(rel_path)
    return written


def _create_branch_and_pr(
    project_root: Path, branch: str, task_id: str, task_type: str,
    description: str, staged_files: list[str], linear_id: str = None,
    commit_msg: str = None, pr_title: str = None, pr_body: str = None,
    auto_pr: bool = True,
) -> dict:
    result = {}
    cwd = str(project_root)

    def run(cmd):
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)

    run(["git", "checkout", "-b", branch])
    for f in staged_files:
        run(["git", "add", f])
    msg = commit_msg or f"{task_type}: {description[:60]}"
    run(["git", "commit", "-m", msg])
    result["commit_msg"] = msg

    if auto_pr:
        title = pr_title or description[:60]
        if linear_id:
            title = f"[{linear_id}] {title}"
        body = pr_body or f"Task: {description}\n\nFiles changed:\n" + "\n".join(f"- {f}" for f in staged_files)
        pr_result = run(["gh", "pr", "create", "--title", title, "--body", body, "--draft", "--head", branch])
        if pr_result.returncode == 0:
            result["pr_url"] = pr_result.stdout.strip()
        else:
            result["pr_error"] = pr_result.stderr.strip()

    return result


def run_crew(
    project: str, task_id: str, description: str,
    task_type: str = None, linear_id: str = None,
    branch: str = None, auto_pr: bool = False,
) -> dict:
    from crewai import Crew, Process

    if not task_type:
        task_type = infer_task_type(description)

    if not branch:
        slug = re.sub(r"[^a-z0-9]+", "-", description.lower())[:40].strip("-")
        branch = f"{task_type}/{slug}"

    project_root = resolve_project_path(project)

    # Scan .cursor folder once — used by all tasks in this run
    cursor_ctx = scan_cursor_folder(project_root) if project_root else {}

    # Load structure hint from projects.yaml
    project_context = ""
    try:
        proj_cfg = yaml.safe_load((BASE / "projects.yaml").read_text())
        project_context = proj_cfg.get("projects", {}).get(project, {}).get("structure_hint", "")
    except Exception:
        pass

    task_names = TASK_MAP.get(task_type, TASK_MAP["feature"])
    tasks_cfg = _load_yaml(BASE / "tasks.yaml")

    # Unique agent names in order
    seen: dict[str, bool] = {}
    for tname in task_names:
        aname = tasks_cfg.get(tname, {}).get("agent", "")
        if aname and aname not in seen:
            seen[aname] = True
    unique_agent_names = list(seen.keys())

    run_logger.start_run(project, task_id, task_type, description, unique_agent_names)
    upsert_branch(project, task_id, branch, task_type, description, "running")

    # Derive featureName: last path component from "folder: X/Y/Z" or "feature/X" in description,
    # falling back to branch slug or task_id.
    _fn_match = re.search(r'(?:folder|feature)[:/\s]+([^\s,]+)', description, re.IGNORECASE)
    if _fn_match:
        feature_name = _fn_match.group(1).rstrip('/').split('/')[-1]
    else:
        # Use the descriptive part of the branch (strip task_id prefix)
        feature_name = re.sub(r'^[^/]+/\d+-[a-f0-9]+-?', '', branch).replace('-', ' ').strip() or task_id

    inputs = {
        "description": description,
        "linear_id": linear_id or "",
        "task_id": task_id,
        "branch": branch,
        "project_context": project_context,
        "featureName": feature_name,
    }

    agent_map = {name: _build_agent(name) for name in unique_agent_names}

    # Track which agents have finished — updated via task callbacks, polled by dashboard
    completed_agents_list: list[str] = []

    def make_step_callback(aname: str):
        def _cb(task_output) -> None:
            try:
                completed_agents_list.append(aname)
                run_logger.update_run(project, task_id, completed_agents=list(completed_agents_list))
            except Exception as e:
                print(f"[step_cb] {aname}: {e}")
        return _cb

    built_tasks = []
    for tname in task_names:
        agent_name = tasks_cfg[tname]["agent"]
        agent = agent_map.get(agent_name)
        if not agent:
            continue
        t = _build_task(tname, agent, built_tasks.copy(), inputs,
                        project_root=project_root, cursor_ctx=cursor_ctx, agent_name=agent_name,
                        callback=make_step_callback(agent_name))
        built_tasks.append(t)

    crew = Crew(agents=list(agent_map.values()), tasks=built_tasks, process=Process.sequential, verbose=True)
    result = crew.kickoff(inputs=inputs)

    task_outputs = [str(t.output) for t in built_tasks if hasattr(t, "output") and t.output]

    # Token tracking (best-effort)
    try:
        tu = getattr(result, "token_usage", None)
        if tu:
            run_logger.record_tokens(project, task_id, "crew",
                input_tokens=getattr(tu, "prompt_tokens", 0),
                output_tokens=getattr(tu, "completion_tokens", 0))
    except Exception:
        pass

    git_artifacts = _extract_git_artifacts(task_outputs[-1]) if task_outputs else {}

    staged_files = []
    pr_url = None
    if project_root:
        root_path = Path(project_root).expanduser()
        staged_files = _write_extracted_files(root_path, task_outputs)
        if staged_files and auto_pr:
            try:
                git_result = _create_branch_and_pr(
                    root_path, branch, task_id, task_type, description, staged_files,
                    linear_id=linear_id,
                    commit_msg=git_artifacts.get("commit_msg"),
                    pr_title=git_artifacts.get("pr_title"),
                    pr_body=git_artifacts.get("pr_body"),
                    auto_pr=auto_pr,
                )
                pr_url = git_result.get("pr_url")
            except Exception as e:
                print(f"Git/PR error: {e}")

    upsert_branch(project, task_id, branch, task_type, description,
                  "awaiting-review", pr_url=pr_url, staged_files=staged_files)
    run_logger.complete_run(project, task_id, outcome="draft_pr_created" if pr_url else "awaiting-review")

    return {"task_id": task_id, "branch": branch, "task_type": task_type,
            "staged_files": staged_files, "pr_url": pr_url}


def run_revision(project: str, task_id: str, feedback: str) -> dict:
    from registry import get_branch
    branch_info = get_branch(project, task_id)
    if not branch_info:
        return {"error": "task not found"}
    description = f"REVISION of {task_id}: {feedback}\nOriginal: {branch_info.get('description', '')}"
    original_type = branch_info.get("task_type", "refactor")
    return run_crew(project=project, task_id=f"{task_id}-rev", description=description,
                    task_type=original_type, branch=branch_info.get("branch"), auto_pr=False)
