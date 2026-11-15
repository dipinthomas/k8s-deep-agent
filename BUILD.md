# Build and publish the images

There are two images. Both are multi-arch (`linux/amd64` + `linux/arm64`), so the same tag runs on
Intel/AMD and Graviton nodes.

| Image | Dockerfile | Published tag (tested) |
|---|---|---|
| Agent | `agent/Dockerfile` | `dipinthomas2003/k8s-deep-agent:v119` |
| MCP sidecars (kubectl + CloudWatch) | `infra/mcp-gateway.Dockerfile` | `dipinthomas2003/k8s-mcp-gateway:v3` |

You only need to build your own if you change the code or want them in your own registry.
`infra/deploy.sh` uses the published tags unless you set `AGENT_IMAGE` / `MCP_IMAGE`.

## Option A: let CI do it (recommended)

`.github/workflows/ci.yml` publishes on a tag, after the checks and smoke tests pass:

1. In your fork's GitHub settings, add secrets `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN`
   (and optionally the variable `DOCKERHUB_NAMESPACE` if it differs from the username).
2. Push a tag:

   ```bash
   git tag agent-v120 && git push origin agent-v120   # -> <namespace>/k8s-deep-agent:v120
   git tag mcp-v4 && git push origin mcp-v4           # -> <namespace>/k8s-mcp-gateway:v4
   ```

## Option B: build on your machine

Needs Docker with `buildx`. Create a multi-arch builder once:

```bash
docker buildx create --name multiarch-builder --use --bootstrap
```

Test locally first (native architecture only, fast):

```bash
docker build -t k8s-deep-agent:test -f agent/Dockerfile .
docker build -t k8s-mcp-gateway:test -f infra/mcp-gateway.Dockerfile .
tests/smoke/run.sh tools && tests/smoke/run.sh sidecars
```

Then build and push both architectures. Any registry works (Docker Hub, ECR, GHCR):

```bash
REPO=<your-registry>/<your-namespace>      # e.g. docker.io/alice or 123456789012.dkr.ecr.us-east-1.amazonaws.com
docker login <your-registry>                # or: aws ecr get-login-password | docker login --username AWS --password-stdin <ecr-host>

docker buildx build --platform linux/amd64,linux/arm64 --build-arg VERSION=v120 \
  -t $REPO/k8s-deep-agent:v120 -f agent/Dockerfile --push .
docker buildx build --platform linux/amd64,linux/arm64 \
  -t $REPO/k8s-mcp-gateway:v4 -f infra/mcp-gateway.Dockerfile --push .

docker buildx imagetools inspect $REPO/k8s-deep-agent:v120   # should list amd64 and arm64
```

Run from the repo root (the trailing `.` is the build context). Then deploy with
`AGENT_IMAGE=$REPO/k8s-deep-agent:v120 MCP_IMAGE=$REPO/k8s-mcp-gateway:v4 ./infra/deploy.sh`.

## Rules that keep builds reproducible

- **Never use `:latest`.** Bump the tag for every change, so you always know what's running.
- **Python dependencies** come from the lockfile. To change one, edit `agent/requirements.in` and run
  `uv pip compile requirements.in -o requirements.txt --python-version 3.11 --python-platform linux`
  in `agent/`. CI fails if the lockfile is stale.
- **MCP server versions** are build args in `infra/mcp-gateway.Dockerfile`. The approval gate
  matches tool names, so after bumping them run `tests/smoke/run.sh sidecars` and check every
  tool that changes something still requires approval.

## What's in each tag

- **agent v119:** skills and AGENTS.md load from disk (`/harness/` route); tool results stay
  next to tool_use (Bedrock/Anthropic); approval required for `kubectl_generic`, `exec_in_pod`
  and the helm tools; dependencies locked to the versions verified on the AgentCore lab;
  `langchain-aws` added for Bedrock models.
- **mcp-gateway v3:** multi-arch kubectl v1.36.0; pinned mcp-server-kubernetes 3.5.0,
  supergateway 3.4.3, cloudwatch-mcp-server 0.0.25 (with mcp 1.27.0), installed at build time.
