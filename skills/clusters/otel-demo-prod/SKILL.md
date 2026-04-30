---
name: otel-demo-cluster
description: Load this skill when operating against the otel-demo-prod cluster.
             It defines the services, tiers, priority classes, cluster characteristics,
             and known incident patterns for the OpenTelemetry Astronomy Shop workload.
---

# Cluster Context: otel-demo-prod (EKS us-east-1)

## Cluster Facts

- **Cloud:** AWS us-east-1 (N. Virginia)
- **Platform:** Amazon EKS (managed Kubernetes)
- **Observability:** OTel Collector → Amazon CloudWatch (Container Insights + custom metrics)
- **Namespace:** `otel-demo` (all application workloads run here)
- **AWS Account:** fernhub (637039075925)
- **AWS Profile:** `fernhub`
- **Node type:** m5.2xlarge (8 vCPU, 32 GB RAM, ~100 GB ephemeral storage)
- **Disk pressure threshold:** 85% node filesystem utilisation

## Application: OpenTelemetry Astronomy Shop

A 16-service e-commerce microservices application. Services communicate via gRPC and HTTP.
All emit traces, metrics, and logs via OpenTelemetry SDK → in-cluster OTel Collector → CloudWatch.

CloudWatch namespaces:
- `ContainerInsights` — node and pod level CPU, memory, disk metrics
- `OTelDemo` — application-level latency, error rate, throughput per service

## Service Tiers and Priority Classes

### Payment-Critical (Priority Class: `payment-critical`, value 1000000)
Handles real payment processing. Disruption means orders fail and revenue is lost.
**Never evict, restart, or disrupt without explicit written human approval.**

| Service | Language | Role |
|---|---|---|
| checkoutservice | Go | Orchestrates the full checkout flow |
| paymentservice | JavaScript | Processes payment transactions |
| cartservice | .NET | Manages shopping cart state |
| productcatalogservice | Go | Serves product data to checkout and frontend |

Healthy thresholds:
- `checkoutservice` p99 latency < 150ms. Degraded: 150–500ms. Critical: > 500ms.
- `paymentservice` error rate < 0.1%. Alert: > 1%.

### Infrastructure (Priority Class: `infrastructure`, value 900000)
Support services. Evicting affects observability or image serving, not payments.

| Service | Role | Known Characteristics |
|---|---|---|
| imageprovider | nginx — serves product images | High disk I/O under load; verbose access logging can produce 40–50 MB/min of writes under high traffic. First suspect under disk pressure. |
| otel-collector | Telemetry pipeline | Writes trace buffers to emptyDir. Appears as a disk pressure suspect but is usually within normal limits (30–40% full). Always measure before concluding. |

### User-Facing (Priority Class: `user-facing`, value 500000)
Disruption degrades the browsing experience but does not affect payments.

| Service | Language | Role |
|---|---|---|
| frontend | TypeScript/Next.js | Main web UI |
| shippingservice | Go | Calculates shipping costs |
| currencyservice | C++ | Currency conversion |
| recommendationservice | Python | "You might also like" suggestions |

### Background (Priority Class: `background`, value 100000)
Safe to evict under resource pressure. No direct user or payment impact.

| Service | Language | Role |
|---|---|---|
| loadgenerator | Python/Locust | Simulates synthetic user traffic — always safe to stop, zero real-user impact |
| adservice | Java | Serves banner ads |
| emailservice | Go | Sends order confirmation emails |
| frauddetectionservice | Python | Async fraud scoring (non-blocking) |
| quoteservice | PHP | Generates shipping quotes |

## Eviction Order (lowest priority first)

When resource pressure requires eviction, always work in this order and stop
as soon as pressure is resolved:

1. `loadgenerator` — synthetic traffic, zero user impact
2. `adservice`, `emailservice`, `frauddetectionservice`, `quoteservice` — background jobs
3. `recommendationservice` — browsing feature, not checkout
4. `imageprovider` — product images unavailable, browsing degraded, checkout unaffected
5. `frontend` — browsing impossible, checkout still reachable via API
6. **STOP** — never proceed to payment-critical tier without explicit approval

## Cluster Characteristics

Known behaviours that help pattern-match incidents in this cluster:

- **imageprovider** is the most common disk pressure culprit. Check it first under any
  disk or I/O related alert.
- **loadgenerator** amplifies all resource pressure. Stopping it is always the lowest-risk
  first action and often reduces pressure enough to avoid further evictions.
- **otel-collector emptyDir** looks alarming but is rarely the root cause of disk pressure.
  Always measure actual usage before dismissing or escalating it.
- **No HPA** is configured for most services. Evicted pods reschedule on the same or
  another node — they do not automatically scale up. Watch for pods re-landing on a
  still-pressured node.
- **EKS node disk pressure** fires at 85% filesystem utilisation. The kubelet begins
  evicting background-class pods automatically at this threshold before the agent acts.

## Known Incident Patterns

| Incident Type | Typical Signal | Skill to Select |
|---|---|---|
| Node disk pressure | `DiskPressure=True` on node, pods evicted, imageprovider I/O spike | `node-disk-pressure` |
| Noisy neighbour | One pod consuming disproportionate CPU/memory, co-located pods degraded | `noisy-neighbor` |
| Checkout/payment degradation | Checkout p99 > 500ms, payment error rate rising | `critical-service-protection` |
| Pod eviction decisions | Need to rank and select pods for eviction | `pod-priority-eviction` |
| CrashLoopBackOff | High restart count, OOMKilled or application error in logs | kubectl events + CloudWatch logs |
| Pending pods | Pods stuck Pending — insufficient resources or scheduling constraints | kubectl describe node + events |
| OTel pipeline failure | Metrics/traces missing from CloudWatch, collector pod unhealthy | kubectl + otel-collector logs |

## Slack

- **Channel:** `#k8s-alerts`
- **Approval contact:** @dipin
- Post all investigation updates and approval requests as thread replies to the original alert.
