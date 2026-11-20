---
name: retail-prod-cluster
description: Cluster skill for the retail-prod EKS cluster (lab replay of the
             Cyber Monday checkout latency incident). Services, tiers,
             priority classes, known noisy workloads and Slack templates.
---

# retail-prod cluster

EKS cluster `retail-prod`, region ap-southeast-2. Workload namespaces:
`retail` (shop services), `batch` (background jobs), `observability`.
Never assume a namespace; confirm with kubectl_get first.

## Tiers and priority classes

| Tier        | PriorityClass       | Value     | Services                                        |
|-------------|---------------------|-----------|-------------------------------------------------|
| Critical    | payment-critical    | 1000000   | checkoutservice, paymentservice, cartservice     |
| User-facing | user-facing         | 500000    | frontend, productcatalogservice                  |
| Background  | background          | 100000    | image-resizer, report-exporter                   |

Critical services must never be scaled, restarted or deleted by the agent.

## Healthy thresholds (CloudWatch namespace `RetailProd/Services`)

- checkoutservice LatencyP99 below 300 ms, ErrorRate below 1 %
- paymentservice LatencyP99 below 250 ms

## Known CPU-heavy background workloads

- `batch/image-resizer` (Deployment): thumbnail regeneration. Has NO CPU limit.
  When it runs on the same node as checkoutservice it CPU-starves checkout.

## Decision tree for checkout latency alarms

1. Is any background-tier pod using more than 1000m CPU on the node that runs
   checkoutservice? If yes, that pod is the root cause (noisy neighbour).
2. The alarm service (checkoutservice) is the VICTIM. Never target it.
3. Remediate the culprit by its controller kind (see noisy-neighbor skill).
   For `image-resizer` that is: `kubectl_scale deployment/image-resizer -n batch --replicas=0`.
4. After the action, confirm checkout LatencyP99 is back below 300 ms.

## Slack templates

post_to_slack (findings):

```
*Root cause:* {one line}
*Victim:* {service} p99 {value} ms (threshold 300 ms)
*Culprit:* {namespace}/{pod} {cpu}m CPU, no limit, priority {class}
━━━
• *Node:* {node} CPU {pct}%
• *Evidence:* {metric + kubectl facts}
• *Recommended action:* {exact kubectl command}
```
