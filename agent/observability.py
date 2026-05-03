"""
Arize Phoenix observability — optional, zero-overhead when disabled.

Phoenix auto-instruments LangChain/LangGraph via OpenInference. Every model
call, tool invocation, and subagent run appears as a nested span in the Phoenix
trace tree, grouped by investigation thread via LangGraph's thread_id.

Enable by setting PHOENIX_ENABLED=true. Point at a self-hosted Phoenix server
via PHOENIX_COLLECTOR_ENDPOINT (defaults to localhost for local dev).

Call setup_phoenix() immediately after load_dotenv() and before any agent
execution — the instrumentation hooks into LangChain's callback system, which
fires at runtime, so it works even though LangGraph is already imported.
"""
import logging
import os

logger = logging.getLogger(__name__)


def setup_phoenix() -> bool:
    """Register Phoenix tracing. Returns True when active, False when disabled or failed."""
    enabled = os.environ.get("PHOENIX_ENABLED", "").lower() in ("true", "1", "yes")
    endpoint = os.environ.get("PHOENIX_COLLECTOR_ENDPOINT", "")

    if not enabled and not endpoint:
        return False

    collector_endpoint = endpoint or "http://localhost:6006/v1/traces"
    project_name = os.environ.get("PHOENIX_PROJECT_NAME", "k8s-agent")

    try:
        from phoenix.otel import register
        register(
            project_name=project_name,
            endpoint=collector_endpoint,
            auto_instrument=True,
            batch=True,
        )
        logger.info(
            "Phoenix tracing active → %s  (project: %s)",
            collector_endpoint,
            project_name,
        )
        return True
    except ImportError:
        logger.warning(
            "PHOENIX_ENABLED=true but arize-phoenix-otel is not installed. "
            "Run: pip install arize-phoenix-otel openinference-instrumentation-langchain"
        )
        return False
    except Exception:
        logger.exception("Phoenix setup failed — continuing without tracing")
        return False
