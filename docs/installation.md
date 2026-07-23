# Installation Guide

## Requirements

1. Python 3.11+
2. `pipx` (recommended) or `pip`
3. At least one authenticated provider CLI:
   - Claude Code CLI: `npm install -g @anthropic-ai/claude-code && claude auth`
   - Codex CLI: `npm install -g @openai/codex && codex auth`
   - Gemini CLI: `npm install -g @google/gemini-cli` and authenticate in `gemini`.
     Note: since 2026-06-18 Gemini CLI no longer serves requests for individual
     Google accounts (free/Pro/Ultra) — it requires an API key or a Gemini Code
     Assist license. For the free individual tier, use Antigravity (`agy`) instead.
4. One of these messaging transports:
    - **Telegram**: Bot token from [@BotFather](https://t.me/BotFather) + user ID from [@userinfobot](https://t.me/userinfobot)
    - **Matrix**: install Matrix support first (`ductor install matrix` or `pip install \"ductor[matrix]\"`), then provide homeserver URL, user ID, and password/access token
    - **Slack**: install Slack support first (`pip install "ductor[slack]"`), then create a Slack app with Socket Mode, the bot/app scopes below, and provide bot/app tokens plus Slack member/channel IDs for the allowlist
5. Docker optional (recommended for sandboxing)

## Install

### pipx (recommended)

```bash
pipx install ductor
```

### pip

```bash
pip install ductor
```

### from source

```bash
git clone https://github.com/PleasePrompto/ductor.git
cd ductor
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## First run

```bash
ductor
```

On first run, onboarding does:

- checks Claude/Codex/Gemini/Antigravity auth status,
- asks which transport to use (Telegram, Matrix, or Slack),
- collects transport credentials,
- asks timezone,
- offers Docker sandboxing (with optional AI/ML package selection),
- offers service install,
- writes config and seeds `~/.ductor/`.

Multiple transports can run in parallel (e.g. Telegram + Slack
simultaneously). After initial setup, configure the `transports` array
in `config.json`. See [config.md](config.md) for details.

If service install succeeds, onboarding returns without starting foreground bot.

## Slack setup

ductor's Slack transport uses **Slack Bolt + Socket Mode** — no public webhook URL is needed.

```bash
pip install "ductor[slack]"   # or: ductor install slack
```

You need a Slack app with Socket Mode, the required bot scopes and event subscriptions, plus a bot token (`xoxb-...`) and app-level token (`xapp-...`) for `slack.bot_token` / `slack.app_token`.

Full step-by-step guide (app creation, scopes, events, config, troubleshooting): [`docs/slack-setup.md`](slack-setup.md)

## Platform notes

### Linux

```bash
sudo apt update && sudo apt install python3 python3-pip python3-venv nodejs npm
pip install pipx
pipx ensurepath
pipx install ductor
ductor
```

Optional Docker:

```bash
sudo apt install docker.io
sudo usermod -aG docker $USER
```

### macOS

```bash
brew install python@3.11 node pipx
pipx ensurepath
pipx install ductor
ductor
```

### Windows (native)

```powershell
winget install Python.Python.3.11
winget install OpenJS.NodeJS
pip install pipx
pipx ensurepath
pipx install ductor
ductor
```

Native Windows is fully supported, including service management via Task Scheduler.

### Windows (WSL)

WSL works too. Install like Linux inside WSL.

```bash
sudo apt update && sudo apt install python3 python3-pip python3-venv nodejs npm
pip install pipx
pipx ensurepath
pipx install ductor
ductor
```

## Docker sandboxing

Enable in config:

```json
{
  "docker": {
    "enabled": true
  }
}
```

Notes:

- Docker image is built on first use when missing.
- Container is reused between calls.
- On Linux, ductor maps UID/GID to avoid root-owned files.
- If Docker setup fails at startup, ductor logs warning and falls back to host execution.

Docker CLI shortcuts:

```bash
ductor docker enable
ductor docker disable
ductor docker rebuild
ductor docker mount /path/to/project
ductor docker unmount /path/to/project
ductor docker mounts
ductor docker extras
ductor docker extras-add <id>
ductor docker extras-remove <id>
```

- `enable` / `disable` toggles `docker.enabled` in `config.json` (restart bot afterwards).
- `rebuild` stops the bot, removes container + image, and forces fresh build on next start.
- `mount` / `unmount` manage `docker.mounts` entries.
- mounts are available in-container under `/mnt/<name>` (basename-based mapping with collision suffixes).
- run `ductor docker mounts` to inspect effective mapping and broken paths.
- `extras` lists all optional packages with their selection status.
- `extras-add` / `extras-remove` manage optional AI/ML packages (Whisper, PyTorch, OpenCV, etc.) in `config.json`. Transitive dependencies are resolved automatically.
- after changing extras, run `ductor docker rebuild` to apply. Build output is streamed live to the terminal.

## Direct API server (optional)

Preferred enable path:

```bash
ductor api enable
```

This writes/updates the `api` block in `config.json` and generates a token if missing.

`ductor api enable` requires PyNaCl (used for E2E encryption against the direct API). PyNaCl is **only needed when the direct WebSocket API is enabled** — the core bot, Telegram, and Matrix transports run without it. If it is missing:

```bash
# pipx install
pipx inject ductor PyNaCl

# pip install
pip install "ductor[api]"
```

Manual config equivalent:

```json
{
  "api": {
    "enabled": true,
    "host": "0.0.0.0",
    "port": 8741,
    "token": "",
    "chat_id": 0,
    "allow_public": false
  }
}
```

Notes:

- token is generated and persisted by `ductor api enable` (runtime also generates it on API start if still empty).
- WebSocket auth frame must include `type="auth"`, `token`, and `e2e_pk` (client ephemeral public key).
- endpoints:
  - WebSocket: `ws://<host>:8741/ws`
  - health: `GET /health`
  - file download: `GET /files?path=...` (Bearer token)
  - file upload: `POST /upload` (Bearer token, multipart)
- default API session uses `api.chat_id` by truthiness (`0` falls back), else first `allowed_user_ids` entry (fallback `1`); clients can override `chat_id` in auth payload.
- recommended deployment is a private network (for example Tailscale).

## Background service

Install:

```bash
ductor service install
```

Manage:

```bash
ductor service status
ductor service start
ductor service stop
ductor service logs
ductor service uninstall
```

Backend details and platform quirks: [Service Management](modules/service_management.md)

Backends:

- Linux: `systemd --user` service `~/.config/systemd/user/ductor.service`
- macOS: Launch Agent `~/Library/LaunchAgents/dev.ductor.plist`
- Windows: Task Scheduler task `ductor`

Linux note:

- user services survive logout/start on boot only when user linger is enabled (`sudo loginctl enable-linger <user>`). Installer attempts this and prints a hint when it cannot set linger.

Windows note:

- service install prefers `pythonw.exe -m ductor_bot` (no visible console window),
- installed Task Scheduler service uses logon trigger + restart-on-failure retries,
- some systems require elevated terminal permissions for Task Scheduler operations.

Log command behavior:

- Linux: live `journalctl --user -u ductor -f`
- macOS/Windows: recent lines from `~/.ductor/logs/agent.log` (fallback newest `*.log`)

## VPS notes

Small Linux VPS is enough. Typical path:

```bash
ssh user@host
sudo apt update && sudo apt install python3 python3-pip python3-venv nodejs npm docker.io
pip install pipx
pipx ensurepath
pipx install ductor
ductor
```

Security basics:

- keep SSH key-only auth
- enable Docker sandboxing for unattended automation
- keep `allowed_user_ids` restricted
- use `/upgrade` or `pipx upgrade ductor`

## Troubleshooting

### Bot not responding

1. check transport credentials (`telegram_token` / `matrix` block) + allowlists
2. run `ductor status`
3. inspect `~/.ductor/logs/agent.log`
4. run `/diagnose` in chat

### CLI installed but not authenticated

Authenticate at least one provider and restart:

```bash
claude auth
# or
codex auth
# or
# authenticate in gemini CLI
```

### Docker enabled but not running

```bash
docker info
```

Then validate `docker.enabled` + image/container names in config.

### Webhooks not arriving

- set `webhooks.enabled: true`
- expose `127.0.0.1:8742` through tunnel/proxy when external sender is used
- verify auth settings and hook ID

## Upgrade and uninstall

Upgrade:

```bash
pipx upgrade ductor
```

Uninstall:

```bash
pipx uninstall ductor
rm -rf ~/.ductor  # optional data removal
```
