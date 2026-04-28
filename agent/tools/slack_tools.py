"""
Slack tools for the K8s agent.

Routing rules (enforced — the model cannot override):
- channel = config["configurable"]["channel_id"], falls back to SLACK_CHANNEL_ID env.
- thread_ts = config["configurable"]["thread_ts"]; ALWAYS used when present.
  The model MUST NOT supply thread_ts itself; we inject it from the run config
  set in main.py:agent_config(). This guarantees every investigation message
  lands in the alarm's thread, never the channel root.
"""

import os
import logging
from typing import Annotated
from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

logger = logging.getLogger(__name__)

_client = None


def _slack():
    global _client
    if _client is None:
        _client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    return _client


def _route(config: RunnableConfig | None) -> tuple[str, str]:
    """Resolve (channel, thread_ts) from the LangGraph run config. Falls back
    to env defaults so module-level use (e.g. tests) still works."""
    cfg = (config or {}).get("configurable", {}) if isinstance(config, dict) else {}
    channel = cfg.get("channel_id") or os.environ.get("SLACK_CHANNEL_ID", "#k8s-alerts")
    thread_ts = cfg.get("thread_ts") or ""
    return channel, thread_ts


def _post(label: str, channel: str, thread_ts: str, **kwargs) -> tuple[str | None, str | None]:
    """Post to Slack with consistent logging. Returns (ts_or_None, err_or_None)."""
    logger.info("%s posting: channel=%s thread_ts=%r", label, channel, thread_ts)
    if thread_ts:
        kwargs["thread_ts"] = thread_ts
    kwargs["channel"] = channel
    try:
        response = _slack().chat_postMessage(**kwargs)
        logger.info("%s succeeded: ts=%s", label, response["ts"])
        return response["ts"], None
    except SlackApiError as e:
        err = e.response["error"]
        logger.error("%s Slack API error: %s", label, err)
        return None, err


@tool
def post_to_slack(message: str, config: RunnableConfig) -> str:
    """
    Post a message to the active investigation Slack thread.

    The channel and thread are determined automatically from the run context —
    you do NOT need to (and cannot) supply them. Just call this with your
    message text.

    Args:
        message: Message text (supports Slack mrkdwn formatting).
    """
    channel, thread_ts = _route(config)
    ts, err = _post("post_to_slack", channel, thread_ts, text=message, mrkdwn=True)
    return f"Slack error: {err}" if err else f"Posted to Slack thread (ts={ts})"


# This tool ONLY posts the APPROVE/DENY buttons to Slack — it does NOT pause
# the graph. The pause is created by interrupt_on when a destructive kubectl
# tool is actually called. The agent MUST follow this tool, in the same turn,
# with the destructive tool call described in action_list. Otherwise the
# buttons are no-ops (nothing pending to resume).
@tool
def post_approval_request(
    summary: str,
    evidence: str,
    action_list: str,
    impact: str,
    config: RunnableConfig,
) -> str:
    """
    Post the APPROVE / DENY / MORE DETAILS buttons to the investigation thread.

    IMPORTANT — this tool does NOT pause the agent and does NOT wait for the
    human. It is just a Slack post. The actual human-in-the-loop pause is
    created by interrupt_on when you call the destructive kubectl tool.

    REQUIRED USAGE: in the SAME turn that you call this tool, you MUST also
    call the destructive kubectl tool you described in `action_list` (e.g.
    kubectl_delete with the exact pod/namespace from action_list). The graph
    will pause at THAT tool call until the human clicks a button. If you end
    your turn after calling post_approval_request without queuing the
    destructive tool, the APPROVE/DENY buttons will be no-ops.

    The channel and thread are determined automatically — do NOT supply them.

    Args:
        summary:     1-2 sentence root cause summary
        evidence:    Key metrics and findings (mrkdwn)
        action_list: Ranked list of actions to take (lowest risk first).
                     You MUST call the top action's kubectl tool next, in this
                     same turn.
        impact:      What will be affected by the actions
    """
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "⚠️  Approval Required", "emoji": True},
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Root cause:*\n{summary}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Evidence:*\n{evidence}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Recommended actions:*\n{action_list}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Impact:*\n{impact}"}},
        {"type": "divider"},
        {
            "type": "actions",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "✅ APPROVE", "emoji": True}, "style": "primary", "action_id": "agent_approve", "value": "approve"},
                {"type": "button", "text": {"type": "plain_text", "text": "🚫 DENY", "emoji": True}, "style": "danger", "action_id": "agent_deny", "value": "deny"},
                {"type": "button", "text": {"type": "plain_text", "text": "🔍 MORE DETAILS", "emoji": True}, "action_id": "agent_more_details", "value": "details"},
            ],
        },
    ]

    channel, thread_ts = _route(config)
    ts, err = _post(
        "post_approval_request", channel, thread_ts,
        text=f"⚠️ Approval required: {summary}",
        blocks=blocks,
    )
    if err:
        return f"Slack error posting approval request: {err}"
    return (
        f"Approval UI posted to thread (ts={ts}). This tool does NOT pause "
        "the graph. You MUST now, in this same turn, call the destructive "
        "kubectl tool from your action_list — interrupt_on will pause the "
        "graph at that call until the human clicks APPROVE or DENY. Do not "
        "end the turn here."
    )
