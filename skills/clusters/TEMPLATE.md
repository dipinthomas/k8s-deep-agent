# Cluster Skill Template

Copy this file to `skills/clusters/<your-cluster-name>/SKILL.md` and fill in
the sections below. Set `CLUSTER_SKILL_PATH=./skills/clusters/<your-cluster-name>/SKILL.md`
in your agent deployment.

The universal skills (`skills/universal/`) describe investigation *patterns* that apply
to any cluster. This file supplies the *specifics* — service names, namespaces,
priority classes, thresholds — that the agent needs to act correctly on your cluster.

---

```yaml
---
name: <cluster-name>
description: Load this skill when operating against <cluster-name>.
---
```

# Cluster Context: <cluster-name>

## Cluster Facts

- **Cloud:** AWS | GCP | Azure
- **Region:** e.g. us-east-1
- **Platform:** EKS | GKE | AKS | self-managed
- **Observability:** e.g. CloudWatch Container Insights, Prometheus, Datadog
- **Workload namespace(s):** e.g. `production`, `staging`
- **AWS Profile (if applicable):** e.g. `my-aws-profile`
- **Node type:** e.g. m5.large (2 vCPU, 8 GB RAM)
- **Disk pressure threshold:** e.g. 85%

## OUT-OF-SCOPE WORKLOADS — NEVER TARGET THESE

| Namespace       | Workload        | Reason                  |
|-----------------|-----------------|-------------------------|
| `kube-system`   | any             | Kubernetes control plane|
| `<agent-ns>`    | agent pods      | Investigation infra     |
| ...             | ...             | ...                     |

---

## Application: <Your Application Name>

Brief description of the application.

### Service Tiers and Priority Classes

#### Critical (Priority Class: `<critical-class>`, value XXXXXXX)
**Never evict, restart, or disrupt without explicit human approval.**

| Service | Role |
|---------|------|
| ...     | ...  |

Healthy thresholds:
- Service A p99 < Xms
- Service B error rate < X%

#### Infrastructure (Priority Class: `<infra-class>`, value XXXXXXX)

| Service | Role | Known Characteristics |
|---------|------|-----------------------|
| ...     | ...  | ...                   |

#### User-Facing (Priority Class: `<user-facing-class>`, value XXXXXXX)

| Service | Role |
|---------|------|
| ...     | ...  |

#### Background (Priority Class: `<background-class>`, value XXXXXXX)
Safe to evict under resource pressure.

| Service | Role |
|---------|------|
| ...     | ...  |

---

## Eviction Order (lowest impact → highest)

1. `<background-service-1>` — reason it's safe
2. `<background-service-2>` — reason it's safe
3. `<user-facing-service>` — impact if evicted
4. **STOP** — never proceed beyond this point without explicit human approval

---

## Known Incident Patterns

| Incident Type       | Typical Signal                       | Universal Skill to Load    |
|---------------------|--------------------------------------|----------------------------|
| Node disk pressure  | DiskPressure=True, pods evicting     | `node-disk-pressure`       |
| Noisy neighbour     | One pod hogging CPU, others degraded | `noisy-neighbor`           |
| Critical degradation| p99 > threshold, errors rising       | `critical-service-protection` |
| Eviction ranking    | Multiple pods competing for resources| `pod-priority-eviction`    |

## Cluster Characteristics (pattern-matching hints)

- List any known behaviours that help the agent pattern-match faster.
- e.g. "Service X is high disk I/O — check it first under disk pressure"
- e.g. "HPA is enabled for service Y — scaling events are expected"

---

## Healthy Thresholds

| Metric                    | Normal   | Degraded  | Critical  |
|---------------------------|----------|-----------|-----------|
| <service> p99 latency     | < Xms    | X–Yms     | > Yms     |
| <service> error rate      | < X%     | X–Y%      | > Y%      |
| Node CPU utilisation      | < X%     | X–Y%      | > Y%      |
| Node disk utilisation     | < X%     | X–Y%      | > Y%      |

---

## Slack

- **Channel:** `#<your-alert-channel>`
- Post all updates and approval requests as thread replies to the original alert.

## Slack Message Templates

### post_to_slack — investigation findings

```
:rotating_light: *{alarm_name} | {metric} {value}* — threshold breached

*Root cause* — {root_cause_summary}
*Why it's safe to act* — {priority_class_rationale}
*Impact* — {user_impact}
*Fix* — {proposed_action}

━━━━━━━━━━━━━━━━━━━━━
:mag: *Investigation details*

• *CloudWatch:* {cloudwatch_findings}
• *Node:* {node_findings}
• *Priority class:* {priority_class_findings}
• *Confidence:* {confidence_statement}
```

### post_approval_request

- `summary`: one-line root cause + proposed action
- `evidence`: `See investigation details above ↑`
- `action_list`: exact kubectl command
- `impact`: expected outcome after remediation
