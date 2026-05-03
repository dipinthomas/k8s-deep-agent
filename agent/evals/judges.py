"""
LLM-as-judge evaluators for qualitative dimensions.

Each judge builds a structured transcript from the LangGraph message history,
sends it to the configured LLM (same AGENT_MODEL env var as the agent itself),
and parses a score + explanation from the response.

These are more expensive than the rule-based evals in metrics.py and are
intended to run asynchronously after an investigation completes, not inline.

Evaluated dimensions:
  1. root_cause_identification  — did the agent name a specific, actionable root cause?
  2. skill_md_compliance        — did the agent follow the SKILL.md decision tree?
  3. remediation_target_correct — did it target the right resource (cause, not victim)?
  4. slack_message_format       — does the Slack message match the required template?
  5. investigation_completeness — did all 3 subagents contribute to the hypothesis?
  6. confidence_language        — no hedging words in the final recommendation?
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from evals.metrics import EvalResult, SUBAGENT_NAMES, _ai_turns, _tc_name, _extract_text

logger = logging.getLogger(__name__)

_HEDGING_WORDS = (
    "likely", "probably", "probable", "suspected", "possibly",
    "perhaps", "might be", "could be", "may be", "seemingly",
)

# Max chars of transcript to send to the judge — keeps the eval call cheap.
_MAX_TRANSCRIPT_CHARS = 6000


def _build_transcript(messages: list) -> str:
    """Convert LangGraph message history into a legible text transcript."""
    lines: list[str] = []
    turn = 0
    for msg in messages:
        kind = type(msg).__name__
        raw = getattr(msg, "content", "")
        if isinstance(raw, list):
            text = "\n".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in raw)
        else:
            text = str(raw or "")
        text = text.strip()

        if isinstance(msg, AIMessage):
            tcs = msg.tool_calls or []
            if tcs:
                tc_summary = ", ".join(
                    f"{_tc_name(tc)}({json.dumps(tc.get('args', {}) if isinstance(tc, dict) else getattr(tc, 'args', {}))[:80]})"
                    for tc in tcs
                )
                lines.append(f"[Agent turn {turn} → tools: {tc_summary}]")
                if text:
                    lines.append(f"  reasoning: {text[:300]}")
            else:
                lines.append(f"[Agent turn {turn}] {text[:400]}")
            turn += 1

        elif isinstance(msg, ToolMessage):
            name = getattr(msg, "name", "tool")
            excerpt = text[:500] + ("…" if len(text) > 500 else "")
            lines.append(f"  [tool result: {name}] {excerpt}")

        elif isinstance(msg, HumanMessage):
            # Skip middleware injections to keep the transcript readable
            from evals.metrics import _is_middleware_injection
            if not _is_middleware_injection(msg):
                lines.append(f"[Human] {text[:300]}")

    transcript = "\n".join(lines)
    if len(transcript) > _MAX_TRANSCRIPT_CHARS:
        # Keep head (investigation) + tail (resolution) for context
        head = transcript[:int(_MAX_TRANSCRIPT_CHARS * 0.6)]
        tail = transcript[-int(_MAX_TRANSCRIPT_CHARS * 0.4):]
        transcript = head + "\n\n…[middle truncated]…\n\n" + tail
    return transcript


def _get_judge_model():
    """Return a LangChain chat model for eval judges. Reuses AGENT_MODEL config."""
    from agent import _build_model_with_timeout
    model_spec = os.environ.get("AGENT_MODEL", "openai:gpt-5-mini")
    return _build_model_with_timeout(model_spec)


def _parse_verdict(text: str, valid_labels: tuple[str, ...]) -> tuple[str, float, str]:
    """Extract VERDICT / SCORE / EXPLANATION from judge response.
    Returns (label, score, explanation).
    """
    label = "warn"
    score = 0.5
    explanation = text.strip()

    for line in text.splitlines():
        line = line.strip()
        if line.upper().startswith("VERDICT:"):
            raw = line.split(":", 1)[1].strip().lower()
            for v in valid_labels:
                if v in raw:
                    label = v
                    score = 1.0 if v == "pass" else (0.5 if v == "warn" else 0.0)
                    break
        elif line.upper().startswith("EXPLANATION:"):
            explanation = line.split(":", 1)[1].strip()

    return label, score, explanation


async def _judge(prompt: str, valid_labels: tuple[str, ...] = ("pass", "warn", "fail")) -> tuple[str, float, str]:
    """Call the judge LLM and parse the structured response."""
    from langchain_core.messages import SystemMessage, HumanMessage as LCHumanMessage

    model = _get_judge_model()
    try:
        response = await model.ainvoke([
            SystemMessage(content=(
                "You are an expert evaluator of AI agent behaviour. "
                "Respond ONLY in this format with no extra text:\n"
                "VERDICT: <pass|warn|fail>\n"
                "EXPLANATION: <one concise sentence>\n\n"
                f"Valid verdicts: {', '.join(valid_labels)}"
            )),
            LCHumanMessage(content=prompt),
        ])
        raw = getattr(response, "content", "")
        if isinstance(raw, list):
            raw = "\n".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in raw)
        return _parse_verdict(str(raw), valid_labels)
    except Exception:
        logger.exception("Judge LLM call failed")
        return "warn", 0.5, "Judge call failed — result not available."


async def root_cause_identification(messages: list, alarm_name: str = "") -> EvalResult:
    """Did the agent name a specific, actionable root cause — not a vague summary?

    pass — identifies a specific resource (e.g. 'inventory-sync-job consuming 3.2 CPUs')
    warn — identifies a category but not a specific resource ('high CPU from a batch job')
    fail — vague or tautological ('CPU is high because of high CPU usage')
    """
    transcript = _build_transcript(messages)
    verdict, score, explanation = await _judge(
        f"""Alarm: {alarm_name or 'unknown'}

Investigation transcript:
---
{transcript}
---

Evaluate: Did the agent identify a SPECIFIC, ACTIONABLE root cause?

PASS: Agent named a specific resource/deployment/pod as the cause with supporting
      evidence (e.g. "inventory-sync-job is consuming 3.2 CPUs per the kubectl top output").
WARN: Agent identified a category (e.g. "a batch job is consuming CPU") but did not
      name the specific resource or cited evidence is indirect.
FAIL: Agent's root cause is vague, tautological, or contradicts the evidence
      (e.g. "the node is under pressure due to resource contention").
""",
        ("pass", "warn", "fail"),
    )
    return EvalResult(
        name="root_cause_identification",
        score=score,
        label=verdict,
        explanation=explanation,
        metadata={"alarm": alarm_name},
    )


async def skill_md_compliance(messages: list, skill_content: str = "") -> EvalResult:
    """Did the agent follow the SKILL.md decision tree for its remediation choice?

    pass — followed the decision tree; targeted the resource the SKILL.md specifies
    warn — partially followed the skill (correct tool, wrong target or vice versa)
    fail — ignored the skill; targeted the victim service or used a prohibited approach
    """
    if not skill_content:
        return EvalResult(
            name="skill_md_compliance",
            score=0.5,
            label="warn",
            explanation="SKILL.md content not provided — compliance cannot be evaluated.",
        )

    transcript = _build_transcript(messages)
    # Extract only the decision tree section to keep the prompt bounded
    skill_excerpt = skill_content[:2000]

    verdict, score, explanation = await _judge(
        f"""Cluster SKILL.md (decision tree excerpt):
---
{skill_excerpt}
---

Investigation transcript:
---
{transcript}
---

Evaluate: Did the agent follow the SKILL.md decision tree for its remediation decision?

PASS: Agent's chosen target and tool match exactly what the decision tree prescribes.
      The SKILL.md says the CAUSE is the noisy-neighbor deployment, not the victim alarm service —
      agent targeted the correct deployment.
WARN: Agent partially followed the skill (right tool, wrong target; or right target, wrong tool).
FAIL: Agent targeted the victim service named in the alarm instead of the cause named in SKILL.md,
      or chose a remediation approach explicitly prohibited by the skill.
""",
        ("pass", "warn", "fail"),
    )
    return EvalResult(
        name="skill_md_compliance",
        score=score,
        label=verdict,
        explanation=explanation,
    )


async def remediation_target_correct(messages: list, alarm_name: str = "", skill_content: str = "") -> EvalResult:
    """Did the agent target the ROOT CAUSE resource, not the victim service?

    The alarm names the victim. The SKILL.md names the cause. The agent must
    distinguish between the two and act on the cause.
    """
    transcript = _build_transcript(messages)
    skill_hint = f"SKILL.md excerpt:\n{skill_content[:1000]}\n\n" if skill_content else ""

    verdict, score, explanation = await _judge(
        f"""{skill_hint}Alarm: {alarm_name or 'unknown'}

Investigation transcript:
---
{transcript}
---

Evaluate: Did the agent target the CORRECT resource for remediation?

Context: CloudWatch alarms name the VICTIM service (the one degraded). The actual
CAUSE is a different service (noisy neighbor, disk hog, etc.) identified during investigation.

PASS: Agent correctly distinguished victim from cause and targeted the cause for remediation.
WARN: Agent targeted the correct resource but with some confusion about victim/cause distinction.
FAIL: Agent attempted to scale/delete/drain the VICTIM service named in the alarm,
      not the CAUSE identified during investigation.
""",
        ("pass", "warn", "fail"),
    )
    return EvalResult(
        name="remediation_target_correct",
        score=score,
        label=verdict,
        explanation=explanation,
        metadata={"alarm": alarm_name},
    )


async def slack_message_format(messages: list) -> EvalResult:
    """Did the post_to_slack call follow the required template format?

    Required elements from the system prompt:
      - ━━━ separator present between summary and verbose section
      - Verbose section uses '• *Label:* value' bullet format
      - No paragraphs in the verbose section
      - No hedging language ('likely', 'probable', 'suspected')
      - Confident language in the recommendation
    """
    # Extract the actual Slack message content from ToolMessage for post_to_slack
    slack_content = ""
    for msg in messages:
        if isinstance(msg, ToolMessage) and getattr(msg, "name", "") == "post_to_slack":
            # The ToolMessage content is the API response, not the message text.
            # Get the message from the preceding AIMessage's tool_call args instead.
            pass

    # Get the args passed to post_to_slack from the AIMessage tool calls
    for msg in messages:
        if isinstance(msg, AIMessage):
            for tc in (msg.tool_calls or []):
                if _tc_name(tc) == "post_to_slack":
                    args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
                    slack_content = str(args.get("message", args.get("text", args.get("content", ""))))
                    break
        if slack_content:
            break

    if not slack_content:
        return EvalResult(
            name="slack_message_format",
            score=0.5,
            label="warn",
            explanation="Could not extract post_to_slack message content — format not verifiable.",
        )

    # Rule-based pre-checks (fast)
    has_separator = "━━━" in slack_content
    has_bullet_format = bool(re.search(r"•\s+\*[^*]+\*:", slack_content))
    hedging_found = [w for w in _HEDGING_WORDS if w in slack_content.lower()]

    if hedging_found:
        return EvalResult(
            name="slack_message_format",
            score=0.0,
            label="fail",
            explanation=f"Hedging language found in Slack message: {hedging_found}. "
                        "System prompt requires confident language — no 'likely', 'probable', 'suspected'.",
            metadata={"hedging_words": hedging_found},
        )

    if not has_separator:
        return EvalResult(
            name="slack_message_format",
            score=0.0,
            label="fail",
            explanation="Missing ━━━ separator in Slack message. "
                        "System prompt requires this to separate the summary from the verbose section.",
        )

    if not has_bullet_format:
        # LLM judge to verify if the format is close enough
        verdict, score, explanation = await _judge(
            f"""Slack message sent by the agent:
---
{slack_content[:1500]}
---

Evaluate whether the verbose section (after ━━━) uses '• *Label:* value' bullet format
with one bullet per line and no prose paragraphs.

PASS: Verbose section uses '• *Label:* value' bullets throughout, no paragraphs.
WARN: Mostly bullets but with some prose mixing.
FAIL: Verbose section is written as paragraphs, not bullets.
""",
            ("pass", "warn", "fail"),
        )
        return EvalResult(
            name="slack_message_format",
            score=score,
            label=verdict,
            explanation=explanation,
        )

    return EvalResult(
        name="slack_message_format",
        score=1.0,
        label="pass",
        explanation="Slack message contains ━━━ separator, bullet format, and no hedging language.",
    )


async def investigation_completeness(messages: list) -> EvalResult:
    """Did all three subagents contribute findings that were synthesised into
    the final hypothesis — or did the agent ignore one or more of them?
    """
    # Check which subagents were called and which returned results
    called = set()
    returned = set()
    for msg in messages:
        if isinstance(msg, AIMessage):
            for tc in (msg.tool_calls or []):
                n = _tc_name(tc)
                if n in SUBAGENT_NAMES:
                    called.add(n)
        if isinstance(msg, ToolMessage):
            n = getattr(msg, "name", "")
            if n in SUBAGENT_NAMES:
                content = str(getattr(msg, "content", "") or "")
                if "Tool error" not in content and len(content) > 50:
                    returned.add(n)

    uncalled = SUBAGENT_NAMES - called
    no_result = called - returned

    if uncalled:
        return EvalResult(
            name="investigation_completeness",
            score=0.0,
            label="fail",
            explanation=f"Subagent(s) never called: {sorted(uncalled)}. "
                        "All three investigators (cloudwatch, kubectl, otel) must run for complete evidence.",
            metadata={"uncalled": sorted(uncalled), "no_result": sorted(no_result)},
        )

    if no_result:
        return EvalResult(
            name="investigation_completeness",
            score=0.5,
            label="warn",
            explanation=f"Subagent(s) called but returned no usable result: {sorted(no_result)}. "
                        "Evidence may be incomplete.",
            metadata={"uncalled": sorted(uncalled), "no_result": sorted(no_result)},
        )

    # All three returned — use LLM to verify synthesis
    transcript = _build_transcript(messages)
    verdict, score, explanation = await _judge(
        f"""Investigation transcript:
---
{transcript}
---

All three subagents (cloudwatch-investigator, kubectl-investigator, otel-investigator)
returned results. Evaluate whether the agent SYNTHESISED all three sets of findings
into its final hypothesis and Slack message — or whether it ignored one or more.

PASS: Agent's final conclusion references evidence from all three investigators.
WARN: Agent used two out of three; the third was mentioned but not meaningfully integrated.
FAIL: Agent built its conclusion from only one or two investigators and disregarded the rest.
""",
        ("pass", "warn", "fail"),
    )
    return EvalResult(
        name="investigation_completeness",
        score=score,
        label=verdict,
        explanation=explanation,
        metadata={"called": sorted(called), "returned": sorted(returned)},
    )


async def run_all(
    messages: list,
    alarm_name: str = "",
    alarm_node: str = "",
    skill_content: str = "",
) -> list[EvalResult]:
    """Run all LLM-as-judge evals concurrently. Returns results in stable order."""
    import asyncio

    tasks = {
        "root_cause_identification": root_cause_identification(messages, alarm_name),
        "skill_md_compliance": skill_md_compliance(messages, skill_content),
        "remediation_target_correct": remediation_target_correct(messages, alarm_name, skill_content),
        "slack_message_format": slack_message_format(messages),
        "investigation_completeness": investigation_completeness(messages),
    }

    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    out = []
    for name, result in zip(tasks.keys(), results):
        if isinstance(result, Exception):
            logger.exception("Judge eval %s raised an exception", name)
            out.append(EvalResult(
                name=name,
                score=0.5,
                label="warn",
                explanation=f"Eval raised an exception: {result!r}",
            ))
        else:
            out.append(result)
    return out
