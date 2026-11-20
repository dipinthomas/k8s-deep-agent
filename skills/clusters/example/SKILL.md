---
name: my-cluster
description: Cluster skill for <cluster name>. Services, tiers, priority classes,
             healthy thresholds and known noisy workloads.
---

# <cluster name>

EKS cluster `<cluster name>`, region `<region>`. Workload namespaces:
`<namespace>` (<what runs there>), `<namespace>` (<what runs there>).
Never assume a namespace; confirm with kubectl_get first.

## Tiers and priority classes

| Tier | PriorityClass | Services | Rule |
|---|---|---|---|
| Critical | `payment-critical` | <service>, <service> | NEVER scale, delete or restart without explicit human instruction |
| User-facing | `user-facing` | <service> | Only with approval and a clear reason |
| Background | `background` | <batch jobs> | First to sacrifice under resource pressure |

## Healthy thresholds

- <service> p99 latency below <N> ms (CloudWatch namespace `<namespace>`, metric `<metric>`)
- Node CPU below <N>%

## Known noisy workloads

- `<namespace>/<deployment>`: <why it can hog CPU/memory/disk>. Preferred fix:
  `kubectl scale deployment/<name> -n <namespace> --replicas=0` (deleting the pod
  doesn't help: its ReplicaSet recreates it).

## Decision tree for <alarm name>

1. Find the node the alarmed service runs on.
2. List every pod on that node with CPU usage and limits.
3. If a background pod without a CPU limit is using most of the CPU, it is the cause.
   The alarmed service is the victim; never act on the victim.
4. Post the evidence, ask for approval, scale the cause to 0, then confirm the
   latency metric is back under the threshold.
