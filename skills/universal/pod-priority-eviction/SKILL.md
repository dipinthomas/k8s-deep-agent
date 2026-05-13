---
name: pod-priority-eviction
description: Use this skill when evaluating which pods to evict from a node,
             determining eviction order by priority class, or calculating the
             impact of pod evictions on cluster services.
---

## Pod Priority Eviction Playbook

This playbook is cluster-agnostic. The actual priority class names, values,
and the services that belong to each tier come from the cluster skill
loaded for this deployment. This file describes the mechanics; the cluster
skill supplies the data.

### How to read a cluster's tiering

Every cluster skill defines a tiered model along these lines:

| Tier            | Typical priority value | Eviction policy |
|-----------------|------------------------|-----------------|
| Critical        | highest (e.g. 1000000) | NEVER evict without explicit human approval |
| Infrastructure  | high (e.g. 900000)     | Evict only after exhausting lower tiers |
| User-facing     | medium (e.g. 500000)   | Evict only when pressure persists after background tier |
| Background      | low (e.g. 100000)      | Safe to evict first; usually no real-user impact |

Read the cluster skill before recommending any eviction. It will tell you
which services belong in which tier and which tier-internal order to
follow.

### Eviction order (lowest priority first)

Always work in this order, stopping as soon as resource pressure is
resolved:

1. **Background tier** — synthetic traffic, batch jobs, async services.
   Usually zero real-user impact.
2. **Infrastructure tier** — support services (image servers, telemetry
   buffers). Evicting affects the support function, not user requests.
3. **User-facing tier** — browsing, recommendations, ancillary UI features.
   Eviction degrades UX but does not break critical flows.
4. **STOP** — never proceed to the critical tier without explicit human
   approval. Re-plan and propose a different remediation
   (scale, restart, node replacement) through the approval gate.

The cluster skill may override this order with cluster-specific knowledge
(e.g. "imageprovider is the most common disk culprit — evict it before the
rest of infrastructure tier"). Follow the cluster skill where it specifies.

### Calculating eviction impact

For each candidate pod, estimate:
- **Resource freed** — CPU / memory / disk, depending on the pressure
  dimension. Read current usage from `kubectl top` or CloudWatch.
- **User impact** — described in the cluster skill's per-service notes
  (e.g. "ads disappear", "browsing degraded", "payments unaffected").
- **Reschedule risk** — will the controller put the pod back on the same
  node? If the cluster has no headroom, eviction may not actually relieve
  pressure.

### How to evict a pod

After receiving human approval, prefer:
```
kubectl_delete pod <full-pod-name> -n <namespace>
```

The deployment controller will reschedule the pod. If the same node is
the only viable node, the pod may land back there — watch for this and
notify in the Slack thread.

Avoid `node_management` drain except for genuine node-level failures
(hardware faults, planned node replacement). Drain commonly fails on
clusters with bare pods, DaemonSets, or `emptyDir` volumes — see
`node-disk-pressure/SKILL.md`.

### After eviction — verify recovery

1. Check node conditions:
   ```
   kubectl describe node <node-name> | grep -A5 Conditions
   ```
2. Check the relevant resource metric in CloudWatch — should start
   dropping within 60 seconds of eviction.
3. Check critical-service health (latency, error rate) — should return to
   the cluster skill's healthy threshold within 2–3 minutes.
4. Post a resolution summary to Slack with before/after metrics.

### Write to memory

After resolution, write the outcome to long-term memory using
`format_incident_record()` from `agent/memory/store.py`. Include:
- Root cause identified
- Which pods were evicted and their tiers
- Before/after metrics for the pressure dimension
- Before/after metrics for any critical service that was impacted
- Any follow-up actions recommended

This builds institutional knowledge across incidents on this cluster.
