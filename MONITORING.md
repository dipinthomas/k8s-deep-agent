# Live Agent Monitoring Playbook

How I monitored the k8s-agent + redis pods continuously across pod restarts,
deployment rollouts, and ImagePull failures during the v22 → v28 iteration cycle.

The goal: **never miss an event** while the agent runs, and surface only the
signals worth reading (todo lists, tool calls, approval interrupts, failures) —
not the firehose of MCP schema echoes and OpenTelemetry chatter.

---

## 1. Architecture

Two pieces. The first does the work, the second filters it.

```
┌─────────────────────────────────────────────────────────────────────┐
│  /tmp/k8s-agent-monitor/watch.sh   (bash supervisor, daemon)        │
│                                                                     │
│  every 5s:                                                          │
│    ┌─────────────────────────────────────────────────────────────┐  │
│    │ resolve current pod names via labels (Running phase only)   │  │
│    │   app=k8s-agent     → e.g. k8s-agent-86b7744b8d-9gf9z       │  │
│    │   app=agent-redis   → e.g. agent-redis-7b8cb6d988-gt42r     │  │
│    └─────────────────────────────────────────────────────────────┘  │
│                            │                                        │
│                            ▼                                        │
│    ┌─────────────────────────────────────────────────────────────┐  │
│    │ if pod name changed → kill old tails, attach 4 new ones:    │  │
│    │   kubectl logs ... -c agent          --follow --since=5s    │  │
│    │   kubectl logs ... -c kubectl-mcp    --follow --since=5s    │  │
│    │   kubectl logs ... -c cloudwatch-mcp --follow --since=5s    │  │
│    │   kubectl logs ... -c redis          --follow --since=5s    │  │
│    │ each tail:                                                  │  │
│    │   sed "s|^|[$(stamp)] [$tag] |" >> /tmp/.../stream.log      │  │
│    └─────────────────────────────────────────────────────────────┘  │
│                            │                                        │
│                            ▼                                        │
│    ┌─────────────────────────────────────────────────────────────┐  │
│    │ scan k8s pod state via custom-columns:                      │  │
│    │   - alert if restartCount went up                           │  │
│    │   - alert if state.waiting.reason ∈                         │  │
│    │     {CrashLoopBackOff, OOMKilled, ImagePullBackOff,         │  │
│    │      ErrImagePull, RunContainerError, ...}                  │  │
│    │   - alert if pod.status.phase ∈ {Failed, Error}             │  │
│    └─────────────────────────────────────────────────────────────┘  │
│                            │                                        │
│                            ▼                                        │
│    ┌─────────────────────────────────────────────────────────────┐  │
│    │ reap dead tail PIDs → unset LAST_POD → reattach next loop   │  │
│    └─────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
                             │
                             ▼  appends to stream.log
        /tmp/k8s-agent-monitor/stream.log  (timestamped, prefixed)
                             │
                             ▼  tail -F + grep filter
┌─────────────────────────────────────────────────────────────────────┐
│  Monitor (Claude harness tool)                                      │
│                                                                     │
│  tail -F /tmp/k8s-agent-monitor/stream.log \                        │
│    | grep -E --line-buffered \                                      │
│      "MONITOR\]\[ALERT\]|MONITOR\] (AGENT|REDIS) POD|              │
│       MONITOR\] tail .* (ended|stopping)|                          │
│       write_todos|tool_call|kubectl_(get|describe|logs|delete|...)|│
│       cloudwatch_(get|logs|describe)|slack_post|interrupt|approval|│
│       Subagent spawn|hypothesis|root cause|Evicted|                 │
│       CrashLoopBackOff|OOMKilled|Traceback|FATAL|panic:"            │
│                                                                     │
│  Each matching line → one notification in the chat.                 │
└─────────────────────────────────────────────────────────────────────┘
```

The split matters. The supervisor is a dumb, resilient log-pipeliner. The
filter is what makes the firehose readable. Either piece can be replaced
without touching the other.

---

## 2. Bash supervisor (`/tmp/k8s-agent-monitor/watch.sh`)

Key behaviours that took several iterations to get right:

### 2.1 Re-resolve the pod every 5 seconds

K8s pod names change every rollout. Hardcoding a pod name kills the watcher
the moment a deploy happens. Instead, query by label every loop:

```bash
AGENT_POD=$(kubectl get pod -n "$NS" -l app=k8s-agent \
  --field-selector=status.phase=Running \
  -o jsonpath='{.items[0].metadata.name}')
```

Two important nuances:

- **`--field-selector=status.phase=Running`** — without this, you can resolve
  to a pod that's `Terminating` and immediately lose the tails. With it, you
  briefly might resolve the *old* pod during a rollout (because the new pod
  is still in `Init`), but then naturally switch when the new one becomes
  Running. That's correct behaviour.
- **Fallback by name prefix** — if labels are missing or mid-edit, fall back
  to `awk '$1 ~ /^k8s-agent-/ && $3 !~ /Terminating/'`. Belt and braces.

### 2.2 Use `--since=5s`, not `--tail=N`

The temptation is `kubectl logs --tail=50` to see recent context. Don't.
Every reattach (and reattaches happen often during rollouts) replays the
last 50 lines, so you flood the stream with stale data and trigger duplicate
notifications.

`--since=5s` says "only stream lines from the last 5 seconds onwards" — so
reattaches start clean, and you only get genuinely new events.

### 2.3 Don't capture background-PID via `$( )`

This bug burned ~30 minutes on day one. The naive code:

```bash
PIDS[$key]=$(tail_container "$pod" "$container" "$tag")
```

…blocks the loop forever. Reason: `$( )` waits for *all* file descriptors of
the command substitution to close, including the backgrounded subshell's
stdout pipe to `sed`. Since `kubectl logs --follow` keeps that pipe open
indefinitely, `$( )` never returns and the second/third loop iterations
(for the other containers) never run.

Fix: write the PID to a temp file and read it back.

```bash
tail_container() {
  ( kubectl logs ... | sed ... >> "$OUT" ) &
  echo $! > "$DIR/.pid_${key}"
}

# caller:
tail_container "$pod" "$container" "$tag" "$key"
PIDS[$key]=$(cat "$DIR/.pid_${key}")
```

### 2.4 Parse health with `custom-columns`, not raw jsonpath

The first version of the alert path used a jsonpath that dumped raw
`state.running.startedAt` JSON into the column where the parser expected
`true`/`false`. Result: every container looked "NOT READY" and the chat
filled with false alarms.

The robust form is `kubectl get pods -o custom-columns=...` because each
column is rendered as a flat scalar, and missing values become the literal
string `<none>` instead of broken JSON.

```bash
kubectl get pods -n "$NS" \
  -o "custom-columns=POD:.metadata.name,\
PHASE:.status.phase,\
C:.status.containerStatuses[*].name,\
R:.status.containerStatuses[*].ready,\
RC:.status.containerStatuses[*].restartCount,\
RSN:.status.containerStatuses[*].state.waiting.reason" \
  --no-headers
```

Then explode the comma-separated multi-container columns with awk:

```awk
{
  pod=$1; phase=$2;
  if ($3=="<none>") next;
  n=split($3,c,",");  split($4,r,",");
  split($5,rc,",");  split($6,rsn,",");
  for (i=1;i<=n;i++)
    printf "%s\t%s\t%s\t%s\t%s\t%s\n",
           pod, phase, c[i], r[i], rc[i], rsn[i];
}
```

### 2.5 Alert only on real failures

Two alert classes, both important, both narrow:

```bash
# 1. restartCount went up (treat as integer comparison)
if [[ "$prev" =~ ^[0-9]+$ ]] && [[ "$restarts" =~ ^[0-9]+$ ]] \
   && [ "$restarts" -gt "$prev" ]; then
  echo "[$(stamp)] [MONITOR][ALERT] $key restart count $prev -> $restarts ..."
fi

# 2. container reason is a known failure mode
case "$reason" in
  CrashLoopBackOff|Error|OOMKilled|ImagePullBackOff|ErrImagePull|\
  CreateContainerError|RunContainerError)
    echo "[$(stamp)] [MONITOR][ALERT] $key UNHEALTHY reason=$reason ..."
    ;;
esac

# 3. (added later) pod-level Failed/Error phase
case "$phase" in
  Failed|Error)
    echo "[$(stamp)] [MONITOR][ALERT] $key POD $phase ..."
    ;;
esac
```

The pod-phase alert was added after a real `mqs9x` pod went `Error` and the
container-state matcher missed it because the failed pod was already
garbage-collected by the time we polled.

What the watcher deliberately does **not** alert on:
- Brief `Init:N/M` windows during a normal rollout (every deploy would page).
- `Terminating` (every deploy).
- `PodInitializing` (every deploy + every cold start).
- A tail dropping (could be normal API-server idle-timeout).

### 2.6 Reaper for dead tails

Every loop, check if any tail PID has died (`kill -0 "$pid"` returns false).
If so, unset its `LAST_POD[...]` so the next loop forces re-resolution and
reattaches.

This handles two real failure modes:

- The EKS API server idle-times-out `kubectl logs --follow` after ~4 minutes
  of silence. Both MCP sidecars were the most common victims.
- A pod gets terminated mid-tail; the kubectl client exits with an error;
  we need to switch to the new pod next tick.

---

## 3. Notification filter (the Monitor tool)

The supervisor produces a busy stream — every MCP request, every JSON-RPC
schema echo, every OpenTelemetry span dump. Reading it raw is impossible.

The filter keeps **only** these patterns:

| Pattern | Why |
|---|---|
| `MONITOR\]\[ALERT\]` | Real container/pod failures |
| `MONITOR\] (AGENT\|REDIS) POD` | Pod identity changes (rollout boundary) |
| `MONITOR\] tail .* (ended\|stopping)` | Stream lifecycle, helps debug watcher |
| `write_todos` | LLM-emitted plan changes |
| `tool_call` | Any tool invocation |
| `kubectl_(get\|describe\|logs\|top\|exec\|delete\|drain\|cordon)` | K8s ops |
| `cloudwatch_(get\|logs\|describe)` | AWS observability ops |
| `slack_post` | Bot output |
| `interrupt\|approval` | The human-in-loop moments |
| `Subagent spawn\|hypothesis\|root cause` | Investigation milestones |
| `Evicted\|CrashLoopBackOff\|OOMKilled\|Traceback\|FATAL\|panic:` | Failures |

**Crucial flag: `grep --line-buffered`** — without it, pipe buffering delays
notifications by minutes. Without that single flag the whole approach is
useless because you only see batched events long after they happened.

The filter is **selective on what's interesting, not on what's good**. It
includes `Traceback`, `CrashLoopBackOff`, `OOMKilled` — i.e. signs of
trouble — alongside the success-path signals. Silence is not success: a
filter that only matches the happy path will look identical between
"investigation completed" and "agent crashed silently."

---

## 4. Operational tips learned the hard way

### 4.1 Restart cleanly

When you change `watch.sh`, kill the old supervisor *and* its tail children
before relaunching, otherwise you double-stream:

```bash
ps -ef | grep -E 'watch.sh|kubectl logs -n k8s-agent' | grep -v grep \
  | awk '{print $2}' | xargs -r kill 2>/dev/null
sleep 2
nohup bash /tmp/k8s-agent-monitor/watch.sh \
  > /tmp/k8s-agent-monitor/watch.err 2>&1 &
```

### 4.2 Watch out for the Running-phase race

During a rollout, the old pod (`Terminating`) and the new pod (`Init`) both
exist simultaneously. Only one is `Running`. The watcher resolves to the
old `Running` one, attaches, and the tails immediately end because the old
pod is shutting down. Next 5s tick: still old pod is `Running`. Eventually
the new pod becomes `Running` and the watcher switches.

This shows up as a thrash of repeated `tail ... ended → reattach → ended`
notifications for ~10–30 seconds. It's normal and self-resolves.

### 4.3 Use the AWS profile

For this project: `export AWS_PROFILE=fernhub`. Set inside `watch.sh` so
`kubectl` resolves the right cluster regardless of who launched the script.

### 4.4 The Monitor tool can be replaced by your own pipeline

Nothing magical about the harness Monitor tool. The same effect is
reproducible with:

```bash
tail -F /tmp/k8s-agent-monitor/stream.log \
  | grep -E --line-buffered '<your patterns>' \
  | <wherever you want notifications: stdout, slack webhook, syslog, ...>
```

The architecture is: fan-out to a single durable log file, then any number
of readers (with different filters) can consume it independently.

### 4.5 Don't trust the filter for forensics

The filter is for **live operations**. For retrospective debugging
(e.g. "what exactly happened during the v23 approval bug?"), read
`stream.log` directly with `grep`/`awk`/`python` — it's the unfiltered
firehose, and the answer is always in there. The filter strips noise; for
forensics you sometimes need the noise.

---

## 5. What this setup caught during the iteration

Real events surfaced by this pipeline that would've been easy to miss
otherwise:

- **v22 approval-loop bug** — the `before_agent` Overwrite re-entries
  showed up as a clear pattern of repeated full-alarm payloads.
- **v23 "lost context" regression** — token-count signature dropped from
  ~41k to ~21k after a button click, immediately diagnostic.
- **kubectl-mcp `-o=wide` injection** on `kubectl top` — three retries
  visible in sequence.
- **CloudWatch IAM `AccessDeniedException`** for `logs:DescribeQueryDefinitions`
  in `us-east-1` (despite the cluster being in `ap-southeast-2`).
- **`mqs9x` pod silently entered `Error` phase** — caught by the
  pod-phase-level alert that was added in response to this miss.
- **The "no pending interrupt" warning** in v25/v26 — the smoking-gun log
  line that proved `kubectl_delete` was never being queued after
  `post_approval_request`.
- **The v28 fix working end-to-end** — `__interrupt__` fired,
  `Pending interrupt #0` resolved, `pod "demo-stress" deleted` confirmed.

All of those signals were present in the raw `kubectl logs` output, but
without this pipeline they'd have been buried under MCP schema echoes,
OTel spans, and slack_bolt middleware chatter. The whole point of the
filter is making the meaningful 1% legible.

---

## 6. Files

```
/tmp/k8s-agent-monitor/
├── watch.sh        # the supervisor
├── watch.err       # supervisor stderr (mostly empty in steady state)
├── stream.log      # timestamped, prefixed firehose of all 4 containers
├── health.log      # periodic snapshot of `kubectl get pods -n k8s-agent`
└── .pid_<key>      # one file per active tail, holds the PID
```

Re-create from scratch any time — it's a `mkdir -p` + the script.
