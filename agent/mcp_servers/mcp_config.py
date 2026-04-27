"""
MCP server configuration — kept for reference only.

Both MCP servers (kubectl, cloudwatch) run in the k8s-mcp-gateway pod and
are accessed via HTTP/SSE. See mcp/mcp_client.py for the connection logic
and mcp/servers.yaml for the canonical server definitions.
"""
