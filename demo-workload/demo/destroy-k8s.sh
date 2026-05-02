#!/usr/bin/env bash
# Delete demo workload Kubernetes resources. Run this BEFORE destroy-aws.sh
# so the synthesizer stops trying to assume IRSA roles that no longer exist.
#
# Usage:  bash demo/destroy-k8s.sh

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

if [[ -f "$HERE/.env" ]]; then
  # shellcheck disable=SC1091
  source "$HERE/.env"
fi

: "${AWS_PROFILE:?AWS_PROFILE not set}"
export AWS_PROFILE

echo "→ Deleting synthesizer Deployment"
kubectl -n shop-prod delete deploy/latency-synthesizer --ignore-not-found
echo "→ Deleting collector ConfigMap"
kubectl delete -f "$ROOT/manifests/20-collector-sidecar-config.yaml" --ignore-not-found
echo "→ Deleting ServiceAccounts"
kubectl -n shop-prod delete sa/latency-synthesizer sa/adot-collector --ignore-not-found
echo "→ Deleting namespace"
kubectl delete -f "$ROOT/manifests/00-namespace.yaml" --ignore-not-found
echo "✓ K8s resources removed. Now safe to run demo/destroy-aws.sh"
