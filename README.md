<div align="center">

# 🌑 DarkDeployer

**A Discord bot that lets you deploy and manage VPS servers without ever leaving Discord.**

[![Python](https://img.shields.io/badge/Python-3.13+-3776AB?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![discord.py](https://img.shields.io/badge/discord.py-2.x-5865F2?style=flat-square&logo=discord&logoColor=white)](https://github.com/Rapptz/discord.py)
[![Docker](https://img.shields.io/badge/Docker-required-2496ED?style=flat-square&logo=docker&logoColor=white)](https://docker.com)
[![Cloudflare](https://img.shields.io/badge/Cloudflare-Tunnel-F38020?style=flat-square&logo=cloudflare&logoColor=white)](https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/)

</div>

~ Made By ### SHADOW GAMER
---

## What is DarkNodes?

DarkNodes is a Discord bot that turns your server into a full VPS hosting panel. Users can spin up Linux containers, manage their files, run commands, and control everything through slash commands — no web dashboard needed.

You can run containers on the same machine as the bot, or spread them across multiple remote servers. Each remote machine runs a small agent that connects back to the bot automatically.

---

## Features

- **Deploy VPS containers** across one machine or many, from a single Discord server
- **Docker-in-Docker** — every VPS is a real Linux environment with full root access and its own Docker daemon
- **Credit economy** — users buy credits, purchase plans, transfer balances, and view leaderboards
- **Remote node support** — connect machines behind NAT via Cloudflare Tunnel with a one-liner install
- **File manager** — browse, upload, download, and edit files directly in Discord
- **Backups & cloning** — schedule automatic backups or clone any VPS instantly
- **One-click templates** — install Pterodactyl Panel, Cloudflare Tunnels, and more in a single command
- **Abuse detection** — auto-monitors CPU usage and scans for crypto-mining, auto-suspends offenders

---

## Setup

**Requirements:** Linux, Python 3.13+, Docker

### One-line install

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/hishadow1/DarkDeployerBot/main/install.sh)
```

The script clones the repo, installs dependencies, asks for your bot token and admin ID, then lets you choose to run via `python3` or install as a `systemd` service that auto-starts on reboot.

> **Requires root (`sudo`) for the systemd option.**

---

## Commands

<details>
<summary><b>VPS Management</b></summary>

| Command | Description |
|---|---|
| `/manage` | Open your VPS control panel |
| `/ping-vps` | Check if your VPS is online |
| `/uptime-vps` | View container uptime |
| `/rename-vps` | Rename your VPS |
| `/vps-note` | Add a personal note to your VPS |
| `/share-vps` | Give another user access |
| `/revoke-share` | Remove a shared user |
| `/my-shares` | See who has access to your VPS |
| `/fix` | Run automatic self-repair |
| `/guided-setup` | First-boot setup guide |

</details>

<details>
<summary><b>Files & Backups</b></summary>

| Command | Description |
|---|---|
| `/files` | Browse your VPS filesystem |
| `/download` | Download a file from your VPS |
| `/upload` | Upload a file to your VPS |
| `/editfile` | Edit a text file in Discord |
| `/schedule-backup` | Set up automatic backups |
| `/list-backups` | View your backup snapshots |
| `/clone` | Clone your VPS to a new container |

</details>

<details>
<summary><b>Economy</b></summary>

| Command | Description |
|---|---|
| `/plans` | View available plans and pricing |
| `/credits` | Check your balance |
| `/buywc` | Purchase a VPS with credits |
| `/buyc` | View ways to buy credits |
| `/transfer` | Send credits to another user |
| `/leaderboard` | Top credit holders |
| `/myinfo` | Your account dashboard |

</details>

<details>
<summary><b>Node Management</b></summary>

| Command | Description |
|---|---|
| `/node add` | Register a new machine |
| `/node remove` | Remove a machine |
| `/node rename` | Rename a machine |
| `/node list` | See all machines and their usage |
| `/node info` | Detailed stats for a machine |
| `/node reconnect` | Reconnect a machine |
| `/node tunnel` | Set the Cloudflare Tunnel URL |
| `/node regenerate-token` | Rotate a machine's API key |

</details>

<details>
<summary><b>Admin</b></summary>

| Command | Description |
|---|---|
| `/create` | Provision a VPS for a user |
| `/delete-vps` | Delete a user's VPS |
| `/list-all` | List every VPS across all machines |
| `/userinfo` | View a user's account |
| `/exec` | Run a shell command in any VPS |
| `/restart-vps` | Restart a container |
| `/backup-vps` | Manually back up a VPS |
| `/restore-vps` | Restore from a backup |
| `/vps-suspend` / `/vps-unsuspend` | Suspend or restore access |
| `/adminc` / `/adminrc` | Add or remove credits |
| `/setexpire` / `/extendexpire` | Set or extend a VPS expiry date |
| `/cpu-monitor` | Toggle abuse monitoring |
| `/vps-scan` | Scan for mining or abuse |
| `/announce` | Message all VPS owners |

</details>

---

## Adding a Remote Machine

Once a [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/) is configured with `/node tunnel`, adding any remote machine is one command run on that machine:

```bash
curl -sSL https://your-tunnel-url/install | bash -s -- <DNODE_TOKEN>
```

The token is generated by `/node add`. The machine registers itself, shows up in `/node list`, and is ready to accept deployments immediately.

---

<div align="center">

*Made with discord.py · Docker · Cloudflare*

---

### Credits

**SHADOW GAMER** — creator and lead developer of DarkNodes.

</div>
