"""
Lambda function: CloudWatch Alarm → Slack #k8s-alerts

Triggered by SNS when the EKS node disk pressure alarm fires or resolves.
Posts a formatted alert to Slack so the agent can pick it up.

Environment variables (set via infra/setup-alert-pipeline.sh):
  SLACK_BOT_TOKEN  — xoxb-... token with chat:write scope
  SLACK_CHANNEL_ID — ID of #k8s-alerts channel (e.g. C12345678)
"""

import json
import os
import urllib.request
import urllib.error

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_CHANNEL_ID = os.environ["SLACK_CHANNEL_ID"]


def post_to_slack(text: str, blocks: list | None = None) -> None:
    payload = {"channel": SLACK_CHANNEL_ID, "text": text}
    if blocks:
        payload["blocks"] = blocks

    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = json.loads(resp.read())
        if not body.get("ok"):
            raise RuntimeError(f"Slack API error: {body.get('error')}")


def lambda_handler(event, context):
    # SNS wraps the CloudWatch alarm notification in Records[0].Sns.Message
    sns_message = json.loads(event["Records"][0]["Sns"]["Message"])

    alarm_name = sns_message.get("AlarmName", "unknown")
    state = sns_message.get("NewStateValue", "UNKNOWN")  # OK | ALARM | INSUFFICIENT_DATA
    reason = sns_message.get("NewStateReason", "")
    region = sns_message.get("Region", "ap-southeast-2")
    cluster = "otel-demo-prod"

    if state == "ALARM":
        icon = ":red_circle:"
        heading = f"{icon} *ALERT: Node disk pressure detected on cluster `{cluster}`*"
        body = (
            f"*Alarm:* {alarm_name}\n"
            f"*Reason:* {reason}\n\n"
            f"Checkout service latency may be rising. "
            f"The K8s agent will begin investigation now.\n\n"
            f"DiskPressure condition active — investigation starting."
        )
        fallback = f"ALERT: {alarm_name} — disk pressure on {cluster}"
    elif state == "OK":
        icon = ":large_green_circle:"
        heading = f"{icon} *RESOLVED: Disk pressure cleared on cluster `{cluster}`*"
        body = (
            f"*Alarm:* {alarm_name}\n"
            f"*Reason:* {reason}\n\n"
            f"Node is back to healthy state."
        )
        fallback = f"RESOLVED: {alarm_name} — disk pressure cleared on {cluster}"
    else:
        icon = ":large_yellow_circle:"
        heading = f"{icon} *CloudWatch alarm state changed: `{state}`*"
        body = f"*Alarm:* {alarm_name}\n*Reason:* {reason}"
        fallback = f"{alarm_name} state: {state}"

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": heading}},
        {"type": "section", "text": {"type": "mrkdwn", "text": body}},
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"Region: `{region}` | Cluster: `{cluster}` | Alarm: `{alarm_name}`",
                }
            ],
        },
    ]

    post_to_slack(fallback, blocks)
    return {"statusCode": 200, "body": "ok"}
