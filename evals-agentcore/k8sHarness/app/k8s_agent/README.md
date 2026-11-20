# k8s_agent (AgentCore Runtime code)

- `main.py`: the AgentCore entrypoint. It loads the repo's agent from `agent_src/` and swaps the
  EKS plumbing for AgentCore: Gateway tools over SigV4, the AgentCore Memory checkpointer and
  episodic memory, and Slack posts captured in the response.
- `ordered_saver.py`: wraps `AgentCoreMemorySaver` so resumes replay tool results in the right order.
- `agent_src/`, `skills/`, `clusters/`, `AGENTS.md`: copied in by `../../../scripts/sync_agent.sh`
  (gitignored). Don't edit them here. Edit `../../../../agent/` and re-sync.

See `../../../README.md` for how to deploy and run the lab.
