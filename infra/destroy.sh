#!/bin/bash
# Tear down all AWS resources created for the k8s-deep-agent demo.
#
# Resources deleted (in safe dependency order):
#   1. CloudWatch alarm
#   2. SNS subscription + topic
#   3. Lambda function + log group
#   4. Lambda IAM role (both policies detached)
#   5. IRSA IAM role + CloudWatch policy
#   6. CloudWatch Container Insights log groups
#   7. EKS cluster (eksctl — deletes VPC, node groups, load balancers, Lambda SG)
#
# Usage:
#   AWS_PROFILE=fernhub bash infra/destroy.sh
#
# Dry-run (prints what would be deleted, deletes nothing):
#   DRY_RUN=true AWS_PROFILE=fernhub bash infra/destroy.sh

set -euo pipefail

export AWS_PROFILE="${AWS_PROFILE:-fernhub}"
REGION="ap-southeast-2"
CLUSTER="otel-demo-prod"
ALARM_NAME="EKS-NodeCPUPressure-${CLUSTER}"
SNS_TOPIC_NAME="eks-cpu-pressure-alerts"
LAMBDA_NAME="eks-alarm-to-agent"
LAMBDA_SG_NAME="lambda-eks-alarm-agent"
LAMBDA_ROLE_NAME="eks-alarm-to-agent-role"
IRSA_ROLE_NAME="k8s-agent-irsa"
IRSA_POLICY_NAME="k8s-agent-cloudwatch-read"

DRY_RUN="${DRY_RUN:-false}"

# ── Helpers ────────────────────────────────────────────────────────────────────
run() {
  if [[ "$DRY_RUN" == "true" ]]; then
    echo "    [DRY RUN] $*"
  else
    "$@"
  fi
}

check() { "$@" &>/dev/null; }

# ── Preflight ──────────────────────────────────────────────────────────────────
echo "==> Verifying AWS credentials..."
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text --region "$REGION")
echo "    Account: $ACCOUNT_ID  Region: $REGION"
[[ "$DRY_RUN" == "true" ]] && echo "    *** DRY RUN — nothing will be deleted ***"
echo ""

# ── Step 1: CloudWatch Alarm ───────────────────────────────────────────────────
echo "==> Step 1: Deleting CloudWatch alarm '$ALARM_NAME'..."
if check aws cloudwatch describe-alarms --alarm-names "$ALARM_NAME" --region "$REGION" \
    --query 'MetricAlarms[0].AlarmName' --output text; then
  run aws cloudwatch delete-alarms --alarm-names "$ALARM_NAME" --region "$REGION"
  echo "    ✓ Alarm deleted"
else
  echo "    ✓ Alarm not found — skipped"
fi

# ── Step 2: SNS Subscriptions + Topic ─────────────────────────────────────────
echo ""
echo "==> Step 2: Deleting SNS topic '$SNS_TOPIC_NAME'..."
SNS_TOPIC_ARN="arn:aws:sns:${REGION}:${ACCOUNT_ID}:${SNS_TOPIC_NAME}"
if check aws sns get-topic-attributes --topic-arn "$SNS_TOPIC_ARN" --region "$REGION"; then
  SUBS=$(aws sns list-subscriptions-by-topic \
    --topic-arn "$SNS_TOPIC_ARN" --region "$REGION" \
    --query 'Subscriptions[*].SubscriptionArn' --output text 2>/dev/null || true)
  for sub in $SUBS; do
    [[ "$sub" == "PendingConfirmation" ]] && continue
    run aws sns unsubscribe --subscription-arn "$sub" --region "$REGION"
    echo "    ✓ Unsubscribed: $sub"
  done
  run aws sns delete-topic --topic-arn "$SNS_TOPIC_ARN" --region "$REGION"
  echo "    ✓ SNS topic deleted"
else
  echo "    ✓ SNS topic not found — skipped"
fi

# ── Step 3: Lambda Function + Log Group ───────────────────────────────────────
echo ""
echo "==> Step 3: Deleting Lambda function '$LAMBDA_NAME'..."
if check aws lambda get-function --function-name "$LAMBDA_NAME" --region "$REGION"; then
  run aws lambda delete-function --function-name "$LAMBDA_NAME" --region "$REGION"
  echo "    ✓ Lambda function deleted"
else
  echo "    ✓ Lambda function not found — skipped"
fi

LAMBDA_LOG_GROUP="/aws/lambda/${LAMBDA_NAME}"
if check aws logs describe-log-groups \
    --log-group-name-prefix "$LAMBDA_LOG_GROUP" --region "$REGION" \
    --query 'logGroups[0].logGroupName' --output text; then
  run aws logs delete-log-group --log-group-name "$LAMBDA_LOG_GROUP" --region "$REGION"
  echo "    ✓ Log group deleted"
else
  echo "    ✓ Log group not found — skipped"
fi

# ── Step 3b: Lambda VPC ENI cleanup ───────────────────────────────────────────
# AWS leaves VPC-attached Lambda ENIs behind after function deletion (up to ~20 min).
# These block subnet deletion in the eksctl CloudFormation stack. We wait for them
# to detach and then delete them before the EKS stack teardown.
echo ""
echo "==> Step 3b: Cleaning up Lambda VPC ENIs for '$LAMBDA_NAME'..."
if [[ "$DRY_RUN" != "true" ]]; then
  MAX_WAIT=120
  ELAPSED=0
  while true; do
    ENIS=$(aws ec2 describe-network-interfaces \
      --filters "Name=description,Values=AWS Lambda VPC ENI-${LAMBDA_NAME}" \
      --region "$REGION" \
      --query 'NetworkInterfaces[].NetworkInterfaceId' \
      --output text 2>/dev/null || true)

    if [[ -z "$ENIS" ]]; then
      echo "    ✓ No Lambda VPC ENIs found — skipped"
      break
    fi

    IN_USE=$(aws ec2 describe-network-interfaces \
      --filters "Name=description,Values=AWS Lambda VPC ENI-${LAMBDA_NAME}" \
                "Name=status,Values=in-use" \
      --region "$REGION" \
      --query 'NetworkInterfaces[].NetworkInterfaceId' \
      --output text 2>/dev/null || true)

    if [[ -z "$IN_USE" ]]; then
      for eni in $ENIS; do
        aws ec2 delete-network-interface --network-interface-id "$eni" --region "$REGION"
        echo "    ✓ Deleted Lambda VPC ENI: $eni"
      done
      break
    fi

    if [[ $ELAPSED -ge $MAX_WAIT ]]; then
      echo "    ⚠ Timeout waiting for Lambda ENIs to detach — subnet deletion may fail"
      break
    fi

    echo "    Waiting for ENIs to detach ($ELAPSED/${MAX_WAIT}s)..."
    sleep 10
    ELAPSED=$((ELAPSED + 10))
  done
else
  echo "    [DRY RUN] Would wait for and delete Lambda VPC ENIs matching 'AWS Lambda VPC ENI-${LAMBDA_NAME}'"
fi

# Delete the Lambda security group — also blocks VPC deletion if left behind.
LAMBDA_SG_ID=$(aws ec2 describe-security-groups \
  --filters "Name=group-name,Values=${LAMBDA_SG_NAME}" \
  --region "$REGION" \
  --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || true)
if [[ -n "$LAMBDA_SG_ID" && "$LAMBDA_SG_ID" != "None" ]]; then
  run aws ec2 delete-security-group --group-id "$LAMBDA_SG_ID" --region "$REGION"
  echo "    ✓ Deleted Lambda security group: $LAMBDA_SG_ID ($LAMBDA_SG_NAME)"
else
  echo "    ✓ Lambda security group '$LAMBDA_SG_NAME' not found — skipped"
fi

# ── Step 4: Lambda IAM Role ────────────────────────────────────────────────────
echo ""
echo "==> Step 4: Deleting Lambda IAM role '$LAMBDA_ROLE_NAME'..."
if check aws iam get-role --role-name "$LAMBDA_ROLE_NAME"; then
  POLICIES=$(aws iam list-attached-role-policies \
    --role-name "$LAMBDA_ROLE_NAME" \
    --query 'AttachedPolicies[*].PolicyArn' --output text 2>/dev/null || true)
  for policy_arn in $POLICIES; do
    run aws iam detach-role-policy --role-name "$LAMBDA_ROLE_NAME" --policy-arn "$policy_arn"
    echo "    ✓ Detached: $policy_arn"
  done
  run aws iam delete-role --role-name "$LAMBDA_ROLE_NAME"
  echo "    ✓ Lambda IAM role deleted"
else
  echo "    ✓ Lambda IAM role not found — skipped"
fi

# ── Step 5: IRSA Role + Policy ────────────────────────────────────────────────
echo ""
echo "==> Step 5: Deleting IRSA role '$IRSA_ROLE_NAME'..."
if check aws iam get-role --role-name "$IRSA_ROLE_NAME"; then
  POLICIES=$(aws iam list-attached-role-policies \
    --role-name "$IRSA_ROLE_NAME" \
    --query 'AttachedPolicies[*].PolicyArn' --output text 2>/dev/null || true)
  for policy_arn in $POLICIES; do
    run aws iam detach-role-policy --role-name "$IRSA_ROLE_NAME" --policy-arn "$policy_arn"
    echo "    ✓ Detached: $policy_arn"
  done
  run aws iam delete-role --role-name "$IRSA_ROLE_NAME"
  echo "    ✓ IRSA role deleted"
else
  echo "    ✓ IRSA role not found — skipped"
fi

IRSA_POLICY_ARN="arn:aws:iam::${ACCOUNT_ID}:policy/${IRSA_POLICY_NAME}"
if check aws iam get-policy --policy-arn "$IRSA_POLICY_ARN"; then
  run aws iam delete-policy --policy-arn "$IRSA_POLICY_ARN"
  echo "    ✓ IRSA policy deleted"
else
  echo "    ✓ IRSA policy not found — skipped"
fi

# ── Step 6: Container Insights + EKS Log Groups ───────────────────────────────
echo ""
echo "==> Step 6: Deleting CloudWatch log groups for cluster '$CLUSTER'..."
LOG_PREFIXES=(
  "/aws/containerinsights/${CLUSTER}/application"
  "/aws/containerinsights/${CLUSTER}/dataplane"
  "/aws/containerinsights/${CLUSTER}/host"
  "/aws/containerinsights/${CLUSTER}/performance"
  "/aws/eks/${CLUSTER}/cluster"
)
for lg in "${LOG_PREFIXES[@]}"; do
  if check aws logs describe-log-groups \
      --log-group-name-prefix "$lg" --region "$REGION" \
      --query 'logGroups[0].logGroupName' --output text; then
    run aws logs delete-log-group --log-group-name "$lg" --region "$REGION"
    echo "    ✓ Deleted: $lg"
  else
    echo "    ✓ Not found: $lg — skipped"
  fi
done

# ── Step 7: EKS Cluster ────────────────────────────────────────────────────────
echo ""
echo "==> Step 7: Deleting EKS cluster '$CLUSTER'..."
echo "    This deletes the cluster, node groups, VPC, subnets, security groups"
echo "    (including the Lambda SG), load balancers, and CloudFormation stacks."
echo "    Expected time: 10-15 minutes."
echo ""

CF_STACK="eksctl-${CLUSTER}-cluster"
CF_STATUS=$(aws cloudformation describe-stacks \
  --stack-name "$CF_STACK" --region "$REGION" \
  --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo "NOT_FOUND")

if [[ "$CF_STATUS" == "DELETE_FAILED" ]]; then
  echo "    ⚠ CloudFormation stack '$CF_STACK' is in DELETE_FAILED state."
  echo "      This is usually caused by orphaned Lambda VPC ENIs (Step 3b should have"
  echo "      cleared them). Retrying stack deletion now..."
  if [[ "$DRY_RUN" != "true" ]]; then
    aws cloudformation delete-stack --stack-name "$CF_STACK" --region "$REGION"
    echo "    Waiting for stack deletion..."
    aws cloudformation wait stack-delete-complete --stack-name "$CF_STACK" --region "$REGION"
    echo "    ✓ CloudFormation stack deleted"
  else
    echo "    [DRY RUN] aws cloudformation delete-stack --stack-name $CF_STACK --region $REGION"
  fi
elif check aws eks describe-cluster --name "$CLUSTER" --region "$REGION"; then
  if [[ "$DRY_RUN" == "true" ]]; then
    echo "    [DRY RUN] eksctl delete cluster --name $CLUSTER --region $REGION --wait"
  else
    eksctl delete cluster --name "$CLUSTER" --region "$REGION" --wait
    echo "    ✓ EKS cluster deleted"
  fi
else
  echo "    ✓ EKS cluster not found — skipped"
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "✅ Destroy complete. All demo resources removed from account $ACCOUNT_ID."
echo ""
echo "Verify nothing remains:"
echo "  aws eks list-clusters --region $REGION"
echo "  aws cloudwatch describe-alarms --alarm-names '$ALARM_NAME' --region $REGION"
echo "  aws sns list-topics --region $REGION"
echo "  aws lambda get-function --function-name $LAMBDA_NAME --region $REGION"
echo "  aws iam get-role --role-name $IRSA_ROLE_NAME"
