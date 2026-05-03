---
name: retail-prod-eks-use1
description: Load this skill when operating against the retail-prod-eks-use1
             cluster. Defines the synthetic e-commerce checkout workload's
             services, tiers, priority classes, and known incident patterns.
             The workload lives in namespace shop-prod on cluster otel-demo-prod.
---

# Cluster Context: retail-prod-eks-use1 (EKS us-east-1)

---

## ALARM RESPONSE: checkoutservice-p99-latency-high

**When this alarm fires, follow this decision tree exactly:**

**Step A — Check for the noisy neighbor (do this first):**
```
kubectl get pods -n shop-prod -l app=inventory-sync-job
```

**If that command returns a Running pod → investigation is COMPLETE. Root cause confirmed.**
- `inventory-sync-job` is a CPU stress workload (stress-ng, no CPU limit) that saturates
  the node and throttles the `latency-synthesizer` pod, causing checkoutservice latency.
- You do NOT need CPU metric data or further evidence. Stop investigating.
- **The target to scale is `inventory-sync-job`, NOT `checkoutservice`.**
  `checkoutservice` is the VICTIM. `inventory-sync-job` is the CAUSE.
  `checkoutservice` is NOT a Deployment — there is no `deployment/checkoutservice` to scale.
- Action: TWO-TURN sequence:
  Turn N: post_to_slack + post_approval_request (no destructive tool).
  Turn N+1: kubectl_scale deployment/inventory-sync-job -n shop-prod --replicas=0 (alone).
- Do NOT delete the pod — that restarts it. Scale the Deployment to 0.
- Do NOT drain the node. Do NOT stand down.
- Do NOT scale `checkoutservice` — it does not exist as a Deployment.

See the universal noisy-neighbor skill for guidance on confusing signals
(OTel 504s, karpenter annotations, missing kubectl top data, priority class
not returned by subagent). Those are universal patterns — they apply here too.

**If no inventory-sync-job pod is Running → check alarm state:**
- If CloudWatch P99 < 300ms: false positive or already resolved. Stand down.
- If CloudWatch P99 ≥ 300ms but no stress pod: `latency-synthesizer` is in software
  spike mode only. Investigate further before acting.

---

## POD-TO-SERVICE MAPPING (read before looking for service pods)

**There are NO pods named `checkoutservice`, `paymentservice`, `cartservice`, etc.**
The alarm uses service names from CloudWatch metrics. Those services are NOT separate pods.

| CloudWatch service dimension | Actual pod in cluster | Namespace |
|---|---|---|
| checkoutservice, paymentservice, cartservice, productcatalogservice, frontendservice | `latency-synthesizer` | `shop-prod` |
| noisy-neighbor CPU culprit | `inventory-sync-job` | `shop-prod` |

**Do NOT search for a pod named `checkoutservice` — it does not exist.**
`latency-synthesizer` IS the checkoutservice (and all other services simultaneously).

---

## Cluster Facts

- **Cloud:** AWS us-east-1
- **EKS cluster name:** `otel-demo-prod`
- **Platform:** Amazon EKS Auto Mode (Kubernetes 1.33)
- **Observability:** ADOT sidecar → AWS X-Ray; CloudWatch metrics under
  namespace `RetailProd/Services`
- **Workload namespace:** `shop-prod`
- **AWS Profile:** `fernhub`
- **Slack channel:** `#k8s-alerts`

## Workload

**Important:** There is NO pod named `checkoutservice`, `paymentservice`, `cartservice`, etc.
The `latency-synthesizer` pod is the single process that simulates ALL five services using
per-service OTel TracerProviders. Do NOT search for individual service pods — they do not
exist. When the checkoutservice alarm fires, the relevant pods in `shop-prod` are:
- `latency-synthesizer` — the workload pod (always present)
- `inventory-sync-job` — the noisy-neighbor culprit (present only during spike)

Five "services" (a single Python process with per-service OTel TracerProviders)
emit traces matching this call graph:

```
frontendservice
   └─ checkoutservice (POST /checkout)
        ├─ cartservice (GET /cart/{userId})
        ├─ productcatalogservice (POST /products/lookup)
        └─ paymentservice (POST /charge)              ← hot path
             └─ productcatalogservice (POST /reserve)
```

A batch pod `inventory-sync-job` in `shop-prod` is scaled to 1 during the
noisy-neighbor scenario. It runs `stress-ng` with no CPU limit, saturating the
node and CPU-throttling `paymentservice`'s processing loops.

## Service Tiers and Priority Classes

### Critical (Priority Class: `payment-critical`, value 1000000)
**Never evict, restart, or disrupt without explicit human approval.**

| Service | Role |
|---|---|
| checkoutservice | Orchestrates checkout |
| paymentservice  | Processes charges |
| frontendservice | Customer entrypoint |

### User-facing (Priority Class: `user-facing`, value 500000)

| Service | Role |
|---|---|
| cartservice           | Cart state |
| productcatalogservice | Product lookup + reservation |

### Background (Priority Class: `low-priority`, value 100)

| Workload | Role |
|---|---|
| inventory-sync-job | Batch reconciliation. **Safe to evict at any time.** |

## Healthy Thresholds (CloudWatch namespace `RetailProd/Services`)

| Service | LatencyP50 | LatencyP99 | ErrorRate |
|---|---|---|---|
| frontendservice       | <10ms  | <30ms  | <0.1% |
| checkoutservice       | <30ms  | <100ms | <0.5% |
| cartservice           | <15ms  | <40ms  | <0.1% |
| productcatalogservice | <40ms  | <120ms | <0.1% |
| paymentservice        | <80ms  | <300ms | <0.1% |

## Active Alarm

**`checkoutservice-p99-latency-high`** — threshold 300ms, period 30s,
2 evaluation periods. Fans out via SNS `retail-prod-incidents` to:
- AWS Chatbot → Slack `#k8s-alerts` (alarm card)
- Lambda `retail-prod-alarm-to-agent` → POST `/trigger` on the agent

## Scenario Calibration and Stand-Down Rules

The `latency-synthesizer` pod in `shop-prod` exposes its active scenario via
the `SCENARIO` env var. The SCENARIO variable does NOT change the incident response rules.
A Running `inventory-sync-job` always requires remediation regardless of SCENARIO.

**Expected P99 by scenario (software only, no external CPU pressure):**

| SCENARIO | checkoutservice P99 | paymentservice P99 |
|---|---|---|
| `healthy` | ~30-50ms | ~50-80ms |
| `spike` | ~400-600ms | ~350-500ms |

### When `inventory-sync-job` is Running: Action IS Required

**Decision rule (no exceptions):** if `kubectl get pods -n shop-prod -l app=inventory-sync-job`
returns any pod in Running or Pending state, action is required. You do NOT need
CPU metric data to confirm — the pod's presence with replicas > 0 IS the
confirmation. `kubectl top` may show no data for a newly started pod (metrics
server lags by 2-3 minutes); that lag does not change the decision.

It is a CPU stress workload (stress-ng) with no CPU limit that saturates the
node. Its presence alongside an elevated P99 IS the incident — it is the root
cause, not a symptom.

**If `kubectl top` fails or returns no data:** this is expected for a pod that
started recently. Confidence is still **High**. In the post_to_slack message,
use this exact bullet for the confidence line:
`• *Confidence:* High — pod presence alongside elevated P99 is definitive per cluster runbook`
Do NOT write "likely", "probable", "suspected", or "if this aligns" — the presence
of a Running `inventory-sync-job` IS the confirmation. Express it as confirmed fact.

- Do NOT conclude "not enough evidence" when `inventory-sync-job` is Running.
- Do NOT express uncertainty about root cause when `inventory-sync-job` is Running.
- Do NOT stand down without posting an approval request.
- Do NOT drain the node — the entire node is not the problem.
- The correct action is the two-turn approval sequence:
  Turn N: post_to_slack (findings) + post_approval_request (approval card) — no destructive tool.
  Turn N+1: kubectl_scale deployment/inventory-sync-job -n shop-prod --replicas=0 — alone.
  **Do NOT delete the individual pod** — `inventory-sync-job` is a Deployment and will
  immediately restart if you delete the pod. Scale the Deployment to 0 replicas instead.

**P99 > 1000ms with SCENARIO=spike** means the stress pod is amplifying the
software-defined latency (expected ceiling ~500-600ms) by throttling the
synthesizer's CPU-burn loops. This is a real incident, not expected behavior.

### When NOT to take action

Stand down (call `mark_stand_down`) only if:
- CloudWatch P99 for checkoutservice is below 300ms, AND
- `inventory-sync-job` replicas = 0 or pod not found

Otherwise: investigate and propose remediation.

## Noisy-Neighbor Investigation Playbook

This is the primary incident pattern for this cluster. When
`checkoutservice-p99-latency-high` fires, follow this order:

### Step 1 — Find the hot node
```
kubectl top nodes
kubectl top pods -n shop-prod --sort-by=cpu
```
Look for `inventory-sync-job` at the top of the CPU list. It has no CPU
limit so it shows 100%+ of its request easily.

### Step 2 — Confirm co-location
```
kubectl get pods -n shop-prod -o wide
```
Verify that `inventory-sync-job` and the `latency-synthesizer` pod share
the same node (same NODE column). The synthesizer runs paymentservice
logic on the same node.

### Step 3 — Confirm latency cascade in CloudWatch
Query `RetailProd/Services` for:
- `LatencyP99` dimension `Service=paymentservice` — expect >300ms during spike
- `LatencyP99` dimension `Service=checkoutservice` — will be elevated too
  (includes payment wait time)
- `ErrorRate` dimension `Service=checkoutservice` — may show 504s

### Step 4 — Confirm priority class of culprit
```
kubectl get pod -n shop-prod -l app=inventory-sync-job \
  -o jsonpath='{.items[0].spec.priorityClassName}'
```
Expected: `low-priority` — confirms it is safe to evict.

### Step 5 — Approval request + remediation (TWO TURNS)
Turn N — call both in one response, no destructive tool:
- post_to_slack (findings summary using the template below)
- post_approval_request (approval card)

The middleware will reply "✅ Approval card posted."

Turn N+1 — call the destructive tool ALONE:
```
kubectl_scale deployment/inventory-sync-job -n shop-prod --replicas=0
```
**Do NOT delete the individual pod** — deleting a Deployment's pod just restarts it.
**Do NOT include post_to_slack or post_approval_request in Turn N+1** — call kubectl_scale alone.

After scaling, verify: `kubectl top pods -n shop-prod` should show CPU
returning to baseline within 30s. Check CloudWatch P99 returning below
100ms for checkoutservice within 2 metric periods (~60s).

## Known Patterns

| Symptom | Root Cause | Action |
|---|---|---|
| checkoutservice P99 high, paymentservice P99 high, no errors | CPU throttle from noisy neighbor | Scale `inventory-sync-job` deployment to 0 |
| checkoutservice 504s, paymentservice slow | Same — checkout times out waiting for payment | Scale `inventory-sync-job` deployment to 0 |

## Eviction Order

1. `inventory-sync-job` (low-priority, batch — always safe)
2. Other low-priority workloads in `shop-prod`
3. **STOP.** Anything user-facing or critical requires explicit human approval.

Use `kubectl_scale deployment/<name> -n shop-prod --replicas=0`. Do not delete the pod (Deployment restarts it immediately).
Pod delete alone does not stop a Deployment — it restarts immediately.

## Slack and Approval

- Alarms post to `#k8s-alerts-new` via AWS Chatbot.
- Agent posts investigation updates and approval requests in the same channel.
- Approval contact: on-call engineer in `#k8s-alerts-new`.

## Slack Message Templates

Use EXACTLY these formats. Do not add extra headers, narrative prose, or
sections beyond what is shown below.

### post_to_slack — investigation findings

Call this FIRST (before post_approval_request) in Turn N.
Fill in the `{placeholders}` with real values from subagent reports:

```
:rotating_light: *checkoutservice | P99 {measured_p99}ms* — threshold 300ms breached

*Root cause* — `inventory-sync-job` Running in `shop-prod`
Stress-ng workload (no CPU limit) saturating node CPU, throttling the payment processing loops.

*Why it's safe to stop* — `inventory-sync-job` is PriorityClass `low-priority` (value 100). Critical services (`checkoutservice`, `paymentservice`) run at PriorityClass `payment-critical` (value 1,000,000). Stopping the lowest-priority workload has zero impact on checkout.

*Impact* — checkoutservice and paymentservice latency elevated · no errors · no data loss
*Fix* — scale `inventory-sync-job` to 0 · approval request below ↓

━━━━━━━━━━━━━━━━━━━━━
:mag: *Investigation details*

• *CloudWatch:* checkoutservice P99 {measured_p99}ms · paymentservice P99 {pay_p99}ms · alarm fired {alarm_time} UTC
• *Node:* `{node}` — `inventory-sync-job` and `latency-synthesizer` co-located on this node
• *Priority class:* `inventory-sync-job` → `low-priority` (100) · `checkoutservice` → `payment-critical` (1,000,000)
• *OTel:* Latency spike on payment path · cartservice and productcatalogservice within normal range
• *Confidence:* High — Running pod + priority class + elevated P99 are definitive per runbook
```

Keep all verbose detail BELOW the `━━━` line — Slack collapses long sections
with a "Show more" link so the summary above stays visible without scrolling.

**Format rules for the verbose section:**
- Every item MUST be a `•` bullet using `• *Label:* value` format — no prose paragraphs.
- The five bullet labels are fixed: CloudWatch, Node, Priority class, OTel, Confidence.
- If a subagent did not return the priority class, substitute the documented value:
  `inventory-sync-job` → `low-priority` (100) · `checkoutservice` → `payment-critical` (1,000,000).
  Never write "not reported by tools" — the cluster skill is the authoritative source.


### post_approval_request — fill fields as follows (one line each)

Call this SECOND (after post_to_slack) in the SAME Turn N. NOT in Turn N+1.

- `summary`: `inventory-sync-job Running — confirmed noisy neighbour causing CPU saturation`
- `evidence`: `See investigation details above ↑`
- `action_list`: `kubectl scale deployment/inventory-sync-job -n shop-prod --replicas=0`
- `impact`: `Stops stress workload · P99 expected back below 100ms within 60s`
