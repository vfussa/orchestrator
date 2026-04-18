"""Sends Slack and Linear notifications when a crew completes."""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")


def notify_slack(task_id: str, description: str, branch: str, pr_url: str, project: str) -> bool:
    token = os.getenv("SLACK_BOT_TOKEN", "")
    channel = _get_channel(project)
    if not token or not channel:
        print(f"[notify] Slack skipped — SLACK_BOT_TOKEN or channel not configured")
        return False

    try:
        from slack_sdk import WebClient
        client = WebClient(token=token)
        pr_text = f" · <{pr_url}|Draft PR>" if pr_url else ""
        client.chat_postMessage(
            channel=channel,
            text=f"[CREW DONE] {task_id} · \"{description}\"{pr_text} · Branch: `{branch}`",
            unfurl_links=False,
        )
        print(f"[notify] Slack sent to {channel}")
        return True
    except Exception as e:
        print(f"[notify] Slack error: {e}")
        return False


def notify_linear(task_id: str, description: str, pr_url: str) -> bool:
    api_key = os.getenv("LINEAR_API_KEY", "")
    if not api_key:
        print("[notify] Linear skipped — LINEAR_API_KEY not configured")
        return False

    # Linear issue ID must be in format P-XXXXX or similar
    # We move issue to "In Review" and post a comment
    try:
        import httpx
        headers = {"Authorization": api_key, "Content-Type": "application/json"}
        base = "https://api.linear.app/graphql"

        # Find the issue by identifier
        query = """
        query($id: String!) {
          issue(id: $id) { id state { id } }
        }"""
        r = httpx.post(base, json={"query": query, "variables": {"id": task_id}}, headers=headers, timeout=10)
        issue = r.json().get("data", {}).get("issue")
        if not issue:
            print(f"[notify] Linear issue {task_id} not found")
            return False

        issue_gql_id = issue["id"]

        # Post comment
        pr_text = f"\n\nDraft PR: {pr_url}" if pr_url else ""
        comment_mutation = """
        mutation($issueId: String!, $body: String!) {
          commentCreate(input: { issueId: $issueId, body: $body }) { success }
        }"""
        httpx.post(base, json={
            "query": comment_mutation,
            "variables": {"issueId": issue_gql_id, "body": f"Crew done. {description}{pr_text}"},
        }, headers=headers, timeout=10)

        print(f"[notify] Linear comment posted on {task_id}")
        return True
    except Exception as e:
        print(f"[notify] Linear error: {e}")
        return False


def notify_completion(project: str, task_id: str, description: str, branch: str, pr_url: str = None) -> None:
    notify_slack(task_id, description, branch, pr_url, project)
    notify_linear(task_id, description, pr_url)


def _get_channel(project: str) -> str:
    try:
        import yaml
        cfg = yaml.safe_load((Path(__file__).parent / "projects.yaml").read_text())
        return cfg["projects"].get(project, {}).get("slack_channel", "")
    except Exception:
        return os.getenv("SLACK_CHANNEL", "")
