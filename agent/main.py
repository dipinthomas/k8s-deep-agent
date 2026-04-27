"""
Entry point for the K8s AI Agent demo.

Entry points:
  1. HTTP POST /trigger — called by Lambda when a CloudWatch alarm fires.
     Agent posts the opening Slack alert, investigates, then posts an
     approval request with APPROVE / DENY / ask-a-question in the thread.

  2. Slack thread replies — user can ask free-form questions in the thread
     ("what node type?", "how much memory does checkout have?").
     Agent answers and re-posts the approval block so the button stays live.

  3. Slack button actions — APPROVE / DENY / MORE DETAILS resume the workflow.
"""

import asyncio
import os
import time
import logging
import threading
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

from agent import build_agent

slack_app = App(token=os.environ["SLACK_BOT_TOKEN"])
agent = build_agent()

CHANNEL = os.environ["SLACK_CHANNEL_ID"]

# thread_ts → channel for every active investigation
# Populated when /trigger starts, removed when approved/denied
active_investigations: dict[str, str] = {}
_investigations_lock = threading.Lock()


# ── Helpers ────────────────────────────────────────────────────────────────────

def agent_config(thread_ts: str, channel: str) -> dict:
    return {
        "configurable": {
            "thread_id": thread_ts,
            "channel_id": channel,
            "thread_ts": thread_ts,
        }
    }


def stream_agent(messages: list, thread_ts: str, channel: str) -> None:
    """Run the agent and stream chunks. Uses astream() via asyncio.run() because
    MCP tools are async-only and cannot be invoked synchronously."""
    config = agent_config(thread_ts, channel)

    async def _run():
        async for chunk in agent.astream({"messages": messages}, config):
            logger.debug("Agent chunk: %s", chunk)

    asyncio.run(_run())


def post_approval_block(channel: str, thread_ts: str) -> None:
    """Re-post the approval block so the buttons stay live after Q&A."""
    slack_app.client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        text="Ready for your decision:",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "I've answered your question above. "
                        "You can ask more questions in this thread, "
                        "or make a decision now:"
                    ),
                },
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "✅ Approve"},
                        "style": "primary",
                        "action_id": "agent_approve",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "🚫 Deny"},
                        "style": "danger",
                        "action_id": "agent_deny",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "🔍 More Details"},
                        "action_id": "agent_more_details",
                    },
                ],
            },
        ],
    )


# ── HTTP server — receives CloudWatch alarm trigger from Lambda ────────────────

http_app = Flask(__name__)


def run_investigation(alarm: dict, channel: str, thread_ts: str) -> None:
    with _investigations_lock:
        active_investigations[thread_ts] = channel

    alarm_name = alarm.get("alarm_name", "unknown")
    reason = alarm.get("reason", "")
    node = alarm.get("node", "unknown")

    initial_message = (
        f"CloudWatch alarm fired:\n\n"
        f"Alarm: {alarm_name}\n"
        f"Node: {node}\n"
        f"Reason: {reason}\n\n"
        f"Slack thread: {thread_ts}\n"
        f"Channel: {channel}\n"
    )

    try:
        stream_agent(
            [{"role": "user", "content": initial_message}],
            thread_ts,
            channel,
        )
    except Exception as e:
        logger.exception("Investigation failed: %s", e)
        with _investigations_lock:
            active_investigations.pop(thread_ts, None)
        try:
            slack_app.client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=f":x: Investigation failed: `{type(e).__name__}: {e}`",
            )
        except Exception:
            pass


@http_app.route("/trigger", methods=["POST"])
def trigger():
    alarm = request.get_json(force=True) or {}
    state = alarm.get("state", "ALARM")

    if state != "ALARM":
        logger.info("Ignoring non-ALARM state: %s", state)
        return jsonify({"status": "ignored", "state": state}), 200

    logger.info("Alarm received via HTTP: %s", alarm)
    thread_ts = str(time.time())

    # Agent owns the opening Slack message
    slack_app.client.chat_postMessage(
        channel=CHANNEL,
        text=(
            f":red_circle: *ALERT: {alarm.get('alarm_name', 'CloudWatch alarm')} fired*\n"
            f"*Node:* `{alarm.get('node', 'unknown')}`\n"
            f"{alarm.get('reason', '')}\n\n"
            "_Starting investigation — reply in this thread to ask questions._"
        ),
    )

    threading.Thread(
        target=run_investigation,
        args=(alarm, CHANNEL, thread_ts),
        daemon=True,
    ).start()

    return jsonify({"status": "investigation started", "thread_ts": thread_ts}), 200


@http_app.route("/healthz", methods=["GET"])
def healthz():
    return jsonify({"status": "ok"}), 200


# ── Slack thread replies — conversational Q&A before approval ─────────────────

@slack_app.event("message")
def handle_thread_reply(event, say):
    """
    Handles free-form questions posted in an active investigation thread.
    The agent answers the question, then re-posts the approval buttons.
    """
    thread_ts = event.get("thread_ts")
    bot_id = event.get("bot_id")
    subtype = event.get("subtype")

    # Ignore: not a thread reply, bot messages, message edits/deletes
    if not thread_ts or bot_id or subtype:
        return

    with _investigations_lock:
        channel = active_investigations.get(thread_ts)

    if not channel:
        return  # not an active investigation thread

    question = event.get("text", "").strip()
    if not question:
        return

    user = event.get("user", "someone")
    logger.info("Thread question from %s: %s", user, question)

    def answer_and_repost():
        stream_agent(
            [{"role": "user", "content": f"@{user} asks: {question}"}],
            thread_ts,
            channel,
        )
        post_approval_block(channel, thread_ts)

    threading.Thread(target=answer_and_repost, daemon=True).start()


# ── Slack button handlers — resume paused LangGraph workflow ──────────────────

# Resumes the paused LangGraph graph after human approval.
# The graph was frozen at the interrupt_on gate in agent.py.
# Feeding a message into stream_agent with the same thread_id/config
# resumes execution from exactly where it paused.
@slack_app.action("agent_approve")
def handle_approve(ack, body, say):
    ack()
    thread_ts = body["container"].get("thread_ts") or body["container"]["message_ts"]
    channel = body["channel"]["id"]
    user = body["user"]["name"]

    with _investigations_lock:
        active_investigations.pop(thread_ts, None)

    logger.info("Action approved by %s", user)
    say(text=f"✅ Approved by @{user}. Proceeding...", thread_ts=thread_ts, channel=channel)

    threading.Thread(
        target=stream_agent,
        args=(
            [{"role": "user", "content": "APPROVED — proceed with the recommended actions."}],
            thread_ts,
            channel,
        ),
        daemon=True,
    ).start()


@slack_app.action("agent_deny")
def handle_deny(ack, body, say):
    ack()
    thread_ts = body["container"].get("thread_ts") or body["container"]["message_ts"]
    channel = body["channel"]["id"]
    user = body["user"]["name"]

    with _investigations_lock:
        active_investigations.pop(thread_ts, None)

    logger.info("Action denied by %s", user)
    say(text=f"🚫 Denied by @{user}. Standing down.", thread_ts=thread_ts, channel=channel)

    threading.Thread(
        target=stream_agent,
        args=(
            [{"role": "user", "content": "DENIED — do not proceed. Summarise findings and stand down."}],
            thread_ts,
            channel,
        ),
        daemon=True,
    ).start()


@slack_app.action("agent_more_details")
def handle_more_details(ack, body, say):
    ack()
    thread_ts = body["container"].get("thread_ts") or body["container"]["message_ts"]
    channel = body["channel"]["id"]

    threading.Thread(
        target=lambda: (
            stream_agent(
                [{"role": "user", "content": "Provide more detail about your findings."}],
                thread_ts,
                channel,
            ),
            post_approval_block(channel, thread_ts),
        ),
        daemon=True,
    ).start()


if __name__ == "__main__":
    # Flask HTTP server in background thread (receives Lambda triggers)
    threading.Thread(
        target=lambda: http_app.run(host="0.0.0.0", port=8080, use_reloader=False),
        daemon=True,
    ).start()
    logger.info("HTTP trigger endpoint listening on :8080")

    # Slack Socket Mode blocks the main thread
    logger.info("Starting Slack Socket Mode handler...")
    handler = SocketModeHandler(slack_app, os.environ["SLACK_APP_TOKEN"])
    handler.start()
