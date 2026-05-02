# CLAUDE.md — K8s AI Agent

A generic, autonomous incident-investigation agent for Kubernetes clusters
running on AWS. The agent monitors a cluster, investigates alerts using
CloudWatch and the Kubernetes API in parallel via subagents, and asks for
human approval in Slack before taking any action that mutates cluster state.

The agent is application-agnostic. It is configured per deployment via:
- a **cluster skill** (`skills/clusters/<cluster-name>/SKILL.md`) describing
  the workloads, tiers, priority classes, and known characteristics of that
  specific cluster
- a set of **universal skills** (`skills/universal/`) describing investigation
  patterns that apply to any cluster (disk pressure, noisy neighbour, pod
  priority eviction, critical-service protection)
- **environment variables** for AWS, Slack, Redis, and the LLM provider

---

## 1. WHAT THE AGENT DOES

A typical incident lifecycle:

1. CloudWatch alarm fires → message lands in the configured Slack channel.
2. Agent acknowledges in the Slack thread and decomposes the incident into
   a todo list.
3. Subagents investigate in parallel:
   - **cloudwatch-investigator** — metrics, logs, alarms.
   - **kubectl-investigator** — node conditions, pod state, events
     (read-only).
   - **otel-investigator** — application-layer latency / error / throughput
     observed via the cluster's observability pipeline.
4. Agent forms a hypothesis, gathers evidence, and may revise the hypothesis
   as new data arrives. Re-planning is expected.
5. Agent posts findings + evidence to Slack, then issues an approval request
   with APPROVE / DENY buttons.
6. The graph pauses at the destructive tool call (LangGraph `interrupt_on`),
   holding state until the human responds — could be seconds, could be
   hours.
7. On APPROVE: tool executes, agent verifies recovery, writes the resolution
   to long-term memory, and stands down.
8. On DENY: agent acknowledges and stands down without retrying.
9. On tool error: agent re-plans (retry with corrective flags or switch
   tools), re-runs the approval gate, does not summarise-and-stop.

---

## 2. REPOSITORY STRUCTURE

```
.
├── CLAUDE.md                          ← This file
├── AGENTS.md                          ← Agent identity (always loaded into LLM context)
├── README.md                          ← Setup and deployment instructions
│
├── agent/
│   ├── main.py                        ← Entry point (FastAPI + persistent event loop)
│   ├── agent.py                       ← Deep Agent construction
│   ├── subagents.py                   ← Subagent definitions
│   ├── middleware.py                  ← KeepLoopingMiddleware (loop control)
│   ├── tools/
│   │   └── slack_tools.py             ← Slack post + approval-request tools
│   ├── mcp_servers/
│   │   ├── mcp_client.py              ← Loads tools from MCP servers
│   │   ├── mcp_config.py              ← MCP server configurations
│   │   └── servers.yaml               ← MCP server URLs / definitions
│   └── memory/
│       └── store.py                   ← Long-term memory store + seeds
│
├── skills/
│   ├── universal/
│   │   ├── node-disk-pressure/        ← Generic disk-pressure playbook
│   │   ├── noisy-neighbor/            ← Generic CPU/memory contention playbook
│   │   ├── pod-priority-eviction/     ← Generic priority-based eviction logic
│   │   └── critical-service-protection/ ← Rules for protecting critical services
│   └── clusters/
│       └── <cluster-name>/SKILL.md    ← Per-deployment cluster skill (workloads, tiers)
│
├── infra/                             ← Cluster + agent deployment manifests
├── slack/                             ← Slack bot setup + message templates
│
└── demo-workload/                     ← Synthetic checkout workload for the
                                         retail-prod-eks-use1 cluster skill.
                                         NOT imported by the agent. See
                                         demo-workload/README.md.
```

The agent itself is application-agnostic. The `demo-workload/` directory
contains a synthetic e-commerce workload (synthesizer, k8s manifests,
CloudFormation, Lambda, control scripts) that drives a CloudWatch alarm
to exercise the agent end-to-end. It is colocated for operational
convenience — one repo, one branch, one PR — but no agent code references
it. The cluster skill at `skills/clusters/retail-prod-eks-use1/SKILL.md`
describes the cluster the workload populates; the workload itself is
purely deployment artefacts.

---

## 3. AGENT FILES

### 3a. AGENTS.md (always loaded)

Tells the agent what it is and the rules it must follow on every
investigation. Application- and cluster-agnostic. Does not name specific
services, namespaces, or priority classes — those live in the cluster skill.

### 3b. Cluster skill (loaded per deployment)

Path is set via `CLUSTER_SKILL_PATH` env var. The cluster skill names the
services, tiers, priority classes, healthy thresholds, and known incident
patterns for that specific deployment. A cluster skill is required — the
agent logs a warning if it is not configured.

A cluster skill should include:

- Cluster facts: cloud, region, platform, observability pipeline, namespace
  conventions, AWS account/profile, node type, alert thresholds.
- Service tiers tied to priority classes:
  - **Critical** — must never be evicted/restarted/disrupted without
    explicit human approval.
  - **Infrastructure / User-facing / Background** — eviction order from
    lowest impact to highest.
- Healthy thresholds for the metrics that matter on this cluster.
- Known characteristics that help pattern-match (e.g. "service X is high
  disk I/O — check it first under disk pressure").
- Eviction order and a mapping of incident type → universal skill to load.
- Slack channel and approval contact.

### 3c. Universal skills

Investigation playbooks that apply to any cluster. They describe the
*pattern*, not the workload — service names and namespaces come from the
cluster skill at runtime.

- `node-disk-pressure/` — confirm condition, find top consumers, correlate
  with app symptoms, build ranked eviction list, prefer targeted pod delete
  over node drain.
- `noisy-neighbor/` — find CPU/memory hog, identify victims, propose
  eviction by priority class.
- `pod-priority-eviction/` — generic mechanics of eviction order based on
  priority class; how to verify recovery; how to write outcome to memory.
- `critical-service-protection/` — never act on a critical service without
  explicit human approval; how to assess critical-service health before any
  action; what to include in the approval request.

---

## 4. AGENT LOOP CONTROL — `KeepLoopingMiddleware`

The agent uses Deep Agents' middleware hook to force a true plan→act→observe
loop instead of exiting after the first model turn that returns empty
`tool_calls`. Without this, the model regularly stops on three failure
patterns:

1. **Natural end** — model emits an `AIMessage` with no tool calls because
   it thinks the summary it just wrote is "the answer". LangGraph exits.
2. **Tool error stand-down** — a destructive call fails (e.g. drain failing
   because of `emptyDir` or DaemonSet pods). The model posts a summary
   instead of re-planning with corrective flags or switching tools.
3. **Gate never armed** — the model calls `post_approval_request` but does
   NOT queue the destructive tool in the same turn, so the HITL
   `interrupt_on` has nothing to pause on. Buttons are no-ops.

### What it does

[agent/middleware.py](agent/middleware.py) defines `KeepLoopingMiddleware`,
hooked via `after_model` / `aafter_model`. It runs after each model response
and BEFORE `HumanInTheLoopMiddleware`, so a correctly queued destructive
tool still arms the gate normally.

State schema is extended with `explicit_stand_down: bool` so the flag
persists across turns via the Redis checkpointer.

| Detected condition                                                | Action                                                              |
|-------------------------------------------------------------------|---------------------------------------------------------------------|
| `AIMessage` with empty `tool_calls`, no stand-down phrase in text | Inject corrective `HumanMessage` listing the three valid options    |
| `AIMessage` content contains "no action required" / "standing down" / "investigation complete" | Set `explicit_stand_down=True` (graph allowed to exit)              |
| `mark_stand_down` tool was called this turn                       | Set `explicit_stand_down=True`                                      |
| `post_approval_request` called WITHOUT a destructive tool in same turn | Inject corrective `HumanMessage` so the model re-issues both calls  |
| `post_approval_request` only, but a destructive tool just succeeded (mid-loop after APPROVE) | No action — legitimate state                                        |

### The three valid termination paths

The graph keeps looping until ONE of these happens:

1. The agent calls a destructive kubectl tool — HITL pauses the graph.
2. The agent posts a stand-down summary via `post_to_slack` containing a
   recognised phrase ("no action required", "standing down", "investigation
   complete"), then `mark_stand_down` on the next turn.
3. The agent calls `mark_stand_down` directly with a brief reason.

### `mark_stand_down` tool

[agent/agent.py](agent/agent.py) defines `mark_stand_down(reason: str)`. The
agent calls it when the incident is fully resolved, the user denied the
recommended action, or no remediation is appropriate. The middleware
recognises the call and sets `explicit_stand_down=True`, allowing the graph
to exit cleanly on the next turn.

### Re-plan-on-error rule

The system prompt and the universal skills instruct the agent: if a
destructive tool returns `Tool error: ...` after approval, do NOT post a
final summary. Re-plan — retry with corrective flags
(`--force --delete-emptydir-data --ignore-daemonsets` for drain) or switch
tools (e.g. `kubectl_delete pod` per non-critical pod), then re-run the
approval gate.

### Preferred remediation: `kubectl_delete pod` over drain

For most clusters, the universal playbooks prefer
`kubectl_delete pod <name> -n <namespace>` issued for each non-critical pod
in priority order, NOT a node-wide drain. Real clusters commonly contain
bare pods, DaemonSets, and `emptyDir` volumes that make drain fail with
unfixable obstacles. Targeted pod delete succeeds reliably and gives
finer-grained control. See
[skills/universal/node-disk-pressure/SKILL.md](skills/universal/node-disk-pressure/SKILL.md).

### Multi-arch image

[agent/Dockerfile](agent/Dockerfile) uses `ARG TARGETARCH` for the `kubectl`
download URL so `docker buildx build --platform linux/amd64,linux/arm64`
produces a working image on both architectures.

Build & push (versioned tag, multi-arch — both rules from CLAUDE memory):
```bash
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  --builder multiarch-builder \
  -t <registry>/<image>:vNN \
  -f agent/Dockerfile \
  --push .

AWS_PROFILE=<profile> kubectl apply -f infra/agent-deployment.yaml
```

---

## 5. TECHNICAL IMPLEMENTATION

### 5a. Deep Agents Setup (high level)

```python
# agent/agent.py (abridged)
agent = create_deep_agent(
    model=model,                              # OpenAI / Anthropic via AGENT_MODEL
    skills=["./skills/universal/"],           # Loaded into agent context
    subagents=build_subagents(mcp_tools),     # cloudwatch / kubectl / otel investigators
    tools=[
        post_to_slack,
        post_approval_request,
        mark_stand_down,
        *mcp_tools,                           # kubectl + cloudwatch tools from MCP servers
    ],
    middleware=[KeepLoopingMiddleware(set(interrupt_on.keys()))],
    checkpointer=checkpointer,                # Redis-backed when REDIS_URL is set
    store=store,                              # InMemoryStore for cross-incident memory
    interrupt_on=interrupt_on,                # Derived dynamically from MCP tool list
    memory=["./AGENTS.md"] + ([CLUSTER_SKILL_PATH] if CLUSTER_SKILL_PATH else []),
    system_prompt=SYSTEM_PROMPT,
)
```

`interrupt_on` is derived dynamically from the MCP tool list — any tool
whose name or description matches a destructive keyword (delete, drain,
evict, apply, patch, scale, rollout, etc.) is gated automatically. New
destructive tools added to MCP servers in future versions are caught
without code changes.

### 5b. Subagents

Three subagents run in parallel:

- **cloudwatch-investigator** — metric and log evidence. Discovers tools at
  runtime; reports top consumers with rates, alarm states, and an explicit
  "within normal range" call-out where applicable.
- **kubectl-investigator** — read-only cluster state. Node conditions,
  pod status, events, resource usage, priority classes.
- **otel-investigator** — application-layer health. Latency percentiles,
  error rates, and observability pipeline status for the cluster's critical
  services (defined by the cluster skill).

All three return findings using the same structured contract (Summary,
Evidence, Conclusion, Gaps).

### 5c. Long-term memory

`agent/memory/store.py` exposes a `build_memory_store()` (in-memory by
default; Redis-backed in production) and a `format_incident_record()`
helper for writing resolved incidents. Memory carries cross-incident
patterns and prior root causes so the agent improves over time on a given
cluster.

The store is intentionally not seeded with cluster-specific patterns. Any
known patterns for a deployment belong in that deployment's cluster skill.

---

## 6. ENVIRONMENT VARIABLES REQUIRED

```bash
# AWS
AWS_REGION=...               # Region the cluster runs in
AWS_PROFILE=...              # Profile with EKS + CloudWatch read access

# LLM
AGENT_MODEL=openai:gpt-5-mini             # provider:model — openai:* or anthropic:* supported
OPENAI_API_KEY=...                        # if AGENT_MODEL is openai:*
ANTHROPIC_API_KEY=...                     # if AGENT_MODEL is anthropic:*

# Slack
SLACK_BOT_TOKEN=xoxb-...
SLACK_SIGNING_SECRET=...
SLACK_CHANNEL_ID=C...        # Channel ID where alerts and approval requests post

# Cluster
CLUSTER_NAME=...
CLUSTER_SKILL_PATH=./skills/clusters/<cluster-name>/SKILL.md
KUBECONFIG=/path/to/kubeconfig

# Persistence
REDIS_URL=redis://...        # Required for stateful pause/resume across restarts

# MCP Servers (URLs defined in agent/mcp_servers/servers.yaml)
KUBECTL_MCP_PORT=3001
CLOUDWATCH_MCP_PORT=3002

# Optional: tracing
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=ls__...
```

---

## 7. ADAPTING THE AGENT TO A NEW CLUSTER

To deploy the agent against a new cluster:

1. Create `skills/clusters/<cluster-name>/SKILL.md` describing the cluster's
   workloads, tiers, priority classes, healthy thresholds, known
   characteristics, and Slack channel.
2. Set `CLUSTER_SKILL_PATH` in the agent deployment to that file.
3. Apply the priority classes referenced by the cluster skill to the
   cluster.
4. Configure CloudWatch alarms for the conditions you want the agent to
   investigate; route them to the configured Slack channel.
5. Deploy the agent (see [agent/Dockerfile](agent/Dockerfile) and
   [infra/agent-deployment.yaml](infra/agent-deployment.yaml)).

The universal skills do not need to change. They describe patterns that
apply to any cluster — the cluster skill supplies the specifics.

---

## 8. REFERENCES

- Deep Agents docs: https://docs.langchain.com/oss/python/deepagents/overview
- Deep Agents skills: https://docs.langchain.com/oss/python/deepagents/skills
- Deep Agents human-in-loop: https://docs.langchain.com/oss/python/deepagents/human-in-the-loop
- MCP spec: https://modelcontextprotocol.io
- AWS CloudWatch Container Insights for EKS: https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Container-Insights-setup-EKS-quickstart.html
