#!/bin/bash
# One-command deploy for the K8s AI Agent stack (no demo workload).
#
# Deploys in order:
#   1. EKS cluster (Auto Mode, K8s 1.33) + CloudWatch Container Insights config
#   2. Redis (for agent checkpoint + investigation state)
#   3. AI agent (IRSA + K8s deployment + secrets)
#   4. Alert pipeline (CloudWatch disk alarm → SNS → Lambda → Agent /trigger)
#
# The OTel demo workload is NOT installed by this script. Deploy it separately
# only when needed for the on-stage demo:
#   bash otel-demo/deploy.sh
#   bash infra/patch-priority-classes.sh
#
# Prerequisites:
#   - eksctl, kubectl, helm, aws CLI, zip installed
#   - Docker Hub image dipinthomas2003/k8s-deep-agent:latest already pushed
#   - infra/agent-secrets.yaml filled in (copy from agent-secrets.example.yaml)
#   - AWS SSO session active: aws sso login --profile fernhub
#
# Usage:
#   bash infra/deploy-all.sh

set -euo pipefail

export AWS_PROFILE="${AWS_PROFILE:-fernhub}"
REGION="ap-southeast-2"
CLUSTER="otel-demo-prod"
AGENT_NAMESPACE="k8s-agent"
ACCOUNT_ID="637039075925"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SECRETS_FILE="$SCRIPT_DIR/agent-secrets.yaml"

_extract_secret() { grep "${1}:" "$SECRETS_FILE" | head -1 | sed 's/.*: *"\(.*\)"/\1/'; }
export SLACK_WEBHOOK_URL="${SLACK_WEBHOOK_URL:-$(_extract_secret SLACK_WEBHOOK_URL)}"

# ── Colour helpers ─────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
step()  { echo ""; echo -e "${YELLOW}════════════════════════════════════════${NC}"; echo -e "${YELLOW}  $*${NC}"; echo -e "${YELLOW}════════════════════════════════════════${NC}"; }
ok()    { echo -e "    ${GREEN}✓ $*${NC}"; }
warn()  { echo -e "    ${YELLOW}⚠  $*${NC}"; }
die()   { echo -e "    ${RED}✗ $*${NC}"; echo ""; exit 1; }

# ── Preflight checks ───────────────────────────────────────────────────────────
step "Preflight checks"

for cmd in eksctl kubectl helm aws curl zip; do
  command -v "$cmd" &>/dev/null && ok "$cmd found" || die "$cmd not found — install it first"
done

aws sts get-caller-identity --region "$REGION" &>/dev/null \
  && ok "AWS credentials valid ($(aws sts get-caller-identity --query Account --output text --region "$REGION"))" \
  || die "AWS credentials invalid — check AWS_PROFILE"

[[ -f "$SECRETS_FILE" ]] \
  && ok "agent-secrets.yaml found" \
  || die "infra/agent-secrets.yaml not found — copy from agent-secrets.example.yaml and fill in values"

if grep -q "xoxb-\.\.\.\|xapp-\.\.\.\|hooks\.slack\.com/services/\.\.\." "$SECRETS_FILE" 2>/dev/null; then
  die "agent-secrets.yaml still contains placeholder values — fill in real credentials first"
fi

[[ -n "$SLACK_WEBHOOK_URL" ]] || die "SLACK_WEBHOOK_URL not found in agent-secrets.yaml"
ok "Slack webhook URL loaded"

# ── Step 1: EKS Cluster + Container Insights ───────────────────────────────────
step "Step 1/4 — EKS Cluster + CloudWatch Container Insights"

# Guard: if a previous eksctl CloudFormation stack is still being deleted, wait for it.
CF_STACK="eksctl-${CLUSTER}-cluster"
CF_STATUS=$(aws cloudformation describe-stacks \
  --stack-name "$CF_STACK" --region "$REGION" \
  --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo "NOT_FOUND")

if [[ "$CF_STATUS" == "DELETE_IN_PROGRESS" ]]; then
  warn "Previous CloudFormation stack '$CF_STACK' is still deleting — waiting up to 20 min..."
  aws cloudformation wait stack-delete-complete --stack-name "$CF_STACK" --region "$REGION" \
    || die "CloudFormation stack deletion timed out — check AWS console"
  ok "Old stack fully deleted"
elif [[ "$CF_STATUS" == "DELETE_FAILED" ]]; then
  die "CloudFormation stack '$CF_STACK' is in DELETE_FAILED state. Run destroy.sh first to clean up orphaned resources."
fi

if aws eks describe-cluster --name "$CLUSTER" --region "$REGION" &>/dev/null; then
  warn "Cluster '$CLUSTER' already exists — skipping creation"
else
  echo "    Creating EKS Auto Mode cluster (15-20 min)..."
  eksctl create cluster -f "$SCRIPT_DIR/eks-cluster.yaml" \
    || die "EKS cluster creation failed"
  ok "Cluster created"
fi

aws eks update-kubeconfig --name "$CLUSTER" --region "$REGION" &>/dev/null
ok "kubeconfig updated"

echo "    Waiting for cluster nodes to be Ready..."
kubectl wait node --all --for=condition=Ready --timeout=300s 2>/dev/null \
  || warn "No nodes yet (Auto Mode provisions on first workload — continuing)"

# Apply the CloudWatch Container Insights ConfigMap.
# This enables enhanced disk metrics (node_filesystem_utilization at 30s intervals)
# which the CloudWatch alarm in Step 5 relies on.
echo "    Applying CloudWatch Container Insights config..."
kubectl apply -f "$SCRIPT_DIR/cloudwatch-agent.yaml" \
  || warn "cloudwatch-agent.yaml apply failed — disk metrics may lag; check addon is installed"
ok "Container Insights config applied"

# ── Step 2: Redis Memory Store ────────────────────────────────────────────────
step "Step 2/4 — Redis memory store (agent namespace)"

# Cluster-scoped PriorityClasses — required by the agent and MCP gateway pods
# (both reference priorityClassName: infrastructure). Applied here so they exist
# before any workload that depends on them, regardless of OTel demo presence.
kubectl apply -f "$SCRIPT_DIR/priority-classes.yaml" \
  || die "Failed to apply priority classes"
ok "Priority classes applied"

# Ensure the agent namespace exists before applying Redis (which lives in it).
kubectl create namespace "$AGENT_NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f "$SCRIPT_DIR/redis-deployment.yaml" \
  || die "Failed to apply Redis deployment"
kubectl rollout status deployment/agent-redis -n "$AGENT_NAMESPACE" --timeout=120s \
  || die "Redis pod failed to start"
ok "Redis running in $AGENT_NAMESPACE"

# ── Step 3: AI Agent ───────────────────────────────────────────────────────────
step "Step 3/4 — AI Agent"

# ── IRSA: IAM Role for Service Account ────────────────────────────────────────
# Grants the agent pod AWS credentials (CloudWatch + X-Ray) without any secrets.
# EKS injects AWS_ROLE_ARN + AWS_WEB_IDENTITY_TOKEN_FILE into the pod;
# boto3 and the uvx CloudWatch MCP server pick them up via the default credential chain.

ROLE_NAME="k8s-agent-irsa"
POLICY_NAME="k8s-agent-cloudwatch-read"
POLICY_ARN="arn:aws:iam::${ACCOUNT_ID}:policy/${POLICY_NAME}"

echo "    Resolving OIDC provider..."
OIDC_URL=$(aws eks describe-cluster --name "$CLUSTER" --region "$REGION" \
  --query 'cluster.identity.oidc.issuer' --output text)
OIDC_ID=$(echo "$OIDC_URL" | cut -d'/' -f5)
OIDC_PROVIDER="oidc.eks.${REGION}.amazonaws.com/id/${OIDC_ID}"

if aws iam get-open-id-connect-provider \
    --open-id-connect-provider-arn "arn:aws:iam::${ACCOUNT_ID}:oidc-provider/${OIDC_PROVIDER}" \
    &>/dev/null 2>&1; then
  warn "OIDC provider already registered — skipping"
else
  aws iam create-open-id-connect-provider \
    --url "$OIDC_URL" --client-id-list sts.amazonaws.com \
    --thumbprint-list 9e99a48a9960b14926bb7f3b02e22da2b0ab7280 &>/dev/null
  ok "OIDC provider registered"
fi

POLICY_DOC='{
  "Version":"2012-10-17",
  "Statement":[
    {"Sid":"CloudWatchMetrics","Effect":"Allow","Action":["cloudwatch:DescribeAlarms","cloudwatch:DescribeAlarmsForMetric","cloudwatch:GetMetricData","cloudwatch:GetMetricStatistics","cloudwatch:ListMetrics","cloudwatch:ListDashboards"],"Resource":"*"},
    {"Sid":"CloudWatchLogs","Effect":"Allow","Action":["logs:StartQuery","logs:GetQueryResults","logs:StopQuery","logs:DescribeLogGroups","logs:DescribeLogStreams","logs:GetLogEvents","logs:FilterLogEvents"],"Resource":"*"},
    {"Sid":"XRayTraces","Effect":"Allow","Action":["xray:GetServiceGraph","xray:GetTraceSummaries","xray:GetTraceGraph","xray:BatchGetTraces"],"Resource":"*"}
  ]
}'

if aws iam get-policy --policy-arn "$POLICY_ARN" &>/dev/null 2>&1; then
  aws iam create-policy-version --policy-arn "$POLICY_ARN" \
    --policy-document "$POLICY_DOC" --set-as-default &>/dev/null
  ok "IAM policy '$POLICY_NAME' updated"
else
  aws iam create-policy --policy-name "$POLICY_NAME" \
    --policy-document "$POLICY_DOC" &>/dev/null
  ok "IAM policy '$POLICY_NAME' created"
fi

TRUST_POLICY="{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Federated\":\"arn:aws:iam::${ACCOUNT_ID}:oidc-provider/${OIDC_PROVIDER}\"},\"Action\":\"sts:AssumeRoleWithWebIdentity\",\"Condition\":{\"StringEquals\":{\"${OIDC_PROVIDER}:sub\":\"system:serviceaccount:${AGENT_NAMESPACE}:k8s-agent\",\"${OIDC_PROVIDER}:aud\":\"sts.amazonaws.com\"}}}]}"

if aws iam get-role --role-name "$ROLE_NAME" &>/dev/null 2>&1; then
  warn "IAM role '$ROLE_NAME' already exists — skipping"
else
  aws iam create-role --role-name "$ROLE_NAME" \
    --assume-role-policy-document "$TRUST_POLICY" &>/dev/null
  aws iam attach-role-policy --role-name "$ROLE_NAME" --policy-arn "$POLICY_ARN"
  ok "IAM role '$ROLE_NAME' created and policy attached"
fi
ok "IRSA configured"

echo "    Applying agent secrets..."
kubectl apply -f "$SCRIPT_DIR/agent-secrets.yaml" \
  || die "Failed to apply agent secrets"
ok "Secrets applied"

echo "    Deploying agent..."
kubectl apply -f "$SCRIPT_DIR/agent-deployment.yaml" \
  || die "Failed to apply agent deployment"

# Note: don't wait on rollout here — the agent and the MCP gateway are codeployed
# and the agent's startupProbe will block until MCP sidecars are reachable. Apply
# both, then wait at the end.
ok "Agent manifests applied"

# ── Step 3b: MCP Gateway ───────────────────────────────────────────────────────
step "Step 3b/4 — MCP Gateway"

kubectl apply -f "$SCRIPT_DIR/mcp-gateway-deployment.yaml" \
  || die "Failed to apply MCP gateway deployment"
kubectl rollout status deployment/k8s-mcp-gateway -n "$AGENT_NAMESPACE" --timeout=180s \
  || die "MCP gateway pod failed to start — check: kubectl logs -n $AGENT_NAMESPACE deployment/k8s-mcp-gateway"
ok "MCP gateway running"

echo "    Waiting for agent pod to become Ready..."
kubectl rollout status deployment/k8s-agent -n "$AGENT_NAMESPACE" --timeout=300s \
  || die "Agent pod failed to start — check: kubectl logs -n $AGENT_NAMESPACE deployment/k8s-agent"
ok "Agent deployed and running"

# ── Step 4: Alert Pipeline ─────────────────────────────────────────────────────
# CloudWatch disk alarm → SNS → Lambda → Agent /trigger
step "Step 4/4 — Alert Pipeline (disk alarm → SNS → Lambda → Agent)"

SNS_TOPIC_NAME="eks-disk-pressure-alerts"
LAMBDA_NAME="eks-alarm-to-agent"
LAMBDA_ROLE_NAME="eks-alarm-to-agent-role"
ALARM_NAME="EKS-NodeDiskPressure-${CLUSTER}"
DISK_THRESHOLD=75

# ── Detect agent LoadBalancer URL ──────────────────────────────────────────────
echo "    Detecting agent LoadBalancer URL..."
AGENT_HOST=""
for i in $(seq 1 12); do
  AGENT_HOST=$(kubectl get svc k8s-agent -n "$AGENT_NAMESPACE" \
    -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)
  [[ -n "$AGENT_HOST" ]] && break
  warn "LoadBalancer not ready yet — waiting 10s (attempt $i/12)..."
  sleep 10
done
[[ -n "$AGENT_HOST" ]] || die "Agent LoadBalancer hostname not assigned after 2 minutes. Check EKS Auto Mode node pool."
AGENT_TRIGGER_URL="http://${AGENT_HOST}:8080/trigger"
ok "Agent URL: $AGENT_TRIGGER_URL"

# ── SNS Topic ──────────────────────────────────────────────────────────────────
echo "    Creating SNS topic '$SNS_TOPIC_NAME'..."
SNS_TOPIC_ARN=$(aws sns create-topic \
  --name "$SNS_TOPIC_NAME" --region "$REGION" \
  --query TopicArn --output text)
ok "SNS topic: $SNS_TOPIC_ARN"

# ── Lambda IAM Role ────────────────────────────────────────────────────────────
echo "    Setting up Lambda IAM role '$LAMBDA_ROLE_NAME'..."
LAMBDA_TRUST='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'

if aws iam get-role --role-name "$LAMBDA_ROLE_NAME" &>/dev/null 2>&1; then
  warn "Lambda role already exists — skipping"
else
  aws iam create-role --role-name "$LAMBDA_ROLE_NAME" \
    --assume-role-policy-document "$LAMBDA_TRUST" &>/dev/null
  aws iam attach-role-policy --role-name "$LAMBDA_ROLE_NAME" \
    --policy-arn "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
  aws iam attach-role-policy --role-name "$LAMBDA_ROLE_NAME" \
    --policy-arn "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
fi
LAMBDA_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${LAMBDA_ROLE_NAME}"
ok "Lambda role: $LAMBDA_ROLE_ARN"

# ── VPC config for Lambda (must reach agent LoadBalancer from within VPC) ──────
echo "    Resolving VPC for Lambda placement..."
VPC_ID=$(aws eks describe-cluster --name "$CLUSTER" --region "$REGION" \
  --query 'cluster.resourcesVpcConfig.vpcId' --output text)
VPC_CIDR=$(aws ec2 describe-vpcs --region "$REGION" \
  --vpc-ids "$VPC_ID" --query 'Vpcs[0].CidrBlock' --output text)
PRIVATE_SUBNETS=$(aws ec2 describe-subnets --region "$REGION" \
  --filters "Name=vpc-id,Values=${VPC_ID}" "Name=map-public-ip-on-launch,Values=false" \
  --query 'Subnets[*].SubnetId' --output text | tr '\t' ',')
ok "VPC $VPC_ID | subnets: $PRIVATE_SUBNETS"

# Lambda security group
LAMBDA_SG_ID=$(aws ec2 describe-security-groups --region "$REGION" \
  --filters "Name=vpc-id,Values=${VPC_ID}" "Name=group-name,Values=lambda-eks-alarm-agent" \
  --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || true)
if [[ -z "$LAMBDA_SG_ID" || "$LAMBDA_SG_ID" == "None" ]]; then
  LAMBDA_SG_ID=$(aws ec2 create-security-group --region "$REGION" \
    --group-name "lambda-eks-alarm-agent" \
    --description "Lambda: EKS alarm to agent trigger" \
    --vpc-id "$VPC_ID" --query 'GroupId' --output text)
  ok "Lambda SG created: $LAMBDA_SG_ID"
else
  warn "Lambda SG already exists: $LAMBDA_SG_ID"
fi

# Open port 8080 on the NLB and cluster SGs from the VPC CIDR so Lambda can reach the agent
for SG_FILTER in "k8s-traffic-*" "eks-cluster-sg-${CLUSTER}-*"; do
  SG_ID=$(aws ec2 describe-security-groups --region "$REGION" \
    --filters "Name=vpc-id,Values=${VPC_ID}" "Name=group-name,Values=${SG_FILTER}" \
    --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || true)
  [[ -z "$SG_ID" || "$SG_ID" == "None" ]] && continue
  EXISTING=$(aws ec2 describe-security-group-rules --region "$REGION" \
    --filters "Name=group-id,Values=${SG_ID}" \
    --query "SecurityGroupRules[?FromPort==\`8080\` && IpProtocol=='tcp' && CidrIpv4=='${VPC_CIDR}' && !IsEgress].SecurityGroupRuleId" \
    --output text 2>/dev/null || true)
  if [[ -z "$EXISTING" || "$EXISTING" == "None" ]]; then
    aws ec2 authorize-security-group-ingress --group-id "$SG_ID" --region "$REGION" \
      --protocol tcp --port 8080 --cidr "$VPC_CIDR" 2>/dev/null || true
    ok "Port 8080 from $VPC_CIDR added to $SG_ID"
  else
    warn "Port 8080 rule already exists on $SG_ID"
  fi
done

# ── Package + deploy Lambda ────────────────────────────────────────────────────
echo "    Packaging Lambda..."
LAMBDA_ZIP="/tmp/${LAMBDA_NAME}.zip"
zip -j "$LAMBDA_ZIP" "$SCRIPT_DIR/lambda/trigger-agent.py"
ok "Packaged: $LAMBDA_ZIP"

echo "    Waiting for IAM role to propagate (10s)..."
sleep 10

if aws lambda get-function --function-name "$LAMBDA_NAME" --region "$REGION" &>/dev/null 2>&1; then
  aws lambda update-function-code \
    --function-name "$LAMBDA_NAME" --zip-file "fileb://$LAMBDA_ZIP" \
    --region "$REGION" --output text --query FunctionArn &>/dev/null
  aws lambda wait function-updated --function-name "$LAMBDA_NAME" --region "$REGION"
  LAMBDA_ARN=$(aws lambda update-function-configuration \
    --function-name "$LAMBDA_NAME" \
    --environment "Variables={AGENT_TRIGGER_URL=${AGENT_TRIGGER_URL}}" \
    --handler trigger-agent.lambda_handler --timeout 60 \
    --vpc-config "SubnetIds=${PRIVATE_SUBNETS},SecurityGroupIds=${LAMBDA_SG_ID}" \
    --region "$REGION" --query FunctionArn --output text)
  aws lambda wait function-updated --function-name "$LAMBDA_NAME" --region "$REGION"
  ok "Lambda updated: $LAMBDA_ARN"
else
  LAMBDA_ARN=$(aws lambda create-function \
    --function-name "$LAMBDA_NAME" \
    --runtime python3.12 \
    --role "$LAMBDA_ROLE_ARN" \
    --handler trigger-agent.lambda_handler \
    --zip-file "fileb://$LAMBDA_ZIP" \
    --environment "Variables={AGENT_TRIGGER_URL=${AGENT_TRIGGER_URL}}" \
    --timeout 60 \
    --vpc-config "SubnetIds=${PRIVATE_SUBNETS},SecurityGroupIds=${LAMBDA_SG_ID}" \
    --region "$REGION" --query FunctionArn --output text)
  aws lambda wait function-active --function-name "$LAMBDA_NAME" --region "$REGION"
  ok "Lambda created: $LAMBDA_ARN"
fi

# ── Wire SNS → Lambda ──────────────────────────────────────────────────────────
aws lambda add-permission \
  --function-name "$LAMBDA_NAME" --statement-id "sns-invoke" \
  --action "lambda:InvokeFunction" --principal sns.amazonaws.com \
  --source-arn "$SNS_TOPIC_ARN" --region "$REGION" 2>/dev/null || true

aws sns subscribe \
  --topic-arn "$SNS_TOPIC_ARN" --protocol lambda \
  --notification-endpoint "$LAMBDA_ARN" \
  --region "$REGION" --output text --query SubscriptionArn &>/dev/null
ok "SNS subscribed to Lambda"

# ── CloudWatch disk pressure alarm ────────────────────────────────────────────
# Metric: node_filesystem_utilization (ContainerInsights, ClusterName dimension)
# trigger-disk-pressure.sh fills the node to ~78%; alarm fires after 2 min at 75%.
echo "    Creating CloudWatch disk alarm '$ALARM_NAME'..."
aws cloudwatch put-metric-alarm \
  --alarm-name "$ALARM_NAME" \
  --alarm-description "EKS node disk usage above ${DISK_THRESHOLD}% in cluster ${CLUSTER}" \
  --metric-name node_filesystem_utilization \
  --namespace ContainerInsights \
  --dimensions Name=ClusterName,Value="$CLUSTER" \
  --statistic Maximum \
  --period 60 \
  --threshold "$DISK_THRESHOLD" \
  --comparison-operator GreaterThanThreshold \
  --evaluation-periods 2 \
  --treat-missing-data notBreaching \
  --alarm-actions "$SNS_TOPIC_ARN" \
  --ok-actions "$SNS_TOPIC_ARN" \
  --region "$REGION"
ok "CloudWatch alarm: node_filesystem_utilization > ${DISK_THRESHOLD}% × 2 min → SNS → Lambda → Agent"

# ── Smoke test: post a test message via webhook ────────────────────────────────
echo ""
echo "    Testing Slack webhook (expect a message in #k8s-alerts)..."
curl -s -X POST "$SLACK_WEBHOOK_URL" \
  -H 'Content-type: application/json' \
  --data '{"text":"✅ K8s agent demo stack deployed and ready.\nRun `bash fault-injection/trigger-disk-pressure.sh` to start the demo."}' \
  && ok "Slack webhook test posted" \
  || warn "Slack webhook test failed — check SLACK_WEBHOOK_URL in agent-secrets.yaml"

# ── Summary ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  ✅ Full stack deployed successfully!                  ${NC}"
echo -e "${GREEN}════════════════════════════════════════════════════════${NC}"
echo ""

echo "  Cluster:      $CLUSTER ($REGION)"
echo "  Agent logs:   kubectl logs -n $AGENT_NAMESPACE deployment/k8s-agent -f"
echo "  Alarm:        $ALARM_NAME (fires at ${DISK_THRESHOLD}% disk)"
echo "  Pipeline:     CloudWatch → SNS → $LAMBDA_NAME → $AGENT_TRIGGER_URL"
echo ""
echo "  ── To deploy the OTel demo workload (only when needed) ──────"
echo "  bash otel-demo/deploy.sh"
echo "  bash infra/patch-priority-classes.sh"
echo ""
echo "  ── To run the demo (requires OTel demo deployed) ────────────"
echo "  bash fault-injection/trigger-disk-pressure.sh"
echo ""
echo "  ── To test the pipeline without real disk pressure ──────────"
echo "  bash infra/test-alert-pipeline.sh"
echo ""
echo "  ── To tear everything down ──────────────────────────────────"
echo "  AWS_PROFILE=${AWS_PROFILE} bash infra/destroy.sh"
