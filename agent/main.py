"""
Entry point for the K8s AI Agent demo.
Listens for Slack events (CloudWatch alarm notifications) and kicks off
the investigation workflow.
"""

import os
import json
import logging
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

from agent import build_agent

slack_app = App(token=os.environ["SLACK_BOT_TOKEN"])
agent = build_agent()


@slack_app.event("message")
def handle_message(event, say):
    """
    Receives CloudWatch alarm messages forwarded to Slack by SNS.
    Only triggers the agent on alarm notifications containing the cluster name.
    """
    text = event.get("text", "")
    if "NodeDiskPressure" not in text and "DiskPressure" not in text:
        return

    channel = event.get("channel")
    thread_ts = event.get("ts")
    logger.info("Disk pressure alarm received — starting agent investigation")

    config = {
        "configurable": {
            "thread_id": thread_ts,
            "channel_id": channel,
            "thread_ts": thread_ts,
        }
    }

    initial_message = (
        f"A CloudWatch alarm has fired in #k8s-alerts:\n\n{text}\n\n"
        "Read AGENTS.md to understand the cluster. "
        "Investigate this incident using your subagents. "
        "Post all findings to Slack as you go. "
        "Before taking any action that affects running workloads, "
        "post an approval request and wait for human confirmation."
    )

    # Stream the agent — LangGraph will pause at interrupt() calls
    for chunk in agent.stream({"messages": [{"role": "user", "content": initial_message}]}, config):
        logger.debug("Agent chunk: %s", chunk)


@slack_app.action("approve_eviction")
def handle_approve(ack, body, say):
    """Handles the APPROVE button click from the Slack approval message."""
    ack()
    thread_ts = body["container"].get("thread_ts") or body["container"]["message_ts"]
    channel = body["channel"]["id"]
    user = body["user"]["name"]

    logger.info("Eviction approved by %s", user)
    say(
        text=f"✅ Approved by @{user}. Proceeding with evictions...",
        thread_ts=thread_ts,
        channel=channel,
    )

    config = {
        "configurable": {
            "thread_id": thread_ts,
            "channel_id": channel,
            "thread_ts": thread_ts,
        }
    }

    # Resume the paused LangGraph workflow
    for chunk in agent.stream(
        {"messages": [{"role": "user", "content": "APPROVED — proceed with the evictions."}]},
        config,
    ):
        logger.debug("Agent resume chunk: %s", chunk)


@slack_app.action("deny_eviction")
def handle_deny(ack, body, say):
    """Handles the DENY button click from the Slack approval message."""
    ack()
    thread_ts = body["container"].get("thread_ts") or body["container"]["message_ts"]
    channel = body["channel"]["id"]
    user = body["user"]["name"]

    logger.info("Eviction denied by %s", user)
    say(
        text=f"🚫 Denied by @{user}. Standing down — no evictions will be performed.",
        thread_ts=thread_ts,
        channel=channel,
    )

    config = {
        "configurable": {
            "thread_id": thread_ts,
            "channel_id": channel,
            "thread_ts": thread_ts,
        }
    }

    for chunk in agent.stream(
        {"messages": [{"role": "user", "content": "DENIED — do not evict any pods. Summarise findings and stand down."}]},
        config,
    ):
        logger.debug("Agent resume chunk: %s", chunk)


@slack_app.action("more_details")
def handle_more_details(ack, body, say):
    """Handles the GIVE ME MORE DETAILS button."""
    ack()
    thread_ts = body["container"].get("thread_ts") or body["container"]["message_ts"]
    channel = body["channel"]["id"]

    config = {
        "configurable": {
            "thread_id": thread_ts,
            "channel_id": channel,
            "thread_ts": thread_ts,
        }
    }

    for chunk in agent.stream(
        {"messages": [{"role": "user", "content": "Provide more detail about your findings before I decide."}]},
        config,
    ):
        logger.debug("Agent resume chunk: %s", chunk)


if __name__ == "__main__":
    logger.info("Starting K8s AI Agent — listening for Slack events...")
    handler = SocketModeHandler(slack_app, os.environ["SLACK_APP_TOKEN"])
    handler.start()
