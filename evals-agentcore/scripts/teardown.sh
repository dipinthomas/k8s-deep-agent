#!/usr/bin/env bash
# Remove everything the lab created. Run from evals-agentcore/.
set -uo pipefail
REGION="${AWS_REGION:-ap-southeast-2}"
( cd k8sHarness && agentcore remove all --yes; agentcore deploy --yes )   # empty spec -> CDK deletes runtime, gateway, memory, evaluator, online eval
aws lambda delete-function --function-name k8s-harness-replay --region "$REGION"
aws logs delete-log-group --log-group-name /aws/lambda/k8s-harness-replay --region "$REGION"
aws ssm delete-parameter --name /k8s-harness-lab/state --region "$REGION"
aws iam delete-role-policy --role-name k8s-harness-replay-role --policy-name lab-state
aws iam detach-role-policy --role-name k8s-harness-replay-role --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
aws iam delete-role --role-name k8s-harness-replay-role
echo "teardown done (CDKToolkit bootstrap stack left in place)"
