#!/bin/bash
# Trigger the disk pressure demo scenario on the EKS cluster.
# Run this 3-5 minutes before the demo.
#
# What this does:
#   1. Cranks up load generator to 500 users (amplifies nginx log writes)
#   2. Sets image-provider nginx to debug logging (the "root cause misconfiguration")
#   3. Removes image-provider ephemeral-storage limits
#   4. Fills ~60GB of disk directly inside the image-provider pod using dd
#      (needed because EKS Auto Mode nodes have ~100GB disks — nginx logs alone
#       take too long to fill them for a live demo)
#
# The dd fill targets the image-provider pod's emptyDir so evicting that pod
# releases the disk space — matching the demo story exactly.
#
# Monitor progress:
#   kubectl describe node | grep -A5 Conditions
#   watch -n5 kubectl top pods -n otel-demo

set -euo pipefail

NAMESPACE="otel-demo"
REGION="ap-southeast-2"

echo "🔴 Triggering disk pressure demo scenario..."
echo ""

# ── Step 1: Max-out load generator ────────────────────────────────────────────
echo "==> Step 1: Scaling load generator to 500 users..."
kubectl set env deployment/load-generator \
  -n "$NAMESPACE" \
  LOCUST_USERS=500 \
  LOCUST_SPAWN_RATE=50
echo "    ✓ Load generator scaled up"

# ── Step 2: Enable verbose nginx logging (the storytelling root cause) ─────────
echo "==> Step 2: Enabling verbose nginx logging on image-provider..."
kubectl set env deployment/image-provider \
  -n "$NAMESPACE" \
  NGINX_LOG_LEVEL=debug
echo "    ✓ nginx logging set to debug"

# ── Step 3: Remove ephemeral-storage limit ────────────────────────────────────
echo "==> Step 3: Removing ephemeral-storage limit on image-provider..."
kubectl patch deployment image-provider -n "$NAMESPACE" \
  --type=json \
  -p='[{"op":"remove","path":"/spec/template/spec/containers/0/resources/limits/ephemeral-storage"}]' \
  2>/dev/null || true
echo "    ✓ ephemeral-storage limit removed"

# ── Step 4: Wait for image-provider pod to redeploy ───────────────────────────
echo ""
echo "==> Step 4: Waiting for image-provider to redeploy with new settings..."
kubectl rollout status deployment/image-provider -n "$NAMESPACE" --timeout=120s
echo "    ✓ image-provider redeployed"

# ── Step 5: Directly fill disk to push node above threshold ───────────────────
# EKS Auto Mode nodes have ~100GB disks so nginx debug logs alone are too slow.
# We dd a 60GB sparse file into /tmp inside the pod, which counts against the
# node's ephemeral storage.  Evicting the pod drops usage back below threshold.
echo ""
echo "==> Step 5: Filling disk on image-provider pod (~60 GB — takes ~60s)..."

IMAGE_PROVIDER_POD=$(kubectl get pod -n "$NAMESPACE" \
  -l app.kubernetes.io/name=image-provider \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || \
  kubectl get pod -n "$NAMESPACE" \
  -l app.kubernetes.io/component=image-provider \
  -o jsonpath='{.items[0].metadata.name}')

if [[ -z "$IMAGE_PROVIDER_POD" ]]; then
  echo "    ✗ Could not find image-provider pod — skipping disk fill"
  echo "      Run manually: kubectl get pods -n $NAMESPACE | grep image-provider"
else
  echo "    Pod: $IMAGE_PROVIDER_POD"
  # fallocate is faster than dd but may not be available in the nginx image;
  # fall back to dd with bs=1M.
  kubectl exec "$IMAGE_PROVIDER_POD" -n "$NAMESPACE" -- \
    sh -c 'fallocate -l 60G /tmp/demo-disk-fill 2>/dev/null || dd if=/dev/zero of=/tmp/demo-disk-fill bs=1M count=61440 status=none' &
  FILL_PID=$!
  echo "    Filling in background (PID $FILL_PID)..."
  echo "    ✓ Disk fill running — node disk usage will rise in ~60 seconds"
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "⏳ CloudWatch alarm should fire in 2-4 minutes (2 × 1-min evaluation periods)."
echo ""
echo "Monitor disk fill:"
echo "  kubectl exec -n $NAMESPACE $IMAGE_PROVIDER_POD -- df -h /tmp"
echo ""
echo "Monitor node conditions:"
echo "  kubectl describe node | grep -A10 Conditions"
echo "  watch -n5 'kubectl top pods -n $NAMESPACE'"
echo ""
echo "Check CloudWatch metric (requires Container Insights to have scraped at least one point):"
echo "  aws cloudwatch get-metric-statistics \\"
echo "    --namespace ContainerInsights \\"
echo "    --metric-name node_filesystem_utilization \\"
echo "    --dimensions Name=ClusterName,Value=otel-demo-prod \\"
echo "    --start-time \$(date -u -v-15M +%Y-%m-%dT%H:%M:%SZ) \\"
echo "    --end-time \$(date -u +%Y-%m-%dT%H:%M:%SZ) \\"
echo "    --period 60 --statistics Average --region $REGION"
echo ""
echo "When the CloudWatch alarm fires it will post to #k8s-alerts and wake the agent."
