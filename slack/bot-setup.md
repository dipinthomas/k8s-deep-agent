# Slack Bot Setup

This guide creates the Slack app and bot needed for the K8s agent demo.

---

## 1. Create the Slack App

1. Go to https://api.slack.com/apps → **Create New App** → **From scratch**
2. Name: `K8s Agent` · Workspace: your demo workspace
3. Click **Create App**

---

## 2. Enable Socket Mode

Socket Mode lets the bot receive events without a public URL — ideal for a demo laptop.

1. In your app settings: **Settings → Socket Mode** → toggle **Enable Socket Mode**
2. Generate an App-Level Token:
   - Token name: `demo-token`
   - Scope: `connections:write`
   - Click **Generate**
   - Copy the `xapp-...` token → set as `SLACK_APP_TOKEN` in `agent/.env`

---

## 3. Configure Bot Token Scopes

**OAuth & Permissions → Bot Token Scopes** — add these:

| Scope | Purpose |
|-------|---------|
| `chat:write` | Post messages |
| `chat:write.public` | Post to channels without joining |
| `channels:read` | Read channel list |
| `channels:history` | Read messages (for event handling) |

---

## 4. Subscribe to Events

**Event Subscriptions** → toggle **Enable Events**

Under **Subscribe to bot events**, add:
- `message.channels` — fires when a message is posted to a channel

---

## 5. Enable Interactivity (for Approve/Deny buttons)

**Interactivity & Shortcuts** → toggle **Interactivity**

- Request URL: leave blank (Socket Mode handles this automatically)

---

## 6. Install the App to Your Workspace

**OAuth & Permissions** → **Install to Workspace** → **Allow**

Copy the **Bot User OAuth Token** (`xoxb-...`) → set as `SLACK_BOT_TOKEN` in `agent/.env`

---

## 7. Add the Bot to #k8s-alerts

In Slack: open #k8s-alerts → **Add apps** → search `K8s Agent` → **Add**

Get the channel ID:
- Right-click the channel → **Copy link**
- The ID is the last segment: `C0123456789`
- Set as `SLACK_CHANNEL_ID` in `agent/.env`

---

## 8. Configure SNS → Slack for CloudWatch Alarms

The CloudWatch alarm needs to post to Slack. The simplest approach for a demo
is using AWS Chatbot:

1. Go to **AWS Chatbot** in the AWS Console
2. **Configure a client** → **Slack**
3. Authorize AWS Chatbot to your Slack workspace
4. Create a channel configuration:
   - Channel: #k8s-alerts
   - SNS topics: your alarm SNS topic ARN
5. Use the SNS topic ARN in the CloudWatch alarm config (see README.md Step 4)

Alternatively, use a Lambda function that posts to Slack via webhook.

---

## 9. Verify Everything Works

Test by posting manually:

```bash
curl -X POST https://slack.com/api/chat.postMessage \
  -H "Authorization: Bearer $SLACK_BOT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "channel": "'$SLACK_CHANNEL_ID'",
    "text": "✅ K8s Agent bot connected and ready."
  }'
```

You should see the message appear in #k8s-alerts.

---

## 10. Environment Variables Summary

```bash
SLACK_BOT_TOKEN=xoxb-...       # From OAuth & Permissions
SLACK_APP_TOKEN=xapp-...       # From Socket Mode
SLACK_SIGNING_SECRET=...       # From Basic Information → App Credentials
SLACK_CHANNEL_ID=C...          # Channel ID for #k8s-alerts
```
