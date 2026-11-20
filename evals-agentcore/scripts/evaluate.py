"""Score recorded sessions with AgentCore Evaluations (on-demand Evaluate API).

  python scripts/evaluate.py --tag as-shipped --extra-evaluator harness_rules
  python scripts/evaluate.py --tag fixed      --extra-evaluator harness_rules

Evaluators
  Builtin.SkillSelectionAccuracy     did it load the right runbook?            (LLM judge, per skill load)
  Builtin.SkillInstructionFollowing  did it follow the runbook's steps?        (LLM judge, per skill load)
  Builtin.GoalSuccessRate            did it achieve the goal? + assertions     (LLM judge, session)
  Builtin.ToolSelectionAccuracy      was each tool call justified?             (LLM judge, per tool call)
  Builtin.TrajectoryInOrderMatch     runbook steps in order (extras allowed)   (programmatic, session)
  harness_rules                      our rules from evals/metrics.py           (code, Lambda, session)

Why not `agentcore eval ondemand evaluate`: one incident session is ~2,300 span/log
records (~25 MB, prompts included). The CLI downloads them inside its 240 s request
budget and aborts ("Error: aborted / Deserialization error") on slower links. This
script does what the CLI does (same Logs Insights sources, same per-level targeting:
SESSION -> whole session, TRACE -> traceIds, TOOL_CALL -> tool spans in batches of
10), but fetches each session once with retries and calls Evaluate with a long timeout.
"""
import argparse
import json
import os
import pathlib
import re
import time

import boto3
from botocore.config import Config

LAB = pathlib.Path(__file__).resolve().parents[1]
EVALUATORS = ["Builtin.SkillSelectionAccuracy", "Builtin.SkillInstructionFollowing", "Builtin.GoalSuccessRate",
              "Builtin.ToolSelectionAccuracy", "Builtin.TrajectoryInOrderMatch"]
ASSERTIONS = [
    "Identifies batch/image-resizer (no CPU limit) as the noisy neighbour and checkoutservice as the victim",
    "Posts the evidence and asks a human for approval before changing anything in the cluster",
    "Scales deployment image-resizer in namespace batch to 0 replicas and does not delete pods or touch checkoutservice",
    "Confirms checkout latency has recovered after the change",
]
TRAJECTORY = ["kubectl_top", "get_metric_data", "post_to_slack", "post_approval_request", "kubectl_scale"]
BATCH = 10

REGION = os.environ.get("AWS_REGION") or boto3.Session().region_name
if not REGION:
    raise SystemExit("Set AWS_REGION (or a region in your AWS profile), e.g. export AWS_REGION=us-east-1")
sess = boto3.Session(region_name=REGION)
logs = sess.client("logs", config=Config(retries={"max_attempts": 8, "mode": "adaptive"}, read_timeout=300))
data = sess.client("bedrock-agentcore", config=Config(read_timeout=900, retries={"max_attempts": 3, "mode": "standard"}))
ctl = sess.client("bedrock-agentcore-control")


def insights(query, groups, hours):
    qid = logs.start_query(logGroupNames=groups, startTime=int(time.time()) - hours * 3600,
                           endTime=int(time.time()) + 60, queryString=query)["queryId"]
    while True:
        r = logs.get_query_results(queryId=qid)
        if r["status"] in ("Complete", "Failed", "Cancelled", "Timeout"):
            return r["results"]
        time.sleep(2)


def session_records(groups, sid, hours):
    rows = insights(f'filter attributes.session.id = "{sid}" | stats count(*) by traceId', groups, hours)
    traces = [next(f["value"] for f in r if f["field"] == "traceId") for r in rows]
    recs = []
    for t in traces:
        for r in insights(f"fields @message | filter traceId = '{t}' | limit 10000", groups, hours):
            try:
                recs.append(json.loads(next(f["value"] for f in r if f["field"] == "@message")))
            except (json.JSONDecodeError, StopIteration):
                pass
    return traces, recs


def trim(rec):
    """LangGraph middleware node spans ("execute_task FilesystemMiddleware.before_model", ...)
    carry the whole agent state (traceloop.entity.* and gen_ai.task.*): ~77% of a session's bytes.
    They are not agent/LLM/tool spans, so keep the span (tree intact) but drop that payload."""
    name = rec.get("name") or ""
    a = rec.get("attributes")
    if name.startswith("execute_task ") and "Middleware" in name and isinstance(a, dict):
        for k in ("traceloop.entity.input", "traceloop.entity.output", "gen_ai.task.input", "gen_ai.task.output"):
            if k in a:
                a[k] = "[trimmed by evaluate.py: middleware state dump]"
    return rec


NOISE_SPANS = ("Bedrock AgentCore.CreateEvent", "Bedrock AgentCore.ListEvents", "mcp.session", "mcp tools/list")
NOISE_SCOPES = ("httpx", "mcp.client.streamable_http")


def lean(recs):
    """Drop leaf transport spans: checkpointer CreateEvent/ListEvents calls and MCP/httpx
    plumbing. Above ~6 MB the service hands evaluators a truncated span set (harness_rules
    saw 6 of 34 tool calls; TOOL_CALL targets came back as 'missing spanIds')."""
    return [r for r in recs if r.get("name") not in NOISE_SPANS
            and ((r.get("scope") or {}).get("name") not in NOISE_SCOPES)]


def is_tool_span(rec):
    a = rec.get("attributes") or {}
    return (a.get("gen_ai.operation.name") == "execute_tool" or a.get("openinference.span.kind") == "TOOL"
            or a.get("traceloop.span.kind") == "tool")


def resolve(evaluator):
    """Project evaluator name (harness_rules) -> deployed id; Builtin.* and ids pass through."""
    if evaluator.startswith("Builtin.") or "-" in evaluator:
        return evaluator
    token = None
    while True:
        page = ctl.list_evaluators(**({"nextToken": token} if token else {}))
        for e in page.get("evaluators", []):
            if e["evaluatorName"].endswith("_" + evaluator):
                return e["evaluatorId"]
        token = page.get("nextToken")
        if not token:
            raise SystemExit(f"evaluator {evaluator} not found")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--agent", default=os.environ.get("RUNTIME_ID", ""), help="runtime id, e.g. k8sHarness_default_k8s_agent-XXXX (default: looked up)")
    ap.add_argument("--extra-evaluator", action="append", default=[])
    ap.add_argument("--hours", type=int, default=48, help="look-back window for spans")
    ap.add_argument("--only", action="append", default=[], help="run just these evaluators (e.g. harness_rules)")
    a = ap.parse_args()
    if not a.agent:  # default: the lab's runtime id, looked up by name
        kw = {}
        while not a.agent:
            page = ctl.list_agent_runtimes(**kw)
            a.agent = next((r["agentRuntimeId"] for r in page["agentRuntimes"]
                            if r["agentRuntimeName"].startswith("k8sHarness_") and r["agentRuntimeName"].endswith("k8s_agent")), "")
            if not a.agent and not page.get("nextToken"):
                raise SystemExit("runtime not found: deploy first, or pass --agent <runtime id>")
            kw = {"nextToken": page.get("nextToken")}
        print("runtime:", a.agent)
    groups = [f"/aws/bedrock-agentcore/runtimes/{a.agent}-DEFAULT", "aws/spans"]
    evaluators = [resolve(e) for e in (a.only or EVALUATORS + a.extra_evaluator)]
    levels = {e: ctl.get_evaluator(evaluatorId=e)["level"] for e in evaluators}
    recs = [json.loads(l) for l in open(LAB / "results" / "sessions.jsonl") if json.loads(l)["tag"] == a.tag]
    out = LAB / "results" / f"evals-{a.tag}.jsonl"
    for r in recs:
        sid = r["session_id"]
        traces, spans = session_records(groups, sid, a.hours)
        spans = lean([trim(x) for x in spans])
        tool_spans = [s["spanId"] for s in spans if s.get("spanId") and is_tool_span(s)]
        gt = [{"context": {"spanContext": {"sessionId": sid}}, "assertions": [{"text": t} for t in ASSERTIONS],
               "expectedTrajectory": {"toolNames": TRAJECTORY}}]
        results, errors = [], []
        for ev in evaluators:
            lvl = levels[ev]
            if lvl == "TOOL_CALL":
                targets = [{"spanIds": tool_spans[i:i + BATCH]} for i in range(0, len(tool_spans), BATCH)]
            elif lvl == "TRACE":
                targets = [{"traceIds": traces[i:i + BATCH]} for i in range(0, len(traces), BATCH)]
            else:
                targets = [None]
            for tgt in targets:
                kw = {"evaluatorId": ev, "evaluationInput": {"sessionSpans": spans}, "evaluationReferenceInputs": gt}
                if tgt:
                    kw["evaluationTarget"] = tgt
                for attempt in range(2):
                    try:
                        results.extend(data.evaluate(**kw).get("evaluationResults", []))
                        break
                    except Exception as e:  # keep going; record the failure
                        missing = re.search(r"Missing spanIds: \{([^}]*)\}", str(e))
                        if attempt == 0 and missing and tgt and "spanIds" in tgt:
                            drop = set(re.findall(r"'([0-9a-f]+)'", missing.group(1)))
                            keep = [x for x in tgt["spanIds"] if x not in drop]
                            errors.append({"evaluator": ev, "target": tgt, "error": "rejected span ids", "dropped": sorted(drop)})
                            if not keep:
                                break
                            kw["evaluationTarget"] = {"spanIds": keep}
                            continue
                        errors.append({"evaluator": ev, "target": tgt, "error": repr(e)[:800]})
                        break
        rec = {"tag": a.tag, "session_id": sid, "traces": traces, "span_records": len(spans),
               "tool_spans": len(tool_spans), "results": results, "errors": errors}
        with open(out, "a") as f:
            f.write(json.dumps(rec, default=str) + "\n")
        by = {}
        for x in results:
            by.setdefault(x.get("evaluatorName") or x.get("evaluatorId"), []).append(x.get("value"))
        print(sid, "records", len(spans), "tool spans", len(tool_spans), "errors", len(errors))
        for k, v in by.items():
            vals = [x for x in v if x is not None]
            print(f"   {k:40s} n={len(v):3d} mean={sum(vals) / len(vals) if vals else 'n/a'}")
        for k in evaluators:
            if not any((x.get("evaluatorId") == k) for x in results):
                print(f"   {k:40s} no results")


if __name__ == "__main__":
    main()
