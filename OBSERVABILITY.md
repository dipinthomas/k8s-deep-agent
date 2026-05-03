# Observability — Arize Phoenix Integration + Agent Evals

This document covers the tool selection analysis, the code changes made, full EKS deployment instructions, the evaluation suite design, and how to verify everything end-to-end.

---

## 1. Tool Selection Analysis

Four candidate tools were evaluated against this agent's specific constraints:

| Criterion | LangSmith | **Arize Phoenix** | Langfuse | DeepEval |
|---|---|---|---|---|
| Free self-hosting | ❌ Enterprise plan | ✅ Single Docker image | ✅ But 6 containers | ❌ Enterprise plan |
| Code changes required | 0 (env vars only) | **~5 lines** | 10–15 lines | Significant |
| LangGraph interrupt/resume | ✅ Unified trace | ✅ Unified trace | ❌ Splits per resume | N/A |
| OTel-native (vendor-neutral) | ❌ Proprietary | ✅ | ❌ | N/A |
| Async `astream()` | ✅ | ✅ | ⚠️ Known issues | N/A |
| Primary purpose | Production monitoring | Production monitoring | Production monitoring | CI/CD testing |
| Parallel subagent visualization | ✅ | ✅ | ✅ (separate traces) | N/A |

### Why each option was eliminated

**LangSmith** — Already partially wired (env vars in `.env.example`). Zero code changes. However, self-hosting requires an Enterprise plan which is gated behind sales. For a conference demo where attendees want to replicate the setup, this is a blocker.

**Langfuse** — Eliminated for a hard technical reason: every LangGraph interrupt/resume cycle creates a *separate* trace rather than continuing the existing one (GitHub issue [#10962](https://github.com/langfuse/langfuse/issues/10962)). This agent's human-in-the-loop pattern (alarm → investigation → approval gate → resume → resolution) spans multiple interrupt/resume cycles in a single investigation. Langfuse would shatter each investigation into disconnected fragments. Also shows tools that trigger `interrupt()` as ERROR in the UI, which would mislead anyone reading the traces.

**DeepEval** — Not a production monitoring tool. It is a CI/CD evaluation framework (pytest for LLMs). The trace UI and production dashboards require Confident AI, their paid cloud product. Not the right fit for a 24/7 agent that needs live trace inspection.

**Arize Phoenix** — Selected. Key reasons:
- Free self-hosting: single `docker run` command, no sign-up, no Enterprise plan
- Auto-instrumentation via OpenInference: patches LangChain's callback system, so every `astream()` call, every tool, every subagent is traced without touching any agent code
- Unified traces across interrupt/resume: the full investigation — from alarm receipt to stand-down — appears as one tree
- OTel-native: the agent already references OTel in its cluster skill context; Phoenix speaks the same protocol
- The three parallel subagents (cloudwatch, kubectl, otel-investigator) appear as three sibling spans running simultaneously — visually compelling for a conference talk

---

## 2. Code Changes Made

### `agent/observability.py` (new file)

A standalone module that calls `phoenix.otel.register()` which installs the `LangChainInstrumentor`. The instrumentation hooks into `BaseCallbackManager.configure` — LangChain's runtime callback dispatch — not the import system. This means it works even though `langgraph` is already imported at the top of `main.py`; what matters is that it fires before any agent execution begins.

The module is fully opt-in: if neither `PHOENIX_ENABLED` nor `PHOENIX_COLLECTOR_ENDPOINT` is set, it returns immediately with no overhead.

```python
# agent/observability.py — key logic
from phoenix.otel import register
register(
    project_name=project_name,       # groups traces in the Phoenix UI
    endpoint=collector_endpoint,     # where to POST spans
    auto_instrument=True,            # installs LangChainInstrumentor
    batch=True,                      # non-blocking, spans flushed in background
)
```

### `agent/main.py` (4 lines added)

Inserted immediately after `load_dotenv()` so env vars are available when `setup_phoenix()` reads them, and before any agent execution:

```python
load_dotenv()

from observability import setup_phoenix
setup_phoenix()
```

### `agent/requirements.txt` (2 packages added)

```
arize-phoenix-otel>=0.6.0
openinference-instrumentation-langchain>=0.1.0
```

### `agent/.env.example` (env vars documented)

```bash
PHOENIX_ENABLED=true
PHOENIX_PROJECT_NAME=k8s-agent
# PHOENIX_COLLECTOR_ENDPOINT=http://phoenix.k8s-agent.svc.cluster.local:6006/v1/traces
```

### `infra/phoenix-deployment.yaml` (new file)

K8s Deployment + PVC + Service for the Phoenix server, deployed into the `k8s-agent` namespace alongside the agent. The agent reaches it via cluster-local DNS. See Section 4 for full deployment steps.

---

## 3. What Phoenix Shows for This Agent

Once connected, every investigation produces a trace tree in the Phoenix UI. Here is what each level of the tree maps to:

```
Investigation trace  (thread_id = Slack thread_ts)
│
├── Turn 1 — master agent
│   ├── write_todos (tool)
│   ├── cloudwatch-investigator (subagent)  ─┐
│   ├── kubectl-investigator (subagent)      ├─ parallel spans
│   └── otel-investigator (subagent)        ─┘
│       ├── kubectl_get (tool)
│       ├── kubectl_top (tool)
│       └── ...
│
├── Turn 2 — KeepLoopingMiddleware injected correction (if agent stalled)
│
├── Turn 3 — post_to_slack (tool) + post_approval_request (tool)
│   └── [graph paused at interrupt — HITL gate armed]
│
└── Turn 4 — resumed after APPROVE
    ├── kubectl_scale (tool)   ← the destructive tool that ran
    ├── post_to_slack (outcome)
    └── mark_stand_down
```

The Phoenix UI also shows per-span:
- Token counts (prompt / completion / cache_read) and estimated cost
- Latency at each node
- Raw inputs and outputs for every tool call and model turn
- Any tool errors (visible as red spans — useful for debugging MCP failures)

---

## 4. EKS Deployment Instructions

### Prerequisites

- `kubectl` configured against the target cluster
- The `k8s-agent` namespace already exists (it is created by `infra/agent-deployment.yaml`)
- EBS CSI driver installed on the cluster (required for the PVC) — EKS Auto/Karpenter clusters have this by default

### Step 1 — Deploy Phoenix

```bash
kubectl apply -f infra/phoenix-deployment.yaml
```

This creates:
- `PersistentVolumeClaim/phoenix-data` — 10 GiB gp2/gp3 EBS volume for trace storage
- `Deployment/phoenix` — single replica running `arizephoenix/phoenix:latest`
- `Service/phoenix` — cluster-internal service on ports 6006 (UI + OTLP HTTP) and 4317 (OTLP gRPC)

Verify it is running:

```bash
kubectl -n k8s-agent get pods -l app=phoenix
# Expected: phoenix-<hash>   1/1   Running
```

### Step 2 — Add Phoenix env vars to the agent secret

Open `infra/agent-secrets.yaml` and add:

```yaml
stringData:
  PHOENIX_ENABLED: "true"
  PHOENIX_PROJECT_NAME: "k8s-agent"
  PHOENIX_COLLECTOR_ENDPOINT: "http://phoenix.k8s-agent.svc.cluster.local:6006/v1/traces"
```

Apply:

```bash
kubectl apply -f infra/agent-secrets.yaml
```

### Step 3 — Rebuild and redeploy the agent image

The two new Python packages (`arize-phoenix-otel`, `openinference-instrumentation-langchain`) must be baked into the image. Bump the version tag and push:

```bash
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  --builder multiarch-builder \
  -t dipinthomas2003/k8s-deep-agent:v62 \
  -f agent/Dockerfile \
  --push .
```

Update `infra/agent-deployment.yaml` line 105:

```yaml
image: dipinthomas2003/k8s-deep-agent:v62
```

Apply:

```bash
AWS_PROFILE=fernhub kubectl apply -f infra/agent-deployment.yaml
```

Wait for rollout:

```bash
kubectl -n k8s-agent rollout status deployment/k8s-agent
```

### Step 4 — Confirm tracing is active

Check the agent logs for the Phoenix confirmation line:

```bash
kubectl -n k8s-agent logs deployment/k8s-agent -c agent | grep -i phoenix
# Expected: Phoenix tracing active → http://phoenix.k8s-agent.svc.cluster.local:6006/v1/traces  (project: k8s-agent)
```

If you see the warning instead:

```
PHOENIX_ENABLED=true but arize-phoenix-otel is not installed
```

the image was not rebuilt with the new packages. Re-run Step 3.

### Step 5 — Open the Phoenix UI

Port-forward from your laptop (no ingress required):

```bash
kubectl port-forward -n k8s-agent svc/phoenix 6006:6006
```

Open: **http://localhost:6006**

You should see the `k8s-agent` project in the left sidebar. Traces will appear as soon as an investigation runs.

---

## 5. Local Dev / Pre-Demo Verification

To test Phoenix locally without a cluster:

```bash
# Terminal 1 — Phoenix server
docker run -p 6006:6006 -p 4317:4317 arizephoenix/phoenix:latest

# Terminal 2 — Agent (with .env containing PHOENIX_ENABLED=true)
cd agent
python main.py
```

Then trigger a synthetic investigation:

```bash
curl -X POST http://localhost:8080/trigger \
  -H "Content-Type: application/json" \
  -d '{
    "alarm_name": "HighCPUUtilization",
    "state": "ALARM",
    "node": "(service-level alarm)",
    "reason": "CPU utilization exceeded 80% threshold"
  }'
```

Open **http://localhost:6006**, select the `k8s-agent` project. Within 10–15 seconds you should see a new trace appear.

---

## 6. Autonomous Agent Smoke Test

To verify Phoenix is capturing traces correctly without manual intervention, run this sequence:

### 6a. Trigger an investigation

```bash
# Against the live cluster
curl -X POST http://<agent-load-balancer>:8080/trigger \
  -H "Content-Type: application/json" \
  -d '{
    "alarm_name": "CheckoutLatencyHigh",
    "state": "ALARM",
    "node": "(service-level alarm)",
    "reason": "P99 latency exceeded 2000ms"
  }'
```

### 6b. Watch the trace appear in Phoenix

Port-forward (if on EKS) and open http://localhost:6006. In the `k8s-agent` project:

1. Click **Traces** in the left nav
2. The investigation should appear within ~30 seconds of the trigger
3. Click the trace to expand the tree
4. Verify you can see:
   - The three subagent spans running in parallel (cloudwatch, kubectl, otel-investigator)
   - Tool call spans nested under each subagent
   - The approval gate turn (post_to_slack + post_approval_request)

### 6c. Checklist for a healthy integration

```
[ ] Trace appears in Phoenix UI within 30s of trigger
[ ] Trace name shows the thread_id (Slack thread timestamp)
[ ] Three parallel subagent spans visible on Turn 1
[ ] Token count shown per model span
[ ] Tool inputs/outputs visible (click any tool span)
[ ] No spans stuck in "pending" after investigation completes
[ ] Trace status is green (success) after stand-down, not red
```

### 6d. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| No traces in Phoenix UI | `PHOENIX_ENABLED` not set or image not rebuilt | Check agent logs for "Phoenix tracing active" |
| Traces appear but stop mid-investigation | Phoenix pod OOMKilled | Increase memory limit in `phoenix-deployment.yaml` |
| Spans show as ERROR despite successful run | LangGraph interrupt spans — expected | Ignore; these are control-flow interrupts, not failures |
| All spans flat (no nesting) | `auto_instrument=True` not working | Verify `openinference-instrumentation-langchain` is installed in the image |
| Port-forward drops after a few minutes | Normal kubectl behavior | Re-run the `port-forward` command |

---

## 7. Agent Evaluation Suite

The eval suite lives in `agent/evals/` and runs automatically after every investigation. It answers the question: *"Did this investigation go well, or did the agent cut corners?"*

### Architecture

```
agent/evals/
├── __init__.py
├── metrics.py    ← deterministic rule-based checks (no LLM cost)
├── judges.py     ← LLM-as-judge qualitative evals
└── runner.py     ← orchestrates both; posts results to Phoenix as span annotations
```

**Online mode** — fires automatically after each investigation via `main.py`:
- Rule-based evals run synchronously (microseconds, zero cost)
- LLM judges run asynchronously in the background (non-blocking)
- Results posted to Phoenix as span annotations visible alongside the trace

**Offline mode** — re-evaluate historical traces:
```bash
# From the agent/ directory:
python -m evals.runner --project k8s-agent --limit 20 --judges
```

### What Is Evaluated

#### Rule-Based (9 checks — deterministic, zero cost)

| Eval | What it checks | Fail means |
|---|---|---|
| `forbidden_tool_check` | No banned tools called (kubectl_context, port_forward, helm ops) | Agent violated the system prompt's tool restrictions |
| `parallel_subagent_dispatch` | All 3 subagents fired in the same turn as write_todos | Agent investigated sequentially instead of in parallel |
| `approval_gate_order` | post_to_slack appeared before post_approval_request | Human saw approve button without any findings context |
| `two_turn_gate_pattern` | Destructive tool in a separate turn from post_approval_request | HITL gate would strand Slack tools; buttons would be no-ops |
| `no_silent_standown` | post_to_slack called before mark_stand_down | Agent stood down without Slack visibility |
| `terminal_state_reached` | Valid ending: mark_stand_down, HITL interrupt, or stand-down phrase | Agent stalled with empty tool_calls |
| `middleware_correction_count` | Number of KeepLoopingMiddleware injections (0 = ideal) | Agent needed repeated nudging to follow the loop contract |
| `no_direct_kubectl_turn1` | Master agent didn't call kubectl directly on turn 1 (service alarms) | Bypassed the parallel subagent pattern |
| `remediation_used_scale` | Used kubectl_scale not kubectl_delete for Deployment pods | Deleted a pod that immediately restarted — no remediation effect |

Score: **1.0 = pass, 0.5 = warn, 0.0 = fail**

#### LLM-as-Judge (5 checks — qualitative, uses AGENT_MODEL)

| Eval | What it checks | LLM rubric |
|---|---|---|
| `root_cause_identification` | Did the agent name a specific resource as the cause? | pass = specific pod/deployment + evidence; fail = vague/tautological |
| `skill_md_compliance` | Did the agent follow the SKILL.md decision tree? | pass = correct target and tool per decision tree; fail = targeted the victim service |
| `remediation_target_correct` | Did it target the cause, not the alarm victim? | pass = cause targeted; fail = victim service scaled/deleted |
| `slack_message_format` | Does the Slack message match the required template? | Checks ━━━ separator, bullet format, no hedging words |
| `investigation_completeness` | Did all 3 subagents contribute to the final hypothesis? | pass = all 3 synthesised; fail = evidence from only 1–2 investigators |

### Reading Eval Results

**In Phoenix UI**: after an investigation, open the root trace span. The "Evaluations" panel on the right shows each eval as a coloured badge (green = pass, orange = warn, red = fail) with the explanation. Click any badge for the full explanation.

**In agent logs** (searchable via `grep EVAL`):
```
EVAL[RULE-BASED][PASS] [PASS] forbidden_tool_check: No forbidden tools called.
EVAL[RULE-BASED][WARN] [WARN] middleware_correction_count: 2 middleware correction(s) injected.
EVAL[LLM-JUDGE][FAIL]  [FAIL] skill_md_compliance: Agent targeted checkoutservice (victim) instead of inventory-sync-job (cause per SKILL.md).
EVALS[CheckoutLatencyHigh] 12 pass / 1 warn / 1 fail  (thread_ts=1234567890.123456  ← Phoenix span abc123)
```

### Eval Checklist for a Healthy Investigation

```
[ ] forbidden_tool_check          — pass
[ ] parallel_subagent_dispatch    — pass (all 3 in turn 0)
[ ] approval_gate_order           — pass
[ ] two_turn_gate_pattern         — pass
[ ] no_silent_standown            — pass
[ ] terminal_state_reached        — pass
[ ] middleware_correction_count   — pass (0 corrections) or warn (1–2)
[ ] no_direct_kubectl_turn1       — pass (service-level alarm)
[ ] remediation_used_scale        — pass
[ ] root_cause_identification     — pass (specific resource named)
[ ] skill_md_compliance           — pass (decision tree followed)
[ ] remediation_target_correct    — pass (cause not victim)
[ ] slack_message_format          — pass (━━━ separator, bullets, no hedging)
[ ] investigation_completeness    — pass (all 3 subagent findings synthesised)
```

Any `fail` on `skill_md_compliance` or `remediation_target_correct` is a high-priority regression — the agent targeted the wrong resource.

---

## 8. Next Steps

- **Add Phoenix to the demo runbook** — include the port-forward step in the pre-talk checklist so the UI is live during the presentation
- **Screenshot targets for the talk**: the parallel subagent span view (Turn 1 of any investigation), the token cost breakdown panel, the eval badges on the trace, and the full trace tree showing alarm-to-stand-down in one view
- **Upgrade Phoenix image tag** — replace `arizephoenix/phoenix:latest` with a pinned version (e.g. `arizephoenix/phoenix:10.0.0`) once you've validated a specific version in the demo environment, to prevent surprise breaking changes
- **Retention** — Phoenix defaults to keeping all traces. For long-running demos, consider setting `PHOENIX_SERVER_RETENTION_DAYS=7` in the Phoenix deployment env to avoid the PVC filling up
- **LangSmith** — the existing `LANGSMITH_TRACING=true` env var in the agent still works alongside Phoenix. Both can be active simultaneously if you want to compare the two UIs during the talk
- **Eval regression gate** — run `python -m evals.runner --judges --limit 5` in CI after each code change to catch regressions before deployment
