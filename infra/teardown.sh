#!/usr/bin/env bash
# Remove what infra/deploy.sh created. Leaves your cluster, its OIDC provider and
# the priority classes (other workloads may use them) in place.
#   CLUSTER_NAME=<cluster> AWS_REGION=<region> ./infra/teardown.sh
set -uo pipefail
cd "$(dirname "$0")/.."
: "${CLUSTER_NAME:?set CLUSTER_NAME}"; : "${AWS_REGION:?set AWS_REGION}"
CTX="$(kubectl config current-context 2>/dev/null)"
case "$CTX" in *"$CLUSTER_NAME"*) ;; *) echo "current kube context '$CTX' is not $CLUSTER_NAME"; exit 1 ;; esac

kubectl delete namespace k8s-agent --ignore-not-found
kubectl delete clusterrolebinding k8s-agent-cluster-binding --ignore-not-found
kubectl delete clusterrole k8s-agent-cluster-role --ignore-not-found
aws cloudformation delete-stack --region "$AWS_REGION" --stack-name "k8s-agent-iam-$CLUSTER_NAME"
aws cloudformation wait stack-delete-complete --region "$AWS_REGION" --stack-name "k8s-agent-iam-$CLUSTER_NAME"
echo "teardown done"
