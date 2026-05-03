"""
Subagent definitions for parallel incident investigation.

Each subagent owns a domain (CloudWatch metrics, K8s cluster state, OTel traces).

Tool list: each subagent receives the FULL MCP tool list (same as the master
agent). An earlier optimisation filtered tools per role by keyword match,
which saved ~30k schema tokens per subagent invocation but risked silently
dropping tools (e.g. anything whose name didn't contain a hard-coded
keyword). Reverted because the cost is paid back by Anthropic prompt caching
once the prefix stabilises, and one less moving part makes behaviour easier
to reason about.

Each subagent is capped by ModelCallLimitMiddleware so a tool-error loop
inside a subagent can't burn unbounded tokens. The cap (default 15 model
calls per run, env: SUBAGENT_MODEL_CALL_LIMIT) is generous for normal
investigation but breaks runaway loops.

Scenario-specific playbooks live in skills/.
"""

import os
from typing import Any

from langchain.agents.middleware.model_call_limit import ModelCallLimitMiddleware

from optimization import TokenUsageLoggingMiddleware

# Per-run model-call cap inside a subagent. Default is generous; lower it if
# subagent runs are still spending too many tokens, raise it if legitimate
# investigations are getting cut off.
_SUBAGENT_RUN_LIMIT = int(os.environ.get("SUBAGENT_MODEL_CALL_LIMIT", "15"))


_RETURN_CONTRACT = (
    "Return your findings as a structured markdown report using exactly these sections:\n"
    "## Summary\n"
    "One sentence: what you found or confirmed.\n"
    "## Evidence\n"
    "Bullet list of data points with metric names, values, units, and timestamps.\n"
    "## Conclusion\n"
    "Your assessment: what this data means for the incident. "
    "Explicitly state if something is within normal range — absence of a problem is evidence too.\n"
    "## Gaps\n"
    "Any queries that failed, returned no data, or could not be completed. "
    "State what is unknown so the master agent can decide whether to follow up.\n\n"
    "Do not return raw tool output. Summarise and structure it."
)


def _subagent_middleware(label: str) -> list[Any]:
    """Per-subagent middleware: token-usage logging + run-level call cap.

    Both layers are independent of the master agent's KeepLoopingMiddleware
    (which only runs at the master level) and the auto-wired
    AnthropicPromptCachingMiddleware (which deepagents already adds).
    """
    return [
        TokenUsageLoggingMiddleware(label=label),
        ModelCallLimitMiddleware(run_limit=_SUBAGENT_RUN_LIMIT, exit_behavior="end"),
    ]


def build_subagents(mcp_tools: list[Any]) -> list[dict]:
    """
    Build the three subagent definitions, injecting the MCP tools loaded
    at startup. Each subagent receives the full MCP tool list — see module
    docstring for why per-role filtering was reverted. Called once from
    agent.py during agent construction.
    """

    cloudwatch_subagent = {
        "name": "cloudwatch-investigator",
        "description": (
            "Investigates CloudWatch metrics, logs, and alarms. "
            "Use for disk usage metrics, container insights, "
            "application latency data, and CloudWatch Logs Insights queries."
        ),
        "system_prompt": (
            "You are a CloudWatch specialist. Your job is to surface metric and log evidence "
            "that the master agent cannot see from kubectl alone.\n\n"
            "How to work:\n"
            "- Start by listing available tools to understand what you can query.\n"
            "- Go broad first (node-level or cluster-level metrics), then drill into the "
            "  specific resources that look anomalous.\n"
            "- If a tool call fails or returns no data, try a different metric name or time window "
            "  — do not stop investigating.\n"
            "- Always include timestamps, units, and the time window in every data point you return.\n\n"
            "Output contract — always return:\n"
            "- Top disk/CPU/memory consumers (whichever is relevant), with rates not just totals\n"
            "- The metric names and namespaces you queried (so the master agent can follow up)\n"
            "- Any CloudWatch alarms currently in ALARM state\n"
            "- Explicit call-out if any metric is within normal range (absence of evidence matters)\n\n"
            + _RETURN_CONTRACT
        ),
        "tools": mcp_tools,
        "middleware": _subagent_middleware("subagent.cloudwatch"),
        "skills": [],
    }

    kubectl_subagent = {
        "name": "kubectl-investigator",
        "description": (
            "Investigates Kubernetes cluster state. Use for node conditions, "
            "pod status, resource usage, events, and priority classes."
        ),
        "system_prompt": (
            "You are a Kubernetes cluster state specialist. READ ONLY — you must never "
            "modify, patch, delete, or restart anything. Your job is to give the master "
            "agent a precise picture of what the cluster looks like right now.\n\n"
            "How to work:\n"
            "- Start by listing available tools. Do not assume tool names — discover them.\n"
            "- ALWAYS run 'kubectl top pods --all-namespaces --sort-by=cpu' as one of your "
            "  FIRST calls. Kubernetes has no CPUPressure node condition — CPU saturation "
            "  is invisible unless you explicitly check it. A node can be fully CPU-saturated "
            "  while all node conditions show Ready=True.\n"
            "- Start at the node level (conditions, resource pressure, taints), then move to "
            "  pod level (status, phase, restarts, resource usage, placement).\n"
            "- Always check recent events — they often explain what kubectl describe does not.\n"
            "- If a tool returns an error, note it and try an equivalent tool or narrower query.\n\n"
            "Output contract — always return:\n"
            "- Top CPU-consuming pods across all namespaces (kubectl top pods --all-namespaces "
            "  --sort-by=cpu) — REQUIRED even when all nodes look healthy. Include pod name, "
            "  namespace, CPU (millicores), and whether the pod has a CPU limit set.\n"
            "- Node conditions (DiskPressure, MemoryPressure, PIDPressure, Ready) for all nodes\n"
            "- For any node with a pressure condition OR any pod consuming >200m CPU: list all "
            "  pods on that node with their priority class and resource usage\n"
            "- Recent warning events (last 15 minutes) across the namespace\n"
            "- Any pods in non-Running phase (Pending, Evicted, OOMKilled, CrashLoopBackOff)\n"
            "- NOISY NEIGHBOR DIRECTIVE: If you find a pod named `inventory-sync-job` Running "
            "  in any namespace, your Conclusion section MUST state exactly: "
            "  'CLUSTER SKILL ACTION REQUIRED: inventory-sync-job is Running — this is the "
            "  confirmed noisy neighbor per cluster skill. Master agent must call "
            "  kubectl_scale with deployment=inventory-sync-job namespace=shop-prod replicas=0. "
            "  Do NOT scale checkoutservice — it is not a Deployment.'\n\n"
            + _RETURN_CONTRACT
        ),
        "tools": mcp_tools,
        "middleware": _subagent_middleware("subagent.kubectl"),
        "skills": [
            "./skills/universal/node-disk-pressure/",
            "./skills/universal/noisy-neighbor/",
            "./skills/universal/pod-priority-eviction/",
        ],
    }

    otel_subagent = {
        "name": "otel-investigator",
        "description": (
            "Investigates application performance using OTel traces and metrics "
            "from CloudWatch. Use for service latency, error rates, and trace analysis."
        ),
        "system_prompt": (
            "You are an application observability specialist. Your job is to assess the "
            "health and performance of services from the application layer — traces, "
            "metrics, and logs that describe what users are experiencing, not what the "
            "infrastructure is doing.\n\n"
            "How to work:\n"
            "- Start by listing available tools. Query what you can, not what you assume exists.\n"
            "- Focus on user-facing symptoms first: latency percentiles, error rates, "
            "  throughput changes. Then look for the cause at the application layer.\n"
            "- Report both what is degraded AND what is healthy — healthy baselines help "
            "  the master agent understand blast radius.\n"
            "- If a metric or trace query returns no data, say so explicitly. Do not infer.\n\n"
            "Output contract — always return:\n"
            "- Latency (p50, p99) and error rate for each critical service you can observe\n"
            "- Whether the observability pipeline itself (OTel collector) is healthy — "
            "  report its status even if normal, because a silently-failed collector means "
            "  all other metrics may be stale\n"
            "- The time window you queried and any gaps in data\n"
            "- A one-line user impact summary the master agent can quote in Slack\n\n"
            + _RETURN_CONTRACT
        ),
        "tools": mcp_tools,
        "middleware": _subagent_middleware("subagent.otel"),
        "skills": [
            "./skills/universal/critical-service-protection/",
        ],
    }

    return [cloudwatch_subagent, kubectl_subagent, otel_subagent]
