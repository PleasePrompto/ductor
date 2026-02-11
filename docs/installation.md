# Installation guide

## What you need

1. Python 3.11 or newer
2. pipx (or pip)
3. At least one CLI installed and authenticated:
   - [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code): `npm install -g @anthropic-ai/claude-code && claude auth`
   - [Codex CLI](https://github.com/openai/codex): `npm install -g @openai/codex && codex auth`
4. A Telegram bot token from [@BotFather](https://t.me/BotFather)
5. Your Telegram user ID from [@userinfobot](https://t.me/userinfobot)
6. Docker (optional, but good to have for sandboxing)

---

## Install ductor

### With pipx (recommended)

```bash
pipx install ductor
```

This gives you `ductor` as a global command in its own isolated environment.

### With pip

```bash
pip install ductor
```

### From source

```bash
git clone https://github.com/PleasePrompto/ductor.git
cd ductor
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

### First run

```bash
ductor
```

The setup wizard runs automatically the first time. It detects your CLIs, asks for your Telegram token and user ID, checks for Docker, and sets your timezone. Everything gets saved to `~/.ductor/`.

---

## Platform-specific notes

### Linux (Ubuntu / Debian)

```bash
# Python 3.11+
sudo apt update && sudo apt install python3 python3-pip python3-venv

# pipx
pip install pipx
pipx ensurepath

# Node.js (for Claude Code / Codex CLI)
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install -y nodejs

# Claude Code CLI
npm install -g @anthropic-ai/claude-code
claude auth

# Docker (optional)
sudo apt install docker.io
sudo usermod -aG docker $USER
# Log out and back in for group changes to take effect

# ductor
pipx install ductor
ductor
```

### macOS

```bash
# Python 3.11+ (via Homebrew)
brew install python@3.11

# pipx
brew install pipx
pipx ensurepath

# Node.js
brew install node

# Claude Code CLI
npm install -g @anthropic-ai/claude-code
claude auth

# Docker (optional)
brew install --cask docker
# Open Docker Desktop to finish setup

# ductor
pipx install ductor
ductor
```

### Windows (WSL)

ductor runs on Windows through WSL. Native Windows won't work because the Claude Code and Codex CLIs need a Unix-like environment.

```powershell
# Install WSL (PowerShell as admin)
wsl --install -d Ubuntu
```

After restarting and setting up your WSL user:

```bash
# Inside WSL, same as Linux
sudo apt update && sudo apt install python3 python3-pip python3-venv nodejs npm

pip install pipx
pipx ensurepath

npm install -g @anthropic-ai/claude-code
claude auth

pipx install ductor
ductor
```

> **Tip:** Docker Desktop for Windows can share its Docker engine with WSL. Enable "Use the WSL 2 based engine" in Docker Desktop settings.

### Windows (native)

Not supported. Use WSL.

---

## Docker sandboxing

Both CLIs have full file system access by default. They can read, write, and delete anything your user can. Docker sandboxing runs the CLI process inside a container image built from `Dockerfile.sandbox`, so it can only touch mounted directories.

### Why bother?

If the agent decides to `rm -rf ~/` or write to `/etc`, the container stops it. For anything that runs unattended (cron jobs, webhooks, heartbeats), this matters.

### Enable it

The setup wizard asks about Docker on first run. To enable it later, edit `~/.ductor/config/config.json`:

```json
{
  "docker": {
    "enabled": true
  }
}
```

ductor builds the image on first use. The container sticks around between calls so startup stays fast.

### Requirements

- Docker installed and running
- Your user in the `docker` group (Linux: `sudo usermod -aG docker $USER`)
- `docker` commands must work without `sudo`

---

## Running 24/7

ductor runs in the foreground by default. For always-on setups, you need something to keep it running after you close the terminal.

### Linux (systemd) -- recommended

The setup wizard offers this automatically. You can also do it manually:

```bash
ductor service install
```

This creates a systemd user service that starts on boot, restarts on crash, and keeps running after you log out. No manual config files needed.

Management commands:

```bash
ductor service status      # Is it running?
ductor service stop        # Stop the service
ductor service start       # Start it again
ductor service logs        # Live log output (Ctrl+C to stop)
ductor service uninstall   # Remove the service completely
```

Under the hood this creates `~/.config/systemd/user/ductor.service` and enables linger so the service survives SSH logout. Linger requires sudo once (`sudo loginctl enable-linger $USER`), which the installer handles.

### macOS (launchd)

macOS doesn't have systemd. Use a launch agent:

```bash
mkdir -p ~/Library/LaunchAgents

cat > ~/Library/LaunchAgents/dev.ductor.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>dev.ductor</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/ductor</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/ductor.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/ductor.err</string>
</dict>
</plist>
EOF
```

Adjust the path to `ductor` if pipx installed it somewhere else (`which ductor`). Then:

```bash
launchctl load ~/Library/LaunchAgents/dev.ductor.plist     # Start
launchctl unload ~/Library/LaunchAgents/dev.ductor.plist   # Stop
```

### Windows (WSL)

WSL doesn't have a real init system. Two options:

**Option A: Windows Task Scheduler (starts with Windows)**

Create a scheduled task that runs at login:

```powershell
# PowerShell as admin
$action = New-ScheduledTaskAction -Execute "wsl" -Argument "-d Ubuntu -- bash -lc ductor"
$trigger = New-ScheduledTaskTrigger -AtLogon
Register-ScheduledTask -TaskName "ductor" -Action $action -Trigger $trigger -RunLevel Limited
```

**Option B: screen inside WSL (survives closing the terminal)**

```bash
screen -dmS ductor ductor    # Start in background
screen -r ductor             # Reattach to see output
# Ctrl+A, D to detach again
```

This survives closing the terminal but not a Windows reboot. Option A handles reboots.

### Quick and dirty (any platform)

If you just need it running temporarily and don't want a proper service:

```bash
# screen
screen -S ductor
ductor
# Ctrl+A, D to detach

# tmux
tmux new -s ductor
ductor
# Ctrl+B, D to detach

# nohup
nohup ductor > /tmp/ductor.log 2>&1 &
```

These survive closing the terminal but not a reboot.

---

## Hosting on a VPS

A $5/month VPS with 1 GB RAM is enough. Any Linux VPS works -- Hetzner, DigitalOcean, Vultr, Linode, whatever you have.

```bash
ssh user@your-vps-ip

# Dependencies
sudo apt update && sudo apt install python3 python3-pip python3-venv nodejs npm docker.io
sudo usermod -aG docker $USER
newgrp docker

# Install ductor
pip install pipx
pipx ensurepath
source ~/.bashrc
pipx install ductor

# Install and authenticate a CLI
npm install -g @anthropic-ai/claude-code
claude auth

# Run setup wizard (offers background service at the end)
ductor
```

The wizard asks whether to install the systemd service. If you skip it, run `ductor service install` later.

### Security basics

- Only open port 22 (SSH). ductor connects outbound to Telegram, nothing inbound needed. Exception: if you use webhooks, open that port too
- Disable password auth for SSH, use keys only
- Enable Docker sandboxing (`docker.enabled: true`)
- Set `allowed_user_ids` in config so only you can use the bot
- Update from Telegram with `/upgrade` or via SSH with `pipx upgrade ductor`

### Resource usage

Idle: ~50-100 MB RAM, almost no CPU. During CLI execution the subprocess uses more, but ductor itself stays light. Disk is about 200 MB for the package and dependencies. The workspace grows over time, and a daily cleanup pass removes old top-level files from `telegram_files/` and `output_to_user/` (default retention: 30 days). All network traffic is outbound to Telegram and the AI provider.

---

## Troubleshooting

### Bot does not answer in Telegram

1. Check config basics:
   - `telegram_token` is valid
   - your numeric ID is in `allowed_user_ids`
2. Check runtime status:

```bash
ductor status
```

3. Check logs:
   - `~/.ductor/logs/agent.log`
4. In Telegram, run:
   - `/diagnose`

### CLI installed but not authenticated

Auth must be valid for at least one provider:

```bash
claude auth
# or
codex auth
```

Then restart ductor.

### Docker enabled but sandbox not starting

1. Verify Docker works without `sudo`:

```bash
docker info
```

2. Check `docker.enabled` and container/image names in `~/.ductor/config/config.json`.
3. If needed, disable Docker temporarily and verify host-mode runtime works.

### Webhooks not arriving

1. Ensure webhook server is enabled in config (`webhooks.enabled: true`).
2. If sender is external, expose `127.0.0.1:8742` with a tunnel or reverse proxy.
3. Confirm auth mode and token/signature match your webhook definition.
4. Check `~/.ductor/webhooks.json` trigger/error fields.

---

## Upgrading

From Telegram:
```
/upgrade
```

From the command line:
```bash
pipx upgrade ductor
```

ductor checks PyPI every 60 minutes and pings you in Telegram when there's a new version.

## Uninstalling

```bash
pipx uninstall ductor

# Optional: remove all data
rm -rf ~/.ductor
```
