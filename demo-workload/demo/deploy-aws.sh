#!/usr/bin/env bash
# Deploy the demo-workload CFN stack. Idempotent — re-running applies
# updates. Reads demo/.env for AWS profile, region, stack names, and
# (optionally) Slack workspace / channel IDs.
#
# Steps performed:
#   1. Verify cluster CFN stack exists and the agent Service is reachable.
#   2. Resolve AGENT_URL from `kubectl get svc k8s-agent` (or use override).
#   3. Inline aws/lambda/handler.py into the CFN template.
#   4. `aws cloudformation deploy` with all parameters.
#
# Usage:  bash demo/deploy-aws.sh

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

if [[ -f "$HERE/.env" ]]; then
  # shellcheck disable=SC1091
  source "$HERE/.env"
else
  echo "ERROR: $HERE/.env not found. Copy demo/.env.example and fill in." >&2
  exit 1
fi

: "${AWS_PROFILE:?AWS_PROFILE not set}"
: "${AWS_REGION:?AWS_REGION not set}"
: "${CLUSTER_STACK_NAME:?CLUSTER_STACK_NAME not set}"
: "${DEMO_STACK_NAME:?DEMO_STACK_NAME not set}"

# Optional — overrideable.
AGENT_NAMESPACE="${AGENT_NAMESPACE:-k8s-agent}"
AGENT_SERVICE="${AGENT_SERVICE:-k8s-agent}"
AGENT_PORT="${AGENT_PORT:-8080}"

# Distinguish "unset" from "explicitly set to empty" so the user can opt
# out of Chatbot by writing `export SLACK_WORKSPACE_ID=` in demo/.env.
SLACK_WORKSPACE_ID_EXPLICIT="${SLACK_WORKSPACE_ID+set}"
SLACK_CHANNEL_ID_EXPLICIT="${SLACK_CHANNEL_ID+set}"
SLACK_WORKSPACE_ID="${SLACK_WORKSPACE_ID:-}"
SLACK_CHANNEL_ID="${SLACK_CHANNEL_ID:-}"

export AWS_PROFILE AWS_REGION

echo "→ Verifying cluster stack exists: $CLUSTER_STACK_NAME"
aws cloudformation describe-stacks \
  --stack-name "$CLUSTER_STACK_NAME" \
  --query 'Stacks[0].StackStatus' \
  --output text >/dev/null

# Resolve the agent's public LB URL. Override via AGENT_URL in .env.
if [[ -z "${AGENT_URL:-}" ]]; then
  echo "→ Resolving AGENT_URL from kubectl get svc/$AGENT_SERVICE -n $AGENT_NAMESPACE"
  AGENT_HOSTNAME="$(kubectl get svc "$AGENT_SERVICE" -n "$AGENT_NAMESPACE" \
    -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)"
  if [[ -z "$AGENT_HOSTNAME" ]]; then
    echo "ERROR: could not resolve agent LB hostname. Set AGENT_URL in demo/.env" >&2
    exit 1
  fi
  AGENT_URL="http://${AGENT_HOSTNAME}:${AGENT_PORT}/trigger"
fi
echo "  AGENT_URL=$AGENT_URL"

# Resolve Slack IDs from the agent's k8s Secret unless the user explicitly
# set them in demo/.env (even to empty, which means "skip Chatbot").
# The Secret stores SLACK_TEAM_ID (= AWS Chatbot WorkspaceId) and SLACK_CHANNEL_ID.
AGENT_SECRET_NAME="${AGENT_SECRET_NAME:-k8s-agent-secrets}"
if [[ "$SLACK_WORKSPACE_ID_EXPLICIT" != "set" ]]; then
  SLACK_WORKSPACE_ID="$(kubectl get secret "$AGENT_SECRET_NAME" -n "$AGENT_NAMESPACE" \
    -o jsonpath='{.data.SLACK_TEAM_ID}' 2>/dev/null | base64 -d 2>/dev/null || true)"
fi
if [[ "$SLACK_CHANNEL_ID_EXPLICIT" != "set" ]]; then
  SLACK_CHANNEL_ID="$(kubectl get secret "$AGENT_SECRET_NAME" -n "$AGENT_NAMESPACE" \
    -o jsonpath='{.data.SLACK_CHANNEL_ID}' 2>/dev/null | base64 -d 2>/dev/null || true)"
fi

# Inline the Lambda handler into the template. CFN's Code.ZipFile is
# inline-only — there's no way to reference a sibling file directly.
# We do a single placeholder substitution into a tmp copy of the template.
TEMPLATE_SRC="$ROOT/aws/cloudformation/demo-workload.yaml"
TEMPLATE_OUT="$(mktemp -t demo-workload-XXXXXX.yaml)"
trap 'rm -f "$TEMPLATE_OUT"' EXIT

python3 - "$TEMPLATE_SRC" "$ROOT/aws/lambda/handler.py" "$TEMPLATE_OUT" <<'PYEOF'
import sys, pathlib
src, handler, dst = (pathlib.Path(p) for p in sys.argv[1:4])
template = src.read_text()
code = handler.read_text().rstrip()
# Indent the handler so it sits cleanly under `ZipFile: |` (10-space indent
# matches the placeholder line).
indented = "\n".join(("          " + line if line else "") for line in code.splitlines())
placeholder_block = (
    "          # LAMBDA_HANDLER_PLACEHOLDER\n"
    "          # deploy-aws.sh replaces this placeholder with aws/lambda/handler.py\n"
    "          # contents before running `aws cloudformation deploy`.\n"
    "          def handler(event, context):\n"
    "              raise RuntimeError(\"Lambda code not inlined — re-run deploy-aws.sh\")"
)
if placeholder_block not in template:
    print("ERROR: placeholder block not found in template — was it edited?", file=sys.stderr)
    sys.exit(1)
dst.write_text(template.replace(placeholder_block, indented))
PYEOF

# Build parameter overrides. Slack params are passed only if both are set.
PARAMS=(
  "ClusterStackName=$CLUSTER_STACK_NAME"
  "AgentUrl=$AGENT_URL"
)
if [[ -n "$SLACK_WORKSPACE_ID" && -n "$SLACK_CHANNEL_ID" ]]; then
  PARAMS+=("SlackWorkspaceId=$SLACK_WORKSPACE_ID" "SlackChannelId=$SLACK_CHANNEL_ID")
  echo "→ Chatbot will be deployed (workspace=$SLACK_WORKSPACE_ID channel=$SLACK_CHANNEL_ID)"
else
  echo "→ Chatbot SKIPPED (no SLACK_TEAM_ID/SLACK_CHANNEL_ID found in"
  echo "  $AGENT_NAMESPACE/$AGENT_SECRET_NAME and not set in demo/.env)"
fi

echo "→ Deploying $DEMO_STACK_NAME"
aws cloudformation deploy \
  --stack-name "$DEMO_STACK_NAME" \
  --template-file "$TEMPLATE_OUT" \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides "${PARAMS[@]}" \
  --tags Stack="$DEMO_STACK_NAME"

echo "→ Stack outputs:"
aws cloudformation describe-stacks \
  --stack-name "$DEMO_STACK_NAME" \
  --query 'Stacks[0].Outputs' \
  --output table
