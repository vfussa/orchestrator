"""Crew runner — builds and executes a CrewAI crew for a given task."""

import os
import yaml
from pathlib import Path
from dotenv import load_dotenv

from router import infer_task_type, get_agents_for_task, unique_agents
from context_loader import load_context_for_agent, resolve_project_path
from registry import upsert_branch, update_branch_status, update_branch_fields
import logger as run_logger

load_dotenv(Path(__file__).parent / ".env")

BASE = Path(__file__).parent
CAVEMAN_PROMPT = (
    "Terse like caveman. Technical substance exact. Only fluff die. "
    "Drop: articles, filler, pleasantries, hedging. Fragments OK. "
    "Short synonyms. Code unchanged. Pattern: [thing] [action] [reason]. [next step]. "
    "ACTIVE EVERY RESPONSE. No revert. No filler drift."
)


def _load_yaml(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


# Keywords that trigger auto-injection of a skill even when not explicitly listed
SKILL_AUTO_KEYWORDS: dict[str, list[str]] = {
    "ui-implementation": ["component", "tsx", "jsx", "button", "modal", "form", "tailwind", "ui", "render", "style", "dialog", "dropdown"],
    "react-component-tests": ["component", "tsx", "hook", "render", "rtl", "testing-library"],
    "e2e-tests": ["e2e", "playwright", "visual test", "end-to-end", "automation", "spec", "pom", "test", "tests", "coverage", "flake", "flaky"],
    "unit-tests": ["unit test", "node:assert", "sinon", "trpc", "meteor method"],
    "mongodb": ["mongo", "collection", "query", "aggregate", "db.", "database"],
}


def _extract_section(text: str, heading: str) -> str:
    """Extract a single ## heading section from markdown text (up to the next ## or end)."""
    import re
    pattern = re.compile(
        rf'^## {re.escape(heading)}.*?(?=^## |\Z)',
        re.MULTILINE | re.DOTALL,
    )
    m = pattern.search(text)
    return m.group(0).strip() if m else ""


def _load_file_stripped(path: Path, max_lines: int = 100) -> str:
    """Load a file, strip YAML frontmatter (--- ... ---), return up to max_lines."""
    if not path.exists():
        return ""
    try:
        lines = path.read_text().splitlines()
        start = 0
        if lines and lines[0].strip() == "---":
            try:
                end_idx = next(i for i, ln in enumerate(lines[1:], 1) if ln.strip() == "---")
                start = end_idx + 1
            except StopIteration:
                pass
        return "\n".join(lines[start:start + max_lines]).strip()
    except Exception as e:
        print(f"[skills] failed to load {path}: {e}")
        return ""


def _resolve_skills(explicit: list[str], description: str, project_root: Path) -> list[str]:
    """Merge explicit skills with auto-detected ones, deduped, in order."""
    selected = list(explicit)
    desc_lower = description.lower()
    for skill_name, keywords in SKILL_AUTO_KEYWORDS.items():
        if skill_name not in selected and any(kw in desc_lower for kw in keywords):
            selected.append(skill_name)
    return selected


def _build_context_block(cfg: dict, description: str, project_root: Path) -> str:
    """Build the PROJECT CONVENTIONS injection block from skills, rules, and commands."""
    parts = []
    skill_sections_cfg = cfg.get("skill_sections", {})

    # 1. Skills — with optional named section extraction
    for name in _resolve_skills(cfg.get("skills", []), description, project_root):
        skill_path = project_root / ".cursor" / "skills" / name / "SKILL.md"
        if not skill_path.exists():
            continue
        sections = skill_sections_cfg.get(name, [])
        if sections:
            full_text = skill_path.read_text()
            section_parts = [_extract_section(full_text, h) for h in sections]
            content = "\n\n".join(p for p in section_parts if p)
        else:
            max_l = 80 if name in ("e2e-tests", "feature-development") else 100
            content = _load_file_stripped(skill_path, max_lines=max_l)
        if content:
            parts.append(f"=== SKILL: {name} ===\n{content}\n=== END ===")

    # 2. Rules — .cursor/rules/*.mdc
    for rule_name in cfg.get("rules", []):
        path = project_root / ".cursor" / "rules" / f"{rule_name}.mdc"
        content = _load_file_stripped(path, max_lines=60)
        if content:
            parts.append(f"=== RULE: {rule_name} ===\n{content}\n=== END ===")

    # 3. Commands — .cursor/commands/**/*.md (injected as self-review instructions)
    for cmd_path in cfg.get("commands", []):
        path = project_root / ".cursor" / "commands" / f"{cmd_path}.md"
        content = _load_file_stripped(path, max_lines=80)
        if content:
            parts.append(f"=== SELF-REVIEW COMMAND: {cmd_path} ===\n{content}\n=== END ===")

    return "\n\n".join(parts)


def _load_project_context(project: str) -> str:
    """Read the structure_hint for this project from projects.yaml."""
    try:
        cfg = _load_yaml(BASE / "projects.yaml")
        hint = cfg.get("projects", {}).get(project, {}).get("structure_hint", "")
        return hint.strip()
    except Exception:
        return ""


def _stage_files_only(project_root: Path, branch: str, files: list[str]) -> None:
    """Checkout branch, lint-fix, and stage files — but do NOT commit."""
    import subprocess

    def run(cmd):
        return subprocess.run(cmd, cwd=str(project_root), capture_output=True, text=True, timeout=60)

    result = run(["git", "checkout", "-b", branch])
    if result.returncode != 0:
        run(["git", "checkout", branch])

    if files:
        run(["npx", "eslint", "--fix", "--quiet"] + files)
        run(["npx", "prettier", "--write"] + files)

    for f in files:
        run(["git", "add", f])

    print(f"[git] staged (not committed): {files}")


def _create_branch_and_pr(
    project_root: Path, branch: str, task_id: str, task_type: str, description: str,
    files: list[str], linear_id: str = None, commit_msg: str = None,
    pr_title: str = None, pr_body: str = None,
) -> dict:
    """Checkout branch, commit extracted files, push, open a draft PR via gh CLI."""
    import subprocess

    def run(cmd):
        return subprocess.run(cmd, cwd=str(project_root), capture_output=True, text=True, timeout=60)

    # Create branch (ok if it already exists)
    result = run(["git", "checkout", "-b", branch])
    if result.returncode != 0:
        run(["git", "checkout", branch])

    # Stage only the files the crew wrote
    if not files:
        print("[git] no files to commit")
        return {}
    for f in files:
        run(["git", "add", f])

    # Try auto-fixing lint on just the extracted files (best effort — pre-commit hook validates)
    if files:
        run(["npx", "eslint", "--fix", "--quiet"] + files)
        run(["npx", "prettier", "--write"] + files)
    # Re-stage after auto-fix
    for f in files:
        run(["git", "add", f])

    # Commit message — use git_expert output if provided, else fall back to generated one
    if not commit_msg:
        raw_desc = description[:60]
        short_desc = raw_desc[:1].upper() + raw_desc[1:]
        emoji_map = {
            "feature": "⭐️", "bug": "🐞", "test": "✅", "refactor": "♻️",
            "hotfix": "🚨", "docs": "📓", "chore": "🛠",
        }
        emoji = emoji_map.get(task_type, "👌")
        if linear_id and linear_id.startswith("P-"):
            commit_msg = f"{emoji} [{linear_id}] {short_desc}"
        else:
            commit_msg = f"{emoji} {short_desc}"

    commit = run(["git", "commit", "-m", commit_msg])
    if commit.returncode != 0:
        print(f"[git] commit failed (lint/hook): {commit.stderr[-500:]}")
        # Clean up staged files so working tree is left unstaged
        run(["git", "reset", "HEAD"] + files)
        return {"committed": False, "error": "pre-commit hook failed"}

    # Push
    push = run(["git", "push", "-u", "origin", branch])
    if push.returncode != 0:
        print(f"[git] push failed: {push.stderr}")
        return {"committed": True}

    # Create draft PR — use git_expert output if provided, else fall back to generated defaults
    if not pr_title:
        pr_title = commit_msg
    if not pr_body:
        pr_body = (
            f"Generated by crew orchestrator.\n\n"
            f"**Task:** {task_id}  \n**Type:** {task_type}  \n**Branch:** {branch}\n\n"
            f"---\n🤖 Auto-generated draft — review before merging."
        )
    pr = run(["gh", "pr", "create", "--draft", "--title", pr_title, "--body", pr_body])
    if pr.returncode == 0:
        pr_url = pr.stdout.strip()
        print(f"[git] PR created: {pr_url}")
        return {"committed": True, "pr_url": pr_url}
    else:
        print(f"[git] PR creation failed: {pr.stderr}")
        return {"committed": True}


def _build_agent(name: str, project: str):
    from crewai import Agent
    agents_cfg = _load_yaml(BASE / "agents.yaml")
    cfg = agents_cfg[name]
    backstory = cfg["backstory"].format(caveman=CAVEMAN_PROMPT)
    return Agent(
        role=cfg["role"],
        goal=cfg["goal"],
        backstory=backstory,
        llm=cfg.get("llm", "groq/llama-3.3-70b-versatile"),
        max_iter=1,
        verbose=True,
    )


def _extract_and_write_files(output_text: str, project_root: Path) -> list[str]:
    """Parse markdown code blocks with filenames from crew output and write them to disk.

    Looks for blocks in the format:
        ```typescript
        // path/to/file.tests.ts
        ... code ...
        ```
    or:
        **File: path/to/file.tests.ts**
        ```typescript
        ... code ...
        ```
    Returns list of file paths written.
    """
    import re
    written = []

    # Pattern 1: code block where first line is a comment with a path
    pattern1 = re.compile(
        r'```(?:typescript|ts|javascript|js|tsx|jsx|python|py|bash|sh)?\n'
        r'(?://|#)\s*([\w./\-]+\.(?:ts|tsx|js|jsx|py|sh|tests\.ts))\n'
        r'(.*?)```',
        re.DOTALL,
    )
    for m in pattern1.finditer(output_text):
        rel_path, code = m.group(1).strip(), m.group(2)
        _write_extracted(project_root, rel_path, code, written)

    # Pattern 2: **File: path** label before a code block
    pattern2 = re.compile(
        r'\*\*(?:File|Path|Filename|file):\s*([\w./\-]+\.(?:ts|tsx|js|jsx|py|sh|tests\.ts))\*\*\s*\n'
        r'```[^\n]*\n(.*?)```',
        re.DOTALL,
    )
    for m in pattern2.finditer(output_text):
        rel_path, code = m.group(1).strip(), m.group(2)
        _write_extracted(project_root, rel_path, code, written)

    return written


def _write_extracted(project_root: Path, rel_path: str, code: str, written: list):
    """Write an extracted code block to disk, skipping if file already in written."""
    if rel_path in written:
        return
    try:
        target = project_root / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(code)
        written.append(rel_path)
        print(f"[extractor] wrote {target}")
    except Exception as e:
        print(f"[extractor] failed to write {rel_path}: {e}")


def _build_task(task_name: str, agent, context_tasks: list, inputs: dict, project_root: Path = None):
    from crewai import Task
    tasks_cfg = _load_yaml(BASE / "tasks.yaml")
    cfg = tasks_cfg[task_name]
    description = cfg["description"].format(**{k: v or "" for k, v in inputs.items()})

    # Inject project conventions (skills, rules, commands)
    if project_root:
        conventions_block = _build_context_block(cfg, description, project_root)
        if conventions_block:
            description = f"{description}\n\nPROJECT CONVENTIONS (follow these exactly):\n{conventions_block}"

    return Task(
        description=description,
        expected_output=cfg["expected_output"],
        agent=agent,
        context=context_tasks or [],
    )


def run_revision(project: str, task_id: str, feedback: str) -> dict:
    """Analyze feedback against staged files → simple revise or complex re-plan+code."""
    from crewai import Agent, Task, Crew, Process
    from registry import get_branch, update_branch_fields, add_message
    from context_loader import resolve_project_path

    branch_info = get_branch(project, task_id)
    if not branch_info:
        return {"error": "task not found"}

    staged_files = branch_info.get("staged_files", [])
    branch = branch_info["branch"]
    project_root_str = resolve_project_path(project)
    if not project_root_str:
        return {"error": "project path not resolved"}

    # Mark as running so the task appears in Active Crews during revision
    update_branch_status(project, task_id, "running")
    project_root = Path(project_root_str)

    # Save feedback to chat history
    add_message(project, task_id, "user", feedback)

    # Read existing staged file contents from disk
    file_contents = ""
    for f in staged_files:
        path = project_root / f
        if path.exists():
            ext = path.suffix.lstrip(".")
            content = path.read_text()
            file_contents += f"\n\n### FILE: {f}\n```{ext}\n{content}\n```"
    if not file_contents:
        file_contents = "(no staged files found on disk)"

    agents_cfg = _load_yaml(BASE / "agents.yaml")

    def _inline_agent(name):
        cfg = agents_cfg[name]
        backstory = cfg["backstory"].format(caveman=CAVEMAN_PROMPT)
        return Agent(
            role=cfg["role"],
            goal=cfg["goal"],
            backstory=backstory,
            llm=cfg.get("llm", "groq/llama-3.3-70b-versatile"),
            max_iter=cfg.get("max_iter", 2),
            verbose=True,
        )

    # ── Step 1: analyze ────────────────────────────────────────────────────────
    analyze_desc = (
        f"Existing staged files:\n{file_contents}\n\n"
        f"User feedback: \"{feedback}\"\n\n"
        f"Classify the feedback:\n"
        f"  simple  — surgical edit (add/remove/rename/fix something specific)\n"
        f"  complex — new approach, significant refactor, or architectural change\n\n"
        f"Output EXACTLY in this format, nothing else:\n"
        f"COMPLEXITY: simple\n"
        f"CHANGE_PLAN:\n"
        f"- what to change (one bullet per item)"
    )
    analyzer = _inline_agent("feedback_analyzer")
    analyze_task = Task(
        description=analyze_desc,
        expected_output="COMPLEXITY: simple|complex\nCHANGE_PLAN:\n- ...",
        agent=analyzer,
    )
    Crew(agents=[analyzer], tasks=[analyze_task], process=Process.sequential, verbose=True).kickoff()
    analysis_text = getattr(getattr(analyze_task, "output", None), "raw", None) or str(getattr(analyze_task, "output", ""))

    complexity = "complex" if "COMPLEXITY: complex" in analysis_text else "simple"
    change_plan = analysis_text.split("CHANGE_PLAN:")[1].strip() if "CHANGE_PLAN:" in analysis_text else analysis_text

    print(f"[revision] complexity={complexity}")

    # ── Step 2: execute ────────────────────────────────────────────────────────
    if complexity == "simple":
        revise_desc = (
            f"Existing files:\n{file_contents}\n\n"
            f"Change plan:\n{change_plan}\n\n"
            f"Apply ONLY these changes. Keep everything else identical.\n"
            f"Output each modified file as a code block. "
            f"First line inside the block must be a comment with the FULL file path.\n"
            f"Example:\n"
            f"```typescript\n// imports/utils/example.ts\n// full file content here\n```"
        )
        reviser = _inline_agent("reviser")
        revise_task = Task(
            description=revise_desc,
            expected_output="Full updated files as code blocks with path comment on first line.",
            agent=reviser,
        )
        Crew(agents=[reviser], tasks=[revise_task], process=Process.sequential, verbose=True).kickoff()
        output_text = getattr(getattr(revise_task, "output", None), "raw", None) or str(getattr(revise_task, "output", ""))

    else:
        # Complex: planner → coder, both seeded with existing file contents
        project_context = _load_project_context(project)
        plan_desc = (
            f"Original task: \"{branch_info.get('description', '')}\"\n"
            f"User revision feedback: \"{feedback}\"\n\n"
            f"Existing files (refine these — do NOT start from scratch):\n{file_contents}\n\n"
            f"Change plan from analysis:\n{change_plan}\n\n"
            f"Produce a targeted implementation plan listing ONLY what changes."
        )
        code_desc = (
            f"PROJECT STRUCTURE: {project_context}\n\n"
            f"Existing files (starting point):\n{file_contents}\n\n"
            f"Apply the plan from context. Output ALL modified files as code blocks.\n"
            f"First line inside each block must be a comment with the FULL file path.\n"
            f"Keep unchanged code identical."
        )
        planner = _inline_agent("planner")
        coder = _inline_agent("coder")
        plan_t = Task(description=plan_desc, expected_output="Targeted change list. No prose.", agent=planner)
        code_t = Task(description=code_desc, expected_output="Full updated files as code blocks.", agent=coder, context=[plan_t])
        Crew(agents=[planner, coder], tasks=[plan_t, code_t], process=Process.sequential, verbose=True).kickoff()
        plan_raw = getattr(getattr(plan_t, "output", None), "raw", None) or ""
        code_raw = getattr(getattr(code_t, "output", None), "raw", None) or ""
        output_text = "\n\n---\n\n".join(filter(None, [plan_raw, code_raw]))

    # ── Step 3: extract files, re-write, re-stage ──────────────────────────────
    written = _extract_and_write_files(output_text, project_root)
    all_files = list(set(staged_files + written))
    if all_files:
        _stage_files_only(project_root, branch, all_files)
        update_branch_fields(project, task_id, staged_files=all_files)

    # Record outcome in chat
    summary = (
        f"[revision:{complexity}] Files updated: {written}"
        if written else
        f"[revision:{complexity}] No file blocks extracted — check output."
    )
    add_message(project, task_id, "crew", summary)

    # Reset status back to awaiting-review so user can continue reviewing or commit
    update_branch_status(project, task_id, "awaiting-review")

    return {"task_id": task_id, "complexity": complexity, "revised_files": written}


def run_crew(
    project: str,
    task_id: str,
    description: str,
    task_type: str = None,
    linear_id: str = None,
    branch: str = None,
    auto_pr: bool = True,
) -> dict:
    from crewai import Crew, Process

    if not task_type:
        task_type = infer_task_type(description)

    if not branch:
        slug = description.lower().replace(" ", "-")[:40]
        branch = f"feature/{task_id}-{slug}" if task_type == "feature" else f"{task_type}/{task_id}-{slug}"

    # Map task type → which task definitions to run
    TASK_MAP = {
        "feature":  ["git_preflight_task", "plan_task", "architect_task", "code_task", "test_task", "review_task", "standards_task", "git_task"],
        "bug":      ["git_preflight_task", "plan_task", "code_task", "test_task", "review_task", "standards_task", "git_task"],
        "hotfix":   ["code_task", "test_task", "review_task", "git_task"],
        "refactor": ["git_preflight_task", "plan_task", "architect_task", "code_task", "test_task", "review_task", "standards_task", "git_task"],
        "test":     ["git_preflight_task", "test_task", "review_task", "git_task"],
        "docs":     ["git_preflight_task", "plan_task", "code_task", "standards_task", "git_task"],
        "chore":    ["code_task", "git_task"],
    }

    task_names = TASK_MAP.get(task_type, TASK_MAP["feature"])

    # Derive unique agent names directly from tasks config (no router dependency)
    tasks_cfg_pre = _load_yaml(BASE / "tasks.yaml")
    unique_agent_names = list(dict.fromkeys(
        tasks_cfg_pre[t]["agent"] for t in task_names if t in tasks_cfg_pre
    ))

    # Start run log
    run_logger.start_run(project, task_id, task_type, description, unique_agent_names)
    upsert_branch(project, task_id, branch, task_type, description, "running", auto_pr=auto_pr)

    inputs = {
        "description": description,
        "linear_id": linear_id or "",
        "task_id": task_id,
        "branch": branch,
        "project_context": _load_project_context(project),
        "plan_output": "",
        "architect_output": "",
        "code_output": "",
        "test_output": "",
        "review_output": "",
    }

    # Build agent map
    agent_map = {name: _build_agent(name, project) for name in unique_agent_names}

    # Assign agents to tasks by config
    tasks_cfg = _load_yaml(BASE / "tasks.yaml")
    project_root_for_skills = Path(resolve_project_path(project)) if resolve_project_path(project) else None
    built_tasks = []
    for tname in task_names:
        agent_name = tasks_cfg[tname]["agent"]
        agent = agent_map.get(agent_name)
        if not agent:
            continue
        t = _build_task(tname, agent, built_tasks.copy(), inputs, project_root=project_root_for_skills)
        built_tasks.append(t)

    crew = Crew(
        agents=list(agent_map.values()),
        tasks=built_tasks,
        process=Process.sequential,
        verbose=True,
    )

    result = crew.kickoff(inputs=inputs)

    # Collect all task outputs into a single text (extractor needs intermediate outputs too)
    all_outputs = []
    for task_obj in built_tasks:
        try:
            task_out = getattr(task_obj, "output", None)
            if task_out:
                raw = getattr(task_out, "raw", None) or str(task_out)
                all_outputs.append(raw)
        except Exception:
            pass
    all_outputs.append(str(result))
    combined_output = "\n\n---\n\n".join(filter(None, all_outputs))

    # Record token usage from CrewAI's UsageMetrics
    try:
        tu = getattr(result, "token_usage", None)
        if tu:
            # Try per-agent breakdown first (crewai >= 0.80 exposes agent usage via tasks)
            agent_tokens: dict[str, dict] = {}
            for task_obj in built_tasks:
                agent_name = getattr(task_obj.agent, "role", "agent").lower().replace(" ", "_")
                usage = getattr(task_obj, "token_usage", None) or getattr(task_obj.output, "token_usage", None) if hasattr(task_obj, "output") else None
                if usage:
                    agent_tokens[agent_name] = {
                        "input": getattr(usage, "prompt_tokens", 0),
                        "output": getattr(usage, "completion_tokens", 0),
                    }
            if agent_tokens:
                for aname, counts in agent_tokens.items():
                    run_logger.record_tokens(project, task_id, aname, counts["input"], counts["output"])
            else:
                # Fall back to crew-level total
                run_logger.record_tokens(
                    project, task_id, "crew",
                    input_tokens=getattr(tu, "prompt_tokens", 0),
                    output_tokens=getattr(tu, "completion_tokens", 0),
                )
    except Exception:
        pass  # token tracking is best-effort

    # Save all task outputs concatenated
    try:
        run_logger.save_output(project, task_id, combined_output)
    except Exception:
        pass

    # Extract code blocks from ALL task outputs and write them as real files
    written = []
    project_root_path = None
    try:
        project_root_path = resolve_project_path(project)
        if project_root_path:
            written = _extract_and_write_files(combined_output, Path(project_root_path))
            if written:
                run_logger.update_run(project, task_id, files_written=written)
                print(f"[extractor] files written: {written}")
    except Exception as e:
        print(f"[extractor] error: {e}")

    # Extract git artifacts from git_task output (COMMIT_MSG, PR_TITLE, PR_BODY)
    git_commit_msg = None
    git_pr_title = None
    git_pr_body = None
    try:
        for i, tname in enumerate(task_names):
            if tname == "git_task" and i < len(built_tasks):
                git_raw = getattr(getattr(built_tasks[i], "output", None), "raw", None) or ""
                lines = git_raw.splitlines()
                pr_body_lines = []
                in_pr_body = False
                for line in lines:
                    if line.startswith("COMMIT_MSG:") and not git_commit_msg:
                        git_commit_msg = line.split("COMMIT_MSG:", 1)[1].strip()
                    elif line.startswith("PR_TITLE:") and not git_pr_title:
                        git_pr_title = line.split("PR_TITLE:", 1)[1].strip()
                        in_pr_body = False
                    elif line.startswith("PR_BODY:"):
                        in_pr_body = True
                        first = line.split("PR_BODY:", 1)[1].strip()
                        if first:
                            pr_body_lines.append(first)
                    elif line.startswith("FILES:") or line.startswith("DONE:"):
                        in_pr_body = False
                    elif in_pr_body:
                        pr_body_lines.append(line)
                if pr_body_lines:
                    git_pr_body = "\n".join(pr_body_lines).strip()
                break
        if git_commit_msg:
            print(f"[git] using git_expert commit msg: {git_commit_msg}")
    except Exception as e:
        print(f"[git] failed to extract git_task output: {e}")

    # Git commit + draft PR  (or stage-only when auto_pr=False)
    pr_url = None
    outcome = "files_extracted" if written else "no_files"
    if written and project_root_path:
        if not auto_pr:
            # Stage files but don't commit — wait for user review
            try:
                _stage_files_only(Path(project_root_path), branch, written)
                update_branch_fields(project, task_id, staged_files=written)
                outcome = "awaiting_review"
            except Exception as e:
                print(f"[git] stage error: {e}")
        else:
            try:
                git_result = _create_branch_and_pr(
                    Path(project_root_path), branch, task_id, task_type, description, written,
                    linear_id=linear_id, commit_msg=git_commit_msg,
                    pr_title=git_pr_title, pr_body=git_pr_body,
                )
                pr_url = git_result.get("pr_url")
                if pr_url:
                    outcome = "draft_pr_created"
                elif git_result.get("committed"):
                    outcome = "committed"
            except Exception as e:
                print(f"[git] error: {e}")

    # Check for cancel marker written by the dashboard cancel endpoint
    cancel_marker = BASE / "runs" / project / f"{task_id}.cancelled"
    if cancel_marker.exists():
        cancel_marker.unlink(missing_ok=True)
        run_logger.complete_run(project, task_id, outcome="cancelled")
        update_branch_status(project, task_id, "cancelled")
        return {"task_id": task_id, "cancelled": True}

    run_logger.complete_run(project, task_id, outcome=outcome, pr_url=pr_url)
    final_status = "awaiting-review" if outcome == "awaiting_review" else "review-ready"
    update_branch_status(project, task_id, final_status)

    return {"task_id": task_id, "branch": branch, "task_type": task_type, "result": str(result)}
