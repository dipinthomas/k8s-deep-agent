"""
Replayed incident for the lab: Cyber Monday checkout latency on retail-prod.

These tools stand in for the two MCP servers the real agent uses
(mcp-server-kubernetes and awslabs.cloudwatch-mcp-server). They keep the same
tool names and argument shapes, and return text recorded from the incident.
Mutating tools change the replay state, so the agent can verify recovery.

Ground truth for this incident:
  culprit  batch/image-resizer (Deployment, no CPU limit, priority background)
  victim   retail/checkoutservice (payment-critical)
  fix      kubectl_scale deployment image-resizer -n batch replicas=0
"""

from __future__ import annotations

import json
from typing import Optional


NODE = "ip-10-0-42-17.ap-southeast-2.compute.internal"
NODE_B = "ip-10-0-51-88.ap-southeast-2.compute.internal"

PODS = [
    # ns, name, node, cpu_m, mem_mi, cpu_limit, priority, owner_kind, owner
    ("retail", "checkoutservice-6c9f7d8b4-q2lmx", NODE, 410, 210, "500m", "payment-critical", "ReplicaSet", "checkoutservice-6c9f7d8b4"),
    ("retail", "paymentservice-5b7c9d6f8-k8w2p", NODE, 180, 160, "500m", "payment-critical", "ReplicaSet", "paymentservice-5b7c9d6f8"),
    ("retail", "cartservice-7f8d9c5b6-zt4rn", NODE_B, 95, 190, "500m", "payment-critical", "ReplicaSet", "cartservice-7f8d9c5b6"),
    ("retail", "frontend-84d6b7c9f-mm2vd", NODE_B, 120, 140, "1", "user-facing", "ReplicaSet", "frontend-84d6b7c9f"),
    ("retail", "productcatalogservice-6d8f9b7c5-p9xq4", NODE_B, 60, 90, "250m", "user-facing", "ReplicaSet", "productcatalogservice-6d8f9b7c5"),
    ("batch", "image-resizer-7d9f8c6b5-x2k4q", NODE, 3720, 610, None, "background", "ReplicaSet", "image-resizer-7d9f8c6b5"),
    ("batch", "report-exporter-28831140-7hk2d", NODE_B, 15, 40, "200m", "background", "Job", "report-exporter-28831140"),
    ("observability", "otel-collector-agent-5xk9t", NODE, 45, 120, "200m", "system-node-critical", "DaemonSet", "otel-collector-agent"),
    ("observability", "otel-collector-agent-r7mzq", NODE_B, 40, 115, "200m", "system-node-critical", "DaemonSet", "otel-collector-agent"),
]

DEPLOYMENTS = {
    ("retail", "checkoutservice"): 1, ("retail", "paymentservice"): 1, ("retail", "cartservice"): 1,
    ("retail", "frontend"): 1, ("retail", "productcatalogservice"): 1, ("batch", "image-resizer"): 1,
}


class Incident:
    """Mutable replay state plus a log of every tool call (the 'cluster audit log')."""

    def __init__(self, state: dict | None = None) -> None:
        self.deployments = dict(DEPLOYMENTS)
        self.deleted_pods: set[str] = set()
        self.audit: list[dict] = []
        if state:
            for k, v in state.get("deployments", {}).items():
                ns, d = k.split("/", 1)
                self.deployments[(ns, d)] = int(v)
            self.deleted_pods = set(state.get("deleted_pods", []))

    def to_state(self) -> dict:
        return {"deployments": {f"{ns}/{d}": r for (ns, d), r in self.deployments.items()},
                "deleted_pods": sorted(self.deleted_pods)}

    # ---- derived state -------------------------------------------------
    @property
    def fixed(self) -> bool:
        return self.deployments[("batch", "image-resizer")] == 0

    def pods(self):
        out = []
        for p in PODS:
            ns, name = p[0], p[1]
            dep = name.rsplit("-", 2)[0]
            if (ns, dep) in self.deployments and self.deployments[(ns, dep)] == 0:
                continue
            if name in self.deleted_pods and not (ns, dep) in self.deployments:
                continue
            out.append(p)
        return out

    def node_cpu_pct(self, node: str) -> int:
        used = sum(p[3] for p in self.pods() if p[2] == node)
        return min(99, round(used / 4000 * 100))

    def checkout_p99(self) -> int:
        return 236 if self.fixed else 1512

    # ---- tools ---------------------------------------------------------
    def _log(self, tool: str, args: dict) -> None:
        self.audit.append({"tool": tool, "args": {k: v for k, v in args.items() if v not in (None, "")}})

    def kubectl_get(self, resourceType: str, name: str = "", namespace: str = "", allNamespaces: bool = False,
                    output: str = "", labelSelector: str = "", fieldSelector: str = "") -> str:
        """Get or list Kubernetes resources (pods, nodes, deployments, events, priorityclasses).
        Equivalent to `kubectl get <resourceType> [name] -n <namespace> [-o output]`."""
        self._log("kubectl_get", {k: v for k, v in locals().items() if k != "self"})
        rt = resourceType.lower().rstrip("s")
        pods = self.pods()
        if rt in ("pod", "po"):
            if name:
                p = next((x for x in pods if x[1] == name), None)
                if not p:
                    return f'Error from server (NotFound): pods "{name}" not found'
                if output and "json" in output:
                    return json.dumps({"metadata": {"name": p[1], "namespace": p[0], "ownerReferences": [{"kind": p[7], "name": p[8]}]},
                                       "spec": {"nodeName": p[2], "priorityClassName": p[6],
                                                "containers": [{"name": p[1].split("-")[0], "resources": {"limits": ({"cpu": p[5]} if p[5] else {})}}]},
                                       "status": {"phase": "Running"}})
                return f"NAMESPACE  NAME  READY  STATUS  RESTARTS  AGE  NODE\n{p[0]}  {p[1]}  1/1  Running  0  {'11m' if 'image-resizer' in p[1] else '3d4h'}  {p[2]}"
            sel = [p for p in pods if (allNamespaces or not namespace or p[0] == namespace)]
            if fieldSelector and "nodeName=" in fieldSelector:
                n = fieldSelector.split("nodeName=")[1].split(",")[0]
                sel = [p for p in sel if p[2] == n]
            lines = ["NAMESPACE       NAME                                     READY  STATUS   RESTARTS  AGE   NODE                                            PRIORITY"]
            for p in sel:
                lines.append(f"{p[0]:<15} {p[1]:<40} 1/1    Running  0         {'11m ' if 'image-resizer' in p[1] else '3d4h'}  {p[2]:<47} {p[6]}")
            return "\n".join(lines)
        if rt in ("node", "no"):
            return ("NAME                                            STATUS  ROLES   AGE  VERSION   CPU-CAPACITY\n"
                    f"{NODE}  Ready   <none>  12d  v1.31.4   4\n{NODE_B}  Ready   <none>  12d  v1.31.4   4")
        if rt in ("deployment", "deploy"):
            lines = ["NAMESPACE  NAME                   READY  UP-TO-DATE  AVAILABLE"]
            for (ns, d), r in self.deployments.items():
                if namespace and not allNamespaces and ns != namespace:
                    continue
                if name and d != name:
                    continue
                lines.append(f"{ns:<10} {d:<22} {r}/{r}    {r}           {r}")
            return "\n".join(lines)
        if rt in ("event", "ev"):
            if self.fixed:
                return "LAST SEEN  TYPE    REASON            OBJECT                       MESSAGE\n40s  Normal  ScalingReplicaSet  deployment/image-resizer  Scaled down replica set image-resizer-7d9f8c6b5 to 0"
            return ("LAST SEEN  TYPE     REASON     OBJECT                                   MESSAGE\n"
                    "2m         Warning  Unhealthy  pod/checkoutservice-6c9f7d8b4-q2lmx      Readiness probe failed: context deadline exceeded (Client.Timeout exceeded while awaiting headers)\n"
                    "11m        Normal   Scheduled  pod/image-resizer-7d9f8c6b5-x2k4q        Successfully assigned batch/image-resizer-7d9f8c6b5-x2k4q to " + NODE + "\n"
                    "11m        Normal   Started    pod/image-resizer-7d9f8c6b5-x2k4q        Started container resizer")
        if rt in ("priorityclass", "pc"):
            return ("NAME                    VALUE       GLOBAL-DEFAULT\npayment-critical        1000000     false\n"
                    "user-facing             500000      false\nbackground              100000      false\nsystem-node-critical    2000001000  false")
        if rt in ("namespace", "ns"):
            return "NAME            STATUS\nbatch           Active\ndefault         Active\nkube-system     Active\nobservability   Active\nretail          Active"
        if rt in ("replicaset", "rs"):
            return "NAMESPACE  NAME                       DESIRED  CURRENT\nbatch      image-resizer-7d9f8c6b5    %d        %d\nretail     checkoutservice-6c9f7d8b4  1        1" % ((0, 0) if self.fixed else (1, 1))
        return f'error: the server doesn\'t have a resource type "{resourceType}"'

    def kubectl_describe(self, resourceType: str, name: str, namespace: str = "") -> str:
        """Describe a Kubernetes resource in detail (`kubectl describe <resourceType> <name>`)."""
        self._log("kubectl_describe", {k: v for k, v in locals().items() if k != "self"})
        rt = resourceType.lower().rstrip("s")
        if rt in ("node", "no"):
            n = name if name in (NODE, NODE_B) else NODE
            rows = "\n".join(f"  {p[0]:<14} {p[1]:<40} {p[5] or '0 (no limit)':<12} {p[6]}" for p in self.pods() if p[2] == n)
            return (f"Name: {n}\nConditions:\n  MemoryPressure False\n  DiskPressure   False\n  PIDPressure    False\n  Ready          True\n"
                    f"Allocatable: cpu 3920m, memory 15Gi\nNon-terminated Pods:\n  NAMESPACE      NAME                                     CPU-LIMIT    PRIORITY\n{rows}\n"
                    "Note: Kubernetes has no CPUPressure condition; check kubectl top.")
        p = next((x for x in self.pods() if x[1] == name or x[1].startswith(name + "-")), None)
        if rt in ("pod", "po") and p:
            return (f"Name: {p[1]}\nNamespace: {p[0]}\nNode: {p[2]}\nPriority Class Name: {p[6]}\nControlled By: {p[7]}/{p[8]}\n"
                    f"Limits: cpu {p[5] or '<none>'}\nStatus: Running"
                    + ("\nAnnotations: karpenter.sh/do-not-disrupt: true" if "image-resizer" in p[1] else ""))
        if rt in ("deployment", "deploy") and (namespace, name) in self.deployments:
            return f"Name: {name}\nNamespace: {namespace}\nReplicas: {self.deployments[(namespace, name)]} desired"
        return f'Error from server (NotFound): {resourceType} "{name}" not found'

    def kubectl_top(self, resource: str = "pods", name: str = "", namespace: str = "", allNamespaces: bool = True,
                    sortBy: str = "cpu") -> str:
        """Show CPU and memory usage for nodes or pods (`kubectl top nodes|pods`)."""
        self._log("kubectl_top", {k: v for k, v in locals().items() if k != "self"})
        if resource.lower().startswith("node"):
            return ("NAME                                            CPU(cores)  CPU%  MEMORY(bytes)  MEMORY%\n"
                    f"{NODE}  {sum(p[3] for p in self.pods() if p[2] == NODE)}m       {self.node_cpu_pct(NODE)}%   6120Mi         41%\n"
                    f"{NODE_B}  {sum(p[3] for p in self.pods() if p[2] == NODE_B)}m        {self.node_cpu_pct(NODE_B)}%    4980Mi         33%")
        pods = sorted(self.pods(), key=lambda p: -p[3])
        if namespace and not allNamespaces:
            pods = [p for p in pods if p[0] == namespace]
        return "NAMESPACE      NAME                                     CPU(cores)  MEMORY(bytes)\n" + "\n".join(
            f"{p[0]:<14} {p[1]:<40} {p[3]}m{'':<8} {p[4]}Mi" for p in pods)

    def kubectl_logs(self, name: str, namespace: str = "", tail: int = 50) -> str:
        """Get logs from a pod (`kubectl logs <pod> -n <namespace>`)."""
        self._log("kubectl_logs", {k: v for k, v in locals().items() if k != "self"})
        if "checkout" in name:
            return ("level=warn msg=\"payment call slow\" upstream=paymentservice latency_ms=1380\n"
                    "level=error msg=\"rpc error: code = DeadlineExceeded\" route=/hipstershop.CheckoutService/PlaceOrder status=504")
        if "image-resizer" in name:
            return "resizing batch 412/9800 thumbnails workers=16"
        return "no errors in last 50 lines"

    def kubectl_scale(self, name: str, replicas: int, namespace: str = "", resourceType: str = "deployment") -> str:
        """Scale a deployment or statefulset (`kubectl scale <resourceType>/<name> --replicas=N`). MUTATING."""
        self._log("kubectl_scale", {k: v for k, v in locals().items() if k != "self"})
        key = (namespace, name.split("/")[-1])
        if key not in self.deployments:
            return f'Error from server (NotFound): deployments.apps "{name}" not found in namespace "{namespace}"'
        self.deployments[key] = int(replicas)
        return f"deployment.apps/{key[1]} scaled"

    def kubectl_delete(self, resourceType: str, name: str, namespace: str = "") -> str:
        """Delete a Kubernetes resource (`kubectl delete <resourceType> <name>`). MUTATING."""
        self._log("kubectl_delete", {k: v for k, v in locals().items() if k != "self"})
        p = next((x for x in self.pods() if x[1] == name), None)
        if resourceType.lower().startswith("pod") and p:
            if p[7] == "ReplicaSet":
                return f'pod "{name}" deleted\n(ReplicaSet {p[8]} recreated a replacement pod on {p[2]} within 3s)'
            self.deleted_pods.add(name)
            return f'pod "{name}" deleted'
        return f'Error from server (NotFound): {resourceType} "{name}" not found'

    def kubectl_patch(self, resourceType: str, name: str, namespace: str = "", patchData: str = "") -> str:
        """Patch a Kubernetes resource with a JSON/merge patch. MUTATING."""
        self._log("kubectl_patch", {k: v for k, v in locals().items() if k != "self"})
        return f"{resourceType}/{name} patched (no change to running pods until restart)"

    def kubectl_context(self, operation: str = "get") -> str:
        """Get or list kubeconfig contexts. Not useful in-cluster."""
        self._log("kubectl_context", {k: v for k, v in locals().items() if k != "self"})
        return "currentContext: null"

    # ---- CloudWatch ----------------------------------------------------
    def get_active_alarms(self, max_items: int = 50) -> str:
        """List CloudWatch alarms currently in ALARM state."""
        self._log("get_active_alarms", {k: v for k, v in locals().items() if k != "self"})
        if self.fixed:
            return "[]"
        return json.dumps([{"AlarmName": "retail-prod-checkout-latency-p99", "StateValue": "ALARM",
                            "MetricName": "LatencyP99", "Namespace": "RetailProd/Services",
                            "Dimensions": [{"Name": "Service", "Value": "checkoutservice"}], "Threshold": 300,
                            "StateReason": "Threshold Crossed: 3 datapoints [1512.0, 1488.0, 1530.0] were greater than the threshold (300.0)."}])

    def get_metric_data(self, namespace: str, metric_name: str, dimensions: Optional[list] = None,
                        start_time: str = "", end_time: str = "", statistic: str = "Average", period: int = 60) -> str:
        """Fetch CloudWatch metric datapoints (namespace, metric_name, dimensions, time window)."""
        self._log("get_metric_data", {k: v for k, v in locals().items() if k != "self"})
        dims = json.dumps(dimensions or [])
        m = metric_name.lower()
        if "latency" in m and "payment" in dims:
            vals = [208, 211, 640, 702, 688] if not self.fixed else [205]
        elif "latency" in m:
            vals = [221, 219, 1497, 1512, 1530] if not self.fixed else [1490, 640, 236]
        elif "error" in m:
            vals = [0.2, 0.3, 4.6, 4.8, 5.1] if not self.fixed else [0.3]
        elif "cpu" in m:
            vals = [41, 43, 97, 98, 97] if not self.fixed else [38]
        else:
            return json.dumps({"MetricDataResults": [], "Messages": [f"No data for {metric_name} in {namespace}"]})
        ts = ["02:43", "02:45", "02:47", "02:49", "02:51"][-len(vals):] if not self.fixed else ["02:58", "02:59", "03:00"][-len(vals):]
        return json.dumps({"Label": metric_name, "Unit": "Milliseconds" if "latency" in m else "Percent",
                           "Datapoints": [{"Timestamp": f"2026-11-30T{t}:00Z", statistic: v} for t, v in zip(ts, vals)]})

    def execute_log_insights_query(self, log_group_names: list, query_string: str, start_time: str = "", end_time: str = "") -> str:
        """Run a CloudWatch Logs Insights query over the given log groups."""
        self._log("execute_log_insights_query", {k: v for k, v in locals().items() if k != "self"})
        return json.dumps({"status": "Complete", "results": [
            [{"field": "@message", "value": "checkoutservice PlaceOrder status=504 upstream=paymentservice DeadlineExceeded"}],
            [{"field": "count", "value": "418"}]]})

    def describe_log_groups(self, log_group_name_prefix: str = "") -> str:
        """List CloudWatch log groups."""
        self._log("describe_log_groups", {k: v for k, v in locals().items() if k != "self"})
        return json.dumps(["/aws/containerinsights/retail-prod/application", "/aws/containerinsights/retail-prod/performance"])

    TOOL_NAMES = ["kubectl_get", "kubectl_describe", "kubectl_top", "kubectl_logs", "kubectl_scale",
                  "kubectl_delete", "kubectl_patch", "kubectl_context", "get_active_alarms", "get_metric_data",
                  "execute_log_insights_query", "describe_log_groups"]

    def call(self, tool: str, args: dict) -> str:
        if tool not in self.TOOL_NAMES:
            return f"Unknown tool {tool}"
        try:
            return getattr(self, tool)(**args)
        except TypeError as e:
            return f"Tool error: bad arguments for {tool}: {e}"


ALARM = {
    "alarm_name": "retail-prod-checkout-latency-p99",
    "node": "(service-level alarm)",
    "reason": "Threshold Crossed: checkoutservice LatencyP99 1512 ms > 300 ms for 3 datapoints (RetailProd/Services)",
}
