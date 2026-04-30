"""
Subagent definitions for parallel incident investigation.

Each subagent owns a domain (CloudWatch metrics, K8s cluster state, OTel traces).
They receive the full MCP tool list and use their role + skills to decide what to
query — no hardcoded investigation steps. Scenario-specific playbooks live in skills/.
"""

from typing import Any

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


def build_subagents(mcp_tools: list[Any]) -> list[dict]:
    """
    Build the three subagent definitions, injecting the MCP tools loaded
    at startup. Called once from agent.py during agent construction.
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
            "- Start at the node level (conditions, resource pressure, taints), then move to "
            "  pod level (status, phase, restarts, resource usage, placement).\n"
            "- Always check recent events — they often explain what kubectl describe does not.\n"
            "- If a tool returns an error, note it and try an equivalent tool or narrower query.\n\n"
            "Output contract — always return:\n"
            "- Node conditions (DiskPressure, MemoryPressure, PIDPressure, Ready) for all nodes\n"
            "- For any node with a pressure condition: list of pods on that node with their "
            "  priority class and resource usage\n"
            "- Recent warning events (last 15 minutes) across the namespace\n"
            "- Any pods in non-Running phase (Pending, Evicted, OOMKilled, CrashLoopBackOff)\n\n"
            + _RETURN_CONTRACT
        ),
        "tools": mcp_tools,
        "skills": [
            "./skills/universal/node-disk-pressure/",
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
        "skills": [
            "./skills/universal/critical-service-protection/",
        ],
    }

    return [cloudwatch_subagent, kubectl_subagent, otel_subagent]
