# AWS Chatbot — one-time Slack workspace authorization

The `AWS::Chatbot::SlackChannelConfiguration` resource in
[demo-workload.yaml](cloudformation/demo-workload.yaml) is fully
declarative — but it depends on the AWS account having authorized the
AWS Chatbot Slack app for the workspace. That authorization is a manual
console step that cannot be automated.

Do this **once per AWS account + Slack workspace pair**, then leave it.
After this, every future `deploy-aws.sh` is fully hands-off as long as
`SLACK_WORKSPACE_ID` and `SLACK_CHANNEL_ID` stay valid.

## Steps

1. AWS console → search for **Chatbot** → open it.
2. Left nav → **Configured clients** → **Configure new client**.
3. Pick **Slack** → **Configure**.
4. You'll be redirected to Slack. Approve the AWS Chatbot app for the
   workspace.
5. Back in the AWS console, you'll see the workspace listed with an ID
   like `Txxxxxxxx`. Copy this — that's `SLACK_WORKSPACE_ID`.
6. In Slack, find the channel ID for `#retail-prod-incidents`:
   - Right-click the channel → **View channel details** → scroll to the
     bottom. The ID is `Cxxxxxxxx`.
7. Add `#retail-prod-incidents` to the AWS Chatbot Slack app:
   - In Slack: `/invite @aws` in the channel.
8. **No env edit needed if the agent is already deployed.** `deploy-aws.sh`
   reads `SLACK_TEAM_ID` and `SLACK_CHANNEL_ID` straight out of the
   `k8s-agent-secrets` Secret in the `k8s-agent` namespace
   ([infra/agent-secrets.yaml](../../infra/agent-secrets.yaml)).
   Override in `demo/.env` only if you want Chatbot
   to post to a different channel than the agent itself uses:
   ```bash
   export SLACK_WORKSPACE_ID=Txxxxxxxx
   export SLACK_CHANNEL_ID=Cxxxxxxxx
   ```
9. Re-run `bash demo/deploy-aws.sh`. The stack now creates `ChatbotRole`
   and `ChatbotChannel`, subscribes Chatbot to the SNS topic, and starts
   posting alarm cards.

## Verifying

After `deploy-aws.sh` completes with Slack params set:

```bash
aws chatbot describe-slack-channel-configurations \
  --query "SlackChannelConfigurations[?ConfigurationName=='retail-prod-incidents']"
```

Should return one entry with `SnsTopicArns` containing the
`retail-prod-incidents` topic ARN.

Then trigger a smoke test:

```bash
./demo/demo test-alarm
```

A CloudWatch alarm card should arrive in `#retail-prod-incidents` within
~10 s, and the agent's investigation thread within a few seconds after.

## If the workspace authorization is revoked

CFN will fail the `AWS::Chatbot::SlackChannelConfiguration` resource on
the next stack update. Re-do steps 1-4 above, then re-run deploy.

## Removing Chatbot from the stack

Override the secret-derived values to empty in `demo/.env`:

```bash
export SLACK_WORKSPACE_ID=
export SLACK_CHANNEL_ID=
```

Re-run `deploy-aws.sh`. The `EnableChatbot` condition becomes false and
CFN deletes `ChatbotRole` + `ChatbotChannel`. The rest of the stack
(SNS topic, alarm, Lambda, DLQ) is unaffected.
