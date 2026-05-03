---
name: retail-prod-eks-use1
description: Load this skill when operating against the retail-prod-eks-use1
             cluster. Defines the synthetic e-commerce checkout workload's
             services, tiers, priority classes, and known incident patterns.
             The workload lives in namespace shop-prod on cluster otel-demo-prod.
---

# Cluster Context: retail-prod-eks-use1 (EKS us-east-1)

This is a **demo cluster** running a synthetic checkout workload in namespace
`shop-prod` on EKS cluster `otel-demo-prod`. Latency and errors are generated
by `latency-synthesizer` (no real business logic). Tier definitions still
apply — treat services as production-critical during the investigation.

## Cluster Facts

- **Cloud:** AWS us-east-1
- **EKS cluster name:** `otel-demo-prod`
- **Platform:** Amazon EKS Auto Mode (Kubernetes 1.33)
- **Observability:** ADOT sidecar → AWS X-Ray; CloudWatch metrics under
  namespace `RetailProd/Services`
- **Workload namespace:** `shop-prod`
- **AWS Profile:** `fernhub`
- **Slack channel:** `#retail-prod-incidents`

## Workload

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
- AWS Chatbot → Slack `#retail-prod-incidents` (alarm card)
- Lambda `retail-prod-alarm-to-agent` → POST `/trigger` on the agent

## Scenario Calibration and Stand-Down Rules

The `latency-synthesizer` pod in `shop-prod` exposes its active scenario via
the `SCENARIO` env var. This is expected on a demo cluster — it does NOT mean
the incident is intentional or that no action is needed.

**Expected P99 by scenario (software only, no external CPU pressure):**

| SCENARIO | checkoutservice P99 | paymentservice P99 |
|---|---|---|
| `healthy` | ~30-50ms | ~50-80ms |
| `spike` | ~400-600ms | ~350-500ms |

### When `inventory-sync-job` is Running: Action IS Required

**If `inventory-sync-job` pod is present in `shop-prod` with replicas > 0,
that is the noisy-neighbor.** It is a CPU stress workload (stress-ng) with no
CPU limit that deliberately saturates the node. Its presence alongside an
elevated P99 IS the incident — it is the root cause, not a symptom.

- Do NOT conclude "expected load-test behavior" when `inventory-sync-job` is Running.
- Do NOT stand down without posting an approval request.
- The correct action is: post findings + `post_approval_request` + queue
  `kubectl_delete pod <inventory-sync-job-pod> -n shop-prod` in the same turn.

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

### Step 5 — Approval request + remediation
Post findings to Slack, then issue `post_approval_request` AND the
delete call in the same turn:
```
kubectl_delete pod <inventory-sync-job-pod-name> -n shop-prod
```
After deletion, verify: `kubectl top pods -n shop-prod` should show CPU
returning to baseline within 30s. Check CloudWatch P99 returning below
100ms for checkoutservice within 2 metric periods (~60s).

## Known Patterns

| Symptom | Root Cause | Action |
|---|---|---|
| checkoutservice P99 high, paymentservice P99 high, no errors | CPU throttle from noisy neighbor | Delete `inventory-sync-job` pod |
| checkoutservice 504s, paymentservice slow | Same — checkout times out waiting for payment | Delete `inventory-sync-job` pod |

## Eviction Order

1. `inventory-sync-job` (low-priority, batch — always safe)
2. Other low-priority workloads in `shop-prod`
3. **STOP.** Anything user-facing or critical requires explicit human approval.

Prefer `kubectl_delete pod <name> -n shop-prod` over node drain.

## Slack and Approval

- Alarms post to `#retail-prod-incidents` via AWS Chatbot.
- Agent posts investigation updates and approval requests in the same channel.
- Approval contact: on-call engineer in `#retail-prod-incidents`.
