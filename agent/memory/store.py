"""
Long-term memory store for the K8s agent.
Persists incident history and known failure patterns across runs.
Uses Redis when REDIS_URL is set; falls back to InMemoryStore for local dev.
"""

import logging
import os

from langgraph.store.memory import InMemoryStore

logger = logging.getLogger(__name__)

NS_INCIDENTS = ("incidents",)

REDIS_URL = os.environ.get("REDIS_URL", "")


async def build_memory_store_async():
    """
    Build a Redis-backed long-term memory store when REDIS_URL is set.
    Falls back to InMemoryStore if Redis is unavailable or langgraph's
    redis store extra is not installed.

    MUST be awaited from within the persistent agent event loop so the
    store's HTTP/Redis connections are bound to that loop.
    """
    if not REDIS_URL:
        logger.info("REDIS_URL not set — using InMemoryStore for long-term memory")
        return InMemoryStore()
    try:
        from langgraph.store.redis.aio import AsyncRedisStore  # type: ignore
    except ImportError:
        logger.warning(
            "langgraph[redis] store extra not installed — falling back to "
            "InMemoryStore. Install with: pip install langgraph[redis]"
        )
        return InMemoryStore()
    try:
        store = AsyncRedisStore(redis_url=REDIS_URL)
        await store.__aenter__()  # type: ignore[attr-defined]
        await store.setup()
        await store.aset_client_info()
        logger.info("Using AsyncRedisStore at %s for long-term memory", REDIS_URL)
        return store
    except Exception as e:
        logger.exception(
            "AsyncRedisStore initialisation failed (%s) — falling back to "
            "InMemoryStore. Cross-incident learnings will not persist.", e,
        )
        return InMemoryStore()


async def retrieve_past_incidents(store, alarm_name: str, limit: int = 3) -> str | None:
    """
    Search the incident store for past investigations matching this alarm.
    Returns a formatted string ready to inject into the agent's initial message,
    or None if no relevant history exists.

    Called before each new investigation so the agent starts with prior context.
    """
    if store is None:
        return None
    try:
        items = await store.alist(NS_INCIDENTS)
        if not items:
            return None

        # Filter to incidents that match this alarm_name, then take the most recent.
        matching = [
            i for i in items
            if isinstance(i.value, dict) and i.value.get("alarm_name") == alarm_name
        ]
        # Fall back to all incidents if none match by alarm_name (e.g. records
        # written before alarm_name was stored — backwards compatible).
        pool = matching if matching else list(items)

        recent = sorted(
            pool,
            key=lambda i: i.value.get("timestamp", "") if isinstance(i.value, dict) else "",
            reverse=True,
        )[:limit]

        lines = [
            f"LONG-TERM MEMORY — {len(recent)} past incident(s) for alarm "
            f"`{alarm_name}` retrieved from the store:\n"
        ]
        for item in recent:
            v = item.value
            ts = v.get("timestamp", "unknown time")
            rc = v.get("root_cause", "unknown")
            pods = ", ".join(v.get("evicted_pods", [])) or "none"
            metrics = v.get("metrics", {})
            dim = metrics.get("dimension", "")
            before = metrics.get("before", "")
            after = metrics.get("after", "")
            cs = v.get("critical_service", {})
            cs_line = ""
            if cs and cs.get("name"):
                cs_line = (
                    f" | {cs['name']} {cs.get('metric','')}: "
                    f"{cs.get('before','')} → {cs.get('after','')}"
                )
            lines.append(
                f"• [{ts}] Root cause: {rc} | "
                f"Remediation: {pods} | "
                f"{dim}: {before} → {after}{cs_line}"
            )
        lines.append(
            "\nUse this history to inform your hypothesis — do not skip "
            "investigation steps, but let prior patterns guide where you look first."
        )
        return "\n".join(lines)
    except Exception:
        logger.exception("retrieve_past_incidents failed — continuing without memory context")
        return None


def format_incident_record(
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
    timestamp: str = "",
) -> dict:
    """Build a structured incident record for writing to the memory store."""
    record: dict = {
        "alarm_name": alarm_name,
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
