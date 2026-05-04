#!/bin/bash
# Trigger the agent investigation directly via its public HTTP endpoint.
# Bypasses Lambda/SNS — useful for testing without needing Lambda inside VPC.
#
# Usage:
#   bash infra/trigger-agent-direct.sh                       # ALARM (disk pressure scenario)
#   bash infra/trigger-agent-direct.sh cpu                   # ALARM (noisy-neighbor / CPU scenario)
#   bash infra/trigger-agent-direct.sh pods                  # ALARM (investigate all not-Running pods)
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
  # Fallback: first node currently reporting Ready in `kubectl get nodes`.
  # The jsonpath conditions[-1] form is fragile (depends on condition order),
  # so use the kubectl-rendered STATUS column instead.
  CHECKOUT_NODE=$(kubectl get nodes --no-headers 2>/dev/null \
    | awk '$2=="Ready"{print $1; exit}')
fi

if [[ -z "$CHECKOUT_NODE" ]]; then
  if [[ "$SCENARIO" == "pods" || "$SCENARIO" == "PODS" ]]; then
    # Pods scenario is cluster-wide — node is informational only.
    CHECKOUT_NODE="cluster-wide"
  else
    echo "Error: could not resolve a node. Pass --node <name> explicitly." >&2
    exit 1
  fi
fi

if [[ "$SCENARIO" == "ok" || "$SCENARIO" == "OK" ]]; then
  PAYLOAD_STATE="OK"
  ALARM_NAME="EKS-NodeCPUHigh-otel-demo-prod"
  REASON="Threshold Crossed: 1 datapoint [62.4] was not greater than the threshold (75.0). Node CPU returned to normal."

elif [[ "$SCENARIO" == "cpu" || "$SCENARIO" == "CPU" ]]; then
  PAYLOAD_STATE="ALARM"
  ALARM_NAME="checkoutservice-p99-latency-high"
  # Service-level alarm: node is not a single host, so use the sentinel
  # that routes the agent into the subagent path (not node-targeted mode).
  CHECKOUT_NODE="(service-level alarm)"
  REASON="checkoutservice P99 latency threshold breached: 245ms -> 890ms (threshold 300ms). 2 evaluation periods."

elif [[ "$SCENARIO" == "pods" || "$SCENARIO" == "PODS" ]]; then
  PAYLOAD_STATE="ALARM"
  ALARM_NAME="EKS-PodsNotRunning-otel-demo-prod"

  # Snapshot every pod cluster-wide that is NOT healthy. "Not healthy" =
  # phase is anything other than Running/Succeeded, OR phase is Running but a
  # container is not Ready (catches CrashLoopBackOff, ImagePullBackOff, etc.).
  # One pod per line: <namespace>/<name>  phase=<phase>  reason=<reason>  restarts=<n>
  NOT_RUNNING=$(kubectl get pods --all-namespaces -o json 2>/dev/null | python3 -c '
import json, sys
data = json.load(sys.stdin)
for p in data["items"]:
    phase = p["status"].get("phase", "")
    if phase == "Succeeded":
        continue
    cs_list = p["status"].get("containerStatuses") or []
    if phase == "Running" and cs_list and all(c.get("ready") for c in cs_list):
        continue
    ns = p["metadata"]["namespace"]
    name = p["metadata"]["name"]
    bad = next((c for c in cs_list if not c.get("ready")), cs_list[0] if cs_list else None)
    reason, restarts = "", 0
    if bad:
        st = bad.get("state", {})
        reason = ((st.get("waiting") or {}).get("reason")
                  or (st.get("terminated") or {}).get("reason") or "")
        restarts = bad.get("restartCount", 0)
    if not cs_list:
        for cond in p["status"].get("conditions", []) or []:
            if cond.get("type") == "PodScheduled" and cond.get("status") != "True":
                reason = cond.get("reason", "Unschedulable")
                break
    print(f"{ns}/{name}\tphase={phase}\treason={reason}\trestarts={restarts}")
' || true)

  if [[ -z "$NOT_RUNNING" ]]; then
    echo "No pods in a non-Running / non-Succeeded state. Nothing to investigate."
    exit 0
  fi

  POD_COUNT=$(printf '%s\n' "$NOT_RUNNING" | wc -l | tr -d ' ')

  # Embed the list verbatim in the reason. JSON-escape via python so newlines /
  # quotes survive the curl payload below.
  REASON=$(POD_LIST="$NOT_RUNNING" POD_COUNT="$POD_COUNT" python3 -c '
import json, os
pods = os.environ["POD_LIST"].rstrip()
count = os.environ["POD_COUNT"]
msg = (
    f"{count} pod(s) are not in Running state cluster-wide. "
    "Investigate each one: identify why it is failing, group by root cause "
    "(image pull errors, crash loops, scheduling failures, OOM, etc.), and "
    "recommend remediation. Do not take destructive action without approval.\n\n"
    f"Failing pods:\n{pods}"
)
print(json.dumps(msg)[1:-1])  # strip the wrapping quotes, keep escapes
')

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
