#!/usr/bin/env bash
# Apply Kubernetes manifests for the demo workload. Pulls IRSA role ARNs
# from the demo-workload CFN stack outputs and substitutes them into the
# ServiceAccount manifest before kubectl apply.
#
# Pre-req: bash demo/deploy-aws.sh has been run successfully.
#
# Usage:  bash demo/deploy-k8s.sh

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

if [[ -f "$HERE/.env" ]]; then
  # shellcheck disable=SC1091
  source "$HERE/.env"
fi

: "${AWS_PROFILE:?AWS_PROFILE not set}"
: "${AWS_REGION:?AWS_REGION not set}"
: "${DEMO_STACK_NAME:?DEMO_STACK_NAME not set}"
: "${SYNTHESIZER_IMAGE:?SYNTHESIZER_IMAGE not set}"

export AWS_PROFILE AWS_REGION

resolve_output() {
  aws cloudformation describe-stacks \
    --stack-name "$DEMO_STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" \
    --output text
}

echo "→ Resolving stack outputs"
SYNTHESIZER_IRSA_ROLE_ARN="$(resolve_output SynthesizerIrsaRoleArn)"
COLLECTOR_IRSA_ROLE_ARN="$(resolve_output CollectorIrsaRoleArn)"

if [[ -z "$SYNTHESIZER_IRSA_ROLE_ARN" || -z "$COLLECTOR_IRSA_ROLE_ARN" ]]; then
  echo "ERROR: failed to resolve IRSA role ARNs from stack $DEMO_STACK_NAME" >&2
  exit 1
fi

export SYNTHESIZER_IRSA_ROLE_ARN COLLECTOR_IRSA_ROLE_ARN SYNTHESIZER_IMAGE

echo "→ Applying namespace"
kubectl apply -f "$ROOT/manifests/00-namespace.yaml"

echo "→ Applying ServiceAccounts (with IRSA annotations)"
envsubst < "$ROOT/manifests/10-serviceaccounts.yaml" | kubectl apply -f -

echo "→ Applying ADOT collector ConfigMap"
kubectl apply -f "$ROOT/manifests/20-collector-sidecar-config.yaml"

echo "→ Applying synthesizer Deployment"
envsubst < "$ROOT/manifests/30-latency-synthesizer.yaml" | kubectl apply -f -

echo "→ Applying low-priority PriorityClass"
kubectl apply -f "$ROOT/manifests/35-priority-class.yaml"

echo "→ Applying inventory-sync-job (noisy-neighbor, starts at 0 replicas)"
kubectl apply -f "$ROOT/manifests/40-inventory-sync-job.yaml"

echo "→ Waiting for synthesizer pod to become Ready"
kubectl -n shop-prod rollout status deploy/latency-synthesizer --timeout=120s
echo "✓ Synthesizer running. Tail logs with:"
echo "    kubectl -n shop-prod logs -l app=latency-synthesizer -c synth -f"
echo "  Run './demo spike' to scale the noisy-neighbor pod and trigger the alarm."
