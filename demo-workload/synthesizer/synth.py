"""Trace synthesizer for the retail-prod demo.

Pretends to be five services calling each other. Emits OTLP traces via a
sidecar ADOT collector (which exports to X-Ray). Also publishes per-service
CloudWatch metrics every 10 s.

Scenarios are switched live via the SCENARIO env var. The deployment restart
that follows `kubectl set env` is acceptable — there's no state to preserve.

Run locally with `python synth.py` (uses console exporter when OTLP_ENDPOINT
is unset, which makes step-1 verification of "sane latency numbers in stdout"
trivial).
"""

from __future__ import annotations

import logging
import os
import random
import signal
import sys
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
)
from opentelemetry.trace import SpanKind, Status, StatusCode

import distributions
import topology

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("synth")

SERVICE_NAMESPACE = "retail-prod-eks-use1.shop-prod"
DEPLOYMENT_ENV = "production"
SERVICE_VERSIONS = {
    "frontendservice": "1.42.0",
    "checkoutservice": "2.7.3",
    "cartservice": "1.18.4",
    "productcatalogservice": "3.2.1",
    "paymentservice": "1.9.7",
}

OTLP_ENDPOINT = os.environ.get("OTLP_ENDPOINT", "")  # e.g. "http://localhost:4317"
RPS = float(os.environ.get("RPS", "15"))
WORKERS = int(os.environ.get("WORKERS", "32"))
SCENARIO = os.environ.get("SCENARIO", "healthy").lower()
RECOVERY_SECONDS = float(os.environ.get("RECOVERY_SECONDS", "30"))
CW_NAMESPACE = os.environ.get("CW_NAMESPACE", "RetailProd/Services")
CW_PUBLISH_INTERVAL_S = float(os.environ.get("CW_PUBLISH_INTERVAL_S", "10"))
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
ENABLE_CPU_BURN = os.environ.get("ENABLE_CPU_BURN", "false").lower() == "true"

# Step-1 of the build plan ends *before* CloudWatch wiring.
# Default to off so `python synth.py` runs with zero AWS dependencies.
ENABLE_CW = os.environ.get("ENABLE_CW", "false").lower() == "true"


def build_tracer_providers() -> dict:
    """One TracerProvider per service so each span carries its own service.name."""
    providers: dict[str, TracerProvider] = {}
    if OTLP_ENDPOINT:
        # Lazy import — keeps `python synth.py` working in environments
        # without the OTLP exporter installed.
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        exporter = OTLPSpanExporter(endpoint=OTLP_ENDPOINT, insecure=True)
        log.info("OTLP exporter -> %s", OTLP_ENDPOINT)
    else:
        exporter = ConsoleSpanExporter()
        log.info("No OTLP_ENDPOINT set — using ConsoleSpanExporter (stdout)")

    for svc in topology.ALL_SERVICES:
        resource = Resource.create({
            "service.name": svc,
            "service.namespace": SERVICE_NAMESPACE,
            "deployment.environment": DEPLOYMENT_ENV,
            "service.version": SERVICE_VERSIONS[svc],
            "cloud.provider": "aws",
            "cloud.region": AWS_REGION,
            "k8s.cluster.name": "retail-prod-eks-use1",
            "k8s.namespace.name": "shop-prod",
        })
        tp = TracerProvider(resource=resource)
        # ConsoleSpanExporter doesn't accept BatchSpanProcessor kwargs the same
        # way; pass them anyway — they're ignored for the console exporter.
        tp.add_span_processor(
            BatchSpanProcessor(
                exporter,
                schedule_delay_millis=1000,
                max_export_batch_size=64,
            )
        )
        providers[svc] = tp
    return providers


@dataclass
class Metrics:
    """Rolling per-service samples for CloudWatch publishing."""
    samples_ms: deque
    error_count: int = 0
    total_count: int = 0


class MetricsRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._svc: dict[str, Metrics] = defaultdict(
            lambda: Metrics(samples_ms=deque(maxlen=4096))
        )

    def record(self, svc: str, latency_ms: float, errored: bool) -> None:
        with self._lock:
            m = self._svc[svc]
            m.samples_ms.append(latency_ms)
            m.total_count += 1
            if errored:
                m.error_count += 1

    def drain(self) -> dict:
        """Snapshot current samples and reset counters. Returns per-service stats."""
        out = {}
        with self._lock:
            for svc, m in self._svc.items():
                samples = sorted(m.samples_ms)
                if not samples:
                    continue
                p50 = samples[len(samples) // 2]
                p99_idx = max(0, int(len(samples) * 0.99) - 1)
                p99 = samples[p99_idx]
                err_pct = (m.error_count / m.total_count * 100.0) if m.total_count else 0.0
                out[svc] = {
                    "p50_ms": p50,
                    "p99_ms": p99,
                    "error_pct": err_pct,
                    "count": m.total_count,
                }
                m.samples_ms.clear()
                m.error_count = 0
                m.total_count = 0
        return out


METRICS = MetricsRegistry()


# ---- live scenario state -------------------------------------------------
_recover_started_at: float | None = None


def current_distributions() -> dict:
    """Return the latency distribution dict for the active scenario, with
    interpolation if recovering."""
    if SCENARIO == "recovering":
        global _recover_started_at
        if _recover_started_at is None:
            _recover_started_at = time.monotonic()
        elapsed = time.monotonic() - _recover_started_at
        t = min(1.0, elapsed / max(RECOVERY_SECONDS, 0.001))
        return distributions.for_scenario("recovering", t)
    return distributions.for_scenario(SCENARIO)


# ---- request execution ---------------------------------------------------

def _burn_cpu_ms(ms: float) -> None:
    """Tight loop burning approximately `ms` of CPU. Used during spike on
    paymentservice so noisy-neighbor co-location can actually throttle it
    (see DEMO_WORKLOAD_PLAN.md §8.13 Option A)."""
    end = time.monotonic() + ms / 1000.0
    x = 0
    while time.monotonic() < end:
        for _ in range(2000):
            x = (x * 2654435761) & 0xFFFFFFFF


def execute_call(
    call: topology.Call,
    providers: dict,
    parent_ctx,
    dists: dict,
) -> tuple[float, bool]:
    """Execute one call in the topology recursively. Returns (own latency, errored)."""
    tracer = providers[call.service].get_tracer("synth")
    own_params = dists[call.service]
    own_latency_ms = max(1.0, distributions.sample_ms(own_params))

    with tracer.start_as_current_span(
        call.operation,
        context=parent_ctx,
        kind=SpanKind.SERVER if parent_ctx is None else SpanKind.INTERNAL,
        attributes={
            "http.method": call.operation.split(" ")[0],
            "http.route": call.operation.split(" ")[1] if " " in call.operation else call.operation,
            "service.namespace": SERVICE_NAMESPACE,
        },
    ) as span:
        # Optional real CPU burn on paymentservice during spike (Option A).
        if (
            ENABLE_CPU_BURN
            and call.service == "paymentservice"
            and SCENARIO == "spike"
        ):
            _burn_cpu_ms(min(own_latency_ms * 0.1, 80.0))

        # Recurse into children, executing them sequentially under our span.
        from opentelemetry import context as ot_context
        new_ctx = ot_context.get_current()

        child_errored = False
        for child in call.children:
            _, c_err = execute_call(child, providers, new_ctx, dists)
            child_errored = child_errored or c_err

        # Sleep for our own work portion of the latency.
        time.sleep(own_latency_ms / 1000.0)

        errored = False
        # Spike-mode error injection: 3% of checkoutservice calls fail
        # (timeouts on slow paymentservice). paymentservice itself never
        # errors — that's the demo's whole point.
        if SCENARIO == "spike" and call.service == "checkoutservice":
            if random.random() < 0.03:
                errored = True
                span.set_status(Status(StatusCode.ERROR, "upstream timeout"))
                span.set_attribute("http.status_code", 504)

        if errored or child_errored:
            METRICS.record(call.service, own_latency_ms, errored=True)
        else:
            span.set_attribute("http.status_code", 200)
            METRICS.record(call.service, own_latency_ms, errored=False)

        return own_latency_ms, (errored or child_errored)


def run_one_request(providers: dict) -> None:
    try:
        dists = current_distributions()
        execute_call(topology.ROOT, providers, parent_ctx=None, dists=dists)
    except Exception:  # don't kill the worker
        log.exception("request failed")


# ---- CloudWatch publisher -------------------------------------------------

def cw_publisher_loop() -> None:
    if not ENABLE_CW:
        log.info("CloudWatch publishing disabled (ENABLE_CW=false)")
        return
    import boto3  # type: ignore[import-not-found]

    cw = boto3.client("cloudwatch", region_name=AWS_REGION)
    log.info("CloudWatch publisher loop -> namespace=%s every %ss",
             CW_NAMESPACE, CW_PUBLISH_INTERVAL_S)
    while True:
        time.sleep(CW_PUBLISH_INTERVAL_S)
        snapshot = METRICS.drain()
        if not snapshot:
            continue
        metric_data = []
        for svc, stats in snapshot.items():
            dims = [{"Name": "Service", "Value": svc}]
            for name, value, unit in (
                ("LatencyP50", stats["p50_ms"], "Milliseconds"),
                ("LatencyP99", stats["p99_ms"], "Milliseconds"),
                ("ErrorRate", stats["error_pct"], "Percent"),
                ("RequestCount", stats["count"], "Count"),
            ):
                metric_data.append({
                    "MetricName": name,
                    "Dimensions": dims,
                    "Value": value,
                    "Unit": unit,
                    "StorageResolution": 1,
                })
        try:
            # PutMetricData has a 1000-entry limit per call; we're well under.
            cw.put_metric_data(Namespace=CW_NAMESPACE, MetricData=metric_data)
            log.info("published %d CW metric entries", len(metric_data))
        except Exception:
            log.exception("PutMetricData failed (continuing)")


# ---- request scheduler ----------------------------------------------------

def scheduler_loop(providers: dict, executor: ThreadPoolExecutor) -> None:
    """Leaky-bucket scheduler maintaining target RPS."""
    interval = 1.0 / max(RPS, 0.01)
    next_due = time.monotonic()
    log.info("scheduler RPS=%s workers=%s scenario=%s", RPS, WORKERS, SCENARIO)
    while True:
        now = time.monotonic()
        if now < next_due:
            time.sleep(next_due - now)
        executor.submit(run_one_request, providers)
        next_due += interval
        # If we've fallen behind by more than a second, skip ahead so we
        # don't accumulate a forever-growing queue.
        if next_due < time.monotonic() - 1.0:
            next_due = time.monotonic()


# ---- main ----------------------------------------------------------------

def install_signal_handlers() -> None:
    def _exit(signum, frame):
        log.info("signal %s — exiting", signum)
        sys.exit(0)
    signal.signal(signal.SIGINT, _exit)
    signal.signal(signal.SIGTERM, _exit)


def main() -> None:
    install_signal_handlers()
    providers = build_tracer_providers()
    # Set the frontendservice provider as the default so any spans we forget
    # to scope explicitly still have a sane resource.
    trace.set_tracer_provider(providers["frontendservice"])

    executor = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="req")
    threading.Thread(target=cw_publisher_loop, daemon=True, name="cw").start()
    scheduler_loop(providers, executor)


if __name__ == "__main__":
    main()
