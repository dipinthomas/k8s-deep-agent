#!/bin/bash
# Trigger the noisy-neighbor demo scenario.
#
# This script ONLY deploys the stress pod — that's it.
# CloudWatch detects the CPU spike, fires the alarm,
# SNS invokes Lambda, Lambda calls the agent's /trigger endpoint,
# and the agent handles everything from there (including posting to Slack).
#
# Usage: bash fault-injection/trigger-noisy-neighbor.sh
# Reset: bash fault-injection/reset-cluster.sh

set -euo pipefail

NAMESPACE="otel-demo"

echo "🔴 Deploying noisy-neighbor stress pod..."
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
echo "    Checkout node: $TARGET_NODE  (${NODE_CPU} vCPUs)"

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
spec:
  nodeName: ${TARGET_NODE}
  restartPolicy: Never
  tolerations:
    - operator: Exists
  containers:
    - name: stress
      image: progrium/stress
      args: ["--cpu", "${NODE_CPU}", "--timeout", "0"]
      resources:
        requests:
          cpu: "0"
EOF

kubectl apply -f /tmp/demo-stress.yaml

echo "    ✓ Stress pod running on $TARGET_NODE — pegging ${NODE_CPU} CPU cores"
echo ""
echo "⏳ CloudWatch will detect CPU spike in ~60s, alarm fires after 2 periods (~2 min)."
echo "   The agent will start investigating automatically when the alarm fires."
echo ""
echo "Monitor CPU:  kubectl top pods -n $NAMESPACE --sort-by=cpu"
echo "Agent logs:   kubectl logs -n $NAMESPACE -l app=k8s-agent -f"
echo "Reset:        bash fault-injection/reset-cluster.sh"
