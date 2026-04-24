#!/bin/bash
# Set up the full CloudWatch → SNS → Lambda → Slack alert pipeline.
#
# Run once after the EKS cluster is up. Safe to re-run — all operations
# are idempotent (uses --cli-input-json or updates if resource exists).
#
# Prerequisites:
#   - AWS CLI configured (aws sts get-caller-identity should succeed)
#   - SLACK_BOT_TOKEN and SLACK_CHANNEL_ID exported in your shell
#     (or edit the variables below directly — never commit real values)
#   - python3 and zip available on PATH
#
# Usage:
#   export SLACK_BOT_TOKEN="xoxb-..."
#   export SLACK_CHANNEL_ID="C..."
#   bash infra/setup-alert-pipeline.sh

set -euo pipefail

# ── Configuration ──────────────────────────────────────────────────────────────
REGION="ap-southeast-2"
CLUSTER="otel-demo-prod"
ALARM_NAME="EKS-NodeDiskPressure-${CLUSTER}"
SNS_TOPIC_NAME="eks-disk-pressure-alerts"
LAMBDA_NAME="eks-alarm-to-slack"
LAMBDA_ROLE_NAME="eks-alarm-to-slack-role"
NAMESPACE="otel-demo"

# Disk threshold — alarm fires when node_filesystem_utilization > this %
DISK_THRESHOLD=75

: "${SLACK_BOT_TOKEN:?SLACK_BOT_TOKEN must be set}"
: "${SLACK_CHANNEL_ID:?SLACK_CHANNEL_ID must be set}"

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text --region "$REGION")
echo "Account: $ACCOUNT_ID  Region: $REGION"
echo ""

# ── Step 1: SNS Topic ──────────────────────────────────────────────────────────
echo "==> Step 1: Creating SNS topic '$SNS_TOPIC_NAME'..."
SNS_TOPIC_ARN=$(aws sns create-topic \
  --name "$SNS_TOPIC_NAME" \
  --region "$REGION" \
  --query TopicArn --output text)
echo "    ✓ SNS topic ARN: $SNS_TOPIC_ARN"

# ── Step 2: IAM role for Lambda ────────────────────────────────────────────────
echo ""
echo "==> Step 2: Creating IAM role '$LAMBDA_ROLE_NAME'..."

TRUST_POLICY=$(cat <<'EOF'
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "lambda.amazonaws.com"},
    "Action": "sts:AssumeRole"
  }]
}
EOF
)

# Create role (ignore AlreadyExists)
aws iam create-role \
  --role-name "$LAMBDA_ROLE_NAME" \
  --assume-role-policy-document "$TRUST_POLICY" \
  --region "$REGION" \
  --output text --query Role.Arn 2>/dev/null || true

LAMBDA_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${LAMBDA_ROLE_NAME}"

# Attach basic execution policy
aws iam attach-role-policy \
  --role-name "$LAMBDA_ROLE_NAME" \
  --policy-arn "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole" 2>/dev/null || true

echo "    ✓ Role ARN: $LAMBDA_ROLE_ARN"

# ── Step 3: Package Lambda ─────────────────────────────────────────────────────
echo ""
echo "==> Step 3: Packaging Lambda function..."
LAMBDA_DIR="$(dirname "$0")/lambda"
LAMBDA_ZIP="/tmp/${LAMBDA_NAME}.zip"

cd "$LAMBDA_DIR"
zip -j "$LAMBDA_ZIP" alarm-to-slack.py
cd - >/dev/null
echo "    ✓ Packaged: $LAMBDA_ZIP"

# ── Step 4: Deploy Lambda ──────────────────────────────────────────────────────
echo ""
echo "==> Step 4: Deploying Lambda function '$LAMBDA_NAME'..."

# Wait briefly for role to propagate (IAM eventual consistency)
echo "    Waiting for IAM role to propagate..."
sleep 10

if aws lambda get-function --function-name "$LAMBDA_NAME" --region "$REGION" &>/dev/null; then
  # Function exists — update code
  echo "    Function exists, updating code..."
  LAMBDA_ARN=$(aws lambda update-function-code \
    --function-name "$LAMBDA_NAME" \
    --zip-file "fileb://$LAMBDA_ZIP" \
    --region "$REGION" \
    --query FunctionArn --output text)
else
  # Create new function
  LAMBDA_ARN=$(aws lambda create-function \
    --function-name "$LAMBDA_NAME" \
    --runtime python3.12 \
    --role "$LAMBDA_ROLE_ARN" \
    --handler alarm-to-slack.lambda_handler \
    --zip-file "fileb://$LAMBDA_ZIP" \
    --environment "Variables={SLACK_BOT_TOKEN=${SLACK_BOT_TOKEN},SLACK_CHANNEL_ID=${SLACK_CHANNEL_ID}}" \
    --timeout 30 \
    --region "$REGION" \
    --query FunctionArn --output text)
fi

# Wait for code update/creation to finish
echo "    Waiting for Lambda to be ready..."
aws lambda wait function-updated \
  --function-name "$LAMBDA_NAME" \
  --region "$REGION"

# Update env vars (covers both create path and update path)
aws lambda update-function-configuration \
  --function-name "$LAMBDA_NAME" \
  --environment "Variables={SLACK_BOT_TOKEN=${SLACK_BOT_TOKEN},SLACK_CHANNEL_ID=${SLACK_CHANNEL_ID}}" \
  --region "$REGION" \
  --output text --query FunctionArn >/dev/null

# Wait for configuration update to finish before proceeding
aws lambda wait function-updated \
  --function-name "$LAMBDA_NAME" \
  --region "$REGION"

echo "    ✓ Lambda ARN: $LAMBDA_ARN"

# ── Step 5: SNS → Lambda subscription ─────────────────────────────────────────
echo ""
echo "==> Step 5: Subscribing Lambda to SNS topic..."

# Allow SNS to invoke Lambda
aws lambda add-permission \
  --function-name "$LAMBDA_NAME" \
  --statement-id "sns-invoke" \
  --action "lambda:InvokeFunction" \
  --principal sns.amazonaws.com \
  --source-arn "$SNS_TOPIC_ARN" \
  --region "$REGION" 2>/dev/null || true

# Subscribe Lambda to SNS
SUBSCRIPTION_ARN=$(aws sns subscribe \
  --topic-arn "$SNS_TOPIC_ARN" \
  --protocol lambda \
  --notification-endpoint "$LAMBDA_ARN" \
  --region "$REGION" \
  --query SubscriptionArn --output text)
echo "    ✓ Subscription: $SUBSCRIPTION_ARN"

# ── Step 6: CloudWatch Alarm ───────────────────────────────────────────────────
echo ""
echo "==> Step 6: Creating CloudWatch alarm '$ALARM_NAME'..."
echo "    Threshold: node_filesystem_utilization > ${DISK_THRESHOLD}% for 2 consecutive minutes"

aws cloudwatch put-metric-alarm \
  --alarm-name "$ALARM_NAME" \
  --alarm-description "EKS node disk usage above ${DISK_THRESHOLD}% in cluster ${CLUSTER}" \
  --metric-name node_filesystem_utilization \
  --namespace ContainerInsights \
  --dimensions \
      Name=ClusterName,Value="$CLUSTER" \
      Name=NodeName,Value="" \
  --statistic Average \
  --period 60 \
  --threshold "$DISK_THRESHOLD" \
  --comparison-operator GreaterThanThreshold \
  --evaluation-periods 2 \
  --treat-missing-data notBreaching \
  --alarm-actions "$SNS_TOPIC_ARN" \
  --ok-actions "$SNS_TOPIC_ARN" \
  --region "$REGION" 2>/dev/null || \
aws cloudwatch put-metric-alarm \
  --alarm-name "$ALARM_NAME" \
  --alarm-description "EKS node disk usage above ${DISK_THRESHOLD}% in cluster ${CLUSTER}" \
  --metric-name node_filesystem_utilization \
  --namespace ContainerInsights \
  --statistic Average \
  --period 60 \
  --threshold "$DISK_THRESHOLD" \
  --comparison-operator GreaterThanThreshold \
  --evaluation-periods 2 \
  --treat-missing-data notBreaching \
  --alarm-actions "$SNS_TOPIC_ARN" \
  --ok-actions "$SNS_TOPIC_ARN" \
  --region "$REGION"

echo "    ✓ Alarm created"

# ── Summary ────────────────────────────────────────────────────────────────────
echo ""
echo "✅ Alert pipeline setup complete."
echo ""
echo "Pipeline:"
echo "  CloudWatch alarm ($ALARM_NAME)"
echo "    → SNS ($SNS_TOPIC_ARN)"
echo "    → Lambda ($LAMBDA_NAME)"
echo "    → Slack #k8s-alerts"
echo ""
echo "Test the pipeline (sends a fake ALARM notification to Lambda):"
echo "  bash infra/test-alert-pipeline.sh"
echo ""
echo "Verify alarm state:"
echo "  aws cloudwatch describe-alarms \\"
echo "    --alarm-names '$ALARM_NAME' \\"
echo "    --region $REGION \\"
echo "    --query 'MetricAlarms[0].{State:StateValue,Reason:StateReason}'"
