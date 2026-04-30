#!/bin/bash
# Roll out a new agent image to the running deployment without touching
# CloudFormation. Image lifecycle is independent of stack lifecycle —
# stack deploys/updates rarely; image bumps happen often.
#
# What this does:
#   1. Builds and pushes the multi-arch image (linux/amd64 + linux/arm64)
#   2. Updates infra/agent-deployment.yaml to the new tag (idempotent sed)
#   3. kubectl set image + rollout status
#
# Usage:
#   bash infra/update-agent.sh v31
#   SKIP_BUILD=true bash infra/update-agent.sh v31    # if image already pushed

set -euo pipefail

TAG="${1:-}"
[[ -z "$TAG" ]] && { echo "Usage: $0 <tag>  (e.g. v31)"; exit 1; }

export AWS_PROFILE="${AWS_PROFILE:-fernhub}"
REGION="us-east-1"
CLUSTER="otel-demo-prod"
AGENT_NAMESPACE="k8s-agent"
IMAGE="dipinthomas2003/k8s-deep-agent:${TAG}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok() { echo -e "${GREEN}✓ $*${NC}"; }
warn() { echo -e "${YELLOW}⚠  $*${NC}"; }

if [[ "${SKIP_BUILD:-false}" != "true" ]]; then
  echo "==> Building and pushing $IMAGE (linux/amd64 + linux/arm64)..."
  docker buildx build \
    --platform linux/amd64,linux/arm64 \
    --builder multiarch-builder \
    -t "$IMAGE" \
    -f "$REPO_ROOT/agent/Dockerfile" \
    --push "$REPO_ROOT"
  ok "Image pushed"
else
  warn "SKIP_BUILD=true — assuming $IMAGE is already pushed"
fi

echo "==> Updating manifest tag..."
# Replace the agent container image line. Match `image: dipinthomas2003/k8s-deep-agent:<anything>`.
sed -i.bak -E "s|(image: dipinthomas2003/k8s-deep-agent:)[A-Za-z0-9._-]+|\1${TAG}|g" \
  "$SCRIPT_DIR/agent-deployment.yaml"
rm -f "$SCRIPT_DIR/agent-deployment.yaml.bak"
ok "agent-deployment.yaml updated to $TAG"

aws eks update-kubeconfig --name "$CLUSTER" --region "$REGION" &>/dev/null

echo "==> Rolling out..."
# Use set image rather than re-applying the manifest so the rollout is atomic
# and we don't accidentally revert any kubectl-side edits to the deployment.
kubectl set image deployment/k8s-agent \
  -n "$AGENT_NAMESPACE" \
  "agent=$IMAGE" \
  --record=false
kubectl rollout status deployment/k8s-agent -n "$AGENT_NAMESPACE" --timeout=300s
ok "Agent rolled out to $TAG"

echo ""
echo "Tail logs: kubectl logs -n $AGENT_NAMESPACE deployment/k8s-agent -f"
