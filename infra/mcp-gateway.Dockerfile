FROM python:3.11-slim

# MCP tool servers used as sidecars by the agent pod (infra/agent-deployment.yaml):
#   mcp-server-kubernetes   kubectl tools (kubectl_get, kubectl_scale, ...)
#   awslabs cloudwatch MCP  CloudWatch metrics / logs / alarms tools
#   supergateway            bridges each stdio server to streamable HTTP
#
# Versions are pinned: the agent's human-in-the-loop gate (interrupt_on) matches
# tool NAMES, so a server upgrade that renames a tool would silently disarm it.
# Bump these deliberately and re-test.
ARG MCP_SERVER_KUBERNETES_VERSION=3.5.0
ARG SUPERGATEWAY_VERSION=3.4.3
ARG CLOUDWATCH_MCP_VERSION=0.0.25
# cloudwatch-mcp-server 0.0.25 does not cap its mcp dependency, and mcp 2.x removed
# FastMCP, so pin the mcp SDK it was built against.
ARG CLOUDWATCH_MCP_SDK_VERSION=1.27.0
ARG KUBECTL_VERSION=v1.36.0
ARG TARGETARCH

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates nodejs npm && rm -rf /var/lib/apt/lists/*

# kubectl: mcp-server-kubernetes spawns it as a subprocess
RUN curl -fsSLo /usr/local/bin/kubectl \
      "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl" \
    && chmod +x /usr/local/bin/kubectl

RUN npm install -g "mcp-server-kubernetes@${MCP_SERVER_KUBERNETES_VERSION}" \
                   "supergateway@${SUPERGATEWAY_VERSION}" \
    && npm cache clean --force

# Installed as a tool (not fetched by uvx at start-up), so pods start without
# internet access and always run the same version.
ENV UV_TOOL_BIN_DIR=/usr/local/bin UV_TOOL_DIR=/opt/uv-tools
RUN pip install --no-cache-dir uv \
    && uv tool install "awslabs.cloudwatch-mcp-server==${CLOUDWATCH_MCP_VERSION}" \
         --with "mcp==${CLOUDWATCH_MCP_SDK_VERSION}" \
    && /opt/uv-tools/awslabs-cloudwatch-mcp-server/bin/python -c "import awslabs.cloudwatch_mcp_server.server"

# No single entrypoint: each sidecar container sets its own command.
