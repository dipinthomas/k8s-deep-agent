import datetime
from langchain_core.tools import tool
from langgraph.config import get_store
from memory.store import NS_INCIDENTS, format_incident_record


@tool
async def save_incident_to_memory(
    alarm_name: str,
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
) -> str:
    """Persist the resolved incident to long-term memory so future
    investigations on this cluster can recall the root cause and
    remediation. Call this BEFORE mark_stand_down on every successful
    resolution.

    Args:
        alarm_name: The CloudWatch alarm name that fired (e.g. "checkoutservice-p99-latency-high").
        node: Node name where the pressure was observed (or "(service-level alarm)").
        root_cause: One-line root cause summary.
        evicted_pods: List of pod names evicted/scaled during remediation.
        pressure_dimension: e.g. disk_pct, cpu_pct, memory_pct, latency_ms.
        before_value: Metric value before remediation.
        after_value: Metric value after remediation.
        critical_service: Name of critical service monitored, if any.
        critical_metric: Metric used to track critical service health.
        critical_before: Critical service metric before remediation.
        critical_after: Critical service metric after remediation.
    """
    store = get_store()
    if store is None:
        return "Memory store unavailable — incident not saved."

    timestamp = datetime.datetime.utcnow().isoformat()
    record = format_incident_record(
        alarm_name=alarm_name,
        node=node,
        root_cause=root_cause,
        evicted_pods=evicted_pods,
        pressure_dimension=pressure_dimension,
        before_value=before_value,
        after_value=after_value,
        critical_service=critical_service,
        critical_metric=critical_metric,
        critical_before=critical_before,
        critical_after=critical_after,
        timestamp=timestamp,
    )
    key = f"{alarm_name}_{datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%S')}"
    await store.aput(NS_INCIDENTS, key, record)
    return f"Incident saved to long-term memory — key: {key}"
