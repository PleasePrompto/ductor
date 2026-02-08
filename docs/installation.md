# Installation guide

## What you need

1. Python 3.12 or newer
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
# Python 3.12+
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
# Python 3.12+ (via Homebrew)
brew install python@3.12

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

Both CLIs have full file system access by default. They can read, write, and delete anything your user can. Docker sandboxing puts the CLI process inside a Debian Bookworm container so it can only touch mounted directories.

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

## Hosting on a VPS

A $5/month VPS with 1 GB RAM is enough to keep ductor running around the clock. Any Linux VPS works. Hetzner, DigitalOcean, Vultr, Linode, whatever you have.

### Setup

```bash
# SSH into your VPS
ssh user@your-vps-ip

# Install dependencies
sudo apt update && sudo apt install python3 python3-pip python3-venv nodejs npm docker.io

# Add user to docker group
sudo usermod -aG docker $USER
newgrp docker

# Install pipx + ductor
pip install pipx
pipx ensurepath
source ~/.bashrc
pipx install ductor

# Install and authenticate a CLI
npm install -g @anthropic-ai/claude-code
claude auth  # Follow the browser-based auth flow

# Run setup wizard
ductor
```

### Keep it running with systemd

A systemd service starts ductor on boot and restarts it if it crashes:

```bash
sudo tee /etc/systemd/system/ductor.service > /dev/null << 'EOF'
[Unit]
Description=ductor Bot
After=network.target docker.service
Wants=docker.service

[Service]
Type=simple
User=YOUR_USERNAME
Environment=PATH=/home/YOUR_USERNAME/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/home/YOUR_USERNAME/.local/bin/ductor
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
```

Replace `YOUR_USERNAME` with your actual username, then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable ductor
sudo systemctl start ductor

# Check status
sudo systemctl status ductor

# View logs
journalctl -u ductor -f
```

### Security basics

- Only open port 22 (SSH). ductor connects outbound to Telegram, nothing inbound needed. Exception: if you use webhooks, open that port too
- Disable password auth for SSH, use keys only
- Enable Docker sandboxing (`docker.enabled: true`)
- Set `allowed_user_ids` in config so only you can use the bot
- Update from Telegram with `/upgrade` or via SSH with `pipx upgrade ductor`

### Resource usage

Idle: ~50-100 MB RAM, almost no CPU. During CLI execution the subprocess uses more, but ductor itself stays light. Disk is about 200 MB for the package and dependencies. The workspace grows over time but auto-cleans files older than 30 days. All network traffic is outbound to Telegram and the AI provider.

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
