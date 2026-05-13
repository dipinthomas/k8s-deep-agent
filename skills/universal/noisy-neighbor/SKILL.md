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
- Priority class of the culprit and the victim — the contrast between
  a low-priority batch workload and a critical-tier service is the key
  justification for the action
- Expected outcome after eviction (resource pressure drops, victim
  metrics recover within ~60s)
- APPROVE / DENY buttons via `post_approval_request`

### Step 7 — Choose remediation based on the culprit's resource type

First, determine what controls the culprit pod:
```
kubectl get pod <name> -n <namespace> -o jsonpath='{.metadata.ownerReferences[0].kind}'
```

| Owner kind        | Remediation command                                                   |
|-------------------|-----------------------------------------------------------------------|
| ReplicaSet        | `kubectl_scale deployment/<name> -n <ns> --replicas=0`               |
| (none — bare pod) | `kubectl_delete pod <name> -n <ns>` — will NOT restart automatically |
| StatefulSet       | `kubectl_scale statefulset/<name> -n <ns> --replicas=0`              |
| DaemonSet         | `kubectl_delete pod <name> -n <ns>` — cannot scale DaemonSets to 0  |
| Job / CronJob     | `kubectl_delete pod <name> -n <ns>` — deleting is sufficient         |

**Never** `kubectl_delete` a Deployment-managed pod — the controller restarts it immediately on the same node, undoing the remediation.
**Never** `kubectl_scale` a bare pod or DaemonSet — the command will fail or have no effect.

Check the cluster skill: it may name the culprit explicitly, confirm the safe
action, and specify whether CPU metric data is required or pod presence alone
is sufficient to justify action.

---

## Handling Confusing Evidence (applies to any cluster)

These are common signals that look contradictory but are actually consistent
with a noisy-neighbor CPU saturation root cause. Do NOT let them override a
confirmed culprit pod identified by the cluster skill's decision tree.

### OTel / application-layer 504s and timeout errors
When a CPU-bound workload (e.g. a stress-ng batch job) saturates a node, it
CPU-throttles any co-located pod that does CPU-intensive work (processing loops,
cryptography, serialization). This causes those pods to stall internally, which
cascades into downstream 504 Gateway Timeout errors visible in OTel traces.

**504s on the checkout or payment path ARE consistent with CPU throttle from a
noisy-neighbor — they are NOT evidence of a separate root cause.**
Do not interpret checkout-path timeouts as pointing to a dependency network
issue when a high-CPU batch job is confirmed Running on the same node.

### `karpenter.sh/do-not-disrupt: "true"` annotation
This annotation instructs Karpenter NOT to evict or consolidate the pod during
node lifecycle operations. It has **no effect on `kubectl scale`**. You can
scale the Deployment to 0 replicas regardless of this annotation — `kubectl scale`
bypasses Karpenter entirely and writes directly to the Deployment spec.

### `kubectl top` shows no data or the call fails
The metrics-server has a 2–3 minute lag for newly started pods. If `kubectl top`
returns no data or fails for the suspect pod, this is expected and does NOT mean
the pod is not consuming CPU. If the cluster skill states that pod presence alone
is sufficient evidence (e.g. a known stress workload), treat missing `kubectl top`
data as a metrics-server lag, not as "insufficient evidence."

### Priority class not returned by the subagent
If the subagent's `kubectl` output did not include priority class information,
use the values documented in the cluster skill. Do not treat a missing priority
class as a reason to delay action — the cluster skill's tier table is authoritative.
