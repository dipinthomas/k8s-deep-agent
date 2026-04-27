#!/bin/bash
# Trigger the noisy-neighbor demo scenario.
#
# Deploys a stress pod on the same node as checkout, consuming ~80% of node CPU.
# This keeps CPU high enough to trigger the CloudWatch alarm (threshold: 75%)
# without pegging the node to 100% — which would make kubectl unresponsive.
#
# The alarm fires → SNS → Lambda → agent /trigger endpoint.
# The agent investigates: finds the stress pod, identifies it as non-critical,
# and recommends eviction to protect checkoutservice.
#
# Usage: bash fault-injection/trigger-noisy-neighbor.sh
# Reset: bash fault-injection/reset-cluster.sh

set -euo pipefail

NAMESPACE="otel-demo"
# Target 80% CPU utilisation — enough to alarm, low enough to stay manageable.
# stress --cpu N pegs N threads to 100%; using 80% of vCPUs hits ~80% node CPU.
CPU_PCT="${CPU_PCT:-80}"

echo "🔴 Deploying noisy-neighbor stress pod (targeting ~${CPU_PCT}% node CPU)..."
echo ""

# ── Find checkout's node ───────────────────────────────────────────────────────
echo "==> Finding checkout pod's node..."
TARGET_NODE=$(kubectl get pod -n "$NAMESPACE" \
  -l app.kubernetes.io/component=checkout \
  -o jsonpath='{.items[0].spec.nodeName}')

if [[ -z "$TARGET_NODE" ]]; then
  echo "ERROR: Could not find checkout pod. Is the cluster running?"
  exit 1
fi

NODE_CPU=$(kubectl get node "$TARGET_NODE" -o jsonpath='{.status.capacity.cpu}')
# Calculate number of stress threads = floor(vCPUs * CPU_PCT / 100), minimum 1
STRESS_WORKERS=$(python3 -c "import math; print(max(1, math.floor(${NODE_CPU} * ${CPU_PCT} / 100)))")
echo "    Checkout node : $TARGET_NODE  (${NODE_CPU} vCPUs)"
echo "    Stress threads: $STRESS_WORKERS  (targeting ~${CPU_PCT}% utilisation)"

# ── Deploy stress pod on the same node ────────────────────────────────────────
echo ""
echo "==> Deploying stress pod on $TARGET_NODE..."

kubectl delete pod demo-stress -n "$NAMESPACE" --ignore-not-found --force --grace-period=0 2>/dev/null || true

cat > /tmp/demo-stress.yaml <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: demo-stress
  namespace: ${NAMESPACE}
  labels:
    app: demo-stress
    # Clearly non-critical — agent should identify and recommend eviction
    app.kubernetes.io/component: stress-test
spec:
  nodeName: ${TARGET_NODE}
  restartPolicy: Never
  tolerations:
    - operator: Exists
  containers:
    - name: stress
      image: progrium/stress
      args: ["--cpu", "${STRESS_WORKERS}", "--timeout", "0"]
      resources:
        requests:
          cpu: "0"
        limits:
          cpu: "${STRESS_WORKERS}"
EOF

kubectl apply -f /tmp/demo-stress.yaml

echo "    ✓ Stress pod running on $TARGET_NODE — using ${STRESS_WORKERS}/${NODE_CPU} CPU cores (~${CPU_PCT}%)"
echo ""
echo "⏳ CloudWatch will detect CPU spike in ~60s."
echo "   Alarm fires after 2 evaluation periods (~2 min)."
echo "   The agent will start investigating automatically when the alarm fires."
echo ""
echo "Manual trigger (skip alarm wait):"
echo "  bash infra/trigger-agent-direct.sh cpu"
echo ""
echo "Monitor CPU:  kubectl top pods -n $NAMESPACE --sort-by=cpu"
echo "Agent logs:   kubectl logs -n k8s-agent -l app=k8s-agent -c agent -f"
echo "Reset:        bash fault-injection/reset-cluster.sh"
