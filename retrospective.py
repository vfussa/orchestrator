#!/usr/bin/env python3
"""
Retrospective agent — analyses run logs, identifies patterns, proposes
improvements to agents.yaml / tasks.yaml / routing matrix as a draft PR
against ~/crew-orchestrator itself.
"""

import json
import os
import sys
import subprocess
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
load_dotenv(Path(__file__).parent / ".env")

from logger import list_runs, count_runs

BASE = Path(__file__).parent
RETRO_TRIGGER_EVERY = 10  # runs


def _should_run_auto(project: str) -> bool:
    n = count_runs(project)
    return n > 0 and n % RETRO_TRIGGER_EVERY == 0


def analyse_runs(runs: list[dict]) -> dict:
    """Produce a structured analysis from run logs."""
    if not runs:
        return {}

    total = len(runs)
    flags = Counter()
    token_by_agent: dict[str, list[int]] = {}
    duration_by_type: dict[str, list[int]] = {}
    type_counts = Counter()

    for r in runs:
        for f in r.get("flags", []):
            flags[f] += 1
        for agent, usage in r.get("token_usage", {}).items():
            token_by_agent.setdefault(agent, []).append((usage.get("input", 0) + usage.get("output", 0)))
        tt = r.get("task_type", "unknown")
        type_counts[tt] += 1
        if r.get("duration_seconds"):
            duration_by_type.setdefault(tt, []).append(r["duration_seconds"])

    avg_tokens = {a: int(sum(v) / len(v)) for a, v in token_by_agent.items()}
    avg_duration = {t: int(sum(v) / len(v)) for t, v in duration_by_type.items()}

    return {
        "total_runs": total,
        "task_type_distribution": dict(type_counts),
        "top_flags": flags.most_common(5),
        "avg_tokens_per_agent": avg_tokens,
        "avg_duration_by_type": avg_duration,
        "high_token_agents": sorted(avg_tokens.items(), key=lambda x: -x[1])[:3],
    }


def generate_proposals(analysis: dict) -> list[dict]:
    """Rule-based proposal generation from analysis."""
    proposals = []

    # Flag-based routing improvements
    for flag, count in analysis.get("top_flags", []):
        if count >= 3:
            if flag == "architect_needed_but_skipped":
                proposals.append({
                    "type": "routing_matrix",
                    "change": "Add architect to bug task type",
                    "evidence": f"'{flag}' fired {count}x in last {analysis['total_runs']} runs",
                    "file": "router.py",
                })
            if flag == "qa_test_gaps_found":
                proposals.append({
                    "type": "agent_prompt",
                    "change": "Strengthen qa_engineer backstory to be more thorough on gap detection",
                    "evidence": f"'{flag}' fired {count}x",
                    "file": "agents.yaml",
                })

    # High token agent — consider downgrade
    for agent, avg_tok in analysis.get("high_token_agents", []):
        if avg_tok > 3000 and agent not in ("coder",):
            proposals.append({
                "type": "model_downgrade",
                "change": f"Consider downgrading {agent} — averaging {avg_tok} tokens, may be over-generating",
                "evidence": f"Avg output tokens across {analysis['total_runs']} runs",
                "file": "agents.yaml",
            })

    # Slow task types
    for task_type, avg_sec in analysis.get("avg_duration_by_type", {}).items():
        if avg_sec > 900:  # > 15 min
            proposals.append({
                "type": "pipeline",
                "change": f"'{task_type}' tasks averaging {avg_sec//60}m — consider reducing max_iter for non-critical agents",
                "evidence": f"Duration data from {analysis['total_runs']} runs",
                "file": "agents.yaml",
            })

    return proposals


def write_retrospective_report(project: str, analysis: dict, proposals: list[dict]) -> Path:
    retro_dir = BASE / "retrospectives" / project
    retro_dir.mkdir(parents=True, exist_ok=True)
    n = len(list(retro_dir.glob("*.md"))) + 1
    path = retro_dir / f"{n:03d}-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.md"

    lines = [
        f"# Retrospective #{n} — {project} — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
        "",
        f"**Runs analysed:** {analysis.get('total_runs', 0)}",
        f"**Proposals:** {len(proposals)}",
        "",
        "## Analysis",
        "",
        f"**Task type distribution:** {analysis.get('task_type_distribution', {})}",
        f"**Top flags:** {analysis.get('top_flags', [])}",
        "",
        "**Avg tokens per agent:**",
    ]
    for agent, tok in sorted(analysis.get("avg_tokens_per_agent", {}).items(), key=lambda x: -x[1]):
        lines.append(f"  - {agent}: {tok:,} tokens")

    lines += ["", "## Proposals", ""]
    for i, p in enumerate(proposals, 1):
        lines += [
            f"### {i}. [{p['type']}] {p['change']}",
            f"- **File:** `{p['file']}`",
            f"- **Evidence:** {p['evidence']}",
            "",
        ]

    if not proposals:
        lines.append("No significant issues found. Crew is performing within normal parameters.")

    path.write_text("\n".join(lines))
    return path


def create_improvement_pr(project: str, report_path: Path, proposals: list[dict]) -> str | None:
    """Create a draft PR on the crew-orchestrator repo with the retrospective report."""
    if not proposals:
        return None
    try:
        branch = f"retro/{project}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
        os.chdir(BASE)
        subprocess.run(["git", "checkout", "-b", branch], check=True, capture_output=True)
        subprocess.run(["git", "add", str(report_path)], check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", f"retro: {project} analysis — {len(proposals)} proposals"], check=True, capture_output=True)

        result = subprocess.run(
            ["gh", "pr", "create", "--draft",
             "--title", f"🔄 [{project}] Retrospective — {len(proposals)} proposals",
             "--body", report_path.read_text()[:4000]],
            capture_output=True, text=True
        )
        pr_url = result.stdout.strip()
        subprocess.run(["git", "checkout", "-"], capture_output=True)
        return pr_url
    except Exception as e:
        print(f"[retro] PR creation error: {e}")
        return None


def run_retrospective(project: str) -> dict:
    print(f"[retro] Running retrospective for {project}...")
    runs = list_runs(project, limit=50)
    analysis = analyse_runs(runs)
    proposals = generate_proposals(analysis)
    report_path = write_retrospective_report(project, analysis, proposals)
    pr_url = create_improvement_pr(project, report_path, proposals)
    print(f"[retro] Done. {len(proposals)} proposals. Report: {report_path}")
    if pr_url:
        print(f"[retro] Draft PR: {pr_url}")
    return {"proposals": len(proposals), "report": str(report_path), "pr_url": pr_url}


def check_and_run_auto(project: str) -> None:
    """Called after each crew run — fires retrospective every N runs."""
    if _should_run_auto(project):
        run_retrospective(project)


if __name__ == "__main__":
    project = sys.argv[1] if len(sys.argv) > 1 else "planable-app"
    run_retrospective(project)
