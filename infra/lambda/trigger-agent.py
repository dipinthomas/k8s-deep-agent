"""
Lambda function: CloudWatch Alarm → Agent HTTP trigger

Triggered by SNS when a CloudWatch alarm fires.
Calls the agent's /trigger HTTP endpoint directly — no Slack credentials needed.
The agent handles all Slack communication itself.

Environment variables:
  AGENT_TRIGGER_URL — http://<agent-loadbalancer>:8080/trigger
"""

import json
import os
import urllib.request

AGENT_TRIGGER_URL = os.environ["AGENT_TRIGGER_URL"]


def lambda_handler(event, context):
    sns_message = json.loads(event["Records"][0]["Sns"]["Message"])

    alarm_name = sns_message.get("AlarmName", "unknown")
    state = sns_message.get("NewStateValue", "UNKNOWN")
    reason = sns_message.get("NewStateReason", "")
    region = sns_message.get("Region", "us-east-1")

    # Extract node name from alarm dimensions if available
    dimensions = sns_message.get("Trigger", {}).get("Dimensions", [])
    node = next((d["value"] for d in dimensions if d["name"] == "NodeName"), "unknown")

    payload = {
        "alarm_name": alarm_name,
        "state": state,
        "reason": reason,
        "region": region,
        "node": node,
    }

    print(f"Forwarding alarm to agent: {alarm_name} state={state}")

    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        AGENT_TRIGGER_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = resp.read().decode()
        print(f"Agent response: {body}")

    return {"statusCode": 200, "body": "ok"}
