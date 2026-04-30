"""
Middleware that forces the agent to keep looping until it has either:
  1. Posted findings + queued a destructive tool (which the HITL gate then
     pauses on), or
  2. Posted an explicit stand-down message via post_to_slack, or
  3. Called the mark_stand_down tool to signal it is genuinely done.

Without this, gpt-5-mini regularly returns a final AIMessage with empty
tool_calls after a tool error (drain failed, evict failed) or after posting
a summary it considers "the answer". LangGraph then exits and the user is
left with a half-finished investigation — exactly what Deep Agents'
plan/act/observe loop is supposed to prevent.

The middleware sits between deepagents' base stack and the tail stack
(AnthropicPromptCachingMiddleware, MemoryMiddleware, HumanInTheLoopMiddleware,
in that order). This means our after_model hook runs AFTER the model emits
the response but BEFORE HITL inspects it for interrupts — so if the model
correctly queued a destructive tool, HITL still gets to arm its gate.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, AgentState
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.runtime import Runtime
from typing_extensions import NotRequired, TypedDict

logger = logging.getLogger(__name__)

# Marker the model emits via post_to_slack to declare it is intentionally
# ending the investigation without a destructive action. We match it
# loosely (case-insensitive substring) so the model has flexibility in
# wording.
STAND_DOWN_PHRASES = (
    "no action required",
    "standing down",
    "stand down",
    "denied",
    "no further action",
    "investigation complete",
)


class KeepLoopingState(TypedDict):
    """Extra state fields added by KeepLoopingMiddleware. Persisted across
    turns via the checkpointer so they survive interrupts.

    explicit_stand_down: set when the agent has declared the investigation
        is over (mark_stand_down called or stand-down phrase posted).
    unarmed_gate_retry_count: number of consecutive turns where
        post_approval_request was called without queuing a destructive tool.
        Used to escalate the corrective message after repeated failures so
        the agent does not spam Slack indefinitely.
    """

    explicit_stand_down: NotRequired[bool]
    unarmed_gate_retry_count: NotRequired[int]


# After this many consecutive turns of "post_approval_request without a
# destructive tool", we escalate to demanding mark_stand_down so the loop
# terminates rather than spinning forever.
_MAX_UNARMED_GATE_RETRIES = 2


class KeepLoopingMiddleware(AgentMiddleware[AgentState[Any], Any, Any]):
    """Force the agent to keep iterating until it explicitly stands down.

    Triggers a corrective HumanMessage when:
      - The model returned an AIMessage with no tool_calls AND
        explicit_stand_down is not set AND
        the message text doesn't contain a stand-down phrase.
      - The model called post_approval_request without queuing any
        destructive tool in the same turn (interrupt would have nothing
        to pause on).
    """

    state_schema = KeepLoopingState  # type: ignore[assignment]

    def __init__(self, destructive_tool_names: set[str]) -> None:
        super().__init__()
        self.destructive_tool_names = set(destructive_tool_names)

    # ── Hook ────────────────────────────────────────────────────────────────
    def after_model(self, state, runtime: Runtime[Any]) -> dict[str, Any] | None:  # type: ignore[override]
        return self._after_model_logic(state)

    async def aafter_model(self, state, runtime: Runtime[Any]) -> dict[str, Any] | None:  # type: ignore[override]
        return self._after_model_logic(state)

    # ── Core logic ──────────────────────────────────────────────────────────
    def _after_model_logic(self, state) -> dict[str, Any] | None:
        messages = state.get("messages") or []
        if not messages:
            return None

        last = messages[-1]
        if not isinstance(last, AIMessage):
            return None

        # If the agent has already declared a stand-down, never inject more.
        if state.get("explicit_stand_down"):
            return None

        tool_calls = list(getattr(last, "tool_calls", None) or [])
        tool_names = [self._tc_name(tc) for tc in tool_calls]

        # If mark_stand_down was just called, persist the flag and let the
        # graph exit naturally on the next turn.
        if "mark_stand_down" in tool_names:
            logger.info(
                "KeepLooping: mark_stand_down called — recording explicit_stand_down=True"
            )
            return {"explicit_stand_down": True}

        # Case 1 — empty tool_calls AND no stand-down text in the message.
        if not tool_calls:
            text = self._extract_text(last)
            if self._looks_like_stand_down(text):
                logger.info(
                    "KeepLooping: AIMessage with empty tool_calls but text "
                    "contains a stand-down phrase — recording explicit_stand_down=True"
                )
                return {"explicit_stand_down": True}

            logger.warning(
                "KeepLooping: AIMessage ended turn with empty tool_calls and no "
                "stand-down phrase — injecting corrective HumanMessage"
            )
            return {
                "messages": [
                    HumanMessage(
                        content=(
                            "You ended your turn without calling any tool, posting a "
                            "stand-down summary, or calling mark_stand_down. Pick one "
                            "of these THREE options now:\n"
                            "  (a) Continue the investigation — call the next tool "
                            "(e.g. another kubectl read, or post_approval_request + "
                            "the destructive tool in the SAME turn).\n"
                            "  (b) Stand down — call post_to_slack with a message "
                            "containing 'no action required' or 'standing down', "
                            "explaining why you cannot or should not proceed.\n"
                            "  (c) Genuinely done — call mark_stand_down with a brief "
                            "reason. Use this only when the incident is fully resolved "
                            "or no further action is possible.\n"
                            "Do NOT end the turn again with empty tool_calls."
                        )
                    )
                ]
            }

        # Case 2 — post_approval_request was called but no destructive tool
        # was queued in the same turn. The HITL gate has nothing to pause on,
        # so the buttons would be no-ops. Inject corrective guidance.
        if "post_approval_request" in tool_names:
            queued_destructive = [n for n in tool_names if n in self.destructive_tool_names]
            if not queued_destructive:
                # Allow the case where the agent already recently executed a
                # destructive tool via interrupt resume — i.e. an APPROVED
                # action just ran and the agent is re-posting an updated
                # approval block for the next step. We detect that by checking
                # whether the most recent ToolMessage in history corresponds
                # to a destructive tool that just succeeded.
                if self._destructive_just_executed(messages):
                    return {"unarmed_gate_retry_count": 0}

                retry_count = int(state.get("unarmed_gate_retry_count") or 0)
                next_count = retry_count + 1

                if next_count > _MAX_UNARMED_GATE_RETRIES:
                    # Escalation: the model has now failed `_MAX_UNARMED_GATE_RETRIES`
                    # times to queue the destructive tool alongside post_approval_request.
                    # Stop asking nicely — demand mark_stand_down so the loop terminates.
                    logger.error(
                        "KeepLooping: post_approval_request called without a "
                        "destructive tool for the %dth consecutive turn — "
                        "demanding mark_stand_down to terminate the loop",
                        next_count,
                    )
                    return {
                        "unarmed_gate_retry_count": next_count,
                        "messages": [
                            HumanMessage(
                                content=(
                                    f"STOP. You have called post_approval_request "
                                    f"{next_count} times in a row WITHOUT queuing the "
                                    "destructive kubectl tool in the same turn. The "
                                    "approval gate cannot arm and Slack is being "
                                    "spammed with duplicate approval cards.\n\n"
                                    "Do NOT call post_approval_request again. Do NOT "
                                    "call post_to_slack with another summary. Your "
                                    "ONLY valid next action is:\n"
                                    "  mark_stand_down(reason=\"Could not arm the "
                                    "approval gate after repeated retries\")\n\n"
                                    "Call mark_stand_down now. This is the only way "
                                    "to end the loop cleanly."
                                )
                            )
                        ],
                    }

                logger.warning(
                    "KeepLooping: post_approval_request called without a "
                    "destructive tool in the same turn (retry %d/%d) — injecting "
                    "corrective HumanMessage so the gate can be armed",
                    next_count, _MAX_UNARMED_GATE_RETRIES,
                )
                return {
                    "unarmed_gate_retry_count": next_count,
                    "messages": [
                        HumanMessage(
                            content=(
                                "You called post_approval_request but did NOT queue "
                                "the destructive kubectl tool in the SAME turn. The "
                                "approval buttons in Slack are now no-ops because "
                                "interrupt_on has nothing to pause on. Re-issue your "
                                "tool calls now: call post_approval_request AND the "
                                "exact destructive tool from your action_list "
                                "(e.g. kubectl_delete pod ...) together in one "
                                "response. Do not wait for an approval message — "
                                "calling the destructive tool is what creates the "
                                "pending interrupt."
                            )
                        )
                    ],
                }

        # Reset the retry counter once we see any other state — the model
        # has either queued a destructive tool, called mark_stand_down, or
        # is doing genuine investigation work.
        if state.get("unarmed_gate_retry_count"):
            return {"unarmed_gate_retry_count": 0}
        return None

    # ── Helpers ─────────────────────────────────────────────────────────────
    @staticmethod
    def _tc_name(tc) -> str:
        if isinstance(tc, dict):
            return tc.get("name") or ""
        return getattr(tc, "name", "") or ""

    @staticmethod
    def _extract_text(msg: AIMessage) -> str:
        content = getattr(msg, "content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for p in content:
                if isinstance(p, dict) and "text" in p:
                    parts.append(p["text"])
                elif isinstance(p, str):
                    parts.append(p)
            return "\n".join(parts)
        return ""

    @classmethod
    def _looks_like_stand_down(cls, text: str) -> bool:
        if not text:
            return False
        lowered = text.lower()
        return any(phrase in lowered for phrase in STAND_DOWN_PHRASES)

    def _destructive_just_executed(self, messages) -> bool:
        """Return True if the most recent ToolMessage in history was for a
        destructive tool that completed successfully. This means the agent
        is mid-loop after an APPROVED action and is now posting the next
        approval block — that's a legitimate state, not a missing-gate bug."""
        for m in reversed(messages):
            if isinstance(m, ToolMessage):
                tool_name = getattr(m, "name", "") or ""
                if tool_name in self.destructive_tool_names:
                    content = getattr(m, "content", "") or ""
                    if isinstance(content, list):
                        content = " ".join(
                            p.get("text", "") if isinstance(p, dict) else str(p)
                            for p in content
                        )
                    if "Tool error" not in str(content):
                        return True
                # any other ToolMessage means we've moved past — stop scanning
                return False
        return False
