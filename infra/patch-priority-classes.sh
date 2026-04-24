#!/bin/bash
# Patch priority classes onto OTel demo deployments
# Run after: bash otel-demo/deploy.sh
# Usage: bash infra/patch-priority-classes.sh

set -e
NS="otel-demo"

patch() {
  local deploy=$1 pc=$2
  kubectl patch deployment "$deploy" -n "$NS" \
    --patch "{\"spec\":{\"template\":{\"spec\":{\"priorityClassName\":\"$pc\"}}}}" 2>&1 \
    && echo "  ✅ $deploy → $pc" \
    || echo "  ⚠️  $deploy not found (may not be deployed)"
}

echo "Patching priority classes in namespace: $NS"
echo ""

echo "payment-critical (1000000):"
patch checkout    payment-critical
patch payment     payment-critical
patch cart        payment-critical

echo ""
echo "user-facing (500000):"
patch frontend        user-facing
patch frontend-proxy  user-facing
patch product-catalog user-facing

echo ""
echo "infrastructure (900000):"
patch image-provider user-facing   # high priority so eviction is meaningful

echo ""
echo "background (100000):"
patch load-generator   background
patch ad               background
patch recommendation   background
patch fraud-detection  background
patch accounting       background
patch email            background
patch currency         background
patch shipping         background
patch quote            background

echo ""
echo "Done. Verify with:"
echo "  kubectl get pods -n $NS -o custom-columns='NAME:.metadata.name,PRIORITY:.spec.priorityClassName'"
