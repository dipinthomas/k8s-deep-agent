#!/bin/bash
# Trigger the agent investigation directly via its public HTTP endpoint.
# Bypasses Lambda/SNS — useful for testing without needing Lambda inside VPC.
#
# Usage:
#   bash infra/trigger-agent-direct.sh          # trigger ALARM (disk pressure scenario)
#   bash infra/trigger-agent-direct.sh ok       # trigger OK (resolved)
#   bash infra/trigger-agent-direct.sh <url>    # override LB URL

set -euo pipefail

STATE="${1:-ALARM}"

# Auto-detect the agent LB hostname from the cluster, or use the override arg
if [[ "$STATE" == http* ]]; then
  AGENT_URL="$STATE"
  STATE="ALARM"
else
  AGENT_URL=$(kubectl get svc k8s-agent -n k8s-agent \
    -o jsonpath='http://{.status.loadBalancer.ingress[0].hostname}:8080' 2>/dev/null || true)
fi

if [[ -z "$AGENT_URL" ]]; then
  echo "Error: could not auto-detect agent LoadBalancer URL."
  echo "Usage: bash infra/trigger-agent-direct.sh [ALARM|ok|<agent-url>]"
  exit 1
fi

if [[ "$STATE" == "ok" || "$STATE" == "OK" ]]; then
  PAYLOAD_STATE="OK"
  REASON="Threshold Crossed: 1 datapoint [67.2] was not greater than the threshold (85.0)."
else
  PAYLOAD_STATE="ALARM"
  REASON="Node disk usage at 91% (threshold: 85%). Pod image-provider writing 340MB/8min to ephemeral storage. Checkout service p99 latency rising: 245ms -> 890ms."
fi

echo "==> Triggering agent at $AGENT_URL (state=$PAYLOAD_STATE)..."

RESPONSE=$(curl -sf -X POST "$AGENT_URL/trigger" \
  -H "Content-Type: application/json" \
  -d "{
    \"state\": \"$PAYLOAD_STATE\",
    \"alarm_name\": \"EKS-NodeDiskPressure-otel-demo-prod\",
    \"node\": \"i-06573397050277546\",
    \"reason\": \"$REASON\"
  }")

echo "$RESPONSE" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print('  Status :', d.get('status'))
print('  Thread :', d.get('thread_ts', ''))
"

echo ""
echo "  Watch agent logs:"
echo "    kubectl logs -n k8s-agent -l app=k8s-agent -c agent -f"
echo ""
echo "  Or tail from here:"
echo "    kubectl logs -n k8s-agent \$(kubectl get pod -n k8s-agent -l app=k8s-agent -o jsonpath='{.items[0].metadata.name}') -c agent -f"
