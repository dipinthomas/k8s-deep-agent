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

Architecture note — single persistent event loop:
  All async work (MCP tool calls, agent streaming) runs in _agent_loop,
  a dedicated background thread that never exits. MCP tool objects hold HTTP
  sessions bound to the loop they were created in; using asyncio.run() for each
  call creates a new loop and leaves those sessions dead. The fix is one loop,
  always alive, with all coroutines submitted via asyncio.run_coroutine_threadsafe().
"""

import asyncio
import json
import os
import time
import logging
import threading
from concurrent.futures import Future
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from langgraph.types import Command
import redis as redis_lib

load_dotenv()

# Structured DEBUG logging: each line is prefixed with a monotonic sequence number
# so log lines can be sorted/grepped by order even when timestamps collide.
# Use LOG_LEVEL=INFO to suppress debug output in production.
_LOG_LEVEL = os.environ.get("LOG_LEVEL", "DEBUG").upper()
_seq_lock = threading.Lock()
_seq = 0


def _next_seq() -> str:
    global _seq
    with _seq_lock:
        _seq += 1
        return f"{_seq:06d}"


class _SeqFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        record.seq = _next_seq()
        return super().format(record)


_handler = logging.StreamHandler()
_handler.setFormatter(
    _SeqFormatter(
        fmt="[%(seq)s] %(levelname)s %(name)s — %(message)s",
        datefmt=None,
    )
)
logging.basicConfig(level=_LOG_LEVEL, handlers=[_handler], force=True)
# Silence noisy libraries at WARNING unless we're explicitly debugging them.
for _noisy in ("httpx", "httpcore", "urllib3", "botocore", "boto3"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# ── Persistent event loop ──────────────────────────────────────────────────────
# One loop runs for the lifetime of the process in its own daemon thread.
# All coroutines (MCP tool loading, agent streaming) are submitted here.
# This ensures MCP HTTP sessions created during startup remain valid forever.

_agent_loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()


def _start_agent_loop():
    asyncio.set_event_loop(_agent_loop)
    _agent_loop.run_forever()


def run_async(coro) -> Future:
    """Submit a coroutine to the persistent agent loop and return a Future.
    Call .result() to block until done (raises on exception)."""
    return asyncio.run_coroutine_threadsafe(coro, _agent_loop)


from agent import build_agent_async

# These get assigned during bootstrap() inside __main__ so that importing this
# module (e.g. from a test or a tool) does not trigger network calls or crash
# on missing env vars.
slack_app: App | None = None
agent = None
CHANNEL: str = ""
_redis: redis_lib.Redis | None = None

INVESTIGATION_TTL = 4 * 3600  # 4 hours

_REDIS_INVESTIGATIONS_KEY = "k8s_agent:active_investigations"


def bootstrap() -> None:
    """Initialise the persistent loop, Slack app, agent, and Redis client.
    Called once from __main__ — never at import time."""
    global slack_app, agent, CHANNEL, _redis

    threading.Thread(target=_start_agent_loop, daemon=True, name="agent-loop").start()

    slack_app = App(token=os.environ["SLACK_BOT_TOKEN"])
    CHANNEL = os.environ["SLACK_CHANNEL_ID"]

    # Redis client — probe with retries so a slow Redis start doesn't crash the agent.
    _redis = redis_lib.from_url(
        os.environ.get("REDIS_URL", "redis://localhost:6379"), decode_responses=True
    )
    for _attempt in range(10):
        try:
            _redis.ping()
            logger.info("Redis ready (attempt %d)", _attempt + 1)
            break
        except Exception as _e:
            logger.warning("Redis not ready (attempt %d/10): %s — retrying in 3s", _attempt + 1, _e)
            time.sleep(3)
    else:
        logger.error("Redis unavailable after 10 attempts — investigation persistence disabled")

    logger.info("Bootstrapping agent inside persistent event loop...")
    agent = run_async(build_agent_async()).result()
    logger.info("Agent ready.")

    # Register Slack handlers on the now-initialised slack_app.
    _register_slack_handlers()


def _investigations_get(thread_ts: str) -> tuple[str, float] | None:
    try:
        raw = _redis.hget(_REDIS_INVESTIGATIONS_KEY, thread_ts)
    except Exception:
        return None
    if raw is None:
        return None
    data = json.loads(raw)
    return data["channel"], data["started"]


def _investigations_set(thread_ts: str, channel: str, started: float) -> None:
    try:
        _redis.hset(_REDIS_INVESTIGATIONS_KEY, thread_ts, json.dumps({"channel": channel, "started": started}))
    except Exception:
        logger.warning("Redis unavailable — could not persist investigation thread_ts=%s", thread_ts)


def _investigations_delete(thread_ts: str) -> None:
    try:
        _redis.hdel(_REDIS_INVESTIGATIONS_KEY, thread_ts)
    except Exception:
        logger.warning("Redis unavailable — could not delete investigation thread_ts=%s", thread_ts)


def _investigations_all() -> dict[str, tuple[str, float]]:
    try:
        raw = _redis.hgetall(_REDIS_INVESTIGATIONS_KEY)
    except Exception:
        logger.warning("Redis unavailable — returning empty investigations map")
        return {}
    result = {}
    for ts, val in raw.items():
        data = json.loads(val)
        result[ts] = (data["channel"], data["started"])
    return result


def _reap_stale_investigations():
    """Remove investigations open longer than INVESTIGATION_TTL from Redis."""
    while True:
        time.sleep(3600)
        cutoff = time.time() - INVESTIGATION_TTL
        for ts, (_, started) in list(_investigations_all().items()):
            if started < cutoff:
                logger.info("Reaping stale investigation: thread_ts=%s", ts)
                _investigations_delete(ts)


def _recover_paused_investigations():
    """
    On startup: find any investigations still in Redis (surviving a pod restart).
    Since the graph checkpoint is in-memory and lost on restart, notify the Slack
    thread that the agent restarted and the investigation needs to be re-triggered.
    """
    investigations = _investigations_all()
    if not investigations:
        return
    logger.info("Found %d investigation(s) in Redis after restart — notifying Slack", len(investigations))
    for thread_ts, (channel, started) in list(investigations.items()):
        age_hours = (time.time() - started) / 3600
        if age_hours > INVESTIGATION_TTL / 3600:
            logger.info("Dropping expired investigation thread_ts=%s (%.1fh old)", thread_ts, age_hours)
            _investigations_delete(thread_ts)
            continue
        logger.info("Notifying restart for thread_ts=%s", thread_ts)
        try:
            slack_app.client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=(
                    ":warning: Agent pod restarted and lost in-memory state. "
                    "Please re-trigger the investigation via a new alarm."
                ),
            )
        except Exception:
            logger.exception("Failed to notify restart for thread_ts=%s", thread_ts)
        _investigations_delete(thread_ts)


# ── Helpers ────────────────────────────────────────────────────────────────────

def agent_config(thread_ts: str, channel: str) -> dict:
    return {
        "configurable": {
            "thread_id": thread_ts,
            "channel_id": channel,
            "thread_ts": thread_ts,
        }
    }


# Hard ceiling for a single agent.astream() call. The underlying LLM client has
# its own per-HTTP-call timeout (LLM_TIMEOUT_SEC, see agent.py); this is the
# ceiling for an *entire turn* (model call + tool dispatch + sub-stream). Bigger
# than the HTTP timeout because a turn legitimately makes multiple model calls.
_STREAM_TIMEOUT_SEC = float(os.environ.get("STREAM_TIMEOUT_SEC", "600"))

# How often the watchdog thread reminds Slack the agent is still working.
_HEARTBEAT_INTERVAL_SEC = float(os.environ.get("HEARTBEAT_INTERVAL_SEC", "60"))


def _heartbeat_loop(
    thread_ts: str,
    channel: str,
    stop_event: threading.Event,
    paused_event: threading.Event,
    started_at: float,
) -> None:
    """Post a single 'still working' message every HEARTBEAT_INTERVAL_SEC until
    stop_event is set. Skipped while paused_event is set — the graph is waiting
    on a human at an interrupt, so 'still investigating' would be misleading.
    Failures here are non-fatal — heartbeats are best-effort."""
    while not stop_event.wait(_HEARTBEAT_INTERVAL_SEC):
        if paused_event.is_set():
            continue
        elapsed = int(time.time() - started_at)
        try:
            slack_app.client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=f":hourglass_flowing_sand: Still investigating… {elapsed}s elapsed.",
            )
        except Exception:
            logger.warning("Heartbeat post failed for thread_ts=%s", thread_ts, exc_info=False)


def stream_agent(payload, thread_ts: str, channel: str) -> None:
    """Submit the agent coroutine to the persistent loop and block until done.

    `payload` may be:
      - a list of message dicts → a new turn on this thread
      - a langgraph.types.Command → resumes a graph paused at an interrupt
      - None → re-enters the graph with no new input

    Hard-capped by _STREAM_TIMEOUT_SEC so a stalled LLM stream cannot pin the
    worker thread indefinitely. On timeout or any astream() exception, posts
    the failure to Slack and re-raises so callers can clean up state.
    """
    config = agent_config(thread_ts, channel)
    if payload is None:
        input_payload = None
    elif isinstance(payload, Command):
        input_payload = payload
    else:
        input_payload = {"messages": payload}

    # Track whether the model called post_to_slack / post_approval_request this
    # turn. If the turn ends with a plain-text AIMessage and neither was called,
    # the user would never see anything — we post the text ourselves as a
    # fallback so heartbeat-only stalls become impossible.
    posted_to_slack = {"value": False}
    paused_hb = threading.Event()

    async def _run():
        async def _drive():
            async for chunk in agent.astream(input_payload, config):
                logger.debug("Agent chunk: %s", chunk)
                _scan_chunk_for_slack_posts(chunk, posted_to_slack)
                if isinstance(chunk, dict) and "__interrupt__" in chunk:
                    paused_hb.set()
        await asyncio.wait_for(_drive(), timeout=_STREAM_TIMEOUT_SEC)

    started = time.time()
    stop_hb = threading.Event()
    hb_thread = threading.Thread(
        target=_heartbeat_loop,
        args=(thread_ts, channel, stop_hb, paused_hb, started),
        daemon=True,
    )
    hb_thread.start()
    try:
        run_async(_run()).result()
        if not posted_to_slack["value"]:
            _safety_net_post_final_message(thread_ts, channel)
    except asyncio.TimeoutError:
        elapsed = int(time.time() - started)
        logger.error("stream_agent timed out after %ds for thread_ts=%s", elapsed, thread_ts)
        _post_thread_error(
            channel, thread_ts, f"Agent step timed out after {elapsed}s",
            RuntimeError(f"No response within {int(_STREAM_TIMEOUT_SEC)}s"),
        )
        raise
    except Exception as e:
        logger.exception("stream_agent failed for thread_ts=%s", thread_ts)
        _post_thread_error(channel, thread_ts, "Agent step", e)
        raise
    finally:
        stop_hb.set()


_SLACK_TOOL_NAMES = {"post_to_slack", "post_approval_request"}


def _scan_chunk_for_slack_posts(chunk, flag: dict) -> None:
    """Set flag['value']=True if any AIMessage tool_call in this chunk targets
    post_to_slack or post_approval_request. We intentionally over-detect: any
    occurrence in any node is enough."""
    if flag["value"]:
        return
    try:
        for node_state in (chunk.values() if isinstance(chunk, dict) else ()):
            if not isinstance(node_state, dict):
                continue
            for msg in node_state.get("messages", []) or []:
                tool_calls = getattr(msg, "tool_calls", None) or []
                for tc in tool_calls:
                    name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
                    if name in _SLACK_TOOL_NAMES:
                        flag["value"] = True
                        return
    except Exception:
        # Detection is best-effort — never break the stream over it.
        pass


def _safety_net_post_final_message(thread_ts: str, channel: str) -> None:
    """When a turn ends without calling post_to_slack, fetch the final state's
    last AIMessage and post its text content (if any) so the user sees SOMETHING.
    This preserves the demo invariant: every alarm produces a visible response."""
    config = agent_config(thread_ts, channel)

    async def _get_state():
        return await agent.aget_state(config)

    try:
        state = run_async(_get_state()).result()
    except Exception:
        logger.exception("Safety-net: could not read graph state for thread_ts=%s", thread_ts)
        return

    values = getattr(state, "values", None) or {}
    messages = values.get("messages") if isinstance(values, dict) else None
    if not messages:
        return

    last = messages[-1]
    # Only fire on a final AIMessage that is plain text and has no tool_calls
    # left to run — otherwise the graph isn't really "done" and a manual post
    # would interleave badly with the next chunk.
    is_ai = type(last).__name__ in ("AIMessage", "AIMessageChunk")
    has_tool_calls = bool(getattr(last, "tool_calls", None))
    text = ""
    raw = getattr(last, "content", "")
    if isinstance(raw, str):
        text = raw.strip()
    elif isinstance(raw, list):
        text = "\n".join(p.get("text", "") for p in raw if isinstance(p, dict)).strip()

    if not (is_ai and not has_tool_calls and text):
        return

    logger.warning(
        "Safety-net: model ended turn with plain text and no post_to_slack call — "
        "posting final AIMessage to thread_ts=%s ourselves", thread_ts,
    )
    try:
        slack_app.client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f":robot_face: _(auto-delivered final response)_\n\n{text}",
        )
    except Exception:
        logger.exception("Safety-net: failed to post final message")


def _pending_interrupt_action_count(thread_ts: str, channel: str) -> int:
    """Return the number of action_requests inside the pending HITL interrupt.

    LangChain's HumanInTheLoopMiddleware bundles ALL gated tool calls from a
    single AIMessage into ONE interrupt() call whose payload looks like:

        {"action_requests": [...], "review_configs": [...]}

    The middleware then validates that the resume payload's `decisions` list has
    exactly len(action_requests) entries — mismatch raises ValueError, and the
    graph silently exits the resume tick (which is the v26 bug we hit).

    So the correct decision count is len(action_requests) per pending interrupt,
    summed across pending interrupts. Returns 0 if state lookup fails or no
    interrupt is pending — the resume in that case will likely no-op cleanly,
    which is also fine.
    """
    config = agent_config(thread_ts, channel)

    async def _get_state():
        return await agent.aget_state(config)

    try:
        state = run_async(_get_state()).result()
    except Exception:
        logger.exception("Failed to read graph state for thread_ts=%s", thread_ts)
        return 0

    interrupts = []
    tasks = getattr(state, "tasks", None) or ()
    for t in tasks:
        interrupts.extend(getattr(t, "interrupts", ()) or ())
    if not interrupts:
        interrupts = list(getattr(state, "interrupts", ()) or ())

    if not interrupts:
        # Nothing pending — log the last AIMessage's gated tool calls so we can
        # see whether the model proposed a tool but the interrupt didn't persist.
        _log_last_ai_tool_calls(state, "no pending interrupt found")
        return 0

    total = 0
    for i, intr in enumerate(interrupts):
        value = getattr(intr, "value", None)
        action_requests = []
        if isinstance(value, dict):
            action_requests = value.get("action_requests") or []
        n = len(action_requests)
        total += n
        try:
            names = [a.get("name") for a in action_requests if isinstance(a, dict)]
        except Exception:
            names = []
        logger.info(
            "Pending interrupt #%d for thread_ts=%s: %d action(s) %s",
            i, thread_ts, n, names,
        )
    return total


def _log_last_ai_tool_calls(state, why: str) -> None:
    try:
        values = getattr(state, "values", None) or {}
        messages = values.get("messages") if isinstance(values, dict) else None
        if not messages:
            logger.warning("%s; state has no messages", why)
            return
        last_ai = next(
            (m for m in reversed(messages) if type(m).__name__ in ("AIMessage", "AIMessageChunk")),
            None,
        )
        if last_ai is None:
            logger.warning("%s; no AIMessage in state", why)
            return
        tcs = getattr(last_ai, "tool_calls", None) or []
        names = [tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None) for tc in tcs]
        logger.warning("%s; last AIMessage tool_calls=%s", why, names)
    except Exception:
        logger.exception("Failed to introspect last AIMessage")


def _resume_command(
    decision_type: str,
    thread_ts: str,
    channel: str,
    respond_message: str | None = None,
) -> Command:
    """Build a Command(resume={"decisions": [...]}) with EXACTLY as many
    decisions as the pending interrupt requested action_requests for. The
    HumanInTheLoopMiddleware ValueError-rejects any mismatch, so getting this
    count right is non-negotiable.

    decision_type: "approve" | "reject" | "respond". For "respond" the gate
    stays armed (the queued destructive tool is NOT consumed); the message is
    delivered to the model as the human's reply so it can answer Q&A and then
    we re-post the buttons to let the user decide for real."""
    n = _pending_interrupt_action_count(thread_ts, channel)
    if n == 0:
        # No pending interrupt — sending decisions=[] is the cleanest no-op
        # (LangGraph treats an empty-decisions resume as continue-from-checkpoint).
        logger.warning(
            "Resuming thread_ts=%s with %s but found NO pending interrupts — "
            "this resume will likely be a no-op; the gated tool will not execute.",
            thread_ts, decision_type,
        )
        return Command(resume={"decisions": []})

    if decision_type == "respond":
        decisions = [{"type": "respond", "args": respond_message or ""} for _ in range(n)]
    else:
        decisions = [{"type": decision_type} for _ in range(n)]
    logger.info("Resuming thread_ts=%s with %d %s decision(s)", thread_ts, n, decision_type)
    return Command(resume={"decisions": decisions})


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
    _investigations_set(thread_ts, channel, time.time())

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
    except Exception:
        # stream_agent already posted the failure to the thread.
        logger.exception("Investigation failed for thread_ts=%s", thread_ts)
        _investigations_delete(thread_ts)


@http_app.route("/trigger", methods=["POST"])
def trigger():
    alarm = request.get_json(force=True) or {}
    state = alarm.get("state", "ALARM")

    if state != "ALARM":
        logger.info("Ignoring non-ALARM state: %s", state)
        return jsonify({"status": "ignored", "state": state}), 200

    logger.info("Alarm received via HTTP: %s", alarm)

    # Post the opening alert and capture the real Slack thread timestamp.
    # All subsequent agent messages are posted as replies to this thread.
    opening = slack_app.client.chat_postMessage(
        channel=CHANNEL,
        text=(
            f":red_circle: *ALERT: {alarm.get('alarm_name', 'CloudWatch alarm')} fired*\n"
            f"*Node:* `{alarm.get('node', 'unknown')}`\n"
            f"{alarm.get('reason', '')}\n\n"
            "_Starting investigation — reply in this thread to ask questions._"
        ),
    )
    thread_ts = opening["ts"]
    logger.info("Slack thread opened: %s", thread_ts)

    threading.Thread(
        target=run_investigation,
        args=(alarm, CHANNEL, thread_ts),
        daemon=True,
    ).start()

    return jsonify({"status": "investigation started", "thread_ts": thread_ts}), 200


@http_app.route("/healthz", methods=["GET"])
def healthz():
    return jsonify({"status": "ok"}), 200


# ── Slack handler registration ────────────────────────────────────────────────
# Handlers are registered inside a function (not at import time) because
# slack_app is constructed in bootstrap(), not at module load.

def _post_thread_error(channel: str, thread_ts: str, where: str, exc: Exception) -> None:
    try:
        slack_app.client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f":x: {where} failed: `{type(exc).__name__}: {exc}`",
        )
    except Exception:
        logger.exception("Failed to post error to Slack thread")


def _register_slack_handlers() -> None:
    @slack_app.event("message")
    def handle_thread_reply(event, say):
        """Free-form questions in an active investigation thread.
        The agent answers, then re-posts the approval buttons."""
        thread_ts = event.get("thread_ts")
        bot_id = event.get("bot_id")
        subtype = event.get("subtype")

        # Ignore: not a thread reply, bot messages, message edits/deletes
        if not thread_ts or bot_id or subtype:
            return

        entry = _investigations_get(thread_ts)
        if not entry:
            return
        channel, _ = entry

        question = event.get("text", "").strip()
        if not question:
            return

        user = event.get("user", "someone")
        logger.info("Thread question from %s: %s", user, question)

        def answer_and_repost():
            try:
                stream_agent(
                    [{"role": "user", "content": f"@{user} asks: {question}"}],
                    thread_ts,
                    channel,
                )
                post_approval_block(channel, thread_ts)
            except Exception:
                # stream_agent already posted the failure to the thread.
                logger.exception("Thread reply handling failed")

        threading.Thread(target=answer_and_repost, daemon=True).start()

    # APPROVE → resume the paused interrupt with an "approve" decision per pending
    # tool call. This lets the queued kubectl_delete (or whichever destructive tool
    # was gated) execute with its original args. We must NOT inject a new
    # HumanMessage — that creates a new turn on the same thread, the model
    # re-investigates from scratch, re-proposes the tool, and the gate fires again
    # in a loop. See bug report 2026-04-28.
    @slack_app.action("agent_approve")
    def handle_approve(ack, body, say):
        ack()
        thread_ts = body["container"].get("thread_ts") or body["container"]["message_ts"]
        channel = body["channel"]["id"]
        user = body["user"]["name"]

        logger.info("Action approved by %s", user)
        say(text=f"✅ Approved by @{user}. Proceeding...", thread_ts=thread_ts, channel=channel)

        def _resume():
            try:
                stream_agent(
                    _resume_command("approve", thread_ts, channel),
                    thread_ts,
                    channel,
                )
            except Exception:
                logger.exception("Approve resume failed")
            finally:
                _investigations_delete(thread_ts)

        threading.Thread(target=_resume, daemon=True).start()

    # DENY → resume with "reject" so the gated tool call is skipped entirely.
    # The agent receives the rejection in its tool result and continues (typically
    # by summarising and standing down per the system prompt).
    @slack_app.action("agent_deny")
    def handle_deny(ack, body, say):
        ack()
        thread_ts = body["container"].get("thread_ts") or body["container"]["message_ts"]
        channel = body["channel"]["id"]
        user = body["user"]["name"]

        logger.info("Action denied by %s", user)
        say(text=f"🚫 Denied by @{user}. Standing down.", thread_ts=thread_ts, channel=channel)

        def _resume():
            try:
                stream_agent(
                    _resume_command("reject", thread_ts, channel),
                    thread_ts,
                    channel,
                )
            except Exception:
                logger.exception("Deny resume failed")
            finally:
                _investigations_delete(thread_ts)

        threading.Thread(target=_resume, daemon=True).start()

    # MORE_DETAILS → resume the interrupt with decision="respond". This is the
    # LangGraph HITL pattern for asking a follow-up: the queued destructive tool
    # is NOT consumed (gate stays effectively pending — see below), and the
    # supplied message is delivered to the model as the human's reply. The model
    # explains, then is expected to re-propose the action, which re-arms the
    # gate. We re-post the approval block so a fresh APPROVE/DENY pair is
    # visible to the user.
    #
    # Critical distinction from the previous (buggy) implementation:
    # we do NOT use decision="reject" here. "reject" tells the model the human
    # denied the action — the system prompt then mandates mark_stand_down, which
    # terminated the graph and made the subsequent APPROVE click a no-op.
    @slack_app.action("agent_more_details")
    def handle_more_details(ack, body, say):
        ack()
        thread_ts = body["container"].get("thread_ts") or body["container"]["message_ts"]
        channel = body["channel"]["id"]

        say(text="🔍 Pulling more detail…", thread_ts=thread_ts, channel=channel)

        def _details():
            try:
                if _pending_interrupt_action_count(thread_ts, channel) > 0:
                    stream_agent(
                        _resume_command(
                            "respond",
                            thread_ts,
                            channel,
                            respond_message=(
                                "The reviewer clicked MORE DETAILS. Provide a "
                                "deeper explanation of the evidence and the "
                                "recommendation. Do NOT call mark_stand_down — "
                                "the human has not denied the action. After "
                                "explaining, re-propose the same destructive "
                                "tool call so the gate re-arms for approval."
                            ),
                        ),
                        thread_ts,
                        channel,
                    )
                else:
                    # No pending interrupt — graph already exited. Run a fresh
                    # turn asking for explanation; the model can re-propose if
                    # it still considers the action warranted.
                    stream_agent(
                        [{"role": "user", "content":
                            "Provide more detail about your findings — do not "
                            "execute any tool yet, just explain. After explaining, "
                            "re-propose the recommended action so I can decide."}],
                        thread_ts,
                        channel,
                    )
                post_approval_block(channel, thread_ts)
            except Exception:
                # stream_agent already posted the failure to the thread.
                logger.exception("More-details handling failed")

        threading.Thread(target=_details, daemon=True).start()


def _serve_http() -> None:
    """Serve the Flask trigger endpoint with a multi-threaded WSGI server so
    /healthz never blocks behind a /trigger in flight."""
    from waitress import serve
    serve(http_app, host="0.0.0.0", port=8080, threads=8)


if __name__ == "__main__":
    bootstrap()
    _recover_paused_investigations()
    threading.Thread(target=_reap_stale_investigations, daemon=True).start()

    # waitress in background thread (receives Lambda triggers)
    threading.Thread(target=_serve_http, daemon=True).start()
    logger.info("HTTP trigger endpoint listening on :8080 (waitress, 8 threads)")

    # Slack Socket Mode blocks the main thread
    logger.info("Starting Slack Socket Mode handler...")
    handler = SocketModeHandler(slack_app, os.environ["SLACK_APP_TOKEN"])
    handler.start()
