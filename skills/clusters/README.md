# Cluster skills

The universal skills (`../universal/`) describe how to investigate any cluster.
A **cluster skill** tells the agent about *your* cluster: what runs where, what
must never be touched, and what "healthy" looks like. The agent loads it into
memory on every run.

Start from [`example/SKILL.md`](example/SKILL.md) and fill it in. A complete,
working one is the lab's [`evals-agentcore/lab-cluster/SKILL.md`](../../evals-agentcore/lab-cluster/SKILL.md).

You don't need to rebuild the image. `infra/deploy.sh` puts your file into a
ConfigMap and mounts it at `skills/clusters/<CLUSTER_NAME>/SKILL.md`:

```bash
CLUSTER_SKILL=path/to/your/SKILL.md ./infra/deploy.sh
```

Re-run `deploy.sh` after editing it; it restarts the agent.
