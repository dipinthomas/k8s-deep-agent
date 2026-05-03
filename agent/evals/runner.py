"""
Eval runner — orchestrates rule-based and LLM-as-judge evals after each investigation,
then posts all results to Arize Phoenix as span annotations.

Two execution modes:

  Online  (called from main.py after each investigation):
    run_post_investigation_evals(thread_ts, channel, alarm, agent, slack_app)
    Runs rule-based evals immediately (microseconds), schedules LLM judges async.

  Offline (batch against historical Phoenix traces):
    python -m evals.runner [--project k8s-agent] [--limit 20] [--judges]
    Re-evaluates completed traces pulled from Phoenix and annotates them.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from typing import Any

from evals.metrics import EvalResult, run_all as run_metric_evals
from evals import judges as judge_module

logger = logging.getLogger(__name__)

_PHOENIX_BASE = os.environ.get(
    "PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:6006/v1/traces"
).replace("/v1/traces", "")

_PHOENIX_PROJECT = os.environ.get("PHOENIX_PROJECT_NAME", "k8s-agent")


# ── Phoenix annotation helpers ────────────────────────────────────────────────

def _post_eval_to_phoenix(
    eval_result: EvalResult,
    span_id: str | None,
    project_name: str,
) -> None:
    """Post a single EvalResult as a span annotation to Phoenix.
    No-ops silently if Phoenix is not available or span_id is unknown.
    """
    if not span_id:
        return

    try:
        import pandas as pd
        from phoenix.client import Client
        from phoenix.client.types import SpanEvaluations

        client = Client(base_url=_PHOENIX_BASE)
        df = pd.DataFrame({
            "span_id": [span_id],
            "label": [eval_result.label],
            "score": [eval_result.score],
            "explanation": [eval_result.explanation],
        })
        client.log_evaluations(
            SpanEvaluations(
                eval_name=eval_result.name,
                dataframe=df,
            ),
            project_name=project_name,
        )
        logger.debug("Posted eval %s (%s) to Phoenix span %s", eval_result.name, eval_result.label, span_id)
    except ImportError:
        logger.debug("phoenix.client not available — skipping Phoenix annotation")
    except Exception:
        logger.debug("Phoenix annotation failed for eval %s", eval_result.name, exc_info=True)


def _get_root_span_id(thread_ts: str, project_name: str) -> str | None:
    """Query Phoenix for the root span of the investigation identified by thread_ts.
    Returns the span_id string, or None if not found / Phoenix not available.
    """
    try:
        from phoenix.client import Client

        client = Client(base_url=_PHOENIX_BASE)
        # Phoenix supports simple filter expressions on span attributes.
        # LangChainInstrumentor sets thread_id from the LangGraph configurable.
        df = client.get_spans_dataframe(
            project_name=project_name,
            filter_condition=f'metadata["thread_id"] == "{thread_ts}"',
        )
        if df is None or df.empty:
            logger.debug("No Phoenix spans found for thread_ts=%s", thread_ts)
            return None
        # The root span has no parent (parent_id is null/empty)
        root = df[df.get("parent_id", "").isna() | (df.get("parent_id", "") == "")]
        if root.empty:
            root = df  # fall back to the first span if parent filtering fails
        return str(root.iloc[0].name)  # span_id is the DataFrame index
    except ImportError:
        return None
    except Exception:
        logger.debug("Failed to look up root span for thread_ts=%s", thread_ts, exc_info=True)
        return None


# ── Online mode: called from main.py ─────────────────────────────────────────

def run_post_investigation_evals(
    thread_ts: str,
    channel: str,
    alarm: dict,
    agent: Any,
    run_async_fn: Any,
) -> None:
    """Entry point called from main.py after each investigation completes.

    Runs the full rule-based suite synchronously (fast), then schedules the
    LLM-as-judge suite in the background so the investigation flow isn't blocked.

    Args:
        thread_ts:    Slack thread timestamp (used as LangGraph thread_id).
        channel:      Slack channel ID.
        alarm:        The original alarm dict from the /trigger payload.
        agent:        The compiled LangGraph agent (for aget_state).
        run_async_fn: The run_async() helper from main.py (submits coroutines to the persistent loop).
    """
    alarm_name = alarm.get("alarm_name", "")
    alarm_node = alarm.get("node", "")

    async def _fetch_and_eval():
        # 1. Read the final LangGraph state for this investigation
        from main import agent_config
        config = agent_config(thread_ts, channel)
        try:
            state = await agent.aget_state(config)
        except Exception:
            logger.exception("Evals: could not read agent state for thread_ts=%s", thread_ts)
            return

        values = getattr(state, "values", None) or {}
        messages = values.get("messages", []) if isinstance(values, dict) else []
        explicit_stand_down = values.get("explicit_stand_down", False) if isinstance(values, dict) else False

        # Determine if the investigation ended at a HITL interrupt
        tasks = getattr(state, "tasks", None) or ()
        interrupts = []
        for t in tasks:
            interrupts.extend(getattr(t, "interrupts", ()) or ())
        was_interrupted = bool(interrupts)

        if not messages:
            logger.warning("Evals: no messages in state for thread_ts=%s — skipping", thread_ts)
            return

        # 2. Rule-based evals (fast, no LLM)
        metric_results = run_metric_evals(messages, alarm_node=alarm_node, was_interrupted=was_interrupted)
        _log_results("RULE-BASED", metric_results)

        # 3. Resolve Phoenix span for annotation
        span_id = _get_root_span_id(thread_ts, _PHOENIX_PROJECT)

        # 4. Post rule-based results to Phoenix
        for result in metric_results:
            _post_eval_to_phoenix(result, span_id, _PHOENIX_PROJECT)

        # 5. LLM-as-judge evals (async, more expensive)
        skill_content = _load_skill_content()
        judge_results = await judge_module.run_all(
            messages,
            alarm_name=alarm_name,
            alarm_node=alarm_node,
            skill_content=skill_content,
        )
        _log_results("LLM-JUDGE", judge_results)

        for result in judge_results:
            _post_eval_to_phoenix(result, span_id, _PHOENIX_PROJECT)

        # 6. Summary log line
        all_results = metric_results + judge_results
        passed = sum(1 for r in all_results if r.label == "pass")
        warned = sum(1 for r in all_results if r.label == "warn")
        failed = sum(1 for r in all_results if r.label == "fail")
        logger.info(
            "EVALS[%s] %d pass / %d warn / %d fail  (thread_ts=%s%s)",
            alarm_name, passed, warned, failed, thread_ts,
            f"  ← Phoenix span {span_id}" if span_id else "",
        )

    try:
        run_async_fn(_fetch_and_eval())
    except Exception:
        logger.exception("Evals: failed to schedule eval coroutine for thread_ts=%s", thread_ts)


def _log_results(label: str, results: list[EvalResult]) -> None:
    for r in results:
        fn = logger.info if r.label == "pass" else (logger.warning if r.label == "warn" else logger.error)
        fn("EVAL[%s][%s] %s", label, r.label.upper(), r)


def _load_skill_content() -> str:
    skill_path = os.environ.get("CLUSTER_SKILL_PATH", "")
    if not skill_path:
        return ""
    try:
        with open(skill_path) as f:
            return f.read()
    except Exception:
        logger.debug("Could not read CLUSTER_SKILL_PATH=%s for evals", skill_path)
        return ""


# ── Offline mode: batch eval against Phoenix traces ───────────────────────────

async def _offline_eval_trace(
    thread_ts: str,
    project_name: str,
    agent: Any,
    run_judges: bool,
) -> list[EvalResult]:
    """Pull a single investigation's state from the agent checkpointer and
    re-run all evals. Used for batch offline evaluation.
    """
    try:
        from main import agent_config, bootstrap, slack_app, CHANNEL
    except Exception as e:
        logger.error("Cannot import main — run from the agent/ directory: %s", e)
        return []

    config = agent_config(thread_ts, CHANNEL)
    try:
        state = await agent.aget_state(config)
    except Exception:
        logger.exception("Could not read state for thread_ts=%s", thread_ts)
        return []

    values = getattr(state, "values", None) or {}
    messages = values.get("messages", []) if isinstance(values, dict) else []
    if not messages:
        return []

    tasks = getattr(state, "tasks", None) or ()
    interrupts = [i for t in tasks for i in (getattr(t, "interrupts", ()) or ())]
    was_interrupted = bool(interrupts)

    results = run_metric_evals(messages, was_interrupted=was_interrupted)
    if run_judges:
        skill_content = _load_skill_content()
        results += await judge_module.run_all(messages, skill_content=skill_content)

    span_id = _get_root_span_id(thread_ts, project_name)
    for r in results:
        _post_eval_to_phoenix(r, span_id, project_name)

    return results


async def _offline_main(project_name: str, limit: int, run_judges: bool) -> None:
    """Batch evaluate the most recent `limit` investigations from Phoenix."""
    try:
        from phoenix.client import Client
    except ImportError:
        print("ERROR: arize-phoenix-otel not installed. Run: pip install arize-phoenix-otel", file=sys.stderr)
        sys.exit(1)

    client = Client(base_url=_PHOENIX_BASE)
    try:
        spans_df = client.get_spans_dataframe(project_name=project_name)
    except Exception as e:
        print(f"ERROR: Could not connect to Phoenix at {_PHOENIX_BASE}: {e}", file=sys.stderr)
        sys.exit(1)

    if spans_df is None or spans_df.empty:
        print(f"No spans found in project '{project_name}'.")
        return

    # Extract unique thread_ids from span metadata (set by LangGraph configurable)
    thread_col = None
    for col in ("metadata.thread_id", "attributes.metadata.thread_id", "metadata"):
        if col in spans_df.columns:
            thread_col = col
            break

    if thread_col is None:
        print("Cannot find thread_id in span metadata. Check that PHOENIX_ENABLED=true and traces are being collected.")
        return

    thread_ids = spans_df[thread_col].dropna().unique().tolist()
    thread_ids = thread_ids[:limit]
    print(f"Evaluating {len(thread_ids)} investigation(s) from project '{project_name}'...")

    # Bootstrap agent (needed to read checkpointer state)
    from main import bootstrap, agent, run_async
    # agent is already built if bootstrap() was called
    all_pass = all_warn = all_fail = 0
    for thread_ts in thread_ids:
        results = await _offline_eval_trace(thread_ts, project_name, agent, run_judges)
        p = sum(1 for r in results if r.label == "pass")
        w = sum(1 for r in results if r.label == "warn")
        f = sum(1 for r in results if r.label == "fail")
        all_pass += p
        all_warn += w
        all_fail += f
        print(f"  {thread_ts}: {p}✓ {w}⚠ {f}✗")

    print(f"\nTotal — pass: {all_pass}  warn: {all_warn}  fail: {all_fail}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Batch eval runner for the k8s agent")
    parser.add_argument("--project", default=_PHOENIX_PROJECT, help="Phoenix project name")
    parser.add_argument("--limit", type=int, default=10, help="Max number of investigations to evaluate")
    parser.add_argument("--judges", action="store_true", help="Also run LLM-as-judge evals (slower, costs tokens)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s — %(message)s")
    asyncio.run(_offline_main(args.project, args.limit, args.judges))
