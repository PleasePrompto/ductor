# Slack Setup Guide

ductor's primary transport is Telegram. Slack is an optional transport you can add at any time — either as the only transport or running alongside Telegram and/or Matrix. The Slack transport uses **Slack Bolt + Socket Mode**, so no public webhook URL is needed.

## 1. Install Slack support

Slack requires the `slack-bolt` library, which is not included in the base install.

```bash
# pipx (recommended)
ductor install slack

# pip
pip install "ductor[slack]"

# from source
pip install -e ".[slack]"
```

## 2. Create a Slack app

1. Go to <https://api.slack.com/apps>
2. Click **Create New App**
3. Choose **From scratch**
4. Pick a name and workspace

### Bot token scopes

In **OAuth & Permissions → Scopes → Bot Token Scopes**, add:

| Scope | Required | Purpose |
|---|---|---|
| `chat:write` | yes | send bot replies |
| `app_mentions:read` | yes | detect `@bot` in channels |
| `channels:history` | yes | read public-channel messages and thread history |
| `channels:read` | yes | resolve public channel metadata |
| `groups:history` | recommended | read private-channel messages and thread history |
| `im:history` | yes | read DMs |
| `im:read` | yes | access DM metadata |
| `im:write` | yes | open/manage DMs |
| `users:read` | yes | resolve Slack user names |
| `files:read` | yes | download attached files |
| `files:write` | yes | upload files back to Slack |
| `groups:read` | optional | resolve private-channel metadata |

Without `channels:history` / `message.channels`, the bot works in DMs but not in public channels. Without `groups:history` / `message.groups`, it does not work in private channels.

### Socket Mode

In **Settings → Socket Mode**:

1. Turn Socket Mode on
2. Create an app-level token
3. Grant it the `connections:write` scope
4. Copy the resulting `xapp-...` token — this goes into `slack.app_token`

### Event subscriptions

In **Event Subscriptions → Subscribe to bot events**, add:

| Event | Required | Purpose |
|---|---|---|
| `message.im` | yes | direct messages |
| `message.channels` | yes | public-channel messages |
| `message.groups` | recommended | private-channel messages |
| `app_mention` | yes | mention handling in channels |

### Direct messages

In **App Home**:

1. Turn on **Messages Tab**
2. Enable **Allow users to send Slash commands and messages from the messages tab**

Without this, users cannot DM the bot even if tokens and scopes are correct.

ductor does not register native Slack slash commands. Its command keywords work as normal messages (for example `help`, `status`, or `model`) and also accept a leading `/`.

### Install the app

In **Install App**, click **Install to Workspace** and authorize the app. Copy the **Bot User OAuth Token** (`xoxb-...`) — this goes into `slack.bot_token`.

If you change scopes or event subscriptions later, reinstall the app so Slack applies the new permissions.

## 3. Configure

### Option A: Interactive setup (fresh install)

```bash
ductor
```

The onboarding wizard asks which transport to use. Select **Slack** and follow the prompts for bot token, app token, allowed channels, and allowed users.

### Option B: Add Slack to an existing setup

Edit `~/.ductor/config/config.json`:

```json
{
  "transports": ["telegram", "slack"],

  "telegram_token": "YOUR_TELEGRAM_TOKEN",
  "allowed_user_ids": [123456789],

  "group_mention_only": true,
  "slack": {
    "bot_token": "xoxb-your-slack-bot-token",
    "app_token": "xapp-your-slack-app-token",
    "allowed_channels": ["C0123456789"],
    "allowed_users": ["U0123456789"]
  }
}
```

### Option C: Slack only

```json
{
  "transport": "slack",

  "group_mention_only": true,
  "slack": {
    "bot_token": "xoxb-your-slack-bot-token",
    "app_token": "xapp-your-slack-app-token",
    "allowed_channels": ["C0123456789"],
    "allowed_users": ["U0123456789"]
  }
}
```

Then invite the app into each target channel:

```text
/invite @your-bot-name
```

## 4. Start

```bash
ductor
```

## Configuration reference

| Field | Required | Description |
|---|---|---|
| `bot_token` | yes | Bot User OAuth Token (`xoxb-...`) |
| `app_token` | yes | App-level token with `connections:write` (`xapp-...`) |
| `allowed_channels` | no | Channel IDs (`C...`/`G...`) the bot operates in. Empty = all channels it is invited to |
| `allowed_users` | no | Slack user IDs (`U...`) allowed to interact. Empty = all users |

User and channel IDs are shown in Slack's profile/channel details UI.

## Authorization and behavior

| Setting | Effect |
|---|---|
| `allowed_channels: []` | Bot operates in every channel it is invited to |
| `allowed_channels: ["C..."]` | Bot only reacts in listed channels |
| `allowed_users: ["U..."]` | Only listed users can talk to the bot |
| `group_mention_only: true` | In channels, a conversation starts from a `@bot` mention |

- **DMs**: the bot responds to every allowed user message
- **Channels**: with `group_mention_only: true`, a conversation starts from a top-level `@bot` mention or an `@bot` inside an existing thread
- **Activated threads**: once a thread is activated, follow-up replies in that thread continue the same session without another mention

## Differences from Telegram

| Feature | Telegram | Slack |
|---|---|---|
| Streaming | Live message edits | Native status stream (progress plan), falls back to a single full reply when `streaming.enabled` is off |
| Buttons | Inline keyboards | None — text commands instead |
| Command prefix | `/command` | plain keyword or `/command` |
| Topics | Forum topics (one group) | Native threads |
| Sub-agent setup | `ductor agents add` (interactive) | Manual via `agents.json` |

## Running multiple transports

With `"transports": ["telegram", "slack"]` (Matrix can be added the same way), all transports run in parallel sharing the same orchestrator, sessions, workspace, and CLI processes. Session keys are transport-prefixed in persistence, so conversations do not collide across transports.

## Troubleshooting

**"slack-bolt is required" error:**
- Run `ductor install slack` to install the dependency

**Bot does not react in channels:**
- Check bot token scopes and event subscriptions, then **reinstall the app** — Slack only applies permission changes after a reinstall
- Invite the bot into the channel (`/invite @your-bot-name`)
- If `allowed_channels` is set, verify the channel ID is listed

**DMs do not work:**
- Enable the **Messages Tab** plus message sending in **App Home** (step 2, "Direct messages")

**`invalid_auth` / connection errors on startup:**
- Verify `bot_token` starts with `xoxb-` and `app_token` with `xapp-`
- Confirm Socket Mode is enabled and the app token has `connections:write`
