# Live Demo Website — Spec
## demo.1conceptonline.com (or similar)

A publicly accessible website that lets conference attendees (and anyone online)
interact with a real EKS cluster, break things, watch an AI agent fix them,
and see exactly what is happening — live.

---

## The Three URLs

| URL | What it shows |
|---|---|
| `demo.1conceptonline.com` | Interactive service map — break things, watch agent fix them |
| `demo.1conceptonline.com/cluster` | Live Kubernetes cluster view (pods, nodes, events) |
| `demo.1conceptonline.com/agent` | Live agent log stream — every tool call, every thought |

These three pages can be shown simultaneously on different screens at the
conference — or opened side by side by the audience on their phones.

---

## Page 1 — The Main Demo (`/`)

### What the user sees
- The OTel Astronomy Shop service topology (12 microservices)
- Live health status per service (green / amber / red)
- Right-click any service → inject a fault
- "Solve with AI Agent" button
- Left side: service map
- Right side: agent activity panel (mirrors `/agent` page)

### What happens behind the scenes
1. User right-clicks `imageprovider` → selects "Disk Pressure"
2. Browser calls `POST /api/inject-fault` with `{service: "imageprovider", type: "disk_pressure"}`
3. Backend runs the fault injection script against real EKS
4. Browser opens an SSE stream from `/api/agent-stream`
5. Every tool call the real agent makes streams back to the browser in real-time
6. When agent reaches approval request, an Approve/Deny card appears
7. User clicks Approve → `POST /api/approve` → agent executes evictions
8. Service health indicators update as real pod states change

### Why this is honest
- Tool calls shown are real kubectl and CloudWatch calls
- Latency numbers come from real CloudWatch metrics
- Pod evictions actually happen and can be verified on `/cluster`

---

## Page 2 — Live Cluster View (`/cluster`)

### Short answer: Yes, this is absolutely possible.

The closest thing to k9s in a browser already exists. Three options:

**Option A — Headlamp (recommended)**
- Open source, CNCF project
- Deploy it inside EKS, expose via Load Balancer or ingress
- Looks and feels exactly like k9s but in a browser
- Shows pods, nodes, events, logs, resource usage — all live
- GitHub: https://github.com/headlamp-k8s/headlamp

**Option B — Kubernetes Dashboard (official)**
- The standard official dashboard
- More basic than Headlamp but very well known
- Easy to deploy: `kubectl apply -f kubernetes-dashboard.yaml`

**Option C — Custom lightweight view (build it)**
- Small Next.js page that calls `kubectl get pods -n otel-demo --watch`
- Streams output via SSE to browser
- Simpler, more focused — only shows what matters for the demo
- Audience sees exactly: pod name, status, restarts, age

### What the audience can verify
After the agent evicts `imageprovider`, anyone watching `/cluster` sees:
```
NAMESPACE   NAME                          STATUS      RESTARTS
otel-demo   imageprovider-5c8a2d-xxx      Terminating    0
otel-demo   imageprovider-5c8a2d-xxx      (gone)
otel-demo   checkoutservice-8f3b1a-xxx    Running        0   ← still alive
```

This is the proof moment. The agent said it evicted those pods. The cluster view
confirms it actually happened. No smoke and mirrors.

### Recommended approach for the talk
Deploy Headlamp. Put it at `/cluster`. Point the audience to it.
During the demo, have it open on a second monitor or second tab.
When the agent evicts pods, switch to the cluster view — audience sees
the pods disappearing in real-time.

---

## Page 3 — Live Agent Logs (`/agent`)

### What it shows
A terminal-style streaming view of everything the agent is doing:
- Every tool call with its input and output
- The agent's reasoning steps
- The wrong hypothesis and the re-plan
- The approval request
- The resolution

### How it works technically
**Server-Sent Events (SSE)** — the simplest reliable approach.

```
Browser opens: GET /api/agent-stream
Server keeps connection open
Every time agent emits a tool call → server pushes an event
Browser appends it to the terminal view
```

This works over HTTP, no WebSocket infrastructure needed,
works through proxies and load balancers, reconnects automatically.

### What the stream looks like
```
[kubectl]     get nodes
              → ip-10-0-1-45  DiskPressure=True  87%

[cloudwatch]  GetMetricData node_filesystem_utilization
              → 15min: 71% → 79% → 87%

[agent]       Hypothesis 1: OTel Collector buffer overflow

[kubectl]     describe pod otelcol-0 -n otel-demo
              → emptyDir: 2.1GB / 5.0GB (42%) — normal

[agent]       ✗ Hypothesis 1 rejected. Re-planning.

[agent]       Hypothesis 2: imageprovider verbose logging

[kubectl]     describe deploy/imageprovider -n otel-demo
              → NGINX_LOG_LEVEL=debug  ← MISCONFIGURATION

[cloudwatch]  GetMetricData container_fs_writes imageprovider
              → 340MB/8min — 12x above baseline

[agent]       ✓ Root cause confirmed. Building approval request.

[agent]       ⏸ PAUSED — awaiting human approval in Slack
```

### Why this page matters for the talk
The audience can open this on their phones.
While the demo is running on the big screen, they are watching
the raw agent thinking on their own device.
This is more engaging than a slide. They feel like they are inside the system.

---

## Technical Architecture

```
                        ┌─────────────────────────────┐
                        │   demo.1conceptonline.com   │
                        │   (Next.js or Vite on EKS)  │
                        └───────────┬─────────────────┘
                                    │
              ┌─────────────────────┼─────────────────────┐
              │                     │                     │
         /                     /cluster              /agent
    Service map            Headlamp UI           SSE log stream
    Fault injection        (deployed on          (from real agent
    Approve/Deny           EKS, proxied)          running on EKS)
              │                                         │
              ▼                                         ▼
    POST /api/inject-fault                   GET /api/agent-stream
    POST /api/approve                        (Server-Sent Events)
              │                                         │
              ▼                                         ▼
    ┌─────────────────────────────────────────────────────┐
    │              Agent Service (Python)                 │
    │              Deep Agents + LangGraph                │
    │              Running as K8s deployment              │
    └──────────┬──────────────────────┬───────────────────┘
               │                      │
    ┌──────────▼──────┐    ┌──────────▼──────────┐
    │  MCP Servers    │    │  Long-term memory   │
    │  - kubectl      │    │  LangGraph Store    │
    │  - cloudwatch   │    └─────────────────────┘
    │  - slack        │
    └─────────────────┘
```

---

## What to Build (In Order)

- [ ] 1. Deploy Headlamp on EKS → verify `/cluster` works
- [ ] 2. Build the SSE endpoint `/api/agent-stream` → pipe real agent logs
- [ ] 3. Build the fault injection API `POST /api/inject-fault`
- [ ] 4. Build the approval API `POST /api/approve`
- [ ] 5. Build the frontend service map (React — see `k8s-agent-demo.jsx` for reference UI)
- [ ] 6. Connect frontend to real SSE stream (replace simulated script)
- [ ] 7. Deploy everything under `demo.1conceptonline.com`
- [ ] 8. Add rate limiting / abuse protection (public site, real cluster)
- [ ] 9. Test full end-to-end with fault injection → agent → approval → recovery
- [ ] 10. Record demo video from this live site

---

## Security Considerations

This is a real cluster exposed publicly. Important constraints:

**Fault injection must be scoped**
Only allow pre-defined fault types against the otel-demo namespace.
Never expose raw kubectl access to the internet.
The API validates fault type against an allowlist before executing.

**Approval is required for all destructive actions**
The agent cannot evict pods without going through the approval API.
The approval API requires a token (you hold it, presented via Slack or the UI).

**Rate limiting**
One active incident at a time. If an incident is in progress,
new fault injection requests are queued or rejected.

**Namespace isolation**
The agent's RBAC is scoped to `otel-demo` namespace only.
It cannot touch `kube-system`, the agent deployment itself, or any other namespace.

**Reset endpoint**
`POST /api/reset` — restores all pods and config.
Run this after each demo.

---

## For the Conference Talk

Three ways to use this on stage:

**Option 1 — Show only the main page**
Classic: one screen, fault injection, agent solves it. Clean and focused.

**Option 2 — Split screen**
Left: main demo page with agent panel.
Right: `/cluster` showing pods disappearing and recovering in real-time.
Most impressive visually.

**Option 3 — Audience on their phones**
Put `demo.1conceptonline.com` on a slide.
Ask audience to open `/agent` on their phones.
They watch the raw tool calls while you narrate.
Extremely engaging — they feel inside the system.

---

*This document is the spec for building the live demo site.*
*Reference: `CLAUDE.md` for the full agent architecture and EKS setup.*
*Reference: `k8s-agent-demo.jsx` for the frontend UI prototype.*