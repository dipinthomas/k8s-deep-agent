#!/bin/bash
# Trigger the noisy-neighbor demo scenario.
#
# Deploys a stress pod on a worker node, consuming ~80% of node CPU.
# This keeps CPU high enough to trigger the CloudWatch alarm (threshold: 75%)
# without pegging the node to 100% — which would make kubectl unresponsive.
#
# The alarm fires → SNS → Lambda → agent /trigger endpoint.
# The agent investigates: finds the stress pod, identifies it as non-critical,
# and recommends eviction.
#
# Usage:           bash fault-injection/trigger-noisy-neighbor.sh
# Pin to a node:   TARGET_NODE=<node-name> bash fault-injection/trigger-noisy-neighbor.sh
# Use a namespace: NAMESPACE=<ns> bash fault-injection/trigger-noisy-neighbor.sh
# Reset:           bash fault-injection/reset-cluster.sh

set -euo pipefail

NAMESPACE="${NAMESPACE:-default}"
# Target 80% CPU utilisation — enough to alarm, low enough to stay manageable.
# stress --cpu N pegs N threads to 100%; using 80% of vCPUs hits ~80% node CPU.
CPU_PCT="${CPU_PCT:-80}"

echo "🔴 Deploying noisy-neighbor stress pod (targeting ~${CPU_PCT}% node CPU)..."
echo ""

# ── Pick a target node ────────────────────────────────────────────────────────
# If TARGET_NODE is set in the env, use it. Otherwise pick the first Ready
# worker node (excluding control plane). EKS Auto Mode nodes are unlabelled
# workers, so we just take the first node we find.
if [[ -n "${TARGET_NODE:-}" ]]; then
  echo "==> Using node from env: $TARGET_NODE"
else
  # polinux/stress-ng only publishes amd64 — picking an arm64 node (e.g. the
  # EKS Auto `system` NodePool) makes the container exit with "exec format error".
  echo "==> Finding a Ready amd64 worker node..."
  TARGET_NODE=$(kubectl get nodes \
    -l kubernetes.io/arch=amd64 \
    --no-headers \
    -o custom-columns='NAME:.metadata.name,STATUS:.status.conditions[?(@.type=="Ready")].status' \
    | awk '$2=="True" {print $1; exit}')
fi

if [[ -z "$TARGET_NODE" ]]; then
  echo "ERROR: Could not find a Ready amd64 node. Is the cluster running?"
  echo "       Try: kubectl get nodes -L kubernetes.io/arch"
  exit 1
fi

NODE_CPU=$(kubectl get node "$TARGET_NODE" -o jsonpath='{.status.capacity.cpu}')
# stress-ng strategy: spawn one worker per vCPU, each running at CPU_PCT load.
# This gives exact percentage targeting regardless of node size — unlike
# `stress --cpu N` which only takes integer workers and can't do fractions.
STRESS_WORKERS="$NODE_CPU"
echo "    Target node   : $TARGET_NODE  (${NODE_CPU} vCPUs)"
echo "    Stress threads: $STRESS_WORKERS workers @ ${CPU_PCT}% load each (~${CPU_PCT}% node CPU)"

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
  annotations:
    karpenter.sh/do-not-disrupt: "true"
spec:
  nodeName: ${TARGET_NODE}
  restartPolicy: Never
  tolerations:
    - operator: Exists
  containers:
    - name: stress
      # Multi-arch (amd64 + arm64). polinux/stress-ng is amd64-only and
      # fails with "exec format error" on Graviton/arm64 nodes.
      image: ghcr.io/colinianking/stress-ng
      # One worker per vCPU at CPU_PCT load each = ~CPU_PCT% node CPU.
      # No --timeout: runs until the pod is deleted (reset script handles cleanup).
      args: ["--cpu", "${STRESS_WORKERS}", "--cpu-load", "${CPU_PCT}"]
      resources:
        requests:
          cpu: "0"
        # No CPU limit: cgroup CFS throttling under contention prevents the
        # pod from reaching its target % on small (2 vCPU) nodes.
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
