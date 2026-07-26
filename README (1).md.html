<div align="center">

```
██████╗  █████╗ ██████╗ ██╗  ██╗███╗   ██╗ ██████╗ ██████╗ ███████╗███████╗
██╔══██╗██╔══██╗██╔══██╗██║ ██╔╝████╗  ██║██╔═══██╗██╔══██╗██╔════╝██╔════╝
██║  ██║███████║██████╔╝█████╔╝ ██╔██╗ ██║██║   ██║██║  ██║█████╗  ███████╗
██║  ██║██╔══██║██╔══██╗██╔═██╗ ██║╚██╗██║██║   ██║██║  ██║██╔══╝  ╚════██║
██████╔╝██║  ██║██║  ██║██║  ██╗██║ ╚████║╚██████╔╝██████╔╝███████╗███████║
╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═══╝ ╚═════╝ ╚═════╝ ╚══════╝╚══════╝
```

**Deploy · Manage · Monitor** — VPS infrastructure, entirely inside Discord.

[![Python](https://img.shields.io/badge/Python-3.13+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![discord.py](https://img.shields.io/badge/discord.py-2.x-5865F2?style=for-the-badge&logo=discord&logoColor=white)](https://github.com/Rapptz/discord.py)
[![Docker](https://img.shields.io/badge/Docker-DinD-2496ED?style=for-the-badge&logo=docker&logoColor=white)](https://docker.com)
[![Cloudflare](https://img.shields.io/badge/Cloudflare-Tunnel-F38020?style=for-the-badge&logo=cloudflare&logoColor=white)](https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/)
[![License](https://img.shields.io/badge/License-MIT-22c55e?style=for-the-badge)](LICENSE)

</div>

---

## ✦ What is DarkNodes?

DarkNodes turns your Discord server into a full VPS hosting control panel. Spin up Linux containers, distribute them across multiple physical machines, and let users manage everything through slash commands — no web dashboard, no SSH keys to hand out, no extra infrastructure.

Every VPS is a **Docker-in-Docker** container with real root access, real networking, and a built-in credit economy so you can run it as a service.

---

## ✦ Feature Highlights

<table>
<tr>
<td width="50%">

### 🖥️ Multi-Node Cluster
Distribute containers across any number of machines. Each **remote node** runs a lightweight agent that phones home over a Cloudflare Tunnel — no port-forwarding required, works behind any NAT.

</td>
<td width="50%">

### 🐳 Docker-in-Docker VPS
Every VPS is a privileged container with its own Docker daemon, full `systemd`, `/root`, `/home`, `/opt`, and persistent named volumes. Users get a real Linux environment.

</td>
</tr>
<tr>
<td>

### 💳 Built-in Credit Economy
Users buy credits, spend them on VPS plans, transfer balances, and see live leaderboards. Admins top up accounts, set expiry dates, and announce plan changes — all from Discord.

</td>
<td>

### 🛡️ Automated Abuse Detection
Continuous CPU monitoring flags runaway processes. A dedicated mining scanner detects known crypto-mining signatures and auto-suspends offending containers before they drain the host.

</td>
</tr>
<tr>
<td>

### 📦 One-Click Templates
Install complex stacks — Pterodactyl Panel, Cloudflare Tunnels, and more — with a single command. Templates run multi-step provisioning logic over the node's job queue.

</td>
<td>

### 🔒 Secure by Design
Remote agents authenticate with per-node API keys. Job transport is HTTPS-only via Cloudflare. The Discord bot token never leaves the manager host.

</td>
</tr>
</table>

---

## ✦ Architecture

```
╔══════════════════════════════════════════════════════════════════╗
║                        Discord Server                            ║
║   Users / Admins ──► Slash Commands ──► bot.py (Brain)          ║
╚══════════════════════════╦═══════════════════════════════════════╝
                           │
              ┌────────────▼────────────┐
              │    node_system.py       │  ← aiohttp HTTP server
              │    Node Manager         │    + node registry
              └──────┬──────────────────┘
                     │  Cloudflare Tunnel (HTTPS)
        ┌────────────┼────────────────────────────┐
        │            │                            │
   ┌────▼────┐  ┌────▼────┐                 ┌────▼────┐
   │ LOCAL   │  │ NODE A  │       ...       │ NODE N  │
   │ Docker  │  │ agent   │                 │ agent   │
   │ (direct)│  │ (polls) │                 │ (polls) │
   └────┬────┘  └────┬────┘                 └────┬────┘
        │            │                            │
   [containers] [containers]               [containers]
```

| Component | Role |
|---|---|
| `bot.py` | Discord interface, state persistence, orchestration |
| `node_system.py` | HTTP server, node registry, job dispatch, `/node` commands |
| `node_agent.py` | Runs on remote machines; polls for jobs, executes Docker commands |
| `template_system.py` | Multi-step software installers (Pterodactyl, tunnels, etc.) |

---

## ✦ Commands

<details>
<summary><strong>🖥️ VPS — User Commands</strong></summary>

| Command | Description |
|---|---|
| `/manage` | Open the full VPS control panel |
| `/manage-shared` | Manage a VPS shared with you |
| `/share-vps` | Grant another user access to your VPS |
| `/revoke-share` | Remove a shared user |
| `/my-shares` | List who has access to your VPS |
| `/rename-vps` | Rename your VPS |
| `/vps-note` | Attach a personal note to your VPS |
| `/ping-vps` | Check if your VPS is reachable |
| `/uptime-vps` | Show container uptime |
| `/fix` | Attempt automatic self-repair |
| `/cleanup` | Clear temporary files inside your VPS |
| `/guided-setup` | Step-by-step first-boot guide |

</details>

<details>
<summary><strong>📁 Files & Backups</strong></summary>

| Command | Description |
|---|---|
| `/files` | Browse the VPS filesystem in Discord |
| `/download` | Download a file from your VPS |
| `/upload` | Upload a file to your VPS |
| `/editfile` | Edit a text file in-Discord |
| `/schedule-backup` | Set up automatic backups |
| `/list-backups` | View available backup snapshots |
| `/clone` | Clone your VPS to a new container |

</details>

<details>
<summary><strong>💳 Economy</strong></summary>

| Command | Description |
|---|---|
| `/plans` | View available VPS plans and pricing |
| `/credits` | Check your credit balance |
| `/buywc` | Purchase a VPS with credits |
| `/buyc` | View payment methods to buy credits |
| `/transfer` | Send credits to another user |
| `/leaderboard` | Top credit holders |
| `/myinfo` | Your full account dashboard |
| `/stats` | Server-wide usage statistics |

</details>

<details>
<summary><strong>🔧 Node Management — <code>/node</code></strong></summary>

| Command | Description |
|---|---|
| `/node add` | Register a new node (generates install token) |
| `/node remove` | Deregister a node |
| `/node rename` | Rename a node |
| `/node list` | List all nodes with live resource bars |
| `/node info` | Detailed stats for a specific node |
| `/node reconnect` | Force reconnect a node |
| `/node regenerate-token` | Issue a new API key for a node |
| `/node tunnel` | Configure the Cloudflare Tunnel URL |

</details>

<details>
<summary><strong>🛡️ Admin Commands</strong></summary>

| Command | Description |
|---|---|
| `/create` | Provision a new VPS for a user |
| `/delete-vps` | Destroy a user's VPS |
| `/list-all` | List every VPS across all nodes |
| `/userinfo` | View a user's account details |
| `/exec` | Run a shell command inside any VPS |
| `/restart-vps` | Restart a container |
| `/stop-vps-all` | Stop all containers on a node |
| `/backup-vps` | Manually back up a VPS |
| `/restore-vps` | Restore a VPS from backup |
| `/fixvps` | Admin-level container repair |
| `/fixdind` | Repair Docker-in-Docker inside a VPS |
| `/adminc` | Add credits to a user |
| `/adminrc` | Remove credits from a user |
| `/setexpire` | Set VPS expiry date |
| `/extendexpire` | Extend VPS expiry |
| `/vps-suspend` | Suspend a VPS |
| `/vps-unsuspend` | Unsuspend a VPS |
| `/cpu-monitor` | Toggle abuse monitoring |
| `/vps-scan` | Scan for mining or abuse |
| `/announce` | Broadcast a message to all VPS owners |
| `/branding` | Set embed colors and server branding |
| `/setlogschannel` | Configure the admin log channel |

</details>

---

## ✦ Quick Start

### 1 — Prerequisites

```
• Linux host (Ubuntu 22.04+ recommended)
• Docker installed and running
• Python 3.13+
• A Discord bot token with Message Content and Server Members intents
• cloudflared (optional, required for remote nodes)
```

### 2 — Install

```bash
git clone https://github.com/your-repo/darknodes
cd darknodes
pip install -r requirements.txt
```

### 3 — Configure

```bash
# Required
export DISCORD_TOKEN="your-bot-token"
export MAIN_ADMIN_ID="your-discord-user-id"

# Optional
export NODE_MANAGER_PORT=8765          # default
export NODE_MANAGER_URL="https://..."  # set after /node tunnel
export SERVER_IP="1.2.3.4"            # displayed in /node add instructions
```

### 4 — Run

```bash
python bot.py
```

The bot will come online, register the local Docker socket as **Node 0 (local)**, and be ready to accept `/node add` for remote machines.

---

## ✦ Adding a Remote Node

Once the bot is running and a Cloudflare Tunnel is configured (`/node tunnel`), adding a remote machine is one command:

```bash
# Run this on the remote machine — the bot gives you the exact command
curl -sSL https://your-tunnel-url/install | bash -s -- <DNODE_TOKEN>
```

The agent registers, opens a private Discord channel for that node, and begins accepting jobs immediately. No SSH access to the manager host is ever required.

---

## ✦ Data Files

| File | Purpose |
|---|---|
| `vps_data.json` | All user VPS records |
| `user_data.json` | Credit balances and account info |
| `nodes.json` | Registered node registry |
| `node_tunnel.json` | Cloudflare Tunnel configuration |
| `admin_data.json` | Server branding and admin settings |

> **Tip:** Back these files up regularly — they are the only persistent state outside the Docker volumes.

---

## ✦ How Remote Nodes Work

```
Remote Agent lifecycle:
  1. Startup   → POST /api/register   (sends node_id + api_key + system info)
  2. Loop      → POST /api/jobs       (polls every 3 s for pending commands)
  3. Execute   → runs docker command locally via subprocess
  4. Report    → POST /api/result     (sends stdout/stderr + exit code)
  5. Heartbeat → included in every /api/jobs poll (30 s timeout triggers offline alert)
```

If the node goes offline, the bot sends a DM to the admin and suggests `/node reconnect`. When it comes back, a "Node Back Online" DM is sent automatically.

---

## ✦ Security Notes

- Each node has a unique API key stored in `nodes.json`; compromised keys can be rotated with `/node regenerate-token` without rebooting anything.
- All remote traffic travels over Cloudflare Tunnel (TLS 1.3) — the manager never exposes a raw port to the internet.
- VPS containers run `--privileged` by design (DinD requires it). Treat each node host as a trusted machine.

---

<div align="center">

Built with Python · discord.py · Docker · aiohttp · Cloudflare

*DarkNodes — infrastructure that lives in your server.*

</div>
