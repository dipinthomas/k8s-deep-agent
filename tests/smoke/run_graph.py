"""Smoke-test the EKS agent (agent/) against MCP servers, without Slack.

  python run_graph.py --tools-only   # build the agent, check tools + approval gate (no model calls)
  python run_graph.py                # full incident: alarm -> approval -> approve -> done

Runs inside the agent image (PYTHONPATH=/app/agent). Expects KUBECTL_MCP_URL and
CLOUDWATCH_MCP_URL to point at MCP servers: tests/smoke/replay_mcp.py for the
recorded incident, or the real sidecars from infra/mcp-gateway.Dockerfile.
Exits non-zero on any failed check.
"""
import argparse
import asyncio
import json
import os
import sys
import time
import urllib.request

import agent as k8s_agent
import tools.slack_tools as slack_tools
from langchain_core.callbacks import BaseCallbackHandler
from langgraph.types import Command

POSTS: list[dict] = []
FAILURES: list[str] = []


def check(ok: bool, what: str) -> None:
    print(("  PASS  " if ok else "  FAIL  ") + what)
    if not ok:
        FAILURES.append(what)


def capture_post(label, channel, thread_ts, **kwargs):  # replaces the Slack API call
    POSTS.append({"label": label, "text": kwargs.get("text", "")})
    print(f"  [slack:{label}] {kwargs.get('text', '')[:200]}")
    return f"{time.time():.6f}", None


slack_tools._post = capture_post


class FirstPrompt(BaseCallbackHandler):
    """Keep the first prompt the main agent sends to the model (system prompt included)."""
    text = ""

    def on_chat_model_start(self, serialized, messages, **kwargs):
        if not FirstPrompt.text:
            FirstPrompt.text = "\n".join(str(m.content) for batch in messages for m in batch)

ALARM = ("CloudWatch alarm fired:\n\nAlarm: retail-prod-checkout-latency-p99\n"
         "Node: (service-level alarm)\nReason: Threshold Crossed: checkoutservice LatencyP99 "
         "1512 ms > 300 ms for 3 datapoints (RetailProd/Services)\n\n"
         "Slack thread: smoke-0001\nChannel: #k8s-alerts\n")


def pending(state) -> list:
    out = []
    for t in getattr(state, "tasks", ()) or ():
        for intr in getattr(t, "interrupts", ()) or ():
            out += (getattr(intr, "value", {}) or {}).get("action_requests") or []
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tools-only", action="store_true")
    ap.add_argument("--state-url", default=os.environ.get("REPLAY_STATE_URL", ""))
    a = ap.parse_args()

    print("harness root:", k8s_agent.HARNESS_ROOT)
    check(os.path.isfile(os.path.join(k8s_agent.HARNESS_ROOT, "AGENTS.md")), "AGENTS.md under HARNESS_ROOT")
    check(os.path.isdir(os.path.join(k8s_agent.HARNESS_ROOT, "skills", "universal")), "skills/universal under HARNESS_ROOT")

    tools = await asyncio.wait_for(k8s_agent.get_mcp_tools_async(), timeout=180)
    names = sorted(t.name for t in tools)
    gated = sorted(k8s_agent._build_interrupt_on(tools))
    print("tools:", names)
    print("approval gate on:", gated)
    for must in ("kubectl_get", "kubectl_scale", "kubectl_delete", "get_metric_data"):
        check(must in names, f"tool {must} loaded")
    for must in ("kubectl_scale", "kubectl_delete"):
        check(must in gated, f"{must} requires approval")
    for risky in ("kubectl_generic", "exec_in_pod", "install_helm_chart", "uninstall_helm_chart", "cleanup"):
        if risky in names:  # only the real sidecars expose these
            check(risky in gated, f"{risky} requires approval")
    for read_only in ("kubectl_get", "kubectl_top", "kubectl_describe", "kubectl_logs", "get_metric_data",
                      "execute_log_insights_query", "describe_log_groups"):
        check(read_only not in gated, f"{read_only} does not require approval")

    agent = await k8s_agent.build_agent_async()
    check(agent is not None, "agent graph builds")
    if a.tools_only:
        return

    config = {"configurable": {"thread_id": "smoke-0001", "channel_id": "#k8s-alerts",
                               "thread_ts": "smoke-0001"}, "recursion_limit": 200,
              "callbacks": [FirstPrompt()]}
    await agent.ainvoke({"messages": [{"role": "user", "content": ALARM}]}, config)
    state = await agent.aget_state(config)
    reqs = pending(state)
    print("approval requested for:", json.dumps(reqs)[:300])
    check(bool(reqs), "agent paused for human approval")
    check(any(r.get("name") == "kubectl_scale" and "image-resizer" in json.dumps(r) for r in reqs),
          "it asks to scale image-resizer")

    prompt = FirstPrompt.text
    check("No skills available yet" not in prompt, 'prompt does not say "No skills available yet"')
    check("/harness/skills/universal/noisy-neighbor/SKILL.md" in prompt, "noisy-neighbor skill listed in the prompt")
    check("(No memory loaded)" not in prompt and "Agent Identity" in prompt, "AGENTS.md loaded into the prompt")
    if reqs:
        await agent.ainvoke(Command(resume={"decisions": [{"type": "approve"} for _ in reqs]}), config)
        state = await agent.aget_state(config)
        check(not pending(state), "incident finishes after approval")
    tool_calls = [tc["name"] for m in state.values.get("messages", []) for tc in (getattr(m, "tool_calls", None) or [])]
    print("tool calls:", tool_calls)
    check("post_approval_request" in tool_calls, "approval request posted (Slack stub)")
    check(not any(t == "kubectl_delete" for t in tool_calls), "no pods deleted")
    if a.state_url:
        st = json.loads(urllib.request.urlopen(a.state_url, timeout=5).read())
        print("replay state:", st)
        check(st.get("fixed") is True, "replayed cluster is fixed (image-resizer at 0)")


if __name__ == "__main__":
    asyncio.run(main())
    print(f"\n{'FAILED' if FAILURES else 'OK'}: {len(FAILURES)} failed check(s)")
    sys.exit(1 if FAILURES else 0)
