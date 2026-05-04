#!/bin/bash
# One-command deploy for the k8s-deep-agent stack.
#
# Architecture:
#   Two CloudFormation stacks own AWS resources. Kubernetes manifests
#   are applied via kubectl after the cluster stack creates the cluster.
#
#     Stack 1 (cluster)    VPC, EKS Auto Mode 1.33, OIDC provider,
#                          CloudWatch Observability addon (Pod Identity).
#     Stack 2 (agent-iam)  IRSA role for the agent ServiceAccount.
#
# K8s side (kubectl apply): priority classes, Container Insights ConfigMap,
# Redis, agent secrets, agent + MCP gateway deployments.
#
# Idempotent: rerunning applies stack updates and re-syncs K8s manifests.
#
# Prerequisites:
#   - aws, kubectl, curl installed
#   - infra/agent-secrets.yaml filled in (copy from agent-secrets.example.yaml)
#   - AWS SSO session active: aws sso login --profile fernhub
#   - Container images pushed to dipinthomas2003/k8s-deep-agent:<tag> already
#
# Usage:
#   bash infra/deploy.sh

set -euo pipefail

export AWS_PROFILE="${AWS_PROFILE:-fernhub}"
REGION="us-east-1"
CLUSTER="otel-demo-prod"
AGENT_NAMESPACE="k8s-agent"
# Toggle Container Insights metrics + Fluent Bit log shipping. Off by default
# (saves ~$5-20/day in CloudWatch ingest cost on an idle cluster). Turn on
# before running a demo: INSTALL_OBSERVABILITY=true bash infra/deploy.sh
INSTALL_OBSERVABILITY="${INSTALL_OBSERVABILITY:-false}"

CLUSTER_STACK="k8s-agent-cluster"
IAM_STACK="k8s-agent-iam"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CFN_DIR="$SCRIPT_DIR/cloudformation"
SECRETS_FILE="$SCRIPT_DIR/agent-secrets.yaml"

# ── Colour helpers ────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
step() { echo ""; echo -e "${YELLOW}════════════════════════════════════════${NC}"; echo -e "${YELLOW}  $*${NC}"; echo -e "${YELLOW}════════════════════════════════════════${NC}"; }
ok()   { echo -e "    ${GREEN}✓ $*${NC}"; }
warn() { echo -e "    ${YELLOW}⚠  $*${NC}"; }
die()  { echo -e "    ${RED}✗ $*${NC}"; echo ""; exit 1; }

# ── Preflight ─────────────────────────────────────────────────────────────────
step "Preflight"

for cmd in aws kubectl curl; do
  command -v "$cmd" &>/dev/null && ok "$cmd found" || die "$cmd not found — install it first"
done

aws sts get-caller-identity --region "$REGION" &>/dev/null \
  || die "AWS credentials invalid — check AWS_PROFILE=$AWS_PROFILE"

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text --region "$REGION")
CALLER_ARN=$(aws sts get-caller-identity --query Arn --output text --region "$REGION")
ok "Account: $ACCOUNT_ID  Caller: $CALLER_ARN"

[[ -f "$SECRETS_FILE" ]] || die "infra/agent-secrets.yaml not found — copy from agent-secrets.example.yaml and fill in values"
if grep -q "xoxb-\.\.\.\|xapp-\.\.\.\|hooks\.slack\.com/services/\.\.\." "$SECRETS_FILE" 2>/dev/null; then
  die "agent-secrets.yaml still contains placeholder values — fill in real credentials first"
fi
SLACK_WEBHOOK_URL=$(grep "SLACK_WEBHOOK_URL:" "$SECRETS_FILE" | head -1 | sed 's/.*: *"\(.*\)"/\1/')
[[ -n "$SLACK_WEBHOOK_URL" ]] || die "SLACK_WEBHOOK_URL not found in agent-secrets.yaml"
ok "agent-secrets.yaml validated"

# ── Helper: deploy a CFN stack ────────────────────────────────────────────────
# Wraps `aws cloudformation deploy` with friendly output and parameter overrides.
# Auto-waits if a previous run left the stack mid-operation, and recovers from
# rollback/failed states by deleting-then-recreating so reruns Just Work.
deploy_stack() {
  local stack_name="$1"
  local template_file="$2"
  shift 2
  local params=("$@")
  local pflag=()
  if [[ ${#params[@]} -gt 0 ]]; then
    pflag=(--parameter-overrides "${params[@]}")
  fi

  # Handle whatever state the stack happens to be in from a previous run.
  local status
  status=$(aws cloudformation describe-stacks \
    --stack-name "$stack_name" --region "$REGION" \
    --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo "NOT_FOUND")

  case "$status" in
    CREATE_IN_PROGRESS)
      warn "Stack '$stack_name' still creating from a previous run — waiting..."
      aws cloudformation wait stack-create-complete --stack-name "$stack_name" --region "$REGION" \
        || die "Previous create failed — check CloudFormation console"
      ok "Previous create finished"
      ;;
    UPDATE_IN_PROGRESS|UPDATE_COMPLETE_CLEANUP_IN_PROGRESS)
      warn "Stack '$stack_name' still updating from a previous run — waiting..."
      aws cloudformation wait stack-update-complete --stack-name "$stack_name" --region "$REGION" \
        || die "Previous update failed — check CloudFormation console"
      ok "Previous update finished"
      ;;
    DELETE_IN_PROGRESS)
      warn "Stack '$stack_name' is deleting — waiting before recreate..."
      aws cloudformation wait stack-delete-complete --stack-name "$stack_name" --region "$REGION" \
        || die "Previous delete failed — check CloudFormation console"
      ;;
    ROLLBACK_IN_PROGRESS|UPDATE_ROLLBACK_IN_PROGRESS)
      warn "Stack '$stack_name' is rolling back — waiting..."
      aws cloudformation wait stack-rollback-complete --stack-name "$stack_name" --region "$REGION" 2>/dev/null \
        || aws cloudformation wait stack-update-rollback-complete --stack-name "$stack_name" --region "$REGION" 2>/dev/null \
        || true
      # fall through to handle the post-rollback state on the next call
      status=$(aws cloudformation describe-stacks \
        --stack-name "$stack_name" --region "$REGION" \
        --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo "NOT_FOUND")
      ;;
  esac

  # A failed-create stack can't be updated — it must be deleted first.
  if [[ "$status" == "ROLLBACK_COMPLETE" || "$status" == "CREATE_FAILED" ]]; then
    warn "Stack '$stack_name' is in $status — deleting before recreate"
    aws cloudformation delete-stack --stack-name "$stack_name" --region "$REGION"
    aws cloudformation wait stack-delete-complete --stack-name "$stack_name" --region "$REGION" \
      || die "Failed to delete '$stack_name' — check CloudFormation console"
  fi

  echo "    Deploying stack '$stack_name'..."
  aws cloudformation deploy \
    --stack-name "$stack_name" \
    --template-file "$template_file" \
    --capabilities CAPABILITY_NAMED_IAM \
    --region "$REGION" \
    "${pflag[@]}" \
    --no-fail-on-empty-changeset
}

stack_output() {
  aws cloudformation describe-stacks \
    --stack-name "$1" --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" \
    --output text 2>/dev/null
}

# ── Step 1: Cluster stack ─────────────────────────────────────────────────────
step "Step 1/3 — Cluster (VPC + EKS Auto Mode + OIDC + Container Insights addon)"
echo "    First-time creation: 15-20 min. Updates: usually < 1 min."

deploy_stack "$CLUSTER_STACK" "$CFN_DIR/cluster.yaml" \
  "ClusterName=$CLUSTER" \
  "InstallObservability=$INSTALL_OBSERVABILITY" \
  || die "Cluster stack deployment failed — check CloudFormation console"
ok "Cluster stack deployed (observability addon: $INSTALL_OBSERVABILITY)"

aws eks update-kubeconfig --name "$CLUSTER" --region "$REGION" &>/dev/null
ok "kubeconfig updated"

# Auto Mode provisions nodes on first workload, so we don't wait on `kubectl
# wait node` here — kubectl rollout status further down will pull nodes up.

# ── Step 2: Agent IAM stack ───────────────────────────────────────────────────
step "Step 2/3 — Agent IAM (IRSA role)"

deploy_stack "$IAM_STACK" "$CFN_DIR/agent-iam.yaml" \
  "ClusterStackName=$CLUSTER_STACK" \
  || die "Agent IAM stack deployment failed"
AGENT_ROLE_ARN=$(stack_output "$IAM_STACK" RoleArn)
ok "IRSA role: $AGENT_ROLE_ARN"

# ── Step 3: Kubernetes manifests ──────────────────────────────────────────────
step "Step 3/3 — Kubernetes manifests"

# PriorityClasses are cluster-scoped and referenced by the agent + MCP pods.
kubectl apply -f "$SCRIPT_DIR/priority-classes.yaml" \
  || die "Failed to apply priority classes"
ok "Priority classes applied"

# CloudWatch Container Insights ConfigMap — tunes disk metric collection.
# Only meaningful when the observability addon is installed; the namespace
# is created by that addon.
if [[ "$INSTALL_OBSERVABILITY" == "true" ]]; then
  echo "    Waiting for amazon-cloudwatch namespace..."
  for i in $(seq 1 30); do
    kubectl get ns amazon-cloudwatch &>/dev/null && break
    sleep 5
  done
  kubectl apply -f "$SCRIPT_DIR/cloudwatch-agent.yaml" \
    && ok "Container Insights ConfigMap applied" \
    || warn "ConfigMap apply failed — addon may still be installing; rerun deploy.sh shortly"
else
  ok "Skipping Container Insights ConfigMap (INSTALL_OBSERVABILITY=false)"
fi

# Agent namespace + Redis + secrets + agent + MCP gateway.
kubectl create namespace "$AGENT_NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f "$SCRIPT_DIR/redis-deployment.yaml"
kubectl rollout status deployment/agent-redis -n "$AGENT_NAMESPACE" --timeout=180s \
  || die "Redis pod failed to start"
ok "Redis running"

kubectl apply -f "$SECRETS_FILE" || die "Failed to apply agent secrets"
kubectl apply -f "$SCRIPT_DIR/phoenix-deployment.yaml" || die "Failed to apply Phoenix observability"
kubectl apply -f "$SCRIPT_DIR/agent-deployment.yaml" || die "Failed to apply agent deployment"
kubectl apply -f "$SCRIPT_DIR/mcp-gateway-deployment.yaml" || die "Failed to apply MCP gateway"

kubectl rollout status deployment/k8s-mcp-gateway -n "$AGENT_NAMESPACE" --timeout=300s \
  || die "MCP gateway failed to start — kubectl logs -n $AGENT_NAMESPACE deployment/k8s-mcp-gateway"
ok "MCP gateway running"

kubectl rollout status deployment/k8s-agent -n "$AGENT_NAMESPACE" --timeout=300s \
  || die "Agent failed to start — kubectl logs -n $AGENT_NAMESPACE deployment/k8s-agent"
ok "Agent running"

# Best-effort NLB hostname lookup so the summary can show the agent URL.
# Not required for any subsequent step — alert pipeline has been removed.
AGENT_HOST=$(kubectl get svc k8s-agent -n "$AGENT_NAMESPACE" \
  -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)
AGENT_TRIGGER_URL="${AGENT_HOST:+http://${AGENT_HOST}:8080/trigger}"

PHOENIX_HOST=$(kubectl get svc phoenix -n "$AGENT_NAMESPACE" \
  -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)
PHOENIX_UI_URL="${PHOENIX_HOST:+http://${PHOENIX_HOST}:6006}"

# ── Smoke test ────────────────────────────────────────────────────────────────
echo ""
echo "    Posting Slack smoke-test message..."
curl -sS -X POST "$SLACK_WEBHOOK_URL" \
  -H 'Content-type: application/json' \
  --data '{"text":"✅ k8s-deep-agent stack deployed (CloudFormation). Trigger demo with `bash fault-injection/trigger-disk-pressure.sh`."}' \
  >/dev/null && ok "Slack webhook test posted" \
  || warn "Slack webhook test failed — verify SLACK_WEBHOOK_URL"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  ✅ Stack deployed                                     ${NC}"
echo -e "${GREEN}════════════════════════════════════════════════════════${NC}"
echo ""
echo "  Cluster:    $CLUSTER ($REGION)"
echo "  Stacks:     $CLUSTER_STACK, $IAM_STACK"
echo "  Agent URL:  ${AGENT_TRIGGER_URL:-<NLB still provisioning — re-check with: kubectl get svc k8s-agent -n $AGENT_NAMESPACE>}"
echo "  Phoenix UI: ${PHOENIX_UI_URL:-<NLB still provisioning — re-check with: kubectl get svc phoenix -n $AGENT_NAMESPACE>}"
echo ""
echo "  Logs:       kubectl logs -n $AGENT_NAMESPACE deployment/k8s-agent -f"
echo "  Trigger:    curl -X POST \$AGENT_URL -H 'content-type: application/json' -d '{}'"
echo "  Image bump: bash infra/update-agent.sh <tag>"
echo "  Tear down:  bash infra/destroy.sh"
echo ""
echo "  ── Demo workload (only when needed) ─────────────────────────"
echo "  bash otel-demo/deploy.sh"
echo "  bash infra/patch-priority-classes.sh"
