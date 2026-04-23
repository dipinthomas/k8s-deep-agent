"""
MCP server configuration for the K8s agent.
Three MCP servers provide kubectl, CloudWatch, and Slack tool access.
"""

import os

KUBECTL_PORT = int(os.environ.get("KUBECTL_MCP_PORT", 3001))
CLOUDWATCH_PORT = int(os.environ.get("CLOUDWATCH_MCP_PORT", 3002))
SLACK_PORT = int(os.environ.get("SLACK_MCP_PORT", 3003))

MCP_SERVERS = [
    {
        "name": "kubectl-mcp",
        "type": "url",
        "url": f"http://localhost:{KUBECTL_PORT}/mcp",
        "description": "Kubernetes cluster operations via kubectl",
        "tools": [
            "kubectl_get",
            "kubectl_describe",
            "kubectl_logs",
            "kubectl_top",
            "kubectl_get_events",
            "kubectl_evict_pod",    # requires human approval
            "kubectl_drain_node",   # requires human approval
            "kubectl_delete",       # requires human approval
        ],
    },
    {
        "name": "cloudwatch-mcp",
        "type": "url",
        "url": f"http://localhost:{CLOUDWATCH_PORT}/mcp",
        "description": "AWS CloudWatch metrics, logs, and alarms",
        "tools": [
            "cloudwatch_get_metric",
            "cloudwatch_get_metric_data",
            "cloudwatch_logs_insights",
            "cloudwatch_describe_alarms",
            "cloudwatch_get_traces",
        ],
    },
    {
        "name": "slack-mcp",
        "type": "url",
        "url": f"http://localhost:{SLACK_PORT}/mcp",
        "description": "Post to Slack, read messages, handle approvals",
        "tools": [
            "post_to_slack",
            "post_approval_request",
        ],
    },
]
