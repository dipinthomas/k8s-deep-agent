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

echo "==> Removing disk-fill file from image-provider (if still running)..."
IMAGE_PROVIDER_POD=$(kubectl get pod -n "$NAMESPACE" \
  -l app.kubernetes.io/name=image-provider \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || \
  kubectl get pod -n "$NAMESPACE" \
  -l app.kubernetes.io/component=image-provider \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
if [[ -n "$IMAGE_PROVIDER_POD" ]]; then
  kubectl exec "$IMAGE_PROVIDER_POD" -n "$NAMESPACE" -- \
    sh -c 'rm -f /tmp/demo-disk-fill' 2>/dev/null || true
  echo "    ✓ Disk-fill file removed (or pod already replaced)"
else
  echo "    ✓ No image-provider pod found — skipped (will be gone after restart)"
fi

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
echo "    --alarm-names EKS-NodeDiskPressure-otel-demo-prod \\"
echo "    --region ap-southeast-2 \\"
echo "    --query 'MetricAlarms[0].StateValue'"
