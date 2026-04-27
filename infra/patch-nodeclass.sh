#!/bin/bash
# Patch EKS Auto Mode default NodeClass to use 20Gi ephemeral storage.
# The default is 80Gi — far too large for a demo fill scenario.
# 20Gi means the fill pod only needs to write ~9GB to reach 78%, done in ~90s.

set -euo pipefail

cat > /tmp/nodeclass-patch.yaml <<'EOF'
spec:
  ephemeralStorage:
    iops: 3000
    size: 20Gi
    throughput: 125
EOF

kubectl patch nodeclass default --type=merge --patch-file /tmp/nodeclass-patch.yaml
echo "  ✅ NodeClass default: ephemeralStorage patched to 20Gi"
echo ""
echo "  Existing nodes keep their old disk until replaced."
echo "  To force replacement: kubectl delete node <node-name>"
