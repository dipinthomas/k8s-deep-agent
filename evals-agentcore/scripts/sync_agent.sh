#!/usr/bin/env bash
# Copy the repo's agent into the AgentCore Runtime code folder.
#   ./scripts/sync_agent.sh as-shipped   the agent with the skills bug put back in
#                                        (reverse of fixes/load-skills-and-agents-md.patch)
#   ./scripts/sync_agent.sh fixed        the repo's agent/ exactly as it is
#
# The repo's agent/ already contains the skills fix and the KeepLooping
# tool-result-order fix. "as-shipped" only reverts the skills fix, so the one
# difference between the two variants is the bug the evals catch.
set -euo pipefail
VARIANT="${1:?usage: sync_agent.sh as-shipped|fixed}"
case "$VARIANT" in as-shipped|fixed) ;; *) echo "usage: sync_agent.sh as-shipped|fixed" >&2; exit 2 ;; esac
LAB="$(cd "$(dirname "$0")/.." && pwd)"
REPO="${REPO:-$(cd "$LAB/.." && pwd)}"          # evals-agentcore/ sits in the repo root
APP="$LAB/k8sHarness/app/k8s_agent"

rm -rf "$APP/agent_src" "$APP/skills" "$APP/clusters" "$APP/AGENTS.md"
mkdir -p "$APP/agent_src" "$APP/clusters/retail-prod"
for f in agent.py subagents.py middleware.py optimization.py; do cp "$REPO/agent/$f" "$APP/agent_src/"; done
for d in tools mcp_servers memory; do cp -r "$REPO/agent/$d" "$APP/agent_src/"; done
find "$APP/agent_src" -name __pycache__ -prune -exec rm -rf {} +
cp "$REPO/AGENTS.md" "$APP/AGENTS.md"
cp -r "$REPO/skills" "$APP/skills"
cp "$LAB/lab-cluster/SKILL.md" "$APP/clusters/retail-prod/SKILL.md"

if [ "$VARIANT" = "as-shipped" ]; then
  # Put the bug back: skills/AGENTS.md read from the empty StateBackend.
  (cd "$APP/agent_src" && patch -p2 --no-backup-if-mismatch < "$LAB/fixes/reintroduce-skills-bug.patch")
fi

python3 - "$LAB/k8sHarness/agentcore/agentcore.json" "$VARIANT" <<'EOF'
import json, sys
p, variant = sys.argv[1], sys.argv[2]
d = json.load(open(p))
for r in d["runtimes"]:
    if r["name"] == "k8s_agent":
        env = [e for e in r.get("envVars", []) if e["name"] != "HARNESS_VERSION"]
        env.append({"name": "HARNESS_VERSION", "value": variant})
        r["envVars"] = env
for g in d.get("agentCoreGateways", []):
    for t in g.get("targets", []):
        arn = t.get("lambdaFunctionArn", {}).get("lambdaArn", "")
        if not arn.startswith("arn:"):
            print("WARNING: gateway target has no Lambda ARN yet; run scripts/setup_replay.py first", file=sys.stderr)
json.dump(d, open(p, "w"), indent=2)
EOF
echo "synced agent ($VARIANT) into $APP"
