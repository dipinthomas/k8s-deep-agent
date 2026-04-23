# Fault Injection — Disk Pressure Demo

This directory contains scripts to trigger and reset the disk pressure scenario
used in the NZ Tech Rally 2026 demo.

---

## The Scenario

The failure is caused by two compounding issues:

1. **imageprovider** nginx is set to `NGINX_LOG_LEVEL=debug`, writing large access
   logs to ephemeral storage with every HTTP request.

2. **loadgenerator** is cranked up to 500 concurrent users, multiplying the nginx
   log write rate by ~50x.

Together, these fill the node's ephemeral disk from healthy (~40%) to the alarm
threshold (>80%) in approximately 3–5 minutes.

The OTel collector's emptyDir buffer grows slightly under load — this is the
**intentional red herring** the agent investigates first before pivoting to the
real cause (imageprovider).

---

## Pre-Demo Checklist

Run through this list before going on stage:

- [ ] `kubectl get pods -n otel-demo` — all pods Running
- [ ] `kubectl describe node | grep -A5 Conditions` — no DiskPressure
- [ ] CloudWatch alarm state is OK (not ALARM)
- [ ] Slack bot is posting to #k8s-alerts
- [ ] Agent is running (`python main.py` in a terminal)
- [ ] LangSmith trace view is open (optional, for post-talk questions)

---

## Triggering the Failure

Run this ~5 minutes before the demo moment in your talk:

```bash
bash fault-injection/trigger-disk-pressure.sh
```

The CloudWatch alarm fires when `node_filesystem_utilization` exceeds 80% for
2 consecutive 60-second periods (i.e., about 2 minutes after crossing 80%).

**Timing tip:** Start the script during your "before" slide. By the time you
reach the live demo section (~5 minutes later), the alarm will have fired and
the agent will be mid-investigation.

---

## Monitoring During the Demo

```bash
# Node disk % (refreshes every 10s)
watch -n10 "kubectl describe node | grep -A3 'Allocatable'"

# Top disk-writing pods
watch -n5 "kubectl top pods -n otel-demo"

# Recent events
kubectl get events -n otel-demo --sort-by=.lastTimestamp | tail -20
```

---

## Resetting After the Demo

```bash
bash fault-injection/reset-cluster.sh
```

This:
- Scales loadgenerator back down to 10 users
- Restores nginx log level to `warn`
- Restores ephemeral-storage limits
- Restarts any evicted pods

The cluster will be healthy and ready for another run within 2–3 minutes.

---

## Troubleshooting

**Alarm fires too slowly:**
- Reduce the alarm threshold from 80% to 70% in the CloudWatch alarm config
- Or pre-fill some disk: `kubectl exec -n otel-demo deployment/loadgenerator -- dd if=/dev/zero of=/tmp/fill bs=1M count=500`

**Alarm fires too fast:**
- Increase `LOCUST_USERS` ramp-up time in the script
- Reduce `LOCUST_SPAWN_RATE` from 50 to 10

**Agent doesn't pick up the Slack message:**
- Check `python main.py` logs for errors
- Verify `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` are set correctly
- Ensure the Slack bot is added to #k8s-alerts channel
