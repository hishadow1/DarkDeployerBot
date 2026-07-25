#!/usr/bin/env python3
"""
DarkNodes Node Agent  —  Discord-relay edition
───────────────────────────────────────────────
Connects this machine to your DarkNodes bot with NO public IP required —
on either the node OR the bot server.

How it works
────────────
Both the bot and this agent communicate exclusively through a private Discord
thread.  All traffic is outbound HTTPS to Discord's servers.

  Node machine  ──HTTPS──▶  Discord API  ◀──HTTPS──  Bot server
                                 │
                   (messages relayed through a private thread)

Neither side needs:
  • A public IP address
  • Open inbound ports
  • Port forwarding
  • SSH access from the other side

The node only needs outbound internet access (port 443 to discord.com).

────────────────────────────────────────────────────────────────────────────────
SETUP  (run these commands on the remote machine)
────────────────────────────────────────────────────────────────────────────────

1. Copy node_agent.py to the remote machine.

2. In Discord, run  /node add  (admin only).
   The bot replies with a ready-to-run command — just copy and paste it.

3. The command will look like:
       python3 node_agent.py \\
         --token  <BOT_TOKEN>  \\
         --thread <THREAD_ID>  \\
         --code   <CODE>

   • --token   Your Discord bot token  (same token the bot uses)
   • --thread  The private Discord thread ID created by  /node add
   • --code    One-time registration code (expires in 30 min)

4. After first registration the credentials are saved to node_agent.json.
   All future restarts only need:
       python3 node_agent.py --token <BOT_TOKEN>

────────────────────────────────────────────────────────────────────────────────
OPTIONAL: RUN AS A SYSTEM SERVICE
────────────────────────────────────────────────────────────────────────────────

Create  /etc/systemd/system/darknodes-agent.service :

    [Unit]
    Description=DarkNodes Node Agent
    After=network-online.target
    Wants=network-online.target

    [Service]
    Type=simple
    ExecStart=/usr/bin/python3 /opt/darknodes/node_agent.py --token <BOT_TOKEN>
    WorkingDirectory=/opt/darknodes
    Restart=always
    RestartSec=15

    [Install]
    WantedBy=multi-user.target

    systemctl enable --now darknodes-agent

────────────────────────────────────────────────────────────────────────────────
REQUIREMENTS
────────────────────────────────────────────────────────────────────────────────
  • Python 3.8+  (no extra packages — uses only the standard library)
  • Outbound HTTPS to discord.com  (port 443)
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import shlex
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
logger = logging.getLogger("darknodes.agent")

DEFAULT_CONFIG   = "node_agent.json"
DEFAULT_INTERVAL = 15    # seconds between heartbeats
POLL_INTERVAL    = 5     # seconds between Discord message polls
MAX_FAILURES     = 20    # reset failure counter after this many consecutive errors
DISCORD_API      = "https://discord.com/api/v10"
MAX_OUTPUT_BYTES = 1500  # keep under Discord's 2000-char limit after base64


# ══════════════════════════════════════════════════════════════════════════════
# Discord REST helpers  (pure stdlib — no discord.py dependency)
# ══════════════════════════════════════════════════════════════════════════════

def _discord_request(
    method: str,
    path: str,
    bot_token: str,
    payload: dict | None = None,
    timeout: int = 15,
) -> dict | list:
    """Make a Discord REST API request and return the parsed JSON response."""
    url  = f"{DISCORD_API}{path}"
    data = json.dumps(payload).encode() if payload else None
    headers = {
        "Authorization": f"Bot {bot_token}",
        "User-Agent":    "DarkNodes-Agent/2.0",
    }
    if data:
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode()
            return json.loads(body) if body.strip() else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise RuntimeError(f"Discord API {method} {path} → HTTP {exc.code}: {body[:300]}")


def _post_message(thread_id: str, bot_token: str, content: str) -> dict:
    """Post a plain-text message to a Discord thread."""
    # Discord message limit is 2000 chars; truncate if needed
    if len(content) > 1990:
        content = content[:1987] + "…"
    return _discord_request(
        "POST",
        f"/channels/{thread_id}/messages",
        bot_token,
        {"content": content},
    )


def _get_messages(thread_id: str, bot_token: str, after_id: str = "") -> list:
    """
    Fetch up to 10 messages from a thread newer than after_id.
    Returns newest-last (ascending chronological order).
    """
    path = f"/channels/{thread_id}/messages?limit=10"
    if after_id:
        path += f"&after={after_id}"
    msgs = _discord_request("GET", path, bot_token)
    if not isinstance(msgs, list):
        return []
    # Discord returns newest-first; reverse to process oldest→newest
    return list(reversed(msgs))


# ══════════════════════════════════════════════════════════════════════════════
# System stats  (pure stdlib)
# ══════════════════════════════════════════════════════════════════════════════

def _get_public_ip() -> str:
    for svc in ["https://api.ipify.org", "https://icanhazip.com", "https://ifconfig.me/ip"]:
        try:
            req = urllib.request.Request(svc, headers={"User-Agent": "DarkNodes-Agent/2.0"})
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.read().decode().strip()
        except Exception:
            continue
    return ""


def _cpu_percent() -> float:
    def _read():
        try:
            with open("/proc/stat") as fh:
                line = fh.readline()
            fields = list(map(int, line.split()[1:]))
            return sum(fields), fields[3]
        except Exception:
            return 0, 0
    t1, i1 = _read()
    time.sleep(0.3)
    t2, i2 = _read()
    dt = t2 - t1 or 1
    return round((1 - (i2 - i1) / dt) * 100, 1)


def _ram_mb() -> tuple[int, int]:
    try:
        info: dict = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                key, _, val = line.partition(":")
                info[key.strip()] = int(val.split()[0])
        total = info.get("MemTotal", 0) // 1024
        avail = info.get("MemAvailable", info.get("MemFree", 0)) // 1024
        return total - avail, total
    except Exception:
        return 0, 0


def _running_vps_count() -> int:
    try:
        result = subprocess.run(
            ["docker", "ps", "--filter", "label=darknodes.vps=true", "-q"],
            capture_output=True, text=True, timeout=10,
        )
        return len([l for l in result.stdout.strip().splitlines() if l.strip()])
    except Exception:
        return 0


# ══════════════════════════════════════════════════════════════════════════════
# Shell command execution
# ══════════════════════════════════════════════════════════════════════════════

def _run_shell(command: str, timeout: int = 120) -> tuple[bool, str]:
    """Run a shell command; return (success, combined output)."""
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=timeout
        )
        combined = (result.stdout + result.stderr).strip()
        return result.returncode == 0, combined
    except subprocess.TimeoutExpired:
        return False, f"Command timed out after {timeout}s"
    except Exception as exc:
        return False, str(exc)


# ══════════════════════════════════════════════════════════════════════════════
# Config persistence
# ══════════════════════════════════════════════════════════════════════════════

def _load_config(path: str) -> dict:
    try:
        with open(path) as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_config(path: str, data: dict) -> None:
    with open(path, "w") as fh:
        json.dump(data, fh, indent=2)
    logger.info(f"Credentials saved to {path}")


# ══════════════════════════════════════════════════════════════════════════════
# Registration
# ══════════════════════════════════════════════════════════════════════════════

def register(bot_token: str, thread_id: str, code: str, name: str) -> tuple[str, str]:
    """
    Post REG:{code}:{hostname}:{public_ip} to the thread.
    Poll until the bot replies  REGISTERED:{node_id}:{secret}.
    Returns (node_id, secret).
    """
    hostname  = name or socket.gethostname()
    public_ip = _get_public_ip()

    logger.info(f"Registering '{hostname}' (IP: {public_ip or 'unknown'}) …")
    msg = _post_message(thread_id, bot_token, f"REG:{code}:{hostname}:{public_ip}")
    after_id = msg.get("id", "")

    deadline = time.time() + 120   # wait up to 2 min for bot response
    while time.time() < deadline:
        time.sleep(3)
        try:
            messages = _get_messages(thread_id, bot_token, after_id)
        except Exception as exc:
            logger.warning(f"Polling error: {exc}")
            continue

        for m in messages:
            content = m.get("content", "")
            mid     = m.get("id", "")
            if content.startswith("REGISTERED:"):
                parts = content.split(":")
                if len(parts) >= 3:
                    node_id = parts[1]
                    secret  = parts[2]
                    logger.info(f"✅  Registered — node_id={node_id}")
                    return node_id, secret
            if content.startswith("❌"):
                raise RuntimeError(f"Registration rejected by bot: {content}")
            if mid:
                after_id = mid  # advance past messages we've already seen

    raise RuntimeError("Registration timed out — bot did not respond within 2 minutes")


# ══════════════════════════════════════════════════════════════════════════════
# Main agent loop
# ══════════════════════════════════════════════════════════════════════════════

def run_agent(
    bot_token: str,
    thread_id: str,
    node_id:   str,
    secret:    str,
    interval:  int,
) -> None:
    """
    Send heartbeats and process commands indefinitely.

    Protocol messages (all in the node's private Discord thread):

    Agent → Bot:
        STAT:{node_id}:{secret}:{cpu}:{ram_used}:{ram_total}:{vps_count}
        RES:{node_id}:{secret}:{cmd_id}:{ok}:{b64_output}

    Bot → Agent:
        CMD:{node_id}:{cmd_id}:{command_b64}
    """
    logger.info(
        f"Agent running — node_id={node_id}  thread={thread_id}  "
        f"heartbeat={interval}s  poll={POLL_INTERVAL}s"
    )

    last_heartbeat  = 0.0
    last_message_id = ""   # track last seen message so we don't re-process
    consecutive_failures = 0

    while True:
        now = time.time()

        # ── Heartbeat ─────────────────────────────────────────────────────────
        if now - last_heartbeat >= interval:
            try:
                cpu       = _cpu_percent()
                used, tot = _ram_mb()
                vps_count = _running_vps_count()
                _post_message(
                    thread_id, bot_token,
                    f"STAT:{node_id}:{secret}:{cpu}:{used}:{tot}:{vps_count}",
                )
                last_heartbeat = time.time()
                consecutive_failures = 0
                logger.debug(f"Heartbeat sent — CPU={cpu}%  RAM={used}/{tot}MB  VPS={vps_count}")
            except Exception as exc:
                consecutive_failures += 1
                logger.warning(f"Heartbeat failed ({consecutive_failures}): {exc}")
                if consecutive_failures >= MAX_FAILURES:
                    logger.error("Too many consecutive failures — check connectivity.")
                    consecutive_failures = 0

        # ── Poll for commands ──────────────────────────────────────────────────
        try:
            messages = _get_messages(thread_id, bot_token, last_message_id)
        except Exception as exc:
            logger.warning(f"Message poll failed: {exc}")
            time.sleep(POLL_INTERVAL)
            continue

        for msg in messages:
            mid     = msg.get("id", "")
            content = msg.get("content", "").strip()
            if mid:
                last_message_id = mid

            # Only process CMD messages directed at this node
            if not content.startswith(f"CMD:{node_id}:"):
                continue

            # CMD:{node_id}:{cmd_id}:{command_b64}
            parts = content.split(":", 3)
            if len(parts) < 4:
                continue

            cmd_id  = parts[2]
            try:
                command = base64.b64decode(parts[3].encode()).decode(errors="replace")
            except Exception:
                command = parts[3]

            logger.info(f"Executing [{cmd_id}]: {command[:120]}")
            ok, output = _run_shell(command)
            logger.info(f"Result [{cmd_id}]: {'✅' if ok else '❌'}  ({len(output)} chars)")

            # Truncate output so it fits in a Discord message
            output_bytes = output.encode(errors="replace")
            if len(output_bytes) > MAX_OUTPUT_BYTES:
                output_bytes = output_bytes[:MAX_OUTPUT_BYTES] + b"\n[output truncated]"
            b64_output = base64.b64encode(output_bytes).decode()

            try:
                _post_message(
                    thread_id, bot_token,
                    f"RES:{node_id}:{secret}:{cmd_id}:{1 if ok else 0}:{b64_output}",
                )
            except Exception as re:
                logger.warning(f"Failed to send result for [{cmd_id}]: {re}")

        time.sleep(POLL_INTERVAL)


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description="DarkNodes Node Agent — connect this machine to your DarkNodes bot via Discord",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--token", required=False, default="",
        help="Discord bot token (same token your bot uses)",
    )
    parser.add_argument(
        "--thread", default="",
        help="Discord thread ID from /node add (required for first registration only)",
    )
    parser.add_argument(
        "--code", default="",
        help="One-time registration code from /node add (required for first registration only)",
    )
    parser.add_argument(
        "--name", default="",
        help="Custom display name for this node (default: machine hostname)",
    )
    parser.add_argument(
        "--interval", type=int, default=DEFAULT_INTERVAL,
        help=f"Heartbeat interval in seconds (default: {DEFAULT_INTERVAL})",
    )
    parser.add_argument(
        "--config", default=DEFAULT_CONFIG,
        help=f"Path to credentials file (default: {DEFAULT_CONFIG})",
    )
    args = parser.parse_args()

    # ── Load saved credentials ────────────────────────────────────────────────
    cfg = _load_config(args.config)

    bot_token = args.token or cfg.get("bot_token", "")
    thread_id = args.thread or cfg.get("thread_id", "")
    node_id   = cfg.get("node_id",  "")
    secret    = cfg.get("secret",   "")

    if not bot_token:
        parser.error(
            "--token is required.\n"
            "Provide your Discord bot token (the same token your bot uses)."
        )

    if not node_id:
        # First run — need to register
        code = args.code or cfg.get("code", "")
        if not thread_id or not code:
            parser.error(
                "--thread and --code are required for first registration.\n"
                "Run  /node add  in Discord to get a ready-to-paste command."
            )

        try:
            node_id, secret = register(bot_token, thread_id, code, args.name)
        except Exception as exc:
            logger.error(f"Registration failed: {exc}")
            sys.exit(1)

        _save_config(args.config, {
            "bot_token": bot_token,
            "thread_id": thread_id,
            "node_id":   node_id,
            "secret":    secret,
            "name":      args.name or socket.gethostname(),
        })
    else:
        logger.info(f"Resuming as node {node_id} (loaded from {args.config})")

    run_agent(bot_token, thread_id, node_id, secret, args.interval)


if __name__ == "__main__":
    main()
