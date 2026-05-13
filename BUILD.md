# Build & push the agent image

The agent image is multi-arch (`linux/amd64` + `linux/arm64`) so the same tag
runs on Intel/AMD nodes and Graviton nodes without a per-arch fork. Build is
done with `docker buildx`; push is to Docker Hub at
`dipinthomas2003/k8s-deep-agent:<tag>`.

This doc covers the build that happens on a machine separate from the cluster
admin laptop. The build machine only needs Docker + Docker Hub access; it
does **not** need AWS credentials or kubeconfig.

---

## 1. Prerequisites

On the build machine:

- **Docker** with `buildx` (Docker 20.10+, or any modern Docker Desktop /
  Colima).
- **Docker Hub login** as a user that can push to `dipinthomas2003/*`:

  ```bash
  docker login -u dipinthomas2003
  ```

- The repo cloned at the branch you want to build:

  ```bash
  git clone https://github.com/<owner>/k8s-deep-agent.git
  cd k8s-deep-agent
  git checkout fix/more-details-allowed-decisions
  ```

### macOS note (Colima)

If the build machine is a Mac running Colima instead of Docker Desktop:

```bash
colima start            # start the VM (only if not already running)
docker context use colima
```

Colima ships its own `buildx`, so step 2 below works as-is.

---

## 2. One-time: create the multi-arch builder

The build script uses a builder named `multiarch-builder`. Create it once
per machine; reuse it for every subsequent build:

```bash
docker buildx create --name multiarch-builder --use --bootstrap
docker buildx inspect multiarch-builder | grep -E 'Platforms|Status'
```

Expected output includes:

```
Status:    running
Platforms: linux/amd64, linux/arm64, ...
```

If the builder already exists from a previous build, `docker buildx use
multiarch-builder` is enough — no need to recreate.

---

## 3. Build & push v40

Two ways: the helper script (recommended), or the raw `buildx` command.

### Option A — script (recommended)

```bash
SKIP_BUILD=false bash infra/update-agent.sh v40
```

> The script also runs `kubectl set image` + `kubectl rollout status` after
> the push. **On a build-only machine without kubeconfig, those steps will
> fail** — that's fine; the build/push will already have completed. To skip
> the rollout entirely, use Option B.

### Option B — raw buildx (build & push only, no rollout)

```bash
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  --builder multiarch-builder \
  -t dipinthomas2003/k8s-deep-agent:v40 \
  -f agent/Dockerfile \
  --push .
```

Run this from the **repo root** (the trailing `.` is the build context).

---

## 4. Verify the push

```bash
docker buildx imagetools inspect dipinthomas2003/k8s-deep-agent:v40
```

Should list two manifests: one for `linux/amd64`, one for `linux/arm64`.

---

## 5. Roll out (on the cluster-admin machine)

The build machine's job ends at step 4. On the machine with kubeconfig +
AWS credentials:

```bash
git pull                                          # pick up the v40 manifest
AWS_PROFILE=your-aws-profile kubectl apply -f infra/agent-deployment.yaml
kubectl rollout status deployment/k8s-agent -n k8s-agent --timeout=300s
kubectl logs -n k8s-agent deployment/k8s-agent -f
```

The deployment manifest in this branch already pins `image: dipinthomas2003/k8s-deep-agent:v40`, so `kubectl apply` is sufficient — no manual `kubectl set image` needed.

---

## What's in v40 (this branch)

- Fix: MORE DETAILS Slack button — `respond` decision type whitelisted in
  the HITL gate config ([agent/agent.py](agent/agent.py)).
- Default model switched from Anthropic Sonnet 4.5 → OpenAI gpt-5-mini
  ([infra/agent-deployment.yaml](infra/agent-deployment.yaml),
  [agent/agent.py](agent/agent.py)).
- kubectl-mcp sidecar memory bumped (128Mi/512Mi → 256Mi/1Gi) to address
  the OOMKill seen under sustained investigation traffic
  ([infra/agent-deployment.yaml](infra/agent-deployment.yaml)).
