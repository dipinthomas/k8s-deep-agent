---
name: critical-service-protection
description: Use this skill when assessing the impact of any action on the
             cluster's critical-tier services. Invoke before recommending
             any evictions, restarts, scales, or node operations.
---

## Critical Service Protection Playbook

This playbook is cluster-agnostic. The list of critical services, their
healthy thresholds, and the metric names to query come from the cluster
skill loaded for this deployment.

### The Rule

**Critical-tier services (as defined in the cluster skill) must NEVER be
evicted, restarted, scaled, or otherwise disrupted without explicit
written approval from a human operator.**

These are the services the cluster owner has identified as load-bearing
for end users — payment, authentication, primary data plane, whatever the
business cannot afford to lose. Disruption means real customer-facing
impact.

If you are unsure whether a service is critical: read the cluster skill.
If still unsure: treat it as critical and ask.

### Assessing Critical-Service Health

Before taking ANY action, check the health of every service the cluster
skill flags as critical. The cluster skill should specify, per service:

- The metric namespace and name to query (e.g. CloudWatch namespace +
  metric name + dimension)
- Healthy / degraded / critical thresholds (e.g. p99 latency < 150ms is
  healthy, > 500ms is critical)
- The expected error-rate ceiling

Run these queries against CloudWatch (or whichever observability backend
the cluster skill specifies) for the past 5–15 minutes.

Also confirm pod-level health for each critical service:
```
kubectl get pod -n <namespace> -l <selector>
```
All pods of a critical service must be `Running` with `Ready=True`. Any
pod not Ready is itself a degradation event — investigate before
proceeding.

### Impact Assessment Matrix

For every proposed action, classify its impact on critical services:

| Action class                          | Critical-service impact | Proceed without approval? |
|---------------------------------------|-------------------------|---------------------------|
| Evict a background-tier pod           | None expected           | After approval only       |
| Evict an infrastructure-tier pod      | None expected           | After approval only       |
| Evict a user-facing-tier pod          | None expected           | After approval only       |
| Evict a critical-tier pod             | DIRECT OUTAGE           | NEVER                     |
| Drain a node hosting a critical pod   | DIRECT OUTAGE           | NEVER                     |
| Scale a critical service to zero      | DIRECT OUTAGE           | NEVER                     |
| Restart a critical-service deployment | Brief degradation       | NEVER without approval    |

Even when the proposed action does not directly touch a critical-tier
service, you must verify critical-service health is currently healthy
before acting — because a coincident degradation may turn a routine
remediation into a customer-facing incident.

### What to Include in the Approval Request

Always include a "Protected" block in the Slack approval request listing
every critical-tier service by name, confirming they are NOT in the
proposed action list. Pull the list from the cluster skill so it stays
in sync.

Example block (substitute the cluster's actual critical services):
```
✅ Protected services (NOT in eviction list):
• <critical-service-1> — <one-line description of why it matters>
• <critical-service-2> — ...
• <critical-service-3> — ...
```

### If a Critical Service Is Already Degraded

If any critical service is already above its degraded threshold when the
incident begins, escalate urgency in the Slack message:

```
⚠️ URGENCY: <service-name> is already degraded (p99: <X>ms,
   threshold: <Y>ms). Resource pressure is actively affecting users.
   Immediate action required.
```

This does NOT change the rule — approval is still required — but ensures
the human understands the business impact before deciding.

### After Resolution

Verify critical-service recovery:
- Latency returns within healthy threshold (cluster skill specifies the
  number) within 2–5 minutes of remediation
- Error rate returns within healthy ceiling
- All critical-service pods remain `Running` and `Ready`

If any critical service has not recovered within 5 minutes, do NOT call
`mark_stand_down`. Post an escalation message and re-plan:

```
⚠️ <service-name> has not recovered after remediation.
   Current p99: <X>ms (threshold: <Y>ms).
   Re-investigating — manual intervention may be required.
```

Then re-run the investigation pipeline (subagents) to find the next
contributing factor and propose a follow-up action through the approval
gate.
