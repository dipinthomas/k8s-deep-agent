"""
Slack tools for the K8s agent.

Routing rules (enforced — the model cannot override):
- channel = config["configurable"]["channel_id"], falls back to SLACK_CHANNEL_ID env.
- thread_ts = config["configurable"]["thread_ts"]; ALWAYS used when present.
  The model MUST NOT supply thread_ts itself; we inject it from the run config
  set in main.py:agent_config(). This guarantees every investigation message
  lands in the alarm's thread, never the channel root.
"""

import hashlib
import os
import logging
from typing import Annotated
from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

logger = logging.getLogger(__name__)

_client = None

# Per-thread fingerprint of the last approval card we posted. If the model
# tries to post an identical (or near-identical) card again — which is the
# classic "gate never armed, model retries" failure mode — we refuse and
# return an instructive error instead of spamming Slack.
# Process-local is fine: the agent runs as a single long-lived pod and
# investigations are scoped to that process.
_LAST_APPROVAL_FINGERPRINT: dict[str, str] = {}


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


# This tool posts the APPROVE/DENY buttons to Slack. Call it TOGETHER with
# post_to_slack (findings) in one turn — no destructive tool in this turn.
# The middleware will then direct you to call the destructive tool ALONE in
# the very next turn, where interrupt_on will pause the graph for human review.
# Two-turn sequence:
#   Turn N:   post_to_slack + post_approval_request  (visible in Slack, no interrupt)
#   Turn N+1: kubectl_scale alone                    (HITL fires, graph pauses)
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

    IMPORTANT — TWO-TURN SEQUENCE REQUIRED:
    1. Call post_to_slack (findings) AND this tool in the SAME turn. Do NOT
       include the destructive kubectl tool in this turn.
    2. The middleware will respond "✅ Approval card posted." After that, call
       the destructive kubectl tool ALONE in the NEXT turn. The graph will
       pause at that tool call until the human clicks a button.

    Why two turns? LangGraph's interrupt_on fires before parallel tool siblings
    can execute, which would strand this Slack post. The two-turn pattern
    guarantees the approval card is visible before the graph pauses.

    The channel and thread are determined automatically — do NOT supply them.

    Args:
        summary:     1-2 sentence root cause summary (e.g. "inventory-sync-job Running — confirmed noisy neighbour")
        evidence:    Put ONLY "See investigation details above ↑" — do NOT repeat
                     metrics or kubectl output already posted via post_to_slack.
        action_list: The single kubectl command to execute (e.g. "kubectl scale deployment/inventory-sync-job -n shop-prod --replicas=0").
                     Call that kubectl tool ALONE in the NEXT turn (not this one).
        impact:      One line: expected outcome + recovery time (e.g. "Stops stress workload · P99 back below 100ms within 60s")
    """
    channel, thread_ts = _route(config)

    # Dedup guard: if we already posted an identical approval card to this
    # thread, refuse to post again. This prevents the "model loops on
    # post_approval_request without queuing the destructive tool" failure
    # from spamming Slack with duplicate cards.
    fingerprint = hashlib.sha256(
        "|".join([summary, evidence, action_list, impact]).encode("utf-8")
    ).hexdigest()
    thread_key = f"{channel}:{thread_ts}"
    if _LAST_APPROVAL_FINGERPRINT.get(thread_key) == fingerprint:
        logger.warning(
            "post_approval_request DEDUP: identical approval card already posted "
            "to thread %s — refusing to post again", thread_key,
        )
        return (
            "ERROR: An identical approval card has ALREADY been posted to this "
            "thread. Do NOT call post_approval_request again with the same "
            "content. Your next action must be ONE of: (a) call the destructive "
            "kubectl tool from action_list NOW so the human-in-the-loop gate "
            "arms and the existing APPROVE/DENY buttons can resume it; or "
            "(b) call mark_stand_down(reason=\"...\") if you cannot proceed. "
            "Do NOT re-post the approval card."
        )

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "⚠️  Approval Required", "emoji": True},
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Root cause:*\n{summary}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Action:*\n`{action_list}`"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Expected outcome:*\n{impact}"}},
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

    ts, err = _post(
        "post_approval_request", channel, thread_ts,
        text=f"⚠️ Approval required: {summary}",
        blocks=blocks,
    )
    if err:
        return f"Slack error posting approval request: {err}"
    _LAST_APPROVAL_FINGERPRINT[thread_key] = fingerprint
    return (
        f"Approval UI posted to thread (ts={ts}). This tool does NOT pause "
        "the graph. You MUST now, in this same turn, call the destructive "
        "kubectl tool from your action_list — interrupt_on will pause the "
        "graph at that call until the human clicks APPROVE or DENY. Do not "
        "end the turn here."
    )
