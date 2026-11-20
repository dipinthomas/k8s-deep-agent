# Results: k8s deep agent on AgentCore, as-shipped vs fixed

One lab run, 12 November 2026, in `ap-southeast-2`.
Model: `global.anthropic.claude-sonnet-5-5` (Bedrock Converse).
"as-shipped" is the agent with the skills bug; "fixed" adds `fixes/load-skills-and-agents-md.patch`.
All numbers come from the recorded sessions, the evaluation output and the console. None are estimated.
Your numbers will differ a little from run to run, because an LLM judges most of them.

## Second check: fresh clone in a new region (15 November 2026, `ap-south-1`)

A fresh clone of the branch, run in Mumbai following the README, from `preflight.sh` to
`teardown.sh`. One run per variant.

| | as-shipped | fixed |
|---|---|---|
| "No skills available yet" in the prompt | yes (22 spans) | no; noisy-neighbor skill and AGENTS.md loaded |
| Builtin.GoalSuccessRate | 0.0 | 1.0 |
| Builtin.TrajectoryInOrderMatch | 0.0 | 1.0 |
| harness_rules | 0.8 | 0.8 |
| Memory demo (`seed_memory.py` then `--use-memory`) | | seeded incident recalled in the prompt |

Found and fixed during this check: the first ~12 minutes of traces in a new region are dropped
(`run_incident.py` now waits), and `seed_memory.py` must write to the exact recall namespace.

## Headline (first run, Sydney)

| | as-shipped (3 sessions) | fixed (3 sessions) |
|---|---|---|
| Skills listed in the prompt | none ("No skills available yet") | 4 skills, `/harness/skills/universal/*/SKILL.md` |
| AGENTS.md in `<agent_memory>` | `(No memory loaded)` | AGENTS.md loaded |
| Any `read_file` of a `SKILL.md` | **0 / 3** (it tried `glob **/SKILL.md` and `ls /skills`: "No files found") | **2 / 3** (the kubectl-investigator subagent read `noisy-neighbor/SKILL.md`) |
| Builtin.GoalSuccessRate | **0.00** (0/3) | **1.00** (3/3) |
| Builtin.TrajectoryInOrderMatch | 1.00 | 1.00 |
| Builtin.ToolSelectionAccuracy | 0.95 over 127 tool calls | 0.94 over 105 tool calls |
| harness_rules (on-demand) | 0.8, 0.8, 0.8 | 0.6, 1.0, 1.0 |
| Builtin.SkillSelectionAccuracy / SkillInstructionFollowing | no results | no results (see below) |
| Outcome | all 3: approval, then `scale image-resizer=0`, no pods deleted | same |
| Tokens, one session (console trace) | | about 142k |

What the evals showed:

1. **Both versions fix the incident. Only the fixed one checks its work.** All 3 as-shipped runs fail
   the judge's assertion "Confirms checkout latency has recovered after the change". The judge quotes
   the agent: "I haven't confirmed that checkout latency has recovered yet". All 3 fixed runs call
   `get_metric_data` and `kubectl_top` after the scale and report 1490 → 236 ms. That verification
   step is in AGENTS.md and the noisy-neighbor runbook, and as-shipped never loaded either.
2. **Nothing errors when the skills are missing.** The as-shipped agent just works without its
   runbooks. GoalSuccessRate is the evaluator that caught it.
3. **The skill evaluators did not score either version.** See below.

## Per-session scores (`scripts/evaluate.py`)

| session (suffix) | tag | GoalSuccess | Trajectory | ToolSelection (n) | harness_rules | failed rules | SKILL.md read |
|---|---|---|---|---|---|---|---|
| `d68113a8` | as-shipped | 0.0 | 1.0 | 0.976 (42) | 0.8 | read_runbook | no |
| `55d79c8b` | as-shipped | 0.0 | 1.0 | 0.930 (43) | 0.8 | read_runbook | no |
| `be0c0e1c` | as-shipped | 0.0 | 1.0 | 0.976 (42) | 0.8 | read_runbook | no |
| `b630a421` | fixed | 1.0 | 1.0 | 0.912 (34) | 0.6 | read_runbook, no_forbidden_tools (subagent called `kubectl_context`) | no |
| `8144e9b0` | fixed | 1.0 | 1.0 | 0.974 (38) | 1.0 | none | yes (subagent) |
| `1f1c444b` | fixed | 1.0 | 1.0 | 0.909 (33) | 1.0 | none | yes (subagent) |

Means: as-shipped GoalSuccess 0.00, harness_rules 0.80. Fixed GoalSuccess 1.00, harness_rules 0.87.

Batch evaluations in the console gave the same GoalSuccess, Trajectory and skill numbers. Their
harness_rules values were not valid: every session reported `tool calls=0` (see "Known issues").

## Why the skill evaluators return no results

The AgentCore docs ("skill evaluators", framework support) count a file read as a skill load only if
the path ends in `/SKILL.md` **and the tool result is a well-formed SKILL.md** (frontmatter with
`name` and `description`). Deep Agents' `read_file` puts a header in front of the content
(`@@ lines 1-129 of 129 @@`) and wraps it in a LangChain ToolMessage. That most likely fails the
check. This has not been confirmed with AWS.

To get a score, the agent would have to load the skill through Deep Agents' native skill tool, or
`read_file` would have to return the raw file.

## Memory demo (`--use-memory`, fixed agent)

| run | started (UTC) | past incident in the prompt? | outcome |
|---|---|---|---|
| 1 | 15:38 | no (nothing stored yet) | done, scaled to 0 |
| 2 | 15:50 | **no** | done, scaled to 0 |
| 3 | 16:01 | **yes**: "Scale down the non-critical batch deployment (e.g., image-resizer…) to zero" | done, scaled to 0 |

Run 1 wrote its event at 15:39. The EPISODIC strategy produced the long-term record at 15:55,
about 15 minutes later, so run 2 (12 minutes later) had nothing to recall. Wait at least 15
minutes, or seed an episode before a live demo.

## Online evaluation (`live_incident_quality`, 100% sampling)

12 sessions scored for GoalSuccessRate, average 0.92 (this includes early crashed sessions and the
memory runs). harness_rules also has 12 results, but they are unreliable for the reason below.
The two skill evaluators produced 0 results online.

## Known issues found during the lab (and what was done)

1. **Crash at the approval step (fixed in `agent/`).** `KeepLoopingMiddleware` put a HumanMessage
   between `tool_use` and `tool_result`. Bedrock Converse and the Anthropic API reject that with a
   400. Fix: `fixes/keeplooping-tool-result-order.patch`, now applied to `agent/middleware.py`.
2. **Resume after approval failed** ("Invalid `signature` in `thinking` block"). langgraph 1.2
   rebuilds the message list by replaying writes. `AgentCoreMemorySaver` replayed parallel tool
   results in the wrong order, and it was slow (about 165 s per resume). Fix:
   `k8sHarness/app/k8s_agent/ordered_saver.py` (AgentCore side only).
3. **`agentcore eval ondemand` aborted** on these sessions (about 2,300 span records, 25 MB each).
   `scripts/evaluate.py` calls the `Evaluate` API directly instead, and trims middleware state dumps
   to stay under the code evaluator's ~6 MB limit.
4. **harness_rules counted every Gateway tool twice** (agent span + MCP client span). Fixed in
   `k8sHarness/app/harness_rules/`.
5. **Batch and online evaluation give harness_rules a truncated span set** for sessions this big.
   Use the on-demand numbers from `scripts/evaluate.py`.
