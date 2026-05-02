# Demo Workload End-to-End Test Report — 2026-05-03

**Branch:** feat/demo-workload-scaffold  
**Cluster:** retail-prod-eks-use1 (otel-demo-prod, us-east-1)  
**AWS account:** 637039075925  
**Agent URL:** http://k8s-k8sagent-k8sagent-1e7cc95eb0-2686e228b2e2ace3.elb.us-east-1.amazonaws.com:8080/trigger

---

## Summary

All 13 planned steps completed successfully. Three bugs were found and fixed during the run (ADOT exporter deprecation, X-Ray service-map visibility, and synthesizer CW metric recording). Two full destroy+redeploy cycles passed. The cluster is left running in healthy scenario.

---

## Steps and Outcomes

| Step | Description | Result |
|------|-------------|--------|
| 1 | Sanity check (AWS creds, Docker buildx, working tree) | Pass |
| 2 | Build + push synthesizer image | Pass — v1→v4→v5 through fixes |
| 3 | Deploy cluster + agent (`infra/deploy.sh`) | Pass — ~13 min first run |
| 4 | Deploy demo AWS stack (`demo/deploy-aws.sh`) | Pass |
| 5 | Deploy demo k8s manifests (`demo/deploy-k8s.sh`) | Pass |
| 6 | X-Ray service map — 5 services visible | Pass (after 2-part fix) |
| 7 | CloudWatch metrics populate (`RetailProd/Services`) | Pass |
| 8 | Smoke-test alarm path (`./demo test-alarm`) | Pass |
| 9 | Natural spike → alarm fire → Lambda → agent | Pass — alarm at 23:10:59 UTC |
| 10 | Tear-down rehearsal (2 × destroy + redeploy) | Pass — both cycles clean |
| 11 | Test report | This file |
| 12 | Commit fixes | See commits below |
| 13 | Leave running (healthy scenario) | Done |

---

## Bugs Found and Fixed

### Bug 1 — ADOT collector CrashLoopBackOff

**Symptom:** `latency-synthesizer` pod stuck in CrashLoopBackOff immediately after first deploy. ADOT sidecar log: `unknown exporter "logging"`.

**Root cause:** ADOT v0.47.0 removed the `logging` exporter. The collector config in `manifests/20-collector-sidecar-config.yaml` still referenced it.

**Fix:** Changed `logging` → `debug` exporter throughout the config. Applied configmap + `kubectl rollout restart`.

**File:** `demo-workload/manifests/20-collector-sidecar-config.yaml`

---

### Bug 2 — X-Ray service map only showed frontendservice

**Symptom:** AWS X-Ray console service map showed a single `frontendservice` node after traces were flowing. No downstream services visible.

**Root cause (Part A):** All child service spans used `SpanKind.INTERNAL`. X-Ray treats INTERNAL spans as subsegments of the parent segment → they appear folded under `frontendservice`, not as separate service nodes.

**Partial fix:** Changed all spans to `SpanKind.SERVER`.

**Root cause (Part B):** Even with SERVER kind, the child spans were started with the in-process OTel context (`ot_context.get_current()`). ADOT determines segment vs subsegment based on whether the parent span is remote (different process). In-process propagation means the parent is always local → all spans become subsegments of the same segment.

**Full fix:** At each service call boundary, inject the current span context into a W3C traceparent carrier with `TraceContextTextMapPropagator().inject()`, then extract it with `.extract()`. Passing this remote context as the parent of the child span makes ADOT treat each service as a separate origin segment → all 5 services appear as distinct nodes in the X-Ray service map.

**Files:** `demo-workload/synthesizer/synth.py`

---

### Bug 3 — `./demo test-alarm` used wrong AWS region

**Symptom:** `aws cloudwatch set-alarm-state` returned `ResourceNotFoundException`. The alarm existed but the command ran against the wrong region.

**Root cause:** The `demo` script had no mechanism to set `AWS_PROFILE` or `AWS_REGION`. All `aws` commands used shell defaults.

**Fix:** Added `.env` sourcing and `export AWS_PROFILE/AWS_REGION` at the top of the script. Added `--region "$AWS_REGION"` to every `aws cloudwatch`, `aws logs`, and `aws sqs` invocation in the script.

**File:** `demo-workload/demo/demo`

---

### Bug 4 — Spike alarm never fired naturally (checkoutservice P99 ~37ms during spike)

**Symptom:** `./demo spike` switched the synthesizer to spike scenario. CloudWatch `checkoutservice LatencyP99` stayed at ~35ms. Alarm threshold is 300ms. Alarm never fired.

**Root cause:** `execute_call` called `METRICS.record(call.service, own_latency_ms, ...)` where `own_latency_ms` is sampled from the service's own latency distribution. For checkoutservice that is ~15-20ms. The slow paymentservice latency (~350ms in spike mode) was not included — it is a downstream call, not the service's own processing time.

A real checkout request takes ~370ms end-to-end during a spike (15ms own + 350ms waiting for payment), which is what CloudWatch should show for checkoutservice. The synthesizer was under-reporting.

**Fix:** Changed `execute_call` to record wall-clock total latency (span start to span end, including all child calls and own sleep) rather than the sampled own-processing time:

```python
start_wall = time.monotonic()
# ... span, children, own sleep ...
total_latency_ms = (time.monotonic() - start_wall) * 1000.0
METRICS.record(call.service, total_latency_ms, errored=...)
return total_latency_ms, (errored or child_errored)
```

Built as v5, redeployed. Spike peak observed: **2023ms** (23:10:30 UTC period).

**File:** `demo-workload/synthesizer/synth.py`

---

## Spike Metrics — checkoutservice LatencyP99 (30s periods, UTC)

| Time (UTC) | P99 (ms) | State |
|------------|----------|-------|
| 23:08:00 | 31.5 | Healthy baseline |
| 23:08:30 | 166.7 | Transitional (pod restarting) |
| 23:09:30 | 1422.3 | **SPIKE** |
| 23:10:00 | 1420.9 | SPIKE |
| 23:10:30 | **2023.2** | SPIKE peak |
| 23:10:59 | — | **ALARM fired** |
| 23:11:00 | 1870.9 | SPIKE (reading before healthy restart) |
| 23:11:30 | 171.6 | Recovery |
| 23:12:00 | 172.0 | Recovery |
| 23:12:25 | — | **Alarm cleared → OK** |

Alarm config: threshold=300ms, period=30s, evaluation_periods=2.  
Time from spike activation to alarm fire: **89 seconds**.

---

## End-to-End Alarm Path — Verified Events

| Time (UTC) | Event |
|------------|-------|
| 23:09:31 | Synthesizer v5 started (SCENARIO=spike) |
| 23:09:41 | First CW publish (20 metric entries) |
| 23:10:40 | Lambda INIT_START |
| 23:10:41 | Lambda invoked: agent responded 200 |
| 23:10:41 | Investigation started — `thread_ts: 1777763441.552309` |
| 23:10:59 | Alarm state confirmed ALARM |
| 23:12:25 | Alarm returned to OK after `./demo healthy` |

Lambda duration: 456ms (billed 603ms, including 146ms cold-start init).  
Previous smoke-test Slack `thread_ts` values: `1777762764.088659`, `1777762897.643779`.

---

## Tear-down Rehearsal

Two full cycles of `destroy-k8s.sh → destroy-aws.sh → deploy-aws.sh → deploy-k8s.sh` were run without error. Each CFN stack delete completes in ~1-2 min. Each redeploy completes in ~2-3 min. The cluster itself (`k8s-agent-cluster`) was not destroyed between cycles.

---

## Code Changes (this branch)

| File | Change |
|------|--------|
| `demo-workload/manifests/20-collector-sidecar-config.yaml` | `logging` → `debug` exporter |
| `demo-workload/synthesizer/synth.py` | W3C propagator for remote parent simulation; SERVER span kind for all services; wall-clock total latency for CW metrics |
| `demo-workload/demo/demo` | `.env` sourcing; `AWS_PROFILE`/`AWS_REGION` exports; `--region` flag on all aws CLI calls |
| `demo-workload/demo/.env` | Created from `.env.example`; `SYNTHESIZER_IMAGE=dipinthomas2003/latency-synthesizer:v5` |

---

## Cleanup Commands

The cluster is left running. To tear down completely:

```bash
cd demo-workload
bash demo/destroy-k8s.sh
bash demo/destroy-aws.sh
# If tearing down the cluster + agent too:
bash infra/destroy.sh
```

Approximate burn rate while running:
- EKS Auto Mode control plane: ~$0.10/hr
- 1-2 EC2 nodes (t3.medium): ~$0.07/hr each
- NAT Gateway: ~$0.045/hr + data
- ALB (agent service): ~$0.025/hr
- CloudWatch metrics (StorageResolution=1): ~$0.005/hr

**Estimated total: ~$0.30–0.40/hr**
