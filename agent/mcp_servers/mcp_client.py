"""
MCP client manager.

Connects to two MCP servers running in the k8s-mcp-gateway pod via streamable-http:

  kubectl     → mcp-server-kubernetes (port 3001)
                Auth: in-cluster ServiceAccount token (K8s RBAC in mcp-gateway-deployment.yaml)

  cloudwatch  → awslabs.cloudwatch-mcp-server (port 3002)
                Auth: IRSA — EKS injects AWS_ROLE_ARN + AWS_WEB_IDENTITY_TOKEN_FILE
                into the gateway pod; the AWS SDK credential chain picks them up.

Slack stays as direct Python (tools/slack_tools.py) — post_approval_request
uses custom Block Kit that is not in the generic Slack MCP server.

Local dev: set KUBECTL_MCP_URL and CLOUDWATCH_MCP_URL to point at locally-running
MCP servers.

IMPORTANT: get_mcp_tools_async() must be called from within the persistent event loop
(via the _agent_loop in main.py). Tool objects hold HTTP sessions bound to the loop
they were created in — calling them from a different loop causes silent hangs.
"""

import asyncio
import logging
import os
from typing import Any

from langchain_mcp_adapters.client import MultiServerMCPClient

logger = logging.getLogger(__name__)

# Sidecar MCP servers may take a few seconds longer to be ready than the agent
# container. Retry tool loading so a slow sidecar doesn't crash bootstrap.
_MCP_LOAD_ATTEMPTS = 20
_MCP_LOAD_BACKOFF_SEC = 3

_SERVER_CONFIG: dict[str, Any] = {
    "kubectl": {
        "transport": "streamable_http",
        "url": os.environ.get(
            "KUBECTL_MCP_URL",
            "http://k8s-mcp-gateway.k8s-agent.svc.cluster.local:3001/mcp",
        ),
    },
    "cloudwatch": {
        "transport": "streamable_http",
        "url": os.environ.get(
            "CLOUDWATCH_MCP_URL",
            "http://k8s-mcp-gateway.k8s-agent.svc.cluster.local:3002/mcp",
        ),
    },
}


# Tools to exclude — these tools are loaded by the MCP servers but have no
# functional backend in this cluster. Filtering prevents the agent from wasting
# turns attempting queries that will always fail.
#
# promql_* / execute_promql_* — added by awslabs.cloudwatch-mcp-server in
# recent versions; require a Prometheus endpoint which is not installed here.
_BLOCKED_TOOL_SUBSTRINGS = {"promql"}


def _filter_tools(tools: list[Any]) -> list[Any]:
    filtered = [
        t for t in tools
        if not any(blocked in t.name.lower() for blocked in _BLOCKED_TOOL_SUBSTRINGS)
    ]
    blocked_names = [t.name for t in tools if t not in filtered]
    if blocked_names:
        logger.info("Filtered %d unsupported tools: %s", len(blocked_names), blocked_names)
    return filtered


async def get_mcp_tools_async() -> list[Any]:
    """
    Load MCP tools asynchronously. MUST be awaited from within the persistent
    agent event loop so the returned tool objects' HTTP sessions are bound to
    that loop and remain valid for the lifetime of the process.

    Retries on failure — sidecars often need a few extra seconds to bind their
    ports after the agent container starts.
    """
    client = MultiServerMCPClient(_SERVER_CONFIG)
    last_exc: Exception | None = None
    for attempt in range(1, _MCP_LOAD_ATTEMPTS + 1):
        try:
            logger.info("Loading MCP tools (attempt %d/%d)...", attempt, _MCP_LOAD_ATTEMPTS)
            tools = _filter_tools(await client.get_tools())
            logger.info(
                "MCP ready — %d tools loaded: %s",
                len(tools),
                [t.name for t in tools],
            )
            return tools
        except Exception as e:
            last_exc = e
            logger.warning(
                "MCP tool load failed (attempt %d/%d): %s — retrying in %ds",
                attempt, _MCP_LOAD_ATTEMPTS, e, _MCP_LOAD_BACKOFF_SEC,
            )
            await asyncio.sleep(_MCP_LOAD_BACKOFF_SEC)
    raise RuntimeError(
        f"MCP tool load failed after {_MCP_LOAD_ATTEMPTS} attempts"
    ) from last_exc
