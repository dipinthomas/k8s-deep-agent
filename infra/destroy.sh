#!/bin/bash
# Tear down all AWS resources created for the k8s-deep-agent demo.
#
# Resources deleted (in safe dependency order):
#   1. CloudWatch alarm
#   2. SNS subscription + topic
#   3. Lambda function + log group
#   4. IAM role (detach policies first)
#   5. CloudWatch Container Insights log groups
#   6. EKS cluster (eksctl — deletes VPC, node groups, load balancers)
#
# Usage:
#   AWS_PROFILE=fernhub bash infra/destroy.sh
#
# Dry-run (prints what would be deleted, deletes nothing):
#   DRY_RUN=true AWS_PROFILE=fernhub bash infra/destroy.sh

set -euo pipefail

REGION="ap-southeast-2"
CLUSTER="otel-demo-prod"
ALARM_NAME="EKS-NodeDiskPressure-${CLUSTER}"
SNS_TOPIC_NAME="eks-disk-pressure-alerts"
LAMBDA_NAME="eks-alarm-to-slack"
LAMBDA_ROLE_NAME="eks-alarm-to-slack-role"

DRY_RUN="${DRY_RUN:-false}"

# ── Helpers ────────────────────────────────────────────────────────────────────
run() {
  if [[ "$DRY_RUN" == "true" ]]; then
    echo "    [DRY RUN] $*"
  else
    "$@"
  fi
}

check() {
  # Returns 0 if resource exists, 1 if not found
  "$@" &>/dev/null
}

# ── Preflight ──────────────────────────────────────────────────────────────────
echo "==> Verifying AWS credentials..."
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text --region "$REGION")
echo "    Account: $ACCOUNT_ID  Region: $REGION"
[[ "$DRY_RUN" == "true" ]] && echo "    *** DRY RUN — nothing will be deleted ***"
echo ""

# ── Step 1: CloudWatch Alarm ───────────────────────────────────────────────────
echo "==> Step 1: Deleting CloudWatch alarm '$ALARM_NAME'..."
if check aws cloudwatch describe-alarms --alarm-names "$ALARM_NAME" --region "$REGION" --query 'MetricAlarms[0].AlarmName' --output text; then
  run aws cloudwatch delete-alarms \
    --alarm-names "$ALARM_NAME" \
    --region "$REGION"
  echo "    ✓ Alarm deleted"
else
  echo "    ✓ Alarm not found — skipped"
fi

# ── Step 2: SNS Subscriptions + Topic ─────────────────────────────────────────
echo ""
echo "==> Step 2: Deleting SNS topic '$SNS_TOPIC_NAME'..."
SNS_TOPIC_ARN="arn:aws:sns:${REGION}:${ACCOUNT_ID}:${SNS_TOPIC_NAME}"
if check aws sns get-topic-attributes --topic-arn "$SNS_TOPIC_ARN" --region "$REGION"; then
  # List and delete all subscriptions first
  SUBS=$(aws sns list-subscriptions-by-topic \
    --topic-arn "$SNS_TOPIC_ARN" \
    --region "$REGION" \
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

# ── Step 3: Lambda Function ────────────────────────────────────────────────────
echo ""
echo "==> Step 3: Deleting Lambda function '$LAMBDA_NAME'..."
if check aws lambda get-function --function-name "$LAMBDA_NAME" --region "$REGION"; then
  run aws lambda delete-function \
    --function-name "$LAMBDA_NAME" \
    --region "$REGION"
  echo "    ✓ Lambda function deleted"
else
  echo "    ✓ Lambda function not found — skipped"
fi

echo "==> Deleting Lambda log group..."
LAMBDA_LOG_GROUP="/aws/lambda/${LAMBDA_NAME}"
if check aws logs describe-log-groups --log-group-name-prefix "$LAMBDA_LOG_GROUP" --region "$REGION" --query 'logGroups[0].logGroupName' --output text; then
  run aws logs delete-log-group \
    --log-group-name "$LAMBDA_LOG_GROUP" \
    --region "$REGION"
  echo "    ✓ Log group $LAMBDA_LOG_GROUP deleted"
else
  echo "    ✓ Log group not found — skipped"
fi

# ── Step 4: IAM Role ───────────────────────────────────────────────────────────
echo ""
echo "==> Step 4: Deleting IAM role '$LAMBDA_ROLE_NAME'..."
if check aws iam get-role --role-name "$LAMBDA_ROLE_NAME"; then
  # Detach all managed policies first
  POLICIES=$(aws iam list-attached-role-policies \
    --role-name "$LAMBDA_ROLE_NAME" \
    --query 'AttachedPolicies[*].PolicyArn' --output text 2>/dev/null || true)
  for policy_arn in $POLICIES; do
    run aws iam detach-role-policy \
      --role-name "$LAMBDA_ROLE_NAME" \
      --policy-arn "$policy_arn"
    echo "    ✓ Detached policy: $policy_arn"
  done
  run aws iam delete-role --role-name "$LAMBDA_ROLE_NAME"
  echo "    ✓ IAM role deleted"
else
  echo "    ✓ IAM role not found — skipped"
fi

# ── Step 5: Container Insights + EKS Log Groups ───────────────────────────────
echo ""
echo "==> Step 5: Deleting CloudWatch log groups for cluster '$CLUSTER'..."
LOG_PREFIXES=(
  "/aws/containerinsights/${CLUSTER}/application"
  "/aws/containerinsights/${CLUSTER}/dataplane"
  "/aws/containerinsights/${CLUSTER}/host"
  "/aws/containerinsights/${CLUSTER}/performance"
  "/aws/eks/${CLUSTER}/cluster"
)
for lg in "${LOG_PREFIXES[@]}"; do
  if check aws logs describe-log-groups \
    --log-group-name-prefix "$lg" \
    --region "$REGION" \
    --query 'logGroups[0].logGroupName' --output text; then
    run aws logs delete-log-group \
      --log-group-name "$lg" \
      --region "$REGION"
    echo "    ✓ Deleted: $lg"
  else
    echo "    ✓ Not found: $lg — skipped"
  fi
done

# ── Step 6: EKS Cluster ────────────────────────────────────────────────────────
echo ""
echo "==> Step 6: Deleting EKS cluster '$CLUSTER'..."
echo "    This will delete the cluster, node groups, VPC, subnets,"
echo "    security groups, load balancers, and all associated CloudFormation stacks."
echo "    Expected time: 10-15 minutes."
echo ""

if check aws eks describe-cluster --name "$CLUSTER" --region "$REGION"; then
  if [[ "$DRY_RUN" == "true" ]]; then
    echo "    [DRY RUN] eksctl delete cluster --name $CLUSTER --region $REGION --wait"
  else
    eksctl delete cluster \
      --name "$CLUSTER" \
      --region "$REGION" \
      --wait
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
echo "  aws sns list-topics --region $REGION --query \"Topics[?contains(TopicArn,'eks-disk-pressure')]\""
echo "  aws lambda get-function --function-name $LAMBDA_NAME --region $REGION"
