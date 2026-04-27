---
name: pod-priority-eviction
description: Use this skill when evaluating which pods to evict from a node,
             determining eviction order by priority class, or calculating the
             impact of pod evictions on cluster services.
---

## Pod Priority Eviction Playbook

### Priority Classes in This Cluster

| Priority Class    | Value   | Services |
|-------------------|---------|----------|
| payment-critical  | 1000000 | checkoutservice, paymentservice, cartservice |
| infrastructure    | 900000  | imageprovider, otel-collector |
| user-facing       | 500000  | frontend, productcatalogservice, shippingservice, currencyservice |
| background        | 100000  | loadgenerator, adservice, recommendationservice, emailservice, frauddetectionservice |

### Eviction Order (lowest priority first)

Always evict in this order, stopping as soon as disk pressure is resolved:

1. **loadgenerator** — synthetic traffic simulator, always safe to stop
   - Impact: traffic simulation stops; no real users affected
   - Disk freed: minimal (the traffic it generates causes imageprovider to write)

2. **adservice** — ads disappear from frontend
   - Impact: no ads shown; no revenue impact for this demo cluster
   - Disk freed: low

3. **recommendationservice** — recommendations disappear
   - Impact: "You might also like..." section empty
   - Disk freed: low

4. **imageprovider** — product images stop loading
   - Impact: browsing experience degraded; checkout still works
   - Disk freed: HIGH (this is usually the root cause, so eviction frees the most)

5. **emailservice** / **frauddetectionservice** / **quoteservice** — background jobs
   - Impact: emails delayed; fraud checks bypass (temporary); shipping quotes unavailable
   - Only evict if pressure still not resolved after steps 1-4

6. **frontend** — entire UI goes down
   - Impact: browsing impossible; checkout still available via API
   - Only evict as last resort before touching payment-critical tier

### NEVER Evict (payment-critical)
- checkoutservice
- paymentservice
- cartservice
- productcatalogservice

### How to Evict a Pod

After receiving human approval, use `kubectl_evict_pod`:
```python
kubectl_evict_pod(pod_name="<full-pod-name>", namespace="otel-demo")
```

The deployment controller will reschedule the pod. If the same node is the
only viable node, the pod may land back there — watch for this and notify.

### After Eviction — Verify Recovery
1. Check node disk usage: `kubectl describe node <node-name> | grep -A5 Conditions`
2. Check CloudWatch `node_filesystem_utilization` — should start dropping within 60 seconds
3. Check checkout latency: should return to <150ms p99 within 2-3 minutes
4. Post resolution summary to Slack with before/after metrics

### Write to Memory
After resolution, call `store.put` to record:
- Root cause identified
- Which pods were evicted
- Before/after disk % and checkout latency
- Any follow-up actions recommended
