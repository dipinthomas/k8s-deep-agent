---
name: noisy-neighbor
description: Use this skill when a Kubernetes node shows high CPU utilization,
             when one pod is consuming disproportionate CPU or memory and
             impacting co-located pods, or when checkout/payment latency is
             rising without an obvious application-level cause.
---

## Noisy Neighbor Investigation Playbook

### Step 1 — Confirm node CPU saturation
```
kubectl top nodes
```
Look for any node at >80% CPU. Note the node name.

### Step 2 — Find the top CPU consumers on that node
```
kubectl top pods -n otel-demo --sort-by=cpu
```
Look for a pod consuming far more CPU than expected.
Any pod using >1000m on a 2-vCPU node is a suspect.

### Step 3 — Check which pods share the suspect node
```
kubectl get pods -n otel-demo -o wide | grep <node-name>
```
Identify all co-located pods — these are the victims.

### Step 4 — Correlate with checkout latency
Query CloudWatch ContainerInsights for checkout service p99 latency.
If latency is rising and the CPU spike matches the timing: confirmed noisy neighbor.

### Step 5 — Identify the culprit's priority class
```
kubectl get pod <suspect-pod> -n otel-demo -o jsonpath='{.spec.priorityClassName}'
```
Cross-reference with AGENTS.md priority classes.
`demo-stress` and `background` class pods are always safe to evict.

### Step 6 — Build the approval request
Post to Slack with:
- Culprit pod name, namespace, node, CPU consumption
- Victim pods affected (especially checkout/payment)
- Priority class of culprit (justifies eviction)
- Expected outcome after eviction (CPU drops, latency recovers)
- Clear APPROVE / DENY buttons
