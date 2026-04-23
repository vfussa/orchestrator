"""Context loader — resolves project paths and scans .cursor folder for agent context."""

import os
import re
from pathlib import Path
import yaml


def load_projects() -> dict:
    cfg = Path(__file__).parent / "projects.yaml"
    with open(cfg) as f:
        return yaml.safe_load(f)["projects"]


def resolve_project_path(project_name: str) -> str | None:
    try:
        projects = load_projects()
        raw = projects.get(project_name, {}).get("path")
        return str(Path(os.path.expanduser(raw))) if raw else None
    except Exception:
        return None


def _read_stripped(path: Path, max_lines: int = 150) -> str:
    """Read a file, strip YAML frontmatter, truncate to max_lines."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""
    # Strip YAML frontmatter
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:]
    lines = text.splitlines()
    if len(lines) > max_lines:
        text = "\n".join(lines[:max_lines]) + f"\n... [{len(lines) - max_lines} lines truncated]"
    return text.strip()


def _skill_summary(skill_dir: Path) -> str:
    """Return a one-line summary for a skill from its SKILL.md."""
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return ""
    text = skill_md.read_text(errors="ignore")
    # Try to get description from frontmatter
    m = re.search(r"^description:\s*(.+)$", text, re.MULTILINE)
    if m:
        return m.group(1).strip()[:120]
    # Fall back to first non-empty content line after headings
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and not stripped.startswith("---") and len(stripped) > 20:
            return stripped[:120]
    return ""


def scan_cursor_folder(project_root: str) -> dict:
    """
    Scan the project's .cursor folder and return structured context.

    Returns:
        rules:        {name: content}  — all rule files, inject per-agent
        skills_index: {name: summary}  — one-line per skill, inject into planner
        commands:     [name, ...]      — available command names
        agents:       {name: content}  — specialized agent context files
    """
    cursor = Path(project_root).expanduser() / ".cursor"
    result = {"rules": {}, "skills_index": {}, "commands": [], "agents": {}}

    if not cursor.exists():
        return result

    # ── Rules ─────────────────────────────────────────────────────────────────
    rules_dir = cursor / "rules"
    if rules_dir.exists():
        for f in sorted(rules_dir.iterdir()):
            if f.is_file() and f.suffix in (".md", ".mdc"):
                content = _read_stripped(f, max_lines=150)
                if content:
                    result["rules"][f.stem] = content

    # ── Skills index (names + one-line summary) ────────────────────────────────
    skills_dir = cursor / "skills"
    if skills_dir.exists():
        for d in sorted(skills_dir.iterdir()):
            if d.is_dir():
                summary = _skill_summary(d)
                result["skills_index"][d.name] = summary

    # ── Commands (names only) ──────────────────────────────────────────────────
    commands_dir = cursor / "commands"
    if commands_dir.exists():
        for f in sorted(commands_dir.rglob("*.md")):
            rel = f.relative_to(commands_dir)
            result["commands"].append(str(rel.with_suffix("")))

    # ── Agents (specialized context files) ────────────────────────────────────
    agents_dir = cursor / "agents"
    if agents_dir.exists():
        for f in sorted(agents_dir.iterdir()):
            if f.is_file() and f.suffix == ".md":
                content = _read_stripped(f, max_lines=100)
                if content:
                    result["agents"][f.stem] = content

    return result


# Which rules each agent type cares about
AGENT_RULES: dict[str, list[str]] = {
    "planner":           ["preferences"],
    "architect":         ["clean-code", "preferences"],
    "coder":             ["clean-code", "git-conventions", "preferences"],
    "tester":            ["e2e-standards", "test-naming-conventions"],
    "pr_reviewer":       ["clean-code", "git-conventions"],
    "standards_guardian":["clean-code", "git-conventions", "e2e-standards", "test-naming-conventions"],
    "git_expert":        ["git-conventions"],
}

# Which .cursor/agents files to inject per agent role (keyword → agent file)
AGENT_CONTEXT_KEYWORDS: dict[str, list[str]] = {
    "mongodb-specialist":         ["mongo", "collection", "aggregate", "db."],
    "trpc-api":                   ["trpc", "router", "procedure", "api route"],
    "analytics-metrics":          ["analytics", "event", "tracking", "mixpanel"],
    "stripe-payments":            ["stripe", "payment", "subscription", "billing"],
    "social-media-integrations":  ["instagram", "tiktok", "facebook", "twitter", "linkedin"],
    "background-jobs":            ["job", "queue", "worker", "cron", "async task"],
    "security-auditor":           ["auth", "permission", "token", "xss", "injection"],
    "code-improver":              ["refactor", "cleanup", "improve", "simplify"],
}


def build_rules_block(agent_name: str, cursor_ctx: dict) -> str:
    """Return relevant rules for a given agent as a formatted block."""
    relevant = AGENT_RULES.get(agent_name, [])
    parts = []
    for rule_name in relevant:
        content = cursor_ctx["rules"].get(rule_name)
        if content:
            parts.append(f"## RULE: {rule_name}\n{content}")
    return "\n\n---\n\n".join(parts)


def build_skills_index_block(cursor_ctx: dict) -> str:
    """Return the full skills index for injection into the planner."""
    if not cursor_ctx["skills_index"]:
        return ""
    lines = ["## AVAILABLE SKILLS", "The following skills exist in this project. Reference them by name when relevant.\n"]
    for name, summary in cursor_ctx["skills_index"].items():
        lines.append(f"- **{name}**: {summary}" if summary else f"- **{name}**")
    if cursor_ctx["commands"]:
        lines.append("\n## AVAILABLE COMMANDS")
        for cmd in cursor_ctx["commands"]:
            lines.append(f"- {cmd}")
    return "\n".join(lines)


def build_agent_context_block(description: str, cursor_ctx: dict) -> str:
    """Inject specialized .cursor/agents context based on description keywords."""
    desc_lower = description.lower()
    parts = []
    for agent_file, keywords in AGENT_CONTEXT_KEYWORDS.items():
        if any(kw in desc_lower for kw in keywords):
            content = cursor_ctx["agents"].get(agent_file)
            if content:
                parts.append(f"## SPECIALIST CONTEXT: {agent_file}\n{content}")
    return "\n\n---\n\n".join(parts)

def load_task_skill_context(project_root: str, task_type: str, project_cfg: dict) -> str:
    """Load CLAUDE.md + task_type-specific skill files from the repo.

    Configured per project in projects.yaml:
      context_files: [CLAUDE.md]          # always loaded
      skill_map:
        test:    [.cursor/skills/e2e-tests/SKILL.md, ...]
        feature: [.cursor/skills/feature-development/SKILL.md]
    """
    if not project_root:
        return ""
    root = Path(project_root)
    parts = []

    # Always-on context files (e.g. CLAUDE.md)
    for rel in project_cfg.get("context_files", []):
        p = root / rel
        content = _read_stripped(p, max_lines=250)
        if content:
            parts.append(f"=== {rel} ===
{content}")

    # Skill files keyed to this task_type
    skill_map = project_cfg.get("skill_map", {})
    skill_files = skill_map.get(task_type, skill_map.get("default", []))
    for rel in skill_files:
        p = root / rel
        content = _read_stripped(p, max_lines=300)
        if content:
            parts.append(f"=== skill: {rel} ===
{content}")

    return "

".join(parts)
