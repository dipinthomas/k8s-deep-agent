---
name: checkout-protection
description: Use this skill when assessing the impact of any action on the
             checkout, payment, or cart services. Invoke before recommending
             any evictions, restarts, or node operations.
---

## Checkout Protection Playbook

### The Rule
**Checkoutservice, paymentservice, cartservice, and productcatalogservice
must NEVER be evicted, restarted, or disrupted without explicit written approval
from a human operator.**

These services handle real payment processing. Any disruption means orders cannot
be placed and revenue is lost.

### Assessing Checkout Health

Before taking ANY action, check checkout health with these CloudWatch queries:

**1. Checkout latency (p99)**
- Namespace: `OTelDemo`
- Metric: `checkoutservice.latency` (or `latency` with dimension `service=checkoutservice`)
- Healthy: < 150ms p99
- Degraded: 150ms – 500ms p99
- Critical: > 500ms p99 → users experiencing timeouts

**2. Checkout error rate**
- Namespace: `OTelDemo`
- Metric: `checkoutservice.errors`
- Healthy: < 0.1%
- Alert: > 1%

**3. Payment service health**
```
kubectl get pod -n otel-demo -l app=paymentservice
```
All payment pods must be Running with Ready=True.

### Impact Assessment Matrix

| Action | Checkout Impact | Payment Impact | Proceed Without Approval? |
|--------|----------------|----------------|--------------------------|
| Evict loadgenerator | None | None | After approval only |
| Evict imageprovider | None | None | After approval only |
| Evict adservice | None | None | After approval only |
| Evict recommendationservice | None | None | After approval only |
| Evict frontend | Browsing unavailable | None | After approval only |
| Evict checkoutservice | PAYMENT OUTAGE | PAYMENT OUTAGE | NEVER |
| Evict paymentservice | PAYMENT OUTAGE | PAYMENT OUTAGE | NEVER |
| Evict cartservice | CART LOST | PAYMENT OUTAGE | NEVER |
| Drain node (with checkout) | PAYMENT OUTAGE | PAYMENT OUTAGE | NEVER |

### What to Say in the Approval Request

Always include this block in the Slack approval request:
```
✅ Protected services (NOT in eviction list):
• checkoutservice — payments continue to work
• paymentservice — payment processing unaffected
• cartservice — cart data preserved
• productcatalogservice — product data available
```

### If Checkout Is Already Degraded

If checkout p99 > 500ms, escalate urgency in the Slack message:
```
⚠️ URGENCY: Checkout service is already degraded (p99: Xms).
Disk pressure is actively affecting payment processing.
Immediate action required.
```

This does NOT change the rule — approval is still required — but ensures
the human understands the business impact before deciding.

### After Resolution

Verify checkout recovery:
- p99 latency returns to < 150ms within 3 minutes of eviction
- Error rate returns to < 0.1%
- All payment pods remain Running

If checkout does not recover within 5 minutes of eviction, alert immediately:
```
⚠️ Checkout has not recovered. Manual investigation required.
Current p99: Xms. Escalating.
```
