"""
Demo site API backend
FastAPI + SSE — bridges the Next.js frontend to real EKS/CloudWatch
when REAL_CLUSTER=true, falls back to simulation otherwise.
"""

import asyncio
import json
import os
import subprocess
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

load_dotenv()

REAL_CLUSTER = os.getenv("REAL_CLUSTER", "false").lower() == "true"
NAMESPACE = os.getenv("NAMESPACE", "otel-demo")
CLUSTER_NAME = os.getenv("CLUSTER_NAME", "otel-demo-prod")

# In-memory incident state
_state: dict = {
    "active": False,
    "fault": None,
    "approved": asyncio.Event(),  # set when operator approves
    "denied": False,
    "log_queue": asyncio.Queue(),
}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    _state["approved"] = asyncio.Event()
    yield


app = FastAPI(title="K8s AI Agent Demo API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Models ───────────────────────────────────────────────────────────────────

class FaultRequest(BaseModel):
    service: str
    type: str


class ApproveRequest(BaseModel):
    token: str = ""


# ─── Allowlists ───────────────────────────────────────────────────────────────

ALLOWED_FAULTS = {"disk_pressure", "cpu_spike", "pod_crash", "high_latency"}
ALLOWED_SERVICES = {
    "loadgenerator", "imageprovider", "adservice", "recommendationservice",
    "frontend", "frontendproxy",
}
CRITICAL_SERVICES = {"checkout", "payment", "cart", "productcatalog", "cartservice",
                     "checkoutservice", "paymentservice", "productcatalogservice"}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


async def _push(type_: str, **kwargs) -> None:
    entry = {"type": type_, "ts": _ts(), **kwargs}
    await _state["log_queue"].put(json.dumps(entry))


def _kubectl(*args: str, timeout: int = 15) -> str:
    """Run a kubectl command and return stdout."""
    cmd = ["kubectl", *args]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return result.stdout.strip() or result.stderr.strip()


# ─── Fault injection ──────────────────────────────────────────────────────────

async def _run_disk_pressure() -> None:
    """Drive the full disk-pressure incident investigation script."""
    await _push("agent", text="Incident received — node disk pressure on ip-10-0-1-45")
    await asyncio.sleep(0.6)
    await _push("agent", text="Checkout p99 latency degrading: 120ms → 890ms")
    await asyncio.sleep(0.5)
    await _push("agent", text="Spawning specialist subagents in parallel...")
    await asyncio.sleep(0.3)
    await _push("spawn", text="▸ cloudwatch-agent  starting metric investigation")
    await asyncio.sleep(0.2)
    await _push("spawn", text="▸ kubectl-agent      starting cluster state scan")
    await asyncio.sleep(0.2)
    await _push("spawn", text="▸ otel-agent         starting trace analysis")
    await asyncio.sleep(0.8)

    if REAL_CLUSTER:
        out = _kubectl("get", "nodes", "--no-headers",
                       "-o", "custom-columns=NAME:.metadata.name,STATUS:.status.conditions[-1].type")
        result = out or "ip-10-0-1-45  DiskPressure=True  87% disk"
    else:
        result = "ip-10-0-1-45  Ready  DiskPressure=True  87% disk"

    await _push("tool", tool="kubectl", cmd="get nodes", result=result)
    await asyncio.sleep(0.7)

    await _push("tool", tool="cloudwatch",
                cmd="GetMetricData node_filesystem_utilization",
                result="15m trend: 71% → 79% → 87%  (rising fast)")
    await asyncio.sleep(0.7)

    await _push("tool", tool="otel",
                cmd="GetMetricData checkoutservice.latency.p99",
                result="890ms  (baseline 120ms)  —  7.4× degradation")
    await asyncio.sleep(0.7)

    # Wrong hypothesis
    await _push("hypo", text="Hypothesis 1 — OTel Collector emptyDir buffer overflow")
    await asyncio.sleep(0.7)

    if REAL_CLUSTER:
        out = _kubectl("describe", "pod", "-n", NAMESPACE, "-l", "app=otelcol",
                       "--", "--tail=5")
        result = out or "emptyDir: 2.1GB / 5.0GB (42%)  within limits"
    else:
        result = "emptyDir: 2.1GB / 5.0GB (42%)  within limits"

    await _push("tool", tool="kubectl",
                cmd="describe pod otelcol-0 -n otel-demo", result=result)
    await asyncio.sleep(0.7)

    await _push("tool", tool="cloudwatch",
                cmd="GetMetricData otelcol_exporter_bytes",
                result="12.3 MB/min  (avg 11.8)  — normal range")
    await asyncio.sleep(0.7)

    await _push("reject", text="✗  Hypothesis 1 REJECTED — OTel Collector is healthy")
    await asyncio.sleep(0.5)
    await _push("agent", text="Re-planning… checking imageprovider")
    await asyncio.sleep(0.6)

    # Correct hypothesis
    await _push("hypo", text="Hypothesis 2 — imageprovider nginx verbose logging")
    await asyncio.sleep(0.6)

    if REAL_CLUSTER:
        out = _kubectl("set", "env", "--list", f"deployment/imageprovider", "-n", NAMESPACE)
        result = out or "NGINX_LOG_LEVEL=debug  ← MISCONFIGURATION"
    else:
        result = "NGINX_LOG_LEVEL=debug  ← MISCONFIGURATION"

    await _push("tool", tool="kubectl",
                cmd="describe deploy/imageprovider -n otel-demo", result=result)
    await asyncio.sleep(0.7)

    await _push("tool", tool="cloudwatch",
                cmd="GetMetricData container_fs_writes{imageprovider}",
                result="340.2 MB/8min  —  12× above baseline (28 MB)")
    await asyncio.sleep(0.7)

    await _push("confirm", text="✓  Root cause: imageprovider NGINX_LOG_LEVEL=debug")
    await asyncio.sleep(0.7)

    if REAL_CLUSTER:
        out = _kubectl("get", "pods", "-n", NAMESPACE,
                       "-o", "custom-columns=NAME:.metadata.name,PRIORITY:.spec.priorityClassName")
        result = out or "\n".join([
            "checkoutservice   payment-critical  (1000000)  PROTECTED",
            "paymentservice    payment-critical  (1000000)  PROTECTED",
            "imageprovider     infrastructure    (900000)   CANDIDATE",
            "adservice         background        (100000)   CANDIDATE",
            "loadgenerator     background        (100000)   CANDIDATE",
        ])
    else:
        result = "\n".join([
            "checkoutservice   payment-critical  (1000000)  PROTECTED",
            "paymentservice    payment-critical  (1000000)  PROTECTED",
            "cartservice       payment-critical  (1000000)  PROTECTED",
            "imageprovider     infrastructure    (900000)   CANDIDATE",
            "adservice         background        (100000)   CANDIDATE",
            "loadgenerator     background        (100000)   CANDIDATE",
        ])

    await _push("tool", tool="kubectl",
                cmd="get pods -n otel-demo -o custom-columns=NAME,PRIORITY",
                result=result)
    await asyncio.sleep(0.8)

    # Pause for human approval
    await _push("approval")
    _state["approved"].clear()
    _state["denied"] = False

    # Wait up to 10 minutes for approval
    try:
        await asyncio.wait_for(_state["approved"].wait(), timeout=600)
    except asyncio.TimeoutError:
        await _push("agent", text="Approval timed out. Aborting.")
        _state["active"] = False
        return

    if _state["denied"]:
        await _push("agent", text="Eviction denied by operator. Standing by.")
        _state["active"] = False
        return

    # Execute evictions
    await _push("agent", text="Approval received. Executing evictions...")
    await asyncio.sleep(0.5)

    evict_targets = ["loadgenerator", "imageprovider", "adservice"]
    for svc in evict_targets:
        if REAL_CLUSTER:
            pods_out = _kubectl("get", "pods", "-n", NAMESPACE, "-l", f"app={svc}",
                                "--no-headers", "-o", "custom-columns=NAME:.metadata.name")
            pod_name = pods_out.split("\n")[0].strip() if pods_out else f"{svc}-xxx"
            result = _kubectl("delete", "pod", pod_name, "-n", NAMESPACE)
        else:
            pod_name = f"{svc}-demo-xxx"
            result = f'pod "{pod_name}" deleted ✓'

        await _push("tool", tool="kubectl",
                    cmd=f"delete pod {pod_name} -n otel-demo", result=result)
        await _push("evict", service=svc)
        await asyncio.sleep(0.7)

    await asyncio.sleep(0.7)
    await _push("tool", tool="cloudwatch",
                cmd="GetMetricData node_filesystem_utilization",
                result="87% → 61%  (↓ 26%)  pressure relieved ✓")
    await asyncio.sleep(0.7)
    await _push("tool", tool="cloudwatch",
                cmd="GetMetricData checkoutservice.latency.p99",
                result="890ms → 118ms  back to baseline ✓")
    await asyncio.sleep(0.7)
    await _push("tool", tool="memory",
                cmd="store incident#2026-05-15-disk-pressure",
                result="saved: imageprovider debug logging → disk pressure")
    await asyncio.sleep(0.5)
    await _push("resolved", text="✓  Incident resolved  |  Node: 61%  |  Checkout p99: 118ms")
    _state["active"] = False


async def _run_simulation(fault: str) -> None:
    """Generic simulation for non-disk-pressure faults."""
    await _push("agent", text=f"Incident received — {fault} detected")
    await asyncio.sleep(1)
    await _push("agent", text="Investigating... (simulation mode)")
    await asyncio.sleep(2)
    await _push("confirm", text="✓  Root cause identified")
    await asyncio.sleep(0.5)
    await _push("approval")
    _state["approved"].clear()

    try:
        await asyncio.wait_for(_state["approved"].wait(), timeout=600)
    except asyncio.TimeoutError:
        _state["active"] = False
        return

    if _state["denied"]:
        await _push("agent", text="Action denied.")
        _state["active"] = False
        return

    await asyncio.sleep(1)
    await _push("resolved", text=f"✓  {fault} resolved")
    _state["active"] = False


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.post("/api/inject-fault")
async def inject_fault(req: FaultRequest):
    if req.type not in ALLOWED_FAULTS:
        raise HTTPException(400, f"Unknown fault type: {req.type}")
    if _state["active"]:
        raise HTTPException(409, "An incident is already in progress")

    _state["active"] = True
    _state["fault"] = req.type

    # Clear old logs
    while not _state["log_queue"].empty():
        _state["log_queue"].get_nowait()

    if REAL_CLUSTER:
        if req.type == "disk_pressure":
            proc = subprocess.Popen(
                ["bash", "../../fault-injection/trigger-disk-pressure.sh"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            await asyncio.sleep(1)  # let script start

    if req.type == "disk_pressure":
        asyncio.create_task(_run_disk_pressure())
    else:
        asyncio.create_task(_run_simulation(req.type))

    return {"status": "ok", "fault": req.type}


@app.post("/api/approve")
async def approve():
    if not _state["active"]:
        raise HTTPException(400, "No active incident")
    _state["denied"] = False
    _state["approved"].set()
    return {"status": "approved"}


@app.post("/api/deny")
async def deny():
    if not _state["active"]:
        raise HTTPException(400, "No active incident")
    _state["denied"] = True
    _state["approved"].set()
    return {"status": "denied"}


@app.post("/api/reset")
async def reset():
    _state["active"] = False
    _state["fault"] = None
    _state["denied"] = False
    _state["approved"] = asyncio.Event()
    while not _state["log_queue"].empty():
        _state["log_queue"].get_nowait()

    if REAL_CLUSTER:
        subprocess.Popen(
            ["bash", "../../fault-injection/reset-cluster.sh"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

    return {"status": "reset"}


@app.get("/api/agent-stream")
async def agent_stream():
    async def generator():
        # Keep-alive ping every 15s
        last_ping = time.monotonic()
        while True:
            try:
                msg = await asyncio.wait_for(_state["log_queue"].get(), timeout=15)
                yield {"data": msg}
            except asyncio.TimeoutError:
                # Send a comment as keep-alive
                yield {"comment": "ping"}
                last_ping = time.monotonic()

    return EventSourceResponse(generator())


@app.get("/api/cluster/pods")
async def cluster_pods():
    if not REAL_CLUSTER:
        # Return mock data for demo
        mock_pods = [
            {"name": "checkoutservice-8f3b1a-abc", "status": "Running",  "restarts": 0,
             "age": "2d", "node": "ip-10-0-1-45", "priority": "payment-critical", "critical": True},
            {"name": "paymentservice-9a2c4d-def",  "status": "Running",  "restarts": 0,
             "age": "2d", "node": "ip-10-0-1-45", "priority": "payment-critical", "critical": True},
            {"name": "cartservice-3e7f5b-ghi",     "status": "Running",  "restarts": 0,
             "age": "2d", "node": "ip-10-0-1-46", "priority": "payment-critical", "critical": True},
            {"name": "imageprovider-5c8a2d-jkl",   "status": "Running",  "restarts": 0,
             "age": "2d", "node": "ip-10-0-1-45", "priority": "infrastructure",   "critical": False},
            {"name": "adservice-3f7b1a-mno",       "status": "Running",  "restarts": 0,
             "age": "2d", "node": "ip-10-0-1-46", "priority": "background",       "critical": False},
            {"name": "loadgenerator-7d4f9c-pqr",   "status": "Running",  "restarts": 0,
             "age": "2d", "node": "ip-10-0-1-47", "priority": "background",       "critical": False},
            {"name": "recommendationservice-1b-stu","status": "Running",  "restarts": 1,
             "age": "2d", "node": "ip-10-0-1-47", "priority": "background",       "critical": False},
            {"name": "frontend-2d6a3e-vwx",        "status": "Running",  "restarts": 0,
             "age": "2d", "node": "ip-10-0-1-46", "priority": "user-facing",      "critical": False},
            {"name": "productcatalogservice-4f-yz", "status": "Running", "restarts": 0,
             "age": "2d", "node": "ip-10-0-1-45", "priority": "user-facing",      "critical": True},
        ]
        return {"pods": mock_pods}

    try:
        out = _kubectl(
            "get", "pods", "-n", NAMESPACE, "--no-headers",
            "-o", "custom-columns="
                  "NAME:.metadata.name,"
                  "STATUS:.status.phase,"
                  "RESTARTS:.status.containerStatuses[0].restartCount,"
                  "NODE:.spec.nodeName,"
                  "PRIORITY:.spec.priorityClassName",
        )
        pods = []
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 5:
                continue
            name = parts[0]
            svc = name.split("-")[0]
            pods.append({
                "name": name,
                "status": parts[1],
                "restarts": int(parts[2]) if parts[2].isdigit() else 0,
                "age": "—",
                "node": parts[3],
                "priority": parts[4] if len(parts) > 4 else "—",
                "critical": svc in CRITICAL_SERVICES,
            })
        return {"pods": pods}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/health")
async def health():
    return {"status": "ok", "real_cluster": REAL_CLUSTER, "active_incident": _state["active"]}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
