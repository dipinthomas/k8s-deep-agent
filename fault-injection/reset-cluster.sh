#!/bin/bash
# Reset the cluster to healthy state after the demo.
# Run this immediately after the talk or after a failed demo run.
#
# Cleans up both fault injection scenarios:
#   trigger-disk-pressure.sh  → removes demo-disk-filler, resets nginx logging + load
#   trigger-noisy-neighbor.sh → removes demo-stress pod

set -euo pipefail

NAMESPACE="${NAMESPACE:-default}"
AGENT_NAMESPACE="${AGENT_NAMESPACE:-k8s-agent}"

echo "🟢 Resetting cluster to healthy state..."
echo ""

# ── Flush agent investigation state (checkpoints only, NOT the memory index) ──
# FLUSHALL destroys the Redis search index used by the long-term memory store.
# Instead, delete only the checkpoint and write_keys keys that belong to in-flight
# investigations, then restart the agent so the store re-initialises if needed.
echo "==> Flushing agent investigation state (Redis checkpoints)..."
kubectl exec -n "$AGENT_NAMESPACE" deployment/agent-redis -- \
  redis-cli --scan --pattern 'checkpoint*' | \
  xargs -r kubectl exec -n "$AGENT_NAMESPACE" deployment/agent-redis -- redis-cli DEL \
  2>/dev/null && echo "    ✓ Checkpoint keys flushed" || echo "    ⚠  No checkpoint keys found (already clean)"

kubectl exec -n "$AGENT_NAMESPACE" deployment/agent-redis -- \
  redis-cli --scan --pattern 'write_keys_zset*' | \
  xargs -r kubectl exec -n "$AGENT_NAMESPACE" deployment/agent-redis -- redis-cli DEL \
  2>/dev/null && echo "    ✓ Write-key sets flushed" || true

echo "==> Restarting agent to reinitialise memory store..."
kubectl rollout restart deployment/k8s-agent -n "$AGENT_NAMESPACE" 2>/dev/null || true
kubectl rollout status deployment/k8s-agent -n "$AGENT_NAMESPACE" --timeout=60s 2>/dev/null || true
echo "    ✓ Agent restarted"
echo ""

# ── Remove fault injection pods ────────────────────────────────────────────────
echo "==> Removing fault injection pods..."
kubectl delete pod demo-disk-filler -n "$NAMESPACE" --ignore-not-found --force --grace-period=0 2>/dev/null && \
  echo "    ✓ demo-disk-filler removed" || true
kubectl delete pod demo-stress -n "$NAMESPACE" --ignore-not-found --force --grace-period=0 2>/dev/null && \
  echo "    ✓ demo-stress removed" || true

# ── Reset image-provider nginx logging (disk pressure scenario) ────────────────
echo ""
echo "==> Resetting nginx logging on image-provider..."
kubectl set env deployment/image-provider \
  -n "$NAMESPACE" \
  NGINX_LOG_LEVEL=warn 2>/dev/null || true
echo "    ✓ NGINX_LOG_LEVEL=warn"

# ── Reset load generator (disk pressure scenario) ──────────────────────────────
echo ""
echo "==> Scaling down load generator to baseline (10 users)..."
kubectl set env deployment/load-generator \
  -n "$NAMESPACE" \
  LOCUST_USERS=10 \
  LOCUST_SPAWN_RATE=1 2>/dev/null || true
echo "    ✓ load-generator: 10 users"

# ── Restart any evicted or crashed deployments ─────────────────────────────────
echo ""
echo "==> Restarting deployments that may have been evicted..."
for deployment in checkout payment cart image-provider ad recommendation load-generator; do
  if kubectl get deployment "$deployment" -n "$NAMESPACE" &>/dev/null; then
    kubectl rollout restart deployment/"$deployment" -n "$NAMESPACE" 2>/dev/null || true
    echo "    ✓ Restarted $deployment"
  fi
done

# ── Wait for checkout to recover (only if the OTel demo is deployed) ──────────
if kubectl get deployment checkout -n "$NAMESPACE" &>/dev/null; then
  echo ""
  echo "==> Waiting for checkout to be ready..."
  kubectl rollout status deployment/checkout -n "$NAMESPACE" --timeout=120s
fi

echo ""
echo "==> Current pod status:"
kubectl get pods -n "$NAMESPACE" --sort-by=.metadata.name

echo ""
echo "✅ Cluster reset complete. Ready for next demo run."
echo ""
echo "   Disk should drop as the fill file is removed:"
echo "     kubectl exec -n $NAMESPACE deploy/checkout -- df -h / 2>/dev/null || true"
echo "   CPU should return to normal immediately."
echo ""
echo "   Trigger next run: bash fault-injection/trigger-disk-pressure.sh"
