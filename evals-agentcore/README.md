# evals-agentcore: run the agent on Amazon Bedrock AgentCore and score it

This folder runs **this repo's agent** on AgentCore and scores it with AgentCore Evaluations.
It replays a recorded Kubernetes incident, so you **don't need** an EKS cluster, Slack, or an
OpenAI key. You need an AWS account and Bedrock model access.

You run the agent twice:

- **as-shipped**: the agent with a hidden bug. Its skills and `AGENTS.md` never load.
- **fixed**: the agent as it is in `agent/` today.

Then you watch the evals tell the two apart.

```
alarm ──► AgentCore Runtime  (k8s_agent: create_deep_agent, skills, AGENTS.md, subagents, HITL)
              │  MCP over SigV4
              ▼
          AgentCore Gateway  (k8s-tools) ──► Lambda k8s-harness-replay
              │                               same tool names as kubectl-mcp + cloudwatch-mcp,
              │                               answers from the recorded incident
          AgentCore Memory   (checkpointer instead of Redis; episodic incident memory)
          AgentCore Observability ──► AgentCore Evaluations (built-in + harness_rules Lambda)
```

| On EKS (`../agent`, `../infra`)      | In this lab (AgentCore)                                |
|--------------------------------------|--------------------------------------------------------|
| Flask `/trigger` + Slack bolt        | Runtime invoke `{"action":"alarm"}` / `{"action":"decide"}` |
| kubectl-mcp + cloudwatch-mcp (SSE)   | Gateway (MCP, AWS_IAM) → replay Lambda                 |
| Redis checkpointer                   | `AgentCoreMemorySaver`                                 |
| `/memories/incidents.md`             | AgentCore Memory, EPISODIC strategy                    |
| Phoenix + `agent/evals/`             | Observability + Evaluations (`harness_rules` ports `metrics.py`) |

## The incident

Cyber Monday, `retail-prod`. `checkoutservice` p99 latency is 1.5 s. The cause is
`batch/image-resizer`: a Deployment with **no CPU limit** and low priority, using about 3.7 cores on
the same node.

The right fix is `kubectl scale deployment/image-resizer -n batch --replicas=0`.

The traps:

- Deleting the pod. The ReplicaSet brings it straight back.
- Touching the victim (`checkoutservice`).
- Chasing 504s that look like a network problem.

The tools keep their real names (`kubectl_get`, `kubectl_top`, `get_metric_data`, `kubectl_scale`, ...),
and mutating calls change the replayed cluster state.

## The bug the evals catch

With deepagents 0.7.x, `CompositeBackend(default=StateBackend())` reads `./skills/` and `./AGENTS.md`
from in-memory state, which is empty. The agent starts with "No skills available yet" and
"(No memory loaded)". Nothing errors.

A second slip: a subagent's skills *source* must be a folder **of** skill folders, so
`./skills/universal/noisy-neighbor/` finds nothing.

The fix (`fixes/load-skills-and-agents-md.patch`) adds a `/harness/` FilesystemBackend route and
corrects the paths. It is already applied in `../agent/`. `scripts/sync_agent.sh as-shipped` puts the
bug back with `fixes/reintroduce-skills-bug.patch`, so you can see it.

## Before you start

- An AWS account where you can create IAM roles, Lambda functions and AgentCore resources.
- **Bedrock model access** to the model you use. The default is Claude Sonnet 5.5
  (`global.anthropic.claude-sonnet-5-5`). Enable it in the Bedrock console under **Model access**.
- A region with AgentCore Runtime, Gateway, Memory and Evaluations. We tested `ap-southeast-2`
  (Sydney) and `ap-south-1` (Mumbai).
- Node 20+, Python 3.10+, [`uv`](https://docs.astral.sh/uv/) (`pip install uv`), the AWS CLI.
- `npm i -g @aws/agentcore@1.0.2 aws-cdk` (1.0.2 matches the CDK project; 0.x is too old).
- CDK bootstrapped once per account and region: `cdk bootstrap aws://<account-id>/<region>`

Run `./scripts/preflight.sh` (after the venv step below). It checks every item above, including a
one-line Bedrock call, and prints the fix for anything missing.

> [!WARNING]
> **Cost and cleanup**
> - Bedrock tokens: one incident uses about **140k tokens** (one trace we measured: 141,733).
>   The commands below (one run per variant plus evaluation) use roughly 400k.
> - The online evaluation `live_incident_quality` scores **10% of sessions** with LLM judges
>   (`samplingRate` in `k8sHarness/agentcore/agentcore.json`). Raise it to 100 to see every run in
>   the dashboard; it costs judge tokens per session.
> - Always run `./scripts/teardown.sh` when you're done. It removes everything in `AWS_REGION`
>   except the CDK bootstrap stack.

## Run it

```bash
export AWS_PROFILE=<your-profile>          # any profile; skip in CloudShell
export AWS_REGION=<your-region>            # e.g. ap-south-1

cd evals-agentcore
uv venv && source .venv/bin/activate && uv pip install boto3
./scripts/preflight.sh                     # fix anything marked ✗ first

# 1. Replay Lambda + state; writes your account into aws-targets.json and agentcore.json
python scripts/setup_replay.py

# 2. Deploy the agent WITH the bug
./scripts/sync_agent.sh as-shipped
(cd k8sHarness/agentcore/cdk && npm install)
(cd k8sHarness && agentcore deploy)

# 3. Fire the incident and score it
python scripts/run_incident.py --runs 1 --tag as-shipped
python scripts/evaluate.py --tag as-shipped --extra-evaluator harness_rules

# 4. Switch to the fixed agent, redeploy, run and score again
./scripts/sync_agent.sh fixed
(cd k8sHarness && agentcore deploy)
python scripts/run_incident.py --runs 1 --tag fixed
python scripts/evaluate.py --tag fixed --extra-evaluator harness_rules

# 5. Remove everything
./scripts/teardown.sh
```

`run_incident.py` plays the on-call engineer. It prints what the agent would post to Slack and
approves the action it asks for. Use `--runs 3` per variant for steadier numbers (3x the tokens).

### Memory demo

```bash
python scripts/seed_memory.py                                   # one past incident, no waiting
python scripts/run_incident.py --runs 1 --tag memory --use-memory
```

The agent's prompt now starts with "Past incidents from long-term memory". Without seeding, AgentCore
Memory needs about 15 minutes after a session to build the long-term record.

## What to look for

| | as-shipped | fixed |
|---|---|---|
| Agent's prompt (Observability → trace) | "No skills available yet", "(No memory loaded)" | 4 skills under `/harness/skills/universal/`, AGENTS.md loaded |
| `Builtin.GoalSuccessRate` | **0**: never confirms latency recovered | **1**: checks metrics after the fix |
| `harness_rules` | fails `read_runbook` | usually passes |

Both versions usually pick the right fix. The difference is whether the agent follows its runbook
and checks its work. Our full numbers are in [RESULTS.md](RESULTS.md). Your own results go to
`results/` (gitignored).

## Change the model

The model is the `AGENT_MODEL` env var on the runtime, in `k8sHarness/agentcore/agentcore.json`:

```json
{ "name": "AGENT_MODEL", "value": "bedrock_converse:global.anthropic.claude-sonnet-5-5" }
```

Use `bedrock_converse:<model or inference profile id>`, enable access to that model in Bedrock,
then run `agentcore deploy` again. Only Claude Sonnet 5.5 was tested on this path.

## Things that will surprise you

- **Memory is slow to appear.** AgentCore Memory builds the long-term episode about 15 minutes after a
  session, so a second run straight away recalls nothing. Use `seed_memory.py` for demos.
- **It's a replay, not a live cluster.** The Lambda answers from a recorded incident, so the agent
  can't break anything, and every run starts from the same state.
- **The skill evaluators return "no results"** even for the fixed agent. See RESULTS.md.
- **`agentcore eval ondemand` aborts** on these big sessions (about 25 MB each). Use
  `scripts/evaluate.py`, which calls the Evaluate API directly.
- **CloudShell's home folder is 1 GB.** The deploy needs about 600 MB, so clone into `/tmp` and run
  the teardown before the session ends.
- **Running in two regions is fine.** Everything is regional; the one global resource, the replay
  Lambda's IAM role, is named `k8s-harness-replay-role-<region>`.

## Files

| Path | What it is |
|---|---|
| `scripts/preflight.sh` | Checks tools, versions, credentials, region, CDK bootstrap, AgentCore and Bedrock access |
| `scripts/setup_replay.py` | Creates the replay Lambda, its IAM role and SSM state; wires the Gateway target |
| `scripts/sync_agent.sh` | Copies `../agent`, `../skills`, `../AGENTS.md` into the runtime code (`as-shipped` or `fixed`) |
| `scripts/run_incident.py` | Fires the alarm, approves, records each session to `results/sessions.jsonl` |
| `scripts/seed_memory.py` | Writes one past incident into AgentCore Memory for the memory demo |
| `scripts/evaluate.py` | Scores recorded sessions with AgentCore Evaluations |
| `scripts/teardown.sh` | Deletes everything the lab created (not the CDK bootstrap stack) |
| `k8sHarness/` | AgentCore project (`agentcore.json`, CDK, runtime and evaluator code) |
| `replay_lambda/` | The recorded incident, served as MCP tools |
| `lab-cluster/SKILL.md` | Cluster skill for `retail-prod` |
| `fixes/` | The skills fix, its reverse, and the tool-result-order fix |
