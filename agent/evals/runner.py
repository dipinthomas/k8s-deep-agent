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
    """Post a single EvalResult as a span annotation to Phoenix via REST.

    Uses httpx directly — the phoenix.client Python package is not required.
    No-ops silently if Phoenix is unreachable or span_id is unknown.
    """
    if not span_id:
        return
    try:
        import httpx
        resp = httpx.post(
            f"{_PHOENIX_BASE}/v1/span_annotations",
            json={"data": [{
                "span_id": span_id,
                "name": eval_result.name,
                "annotator_kind": "CODE",
                "result": {
                    "label": eval_result.label,
                    "score": float(eval_result.score),
                    "explanation": eval_result.explanation,
                },
            }]},
            timeout=10,
        )
        resp.raise_for_status()
        logger.debug("Posted eval %s (%s) to Phoenix span %s", eval_result.name, eval_result.label, span_id)
    except Exception:
        logger.debug("Phoenix annotation failed for eval %s", eval_result.name, exc_info=True)


def _get_root_span_id(thread_ts: str, project_name: str, retries: int = 4, retry_delay: float = 3.0) -> str | None:
    """Query Phoenix for the root span of the investigation identified by thread_ts.

    Uses the REST API directly because the Python client filter syntax changed
    across Phoenix versions. The LangChainInstrumentor stores thread_ts in
    session.id (top-level) and metadata.thread_id (attribute) — we match both.

    Retries with a delay because the OTel exporter is async — the current
    investigation's spans may not be flushed to Phoenix at the moment evals run.
    Without retries, the function can find an older span from a previous
    investigation that shared the same thread_ts (e.g. after an agent restart).
    """
    import time

    def _find_root(spans: list) -> str | None:
        # Find root spans (no parent) whose session.id or metadata.thread_id matches thread_ts.
        # Phoenix returns spans newest-first; the first matching root span is the current one.
        for span in spans:
            attrs = span.get("attributes", {})
            session_id = attrs.get("session.id", "") or ""
            meta_thread = attrs.get("metadata.thread_id", "") or ""
            if thread_ts not in (session_id, meta_thread):
                continue
            if span.get("parent_id") is None:
                return span["context"]["span_id"]
        return None

    try:
        import httpx
        url = f"{_PHOENIX_BASE}/v1/projects/{project_name}/spans"

        for attempt in range(retries):
            if attempt > 0:
                time.sleep(retry_delay)
            try:
                resp = httpx.get(url, params={"limit": 200}, timeout=10)
                resp.raise_for_status()
                spans = resp.json().get("data", [])
            except Exception:
                logger.debug("Phoenix span query failed (attempt %d)", attempt + 1, exc_info=True)
                continue

            span_id = _find_root(spans)
            if span_id:
                return span_id
            logger.debug("No Phoenix root span for thread_ts=%s (attempt %d/%d)", thread_ts, attempt + 1, retries)

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

    Reads agent state safely on _agent_loop (via run_async_fn), skips if the
    investigation is still at HITL, then runs slow evals (Phoenix queries, LLM
    judges) in a daemon thread with its own asyncio loop so _agent_loop stays
    free for approval resumes.

    Args:
        thread_ts:    Slack thread timestamp (used as LangGraph thread_id).
        channel:      Slack channel ID.
        alarm:        The original alarm dict from the /trigger payload.
        agent:        The compiled LangGraph agent (for aget_state).
        run_async_fn: main.py's run_async() — submits coroutines to _agent_loop.
    """
    import threading

    alarm_name = alarm.get("alarm_name", "")
    alarm_node = alarm.get("node", "")

    # Read the final LangGraph state on _agent_loop (safe — uses the correct loop
    # for the shared async Redis client). Must NOT be called from any other loop.
    from main import agent_config
    config = agent_config(thread_ts, channel)

    async def _get_state():
        return await agent.aget_state(config)

    try:
        state = run_async_fn(_get_state()).result(timeout=30)
    except Exception:
        logger.exception("Evals: could not read agent state for thread_ts=%s", thread_ts)
        return

    values = getattr(state, "values", None) or {}
    messages = values.get("messages", []) if isinstance(values, dict) else []

    tasks = getattr(state, "tasks", None) or ()
    interrupts = []
    for t in tasks:
        interrupts.extend(getattr(t, "interrupts", ()) or ())
    was_interrupted = bool(interrupts)

    if was_interrupted:
        # Investigation paused at HITL — incomplete, no evals to run yet.
        logger.info("Evals: investigation paused at HITL for thread_ts=%s — deferring", thread_ts)
        return

    if not messages:
        logger.warning("Evals: no messages in state for thread_ts=%s — skipping", thread_ts)
        return

    # Run slow evals (Phoenix queries + LLM judges) in a dedicated thread so
    # _agent_loop stays free. The thread has its own asyncio loop and does NOT
    # touch the shared async Redis client.
    def _run_slow_evals():
        async def _slow():
            # Rule-based evals (fast, no LLM)
            metric_results = run_metric_evals(messages, alarm_node=alarm_node, was_interrupted=was_interrupted)
            _log_results("RULE-BASED", metric_results)

            span_id = _get_root_span_id(thread_ts, _PHOENIX_PROJECT)

            for result in metric_results:
                _post_eval_to_phoenix(result, span_id, _PHOENIX_PROJECT)

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
            asyncio.run(_slow())
        except Exception:
            logger.exception("Evals: slow eval thread failed for thread_ts=%s", thread_ts)

    try:
        threading.Thread(target=_run_slow_evals, daemon=True, name=f"evals-{thread_ts}").start()
    except Exception:
        logger.exception("Evals: failed to start eval thread for thread_ts=%s", thread_ts)


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
