FROM python:3.11-slim

# Dependencies:
#   nodejs + npm  → mcp-server-kubernetes + supergateway (stdio→SSE bridge)
#   kubectl       → required by mcp-server-kubernetes to query the K8s API
#   uv            → uvx awslabs.cloudwatch-mcp-server
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates nodejs npm && rm -rf /var/lib/apt/lists/*

# kubectl — mcp-server-kubernetes spawns this as a subprocess
RUN curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" \
    && chmod +x kubectl && mv kubectl /usr/local/bin/

# mcp-server-kubernetes: Kubernetes cluster operations via kubectl
RUN npm install -g mcp-server-kubernetes

# supergateway: bridges stdio MCP servers to HTTP/SSE
RUN npm install -g supergateway

RUN pip install --no-cache-dir uv

# Pre-warm the uvx cache so the CloudWatch MCP server starts instantly at runtime.
RUN uvx awslabs.cloudwatch-mcp-server@latest --help 2>/dev/null || true

# Entrypoint is not a single process — the K8s deployment runs sidecar containers,
# each with their own command. This image just packages the dependencies.
