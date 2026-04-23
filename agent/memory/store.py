"""
Long-term memory store for the K8s agent.
Persists incident history and known failure patterns across runs.
Uses LangGraph InMemoryStore for the demo (swap for a persistent store in production).
"""

from langgraph.store.memory import InMemoryStore

# Namespace keys used for storing agent memories
NS_INCIDENTS = ("incidents",)     # Past incidents and resolutions
NS_PATTERNS = ("patterns",)       # Known failure patterns (root causes)
NS_NODES = ("nodes",)             # Node-specific notes


def build_memory_store() -> InMemoryStore:
    """
    Build and pre-seed the memory store with known cluster patterns.
    The agent reads from this store when investigating new incidents,
    and writes to it after each resolution.
    """
    store = InMemoryStore()

    # Pre-seed known failure patterns so the agent has institutional memory
    store.put(
        NS_PATTERNS,
        "imageprovider-nginx-verbose-logging",
        {
            "pattern": "imageprovider nginx verbose logging disk pressure",
            "description": (
                "imageprovider runs nginx. When NGINX_LOG_LEVEL=debug (or access logging is "
                "enabled with high traffic), it writes large volumes of logs to ephemeral storage. "
                "Under load generator traffic spikes, this can fill node disk in minutes."
            ),
            "root_cause": "NGINX_LOG_LEVEL=debug + high traffic → excessive ephemeral writes",
            "fix": "Evict imageprovider pod; set NGINX_LOG_LEVEL=warn in deployment env",
            "check_first": True,
            "services_affected": ["imageprovider"],
        },
    )

    store.put(
        NS_PATTERNS,
        "otelcol-emptydir-buffer",
        {
            "pattern": "OTel collector emptyDir buffer accumulation",
            "description": (
                "The OTel collector writes trace buffers to an emptyDir volume. "
                "Under sustained high trace volume, this can grow but rarely causes "
                "disk pressure on its own. Check imageprovider first before blaming the collector."
            ),
            "root_cause": "High trace throughput → emptyDir growth (usually < 1GB)",
            "fix": "Reduce trace sampling rate; increase OTel collector memory limit",
            "check_first": False,
            "services_affected": ["otelcol"],
        },
    )

    store.put(
        NS_PATTERNS,
        "loadgenerator-traffic-spike",
        {
            "pattern": "Load generator traffic spike amplifying disk writes",
            "description": (
                "LOCUST_USERS > 100 drives high request volume that amplifies nginx access log "
                "write rates. loadgenerator itself uses minimal disk. It is always safe to stop."
            ),
            "root_cause": "High LOCUST_USERS → more HTTP requests → more nginx access log lines",
            "fix": "Evict or scale down loadgenerator to reduce traffic",
            "check_first": False,
            "services_affected": ["loadgenerator", "imageprovider"],
        },
    )

    return store


def format_incident_record(
    node: str,
    root_cause: str,
    evicted_pods: list[str],
    before_disk_pct: float,
    after_disk_pct: float,
    before_checkout_p99_ms: float,
    after_checkout_p99_ms: float,
    timestamp: str,
) -> dict:
    """
    Build a structured incident record for writing to the memory store.
    Call this after a successful resolution.
    """
    return {
        "node": node,
        "root_cause": root_cause,
        "evicted_pods": evicted_pods,
        "metrics": {
            "disk_before_pct": before_disk_pct,
            "disk_after_pct": after_disk_pct,
            "checkout_p99_before_ms": before_checkout_p99_ms,
            "checkout_p99_after_ms": after_checkout_p99_ms,
        },
        "timestamp": timestamp,
    }
