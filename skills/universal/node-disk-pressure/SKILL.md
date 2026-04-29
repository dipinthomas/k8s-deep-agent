---
name: node-disk-pressure
description: Use this skill when a Kubernetes node shows disk pressure,
             high disk usage, DiskPressure condition, or when pods are
             being evicted due to disk resource constraints.
---

## Node Disk Pressure Investigation Playbook

### Step 1 — Confirm the condition
```
kubectl describe node <node-name> | grep -A5 Conditions
```
Look for: `DiskPressure = True`

### Step 2 — Check imageprovider FIRST (known culprit)
```
kubectl logs -n otel-demo deployment/imageprovider --tail=100
```
Check CloudWatch: `/aws/containerinsights/otel-demo-prod/performance`  
Filter: `pod_name = imageprovider`, metric: `container_fs_usage_bytes`

If imageprovider disk writes are abnormally high (>20MB/min), it is almost certainly the culprit.
Stop here and skip to Step 6.

### Step 3 — Find top disk consumers on the node
```
kubectl get pods -n otel-demo -o wide | grep <node-name>
```
For each pod: check CloudWatch `container_fs_usage_bytes` grouped by `PodName`

Run this CloudWatch Logs Insights query on `/aws/containerinsights/otel-demo-prod/performance`:
```
fields @timestamp, pod_name, container_fs_usage_bytes
| filter ClusterName = "otel-demo-prod"
| stats max(container_fs_usage_bytes) as max_bytes by pod_name
| sort max_bytes desc
| limit 10
```

### Step 4 — Check OTel collector buffer
```
kubectl describe pod -n otel-demo -l app=otelcol
```
Check emptyDir volume mounts and current usage.
**Note:** The OTel collector emptyDir buffer is a common red herring. It is usually within
normal limits. Always verify imageprovider first.

### Step 5 — Correlate with app symptoms
Check CloudWatch for checkout service latency in the past 15 minutes:
- Namespace: `OTelDemo`
- Metric: `checkoutservice.latency` (p99)
- If checkout p99 is above 500ms: disk pressure is already affecting payments → escalate urgency

### Step 6 — Identify eviction candidates
Cross-reference the pod list on the affected node with priority classes from AGENTS.md.
Build a ranked eviction list from lowest to highest priority:
1. `loadgenerator` (background) — synthetic traffic, zero user impact
2. `imageprovider` (infrastructure) — product images unavailable but checkout unaffected
3. `adservice` (background) — ads stop showing
4. `recommendationservice` (background) — recommendations unavailable
5. `frontend` (user-facing) — browsing unavailable — only if still needed

**NEVER include:** checkoutservice, paymentservice, cartservice, productcatalogservice

### Step 7 — Calculate estimated recovery
For each pod to be evicted, estimate disk freed:
- Check `container_fs_usage_bytes` from CloudWatch for each pod
- Calculate: (node total disk) × (node current %) - sum(evicted pod disk) = estimated post-eviction %

### Step 8 — Build the approval request
Use `post_approval_request` tool with:
- Root cause summary (1-2 sentences)
- CloudWatch evidence (disk %, write rates, checkout latency)
- Ranked eviction list with estimated impact per pod
- Statement that payment services are protected
- @dipin tag for approval

### Step 9 — Choose the remediation tool: PREFER `kubectl_delete pod`

For disk pressure on this cluster, the recommended remediation is
`kubectl_delete pod <name> -n <namespace>` issued for each non-critical pod
in priority order — NOT a node-wide drain.

Why pod delete over drain:
- The demo cluster contains bare pods (no controller), DaemonSets, and
  emptyDir volumes. `kubectl drain` / `node_management` fails on all three
  by default with errors like:
    - `cannot delete Pods that declare no controller`
    - `cannot delete Pods with local storage (emptyDir)`
    - `cannot delete DaemonSet-managed Pods`
  Forcing through these (`--force --delete-emptydir-data --ignore-daemonsets`)
  is risky and noisy.
- Pod deletion is targeted: you control exactly which workloads are sacrificed
  in priority order, payment services are simply never on the list, and the
  controller (Deployment / StatefulSet) reschedules them automatically when
  capacity is available.
- Pod deletion succeeds reliably without special flags.

In the SAME turn as `post_approval_request`, queue ONE `kubectl_delete` per
target pod. The HITL gate will bundle them into a single approval; the human
clicks APPROVE once and all queued deletes execute.

Example tool calls to queue alongside the approval request (substitute your
actual pod names from Step 3):
```
kubectl_delete  resource_type=pod name=loadgenerator-...    namespace=otel-demo
kubectl_delete  resource_type=pod name=imageprovider-...    namespace=otel-demo
kubectl_delete  resource_type=pod name=adservice-...        namespace=otel-demo
```

### Step 10 — If a delete fails, RE-PLAN (do not stand down)

After the human approves, if any `kubectl_delete` call returns an error
(`Tool error: ...` or a Kubernetes 4xx response), you MUST re-plan rather
than summarising and stopping:

- "pod not found" → the controller already rescheduled it; pick the next
  pod in the ranked list and propose again.
- "forbidden" / RBAC error → switch to a different pod or surface the RBAC
  issue to the human via post_to_slack with a fresh approval request.
- node still under disk pressure after the deletes → propose the next tier
  of eviction candidates (e.g. add `recommendationservice` to the list).

Each re-plan goes through the approval gate again: `post_to_slack` with the
new finding + `post_approval_request` + the new destructive tool calls, all
in one turn. Only call `mark_stand_down` once disk pressure has cleared OR
the user has explicitly denied further action.
