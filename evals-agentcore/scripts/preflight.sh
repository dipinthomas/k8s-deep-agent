#!/usr/bin/env bash
# Check everything the lab needs, in about 10 seconds, before you deploy anything.
#   cd evals-agentcore && ./scripts/preflight.sh
# Prints one line per check, with the fix for anything that fails. Exit code 1 if any check fails.
# The Bedrock check sends one tiny request (a few tokens) to the model.
set -uo pipefail
LAB="$(cd "$(dirname "$0")/.." && pwd)"
FAIL=0
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
bad()  { printf '  \033[31m✗\033[0m %s\n      fix: %s\n' "$1" "$2"; FAIL=1; }
warn() { printf '  \033[33m!\033[0m %s\n      %s\n' "$1" "$2"; }
have() { command -v "$1" >/dev/null 2>&1; }
vge()  { [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -1)" = "$2" ]; }   # version $1 >= $2

echo "Tools"
if have node; then v="$(node --version | tr -d v)"; vge "$v" 20.0.0 && ok "node $v" || bad "node $v is too old" "install Node 20 or newer (https://nodejs.org)"; else bad "node not found" "install Node 20 or newer (https://nodejs.org)"; fi
if have python3; then v="$(python3 -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])')"; vge "$v" 3.10.0 && ok "python $v" || bad "python $v is too old" "install Python 3.10 or newer"; else bad "python3 not found" "install Python 3.10 or newer"; fi
have uv && ok "uv $(uv --version | awk '{print $2}')" || bad "uv not found" "pip install uv   (or: curl -LsSf https://astral.sh/uv/install.sh | sh)"
have aws && ok "aws cli $(aws --version 2>&1 | awk '{print $1}' | cut -d/ -f2)" || bad "aws cli not found" "https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
have cdk && ok "cdk $(cdk --version 2>/dev/null | awk '{print $1}')" || bad "cdk not found" "npm i -g aws-cdk"
if have agentcore; then
  v="$(agentcore --version 2>/dev/null | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
  if [ -z "$v" ]; then warn "agentcore installed, version unknown" "this lab is tested with 1.0.2: npm i -g @aws/agentcore@1.0.2"
  elif vge "$v" 1.0.2; then ok "agentcore $v"; else bad "agentcore $v is too old for this project" "npm i -g @aws/agentcore@1.0.2"; fi
else bad "agentcore CLI not found" "npm i -g @aws/agentcore@1.0.2"; fi
if python3 -c 'import boto3' 2>/dev/null; then ok "boto3 importable"; else bad "boto3 not installed for python3" "uv venv && source .venv/bin/activate && uv pip install boto3"; fi
free_kb="$(df -Pk "$LAB" | awk 'NR==2{print $4}')"
if [ "${free_kb:-0}" -gt 1500000 ]; then ok "disk: $((free_kb / 1024)) MB free"; else bad "disk: only $((free_kb / 1024)) MB free here (the deploy needs ~600 MB)" "free space, or in CloudShell clone into /tmp instead of your home folder"; fi

echo "AWS"
REGION="${AWS_REGION:-$(aws configure get region 2>/dev/null)}"
if [ -z "$REGION" ]; then bad "no region" "export AWS_REGION=<your-region>   (e.g. us-east-1)"; echo; echo "Fix the above and re-run."; exit 1; fi
export AWS_REGION="$REGION"
[ -n "${AWS_PROFILE:-}" ] && ok "profile $AWS_PROFILE" || warn "AWS_PROFILE not set" "using your default credentials (fine in CloudShell)"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text 2>/dev/null)"
if [ -n "$ACCOUNT" ]; then ok "credentials work, region $REGION"; else bad "AWS credentials don't work" "aws configure / aws sso login --profile <your-profile>, then export AWS_PROFILE"; echo; exit 1; fi

if aws cloudformation describe-stacks --stack-name CDKToolkit --query 'Stacks[0].StackStatus' --output text >/dev/null 2>&1; then ok "CDK bootstrapped in $REGION"
else bad "CDK not bootstrapped in $REGION" "cdk bootstrap aws://$ACCOUNT/$REGION"; fi

if aws bedrock-agentcore-control list-agent-runtimes --max-results 1 >/dev/null 2>&1 \
   && aws bedrock-agentcore-control list-evaluators --max-results 1 >/dev/null 2>&1; then ok "AgentCore Runtime + Evaluations reachable in $REGION"
else bad "AgentCore (Runtime or Evaluations) not available in $REGION, or no permission" "use a region that has AgentCore Evaluations (we tested ap-southeast-2), check your IAM permissions"; fi

MODEL="$(python3 -c "
import json,sys
d=json.load(open('$LAB/k8sHarness/agentcore/agentcore.json'))
env={e['name']:e['value'] for r in d['runtimes'] if r['name']=='k8s_agent' for e in r.get('envVars',[])}
print(env.get('AGENT_MODEL','').split(':',1)[-1])")"
if out="$(aws bedrock-runtime converse --model-id "$MODEL" --messages '[{"role":"user","content":[{"text":"Reply with OK"}]}]' \
          --inference-config '{"maxTokens":5}' --query 'output.message.content[0].text' --output text 2>&1)"; then
  ok "Bedrock model $MODEL answers"
else
  case "$out" in
    *AccessDenied*|*"don't have access"*|*"not authorized"*) bad "no access to $MODEL" "Bedrock console -> Model access -> enable it (Anthropic models also ask for a short use-case form)";;
    *"invalid"*|*"not found"*|*ValidationException*) bad "$MODEL is not available in $REGION" "pick a model or inference profile your region has, and set AGENT_MODEL in k8sHarness/agentcore/agentcore.json";;
    *) bad "Bedrock call failed: ${out:0:200}" "check model access and region";;
  esac
fi

lam="$(python3 -c "
import json
d=json.load(open('$LAB/k8sHarness/agentcore/agentcore.json'))
print(d['agentCoreGateways'][0]['targets'][0]['lambdaFunctionArn']['lambdaArn'])")"
case "$lam" in arn:*":$REGION:$ACCOUNT:"*) ok "replay Lambda wired ($lam)";;
  arn:*) bad "agentcore.json points at a Lambda in another account/region" "python scripts/setup_replay.py";;
  *) warn "replay Lambda not set up yet" "next step: python scripts/setup_replay.py";; esac

echo
if [ "$FAIL" = 0 ]; then echo "All set. Next: see 'Run it' in evals-agentcore/README.md"; else echo "Fix the ✗ items above and re-run ./scripts/preflight.sh"; fi
exit "$FAIL"
