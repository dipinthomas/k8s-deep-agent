"""Serve the recorded retail-prod incident as local MCP servers (no AWS, no cluster).

  python replay_mcp.py kubectl     --port 3001   # kubectl_* tools
  python replay_mcp.py cloudwatch  --port 3002   # CloudWatch tools

Same tool names, descriptions and behaviour as the AgentCore Gateway target in
evals-agentcore/replay_lambda/, so the EKS agent image can run the full incident
locally. State (replicas, deleted pods) lives in this process.
"""
import argparse
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPLAY = HERE.parents[1] / "evals-agentcore" / "replay_lambda"
sys.path.insert(0, str(REPLAY))

from mcp.server.fastmcp import FastMCP  # noqa: E402
from replay_core import Incident  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("group", choices=["kubectl", "cloudwatch"])
ap.add_argument("--port", type=int, required=True)
ap.add_argument("--host", default="0.0.0.0")
a = ap.parse_args()

schemas = {t["name"]: t for t in json.loads((REPLAY / "tool-schema.json").read_text())}
incident = Incident()
server = FastMCP(f"replay-{a.group}", host=a.host, port=a.port)

for name in Incident.TOOL_NAMES:
    if name.startswith("kubectl_") != (a.group == "kubectl"):
        continue
    # Bound method: FastMCP builds the input schema from its signature, which
    # uses the same argument names as tool-schema.json.
    server.add_tool(getattr(incident, name), name=name, description=schemas[name]["description"])


@server.custom_route("/healthz", methods=["GET"])
async def healthz(request):  # noqa: ARG001
    from starlette.responses import PlainTextResponse
    return PlainTextResponse("ok")


@server.custom_route("/state", methods=["GET"])
async def state(request):  # noqa: ARG001
    from starlette.responses import JSONResponse
    return JSONResponse({**incident.to_state(), "fixed": incident.fixed})


server.run(transport="streamable-http")
