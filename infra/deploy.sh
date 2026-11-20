#!/usr/bin/env bash
# Deploy the agent to YOUR EKS cluster. Works in any account and region.
#
#   export AWS_PROFILE=<your-profile> AWS_REGION=<your-region>
#   export CLUSTER_NAME=<your-eks-cluster>
#   export CLUSTER_SKILL=path/to/your/SKILL.md     # describes your workloads (see skills/clusters/README.md)
#   cp infra/agent-secrets.example.yaml infra/agent-secrets.yaml   # fill in keys + Slack
#   ./infra/deploy.sh
#
# Optional:
#   AGENT_MODEL   provider:model (default openai:gpt-5.4-mini, the tested EKS model)
#   AGENT_IMAGE   default: the published, tested agent image
#   MCP_IMAGE     default: the published, tested MCP sidecar image
#   RENDER_ONLY=1 print the rendered manifests and stop (no AWS or cluster changes);
#                 set ROLE_ARN yourself or a placeholder is used
#
# What it does: checks your kube context points at CLUSTER_NAME, creates the IRSA
# role (CloudFormation stack k8s-agent-iam-<cluster>), then applies priority classes,
# Redis (checkpointer), the secret, your cluster skill (ConfigMap) and the agent.
set -euo pipefail
cd "$(dirname "$0")/.."

AGENT_IMAGE="${AGENT_IMAGE:-dipinthomas2003/k8s-deep-agent:v119}"
MCP_IMAGE="${MCP_IMAGE:-dipinthomas2003/k8s-mcp-gateway:v3}"
AGENT_MODEL="${AGENT_MODEL:-openai:gpt-5.4-mini}"
NS=k8s-agent

die() { echo "ERROR: $*" >&2; exit 1; }
: "${CLUSTER_NAME:?set CLUSTER_NAME to your EKS cluster name}"
: "${AWS_REGION:?set AWS_REGION (e.g. us-east-1)}"
: "${CLUSTER_SKILL:?set CLUSTER_SKILL to your cluster skill file (see skills/clusters/README.md)}"
[ -f "$CLUSTER_SKILL" ] || die "CLUSTER_SKILL=$CLUSTER_SKILL not found"
[[ "$CLUSTER_NAME" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]*$ ]] || die "CLUSTER_NAME must be letters, digits, - or _"

render() {  # $1 = role arn
  sed -e "s|__ROLE_ARN__|$1|g" -e "s|__AWS_REGION__|$AWS_REGION|g" -e "s|__CLUSTER_NAME__|$CLUSTER_NAME|g" \
      -e "s|__AGENT_IMAGE__|$AGENT_IMAGE|g" -e "s|__MCP_IMAGE__|$MCP_IMAGE|g" -e "s|__AGENT_MODEL__|$AGENT_MODEL|g" \
      infra/agent-deployment.yaml
}

if [ "${RENDER_ONLY:-}" = "1" ]; then
  render "${ROLE_ARN:-arn:aws:iam::<account-id>:role/k8s-agent-$CLUSTER_NAME}"
  exit 0
fi

[ -n "${AWS_PROFILE:-}" ] || echo "note: AWS_PROFILE not set; using your default AWS credentials"
command -v kubectl >/dev/null || die "kubectl not installed"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)" || die "AWS credentials not working"

# Safety: never deploy into whatever cluster happens to be the current context.
CTX="$(kubectl config current-context 2>/dev/null)" || die "no current kube context (aws eks update-kubeconfig --name $CLUSTER_NAME)"
case "$CTX" in
  *"$CLUSTER_NAME"*) ;;
  *) die "current kube context is '$CTX', not $CLUSTER_NAME. Run: aws eks update-kubeconfig --name $CLUSTER_NAME --region $AWS_REGION" ;;
esac
echo "account $ACCOUNT, region $AWS_REGION, cluster $CLUSTER_NAME (context $CTX)"

# 1. IRSA role. Needs the cluster's IAM OIDC provider.
ISSUER="$(aws eks describe-cluster --name "$CLUSTER_NAME" --region "$AWS_REGION" --query cluster.identity.oidc.issuer --output text)"
HOST="${ISSUER#https://}"
if ! aws iam list-open-id-connect-providers --query 'OpenIDConnectProviderList[].Arn' --output text | tr '\t' '\n' | grep -q "/$HOST\$"; then
  die "no IAM OIDC provider for this cluster. Run once: eksctl utils associate-iam-oidc-provider --cluster $CLUSTER_NAME --region $AWS_REGION --approve"
fi
STACK="k8s-agent-iam-$CLUSTER_NAME"
aws cloudformation deploy --region "$AWS_REGION" --stack-name "$STACK" \
  --template-file infra/cloudformation/agent-iam.yaml --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides "OidcIssuerHost=$HOST" "RoleName=k8s-agent-$CLUSTER_NAME" --no-fail-on-empty-changeset
ROLE_ARN="$(aws cloudformation describe-stacks --region "$AWS_REGION" --stack-name "$STACK" --query "Stacks[0].Outputs[?OutputKey=='RoleArn'].OutputValue" --output text)"
echo "IRSA role: $ROLE_ARN"

# 2. Cluster objects.
kubectl apply -f infra/priority-classes.yaml
kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f infra/redis-deployment.yaml
if [ -f infra/agent-secrets.yaml ]; then
  kubectl apply -f infra/agent-secrets.yaml
elif ! kubectl get secret k8s-agent-secrets -n "$NS" >/dev/null 2>&1; then
  die "no secret: cp infra/agent-secrets.example.yaml infra/agent-secrets.yaml, fill it in, re-run"
fi
kubectl create configmap k8s-agent-cluster-skill -n "$NS" --from-file=SKILL.md="$CLUSTER_SKILL" \
  --dry-run=client -o yaml | kubectl apply -f -
render "$ROLE_ARN" | kubectl apply -f -
kubectl rollout restart deployment/k8s-agent -n "$NS" >/dev/null   # pick up a changed skill or secret
kubectl rollout status deployment/k8s-agent -n "$NS" --timeout=300s

echo
echo "Deployed. Logs: kubectl logs -n $NS deploy/k8s-agent -c agent -f"
echo "Manual test: kubectl port-forward -n $NS svc/k8s-agent 8080:8080, then POST to http://localhost:8080/trigger"
echo "Remove:      ./infra/teardown.sh"
