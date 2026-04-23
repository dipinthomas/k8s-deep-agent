"""
Deep Agent setup for the K8s disk pressure demo.
Uses LangGraph checkpointing for stateful pause/resume at the approval step.
"""

import os
from deepagents import create_deep_agent
from langgraph.checkpoint.memory import MemorySaver
from langgraph.store.memory import InMemoryStore

from subagents import cloudwatch_subagent, kubectl_subagent, otel_subagent
from tools.slack_tools import post_to_slack, post_approval_request
from tools.kubectl_tools import kubectl_evict_pod, kubectl_drain_node, kubectl_delete
from memory.store import build_memory_store

SYSTEM_PROMPT = """
You are an autonomous Kubernetes operations agent for the otel-demo-prod cluster (EKS ap-southeast-2).

ALWAYS start by reading AGENTS.md to understand the cluster, its services, priority classes, and rules.

Investigation approach:
1. Acknowledge the incident in Slack immediately.
2. Spawn your three subagents in PARALLEL: cloudwatch-investigator, kubectl-investigator, otel-investigator.
3. Analyse their combined findings. It is normal to revise your hypothesis — post updates to Slack as you go.
4. Post CloudWatch evidence to Slack BEFORE asking for approval.
5. Build a ranked eviction list (lowest priority first). NEVER include payment-critical services.
6. Post an approval request with [APPROVE] [DENY] [GIVE ME MORE DETAILS] buttons and WAIT.
7. Only execute evictions after explicit human approval.
8. Post a resolution summary with before/after metrics.
9. Write the incident and root cause to long-term memory.

Rules you must never break:
- Never evict checkoutservice, paymentservice, cartservice, or productcatalogservice.
- Never drain a node without approval.
- Never delete a PVC.
- Always show evidence before asking for approval.
- If unsure, ask — do not guess.
""".strip()


def build_agent():
    checkpointer = MemorySaver()
    store = build_memory_store()

    agent = create_deep_agent(
        model="anthropic:claude-sonnet-4-6",
        skills=["./skills/"],
        subagents=[cloudwatch_subagent, kubectl_subagent, otel_subagent],
        tools=[post_to_slack, post_approval_request],
        checkpointer=checkpointer,
        store=store,
        interrupt_on={
            # These tools require human approval before execution
            "kubectl_evict_pod": True,
            "kubectl_drain_node": True,
            "kubectl_delete": True,
        },
        system_prompt=SYSTEM_PROMPT,
        agents_file="./AGENTS.md",
    )

    return agent
