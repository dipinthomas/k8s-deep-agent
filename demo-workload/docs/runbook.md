# Runbook — first-time bringup (steps 1-8)

## Pre-flight

- [ ] `aws sso login --profile fernhub` has succeeded
- [ ] `kubectl` context points at the demo cluster
- [ ] `docker buildx ls` shows `multiarch-builder` (create if missing)
- [ ] `demo/.env` exists and `SYNTHESIZER_IMAGE` is set
- [ ] Cluster CFN stack `k8s-agent-cluster` is in `CREATE_COMPLETE` /
      `UPDATE_COMPLETE` state

Quick check:
```bash
aws cloudformation describe-stacks --stack-name k8s-agent-cluster \
  --query 'Stacks[0].StackStatus' --output text
```

## Bringup

```bash
cd ~/Documents/my_github/k8s-agent-demo-workload
bash demo/build-image.sh        # ~2-4 min depending on registry
bash demo/deploy-aws.sh         # ~30s; idempotent
bash demo/deploy-k8s.sh         # ~60s including rollout wait
```

## Verify

1. **Pod Ready**
   ```bash
   kubectl -n shop-prod get pods
   ```
   Expect: `latency-synthesizer-...` with READY `2/2` (synth + collector).

2. **Synth emitting**
   ```bash
   kubectl -n shop-prod logs -l app=latency-synthesizer -c synth --tail=20
   ```
   Expect: log lines like `published 20 CW metric entries` every 10s.

3. **Collector exporting**
   ```bash
   kubectl -n shop-prod logs -l app=latency-synthesizer -c adot-collector --tail=30
   ```
   Expect: `TracesExporter` lines from the logging exporter; no `awsxray`
   error backoffs.

4. **X-Ray service map** (AWS console → X-Ray → Service map → last 5min)
   - Five nodes: frontendservice, checkoutservice, cartservice,
     productcatalogservice, paymentservice.
   - Edges match [synthesizer/topology.py](../synthesizer/topology.py).
   - All green; latency numbers near healthy baseline.

5. **CloudWatch metrics** (AWS console → CloudWatch → Metrics →
   `RetailProd/Services`)
   - LatencyP50, LatencyP99, ErrorRate, RequestCount per Service dimension.
   - Updated every ~10s.

## Live scenario switch (smoke test)

```bash
./demo/demo spike
# wait 30-60s
kubectl -n shop-prod logs -l app=latency-synthesizer -c synth --tail=10
# X-Ray: paymentservice latency should jump; trace duration histogram bimodal

./demo/demo healthy
# wait 30-60s, latencies return to baseline
```

## End-to-end alarm path smoke test

If you don't want to wait for a real spike to drive the alarm:

```bash
./demo/demo test-alarm    # forces alarm into ALARM state once
./demo/demo lambda-logs   # in another terminal — should see the invocation
```

Then check Slack `#retail-prod-incidents`:
- Chatbot posts a CloudWatch alarm card (only if Slack params are set
  in `demo/.env`).
- Agent posts an investigation thread within ~5s of the Lambda firing.

If the Lambda errors:
- `./demo/demo dlq` shows DLQ depth (>0 means messages are stuck).
- Look at CloudWatch Logs for `/aws/lambda/retail-prod-alarm-to-agent`
  for the actual error.
- Most common cause: `AGENT_URL` doesn't resolve (agent LB not ready,
  or wrong port). Re-run `bash demo/deploy-aws.sh` to refresh.

## Troubleshooting

### Pod stuck in `Pending`
- `kubectl -n shop-prod describe pod -l app=latency-synthesizer`
- Most likely: image pull error. Check `SYNTHESIZER_IMAGE` is correct and
  the registry allows anonymous pulls (or add `imagePullSecrets`).

### Collector logs `AccessDenied` from xray:PutTraceSegments
- IRSA not bound. Check the SA annotation:
  ```bash
  kubectl -n shop-prod get sa latency-synthesizer -o yaml | grep role-arn
  ```
- If empty, re-run `bash demo/deploy-k8s.sh` (it re-renders annotations).

### CW metrics never appear
- Synth logs show `PutMetricData failed`? Check the IRSA role policy
  scope — it allows only namespace `RetailProd/Services` (override via
  `CW_NAMESPACE` env on the deployment if you change it).

### X-Ray service map is empty
- Sampling rule applied? `aws xray get-sampling-rules` should list
  `retail-prod-services-100pct` at priority 100.
- Resources? The OTel resource attribute `service.name` must equal one of
  the five names — confirm via the collector's logging exporter output.

### Alarm fires but agent never starts an investigation
1. Check the Lambda invocation:
   ```bash
   aws logs tail /aws/lambda/retail-prod-alarm-to-agent --since 5m
   ```
   If you see `Skipping NewStateValue=...`, the alarm went OK rather
   than ALARM — check the threshold.
2. If the Lambda logs `POST to agent failed`, the agent isn't reachable
   from Lambda. Confirm:
   ```bash
   curl -sf "$AGENT_URL/healthz" || echo "agent unreachable"
   ```
3. DLQ depth:
   ```bash
   ./demo/demo dlq
   ```
   Non-zero means SNS retried twice and gave up. Drain by purging
   the DLQ once you've fixed the root cause.

### Chatbot posts no card even though the alarm fires
- `aws chatbot describe-slack-channel-configurations` — is the
  `retail-prod-incidents` config present? (Only created if both
  `SLACK_WORKSPACE_ID` and `SLACK_CHANNEL_ID` are set in `demo/.env`.)
- Is the AWS Chatbot app a member of `#retail-prod-incidents`?
  In Slack: `/invite @aws`.
- Workspace authorization expired — see [aws/chatbot-config.md](../aws/chatbot-config.md).
