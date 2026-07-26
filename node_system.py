"""
DarkNodes Node System  —  Discord Channel Relay + Cloudflare Tunnel Edition
═══════════════════════════════════════════════════════════════════════════════
Architecture
────────────
  Discord Bot  (slash commands, embeds, user interactions)
      │
  Discord Servers  ◄────────────────────────────────────────────────────────┐
      │                                                                      │
      │  on_message events (WebSocket — bot already has this connection)     │
      │  channel.send() for job commands                                     │
      │                                                                      │
  Node Agent  ──── outbound HTTPS to discord.com/api ──── sends heartbeats,
  (node_agent.py)    polls for commands, posts results

No public IP is required on the bot or on any node.
No port forwarding, no SSH tunnels, no firewall changes.
Both sides only need outbound HTTPS to discord.com (port 443).

Cloudflare Tunnel (optional)
────────────────────────────
A small aiohttp HTTP server runs inside the bot process on NODE_MANAGER_PORT
(default 8765).  When Cloudflare Tunnel is configured (/node tunnel), the
tunnel exposes this server publicly.  The public URL is then used by /node add
to show administrators a one-liner install command:

    curl -fsSL https://nodes.example.com/install.sh | bash

The install.sh is generated on-the-fly with the DNODE registration token
embedded, so the remote machine needs no other arguments.

How it works
────────────
• On /node add the bot creates a private Discord channel (#node-<id>) in the
  server's "🖥 DarkNodes" category visible only to the bot.
• The bot shows a setup command.  On the remote machine the admin runs:
      export DISCORD_TOKEN="the_bot_token"   # same token the bot uses
      python3 node_agent.py --channel <CHANNEL_ID> --reg-token <TOKEN>
• The agent posts a registration message into the channel using the Discord
  REST API (outbound HTTPS from the agent only — no inbound needed).
• The bot's on_message handler validates the token and activates the node.
• Normal operation: agent polls the channel for job commands every 2–5 s,
  sends a heartbeat every 30 s.  The bot posts jobs as channel messages and
  reads results via on_message.

Message format
──────────────
  →  (U+2192)  =  bot → agent   (job commands)
  ←  (U+2190)  =  agent → bot   (heartbeat, result, registration, vps-sync)

Both sides filter by prefix so messages are never confused, even though the
bot token is used by both parties (agent appears as the same Discord user).

Registration flow
─────────────────
1. Admin runs  /node add  in Discord.
2. Bot creates channel #node-<id>, stores a one-time token → channel mapping.
3. Bot responds (ephemeral) with the setup command — channel ID + token.
4. Admin sets DISCORD_TOKEN on the remote machine and runs the command.
5. Agent posts  ←{"type":"reg","token":"...","hostname":"..."}  to channel.
6. Bot validates token, promotes node from "pending" → "remote", posts
   →{"type":"registered","node_id":"..."}  confirmation back.
7. Agent saves config; normal heartbeat + job-poll loop begins.

Integration with bot.py (unchanged interface)
─────────────────────────────────────────────
  node_system.init(docker_exec_fn, run_docker_fn, get_logo_fn, get_brand_fn,
                   main_admin_id, admin_data_ref,
                   vps_data_ref=vps_data, save_data_fn=save_data)
  node_system.register_commands(bot)
  asyncio.create_task(node_system.startup())   # inside on_ready
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import secrets
import shutil
import socket
import subprocess
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Dict, List, Optional

import discord
from discord import app_commands

# ── Logger ────────────────────────────────────────────────────────────────────
logger = logging.getLogger("vps_bot.nodes")

# ── Constants ─────────────────────────────────────────────────────────────────
_BASE             = os.path.dirname(os.path.abspath(__file__))
NODES_FILE        = os.path.join(_BASE, "nodes.json")
TOKENS_FILE              = os.path.join(_BASE, "node_tokens.json")
RECONNECT_TOKENS_FILE    = os.path.join(_BASE, "node_reconnect_tokens.json")
TUNNEL_FILE              = os.path.join(_BASE, "node_tunnel.json")
TOKEN_EXPIRY_MIN         = 60   # minutes a registration token is valid
RECONNECT_TOKEN_EXPIRY_MIN = 10  # minutes a reconnect token is valid
OFFLINE_SECS      = 90       # no heartbeat → node considered offline
SELECT_TIMEOUT_S  = 90       # seconds user has to pick a node
LOCAL_NODE_ID     = "local"
CATEGORY_NAME     = "🖥 DarkNodes"   # Discord category for node channels

# Node Manager HTTP server (serves install.sh and node_agent.py for Cloudflare Tunnel)
NODE_MANAGER_PORT = int(os.environ.get("NODE_MANAGER_PORT", "8765"))

# Path to the pre-exported VPS image tarball served to remote nodes at
# GET /image/darknodes-vps.  Set by bot.py after each successful local build.
_image_export_path: str = ""


def set_image_export_path(path: str) -> None:
    """Register the path to the exported image tarball so the HTTP server
    can serve it.  Called by bot.py after every successful image export."""
    global _image_export_path
    _image_export_path = path
    logger.info(f"[nodes] Image export registered at {path}")

# Message direction tags
MSG_FROM_BOT   = "→"   # bot → agent  (job commands, confirmations)
MSG_FROM_AGENT = "←"   # agent → bot  (heartbeats, results, registration)

# ── Injected by init() ────────────────────────────────────────────────────────
_docker_exec:   Optional[Callable] = None
_run_docker:    Optional[Callable] = None
_get_logo:      Optional[Callable] = None
_get_brand:     Optional[Callable] = None
_main_admin_id: str                = ""
_admin_data:    Optional[dict]     = None
_vps_data:      Optional[dict]     = None
_save_data_fn:  Optional[Callable] = None
_bot:           Optional[Any]      = None

# ── In-memory state ───────────────────────────────────────────────────────────
nodes:             Dict[str, dict] = {}
_tokens:           Dict[str, dict] = {}
_reconnect_tokens: Dict[str, dict] = {}
# channel_id (str) → node_id for active remote nodes
_node_by_channel:  Dict[str, str]  = {}
# job_id → {command, timeout, created_at, node_id}
_pending_jobs:     Dict[str, dict] = {}
# job_id → asyncio.Event (set when result arrives)
_job_events:       Dict[str, asyncio.Event] = {}
# job_id → {success, output}
_job_results:      Dict[str, dict] = {}
# node_id → bool (previous online state, for change-detection)
_node_was_online:  Dict[str, bool] = {}


# ══════════════════════════════════════════════════════════════════════════════
# Persistence
# ══════════════════════════════════════════════════════════════════════════════

def _load_nodes() -> None:
    global nodes
    try:
        with open(NODES_FILE) as fh:
            nodes = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        nodes = {}


def _save_nodes() -> None:
    try:
        with open(NODES_FILE, "w") as fh:
            json.dump(nodes, fh, indent=2)
    except Exception as exc:
        logger.error(f"[nodes] save_nodes failed: {exc}")


def _load_tokens() -> None:
    global _tokens
    try:
        with open(TOKENS_FILE) as fh:
            _tokens = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        _tokens = {}


def _save_tokens() -> None:
    try:
        with open(TOKENS_FILE, "w") as fh:
            json.dump(_tokens, fh, indent=2)
    except Exception as exc:
        logger.error(f"[nodes] save_tokens failed: {exc}")


def _load_reconnect_tokens() -> None:
    global _reconnect_tokens
    try:
        with open(RECONNECT_TOKENS_FILE) as fh:
            _reconnect_tokens = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        _reconnect_tokens = {}


def _save_reconnect_tokens() -> None:
    try:
        with open(RECONNECT_TOKENS_FILE, "w") as fh:
            json.dump(_reconnect_tokens, fh, indent=2)
    except Exception as exc:
        logger.error(f"[nodes] save_reconnect_tokens failed: {exc}")


# ── Tunnel config ─────────────────────────────────────────────────────────────

def _load_tunnel() -> dict:
    try:
        with open(TUNNEL_FILE) as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_tunnel(data: dict) -> None:
    try:
        with open(TUNNEL_FILE, "w") as fh:
            json.dump(data, fh, indent=2)
    except Exception as exc:
        logger.error(f"[nodes] save_tunnel failed: {exc}")


def _tunnel_configured() -> bool:
    cfg = _load_tunnel()
    return bool(cfg.get("tunnel_url") and cfg.get("configured"))


def _tunnel_url() -> str:
    cfg = _load_tunnel()
    return cfg.get("tunnel_url", "").rstrip("/")


# ── DNODE token helpers ───────────────────────────────────────────────────────
# DNODE token = "DNODE_" + base64url( JSON({n: node_id, r: reg_token, u: manager_url}) )
# This lets the install.sh decode all info from a single token — no extra args needed.
#
# RNODE token = "RNODE_" + base64url( JSON({t: token_code, u: manager_url}) )
# Short-lived reconnect token — restores an existing node's identity.

def _encode_dnode_token(node_id: str, reg_token: str, manager_url: str) -> str:
    payload = json.dumps({
        "n": node_id,
        "r": reg_token,
        "u": manager_url,
    }, separators=(",", ":")).encode()
    return "DNODE_" + base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_dnode_token(dnode: str) -> Optional[dict]:
    try:
        if not dnode.startswith("DNODE_"):
            return None
        b64 = dnode[6:]
        b64 += "=" * (-len(b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(b64).decode())
        return {
            "node_id":     payload.get("n", ""),
            "reg_token":   payload.get("r", ""),
            "manager_url": payload.get("u", ""),
        }
    except Exception:
        return None


def _encode_rnode_token(token_code: str, manager_url: str) -> str:
    """Encode a short-lived reconnect token as a self-contained RNODE_xxx string."""
    payload = json.dumps({
        "t": token_code,
        "u": manager_url,
    }, separators=(",", ":")).encode()
    return "RNODE_" + base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_rnode_token(rnode: str) -> Optional[dict]:
    """Decode an RNODE_xxx token.  Returns {token_code, manager_url} or None."""
    try:
        if not rnode.startswith("RNODE_"):
            return None
        b64 = rnode[6:]
        b64 += "=" * (-len(b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(b64).decode())
        return {
            "token_code":  payload.get("t", ""),
            "manager_url": payload.get("u", ""),
        }
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════════
# Channel index
# ══════════════════════════════════════════════════════════════════════════════

def _rebuild_channel_index() -> None:
    """
    Build _node_by_channel from the nodes dict.  Called on startup.

    Also seeds _node_was_online from the persisted last_seen so the offline
    monitor doesn't immediately fire "went offline" notifications for nodes
    that were online when the bot was last running.
    """
    global _node_by_channel
    _node_by_channel = {}
    for nid, node in nodes.items():
        cid = node.get("channel_id", "")
        if cid and node.get("type") == "remote":
            _node_by_channel[str(cid)] = nid
        # Seed the previous-online state from the persisted last_seen so the
        # monitor doesn't spam "went offline" for every node on restart.
        if node.get("type") not in ("local", "pending"):
            _node_was_online[nid] = _node_online(node)


# ══════════════════════════════════════════════════════════════════════════════
# Discord channel management
# ══════════════════════════════════════════════════════════════════════════════

async def _get_or_create_category(guild: discord.Guild) -> discord.CategoryChannel:
    cat = discord.utils.get(guild.categories, name=CATEGORY_NAME)
    if cat:
        return cat
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        guild.me: discord.PermissionOverwrite(
            read_messages=True, send_messages=True,
            manage_messages=True, manage_channels=True,
        ),
    }
    cat = await guild.create_category(CATEGORY_NAME, overwrites=overwrites)
    logger.info(f"[nodes] Created category '{CATEGORY_NAME}' in guild {guild.id}")
    return cat


async def _create_node_channel(
    guild: discord.Guild, node_id: str,
    invoker: Optional[discord.Member] = None,
) -> discord.TextChannel:
    """Create a private channel for a node.  Returns the new channel."""
    category = await _get_or_create_category(guild)
    overwrites: dict = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        guild.me: discord.PermissionOverwrite(
            read_messages=True, send_messages=True,
            manage_messages=True, manage_channels=True,
        ),
    }
    # Give the invoking admin read access so they can see the channel exists
    if invoker and invoker != guild.me:
        overwrites[invoker] = discord.PermissionOverwrite(read_messages=True, send_messages=False)

    channel = await guild.create_text_channel(
        f"node-{node_id}",
        category=category,
        overwrites=overwrites,
        topic=f"DarkNodes private relay channel for node {node_id}. Do not post here manually.",
    )
    logger.info(f"[nodes] Created Discord channel #{channel.name} ({channel.id}) for node {node_id}")
    return channel


async def _delete_node_channel(node: dict) -> None:
    """Delete the Discord channel associated with a node (best-effort)."""
    if not _bot:
        return
    cid = node.get("channel_id", "")
    if not cid:
        return
    try:
        channel = _bot.get_channel(int(cid))
        if channel:
            await channel.delete(reason="Node removed via /node remove")
            logger.info(f"[nodes] Deleted Discord channel {cid} for node {node.get('id','?')}")
    except Exception as exc:
        logger.debug(f"[nodes] Could not delete node channel {cid}: {exc}")


# ══════════════════════════════════════════════════════════════════════════════
# Channel resolution, healing, and audit
# ══════════════════════════════════════════════════════════════════════════════

async def _resolve_channel(node_id: str) -> Optional[discord.TextChannel]:
    """
    Return the live Discord channel object for a node.
    Tries the in-memory cache first (instant), then a REST API fetch (one HTTP
    call).  Returns None if the channel has been deleted or is unreachable.
    """
    node = nodes.get(node_id)
    if not node or not _bot:
        return None
    cid = node.get("channel_id", "")
    if not cid:
        return None
    ch = _bot.get_channel(int(cid))
    if ch:
        return ch
    try:
        ch = await _bot.fetch_channel(int(cid))
        return ch
    except (discord.NotFound, discord.Forbidden, Exception):
        return None


async def _heal_node_channel(node_id: str) -> tuple:
    """
    Automatic channel recovery when the stored channel no longer exists.

    Steps
    ─────
    1. Locate the guild where the node originally registered.
    2. Create a replacement channel in the DarkNodes category.
    3. Update node["channel_id"], _node_by_channel, and nodes.json.
    4. Invalidate stale registration tokens; issue a fresh DNODE token.
    5. DM the admin with the reconnect command — no manual node deletion needed.

    Returns (success: bool, human_readable_message: str).
    """
    if not _bot:
        return False, "Bot not initialised."

    node = nodes.get(node_id)
    if not node:
        return False, f"Unknown node {node_id!r}."

    guild_id = node.get("guild_id", "")
    if not guild_id:
        return False, (
            f"Node {node_id!r} has no `guild_id` stored — cannot recreate its channel. "
            "Use `/node remove` then `/node add` to fully re-register it."
        )

    # Resolve the guild
    guild: Optional[discord.Guild] = _bot.get_guild(int(guild_id))
    if not guild:
        try:
            guild = await _bot.fetch_guild(int(guild_id))
        except Exception as exc:
            return False, f"Cannot access guild {guild_id}: {exc}"

    # Remove the dead channel from the index so it doesn't pollute routing
    old_cid = node.get("channel_id", "")
    _node_by_channel.pop(old_cid, None)

    # Create the replacement channel
    try:
        new_ch = await _create_node_channel(guild, node_id)
    except Exception as exc:
        # Restore index so the node is not left in a broken state
        if old_cid:
            _node_by_channel[old_cid] = node_id
        return False, f"Failed to create replacement channel: {exc}"

    new_cid = str(new_ch.id)
    node["channel_id"] = new_cid
    _node_by_channel[new_cid] = node_id
    _save_nodes()

    logger.warning(
        f"[nodes] Channel healed for node {node_id}: "
        f"old_channel={old_cid!r} → new_channel={new_cid!r}"
    )

    # Invalidate every stale pending reg token for this node; issue a fresh one
    stale = [tc for tc, td in list(_tokens.items()) if td.get("node_id") == node_id]
    for tc in stale:
        _tokens.pop(tc, None)

    tunnel_url = _tunnel_url()
    token_code = secrets.token_urlsafe(24)
    expires_at = (datetime.utcnow() + timedelta(minutes=TOKEN_EXPIRY_MIN)).isoformat()

    _tokens[token_code] = {
        "node_id":     node_id,
        "channel_id":  new_cid,
        "manager_url": tunnel_url,
        "expires_at":  expires_at,
    }
    _save_tokens()

    dnode_token = _encode_dnode_token(node_id, token_code, tunnel_url)

    # Notify admin — include a ready-to-paste reconnect command
    e = _embed("⚠️  Node Channel Recreated", 0xFEE75C)
    e.description = (
        f"The channel for **{node.get('name', node_id)}** (`{node_id}`) was missing and has been "
        f"recreated as {new_ch.mention}.\n"
        f"**The agent must reconnect before deployments will work.**"
    )
    e.add_field(
        name="🔧  Reconnect the agent",
        value=(
            f"```bash\n"
            f"python3 /opt/darknodes/node_agent.py --dnode-token \"{dnode_token}\"\n"
            f"systemctl restart darknodes-agent\n"
            f"```"
        ),
        inline=False,
    )
    e.add_field(name="🔐  DNODE Token", value=f"```\n{dnode_token}\n```", inline=False)
    asyncio.create_task(_notify_admin(e))

    msg = (
        f"New channel {new_ch.mention} created. "
        f"A reconnect token has been sent to the admin — run the provided "
        f"DNODE command on the remote machine, then retry the deployment."
    )
    return True, msg


async def _channel_audit_loop() -> None:
    """
    Background loop: verifies every online remote node's channel still exists.
    Starts 60 s after bot ready (to let Discord cache warm up), then every
    10 minutes thereafter.  Missing channels are healed automatically.
    """
    await asyncio.sleep(60)
    while True:
        try:
            for node_id, node in list(nodes.items()):
                if node.get("type") != "remote":
                    continue
                # Only audit nodes that were recently online — offline nodes may
                # simply not have connected yet; don't spam channel recreation.
                if not _node_online(node):
                    continue
                ch = await _resolve_channel(node_id)
                if ch is None:
                    logger.warning(
                        f"[nodes] Audit: channel missing for ONLINE node {node_id} — healing"
                    )
                    healed, msg = await _heal_node_channel(node_id)
                    logger.info(
                        f"[nodes] Heal result for {node_id}: healed={healed}  {msg}"
                    )
        except Exception as exc:
            logger.debug(f"[nodes] channel audit loop error: {exc}")
        await asyncio.sleep(600)   # re-audit every 10 minutes


# ══════════════════════════════════════════════════════════════════════════════
# Sending messages to a node via Discord
# ══════════════════════════════════════════════════════════════════════════════

async def _send_to_node(node_id: str, payload: dict) -> bool:
    """
    Post a bot→agent message into the node's private channel.
    Returns True on success.

    Falls back to fetch_channel() when the channel is not in the bot's
    in-memory cache (e.g. after a bot restart or long idle period) so
    that a cold cache never causes spurious "channel not found" failures.
    """
    node = nodes.get(node_id)
    if not node or not _bot:
        return False
    cid = node.get("channel_id", "")
    if not cid:
        return False

    # Try cache first (instant); fall back to API fetch on cache miss.
    channel = _bot.get_channel(int(cid))
    if not channel:
        try:
            channel = await _bot.fetch_channel(int(cid))
        except Exception as exc:
            logger.warning(
                f"[nodes] Cannot find channel {cid} for node {node_id}: {exc}"
            )
            return False

    try:
        content = MSG_FROM_BOT + json.dumps(payload, separators=(",", ":"))
        await channel.send(content)
        return True
    except Exception as exc:
        logger.warning(f"[nodes] Failed to send to node {node_id}: {exc}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# Agent message handlers  (called from on_message)
# ══════════════════════════════════════════════════════════════════════════════

async def _handle_agent_register(
    message: discord.Message,
    token: str,
    tok_data: dict,
    data: dict,
) -> None:
    """Process  ←{"type":"reg","token":"...","hostname":"...","public_ip":"..."}"""
    # Check token expiry
    try:
        exp = datetime.fromisoformat(tok_data["expires_at"])
        if datetime.utcnow() > exp:
            del _tokens[token]
            _save_tokens()
            logger.warning(f"[nodes] Expired registration token used in channel {message.channel.id}")
            return
    except Exception:
        pass

    node_id    = tok_data.get("node_id", "")
    channel_id = str(message.channel.id)
    hostname   = data.get("hostname", socket.gethostname()).strip() or socket.gethostname()
    public_ip  = data.get("public_ip", "").strip()

    if not node_id or node_id not in nodes:
        logger.warning(f"[nodes] Registration for unknown node_id {node_id!r}")
        return

    # ── Snapshot old state so we can roll back if anything goes wrong ─────────
    _old_node        = dict(nodes[node_id])
    _old_token_entry = dict(tok_data)   # keep a copy in case token was already consumed
    _channel_indexed = False

    try:
        # Promote from pending to active remote node
        nodes[node_id].update({
            "type":       "remote",
            "hostname":   hostname,
            "public_ip":  public_ip,
            "last_seen":  _now_iso(),
            "stats":      {},
            # Persist the channel_id in case it was just healed to a new one
            "channel_id": channel_id,
        })
        _save_nodes()

        # Consume the token
        del _tokens[token]
        _save_tokens()

        # Update channel index
        _node_by_channel[channel_id] = node_id
        _channel_indexed             = True
        _node_was_online[node_id]    = True

        logger.info(
            f"[nodes] Remote node registered: {node_id} "
            f"(hostname={hostname!r}  ip={public_ip or 'unknown'}  channel={channel_id})"
        )

        # Send confirmation back so the agent knows its node_id
        await message.channel.send(
            MSG_FROM_BOT + json.dumps({
                "type":    "registered",
                "node_id": node_id,
                "name":    hostname,
            }, separators=(",", ":"))
        )

    except Exception as exc:
        # ── Rollback ──────────────────────────────────────────────────────────
        logger.error(
            f"[nodes] Registration failed for {node_id} — rolling back. Error: {exc}"
        )
        # Restore node state
        nodes[node_id] = _old_node
        try:
            _save_nodes()
        except Exception:
            pass
        # Restore token so the agent can retry
        if token not in _tokens:
            _tokens[token] = _old_token_entry
            try:
                _save_tokens()
            except Exception:
                pass
        # Remove from channel index if we partially wrote it
        if _channel_indexed:
            _node_by_channel.pop(channel_id, None)
        return

    # Notify admin via DM (best-effort — not rolled back on failure)
    if _bot:
        e = _embed("🟢  Node Connected", 0x57F287)
        e.description = f"**{hostname}** has registered and is online."
        e.add_field(name="🔑 ID",       value=f"`{node_id}`",                inline=True)
        e.add_field(name="💻 Hostname",  value=f"`{hostname}`",               inline=True)
        e.add_field(name="🌍 IP",        value=f"`{public_ip or 'unknown'}`", inline=True)
        e.add_field(name="📢 Channel",   value=message.channel.mention,       inline=True)
        asyncio.create_task(_notify_admin(e))


def _handle_agent_heartbeat(node_id: str, data: dict) -> None:
    """Process  ←{"type":"hb","cpu":...,"ram_used_mb":...,...}"""
    node = nodes.get(node_id)
    if not node:
        return
    node["stats"] = {
        "cpu":           float(data.get("cpu", 0)),
        "cpu_total":     int(data.get("cpu_total", 0)),
        "ram_used_mb":   int(data.get("ram_used_mb", 0)),
        "ram_total_mb":  int(data.get("ram_total_mb", 0)),
        "running_vps":   int(data.get("running_vps", 0)),
        "disk_used_gb":  float(data.get("disk_used_gb", 0)),
        "disk_total_gb": float(data.get("disk_total_gb", 0)),
    }
    node["last_seen"] = _now_iso()
    _save_nodes()
    logger.debug(
        f"[nodes] HB from {node_id}: "
        f"CPU={data.get('cpu')}%  RAM={data.get('ram_used_mb')}/{data.get('ram_total_mb')}MB"
    )


def _handle_agent_result(data: dict) -> None:
    """Process  ←{"type":"result","job_id":"...","success":true,"output":"..."}"""
    job_id  = data.get("job_id", "")
    success = bool(data.get("success", False))
    output  = str(data.get("output", ""))

    _pending_jobs.pop(job_id, None)
    _job_results[job_id] = {"success": success, "output": output}

    ev = _job_events.get(job_id)
    if ev:
        ev.set()

    logger.debug(f"[nodes] Job result {job_id}: success={success}  output={output[:80]!r}")


# ── HTTP node authentication ──────────────────────────────────────────────────

def _auth_node(data: dict) -> Optional[str]:
    """
    Validate a node_id + node_api_key pair from an HTTP request body.
    Returns the node_id on success, None on failure.
    """
    node_id      = data.get("node_id", "")
    node_api_key = data.get("node_api_key", "")
    node = nodes.get(node_id)
    if not node:
        return None
    stored = node.get("node_api_key", "")
    if not stored or stored != node_api_key:
        return None
    # Any authenticated request proves that the agent is alive.  In particular,
    # the jobs poll continues during normal operation and is a useful fallback
    # liveness signal if a heartbeat request is delayed by the host.  Do not
    # persist this on every one-second poll; the heartbeat handler persists
    # stats at its normal cadence.
    node["last_seen"] = _now_iso()
    return node_id


async def _handle_agent_vsync(node_id: str, data: dict) -> None:
    """Process  ←{"type":"vsync","vps_list":[...]}"""
    if not _vps_data or not _save_data_fn:
        return

    vps_list = data.get("vps_list", [])
    if not vps_list:
        return

    # Known container names (to avoid duplicates)
    known: set = set()
    for user_vps_list in _vps_data.values():
        for vps in user_vps_list:
            known.add(vps.get("container_name", ""))

    merged = 0
    for vps_rec in vps_list:
        cname   = vps_rec.get("container_name", "")
        user_id = vps_rec.get("user_id", "")
        if not cname or not user_id or cname in known:
            continue
        vps_rec["node_id"] = node_id
        vps_rec.setdefault("status", "running")
        _vps_data.setdefault(user_id, []).append(vps_rec)
        known.add(cname)
        merged += 1
        logger.info(f"[nodes] VPS metadata restored from agent {node_id}: {cname} (user={user_id})")

    if merged:
        try:
            _save_data_fn()
        except Exception as exc:
            logger.error(f"[nodes] save_data failed after vsync: {exc}")


# ══════════════════════════════════════════════════════════════════════════════
# Discord on_message listener
# ══════════════════════════════════════════════════════════════════════════════

async def _on_node_message(message: discord.Message) -> None:
    """
    Registered as a bot listener.  Handles all agent→bot messages that arrive
    in node channels.  Only processes messages from the bot's own user account
    (the agent uses the same Discord token) with the ← prefix.
    """
    # Must be from the bot itself (agent uses the same bot token)
    if not _bot or not _bot.user or message.author.id != _bot.user.id:
        return

    content = message.content or ""
    if not content.startswith(MSG_FROM_AGENT):
        return  # Bot's own output (→) or unrelated message

    channel_id = str(message.channel.id)

    try:
        data = json.loads(content[len(MSG_FROM_AGENT):])
    except (json.JSONDecodeError, ValueError):
        return

    msg_type = data.get("type", "")

    # ── Registration phase ──────────────────────────────────────────────────
    if msg_type == "reg":
        token = data.get("token", "")
        tok   = _tokens.get(token)
        if tok and tok.get("channel_id") == channel_id:
            await _handle_agent_register(message, token, tok, data)
        else:
            logger.warning(
                f"[nodes] Got reg message in channel {channel_id} but token "
                f"{token[:12]!r}… is unknown or mismatched"
            )
        return

    # ── Active node channel ─────────────────────────────────────────────────
    node_id = _node_by_channel.get(channel_id)
    if not node_id:
        return  # Not a known node channel

    if msg_type == "hb":
        _handle_agent_heartbeat(node_id, data)
    elif msg_type == "result":
        _handle_agent_result(data)
    elif msg_type == "vsync":
        asyncio.create_task(_handle_agent_vsync(node_id, data))
    else:
        logger.debug(f"[nodes] Unknown agent message type {msg_type!r} from node {node_id}")


# ══════════════════════════════════════════════════════════════════════════════
# Remote command execution  (via Discord channel message)
# ══════════════════════════════════════════════════════════════════════════════

async def remote_execute(node_id: str, command: str, timeout: int = 120) -> str:
    """
    Run a shell command on a remote node and return its stdout.
    Posts a job message to the node's private Discord channel; waits for the
    agent to pick it up and reply.
    Raises RuntimeError on failure or timeout.
    """
    node = nodes.get(node_id)
    if not node:
        raise RuntimeError(f"Unknown node: {node_id}")
    if node.get("type") == "local":
        raise RuntimeError("Use run_docker_command for the local node, not remote_execute.")
    if node.get("type") == "pending":
        raise RuntimeError(f"Node {node_id} has not completed registration yet.")

    if not _node_online(node):
        raise RuntimeError(f"Node {node_id} ({node.get('name','?')}) is offline.")

    # ── Determine transport mode ────────────────────────────────────────────────
    # HTTP nodes (those with a node_api_key) receive jobs via HTTP polling of
    # /api/jobs.  The Discord channel message is an optional push notification
    # that speeds up delivery but is NOT required.  For legacy channel-only nodes
    # the Discord channel IS the delivery mechanism and must be valid.
    is_http_node = bool(node.get("node_api_key"))

    # ── Pre-flight: verify the communication channel (channel-mode nodes only) ──
    # For HTTP nodes a missing/deleted channel does not block job delivery — the
    # agent polls /api/jobs on its own schedule.  We still try to heal the channel
    # for admin visibility, but we never block the job on the result.
    if not is_http_node:
        channel = await _resolve_channel(node_id)
        if channel is None:
            logger.warning(
                f"[nodes] pre-flight: channel missing for channel-mode node {node_id} "
                f"— attempting auto-heal"
            )
            healed, heal_msg = await _heal_node_channel(node_id)
            node_name = node.get("name", node_id)
            if healed:
                raise RuntimeError(
                    f"Node `{node_name}` (`{node_id}`) communication channel was missing and "
                    f"has been **automatically recreated**.\n\n"
                    f"{heal_msg}\n\n"
                    f"The node must reconnect before deployments can proceed. "
                    f"Once the agent reconnects and sends its first heartbeat, retry."
                )
            else:
                raise RuntimeError(
                    f"Node `{node_name}` (`{node_id}`) communication channel is missing "
                    f"and could not be recreated automatically.\n\n"
                    f"Reason: {heal_msg}\n\n"
                    f"Use `/node remove {node_id}` then `/node add` to fully re-register this node."
                )
    else:
        # HTTP node — channel is admin-visibility only.  If it's gone, heal it
        # in the background so the admin can still see it, but never block the job.
        channel = await _resolve_channel(node_id)
        if channel is None:
            logger.warning(
                f"[nodes] pre-flight: channel missing for HTTP node {node_id} "
                f"— healing in background (job will proceed via HTTP polling)"
            )
            asyncio.create_task(_heal_node_channel(node_id))

    job_id = secrets.token_hex(8)
    event  = asyncio.Event()

    _pending_jobs[job_id]  = {
        "node_id":    node_id,
        "command":    command,
        "timeout":    timeout,
        "created_at": time.time(),
    }
    _job_events[job_id] = event

    # Send job to node via its Discord channel (push notification).
    # For HTTP nodes this is best-effort only — the agent polls /api/jobs
    # independently, so a send failure must not abort the job.
    sent = await _send_to_node(node_id, {
        "type":    "job",
        "job_id":  job_id,
        "command": command,
        "timeout": timeout,
    })
    if not sent:
        if is_http_node:
            # Non-fatal for HTTP nodes: agent will pick up via HTTP polling.
            logger.warning(
                f"[nodes] Discord push failed for HTTP node {node_id} job {job_id}; "
                f"agent will collect via /api/jobs polling (up to ~3s delay)."
            )
        else:
            # Channel-mode node: Discord is the only delivery path — abort.
            _pending_jobs.pop(job_id, None)
            _job_events.pop(job_id, None)
            raise RuntimeError(
                f"Could not deliver job to node `{node_id}` — "
                f"the channel was found but the message could not be sent. "
                f"Check that the bot has **Send Messages** permission in the node channel, "
                f"then retry."
            )

    logger.info(f"[nodes] Job {job_id} sent to node {node_id}: {command[:80]!r}")

    try:
        await asyncio.wait_for(event.wait(), timeout=timeout + 15)
    except asyncio.TimeoutError:
        _pending_jobs.pop(job_id, None)
        _job_events.pop(job_id, None)
        _job_results.pop(job_id, None)
        raise RuntimeError(
            f"Remote command timed out after {timeout}s "
            f"(node={node_id}, job={job_id})"
        )
    finally:
        _job_events.pop(job_id, None)

    result = _job_results.pop(job_id, None)
    if result is None:
        raise RuntimeError(f"No result received for job {job_id}")
    if not result.get("success"):
        raise RuntimeError(result.get("output") or "Remote command failed with no output")
    return result.get("output", "")


# ══════════════════════════════════════════════════════════════════════════════
# Local node stats
# ══════════════════════════════════════════════════════════════════════════════

def _read_cpu() -> float:
    try:
        def _snap():
            with open("/proc/stat") as f:
                parts = list(map(int, f.readline().split()[1:]))
            return sum(parts), parts[3]
        t1, i1 = _snap(); time.sleep(0.25); t2, i2 = _snap()
        return round((1 - (i2 - i1) / max(t2 - t1, 1)) * 100, 1)
    except Exception:
        return 0.0


def _read_ram() -> tuple:
    try:
        info: dict = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                info[k.strip()] = int(v.split()[0])
        total = info.get("MemTotal", 0) // 1024
        avail = info.get("MemAvailable", info.get("MemFree", 0)) // 1024
        return total - avail, total
    except Exception:
        return 0, 0


def _read_disk() -> tuple:
    try:
        usage = shutil.disk_usage("/")
        gb = 1024 ** 3
        return (usage.total - usage.free) / gb, usage.total / gb
    except Exception:
        return 0.0, 0.0


async def _count_local_vps() -> int:
    if not _run_docker:
        return 0
    try:
        out, _, rc = await _run_docker(
            'docker ps --filter "label=darknodes.vps=true" -q', timeout=10
        )
        if rc != 0:
            return 0
        return len([l for l in out.strip().splitlines() if l.strip()])
    except Exception:
        return 0


async def _collect_local_stats() -> dict:
    cpu             = await asyncio.to_thread(_read_cpu)
    used_mb, tot_mb = await asyncio.to_thread(_read_ram)
    used_gb, tot_gb = await asyncio.to_thread(_read_disk)
    vps             = await _count_local_vps()
    return {
        "cpu":           cpu,
        "cpu_total":     os.cpu_count() or 1,
        "ram_used_mb":   used_mb,
        "ram_total_mb":  tot_mb,
        "disk_used_gb":  round(used_gb, 2),
        "disk_total_gb": round(tot_gb, 2),
        "running_vps":   vps,
    }


def get_node_cpu_total(node_id: str | None) -> int:
    """Return the total CPU count for a node (0 = unknown)."""
    if not node_id or node_id == LOCAL_NODE_ID:
        return os.cpu_count() or 0
    node = nodes.get(node_id)
    if not node:
        return 0
    return int(node.get("stats", {}).get("cpu_total", 0))


async def _local_stats_loop() -> None:
    while True:
        try:
            stats = await _collect_local_stats()
            if LOCAL_NODE_ID in nodes:
                nodes[LOCAL_NODE_ID]["stats"]     = stats
                nodes[LOCAL_NODE_ID]["last_seen"] = _now_iso()
                _save_nodes()
        except Exception as exc:
            logger.debug(f"[nodes] local stats loop error: {exc}")
        await asyncio.sleep(30)


# ══════════════════════════════════════════════════════════════════════════════
# Offline monitor
# ══════════════════════════════════════════════════════════════════════════════

async def _offline_monitor_loop() -> None:
    await asyncio.sleep(30)
    while True:
        try:
            for node_id, node in list(nodes.items()):
                if node.get("type") in ("local", "pending"):
                    continue
                currently_online = _node_online(node)
                was_online       = _node_was_online.get(node_id, True)
                if currently_online == was_online:
                    continue
                _node_was_online[node_id] = currently_online
                name = node.get("name", node_id)
                if currently_online:
                    logger.info(f"[nodes] Node {node_id} ({name}) came back online")
                    if _bot:
                        e = _embed("🟢  Node Back Online", 0x57F287)
                        e.description = f"**{name}** (`{node_id}`) is back online and sending heartbeats."
                        asyncio.create_task(_notify_admin(e))
                else:
                    logger.info(f"[nodes] Node {node_id} ({name}) went offline")
                    if _bot:
                        e = _embed("🔴  Node Offline", 0xED4245)
                        e.description = (
                            f"**{name}** (`{node_id}`) stopped sending heartbeats.\n"
                            f"Run `/node reconnect {node_id}` or restart `darknodes-agent` on that machine."
                        )
                        asyncio.create_task(_notify_admin(e))
        except Exception as exc:
            logger.debug(f"[nodes] offline monitor error: {exc}")
        await asyncio.sleep(30)


async def _notify_admin(embed: discord.Embed) -> None:
    if not _bot or not _main_admin_id:
        return
    try:
        user = await _bot.fetch_user(int(_main_admin_id))
        await user.send(embed=embed)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# Local node initialisation
# ══════════════════════════════════════════════════════════════════════════════

async def _init_local_node() -> None:
    if LOCAL_NODE_ID in nodes:
        return
    hostname = socket.gethostname()
    nodes[LOCAL_NODE_ID] = {
        "id":         LOCAL_NODE_ID,
        "name":       hostname,
        "type":       "local",
        "hostname":   hostname,
        "created_at": _now_iso(),
        "last_seen":  _now_iso(),
        "stats":      {},
        "channel_id": "",
    }
    _save_nodes()
    logger.info(f"[nodes] Local node initialised (hostname={hostname})")


# ══════════════════════════════════════════════════════════════════════════════
# Node helpers
# ══════════════════════════════════════════════════════════════════════════════

def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _node_online(node: dict) -> bool:
    if node.get("type") == "local":
        return True
    if node.get("type") == "pending":
        return False
    last = node.get("last_seen")
    if not last:
        return False
    try:
        return (datetime.utcnow() - datetime.fromisoformat(last)).total_seconds() < OFFLINE_SECS
    except Exception:
        return False


def list_online_nodes() -> Dict[str, dict]:
    return {nid: n for nid, n in nodes.items() if _node_online(n)}


def get_node(node_id: str) -> Optional[dict]:
    return nodes.get(node_id)


def auto_select_node() -> str:
    candidates = list(list_online_nodes().items())
    if not candidates:
        return LOCAL_NODE_ID

    def _score(item):
        s    = item[1].get("stats", {})
        cpu  = s.get("cpu", 100.0)
        tot  = max(s.get("ram_total_mb", 1), 1)
        free = (tot - s.get("ram_used_mb", tot)) / tot * 100
        return (cpu, -free)

    return sorted(candidates, key=_score)[0][0]


# ══════════════════════════════════════════════════════════════════════════════
# Discord embed helpers
# ══════════════════════════════════════════════════════════════════════════════

def _brand() -> str:
    return _get_brand() if _get_brand else "DarkNodes"


def _logo() -> str:
    return _get_logo() if _get_logo else ""


def _embed(title: str, color: int = 0x5865F2) -> discord.Embed:
    e    = discord.Embed(title=title, color=color,
                         timestamp=datetime.now(timezone.utc).replace(tzinfo=None))
    logo = _logo()
    if logo:
        e.set_author(name=f"{_brand()} Node System", icon_url=logo)
    e.set_footer(text=f"{_brand()}  •  Node Manager", icon_url=logo or None)
    return e


def _err(title: str, desc: str) -> discord.Embed:
    e = _embed(f"❌  {title}", 0xED4245)
    e.description = desc
    return e


def _ok(title: str, desc: str = "") -> discord.Embed:
    e = _embed(f"✅  {title}", 0x57F287)
    if desc:
        e.description = desc
    return e


def _status_dot(node: dict) -> str:
    return "🟢" if _node_online(node) else "🔴"


def _rbar(used, total, width: int = 10) -> str:
    """Compact ASCII resource bar  e.g. ████████░░"""
    if not total or total <= 0:
        return "░" * width
    pct    = max(0.0, min(1.0, float(used) / float(total)))
    filled = round(pct * width)
    return "█" * filled + "░" * (width - filled)


def _fmt_ram(used_mb, total_mb) -> str:
    """Return a human-readable RAM string (MB → GB when ≥ 1 GB)."""
    if used_mb is None or not total_mb:
        return "—"
    if total_mb >= 1024:
        return f"{used_mb/1024:.1f}/{total_mb/1024:.1f} GB"
    return f"{used_mb}/{total_mb} MB"


def _stats_line(node: dict) -> str:
    """One compact line with progress bars for CPU / RAM / Disk / VPS count."""
    s = node.get("stats", {})
    if not s:
        return "*No stats — agent not yet connected*"
    cpu  = s.get("cpu", 0)
    u, t = s.get("ram_used_mb", 0), s.get("ram_total_mb", 0)
    du, dt = s.get("disk_used_gb", 0), s.get("disk_total_gb", 0)
    vps  = s.get("running_vps", 0)
    ram_pct  = int(u / t * 100)  if t  else 0
    disk_pct = int(du / dt * 100) if dt else 0
    return (
        f"⚙️ `{_rbar(cpu, 100, 8)}` {cpu}%  "
        f"🧠 `{_rbar(u, t, 8)}` {ram_pct}%  "
        f"💾 `{_rbar(du, dt, 8)}` {disk_pct}%  "
        f"🖥️ {vps} VPS"
    )


def _last_seen_str(node: dict) -> str:
    if node.get("type") == "local":
        return "Always online (local machine)"
    if node.get("type") == "pending":
        return "Never — waiting for first agent connection"
    last = node.get("last_seen")
    if not last or not node.get("stats"):
        return "Never — run the node agent to register"
    try:
        dt   = datetime.fromisoformat(last)
        unix = int(dt.replace(tzinfo=timezone.utc).timestamp())
        return f"<t:{unix}:R>"
    except Exception:
        return f"`{last}`"


def _connection_status_str(node: dict) -> str:
    if node.get("type") == "local":
        return "🟢 Online — local machine"
    if node.get("type") == "pending":
        return "⚫ Pending — agent has not registered yet"
    last  = node.get("last_seen")
    stats = node.get("stats")
    if not last or not stats:
        return "⚫ Never connected — run the node agent"
    if _node_online(node):
        return "🟢 Online — heartbeat active"
    return "🔴 Offline — no heartbeat received recently"


# ── Autocomplete helper ───────────────────────────────────────────────────────

async def _node_id_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Return matching node IDs with name + status prefix for Discord autocomplete."""
    choices: list[app_commands.Choice[str]] = []
    for nid, n in nodes.items():
        display = n.get("name") or n.get("hostname") or nid
        dot     = _status_dot(n)
        label   = f"{dot} {display} [{nid}]"
        if not current or current.lower() in nid.lower() or current.lower() in display.lower():
            choices.append(app_commands.Choice(name=label[:100], value=nid))
        if len(choices) >= 25:
            break
    return choices


# ══════════════════════════════════════════════════════════════════════════════
# Node selection UI
# ══════════════════════════════════════════════════════════════════════════════

class _NodeSelectView(discord.ui.View):
    def __init__(self, online: Dict[str, dict]):
        super().__init__(timeout=SELECT_TIMEOUT_S)
        self._chosen: Optional[str] = None
        self._event   = asyncio.Event()

        opts = [discord.SelectOption(
            label="⭐  Auto Select (healthiest node)",
            value="__auto__",
            description="Bot picks the node with most free resources",
            default=True,
        )]
        for nid, n in online.items():
            s   = n.get("stats", {})
            cpu = s.get("cpu", "?")
            u   = s.get("ram_used_mb")
            t   = s.get("ram_total_mb")
            ram = f"{u}/{t}MB" if u is not None and t else "?"
            tag = "(local)" if nid == LOCAL_NODE_ID else "(remote)"
            opts.append(discord.SelectOption(
                label=f"{n.get('name', nid)} {tag}",
                value=nid,
                description=f"CPU {cpu}%  RAM {ram}  VPSes {s.get('running_vps','?')}",
            ))

        sel = discord.ui.Select(placeholder="Choose a deployment node…", options=opts, row=0)
        sel.callback = self._on_select
        self.add_item(sel)
        self._sel = sel

        ok = discord.ui.Button(label="Deploy Here", style=discord.ButtonStyle.success, row=1, emoji="🚀")
        ok.callback = self._on_ok
        self.add_item(ok)

        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.danger, row=1, emoji="✖")
        cancel.callback = self._on_cancel
        self.add_item(cancel)

    async def _on_select(self, i: discord.Interaction):
        await i.response.defer()

    async def _on_ok(self, i: discord.Interaction):
        val = self._sel.values[0] if self._sel.values else "__auto__"
        self._chosen = auto_select_node() if val == "__auto__" else val
        await i.response.defer()
        self._event.set()
        self.stop()

    async def _on_cancel(self, i: discord.Interaction):
        self._chosen = None
        await i.response.send_message("❌ Deployment cancelled.", ephemeral=True)
        self._event.set()
        self.stop()

    async def on_timeout(self):
        self._event.set()


async def maybe_select_node(ctx) -> Optional[str]:
    """
    Returns a node_id for deployment.
    • Only local node online → returns LOCAL_NODE_ID immediately (no UI).
    • Multiple nodes online  → shows a selection UI.
    • Returns None if cancelled / timed out.
    """
    online        = list_online_nodes()
    remote_online = {nid: n for nid, n in online.items() if nid != LOCAL_NODE_ID}

    if len(remote_online) == 0:
        return LOCAL_NODE_ID

    embed = _embed("🌐  Select Deployment Node", 0x5865F2)
    embed.description = (
        "Multiple nodes are available — choose where to deploy this VPS.\n"
        "The bot will automatically pick the healthiest node if you don't."
    )
    for nid, n in online.items():
        tag   = "🏠 Local" if nid == LOCAL_NODE_ID else "🌐 Remote"
        s     = n.get("stats", {})
        cpu   = s.get("cpu", 0)
        u, t  = s.get("ram_used_mb", 0), s.get("ram_total_mb", 0)
        du, dt = s.get("disk_used_gb", 0), s.get("disk_total_gb", 0)
        vps   = s.get("running_vps", 0)
        cpu_bar  = _rbar(cpu, 100, 10)
        ram_bar  = _rbar(u, t, 10)
        disk_bar = _rbar(du, dt, 10)
        embed.add_field(
            name=f"{_status_dot(n)}  **{n.get('name', nid)}** — {tag}",
            value=(
                f"> ⚙️ CPU  `{cpu_bar}` `{cpu}%`\n"
                f"> 🧠 RAM  `{ram_bar}` `{u}/{t} MB`\n"
                f"> 💾 Disk `{disk_bar}` `{du:.1f}/{dt:.1f} GB`  •  🖥️ VPSes `{vps}`"
            ),
            inline=False,
        )

    view = _NodeSelectView(online)
    msg  = await ctx.send(embed=embed, view=view)
    await view._event.wait()
    try:
        await msg.delete()
    except Exception:
        pass
    return view._chosen


# ══════════════════════════════════════════════════════════════════════════════
# Admin check helper
# ══════════════════════════════════════════════════════════════════════════════

def _is_admin(interaction: discord.Interaction) -> bool:
    uid = str(interaction.user.id)
    return uid == _main_admin_id or (
        bool(_admin_data) and uid in _admin_data.get("admins", [])
    )


# ══════════════════════════════════════════════════════════════════════════════
# Cloudflare Tunnel helpers
# ══════════════════════════════════════════════════════════════════════════════

def _cloudflared_installed() -> bool:
    """Check if cloudflared binary is available."""
    return shutil.which("cloudflared") is not None


def _run_shell_sync(cmd: str, timeout: int = 60) -> tuple:
    """Run a shell command synchronously.  Returns (success, output)."""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        combined = (result.stdout + result.stderr).strip()
        return result.returncode == 0, combined
    except subprocess.TimeoutExpired:
        return False, f"Command timed out after {timeout}s"
    except Exception as exc:
        return False, str(exc)


async def _shell(cmd: str, timeout: int = 120) -> tuple:
    """Run a shell command in a thread (non-blocking).  Returns (success, output)."""
    return await asyncio.to_thread(_run_shell_sync, cmd, timeout)


async def _install_cloudflared() -> tuple:
    """
    Install cloudflared on the local machine.
    Tries the official Cloudflare packages for Debian/Ubuntu and generic Linux.
    Returns (success: bool, message: str).
    """
    # Check if already installed
    if _cloudflared_installed():
        ok, ver = await _shell("cloudflared --version")
        ver_str = ver.splitlines()[0] if ver else "unknown"
        return True, f"Already installed: {ver_str}"

    # Detect architecture
    ok, arch = await _shell("uname -m")
    arch = arch.strip()
    if arch == "x86_64":
        arch_slug = "amd64"
    elif arch in ("aarch64", "arm64"):
        arch_slug = "arm64"
    elif arch.startswith("arm"):
        arch_slug = "arm"
    else:
        arch_slug = "amd64"

    # Try Debian/Ubuntu package first
    ok_dep, _ = await _shell("which dpkg")
    if ok_dep:
        pkg_url = (
            f"https://github.com/cloudflare/cloudflared/releases/latest/download/"
            f"cloudflared-linux-{arch_slug}.deb"
        )
        cmds = [
            f"curl -fsSL {pkg_url} -o /tmp/cloudflared.deb",
            "dpkg -i /tmp/cloudflared.deb",
            "rm -f /tmp/cloudflared.deb",
        ]
        for cmd in cmds:
            ok, out = await _shell(cmd, timeout=120)
            if not ok:
                break
        if ok and _cloudflared_installed():
            return True, "Installed via .deb package"

    # Fallback: download binary directly
    bin_url = (
        f"https://github.com/cloudflare/cloudflared/releases/latest/download/"
        f"cloudflared-linux-{arch_slug}"
    )
    ok1, _ = await _shell(f"curl -fsSL {bin_url} -o /usr/local/bin/cloudflared", timeout=120)
    ok2, _ = await _shell("chmod +x /usr/local/bin/cloudflared")

    if ok1 and ok2 and _cloudflared_installed():
        return True, "Installed binary to /usr/local/bin/cloudflared"

    return False, "Could not install cloudflared — please install it manually and re-run /node tunnel"


async def _configure_cloudflared_service(tunnel_token: str) -> tuple:
    """
    Install cloudflared as a systemd service using a tunnel token.
    Returns (success, message).
    """
    # Install the service unit
    ok, out = await _shell(
        f"cloudflared service install {tunnel_token}",
        timeout=60,
    )
    if not ok:
        return False, f"Service install failed:\n{out}"

    # Enable and start
    await _shell("systemctl daemon-reload")
    ok2, out2 = await _shell("systemctl enable --now cloudflared", timeout=30)
    if not ok2:
        # Some distros name the unit differently
        await _shell("systemctl enable --now cloudflared.service", timeout=30)

    return True, "cloudflared service installed and started"


async def _verify_tunnel(tunnel_url: str, timeout: int = 30) -> tuple:
    """
    Verify the Cloudflare Tunnel is online by checking the Node Manager
    health endpoint through the public URL.
    Returns (reachable: bool, message: str).
    """
    health_url = f"{tunnel_url.rstrip('/')}/healthz"
    ok, out = await _shell(
        f"curl -fsSL --max-time 10 --retry 3 --retry-delay 5 {health_url}",
        timeout=timeout,
    )
    if ok:
        return True, f"Tunnel is online — {health_url} responded"
    return False, (
        f"Could not reach {health_url} — the tunnel may still be starting up.\n"
        f"Wait 30 seconds and run  `/node tunnel`  again to check status."
    )


# ══════════════════════════════════════════════════════════════════════════════
# Node Manager HTTP server  (serves install.sh and node_agent.py)
# ══════════════════════════════════════════════════════════════════════════════

def _generate_install_sh(dnode_token: str, tunnel_url: str) -> str:
    """
    Generate the install.sh script served at /install.sh.
    The DNODE token encodes node_id + reg_token + manager_url.
    No Discord token is required on the remote machine.
    """
    agent_url = f"{tunnel_url.rstrip('/')}/node_agent.py"
    return f"""#!/usr/bin/env bash
# DarkNodes Node Agent — Automatic Installer
# Generated by DarkNodes Node System
# Run as root or with sudo
# No Discord token needed — the DNODE token is all you need.

set -euo pipefail

DNODE_TOKEN="{dnode_token}"
AGENT_URL="{agent_url}"
INSTALL_DIR="/opt/darknodes"
SERVICE_FILE="/etc/systemd/system/darknodes-agent.service"

echo "========================================"
echo "  DarkNodes Node Agent Installer"
echo "========================================"
echo ""

# ── Check requirements ──────────────────────────────────────────────────────
if [ "$EUID" -ne 0 ]; then
  echo "ERROR: Please run this script as root or with sudo"
  exit 1
fi

if ! command -v python3 &>/dev/null; then
  echo "ERROR: python3 is required. Install it with: apt install python3"
  exit 1
fi

# ── Create directory ────────────────────────────────────────────────────────
echo "[1/5] Creating install directory..."
mkdir -p "$INSTALL_DIR"

# ── Download node_agent.py ──────────────────────────────────────────────────
echo "[2/5] Downloading node_agent.py..."
if command -v curl &>/dev/null; then
  curl -fsSL "$AGENT_URL" -o "$INSTALL_DIR/node_agent.py"
elif command -v wget &>/dev/null; then
  wget -qO "$INSTALL_DIR/node_agent.py" "$AGENT_URL"
else
  echo "ERROR: curl or wget is required to download the agent."
  exit 1
fi
chmod +x "$INSTALL_DIR/node_agent.py"

# ── Register the node ───────────────────────────────────────────────────────
echo "[3/5] Registering node with DarkNodes manager..."
# Remove any stale credentials from a previous install so a fresh registration
# is always performed with the new DNODE token.
rm -f "$INSTALL_DIR/node_agent.json"
# --register-only: saves credentials then exits cleanly (no background process needed)
python3 "$INSTALL_DIR/node_agent.py" \\
  --dnode-token "$DNODE_TOKEN" \\
  --config "$INSTALL_DIR/node_agent.json" \\
  --register-only

if [ $? -ne 0 ]; then
  echo "ERROR: Registration failed."
  echo "Make sure the DarkNodes bot is running and the tunnel is online, then run /node add again."
  exit 1
fi

echo "  Node registered successfully!"

# ── Install systemd service ─────────────────────────────────────────────────
echo "[4/5] Installing systemd service..."
cat > "$SERVICE_FILE" <<'EOF_SVC'
[Unit]
Description=DarkNodes Node Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 INSTALL_DIR_PH/node_agent.py --config INSTALL_DIR_PH/node_agent.json
WorkingDirectory=INSTALL_DIR_PH
Restart=always
RestartSec=15
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF_SVC

# Substitute placeholder with actual install dir
sed -i "s|INSTALL_DIR_PH|$INSTALL_DIR|g" "$SERVICE_FILE"

systemctl daemon-reload
systemctl enable --now darknodes-agent

# ── Done ────────────────────────────────────────────────────────────────────
echo "[5/5] Done!"
echo ""
echo "========================================"
echo "  Node Agent installed and running!"
echo ""
echo "  Service: darknodes-agent"
echo "  Status:  systemctl status darknodes-agent"
echo "  Logs:    journalctl -u darknodes-agent -f"
echo ""
echo "  Credentials saved to $INSTALL_DIR/node_agent.json"
echo "  No Discord token stored — your bot token stays private."
echo "  The agent restarts automatically after reboots."
echo "========================================"
"""


async def _start_node_manager_http() -> None:
    """
    Start the Node Manager HTTP server.

    Static endpoints:
      GET  /healthz        — tunnel health check
      GET  /install.sh     — generated install script (requires ?token=DNODE_xxx)
      GET  /node_agent.py  — the node agent source file

    Agent API (used by remote node agents — no Discord token needed):
      POST /api/register   — one-time registration with DNODE token → {node_id, node_api_key}
      POST /api/heartbeat  — periodic stats update
      POST /api/jobs       — poll for pending jobs
      POST /api/result     — post job execution result
      POST /api/vsync      — sync VPS metadata
    """
    try:
        from aiohttp import web
    except ImportError:
        logger.warning("[nodes] aiohttp not available — Node Manager HTTP server not started")
        return

    # ── Static endpoints ──────────────────────────────────────────────────────

    async def handle_healthz(request: web.Request) -> web.Response:
        return web.Response(text="ok\n")

    async def handle_install_sh(request: web.Request) -> web.Response:
        dnode_token = request.rel_url.query.get("token", "")
        if not dnode_token:
            return web.Response(status=400, text="Missing ?token=DNODE_...\n")

        decoded = _decode_dnode_token(dnode_token)
        if not decoded:
            return web.Response(status=400, text="Invalid token format\n")

        reg_token = decoded.get("reg_token", "")
        if reg_token not in _tokens:
            return web.Response(status=410, text="Token not found or already used\n")

        url = _tunnel_url()
        if not url:
            return web.Response(status=503, text="Tunnel not configured\n")

        script = _generate_install_sh(dnode_token, url)
        return web.Response(
            text=script,
            content_type="text/x-shellscript",
            headers={"Content-Disposition": "inline; filename=install.sh"},
        )

    async def handle_node_agent(request: web.Request) -> web.Response:
        agent_path = os.path.join(_BASE, "node_agent.py")
        try:
            with open(agent_path) as fh:
                content = fh.read()
            return web.Response(
                text=content,
                content_type="text/x-python",
                headers={"Content-Disposition": "inline; filename=node_agent.py"},
            )
        except FileNotFoundError:
            return web.Response(status=404, text="node_agent.py not found\n")

    async def handle_image_export(request: web.Request) -> web.Response:
        """Serve the pre-exported VPS image tarball for zero-config distribution.

        Remote nodes download this via the Cloudflare Tunnel and pipe it into
        `docker load` — no registry credentials required.
        """
        if not _image_export_path or not os.path.exists(_image_export_path):
            return web.Response(
                status=404,
                text=(
                    "Image export not available yet.\n"
                    "Wait for the first local build to complete, then retry.\n"
                ),
            )
        try:
            return web.FileResponse(
                _image_export_path,
                headers={
                    "Content-Type": "application/gzip",
                    "Content-Disposition": "attachment; filename=darknodes-vps.tar.gz",
                },
            )
        except Exception as exc:
            return web.Response(status=500, text=f"Error serving image: {exc}\n")

    async def handle_image_hash(request: web.Request) -> web.Response:
        """Return the Dockerfile MD5 of the currently exported image.

        Remote nodes call this first (fast, ~1 KB) to decide whether they
        need to download the full tarball or already have the right version.
        """
        hash_path = (_image_export_path + ".hash") if _image_export_path else ""
        if not hash_path or not os.path.exists(hash_path):
            return web.Response(status=404, text="No image hash available\n")
        try:
            with open(hash_path) as fh:
                return web.Response(text=fh.read().strip() + "\n")
        except Exception as exc:
            return web.Response(status=500, text=f"Error reading hash: {exc}\n")

    # ── Agent API ─────────────────────────────────────────────────────────────

    async def handle_api_register(request: web.Request) -> web.Response:
        """One-time registration.  Body: {dnode_token, hostname, public_ip}"""
        try:
            data = await request.json()
        except Exception:
            return web.Response(status=400, text="Invalid JSON\n")

        dnode_token = data.get("dnode_token", "").strip()
        hostname    = data.get("hostname", "").strip() or socket.gethostname()
        public_ip   = data.get("public_ip", "").strip()

        decoded = _decode_dnode_token(dnode_token)
        if not decoded:
            return web.Response(status=400, text="Invalid DNODE token format\n")

        node_id   = decoded.get("node_id", "")
        reg_token = decoded.get("reg_token", "")

        tok = _tokens.get(reg_token)
        if not tok or tok.get("node_id") != node_id:
            return web.Response(status=410, text="Token not found or node_id mismatch\n")

        # Check expiry
        try:
            exp = datetime.fromisoformat(tok["expires_at"])
            if datetime.utcnow() > exp:
                _tokens.pop(reg_token, None)
                _save_tokens()
                return web.Response(status=410, text="Registration token expired\n")
        except Exception:
            pass

        if node_id not in nodes:
            return web.Response(status=410, text="Node record not found — was it removed?\n")

        # ── Snapshot old state for rollback ──────────────────────────────────
        _old_node        = dict(nodes[node_id])
        _old_token_entry = dict(tok)
        _node_updated    = False

        try:
            # Generate a node-specific API key (not the Discord bot token!)
            node_api_key = secrets.token_urlsafe(32)

            nodes[node_id].update({
                "type":         "remote",
                "hostname":     hostname,
                "public_ip":    public_ip,
                "last_seen":    _now_iso(),
                "stats":        {},
                "node_api_key": node_api_key,
            })
            _node_updated = True
            _save_nodes()

            _tokens.pop(reg_token, None)
            _save_tokens()
            _node_was_online[node_id] = True

            # Rebuild channel index (channel still exists for admin visibility)
            cid = nodes[node_id].get("channel_id", "")
            if cid:
                _node_by_channel[cid] = node_id

        except Exception as exc:
            # ── Rollback — restore node and token so the agent can retry ────
            logger.error(
                f"[nodes] HTTP registration failed for {node_id} — rolling back. Error: {exc}"
            )
            nodes[node_id] = _old_node
            try:
                _save_nodes()
            except Exception:
                pass
            if reg_token not in _tokens:
                _tokens[reg_token] = _old_token_entry
                try:
                    _save_tokens()
                except Exception:
                    pass
            return web.Response(status=500, text=f"Registration failed internally — please retry.\n")

        # ── Verify the Discord channel exists; auto-heal if missing ──────────
        # The channel was created by /node add.  If it was deleted between then
        # and now, recreate it so admin visibility is maintained.  This is done
        # AFTER committing the node record so a heal failure doesn't prevent
        # the agent from operating (HTTP transport doesn't need the channel).
        cid = nodes[node_id].get("channel_id", "")
        if cid and _bot:
            ch = await _resolve_channel(node_id)
            if ch is None:
                logger.warning(
                    f"[nodes] HTTP registration: channel {cid} is missing for node "
                    f"{node_id} — auto-healing in background"
                )
                asyncio.create_task(_heal_node_channel(node_id))

        logger.info(
            f"[nodes] HTTP registration: node_id={node_id} "
            f"hostname={hostname!r} ip={public_ip or 'unknown'}"
        )

        if _bot:
            e = _embed("Node Connected", 0x57F287)
            e.description = (
                f"🟢  **{hostname}** has come online\n"
                f"> ID `{node_id}` · IP `{public_ip or 'unknown'}`"
            )
            asyncio.create_task(_notify_admin(e))

        return web.json_response({"node_id": node_id, "node_api_key": node_api_key})

    async def handle_api_heartbeat(request: web.Request) -> web.Response:
        """Body: {node_id, node_api_key, stats: {...}}"""
        try:
            data = await request.json()
        except Exception:
            return web.Response(status=400, text="Invalid JSON\n")
        node_id = _auth_node(data)
        if not node_id:
            return web.Response(status=401, text="Unauthorized\n")
        _handle_agent_heartbeat(node_id, data.get("stats", {}))
        return web.json_response({"ok": True})

    async def handle_api_jobs(request: web.Request) -> web.Response:
        """Body: {node_id, node_api_key} → {jobs: [{job_id, command, timeout, created_at}]}"""
        try:
            data = await request.json()
        except Exception:
            return web.Response(status=400, text="Invalid JSON\n")
        node_id = _auth_node(data)
        if not node_id:
            return web.Response(status=401, text="Unauthorized\n")
        jobs = [
            {
                "job_id":     jid,
                "command":    j["command"],
                "timeout":    j["timeout"],
                "created_at": j["created_at"],
            }
            for jid, j in list(_pending_jobs.items())
            if j.get("node_id") == node_id
        ]
        return web.json_response({"jobs": jobs})

    async def handle_api_result(request: web.Request) -> web.Response:
        """Body: {node_id, node_api_key, job_id, success, output}"""
        try:
            data = await request.json()
        except Exception:
            return web.Response(status=400, text="Invalid JSON\n")
        node_id = _auth_node(data)
        if not node_id:
            return web.Response(status=401, text="Unauthorized\n")
        _handle_agent_result(data)
        return web.json_response({"ok": True})

    async def handle_api_vsync(request: web.Request) -> web.Response:
        """Body: {node_id, node_api_key, vps_list: [...]}"""
        try:
            data = await request.json()
        except Exception:
            return web.Response(status=400, text="Invalid JSON\n")
        node_id = _auth_node(data)
        if not node_id:
            return web.Response(status=401, text="Unauthorized\n")
        asyncio.create_task(_handle_agent_vsync(node_id, data))
        return web.json_response({"ok": True})

    async def handle_api_reconnect(request: web.Request) -> web.Response:
        """
        Body: {token_code}
        Returns: {node_id, node_api_key, manager_url, hostname}

        Validates a short-lived RNODE reconnect token and restores the node's
        existing API key (or issues a fresh one if it was cleared).
        Does NOT create a new node — the node must already exist.
        """
        try:
            data = await request.json()
        except Exception:
            return web.Response(status=400, text="Invalid JSON\n")

        token_code = data.get("token_code", "").strip()
        tok = _reconnect_tokens.get(token_code)
        if not tok:
            return web.Response(status=410, text="Reconnect token not found or already used\n")

        # Check expiry
        try:
            exp = datetime.fromisoformat(tok["expires_at"])
            if datetime.utcnow() > exp:
                _reconnect_tokens.pop(token_code, None)
                _save_reconnect_tokens()
                return web.Response(status=410, text="Reconnect token expired\n")
        except Exception:
            pass

        node_id = tok["node_id"]
        node    = nodes.get(node_id)
        if not node:
            _reconnect_tokens.pop(token_code, None)
            _save_reconnect_tokens()
            return web.Response(status=410, text="Node record not found — was it removed?\n")

        # Reuse existing API key or issue a fresh one
        node_api_key = node.get("node_api_key") or secrets.token_urlsafe(32)
        node["node_api_key"] = node_api_key
        node["last_seen"]    = _now_iso()
        _save_nodes()

        # Consume the token (single-use)
        _reconnect_tokens.pop(token_code, None)
        _save_reconnect_tokens()

        logger.info(f"[nodes] Reconnect token used: node_id={node_id} hostname={node.get('hostname','?')!r}")

        if _bot:
            e = _embed("Node Reconnected", 0x5865F2)
            e.description = (
                f"🔄  **{node.get('name', node_id)}** reconnected manually\n"
                f"> ID `{node_id}` · Host `{node.get('hostname', '?')}`"
            )
            asyncio.create_task(_notify_admin(e))

        return web.json_response({
            "node_id":      node_id,
            "node_api_key": node_api_key,
            "manager_url":  tok.get("manager_url", ""),
            "hostname":     node.get("hostname", ""),
        })

    # ── Register routes ───────────────────────────────────────────────────────

    app = web.Application()
    app.router.add_get("/healthz",               handle_healthz)
    app.router.add_get("/install.sh",            handle_install_sh)
    app.router.add_get("/node_agent.py",         handle_node_agent)
    app.router.add_get("/image/darknodes-vps",   handle_image_export)
    app.router.add_get("/image/darknodes-vps.hash", handle_image_hash)
    app.router.add_post("/api/register",         handle_api_register)
    app.router.add_post("/api/reconnect",        handle_api_reconnect)
    app.router.add_post("/api/heartbeat",        handle_api_heartbeat)
    app.router.add_post("/api/jobs",             handle_api_jobs)
    app.router.add_post("/api/result",           handle_api_result)
    app.router.add_post("/api/vsync",            handle_api_vsync)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", NODE_MANAGER_PORT)
    try:
        await site.start()
        logger.info(f"[nodes] Node Manager HTTP server listening on 127.0.0.1:{NODE_MANAGER_PORT}")
    except OSError as exc:
        logger.warning(f"[nodes] Could not start Node Manager HTTP server on port {NODE_MANAGER_PORT}: {exc}")


# ══════════════════════════════════════════════════════════════════════════════
# Tunnel setup guide + modal
# ══════════════════════════════════════════════════════════════════════════════

def _tunnel_guide_embed() -> discord.Embed:
    """
    Build the step-by-step Cloudflare Tunnel setup guide embed.
    Shown before the modal so the admin knows exactly what to do.
    """
    e = _embed("☁️  Cloudflare Tunnel — Setup Guide", 0xF6821F)   # Cloudflare orange
    e.description = (
        "Follow these steps in the **Cloudflare dashboard** before clicking "
        "**\"I'm Ready\"** below.\n"
        "This is a **one-time setup** — once done, every new node is added with a single command."
    )

    e.add_field(
        name="📋  What you need",
        value=(
            "• A **Cloudflare account** (free tier works) → [dash.cloudflare.com](https://dash.cloudflare.com)\n"
            "• A **domain** added to Cloudflare (e.g. `example.com`)\n"
            "  *(No domain? Use a free [Cloudflare Pages](https://pages.cloudflare.com) subdomain "
            "or any registrar — just point its nameservers to Cloudflare)*"
        ),
        inline=False,
    )

    e.add_field(
        name="1️⃣  Open Zero Trust",
        value=(
            "Go to **[one.dash.cloudflare.com](https://one.dash.cloudflare.com)**\n"
            "In the left sidebar: **Networks → Tunnels**"
        ),
        inline=False,
    )

    e.add_field(
        name="2️⃣  Create a new tunnel",
        value=(
            "Click **\"Create a tunnel\"**\n"
            "• Select **Cloudflared** (not WARP Connector)\n"
            "• Click **Next**\n"
            "• Name it anything — e.g. **`darknodes`**\n"
            "• Click **Save tunnel**"
        ),
        inline=False,
    )

    e.add_field(
        name="3️⃣  Copy the tunnel token",
        value=(
            "After saving, Cloudflare shows an **Install connector** page.\n"
            "Select **Linux** → you will see a command like:\n"
            "```\nsudo cloudflared service install eyJhIjoiN...\n```\n"
            "Copy **only the token** — the long string at the very end.\n"
            "Do **not** copy `sudo cloudflared service install` — just the `eyJh...` part.\n\n"
            "✅ Correct: `eyJhIjoiNTVkOTVlMzRkNGM4NGYzNWJkOTkxNDI0YWQzZGMyNGEi…`\n"
            "❌ Wrong:   `sudo cloudflared service install eyJh…`"
        ),
        inline=False,
    )

    e.add_field(
        name="4️⃣  Add a Public Hostname",
        value=(
            "Click **Next** (or go to the **Public Hostname** tab of your tunnel)\n"
            "Click **Add a public hostname** and fill in:\n\n"
            "| Field | Value |\n"
            "|-------|-------|\n"
            "| **Subdomain** | `nodes` *(or any name you want)* |\n"
            "| **Domain** | `example.com` *(your domain)* |\n"
            "| **Type** | `HTTP` *(this is the internal protocol — leave it as HTTP)* |\n"
            "| **URL** | `localhost:8765` *(the local port the bot runs on)* |\n\n"
            "Click **Save hostname**.\n\n"
            "⚠️ **HTTP vs HTTPS:** Setting Type to `HTTP` means the tunnel talks to "
            "your bot on `localhost:8765` internally. "
            "Cloudflare **automatically provides HTTPS** on the public side — "
            "your public URL will always start with `https://`."
        ),
        inline=False,
    )

    e.add_field(
        name="5️⃣  Done in Cloudflare — come back here",
        value=(
            "Once the hostname is saved, click **\"I'm Ready — Enter My Token\"** below.\n"
            "You will be asked for two things:\n\n"
            "**Field 1 — Tunnel Token:** the `eyJh…` string from step 3\n\n"
            "**Field 2 — Public URL:** the `https://` address Cloudflare assigned to "
            "this hostname — e.g. `https://nodes.example.com`\n"
            "*(Always `https://` even though you chose HTTP internally in step 4)*"
        ),
        inline=False,
    )

    return e


class _TunnelGuideView(discord.ui.View):
    """Shown with the setup guide.  'I'm Ready' opens the credential modal."""

    def __init__(self):
        super().__init__(timeout=600)   # 10-minute window to complete dashboard steps
        self._ready      = False
        self._interaction: Optional[discord.Interaction] = None
        self._event      = asyncio.Event()

    @discord.ui.button(
        label="I'm Ready — Enter My Token",
        style=discord.ButtonStyle.success,
        emoji="✅",
    )
    async def ready(self, interaction: discord.Interaction, button: discord.ui.Button):
        self._ready       = True
        self._interaction = interaction
        self._event.set()
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="✖")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self._ready = False
        await interaction.response.defer()
        self._event.set()
        self.stop()

    async def on_timeout(self):
        self._event.set()


class _TunnelSetupModal(discord.ui.Modal, title="Cloudflare Tunnel — Enter Credentials"):
    tunnel_token = discord.ui.TextInput(
        label="Tunnel Token  (from step 3 of the guide)",
        placeholder="eyJhIjoiN…  (the long string after --token in the Cloudflare install command)",
        required=True,
        max_length=1000,
        style=discord.TextStyle.long,
    )
    tunnel_hostname = discord.ui.TextInput(
        label="Public URL  (the hostname you set in step 4)",
        placeholder="https://nodes.example.com",
        required=True,
        max_length=200,
    )

    def __init__(self):
        super().__init__()
        self._done  = asyncio.Event()
        self._token = ""
        self._host  = ""
        self._error = ""

    async def on_submit(self, interaction: discord.Interaction):
        self._token = self.tunnel_token.value.strip()
        self._host  = self.tunnel_hostname.value.strip()
        if not self._token:
            self._error = "Tunnel token is required."
            await interaction.response.send_message(
                embed=_err("Missing Token", self._error), ephemeral=True
            )
        elif not self._host.startswith(("http://", "https://")):
            self._error = "Public URL must start with https:// — e.g. https://nodes.example.com"
            await interaction.response.send_message(
                embed=_err("Invalid URL", self._error), ephemeral=True
            )
        else:
            await interaction.response.defer(ephemeral=True)
        self._done.set()

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        self._error = str(error)
        self._done.set()


# ══════════════════════════════════════════════════════════════════════════════
# Slash commands   /node  group
# ══════════════════════════════════════════════════════════════════════════════

def register_commands(bot_instance) -> None:
    """Register /node slash commands, store bot reference, add on_message listener."""
    global _bot
    _bot = bot_instance

    # Register the on_message listener so we receive agent messages
    bot_instance.add_listener(_on_node_message, "on_message")

    node_group = app_commands.Group(
        name="node",
        description="Manage DarkNodes deployment nodes",
    )

    # ── /node tunnel ──────────────────────────────────────────────────────────
    @node_group.command(
        name="tunnel",
        description="Set up or check Cloudflare Tunnel for the Node Manager (Admin only)",
    )
    async def node_tunnel(interaction: discord.Interaction):
        if not _is_admin(interaction):
            await interaction.response.send_message(
                embed=_err("Access Denied", "Only admins can configure the tunnel."), ephemeral=True
            )
            return

        tunnel_cfg = _load_tunnel()

        # ── Already configured: show status + reconfigure option ─────────────
        if tunnel_cfg.get("configured") and tunnel_cfg.get("tunnel_url"):
            url    = tunnel_cfg["tunnel_url"]
            set_at = tunnel_cfg.get("configured_at", "unknown")[:19]

            # Defer immediately so Discord doesn't time out while we check the tunnel
            await interaction.response.defer(ephemeral=True)

            # Live status check and service state in parallel
            (reachable, reach_msg), (_, svc_out) = await asyncio.gather(
                _verify_tunnel(url),
                _shell("systemctl is-active cloudflared 2>/dev/null || echo inactive"),
            )
            svc_status = svc_out.strip()

            e = _embed("☁️  Cloudflare Tunnel Status", 0x57F287 if reachable else 0xFEE75C)
            e.description = (
                "🟢 **Tunnel is online and reachable.**" if reachable
                else "⚠️ **Tunnel is configured but not yet reachable.** It may still be starting."
            )
            e.add_field(name="🔗 Public URL",     value=f"`{url}`",                              inline=False)
            e.add_field(name="📅 Configured",     value=f"`{set_at} UTC`",                       inline=True)
            e.add_field(name="⚙️ cloudflared",    value=f"`{svc_status}`",                       inline=True)
            e.add_field(name="🌐 Reachable",      value="✅ Yes" if reachable else "❌ Not yet",  inline=True)
            e.add_field(name="🩺 Health Check",   value=reach_msg,                               inline=False)
            e.add_field(
                name="🔄  Need to reconfigure?",
                value=(
                    "Click **Reconfigure** to run the setup again with a new tunnel token or hostname."
                ),
                inline=False,
            )

            view = _TunnelReconfigureView()
            await interaction.followup.send(embed=e, view=view, ephemeral=True)
            await view._event.wait()
            if not view._reconfigure:
                return

            # User clicked Reconfigure — show the guide via a followup, then
            # instruct them to run /node tunnel again so we get a fresh interaction
            # (Discord modals require a first-response interaction, not a followup)
            guide = _tunnel_guide_embed()
            guide.title = "☁️  Reconfigure — Follow the Guide, Then Run /node tunnel Again"
            guide.description = (
                "Review the steps below, make any changes in your Cloudflare dashboard "
                "(e.g. update the hostname or create a new tunnel), then run "
                "**`/node tunnel`** again to enter your new credentials."
            )
            await interaction.followup.send(embed=guide, ephemeral=True)
            return

        # ── Not yet configured: show the step-by-step guide ──────────────────
        guide = _tunnel_guide_embed()
        guide_view = _TunnelGuideView()
        await interaction.response.send_message(embed=guide, view=guide_view, ephemeral=True)
        await guide_view._event.wait()

        if not guide_view._ready or guide_view._interaction is None:
            return   # user cancelled or timed out

        # Open the credential modal from the button's interaction
        modal        = _TunnelSetupModal()
        btn_interact = guide_view._interaction
        await btn_interact.response.send_modal(modal)
        await modal._done.wait()

        if modal._error or not modal._token or not modal._host:
            # Error message already sent by modal.on_submit
            return

        tunnel_token = modal._token
        tunnel_url   = modal._host.rstrip("/")

        # ── Step 1: Install cloudflared ───────────────────────────────────────
        progress = _embed("☁️  Cloudflare Tunnel Setup", 0x5865F2)
        progress.description = "**Step 1/4** — Installing cloudflared…"
        await interaction.followup.send(embed=progress, ephemeral=True)

        inst_ok, inst_msg = await _install_cloudflared()
        if not inst_ok:
            await interaction.followup.send(
                embed=_err("Installation Failed", inst_msg), ephemeral=True
            )
            return

        progress2 = _embed("☁️  Cloudflare Tunnel Setup", 0x5865F2)
        progress2.description = (
            f"✅ cloudflared ready: {inst_msg}\n\n"
            f"**Step 2/4** — Configuring systemd service…"
        )
        await interaction.followup.send(embed=progress2, ephemeral=True)

        # ── Step 2: Configure cloudflared as a system service ─────────────────
        svc_ok, svc_msg = await _configure_cloudflared_service(tunnel_token)
        if not svc_ok:
            await interaction.followup.send(
                embed=_err("Service Configuration Failed", svc_msg), ephemeral=True
            )
            return

        progress3 = _embed("☁️  Cloudflare Tunnel Setup", 0x5865F2)
        progress3.description = (
            f"✅ cloudflared ready\n"
            f"✅ Service installed\n\n"
            f"**Step 3/4** — Saving configuration…"
        )
        await interaction.followup.send(embed=progress3, ephemeral=True)

        # ── Step 3: Save tunnel config ────────────────────────────────────────
        _save_tunnel({
            "configured":    True,
            "tunnel_url":    tunnel_url,
            "tunnel_token":  tunnel_token,   # stored for service re-installs
            "configured_at": _now_iso(),
        })
        logger.info(f"[nodes] Cloudflare Tunnel configured: {tunnel_url}")

        progress4 = _embed("☁️  Cloudflare Tunnel Setup", 0x5865F2)
        progress4.description = (
            f"✅ cloudflared ready\n"
            f"✅ Service installed\n"
            f"✅ Configuration saved\n\n"
            f"**Step 4/4** — Verifying tunnel is online…"
        )
        await interaction.followup.send(embed=progress4, ephemeral=True)

        # ── Step 4: Verify tunnel is reachable ────────────────────────────────
        # Give the tunnel 15 s to come up before checking
        await asyncio.sleep(15)
        reachable, reach_msg = await _verify_tunnel(tunnel_url)

        final = _embed(
            "☁️  Cloudflare Tunnel Ready" if reachable else "☁️  Tunnel Configured (Not Yet Verified)",
            0x57F287 if reachable else 0xFEE75C,
        )
        final.description = (
            "Cloudflare Tunnel has been configured successfully!\n\n"
            "Administrators can now use `/node add` to register remote nodes."
        )
        final.add_field(name="Public URL",      value=f"`{tunnel_url}`",                inline=False)
        final.add_field(name="Install Script",  value=f"`{tunnel_url}/install.sh?token=<DNODE>`", inline=False)
        final.add_field(name="Health Check",    value=reach_msg,                         inline=False)
        if not reachable:
            final.add_field(
                name="⚠️  If the tunnel is not online yet",
                value=(
                    "Cloudflare Tunnels can take 30–60 seconds to establish.\n"
                    "Run `/node tunnel` again in a minute to check the status."
                ),
                inline=False,
            )

        await interaction.followup.send(embed=final, ephemeral=True)

    # ── /node add ─────────────────────────────────────────────────────────────
    @node_group.command(
        name="add",
        description="Generate a setup command to register a new remote node (Admin only)",
    )
    async def node_add(interaction: discord.Interaction):
        if not _is_admin(interaction):
            await interaction.response.send_message(
                embed=_err("Access Denied", "Only admins can add nodes."), ephemeral=True
            )
            return

        if not interaction.guild:
            await interaction.response.send_message(
                embed=_err("Server Only", "This command must be run inside a server, not a DM."),
                ephemeral=True,
            )
            return

        # ── Require Cloudflare Tunnel to be configured ────────────────────────
        if not _tunnel_configured():
            e = _embed("☁️  Cloudflare Tunnel Required", 0xFEE75C)
            e.description = (
                "Before adding remote nodes, you need to configure a **Cloudflare Tunnel** "
                "so the Node Manager can be reached by remote machines.\n\n"
                "This is a one-time setup — after it's done, every new node can be added "
                "with a single curl command."
            )
            e.add_field(
                name="👉  Set up the tunnel first",
                value="```\n/node tunnel\n```",
                inline=False,
            )
            e.add_field(
                name="What you'll need",
                value=(
                    "• A Cloudflare account (free tier is fine)\n"
                    "• A domain managed by Cloudflare\n"
                    "• A tunnel token from the [Zero Trust dashboard](https://one.dash.cloudflare.com/)"
                ),
                inline=False,
            )
            await interaction.response.send_message(embed=e, ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        tunnel_url_str = _tunnel_url()

        # Generate node_id and one-time token
        node_id    = secrets.token_hex(6)
        token_code = secrets.token_urlsafe(24)
        expires_at = (datetime.utcnow() + timedelta(minutes=TOKEN_EXPIRY_MIN)).isoformat()

        # Create the private Discord channel for this node
        try:
            channel = await _create_node_channel(
                interaction.guild, node_id,
                invoker=interaction.user,
            )
        except discord.Forbidden:
            await interaction.followup.send(
                embed=_err(
                    "Missing Permissions",
                    "The bot needs **Manage Channels** permission to create node channels.",
                ),
                ephemeral=True,
            )
            return
        except Exception as exc:
            await interaction.followup.send(
                embed=_err("Channel Creation Failed", f"```{exc}```"), ephemeral=True
            )
            return

        channel_id = str(channel.id)

        # Create a PENDING node record (activated when agent registers)
        nodes[node_id] = {
            "id":         node_id,
            "name":       f"Pending-{node_id}",
            "type":       "pending",
            "hostname":   "",
            "public_ip":  "",
            "channel_id": channel_id,
            "guild_id":   str(interaction.guild.id),
            "created_at": _now_iso(),
            "last_seen":  "",
            "stats":      {},
        }
        _save_nodes()

        # Save the registration token
        _tokens[token_code] = {
            "node_id":     node_id,
            "channel_id":  channel_id,
            "manager_url": tunnel_url_str,
            "expires_at":  expires_at,
        }
        _save_tokens()

        # Build the DNODE composite token (encodes node_id + reg_token + manager_url)
        dnode_token = _encode_dnode_token(node_id, token_code, tunnel_url_str)

        # Install script URL
        install_url  = f"{tunnel_url_str}/install.sh?token={dnode_token}"
        curl_cmd     = f"curl -fsSL '{install_url}' | sudo bash"

        exp_ts = int((datetime.utcnow() + timedelta(minutes=TOKEN_EXPIRY_MIN)).timestamp())

        embed = _embed("New Node", 0x57F287)
        embed.description = (
            f"Run the command below on your remote machine to register it as a node.\n"
            f"> ID `{node_id}` · expires <t:{exp_ts}:R>"
        )

        embed.add_field(
            name="One-liner install (recommended)",
            value=f"```bash\n{curl_cmd}\n```",
            inline=False,
        )
        embed.add_field(
            name="Manual",
            value=(
                f"```bash\n"
                f"curl -fsSL '{tunnel_url_str}/node_agent.py' -o node_agent.py\n"
                f"python3 node_agent.py --dnode-token \"{dnode_token}\"\n"
                f"```"
            ),
            inline=False,
        )
        embed.add_field(
            name="DNODE Token",
            value=f"```\n{dnode_token}\n```",
            inline=False,
        )

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /node remove ──────────────────────────────────────────────────────────
    @node_group.command(
        name="remove",
        description="Remove a registered node and delete its channel (Admin only)",
    )
    @app_commands.describe(node_id="Node ID (start typing to see suggestions)")
    @app_commands.autocomplete(node_id=_node_id_autocomplete)
    async def node_remove(interaction: discord.Interaction, node_id: str):
        if not _is_admin(interaction):
            await interaction.response.send_message(
                embed=_err("Access Denied", "Only admins can remove nodes."), ephemeral=True
            )
            return
        if node_id == LOCAL_NODE_ID:
            await interaction.response.send_message(
                embed=_err("Cannot Remove", "The local node cannot be removed."), ephemeral=True
            )
            return
        node = nodes.pop(node_id, None)
        if not node:
            await interaction.response.send_message(
                embed=_err("Node Not Found", f"No node with ID `{node_id}`."), ephemeral=True
            )
            return

        cid = node.get("channel_id", "")
        _node_by_channel.pop(cid, None)
        _save_nodes()

        # Delete the Discord channel in the background
        asyncio.create_task(_delete_node_channel(node))

        e = _embed("Node Removed", 0xED4245)
        e.description = (
            f"**{node.get('name', node_id)}** has been unregistered and its channel deleted.\n"
            f"> ID `{node_id}`"
        )
        await interaction.response.send_message(embed=e)

    # ── /node rename ──────────────────────────────────────────────────────────
    @node_group.command(
        name="rename",
        description="Give a node a custom display name (Admin only)",
    )
    @app_commands.describe(
        node_id="Node ID (start typing to see suggestions)",
        name="New display name (leave blank to reset to hostname)",
    )
    @app_commands.autocomplete(node_id=_node_id_autocomplete)
    async def node_rename(interaction: discord.Interaction, node_id: str, name: str = ""):
        if not _is_admin(interaction):
            await interaction.response.send_message(
                embed=_err("Access Denied", "Only admins can rename nodes."), ephemeral=True
            )
            return
        node = nodes.get(node_id)
        if not node:
            await interaction.response.send_message(
                embed=_err("Node Not Found", f"No node with ID `{node_id}`."), ephemeral=True
            )
            return
        new_name = name.strip()[:64] or node.get("hostname", node_id) or node_id
        old      = node.get("name", node_id)
        node["name"] = new_name
        _save_nodes()
        e = _embed("Node Renamed", 0x57F287)
        e.description = f"`{old}` → **{new_name}**\n> ID `{node_id}`"
        await interaction.response.send_message(embed=e)

    # ── /node list ────────────────────────────────────────────────────────────
    @node_group.command(
        name="list",
        description="Show all registered nodes and their current status",
    )
    async def node_list(interaction: discord.Interaction):
        await interaction.response.defer()
        if not nodes:
            await interaction.followup.send(
                embed=_err("No Nodes", "No nodes registered yet. Use `/node add` to add one.")
            )
            return

        total_nodes  = len(nodes)
        online_count = sum(1 for n in nodes.values() if _node_online(n))
        embed = _embed("Node Fleet", 0x5865F2)
        _online_icon  = "🟢"
        _offline_icon = "🔴"
        embed.description = (
            f"**{total_nodes}** registered  ·  "
            f"{_online_icon} **{online_count}** online  ·  "
            f"{_offline_icon} **{total_nodes - online_count}** offline"
        )

        for nid, n in nodes.items():
            ntype = n.get("type", "remote")
            if nid == LOCAL_NODE_ID:
                tag = "local"
            elif ntype == "pending":
                tag = "pending"
            else:
                tag = "remote"

            s      = n.get("stats", {})
            cpu    = s.get("cpu", 0)
            u, t   = s.get("ram_used_mb", 0), s.get("ram_total_mb", 0)
            du, dt = s.get("disk_used_gb", 0.0), s.get("disk_total_gb", 0.0)
            vps_c  = s.get("running_vps", 0)
            dot    = _status_dot(n)
            name   = n.get("name") or nid
            ip_str = f"`{n['public_ip']}`" if n.get("public_ip") else "—"

            if s:
                ram_pct  = int(u / t * 100) if t else 0
                disk_pct = int(du / dt * 100) if dt else 0
                res_line = (
                    f"CPU **{int(cpu)}%** `{_rbar(cpu,100,6)}`  "
                    f"RAM **{ram_pct}%** `{_rbar(u,t,6)}`  "
                    f"Disk **{disk_pct}%** `{_rbar(du,dt,6)}`  "
                    f"· {vps_c} VPS"
                )
            else:
                res_line = "*No stats yet*"

            embed.add_field(
                name=f"{dot} {name}",
                value=(
                    f"`{nid}` · {tag} · {ip_str}\n"
                    f"{_connection_status_str(n)}\n"
                    f"{res_line}"
                ),
                inline=False,
            )
        await interaction.followup.send(embed=embed)

    # ── /node info ────────────────────────────────────────────────────────────
    @node_group.command(
        name="info",
        description="Show detailed information for a specific node",
    )
    @app_commands.describe(node_id="Node ID (start typing to see suggestions)")
    @app_commands.autocomplete(node_id=_node_id_autocomplete)
    async def node_info(interaction: discord.Interaction, node_id: str):
        await interaction.response.defer()
        n = nodes.get(node_id)
        if not n:
            await interaction.followup.send(
                embed=_err("Not Found", f"Node `{node_id}` does not exist.")
            )
            return

        online    = _node_online(n)
        s         = n.get("stats", {})
        ntype     = n.get("type", "remote")
        has_stats = bool(s)
        color     = 0x57F287 if online else (0xED4245 if has_stats else 0x747F8D)

        if node_id == LOCAL_NODE_ID:
            tag = "🏠 Local"
        elif ntype == "pending":
            tag = "⏳ Pending"
        else:
            tag = "🌐 Remote"

        display_name = n.get("name") or n.get("hostname") or node_id
        e = _embed(f"{_status_dot(n)}  {display_name}", color)

        # Connection status + key facts as description
        cid = n.get("channel_id", "")
        ch_mention = "—"
        if cid and _bot:
            try:
                ch = _bot.get_channel(int(cid))
                if ch:
                    ch_mention = ch.mention
            except (ValueError, TypeError):
                pass

        e.description = (
            f"{_connection_status_str(n)}\n"
            f"**Type** {tag}  ·  **Channel** {ch_mention}"
        )

        # ── Identity row ────────────────────────────────────────────────────
        e.add_field(name="ID",         value=f"`{node_id}`",                       inline=True)
        e.add_field(name="Hostname",   value=f"`{n.get('hostname') or '—'}`",      inline=True)
        e.add_field(name="Public IP",  value=f"`{n.get('public_ip') or '—'}`",     inline=True)

        # ── Timing row ──────────────────────────────────────────────────────
        e.add_field(name="Registered", value=f"`{n.get('created_at', '—')[:10]}`", inline=True)
        e.add_field(name="Last Seen",  value=_last_seen_str(n),                    inline=True)
        e.add_field(name="\u200b",     value="\u200b",                              inline=True)

        # ── Resources ────────────────────────────────────────────────────────
        if s:
            cpu    = s.get("cpu", 0)
            u, t   = s.get("ram_used_mb", 0), s.get("ram_total_mb", 0)
            du, dt = s.get("disk_used_gb", 0.0), s.get("disk_total_gb", 0.0)
            vps    = s.get("running_vps", 0)
            cpu_pct  = int(cpu) if cpu else 0
            ram_pct  = int(u / t * 100) if t else 0
            disk_pct = int(du / dt * 100) if dt else 0
            e.add_field(
                name="Resources",
                value=(
                    f"CPU  `{_rbar(cpu, 100, 10)}` **{cpu_pct}%**\n"
                    f"RAM  `{_rbar(u, t, 10)}` **{ram_pct}%**  ·  {_fmt_ram(u, t)}\n"
                    f"Disk `{_rbar(du, dt, 10)}` **{disk_pct}%**  ·  {du:.1f}/{dt:.1f} GB\n"
                    f"VPS  **{vps}** running"
                ),
                inline=False,
            )
        else:
            e.add_field(
                name="Resources",
                value="*No stats — agent not yet connected.*",
                inline=False,
            )

        await interaction.followup.send(embed=e)

    # ── /node reconnect ───────────────────────────────────────────────────────
    @node_group.command(
        name="reconnect",
        description="Generate a short-lived reconnect token for manual node recovery (Admin only)",
    )
    @app_commands.describe(node_id="Node ID (start typing to see suggestions)")
    @app_commands.autocomplete(node_id=_node_id_autocomplete)
    async def node_reconnect(interaction: discord.Interaction, node_id: str):
        if not _is_admin(interaction):
            await interaction.response.send_message(
                embed=_err("Access Denied", "Only admins can use this command."), ephemeral=True
            )
            return

        if node_id == LOCAL_NODE_ID:
            await interaction.response.send_message(
                embed=_err("Not Applicable", "The local node does not need manual reconnection."),
                ephemeral=True,
            )
            return

        node = nodes.get(node_id)
        if not node:
            await interaction.response.send_message(
                embed=_err("Node Not Found", f"No node with ID `{node_id}`."), ephemeral=True
            )
            return

        if node.get("type") == "pending":
            await interaction.response.send_message(
                embed=_err(
                    "Node Not Yet Registered",
                    f"Node `{node_id}` has never completed registration.\n\n"
                    "Use `/node regenerate-token` to get a fresh registration token instead.",
                ),
                ephemeral=True,
            )
            return

        if not _tunnel_configured():
            await interaction.response.send_message(
                embed=_err(
                    "Tunnel Not Configured",
                    "A Cloudflare Tunnel is required for the agent to reach the Node Manager.\n"
                    "Run `/node tunnel` to configure it first.",
                ),
                ephemeral=True,
            )
            return

        tunnel_url_str = _tunnel_url()
        token_code     = secrets.token_urlsafe(24)
        expires_at     = (datetime.utcnow() + timedelta(minutes=RECONNECT_TOKEN_EXPIRY_MIN)).isoformat()

        _reconnect_tokens[token_code] = {
            "node_id":     node_id,
            "manager_url": tunnel_url_str,
            "expires_at":  expires_at,
        }
        _save_reconnect_tokens()

        rnode_token = _encode_rnode_token(token_code, tunnel_url_str)
        exp_ts      = int((datetime.utcnow() + timedelta(minutes=RECONNECT_TOKEN_EXPIRY_MIN)).timestamp())
        install_dir = "/opt/darknodes"

        reconnect_cmd = f'python3 {install_dir}/node_agent.py --reconnect-token "{rnode_token}"'
        service_cmd   = (
            f"systemctl stop darknodes-agent\n"
            f"{reconnect_cmd}\n"
            f"systemctl start darknodes-agent"
        )

        e = _embed("Node Reconnect Token", 0xFEE75C)
        e.description = (
            f"Run on **{node.get('name', node_id)}** to restore the connection.\n"
            f"> ID `{node_id}` · single-use · expires <t:{exp_ts}:R>"
        )
        e.add_field(
            name="With systemd (recommended)",
            value=f"```bash\n{service_cmd}\n```",
            inline=False,
        )
        e.add_field(
            name="Without systemd",
            value=f"```bash\n{reconnect_cmd}\n```",
            inline=False,
        )
        e.add_field(name="RNODE Token", value=f"```\n{rnode_token}\n```", inline=False)
        await interaction.response.send_message(embed=e, ephemeral=True)

    # ── /node regenerate-token ─────────────────────────────────────────────────
    @node_group.command(
        name="regenerate-token",
        description="Generate a new registration token when credentials are lost (Admin only)",
    )
    @app_commands.describe(node_id="Node ID (start typing to see suggestions)")
    @app_commands.autocomplete(node_id=_node_id_autocomplete)
    async def node_regenerate_token(interaction: discord.Interaction, node_id: str):
        if not _is_admin(interaction):
            await interaction.response.send_message(
                embed=_err("Access Denied", "Only admins can use this command."), ephemeral=True
            )
            return

        if node_id == LOCAL_NODE_ID:
            await interaction.response.send_message(
                embed=_err("Not Applicable", "The local node does not use registration tokens."),
                ephemeral=True,
            )
            return

        node = nodes.get(node_id)
        if not node:
            await interaction.response.send_message(
                embed=_err("Node Not Found", f"No node with ID `{node_id}`."), ephemeral=True
            )
            return

        if not _tunnel_configured():
            await interaction.response.send_message(
                embed=_err(
                    "Tunnel Not Configured",
                    "A Cloudflare Tunnel is required. Run `/node tunnel` to configure it first.",
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        tunnel_url_str = _tunnel_url()

        # Invalidate all existing pending registration tokens for this node
        stale = [tc for tc, td in list(_tokens.items()) if td.get("node_id") == node_id]
        for tc in stale:
            _tokens.pop(tc, None)
        if stale:
            _save_tokens()

        # Generate a fresh registration token
        token_code = secrets.token_urlsafe(24)
        expires_at = (datetime.utcnow() + timedelta(minutes=TOKEN_EXPIRY_MIN)).isoformat()
        channel_id = node.get("channel_id", "")

        _tokens[token_code] = {
            "node_id":     node_id,
            "channel_id":  channel_id,
            "manager_url": tunnel_url_str,
            "expires_at":  expires_at,
        }
        _save_tokens()

        dnode_token = _encode_dnode_token(node_id, token_code, tunnel_url_str)
        install_url = f"{tunnel_url_str}/install.sh?token={dnode_token}"
        curl_cmd    = f"curl -fsSL '{install_url}' | sudo bash"
        exp_ts      = int((datetime.utcnow() + timedelta(minutes=TOKEN_EXPIRY_MIN)).timestamp())

        e = _embed("🔑  Token Regenerated", 0x57F287)
        e.description = (
            f"A fresh registration token has been issued for **{node.get('name', node_id)}**."
            + (f" `{len(stale)}` old token(s) invalidated." if stale else "")
        )
        e.description += (
            f"\n> ID `{node_id}` · expires <t:{exp_ts}:R>"
        )

        e.add_field(
            name="One-liner reinstall (recommended)",
            value=f"```bash\n{curl_cmd}\n```",
            inline=False,
        )
        e.add_field(
            name="Manual",
            value=(
                f"```bash\n"
                f"python3 /opt/darknodes/node_agent.py --dnode-token \"{dnode_token}\"\n"
                f"systemctl restart darknodes-agent\n"
                f"```"
            ),
            inline=False,
        )
        e.add_field(name="DNODE Token", value=f"```\n{dnode_token}\n```", inline=False)
        await interaction.followup.send(embed=e, ephemeral=True)

    # ── Group-level error handler — catches ALL unhandled exceptions ──────────
    @node_group.error
    async def node_group_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
        logger.exception(f"[nodes] Unhandled error in /node command: {error}")
        cause = getattr(error, "original", error)
        embed = _err("Command Error", f"An unexpected error occurred:\n```{cause}```")
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception:
            pass

    bot_instance.tree.add_command(node_group)
    logger.info("[nodes] /node command group registered (add|list|info|rename|remove|tunnel|reconnect|regenerate-token)")


# ══════════════════════════════════════════════════════════════════════════════
# Tunnel reconfigure view (shown when tunnel is already configured)
# ══════════════════════════════════════════════════════════════════════════════

class _TunnelReconfigureView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self._reconfigure = False
        self._event       = asyncio.Event()

    @discord.ui.button(label="Reconfigure Tunnel", style=discord.ButtonStyle.primary, emoji="🔄")
    async def reconfigure(self, interaction: discord.Interaction, button: discord.ui.Button):
        self._reconfigure = True
        await interaction.response.defer()
        self._event.set()
        self.stop()

    @discord.ui.button(label="Close", style=discord.ButtonStyle.secondary, emoji="✖")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        self._reconfigure = False
        await interaction.response.defer()
        self._event.set()
        self.stop()

    async def on_timeout(self):
        self._event.set()


# ══════════════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════════════

def init(
    docker_exec_fn:   Callable,
    run_docker_fn:    Callable,
    get_logo_fn:      Callable,
    get_brand_fn:     Callable,
    main_admin_id:    str,
    admin_data_ref:   Optional[dict]     = None,
    get_server_ip_fn: Optional[Callable] = None,   # kept for API compatibility
    vps_data_ref:     Optional[dict]     = None,   # for VPS metadata restore
    save_data_fn:     Optional[Callable] = None,   # for VPS metadata restore
) -> None:
    """Inject bot helpers.  Call BEFORE register_commands() and startup()."""
    global _docker_exec, _run_docker, _get_logo, _get_brand
    global _main_admin_id, _admin_data, _vps_data, _save_data_fn
    _docker_exec   = docker_exec_fn
    _run_docker    = run_docker_fn
    _get_logo      = get_logo_fn
    _get_brand     = get_brand_fn
    _main_admin_id = str(main_admin_id)
    _admin_data    = admin_data_ref
    _vps_data      = vps_data_ref
    _save_data_fn  = save_data_fn

    _load_nodes()
    _load_tokens()
    _load_reconnect_tokens()
    _rebuild_channel_index()
    logger.info("[nodes] Node system initialised (Discord channel relay + Cloudflare Tunnel edition)")


async def startup(bot_instance=None) -> None:
    """
    Call from on_ready as:  asyncio.create_task(node_system.startup())

    • Ensures the local node exists.
    • Starts the local stats refresh loop.
    • Starts the offline monitor loop.
    • Starts the Node Manager HTTP server (serves install.sh, node_agent.py).
    • No HTTP server replacement — communication uses Discord channels.
    """
    global _bot
    if bot_instance is not None:
        _bot = bot_instance

    await _init_local_node()
    asyncio.create_task(_local_stats_loop())
    asyncio.create_task(_offline_monitor_loop())
    asyncio.create_task(_channel_audit_loop())
    asyncio.create_task(_start_node_manager_http())
    logger.info(f"[nodes] Node system started — HTTP polling API on 127.0.0.1:{NODE_MANAGER_PORT}")
