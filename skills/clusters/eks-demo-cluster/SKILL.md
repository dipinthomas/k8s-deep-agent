---
name: eks-demo-cluster
description: Load this skill when operating against the eks-demo-cluster.
             Defines the OpenTelemetry Astronomy Shop workload's services,
             tiers, priority classes, and known incident patterns.
---

# Cluster Context: eks-demo-cluster (EKS us-east-1)

## Cluster Facts

- **Cloud:** AWS us-east-1
- **Platform:** Amazon EKS Auto Mode (Kubernetes 1.33)
- **Observability:** OTel Collector → Amazon CloudWatch (Container Insights + custom metrics)
- **Workload namespace:** `otel-demo`
- **AWS Profile:** `fernhub`
- **Node type:** c6a.large (2 vCPU, ~4 GB RAM, ~100 GB ephemeral storage)
- **Disk pressure threshold:** 85% node filesystem utilisation

## CloudWatch Namespaces

- `ContainerInsights` — node and pod level CPU, memory, disk metrics
- `OTelDemo` — application-level latency, error rate, throughput per service

## OUT-OF-SCOPE WORKLOADS — NEVER TARGET THESE

| Namespace       | Workload                                          | Reason                              |
|-----------------|---------------------------------------------------|-------------------------------------|
| `k8s-agent`     | `k8s-agent`, `k8s-mcp-gateway`, `agent-redis`    | Agent infrastructure                |
| `kube-system`   | any                                               | Kubernetes control plane            |
| `amazon-cloudwatch` | any                                           | Observability pipeline              |

---

## Application: OpenTelemetry Astronomy Shop

A 16-service e-commerce microservices demo. Services communicate via gRPC and HTTP.
All emit traces, metrics, and logs via OpenTelemetry SDK → OTel Collector → CloudWatch.

### Service Tiers and Priority Classes

#### Payment-Critical (Priority Class: `payment-critical`, value 1000000)
**Never evict, restart, or disrupt without explicit human approval.**

| Service               | Language      | Role                                      |
|-----------------------|---------------|-------------------------------------------|
| checkoutservice       | Go            | Orchestrates the full checkout flow       |
| paymentservice        | JavaScript    | Processes payment transactions            |
| cartservice           | .NET          | Manages shopping cart state               |
| productcatalogservice | Go            | Serves product data to checkout/frontend  |

Healthy thresholds:
- `checkoutservice` p99 < 150ms. Degraded: 150–500ms. Critical: > 500ms.
- `paymentservice` error rate < 0.1%. Alert threshold: > 1%.

#### Infrastructure (Priority Class: `infrastructure`, value 900000)
Support services — evicting affects observability or image serving, not payments.

| Service          | Role                  | Known Characteristics                                                                                          |
|------------------|-----------------------|----------------------------------------------------------------------------------------------------------------|
| imageprovider    | nginx image server    | High disk I/O under load; verbose logging can write 40–50 MB/min. First suspect under disk pressure.          |
| otel-collector   | Telemetry pipeline    | Writes trace buffers to emptyDir. Usually within normal limits (~30–40% full). Measure before concluding.     |

#### User-Facing (Priority Class: `user-facing`, value 500000)
Disruption degrades browsing but does not affect payments.

| Service               | Language       | Role                        |
|-----------------------|----------------|-----------------------------|
| frontend              | TypeScript     | Main web UI                 |
| shippingservice       | Go             | Calculates shipping costs   |
| currencyservice       | C++            | Currency conversion         |
| recommendationservice | Python         | "You might also like"       |

#### Background (Priority Class: `background`, value 100000)
Safe to evict under resource pressure. No direct user or payment impact.

| Service              | Language | Role                                               |
|----------------------|----------|----------------------------------------------------|
| loadgenerator        | Python   | Synthetic traffic — always safe to stop            |
| adservice            | Java     | Banner ads                                         |
| emailservice         | Go       | Order confirmation emails                          |
| frauddetectionservice| Python   | Async fraud scoring (non-blocking)                 |
| quoteservice         | PHP      | Shipping quotes                                    |

---

## Eviction Order (lowest impact → highest)

When resource pressure requires eviction, work in this order and stop as soon as pressure resolves:

1. `loadgenerator` — synthetic traffic, zero user impact
2. `adservice`, `emailservice`, `frauddetectionservice`, `quoteservice` — background batch
3. `recommendationservice` — browsing feature, not checkout
4. `imageprovider` — images unavailable, browsing degraded, checkout unaffected
5. `frontend` — browsing impossible, checkout still reachable via API
6. **STOP** — never proceed to payment-critical tier without explicit human approval

Use `kubectl_scale deployment/<name> -n otel-demo --replicas=0`. Do not delete the pod —
a Deployment-managed pod restarts immediately.

---

## Known Incident Patterns

| Incident Type              | Typical Signal                                              | Universal Skill to Load            |
|----------------------------|-------------------------------------------------------------|------------------------------------|
| Node disk pressure         | `DiskPressure=True` on node, pods evicted, imageprovider spike | `node-disk-pressure`            |
| Noisy neighbour            | One pod consuming disproportionate CPU, co-located pods degraded | `noisy-neighbor`              |
| Checkout/payment degraded  | Checkout p99 > 500ms, payment errors rising                 | `critical-service-protection`      |
| Eviction decisions needed  | Multiple pods competing for scarce resources                | `pod-priority-eviction`            |
| CrashLoopBackOff           | High restart count, OOMKilled or app error in logs          | kubectl events + CloudWatch logs   |
| Pending pods               | Pods stuck Pending — insufficient resources                 | kubectl describe node + events     |

## Cluster Characteristics (pattern-matching hints)

- **imageprovider** is the most common disk pressure culprit. Check it first under any disk/I/O alert.
- **loadgenerator** amplifies all resource pressure. Stopping it is always the lowest-risk first action.
- **otel-collector emptyDir** looks alarming but is rarely the root cause of disk pressure. Measure before acting.
- **No HPA** is configured for most services. Evicted pods reschedule on the same or another node.
- **EKS Auto Mode** provisions and terminates nodes automatically. A node with only low-priority pods may be drained and terminated by Karpenter — this is expected behaviour, not an incident.

---

## Fault-Injection Pods (demo only)

During demos, fault-injection pods may appear in the `default` namespace:

| Pod name      | Purpose                              | Safe to evict? |
|---------------|--------------------------------------|----------------|
| `demo-stress` | CPU stress (noisy-neighbour scenario) | Yes — always  |
| `demo-disk-filler` | Fill node disk (disk-pressure scenario) | Yes — always |

If either pod is Running and the corresponding alarm has fired, it is the root cause.
Apply the relevant universal skill (`noisy-neighbor` or `node-disk-pressure`).

---

## Healthy Thresholds

| Metric                      | Normal      | Degraded    | Critical    |
|-----------------------------|-------------|-------------|-------------|
| checkoutservice p99 latency | < 150ms     | 150–500ms   | > 500ms     |
| paymentservice error rate   | < 0.1%      | 0.1–1%      | > 1%        |
| Node CPU utilisation        | < 70%       | 70–90%      | > 90%       |
| Node disk utilisation       | < 70%       | 70–85%      | > 85%       |

---

## Slack

- **Channel:** `#k8s-alerts`
- Post all investigation updates and approval requests as thread replies to the original alert.

## Slack Message Templates

### post_to_slack — investigation findings

```
:rotating_light: *{alarm_name} | {metric} {measured_value}* — threshold breached

*Root cause* — {root_cause_summary}

*Why it's safe to act* — {priority_class_rationale}

*Impact* — {user_impact}
*Fix* — {proposed_action}

━━━━━━━━━━━━━━━━━━━━━
:mag: *Investigation details*

• *CloudWatch:* {cloudwatch_findings}
• *Node:* {node_findings}
• *Priority class:* {priority_class_findings}
• *Confidence:* {confidence_statement}
```

### post_approval_request — fill fields as follows

- `summary`: one-line description of root cause and proposed action
- `evidence`: `See investigation details above ↑`
- `action_list`: exact kubectl command (e.g. `kubectl scale deployment/<name> -n otel-demo --replicas=0`)
- `impact`: expected outcome after remediation
