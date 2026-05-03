"""
Agent evaluation suite.

Two layers:
  - metrics.py  — deterministic rule-based checks extracted from LangGraph message history
  - judges.py   — LLM-as-judge evals for qualitative dimensions (root cause, format, compliance)
  - runner.py   — orchestrates both, posts results to Phoenix as trace annotations

Trigger:
  Online  — called automatically after each investigation in main.py
  Offline — python -m evals.runner --project k8s-agent --limit 20
"""
from evals.metrics import EvalResult

__all__ = ["EvalResult"]
