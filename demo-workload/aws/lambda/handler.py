"""SNS -> agent /trigger bridge.

CloudWatch publishes alarm state changes to the IncidentsTopic; SNS invokes
this Lambda once per record. We translate the SNS-wrapped alarm payload
into the agent's HTTP /trigger contract.

The payload shape comes from agent/main.py:686 — {state, alarm_name, node, reason}.
node is "(service-level alarm)" for our latency alarm; the agent discovers
the actual node via its X-Ray + kubectl investigation. That's the demo's
whole point — don't pre-resolve it here.

Zero external deps (urllib only) so the file inlines cleanly into the CFN
template via Code.ZipFile.
"""

import json
import os
import urllib.request

AGENT_URL = os.environ["AGENT_URL"]
TIMEOUT_S = int(os.environ.get("AGENT_TIMEOUT_S", "10"))


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

        payload = {
            "alarm_name": alarm_name,
            "state": "ALARM",
            "node": "(service-level alarm)",
            "reason": (
                f"{reason}\n"
                f"Service: {service or 'unknown'}\n"
                f"Metric: {trigger.get('MetricName', '?')}\n"
                f"Threshold: {trigger.get('Threshold', '?')}"
            ),
        }

        try:
            _post_to_agent(payload)
        except Exception as exc:
            # Re-raise so SNS retries (and ultimately routes to the DLQ)
            print(f"POST to agent failed: {exc!r}")
            raise

    return {"statusCode": 200}
