---
name: node-disk-pressure
description: Use this skill when a Kubernetes node shows disk pressure,
             high disk usage, DiskPressure condition, or when pods are
             being evicted due to disk resource constraints.
---

## Node Disk Pressure Investigation Playbook

This playbook is cluster-agnostic. Service names, namespaces, and known
high-disk-I/O suspects come from the cluster skill loaded for this
deployment — refer to it before naming specific workloads.

### Step 1 — Confirm the condition
```
kubectl describe node <node-name> | grep -A5 Conditions
```
Look for: `DiskPressure = True`. Note the node name and the timestamp the
condition transitioned to true.

### Step 2 — Check known high-disk suspects FIRST
The cluster skill should list services with known high disk I/O behaviour
(verbose loggers, telemetry buffers, image servers, etc.). If any are
listed, query their disk write rates first — they are the most common root
cause of disk pressure on this cluster.

For each suspect:
```
kubectl logs -n <namespace> deployment/<suspect> --tail=100
```
Check CloudWatch Container Insights:
- Filter: `pod_name = <suspect>`, metric: `container_fs_usage_bytes`
- Look for write rate >> baseline (e.g. >20MB/min where baseline is <5MB/min)

If a suspect is clearly the culprit, skip to Step 6.

### Step 3 — Find top disk consumers on the node
```
kubectl get pods --all-namespaces -o wide | grep <node-name>
```
For each pod: check CloudWatch `container_fs_usage_bytes` grouped by
`PodName`.

Run this CloudWatch Logs Insights query on the cluster's Container Insights
performance log group:
```
fields @timestamp, pod_name, container_fs_usage_bytes
| filter ClusterName = "<cluster-name>"
| stats max(container_fs_usage_bytes) as max_bytes by pod_name
| sort max_bytes desc
| limit 10
```

### Step 4 — Check telemetry / observability buffers
If the cluster runs an in-cluster observability collector (OTel collector,
Fluent Bit, etc.) that buffers to `emptyDir`, check its current usage:
```
kubectl describe pod -n <namespace> -l <collector-label>
```
**Note:** telemetry collector buffers are a common red herring — they look
alarming but are usually within normal limits. Always measure before
concluding.

### Step 5 — Correlate with application symptoms
For each critical service named in the cluster skill, check its latency and
error rate over the past 15 minutes via CloudWatch (or whichever metric
namespace the cluster skill specifies).

If any critical service is degraded above its threshold (also defined in
the cluster skill): disk pressure is already affecting users → escalate
urgency in the Slack message.

### Step 6 — Identify eviction candidates
Cross-reference the pod list on the affected node with the priority classes
defined in the cluster skill. Build a ranked eviction list from lowest to
highest priority. Use the cluster skill's eviction order if it specifies
one.

**NEVER include critical-tier services** (defined in the cluster skill) in
the eviction list without explicit human approval. See the
`critical-service-protection` skill.

### Step 7 — Calculate estimated recovery
For each pod to be evicted, estimate disk freed:
- Read `container_fs_usage_bytes` from CloudWatch for each pod
- Estimate post-eviction node disk %:
  `(node total disk × node current %) − sum(evicted pod disk)`

### Step 8 — Build the approval request
Use `post_approval_request` with:
- Root cause summary (1–2 sentences)
- CloudWatch evidence (disk %, write rates, critical-service latency)
- Ranked eviction list with estimated impact per pod
- Statement that critical-tier services are protected (list them from the
  cluster skill)
- Approval contact tag from the cluster skill

### Step 9 — Choose the remediation tool: PREFER `kubectl_delete pod`

The recommended remediation is `kubectl_delete pod <name> -n <namespace>`
issued for each non-critical pod in priority order — NOT a node-wide drain.

Why pod delete over drain:
- Real clusters commonly contain bare pods (no controller), DaemonSets, and
  `emptyDir` volumes. `kubectl drain` / `node_management` fails on all three
  by default with errors like:
    - `cannot delete Pods that declare no controller`
    - `cannot delete Pods with local storage (emptyDir)`
    - `cannot delete DaemonSet-managed Pods`
  Forcing through these (`--force --delete-emptydir-data --ignore-daemonsets`)
  is risky and noisy.
- Pod deletion is targeted: you control exactly which workloads are
  sacrificed in priority order, critical services are simply never on the
  list, and the controller (Deployment / StatefulSet) reschedules pods
  automatically when capacity is available.
- Pod deletion succeeds reliably without special flags.

In the SAME turn as `post_approval_request`, queue ONE `kubectl_delete` per
target pod. The HITL gate will bundle them into a single approval; the
human clicks APPROVE once and all queued deletes execute.

Example tool calls to queue alongside the approval request (substitute
your actual pod names from Step 3 and the namespace from the cluster
skill):
```
kubectl_delete  resource_type=pod  name=<pod-1>  namespace=<namespace>
kubectl_delete  resource_type=pod  name=<pod-2>  namespace=<namespace>
kubectl_delete  resource_type=pod  name=<pod-3>  namespace=<namespace>
```

### Step 10 — If a delete fails, RE-PLAN (do not stand down)

After the human approves, if any `kubectl_delete` call returns an error
(`Tool error: ...` or a Kubernetes 4xx response), you MUST re-plan rather
than summarising and stopping:

- "pod not found" → the controller already rescheduled it; pick the next
  pod in the ranked list and propose again.
- "forbidden" / RBAC error → switch to a different pod or surface the RBAC
  issue to the human via `post_to_slack` with a fresh approval request.
- node still under disk pressure after the deletes → propose the next tier
  of eviction candidates from the cluster skill.

Each re-plan goes through the approval gate again: `post_to_slack` with the
new finding + `post_approval_request` + the new destructive tool calls,
all in one turn. Only call `mark_stand_down` once disk pressure has cleared
OR the user has explicitly denied further action.
