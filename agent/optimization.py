"""
Token-cost optimization middleware and helpers.

Two concerns live here, both aimed at cutting input-token spend without
touching the agent graph, interrupt machinery, or KeepLoopingMiddleware:

1. TokenUsageLoggingMiddleware — wraps every model call and logs LangChain's
   normalized usage_metadata (input / output / cache_read / cache_creation)
   so we can see cache hit rates and verify other optimisations actually
   save tokens. Works for any provider LangChain supports; cache_read is
   only populated by providers with prefix caching.

2. ToolOutputTruncator — truncates oversized MCP tool outputs at the wrapper
   layer. Raw kubectl output (`get pods -A -o wide`) and CloudWatch
   responses can be 10–50 KB; the LLM rarely needs more than the first few
   KB. We keep head + tail with a marker in the middle so the model still
   sees structure but stops paying for replays of full dumps every turn.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelCallResult,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import AIMessage

logger = logging.getLogger(__name__)


# ── 1. Token-usage logging ────────────────────────────────────────────────────

class TokenUsageLoggingMiddleware(AgentMiddleware):
    """Log every model call's token usage at INFO so cache hit rate and
    per-turn input-token spend are visible.

    LangChain normalises usage_metadata across providers:
      - input_tokens         (excluding cached prefix reads)
      - output_tokens
      - input_token_details.cache_read       (read from the prefix cache)
      - input_token_details.cache_creation   (newly written to the cache)

    Not every provider populates cache_read in usage_metadata — OpenAI
    (we use /responses via use_responses_api=True) doesn't surface it,
    so cache_read stays at 0 there and only input/output counts are
    meaningful. Anthropic does populate it, in which case a healthy
    steady-state investigation shows input_tokens shrink while
    cache_read grows; cache_read=0 across turns means the prefix is
    being invalidated (often: prompt or tools list changed shape).
    """

    name = "TokenUsageLoggingMiddleware"

    def __init__(self, label: str = "agent") -> None:
        super().__init__()
        self.label = label

    def _log(self, response: ModelResponse) -> None:
        try:
            messages = getattr(response, "result", None) or []
            for msg in messages:
                if not isinstance(msg, AIMessage):
                    continue
                usage = getattr(msg, "usage_metadata", None) or {}
                if not usage:
                    continue
                input_t = usage.get("input_tokens", 0) or 0
                output_t = usage.get("output_tokens", 0) or 0
                details = usage.get("input_token_details") or {}
                cache_read = details.get("cache_read") or 0
                cache_creation = details.get("cache_creation") or 0
                # cache hit ratio relative to the full prompt the model saw
                full_input = input_t + cache_read + cache_creation
                hit_pct = (cache_read / full_input * 100) if full_input else 0.0
                logger.info(
                    "TOKENS[%s] in=%d out=%d cache_read=%d cache_create=%d "
                    "full_in=%d cache_hit=%.0f%%",
                    self.label, input_t, output_t, cache_read, cache_creation,
                    full_input, hit_pct,
                )
        except Exception:
            # Logging must never break the model loop.
            logger.debug("TokenUsageLoggingMiddleware logging failed", exc_info=True)

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        response = handler(request)
        self._log(response)
        return response

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        response = await handler(request)
        self._log(response)
        return response


# ── 2. Tool-output truncation ─────────────────────────────────────────────────

# Default budget per tool result in characters (~tokens × 4). Keeping
# head + tail of a payload preserves structural cues (column headers at the
# top, totals at the bottom) which is usually all the model needs.
_DEFAULT_TOOL_OUTPUT_CHAR_LIMIT = int(
    os.environ.get("TOOL_OUTPUT_CHAR_LIMIT", "8000")
)
_TRUNCATION_HEAD_FRAC = 0.6
_TRUNCATION_MARKER_TMPL = (
    "\n\n…[truncated {dropped} characters of {total} — "
    "ask for narrower output if you need the middle]…\n\n"
)


def truncate_tool_output(
    text: str, char_limit: int = _DEFAULT_TOOL_OUTPUT_CHAR_LIMIT
) -> str:
    """Truncate `text` to roughly `char_limit` characters by keeping a head
    and tail slice with a marker between them. Returns text unchanged if
    already under the limit."""
    if not isinstance(text, str):
        return text
    if len(text) <= char_limit:
        return text
    head_len = int(char_limit * _TRUNCATION_HEAD_FRAC)
    tail_len = max(0, char_limit - head_len - 200)  # leave room for marker
    head = text[:head_len]
    tail = text[-tail_len:] if tail_len > 0 else ""
    marker = _TRUNCATION_MARKER_TMPL.format(
        dropped=len(text) - head_len - tail_len, total=len(text)
    )
    return f"{head}{marker}{tail}"

