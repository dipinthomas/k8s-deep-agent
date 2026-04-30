"""
Long-term memory store for the K8s agent.
Persists incident history and known failure patterns across runs.
Uses Redis when REDIS_URL is set; falls back to InMemoryStore for local dev.
"""

import os
import logging
from langgraph.store.memory import InMemoryStore

logger = logging.getLogger(__name__)

# Namespace keys used for storing agent memories
NS_INCIDENTS = ("incidents",)     # Past incidents and resolutions
NS_PATTERNS = ("patterns",)       # Known failure patterns (root causes)
NS_NODES = ("nodes",)             # Node-specific notes

REDIS_URL = os.environ.get("REDIS_URL", "")


def build_memory_store():
    """
    Build the memory store. Uses Redis if REDIS_URL is set, otherwise InMemoryStore.
    Seeding is done separately via seed_memory_store() at agent startup.
    """
    logger.info("Using InMemoryStore (Redis Stack required for persistent store)")
    return InMemoryStore()


def seed_memory_store(store) -> None:
    """
    Pre-seed the memory store with cluster-specific known patterns.

    The agent itself is application-agnostic — known failure patterns for a
    given deployment belong in that deployment's cluster skill
    (skills/clusters/<cluster-name>/SKILL.md), not hardcoded here. This
    function is intentionally a no-op so the same agent binary can run
    against any cluster without carrying baked-in assumptions.

    If you need to pre-load patterns at startup for a specific deployment,
    extend this function in your fork or set them via the cluster skill.
    """
    return None


def format_incident_record(
    node: str,
    root_cause: str,
    evicted_pods: list[str],
    pressure_dimension: str,
    before_value: float,
    after_value: float,
    critical_service: str | None = None,
    critical_metric: str | None = None,
    critical_before: float | None = None,
    critical_after: float | None = None,
    timestamp: str = "",
) -> dict:
    """
    Build a structured incident record for writing to the memory store.
    Call this after a successful resolution.

    Args:
        node: Node name where pressure was observed.
        root_cause: One-line root cause summary.
        evicted_pods: List of pod names that were evicted as part of the
            remediation.
        pressure_dimension: Which dimension was under pressure — e.g.
            "disk_pct", "cpu_pct", "memory_pct".
        before_value: Value of the pressure dimension before remediation.
        after_value: Value of the pressure dimension after remediation.
        critical_service: Name of the critical service whose health was
            tracked through the incident, if any.
        critical_metric: Metric name used to track that service (e.g.
            "p99_latency_ms").
        critical_before: Value of the critical-service metric before
            remediation.
        critical_after: Value of the critical-service metric after
            remediation.
        timestamp: ISO-8601 timestamp of resolution.
    """
    record: dict = {
        "node": node,
        "root_cause": root_cause,
        "evicted_pods": evicted_pods,
        "metrics": {
            "dimension": pressure_dimension,
            "before": before_value,
            "after": after_value,
        },
        "timestamp": timestamp,
    }
    if critical_service:
        record["critical_service"] = {
            "name": critical_service,
            "metric": critical_metric,
            "before": critical_before,
            "after": critical_after,
        }
    return record
