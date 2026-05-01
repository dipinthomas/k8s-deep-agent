# Demo Workload — Build Plan

This document is a self-contained build spec for a synthetic demo workload that exercises the k8s-deep-agent end-to-end. It is intended to be handed to a builder agent or engineer and acted on without further context.

The artifact described here is **not** part of the agent repo. The agent stays generic. This workload lives in a sibling repo (suggested name: `k8s-agent-demo-workload`).

---

## 1. Purpose

Produce a small, realistic-looking workload that:

- Emits OpenTelemetry traces to AWS X-Ray with controllable latency distributions over a long horizon.
- Publishes CloudWatch metrics derived from those traces so a CloudWatch alarm can fire.
- Includes a **real** noisy-neighbor pod that creates a verifiable infra-side signal (CPU throttling) when triggered, so the agent's kubectl-side investigation lands on something true.
- Can be flipped between `healthy`, `spike`, and `recovering` modes via a single command.
- Looks indistinguishable from a real e-commerce checkout stack to a non-technical demo audience.

The end-to-end story the demo tells:

> Black Friday. Checkout starts throwing 5xx. The on-call engineer would naturally blame checkout. The agent investigates, follows the call graph from checkout → payments, finds payments is being CPU-throttled despite not hitting its own limit, traces it to a noisy pod (`inventory-sync-job`) co-located on the same node, and asks for approval to evict it.

## 2. Non-goals

- **Not** the full OpenTelemetry demo (16 services, postgres, kafka, valkey). That is overkill and pollutes the agent repo with demo-specific assumptions.
- **Not** real business logic. No actual cart, checkout, payment, or inventory operations occur. All HTTP work is faked at the trace level.
- **Not** a load-testing tool. Throughput is intentionally moderate (~10–20 req/s) so the demo is cheap to run continuously.
- **Not** part of the agent repo. The agent must remain application-agnostic.

## 2a. Infrastructure-as-Code requirement (mandatory)

**Every AWS resource introduced by this build plan must be declared in CloudFormation and deployable / destroyable via stack operations. No `aws` CLI commands or console clicks are acceptable as the source of truth.**

The agent repo already follows this pattern: [infra/cloudformation/cluster.yaml](infra/cloudformation/cluster.yaml) (VPC + EKS + OIDC) and [infra/cloudformation/agent-iam.yaml](infra/cloudformation/agent-iam.yaml) (agent IRSA), orchestrated by [infra/deploy.sh](infra/deploy.sh) and `infra/destroy.sh`. The demo workload must mirror this exactly.

**Resources that MUST be in CloudFormation:**

- SNS topic `retail-prod-incidents` (and its access policy)
- CloudWatch alarm `checkoutservice-p99-latency-high`
- Lambda function `retail-prod-alarm-to-agent` (function + role + VPC config + log group + DLQ)
- IAM roles + IRSA trust policies for the synthesizer pod and the ADOT collector sidecar
- X-Ray sampling rule `retail-prod-services-100pct`
- SQS DLQ for the Lambda
- Any CloudWatch log groups created for the above

**Resources that are NOT in CloudFormation** (intentional exceptions, with justification):

- AWS Chatbot configuration. CloudFormation supports `AWS::Chatbot::SlackChannelConfiguration`, but the underlying Slack workspace authorization is a one-time console step that cannot be automated. Use the CFN resource for the channel config; document the workspace authorization as a manual prerequisite. If the CFN resource proves brittle in this account, fall back to console setup but document it precisely in `aws/chatbot-config.md`.
- Kubernetes manifests (synthesizer Deployment, noisy-neighbor Deployment, namespace, etc.). These are `kubectl apply` artifacts, not AWS resources. They are applied after the CloudFormation stack creates the IRSA roles they bind to.

**Stack ownership and lifecycle:**

A new CloudFormation stack — name `retail-prod-demo-workload` — owns all the AWS resources above. It depends on the existing cluster stack (`retail-prod-eks-use1` or whatever the cluster stack is named in this account) for OIDC provider URL, VPC ID, and private subnet IDs. It is deployed *after* the cluster stack and *before* the kubectl manifests. It must be destroyable in isolation (`aws cloudformation delete-stack`) without disturbing the cluster.

The demo workload repo provides:

- `aws/cloudformation/demo-workload.yaml` — the stack template.
- `demo/deploy-aws.sh` — wrapper that resolves the cluster stack's outputs as parameters and runs `aws cloudformation deploy`. Idempotent (re-running applies updates).
- `demo/destroy-aws.sh` — `aws cloudformation delete-stack` with status polling.

**The user's "spin up / destroy on demand" requirement is the load-bearing constraint here.** Anything that requires manual setup to recreate the demo from scratch is a defect.

Specifications below in Section 6 describe the *resource shape* (alarm thresholds, Lambda handler code, IAM scope, etc.). The CloudFormation YAML is the *implementation* of those specifications. Where Section 6 shows a CLI command, treat it as a sketch of intended behavior — the actual delivery is the YAML resource.

## 3. High-level architecture

```
┌─────────────────────────────────────┐
│  shop-prod namespace                │
│                                     │
│  ┌─────────────────────────────┐   │
│  │ latency-synthesizer pod     │   │      ┌──────────────┐
│  │  ├─ synth container         │──OTLP──→ │ ADOT         │──awsxray──→ AWS X-Ray
│  │  └─ adot-collector sidecar  │   │      │ collector    │
│  │                             │   │      └──────────────┘
│  │  also: PutMetricData ───────┼──────────────────────────────→ CloudWatch
│  └─────────────────────────────┘   │                               │
│                                     │                               ▼
│  ┌─────────────────────────────┐   │                          ┌─────────┐
│  │ inventory-sync-job pod      │   │                          │  Alarm  │
│  │  (real CPU burner —          │   │                          └────┬────┘
│  │   nodeSelector pins it       │   │                               │
│  │   onto payments' node)       │   │                               ▼
│  └─────────────────────────────┘   │                          ┌─────────┐
└─────────────────────────────────────┘                          │   SNS   │
                                                                 └────┬────┘
                                                                      │
                                            ┌─────────────────────────┴─────────────┐
                                            ▼                                         ▼
                                   ┌────────────────┐                       ┌──────────────────┐
                                   │  AWS Chatbot   │                       │ alarm-to-agent   │
                                   │                │                       │     Lambda       │
                                   └───────┬────────┘                       └────────┬─────────┘
                                           │                                          │
                                           ▼                                          ▼ POST /trigger
                                   ┌──────────────────┐                       ┌──────────────┐
                                   │ Slack channel    │                       │ k8s-deep-    │
                                   │  alarm thread    │                       │  agent       │
                                   │  (chart attached)│                       └──────┬───────┘
                                   └──────────────────┘                              │
                                                                                     ▼
                                                                            ┌──────────────────┐
                                                                            │ Slack channel    │
                                                                            │ investigation    │
                                                                            │ thread (own ts)  │
                                                                            └──────────────────┘
```

Two threads in the same Slack channel by design. The audience sees the alarm arrive (Chatbot) and the agent kick off in a parallel thread (its own ack). No thread-correlation code needed.

## 4. Realistic naming reference

Use these names everywhere. They make the demo feel like a real production environment.

| Concept | Name |
|---|---|
| AWS region | `us-east-1` |
| EKS cluster | `retail-prod-eks-use1` |
| Kubernetes namespace | `shop-prod` |
| Services in trace topology | `frontendservice`, `checkoutservice`, `paymentservice`, `cartservice`, `productcatalogservice` |
| Synthesizer Deployment | `latency-synthesizer` |
| Noisy neighbor Deployment | `inventory-sync-job` |
| CloudWatch namespace | `RetailProd/Services` |
| CloudWatch alarm name | `checkoutservice-p99-latency-high` |
| SNS topic | `retail-prod-incidents` |
| Lambda function | `retail-prod-alarm-to-agent` |
| IRSA roles | `retail-prod-latency-synthesizer`, `retail-prod-adot-collector` |
| Slack channel | `#retail-prod-incidents` (existing) |

`inventory-sync-job` is deliberate: it sounds like a legitimately-scheduled batch workload that happened to land on the wrong node. The story is "scheduling mistake," not "malicious pod."

## 5. Repository layout (sibling repo)

```
k8s-agent-demo-workload/
├── README.md
├── synthesizer/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── synth.py                     # main loop + scenarios + metrics
│   ├── topology.py                  # service call graph definition
│   ├── distributions.py             # latency distributions per scenario
│   └── backfill.py                  # one-shot historical trace emitter
├── noisy-neighbor/
│   ├── Dockerfile                   # stress-ng based image (or python GC thrasher)
│   └── entrypoint.sh
├── manifests/
│   ├── 00-namespace.yaml
│   ├── 10-irsa-synthesizer.yaml
│   ├── 11-irsa-collector.yaml
│   ├── 20-collector-sidecar-config.yaml
│   ├── 30-latency-synthesizer.yaml  # Deployment with sidecar collector
│   ├── 40-inventory-sync-job.yaml   # Deployment, replicas=0 by default
│   └── 50-priority-classes.yaml     # if not already on cluster
├── aws/
│   ├── cloudformation/
│   │   └── demo-workload.yaml       # SINGLE stack: SNS, alarm, Lambda+role+DLQ,
│   │                                #   synthesizer IRSA, collector IRSA,
│   │                                #   X-Ray sampling rule, Chatbot config
│   ├── lambda/
│   │   ├── handler.py               # SNS event → POST /trigger (inlined into CFN)
│   │   └── requirements.txt         # empty — handler uses stdlib only
│   └── chatbot-config.md            # one-time Slack workspace authorization steps
├── demo/
│   ├── demo                         # bash entrypoint: ./demo {healthy,spike,reset,backfill,status}
│   ├── deploy-aws.sh                # CFN deploy: resolves cluster stack outputs, runs `aws cloudformation deploy`
│   ├── destroy-aws.sh               # CFN delete: `aws cloudformation delete-stack` with polling
│   ├── deploy-k8s.sh                # kubectl apply for manifests/ (after CFN deploy)
│   └── destroy-k8s.sh               # kubectl delete for manifests/ (before CFN destroy)
└── docs/
    ├── architecture.md
    ├── runbook.md
    └── troubleshooting.md
```

## 6. Component specifications

### 6.1 Trace synthesizer (`latency-synthesizer`)

Single Python process that pretends to be five services calling each other. Emits OTLP traces via a sidecar ADOT collector, which exports to X-Ray. Also publishes CloudWatch metrics.

**Service topology** (see `synthesizer/topology.py`):

```
frontendservice
   └─ checkoutservice (POST /checkout)
        ├─ cartservice (GET /cart/{userId})
        ├─ productcatalogservice (POST /products/lookup)
        └─ paymentservice (POST /charge)              ← the hot path
             └─ productcatalogservice (POST /reserve)
```

Each request originates at `frontendservice` and follows the call graph above. Latency for each hop is drawn independently from a distribution determined by the active scenario.

**Latency distributions** (see `synthesizer/distributions.py`):

Use **lognormal** distributions. Real service latency is right-skewed; lognormal reproduces this faithfully.

| Service | Healthy p50 | Healthy sigma | Spike p50 | Spike sigma |
|---|---|---|---|---|
| frontendservice (own work) | 5 ms | 0.3 | 5 ms | 0.3 |
| checkoutservice (own work) | 15 ms | 0.4 | 15 ms | 0.4 |
| cartservice | 8 ms | 0.3 | 8 ms | 0.3 |
| productcatalogservice | 20 ms | 0.4 | 20 ms | 0.4 |
| **paymentservice** | **40 ms** | **0.4** | **350 ms** | **0.6** |

In `spike` mode, paymentservice is **also bimodal**: 5% of calls take an additional 400–800 ms (sampled uniformly). This produces the bimodal trace-duration histogram in X-Ray that's the visual punchline ("throttled traces vs healthy traces").

Errors:
- `healthy`: 0% error rate everywhere.
- `spike`: 3% error rate at `checkoutservice` (timeouts on slow `paymentservice` calls — set span status `ERROR`, attribute `http.status_code=504`). `paymentservice` itself does NOT error — it just gets slow. This is critical: it's the whole point of the demo. The failing service is not the broken one.

A `recovering` scenario interpolates p50 back toward healthy over a configurable window (default 30 s) so the alarm transitions ALARM → OK cleanly.

**Scenarios** (env var `SCENARIO`, default `healthy`):

| Value | Behavior |
|---|---|
| `healthy` | All services at baseline. Used for steady-state and backfill. |
| `spike` | paymentservice latency shifted up, bimodal tail, checkoutservice 3% errors. |
| `recovering` | Linear interpolation from current to healthy over `RECOVERY_SECONDS` (default 30). |

Scenario can be changed live via `kubectl set env deploy/latency-synthesizer SCENARIO=spike`. The pod re-reads the env on signal `SIGHUP` (deployment update triggers pod restart, which is acceptable — no state to preserve).

**Resource attributes** (set on the OTel TracerProvider per service):

```python
Resource.create({
    "service.name": "checkoutservice",   # one of the five
    "service.namespace": "retail-prod-eks-use1.shop-prod",
    "deployment.environment": "production",
    "service.version": "1.42.0",
    "cloud.provider": "aws",
    "cloud.region": "us-east-1",
    "k8s.cluster.name": "retail-prod-eks-use1",
    "k8s.namespace.name": "shop-prod",
})
```

`service.namespace` makes the X-Ray service map group these services under a single application. Use distinct, plausible `service.version` values (e.g., 1.42.0, 2.7.3) so traces look like a heterogeneous deployment.

**OTel SDK setup**:

- Create five `TracerProvider` instances, one per service name. Each shares the same `BatchSpanProcessor` pointing at `http://localhost:4317` (sidecar collector).
- Set `BatchSpanProcessor` `schedule_delay_millis=1000` and `max_export_batch_size=64` so spans flush within ~1 s. The default 5 s makes demo feedback feel laggy.
- Sampler: `parentbased_always_on`. We want every request traced.
- Span IDs and trace IDs: let OTel generate them. The awsxray exporter in the collector handles X-Ray ID format conversion.

**Concurrency**:

A naive `time.sleep(latency_ms / 1000)` in a single loop blocks throughput during spikes. Use a worker pool:

- Maintain target throughput `RPS` (default 15) via a leaky-bucket scheduler.
- Workers (`asyncio` tasks or `ThreadPoolExecutor` with 32 workers) pick up requests and execute the topology with simulated sleeps.
- During a spike, sleeps are longer but throughput stays constant — trace volume remains realistic and the spike is visible by latency, not by request drop-off.

**CloudWatch metric publishing** (see `synthesizer/synth.py`):

Every 10 s, flush rolling per-service metrics:

```python
cw.put_metric_data(
    Namespace="RetailProd/Services",
    MetricData=[
        {
            "MetricName": "LatencyP99",
            "Dimensions": [{"Name": "Service", "Value": service}],
            "Value": p99_ms,
            "Unit": "Milliseconds",
            "StorageResolution": 1,
        },
        {
            "MetricName": "LatencyP50",
            "Dimensions": [{"Name": "Service", "Value": service}],
            "Value": p50_ms,
            "Unit": "Milliseconds",
            "StorageResolution": 1,
        },
        {
            "MetricName": "ErrorRate",
            "Dimensions": [{"Name": "Service", "Value": service}],
            "Value": error_pct,
            "Unit": "Percent",
            "StorageResolution": 1,
        },
        {
            "MetricName": "RequestCount",
            "Dimensions": [{"Name": "Service", "Value": service}],
            "Value": count,
            "Unit": "Count",
            "StorageResolution": 1,
        },
    ],
)
```

`StorageResolution=1` enables high-resolution metrics (1-second granularity), which lets the alarm fire in ~30 s instead of ~2 min. Worth the small cost.

**Backfill mode** (`synthesizer/backfill.py`):

Run once before the demo to populate a "yesterday was fine" timeline. Emits 24 h of `healthy` traces and CloudWatch metrics with backdated timestamps.

- For traces: the awsxray exporter accepts segments with `start_time` up to 30 days in the past. Generate spans normally but pass historical timestamps via OTel's `Span(..., start_time=ns_epoch)`.
- For metrics: `PutMetricData` accepts timestamps up to 14 days in the past. Loop in 1-minute buckets emitting `LatencyP99` / `LatencyP50` / `ErrorRate` / `RequestCount` with the corresponding `Timestamp` parameter.
- Run on demand: `./demo backfill` invokes `kubectl run backfill-job --image=<image> --command -- python backfill.py --hours 24`.

**Image & resources**:

- Base: `python:3.12-slim`.
- Final image size target: < 200 MB.
- Pod resources: `requests: { cpu: 100m, memory: 128Mi }`, `limits: { memory: 256Mi }`. The pod is mostly sleeping; cost is negligible.

### 6.2 Noisy neighbor (`inventory-sync-job`)

Real CPU burner. Required because the agent's kubectl-side investigation must find a true signal (`kubectl top pod`, `container_cpu_cfs_throttled_seconds`).

**Implementation**: tiny Python script in a tight loop allocating and discarding large lists, OR `stress-ng --cpu N --vm 2 --vm-bytes 256M` from the `polinux/stress-ng` image. The stress-ng path is simpler; the GC-thrasher path tells a more believable story (matches the original demo narrative of "GC death spiral"). Build both, use the GC thrasher by default.

**Pinning to paymentservice's node**: this is the crux of the demo.

Approach: assign a label `workload-tier=batch` to one specific node, and label the synthesizer's `paymentservice` workload with `workload-tier=batch` too via a topologySpreadConstraint or nodeAffinity preferring that node. Easier for demo: hard-pin both to a named node via `nodeSelector`.

Practical recipe for a 3-node cluster:
1. Pick one node, label it: `kubectl label node <node-name> demo-node=true`.
2. The `latency-synthesizer` deployment uses `nodeSelector: { demo-node: "true" }` so the paymentservice tracer (which runs in the same pod) is on that node.
3. The `inventory-sync-job` deployment also uses `nodeSelector: { demo-node: "true" }` so it lands on the same node.

This keeps the demo deterministic — there is no scheduler ambiguity about where the noisy pod ends up.

**Pod default state**: `replicas: 0`. The `./demo spike` script scales it to 1.

**Resources**: no CPU limit (intentional — CFS throttling on the *paymentservice's* container needs to come from competition for node CPU, not from inventory-sync-job's own limit). Memory request 128 Mi, limit 512 Mi.

**Priority class**: `low-priority` (or whatever the cluster skill maps to "background"). This makes the agent's recommendation defensible: "the lowest-priority pod on this node is the noisy one — recommend evict."

### 6.3 ADOT collector (sidecar)

The synthesizer pod runs the AWS Distro for OpenTelemetry collector as a sidecar container. The synth process sends OTLP to `localhost:4317`; the collector exports to X-Ray.

**Why sidecar and not DaemonSet**: keeps the demo workload self-contained. A DaemonSet is more realistic for production but adds a separate deployment and IRSA. Sidecar wins on simplicity for this scope.

**Image**: `public.ecr.aws/aws-observability/aws-otel-collector:latest`.

**Config** (`manifests/20-collector-sidecar-config.yaml`):

```yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
exporters:
  awsxray:
    region: us-east-1
    indexed_attributes: [service.name, http.status_code]
processors:
  batch:
    timeout: 1s
    send_batch_size: 64
service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [batch]
      exporters: [awsxray]
```

The collector's pod ServiceAccount needs IRSA with `AWSXRayDaemonWriteAccess`.

### 6.4 X-Ray sampling rule

X-Ray's default sampling rule is "1 req/sec + 5%". For the demo, every trace from our services must be ingested.

**Resource type**: `AWS::XRay::SamplingRule` in `demo-workload.yaml`. CloudFormation supports this directly — no custom resources or CLI fallback needed.

```yaml
SamplingRule100Pct:
  Type: AWS::XRay::SamplingRule
  Properties:
    SamplingRule:
      RuleName: retail-prod-services-100pct
      ResourceARN: "*"
      Priority: 100
      FixedRate: 1.0
      ReservoirSize: 100
      ServiceName: "*service"
      ServiceType: "*"
      Host: "*"
      HTTPMethod: "*"
      URLPath: "*"
      Version: 1
```

`ServiceName: "*service"` matches all our service names (`frontendservice`, `checkoutservice`, etc). Priority 100 takes precedence over the default rule (priority 10000).

### 6.5 CloudWatch alarm

Single alarm on `checkoutservice` p99 latency.

**Resource type**: `AWS::CloudWatch::Alarm` in `demo-workload.yaml`. References the SNS topic resource by `!Ref` rather than ARN string, so deployment order is enforced by the stack.

```yaml
CheckoutP99LatencyAlarm:
  Type: AWS::CloudWatch::Alarm
  Properties:
    AlarmName: checkoutservice-p99-latency-high
    AlarmDescription: "checkoutservice p99 latency exceeded 300 ms threshold"
    Namespace: RetailProd/Services
    MetricName: LatencyP99
    Dimensions:
      - Name: Service
        Value: checkoutservice
    Statistic: Average
    Period: 30
    EvaluationPeriods: 2
    Threshold: 300
    ComparisonOperator: GreaterThanThreshold
    TreatMissingData: notBreaching
    AlarmActions:
      - !Ref IncidentsTopic
    OKActions:
      - !Ref IncidentsTopic
```

`period=30 evaluation_periods=2` → fires ~60 s after spike begins. `treat-missing-data notBreaching` avoids spurious alarms during pod restarts. Both ALARM and OK transitions fan out so Chatbot posts a "resolved" message and the agent sees resolution if still pending approval.

### 6.6 SNS topic + subscribers

Single topic, two subscribers — all declared in the same stack.

**SNS topic**:

```yaml
IncidentsTopic:
  Type: AWS::SNS::Topic
  Properties:
    TopicName: retail-prod-incidents
    DisplayName: "Retail Prod Incidents"
```

**Subscriber 1: AWS Chatbot.**

CloudFormation supports `AWS::Chatbot::SlackChannelConfiguration`. The Slack workspace authorization itself is a one-time console step that can't be automated; once the workspace is authorized for the AWS account, the channel-level config is fully declarative.

```yaml
ChatbotChannel:
  Type: AWS::Chatbot::SlackChannelConfiguration
  Properties:
    ConfigurationName: retail-prod-incidents
    SlackWorkspaceId: !Ref SlackWorkspaceId    # stack parameter
    SlackChannelId: !Ref SlackChannelId        # stack parameter, e.g. C0123ABC456
    IamRoleArn: !GetAtt ChatbotRole.Arn
    SnsTopicArns:
      - !Ref IncidentsTopic
    LoggingLevel: INFO

ChatbotRole:
  Type: AWS::IAM::Role
  Properties:
    AssumeRolePolicyDocument:
      Version: "2012-10-17"
      Statement:
        - Effect: Allow
          Principal: { Service: chatbot.amazonaws.com }
          Action: sts:AssumeRole
    ManagedPolicyArns:
      - arn:aws:iam::aws:policy/CloudWatchReadOnlyAccess
      - arn:aws:iam::aws:policy/AWSResourceExplorerReadOnlyAccess
```

**One-time manual prerequisite** (document in `aws/chatbot-config.md`):
1. AWS Chatbot console → Configured clients → Slack → "Configure new client".
2. Authorize the AWS Chatbot Slack app for the workspace.
3. Note the Slack workspace ID and pass it to `deploy-aws.sh` as `SlackWorkspaceId`.

**Subscriber 2: Lambda `retail-prod-alarm-to-agent`** — see 6.7.

### 6.7 Trigger Lambda (`retail-prod-alarm-to-agent`)

Translates SNS-from-CloudWatch events into POST requests against the agent's existing `/trigger` endpoint.

**Why a Lambda and not direct SNS→HTTPS subscription**: the agent's endpoint is inside the cluster, not internet-facing. The Lambda runs in the same VPC and reaches the cluster's internal load balancer. SNS→HTTPS retries are also clumsy; Lambda gives clean retry/DLQ semantics.

**Handler code** (`aws/lambda/handler.py`, inlined into the CFN template via `Code.ZipFile` so there is no external artifact bucket to manage):

```python
import json, os, urllib.request

AGENT_URL = os.environ["AGENT_URL"]

def handler(event, context):
    for record in event.get("Records", []):
        sns = record["Sns"]
        msg = json.loads(sns["Message"])
        new_state = msg.get("NewStateValue")
        if new_state != "ALARM":
            print(f"Skipping {new_state}")
            continue

        alarm_name = msg["AlarmName"]
        reason = msg.get("NewStateReason", "")
        service = None
        for d in msg.get("Trigger", {}).get("Dimensions", []):
            if d.get("name") == "Service":
                service = d.get("value")

        payload = {
            "alarm_name": alarm_name,
            "state": "ALARM",
            "node": "(service-level alarm)",
            "reason": (
                f"{reason}\n"
                f"Service: {service or 'unknown'}\n"
                f"Metric: {msg.get('Trigger', {}).get('MetricName')}\n"
                f"Threshold: {msg.get('Trigger', {}).get('Threshold')}"
            ),
        }
        req = urllib.request.Request(
            AGENT_URL,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"Agent responded {resp.status}: {resp.read()[:200]}")

    return {"statusCode": 200}
```

**CloudFormation resources** (in `demo-workload.yaml`):

```yaml
AlarmToAgentDLQ:
  Type: AWS::SQS::Queue
  Properties:
    QueueName: retail-prod-alarm-to-agent-dlq
    MessageRetentionPeriod: 1209600  # 14 days

AlarmToAgentRole:
  Type: AWS::IAM::Role
  Properties:
    AssumeRolePolicyDocument:
      Version: "2012-10-17"
      Statement:
        - Effect: Allow
          Principal: { Service: lambda.amazonaws.com }
          Action: sts:AssumeRole
    ManagedPolicyArns:
      - arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole
    Policies:
      - PolicyName: dlq-write
        PolicyDocument:
          Version: "2012-10-17"
          Statement:
            - Effect: Allow
              Action: sqs:SendMessage
              Resource: !GetAtt AlarmToAgentDLQ.Arn

AlarmToAgentLambda:
  Type: AWS::Lambda::Function
  Properties:
    FunctionName: retail-prod-alarm-to-agent
    Runtime: python3.12
    Handler: index.handler
    Role: !GetAtt AlarmToAgentRole.Arn
    Timeout: 30
    MemorySize: 128
    Environment:
      Variables:
        AGENT_URL: !Ref AgentUrl    # stack parameter
    VpcConfig:
      SubnetIds: !Ref ClusterPrivateSubnetIds   # stack parameter, from cluster stack output
      SecurityGroupIds:
        - !Ref AlarmToAgentSecurityGroup
    DeadLetterConfig:
      TargetArn: !GetAtt AlarmToAgentDLQ.Arn
    Code:
      ZipFile: |
        # contents of aws/lambda/handler.py inlined here
        # (deploy-aws.sh substitutes the file contents into the template before deploy)

AlarmToAgentSecurityGroup:
  Type: AWS::EC2::SecurityGroup
  Properties:
    GroupDescription: Egress to EKS cluster internal LB on 8080
    VpcId: !Ref ClusterVpcId    # stack parameter, from cluster stack output

AlarmToAgentSubscription:
  Type: AWS::SNS::Subscription
  Properties:
    TopicArn: !Ref IncidentsTopic
    Protocol: lambda
    Endpoint: !GetAtt AlarmToAgentLambda.Arn

AlarmToAgentInvokePermission:
  Type: AWS::Lambda::Permission
  Properties:
    FunctionName: !Ref AlarmToAgentLambda
    Action: lambda:InvokeFunction
    Principal: sns.amazonaws.com
    SourceArn: !Ref IncidentsTopic
```

**Key decisions**:
- Filter on `NewStateValue == "ALARM"`. The agent already ignores non-ALARM states ([agent/main.py:604](agent/main.py#L604)) but doing it at the Lambda saves one HTTP roundtrip per OK transition.
- Set `node` to `"(service-level alarm)"` since latency alarms are not node-scoped. The agent will discover the node via X-Ray + kubectl during investigation. **This is the demo's whole point** — do not pass the node, let the agent find it.
- `urllib` over `requests` keeps the Lambda zero-dependency, so it inlines cleanly into CFN.
- Code is inlined via `Code.ZipFile`. `deploy-aws.sh` substitutes `aws/lambda/handler.py` into the template at deploy time (a simple `sed` or Python templating step). This avoids the S3 artifact bucket pattern, keeping the stack self-contained.

### 6.8 IAM / IRSA — all in CloudFormation

All principals below are declared as `AWS::IAM::Role` resources in `demo-workload.yaml`. The IRSA roles use the existing cluster stack's OIDC provider URL (passed in as a stack parameter, `ClusterOidcProviderUrl`).

| Principal | CFN resource | Permissions |
|---|---|---|
| `latency-synthesizer` SA | `SynthesizerIrsaRole` | `cloudwatch:PutMetricData` scoped to namespace `RetailProd/Services` via condition key |
| `adot-collector` SA (sidecar) | `CollectorIrsaRole` | Managed policy `AWSXRayDaemonWriteAccess` |
| `retail-prod-alarm-to-agent` Lambda | `AlarmToAgentRole` (above) | `AWSLambdaVPCAccessExecutionRole` + DLQ write |
| AWS Chatbot | `ChatbotRole` (above) | `CloudWatchReadOnlyAccess` + `AWSResourceExplorerReadOnlyAccess` |
| Agent's existing role | (in agent repo's `agent-iam.yaml`) | Out of scope for this stack |

**IRSA pattern** (example for synthesizer):

```yaml
SynthesizerIrsaRole:
  Type: AWS::IAM::Role
  Properties:
    RoleName: retail-prod-latency-synthesizer
    AssumeRolePolicyDocument:
      Version: "2012-10-17"
      Statement:
        - Effect: Allow
          Principal:
            Federated: !Sub "arn:aws:iam::${AWS::AccountId}:oidc-provider/${ClusterOidcProviderUrl}"
          Action: sts:AssumeRoleWithWebIdentity
          Condition:
            StringEquals:
              !Sub "${ClusterOidcProviderUrl}:sub": "system:serviceaccount:shop-prod:latency-synthesizer"
              !Sub "${ClusterOidcProviderUrl}:aud": "sts.amazonaws.com"
    Policies:
      - PolicyName: put-metric-data
        PolicyDocument:
          Version: "2012-10-17"
          Statement:
            - Effect: Allow
              Action: cloudwatch:PutMetricData
              Resource: "*"
              Condition:
                StringEquals:
                  cloudwatch:namespace: RetailProd/Services
```

The Kubernetes ServiceAccount manifests (in `manifests/`) annotate themselves with the role ARN exported from this stack — typically resolved via a stack-output substitution in `deploy-k8s.sh`.

### 6.9 Stack orchestration

**Single stack**: `retail-prod-demo-workload`. All AWS resources from §6.4–6.8 live in `aws/cloudformation/demo-workload.yaml`.

**Stack parameters** (passed by `deploy-aws.sh`):

| Parameter | Source | Purpose |
|---|---|---|
| `ClusterOidcProviderUrl` | output of the existing cluster stack | IRSA trust policy |
| `ClusterVpcId` | output of the existing cluster stack | Lambda VPC config |
| `ClusterPrivateSubnetIds` | output of the existing cluster stack | Lambda VPC config |
| `AgentUrl` | computed from agent's internal LB (kubectl get svc) | Lambda env var |
| `SlackWorkspaceId` | manual input, stored in `demo/.env` | Chatbot config |
| `SlackChannelId` | manual input, stored in `demo/.env` | Chatbot config |

`deploy-aws.sh` resolves parameters 1–4 by querying the cluster stack and the agent's Service, then runs `aws cloudformation deploy --template-file ... --parameter-overrides ...`. This mirrors the existing [infra/deploy.sh](infra/deploy.sh) pattern.

**Stack outputs** (consumed by `deploy-k8s.sh` to render manifests):

| Output | Used by |
|---|---|
| `SynthesizerIrsaRoleArn` | annotation on `latency-synthesizer` ServiceAccount |
| `CollectorIrsaRoleArn` | annotation on `adot-collector` ServiceAccount |
| `IncidentsTopicArn` | reference only (not consumed by manifests) |

**Deploy / destroy commands**:

```bash
# spin up
./demo/deploy-aws.sh        # creates/updates retail-prod-demo-workload stack
./demo/deploy-k8s.sh        # applies manifests/ to the cluster

# tear down
./demo/destroy-k8s.sh       # deletes K8s resources first (so pods stop emitting)
./demo/destroy-aws.sh       # deletes retail-prod-demo-workload stack

# rebuild from scratch
./demo/destroy-k8s.sh && ./demo/destroy-aws.sh && ./demo/deploy-aws.sh && ./demo/deploy-k8s.sh
```

**Order matters on teardown**: K8s pods must be deleted before the IRSA roles. If the stack is deleted first, the synthesizer keeps trying to use the deleted role and floods CloudWatch logs with permission errors until the pods are also removed.

**Stack does NOT modify**:
- The existing cluster stack (`cluster.yaml`).
- The existing agent IRSA stack (`agent-iam.yaml`).
- Any resources in the agent repo's `infra/` directory.

This isolation is non-negotiable: tearing down the demo workload must not affect the agent or cluster.

## 7. Demo trigger UX

Single bash entrypoint at `demo/demo`:

```bash
#!/usr/bin/env bash
set -euo pipefail
NS=shop-prod
case "${1:-}" in
  healthy)
    kubectl -n "$NS" set env deploy/latency-synthesizer SCENARIO=healthy
    kubectl -n "$NS" scale deploy/inventory-sync-job --replicas=0
    echo "→ healthy. Allow ~60s for alarm to clear."
    ;;
  spike)
    kubectl -n "$NS" scale deploy/inventory-sync-job --replicas=1
    kubectl -n "$NS" set env deploy/latency-synthesizer SCENARIO=spike
    echo "→ spike. Alarm should fire in ~60s."
    ;;
  reset)
    "$0" healthy
    ;;
  backfill)
    HOURS="${2:-24}"
    kubectl -n "$NS" run backfill-$(date +%s) \
      --image=<registry>/latency-synthesizer:latest \
      --restart=Never --rm -i --tty=false \
      --command -- python backfill.py --hours "$HOURS"
    ;;
  status)
    kubectl -n "$NS" get deploy latency-synthesizer inventory-sync-job
    aws cloudwatch describe-alarms --alarm-names checkoutservice-p99-latency-high \
      --query 'MetricAlarms[0].StateValue' --output text
    ;;
  *)
    echo "Usage: $0 {healthy|spike|reset|backfill [hours]|status}"
    exit 1
    ;;
esac
```

## 8. Edge cases & failure modes

### 8.1 Spike arrives before backfill data exists

If you run `./demo spike` on a fresh deploy, the X-Ray service map will only have a few minutes of history and the "spike" looks unimpressive next to a flat line. **Always run `./demo backfill 24` at least once before demoing**, ideally as part of cluster setup. Document this as a step-1 prerequisite in `docs/runbook.md`.

### 8.2 Synthesizer pod restart during demo

If the pod restarts mid-demo:
- Trace gap of 10–30 s in X-Ray. Visible but small.
- CloudWatch metric gap. With `treat-missing-data notBreaching` the alarm won't flip to INSUFFICIENT_DATA, but it could flip ALARM → OK if the spike was barely above threshold. Mitigation: set spike p50 well above threshold (350 ms vs 300 ms threshold leaves headroom).

Add a PodDisruptionBudget with `minAvailable: 1` and a single replica. During demo, do NOT roll the deployment; use `kubectl set env` which restarts the pod but only briefly.

### 8.3 Alarm flapping during recovery

When `./demo healthy` is run, p99 drops from ~600 ms to ~80 ms over a few seconds, but the rolling 30 s metric is the average — it can dip below threshold then briefly rise again. `evaluation_periods=2` damps this. If flapping persists, raise to 3.

### 8.4 X-Ray sampling drops segments

Symptom: service map shows fewer requests than expected. Cause: the X-Ray sampling rule didn't apply, or the awsxray exporter is rate-limited.

Verification: `aws xray get-sampling-statistics-summaries` shows requests sampled vs borrowed.

Fix: confirm the sampling rule from 6.4 was created and has priority 100. Confirm `parentbased_always_on` is set on the OTel side.

### 8.5 CloudWatch PutMetricData throttled

`PutMetricData` quota is 150 transactions/second per region. The synthesizer publishes every 10 s for 5 services = 0.5 TPS. Well under quota.

Edge case: if multiple synthesizer instances are deployed (shouldn't happen — `replicas: 1`) or if a runaway loop publishes too often, throttling kicks in. The boto3 client retries with backoff; in practice this is invisible.

### 8.6 Lambda cold start delays trigger

First invocation after idle takes 1–3 s. The agent's investigation already takes 30+ seconds; a 1–3 s extra at the front is invisible.

To eliminate: provisioned concurrency = 1 on the Lambda. Costs ~$5/month. Worth it if cold-start UX matters.

### 8.7 SNS delivers the same notification twice

SNS guarantees at-least-once delivery. Lambda may be invoked twice for the same alarm. The agent's `/trigger` will create two investigation threads.

Mitigation options:
- Add idempotency in the Lambda: maintain a 5-minute dedupe cache (e.g., DynamoDB with TTL) keyed on `AlarmName + StateChangeTime`. ~30 lines.
- Accept it for the demo: rare in practice, and a duplicate "I'm investigating" Slack message is recoverable.

For the demo: skip dedupe. Document as a known issue in `docs/troubleshooting.md`.

### 8.8 Chatbot and agent post out of order

Audience expects: alarm card first, then agent ack. SNS fans out essentially in parallel and the agent's `/trigger` → Slack post adds a few hundred ms vs. Chatbot's direct path. Chatbot usually wins by 1–2 s. If it ever loses, the demo still makes sense — both threads are visible.

If strict ordering matters, add a 2 s `time.sleep` at the start of the Lambda. Not recommended; the natural order is already correct.

### 8.9 Two engineers run `./demo spike` in parallel

Idempotent: scaling `inventory-sync-job` to 1 when it's already 1 is a no-op. Setting `SCENARIO=spike` when already spike is a no-op. Safe.

### 8.10 Agent restart during a paused approval

The agent's existing `_recover_paused_investigations` ([agent/main.py:190](agent/main.py#L190)) handles this — out of scope here. The synthesizer/alarm side has no state to lose.

### 8.11 Slack rate limiting

Slack allows ~1 msg/sec per channel. The agent posts maybe 10–20 messages during a typical investigation. Chatbot posts 1–2. Combined: well under limit.

Edge case: if multiple alarms fire in quick succession (shouldn't happen with one alarm), the channel could approach the limit. Mitigation: only one alarm exists.

### 8.12 X-Ray service map shows traces from previous demo runs

X-Ray retains traces for 30 days. After backfill + several demo runs, the service map shows accumulated history. This is desirable for the demo (shows a service map that "looks lived-in") but means a stale `spike` trace from yesterday could appear when you're showing `healthy` data.

Use the time-range selector in the X-Ray console to scope to "last 15 minutes" during the demo. Document this in `docs/runbook.md` step "Pre-demo console setup."

### 8.13 The noisy pod doesn't actually throttle paymentservice

Symptom: `kubectl top pod paymentservice-...` shows normal CPU even when `inventory-sync-job` is running.

Cause: the synthesizer process emits traces but doesn't actually consume CPU (it's mostly sleeping). So there's no real CPU competition on the node — the throttling story is *visual only*.

This is a fundamental limitation of the synthesizer pattern. Three options:

- **Option A (recommended)**: have the synthesizer also burn CPU proportional to claimed latency during a `spike`. When generating a 500 ms span for paymentservice, *also* spend 50 ms of real CPU before the sleep. This produces a small but real CPU signal that the noisy pod can amplify into throttling.
- **Option B**: deploy a tiny Python service (`paymentservice-worker`) that *does* real work (computes hashes in a loop) at a configurable rate. Less synthetic but adds a moving part.
- **Option C**: skip the throttling story; let the demo focus on the X-Ray side ("look at the dependency map, payments is slow") and have the agent find the noisy pod via co-location alone, without pretending the throttling caused the slowness. Less satisfying narrative.

Build Option A. In `synth.py`, during `spike`, every paymentservice span burns `min(latency_ms * 0.1, 80) ms` of real CPU via a tight loop before the sleep. Combined with `inventory-sync-job` saturating the node, this produces real throttling that `kubectl top` and `container_cpu_cfs_throttled_seconds` will both surface.

### 8.14 What if the agent's investigation reaches the wrong conclusion

This is not a synthesizer concern, but worth flagging for demo-day. If the agent recommends evicting `paymentservice` instead of `inventory-sync-job`, the cluster skill ([skills/clusters/...](skills/clusters/)) for `retail-prod-eks-use1` must define paymentservice as critical-tier. Otherwise eviction order is undefined.

Action: ensure a cluster skill for `retail-prod-eks-use1` exists in the agent repo before demoing, and that it lists paymentservice / checkoutservice as critical-tier. This is the only place demo-specific knowledge legitimately lives — in the cluster skill, which is supposed to be deployment-specific.

### 8.15 Cost during continuous operation

Running 24/7:
- X-Ray ingest: ~15 traces/s × 86400 s = 1.3 M traces/day × $5/M = $6.50/day = ~$200/month.
- CloudWatch metrics: 5 services × 4 metrics × high-resolution × 1 region = $1.50/month.
- CloudWatch alarms: 1 alarm × $0.10 = $0.10/month.
- Lambda: <$1/month at this volume.
- SNS: pennies.

Total: ~$200/month. If cost matters: tear the stack down between demos with `./demo/destroy-aws.sh && ./demo/destroy-k8s.sh`, or scale synthesizer down to 1 req/s outside of demo windows. Document a cost-control mode in the runbook.

### 8.16 CloudFormation stack stuck in CREATE_FAILED / DELETE_FAILED / UPDATE_ROLLBACK_FAILED

Most likely causes:
- **VPC config mismatch**: stack parameters point to subnets in a different VPC than the security group. Diagnostic: `aws cloudformation describe-stack-events --stack-name retail-prod-demo-workload | head -50`.
- **Chatbot resource dependency on Slack workspace**: if the workspace authorization was revoked, the `AWS::Chatbot::SlackChannelConfiguration` resource fails. Re-authorize in console, then retry.
- **Lambda VPC role not yet propagated**: IAM is eventually consistent. CFN occasionally fails the first time and succeeds on retry within 30 s.
- **Stack stuck on DLQ delete**: SQS queues with un-purged messages can take up to a minute. Wait, then retry.

Recovery steps:
- For `UPDATE_ROLLBACK_FAILED`: `aws cloudformation continue-update-rollback` after fixing the underlying issue.
- For `DELETE_FAILED`: identify the stuck resource in stack events, manually delete it via console or CLI, then re-run `delete-stack`.
- For total wedge: `aws cloudformation delete-stack --retain-resources <stuck-resource-logical-ids>` removes the stack record but leaves orphans for manual cleanup.

`docs/troubleshooting.md` should include a copy-pasteable runbook for each scenario above.

### 8.17 Re-deploying the stack while the agent is mid-investigation

If the SNS topic or Lambda is replaced (rather than updated in place), an in-flight investigation could lose its alarm fan-out path. CFN treats `TopicName` as immutable in some scenarios — changing it triggers replacement.

Mitigation: never change `TopicName` or `FunctionName` after initial deploy. Use stack updates (not replacement) for routine changes. The `deploy-aws.sh` script should print a warning if `cloudformation deploy` reports any resource as `Replacement: True`.

## 9. Build order

Follow this order. Each step is verifiable independently. Steps 1–3 build the CloudFormation stack incrementally so it stays deployable at every checkpoint.

1. **Bare synthesizer (local).** Build `synth.py` with topology, distributions, scenario switch. Run locally with `python synth.py`. Verify it produces sane latency numbers in stdout. **Do not** wire X-Ray yet, **do not** deploy to cluster yet.

2. **Initial CFN stack — IRSA only.** Write `aws/cloudformation/demo-workload.yaml` with just `SynthesizerIrsaRole` and `CollectorIrsaRole`. Wire `deploy-aws.sh` to resolve cluster stack outputs (OIDC URL, VPC, subnets) as parameters. Deploy: `./demo/deploy-aws.sh`. Verify: `aws cloudformation describe-stacks --stack-name retail-prod-demo-workload --query 'Stacks[0].StackStatus'` returns `CREATE_COMPLETE`. Verify `destroy-aws.sh` works on this minimal stack before adding more.

3. **Synthesizer in cluster, X-Ray traces flowing.** Add the ADOT sidecar collector to the synthesizer Deployment. Apply manifests via `./demo/deploy-k8s.sh` — it consumes the `SynthesizerIrsaRoleArn` and `CollectorIrsaRoleArn` outputs from the stack to annotate ServiceAccounts. Verify traces in X-Ray console within 60 s and the service map shows the five services with the expected edges.

4. **CloudWatch metrics.** Add `PutMetricData` to `synth.py`. Verify metrics appear in the CloudWatch console under `RetailProd/Services` namespace.

5. **Add SNS + alarm + X-Ray sampling rule to the CFN stack.** Append `IncidentsTopic`, `CheckoutP99LatencyAlarm`, `SamplingRule100Pct` to `demo-workload.yaml`. Re-run `./demo/deploy-aws.sh` (idempotent update). Set `SCENARIO=spike` manually. Verify alarm transitions to `ALARM` within 60 s. Verify `SCENARIO=healthy` transitions back to `OK`.

6. **Backfill mode.** Run `python backfill.py --hours 1`. Verify backdated traces and metrics show up correctly in the past time range.

7. **Add Lambda + DLQ + role + SNS subscription to CFN stack.** Append all Lambda-related resources. The handler code is inlined via `Code.ZipFile`; `deploy-aws.sh` must read `aws/lambda/handler.py` and substitute it into the template before `cloudformation deploy`. Re-run deploy. Verify the Lambda exists and is subscribed to the SNS topic (`aws sns list-subscriptions-by-topic`). Trigger the spike, verify SNS → Lambda invocation in CloudWatch Logs for the Lambda, and verify the agent posts its own ack in Slack. Use [infra/trigger-agent-direct.sh](infra/trigger-agent-direct.sh) as a reference for the expected `/trigger` payload shape.

8. **Add Chatbot config to CFN stack.** Pre-condition: Slack workspace authorization for AWS Chatbot is complete (one-time manual step, documented in `aws/chatbot-config.md`). Append `ChatbotChannel` and `ChatbotRole` to the template, with `SlackWorkspaceId` and `SlackChannelId` as new stack parameters. Re-deploy. Verify Chatbot posts the alarm card to `#retail-prod-incidents` on the next spike.

9. **Noisy neighbor pod.** Build and deploy `inventory-sync-job` (replicas=0) via manifests. Verify nodeSelector pins it correctly when scaled to 1.

10. **Real-CPU mode in synthesizer.** Add the CPU-burn-in-spike behavior (8.13 Option A). Verify with `kubectl top pod` that CPU climbs during a spike, and that running the noisy pod alongside causes `container_cpu_cfs_throttled_seconds` to climb on the synthesizer pod.

11. **Demo bash entrypoint.** Build `demo/demo`. Test all subcommands.

12. **Stack tear-down rehearsal.** Run `./demo/destroy-k8s.sh && ./demo/destroy-aws.sh`. Verify both complete cleanly. Re-run `./demo/deploy-aws.sh && ./demo/deploy-k8s.sh`. Verify everything comes back identically. Repeat at least twice — this is the load-bearing requirement (see §2a).

13. **End-to-end rehearsal.** From `healthy` baseline (with backfill loaded), run `./demo spike`. Watch:
    - X-Ray service map: paymentservice edge turns red within 60 s.
    - CloudWatch console: alarm goes ALARM within 60 s.
    - Slack: Chatbot posts alarm card.
    - Slack: agent posts ack in a separate thread within ~5 s of Chatbot.
    - Agent investigation runs to completion, posting findings + approval gate.

14. **Runbook + troubleshooting docs.** Write `docs/runbook.md` (pre-demo prep, demo script, post-demo cleanup) and `docs/troubleshooting.md` (each edge case from §8 with diagnostic commands).

## 10. Validation checklist

Before declaring the demo ready:

**Stack lifecycle (the non-negotiable spin-up/tear-down requirement):**

- [ ] `./demo/deploy-aws.sh` brings the stack to `CREATE_COMPLETE` from a clean account in under 10 minutes.
- [ ] `./demo/deploy-aws.sh` is idempotent — running it on an already-deployed stack returns `UPDATE_COMPLETE` (or "no changes") without errors.
- [ ] `./demo/destroy-aws.sh` brings the stack to deleted state in under 5 minutes with no manual cleanup.
- [ ] Full cycle (deploy → destroy → deploy → destroy) completes successfully twice in a row.
- [ ] No resources orphaned after `destroy-aws.sh`: `aws resourcegroupstaggingapi get-resources --tag-filters Key=Stack,Values=retail-prod-demo-workload` returns empty.
- [ ] Tearing down the demo stack does NOT affect the cluster stack or agent IRSA stack (verify with `aws cloudformation describe-stacks` for both).
- [ ] All AWS resources (SNS, alarm, Lambda, DLQ, IAM roles, X-Ray sampling rule, Chatbot config) carry a `Stack=retail-prod-demo-workload` tag for cost allocation.

**Demo behavior:**

- [ ] X-Ray service map shows all five services with correct edges.
- [ ] X-Ray service map under `healthy` shows all green.
- [ ] X-Ray service map within 60 s of `spike` shows `paymentservice` red.
- [ ] X-Ray trace duration histogram for `paymentservice` is bimodal during spike.
- [ ] CloudWatch metrics for `RetailProd/Services` populated for all five services.
- [ ] CloudWatch alarm fires within 60 s of `spike`, clears within 90 s of `reset`.
- [ ] Chatbot posts alarm card with chart to `#retail-prod-incidents`.
- [ ] Lambda invocation succeeds (visible in CloudWatch Logs for the Lambda).
- [ ] Agent posts opening ack in `#retail-prod-incidents` (separate thread from Chatbot's).
- [ ] Agent investigation runs, references real X-Ray + kubectl data.
- [ ] `kubectl top pod` confirms real CPU on the synthesizer's paymentservice during spike.
- [ ] `container_cpu_cfs_throttled_seconds` for the synthesizer pod climbs when noisy pod is scaled to 1.
- [ ] Backfill produces a 24 h flat-baseline-then-spike timeline visible in X-Ray and CloudWatch.
- [ ] `./demo reset` returns alarm state to OK and noisy pod to 0 replicas.
- [ ] Cost dashboard for the AWS account shows expected daily spend (~$7/day with continuous operation).

## 11. What does NOT change in the agent repo

The agent repo (`k8s-deep-agent`) must remain application-agnostic. The following are **not allowed** in this build plan:

- No code changes to `agent/`.
- No new universal skills under `skills/universal/` referencing checkoutservice / paymentservice / inventory-sync-job by name.
- No demo-specific files in `fault-injection/` (the existing disk-pressure scenario stays; new files belong in the sibling demo workload repo).
- No images, manifests, or scripts in this repo for the synthesizer or noisy pod.

The **one exception**: a cluster skill at `skills/clusters/retail-prod-eks-use1/SKILL.md` describing the cluster's services, tiers, and priority classes is **expected** — that is the intended place for deployment-specific knowledge. This skill should:
- List `frontendservice`, `checkoutservice`, `paymentservice`, `cartservice`, `productcatalogservice` with tiers (frontend/checkout/payment = critical; cart/productcatalog = user-facing).
- Map `inventory-sync-job` to `low-priority` / `background` tier.
- Reference the universal `noisy-neighbor` skill for CPU contention investigations.
- Name `#retail-prod-incidents` as the Slack channel.

This file lives in the agent repo because it describes the *target cluster*, not the demo workload. The demo workload populates the cluster; the cluster skill describes it.

## 12. Open questions for the builder

These are decisions the builder may need to make in flight:

1. **Container registry**: where do `latency-synthesizer` and `inventory-sync-job` images live? ECR private repo `retail-prod` is the natural choice but requires the image-pull secret. Public ECR is simpler.
2. **Cluster context**: does `retail-prod-eks-use1` already exist, or is it a new cluster spun up for the demo? If new, capacity (3 nodes × t3.medium is sufficient) and node labels need to be set during creation.
3. **Slack channel**: `#retail-prod-incidents` — does this channel exist with the agent already invited and Chatbot authorized? If not, that's a manual setup step before the runbook works.
4. **Backfill granularity**: 24 h × 1-min buckets = 1440 metric data points × 5 services × 4 metrics = 28800 PutMetricData entries. Well within `PutMetricData` quota but takes ~5 minutes of wall time for the backfill job. Acceptable.
5. **Demo cluster vs. shared dev cluster**: do not run the synthesizer on a shared cluster — the noisy pod intentionally saturates a node. Use a dedicated demo cluster.

---

End of build plan. The agent repo's [CLAUDE.md](CLAUDE.md), [README.md](README.md), and [skills/universal/noisy-neighbor/](skills/universal/noisy-neighbor/) are useful references for understanding what the agent will do once the alarm fires, but no changes to those files are expected as part of this build.
