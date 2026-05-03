"""SNS -> agent /trigger bridge.

CloudWatch publishes alarm state changes to the IncidentsTopic; SNS invokes
this Lambda once per record. We translate the SNS-wrapped alarm payload
into the agent's HTTP /trigger contract.

The payload shape comes from agent/main.py — {state, alarm_name, node, reason}.
node is "(service-level alarm)" for our latency alarm; the agent discovers
the actual node via its investigation. That's the demo's whole point —
don't pre-resolve it here.

Also posts a fresh top-level Slack alarm card via SLACK_WEBHOOK_URL so each
alarm appears as a new channel message (not threaded under a previous one).
AWS Chatbot is NOT used for this — it threads all state changes under the
first message, making new alarms invisible.

Zero external deps (urllib only) so the file inlines cleanly into the CFN
template via Code.ZipFile.
"""

import json
import os
import time
import urllib.request

AGENT_URL = os.environ["AGENT_URL"]
DELAY_SECONDS = int(os.environ.get("DELAY_SECONDS", "0"))
TIMEOUT_S = int(os.environ.get("AGENT_TIMEOUT_S", "10"))
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")


def _post_slack_alarm_card(alarm_name: str, service: str, metric: str,
                            threshold: str, reason: str) -> None:
    """Post a fresh top-level alarm card to Slack via incoming webhook.

    Webhooks always create a new message — no threading — so every alarm
    is visible immediately in the channel.
    """
    if not SLACK_WEBHOOK_URL:
        print("SLACK_WEBHOOK_URL not set, skipping Slack alarm card")
        return

    block = {
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f":rotating_light: ALARM: {alarm_name}",
                    "emoji": True,
                },
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Service*\n{service}"},
                    {"type": "mrkdwn", "text": f"*Metric*\n{metric}"},
                    {"type": "mrkdwn", "text": f"*Threshold*\n{threshold} ms"},
                    {"type": "mrkdwn", "text": f"*Status*\n:red_circle: ALARM"},
                ],
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Reason*\n{reason}"},
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": ":robot_face: K8s agent is investigating...",
                    }
                ],
            },
        ]
    }

    req = urllib.request.Request(
        SLACK_WEBHOOK_URL,
        data=json.dumps(block).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        print(f"Slack alarm card posted: {resp.status}")


def _post_to_agent(payload: dict) -> None:
    req = urllib.request.Request(
        AGENT_URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        body = resp.read()[:200]
        print(f"Agent responded {resp.status}: {body!r}")


def handler(event, context):
    if DELAY_SECONDS:
        time.sleep(DELAY_SECONDS)
    for record in event.get("Records", []):
        sns = record.get("Sns", {})
        try:
            msg = json.loads(sns.get("Message", "{}"))
        except json.JSONDecodeError:
            print(f"Skipping non-JSON SNS message: {sns.get('Message', '')[:120]!r}")
            continue

        new_state = msg.get("NewStateValue")
        if new_state != "ALARM":
            print(f"Skipping NewStateValue={new_state}")
            continue

        alarm_name = msg.get("AlarmName", "unknown-alarm")
        reason = msg.get("NewStateReason", "")
        trigger = msg.get("Trigger", {}) or {}

        service = None
        for d in trigger.get("Dimensions", []) or []:
            if d.get("name") == "Service":
                service = d.get("value")

        metric = trigger.get("MetricName", "?")
        threshold = str(trigger.get("Threshold", "?"))

        # Post alarm card to Slack first so it's visible immediately,
        # before the agent starts its (slower) investigation.
        try:
            _post_slack_alarm_card(alarm_name, service or "unknown",
                                   metric, threshold, reason)
        except Exception as exc:
            print(f"Slack alarm card failed (non-fatal): {exc!r}")

        payload = {
            "alarm_name": alarm_name,
            "state": "ALARM",
            "node": "(service-level alarm)",
            "reason": (
                f"{reason}\n"
                f"Service: {service or 'unknown'}\n"
                f"Metric: {metric}\n"
                f"Threshold: {threshold}"
            ),
        }

        try:
            _post_to_agent(payload)
        except Exception as exc:
            print(f"POST to agent failed: {exc!r}")
            raise

    return {"statusCode": 200}
