"""
Subagent definitions for parallel incident investigation.

Three subagents spawn simultaneously when an incident is detected:
  - cloudwatch_subagent: disk metrics and container insights
  - kubectl_subagent:    cluster state (nodes, pods, conditions)
  - otel_subagent:       app-level traces and latency (checkout/payment health)

The otel_subagent also checks the OTel collector emptyDir buffer — this is
the WRONG HYPOTHESIS the agent will pursue first before correcting itself.
"""

cloudwatch_subagent = {
    "name": "cloudwatch-investigator",
    "description": (
        "Investigates CloudWatch metrics, logs, and alarms. "
        "Use for disk usage metrics, container insights, "
        "application latency data, and CloudWatch Logs Insights queries."
    ),
    "system_prompt": (
        "You are a CloudWatch specialist. Query metrics and logs efficiently. "
        "Always include timestamps and units in findings. "
        "Return structured data the master agent can act on.\n\n"
        "For disk pressure incidents, query these in order:\n"
        "1. node_filesystem_utilization for all nodes in the cluster\n"
        "2. container_fs_usage_bytes grouped by pod_name\n"
        "3. CloudWatch Logs Insights on /aws/containerinsights/otel-demo-prod/performance "
        "   for disk write rates per pod in the last 15 minutes\n"
        "Report: top 5 disk consumers with write rates and total usage."
    ),
    "tools": [
        "cloudwatch_get_metric",
        "cloudwatch_logs_insights",
        "cloudwatch_describe_alarms",
        "cloudwatch_get_metric_data",
    ],
    "skills": [],
}

kubectl_subagent = {
    "name": "kubectl-investigator",
    "description": (
        "Investigates Kubernetes cluster state. Use for node conditions, "
        "pod status, resource usage, events, and priority classes."
    ),
    "system_prompt": (
        "You are a Kubernetes specialist. READ ONLY — never modify anything.\n\n"
        "For disk pressure incidents, check in this order:\n"
        "1. kubectl describe nodes — look for DiskPressure=True condition\n"
        "2. kubectl get pods -n otel-demo -o wide — identify which pods are on the affected node\n"
        "3. kubectl describe pod -n otel-demo <pod> for top disk consumers\n"
        "4. kubectl get events -n otel-demo --sort-by=.lastTimestamp\n"
        "5. kubectl top pods -n otel-demo — current CPU/memory\n\n"
        "Return: node name, DiskPressure status, list of pods on node with their "
        "priority classes, and any relevant events."
    ),
    "tools": [
        "kubectl_get",
        "kubectl_describe",
        "kubectl_logs",
        "kubectl_top",
        "kubectl_get_events",
    ],
    "skills": [
        "./skills/node-disk-pressure/",
        "./skills/pod-priority-eviction/",
    ],
}

otel_subagent = {
    "name": "otel-investigator",
    "description": (
        "Investigates application performance using OTel traces and metrics "
        "from CloudWatch. Use for service latency, error rates, and trace analysis. "
        "Also checks OTel collector emptyDir buffer usage."
    ),
    "system_prompt": (
        "You are an observability specialist. Focus on service health and user-facing impact.\n\n"
        "For disk pressure incidents, check:\n"
        "1. CloudWatch metric: checkout service p99 latency (last 15 min)\n"
        "2. CloudWatch metric: payment service error rate (last 15 min)\n"
        "3. OTel collector pod emptyDir volume usage (kubectl describe pod otelcol)\n"
        "   — check if emptyDir is near capacity (this is a common red herring)\n"
        "4. CloudWatch Logs Insights: trace data volume written by otel-collector\n\n"
        "IMPORTANT: Report OTel collector buffer usage explicitly, even if it looks normal.\n"
        "Report: checkout p99 latency, payment error rate, cart service health, "
        "otel-collector buffer fill %."
    ),
    "tools": [
        "cloudwatch_get_metric",
        "cloudwatch_logs_insights",
        "cloudwatch_get_traces",
    ],
    "skills": [
        "./skills/checkout-protection/",
    ],
}
