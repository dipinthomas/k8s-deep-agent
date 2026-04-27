"""
Slack tools for the K8s agent.
Used to post investigation updates, approval requests, and resolution summaries.
"""

import os
import json
from langchain_core.tools import tool
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

_client = None


def _slack():
    global _client
    if _client is None:
        _client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    return _client


def _channel() -> str:
    return os.environ.get("SLACK_CHANNEL_ID", "#k8s-alerts")


@tool
def post_to_slack(message: str, thread_ts: str = "") -> str:
    """
    Post a message to the #k8s-alerts Slack channel.

    Args:
        message:   Message text (supports Slack mrkdwn formatting)
        thread_ts: Thread timestamp to reply in a thread (optional).
                   If empty, posts as a new top-level message.
    """
    try:
        kwargs = {
            "channel": _channel(),
            "text": message,
            "mrkdwn": True,
        }
        if thread_ts:
            kwargs["thread_ts"] = thread_ts

        response = _slack().chat_postMessage(**kwargs)
        return f"Posted to Slack (ts={response['ts']})"
    except SlackApiError as e:
        return f"Slack error: {e.response['error']}"


# Called by the agent BEFORE attempting a destructive tool call.
# Posts evidence and APPROVE/DENY buttons to Slack.
# The agent continues after this call — the actual pause happens when
# interrupt_on fires on the subsequent destructive tool call.
# See agent.py for the full human-in-the-loop sequence.
@tool
def post_approval_request(
    summary: str,
    evidence: str,
    action_list: str,
    impact: str,
    thread_ts: str = "",
) -> str:
    """
    Post a structured approval request to Slack with interactive buttons.
    Use this when you have gathered evidence and need human approval before
    taking any action that modifies cluster state.

    Args:
        summary:     1-2 sentence root cause summary
        evidence:    Key metrics and findings (mrkdwn)
        action_list: Ranked list of actions to take (lowest risk first)
        impact:      What will be affected by the actions
        thread_ts:   Thread to post in (optional)
    """
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "⚠️  Approval Required",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Root cause:*\n{summary}"},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Evidence:*\n{evidence}"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Recommended actions:*\n{action_list}",
            },
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Impact:*\n{impact}"},
        },
        {"type": "divider"},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ APPROVE", "emoji": True},
                    "style": "primary",
                    "action_id": "agent_approve",
                    "value": "approve",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🚫 DENY", "emoji": True},
                    "style": "danger",
                    "action_id": "agent_deny",
                    "value": "deny",
                },
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "🔍 MORE DETAILS",
                        "emoji": True,
                    },
                    "action_id": "agent_more_details",
                    "value": "details",
                },
            ],
        },
    ]

    try:
        kwargs = {
            "channel": _channel(),
            "text": f"⚠️ Approval required: {summary}",
            "blocks": blocks,
        }
        if thread_ts:
            kwargs["thread_ts"] = thread_ts

        response = _slack().chat_postMessage(**kwargs)
        return (
            f"Approval request posted (ts={response['ts']}). "
            "Waiting for human response via Slack button click."
        )
    except SlackApiError as e:
        return f"Slack error posting approval request: {e.response['error']}"
