#!/bin/bash
# Reset the cluster to healthy state after the demo.
# Run this immediately after the talk or after a failed demo run.

set -euo pipefail

NAMESPACE="otel-demo"

echo "🟢 Resetting cluster to healthy state..."
echo ""

echo "==> Scaling down load generator..."
kubectl set env deployment/load-generator \
  -n "$NAMESPACE" \
  LOCUST_USERS=10 \
  LOCUST_SPAWN_RATE=1
echo "    ✓ Load generator returned to 10 users"

echo "==> Restoring image-provider nginx log level..."
kubectl set env deployment/image-provider \
  -n "$NAMESPACE" \
  NGINX_LOG_LEVEL=warn
echo "    ✓ nginx log level set to warn"

echo "==> Restoring ephemeral-storage limit on image-provider..."
kubectl patch deployment image-provider -n "$NAMESPACE" \
  --type=json \
  -p='[{"op":"add","path":"/spec/template/spec/containers/0/resources/limits/ephemeral-storage","value":"500Mi"}]'
echo "    ✓ ephemeral-storage limit restored to 500Mi"

echo "==> Restarting evicted deployments (if any)..."
for deployment in image-provider ad recommendation load-generator; do
  if kubectl get deployment "$deployment" -n "$NAMESPACE" &>/dev/null; then
    kubectl rollout restart deployment/"$deployment" -n "$NAMESPACE"
    echo "    ✓ Restarted $deployment"
  fi
done

echo ""
echo "==> Waiting for pods to be ready..."
kubectl rollout status deployment/image-provider -n "$NAMESPACE" --timeout=120s
kubectl rollout status deployment/load-generator -n "$NAMESPACE" --timeout=120s

echo ""
echo "==> Current pod status:"
kubectl get pods -n "$NAMESPACE"

echo ""
echo "✅ Cluster reset complete. Ready for next demo run."
echo ""
echo "Verify CloudWatch alarm cleared:"
echo "  aws cloudwatch describe-alarms \\"
echo "    --alarm-names EKS-NodeDiskPressure-otel-demo \\"
echo "    --region ap-southeast-2 \\"
echo "    --query 'MetricAlarms[0].StateValue'"
