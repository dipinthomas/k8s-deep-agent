#!/bin/bash
# Tear down all resources created by deploy.sh.
#
# Order matters: Kubernetes-managed AWS resources (NLB created by the agent's
# Service) must be deleted BEFORE the cluster stack, otherwise the load
# balancer ENIs block subnet delete and the cluster stack fails.
#
# Steps:
#   1. Delete the agent namespace (drops the LoadBalancer Service → NLB cleans up)
#   2. Delete agent-iam stack (IRSA role + policy)
#   3. Delete cluster stack (VPC, cluster, addon, OIDC, log groups)
#
# Usage:
#   AWS_PROFILE=fernhub bash infra/destroy.sh
#
# Dry-run (prints, deletes nothing):
#   DRY_RUN=true AWS_PROFILE=fernhub bash infra/destroy.sh

set -euo pipefail

export AWS_PROFILE="${AWS_PROFILE:-fernhub}"
REGION="us-east-1"
CLUSTER="otel-demo-prod"
AGENT_NAMESPACE="k8s-agent"

CLUSTER_STACK="k8s-agent-cluster"
IAM_STACK="k8s-agent-iam"

DRY_RUN="${DRY_RUN:-false}"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
step() { echo ""; echo -e "${YELLOW}════════════════════════════════════════${NC}"; echo -e "${YELLOW}  $*${NC}"; echo -e "${YELLOW}════════════════════════════════════════${NC}"; }
ok()   { echo -e "    ${GREEN}✓ $*${NC}"; }
warn() { echo -e "    ${YELLOW}⚠  $*${NC}"; }
die()  { echo -e "    ${RED}✗ $*${NC}"; echo ""; exit 1; }

run() {
  if [[ "$DRY_RUN" == "true" ]]; then
    echo "    [DRY RUN] $*"
  else
    "$@"
  fi
}

stack_exists() {
  aws cloudformation describe-stacks --stack-name "$1" --region "$REGION" &>/dev/null
}

delete_stack() {
  local stack="$1"
  if ! stack_exists "$stack"; then
    ok "Stack '$stack' not found — skipped"
    return 0
  fi
  echo "    Deleting stack '$stack'..."
  if [[ "$DRY_RUN" == "true" ]]; then
    echo "    [DRY RUN] aws cloudformation delete-stack --stack-name $stack"
    return 0
  fi
  aws cloudformation delete-stack --stack-name "$stack" --region "$REGION"
  echo "    Waiting for delete (up to 30 min)..."
  if aws cloudformation wait stack-delete-complete --stack-name "$stack" --region "$REGION"; then
    ok "Stack '$stack' deleted"
  else
    # `wait` exits non-zero if the stack ends in DELETE_FAILED. Print failed
    # resources so the operator can investigate manually.
    warn "Stack '$stack' did not finish cleanly — failed resources:"
    aws cloudformation describe-stack-events --stack-name "$stack" --region "$REGION" \
      --query 'StackEvents[?ResourceStatus==`DELETE_FAILED`].[LogicalResourceId,ResourceStatusReason]' \
      --output table 2>/dev/null || true
    return 1
  fi
}

# ── Preflight ─────────────────────────────────────────────────────────────────
step "Preflight"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text --region "$REGION")
ok "Account: $ACCOUNT_ID  Region: $REGION"
[[ "$DRY_RUN" == "true" ]] && warn "DRY RUN — nothing will be deleted"

# ── Step 1: Drop K8s-managed AWS resources ────────────────────────────────────
# The agent's Service type=LoadBalancer creates an NLB whose ENIs are NOT
# tracked by the cluster CFN stack. We delete the namespace (or just the
# Service) so the AWS Load Balancer Controller cleans up the NLB BEFORE the
# cluster stack tries to delete the subnets.
step "Step 1/3 — Drop K8s-managed AWS resources"

if ! stack_exists "$CLUSTER_STACK"; then
  ok "Cluster stack already gone — skipping K8s cleanup"
elif ! command -v kubectl &>/dev/null; then
  warn "kubectl not installed — skipping K8s cleanup (may leave orphan NLBs)"
else
  aws eks update-kubeconfig --name "$CLUSTER" --region "$REGION" &>/dev/null || true

  if kubectl get ns "$AGENT_NAMESPACE" &>/dev/null; then
    echo "    Deleting LoadBalancer Service to release NLB..."
    run kubectl delete svc k8s-agent -n "$AGENT_NAMESPACE" --ignore-not-found=true --timeout=120s || true

    echo "    Deleting agent namespace..."
    run kubectl delete namespace "$AGENT_NAMESPACE" --ignore-not-found=true --timeout=300s || true
    ok "Agent namespace removed"
  else
    ok "Agent namespace already gone"
  fi

  # Belt-and-braces: any leftover ELBs/ALBs/NLBs in the cluster VPC. AWS Load
  # Balancer Controller usually cleans these up when the Service is deleted,
  # but if the controller is unhealthy they linger and block VPC delete.
  CLUSTER_VPC=$(aws eks describe-cluster --name "$CLUSTER" --region "$REGION" \
    --query 'cluster.resourcesVpcConfig.vpcId' --output text 2>/dev/null || echo "")
  if [[ -n "$CLUSTER_VPC" && "$CLUSTER_VPC" != "None" ]]; then
    CLB_NAMES=$(aws elb describe-load-balancers --region "$REGION" \
      --query "LoadBalancerDescriptions[?VPCId==\`${CLUSTER_VPC}\`].LoadBalancerName" \
      --output text 2>/dev/null || true)
    for lb in $CLB_NAMES; do
      [[ -z "$lb" ]] && continue
      run aws elb delete-load-balancer --load-balancer-name "$lb" --region "$REGION" || true
      ok "Deleted classic ELB: $lb"
    done
    ALB_ARNS=$(aws elbv2 describe-load-balancers --region "$REGION" \
      --query "LoadBalancers[?VpcId==\`${CLUSTER_VPC}\`].LoadBalancerArn" \
      --output text 2>/dev/null || true)
    for arn in $ALB_ARNS; do
      [[ -z "$arn" ]] && continue
      run aws elbv2 delete-load-balancer --load-balancer-arn "$arn" --region "$REGION" || true
      ok "Deleted ALB/NLB"
    done
  fi
fi

# ── Step 2: Agent IAM stack ───────────────────────────────────────────────────
step "Step 2/3 — Agent IAM stack"
delete_stack "$IAM_STACK"

# ── Step 3: Cluster stack ─────────────────────────────────────────────────────
step "Step 3/3 — Cluster stack (VPC, EKS, addon, OIDC) — 10-15 min"
delete_stack "$CLUSTER_STACK"

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  ✅ Destroy complete                                   ${NC}"
echo -e "${GREEN}════════════════════════════════════════════════════════${NC}"
echo ""
echo "  Verify:"
echo "    aws cloudformation list-stacks --region $REGION --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE | grep k8s-agent || echo 'all gone'"
echo "    aws eks list-clusters --region $REGION"
