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
