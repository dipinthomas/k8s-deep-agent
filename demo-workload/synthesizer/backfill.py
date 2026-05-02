"""Backdated trace + CloudWatch metric emitter.

Run once before a demo to populate "yesterday was fine" history. The X-Ray
awsxray exporter accepts segment start times up to 30 days in the past;
PutMetricData accepts timestamps up to 14 days back.

Usage (in-cluster):
    kubectl run backfill-$(date +%s) \\
        --image=<registry>/latency-synthesizer:latest \\
        --restart=Never --rm -i --tty=false \\
        --command -- python backfill.py --hours 24
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import distributions
import topology

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backfill")

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
CW_NAMESPACE = os.environ.get("CW_NAMESPACE", "RetailProd/Services")


def emit_cw_backfill(hours: int) -> None:
    """Emit 1-minute-bucket metrics back `hours` hours."""
    import boto3  # type: ignore[import-not-found]

    cw = boto3.client("cloudwatch", region_name=AWS_REGION)
    healthy = distributions.HEALTHY
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = end - timedelta(hours=hours)

    bucket = start
    sent = 0
    batch: list[dict] = []
    while bucket < end:
        for svc in topology.ALL_SERVICES:
            params = healthy[svc]
            # Healthy steady-state: p50 ≈ params.p50_ms, p99 ≈ p50 * e^(2.33*sigma)
            import math
            p50 = params.p50_ms
            p99 = params.p50_ms * math.exp(2.33 * params.sigma)
            for name, value, unit in (
                ("LatencyP50", p50, "Milliseconds"),
                ("LatencyP99", p99, "Milliseconds"),
                ("ErrorRate", 0.0, "Percent"),
                ("RequestCount", 15.0 * 60.0 / 5.0, "Count"),  # 15rps spread across 5 services
            ):
                batch.append({
                    "MetricName": name,
                    "Dimensions": [{"Name": "Service", "Value": svc}],
                    "Value": value,
                    "Unit": unit,
                    "Timestamp": bucket,
                    "StorageResolution": 60,
                })
        if len(batch) >= 800:
            cw.put_metric_data(Namespace=CW_NAMESPACE, MetricData=batch)
            sent += len(batch)
            batch = []
        bucket += timedelta(minutes=1)
    if batch:
        cw.put_metric_data(Namespace=CW_NAMESPACE, MetricData=batch)
        sent += len(batch)
    log.info("backfilled %d metric entries from %s to %s", sent, start, end)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--hours", type=int, default=24)
    p.add_argument("--no-traces", action="store_true",
                   help="Skip backfilling traces (CloudWatch only)")
    args = p.parse_args()

    log.info("backfilling %d hours of CloudWatch metrics", args.hours)
    emit_cw_backfill(args.hours)

    # Trace backfill is more involved (requires constructing OTel spans with
    # historical start_time and bypassing BatchSpanProcessor's sequencing).
    # Step 6 of the build plan covers this; left as a TODO for now so step 1-3
    # remains end-to-end testable.
    if not args.no_traces:
        log.warning("trace backfill not implemented yet (step 6 of plan)")


if __name__ == "__main__":
    main()
