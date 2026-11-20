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
import time
import uuid

import boto3
from botocore.config import Config

LAB = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB / "replay_lambda"))
from replay_core import ALARM  # noqa: E402

REGION = os.environ.get("AWS_REGION") or boto3.Session().region_name
if not REGION:
    raise SystemExit("Set AWS_REGION (or a region in your AWS profile), e.g. export AWS_REGION=us-east-1")
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
    try:
        return _call(arn, session_id, payload)
    except Exception as e:  # not retried: re-sending an alarm would start a second investigation
        print(f"  [error] invoke failed ({type(e).__name__}: {str(e)[:200]}). Re-run this incident.")
        return {"status": "error", "error": str(e)[:500]}


def _call(arn, session_id, payload):
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


TRACING_WARMUP_SEC = 12 * 60


def wait_for_tracing(skip: bool) -> None:
    """First deploy in a region turns on CloudWatch Transaction Search (it creates the
    aws/spans log group). Spans sent in roughly the first 12 minutes after that are
    dropped, so a session run too early can't be evaluated. Wait it out once."""
    groups = sess.client("logs").describe_log_groups(logGroupNamePrefix="aws/spans").get("logGroups", [])
    if not groups:
        print("note: CloudWatch Transaction Search isn't on yet in this region (agentcore deploy turns it on);"
              " this session's traces may be missing")
        return
    age = time.time() - groups[0]["creationTime"] / 1000
    if age >= TRACING_WARMUP_SEC or skip:
        return
    wait = int(TRACING_WARMUP_SEC - age)
    print(f"First run in {REGION}: tracing was switched on {int(age // 60)} min ago and drops spans for its first"
          f" ~12 min.\nWaiting {wait // 60} min {wait % 60} s so this session can be evaluated"
          " (Ctrl-C to stop; --no-wait to skip).")
    time.sleep(wait)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--decision", default="approve")
    ap.add_argument("--use-memory", action="store_true")
    ap.add_argument("--no-wait", action="store_true", help="don't wait for tracing warm-up in a new region")
    a = ap.parse_args()
    arn = runtime_arn()
    wait_for_tracing(a.no_wait)
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
