#!/usr/bin/env bash
# Delete the demo-workload CFN stack. Polls until DELETE_COMPLETE.
#
# Usage:  bash demo/destroy-aws.sh

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -f "$HERE/.env" ]]; then
  # shellcheck disable=SC1091
  source "$HERE/.env"
fi

: "${AWS_PROFILE:?AWS_PROFILE not set}"
: "${AWS_REGION:?AWS_REGION not set}"
: "${DEMO_STACK_NAME:?DEMO_STACK_NAME not set}"

export AWS_PROFILE AWS_REGION

echo "→ Deleting $DEMO_STACK_NAME"
aws cloudformation delete-stack --stack-name "$DEMO_STACK_NAME"

echo "→ Waiting for delete to complete (this can take a few minutes)..."
aws cloudformation wait stack-delete-complete --stack-name "$DEMO_STACK_NAME"
echo "✓ $DEMO_STACK_NAME deleted"
