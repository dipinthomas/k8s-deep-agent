# CLAUDE.md — K8s AI Agent Demo
## Conference: NZ Tech Rally 2026 · May 15, Wellington
## Talk: "AI Agents in Your Kubernetes Cluster: Troubleshooting at Scale, 24/7"

---

## 1. PROJECT OVERVIEW

This project builds a **working AI agent demo** for a 25-minute conference talk.
The agent monitors a Kubernetes cluster, autonomously investigates incidents,
and asks for human approval before taking destructive actions — all via Slack.

The demo tells this story on stage:
> "This happened on our cluster at 2am. Nobody woke up."

The audience sees a complete incident lifecycle:
1. Slack alert fires (disk pressure on a node)
2. Agent investigates using CloudWatch + kubectl in parallel via subagents
3. Agent pursues a wrong hypothesis first, then re-plans (this is intentional — makes it realistic)
4. Agent posts CloudWatch evidence to Slack
5. Agent PAUSES and asks: "Checkout is at risk. I recommend evicting these pods. Approve?"
6. Speaker clicks Approve live on stage
7. Pods evicted, disk pressure drops, checkout service recovers
8. Agent posts resolution summary

---

## 2. WHAT WE ARE BUILDING

### 2a. The Sample Application
**OpenTelemetry Astronomy Shop** — a 16-service e-commerce microservices app
- GitHub: https://github.com/open-telemetry/opentelemetry-demo
- Deployed via Helm on EKS
- Services we care about most:
  - `checkoutservice` (Go) — handles payments, CRITICAL, must survive
  - `paymentservice` (JavaScript) — processes payments, CRITICAL
  - `cartservice` (.NET) — shopping cart, CRITICAL
  - `frontend` (TypeScript/Next.js) — UI, non-critical
  - `imageprovider` (nginx) — serves product images, NON-CRITICAL, disk culprit
  - `adservice` (Java) — ads, NON-CRITICAL
  - `recommendationservice` (Python) — recommendations, NON-CRITICAL
  - `loadgenerator` (Python/Locust) — simulates traffic, NON-CRITICAL

### 2b. The Infrastructure
- **Cloud:** AWS
- **Region:** ap-southeast-2 (Sydney)
- **Kubernetes:** Amazon EKS
- **Monitoring/Alerting:** Amazon CloudWatch + CloudWatch Container Insights
- **Observability:** OTel Collector (comes with the demo app) → CloudWatch
- **Messaging:** Slack (webhook + bot for human-in-the-loop)

### 2c. The AI Agent
- **Framework:** Deep Agents (LangChain) — `pip install deepagents`
- **Runtime:** LangGraph (durable execution, stateful pause/resume)
- **Model:** Claude claude-sonnet-4-6 (Anthropic) — or configurable
- **Tools:** kubectl MCP server, CloudWatch MCP server, Slack MCP server
- **Pattern:** Master agent + specialist subagents (parallel investigation)
- **Human-in-loop:** LangGraph interrupt() — stateful pause in Slack

### 2d. The Failure Scenario
**Node Disk Pressure** caused by:
- `imageprovider` nginx writing excessive logs / ephemeral storage
- `loadgenerator` hammering the cluster with traffic
- This causes `checkoutservice` latency to rise (observed via OTel traces → CloudWatch)
- Node hits disk pressure condition
- Agent must identify the right pods to evict while protecting checkout/payment

This scenario was chosen because:
- It is realistic (happened in real production)
- It requires correlating multiple data sources (CloudWatch + K8s API + OTel traces)
- It has a clear "critical vs non-critical" decision that needs human approval
- It is visually compelling in Slack

---

## 3. REPOSITORY STRUCTURE

```
k8s-ai-agent-demo/
├── CLAUDE.md                          ← This file
├── AGENTS.md                          ← Agent identity/context file (always loaded)
├── README.md                          ← Setup and deployment instructions
│
├── infra/
│   ├── eks-cluster.tf                 ← Terraform for EKS cluster
│   ├── eks-cluster.yaml               ← eksctl cluster config (alternative)
│   ├── cloudwatch-agent.yaml          ← CloudWatch Container Insights setup
│   └── priority-classes.yaml          ← K8s PriorityClass definitions
│
├── otel-demo/
│   ├── values.yaml                    ← Helm values for OTel demo customisation
│   └── deploy.sh                      ← One-command deploy script
│
├── agent/
│   ├── main.py                        ← Agent entry point
│   ├── agent.py                       ← Deep Agent setup (create_deep_agent)
│   ├── subagents.py                   ← Subagent definitions
│   ├── tools/
│   │   ├── cloudwatch_tools.py        ← CloudWatch query tools
│   │   ├── kubectl_tools.py           ← kubectl wrapper tools
│   │   └── slack_tools.py             ← Slack post/read tools
│   ├── mcp/
│   │   ├── mcp_config.py              ← MCP server configurations
│   │   └── servers.yaml               ← MCP server definitions
│   └── memory/
│       └── store.py                   ← Long-term memory setup
│
├── skills/
│   ├── node-disk-pressure/
│   │   └── SKILL.md                   ← Investigation playbook for disk pressure
│   ├── pod-priority-eviction/
│   │   └── SKILL.md                   ← How to evaluate and evict by priority
│   └── checkout-protection/
│       └── SKILL.md                   ← Rules for protecting payment services
│
├── fault-injection/
│   ├── trigger-disk-pressure.sh       ← Script to cause the demo failure
│   ├── reset-cluster.sh               ← Reset everything back to healthy
│   └── README.md                      ← How to run the fault injection
│
└── slack/
    ├── bot-setup.md                   ← How to create the Slack app/bot
    └── message-templates/             ← Example Slack message formats
        ├── alert.json
        ├── investigation-update.json
        ├── approval-request.json
        └── resolution-summary.json
```

---

## 4. AGENT FILES

### 4a. AGENTS.md (The Cluster Identity — Always Loaded)

This file is always injected into the agent's context. It tells the agent
who it is, what cluster it's looking at, and the rules it must follow.

Content to write in `AGENTS.md`:
```markdown
# Cluster Identity: otel-demo-prod (EKS ap-southeast-2)

## What I Am
I am an autonomous Kubernetes operations agent for the OTel Demo cluster.
My job is to investigate incidents, identify root causes, and recommend fixes.
I ALWAYS ask for human approval before taking any action that affects running workloads.

## Critical Services (NEVER evict without explicit approval)
These services handle payments and must be protected at all costs:
- checkoutservice (namespace: otel-demo)
- paymentservice (namespace: otel-demo)
- cartservice (namespace: otel-demo)
- productcatalogservice (namespace: otel-demo)

## Non-Critical Services (Safe to evict under disk pressure)
These services are important but can be sacrificed to protect payments:
- imageprovider (known high disk I/O — CHECK THIS FIRST under disk pressure)
- adservice
- recommendationservice
- loadgenerator (staging traffic simulator — always safe to stop)
- frontend (users lose browsing, not payments)

## Priority Classes in This Cluster
- payment-critical (1000000) → checkoutservice, paymentservice, cartservice
- user-facing (500000)       → frontend, productcatalogservice
- background (100000)        → loadgenerator, adservice, recommendationservice
- infrastructure (900000)    → imageprovider, otel-collector

## Investigation Rules
1. Always check app-level symptoms first (OTel traces → CloudWatch)
2. Then move to infrastructure (node conditions, disk usage)
3. Always identify root cause BEFORE recommending action
4. Always show evidence (CloudWatch screenshot/data) in Slack before asking approval
5. Never drain a node without approval
6. Never delete a PVC without approval
7. If unsure, ask — do not guess

## Known Issues in This Cluster
- imageprovider runs nginx with verbose logging enabled — common disk pressure culprit
- loadgenerator can be stopped safely at any time (it's synthetic traffic)
- OTel collector writes trace buffers to emptyDir — can fill under high load

## Slack Channel
Post all findings and approval requests to: #k8s-alerts
Tag @dipin for any approval requests
```

### 4b. Skills Directory

**skills/node-disk-pressure/SKILL.md:**
```markdown
---
name: node-disk-pressure
description: Use this skill when a Kubernetes node shows disk pressure,
             high disk usage, DiskPressure condition, or when pods are
             being evicted due to disk resource constraints.
---

## Node Disk Pressure Investigation Playbook

### Step 1 — Confirm the condition
kubectl describe node <node-name> | grep -A5 Conditions
Look for: DiskPressure = True

### Step 2 — Check imageprovider FIRST (known culprit)
kubectl logs -n otel-demo deployment/imageprovider --tail=100
Check CloudWatch: /aws/containerinsights/otel-demo-prod/performance
Filter: pod_name = imageprovider, metric: container_fs_usage_bytes

### Step 3 — Find top disk consumers on the node
kubectl get pods -n otel-demo -o wide | grep <node-name>
For each pod: kubectl exec -it <pod> -- df -h (if container permits)
CloudWatch Logs Insights query for disk write rates

### Step 4 — Check OTel collector buffer
kubectl describe pod -n otel-demo -l app=otelcol
Check emptyDir volume mounts and current usage

### Step 5 — Correlate with app symptoms
CloudWatch: check checkout service latency in past 15 minutes
If checkout p99 is rising: disk pressure is already affecting payments
This escalates urgency

### Step 6 — Identify eviction candidates
Cross-reference pod list with priority classes in AGENTS.md
Build ranked eviction list: lowest priority first
Calculate estimated disk recovery if each pod is evicted

### Step 7 — Build the approval request
Post to Slack with:
- Root cause identified
- CloudWatch evidence (metric graph link or data)
- Recommended eviction list (lowest to highest priority)
- Estimated impact of each eviction
- Clear approve/deny buttons
```

---

## 5. THE DEMO FLOW (Exact Sequence)

This is the precise sequence the demo must follow for the talk.
The "wrong hypothesis" in Step 3 is INTENTIONAL — it makes the agent look
realistic rather than scripted.

```
T+0:00  CloudWatch alarm fires → Slack message posted to #k8s-alerts
        "⚠️ ALERT: Node disk pressure detected on node ip-10-0-1-45
         Checkout service p99 latency rising: 245ms → 890ms"

T+0:15  Agent acknowledges in Slack thread:
        "Starting investigation. Spawning subagents."

T+0:20  Three subagents spawn in parallel:
        - Subagent A: CloudWatch disk metrics
        - Subagent B: kubectl node/pod status
        - Subagent C: OTel traces → checkout latency

T+0:45  Agent posts first update to Slack:
        "Initial finding: OTel collector emptyDir buffer at 87% capacity.
         Hypothesis: OTel collector is the disk culprit. Investigating..."
        [THIS IS THE WRONG HYPOTHESIS]

T+1:15  Agent posts correction to Slack:
        "Hypothesis revised. OTel collector usage is within normal range.
         Re-running analysis. New signal: imageprovider nginx access logs
         showing 340MB written in last 8 minutes — 12x normal rate."
        [THIS IS THE CORRECT FINDING]

T+1:45  Agent posts evidence to Slack:
        "Root cause identified: imageprovider nginx logging misconfiguration
         causing excessive disk writes under load generator traffic spike.
         
         📊 CloudWatch data:
         - Node disk usage: 91% (threshold: 85%)
         - imageprovider disk writes: 340MB/8min
         - Checkout p99 latency: 890ms (normal: 120ms)
         
         🎯 Recommendation: Evict the following pods (priority order):
         1. loadgenerator — synthetic traffic, zero user impact
         2. imageprovider — product images unavailable, browsing affected
         3. adservice — ads stop showing, no revenue impact
         
         ✅ Protected: checkoutservice, paymentservice, cartservice
         
         ⚠️ This will make product browsing unavailable.
            Payments will continue to work normally.
         
         Shall I proceed? @dipin"
         [APPROVE] [DENY] [GIVE ME MORE DETAILS]

T+2:00  Speaker clicks APPROVE live on stage

T+2:05  Agent executes evictions, posts to Slack:
        "Executing evictions..."
        "✅ loadgenerator evicted"
        "✅ imageprovider evicted"  
        "✅ adservice evicted"

T+2:30  Agent posts resolution:
        "✅ Incident resolved.
         
         Node disk usage: 91% → 67% (↓24%)
         Checkout p99 latency: 890ms → 118ms (back to normal)
         
         📝 Writing to incident memory for future reference.
         
         Recommended follow-up skill to write:
         - Add imageprovider nginx log rotation to disk-pressure playbook
         - Consider adding resource limits to imageprovider emptyDir"
```

---

## 6. TECHNICAL IMPLEMENTATION

### 6a. Deep Agents Setup

```python
# agent/agent.py
from deepagents import create_deep_agent
from langgraph.checkpoint.memory import MemorySaver
from langgraph.store.memory import InMemoryStore

from subagents import cloudwatch_subagent, kubectl_subagent, otel_subagent
from tools.slack_tools import post_to_slack, wait_for_approval

checkpointer = MemorySaver()  # Enables stateful pause/resume
store = InMemoryStore()        # Long-term memory across incidents

agent = create_deep_agent(
    model="anthropic:claude-sonnet-4-6",
    skills=["./skills/"],
    subagents=[cloudwatch_subagent, kubectl_subagent, otel_subagent],
    tools=[post_to_slack, wait_for_approval],
    checkpointer=checkpointer,
    store=store,
    interrupt_on={
        # Pause and ask Slack before any of these
        "kubectl_evict_pod": True,
        "kubectl_drain_node": True,
        "kubectl_delete": True,
    },
    system_prompt="You are a Kubernetes operations agent. "
                  "Read AGENTS.md before every investigation. "
                  "Always show evidence before asking for approval. "
                  "Never evict critical payment services."
)
```

### 6b. MCP Server Configuration

```python
# agent/mcp/mcp_config.py

MCP_SERVERS = [
    {
        "name": "kubectl-mcp",
        "type": "url",
        "url": "http://localhost:3001/mcp",  # kubectl MCP server
        "description": "Kubernetes cluster operations via kubectl"
    },
    {
        "name": "cloudwatch-mcp", 
        "type": "url",
        "url": "http://localhost:3002/mcp",  # CloudWatch MCP server
        "description": "AWS CloudWatch metrics, logs, and alarms"
    },
    {
        "name": "slack-mcp",
        "type": "url", 
        "url": "http://localhost:3003/mcp",  # Slack MCP server
        "description": "Post to Slack, read messages, handle approvals"
    }
]
```

### 6c. Subagent Definitions

```python
# agent/subagents.py

cloudwatch_subagent = {
    "name": "cloudwatch-investigator",
    "description": "Investigates CloudWatch metrics, logs, and alarms. "
                   "Use for disk usage metrics, container insights, "
                   "application latency data, and CloudWatch Logs Insights queries.",
    "system_prompt": "You are a CloudWatch specialist. Query metrics and logs "
                     "efficiently. Always include timestamps and units in findings. "
                     "Return structured data the master agent can act on.",
    "tools": ["cloudwatch_get_metric", "cloudwatch_logs_insights", 
              "cloudwatch_describe_alarms"],
    "skills": ["./skills/cloudwatch-queries/"]
}

kubectl_subagent = {
    "name": "kubectl-investigator", 
    "description": "Investigates Kubernetes cluster state. Use for node conditions, "
                   "pod status, resource usage, events, and priority classes.",
    "system_prompt": "You are a Kubernetes specialist. Read cluster state carefully. "
                     "Never modify anything — only read and report. "
                     "Return structured findings including pod names, namespaces, "
                     "and resource usage.",
    "tools": ["kubectl_get", "kubectl_describe", "kubectl_logs", "kubectl_top"],
    "skills": ["./skills/node-disk-pressure/", "./skills/pod-priority-eviction/"]
}

otel_subagent = {
    "name": "otel-investigator",
    "description": "Investigates application performance using OTel traces and metrics "
                   "from CloudWatch. Use for service latency, error rates, "
                   "and trace analysis.",
    "system_prompt": "You are an observability specialist. Focus on service health "
                     "and user-facing impact. Always report p99 latency and error rates "
                     "for checkout, payment, and cart services.",
    "tools": ["cloudwatch_get_metric", "cloudwatch_logs_insights"],
    "skills": ["./skills/checkout-protection/"]
}
```

### 6d. Priority Classes (Deploy to EKS before demo)

```yaml
# infra/priority-classes.yaml
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: payment-critical
value: 1000000
globalDefault: false
description: "Payment processing services — never evict"
---
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: user-facing
value: 500000
globalDefault: false
description: "User-facing services — evict only under pressure"
---
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: background
value: 100000
globalDefault: true
description: "Background services — safe to evict"
```

### 6e. Fault Injection Script

```bash
#!/bin/bash
# fault-injection/trigger-disk-pressure.sh
# Creates realistic disk pressure on a target node
# Run this BEFORE the demo to pre-stage the failure

set -e

NAMESPACE="otel-demo"
TARGET_NODE="${1:-auto}"  # Pass node name or auto-detect

echo "🔴 Triggering disk pressure demo scenario..."

# Step 1: Crank up load generator to max traffic
kubectl set env deployment/loadgenerator \
  -n $NAMESPACE \
  LOCUST_USERS=500 \
  LOCUST_SPAWN_RATE=50

# Step 2: Enable verbose nginx logging on imageprovider
# This causes excessive disk writes — our "misconfiguration"
kubectl set env deployment/imageprovider \
  -n $NAMESPACE \
  NGINX_LOG_LEVEL=debug

# Step 3: Remove imageprovider resource limits (makes it worse)
kubectl patch deployment imageprovider -n $NAMESPACE \
  --patch '{"spec":{"template":{"spec":{"containers":[{"name":"imageprovider","resources":{"limits":{"ephemeral-storage":null}}}]}}}}'

echo "⏳ Disk pressure will build in approximately 3-5 minutes..."
echo "✅ Monitor with: kubectl describe node | grep -A5 Conditions"
echo "✅ Watch CloudWatch Container Insights for disk metrics"
```

```bash
#!/bin/bash
# fault-injection/reset-cluster.sh
# Resets everything back to healthy state after demo

NAMESPACE="otel-demo"

echo "🟢 Resetting cluster to healthy state..."

kubectl set env deployment/loadgenerator \
  -n $NAMESPACE \
  LOCUST_USERS=10 \
  LOCUST_SPAWN_RATE=1

kubectl set env deployment/imageprovider \
  -n $NAMESPACE \
  NGINX_LOG_LEVEL=warn

# Restart any evicted pods
kubectl rollout restart deployment/imageprovider -n $NAMESPACE
kubectl rollout restart deployment/adservice -n $NAMESPACE
kubectl rollout restart deployment/recommendationservice -n $NAMESPACE
kubectl rollout restart deployment/loadgenerator -n $NAMESPACE

echo "✅ Cluster reset complete"
```

---

## 7. DEPLOYMENT STEPS

### Step 1 — EKS Cluster

```bash
# Using eksctl
eksctl create cluster \
  --name otel-demo-prod \
  --region ap-southeast-2 \
  --nodegroup-name standard-workers \
  --node-type m5.2xlarge \
  --nodes 3 \
  --nodes-min 2 \
  --nodes-max 4 \
  --managed

# Enable CloudWatch Container Insights
eksctl utils update-cluster-logging \
  --enable-types all \
  --region ap-southeast-2 \
  --cluster otel-demo-prod

aws eks update-addon \
  --cluster-name otel-demo-prod \
  --addon-name amazon-cloudwatch-observability \
  --region ap-southeast-2
```

### Step 2 — Deploy PriorityClasses

```bash
kubectl apply -f infra/priority-classes.yaml
```

### Step 3 — Deploy OTel Demo

```bash
helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts
helm repo update

helm install otel-demo open-telemetry/opentelemetry-demo \
  --namespace otel-demo \
  --create-namespace \
  --values otel-demo/values.yaml \
  --wait
```

### Step 4 — Configure CloudWatch Alarm

```bash
# Create the disk pressure alarm that triggers the demo
aws cloudwatch put-metric-alarm \
  --alarm-name "EKS-NodeDiskPressure-otel-demo" \
  --alarm-description "Node disk usage above 80% in otel-demo cluster" \
  --metric-name node_filesystem_utilization \
  --namespace ContainerInsights \
  --statistic Average \
  --period 60 \
  --threshold 80 \
  --comparison-operator GreaterThanThreshold \
  --evaluation-periods 2 \
  --alarm-actions <SNS_TOPIC_ARN_FOR_SLACK>
```

### Step 5 — Deploy Agent

```bash
cd agent
pip install deepagents langchain-anthropic boto3 slack-sdk

# Set environment variables
export ANTHROPIC_API_KEY="..."
export AWS_REGION="ap-southeast-2"
export SLACK_BOT_TOKEN="xoxb-..."
export SLACK_CHANNEL="#k8s-alerts"
export KUBECONFIG="~/.kube/config"

python main.py
```

---

## 8. WHAT MAKES THIS DEMO CONFERENCE-READY

### The Wrong Hypothesis Moment
The agent MUST pursue the OTel collector hypothesis first and then correct itself.
This is not a bug — it is the most important moment in the demo.
It shows the agent is reasoning, not following a script.
Configure this by ordering the subagent investigation to check OTel collector
before imageprovider in the first pass.

### The Stateful Pause
When the agent posts the approval request and waits, spend 20-30 seconds
talking to the audience before clicking Approve.
> "The agent is waiting. It has done all the analysis. It's holding its
>  entire state in memory. It will wait here until I respond — could be
>  3am, could be tomorrow morning. This is the checkpoint."

### The Live Approve
Click Approve yourself, live on stage. Don't pre-record this part.
The audience seeing the agent instantly respond to your click is the
most powerful moment of the talk.

### GitHub Repo
Share the repo URL at the end so audience can clone and deploy.
The fault injection scripts mean they can reproduce the exact demo scenario.

---

## 9. KEY CONCEPTS TO DEMONSTRATE (maps to talk slides)

| Demo Moment | Concept Being Shown | Slide It Supports |
|---|---|---|
| Agent reads AGENTS.md | Cluster identity layer | "The Onboarding Doc" |
| Skill triggered for disk pressure | Progressive disclosure | "The Senior Engineer's Instinct" |
| Three subagents spawn | Parallel investigation | "The War Room" |
| MCP servers called | Standardised tool protocol | "Tools + MCP" |
| Wrong hypothesis + re-plan | write_todos / re-planning | "The Agent Loop" |
| Stateful pause in Slack | LangGraph interrupt() | "The Escalation Call" |
| Resolution written to memory | Long-term memory store | "The Engineer Who Never Forgets" |

---

## 10. ENVIRONMENT VARIABLES REQUIRED

```bash
# AWS
AWS_REGION=ap-southeast-2
AWS_PROFILE=default  # or use IAM role on EKS

# Anthropic
ANTHROPIC_API_KEY=sk-ant-...

# Slack
SLACK_BOT_TOKEN=xoxb-...
SLACK_SIGNING_SECRET=...
SLACK_CHANNEL_ID=C...  # #k8s-alerts channel ID

# Cluster
CLUSTER_NAME=otel-demo-prod
KUBECONFIG=/path/to/kubeconfig

# MCP Servers
KUBECTL_MCP_PORT=3001
CLOUDWATCH_MCP_PORT=3002
SLACK_MCP_PORT=3003

# Agent
LANGSMITH_TRACING=true        # Optional but recommended for debugging
LANGSMITH_API_KEY=ls__...     # Optional
```

---

## 11. THINGS TO BUILD (In Order)

Claude Code should build these in sequence.
Do not move to the next step until the current step is verified working.

- [ ] 1. EKS cluster deployed and healthy
- [ ] 2. OTel demo app deployed, all pods running
- [ ] 3. CloudWatch Container Insights enabled and showing metrics
- [ ] 4. PriorityClasses applied to all pods
- [ ] 5. Slack bot created and posting to #k8s-alerts
- [ ] 6. CloudWatch alarm configured and triggering to Slack
- [ ] 7. MCP servers running (kubectl, CloudWatch, Slack)
- [ ] 8. AGENTS.md and Skills written
- [ ] 9. Deep Agent code written and connecting to MCP servers
- [ ] 10. Subagents defined and spawning correctly
- [ ] 11. Human-in-loop interrupt working in Slack
- [ ] 12. Fault injection script tested (causes disk pressure in <5 mins)
- [ ] 13. Full end-to-end demo run through (timed: must complete in under 3 mins)
- [ ] 14. Demo video recorded
- [ ] 15. Reset script verified working

---

## 12. REFERENCES

- Deep Agents docs: https://docs.langchain.com/oss/python/deepagents/overview
- Deep Agents skills: https://docs.langchain.com/oss/python/deepagents/skills
- Deep Agents human-in-loop: https://docs.langchain.com/oss/python/deepagents/human-in-the-loop
- OTel Demo repo: https://github.com/open-telemetry/opentelemetry-demo
- OTel Demo Helm chart: https://open-telemetry.github.io/opentelemetry-helm-charts
- Previous talk (LangGraph 3-node): https://github.com/dipinthomas/langraph_3node_agent
- MCP spec: https://modelcontextprotocol.io
- kagent (K8s-native agent runtime): https://kagent.dev
- AWS CloudWatch Container Insights EKS: https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Container-Insights-setup-EKS-quickstart.html
- NZ Tech Rally: https://nztechrally.nz

---

*Last updated: April 2026*
*Speaker: Dipin Thomas*
*Conference: NZ Tech Rally 2026, Wellington, 15 May 2026*