#!/bin/bash
# Trigger the agent investigation directly via its public HTTP endpoint.
# Bypasses Lambda/SNS — useful for testing without needing Lambda inside VPC.
#
# Usage:
#   bash infra/trigger-agent-direct.sh                       # ALARM (disk pressure scenario)
#   bash infra/trigger-agent-direct.sh cpu                   # ALARM (noisy-neighbor / CPU scenario)
#   bash infra/trigger-agent-direct.sh ok                    # OK (resolved)
#   bash infra/trigger-agent-direct.sh <url>                 # override LB URL (still uses disk scenario)
#   bash infra/trigger-agent-direct.sh cpu --node i-abc123   # explicit node override

set -euo pipefail

SCENARIO="${1:-ALARM}"

# Auto-detect the agent LB hostname from the cluster, or use the override arg
if [[ "$SCENARIO" == http* ]]; then
  AGENT_URL="$SCENARIO"
  SCENARIO="ALARM"
else
  AGENT_URL=$(kubectl get svc k8s-agent -n k8s-agent \
    -o jsonpath='http://{.status.loadBalancer.ingress[0].hostname}:8080' 2>/dev/null || true)
fi

if [[ -z "$AGENT_URL" ]]; then
  echo "Error: could not auto-detect agent LoadBalancer URL."
  echo "Usage: bash infra/trigger-agent-direct.sh [ALARM|cpu|ok|<agent-url>]"
  exit 1
fi

# Resolve the affected node dynamically. Try in order:
#   1. --node <name> override (highest priority)
#   2. otel-demo checkout pod's node (real demo deployment)
#   3. node hosting the demo-stress fault-injection pod
#   4. first Ready worker node in the cluster
CHECKOUT_NODE=""

# Allow --node <name> as the LAST argument
for arg in "$@"; do
  if [[ -n "${NEXT_IS_NODE:-}" ]]; then
    CHECKOUT_NODE="$arg"
    NEXT_IS_NODE=""
  elif [[ "$arg" == "--node" ]]; then
    NEXT_IS_NODE=1
  fi
done

if [[ -z "$CHECKOUT_NODE" ]]; then
  CHECKOUT_NODE=$(kubectl get pod -n otel-demo \
    -l app.kubernetes.io/component=checkout \
    -o jsonpath='{.items[0].spec.nodeName}' 2>/dev/null || true)
fi

if [[ -z "$CHECKOUT_NODE" ]]; then
  CHECKOUT_NODE=$(kubectl get pod demo-stress -n default \
    -o jsonpath='{.spec.nodeName}' 2>/dev/null || true)
fi

if [[ -z "$CHECKOUT_NODE" ]]; then
  CHECKOUT_NODE=$(kubectl get nodes \
    -o jsonpath='{range .items[?(@.status.conditions[-1].type=="Ready")]}{.metadata.name}{"\n"}{end}' \
    2>/dev/null | head -1)
fi

if [[ -z "$CHECKOUT_NODE" ]]; then
  echo "Error: could not resolve a node. Pass --node <name> explicitly." >&2
  exit 1
fi

if [[ "$SCENARIO" == "ok" || "$SCENARIO" == "OK" ]]; then
  PAYLOAD_STATE="OK"
  ALARM_NAME="EKS-NodeCPUHigh-otel-demo-prod"
  REASON="Threshold Crossed: 1 datapoint [62.4] was not greater than the threshold (75.0). Node CPU returned to normal."

elif [[ "$SCENARIO" == "cpu" || "$SCENARIO" == "CPU" ]]; then
  PAYLOAD_STATE="ALARM"
  ALARM_NAME="EKS-NodeCPUHigh-otel-demo-prod"
  REASON="Node CPU utilisation at 81% (threshold: 75%). Application service p99 latency rising: 145ms -> 620ms. Unidentified workload consuming ${NODE_CPU:-8} cores on node."

else
  # Default: disk pressure scenario
  PAYLOAD_STATE="ALARM"
  ALARM_NAME="EKS-NodeDiskPressure-otel-demo-prod"
  REASON="Node disk usage at 91% (threshold: 85%). Pod image-provider writing 340MB/8min to ephemeral storage. Checkout service p99 latency rising: 245ms -> 890ms."
fi

echo "==> Triggering agent at $AGENT_URL"
echo "    Scenario : $SCENARIO  →  state=$PAYLOAD_STATE"
echo "    Node     : $CHECKOUT_NODE"
echo ""

RESPONSE=$(curl -sf -X POST "$AGENT_URL/trigger" \
  -H "Content-Type: application/json" \
  -d "{
    \"state\": \"$PAYLOAD_STATE\",
    \"alarm_name\": \"$ALARM_NAME\",
    \"node\": \"$CHECKOUT_NODE\",
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
