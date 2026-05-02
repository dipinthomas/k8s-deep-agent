# demo-workload

Synthetic e-commerce checkout workload that exercises the [k8s-deep-agent](../)
in this repo end-to-end. Emits OTel traces to AWS X-Ray, publishes
CloudWatch metrics, and drives a CloudWatch alarm that fans out via SNS
to AWS Chatbot (Slack card) and to the agent's `/trigger` endpoint
(investigation thread).

The agent itself stays application-agnostic — see [../CLAUDE.md](../CLAUDE.md).
This subdirectory holds everything specific to the demo workload: the
synthesizer, k8s manifests, CloudFormation, Lambda handler, and the
control scripts.

See [DEMO_WORKLOAD_PLAN.md](../.claude/worktrees/hungry-ellis-510c69/DEMO_WORKLOAD_PLAN.md)
for the full build spec.

---

## What's in this scaffold (steps 1-8 of the plan)

- **synthesizer/** — Python service that pretends to be five services
  (frontendservice, checkoutservice, cartservice, productcatalogservice,
  paymentservice) calling each other. Emits OTLP to a sidecar collector
  and publishes per-service CloudWatch metrics every 10s.
- **manifests/** — namespace, IRSA-bound ServiceAccounts, ADOT collector
  ConfigMap, and synthesizer Deployment with the collector as a sidecar.
- **aws/cloudformation/demo-workload.yaml** — single CFN stack containing:
  - IRSA roles (synthesizer + reserved standalone collector)
  - X-Ray 100% sampling rule
  - SNS topic `retail-prod-incidents` (+ access policy for CloudWatch)
  - CloudWatch alarm `checkoutservice-p99-latency-high`
  - Alarm-to-agent Lambda (Python 3.12, inlined handler) + DLQ + role
  - SNS subscription + invoke permission
  - AWS Chatbot Slack channel config (conditional on Slack params)
- **aws/lambda/handler.py** — translates SNS-from-CloudWatch into the
  agent's `/trigger` HTTP contract. Inlined into the template at deploy time.
- **aws/chatbot-config.md** — one-time Slack workspace auth steps.
- **demo/** — deploy / destroy / build scripts + `demo/demo` entrypoint.

Not yet built:
- Trace backfill (step 6 — CloudWatch metric backfill works; trace
  backfill is a TODO)
- Noisy-neighbor pod (step 9)
- Real-CPU mode in synthesizer (step 10)

---

## One-time setup

1. **Cluster stack.** Confirm the agent's cluster CFN stack is deployed.
   The default name is `k8s-agent-cluster` (override via `CLUSTER_STACK_NAME`
   in `demo/.env`). This stack must export `OidcProviderArn` and
   `OidcIssuerUrl` (it does — see [cluster.yaml](../infra/cloudformation/cluster.yaml)).

2. **Container registry.** Pick where you'll push the synthesizer image and
   set `SYNTHESIZER_IMAGE` in `demo/.env`. Docker Hub works
   (`dipinthomas2003/latency-synthesizer:v1`); ECR also works.

3. **buildx builder.** One-time:
   ```bash
   docker buildx create --name multiarch-builder --use
   ```

4. **AWS SSO session.**
   ```bash
   aws sso login --profile fernhub
   ```

5. **demo/.env.**
   ```bash
   cp demo/.env.example demo/.env
   $EDITOR demo/.env
   ```

---

## Deploy

```bash
# Build & push the synthesizer image (multi-arch).
bash demo/build-image.sh

# Create AWS resources (IRSA roles + X-Ray sampling rule).
bash demo/deploy-aws.sh

# Apply Kubernetes manifests (namespace, SAs, collector config, synth Deployment).
bash demo/deploy-k8s.sh
```

Verify:
```bash
# Pod healthy
kubectl -n shop-prod get pods

# Synth emitting requests
kubectl -n shop-prod logs -l app=latency-synthesizer -c synth -f

# Collector exporting (look for "TracesExporter" log lines)
kubectl -n shop-prod logs -l app=latency-synthesizer -c adot-collector -f
```

Then within ~60s, in the AWS console → X-Ray → Service Map (last 5 min):
all five services should appear with the call graph from
[synthesizer/topology.py](synthesizer/topology.py).

CloudWatch metrics under namespace `RetailProd/Services` populate every
10s with `LatencyP50`, `LatencyP99`, `ErrorRate`, `RequestCount` per
service.

---

## Live scenario switch

```bash
./demo/demo healthy       # baseline
./demo/demo spike         # paymentservice slow + 3% checkout errors
./demo/demo status        # synth deployments + alarm state
./demo/demo logs          # tail synth container logs
./demo/demo lambda-logs   # tail alarm-to-agent Lambda logs
./demo/demo dlq           # DLQ depth
./demo/demo test-alarm    # force alarm into ALARM state to smoke-test SNS->Lambda->agent
```

A spike pushes paymentservice p50 to ~350ms (lognormal sigma 0.6) plus
a 5% bimodal tail of +400-800ms. checkoutservice p99 climbs over the
300ms threshold, alarm fires after `period × evaluation_periods` ≈ 60s,
SNS fans out to:
- The Lambda → POST agent `/trigger` → agent posts an investigation thread
- AWS Chatbot → posts an alarm card to `#retail-prod-incidents`

A `kubectl set env` triggers a pod restart (Recreate strategy, single
replica), so expect a 10-30s gap in traces during the switch.

---

## Local development (no AWS, no cluster)

```bash
cd synthesizer
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python synth.py    # OTLP_ENDPOINT unset → ConsoleSpanExporter, ENABLE_CW=false
```

Spans print to stdout; latency numbers should look sane (frontendservice
~5ms, checkoutservice ~15ms, paymentservice ~40ms in healthy mode).

Switch scenario without restart? Not supported yet — set `SCENARIO=spike`
in the env and re-run.

---

## Tear down

```bash
bash demo/destroy-k8s.sh    # delete pods first (so they stop assuming the IRSA role)
bash demo/destroy-aws.sh    # then delete the CFN stack
```

Order matters — see DEMO_WORKLOAD_PLAN.md §6.9.

---

## Known gaps for tomorrow's testing

- **Trace backfill** is a TODO — `backfill.py` only handles CloudWatch
  metrics. Trace history will start when the synthesizer is deployed.
- **Sampling rule region**: hard-coded to `us-east-1` in the collector
  ConfigMap. If `AWS_REGION` differs, edit
  [manifests/20-collector-sidecar-config.yaml](manifests/20-collector-sidecar-config.yaml).
- **Image registry permissions**: `deploy-k8s.sh` does not handle private
  ECR pull secrets. Either use a public image, or add an
  `imagePullSecrets` entry to the synthesizer Deployment manually.
- **EKS Auto Mode + IRSA**: the agent cluster runs EKS Auto Mode (per
  [infra/deploy.sh](../infra/deploy.sh)). IRSA still works
  there, but if the SA annotation isn't picked up, double-check the OIDC
  provider URL in the stack outputs.
- **Agent URL resolution**: `deploy-aws.sh` looks up the agent LB
  hostname via `kubectl get svc k8s-agent -n k8s-agent`. If the agent
  isn't deployed yet, deploy it first or set `AGENT_URL=` in `demo/.env`
  manually. The Lambda needs a routable URL at stack-update time.
- **Chatbot is optional**: leaving `SLACK_WORKSPACE_ID` empty skips
  Chatbot. The alarm + Lambda + agent path still works (you'll just
  miss the Chatbot card in Slack — the agent's own ack still posts).
