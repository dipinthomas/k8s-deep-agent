#!/bin/bash
# One-command deploy of the OTel Demo app
# Usage: bash otel-demo/deploy.sh

set -euo pipefail

NAMESPACE="otel-demo"
RELEASE_NAME="otel-demo"
CHART_VERSION="0.32.0"   # Pin to tested version

echo "==> Adding OpenTelemetry Helm repo..."
helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts
helm repo update

echo "==> Creating namespace..."
kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -

echo "==> Applying PriorityClasses..."
kubectl apply -f "$(dirname "$0")/../infra/priority-classes.yaml"

echo "==> Deploying OTel Demo..."
helm upgrade --install "$RELEASE_NAME" open-telemetry/opentelemetry-demo \
  --namespace "$NAMESPACE" \
  --version "$CHART_VERSION" \
  --values "$(dirname "$0")/values.yaml" \
  --wait \
  --timeout 10m

echo ""
echo "==> Deployment complete. Verifying pods..."
kubectl get pods -n "$NAMESPACE"

echo ""
echo "==> Frontend URL:"
kubectl get svc -n "$NAMESPACE" otel-demo-frontendproxy \
  -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null \
  && echo "" \
  || echo "  (LoadBalancer still provisioning — check with: kubectl get svc -n $NAMESPACE)"

echo ""
echo "==> Next steps:"
echo "  1. Configure CloudWatch alarm:  see README.md Step 4"
echo "  2. Set up Slack bot:            see slack/bot-setup.md"
echo "  3. Start the agent:             cd agent && python main.py"
