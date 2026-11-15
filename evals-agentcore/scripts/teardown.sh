#!/usr/bin/env bash
# Remove everything the lab created in AWS_REGION. Run from evals-agentcore/.
# Leaves the CDKToolkit bootstrap stack (shared by other CDK apps) in place.
set -uo pipefail
cd "$(dirname "$0")/.."
REGION="${AWS_REGION:-$(aws configure get region 2>/dev/null)}"
[ -n "$REGION" ] || { echo "set AWS_REGION"; exit 1; }
export AWS_REGION="$REGION"

# Empty spec + deploy -> CDK deletes the runtime, gateway, memory, evaluator and online eval.
( cd k8sHarness && { agentcore remove all --yes; agentcore deploy --yes; } )

# The replay Lambda and its role. Read the role from the function so older
# setups (role without the region suffix) are cleaned up too.
ROLE="$(aws lambda get-function-configuration --function-name k8s-harness-replay --query Role --output text 2>/dev/null | awk -F/ '{print $NF}')"
aws lambda delete-function --function-name k8s-harness-replay >/dev/null 2>&1
aws logs delete-log-group --log-group-name /aws/lambda/k8s-harness-replay 2>/dev/null
aws ssm delete-parameter --name /k8s-harness-lab/state 2>/dev/null
for r in $(printf '%s\n' "$ROLE" "k8s-harness-replay-role-$REGION" | grep -v '^$' | sort -u); do
  aws iam delete-role-policy --role-name "$r" --policy-name lab-state 2>/dev/null
  aws iam detach-role-policy --role-name "$r" --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole 2>/dev/null
  aws iam delete-role --role-name "$r" 2>/dev/null && echo "deleted role $r"
done
echo "teardown done in $REGION (CDKToolkit bootstrap stack left in place)"
