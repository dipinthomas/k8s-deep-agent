"""
Deep Agent setup for the K8s incident investigation agent.

kubectl and CloudWatch tools come from MCP servers (mcp_client.py).
Slack tools are Python-native to support the custom approval Block Kit UI.

build_agent_async() MUST be awaited from within the persistent event loop
(main.py's _agent_loop). This ensures MCP tool HTTP sessions are bound to
that loop and remain valid for every subsequent agent.astream() call.
"""

import logging
import os
from functools import wraps
from deepagents import create_deep_agent
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver

from subagents import build_subagents
from tools.slack_tools import post_to_slack, post_approval_request
from memory.store import build_memory_store_async, seed_memory_store
from mcp_servers.mcp_client import get_mcp_tools_async
from middleware import KeepLoopingMiddleware
from optimization import (
    TokenUsageLoggingMiddleware,
    truncate_tool_output,
)


@tool
def mark_stand_down(reason: str) -> str:
    """Declare that the investigation is genuinely complete or that no
    further action is possible.

    Call this ONLY when one of the following is true:
      - The incident is fully resolved and you have already posted a
        resolution summary to Slack.
      - The user denied your recommended action and you have nothing
        new to propose without fresh evidence.
      - You have established that no remediation is appropriate (e.g.
        the alarm was a false positive).

    After this is called, the agent loop will exit on the next turn.
    Never call this in lieu of investigating — it ends the loop.

    Args:
        reason: One short sentence on why you are standing down. This is
            recorded in the agent's state for later debugging.
    """
    return f"Stand-down recorded: {reason}"


async def _build_checkpointer():
    """
    Build a persistent Redis-backed checkpointer so paused interrupts survive
    a pod restart. Falls back to in-memory if Redis is unavailable — the agent
    still runs, but a restart will lose any in-flight investigation state.
    """
    redis_url = os.environ.get("REDIS_URL")
    if not redis_url:
        logger.warning("REDIS_URL not set — using in-memory checkpointer (no restart survival)")
        return MemorySaver()
    try:
        from langgraph.checkpoint.redis.aio import AsyncRedisSaver
        # from_conn_string may return either an async context manager or the
        # checkpointer directly, depending on the package version. Handle both.
        result = AsyncRedisSaver.from_conn_string(redis_url)
        if hasattr(result, "__aenter__"):
            checkpointer = await result.__aenter__()
        else:
            checkpointer = result
        await checkpointer.asetup()
        logger.info("Using AsyncRedisSaver checkpointer at %s", redis_url)
        return checkpointer
    except Exception as e:
        logger.exception("Failed to build Redis checkpointer (%s) — falling back to in-memory", e)
        return MemorySaver()

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are an autonomous Kubernetes operations agent.

CONTEXT
AGENTS.md and the cluster SKILL.md are already in your memory — refer to
them directly, do not read_file them. The skills/ directory IS readable for
additional playbooks.

WORKFLOW
1. write_todos to decompose the incident.
2. Discover available tools (do not assume tool names).
3. Run `kubectl get namespaces` before any namespaced query — never
   hardcode a namespace.
4. Investigate; replan when evidence contradicts the hypothesis.
5. Drive to one of three terminal states (see TERMINATION).

APPROVAL GATE — the only way to mutate cluster state
The reviewer sees only what you post to Slack. The LangGraph interrupt
gate is invisible. To take a destructive action you MUST emit, in a
SINGLE turn, all three of:
  (a) post_to_slack — root cause + evidence + recommended action.
  (b) post_approval_request — approve/deny UI.
  (c) the destructive tool call itself with final args.
post_approval_request alone does NOT pause the graph; only the destructive
tool call arms the gate. If you skip (a) or (b), middleware strips (c)
and you waste a turn.

AFTER THE GATE
- APPROVE → the gated tool already ran and its result is in your context.
  On success: post_to_slack with the outcome, then mark_stand_down.
  On error ("Tool error: ..."): RE-PLAN. Pick a different tool or retry
  with corrective flags (e.g. `--force --delete-emptydir-data
  --ignore-daemonsets` for drain), then re-run the gate (a)+(b)+(c) in
  one turn. Do not summarise-and-stop on tool error.
- DENY → post acknowledgment, then mark_stand_down. Do not propose a
  new action without new evidence.

REMEDIATION PREFERENCE
For pod-level pressure relief, prefer `kubectl_delete pod <name> -n <ns>`
per non-critical pod over node-wide drain. Real clusters have bare pods,
DaemonSets, and emptyDir volumes that make drain fail. See
skills/universal/node-disk-pressure/SKILL.md.

TERMINATION — exactly one of:
  1. Destructive kubectl tool call (HITL pauses the graph).
  2. post_to_slack containing "no action required" / "standing down" /
     "investigation complete", then mark_stand_down next turn.
  3. mark_stand_down directly.
A turn ending with empty tool_calls and no stand-down phrase is rejected.
Plain-text turns are invisible to the user — never end on one.

NON-NEGOTIABLE
- Evidence + approval UI BEFORE the destructive tool.
- Destructive tool in the SAME turn as post_approval_request.
- Re-plan on tool error, never summarise-and-stop.
- Write the outcome to long-term memory after resolution.
- If unsure, ask.
"""

# Keywords in a tool NAME that signal it is destructive
_DESTRUCTIVE_NAME_KEYWORDS = {
    "delete", "drain", "evict", "apply", "patch",
    "scale", "restart", "create", "update", "replace", "rollout",
}

# Keywords in a tool DESCRIPTION that signal it is destructive
_DESTRUCTIVE_DESC_KEYWORDS = {
    "modif", "creat", "remov", "destroy", "delet",
    "evict", "drain", "apply", "patch", "restart", "scale", "rollout",
}

# Tools that match the keywords above but are read-only — must never be gated.
# "reconnect" fires in tool names when the MCP client reconnects its session;
# "describe_log_groups" matches "creat" in its description but only lists log groups.
_INTERRUPT_EXCLUSIONS = {"kubectl_reconnect", "describe_log_groups"}


def _build_interrupt_on(mcp_tools: list) -> dict:
    """
    Derive the interrupt_on map dynamically from the MCP tool list.
    Any tool whose name or description contains a destructive keyword
    requires human approval before execution.
    This means new destructive tools added in future MCP server versions
    are caught automatically without changing this code.
    """
    interrupt_on = {}
    for tool in mcp_tools:
        if tool.name in _INTERRUPT_EXCLUSIONS:
            continue

        name_tokens = set(tool.name.lower().split("_"))
        desc_lower = (tool.description or "").lower()

        name_match = bool(name_tokens & _DESTRUCTIVE_NAME_KEYWORDS)
        desc_match = any(kw in desc_lower for kw in _DESTRUCTIVE_DESC_KEYWORDS)

        if name_match or desc_match:
            interrupt_on[tool.name] = True

    return interrupt_on


_TUPLE_RESPONSE_FORMAT = "content_and_artifact"


def _wrap_with_error_handling(tool):
    """
    Wrap an MCP tool so that exceptions are returned as error strings rather than
    crashing the LangGraph graph. This makes MCP errors recoverable — the agent
    sees the error message and can retry with a different tool call.

    Also truncates oversized successful outputs at the wrapper layer so a
    single `kubectl get pods -A -o wide` (often 30+ KB) does not dominate
    every subsequent turn's input tokens. Truncation preserves head + tail
    so the model still sees structural cues (column headers, summary rows).

    MCP tools from langchain_mcp_adapters use response_format='content_and_artifact',
    which requires a (str, Any) tuple return. Plain strings crash the tool node.
    """
    needs_tuple = getattr(tool, "response_format", None) == _TUPLE_RESPONSE_FORMAT

    def _error_response(e: Exception):
        msg = f"Tool error: {type(e).__name__}: {e}"
        return (msg, None) if needs_tuple else msg

    def _truncate_result(result):
        """Truncate the text content of a tool result without altering its
        shape (string, tuple, or list-of-content-blocks)."""
        if isinstance(result, str):
            return truncate_tool_output(result)
        if isinstance(result, tuple) and len(result) == 2:
            text, artifact = result
            return (truncate_tool_output(text) if isinstance(text, str) else text, artifact)
        return result

    original_coroutine = tool.coroutine
    original_func = tool.func

    if original_coroutine:
        @wraps(original_coroutine)
        async def safe_coroutine(*args, **kwargs):
            try:
                return _truncate_result(await original_coroutine(*args, **kwargs))
            except Exception as e:
                return _error_response(e)
        tool.coroutine = safe_coroutine
    elif original_func:
        @wraps(original_func)
        def safe_func(*args, **kwargs):
            try:
                return _truncate_result(original_func(*args, **kwargs))
            except Exception as e:
                return _error_response(e)
        tool.func = safe_func

    return tool


CLUSTER_SKILL_PATH = os.environ.get("CLUSTER_SKILL_PATH", "")

# Per-call hard cap on LLM HTTP requests. Without this the OpenAI / Anthropic
# SDKs default to a multi-minute timeout, so a stalled stream pins the worker
# thread for ~10 minutes before any retry kicks in. 90s is generous for a single
# investigation step; bump via LLM_TIMEOUT_SEC if a step legitimately needs more.
_LLM_TIMEOUT_SEC = float(os.environ.get("LLM_TIMEOUT_SEC", "90"))
_LLM_MAX_RETRIES = int(os.environ.get("LLM_MAX_RETRIES", "3"))


def _build_model_with_timeout(model_spec: str):
    """Construct the chat model with explicit timeout + retry. Accepts the same
    `provider:model` shorthand deepagents normally accepts as a bare string."""
    if ":" not in model_spec:
        # Unknown shape — let deepagents handle it; we lose the timeout but the
        # agent still runs. Log loudly so it's discoverable.
        logger.warning(
            "AGENT_MODEL=%r has no provider prefix — passing through without "
            "timeout/retry config. Use 'openai:gpt-5-mini' or 'anthropic:claude-...'.",
            model_spec,
        )
        return model_spec

    provider, model_name = model_spec.split(":", 1)
    if provider == "openai":
        from langchain_openai import ChatOpenAI
        # use_responses_api=True routes to POST /responses (the modern endpoint)
        # instead of /chat/completions. /responses returns reasoning blocks
        # attached to each AIMessage, which the graph then carries forward as
        # message content — keeping the prefix cache hot and avoiding the
        # "regenerate reasoning from scratch every turn" cost we hit on
        # /chat/completions. NOT setting use_previous_response_id (which would
        # also drop history from the payload) — that interacts badly with
        # langgraph interrupt resume.
        # parallel_tool_calls=True lets the model emit post_approval_request
        # AND the destructive kubectl tool in the same turn, which is required
        # for the human-in-the-loop gate to arm. Without this, gpt-5-mini
        # consistently emits only one tool per turn.
        return ChatOpenAI(
            model=model_name,
            use_responses_api=True,
            timeout=_LLM_TIMEOUT_SEC,
            max_retries=_LLM_MAX_RETRIES,
            model_kwargs={"parallel_tool_calls": True},
        )
    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        # Claude defaults to parallel tool use enabled (disable_parallel_tool_use
        # defaults to False), which is what we need for post_approval_request +
        # destructive tool to land in the same turn and arm the HITL gate.
        # max_tokens is required by the Anthropic API; size it for the longest
        # reasoning chunks the agent emits during multi-step investigation.
        return ChatAnthropic(
            model=model_name,
            timeout=_LLM_TIMEOUT_SEC,
            max_retries=_LLM_MAX_RETRIES,
            max_tokens=int(os.environ.get("ANTHROPIC_MAX_TOKENS", "2048")),
        )
    logger.warning(
        "AGENT_MODEL provider %r not in [openai, anthropic] — passing through "
        "as bare string (no timeout/retry).", provider,
    )
    return model_spec


async def build_agent_async():
    """
    Build and return the agent. Must be awaited from within the persistent
    event loop so that MCP tool HTTP sessions are bound to that loop.
    """
    checkpointer = await _build_checkpointer()
    store = await build_memory_store_async()
    seed_memory_store(store)

    # Load MCP tools inside the persistent loop — sessions remain valid.
    raw_tools = await get_mcp_tools_async()
    mcp_tools = [_wrap_with_error_handling(t) for t in raw_tools]

    interrupt_on = _build_interrupt_on(mcp_tools)
    logger.info(
        "interrupt_on derived from MCP tools (%d tools gated): %s",
        len(interrupt_on),
        list(interrupt_on.keys()),
    )

    if CLUSTER_SKILL_PATH:
        logger.info("Cluster skill loaded: %s", CLUSTER_SKILL_PATH)
    else:
        logger.warning(
            "CLUSTER_SKILL_PATH not set — agent has no cluster context. "
            "Set this env var to the path of the cluster SKILL.md for this deployment."
        )

    model = _build_model_with_timeout(
        os.environ.get("AGENT_MODEL", "anthropic:claude-sonnet-4-6")
    )

    agent = create_deep_agent(
        model=model,
        skills=["./skills/universal/"],
        subagents=build_subagents(mcp_tools),
        tools=[
            post_to_slack,
            post_approval_request,
            mark_stand_down,
            *mcp_tools,
        ],
        middleware=[
            # Logs input/output/cache_read tokens after every model call so
            # cost regressions are visible. Outermost in the user-middleware
            # block — runs after the model returns, before any other
            # post-processing.
            TokenUsageLoggingMiddleware(label="master"),
            KeepLoopingMiddleware(set(interrupt_on.keys())),
        ],
        checkpointer=checkpointer,
        store=store,
        interrupt_on=interrupt_on,
        memory=(
            ["./AGENTS.md"]
            + ([CLUSTER_SKILL_PATH] if CLUSTER_SKILL_PATH else [])
        ),
        system_prompt=SYSTEM_PROMPT,
    )

    return agent


def build_agent():
    """Synchronous shim for backwards compatibility. Prefer build_agent_async()."""
    import asyncio
    return asyncio.run(build_agent_async())
