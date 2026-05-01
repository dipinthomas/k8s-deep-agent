---
name: noisy-neighbor
description: Use this skill when a Kubernetes node shows high CPU or memory
             utilization, when one pod is consuming disproportionate
             resources and impacting co-located pods, or when critical
             service latency is rising without an obvious application-level
             cause.
---

## Noisy Neighbor Investigation Playbook

This playbook is cluster-agnostic. Critical service names, priority class
definitions, and namespace conventions come from the cluster skill loaded
for this deployment.

### Step 1 — Confirm node CPU or memory saturation
```
kubectl top nodes
```
Look for any node at >80% CPU or >85% memory. Note the node name and
which dimension is saturated.

### Step 2 — Find the top consumers on that node
For CPU:
```
kubectl top pods --all-namespaces --sort-by=cpu
```
For memory:
```
kubectl top pods --all-namespaces --sort-by=memory
```
Look for a pod consuming far more than expected. Any pod consuming a
large fraction of a node's allocatable CPU or memory is a suspect.

### Step 3 — Check which pods share the suspect node
```
kubectl get pods --all-namespaces -o wide | grep <node-name>
```
Identify all co-located pods — these are the potential victims. Cross-
reference with the cluster skill's tier definitions to see whether any
critical-tier services share the node.

### Step 4 — Correlate with critical-service symptoms
For each critical service named in the cluster skill, query its latency
percentiles and error rate from CloudWatch (or the metric namespace the
cluster skill specifies).

If the timing of the resource spike matches the timing of degraded
critical-service metrics: confirmed noisy neighbour, escalate urgency.

### Step 5 — Identify the culprit's priority class
```
kubectl get pod <suspect-pod> -n <namespace> -o jsonpath='{.spec.priorityClassName}'
```
Cross-reference with the priority classes defined in the cluster skill.
Lower-priority classes are safe to evict; the cluster skill specifies
which.

### Step 6 — Build the approval request
Post to Slack with:
- Culprit pod name, namespace, node, and resource consumption (CPU /
  memory with units)
- Victim pods affected, with explicit call-out for any critical-tier
  services
- Priority class of the culprit (justifies eviction)
- Expected outcome after eviction (resource pressure drops, victim
  metrics recover)
- Statement that critical-tier services are protected (list them from the
  cluster skill)
- APPROVE / DENY buttons via `post_approval_request`

### Step 7 — Remediation: prefer pod delete over drain
Same rationale as `node-disk-pressure` — issue
`kubectl_delete pod <name> -n <namespace>` for the culprit. The controller
reschedules it; if it lands on the same node and immediately re-saturates,
escalate by proposing scale-down or resource-limit changes via a fresh
approval gate.
