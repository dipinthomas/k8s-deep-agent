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


@tool
def post_approval_request(
    summary: str,
    evidence: str,
    eviction_list: str,
    impact: str,
    thread_ts: str = "",
) -> str:
    """
    Post a structured approval request to Slack with interactive buttons.
    Use this when you are ready to recommend evictions and need human approval.

    Args:
        summary:      1-2 sentence root cause summary
        evidence:     Key CloudWatch metrics and findings (mrkdwn)
        eviction_list: Ranked list of pods to evict (lowest priority first)
        impact:       What will be affected by the evictions
        thread_ts:    Thread to post in (optional)
    """
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "⚠️  Approval Required — Pod Eviction",
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
                "text": f"*Recommended evictions (lowest priority first):*\n{eviction_list}",
            },
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Impact:*\n{impact}"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "✅ *checkoutservice, paymentservice, cartservice are NOT on this list.*",
            },
        },
        {"type": "divider"},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ APPROVE", "emoji": True},
                    "style": "primary",
                    "action_id": "approve_eviction",
                    "value": "approve",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🚫 DENY", "emoji": True},
                    "style": "danger",
                    "action_id": "deny_eviction",
                    "value": "deny",
                },
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "🔍 GIVE ME MORE DETAILS",
                        "emoji": True,
                    },
                    "action_id": "more_details",
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
