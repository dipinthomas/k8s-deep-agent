"""Latency distributions per scenario.

Real service latency is right-skewed, so we use lognormal. The `spike`
scenario shifts paymentservice's p50 way up and adds a bimodal tail
(5% of calls take an extra 400-800ms uniform). This is what makes the
trace duration histogram in X-Ray bimodal during a spike.
"""

import math
import random
from dataclasses import dataclass


@dataclass(frozen=True)
class LatencyParams:
    p50_ms: float
    sigma: float
    bimodal_pct: float = 0.0
    bimodal_extra_min_ms: float = 0.0
    bimodal_extra_max_ms: float = 0.0


def sample_ms(params: LatencyParams) -> float:
    """Sample a single latency value from a lognormal, optionally bimodal."""
    # lognormal where median (= e^mu) equals p50_ms
    mu = math.log(max(params.p50_ms, 0.001))
    base = random.lognormvariate(mu, params.sigma)
    if params.bimodal_pct > 0 and random.random() < params.bimodal_pct:
        base += random.uniform(params.bimodal_extra_min_ms, params.bimodal_extra_max_ms)
    return base


HEALTHY = {
    "frontendservice": LatencyParams(p50_ms=5, sigma=0.3),
    "checkoutservice": LatencyParams(p50_ms=15, sigma=0.4),
    "cartservice": LatencyParams(p50_ms=8, sigma=0.3),
    "productcatalogservice": LatencyParams(p50_ms=20, sigma=0.4),
    "paymentservice": LatencyParams(p50_ms=40, sigma=0.4),
}

SPIKE = {
    "frontendservice": LatencyParams(p50_ms=5, sigma=0.3),
    "checkoutservice": LatencyParams(p50_ms=15, sigma=0.4),
    "cartservice": LatencyParams(p50_ms=8, sigma=0.3),
    "productcatalogservice": LatencyParams(p50_ms=20, sigma=0.4),
    "paymentservice": LatencyParams(
        p50_ms=350,
        sigma=0.6,
        bimodal_pct=0.05,
        bimodal_extra_min_ms=400,
        bimodal_extra_max_ms=800,
    ),
}


def interpolated(t: float) -> dict:
    """Linear interpolation from SPIKE -> HEALTHY for `recovering` scenario.

    t in [0, 1]. t=0 returns SPIKE, t=1 returns HEALTHY.
    """
    out = {}
    for svc, healthy_p in HEALTHY.items():
        spike_p = SPIKE[svc]
        out[svc] = LatencyParams(
            p50_ms=spike_p.p50_ms + (healthy_p.p50_ms - spike_p.p50_ms) * t,
            sigma=spike_p.sigma + (healthy_p.sigma - spike_p.sigma) * t,
            bimodal_pct=spike_p.bimodal_pct * (1 - t),
            bimodal_extra_min_ms=spike_p.bimodal_extra_min_ms,
            bimodal_extra_max_ms=spike_p.bimodal_extra_max_ms,
        )
    return out


def for_scenario(scenario: str, recovery_progress: float = 0.0) -> dict:
    if scenario == "healthy":
        return HEALTHY
    if scenario == "spike":
        return SPIKE
    if scenario == "recovering":
        return interpolated(recovery_progress)
    raise ValueError(f"unknown scenario: {scenario}")
