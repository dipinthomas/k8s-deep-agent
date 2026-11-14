"""
AgentCore Runtime entrypoint for the k8s deep agent (nz-tech-rally / k8s-deep-agent).

The agent itself is the repo's code, copied unchanged into ./agent_src by
scripts/sync_agent.sh. This file only swaps the plumbing that was specific to the
EKS pod deployment:

  EKS pod (before)                      AgentCore (now)
  ------------------------------------  ---------------------------------------------
  Flask /trigger + Slack bolt           Runtime invoke: {"action": "alarm"} / "decide"
  kubectl-mcp + cloudwatch-mcp (SSE)    AgentCore Gateway (MCP, SigV4) -> replay Lambda
  Redis checkpointer                    AgentCore Memory (AgentCoreMemorySaver)
  /memories/incidents.md on emptyDir    AgentCore Memory, episodic strategy
  Phoenix traces                        AgentCore Observability (ADOT, CloudWatch)

Slack posts are captured and returned in the response, so the approval card the
on-call engineer would see in Slack is what the driver script prints.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time

import boto3
import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from langgraph.types import Command

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)  # the agent resolves ./AGENTS.md and ./skills relative to its working dir
os.environ.setdefault("HARNESS_ROOT", HERE)
os.environ.setdefault("CLUSTER_SKILL_PATH", "./clusters/retail-prod/SKILL.md")
os.environ.setdefault("AGENT_MODEL", "bedrock_converse:global.anthropic.claude-sonnet-5-5")
os.environ.setdefault("AGENT_MEMORY_DIR", "/tmp/agent-memory")
sys.path.insert(0, os.path.join(HERE, "agent_src"))

import agent as k8s_agent  # noqa: E402  (the repo's agent.py)
import tools.slack_tools as slack_tools  # noqa: E402

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("k8s-harness")

REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
GATEWAY_URL = os.environ.get("AGENTCORE_GATEWAY_K8S_TOOLS_URL", "")
MEMORY_ID = os.environ.get("MEMORY_INCIDENTMEMORY_ID", "")
ACTOR_ID = "k8s-agent-retail-prod"
HARNESS_VERSION = os.environ.get("HARNESS_VERSION", "unknown")

app = BedrockAgentCoreApp()
_session = boto3.Session(region_name=REGION)


# ---- Gateway: same MCP tools, now behind AgentCore Gateway (AWS_IAM / SigV4) ----
class _SigV4(httpx.Auth):
    requires_request_body = True

    def auth_flow(self, request):
        creds = _session.get_credentials().get_frozen_credentials()
        aws = AWSRequest(method=request.method, url=str(request.url), data=request.content,
                         headers={"Content-Type": request.headers.get("content-type", "application/json")})
        SigV4Auth(creds, "bedrock-agentcore", REGION).add_auth(aws)
        for k, v in aws.headers.items():
            request.headers[k] = v
        yield request


async def gateway_tools_async():
    from langchain_mcp_adapters.client import MultiServerMCPClient

    client = MultiServerMCPClient({"k8s-tools": {"transport": "streamable_http", "url": GATEWAY_URL, "auth": _SigV4()}})
    tools = await client.get_tools()
    for t in tools:  # Gateway prefixes tool names with "<target>___"; keep the production names
        t.name = t.name.split("___")[-1]
    log.info("Gateway tools: %s", [t.name for t in tools])
    return tools


# ---- Slack: capture what would have been posted -------------------------------
_POSTS: dict[str, list] = {}


def _capture_post(label, channel, thread_ts, **kwargs):
    entry = {"label": label, "text": kwargs.get("text", "")}
    if kwargs.get("blocks"):
        entry["blocks"] = [b.get("text", {}).get("text") for b in kwargs["blocks"] if b.get("type") == "section"]
    _POSTS.setdefault(thread_ts, []).append(entry)
    return f"{time.time():.6f}", None


slack_tools._post = _capture_post


# ---- Memory: AgentCore Memory replaces Redis ----------------------------------
async def agentcore_checkpointer():
    # Ordered + single-read get_delta_channel_history; see ordered_saver.py.
    from ordered_saver import OrderedAgentCoreMemorySaver

    return OrderedAgentCoreMemorySaver(MEMORY_ID, region_name=REGION)


k8s_agent.get_mcp_tools_async = gateway_tools_async
if MEMORY_ID:
    k8s_agent._build_checkpointer = agentcore_checkpointer

_AGENT = None
_LOCK = asyncio.Lock()


async def get_agent():
    global _AGENT
    async with _LOCK:
        if _AGENT is None:
            _AGENT = await k8s_agent.build_agent_async()
    return _AGENT


def _config(session_id: str) -> dict:
    return {"configurable": {"thread_id": session_id, "actor_id": ACTOR_ID,
                             "channel_id": "#k8s-alerts", "thread_ts": session_id},
            "recursion_limit": 200}


def _pending(state) -> list:
    out = []
    for t in getattr(state, "tasks", ()) or ():
        for intr in getattr(t, "interrupts", ()) or ():
            val = getattr(intr, "value", {}) or {}
            for req in (val.get("action_requests") or []):
                out.append({"tool": req.get("name"), "args": req.get("args") or req.get("arguments")})
    return out


def _tool_calls(messages) -> list:
    calls = []
    for m in messages:
        for tc in getattr(m, "tool_calls", None) or []:
            calls.append(tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", ""))
    return calls


def _recall(alarm_text: str) -> str:
    """Episodic recall from AgentCore Memory: past incidents that look like this one."""
    if not MEMORY_ID:
        return ""
    try:
        dp = _session.client("bedrock-agentcore")
        recs = dp.retrieve_memory_records(memoryId=MEMORY_ID, namespace=f"/episodes/{ACTOR_ID}",
                                          searchCriteria={"searchQuery": alarm_text, "topK": 3})
        items = [r.get("content", {}).get("text", "") for r in recs.get("memoryRecordSummaries", [])]
        items = [i for i in items if i]
        return ("\n\nPast incidents from long-term memory:\n- " + "\n- ".join(i[:600] for i in items)) if items else ""
    except Exception as e:  # memory is helpful, never fatal
        log.warning("memory recall failed: %s", e)
        return ""


def _remember(session_id: str, alarm_text: str, posts: list) -> None:
    """Write the resolved incident as conversational events; the episodic strategy turns it into an episode."""
    if not MEMORY_ID or not posts:
        return
    summary = "\n\n".join(p["text"] for p in posts if p.get("text"))[:8000]
    try:
        _session.client("bedrock-agentcore").create_event(
            memoryId=MEMORY_ID, actorId=ACTOR_ID, sessionId=session_id,
            eventTimestamp=time.time(),
            payload=[{"conversational": {"role": "USER", "content": {"text": alarm_text}}},
                     {"conversational": {"role": "ASSISTANT", "content": {"text": summary}}}])
    except Exception as e:
        log.warning("memory write failed: %s", e)


@app.entrypoint
async def invoke(payload, context):
    session_id = getattr(context, "session_id", None) or payload.get("session_id") or "local-session-000000000000000000000"
    action = payload.get("action", "alarm")
    agent = await get_agent()
    config = _config(session_id)
    _POSTS[session_id] = []
    t0 = time.time()

    if action == "alarm":
        alarm = payload.get("alarm", {})
        text = (f"CloudWatch alarm fired:\n\nAlarm: {alarm.get('alarm_name', 'unknown')}\n"
                f"Node: {alarm.get('node', 'unknown')}\nReason: {alarm.get('reason', '')}\n\n"
                f"Slack thread: {session_id}\nChannel: #k8s-alerts\n")
        if payload.get("use_memory"):
            text += _recall(text)
        await agent.ainvoke({"messages": [{"role": "user", "content": text}]}, config)
    elif action == "decide":
        state = await agent.aget_state(config)
        n = len(_pending(state)) or 1
        decision = payload.get("decision", "approve")
        cmd = Command(resume={"decisions": [{"type": decision} for _ in range(n)]})
        await agent.ainvoke(cmd, config)
    else:
        return {"error": f"unknown action {action}"}

    state = await agent.aget_state(config)
    pending = _pending(state)
    msgs = (state.values or {}).get("messages", [])
    status = "awaiting_approval" if pending else "done"
    if status == "done" and payload.get("use_memory"):
        _remember(session_id, msgs[0].content if msgs else "", _POSTS[session_id])
    return {"status": status, "harness_version": HARNESS_VERSION, "session_id": session_id,
            "pending_actions": pending, "slack": _POSTS[session_id],
            "tool_calls": _tool_calls(msgs), "seconds": round(time.time() - t0, 1)}


if __name__ == "__main__":
    app.run()
