"""Router — infers task type from description and maps to agent sequences."""


def infer_task_type(description: str) -> str:
    """Classify a task description into a task type for TASK_MAP routing."""
    desc = description.lower()
    if any(w in desc for w in ["hotfix", "urgent", "critical", "production down"]):
        return "hotfix"
    if any(w in desc for w in ["fix", "bug", "broken", "error", "crash", "issue", "patch", "not working"]):
        return "bug"
    if any(w in desc for w in ["test", "spec", "e2e", "playwright", "coverage", "flake", "flaky"]):
        return "test"
    if any(w in desc for w in ["refactor", "cleanup", "clean up", "reorganize", "restructure", "simplify"]):
        return "refactor"
    if any(w in desc for w in ["docs", "documentation", "readme", "changelog", "comment"]):
        return "docs"
    if any(w in desc for w in ["chore", "dependency", "upgrade", "bump", "update package", "ci", "config"]):
        return "chore"
    return "feature"


def get_agents_for_task(task_type: str) -> list[str]:
    """Return the ordered agent sequence for a task type (informational only)."""
    sequences = {
        "feature":  ["git_expert", "planner", "architect", "coder", "tester", "pr_reviewer", "standards_guardian", "git_expert"],
        "bug":      ["git_expert", "planner", "coder", "tester", "pr_reviewer", "standards_guardian", "git_expert"],
        "hotfix":   ["coder", "tester", "pr_reviewer", "git_expert"],
        "refactor": ["git_expert", "planner", "architect", "coder", "tester", "pr_reviewer", "standards_guardian", "git_expert"],
        "test":     ["git_expert", "tester", "pr_reviewer", "git_expert"],
        "docs":     ["git_expert", "planner", "coder", "standards_guardian", "git_expert"],
        "chore":    ["coder", "git_expert"],
    }
    return sequences.get(task_type, sequences["feature"])


def unique_agents(task_type: str) -> list[str]:
    """Return deduped agent names for a task type (legacy — prefer tasks.yaml derivation)."""
    return list(dict.fromkeys(get_agents_for_task(task_type)))
