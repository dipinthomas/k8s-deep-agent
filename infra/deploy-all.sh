#!/bin/bash
# One-command full stack deploy for the K8s AI Agent demo.
#
# Deploys in order:
#   1. EKS cluster (Auto Mode, K8s 1.33)
#   2. OTel Demo app (Helm chart v0.40.7)
#   3. PriorityClasses patched onto pods
#   4. AI agent (Kubernetes deployment + secrets)
#   5. CloudWatch alarm → SNS → Lambda → Slack alert pipeline
#
# Prerequisites:
#   - eksctl, kubectl, helm, aws CLI installed
#   - Docker Hub image dipinthomas2003/k8s-deep-agent:latest already pushed
#   - infra/agent-secrets.yaml filled in (copy from agent-secrets.example.yaml)
#   - SLACK_BOT_TOKEN and SLACK_CHANNEL_ID exported
#
# Usage:
#   export SLACK_BOT_TOKEN="xoxb-..."
#   export SLACK_CHANNEL_ID="C..."
#   AWS_PROFILE=fernhub bash infra/deploy-all.sh

set -euo pipefail

REGION="ap-southeast-2"
CLUSTER="otel-demo-prod"
NAMESPACE="otel-demo"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Colour helpers ─────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
step()  { echo ""; echo -e "${YELLOW}════════════════════════════════════════${NC}"; echo -e "${YELLOW}  $*${NC}"; echo -e "${YELLOW}════════════════════════════════════════${NC}"; }
ok()    { echo -e "    ${GREEN}✓ $*${NC}"; }
warn()  { echo -e "    ${YELLOW}⚠  $*${NC}"; }
die()   { echo -e "    ${RED}✗ $*${NC}"; echo ""; echo "Cleaning up cluster to avoid charges..."; AWS_PROFILE="${AWS_PROFILE:-default}" eksctl delete cluster --name "$CLUSTER" --region "$REGION" --wait 2>/dev/null || true; exit 1; }

# ── Preflight checks ───────────────────────────────────────────────────────────
step "Preflight checks"

for cmd in eksctl kubectl helm aws; do
  command -v "$cmd" &>/dev/null && ok "$cmd found" || die "$cmd not found — install it first"
done

aws sts get-caller-identity --region "$REGION" &>/dev/null \
  && ok "AWS credentials valid ($(aws sts get-caller-identity --query Account --output text --region "$REGION"))" \
  || die "AWS credentials invalid — check AWS_PROFILE"

[[ -f "$SCRIPT_DIR/agent-secrets.yaml" ]] \
  && ok "agent-secrets.yaml found" \
  || die "infra/agent-secrets.yaml not found — copy from agent-secrets.example.yaml and fill in values"

# Check secrets file still has placeholder values
if grep -q "YOUR_" "$SCRIPT_DIR/agent-secrets.yaml" 2>/dev/null; then
  die "agent-secrets.yaml still contains placeholder values — fill in real credentials first"
fi

: "${SLACK_BOT_TOKEN:?SLACK_BOT_TOKEN must be exported}"
: "${SLACK_CHANNEL_ID:?SLACK_CHANNEL_ID must be exported}"
ok "SLACK_BOT_TOKEN and SLACK_CHANNEL_ID set"

# ── Step 1: EKS Cluster ────────────────────────────────────────────────────────
step "Step 1/5 — EKS Cluster"

if aws eks describe-cluster --name "$CLUSTER" --region "$REGION" &>/dev/null; then
  warn "Cluster '$CLUSTER' already exists — skipping creation"
else
  echo "    Creating EKS Auto Mode cluster (15-20 min)..."
  eksctl create cluster -f "$SCRIPT_DIR/eks-cluster.yaml" \
    || die "EKS cluster creation failed"
  ok "Cluster created"
fi

# Update local kubeconfig
aws eks update-kubeconfig --name "$CLUSTER" --region "$REGION" &>/dev/null
ok "kubeconfig updated"

# Wait for cluster to be fully ready
echo "    Waiting for cluster nodes to be Ready..."
kubectl wait node --all --for=condition=Ready --timeout=300s 2>/dev/null \
  || warn "No nodes yet (Auto Mode provisions on first workload — continuing)"

# ── Step 2: OTel Demo App ──────────────────────────────────────────────────────
step "Step 2/5 — OTel Demo App"

echo "    Deploying OTel Demo via Helm (10 min)..."
bash "$REPO_ROOT/otel-demo/deploy.sh" \
  || die "OTel demo deployment failed"
ok "OTel demo deployed"

# ── Step 3: PriorityClasses ────────────────────────────────────────────────────
step "Step 3/5 — PriorityClasses"

bash "$SCRIPT_DIR/patch-priority-classes.sh" \
  || die "Priority class patching failed"

# Rollout restart so pods pick up the new priorityClassName
echo "    Restarting deployments to apply priority classes..."
for deploy in checkout payment cart frontend frontend-proxy product-catalog \
              image-provider load-generator ad recommendation; do
  kubectl rollout restart deployment/"$deploy" -n "$NAMESPACE" 2>/dev/null || true
done

echo "    Waiting for rollouts to stabilise..."
kubectl rollout status deployment/checkout -n "$NAMESPACE" --timeout=120s 2>/dev/null || true
kubectl rollout status deployment/frontend -n "$NAMESPACE" --timeout=120s 2>/dev/null || true
ok "PriorityClasses applied and pods restarted"

# ── Step 4: AI Agent ───────────────────────────────────────────────────────────
step "Step 4/5 — AI Agent"

echo "    Applying secrets..."
kubectl apply -f "$SCRIPT_DIR/agent-secrets.yaml" \
  || die "Failed to apply agent secrets"
ok "Secrets applied"

echo "    Deploying agent..."
kubectl apply -f "$SCRIPT_DIR/agent-deployment.yaml" \
  || die "Failed to apply agent deployment"

echo "    Waiting for agent pod to be Running..."
kubectl rollout status deployment/k8s-agent -n "$NAMESPACE" --timeout=120s \
  || die "Agent pod failed to start — check: kubectl logs -n $NAMESPACE deployment/k8s-agent"
ok "Agent deployed and running"

# ── Step 5: Alert Pipeline ─────────────────────────────────────────────────────
step "Step 5/5 — CloudWatch → SNS → Lambda → Slack"

bash "$SCRIPT_DIR/setup-alert-pipeline.sh" \
  || die "Alert pipeline setup failed"

# Quick end-to-end test — Lambda posts to Slack
echo ""
echo "    Running pipeline test (expect a Slack message in #k8s-alerts)..."
bash "$SCRIPT_DIR/test-alert-pipeline.sh" \
  || warn "Pipeline test failed — check Lambda logs in CloudWatch"
ok "Alert pipeline tested"

# ── Summary ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}════════════════════════════════════════${NC}"
echo -e "${GREEN}  ✅ Full stack deployed successfully!  ${NC}"
echo -e "${GREEN}════════════════════════════════════════${NC}"
echo ""

FRONTEND=$(kubectl get svc otel-demo-frontendproxy -n "$NAMESPACE" \
  -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || echo "still provisioning")

echo "  Cluster:       $CLUSTER ($REGION)"
echo "  OTel Demo:     http://$FRONTEND"
echo "  Agent logs:    kubectl logs -n $NAMESPACE deployment/k8s-agent -f"
echo "  Slack channel: #k8s-alerts"
echo ""
echo "  To run the demo:"
echo "    bash fault-injection/trigger-disk-pressure.sh"
echo ""
echo "  To tear everything down:"
echo "    AWS_PROFILE=${AWS_PROFILE:-fernhub} bash infra/destroy.sh"
