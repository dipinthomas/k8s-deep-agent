FROM python:3.11-slim

# Install kubectl
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && \
    curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" && \
    chmod +x kubectl && mv kubectl /usr/local/bin/ && \
    apt-get purge -y curl && apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first for better layer caching
COPY agent/requirements.txt agent/requirements.txt
RUN pip install --no-cache-dir -r agent/requirements.txt

# Copy repo contents (AGENTS.md, skills/, agent/)
COPY AGENTS.md .
COPY skills/ skills/
COPY agent/ agent/

# agent/ directory must be on PYTHONPATH for intra-package imports
ENV PYTHONPATH=/app/agent

CMD ["python", "agent/main.py"]
