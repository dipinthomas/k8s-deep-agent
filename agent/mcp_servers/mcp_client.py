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
"""

import asyncio
import logging
import os
import threading
from typing import Any

from langchain_mcp_adapters.client import MultiServerMCPClient

logger = logging.getLogger(__name__)

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


def _load_tools_sync() -> list[Any]:
    """
    Load MCP tools synchronously by running an event loop to completion.
    MultiServerMCPClient.get_tools() opens sessions, fetches the tool list,
    and cleanly closes them — all within a single asyncio.run() call.
    """
    async def _fetch():
        client = MultiServerMCPClient(_SERVER_CONFIG)
        return await client.get_tools()

    return asyncio.run(_fetch())


_all_tools: list[Any] = []
_tools_lock = threading.Lock()
_tools_loaded = False


def get_mcp_tools() -> list[Any]:
    """
    Return cached MCP tools, loading them on first call (blocking).

    Tool schemas are fetched once at startup. The MCP gateway serves them
    statelessly — each get_tools() call opens a fresh session, fetches the list,
    and closes it cleanly.
    """
    global _all_tools, _tools_loaded
    if not _tools_loaded:
        with _tools_lock:
            if not _tools_loaded:
                logger.info("Loading MCP tools from gateway...")
                _all_tools = _load_tools_sync()
                _tools_loaded = True
                logger.info(
                    "MCP ready — %d tools loaded: %s",
                    len(_all_tools),
                    [t.name for t in _all_tools],
                )
    return list(_all_tools)
