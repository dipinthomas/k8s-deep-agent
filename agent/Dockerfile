FROM python:3.11-slim

# ── System deps ────────────────────────────────────────────────────────────────
# curl + ca-certificates: kubectl download, OIDC token fetch
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ── kubectl (for fallback / troubleshooting) ───────────────────────────────────
# Pinned so every build of a tag is the same. TARGETARCH is set by buildx
# (amd64 / arm64), so the same tag runs on Intel and Graviton nodes.
ARG TARGETARCH
ARG KUBECTL_VERSION=v1.36.0
RUN curl -fsSLo /usr/local/bin/kubectl \
      "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl" \
    && chmod +x /usr/local/bin/kubectl

# ── Python application deps ────────────────────────────────────────────────────
WORKDIR /app

# requirements.txt is a lockfile (generated from requirements.in with uv).
COPY agent/requirements.txt agent/requirements.txt
RUN pip install --no-cache-dir -r agent/requirements.txt

# ── Application code ───────────────────────────────────────────────────────────
# AGENTS.md and skills/ are read at runtime through the /harness/ backend route
# (HARNESS_ROOT defaults to /app). A cluster skill can be added at deploy time
# by mounting it under /app/skills/clusters/<name>/SKILL.md (see infra/deploy.sh).
COPY AGENTS.md .
COPY skills/ skills/
COPY agent/ agent/

# agent/ must be on PYTHONPATH for intra-package imports (mcp.mcp_client, etc.)
ENV PYTHONPATH=/app/agent

ARG VERSION=dev
LABEL org.opencontainers.image.title="k8s-deep-agent" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.source="https://github.com/dipinthomas/k8s-deep-agent"

CMD ["python", "agent/main.py"]
