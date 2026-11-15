#!/usr/bin/env bash
# Smoke-test the agent image locally with Docker. No cluster, no Slack.
#
#   tests/smoke/run.sh tools      agent image + replayed incident: tools load, approval gate armed (no model calls)
#   tests/smoke/run.sh sidecars   agent image + the REAL MCP sidecar image: same checks against real tool names
#   tests/smoke/run.sh incident   full incident against the replay with a real model (costs ~140k tokens)
#
# Images: AGENT_IMAGE (default k8s-deep-agent:test) and MCP_IMAGE (default k8s-mcp-gateway:test).
# Build them first, e.g.:
#   docker build -t k8s-deep-agent:test -f agent/Dockerfile .
#   docker build -t k8s-mcp-gateway:test -f infra/mcp-gateway.Dockerfile .
#
# "incident" uses AGENT_MODEL (default bedrock_converse:global.anthropic.claude-sonnet-5-5).
# For Bedrock it needs AWS credentials: AWS_PROFILE (your ~/.aws is mounted read-only) or
# AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY/AWS_SESSION_TOKEN, plus AWS_REGION.
# For OpenAI set AGENT_MODEL=openai:gpt-5.4-mini and OPENAI_API_KEY.
set -euo pipefail
MODE="${1:?usage: run.sh tools|sidecars|incident}"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
AGENT_IMAGE="${AGENT_IMAGE:-k8s-deep-agent:test}"
MCP_IMAGE="${MCP_IMAGE:-k8s-mcp-gateway:test}"
NET="k8s-agent-smoke-$$"

cleanup() { docker rm -f "$NET-kubectl" "$NET-cloudwatch" >/dev/null 2>&1 || true; docker network rm "$NET" >/dev/null 2>&1 || true; }
trap cleanup EXIT
docker network create "$NET" >/dev/null

wait_up() {  # container, port, path
  for _ in $(seq 1 60); do
    if docker run --rm --network "$NET" curlimages/curl:8.11.1 -fs "http://$1:$2$3" >/dev/null 2>&1; then return 0; fi
    sleep 2
  done
  echo "timed out waiting for $1:$2$3"; docker logs "$1" | tail -30; exit 1
}

if [ "$MODE" = "sidecars" ]; then
  # Same commands as the sidecars in infra/agent-deployment.yaml.
  docker run -d --name "$NET-kubectl" --network "$NET" "$MCP_IMAGE" \
    supergateway --stdio mcp-server-kubernetes --port 3001 --outputTransport streamableHttp --stateful --healthEndpoint /healthz >/dev/null
  docker run -d --name "$NET-cloudwatch" --network "$NET" -e AWS_REGION="${AWS_REGION:-us-east-1}" "$MCP_IMAGE" \
    supergateway --stdio awslabs.cloudwatch-mcp-server --port 3002 --outputTransport streamableHttp --stateful --healthEndpoint /healthz >/dev/null
else
  for g in kubectl:3001 cloudwatch:3002; do
    docker run -d --name "$NET-${g%%:*}" --network "$NET" -v "$REPO:/repo:ro" --entrypoint python "$AGENT_IMAGE" \
      /repo/tests/smoke/replay_mcp.py "${g%%:*}" --port "${g##*:}" >/dev/null
  done
fi
wait_up "$NET-kubectl" 3001 /healthz
wait_up "$NET-cloudwatch" 3002 /healthz

ARGS=(--tools-only)
# No model calls in tools/sidecars mode; the OpenAI client only needs a key-shaped value to construct.
ENV=(-e KUBECTL_MCP_URL="http://$NET-kubectl:3001/mcp" -e CLOUDWATCH_MCP_URL="http://$NET-cloudwatch:3002/mcp"
     -e AGENT_MODEL=openai:gpt-5.4-mini -e OPENAI_API_KEY=sk-smoke-test-not-used
     -e CLUSTER_SKILL_PATH=./skills/clusters/smoke/SKILL.md -e AGENT_MEMORY_DIR=/tmp/agent-memory)
VOL=(-v "$REPO/tests/smoke:/smoke:ro" -v "$REPO/evals-agentcore/lab-cluster/SKILL.md:/app/skills/clusters/smoke/SKILL.md:ro")
if [ "$MODE" = "incident" ]; then
  ARGS=(--state-url "http://$NET-kubectl:3001/state")
  ENV+=(-e AGENT_MODEL="${AGENT_MODEL:-bedrock_converse:global.anthropic.claude-sonnet-5-5}")
  for v in AWS_REGION AWS_DEFAULT_REGION AWS_PROFILE AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN ANTHROPIC_API_KEY; do
    [ -n "${!v:-}" ] && ENV+=(-e "$v")
  done
  [ -n "${OPENAI_API_KEY:-}" ] && ENV+=(-e OPENAI_API_KEY)   # later -e wins over the dummy key
  [ -d "$HOME/.aws" ] && VOL+=(-v "$HOME/.aws:/root/.aws:ro")
fi
docker run --rm --network "$NET" "${ENV[@]}" "${VOL[@]}" "$AGENT_IMAGE" python /smoke/run_graph.py "${ARGS[@]}"
