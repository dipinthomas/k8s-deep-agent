"""Fire the Cyber Monday alarm at the deployed agent, play the on-call engineer, record the session.

  python scripts/run_incident.py --runs 3 --tag as-shipped
  python scripts/run_incident.py --runs 1 --tag memory-demo --use-memory

Each run: reset the replayed cluster, send the alarm, print what the agent posted to
Slack, approve the action it asks for (the human in the loop), then save a record with
the session id, tool calls and the final cluster state to results/sessions.jsonl.
"""
import argparse
import datetime
import json
import os
import pathlib
import sys
import uuid

import boto3
from botocore.config import Config

LAB = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB / "replay_lambda"))
from replay_core import ALARM  # noqa: E402

REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
sess = boto3.Session(region_name=REGION)
ac = sess.client("bedrock-agentcore", config=Config(read_timeout=900, retries={"max_attempts": 0}))
lam = sess.client("lambda")


def runtime_arn() -> str:
    if os.environ.get("RUNTIME_ARN"):
        return os.environ["RUNTIME_ARN"]
    ctl = sess.client("bedrock-agentcore-control")
    kw = {}
    while True:  # accounts with many runtimes need more than one page
        page = ctl.list_agent_runtimes(**kw)
        for r in page["agentRuntimes"]:
            if r["agentRuntimeName"].startswith("k8sHarness_") and r["agentRuntimeName"].endswith("k8s_agent"):
                return r["agentRuntimeArn"]
        if not page.get("nextToken"):
            raise SystemExit("runtime not found: deploy first, or set RUNTIME_ARN")
        kw = {"nextToken": page["nextToken"]}


def call(arn, session_id, payload):
    r = ac.invoke_agent_runtime(agentRuntimeArn=arn, runtimeSessionId=session_id,
                                payload=json.dumps(payload).encode(), contentType="application/json", accept="application/json")
    body = r["response"].read().decode()
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"raw": body}


def show(resp):
    for p in resp.get("slack", []):
        print(f"  [slack:{p['label']}] {p['text'][:300]}")
        for b in p.get("blocks", []) or []:
            print(f"      {b}")
    for a in resp.get("pending_actions", []):
        print(f"  [approval needed] {a}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--decision", default="approve")
    ap.add_argument("--use-memory", action="store_true")
    a = ap.parse_args()
    arn = runtime_arn()
    out = LAB / "results"
    out.mkdir(exist_ok=True)
    for i in range(a.runs):
        lam.invoke(FunctionName="k8s-harness-replay", Payload=json.dumps({"__lab_reset": True}).encode())
        sid = f"retail-prod-{a.tag}-{datetime.datetime.utcnow():%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"
        print(f"\n=== {a.tag} run {i + 1}/{a.runs} session {sid}")
        transcript = []
        resp = call(arn, sid, {"action": "alarm", "alarm": ALARM, "use_memory": a.use_memory})
        transcript.append(resp); show(resp)
        rounds = 0
        while resp.get("status") == "awaiting_approval" and rounds < 3:
            print(f"  [on-call] {a.decision}")
            resp = call(arn, sid, {"action": "decide", "decision": a.decision, "use_memory": a.use_memory})
            transcript.append(resp); show(resp); rounds += 1
        state = json.loads(lam.invoke(FunctionName="k8s-harness-replay",
                                      Payload=json.dumps({"__lab_state": True}).encode())["Payload"].read())
        rec = {"utc": datetime.datetime.utcnow().isoformat(), "tag": a.tag, "session_id": sid,
               "harness_version": resp.get("harness_version"), "final_status": resp.get("status"),
               "tool_calls": resp.get("tool_calls", []), "approvals": rounds,
               "image_resizer_replicas": state.get("deployments", {}).get("batch/image-resizer", 1),
               "deleted_pods": state.get("deleted_pods", []), "transcript": transcript}
        with open(out / "sessions.jsonl", "a") as f:
            f.write(json.dumps(rec) + "\n")
        print(f"  -> status={rec['final_status']} tool_calls={len(rec['tool_calls'])} "
              f"image-resizer replicas={rec['image_resizer_replicas']} deleted={rec['deleted_pods']}")


if __name__ == "__main__":
    main()
