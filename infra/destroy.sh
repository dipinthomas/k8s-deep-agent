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
REGION="us-east-1"
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
  run aws logs delete-log-group --log-group-name "$LAMBDA_LOG_GROUP" --region "$REGION" || true
  echo "    ✓ Log group deleted (or not found)"
else
  echo "    ✓ Log group not found — skipped"
fi

# ── Step 3b: Lambda VPC ENI + Security Group cleanup ──────────────────────────
# AWS leaves VPC-attached Lambda ENIs behind after function deletion (up to ~20 min).
# These block both the Lambda SG deletion and subnet deletion in the eksctl
# CloudFormation stack. We filter ENIs by the Lambda SG itself (not by description,
# which has changed across Lambda versions and may not include the function name),
# wait for them to detach, delete them, then delete the SG with retry-on-dependency.
echo ""
echo "==> Step 3b: Cleaning up Lambda VPC ENIs and security group '$LAMBDA_SG_NAME'..."

# Resolve the Lambda SG ID up-front so we can filter ENIs by it.
LAMBDA_SG_ID=$(aws ec2 describe-security-groups \
  --filters "Name=group-name,Values=${LAMBDA_SG_NAME}" \
  --region "$REGION" \
  --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || true)

if [[ -z "$LAMBDA_SG_ID" || "$LAMBDA_SG_ID" == "None" ]]; then
  echo "    ✓ Lambda security group '$LAMBDA_SG_NAME' not found — skipped"
elif [[ "$DRY_RUN" == "true" ]]; then
  echo "    [DRY RUN] Would clean up ENIs using $LAMBDA_SG_ID and delete the SG"
else
  MAX_WAIT=600   # 10 minutes — Lambda ENI detach can take up to ~20 min in worst case
  ELAPSED=0
  while true; do
    # Filter ENIs by SG membership — robust against Lambda ENI description changes.
    ENIS=$(aws ec2 describe-network-interfaces \
      --filters "Name=group-id,Values=${LAMBDA_SG_ID}" \
      --region "$REGION" \
      --query 'NetworkInterfaces[].NetworkInterfaceId' \
      --output text 2>/dev/null || true)

    if [[ -z "$ENIS" ]]; then
      echo "    ✓ No ENIs using SG $LAMBDA_SG_ID"
      break
    fi

    IN_USE=$(aws ec2 describe-network-interfaces \
      --filters "Name=group-id,Values=${LAMBDA_SG_ID}" \
                "Name=status,Values=in-use" \
      --region "$REGION" \
      --query 'NetworkInterfaces[].NetworkInterfaceId' \
      --output text 2>/dev/null || true)

    if [[ -z "$IN_USE" ]]; then
      for eni in $ENIS; do
        aws ec2 delete-network-interface --network-interface-id "$eni" --region "$REGION" 2>/dev/null \
          && echo "    ✓ Deleted ENI: $eni" \
          || echo "    ⚠ Could not delete ENI $eni (may already be gone)"
      done
      break
    fi

    if [[ $ELAPSED -ge $MAX_WAIT ]]; then
      echo "    ⚠ Timeout waiting for ENIs to detach after ${MAX_WAIT}s — continuing anyway"
      break
    fi

    echo "    Waiting for ENIs to detach ($ELAPSED/${MAX_WAIT}s)..."
    sleep 10
    ELAPSED=$((ELAPSED + 10))
  done

  # Delete the SG with retry — AWS control plane often lags after ENI deletion,
  # so the first delete attempt can fail with DependencyViolation even when
  # nothing actually references the SG anymore.
  SG_DELETED=false
  for attempt in 1 2 3 4 5 6; do
    if aws ec2 delete-security-group --group-id "$LAMBDA_SG_ID" --region "$REGION" 2>/tmp/sg-del-err.$$; then
      echo "    ✓ Deleted Lambda security group: $LAMBDA_SG_ID ($LAMBDA_SG_NAME)"
      SG_DELETED=true
      rm -f /tmp/sg-del-err.$$
      break
    fi
    ERR=$(cat /tmp/sg-del-err.$$ 2>/dev/null || true)
    if echo "$ERR" | grep -q "InvalidGroup.NotFound"; then
      echo "    ✓ Lambda security group already gone"
      SG_DELETED=true
      rm -f /tmp/sg-del-err.$$
      break
    fi
    if echo "$ERR" | grep -q "DependencyViolation"; then
      echo "    Attempt $attempt: SG still has dependency, waiting 30s and retrying..."
      sleep 30
      continue
    fi
    echo "    ⚠ Unexpected error deleting SG: $ERR"
    break
  done
  rm -f /tmp/sg-del-err.$$
  if [[ "$SG_DELETED" != "true" ]]; then
    echo "    ⚠ Could not delete Lambda SG after retries — eksctl may handle it via VPC teardown"
  fi
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
    run aws logs delete-log-group --log-group-name "$lg" --region "$REGION" || true
    echo "    ✓ Deleted: $lg"
  else
    echo "    ✓ Not found: $lg — skipped"
  fi
done

# ── Step 6b: EKS Addons ────────────────────────────────────────────────────────
# Addons (e.g. amazon-cloudwatch-observability) get their own CloudFormation stacks
# when installed via eksctl. `eksctl delete cluster` does not always remove them
# cleanly — explicitly delete addons first so the VPC/cluster teardown is clean.
echo ""
echo "==> Step 6b: Deleting EKS addons for cluster '$CLUSTER'..."
if check aws eks describe-cluster --name "$CLUSTER" --region "$REGION"; then
  ADDONS=$(aws eks list-addons --cluster-name "$CLUSTER" --region "$REGION" \
    --query 'addons[]' --output text 2>/dev/null || true)
  for addon in $ADDONS; do
    [[ -z "$addon" ]] && continue
    run aws eks delete-addon --cluster-name "$CLUSTER" --addon-name "$addon" \
      --region "$REGION" --preserve 2>/dev/null || true
    echo "    ✓ Requested deletion of addon: $addon"
  done
  if [[ "$DRY_RUN" != "true" && -n "$ADDONS" ]]; then
    echo "    Waiting up to 5 min for addons to delete..."
    for addon in $ADDONS; do
      [[ -z "$addon" ]] && continue
      WAITED=0
      while check aws eks describe-addon --cluster-name "$CLUSTER" \
          --addon-name "$addon" --region "$REGION"; do
        if [[ $WAITED -ge 300 ]]; then
          echo "    ⚠ Timeout waiting for addon $addon — continuing"
          break
        fi
        sleep 10
        WAITED=$((WAITED + 10))
      done
      echo "    ✓ Addon $addon removed"
    done
  fi
else
  echo "    ✓ Cluster not found — no addons to delete"
fi

# Force-delete any leftover addon CloudFormation stacks (sometimes orphaned).
ADDON_STACKS=$(aws cloudformation list-stacks --region "$REGION" \
  --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE DELETE_FAILED ROLLBACK_COMPLETE \
  --query "StackSummaries[?starts_with(StackName, \`eksctl-${CLUSTER}-addon-\`)].StackName" \
  --output text 2>/dev/null || true)
for stack in $ADDON_STACKS; do
  [[ -z "$stack" ]] && continue
  echo "    Deleting orphaned addon stack: $stack"
  run aws cloudformation delete-stack --stack-name "$stack" --region "$REGION" || true
  if [[ "$DRY_RUN" != "true" ]]; then
    aws cloudformation wait stack-delete-complete --stack-name "$stack" --region "$REGION" 2>/dev/null \
      && echo "    ✓ Deleted: $stack" \
      || echo "    ⚠ Stack $stack did not finish deleting cleanly — eksctl may retry"
  fi
done

# ── Step 6c: Stray Load Balancers in cluster VPC ──────────────────────────────
# AWS Load Balancer Controller (or Service type=LoadBalancer) creates ELBs/ALBs/NLBs
# that are NOT tracked by the eksctl CloudFormation stack. They block VPC deletion.
echo ""
echo "==> Step 6c: Cleaning up load balancers tagged for cluster '$CLUSTER'..."
CLUSTER_VPC=$(aws eks describe-cluster --name "$CLUSTER" --region "$REGION" \
  --query 'cluster.resourcesVpcConfig.vpcId' --output text 2>/dev/null || echo "")
if [[ -n "$CLUSTER_VPC" && "$CLUSTER_VPC" != "None" ]]; then
  # Classic ELBs
  CLB_NAMES=$(aws elb describe-load-balancers --region "$REGION" \
    --query "LoadBalancerDescriptions[?VPCId==\`${CLUSTER_VPC}\`].LoadBalancerName" \
    --output text 2>/dev/null || true)
  for lb in $CLB_NAMES; do
    [[ -z "$lb" ]] && continue
    run aws elb delete-load-balancer --load-balancer-name "$lb" --region "$REGION" || true
    echo "    ✓ Deleted classic ELB: $lb"
  done
  # ALBs / NLBs (v2)
  ALB_ARNS=$(aws elbv2 describe-load-balancers --region "$REGION" \
    --query "LoadBalancers[?VpcId==\`${CLUSTER_VPC}\`].LoadBalancerArn" \
    --output text 2>/dev/null || true)
  for arn in $ALB_ARNS; do
    [[ -z "$arn" ]] && continue
    run aws elbv2 delete-load-balancer --load-balancer-arn "$arn" --region "$REGION" || true
    echo "    ✓ Deleted ALB/NLB: $arn"
  done
  if [[ -z "$CLB_NAMES" && -z "$ALB_ARNS" ]]; then
    echo "    ✓ No load balancers in VPC $CLUSTER_VPC"
  fi
else
  echo "    ✓ Cluster VPC not resolvable — skipping LB cleanup"
fi

# ── Step 7: EKS Cluster ────────────────────────────────────────────────────────
echo ""
echo "==> Step 7: Deleting EKS cluster '$CLUSTER'..."
echo "    This deletes the cluster, node groups, VPC, subnets, security groups"
echo "    (including the Lambda SG), load balancers, and CloudFormation stacks."
echo "    Expected time: 10-15 minutes."
echo ""

CF_STACK="eksctl-${CLUSTER}-cluster"
delete_cf_stack_with_retry() {
  local stack="$1"
  echo "    Triggering CloudFormation delete on '$stack'..."
  aws cloudformation delete-stack --stack-name "$stack" --region "$REGION"
  echo "    Waiting for stack deletion (up to 30 min)..."
  if aws cloudformation wait stack-delete-complete --stack-name "$stack" --region "$REGION" 2>/dev/null; then
    echo "    ✓ CloudFormation stack deleted"
    return 0
  fi
  # Wait failed — describe to see why
  local status
  status=$(aws cloudformation describe-stacks --stack-name "$stack" --region "$REGION" \
    --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo "NOT_FOUND")
  if [[ "$status" == "NOT_FOUND" ]]; then
    echo "    ✓ Stack already gone"
    return 0
  fi
  echo "    ⚠ Stack in state: $status — retrying once after 60s"
  sleep 60
  aws cloudformation delete-stack --stack-name "$stack" --region "$REGION"
  aws cloudformation wait stack-delete-complete --stack-name "$stack" --region "$REGION" \
    && echo "    ✓ Stack deleted on retry" \
    || { echo "    ✗ Stack still failing — inspect manually:"; \
         aws cloudformation describe-stack-events --stack-name "$stack" --region "$REGION" \
           --max-items 10 --query 'StackEvents[?ResourceStatus==`DELETE_FAILED`].[LogicalResourceId,ResourceStatusReason]' \
           --output table; \
         return 1; }
}

CF_STATUS=$(aws cloudformation describe-stacks \
  --stack-name "$CF_STACK" --region "$REGION" \
  --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo "NOT_FOUND")

if [[ "$CF_STATUS" == "DELETE_FAILED" ]]; then
  echo "    ⚠ CloudFormation stack '$CF_STACK' is in DELETE_FAILED state — retrying."
  if [[ "$DRY_RUN" != "true" ]]; then
    delete_cf_stack_with_retry "$CF_STACK"
  else
    echo "    [DRY RUN] aws cloudformation delete-stack --stack-name $CF_STACK"
  fi
elif check aws eks describe-cluster --name "$CLUSTER" --region "$REGION"; then
  if [[ "$DRY_RUN" == "true" ]]; then
    echo "    [DRY RUN] eksctl delete cluster --name $CLUSTER --region $REGION --wait"
  else
    if eksctl delete cluster --name "$CLUSTER" --region "$REGION" --wait; then
      echo "    ✓ EKS cluster deleted"
    else
      echo "    ⚠ eksctl delete failed — falling back to direct CloudFormation deletion"
      delete_cf_stack_with_retry "$CF_STACK" || true
    fi
  fi
elif [[ "$CF_STATUS" != "NOT_FOUND" ]]; then
  echo "    Cluster API gone but CF stack still present (state: $CF_STATUS) — deleting stack."
  if [[ "$DRY_RUN" != "true" ]]; then
    delete_cf_stack_with_retry "$CF_STACK"
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
