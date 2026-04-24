#!/bin/bash
# Test the CloudWatch → SNS → Lambda → Slack pipeline end-to-end
# without triggering real disk pressure.
#
# Invokes the Lambda function directly with a fake SNS-wrapped
# CloudWatch ALARM payload. You should see a Slack message in #k8s-alerts
# within a few seconds.
#
# Usage:
#   bash infra/test-alert-pipeline.sh           # test ALARM
#   bash infra/test-alert-pipeline.sh ok        # test OK (resolved)

set -euo pipefail

REGION="ap-southeast-2"
CLUSTER="otel-demo-prod"
LAMBDA_NAME="eks-alarm-to-slack"
ALARM_NAME="EKS-NodeDiskPressure-${CLUSTER}"

STATE="${1:-ALARM}"  # ALARM or OK

if [[ "$STATE" == "ok" || "$STATE" == "OK" ]]; then
  NEW_STATE="OK"
  REASON="Threshold Crossed: 1 datapoint [67.2 (30/04/26 02:00 UTC)] was not greater than the threshold (75.0)."
else
  NEW_STATE="ALARM"
  REASON="Threshold Crossed: 2 datapoints [91.4 (30/04/26 02:01 UTC), 89.7 (30/04/26 02:00 UTC)] were all greater than the threshold (75.0)."
fi

FAKE_PAYLOAD=$(cat <<EOF
{
  "Records": [
    {
      "EventSource": "aws:sns",
      "Sns": {
        "Message": "{\"AlarmName\":\"${ALARM_NAME}\",\"AlarmDescription\":\"EKS node disk usage above 75% in cluster ${CLUSTER}\",\"AWSAccountId\":\"123456789012\",\"NewStateValue\":\"${NEW_STATE}\",\"NewStateReason\":\"${REASON}\",\"StateChangeTime\":\"2026-04-30T02:02:00.000+0000\",\"Region\":\"${REGION}\",\"OldStateValue\":\"OK\",\"Trigger\":{\"MetricName\":\"node_filesystem_utilization\",\"Namespace\":\"ContainerInsights\",\"StatisticType\":\"Statistic\",\"Statistic\":\"AVERAGE\",\"Unit\":null,\"Dimensions\":[{\"name\":\"ClusterName\",\"value\":\"${CLUSTER}\"}],\"Period\":60,\"EvaluationPeriods\":2,\"ComparisonOperator\":\"GreaterThanThreshold\",\"Threshold\":75.0,\"TreatMissingData\":\"notBreaching\",\"EvaluateLowSampleCountPercentile\":\"\"}}"
      }
    }
  ]
}
EOF
)

echo "==> Invoking Lambda '$LAMBDA_NAME' with state=$NEW_STATE..."
RESPONSE=$(aws lambda invoke \
  --function-name "$LAMBDA_NAME" \
  --payload "$FAKE_PAYLOAD" \
  --cli-binary-format raw-in-base64-out \
  --region "$REGION" \
  /tmp/lambda-test-response.json)

STATUS=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('StatusCode','?'))")
BODY=$(cat /tmp/lambda-test-response.json)

if [[ "$STATUS" == "200" && "$BODY" == *'"statusCode": 200'* ]]; then
  echo "    ✓ Lambda returned 200 — check #k8s-alerts in Slack"
else
  echo "    ✗ Unexpected response (HTTP $STATUS):"
  echo "      $BODY"
  exit 1
fi

echo ""
echo "If you see the alert in Slack, the pipeline is working correctly."
echo "The agent will pick up any message containing 'DiskPressure' in #k8s-alerts."
