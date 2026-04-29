#!/bin/bash
# Build Docker images, push to ECR, and deploy to EKS
# Usage: ./deploy-to-eks.sh
# Requires: AWS CLI, Docker, kubectl configured for otel-demo-prod

set -e
cd "$(dirname "$0")"

AWS_REGION="${AWS_REGION:-us-east-1}"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_BASE="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

echo "==> Logging in to ECR"
aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "$ECR_BASE"

# Create ECR repos if they don't exist
for repo in demo-api demo-frontend; do
  aws ecr describe-repositories --repository-names "$repo" --region "$AWS_REGION" 2>/dev/null \
    || aws ecr create-repository --repository-name "$repo" --region "$AWS_REGION"
done

echo "==> Building and pushing demo-api"
docker build -t "${ECR_BASE}/demo-api:latest" ./api
docker push "${ECR_BASE}/demo-api:latest"

echo "==> Building and pushing demo-frontend"
docker build -t "${ECR_BASE}/demo-frontend:latest" ./demo-site
docker push "${ECR_BASE}/demo-frontend:latest"

echo "==> Patching image references in k8s manifest"
sed \
  -e "s|demo-api:latest|${ECR_BASE}/demo-api:latest|g" \
  -e "s|demo-frontend:latest|${ECR_BASE}/demo-frontend:latest|g" \
  k8s/demo-site.yaml | kubectl apply -f -

echo "==> Waiting for rollout"
kubectl rollout status deployment/demo-api      -n demo-site
kubectl rollout status deployment/demo-frontend -n demo-site

echo ""
echo "  Deployed. Get the ALB address with:"
echo "  kubectl get ingress demo-ingress -n demo-site"
