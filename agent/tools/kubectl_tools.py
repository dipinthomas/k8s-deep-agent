"""
kubectl tools for the K8s agent.
Read-only tools are always available. Write tools (evict, drain, delete)
are gated behind LangGraph interrupt() — they require human approval.
"""

import subprocess
import json
import os
from langchain_core.tools import tool


def _run(args: list[str], check: bool = True) -> str:
    """Run a kubectl command and return stdout."""
    kubeconfig = os.environ.get("KUBECONFIG", os.path.expanduser("~/.kube/config"))
    cmd = ["kubectl", "--kubeconfig", kubeconfig] + args
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0 and check:
            return f"ERROR (exit {result.returncode}):\n{result.stderr}"
        return result.stdout or result.stderr
    except subprocess.TimeoutExpired:
        return "ERROR: kubectl command timed out after 30s"
    except FileNotFoundError:
        return "ERROR: kubectl not found in PATH"


# ---------------------------------------------------------------------------
# READ-ONLY tools
# ---------------------------------------------------------------------------

@tool
def kubectl_get(resource: str, namespace: str = "", extra_args: str = "") -> str:
    """
    Run 'kubectl get <resource>' and return output.

    Args:
        resource:   Resource type and optional name (e.g. 'pods', 'nodes', 'pod/imageprovider-xxx')
        namespace:  Kubernetes namespace. Empty string means cluster-scoped (nodes, etc.)
        extra_args: Additional flags as a space-separated string (e.g. '-o wide')
    """
    args = ["get", resource]
    if namespace:
        args += ["-n", namespace]
    if extra_args:
        args += extra_args.split()
    return _run(args)


@tool
def kubectl_describe(resource: str, name: str, namespace: str = "") -> str:
    """
    Run 'kubectl describe <resource> <name>' and return output.

    Args:
        resource:  Resource type (e.g. 'node', 'pod', 'deployment')
        name:      Resource name
        namespace: Namespace (empty for cluster-scoped resources like nodes)
    """
    args = ["describe", resource, name]
    if namespace:
        args += ["-n", namespace]
    return _run(args)


@tool
def kubectl_logs(
    pod_name: str,
    namespace: str = "otel-demo",
    container: str = "",
    tail: int = 100,
) -> str:
    """
    Fetch logs from a pod.

    Args:
        pod_name:   Pod name or deployment name (e.g. 'deployment/imageprovider')
        namespace:  Namespace (default: otel-demo)
        container:  Container name (optional, for multi-container pods)
        tail:       Number of lines from end of log (default 100)
    """
    args = ["logs", pod_name, "-n", namespace, f"--tail={tail}"]
    if container:
        args += ["-c", container]
    return _run(args)


@tool
def kubectl_top(resource: str = "pods", namespace: str = "otel-demo") -> str:
    """
    Run 'kubectl top pods|nodes' to get current resource usage.

    Args:
        resource:  'pods' or 'nodes'
        namespace: Namespace for pod metrics (default: otel-demo)
    """
    args = ["top", resource]
    if resource == "pods":
        args += ["-n", namespace]
    return _run(args)


@tool
def kubectl_get_events(namespace: str = "otel-demo", minutes_back: int = 15) -> str:
    """
    Get recent Kubernetes events sorted by timestamp.

    Args:
        namespace:    Namespace to check (default: otel-demo)
        minutes_back: Only show events from the last N minutes (default 15)
    """
    args = ["get", "events", "-n", namespace, "--sort-by=.lastTimestamp"]
    output = _run(args)
    return output


# ---------------------------------------------------------------------------
# WRITE tools — these trigger LangGraph interrupt() for human approval
# ---------------------------------------------------------------------------

@tool
def kubectl_evict_pod(pod_name: str, namespace: str = "otel-demo") -> str:
    """
    Evict a pod from the cluster. REQUIRES HUMAN APPROVAL before execution.

    Args:
        pod_name:  Pod name to evict
        namespace: Namespace (default: otel-demo)
    """
    args = ["delete", "pod", pod_name, "-n", namespace, "--grace-period=30"]
    output = _run(args)
    return f"Evicted {namespace}/{pod_name}:\n{output}"


@tool
def kubectl_drain_node(node_name: str, ignore_daemonsets: bool = True) -> str:
    """
    Drain all pods from a node. REQUIRES HUMAN APPROVAL before execution.
    This is a destructive action — use evict_pod instead where possible.

    Args:
        node_name:          Node to drain
        ignore_daemonsets:  Skip DaemonSet pods (default True)
    """
    args = ["drain", node_name, "--delete-emptydir-data"]
    if ignore_daemonsets:
        args.append("--ignore-daemonsets")
    output = _run(args)
    return f"Drained node {node_name}:\n{output}"


@tool
def kubectl_delete(resource: str, name: str, namespace: str = "otel-demo") -> str:
    """
    Delete a Kubernetes resource. REQUIRES HUMAN APPROVAL before execution.
    Do NOT use this to evict pods — use kubectl_evict_pod instead.

    Args:
        resource:  Resource type (e.g. 'deployment', 'pvc')
        name:      Resource name
        namespace: Namespace (default: otel-demo)
    """
    args = ["delete", resource, name, "-n", namespace]
    output = _run(args)
    return f"Deleted {namespace}/{resource}/{name}:\n{output}"
