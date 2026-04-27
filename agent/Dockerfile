FROM python:3.11-slim

# ── System deps ────────────────────────────────────────────────────────────────
# curl + ca-certificates: kubectl download, OIDC token fetch
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ── kubectl (for fallback / troubleshooting) ───────────────────────────────────
RUN curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" \
    && chmod +x kubectl && mv kubectl /usr/local/bin/

# ── Python application deps ────────────────────────────────────────────────────
WORKDIR /app

COPY agent/requirements.txt agent/requirements.txt
RUN pip install --no-cache-dir -r agent/requirements.txt

# ── Application code ───────────────────────────────────────────────────────────
# Copy AGENTS.md and skills so the agent can read them at runtime
COPY AGENTS.md .
COPY skills/ skills/
COPY agent/ agent/

# agent/ must be on PYTHONPATH for intra-package imports (mcp.mcp_client, etc.)
ENV PYTHONPATH=/app/agent

CMD ["python", "agent/main.py"]
