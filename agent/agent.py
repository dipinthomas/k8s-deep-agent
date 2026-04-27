"""
Deep Agent setup for the K8s incident investigation agent.

kubectl and CloudWatch tools come from MCP servers (mcp_client.py).
Slack tools are Python-native to support the custom approval Block Kit UI.
"""

import logging
import os
from deepagents import create_deep_agent
from langgraph.checkpoint.memory import MemorySaver

from subagents import build_subagents
from tools.slack_tools import post_to_slack, post_approval_request
from memory.store import build_memory_store, seed_memory_store
from mcp_servers.mcp_client import get_mcp_tools

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """
You are an autonomous Kubernetes operations agent.

Before every investigation:
- Read AGENTS.md to understand this cluster — its services, priorities, and rules.
- Select the skill that matches the incident type. Skills contain investigation playbooks.
- Use whatever tools are available to gather evidence. Do not assume which tools exist —
  discover them and use the ones that answer the question.

At the start of every investigation:
- Call write_todos to decompose the incident into discrete investigation steps.
- Update your todos as findings emerge — add steps, complete them, or replan entirely
  if your hypothesis turns out to be wrong. Replanning is expected and correct.
- After human approval and execution, mark all steps complete and write the outcome
  to long-term memory.

Non-negotiable rules:
- ALWAYS ask for human approval before any action that modifies cluster state.
- ALWAYS post evidence to Slack before asking for approval.
- ALWAYS write the outcome to long-term memory after resolution.
- NEVER guess. If you are unsure, ask.
"""

# Keywords in a tool NAME that signal it is destructive
_DESTRUCTIVE_NAME_KEYWORDS = {
    "delete", "drain", "evict", "apply", "patch",
    "scale", "restart", "create", "update", "replace",
}

# Keywords in a tool DESCRIPTION that signal it is destructive
_DESTRUCTIVE_DESC_KEYWORDS = {
    "modif", "creat", "remov", "destroy", "delet",
    "evict", "drain", "apply", "patch", "restart", "scale",
}


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
        name_tokens = set(tool.name.lower().split("_"))
        desc_lower = (tool.description or "").lower()

        name_match = bool(name_tokens & _DESTRUCTIVE_NAME_KEYWORDS)
        desc_match = any(kw in desc_lower for kw in _DESTRUCTIVE_DESC_KEYWORDS)

        if name_match or desc_match:
            interrupt_on[tool.name] = True

    return interrupt_on


CLUSTER_SKILL_PATH = os.environ.get("CLUSTER_SKILL_PATH", "")


def build_agent():
    checkpointer = MemorySaver()
    store = build_memory_store()
    seed_memory_store(store)

    # Load MCP tools from gateway pod (blocking — fetches tool schemas once at startup).
    mcp_tools = get_mcp_tools()

    # Human-in-the-loop sequence (must always happen in this order):
    #   1. Agent calls post_approval_request → posts evidence + buttons to Slack
    #   2. Agent calls a destructive tool → interrupt_on fires → graph pauses
    #   3. Slack button click → handle_approve/deny in main.py → graph resumes
    #
    # interrupt_on is the guarantee. post_approval_request is the UI.
    # If interrupt_on doesn't gate a tool, the action executes without approval
    # even if post_approval_request was called. That's why dynamic derivation
    # is critical — hardcoded tool names may silently miss tools.
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

    model = os.environ.get("AGENT_MODEL", "anthropic:claude-sonnet-4-6")

    agent = create_deep_agent(
        model=model,
        skills=["./skills/universal/"],
        subagents=build_subagents(mcp_tools),
        tools=[
            post_to_slack,
            post_approval_request,
            *mcp_tools,
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
