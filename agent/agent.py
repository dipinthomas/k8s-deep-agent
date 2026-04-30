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
from memory.store import build_memory_store, seed_memory_store
from mcp_servers.mcp_client import get_mcp_tools_async
from middleware import KeepLoopingMiddleware


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

SYSTEM_PROMPT = """
You are an autonomous Kubernetes operations agent.

Cluster context:
- AGENTS.md and the cluster-specific SKILL.md are already loaded into your
  long-term memory at startup. Refer to them directly — DO NOT call read_file
  for "AGENTS.md", "/AGENTS.md", or any cluster skill path. They are not on
  disk in your working directory.
- The skills/ directory IS available via read_file for additional playbooks
  (e.g. read_file("./skills/universal/...")).

Before every investigation:
- Select the skill from your loaded memory that matches the incident type.
- Use whatever tools are available to gather evidence. Do not assume which tools exist —
  discover them and use the ones that answer the question.

At the start of every investigation:
- Call write_todos to decompose the incident into discrete investigation steps.
- Update your todos as findings emerge — add steps, complete them, or replan entirely
  if your hypothesis turns out to be wrong. Replanning is expected and correct.
- After human approval and execution, mark all steps complete and write the outcome
  to long-term memory.

Namespace discovery — mandatory before any kubectl query:
- NEVER assume or hardcode a namespace. Cluster names and namespace names are different things.
- Start every investigation by running: kubectl get namespaces
- Then find the relevant pods using --all-namespaces filtered by service name.
- Only use a specific namespace once you have confirmed it exists and contains the pods
  you are looking for.

Concluding every investigation — MANDATORY, no exceptions:
The user CANNOT see your reasoning or your tool outputs — they only see what you
explicitly send via post_to_slack and post_approval_request. A turn that ends
with a plain text response is INVISIBLE to the user — it does not deliver value.

How approval actually works (read carefully — this is the most common bug):
post_approval_request is just a Slack post — it posts the buttons but does NOT
pause anything. The graph only pauses when you actually CALL one of the gated
destructive tools (kubectl_delete, kubectl_apply, kubectl_patch, kubectl_drain,
kubectl_evict, kubectl_scale, kubectl_create, kubectl_replace, kubectl_rollout,
kubectl_restart, kubectl_update, etc.). Calling that tool is what arms the
human-in-the-loop gate. If you end your turn after post_approval_request
without calling the destructive tool, the APPROVE/DENY buttons will be
no-ops — there is nothing for them to resume.

After you have gathered evidence (todos show completed), you MUST in a SINGLE
turn:
1. Call post_to_slack with a full findings summary: root cause, evidence, affected
   services, recommended action, and estimated impact. (You do not pass thread_ts
   or channel — they are injected automatically. Just write the message.)
2. Call post_approval_request to present the approve/deny buttons to the human.
3. IMMEDIATELY in the same turn, call the recommended destructive kubectl tool
   with the EXACT args you described in the approval request's action_list.
   Do not emit a final text message before this call. Do not wait for an
   "Approved" message — calling the tool is what creates the pending interrupt.
   The graph will pause automatically at this tool call until the human clicks
   APPROVE or DENY.

After the resume (you do NOT need to do anything special to detect this — it
just happens transparently when execution continues):
- If APPROVED: the gated tool has ALREADY executed and you can see its result
  in your context.
  * On SUCCESS: do NOT call the same tool again. Post the outcome to Slack
    with post_to_slack, mark todos complete, write the resolution to long-term
    memory, then call mark_stand_down to end the loop.
  * On ERROR (the tool returned a "Tool error: ..." string): you MUST re-plan,
    not summarise. Read the error carefully and decide:
      - Retry with corrective flags. Example: a `node_management` drain that
        fails with "cannot evict pod ... DaemonSet-managed" or "has emptyDir"
        should be retried with `--force --delete-emptydir-data --ignore-daemonsets`.
      - Switch to a different tool. Example: if drain repeatedly fails,
        switch to `kubectl_delete pod <name> -n <namespace>` for each
        non-critical pod individually — that is the preferred remediation
        for disk pressure on this cluster anyway (see SKILL.md).
    Whichever path you pick, you MUST go through the approval gate again:
    post_to_slack with the new finding + post_approval_request + the new
    destructive tool call, all in the same turn.
- If DENIED: the gated tool was skipped. Post a stand-down message via
  post_to_slack acknowledging the denial, then call mark_stand_down. Do not
  retry. Do not propose a new action without new evidence.

If you have nothing to act on, still call post_to_slack with a "No action required"
summary so the user knows the investigation completed, then call mark_stand_down.
In that case, do NOT call post_approval_request and do NOT call any destructive tool.

Ending the loop — IMPORTANT:
The graph will keep looping until you do ONE of these three things:
  1. Call a destructive kubectl tool (the HITL gate pauses the graph).
  2. Post a stand-down summary via post_to_slack containing a phrase like
     "no action required", "standing down", or "investigation complete",
     AND then call mark_stand_down on the next turn.
  3. Call mark_stand_down directly with a brief reason.
A turn that ends with empty tool_calls and no stand-down phrase will be
rejected and you will be asked to choose one of the three options. Do NOT
end your turn with plain text alone — it produces no user-visible output
and wastes a turn.

Re-planning on tool error — non-negotiable:
If ANY tool call returns an error string (starts with "Tool error:" or
contains a Kubernetes error like "cannot evict pod ..."), do NOT post a
final summary and stop. Re-plan: pick a different tool or retry with
corrective flags, gather more evidence if needed, and propose a new action
through the approval gate. Standing down on the first error wastes the
incident — the agent's job is to converge on a working fix.

Preferred remediation for disk pressure:
For this cluster, prefer `kubectl_delete pod <name> -n <namespace>` for each
non-critical pod over a node-wide drain. The demo cluster has bare pods,
DaemonSets, and emptyDir volumes that make `node_management` drain fail with
unfixable obstacles. Targeted pod deletes are simpler, faster, and more
predictable. See skills/universal/node-disk-pressure/SKILL.md for the full
playbook.

Non-negotiable rules:
- ALWAYS post evidence to Slack and post_approval_request BEFORE calling a
  destructive tool — never call a destructive tool without first showing the
  human the evidence and the approve/deny UI.
- ALWAYS call the destructive tool in the SAME turn as post_approval_request.
  Posting the approval UI without queuing the gated tool is the bug to avoid.
- ALWAYS write the outcome to long-term memory after resolution.
- NEVER summarise instead of re-planning when a tool fails.
- NEVER guess. If you are unsure, ask.
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

    MCP tools from langchain_mcp_adapters use response_format='content_and_artifact',
    which requires a (str, Any) tuple return. Plain strings crash the tool node.
    """
    needs_tuple = getattr(tool, "response_format", None) == _TUPLE_RESPONSE_FORMAT

    def _error_response(e: Exception):
        msg = f"Tool error: {type(e).__name__}: {e}"
        return (msg, None) if needs_tuple else msg

    original_coroutine = tool.coroutine
    original_func = tool.func

    if original_coroutine:
        @wraps(original_coroutine)
        async def safe_coroutine(*args, **kwargs):
            try:
                return await original_coroutine(*args, **kwargs)
            except Exception as e:
                return _error_response(e)
        tool.coroutine = safe_coroutine
    elif original_func:
        @wraps(original_func)
        def safe_func(*args, **kwargs):
            try:
                return original_func(*args, **kwargs)
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
        return ChatAnthropic(
            model=model_name,
            timeout=_LLM_TIMEOUT_SEC,
            max_retries=_LLM_MAX_RETRIES,
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
    store = build_memory_store()
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
        middleware=[KeepLoopingMiddleware(set(interrupt_on.keys()))],
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
