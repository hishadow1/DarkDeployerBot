#!/usr/bin/env python3
"""
DarkNodes Node Agent  —  Discord Channel Relay Edition
═══════════════════════════════════════════════════════════════════════════════
Connects this machine to your DarkNodes bot using Discord as the transport
layer.  No public IP, no open ports, no SSH tunnels, no port forwarding.
Both sides only need outbound HTTPS to discord.com (port 443).

  Remote machine ──── HTTPS ──▶ discord.com ◀──── HTTPS ──── Bot server
                   (REST API)                   (WebSocket)

Neither the node nor the bot needs:
  • A public IP address
  • Open inbound ports
  • Port forwarding
  • A static IP

The only requirement: outbound internet access to discord.com.

═══════════════════════════════════════════════════════════════════════════════
REQUIREMENTS
═══════════════════════════════════════════════════════════════════════════════
  • Python 3.8+  (no extra packages — uses only the standard library)
  • The Discord bot token set in the DISCORD_TOKEN environment variable
  • Outbound HTTPS to discord.com
  • Docker (for VPS management)

═══════════════════════════════════════════════════════════════════════════════
SETUP
═══════════════════════════════════════════════════════════════════════════════

1. Copy node_agent.py to the remote machine.

2. Set the bot token environment variable:
       export DISCORD_TOKEN="your_discord_bot_token"

   This is the SAME token you use to run the bot.  Never share it publicly.
   Add it to /etc/environment or your shell profile for persistence.

3. In Discord, run  /node add  (admin only).
   The bot creates a private channel and shows a ready-to-run command:

       python3 node_agent.py \\
         --channel <CHANNEL_ID> \\
         --reg-token <ONE_TIME_TOKEN>

4. After first registration, credentials are saved to node_agent.json.
   All future restarts only need:

       python3 node_agent.py

   The DISCORD_TOKEN env var must still be set on each start.

═══════════════════════════════════════════════════════════════════════════════
RUN AS A SYSTEM SERVICE (Linux/systemd)
═══════════════════════════════════════════════════════════════════════════════

Create  /etc/systemd/system/darknodes-agent.service :

    [Unit]
    Description=DarkNodes Node Agent
    After=network-online.target
    Wants=network-online.target

    [Service]
    Type=simple
    EnvironmentFile=/etc/darknodes/env      # put DISCORD_TOKEN=... here
    ExecStart=/usr/bin/python3 /opt/darknodes/node_agent.py
    WorkingDirectory=/opt/darknodes
    Restart=always
    RestartSec=15
    StandardOutput=journal
    StandardError=journal

    [Install]
    WantedBy=multi-user.target

Create  /etc/darknodes/env  with mode 600:

    DISCORD_TOKEN=your_bot_token_here

Then:
    chmod 600 /etc/darknodes/env
    systemctl daemon-reload
    systemctl enable --now darknodes-agent
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
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

# ── Discord API ───────────────────────────────────────────────────────────────
DISCORD_API   = "https://discord.com/api/v10"
DISCORD_EPOCH = 1420070400000   # ms; Discord snowflake epoch

# ── Agent config ──────────────────────────────────────────────────────────────
DEFAULT_CONFIG    = "node_agent.json"
HEARTBEAT_SECS    = 30     # seconds between heartbeats
POLL_SECS         = 3      # seconds between job-poll cycles
RECONNECT_SECS    = 20     # wait between reconnect attempts after repeated failures
MAX_FAILURES      = 5      # consecutive failures before slowing to RECONNECT_SECS
JOB_MAX_AGE_SECS  = 600    # skip jobs older than this (10 minutes)

# ── Message direction prefixes (must match node_system.py) ────────────────────
MSG_FROM_BOT   = "→"   # bot → agent  (we process these)
MSG_FROM_AGENT = "←"   # agent → bot  (we send these; ignore on read)


# ══════════════════════════════════════════════════════════════════════════════
# Discord REST API helpers  (pure stdlib)
# ══════════════════════════════════════════════════════════════════════════════

def _discord(
    method:  str,
    path:    str,
    payload: dict | None = None,
    token:   str = "",
    timeout: int = 15,
) -> dict | list:
    """
    Make an authenticated Discord REST API call.
    Returns the parsed JSON response (dict or list).
    Raises RuntimeError on HTTP errors or network failures.
    """
    url     = DISCORD_API + path
    data    = json.dumps(payload).encode() if payload is not None else None
    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type":  "application/json",
        "User-Agent":    "DarkNodes-Agent/4.0 (Discord channel relay; no-public-ip)",
    }
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode()
            return json.loads(body) if body.strip() else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        # Surface the rate-limit header for callers if present
        retry_after = exc.headers.get("Retry-After", "")
        suffix = f"  retry-after={retry_after}s" if retry_after else ""
        raise RuntimeError(f"Discord HTTP {exc.code}{suffix}: {body[:300]}")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error reaching discord.com: {exc.reason}")


def _send_message(channel_id: str, content: str, token: str) -> dict:
    """POST a message to a channel.  Returns the created message object."""
    return _discord("POST", f"/channels/{channel_id}/messages",
                    {"content": content}, token=token)


def _get_messages(channel_id: str, after: str, token: str, limit: int = 100) -> list:
    """
    GET up to `limit` messages after a given snowflake ID.
    Discord returns them in ascending chronological order.
    """
    path = f"/channels/{channel_id}/messages?limit={limit}&after={after}"
    result = _discord("GET", path, token=token)
    if isinstance(result, list):
        return result
    return []


def _delete_message(channel_id: str, message_id: str, token: str) -> None:
    """DELETE a message (best-effort; ignores errors)."""
    try:
        _discord("DELETE", f"/channels/{channel_id}/messages/{message_id}",
                 token=token, timeout=10)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# Discord snowflake utilities
# ══════════════════════════════════════════════════════════════════════════════

def _snowflake_now() -> str:
    """
    Return a Discord snowflake ID representing approximately the current time.
    Used as the starting `after` value so the agent only sees NEW messages.
    """
    ms = int(time.time() * 1000) - DISCORD_EPOCH
    return str(max(ms, 0) << 22)


def _snowflake_age_secs(snowflake: str) -> float:
    """Return how many seconds ago a Discord message snowflake was created."""
    try:
        created_ms = (int(snowflake) >> 22) + DISCORD_EPOCH
        return (time.time() * 1000 - created_ms) / 1000
    except Exception:
        return 0.0


# ══════════════════════════════════════════════════════════════════════════════
# System stats  (pure stdlib)
# ══════════════════════════════════════════════════════════════════════════════

def _get_public_ip() -> str:
    for svc in ["https://api.ipify.org", "https://icanhazip.com", "https://ifconfig.me/ip"]:
        try:
            req = urllib.request.Request(svc, headers={"User-Agent": "DarkNodes-Agent/4.0"})
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.read().decode().strip()
        except Exception:
            continue
    return ""


def _cpu_percent() -> float:
    """Read CPU usage from /proc/stat (two snapshots 300 ms apart)."""
    def _read():
        try:
            with open("/proc/stat") as fh:
                fields = list(map(int, fh.readline().split()[1:]))
            return sum(fields), fields[3]
        except Exception:
            return 0, 0
    t1, i1 = _read()
    time.sleep(0.3)
    t2, i2 = _read()
    dt = t2 - t1 or 1
    return round((1 - (i2 - i1) / dt) * 100, 1)


def _ram_mb() -> tuple:
    """Returns (used_mb, total_mb)."""
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


def _disk_gb() -> tuple:
    """Returns (used_gb, total_gb) for the root filesystem."""
    try:
        usage = shutil.disk_usage("/")
        gb = 1024 ** 3
        return round((usage.total - usage.free) / gb, 2), round(usage.total / gb, 2)
    except Exception:
        return 0.0, 0.0


def _running_vps_count() -> int:
    """Count Docker containers labelled darknodes.vps=true."""
    try:
        result = subprocess.run(
            ["docker", "ps", "--filter", "label=darknodes.vps=true", "-q"],
            capture_output=True, text=True, timeout=10,
        )
        return len([l for l in result.stdout.strip().splitlines() if l.strip()])
    except Exception:
        return 0


def _collect_vps_meta() -> list:
    """
    Read metadata from VPS Docker containers via their labels.
    Labels expected: darknodes.vps=true, darknodes.user_id, darknodes.plan,
                     darknodes.hostname
    """
    try:
        result = subprocess.run(
            [
                "docker", "ps", "-a",
                "--filter", "label=darknodes.vps=true",
                "--format",
                "{{.Names}}\t{{.Label \"darknodes.user_id\"}}\t"
                "{{.Label \"darknodes.plan\"}}\t{{.Label \"darknodes.hostname\"}}\t{{.Status}}",
            ],
            capture_output=True, text=True, timeout=10,
        )
        meta = []
        for line in result.stdout.strip().splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            cname   = parts[0].strip()
            user_id = parts[1].strip() if len(parts) > 1 else ""
            plan    = parts[2].strip() if len(parts) > 2 else "Custom"
            host    = parts[3].strip() if len(parts) > 3 else cname
            status  = parts[4].strip() if len(parts) > 4 else "unknown"
            if not cname or not user_id:
                continue
            meta.append({
                "container_name": cname,
                "user_id":        user_id,
                "plan":           plan,
                "hostname":       host,
                "status":         "running" if "Up" in status else "stopped",
            })
        return meta
    except Exception:
        return []


# ══════════════════════════════════════════════════════════════════════════════
# Shell command execution
# ══════════════════════════════════════════════════════════════════════════════

def _run_shell(command: str, timeout: int = 120) -> tuple:
    """Run a shell command.  Returns (success: bool, output: str)."""
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


def _update_config(path: str, updates: dict) -> None:
    cfg = _load_config(path)
    cfg.update(updates)
    _save_config(path, cfg)


# ══════════════════════════════════════════════════════════════════════════════
# Registration  (posts a message to the node channel, waits for confirmation)
# ══════════════════════════════════════════════════════════════════════════════

def register(channel_id: str, reg_token: str, token: str, name: str = "") -> str:
    """
    Register this machine with the bot by posting a registration message to the
    node's private Discord channel.  Waits up to 60 s for confirmation.
    Returns the node_id assigned by the bot.
    """
    hostname  = name or socket.gethostname()
    public_ip = _get_public_ip()

    logger.info(f"Registering '{hostname}' (public_ip={public_ip or 'unknown'}) …")

    # Start listening just before we post so we don't miss the reply
    after = _snowflake_now()

    # Post registration message
    payload = {
        "type":      "reg",
        "token":     reg_token,
        "hostname":  hostname,
        "public_ip": public_ip,
    }
    content = MSG_FROM_AGENT + json.dumps(payload, separators=(",", ":"))
    _send_message(channel_id, content, token)
    logger.info("Registration message sent.  Waiting for bot confirmation …")

    # Poll for the bot's response
    deadline = time.time() + 60
    while time.time() < deadline:
        time.sleep(3)
        try:
            messages = _get_messages(channel_id, after=after, token=token, limit=20)
        except Exception as exc:
            logger.warning(f"Poll error while waiting for confirmation: {exc}")
            continue

        for msg in messages:
            content_r = msg.get("content", "")
            if not content_r.startswith(MSG_FROM_BOT):
                continue
            try:
                data = json.loads(content_r[len(MSG_FROM_BOT):])
            except Exception:
                continue
            if data.get("type") == "registered":
                node_id = data.get("node_id", "")
                if node_id:
                    logger.info(f"✅  Registered — node_id={node_id}")
                    return node_id

        # Advance our after cursor
        if messages:
            after = max(m["id"] for m in messages)

    raise RuntimeError(
        "Registration timed out after 60 s — "
        "make sure the bot is running and DISCORD_TOKEN is correct."
    )


# ══════════════════════════════════════════════════════════════════════════════
# Main agent loop
# ══════════════════════════════════════════════════════════════════════════════

def run_agent(channel_id: str, node_id: str, token: str, config_path: str) -> None:
    """
    Main agent loop:
      • Heartbeat: post a message every HEARTBEAT_SECS seconds (deletes the
        previous heartbeat first to keep the channel clean).
      • Job poll: read new messages every POLL_SECS seconds; execute any job
        commands and post results back.
      • VPS sync: on (re)connect, post locally cached VPS metadata so the bot
        can restore its database if it was lost.
      • Auto-reconnect: after MAX_FAILURES consecutive failures, waits
        RECONNECT_SECS seconds then retries.
    """
    logger.info(
        f"Agent running — node_id={node_id}  channel={channel_id}  "
        f"heartbeat={HEARTBEAT_SECS}s  poll={POLL_SECS}s"
    )

    # Load saved cursor so we don't replay old commands after a restart
    cfg              = _load_config(config_path)
    last_message_id  = cfg.get("last_message_id") or _snowflake_now()

    last_heartbeat   = 0.0
    last_hb_msg_id   = ""       # ID of the previous heartbeat message (to delete)
    consecutive_fail = 0
    reconnected      = True     # triggers VPS sync on first iteration

    while True:
        now = time.time()

        # ── VPS sync on (re)connect ────────────────────────────────────────────
        if reconnected:
            reconnected = False
            try:
                cfg        = _load_config(config_path)
                stored_vps = cfg.get("vps_meta", [])
                live_meta  = _collect_vps_meta()
                # Merge: prefer stored, supplement with live labels
                known = {v["container_name"] for v in stored_vps}
                for lv in live_meta:
                    if lv["container_name"] not in known:
                        stored_vps.append(lv)
                if stored_vps:
                    vsync_payload = {
                        "type":     "vsync",
                        "vps_list": stored_vps,
                    }
                    _send_message(
                        channel_id,
                        MSG_FROM_AGENT + json.dumps(vsync_payload, separators=(",", ":")),
                        token,
                    )
                    logger.info(f"VPS metadata synced to bot ({len(stored_vps)} records)")
            except Exception as exc:
                logger.debug(f"VPS sync error on reconnect: {exc}")

        # ── Heartbeat ──────────────────────────────────────────────────────────
        if now - last_heartbeat >= HEARTBEAT_SECS:
            try:
                cpu           = _cpu_percent()
                used_mb, tot_mb = _ram_mb()
                used_gb, tot_gb = _disk_gb()
                vps_count     = _running_vps_count()

                hb_payload = {
                    "type":          "hb",
                    "cpu":           cpu,
                    "ram_used_mb":   used_mb,
                    "ram_total_mb":  tot_mb,
                    "disk_used_gb":  used_gb,
                    "disk_total_gb": tot_gb,
                    "running_vps":   vps_count,
                }
                hb_content = MSG_FROM_AGENT + json.dumps(hb_payload, separators=(",", ":"))

                # Delete previous heartbeat message to keep the channel clean
                if last_hb_msg_id:
                    _delete_message(channel_id, last_hb_msg_id, token)
                    last_hb_msg_id = ""

                msg = _send_message(channel_id, hb_content, token)
                last_hb_msg_id    = msg.get("id", "")
                last_heartbeat    = time.time()
                consecutive_fail  = 0

                logger.debug(
                    f"Heartbeat sent — CPU={cpu}%  "
                    f"RAM={used_mb}/{tot_mb}MB  "
                    f"Disk={used_gb}/{tot_gb}GB  VPS={vps_count}"
                )
            except Exception as exc:
                consecutive_fail += 1
                logger.warning(f"Heartbeat failed ({consecutive_fail}/{MAX_FAILURES}): {exc}")
                _maybe_reconnect(consecutive_fail)
                if consecutive_fail >= MAX_FAILURES:
                    consecutive_fail = 0
                    reconnected      = True
                    last_heartbeat   = 0   # force immediate retry next iteration
                time.sleep(POLL_SECS)
                continue

        # ── Poll for job commands ──────────────────────────────────────────────
        try:
            messages = _get_messages(channel_id, after=last_message_id, token=token)
        except Exception as exc:
            consecutive_fail += 1
            logger.warning(f"Message poll failed ({consecutive_fail}/{MAX_FAILURES}): {exc}")
            _maybe_reconnect(consecutive_fail)
            if consecutive_fail >= MAX_FAILURES:
                consecutive_fail = 0
                reconnected      = True
                last_heartbeat   = 0
            time.sleep(POLL_SECS)
            continue

        consecutive_fail = 0

        # Advance cursor
        if messages:
            new_cursor = max(m["id"] for m in messages)
            if new_cursor != last_message_id:
                last_message_id = new_cursor
                _update_config(config_path, {"last_message_id": last_message_id})

        # Process job commands
        for msg in messages:
            content = msg.get("content", "")
            if not content.startswith(MSG_FROM_BOT):
                continue   # skip agent's own messages (← prefix) and unrelated

            try:
                data = json.loads(content[len(MSG_FROM_BOT):])
            except (json.JSONDecodeError, ValueError):
                continue

            msg_type = data.get("type", "")

            if msg_type == "job":
                _handle_job(msg, data, channel_id, token, config_path)
            # "registered" confirmation is handled during register() only; ignore here

        time.sleep(POLL_SECS)


def _maybe_reconnect(consecutive_fail: int) -> None:
    if consecutive_fail >= MAX_FAILURES:
        logger.warning(
            f"Too many consecutive failures — waiting {RECONNECT_SECS}s before retry"
        )
        time.sleep(RECONNECT_SECS)


def _handle_job(msg: dict, data: dict, channel_id: str, token: str, config_path: str) -> None:
    """Execute a single job command and post the result back."""
    job_id  = data.get("job_id", "")
    command = data.get("command", "")
    timeout = int(data.get("timeout", 120))

    if not job_id or not command:
        return

    # Skip stale jobs — the bot's future is already resolved as a timeout
    age = _snowflake_age_secs(msg["id"])
    if age > JOB_MAX_AGE_SECS:
        logger.info(f"Skipping stale job [{job_id}] (age={age:.0f}s): {command[:60]!r}")
        return

    logger.info(f"Executing job [{job_id}]: {command[:120]!r}")

    # ── Special internal job types ─────────────────────────────────────────────
    if command.startswith("__push_vps_meta__:"):
        ok, output = _job_push_vps_meta(command[len("__push_vps_meta__:"):], config_path)
    elif command.startswith("__remove_vps_meta__:"):
        ok, output = _job_remove_vps_meta(command[len("__remove_vps_meta__:"):], config_path)
    else:
        # ── Normal shell command ───────────────────────────────────────────────
        ok, output = _run_shell(command, timeout=timeout)

    logger.info(f"Job [{job_id}]: {'✅' if ok else '❌'}  ({len(output)} chars output)")

    # Post result back to the channel
    result_payload = {
        "type":    "result",
        "job_id":  job_id,
        "success": ok,
        "output":  output,
    }
    try:
        _send_message(
            channel_id,
            MSG_FROM_AGENT + json.dumps(result_payload, separators=(",", ":")),
            token,
        )
    except Exception as exc:
        logger.warning(f"Failed to post result for job [{job_id}]: {exc}")


def _job_push_vps_meta(payload_str: str, config_path: str) -> tuple:
    """Store VPS metadata sent by the bot into local config."""
    try:
        vps_rec  = json.loads(payload_str)
        cfg      = _load_config(config_path)
        vps_meta = cfg.get("vps_meta", [])
        existing = {v["container_name"] for v in vps_meta}
        cname    = vps_rec.get("container_name", "")
        if cname not in existing:
            vps_meta.append(vps_rec)
        else:
            vps_meta = [
                vps_rec if v["container_name"] == cname else v
                for v in vps_meta
            ]
        _update_config(config_path, {"vps_meta": vps_meta})
        return True, f"VPS metadata stored: {cname}"
    except Exception as exc:
        return False, f"Failed to store VPS metadata: {exc}"


def _job_remove_vps_meta(container_name: str, config_path: str) -> tuple:
    """Remove a VPS record from local config (VPS was deleted)."""
    try:
        cfg      = _load_config(config_path)
        vps_meta = [
            v for v in cfg.get("vps_meta", [])
            if v.get("container_name") != container_name
        ]
        _update_config(config_path, {"vps_meta": vps_meta})
        return True, f"VPS metadata removed: {container_name}"
    except Exception as exc:
        return False, f"Failed to remove VPS metadata: {exc}"


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description="DarkNodes Node Agent — Discord channel relay edition",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--channel", default="",
        help="Discord channel ID for this node's relay channel (from /node add).",
    )
    parser.add_argument(
        "--reg-token", default="",
        dest="reg_token",
        help="One-time registration token from /node add.  Required for first registration only.",
    )
    parser.add_argument(
        "--name", default="",
        help="Custom display name for this node (default: machine hostname).",
    )
    parser.add_argument(
        "--config", default=DEFAULT_CONFIG,
        help=f"Path to credentials/state file (default: {DEFAULT_CONFIG}).",
    )
    args = parser.parse_args()

    config_path = args.config

    # ── Read bot token from environment (never from CLI or config file) ────────
    discord_token = os.environ.get("DISCORD_TOKEN", "").strip()
    if not discord_token:
        logger.error(
            "DISCORD_TOKEN environment variable is not set.\n"
            "Set it to your Discord bot token before running the agent:\n"
            "    export DISCORD_TOKEN=\"your_bot_token_here\""
        )
        sys.exit(1)

    cfg        = _load_config(config_path)
    channel_id = args.channel or cfg.get("channel_id", "")
    node_id    = cfg.get("node_id", "")

    # ── First-time registration ────────────────────────────────────────────────
    if not node_id:
        if not channel_id:
            parser.error(
                "--channel is required for first registration.\n"
                "Run  /node add  in Discord to get the ready-to-run command."
            )
        if not args.reg_token:
            parser.error(
                "--reg-token is required for first registration.\n"
                "Run  /node add  in Discord to get the ready-to-run command."
            )

        try:
            node_id = register(channel_id, args.reg_token, discord_token, args.name)
        except Exception as exc:
            logger.error(f"Registration failed: {exc}")
            sys.exit(1)

        _save_config(config_path, {
            "channel_id":      channel_id,
            "node_id":         node_id,
            "name":            args.name or socket.gethostname(),
            "last_message_id": _snowflake_now(),
            "vps_meta":        [],
        })
        logger.info(f"Credentials saved to {config_path}")

    else:
        # ── Resume from saved config ───────────────────────────────────────────
        if args.channel and args.channel != channel_id:
            # Allow overriding the channel (e.g. if the admin re-ran /node add)
            channel_id = args.channel
            _update_config(config_path, {"channel_id": channel_id})

        if not channel_id:
            logger.error(
                "No channel_id in config.  Re-run with --channel to set it."
            )
            sys.exit(1)

        logger.info(
            f"Resuming as node_id={node_id}  channel={channel_id}"
        )

    # ── Main polling loop ──────────────────────────────────────────────────────
    run_agent(channel_id, node_id, discord_token, config_path)


if __name__ == "__main__":
    main()
