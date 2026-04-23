#!/bin/bash
# Trigger the disk pressure demo scenario on the EKS cluster.
# Run this 3-5 minutes before the demo to allow pressure to build naturally.
#
# What this does:
#   1. Cranks up load generator to 500 users (amplifies nginx log writes)
#   2. Sets imageprovider nginx to debug logging (the root cause)
#   3. Removes imageprovider ephemeral-storage limits (accelerates disk fill)
#
# Monitor progress:
#   kubectl describe node | grep -A5 Conditions
#   watch kubectl top pods -n otel-demo

set -euo pipefail

NAMESPACE="otel-demo"

echo "🔴 Triggering disk pressure demo scenario..."
echo ""

# Step 1: Crank up load generator to max traffic
echo "==> Step 1: Scaling load generator to 500 users..."
kubectl set env deployment/loadgenerator \
  -n "$NAMESPACE" \
  LOCUST_USERS=500 \
  LOCUST_SPAWN_RATE=50
echo "    ✓ Load generator scaled up"

# Step 2: Enable verbose nginx logging on imageprovider (the "misconfiguration")
echo "==> Step 2: Enabling verbose nginx logging on imageprovider..."
kubectl set env deployment/imageprovider \
  -n "$NAMESPACE" \
  NGINX_LOG_LEVEL=debug
echo "    ✓ nginx logging set to debug"

# Step 3: Remove ephemeral-storage limit on imageprovider
echo "==> Step 3: Removing ephemeral-storage limit on imageprovider..."
kubectl patch deployment imageprovider -n "$NAMESPACE" \
  --type=json \
  -p='[{"op":"remove","path":"/spec/template/spec/containers/0/resources/limits/ephemeral-storage"}]' \
  2>/dev/null || true   # ignore if limit was already absent
echo "    ✓ ephemeral-storage limit removed"

echo ""
echo "⏳ Disk pressure will build in approximately 3-5 minutes."
echo ""
echo "Monitor with:"
echo "  kubectl describe node | grep -A10 Conditions"
echo "  watch -n5 'kubectl top pods -n $NAMESPACE'"
echo "  aws cloudwatch get-metric-statistics \\"
echo "    --namespace ContainerInsights \\"
echo "    --metric-name node_filesystem_utilization \\"
echo "    --dimensions Name=ClusterName,Value=otel-demo-prod \\"
echo "    --start-time \$(date -u -v-15M +%Y-%m-%dT%H:%M:%SZ) \\"
echo "    --end-time \$(date -u +%Y-%m-%dT%H:%M:%SZ) \\"
echo "    --period 60 --statistics Average --region ap-southeast-2"
echo ""
echo "When the CloudWatch alarm fires, it will post to #k8s-alerts and start the demo."
