# ACTIONS — Pending Architecture Changes

---

## INSTRUCTIONS FOR THE IMPLEMENTING AGENT

You are a senior software engineer implementing a series of agreed architectural
changes to a Kubernetes AI agent codebase. Read these instructions fully before
touching any file.

### Your working directory
`/Users/admin/Documents/my_github/nz-tech-rally`

You have full permission to read, edit, create, and delete any file within this
directory. Do not ask for permission — just do the work.

### How to work through the actions

1. **One action at a time.** Complete an action fully before starting the next.
   Do not batch changes across multiple actions in a single pass.

2. **Read before you write.** Before editing any file, read its current content.
   Never assume what a file contains based on this document alone.

3. **Verify after each action.** After completing an action:
   - Re-read every file you changed and confirm the change is correct
   - Check for import errors, broken references, or missing dependencies
   - If the action involves a new file, confirm it is syntactically valid
   - Mark the action **Status: DONE** in this file before moving to the next

4. **Work in order.** Actions have dependencies noted in their headers.
   Respect them — do not implement an action before its dependency is done.
   The correct implementation order is:
   ```
   2 → 5 → 6 → 6b → 9 → 3 → 4 → 7 → 8 → 11 → 12 → 13 → 1 → 10
   ```
   Explanation:
   - ACTION 2 first — deletes dead files, cleans the slate
   - ACTION 5 — fixes AGENTS.md injection (6 depends on it)
   - ACTION 6 — rewrites system prompt (6b depends on it)
   - ACTION 6b — adds write_todos instruction + cleans initial message
   - ACTION 9 — restructures skills directory (agent.py references it)
   - ACTION 3 — memory limit reduction (safe now that dead deps noted)
   - ACTION 4 — dynamic interrupt_on (needs stable MCP tool list)
   - ACTION 7 — rewrites subagents (needs ACTION 4 done first per dependency)
   - ACTION 8 — fixes main.py action IDs and More Details button
   - ACTION 11 — subagent guardrails (needs ACTION 7 done first)
   - ACTION 12 — Redis memory store
   - ACTION 13 — sequence comments (needs ACTION 4 done)
   - ACTION 1 — MCP gateway split (largest change, do last among code changes)
   - ACTION 10 — README update (always last, reflects everything above)

5. **Final verification pass.** After all 13 actions are marked DONE:
   - Read `agent/agent.py`, `agent/main.py`, `agent/subagents.py`,
     `agent/memory/store.py`, and `agent/mcp/mcp_client.py` in full
   - Check for contradictions: hardcoded tool names, scenario-specific strings,
     duplicate logic, broken imports, references to deleted files
   - Check `skills/` directory structure matches what `agent.py` references
   - Check all env vars referenced in Python files are documented in
     `infra/agent-deployment.yaml` and `infra/agent-secrets.example.yaml`
   - Report any contradictions or gaps found — do not silently skip them

### What this codebase is

A production-grade Kubernetes AI agent for incident investigation and remediation.
It runs on EKS, uses Deep Agents (LangGraph), connects to kubectl and CloudWatch
via MCP servers, posts findings to Slack, and requires human approval before
any destructive action. The demo scenario is disk pressure on the otel-demo-prod
cluster — but the agent is being made generic so it can handle any incident type.

Full context is in `CLAUDE.md` at the repo root. Read it if you need background
on any decision.

### What NOT to do

- Do not add features beyond what each action specifies
- Do not refactor code that an action doesn't touch
- Do not add comments beyond what ACTION 13 specifies
- Do not change the demo flow or fault injection scripts
- Do not modify `CLAUDE.md` or `AGENTS.md` — those are already correct

---

## ACTION STATUS SUMMARY

| Action | Description | Status |
|---|---|---|
| ACTION 1 | Split MCP servers into dedicated pod | DONE |
| ACTION 2 | Delete dead tool files | DONE |
| ACTION 3 | Reduce agent pod memory limits | DONE |
| ACTION 4 | Dynamic interrupt_on from MCP tools | DONE |
| ACTION 5 | Load AGENTS.md via memory= parameter | DONE |
| ACTION 6 | Rewrite system prompt — generic | DONE |
| ACTION 6b | Wire write_todos + clean initial message | DONE |
| ACTION 7 | Rewrite subagents — smart and generic | DONE |
| ACTION 8 | Fix main.py action IDs + More Details button | DONE |
| ACTION 9 | Split skills into universal/ and clusters/ | DONE |
| ACTION 10 | Update README (always last) | TODO |
| ACTION 11 | Subagent guardrails — depth + return contract | DONE |
| ACTION 12 | Swap InMemoryStore for Redis | DONE |
| ACTION 13 | Document interrupt_on sequence with comments | DONE |

---

## DETAILED ACTIONS

---

## ACTION 7 — Rewrite subagents to be smart and scenario-agnostic

**Status:** TODO
**Depends on:** ACTION 1 (MCP gateway, so mcp_tools list is stable at startup)

### Context

`agent/subagents.py` has the same over-prescription problem we fixed in ACTION 6,
but worse — each subagent system prompt hardcodes a numbered step-by-step sequence
for disk pressure specifically. The `otel_subagent` is even deliberately rigged to
report OTel collector buffer usage "even if it looks normal" to manufacture a wrong
hypothesis.

This makes subagents brittle:
- A new incident type (OOM, network partition, certificate expiry) — the step list is
  irrelevant and the subagent fights against the investigation
- A new MCP tool version with renamed tools — steps call tools that no longer exist
- The manufactured wrong hypothesis is a demo hack visible in the source code —
  not something you'd want in a real codebase

The skills layer already does the right thing. `skills/node-disk-pressure/SKILL.md`
Step 4 already documents OTel collector as a "common red herring" — the agent will
naturally check it and find it normal, which IS the wrong hypothesis moment. The skill
produces the behaviour; the subagent doesn't need to rig it.

### What makes a subagent "smart"

A smart subagent has three things and nothing more:

1. **Role identity** — what domain it owns and how to approach it
2. **Epistemics** — how to think about using tools (start broad, drill down,
   don't assume tool names — discover them; adapt if a tool call fails)
3. **Output contract** — what structured data to return so the master agent
   can synthesise across all three subagents cleanly

It does NOT have:
- Scenario-specific investigation steps (those belong in skills)
- Hardcoded tool names (MCP tool names may change between versions)
- Hardcoded service names or namespaces (those belong in AGENTS.md)

### What to change

#### `agent/subagents.py` — rewrite all three subagent system prompts

**cloudwatch_subagent** — before:
```python
"system_prompt": (
    "You are a CloudWatch specialist. Query metrics and logs efficiently. "
    "Always include timestamps and units in your findings. "
    "Return structured data the master agent can act on.\n\n"
    "For disk pressure incidents, query in this order:\n"
    "1. Node filesystem utilisation for all nodes in the otel-demo-prod cluster\n"
    "2. Container filesystem usage bytes grouped by pod name\n"
    "3. CloudWatch Logs Insights on /aws/containerinsights/otel-demo-prod/performance "
    "   — disk write rates per pod in the last 15 minutes\n"
    "Report: top 5 disk consumers with write rates and total usage."
),
```

**cloudwatch_subagent** — after:
```python
"system_prompt": (
    "You are a CloudWatch specialist. Your job is to surface metric and log evidence "
    "that the master agent cannot see from kubectl alone.\n\n"
    "How to work:\n"
    "- Start by listing available tools to understand what you can query.\n"
    "- Go broad first (node-level or cluster-level metrics), then drill into the "
    "  specific resources that look anomalous.\n"
    "- If a tool call fails or returns no data, try a different metric name or time window "
    "  — do not stop investigating.\n"
    "- Always include timestamps, units, and the time window in every data point you return.\n\n"
    "Output contract — always return:\n"
    "- Top disk/CPU/memory consumers (whichever is relevant), with rates not just totals\n"
    "- The metric names and namespaces you queried (so the master agent can follow up)\n"
    "- Any CloudWatch alarms currently in ALARM state\n"
    "- Explicit call-out if any metric is within normal range (absence of evidence matters)\n"
),
```

**kubectl_subagent** — before:
```python
"system_prompt": (
    "You are a Kubernetes specialist. READ ONLY — never modify anything.\n\n"
    "For disk pressure incidents, check in this order:\n"
    "1. Describe all nodes — look for DiskPressure=True condition\n"
    "2. List pods in otel-demo namespace with -o wide — identify pods on the affected node\n"
    "3. Describe the top disk-consumer pods\n"
    "4. Get recent events in otel-demo namespace sorted by timestamp\n"
    "5. Get current CPU and memory usage for pods in otel-demo\n\n"
    "Return: node name, DiskPressure status, list of pods on node with their "
    "priority classes, and any relevant events."
),
```

**kubectl_subagent** — after:
```python
"system_prompt": (
    "You are a Kubernetes cluster state specialist. READ ONLY — you must never "
    "modify, patch, delete, or restart anything. Your job is to give the master "
    "agent a precise picture of what the cluster looks like right now.\n\n"
    "How to work:\n"
    "- Start by listing available tools. Do not assume tool names — discover them.\n"
    "- Start at the node level (conditions, resource pressure, taints), then move to "
    "  pod level (status, phase, restarts, resource usage, placement).\n"
    "- Always check recent events — they often explain what kubectl describe does not.\n"
    "- If a tool returns an error, note it and try an equivalent tool or narrower query.\n\n"
    "Output contract — always return:\n"
    "- Node conditions (DiskPressure, MemoryPressure, PIDPressure, Ready) for all nodes\n"
    "- For any node with a pressure condition: list of pods on that node with their "
    "  priority class and resource usage\n"
    "- Recent warning events (last 15 minutes) across the namespace\n"
    "- Any pods in non-Running phase (Pending, Evicted, OOMKilled, CrashLoopBackOff)\n"
),
```

**otel_subagent** — before (deliberately rigged):
```python
"system_prompt": (
    "You are an observability specialist. Focus on service health and user-facing impact.\n\n"
    "For disk pressure incidents, check:\n"
    "1. CloudWatch metric: checkoutservice p99 latency (last 15 min)\n"
    "2. CloudWatch metric: paymentservice error rate (last 15 min)\n"
    "3. OTel collector pod — describe the pod and check emptyDir volume usage\n"
    "   — report the fill percentage even if it looks normal (this is important)\n"
    "4. CloudWatch Logs Insights: trace data volume written by otel-collector\n\n"
    "IMPORTANT: Always report OTel collector buffer usage explicitly.\n"
    "Report: checkout p99 latency, payment error rate, cart service health, "
    "otel-collector buffer fill %."
),
```

**otel_subagent** — after:
```python
"system_prompt": (
    "You are an application observability specialist. Your job is to assess the "
    "health and performance of services from the application layer — traces, "
    "metrics, and logs that describe what users are experiencing, not what the "
    "infrastructure is doing.\n\n"
    "How to work:\n"
    "- Start by listing available tools. Query what you can, not what you assume exists.\n"
    "- Focus on user-facing symptoms first: latency percentiles, error rates, "
    "  throughput changes. Then look for the cause at the application layer.\n"
    "- Report both what is degraded AND what is healthy — healthy baselines help "
    "  the master agent understand blast radius.\n"
    "- If a metric or trace query returns no data, say so explicitly. Do not infer.\n\n"
    "Output contract — always return:\n"
    "- Latency (p50, p99) and error rate for each critical service you can observe\n"
    "- Whether the observability pipeline itself (OTel collector) is healthy — "
    "  report its status even if normal, because a silently-failed collector means "
    "  all other metrics may be stale\n"
    "- The time window you queried and any gaps in data\n"
    "- A one-line user impact summary the master agent can quote in Slack\n"
),
```

#### `agent/subagents.py` — remove the module docstring's wrong-hypothesis reference

The docstring (lines 14–17) documents the intentional rigging. Remove those lines
after rewriting the system prompts — they no longer apply.

Replace the module docstring with:
```python
"""
Subagent definitions for parallel incident investigation.

Each subagent owns a domain (CloudWatch metrics, K8s cluster state, OTel traces).
They receive the full MCP tool list and use their role + skills to decide what to
query — no hardcoded investigation steps. Scenario-specific playbooks live in skills/.
"""
```

### Why the wrong hypothesis still happens (naturally)

The `node-disk-pressure` SKILL.md Step 4 says:
> "Check OTel collector buffer... Note: The OTel collector emptyDir buffer is a
>  common red herring. It is usually within normal limits. Always verify imageprovider first."

The `otel_subagent` output contract now explicitly says to report OTel collector
status "even if normal". So when the master agent synthesises the three reports:
- CloudWatch: imageprovider disk writes are spiking
- kubectl: DiskPressure=True, pods on node listed
- otel: OTel collector buffer at 34% (normal) — plus checkout latency rising

The master agent sees "OTel collector buffer at 34%" as a data point. Combined with
the skill's framing that this is a common red herring, it may (correctly) consider
and then dismiss the OTel collector hypothesis before arriving at imageprovider.
This is emergent reasoning, not a scripted detour — which is exactly what makes it
convincing on stage and credible in production.

### What this enables

- New incident type → write a new SKILL.md. Subagents adapt automatically.
- New cluster → update AGENTS.md. Subagent prompts need no changes.
- New MCP tools → subagents discover them via tool listing. No prompt changes needed.
- A real team deploying this → the subagent prompts read as legitimate engineering
  practice, not demo scaffolding.

---

## ACTION 6b — Wire `write_todos` planning into system prompt and initial message

**Status:** TODO
**Depends on:** ACTION 6 (generic system prompt must be in place first)

### Context

`create_deep_agent` provides a built-in `write_todos` tool to every Deep Agent
automatically — no configuration needed. It allows the agent to:
- Decompose a complex incident into discrete steps at the start
- Track which steps are done, in-progress, or blocked
- Rewrite the plan mid-investigation when a hypothesis fails (the re-planning moment)
- Resume from the correct step after a LangGraph checkpoint (the human approval pause)

**What we have:** `write_todos` is available because `create_deep_agent` is used.
`MemorySaver` checkpointer is configured so plan state survives pause/resume.

**What's missing — two things:**

**1. The system prompt doesn't instruct the agent to use `write_todos`.**
Without an explicit instruction, the agent may or may not call it depending on
model judgment. For reliable planning behaviour — especially on stage — it must
be instructed. Deep Agents docs explicitly recommend this.

**2. The initial message in `main.py` pre-scripts the investigation (lines 112–124).**
It tells the agent exactly what to do step by step:
```python
initial_message = (
    "Read AGENTS.md to understand the cluster. "
    "Investigate this incident using your subagents. "
    "Post all findings to Slack as you go. "
    "When you have enough evidence, post an approval request..."
)
```
This gives the agent a ready-made plan before it can build its own. The agent
reads this and skips `write_todos` entirely — the planning capability is bypassed
at the point it matters most.

### What to change

#### `agent/agent.py` — add `write_todos` instruction to the new system prompt (from ACTION 6)

Append to the system prompt after the operating rules:

```python
SYSTEM_PROMPT = """
You are an autonomous Kubernetes operations agent.

Before every investigation:
- Read AGENTS.md to understand this cluster — its services, priorities, and rules.
- Select the skill that matches the incident type. Skills contain investigation playbooks.
- Use whatever tools are available to gather evidence. Do not assume which tools exist —
  discover them and use the ones that answer the question.

At the start of every investigation:
- Call write_todos to decompose the incident into discrete investigation steps.
- Update your todos as findings emerge — add steps, complete them, or replan entirely
  if your hypothesis turns out to be wrong. Replanning is expected and correct.
- After human approval and execution, mark all steps complete and write the outcome
  to long-term memory.

Non-negotiable rules:
- ALWAYS ask for human approval before any action that modifies cluster state.
- ALWAYS post evidence to Slack before asking for approval.
- ALWAYS write the outcome to long-term memory after resolution.
- NEVER guess. If you are unsure, ask.
"""
```

#### `agent/main.py` — strip the step-by-step script from the initial message

The initial message should hand the agent the alarm context and nothing more.
The agent builds its own plan from there.

Before (current — pre-scripted):
```python
initial_message = (
    f"A CloudWatch alarm has fired:\n\n"
    f"Alarm: {alarm_name}\n"
    f"Node: {node}\n"
    f"Reason: {reason}\n\n"
    "Read AGENTS.md to understand the cluster. "
    "Investigate this incident using your subagents. "
    "Post all findings to Slack as you go. "
    "When you have enough evidence, post an approval request "
    "with APPROVE and DENY buttons and wait for human confirmation. "
    "The human may ask follow-up questions in the thread before deciding — "
    "answer them fully and re-post the approval buttons each time."
)
```

After (alarm context only — agent plans from here):
```python
initial_message = (
    f"CloudWatch alarm fired:\n\n"
    f"Alarm: {alarm_name}\n"
    f"Node: {node}\n"
    f"Reason: {reason}\n\n"
    f"Slack thread: {thread_ts}\n"
    f"Channel: {channel}\n"
)
```

### Why this matters for the demo

The `write_todos` re-planning moment is the most important reasoning signal in the talk.
Without explicit instruction + a clean initial message, the agent follows the pre-scripted
path and never visibly replans. With both changes:

1. Agent receives alarm context
2. Agent calls `write_todos` → builds its own investigation plan (visible in LangSmith traces)
3. Subagents run, otel_subagent reports OTel collector as normal
4. Agent updates todos — marks OTel collector hypothesis investigated, adds new step for imageprovider
5. Agent arrives at root cause through its own reasoning

Steps 2 and 4 are now traceable, auditable, and genuine — not scripted.

---

## ACTION 8 — Fix main.py scenario-specific action IDs and missing More Details button

**Status:** TODO
**Depends on:** Nothing (can do now)

### Context

`agent/main.py` has two issues that conflict with making the agent generic:

**Issue 1 — Scenario-hardcoded action IDs**

The Slack button action IDs are named `approve_eviction` and `deny_eviction`:
```python
@slack_app.action("approve_eviction")
def handle_approve(ack, body, say): ...

@slack_app.action("deny_eviction")
def handle_deny(ack, body, say): ...
```

And in `post_approval_block()`:
```python
{"action_id": "approve_eviction"},
{"action_id": "deny_eviction"},
```

If the agent is handling an OOM kill or a certificate expiry, these IDs are
semantically wrong — there's nothing being "evicted". More practically: when
`tools/slack_tools.py` calls `post_approval_request`, it posts its own Block Kit
block with its own action IDs. The re-post in `post_approval_block()` (called after
Q&A in the thread) uses different hardcoded action IDs — these may not match what
`post_approval_request` used, so the buttons may route to wrong handlers.

**Issue 2 — `more_details` handler is registered but button is never rendered**

`handle_more_details` (line 273) handles action ID `more_details`, but
`post_approval_block()` only renders APPROVE and DENY buttons. The More Details
button will never appear in Slack, so `handle_more_details` is dead code.

### What to change

#### `agent/main.py` — rename action IDs to generic names

Rename everywhere (action IDs in decorators AND in the block elements):
- `approve_eviction` → `agent_approve`
- `deny_eviction` → `agent_deny`

Also rename the corresponding Slack button labels to match:
- "✅ Approve" stays — it's already generic
- "🚫 Deny" stays — it's already generic

#### `agent/main.py` — add More Details button to `post_approval_block()`

Add a third button to the actions block in `post_approval_block()`:
```python
{
    "type": "button",
    "text": {"type": "plain_text", "text": "🔍 More Details"},
    "action_id": "agent_more_details",
},
```

And rename the handler decorator:
```python
@slack_app.action("agent_more_details")
def handle_more_details(ack, body, say): ...
```

#### `agent/tools/slack_tools.py` — align action IDs

The `post_approval_request` tool in `slack_tools.py` posts the first approval block
with its own action IDs. Those must match the handlers in `main.py`.

Check `slack_tools.py` and update any `approve_eviction` / `deny_eviction` /
`more_details` action IDs to `agent_approve` / `agent_deny` / `agent_more_details`.

---

## ACTION 1 — Split MCP servers into a dedicated pod

**Status:** TODO

### Context

Currently both MCP servers (`mcp-server-kubernetes` and `awslabs.cloudwatch-mcp-server`)
run as stdio subprocesses inside the agent pod (`mcp_client.py`). This was fine for the
initial demo but has production problems:

- Three runtimes (Python + Node.js + uvx/Python) share one pod's memory budget
- All subagent tool calls are serialised through one asyncio event loop
- If either MCP subprocess crashes, the agent loses all tools silently
- Can't scale or restart MCP servers independently of the agent

### What to build

**New pod: `k8s-mcp-gateway`** — a single pod (or deployment) that runs both MCP servers
and exposes them over HTTP/SSE so the agent can reach them via a Kubernetes Service.

```
agent pod  ──HTTP/SSE──►  k8s-mcp-gateway pod
                              ├── mcp-server-kubernetes   (kubectl MCP, port 3001)
                              └── awslabs.cloudwatch-mcp-server (CloudWatch MCP, port 3002)
```

The agent connects to them by internal DNS:
- `http://k8s-mcp-gateway.otel-demo.svc.cluster.local:3001` (kubectl)
- `http://k8s-mcp-gateway.otel-demo.svc.cluster.local:3002` (cloudwatch)

### Files to create / modify

#### 1. `infra/mcp-gateway-deployment.yaml` (NEW)

Create a Deployment + Service for the MCP gateway pod:

- **ServiceAccount:** `k8s-mcp-gateway` with IRSA annotation pointing to the same
  `k8s-agent-irsa` IAM role (needs CloudWatch + X-Ray read access)
- **RBAC:** same ClusterRole as the agent (node reader) + Role in otel-demo namespace
  (pods, logs, events, deployments — read only for MCP, no delete verbs needed here)
- **Container 1 — kubectl-mcp** (port 3001):
  - Image: `ghcr.io/strowk/mcp-server-kubernetes:latest`
    (or whichever image exposes HTTP/SSE transport for `mcp-server-kubernetes`)
  - Transport: HTTP/SSE on port 3001
  - Auth: in-cluster ServiceAccount token (auto-mounted)
  - Env: `KUBECONFIG` unset (uses in-cluster config)
- **Container 2 — cloudwatch-mcp** (port 3002):
  - Run `uvx awslabs.cloudwatch-mcp-server@latest` with `--transport sse --port 3002`
  - Auth: IRSA (EKS injects `AWS_ROLE_ARN` + `AWS_WEB_IDENTITY_TOKEN_FILE`)
  - Env: `AWS_REGION=ap-southeast-2`
- **Resources per container:** requests 128Mi/100m, limits 512Mi/500m
- **Service:** ClusterIP (internal only), ports 3001 and 3002
- **PriorityClass:** `infrastructure` (same as agent)
- **Liveness probes:** HTTP GET on each port's `/health` or MCP ping endpoint

#### 2. `agent/mcp/mcp_client.py` (MODIFY)

Replace the stdio `_SERVER_CONFIG` with SSE/HTTP URLs pointing to the gateway service:

```python
_SERVER_CONFIG = {
    "kubectl": {
        "transport": "sse",
        "url": os.environ.get(
            "KUBECTL_MCP_URL",
            "http://k8s-mcp-gateway.otel-demo.svc.cluster.local:3001/sse",
        ),
    },
    "cloudwatch": {
        "transport": "sse",
        "url": os.environ.get(
            "CLOUDWATCH_MCP_URL",
            "http://k8s-mcp-gateway.otel-demo.svc.cluster.local:3002/sse",
        ),
    },
}
```

The `MCPClientManager` class can be simplified significantly — no asyncio background thread
needed for HTTP transport. `MultiServerMCPClient` handles HTTP/SSE natively.

Remove the 90-second subprocess startup timeout — HTTP connections fail fast and retry cleanly.

#### 3. `agent/mcp/servers.yaml` (MODIFY)

Update to reflect the new HTTP transport and service URLs.
Remove the stdio subprocess documentation. Add local dev override instructions:
```
# Local dev: set KUBECTL_MCP_URL and CLOUDWATCH_MCP_URL to point at
# locally-running MCP servers (e.g. via `npx mcp-server-kubernetes --http`)
```

#### 4. `agent/Dockerfile` (MODIFY)

Remove these lines — no longer needed in the agent image:
```dockerfile
RUN apt-get install -y nodejs npm          # remove
RUN npm install -g mcp-server-kubernetes  # remove
RUN pip install --no-cache-dir uv          # remove
RUN uvx awslabs.cloudwatch-mcp-server@latest --help  # remove
```

The agent image becomes pure Python — much smaller and faster to build.

#### 5. `infra/mcp-gateway.Dockerfile` (NEW)

Dockerfile for the gateway image:
```dockerfile
FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates nodejs npm && rm -rf /var/lib/apt/lists/*
RUN npm install -g mcp-server-kubernetes
RUN pip install --no-cache-dir uv
RUN uvx awslabs.cloudwatch-mcp-server@latest --help 2>/dev/null || true
# Entrypoint is not a single process — the K8s deployment runs two containers,
# each with their own command. This image just packages the dependencies.
```

#### 6. `agent/agent-deployment.yaml` (MODIFY)

Bump agent pod resources now that Node.js and uvx are gone:
- requests: 256Mi / 250m  (down from 512Mi)
- limits: 512Mi / 500m    (down from 1Gi)

Add env vars for MCP URLs (with cluster-local defaults already in mcp_client.py):
```yaml
- name: KUBECTL_MCP_URL
  value: "http://k8s-mcp-gateway.otel-demo.svc.cluster.local:3001/sse"
- name: CLOUDWATCH_MCP_URL
  value: "http://k8s-mcp-gateway.otel-demo.svc.cluster.local:3002/sse"
```

#### 7. `infra/deploy-all.sh` (MODIFY)

After Step 4 (agent deployment), add a Step 4b:
```bash
step "Step 4b — MCP Gateway"
kubectl apply -f "$SCRIPT_DIR/mcp-gateway-deployment.yaml"
kubectl rollout status deployment/k8s-mcp-gateway -n "$NAMESPACE" --timeout=120s
ok "MCP gateway running"
```

### Things to verify before implementing

- Confirm `mcp-server-kubernetes` supports `--transport http` or `--transport sse`.
  Check: `npx mcp-server-kubernetes --help` or the repo README.
- Confirm `awslabs.cloudwatch-mcp-server` supports `--transport sse --port <N>`.
  Check: `uvx awslabs.cloudwatch-mcp-server@latest --help`
- Confirm `langchain-mcp-adapters` `MultiServerMCPClient` accepts `transport: sse` with a URL.
  Check the langchain-mcp-adapters docs/source for the SSE config shape.

### Local dev impact

With stdio gone, local dev needs the two MCP servers running separately:
```bash
# Terminal 1
npx mcp-server-kubernetes --transport http --port 3001

# Terminal 2
uvx awslabs.cloudwatch-mcp-server@latest --transport sse --port 3002

# Terminal 3 — agent
KUBECTL_MCP_URL=http://localhost:3001/sse \
CLOUDWATCH_MCP_URL=http://localhost:3002/sse \
python agent/main.py
```

Document this in the repo README under "Local Development".

---

## ACTION 6 — Rewrite system prompt to be generic and non-prescriptive

**Status:** TODO  
**Depends on:** ACTION 5 (AGENTS.md moved to `memory=`)

### Context

The current system prompt in `agent/agent.py` lines 20–44 is over-prescribed:

- It tells the agent exactly which 9 steps to follow for one specific scenario
- It hardcodes 3 subagent names to spawn — limiting investigation to those three
- It names specific services never to evict — that belongs in AGENTS.md, not here
- It tells the agent to use specific tools (`[APPROVE] [DENY]` buttons) — tool choice
  should be the agent's decision based on what's available
- It assumes every incident is a disk pressure eviction scenario

This makes the agent a scripted responder, not an autonomous investigator. If a new
incident type arrives that doesn't fit the 9-step script, the agent is confused.

### What to change

#### `agent/agent.py` — replace `SYSTEM_PROMPT` entirely

Before (current — 25 lines, scenario-specific):
```python
SYSTEM_PROMPT = """
{agents_md}

---

You are an autonomous Kubernetes operations agent for the otel-demo-prod cluster (EKS ap-southeast-2).

Investigation approach:
1. Acknowledge the incident in Slack immediately.
2. Spawn your three subagents in PARALLEL: cloudwatch-investigator, kubectl-investigator, otel-investigator.
3. Analyse their combined findings. It is normal to revise your hypothesis — post updates to Slack as you go.
4. Post CloudWatch evidence to Slack BEFORE asking for approval.
5. Build a ranked eviction list (lowest priority first). NEVER include payment-critical services.
6. Post an approval request with [APPROVE] [DENY] [GIVE ME MORE DETAILS] buttons and WAIT.
7. Only execute evictions after explicit human approval.
8. Post a resolution summary with before/after metrics.
9. Write the incident and root cause to long-term memory.

Rules you must never break:
- Never evict checkoutservice, paymentservice, cartservice, or productcatalogservice.
- Never drain a node without approval.
- Never delete a PVC.
- Always show evidence before asking for approval.
- If unsure, ask — do not guess.
"""
```

After (new — identity + safety rules only):
```python
SYSTEM_PROMPT = """
You are an autonomous Kubernetes operations agent.

Before every investigation:
- Read AGENTS.md to understand this cluster — its services, priorities, and rules.
- Select the skill that matches the incident type. Skills contain investigation playbooks.
- Use whatever tools are available to gather evidence. Do not assume which tools exist —
  discover them and use the ones that answer the question.

Non-negotiable rules:
- ALWAYS ask for human approval before any action that modifies cluster state.
- ALWAYS post evidence to Slack before asking for approval.
- ALWAYS write the outcome to long-term memory after resolution.
- NEVER guess. If you are unsure, ask.
"""
```

### Why each line is there and nothing more

| Line | Why it's in the system prompt |
|---|---|
| Read AGENTS.md | Grounds every investigation in cluster-specific context |
| Select the skill | Delegates HOW to investigate to the skills layer |
| Use whatever tools are available | Gives the agent full autonomy over tool selection |
| Ask for approval before modifying | Non-negotiable safety rule — must always apply |
| Post evidence before approval | Non-negotiable process rule |
| Write to memory after resolution | Non-negotiable — institutional knowledge must persist |
| Never guess | Non-negotiable — prevents confident wrong actions |

### What moves out of the system prompt and where it goes

| Removed from system prompt | Moved to |
|---|---|
| 9-step investigation sequence | `skills/node-disk-pressure/SKILL.md` (already there) |
| "spawn 3 subagents in parallel" | Agent's own judgment based on incident |
| Specific service names (checkoutservice etc.) | `AGENTS.md` via `memory=` |
| "post approval request with buttons" | Agent selects `post_approval_request` tool naturally |
| "build a ranked eviction list" | `skills/pod-priority-eviction/SKILL.md` (already there) |

### What this enables

- New incident type (e.g. OOM kills, network partition, certificate expiry) →
  write a new SKILL.md, agent handles it. Zero code changes.
- New cluster → write a new AGENTS.md, deploy. Zero code changes.
- New MCP tools deployed → agent discovers and uses them. Zero code changes.
- More subagents added → agent uses them when relevant. Zero code changes.

The system prompt becomes stable infrastructure. It should almost never need to change.

---

## ACTION 5 — Load AGENTS.md via the `memory` parameter, not string formatting

**Status:** TODO  
**Depends on:** Nothing (can do now)

### Context

`agent/agent.py` currently loads AGENTS.md by reading it manually and injecting it into
the system prompt via Python string formatting:

```python
# agent/agent.py lines 18, 59-61
_AGENTS_MD = pathlib.Path(__file__).parent.parent / "AGENTS.md"

agents_md = _AGENTS_MD.read_text() if _AGENTS_MD.exists() else ""
system_prompt = SYSTEM_PROMPT.format(agents_md=agents_md).strip()
```

And the system prompt has a placeholder:
```python
SYSTEM_PROMPT = """
{agents_md}
---
You are an autonomous Kubernetes operations agent...
"""
```

This works but is a manual workaround. `create_deep_agent` has a dedicated `memory`
parameter designed exactly for this — it loads markdown files into the agent's context
management system natively, which is the correct way to use the framework.

### What to change

#### `agent/agent.py` (MODIFY)

1. Remove `_AGENTS_MD` path constant
2. Remove the `{agents_md}` placeholder from `SYSTEM_PROMPT` and the `---` separator below it
3. Remove the `agents_md` read and `SYSTEM_PROMPT.format(...)` call
4. Add `memory=["./AGENTS.md"]` to `create_deep_agent()`

Before:
```python
_AGENTS_MD = pathlib.Path(__file__).parent.parent / "AGENTS.md"

SYSTEM_PROMPT = """
{agents_md}

---

You are an autonomous Kubernetes operations agent...
"""

def build_agent():
    agents_md = _AGENTS_MD.read_text() if _AGENTS_MD.exists() else ""
    system_prompt = SYSTEM_PROMPT.format(agents_md=agents_md).strip()
    ...
    agent = create_deep_agent(
        ...
        system_prompt=system_prompt,
    )
```

After:
```python
SYSTEM_PROMPT = """
You are an autonomous Kubernetes operations agent for the otel-demo-prod cluster (EKS ap-southeast-2).
...
"""

def build_agent():
    ...
    agent = create_deep_agent(
        ...
        memory=["./AGENTS.md"],
        system_prompt=SYSTEM_PROMPT,
    )
```

### Why this is better

- The framework manages how and when AGENTS.md is injected — it can reload it,
  summarise it, or reference it selectively based on context window pressure.
  Manual string formatting always dumps the full file into every prompt unconditionally.
- `system_prompt` stays clean — just agent behaviour rules, no cluster identity mixed in.
- If AGENTS.md grows or changes, no code changes needed — the `memory` parameter
  picks up the latest version on restart automatically.
- Removes the silent failure mode where a missing AGENTS.md produces an empty
  string with no warning (`if _AGENTS_MD.exists() else ""`).

---

## ACTION 4 — Dynamic interrupt_on derived from MCP tools

**Status:** TODO  
**Depends on:** ACTION 1 (MCP gateway, so tool list is stable at startup)

### Context

`agent/agent.py` lines 49–55 hardcode destructive tool names:

```python
_INTERRUPT_TOOLS = {
    "kubectl_delete": True,
    "kubectl_drain": True,
    "delete_resource": True,
    "evict_pod": True,
    "apply_manifest": True,
}
```

This is a guess. If the MCP server names a tool differently (e.g. `pod_evict` vs `evict_pod`),
the interrupt never fires and the agent executes destructive actions without human approval.
Worse, if a future MCP version adds new destructive tools (e.g. `scale_deployment`,
`restart_rollout`), they run unguarded automatically.

### What to build

Replace the hardcoded dict with a function that inspects the actual tool objects returned
by the MCP server at startup, using tool name and description as signals.

#### `agent/agent.py` (MODIFY)

Remove `_INTERRUPT_TOOLS` entirely. Add this function above `build_agent()`:

```python
# Keywords in a tool NAME that signal it is destructive
_DESTRUCTIVE_NAME_KEYWORDS = {
    "delete", "drain", "evict", "apply", "patch",
    "scale", "restart", "create", "update", "replace",
}

# Keywords in a tool DESCRIPTION that signal it is destructive
_DESTRUCTIVE_DESC_KEYWORDS = {
    "modif", "creat", "remov", "destroy", "delet",
    "evict", "drain", "apply", "patch", "restart", "scale",
}


def _build_interrupt_on(mcp_tools: list) -> dict:
    """
    Derive the interrupt_on map dynamically from the MCP tool list.
    Any tool whose name or description contains a destructive keyword
    requires human approval before execution.
    This means new destructive tools added in future MCP server versions
    are caught automatically without changing this code.
    """
    interrupt_on = {}
    for tool in mcp_tools:
        name_lower = tool.name.lower()
        desc_lower = (tool.description or "").lower()

        name_match = any(kw in name_lower for kw in _DESTRUCTIVE_NAME_KEYWORDS)
        desc_match = any(kw in desc_lower for kw in _DESTRUCTIVE_DESC_KEYWORDS)

        if name_match or desc_match:
            interrupt_on[tool.name] = True

    return interrupt_on
```

Then in `build_agent()`, replace:
```python
interrupt_on=_INTERRUPT_TOOLS,
```
with:
```python
interrupt_on=_build_interrupt_on(mcp_tools),
```

#### Logging — add to `build_agent()` after the call

After computing `interrupt_on`, log the result so it's visible in pod logs at startup:

```python
import logging
logger = logging.getLogger(__name__)

interrupt_on = _build_interrupt_on(mcp_tools)
logger.info(
    "interrupt_on derived from MCP tools (%d tools gated): %s",
    len(interrupt_on),
    list(interrupt_on.keys()),
)
```

This makes it easy to verify the right tools are gated after each MCP server upgrade,
without reading source code.

### False positive risk and mitigation

A tool named `get_deleted_pods` contains "delet" in its description match — this is a
read-only tool that should NOT be gated. To guard against this:

- Name keywords check for whole words via `in name_lower` against the split token list,
  not substring matching on the full name. Split on `_` first:
  ```python
  name_tokens = set(name_lower.split("_"))
  name_match = bool(name_tokens & _DESTRUCTIVE_NAME_KEYWORDS)
  ```
- Description keywords use substring match (intentionally broader — descriptions are
  prose and won't have `_` delimiters).

This eliminates `get_deleted_pods` false positives while still catching `delete_pod`,
`drain_node`, `evict_pod`, etc.

### Verification step

After implementing, start the agent locally against the MCP gateway and check the startup
log line. It should list exactly the destructive tools — no read tools, no gaps.

---

## ACTION 2 — Delete dead tool files

**Status:** TODO  
**Depends on:** Nothing (can do now)

Delete these two files — they are never imported and duplicated by MCP:
- `agent/tools/cloudwatch_tools.py`
- `agent/tools/kubectl_tools.py`

Keep:
- `agent/tools/slack_tools.py` — still imported by `agent/agent.py`
- `agent/tools/__init__.py` — required for the package import to work

---

## ACTION 3 — Bump agent pod memory limit

**Status:** TODO  
**Depends on:** ACTION 1 (after Node.js/uvx move out)

In `infra/agent-deployment.yaml`, set:
```yaml
resources:
  requests:
    memory: "256Mi"
    cpu: "250m"
  limits:
    memory: "512Mi"
    cpu: "500m"
```

---

## ACTION 9 — Split skills into universal and cluster-specific, inject cluster skill at deploy time

**Status:** TODO
**Depends on:** Nothing (can do independently)

### Context

`agent/agent.py` line 72 loads all skills from a single directory:

```python
skills=["./skills/"]
```

`create_deep_agent` loads ALL SKILL.md files in that tree at startup and embeds
them into the agent's context. This means `skills/otel-demo-cluster/SKILL.md` —
which contains the AWS account number, Slack contact, service names, and namespace
for this specific cluster — is injected into every agent invocation regardless of
which cluster the agent is running against.

This breaks portability. A team pointing this agent at a different cluster gets
otel-demo-prod's context baked into every prompt.

### Correct model

Skills fall into two categories:

- **Universal skills** — investigation playbooks that apply to any Kubernetes cluster
  running on AWS. Loaded for every deployment automatically.
- **Cluster skill** — deployment-specific context (services, tiers, namespaces, contacts).
  Injected at deploy time via an environment variable. Different cluster = different env var.

### What to change

#### 1. Restructure the skills directory

Move existing scenario skills under `skills/universal/`:

```
skills/
  universal/
    node-disk-pressure/
      SKILL.md
    noisy-neighbor/
      SKILL.md
    pod-priority-eviction/
      SKILL.md
    checkout-protection/
      SKILL.md
  clusters/
    otel-demo-prod/
      SKILL.md        ← moved from skills/otel-demo-cluster/SKILL.md
```

The `skills/universal/` tree is committed to the repo and always loaded.
The `skills/clusters/` tree is also committed but only one is injected per deployment.

#### 2. `agent/agent.py` — load universal skills always, cluster skill via env var

```python
CLUSTER_SKILL_PATH = os.environ.get("CLUSTER_SKILL_PATH", "")

agent = create_deep_agent(
    ...
    skills=["./skills/universal/"],
    memory=(
        ["./AGENTS.md"]
        + ([CLUSTER_SKILL_PATH] if CLUSTER_SKILL_PATH else [])
    ),
    ...
)
```

Add a startup log so it's visible which cluster skill is loaded:
```python
if CLUSTER_SKILL_PATH:
    logger.info("Cluster skill loaded: %s", CLUSTER_SKILL_PATH)
else:
    logger.warning(
        "CLUSTER_SKILL_PATH not set — agent has no cluster context. "
        "Set this env var to the path of the cluster SKILL.md for this deployment."
    )
```

#### 3. `infra/agent-deployment.yaml` — add `CLUSTER_SKILL_PATH` env var

```yaml
- name: CLUSTER_SKILL_PATH
  value: "./skills/clusters/otel-demo-prod/SKILL.md"
```

#### 4. `infra/agent-secrets.example.yaml` — document the variable

Add a comment explaining that `CLUSTER_SKILL_PATH` is the only thing that changes
between deployments targeting different clusters.

### Result

To deploy this agent against a new cluster:
1. Write `skills/clusters/<new-cluster>/SKILL.md` — services, tiers, contacts
2. Set `CLUSTER_SKILL_PATH=./skills/clusters/<new-cluster>/SKILL.md` in the deployment
3. Done — no code changes, no AGENTS.md edits, no skill restructuring

The agent is now genuinely portable. Universal investigation playbooks are shared
across all deployments. Cluster identity is a deployment-time configuration.

### Why `memory=` for the cluster skill, not `skills=`

`skills=` is designed for scenario playbooks — the agent selects which skill to
apply based on the incident type. The cluster skill is not a scenario — it is
always-relevant context (like AGENTS.md). Using `memory=` means the framework
treats it as persistent background context, injected consistently, not selected
competitively against other skills.

---

## ACTION 10 — Update README.md to reflect all architectural changes (always last)

**Status:** TODO
**Depends on:** All other actions (implement this last)

### Context

`README.md` was written for the initial demo architecture. All the actions in this
file change significant parts of that architecture. The README must be updated after
all other actions are implemented so it accurately reflects what someone cloning the
repo will actually find and deploy.

This action should always remain the last entry in ACTIONS.md. If new actions are
added after ACTION 10, renumber this one accordingly before implementing.

### What the README must cover after all changes

#### 1. Architecture overview

Update the architecture diagram and description to reflect:
- **MCP gateway pod** (ACTION 1) — separate pod running kubectl-mcp and cloudwatch-mcp,
  exposed via ClusterIP Service. Agent connects over HTTP/SSE, not stdio subprocesses.
- **Agent pod** is now pure Python — no Node.js, no uvx (ACTION 1, 4 Dockerfile change)
- Remove any reference to MCP servers running inside the agent pod

#### 2. Repository structure

Update the file tree to reflect:
- `infra/mcp-gateway-deployment.yaml` (new, ACTION 1)
- `infra/mcp-gateway.Dockerfile` (new, ACTION 1)
- `agent/tools/cloudwatch_tools.py` and `kubectl_tools.py` deleted (ACTION 2)
- `skills/` restructured into `skills/universal/` and `skills/clusters/` (ACTION 9)
- `skills/clusters/otel-demo-prod/SKILL.md` (new, ACTION 9)

#### 3. How the skills system works

Add a clear explanation of the two-layer skills model:
- **Universal skills** (`skills/universal/`) — scenario playbooks loaded for every deployment
- **Cluster skill** (`skills/clusters/<name>/SKILL.md`) — cluster-specific context
  injected via `CLUSTER_SKILL_PATH` env var at deploy time
- How to add a new cluster: write a SKILL.md, set the env var, done

#### 4. Deployment steps

Update the step-by-step deployment section to include:
- Step for deploying the MCP gateway (`kubectl apply -f infra/mcp-gateway-deployment.yaml`)
- The `CLUSTER_SKILL_PATH` env var in the agent deployment configuration
- Removal of any steps that installed MCP servers inside the agent (npm install, uvx)
- Correct resource sizes for the agent pod (ACTION 3: 256Mi/512Mi)

#### 5. Environment variables

Update the env var reference table to include:
- `CLUSTER_SKILL_PATH` — path to the cluster skill for this deployment
- `KUBECTL_MCP_URL` — URL of the kubectl MCP server (defaults to cluster-local gateway)
- `CLOUDWATCH_MCP_URL` — URL of the CloudWatch MCP server (defaults to cluster-local gateway)
- Remove any env vars that are no longer used

#### 6. Local development

Add a local development section explaining how to run the agent locally after ACTION 1:
```bash
# Terminal 1 — kubectl MCP server
npx mcp-server-kubernetes --transport http --port 3001

# Terminal 2 — CloudWatch MCP server
uvx awslabs.cloudwatch-mcp-server@latest --transport sse --port 3002

# Terminal 3 — agent
export CLUSTER_SKILL_PATH=./skills/clusters/otel-demo-prod/SKILL.md
export KUBECTL_MCP_URL=http://localhost:3001/sse
export CLOUDWATCH_MCP_URL=http://localhost:3002/sse
python agent/main.py
```

#### 7. How to adapt this agent to a new cluster

Add a short "Bring Your Own Cluster" section:
1. Write `skills/clusters/<your-cluster>/SKILL.md` — describe your services, tiers, contacts
2. Set `CLUSTER_SKILL_PATH=./skills/clusters/<your-cluster>/SKILL.md` in your deployment
3. Update `AGENTS.md` only if the agent's operating rules need to change (rare)
4. Deploy — no code changes required

#### 8. What NOT to change in the README

- Keep the demo flow section (T+0:00 through T+2:30) — it describes the conference
  narrative and is still accurate
- Keep the fault injection instructions — scripts still work the same way
- Keep the architecture diagram — update it to show the MCP gateway pod, but preserve
  the overall flow (fault injection → CloudWatch alarm → Lambda → agent → Slack)

### Tone

Write the README as if the reader is a platform engineer who wants to deploy this
against their own cluster. The conference context (NZ Tech Rally, speaker name) can
stay in the header, but the body should read as a usable engineering document, not
a talk script.

---

## ACTION 11 — Subagent guardrails: depth limit and structured return contract

**Status:** TODO
**Depends on:** ACTION 7 (subagent system prompt rewrite must be in place first)

### Context

`create_deep_agent` supports subagents spawning their own sub-subagents — the `execute`
tool is available at every depth by default. Without a depth limit, a subagent can
become an orchestrator itself, spawning further subagents indefinitely. There is
currently nothing in this codebase that prevents this.

Additionally, subagents currently return free-form text. The master agent receives
three unstructured blobs and has to synthesise them without a guaranteed shape.
If a subagent returns raw tool output or an incomplete report, the synthesis degrades
silently with no signal to the master that the data is unreliable.

### What to change

#### `agent/agent.py` — add `max_subagent_depth` to `create_deep_agent`

```python
agent = create_deep_agent(
    ...
    max_subagent_depth=3,   # master → subagent → sub-subagent → stop
    # max_subagent_calls left unlimited — observe natural behaviour during demo
    ...
)
```

**Why depth 3:**
- Depth 1: master only calls the three specialist subagents. No further spawning.
- Depth 2: a subagent can spawn one level of its own subagents if needed.
- Depth 3: allows one more level of targeted delegation for complex incidents
  (e.g. a subagent spawns a focused log-query subagent for a specific pod).
- Beyond 3: almost certainly runaway reasoning, not genuine investigation depth.

`max_subagent_calls` is left unset (unlimited) intentionally for the demo so the
full natural behaviour is visible in LangSmith traces. Add a limit after observing
real investigation patterns.

#### `agent/subagents.py` — add structured return instruction to every subagent system prompt

Append this block to the end of each subagent's `system_prompt` (after the output
contract defined in ACTION 7):

```python
"Return your findings as a structured markdown report using exactly these sections:\n"
"## Summary\n"
"One sentence: what you found or confirmed.\n"
"## Evidence\n"
"Bullet list of data points with metric names, values, units, and timestamps.\n"
"## Conclusion\n"
"Your assessment: what this data means for the incident. "
"Explicitly state if something is within normal range — absence of a problem is evidence too.\n"
"## Gaps\n"
"Any queries that failed, returned no data, or could not be completed. "
"State what is unknown so the master agent can decide whether to follow up.\n\n"
"Do not return raw tool output. Summarise and structure it."
```

**Why this structure matters:**

| Section | Why it exists |
|---|---|
| Summary | Master agent gets the one-line finding immediately without parsing |
| Evidence | Traceable data — master can quote specific metrics in the Slack approval request |
| Conclusion | Forces the subagent to reason, not just report. "Normal range" callouts prevent false positives |
| Gaps | Makes unknowns explicit. Master can spawn a follow-up subagent to fill a gap rather than guessing |

The Gaps section is the most important one for re-planning. When the master agent
sees "CloudWatch Logs Insights returned no data for the last 15 minutes" in a
subagent's Gaps section, it has a concrete signal to replan — spawn a new subagent
with a wider time window, or pivot to a different data source. Without an explicit
Gaps section, the master only sees what was found, never what wasn't.

---

## ACTION 12 — Swap InMemoryStore for Redis-backed persistent memory

**Status:** TODO
**Depends on:** Nothing (can do independently)

### Context

`agent/memory/store.py` uses `langgraph.store.memory.InMemoryStore`. This store
lives in the agent pod's process memory. Every pod restart — deployment update,
OOM kill, node eviction — wipes all incident history. The agent wakes up with
no knowledge of past incidents except the hardcoded pre-seeded patterns.

This directly contradicts the "engineer who never forgets" narrative. A Redis pod
running inside the cluster is the right balance for this project: persistent across
agent restarts, zero AWS cost, inspectable live (`redis-cli`), and straightforward
to wire up.

### What to build

#### 1. `infra/redis-deployment.yaml` (NEW)

A single-pod Redis deployment in the `otel-demo` namespace:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: agent-redis
  namespace: otel-demo
spec:
  replicas: 1
  selector:
    matchLabels:
      app: agent-redis
  template:
    metadata:
      labels:
        app: agent-redis
    spec:
      containers:
      - name: redis
        image: redis:7-alpine
        ports:
        - containerPort: 6379
        resources:
          requests:
            memory: "64Mi"
            cpu: "50m"
          limits:
            memory: "256Mi"
            cpu: "200m"
        # Persist to emptyDir so data survives container restarts within the pod
        # (not pod deletion — acceptable for demo)
        args: ["--appendonly", "yes", "--appendfsync", "everysec"]
        volumeMounts:
        - name: redis-data
          mountPath: /data
      volumes:
      - name: redis-data
        emptyDir: {}
---
apiVersion: v1
kind: Service
metadata:
  name: agent-redis
  namespace: otel-demo
spec:
  type: ClusterIP
  selector:
    app: agent-redis
  ports:
  - port: 6379
    targetPort: 6379
```

Note: `emptyDir` means data survives container restarts but not pod deletion.
For the demo this is sufficient. The agent pod restarting does not delete the
Redis pod. Only a Redis pod deletion or node drain clears the memory.

#### 2. `agent/memory/store.py` (MODIFY)

Replace `InMemoryStore` with `AsyncRedisStore` from `langgraph-checkpoint-redis`.
Fall back to `InMemoryStore` if `REDIS_URL` is not set (local dev without Redis).

```python
import os
import logging
from langgraph.store.memory import InMemoryStore

logger = logging.getLogger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "")


def build_memory_store():
    if REDIS_URL:
        try:
            from langgraph.checkpoint.redis.aio import AsyncRedisStore
            store = AsyncRedisStore(REDIS_URL)
            logger.info("Memory store: Redis at %s", REDIS_URL)
            return store
        except Exception as e:
            logger.warning(
                "Redis store failed to initialise (%s) — falling back to InMemoryStore", e
            )

    logger.warning(
        "REDIS_URL not set — using InMemoryStore. "
        "Memory will be lost on pod restart."
    )
    return InMemoryStore()
```

Keep `NS_INCIDENTS`, `NS_PATTERNS`, `NS_NODES` namespace constants — they work
identically with both backends.

Move the pre-seeded patterns out of `build_memory_store()` into a separate
`seed_memory_store(store)` function called once at agent startup in `agent.py`.
This way seeding is explicit and only runs when needed, not on every store init.

#### 3. `agent/requirements.txt` (MODIFY)

Add:
```
langgraph-checkpoint-redis
redis
```

#### 4. `infra/agent-deployment.yaml` (MODIFY)

Add env var:
```yaml
- name: REDIS_URL
  value: "redis://agent-redis.otel-demo.svc.cluster.local:6379"
```

#### 5. `infra/deploy-all.sh` (MODIFY)

Add a step before the agent deployment:
```bash
step "Step 3b — Redis memory store"
kubectl apply -f "$SCRIPT_DIR/redis-deployment.yaml"
kubectl rollout status deployment/agent-redis -n "$NAMESPACE" --timeout=60s
ok "Redis running"
```

### Demo moment — inspecting memory live

After the incident resolves, run this from your laptop to show the audience
what the agent wrote to memory:

```bash
kubectl exec -n otel-demo deploy/agent-redis -- redis-cli keys "*"
kubectl exec -n otel-demo deploy/agent-redis -- redis-cli hgetall incidents:<thread_ts>
```

This shows the incident record the agent wrote — root cause, evicted pods,
before/after metrics — stored and queryable. The next time an incident fires,
the agent reads this and recognises the pattern.

---

## ACTION 13 — Document and enforce the interrupt_on + post_approval_request sequence

**Status:** TODO
**Depends on:** ACTION 4 (dynamic interrupt_on must be in place first)

### Context

There are two approval mechanisms in this codebase and they must work as a
sequence, not independently. Currently nothing documents or enforces this sequence,
so they risk being decoupled in future changes — breaking the human-in-the-loop
guarantee silently.

**Mechanism 1 — `post_approval_request` (Slack tool)**
The agent calls this explicitly. It posts evidence + APPROVE/DENY/MORE DETAILS
buttons to the Slack thread. After calling it, the agent continues running —
it does not pause here.

**Mechanism 2 — `interrupt_on` (LangGraph graph interrupt)**
When the agent attempts to call a tool listed in `interrupt_on`, the LangGraph
graph pauses BEFORE executing that tool. The graph holds its full state.
It resumes only when a message is fed back into `stream_agent` — which happens
when a Slack button is clicked (`handle_approve` / `handle_deny` in `main.py`).

**The correct sequence — always in this order:**
```
1. Agent gathers evidence (subagents)
2. Agent calls post_approval_request → Slack shows evidence + buttons
3. Agent proceeds to call the destructive tool (e.g. evict_pod)
4. interrupt_on fires → graph pauses, holds state
5. Human clicks APPROVE in Slack
6. handle_approve feeds "APPROVED" message into stream_agent
7. Graph resumes from the interrupt point
8. Destructive tool executes
```

If this sequence breaks — e.g. the agent calls the destructive tool without
calling `post_approval_request` first, or `interrupt_on` doesn't gate the tool
because the name doesn't match — the human never sees the approval request and
the action executes silently.

### What to change

#### `agent/agent.py` — add sequence comment above `interrupt_on`

```python
# Human-in-the-loop sequence (must always happen in this order):
#   1. Agent calls post_approval_request → posts evidence + buttons to Slack
#   2. Agent calls a destructive tool → interrupt_on fires → graph pauses
#   3. Slack button click → handle_approve/deny in main.py → graph resumes
#
# interrupt_on is the guarantee. post_approval_request is the UI.
# If interrupt_on doesn't gate a tool, the action executes without approval
# even if post_approval_request was called. That's why ACTION 4 (dynamic
# derivation) is critical — hardcoded tool names may silently miss tools.
interrupt_on = _build_interrupt_on(mcp_tools),
```

#### `agent/tools/slack_tools.py` — add sequence comment above `post_approval_request`

```python
# Called by the agent BEFORE attempting a destructive tool call.
# Posts evidence and APPROVE/DENY buttons to Slack.
# The agent continues after this call — the actual pause happens when
# interrupt_on fires on the subsequent destructive tool call.
# See agent.py for the full human-in-the-loop sequence.
def post_approval_request(...):
```

#### `agent/main.py` — add sequence comment above `handle_approve`

```python
# Resumes the paused LangGraph graph after human approval.
# The graph was frozen at the interrupt_on gate in agent.py.
# Feeding a message into stream_agent with the same thread_id/config
# resumes execution from exactly where it paused.
@slack_app.action("agent_approve")
def handle_approve(ack, body, say):
```

### Why comments and not code

The sequence is already mechanically correct when ACTION 4 is implemented.
The risk is not a current bug — it is future maintainers not understanding
why both mechanisms exist and removing one thinking it's redundant.
The comments make the dependency explicit and survives code reviews.

---
