"""AgentCore Gateway target for the retail-prod lab.

Serves the same tool names as the real kubectl and CloudWatch MCP servers, answering
from the recorded incident in replay_core.py. Cluster state (replica counts, deleted
pods) lives in one SSM parameter so it survives between tool calls. The driver resets
it before each incident by invoking this function with {"__lab_reset": true}.
"""
import json
import os

import boto3

from replay_core import Incident

PARAM = os.environ.get("STATE_PARAM", "/k8s-harness-lab/state")
_ssm = boto3.client("ssm")


def _load() -> Incident:
    try:
        return Incident(json.loads(_ssm.get_parameter(Name=PARAM)["Parameter"]["Value"]))
    except _ssm.exceptions.ParameterNotFound:
        return Incident()


def _save(inc: Incident) -> None:
    _ssm.put_parameter(Name=PARAM, Value=json.dumps(inc.to_state()), Type="String", Overwrite=True)


def handler(event, context):
    if event.get("__lab_reset"):
        _save(Incident())
        return {"reset": True}
    if event.get("__lab_state"):
        return _load().to_state()
    tool = ""
    try:
        tool = context.client_context.custom.get("bedrockAgentCoreToolName", "")
    except AttributeError:
        pass
    tool = tool.split("___")[-1]
    inc = _load()
    result = inc.call(tool, dict(event))
    if tool in ("kubectl_scale", "kubectl_delete", "kubectl_patch"):
        _save(inc)
    print(json.dumps({"tool": tool, "args": event, "fixed_after": inc.fixed}))
    return result
