"""
DarkNodes Node System  —  Discord-relay edition
──────────────────────────────────────────────
All communication between the bot and remote agents flows through private
Discord threads.  Neither the bot server nor the remote node needs a public
IP address or open inbound ports of any kind.

Architecture
────────────
• Bot creates one private Discord thread per node via /node add.
• Agent runs node_agent.py — only needs outbound HTTPS to discord.com.
• All traffic goes through Discord's servers as messages in the thread.

  Bot machine  ──HTTPS──▶  Discord API  ◀──HTTPS──  Node machine
                                │
                (messages relayed through per-node private threads)

Registration flow
-----------------
1. Admin runs  /node add  in Discord (ephemeral reply shows a one-line command).
2. Admin copies the command to the remote machine and runs it:
       python3 node_agent.py \\
         --token  <BOT_TOKEN>  \\
         --thread <THREAD_ID>  \\
         --code   <CODE>
3. Agent posts REG:{code}:{hostname}:{public_ip} to the thread.
4. Bot validates, creates node record, replies REGISTERED:{node_id}:{secret}.
5. Agent saves credentials; polling loop begins.

Ongoing protocol (all inside the node's private Discord thread)
---------------------------------------------------------------
Agent → Bot:
    STAT:{node_id}:{secret}:{cpu}:{ram_used}:{ram_total}:{vps_count}
    RES:{node_id}:{secret}:{cmd_id}:{ok}:{b64_output}

Bot → Agent:
    CMD:{node_id}:{cmd_id}:{command_b64}

Integration with bot.py
-----------------------
    node_system.init(docker_exec, run_docker_command,
                     get_logo_url, get_brand_name,
                     MAIN_ADMIN_ID, admin_data)
    node_system.register_commands(bot)
    # inside on_ready:
    asyncio.create_task(node_system.startup(bot))
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import secrets
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Dict, List, Optional

import discord
from discord import app_commands

# ── Logger ────────────────────────────────────────────────────────────────────
logger = logging.getLogger("vps_bot.nodes")

# ── Constants ─────────────────────────────────────────────────────────────────
_BASE            = os.path.dirname(os.path.abspath(__file__))
NODES_FILE       = os.path.join(_BASE, "nodes.json")
TOKENS_FILE      = os.path.join(_BASE, "node_tokens.json")
TOKEN_EXPIRY_MIN = 60          # minutes a registration token is valid
OFFLINE_SECS     = 90          # no heartbeat → node is offline
SELECT_TIMEOUT_S = 90          # seconds user has to pick a node
LOCAL_NODE_ID    = "local"
POLL_INTERVAL    = 5           # seconds between Discord thread polls
DISCORD_API      = "https://discord.com/api/v10"

# ── Injected by init() ────────────────────────────────────────────────────────
_docker_exec:   Optional[Callable] = None
_run_docker:    Optional[Callable] = None
_get_logo:      Optional[Callable] = None
_get_brand:     Optional[Callable] = None
_main_admin_id: str                = ""
_admin_data:    Optional[dict]     = None
_bot:           Optional[Any]      = None

# ── In-memory state ───────────────────────────────────────────────────────────
nodes:         Dict[str, Dict] = {}   # {node_id: node_record}
_tokens:       Dict[str, Dict] = {}   # {code: {thread_id, expires_at}}
_cmd_results:  Dict[str, Dict] = {}   # {cmd_id: {success, output}}
# Track last-seen message ID per thread so we don't re-process old messages
_thread_cursors: Dict[str, str] = {}  # {thread_id: last_message_id}


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


# ══════════════════════════════════════════════════════════════════════════════
# Discord REST helpers  (async, pure stdlib HTTP)
# ══════════════════════════════════════════════════════════════════════════════

def _bot_token() -> str:
    """Get the bot token from the injected bot instance."""
    if _bot and hasattr(_bot, "http") and hasattr(_bot.http, "token"):
        return _bot.http.token or ""
    return os.environ.get("DISCORD_BOT_TOKEN", "")


def _discord_request(
    method: str,
    path: str,
    payload: dict | None = None,
    timeout: int = 15,
) -> dict | list:
    """Synchronous Discord REST call (used only during setup)."""
    token = _bot_token()
    url   = f"{DISCORD_API}{path}"
    data  = json.dumps(payload).encode() if payload else None
    headers = {
        "Authorization": f"Bot {token}",
        "User-Agent":    "DarkNodes-NodeSystem/3.0",
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
        raise RuntimeError(f"Discord {method} {path} → HTTP {exc.code}: {body[:300]}")


async def _discord_request_async(
    method: str,
    path: str,
    payload: dict | None = None,
    timeout: int = 15,
) -> dict | list:
    """Async wrapper — runs sync Discord call in a thread pool."""
    return await asyncio.to_thread(_discord_request, method, path, payload, timeout)


async def _post_to_thread(thread_id: str, content: str) -> dict:
    """Post a message to a Discord thread."""
    if len(content) > 1990:
        content = content[:1987] + "…"
    return await _discord_request_async(
        "POST",
        f"/channels/{thread_id}/messages",
        {"content": content},
    )


async def _get_thread_messages(thread_id: str, after_id: str = "") -> list:
    """
    Fetch up to 10 messages from a thread newer than after_id.
    Returns oldest-first (ascending chronological).
    """
    path = f"/channels/{thread_id}/messages?limit=10"
    if after_id:
        path += f"&after={after_id}"
    msgs = await _discord_request_async("GET", path)
    if not isinstance(msgs, list):
        return []
    # Discord returns newest-first; reverse to oldest-first
    return list(reversed(msgs))


# ══════════════════════════════════════════════════════════════════════════════
# Local node stats  (pure stdlib)
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


def _read_ram() -> tuple[int, int]:
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


async def _count_local_vps() -> int:
    if not _run_docker:
        return 0
    out, _, rc = await _run_docker(
        'docker ps --filter "label=darknodes.vps=true" -q', timeout=10
    )
    if rc != 0:
        return 0
    return len([l for l in out.strip().splitlines() if l.strip()])


async def _collect_local_stats() -> Dict[str, Any]:
    cpu       = await asyncio.to_thread(_read_cpu)
    used, tot = await asyncio.to_thread(_read_ram)
    vps       = await _count_local_vps()
    return {"cpu": cpu, "ram_used_mb": used, "ram_total_mb": tot, "running_vps": vps}


# ══════════════════════════════════════════════════════════════════════════════
# Node helpers
# ══════════════════════════════════════════════════════════════════════════════

def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _node_online(node: dict) -> bool:
    if node.get("type") == "local":
        return True
    last = node.get("last_seen")
    if not last:
        return False
    try:
        return (datetime.utcnow() - datetime.fromisoformat(last)).total_seconds() < OFFLINE_SECS
    except Exception:
        return False


def list_online_nodes() -> Dict[str, Dict]:
    return {nid: n for nid, n in nodes.items() if _node_online(n)}


def get_node(node_id: str) -> Optional[Dict]:
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
        "secret":     "",
        "thread_id":  "",
    }
    _save_nodes()
    logger.info(f"[nodes] Local node initialised (hostname={hostname})")


# ══════════════════════════════════════════════════════════════════════════════
# Thread message processing
# ══════════════════════════════════════════════════════════════════════════════

async def _process_message(content: str) -> None:
    """
    Dispatch a single Discord thread message to the appropriate handler.

    Expected message formats (from node_agent.py):
      REG:{code}:{hostname}:{public_ip}
      STAT:{node_id}:{secret}:{cpu}:{ram_used}:{ram_total}:{vps_count}
      RES:{node_id}:{secret}:{cmd_id}:{ok}:{b64_output}
    """
    if content.startswith("REG:"):
        # REG:{code}:{hostname}:{public_ip}
        parts = content.split(":", 3)
        if len(parts) < 3:
            return
        code      = parts[1]
        hostname  = parts[2]
        public_ip = parts[3] if len(parts) > 3 else ""

        token_data = _tokens.get(code)
        if not token_data:
            logger.warning(f"[nodes] REG received with unknown code: {code[:16]}…")
            return

        # Check expiry
        try:
            exp = datetime.fromisoformat(token_data["expires_at"])
            if datetime.utcnow() > exp:
                logger.warning(f"[nodes] REG received with expired code: {code[:16]}…")
                del _tokens[code]
                _save_tokens()
                return
        except Exception:
            pass

        thread_id = token_data.get("thread_id", "")
        node_id   = secrets.token_hex(6)
        secret    = secrets.token_hex(24)

        nodes[node_id] = {
            "id":         node_id,
            "name":       hostname,
            "type":       "remote",
            "hostname":   hostname,
            "public_ip":  public_ip,
            "thread_id":  thread_id,
            "created_at": _now_iso(),
            "last_seen":  _now_iso(),
            "stats":      {},
            "secret":     secret,
        }
        _save_nodes()

        del _tokens[code]
        _save_tokens()

        # Reply to the agent
        if thread_id:
            try:
                await _post_to_thread(thread_id, f"REGISTERED:{node_id}:{secret}")
            except Exception as exc:
                logger.error(f"[nodes] Failed to send REGISTERED reply: {exc}")

        logger.info(f"[nodes] Remote node registered: {node_id} ({hostname} @ {public_ip or 'unknown IP'})")

    elif content.startswith("STAT:"):
        # STAT:{node_id}:{secret}:{cpu}:{ram_used}:{ram_total}:{vps_count}
        parts = content.split(":")
        if len(parts) < 7:
            return
        node_id   = parts[1]
        secret    = parts[2]
        node      = nodes.get(node_id)
        if not node or node.get("secret") != secret:
            return
        try:
            node["stats"] = {
                "cpu":          float(parts[3]),
                "ram_used_mb":  int(parts[4]),
                "ram_total_mb": int(parts[5]),
                "running_vps":  int(parts[6]),
            }
            node["last_seen"] = _now_iso()
            _save_nodes()
            logger.debug(f"[nodes] Heartbeat from {node_id}: CPU={parts[3]}% RAM={parts[4]}/{parts[5]}MB")
        except (ValueError, IndexError) as exc:
            logger.debug(f"[nodes] STAT parse error: {exc}")

    elif content.startswith("RES:"):
        # RES:{node_id}:{secret}:{cmd_id}:{ok}:{b64_output}
        parts = content.split(":", 5)
        if len(parts) < 6:
            return
        node_id  = parts[1]
        secret   = parts[2]
        cmd_id   = parts[3]
        ok       = parts[4] == "1"
        b64_out  = parts[5]

        node = nodes.get(node_id)
        if not node or node.get("secret") != secret:
            return

        try:
            output = base64.b64decode(b64_out.encode()).decode(errors="replace")
        except Exception:
            output = b64_out

        _cmd_results[cmd_id] = {"success": ok, "output": output}
        logger.debug(f"[nodes] Result for cmd {cmd_id}: success={ok}")


# ══════════════════════════════════════════════════════════════════════════════
# Background polling loop — reads messages from all node threads
# ══════════════════════════════════════════════════════════════════════════════

async def _poll_threads_loop() -> None:
    """
    Continuously poll Discord threads for all registered nodes and pending
    registration tokens.  Processes STAT, RES, and REG messages.
    """
    logger.info("[nodes] Discord thread polling loop started")
    while True:
        try:
            # Collect all thread IDs to poll
            thread_ids: set[str] = set()

            # Pending-registration threads
            for code, tdata in list(_tokens.items()):
                tid = tdata.get("thread_id", "")
                if tid:
                    thread_ids.add(tid)

            # Registered node threads
            for node in nodes.values():
                tid = node.get("thread_id", "")
                if tid:
                    thread_ids.add(tid)

            for tid in thread_ids:
                try:
                    after = _thread_cursors.get(tid, "")
                    messages = await _get_thread_messages(tid, after)
                    for msg in messages:
                        mid     = msg.get("id", "")
                        content = msg.get("content", "").strip()
                        if mid:
                            _thread_cursors[tid] = mid
                        if content:
                            await _process_message(content)
                except Exception as exc:
                    logger.debug(f"[nodes] Poll error for thread {tid}: {exc}")

        except Exception as exc:
            logger.warning(f"[nodes] Poll loop error: {exc}")

        await asyncio.sleep(POLL_INTERVAL)


# ══════════════════════════════════════════════════════════════════════════════
# Local stats refresh loop
# ══════════════════════════════════════════════════════════════════════════════

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
# Remote command execution
# ══════════════════════════════════════════════════════════════════════════════

async def remote_execute(node_id: str, command: str, timeout: int = 120) -> str:
    """
    Run a shell command on a remote node and return its stdout.

    Posts a CMD message to the node's Discord thread and waits for
    the RES reply.  Raises RuntimeError on failure or timeout.
    """
    node = nodes.get(node_id)
    if not node:
        raise RuntimeError(f"Unknown node: {node_id}")

    thread_id = node.get("thread_id", "")
    if not thread_id:
        raise RuntimeError(f"Node {node_id} has no thread_id — cannot relay command")

    cmd_id    = secrets.token_hex(8)
    b64_cmd   = base64.b64encode(command.encode()).decode()

    await _post_to_thread(thread_id, f"CMD:{node_id}:{cmd_id}:{b64_cmd}")
    logger.info(f"[nodes] Sent CMD {cmd_id} to node {node_id}: {command[:80]}")

    deadline = time.time() + timeout
    while time.time() < deadline:
        await asyncio.sleep(0.5)
        result = _cmd_results.get(cmd_id)
        if result is not None:
            del _cmd_results[cmd_id]
            if not result.get("success"):
                raise RuntimeError(result.get("output") or "Remote command failed")
            return result.get("output", "")

    raise RuntimeError(f"Remote command timed out after {timeout}s (cmd_id={cmd_id})")


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
        e.set_thumbnail(url=logo)
    e.set_footer(text=f"{_brand()}  •  Node System")
    return e


def _err(title: str, desc: str) -> discord.Embed:
    e = _embed(f"❌  {title}", 0xED4245)
    e.description = desc
    return e


def _status_dot(node: dict) -> str:
    return "🟢" if _node_online(node) else "🔴"


def _stats_line(node: dict) -> str:
    s   = node.get("stats", {})
    cpu = s.get("cpu", "—")
    u   = s.get("ram_used_mb")
    t   = s.get("ram_total_mb")
    ram = f"{u}/{t} MB" if u is not None and t else "—"
    vps = s.get("running_vps", "—")
    return f"CPU `{cpu}%`  RAM `{ram}`  VPSes `{vps}`"


# ══════════════════════════════════════════════════════════════════════════════
# Node selection UI
# ══════════════════════════════════════════════════════════════════════════════

class _NodeSelectView(discord.ui.View):
    def __init__(self, online: Dict[str, Dict]):
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
            s     = n.get("stats", {})
            cpu   = s.get("cpu", "?")
            u     = s.get("ram_used_mb")
            t     = s.get("ram_total_mb")
            ram   = f"{u}/{t}MB" if u is not None and t else "?"
            tag   = "(local)" if nid == LOCAL_NODE_ID else "(remote)"
            opts.append(discord.SelectOption(
                label=f"{n.get('name', nid)} {tag}",
                value=nid,
                description=f"CPU {cpu}%  RAM {ram}  VPSes {s.get('running_vps','?')}",
            ))

        sel = discord.ui.Select(placeholder="Choose a deployment node…", options=opts, row=0)
        sel.callback = self._on_select
        self.add_item(sel)
        self._sel = sel

        ok = discord.ui.Button(label="Deploy Here", style=discord.ButtonStyle.success,
                               row=1, emoji="🚀")
        ok.callback = self._on_ok
        self.add_item(ok)

        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.danger,
                                   row=1, emoji="✖")
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
    • Single node  → returns immediately (no UI shown).
    • Multiple nodes → shows a Discord selection UI and waits.
    • Returns None if cancelled / timed out.
    """
    online = list_online_nodes()
    if len(online) <= 1:
        return next(iter(online), LOCAL_NODE_ID)

    embed = _embed("🖥️  Select Deployment Node", 0x5865F2)
    embed.description = (
        "Multiple nodes are available.\n"
        "Pick where this VPS should be deployed, or let the bot choose."
    )
    for nid, n in online.items():
        tag = "`local`" if nid == LOCAL_NODE_ID else "`remote`"
        embed.add_field(
            name=f"{_status_dot(n)}  {n.get('name', nid)}  {tag}",
            value=_stats_line(n),
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
# Slash commands   /node  group
# ══════════════════════════════════════════════════════════════════════════════

def register_commands(bot) -> None:
    """Register /node slash commands."""

    node_group = app_commands.Group(
        name="node",
        description="Manage DarkNodes deployment nodes",
    )

    # ── /node add ─────────────────────────────────────────────────────────────
    @node_group.command(
        name="add",
        description="Generate a one-time token to register a new remote node (Admin only)",
    )
    async def node_add(interaction: discord.Interaction):
        if not _is_admin(interaction):
            await interaction.response.send_message(
                embed=_err("Access Denied", "Only admins can add nodes."), ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)

        # Create a private thread for this node's relay channel
        thread_id = ""
        thread_mention = ""
        try:
            channel = interaction.channel
            if hasattr(channel, "create_thread"):
                thread_name = f"node-{secrets.token_hex(3)}"
                try:
                    thread = await channel.create_thread(
                        name=thread_name,
                        type=discord.ChannelType.private_thread,
                        reason="DarkNodes: new node relay thread",
                    )
                except Exception:
                    # Fall back to public thread if private threads aren't available
                    thread = await channel.create_thread(
                        name=thread_name,
                        auto_archive_duration=10080,  # 7 days
                        reason="DarkNodes: new node relay thread",
                    )
                thread_id      = str(thread.id)
                thread_mention = thread.mention
        except Exception as exc:
            logger.warning(f"[nodes] Could not create thread: {exc}")

        # Generate one-time registration code
        code       = secrets.token_urlsafe(16)
        expires_at = (datetime.utcnow() + timedelta(minutes=TOKEN_EXPIRY_MIN)).isoformat()
        _tokens[code] = {"thread_id": thread_id, "expires_at": expires_at}
        _save_tokens()

        exp_ts = int((datetime.utcnow() + timedelta(minutes=TOKEN_EXPIRY_MIN)).timestamp())

        # Get the bot token to show in the command
        bot_token_display = _bot_token() or "<YOUR_BOT_TOKEN>"

        embed = _embed("➕  New Node Registration", 0x57F287)
        embed.description = (
            "Run the **Node Agent** on the remote machine to connect it.\n"
            "**No public IP or open ports needed** — the node only needs outbound HTTPS."
        )
        embed.add_field(
            name="🖥️  Run on the remote machine",
            value=(
                f"```bash\n"
                f"python3 node_agent.py \\\n"
                f"  --token  {bot_token_display} \\\n"
                f"  --thread {thread_id or '<thread_id>'} \\\n"
                f"  --code   {code}\n"
                f"```"
            ),
            inline=False,
        )
        if thread_mention:
            embed.add_field(
                name="💬  Relay Thread",
                value=f"{thread_mention}\nAll node communication happens here.",
                inline=True,
            )
        embed.add_field(
            name="⏰  Code Expires",
            value=f"<t:{exp_ts}:R>",
            inline=True,
        )
        embed.add_field(
            name="ℹ️  Requirements",
            value=(
                "• Python 3.8+  (no extra packages needed)\n"
                "• Outbound internet access only — **no public IP required**\n"
                "• Works behind NAT, CGNAT, VPN, firewalls\n"
                "• Code is single-use and expires automatically"
            ),
            inline=False,
        )

        if not thread_id:
            embed.add_field(
                name="⚠️  Thread creation failed",
                value=(
                    "Could not create a relay thread automatically. "
                    "Create a private thread manually, then replace `<thread_id>` "
                    "in the command above with its ID."
                ),
                inline=False,
            )

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /node remove ─────────────────────────────────────────────────────────
    @node_group.command(
        name="remove",
        description="Remove a registered node (Admin only)",
    )
    @app_commands.describe(node_id="Node ID shown in /node list")
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

        _save_nodes()
        e = _embed("🗑️  Node Removed", 0x57F287)
        e.add_field(name="ID",   value=f"`{node_id}`",              inline=True)
        e.add_field(name="Name", value=f"`{node.get('name','?')}`",  inline=True)
        await interaction.response.send_message(embed=e)

    # ── /node rename ──────────────────────────────────────────────────────────
    @node_group.command(
        name="rename",
        description="Give a node a custom display name (Admin only)",
    )
    @app_commands.describe(
        node_id="Node ID shown in /node list",
        name="New display name",
    )
    async def node_rename(interaction: discord.Interaction, node_id: str, name: str):
        if not _is_admin(interaction):
            await interaction.response.send_message(
                embed=_err("Access Denied", "Only admins can rename nodes."), ephemeral=True
            )
            return

        name = name.strip()[:64]
        if not name:
            await interaction.response.send_message(
                embed=_err("Invalid Name", "Name cannot be empty."), ephemeral=True
            )
            return

        node = nodes.get(node_id)
        if not node:
            await interaction.response.send_message(
                embed=_err("Node Not Found", f"No node with ID `{node_id}`."),
                ephemeral=True,
            )
            return

        old          = node.get("name", node_id)
        node["name"] = name
        _save_nodes()

        e = _embed("✏️  Node Renamed", 0x57F287)
        e.add_field(name="ID",       value=f"`{node_id}`", inline=True)
        e.add_field(name="Old Name", value=f"`{old}`",     inline=True)
        e.add_field(name="New Name", value=f"`{name}`",    inline=True)
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

        embed = _embed("🖥️  Node List", 0x5865F2)
        embed.description = f"**{len(nodes)}** node(s) registered"

        for nid, n in nodes.items():
            online = _node_online(n)
            status = "🟢 **Online**" if online else "🔴 **Offline**"
            tag    = "`Local`" if nid == LOCAL_NODE_ID else "`Remote`"
            s      = n.get("stats", {})
            cpu    = f"`{s.get('cpu', '—')}%`"
            u, t   = s.get("ram_used_mb"), s.get("ram_total_mb")
            ram    = f"`{u}/{t} MB`" if u is not None and t else "`—`"
            vps_c  = f"`{s.get('running_vps', '—')}`"
            ip_ln  = f"\n**IP:** `{n['public_ip']}`" if n.get("public_ip") else ""
            tid    = n.get("thread_id", "")
            thread_ln = f"\n**Thread:** `{tid}`" if tid else ""

            embed.add_field(
                name=f"{_status_dot(n)}  {n.get('name', nid)}  {tag}",
                value=(
                    f"**ID:** `{nid}`\n"
                    f"**Status:** {status}{ip_ln}{thread_ln}\n"
                    f"**CPU:** {cpu}  **RAM:** {ram}  **VPSes:** {vps_c}"
                ),
                inline=False,
            )

        await interaction.followup.send(embed=embed)

    # ── /node status ──────────────────────────────────────────────────────────
    @node_group.command(
        name="status",
        description="Show detailed status for a specific node",
    )
    @app_commands.describe(node_id="Node ID (leave blank for all)")
    async def node_status(interaction: discord.Interaction, node_id: str = ""):
        await interaction.response.defer()

        target = {node_id: nodes[node_id]} if node_id and node_id in nodes else nodes
        if not target:
            await interaction.followup.send(
                embed=_err("Not Found", f"Node `{node_id}` does not exist.")
            )
            return

        for nid, n in target.items():
            online = _node_online(n)
            s      = n.get("stats", {})
            e = _embed(f"🖥️  {n.get('name', nid)}", 0x57F287 if online else 0xED4245)
            e.add_field(name="ID",       value=f"`{nid}`",                    inline=True)
            e.add_field(name="Type",     value=n.get("type", "?"),            inline=True)
            e.add_field(name="Status",   value="🟢 Online" if online else "🔴 Offline", inline=True)
            e.add_field(name="Host",     value=f"`{n.get('hostname','?')}`",  inline=True)
            e.add_field(name="IP",       value=f"`{n.get('public_ip','—')}`", inline=True)
            tid = n.get("thread_id", "")
            e.add_field(name="Thread",   value=f"`{tid}`" if tid else "`—`",  inline=True)
            if s:
                e.add_field(name="CPU",  value=f"`{s.get('cpu','—')}%`",      inline=True)
                u, t = s.get("ram_used_mb"), s.get("ram_total_mb")
                e.add_field(name="RAM",  value=f"`{u}/{t} MB`" if u and t else "`—`", inline=True)
                e.add_field(name="VPSes", value=f"`{s.get('running_vps','—')}`", inline=True)
            e.add_field(name="Last Seen", value=f"`{n.get('last_seen','never')}`", inline=False)
            await interaction.followup.send(embed=e)

    bot.tree.add_command(node_group)
    logger.info("[nodes] /node command group registered")


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
    get_server_ip_fn: Optional[Callable] = None,   # kept for API compatibility; unused
) -> None:
    """Inject bot helpers. Call BEFORE register_commands() and startup()."""
    global _docker_exec, _run_docker, _get_logo, _get_brand, _main_admin_id, _admin_data
    _docker_exec   = docker_exec_fn
    _run_docker    = run_docker_fn
    _get_logo      = get_logo_fn
    _get_brand     = get_brand_fn
    _main_admin_id = str(main_admin_id)
    _admin_data    = admin_data_ref

    _load_nodes()
    _load_tokens()
    logger.info("[nodes] node_system initialised (Discord-relay edition — no public IP required)")


async def startup(bot_instance=None) -> None:
    """
    Call from on_ready as:  asyncio.create_task(node_system.startup(bot))

    • Ensures the local node exists.
    • Starts the local stats refresh loop.
    • Starts the Discord thread polling loop (replaces WebSocket server).
    """
    global _bot
    if bot_instance is not None:
        _bot = bot_instance

    await _init_local_node()
    asyncio.create_task(_local_stats_loop())
    asyncio.create_task(_poll_threads_loop())
    logger.info("[nodes] Node system started — polling Discord threads for node messages")
