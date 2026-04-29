#!/bin/bash
# Trigger the disk pressure demo scenario on the EKS cluster.
# Run this 3-5 minutes before the demo moment in the talk.
#
# How it works:
#   1. Cranks load generator to 500 users
#   2. Sets image-provider nginx to debug logging (the root cause)
#   3. Deploys a privileged fill pod on the same node as image-provider,
#      which writes exactly enough bytes to reach TARGET_PCT (default 78%)
#      on the NODE's actual filesystem — bypassing container ephemeral limits.
#
# The fill pod stays running to hold the allocation.
# Run reset-cluster.sh to clean up after the demo.

set -euo pipefail

NAMESPACE="otel-demo"
TARGET_PCT="${TARGET_PCT:-78}"   # fill node disk to this percentage
REGION="us-east-1"

echo "🔴 Triggering disk pressure demo scenario..."
echo ""

# ── Step 1: Max-out load generator ────────────────────────────────────────────
echo "==> Step 1: Scaling load generator to 500 users..."
kubectl set env deployment/load-generator \
  -n "$NAMESPACE" \
  LOCUST_USERS=500 \
  LOCUST_SPAWN_RATE=50
echo "    ✓ Load generator scaled up"

# ── Step 2: Enable verbose nginx logging ──────────────────────────────────────
echo "==> Step 2: Enabling verbose nginx logging on image-provider..."
kubectl set env deployment/image-provider \
  -n "$NAMESPACE" \
  NGINX_LOG_LEVEL=debug
echo "    ✓ nginx logging set to debug"

# ── Step 3: Wait for image-provider to redeploy ───────────────────────────────
echo ""
echo "==> Step 3: Waiting for image-provider to redeploy with new settings..."
kubectl rollout status deployment/image-provider -n "$NAMESPACE" --timeout=120s
echo "    ✓ image-provider redeployed"

# ── Step 4: Find the node image-provider landed on ────────────────────────────
echo ""
echo "==> Step 4: Finding target node..."
TARGET_NODE=$(kubectl get pod -n "$NAMESPACE" \
  -l app.kubernetes.io/component=image-provider \
  -o jsonpath='{.items[0].spec.nodeName}')
echo "    Target node: $TARGET_NODE"

# ── Step 5: Deploy privileged fill pod ────────────────────────────────────────
# Writes directly to the Bottlerocket data partition (/local on the node).
# On Bottlerocket, /local is the writable 80GB xfs data partition (nvme1n1p1).
# /local/mnt, /local/opt, /local/var are bind-mounted read-only from the OS root —
# must write to a NEW directory created directly under /local (e.g. /local/demo-diskfill/).
echo ""
echo "==> Step 5: Deploying disk fill pod on $TARGET_NODE..."

# Remove any previous fill pod
kubectl delete pod demo-disk-filler -n "$NAMESPACE" --ignore-not-found --wait=false 2>/dev/null || true

# Write pod spec to file then apply (avoids heredoc quoting issues with shell escapes)
FILL_POD_YAML="/tmp/demo-disk-filler.yaml"
cat > "$FILL_POD_YAML" <<PODEOF
apiVersion: v1
kind: Pod
metadata:
  name: demo-disk-filler
  namespace: ${NAMESPACE}
  labels:
    app: demo-disk-filler
spec:
  nodeName: ${TARGET_NODE}
  restartPolicy: Never
  tolerations:
    - operator: Exists
  containers:
    - name: filler
      image: busybox
      command:
        - sh
        - -c
        - |
          set -e
          TARGET_PCT=${TARGET_PCT}

          # EKS Auto Mode nodes run Bottlerocket OS.
          # /dev/nvme1n1p1 is the 80GB data partition mounted at /local (rw xfs).
          # /local/mnt, /local/opt, /local/var are bind-mounted read-only from the erofs OS root.
          # Must create a new directory directly under /local to get a writable path.
          DATA_MOUNT="/host/local"
          FILL_FILE="\${DATA_MOUNT}/filldata"


          TOTAL_KB=\$(df "\$DATA_MOUNT" | awk 'NR==2{print \$2}')
          USED_KB=\$(df "\$DATA_MOUNT"  | awk 'NR==2{print \$3}')
          AVAIL_KB=\$(df "\$DATA_MOUNT" | awk 'NR==2{print \$4}')

          CURRENT_PCT=\$(( USED_KB * 100 / TOTAL_KB ))
          echo "Node data disk (/local): \${CURRENT_PCT}% used (\${USED_KB}KB / \${TOTAL_KB}KB)"

          if [ "\$CURRENT_PCT" -ge "\$TARGET_PCT" ]; then
            echo "Already at \${CURRENT_PCT}% — no fill needed."
            tail -f /dev/null
          fi

          HEADROOM_KB=\$(( 3 * 1024 * 1024 ))
          FILL_KB=\$(( TOTAL_KB * TARGET_PCT / 100 - USED_KB ))
          MAX_FILL_KB=\$(( AVAIL_KB - HEADROOM_KB ))
          [ "\$FILL_KB" -gt "\$MAX_FILL_KB" ] && FILL_KB=\$MAX_FILL_KB
          FILL_MB=\$(( FILL_KB / 1024 ))

          echo "Writing \${FILL_MB}MB to \${FILL_FILE} to reach \${TARGET_PCT}%..."
          dd if=/dev/zero of="\${FILL_FILE}" bs=1M count=\$FILL_MB
          echo "Fill complete."
          df "\$DATA_MOUNT" | awk 'NR==2{printf "Final: %d%% used (%sKB / %sKB)\n", \$3*100/\$2, \$3, \$2}'

          tail -f /dev/null
      securityContext:
        privileged: true
      volumeMounts:
        - name: host
          mountPath: /host
  volumes:
    - name: host
      hostPath:
        path: /
PODEOF

kubectl apply -f "$FILL_POD_YAML"

echo "    ✓ Fill pod deployed — writing to node filesystem..."
echo ""
echo "⏳ Disk will fill in ~30-60s. CloudWatch alarm fires after 2 evaluation periods (~2 min)."
echo ""
echo "Monitor fill progress:"
echo "  kubectl logs -n $NAMESPACE demo-disk-filler -f"
echo ""
echo "Monitor node disk %:"
echo "  kubectl exec -n $NAMESPACE demo-disk-filler -- df -h /host"
echo ""
echo "Monitor node conditions:"
echo "  kubectl describe node $TARGET_NODE | grep -A6 Conditions"
echo ""
echo "When the alarm fires it posts to #k8s-alerts and wakes the agent."
