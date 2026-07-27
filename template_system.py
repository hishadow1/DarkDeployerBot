"""
DarkNodes Template System  (v3)
─────────────────────────────────────────────────────────────────────────────
Modular application installer for VPS containers.

Flow:
  /template → Select VPS → Select Template → (component select) → questions → install

Adding a new template
─────────────────────
1. Add a handler function  _handle_<name>(interaction, container, vps_info)
2. Add a button for it in  _TemplateSelectView
3. Add its step list and   _run_<name>_installation() runner

Everything else (progress embed, exec helper, modals) is shared infrastructure.

Registered into bot.py via:
    import template_system
    template_system.init(docker_exec, get_logo_url, vps_data, get_brand_name)
    template_system.register_commands(bot)
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import asyncio
import base64
import secrets
import string
import logging
import re
import shlex
from typing import Callable, Awaitable, Optional

import aiohttp

import discord
from discord.ext import commands as ext_commands

log = logging.getLogger("template_system")

# ─────────────────────────────────────────────────────────────────────────────
# Injected module-level references  (set once from bot.py via init())
# ─────────────────────────────────────────────────────────────────────────────
_docker_exec:          Optional[Callable[..., Awaitable]] = None
_run_container_cmd:    Optional[Callable[..., Awaitable]] = None   # (node_id, container, cmd, timeout) → (out, err, rc)
_get_logo_url:         Optional[Callable[[], str]]        = None
_get_brand_name:       Optional[Callable[[], str]]        = None
_vps_data:             Optional[dict]                     = None
_save_data:            Optional[Callable[[], None]]        = None
_LOCAL_NODE_ID:        str                                = "local"


def init(
    docker_exec_fn:         Callable[..., Awaitable],
    get_logo_url_fn:        Callable[[], str],
    vps_data_ref:           dict,
    get_brand_name_fn:      Optional[Callable[[], str]] = None,
    run_container_cmd_fn:   Optional[Callable[..., Awaitable]] = None,
    local_node_id:          str = "local",
    save_data_fn:           Optional[Callable[[], None]] = None,
) -> None:
    global _docker_exec, _run_container_cmd, _get_logo_url, _get_brand_name, _vps_data, _save_data, _LOCAL_NODE_ID
    _docker_exec       = docker_exec_fn
    _run_container_cmd = run_container_cmd_fn
    _get_logo_url      = get_logo_url_fn
    _get_brand_name    = get_brand_name_fn
    _vps_data          = vps_data_ref
    _save_data         = save_data_fn
    _LOCAL_NODE_ID     = local_node_id


def _brand() -> str:
    return _get_brand_name() if _get_brand_name else "DarkNodes"

def _brand_slug() -> str:
    """Return a lowercase alphanumeric slug from the current brand name.
    Used wherever a safe identifier is needed: usernames, DB names, location
    short codes, email domains.  Falls back to 'panel' if the brand name
    contains no alphanumeric characters at all."""
    raw = _brand()
    slug = re.sub(r"[^a-z0-9]", "", raw.lower())
    return slug[:20] or "panel"

def _logo() -> str:
    return _get_logo_url() if _get_logo_url else ""


# ─────────────────────────────────────────────────────────────────────────────
# Installation step lists
# ─────────────────────────────────────────────────────────────────────────────
PANEL_STEPS = [
    "Preparing System",
    "Installing Dependencies",
    "Installing PHP 8.3",
    "Installing MariaDB",
    "Installing Redis",
    "Installing Nginx",
    "Installing Composer",
    "Downloading Pterodactyl",
    "Installing Panel Files",
    "Configuring Database",
    "Configuring Environment",
    "Running Migrations",
    "Creating Admin Account",
    "Setting Permissions",
    "Configuring Nginx",
    "Setting Up SSL",
    "Enabling Services",
    "Verifying Installation",
]

WINGS_STEPS = [
    "Preparing System",        # 0
    "Installing Dependencies", # 1
    "Downloading Wings Binary",# 2
    "Creating Directories",    # 3
    "Setting Up SSL",          # 4
    "Writing Configuration",   # 5
    "Installing Service",      # 6
    "Connecting to Panel",     # 7  ← auto-creates location+node, writes real config
    "Starting Wings",          # 8
    "Verifying Install",       # 9
]

CLOUDFLARE_STEPS = [
    "Preparing System",
    "Installing cloudflared",
    "Validating Tunnel Token",
    "Installing Systemd Service",
    "Starting Tunnel",
    "Verifying Tunnel",
    "Detecting Local Services",
]


# ─────────────────────────────────────────────────────────────────────────────
# Shared utilities
# ─────────────────────────────────────────────────────────────────────────────
def _secure(length: int, extra: str = "") -> str:
    alpha = string.ascii_letters + string.digits + extra
    return "".join(secrets.choice(alpha) for _ in range(length))


def _gen_panel_creds() -> dict:
    # NOTE: db_pass and admin_pass intentionally use only alphanumeric + safe punctuation.
    # Characters like $, !, #, @ break shell double-quoted strings (variable expansion,
    # history expansion) and corrupt SQL passed via `mysql -e "..."`.
    # The extra length compensates for the reduced character set.
    slug = _brand_slug()
    return {
        "admin_user":  f"{slug[:6]}_{_secure(8)}",
        "admin_email": f"admin_{_secure(10)}@{slug}.internal",
        "admin_pass":  _secure(28),          # alphanumeric only — safe in shell + artisan
        "db_name":     "ptero_" + _secure(8),
        "db_user":     "pterouser_" + _secure(6),
        "db_pass":     _secure(28),          # alphanumeric only — safe in SQL + shell + sed
    }


def _write_file_cmd(path: str, content: str) -> str:
    """Write content to a file on the remote via base64 (avoids heredoc escaping issues)."""
    encoded = base64.b64encode(content.encode()).decode()
    return f"echo '{encoded}' | base64 -d > {path}"


def _set_footer(embed: discord.Embed, logo: str) -> None:
    kw: dict = {"text": f"{_brand()}  •  Template System"}
    if logo:
        embed.set_author(name=f"{_brand()}  •  Template System", icon_url=logo)
        kw["icon_url"] = logo
    embed.set_footer(**kw)


# ─────────────────────────────────────────────────────────────────────────────
# Nginx config builders
# ─────────────────────────────────────────────────────────────────────────────
_NGINX_PHP_BLOCK = """\
    location ~ \\.php$ {
        fastcgi_split_path_info ^(.+\\.php)(/.+)$;
        fastcgi_pass unix:/run/php/php8.3-fpm.sock;
        fastcgi_index index.php;
        include fastcgi_params;
        fastcgi_param PHP_VALUE "upload_max_filesize = 100M\\npost_max_size=100M";
        fastcgi_param SCRIPT_FILENAME $document_root$fastcgi_script_name;
        fastcgi_param HTTP_PROXY "";
        fastcgi_intercept_errors off;
        fastcgi_buffer_size 16k;
        fastcgi_buffers 4 16k;
        fastcgi_connect_timeout 300;
        fastcgi_send_timeout 300;
        fastcgi_read_timeout 300;
    }"""


def _nginx_http(domain: str) -> str:
    return f"""server {{
    listen 80;
    server_name {domain};
    root /var/www/pterodactyl/public;
    index index.php;
    charset utf-8;
    location / {{ try_files $uri $uri/ /index.php?$query_string; }}
    location = /favicon.ico {{ access_log off; log_not_found off; }}
    location = /robots.txt  {{ access_log off; log_not_found off; }}
{_NGINX_PHP_BLOCK}
    location ~ /\\.ht {{ deny all; }}
    access_log /var/log/nginx/pterodactyl.access.log;
    error_log  /var/log/nginx/pterodactyl.error.log error;
}}"""


def _nginx_https(domain: str, cert: str, key: str) -> str:
    return f"""server {{
    listen 443 ssl http2;
    server_name {domain};
    root /var/www/pterodactyl/public;
    index index.php;
    charset utf-8;
    ssl_certificate     {cert};
    ssl_certificate_key {key};
    ssl_session_cache   shared:SSL:10m;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;
    location / {{ try_files $uri $uri/ /index.php?$query_string; }}
    location = /favicon.ico {{ access_log off; log_not_found off; }}
    location = /robots.txt  {{ access_log off; log_not_found off; }}
{_NGINX_PHP_BLOCK}
    location ~ /\\.ht {{ deny all; }}
    access_log /var/log/nginx/pterodactyl.access.log;
    error_log  /var/log/nginx/pterodactyl.error.log error;
}}
server {{
    listen 80;
    server_name {domain};
    return 301 https://$host$request_uri;
}}"""


# ─────────────────────────────────────────────────────────────────────────────
# Progress embed  —  shows every step with ⬜ / ⏳ / ✅ / ❌
# ─────────────────────────────────────────────────────────────────────────────
def _progress_embed(
    title:     str,
    steps:     list[str],
    current:   int,          # index of the step currently running
    logo:      str = "",
    failed_at: int = -1,
    error:     str = "",
) -> discord.Embed:
    """Build a live-progress embed.

    States per step:
        ⬜  not yet reached
        ⏳  currently running  (current step)
        ✅  completed
        ❌  failed
    """
    if failed_at >= 0:
        color = 0xED4245
        subtitle = f"❌  Failed at: **{steps[failed_at]}**"
    elif current >= len(steps):
        color = 0x57F287
        subtitle = "✅  Installation complete!"
    else:
        color = 0x5865F2
        subtitle = f"⏳  {steps[current]}"

    lines: list[str] = []
    for i, name in enumerate(steps):
        if failed_at >= 0:
            if i < failed_at:
                icon = "✅"
            elif i == failed_at:
                icon = "❌"
            else:
                icon = "⬜"
        else:
            if i < current:
                icon = "✅"
            elif i == current:
                icon = "⏳"
            else:
                icon = "⬜"
        lines.append(f"{icon}  {name}")

    progress_text = "\n".join(lines)
    done   = min(current, len(steps))
    total  = len(steps)
    pct    = int(done / total * 100) if total else 0
    bar_w  = 12
    filled = int(done / total * bar_w) if total else 0
    bar    = "█" * filled + "░" * (bar_w - filled)

    desc = f"{subtitle}\n\n```\n{progress_text}\n```\n`[{bar}] {pct}%`"
    if failed_at >= 0 and error:
        tail = error[-600:] if len(error) > 600 else error
        desc += f"\n\n**Error output:**\n```\n{tail}\n```"

    embed = discord.Embed(title=title, description=desc, color=color)
    _set_footer(embed, logo)
    return embed


# ─────────────────────────────────────────────────────────────────────────────
# Exec helpers  (bound per container)
# ─────────────────────────────────────────────────────────────────────────────
def _make_exec(container: str, node_id: str = None):
    """Return an async exec(cmd, timeout) bound to a container and its node.

    Routes through _run_container_cmd for remote nodes so the command runs
    inside the correct Docker daemon, not the local one.
    """
    async def _exec(cmd: str, timeout: int = 180) -> tuple[bool, str]:
        is_local = not node_id or node_id == _LOCAL_NODE_ID
        try:
            if is_local or _run_container_cmd is None:
                if not _docker_exec:
                    return False, "docker_exec not initialised"
                out, err, rc = await _docker_exec(container, cmd, timeout=timeout)
            else:
                # Remote node — wrap with docker exec on the node
                out, err, rc = await _run_container_cmd(node_id, container, cmd, timeout=timeout)
            combined = ((out or "") + "\n" + (err or "")).strip()
            return rc == 0, combined
        except Exception as exc:
            return False, str(exc)
    return _exec


def _make_step(exec_fn, update_fn, steps: list[str], tag: str):
    """Return an async step(idx, cmd, timeout) function.

    Marks the step running, runs the command, advances or fails.
    """
    async def _step(idx: int, cmd: str, timeout: int = 180) -> bool:
        await update_fn(idx)
        ok, out = await exec_fn(cmd, timeout)
        if not ok:
            tail = out[-800:] if len(out) > 800 else out
            await update_fn(idx, failed_at=idx, error=tail)
            log.error(f"[{tag}] step {idx} ({steps[idx]}) failed:\n{tail}")
        else:
            await update_fn(idx + 1)
        return ok
    return _step


# ─────────────────────────────────────────────────────────────────────────────
# Interaction helper — send progress embed and return a Message for editing
# ─────────────────────────────────────────────────────────────────────────────
async def _start_progress(
    interaction: discord.Interaction,
    embed: discord.Embed,
) -> discord.Message:
    """Respond to any interaction type and return an editable Message."""
    if interaction.type == discord.InteractionType.modal_submit:
        await interaction.response.send_message(embed=embed, ephemeral=True)
    else:
        await interaction.response.edit_message(embed=embed, view=None)
    resp = await interaction.original_response()
    try:
        return await interaction.channel.fetch_message(resp.id)
    except Exception:
        return resp


# ─────────────────────────────────────────────────────────────────────────────
# Embed builders for navigation screens
# ─────────────────────────────────────────────────────────────────────────────
def _embed_vps_select(logo: str) -> discord.Embed:
    e = discord.Embed(
        title="🖥️  Template Installer",
        description="Select the **VPS** you want to install software on.",
        color=0x5865F2,
    )
    _set_footer(e, logo)
    return e


def _embed_template_select(container: str, logo: str) -> discord.Embed:
    e = discord.Embed(
        title="📦  Select Template",
        description=f"Installing on: `{container}`\n\nChoose a template to install.",
        color=0x5865F2,
    )
    e.add_field(
        name="🦖  Pterodactyl",
        value="Game server panel + Wings daemon. Choose **Panel** or **Wings** independently.",
        inline=False,
    )
    e.add_field(
        name="☁️  Cloudflare Tunnel",
        value="Zero Trust tunnel via cloudflared — expose services without opening ports.",
        inline=False,
    )
    _set_footer(e, logo)
    return e


def _embed_ptero_component(container: str, logo: str) -> discord.Embed:
    e = discord.Embed(
        title="🦖  Select Pterodactyl Component",
        description=(
            f"Installing on: `{container}`\n\n"
            "**Panel** and **Wings** are independent — install them separately.\n"
            "For the no-public-IP setup, click **🪽 Wings** — a step-by-step "
            "guide will walk you through Cloudflare Tunnel setup and the bot "
            "will handle everything else automatically."
        ),
        color=0x5865F2,
    )
    e.add_field(
        name="🖥️  Panel",
        value="Web interface + database + API. Full game panel management.",
        inline=False,
    )
    e.add_field(
        name="🪽  Wings",
        value=(
            "Game server daemon. Click to see the step-by-step guide.\n"
            "Cloudflare Tunnel path: **fully automated** — no public IP, "
            "no SSL certs, no manual YAML."
        ),
        inline=False,
    )
    _set_footer(e, logo)
    return e


def _embed_https_question(container: str, component: str, domain: str, logo: str) -> discord.Embed:
    e = discord.Embed(
        title=f"🔒  Enable HTTPS for {component}?",
        description=(
            f"Container: `{container}`\n"
            f"Domain: `{domain}`\n\n"
            "Do you want to enable HTTPS for this installation?"
        ),
        color=0x5865F2,
    )
    e.add_field(name="✅  Yes", value="Install with HTTPS. You'll choose the SSL method next.", inline=True)
    e.add_field(name="🚫  No",  value="Install with HTTP only (port 80).", inline=True)
    _set_footer(e, logo)
    return e


def _embed_ssl_method(container: str, component: str, domain: str, logo: str) -> discord.Embed:
    e = discord.Embed(
        title="📜  Select SSL Method",
        description=(
            f"Container: `{container}`\n"
            f"Domain: `{domain}`\n\n"
            "How would you like to obtain your SSL certificate?"
        ),
        color=0x5865F2,
    )
    e.add_field(
        name="🔐  Let's Encrypt",
        value="Automatically obtain a free certificate. Domain must point to this server.",
        inline=False,
    )
    e.add_field(
        name="📋  Existing Certificate",
        value="Provide the paths to a certificate and private key already on the server.",
        inline=False,
    )
    _set_footer(e, logo)
    return e


# ─────────────────────────────────────────────────────────────────────────────
# Pterodactyl Application API helpers  (used by Wings auto-connect)
# ─────────────────────────────────────────────────────────────────────────────
async def _panel_api(
    method:    str,
    panel_url: str,
    api_key:   str,
    path:      str,
    data:      dict = None,
) -> tuple[int, dict | str]:
    """
    Call the Pterodactyl Application API.
    Returns (http_status, body).
    body is a dict on JSON responses, or a plain str for YAML/text responses
    (e.g. /nodes/{id}/configuration returns YAML as text/plain).
    """
    url = panel_url.rstrip("/") + "/api/application/" + path.lstrip("/")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    try:
        async with aiohttp.ClientSession() as session:
            req = {
                "GET": session.get,
                "POST": session.post,
                "DELETE": session.delete,
            }.get(method.upper())
            if req is None:
                return 0, {"error": f"Unsupported Panel API method: {method}"}
            async with req(url, headers=headers, json=data, ssl=False) as resp:
                ct = resp.headers.get("Content-Type", "")
                if "json" in ct:
                    return resp.status, await resp.json(content_type=None)
                else:
                    return resp.status, await resp.text()
    except Exception as exc:
        return 0, {"error": str(exc)}


def _panel_error(body: dict | str) -> str:
    """Turn Pterodactyl's nested validation response into readable text."""
    if isinstance(body, dict):
        errors = body.get("errors")
        if isinstance(errors, list):
            parts = []
            for item in errors:
                if isinstance(item, dict):
                    detail = item.get("detail") or item.get("code") or str(item)
                    source = (item.get("meta") or {}).get("source_field")
                    parts.append(f"{source}: {detail}" if source else str(detail))
            if parts:
                return "; ".join(parts)
        if body.get("error"):
            return str(body["error"])
    return str(body)


def _vps_limits(container: str) -> tuple[int, int]:
    """Return safe Panel node limits from the VPS record.

    Pterodactyl rejects zero memory/disk limits.  Older records may contain
    values such as ``4GB`` or just ``4``; invalid/missing values are rejected
    by the caller rather than silently sent as zero.
    """
    if not _vps_data:
        return 0, 0
    for records in _vps_data.values():
        for record in records:
            if record.get("container_name") != container:
                continue
            def _whole_gb(value) -> int:
                match = re.search(r"\d+(?:\.\d+)?", str(value or ""))
                return int(float(match.group(0))) if match else 0
            return _whole_gb(record.get("ram")), _whole_gb(
                record.get("storage", record.get("disk"))
            )
    return 0, 0


def _find_vps_record(container: str) -> Optional[dict]:
    if not _vps_data:
        return None
    for records in _vps_data.values():
        for record in records:
            if record.get("container_name") == container:
                return record
    return None


def _embed_panel_connect(container: str, logo: str) -> discord.Embed:
    e = discord.Embed(
        title="🤖  Automatic Wings Setup",
        description=(
            "The bot will generate a self-signed SSL cert, install Wings in **HTTPS mode**, "
            "create or reuse the Panel location and node, write the generated `config.yml`, and start Wings.\n\n"
            "**Before continuing:**\n"
            "• Add `wings.yourdomain.com` to the Cloudflare Tunnel → "
            "service **HTTPS** → `localhost:8080` → ✅ **No TLS Verify**\n"
            "• Make sure your Panel is already installed and reachable\n"
            "• Create an Admin Application API key in Panel → Admin → Application API"
        ),
        color=0x5865F2,
    )
    e.add_field(name="🖥️  VPS", value=f"`{container}`", inline=True)
    e.add_field(
        name="✅  No manual Wings work",
        value="No manual node creation, YAML copying, certificate setup, or public IP is required.",
        inline=False,
    )
    _set_footer(e, logo)
    return e


def _embed_cloudflare_info(container: str, logo: str) -> discord.Embed:
    """Standalone Cloudflare Tunnel install (no Wings). Shown from the template select screen."""
    e = discord.Embed(
        title="☁️  Cloudflare Tunnel — Install Only",
        description=(
            f"Installing on: `{container}`\n\n"
            "Installs `cloudflared` as a systemd service so you can expose any local "
            "service through a Cloudflare hostname **without a public IP**.\n\n"
            "⚠️  **Want Wings + Cloudflare together?**\n"
            "Use **🦖 Pterodactyl → 🪽 Wings** instead — it runs the full automated "
            "setup in one shot."
        ),
        color=0x000000,
    )
    e.add_field(
        name="📋  Before clicking Enter Tunnel Token",
        value=(
            "1. Open **[one.dash.cloudflare.com](https://one.dash.cloudflare.com)** "
            "→ **Networks → Tunnels**.\n"
            "2. Click **Create a Tunnel** → **Cloudflared** → name it → **Save**.\n"
            "3. Copy the tunnel token from the **Install connector** screen "
            "(the `eyJh…` string after `--token`).\n"
            "4. In the tunnel, add your **Public Hostnames** as needed "
            "(e.g. `panel.yourdomain.com → HTTP → localhost:80`).\n"
            "5. Click **Enter Tunnel Token** below and paste the token."
        ),
        inline=False,
    )
    e.add_field(
        name="✅  What gets installed",
        value=(
            "`cloudflared` as a systemd service\n"
            "Auto-starts on reboot — tunnel is always connected"
        ),
        inline=False,
    )
    _set_footer(e, logo)
    return e


def _wings_cloudflare_guide_embed(container: str, logo: str) -> discord.Embed:
    """
    Step-by-step Wings setup guide shown before the modal.
    Covers everything the user needs to do in Cloudflare dashboard
    before clicking I'm Ready.
    """
    e = discord.Embed(
        title="🪽  Wings — Step-by-Step Setup Guide",
        description=(
            f"Installing on: `{container}`\n\n"
            "The **recommended path** is fully automated via Cloudflare Tunnel.\n"
            "No public IP, no port forwarding, no SSL certificates needed.\n\n"
            "**Complete the steps below in the Cloudflare dashboard first, "
            "then click** ✅ **I'm Ready below.**"
        ),
        color=0xF6821F,
    )
    e.add_field(
        name="📋  What you need before starting",
        value=(
            "• A **Cloudflare account** (free tier works) → "
            "[dash.cloudflare.com](https://dash.cloudflare.com)\n"
            "• A **domain** managed by Cloudflare (e.g. `example.com`)\n"
            "• A **Pterodactyl Panel** already installed and reachable\n"
            "• An **Admin Application API key** from your Panel"
        ),
        inline=False,
    )
    e.add_field(
        name="1️⃣  Open Cloudflare Zero Trust",
        value=(
            "Go to **[one.dash.cloudflare.com](https://one.dash.cloudflare.com)**\n"
            "In the left sidebar: **Networks → Tunnels**"
        ),
        inline=False,
    )
    e.add_field(
        name="2️⃣  Create a new tunnel",
        value=(
            "Click **Create a tunnel** → select **Cloudflared** → click **Next**\n"
            "• Name it anything, e.g. `wings-vps1`\n"
            "• Click **Save tunnel**"
        ),
        inline=False,
    )
    e.add_field(
        name="3️⃣  Copy the tunnel token",
        value=(
            "Cloudflare shows an **Install connector** screen with a command like:\n"
            "```\nsudo cloudflared service install eyJhIjoiN…\n```\n"
            "Copy **only the long token** at the very end — the `eyJh…` part.\n"
            "✅ Correct: `eyJhIjoiNTVkOTVlMzRkNGM4NGYzNWJkOTkxNDI0…`\n"
            "❌ Wrong: `sudo cloudflared service install eyJh…`"
        ),
        inline=False,
    )
    e.add_field(
        name="4️⃣  Add a Public Hostname for Wings",
        value=(
            "In your tunnel → **Public Hostnames** tab → **Add a public hostname**:\n\n"
            "| Field | Value |\n"
            "|-------|-------|\n"
            "| **Subdomain** | `wings` *(or any name)* |\n"
            "| **Domain** | `example.com` *(your domain)* |\n"
            "| **Type** | `HTTPS` |\n"
            "| **URL** | `localhost:8080` |\n\n"
            "⚠️ **Important — expand Additional application settings:**\n"
            "→ **TLS** section → enable **No TLS Verify**\n"
            "*(Wings uses a self-signed cert — this tells Cloudflare to accept it)*\n\n"
            "Click **Save hostname**.\n"
            "Your Wings hostname will be `https://wings.example.com`."
        ),
        inline=False,
    )
    e.add_field(
        name="5️⃣  Get your Panel Admin API key",
        value=(
            "In your **Pterodactyl Panel** → **Admin** → **Application API** → "
            "**Create new key**\n"
            "Copy the key — it starts with `ptla_`."
        ),
        inline=False,
    )
    e.add_field(
        name="6️⃣  Come back here and click I'm Ready",
        value=(
            "Click **✅ I'm Ready — Set Up Wings Automatically** below.\n"
            "You will be asked for **4 things**:\n\n"
            "• **Wings Hostname** — the subdomain from step 4 (e.g. `wings.example.com`)\n"
            "• **Tunnel Token** — the `eyJh…` string from step 3\n"
            "• **Panel URL** — e.g. `https://panel.example.com`\n"
            "• **Panel Admin API Key** — the `ptla_` key from step 5"
        ),
        inline=False,
    )
    e.add_field(
        name="🤖  What the bot does automatically",
        value=(
            "① Installs `cloudflared` on your VPS → Wings tunnel online\n"
            "② Downloads and installs the Wings daemon binary\n"
            "③ Generates a self-signed SSL cert → `/etc/certs/fullchain.pem` + `privkey.pem`\n"
            "④ Creates a location + node in your Panel via the API\n"
            "⑤ Writes the Panel-generated config to `/etc/pterodactyl/config.yml`\n"
            "⑥ Starts Wings — the node turns **🟢 green** in your Panel"
        ),
        inline=False,
    )
    _set_footer(e, logo)
    return e


# ─────────────────────────────────────────────────────────────────────────────
# UI — Step 1: VPS dropdown
# ─────────────────────────────────────────────────────────────────────────────
class _VPSDropdown(discord.ui.Select):
    def __init__(self, vps_list: list[dict]):
        # Index by position, not by container_name.
        # Using the container name as the Discord Select value causes a
        # ValueError (duplicate values) when two VPS share the same container
        # name (e.g. after a race-condition double-deploy).  Using the list
        # index is always unique and avoids any name-collision crashes that
        # would prevent the interaction from responding at all.
        self._vps_list = vps_list
        options = []
        for idx, vps in enumerate(vps_list[:25]):
            name   = vps.get("container_name", "?")
            status = vps.get("status", "unknown")
            emoji  = "🟢" if status == "running" else "🔴"
            ram    = vps.get("ram", "?")
            cpu    = vps.get("cpu", "?")
            node   = vps.get("node_id", "local")
            label  = name[:95]
            desc   = f"RAM: {ram}  •  CPU: {cpu}  •  {node}  •  {status}"[:100]
            options.append(discord.SelectOption(
                label=label,
                description=desc,
                value=str(idx),   # always unique — safe even with duplicate container names
                emoji=emoji,
            ))
        super().__init__(placeholder="Select a VPS…", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        idx      = int(self.values[0])
        vps_info = self._vps_list[idx]
        container = vps_info.get("container_name", "?")
        logo      = _logo()
        await interaction.response.edit_message(
            embed=_embed_template_select(container, logo),
            view=_TemplateSelectView(container, vps_info),
        )


class _VPSSelectView(discord.ui.View):
    def __init__(self, vps_list: list[dict]):
        super().__init__(timeout=300)
        self.add_item(_VPSDropdown(vps_list))


# ─────────────────────────────────────────────────────────────────────────────
# UI — Step 2: Template select  ← ADD NEW TEMPLATE BUTTONS HERE
# ─────────────────────────────────────────────────────────────────────────────
class _TemplateSelectView(discord.ui.View):
    """One button per template + Back."""

    def __init__(self, container: str, vps_info: dict):
        super().__init__(timeout=300)
        self._container = container
        self._vps_info  = vps_info

    @discord.ui.button(label="🦖 Pterodactyl", style=discord.ButtonStyle.primary, row=0)
    async def pterodactyl_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        logo = _logo()
        await interaction.response.edit_message(
            embed=_embed_ptero_component(self._container, logo),
            view=_PterodactylComponentView(self._container, self._vps_info),
        )

    @discord.ui.button(label="☁️ Cloudflare Tunnel", style=discord.ButtonStyle.secondary, row=0)
    async def cloudflare_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        logo = _logo()
        await interaction.response.edit_message(
            embed=_embed_cloudflare_info(self._container, logo),
            view=_CloudflareStartView(self._container, self._vps_info),
        )

    @discord.ui.button(label="↩ Back", style=discord.ButtonStyle.danger, row=1)
    async def back_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        user_id  = str(interaction.user.id)
        vps_list = (_vps_data or {}).get(user_id, [])
        await interaction.response.edit_message(
            embed=_embed_vps_select(_logo()),
            view=_VPSSelectView(vps_list),
        )


# ─────────────────────────────────────────────────────────────────────────────
# UI — Pterodactyl component select (Panel vs Wings)
# ─────────────────────────────────────────────────────────────────────────────
class _PterodactylComponentView(discord.ui.View):
    def __init__(self, container: str, vps_info: dict):
        super().__init__(timeout=300)
        self._container = container
        self._vps_info  = vps_info

    @discord.ui.button(label="🖥️ Panel", style=discord.ButtonStyle.primary, row=0)
    async def panel_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        await interaction.response.send_modal(
            _PanelDomainModal(self._container, self._vps_info)
        )

    @discord.ui.button(label="🪽 Wings", style=discord.ButtonStyle.secondary, row=0)
    async def wings_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        logo = _logo()
        await interaction.response.edit_message(
            embed=_wings_cloudflare_guide_embed(self._container, logo),
            view=_WingsSetupGuideView(self._container, self._vps_info),
        )

    @discord.ui.button(label="↩ Back", style=discord.ButtonStyle.danger, row=1)
    async def back_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_embed_template_select(self._container, _logo()),
            view=_TemplateSelectView(self._container, self._vps_info),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Wings — Setup Guide View + Automated Modal
# The main entry point when user clicks "🪽 Wings" from the component selector.
# Shows the step-by-step Cloudflare guide.  "I'm Ready" opens a 2-field modal
# (Wings hostname + tunnel token) → installs the tunnel → then shows a button
# to connect Wings to Panel (which asks Panel URL + API key in that step).
# ─────────────────────────────────────────────────────────────────────────────
class _WingsSetupGuideView(discord.ui.View):
    """
    Shown alongside the step-by-step Wings setup guide.
    'I'm Ready' opens the automated modal (Wings hostname + tunnel token only).
    'Manual Setup' falls back to the classic domain/SSL flow.
    """

    def __init__(self, container: str, vps_info: dict):
        super().__init__(timeout=600)
        self._container = container
        self._vps_info  = vps_info

    @discord.ui.button(
        label="✅  I'm Ready — Set Up Wings Automatically",
        style=discord.ButtonStyle.success,
        row=0,
    )
    async def ready_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        await interaction.response.send_modal(
            _WingsAutomatedModal(self._container, self._vps_info)
        )

    @discord.ui.button(
        label="🔧  Manual Setup (own domain / SSL)",
        style=discord.ButtonStyle.secondary,
        row=0,
    )
    async def manual_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        await interaction.response.send_modal(
            _WingsDomainModal(self._container, self._vps_info)
        )

    @discord.ui.button(label="↩ Back", style=discord.ButtonStyle.danger, row=1)
    async def back_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_embed_ptero_component(self._container, _logo()),
            view=_PterodactylComponentView(self._container, self._vps_info),
        )


class _WingsAutomatedModal(discord.ui.Modal, title="🪽  Wings — Automated Setup"):
    """
    Collects only the Wings hostname.
    Assumes the Cloudflare Tunnel is already installed (via /template → Cloudflare
    Tunnel or /node tunnel).  Installs Wings with a self-signed SSL cert, then
    shows the "Connect Wings to Panel" button to collect Panel URL + API key.
    """

    wings_hostname = discord.ui.TextInput(
        label="Wings Hostname",
        placeholder="wings.example.com  (the hostname you routed in Cloudflare)",
        min_length=4,
        max_length=253,
    )

    def __init__(self, container: str, vps_info: dict):
        super().__init__()
        self._container = container
        self._vps_info  = vps_info

    async def on_submit(self, interaction: discord.Interaction):
        logo         = _logo()
        node_id      = self._vps_info.get("node_id")
        wings_domain = (
            self.wings_hostname.value.strip().lower()
            .removeprefix("https://")
            .removeprefix("http://")
            .rstrip("/")
        )

        embed = _progress_embed("🚀  Installing Wings", WINGS_STEPS, 0, logo)
        msg   = await _start_progress(interaction, embed)
        # Cloudflare Tunnel is already running — install Wings directly with a
        # self-signed cert (Cloudflare handles external TLS).  The Panel connect
        # button is shown after Wings installs.
        asyncio.create_task(_run_wings_installation(
            interaction.user,
            self._container,
            wings_domain,
            "localhost-ssl",
            "",
            "",
            msg,
            logo,
            node_id,
            post_success_view=_CloudflareWingsSetupView(
                self._container, self._vps_info, wings_domain
            ),
        ))


# ─────────────────────────────────────────────────────────────────────────────
# HTTPS / SSL views  —  generic, reused by both Panel and Wings flows
# ─────────────────────────────────────────────────────────────────────────────
class _HTTPSView(discord.ui.View):
    """Yes / No HTTPS question. Subclasses supply _on_yes and _on_no."""

    def __init__(self, container: str, vps_info: dict, domain: str, component: str):
        super().__init__(timeout=300)
        self._container = container
        self._vps_info  = vps_info
        self._domain    = domain
        self._component = component

    async def _on_yes(self, interaction: discord.Interaction) -> None:
        raise NotImplementedError

    async def _on_no(self, interaction: discord.Interaction) -> None:
        raise NotImplementedError

    @discord.ui.button(label="✅ Yes — Enable HTTPS", style=discord.ButtonStyle.success, row=0)
    async def yes_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        await self._on_yes(interaction)

    @discord.ui.button(label="🚫 No — HTTP only", style=discord.ButtonStyle.secondary, row=0)
    async def no_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        await self._on_no(interaction)


class _SSLMethodView(discord.ui.View):
    """Let's Encrypt / Existing Certificate selection. Subclasses supply handlers."""

    def __init__(self, container: str, vps_info: dict, domain: str, component: str):
        super().__init__(timeout=300)
        self._container = container
        self._vps_info  = vps_info
        self._domain    = domain
        self._component = component

    async def _on_letsencrypt(self, interaction: discord.Interaction) -> None:
        raise NotImplementedError

    async def _on_existing(self, interaction: discord.Interaction) -> None:
        raise NotImplementedError

    @discord.ui.button(label="🔐 Let's Encrypt", style=discord.ButtonStyle.primary, row=0)
    async def le_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        await self._on_letsencrypt(interaction)

    @discord.ui.button(label="📋 Existing Certificate", style=discord.ButtonStyle.secondary, row=0)
    async def existing_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        await self._on_existing(interaction)


# ─────────────────────────────────────────────────────────────────────────────
# Panel flow:  Domain → HTTPS? → SSL Method → [cert paths] → Install
# ─────────────────────────────────────────────────────────────────────────────
class _PanelDomainModal(discord.ui.Modal, title="🦖  Panel — Domain"):
    domain = discord.ui.TextInput(
        label="Panel Domain",
        placeholder="panel.yourdomain.com  (no https://)",
        min_length=4, max_length=253,
    )

    def __init__(self, container: str, vps_info: dict):
        super().__init__()
        self._container = container
        self._vps_info  = vps_info

    async def on_submit(self, interaction: discord.Interaction):
        domain = self.domain.value.strip().lower().removeprefix("https://").removeprefix("http://").rstrip("/")
        await interaction.response.edit_message(
            embed=_embed_https_question(self._container, "Panel", domain, _logo()),
            view=_PanelHTTPSView(self._container, self._vps_info, domain),
        )


class _PanelHTTPSView(_HTTPSView):
    def __init__(self, container: str, vps_info: dict, domain: str):
        super().__init__(container, vps_info, domain, "Panel")

    async def _on_yes(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            embed=_embed_ssl_method(self._container, "Panel", self._domain, _logo()),
            view=_PanelSSLMethodView(self._container, self._vps_info, self._domain),
        )

    async def _on_no(self, interaction: discord.Interaction):
        logo    = _logo()
        node_id = self._vps_info.get("node_id")
        embed   = _progress_embed("🚀  Installing Pterodactyl Panel", PANEL_STEPS, 0, logo)
        msg     = await _start_progress(interaction, embed)
        asyncio.create_task(_run_panel_installation(
            interaction.user, self._container, self._domain, "http", "", "", msg, logo, node_id
        ))


class _PanelSSLMethodView(_SSLMethodView):
    def __init__(self, container: str, vps_info: dict, domain: str):
        super().__init__(container, vps_info, domain, "Panel")

    async def _on_letsencrypt(self, interaction: discord.Interaction):
        logo    = _logo()
        node_id = self._vps_info.get("node_id")
        embed   = _progress_embed("🚀  Installing Pterodactyl Panel", PANEL_STEPS, 0, logo)
        msg     = await _start_progress(interaction, embed)
        asyncio.create_task(_run_panel_installation(
            interaction.user, self._container, self._domain, "letsencrypt", "", "", msg, logo, node_id
        ))

    async def _on_existing(self, interaction: discord.Interaction):
        await interaction.response.send_modal(
            _PanelCustomCertModal(self._container, self._vps_info, self._domain)
        )


class _PanelCustomCertModal(discord.ui.Modal, title="📜  Panel — Certificate Paths"):
    cert_path = discord.ui.TextInput(
        label="Certificate Path (fullchain.pem)",
        placeholder="/etc/ssl/certs/panel/fullchain.pem",
        min_length=5, max_length=500,
    )
    key_path = discord.ui.TextInput(
        label="Private Key Path (privkey.pem)",
        placeholder="/etc/ssl/certs/panel/privkey.pem",
        min_length=5, max_length=500,
    )

    def __init__(self, container: str, vps_info: dict, domain: str):
        super().__init__()
        self._container = container
        self._vps_info  = vps_info
        self._domain    = domain

    async def on_submit(self, interaction: discord.Interaction):
        logo    = _logo()
        node_id = self._vps_info.get("node_id")
        embed   = _progress_embed("🚀  Installing Pterodactyl Panel", PANEL_STEPS, 0, logo)
        msg     = await _start_progress(interaction, embed)
        asyncio.create_task(_run_panel_installation(
            interaction.user,
            self._container,
            self._domain,
            "custom",
            self.cert_path.value.strip(),
            self.key_path.value.strip(),
            msg,
            logo,
            node_id,
        ))


# ─────────────────────────────────────────────────────────────────────────────
# Wings flow:  Domain → HTTPS? → SSL Method → [cert paths] → Install
# ─────────────────────────────────────────────────────────────────────────────
class _WingsDomainModal(discord.ui.Modal, title="🪽  Wings — Domain"):
    domain = discord.ui.TextInput(
        label="Wings Domain",
        placeholder="wings.yourdomain.com  (no https://)",
        min_length=4, max_length=253,
    )

    def __init__(self, container: str, vps_info: dict):
        super().__init__()
        self._container = container
        self._vps_info  = vps_info

    async def on_submit(self, interaction: discord.Interaction):
        domain = self.domain.value.strip().lower().removeprefix("https://").removeprefix("http://").rstrip("/")
        await interaction.response.edit_message(
            embed=_embed_panel_connect(self._container, _logo()),
            view=_WingsPanelConnectView(
                self._container, self._vps_info, domain, "http", "", ""
            ),
        )


class _WingsHTTPSView(_HTTPSView):
    def __init__(self, container: str, vps_info: dict, domain: str):
        super().__init__(container, vps_info, domain, "Wings")

    async def _on_yes(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            embed=_embed_ssl_method(self._container, "Wings", self._domain, _logo()),
            view=_WingsSSLMethodView(self._container, self._vps_info, self._domain),
        )

    async def _on_no(self, interaction: discord.Interaction):
        logo = _logo()
        await interaction.response.edit_message(
            embed=_embed_panel_connect(self._container, logo),
            view=_WingsPanelConnectView(self._container, self._vps_info, self._domain, "http", "", ""),
        )


class _WingsSSLMethodView(_SSLMethodView):
    def __init__(self, container: str, vps_info: dict, domain: str):
        super().__init__(container, vps_info, domain, "Wings")

    async def _on_letsencrypt(self, interaction: discord.Interaction):
        logo = _logo()
        await interaction.response.edit_message(
            embed=_embed_panel_connect(self._container, logo),
            view=_WingsPanelConnectView(self._container, self._vps_info, self._domain, "letsencrypt", "", ""),
        )

    async def _on_existing(self, interaction: discord.Interaction):
        await interaction.response.send_modal(
            _WingsCustomCertModal(self._container, self._vps_info, self._domain)
        )


class _WingsCustomCertModal(discord.ui.Modal, title="📜  Wings — Certificate Paths"):
    cert_path = discord.ui.TextInput(
        label="Certificate Path (fullchain.pem)",
        placeholder="/etc/ssl/certs/wings/fullchain.pem",
        min_length=5, max_length=500,
    )
    key_path = discord.ui.TextInput(
        label="Private Key Path (privkey.pem)",
        placeholder="/etc/ssl/certs/wings/privkey.pem",
        min_length=5, max_length=500,
    )

    def __init__(self, container: str, vps_info: dict, domain: str):
        super().__init__()
        self._container = container
        self._vps_info  = vps_info
        self._domain    = domain

    async def on_submit(self, interaction: discord.Interaction):
        logo = _logo()
        cert = self.cert_path.value.strip()
        key  = self.key_path.value.strip()
        # Modal submit → send_message (can't edit_message from a modal response)
        await interaction.response.send_message(
            embed=_embed_panel_connect(self._container, logo),
            view=_WingsPanelConnectView(self._container, self._vps_info, self._domain, "custom", cert, key),
            ephemeral=True,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Wings — Panel auto-connect screen
# ─────────────────────────────────────────────────────────────────────────────
class _WingsPanelConnectView(discord.ui.View):
    """
    Shown after the Wings hostname is supplied. The supported Cloudflare path
    is automatic: collect Panel URL + API key, then install and connect Wings.
    """

    def __init__(
        self,
        container:  str,
        vps_info:   dict,
        domain:     str,
        ssl_mode:   str,
        cert_path:  str,
        key_path:   str,
    ):
        super().__init__(timeout=600)
        self._container = container
        self._vps_info  = vps_info
        self._domain    = domain
        self._ssl_mode  = ssl_mode
        self._cert_path = cert_path
        self._key_path  = key_path

    @discord.ui.button(label="🔌 Connect to Panel", style=discord.ButtonStyle.primary, row=0)
    async def connect_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        await interaction.response.send_modal(
            _WingsPanelModal(
                self._container, self._vps_info, self._domain,
                self._ssl_mode, self._cert_path, self._key_path,
            )
        )

class _WingsPanelModal(discord.ui.Modal, title="🔌  Wings — Panel Connection"):
    panel_url = discord.ui.TextInput(
        label="Panel URL",
        placeholder="https://panel.yourdomain.com",
        min_length=8, max_length=500,
    )
    api_key = discord.ui.TextInput(
        label="Admin API Key",
        placeholder="ptla_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        min_length=10, max_length=500,
    )

    def __init__(
        self,
        container:  str,
        vps_info:   dict,
        domain:     str,
        ssl_mode:   str,
        cert_path:  str,
        key_path:   str,
    ):
        super().__init__()
        self._container = container
        self._vps_info  = vps_info
        self._domain    = domain
        self._ssl_mode  = ssl_mode
        self._cert_path = cert_path
        self._key_path  = key_path

    async def on_submit(self, interaction: discord.Interaction):
        logo    = _logo()
        node_id = self._vps_info.get("node_id")
        embed   = _progress_embed("🚀  Installing Wings", WINGS_STEPS, 0, logo)
        msg     = await _start_progress(interaction, embed)
        asyncio.create_task(_run_wings_installation(
            interaction.user, self._container, self._domain,
            self._ssl_mode, self._cert_path, self._key_path,
            msg, logo, node_id,
            panel_url=self.panel_url.value.strip().rstrip("/"),
            panel_api_key=self.api_key.value.strip(),
        ))


# ─────────────────────────────────────────────────────────────────────────────
# Cloudflare flow:  Enter Token → Install
# ─────────────────────────────────────────────────────────────────────────────
class _CloudflareStartView(discord.ui.View):
    def __init__(self, container: str, vps_info: dict):
        super().__init__(timeout=300)
        self._container = container
        self._vps_info  = vps_info

    @discord.ui.button(label="☁️ Enter Tunnel Token", style=discord.ButtonStyle.primary, row=0)
    async def setup_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        await interaction.response.send_modal(
            _CloudflareTunnelModal(self._container, self._vps_info)
        )

    @discord.ui.button(label="↩ Back", style=discord.ButtonStyle.danger, row=1)
    async def back_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_embed_template_select(self._container, _logo()),
            view=_TemplateSelectView(self._container, self._vps_info),
        )


class _CloudflareTunnelModal(discord.ui.Modal, title="☁️  Cloudflare Tunnel Token"):
    """Only asks for what's needed to install the tunnel itself.
    Panel URL is collected in the Panel setup flow; Wings Hostname is collected
    in the Wings setup flow — no need to ask for them here."""

    tunnel_name = discord.ui.TextInput(
        label="Tunnel Name",
        placeholder="my-vps",
        min_length=1, max_length=64,
    )
    token = discord.ui.TextInput(
        label="Tunnel Token",
        placeholder="Paste your Cloudflare tunnel token here…",
        min_length=20, max_length=2000,
        style=discord.TextStyle.paragraph,
    )

    def __init__(self, container: str, vps_info: dict):
        super().__init__()
        self._container = container
        self._vps_info  = vps_info

    async def on_submit(self, interaction: discord.Interaction):
        logo    = _logo()
        node_id = self._vps_info.get("node_id")
        embed   = _progress_embed("🚀  Installing Cloudflare Tunnel", CLOUDFLARE_STEPS, 0, logo)
        msg     = await _start_progress(interaction, embed)
        asyncio.create_task(_run_cloudflare_installation(
            interaction.user,
            self._container,
            self.tunnel_name.value.strip(),
            self.token.value.strip(),
            msg,
            logo,
            node_id,
            self._vps_info,
            # wings_domain / panel_url / panel_api_key are empty here — the
            # installation sends a DM guide for the next steps, and the Wings
            # setup flow collects those values when the user installs Wings.
        ))


class _CloudflareWingsSetupView(discord.ui.View):
    """
    Shown after Cloudflare Tunnel installs successfully (Wings path).
    The button opens _WingsPanelModal which asks for Panel URL + Admin API key,
    then installs Wings with a self-signed cert (localhost-ssl) and auto-connects
    to Panel.  SSL is handled by the Cloudflare Tunnel, not Wings directly.
    """

    def __init__(self, container: str, vps_info: dict, wings_domain: str):
        super().__init__(timeout=900)
        self._container = container
        self._vps_info = vps_info
        self._wings_domain = wings_domain

    @discord.ui.button(
        label="🤖 Configure Wings automatically",
        style=discord.ButtonStyle.success,
        row=0,
    )
    async def configure_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        await interaction.response.send_modal(
            _WingsPanelModal(
                self._container,
                self._vps_info,
                self._wings_domain,
                "localhost-ssl",   # self-signed cert; Cloudflare Tunnel handles external TLS
                "",
                "",
            )
        )


# ─────────────────────────────────────────────────────────────────────────────
# Panel installation runner  —  ONLY installs Panel, never Wings
# ─────────────────────────────────────────────────────────────────────────────
async def _run_panel_installation(
    user:      discord.User | discord.Member,
    container: str,
    domain:    str,
    ssl_mode:  str,   # "http" | "letsencrypt" | "custom"
    cert_path: str,
    key_path:  str,
    msg:       discord.Message,
    logo:      str,
    node_id:   str = None,
) -> None:
    title = "🚀  Installing Pterodactyl Panel"
    steps = PANEL_STEPS
    creds = _gen_panel_creds()
    db_name    = creds["db_name"]
    db_user    = creds["db_user"]
    db_pass    = creds["db_pass"]
    admin_user = creds["admin_user"]
    admin_email = creds["admin_email"]
    admin_pass  = creds["admin_pass"]
    app_url = f"https://{domain}" if ssl_mode != "http" else f"http://{domain}"

    _exec = _make_exec(container, node_id)

    async def _upd(idx: int, failed_at: int = -1, error: str = "") -> None:
        try:
            await msg.edit(embed=_progress_embed(title, steps, idx, logo, failed_at, error))
        except Exception as e:
            log.warning(f"[panel] embed update failed: {e}")

    _step = _make_step(_exec, _upd, steps, "panel")

    # 0 — Preparing System
    if not await _step(0,
        "export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a NEEDRESTART_SUSPEND=1 && "
        "apt-get update -qq 2>&1 | tail -5", timeout=120): return

    # 1 — Dependencies
    if not await _step(1,
        "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "
        "curl wget tar unzip git software-properties-common "
        "apt-transport-https ca-certificates gnupg2 lsb-release cron 2>&1 | tail -8",
        timeout=240): return

    # 2 — PHP 8.3
    if not await _step(2, "\n".join([
        "LC_ALL=C.UTF-8 add-apt-repository -y ppa:ondrej/php 2>&1 | tail -3",
        "apt-get update -qq 2>&1 | tail -3",
        "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "
        "php8.3 php8.3-cli php8.3-gd php8.3-mysql php8.3-pdo php8.3-mbstring "
        "php8.3-tokenizer php8.3-bcmath php8.3-xml php8.3-fpm php8.3-curl "
        "php8.3-zip php8.3-redis 2>&1 | tail -10",
        "systemctl enable php8.3-fpm 2>&1 || true",
        "systemctl start  php8.3-fpm 2>&1 || true",
    ]), timeout=360): return

    # 3 — MariaDB
    if not await _step(3, "\n".join([
        "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq mariadb-server 2>&1 | tail -5",
        "systemctl enable mariadb 2>&1 || true",
        "systemctl start  mariadb 2>&1 || true",
        'mysql -e "SELECT 1" 2>&1',
    ]), timeout=240): return

    # 4 — Redis
    if not await _step(4, "\n".join([
        "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq redis-server 2>&1 | tail -5",
        "systemctl enable redis-server 2>&1 || true",
        "systemctl start  redis-server 2>&1 || true",
        "redis-cli ping",
    ]), timeout=120): return

    # 5 — Nginx
    if not await _step(5, "\n".join([
        "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nginx 2>&1 | tail -5",
        "systemctl enable nginx 2>&1 || true",
        "systemctl start  nginx 2>&1 || true",
    ]), timeout=120): return

    # 6 — Composer
    if not await _step(6, "\n".join([
        "curl -sS https://getcomposer.org/installer | php -- --install-dir=/usr/local/bin --filename=composer 2>&1 | tail -3",
        "composer --version",
    ]), timeout=120): return

    # 7 — Download Pterodactyl
    if not await _step(7, "\n".join([
        "mkdir -p /var/www/pterodactyl",
        "curl -Lo /var/www/pterodactyl/panel.tar.gz "
        "https://github.com/pterodactyl/panel/releases/latest/download/panel.tar.gz 2>&1 | tail -3",
        "tar -xzf /var/www/pterodactyl/panel.tar.gz -C /var/www/pterodactyl/ 2>&1 | tail -3",
        "rm -f /var/www/pterodactyl/panel.tar.gz",
    ]), timeout=180): return

    # 8 — Composer install (panel files)
    if not await _step(8,
        "cd /var/www/pterodactyl && "
        "COMPOSER_ALLOW_SUPERUSER=1 composer install --no-dev --optimize-autoloader --no-interaction 2>&1 | tail -10",
        timeout=360): return

    # 9 — Configure Database
    # Write SQL to a temp file via base64 to avoid ANY shell interpretation of
    # the database name, username, or password (special chars, $, quotes, etc.)
    sql = (
        f"CREATE DATABASE IF NOT EXISTS `{db_name}`;\n"
        f"CREATE USER IF NOT EXISTS '{db_user}'@'127.0.0.1' IDENTIFIED BY '{db_pass}';\n"
        f"GRANT ALL PRIVILEGES ON `{db_name}`.* TO '{db_user}'@'127.0.0.1';\n"
        "FLUSH PRIVILEGES;\n"
    )
    db_step_cmd = "\n".join([
        _write_file_cmd("/tmp/ptero_db_setup.sql", sql),
        "mysql < /tmp/ptero_db_setup.sql 2>&1",
        "rm -f /tmp/ptero_db_setup.sql",
    ])
    if not await _step(9, db_step_cmd, timeout=30): return

    # 10 — Configure Environment (.env)
    env_cmds = "\n".join([
        "cd /var/www/pterodactyl",
        "cp .env.example .env",
        f"sed -i 's|^APP_URL=.*|APP_URL={app_url}|' .env",
        "sed -i 's|^APP_ENVIRONMENT=.*|APP_ENVIRONMENT=production|' .env",
        "sed -i 's|^APP_DEBUG=.*|APP_DEBUG=false|' .env",
        "sed -i 's|^DB_HOST=.*|DB_HOST=127.0.0.1|' .env",
        "sed -i 's|^DB_PORT=.*|DB_PORT=3306|' .env",
        f"sed -i 's|^DB_DATABASE=.*|DB_DATABASE={db_name}|' .env",
        f"sed -i 's|^DB_USERNAME=.*|DB_USERNAME={db_user}|' .env",
        f"sed -i 's|^DB_PASSWORD=.*|DB_PASSWORD={db_pass}|' .env",
        "sed -i 's|^CACHE_DRIVER=.*|CACHE_DRIVER=redis|' .env",
        "sed -i 's|^SESSION_DRIVER=.*|SESSION_DRIVER=redis|' .env",
        "sed -i 's|^QUEUE_CONNECTION=.*|QUEUE_CONNECTION=redis|' .env",
        "sed -i 's|^REDIS_HOST=.*|REDIS_HOST=127.0.0.1|' .env",
        "sed -i 's|^REDIS_PORT=.*|REDIS_PORT=6379|' .env",
        "php artisan key:generate --force 2>&1 | tail -3",
    ])
    if not await _step(10, env_cmds, timeout=60): return

    # 11 — Migrations
    if not await _step(11,
        "cd /var/www/pterodactyl && php artisan migrate --seed --force 2>&1 | tail -12",
        timeout=240): return

    # 12 — Create Admin Account (auto-generated, no user input)
    # Use shlex.quote for all values so special characters in passwords / emails
    # cannot break out of the argument boundary when bash evaluates this command.
    import shlex as _shlex
    user_cmd = (
        f"cd /var/www/pterodactyl && php artisan p:user:make "
        f"--email={_shlex.quote(admin_email)} "
        f"--username={_shlex.quote(admin_user)} "
        f"--name-first={_shlex.quote(_brand())} "
        f"--name-last=Admin "
        f"--password={_shlex.quote(admin_pass)} "
        f"--admin=1 "
        f"--no-interaction 2>&1 | tail -5"
    )
    if not await _step(12, user_cmd, timeout=60): return

    # 13 — Permissions
    if not await _step(13, "\n".join([
        "chown -R www-data:www-data /var/www/pterodactyl/",
        "chmod -R 755 /var/www/pterodactyl/storage/",
        "chmod -R 755 /var/www/pterodactyl/bootstrap/cache/",
    ]), timeout=60): return

    # 14 — Configure Nginx
    if ssl_mode == "custom":
        nginx_content = _nginx_https(domain, cert_path, key_path)
    else:
        # For HTTP or LE, start with HTTP config (LE will swap it via certbot)
        nginx_content = _nginx_http(domain)

    if not await _step(14, "\n".join([
        _write_file_cmd("/etc/nginx/sites-available/pterodactyl.conf", nginx_content),
        "ln -sf /etc/nginx/sites-available/pterodactyl.conf /etc/nginx/sites-enabled/pterodactyl.conf",
        "rm -f /etc/nginx/sites-enabled/default",
        "nginx -t 2>&1",
        "systemctl reload nginx 2>&1 || systemctl restart nginx 2>&1",
    ]), timeout=60): return

    # 15 — SSL (Let's Encrypt)
    if ssl_mode == "letsencrypt":
        if not await _step(15, "\n".join([
            "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq certbot python3-certbot-nginx 2>&1 | tail -5",
            f"certbot --nginx -d {domain} --non-interactive --agree-tos "
            f"--email {admin_email} --redirect 2>&1 | tail -15",
        ]), timeout=300): return
    else:
        await _upd(16)  # advance past SSL step

    # 16 — Enable Services (queue worker + cron)
    pteroq_svc = "\n".join([
        "[Unit]",
        "Description=Pterodactyl Queue Worker",
        "After=redis-server.service",
        "",
        "[Service]",
        "User=www-data",
        "Group=www-data",
        "Restart=always",
        "StartLimitInterval=180",
        "StartLimitBurst=30",
        "RestartSec=5s",
        f"ExecStart=/usr/bin/php /var/www/pterodactyl/artisan queue:work "
        "--queue=high,standard,low --sleep=3 --tries=3 --max-time=3600",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
    ])
    cron_entry = "* * * * * php /var/www/pterodactyl/artisan schedule:run >> /dev/null 2>&1"
    if not await _step(16, "\n".join([
        _write_file_cmd("/etc/systemd/system/pteroq.service", pteroq_svc),
        "systemctl daemon-reload",
        "systemctl enable --now pteroq 2>&1 || true",
        "systemctl enable cron 2>&1 && systemctl start cron 2>&1 || true",
        f'(crontab -u www-data -l 2>/dev/null; echo "{cron_entry}") | crontab -u www-data -',
    ]), timeout=60): return

    # 17 — Verify
    await _upd(17)
    verify_checks = [
        ("nginx",       "systemctl is-active nginx"),
        ("php8.3-fpm",  "systemctl is-active php8.3-fpm"),
        ("mariadb",     "systemctl is-active mariadb"),
        ("redis",       "systemctl is-active redis-server"),
        ("pteroq",      "systemctl is-active pteroq"),
        # Use --password= with a shell-safe single-quoted value so db_pass
        # special chars (if any) don't get shell-expanded.
        ("db-connect",  f"mysql -u{db_user} -p{db_pass} -h 127.0.0.1 {db_name} -e 'SELECT 1' 2>&1"),
        ("panel-http",  "curl -s -o /dev/null -w '%{http_code}' http://localhost/ 2>&1 "
                        "| grep -qE '^(200|301|302)' && echo OK || echo FAIL"),
    ]
    failures: list[str] = []
    for label, cmd in verify_checks:
        ok, out = await _exec(cmd, timeout=30)
        passed  = ok and (out.strip() in ("active", "OK", "1") or "1" in out)
        if not passed:
            failures.append(f"{label}: {out[:120]}")

    if failures:
        await _upd(17, failed_at=17, error="Verification checks failed:\n" + "\n".join(failures))
        return

    await _upd(len(steps))

    # Completion embed
    done = discord.Embed(
        title="✅  Pterodactyl Panel Installed!",
        description=(
            f"Installation complete on `{container}`.\n\n"
            f"🌐 **Panel URL:** {app_url}\n\n"
            f"> 📬 Your administrator credentials have been sent to your DMs."
        ),
        color=0x57F287,
    )
    _set_footer(done, logo)
    try:
        await msg.edit(embed=done, view=None)
    except Exception:
        pass

    # DM credentials
    try:
        ok_v, ver_out = await _exec(
            "cd /var/www/pterodactyl && composer show pterodactyl/panel 2>/dev/null "
            "| grep versions | head -1 || echo 'latest'",
            timeout=30,
        )
        version = (ver_out or "latest").strip()[:40]

        dm = discord.Embed(
            title="🦖  Pterodactyl Panel — Your Credentials",
            description=(
                "Your Panel has been installed and an administrator account was created automatically.\n"
                "**Keep these credentials safe and private.**"
            ),
            color=0x000000,
        )
        dm.add_field(name="🌐  Panel URL",    value=app_url,               inline=False)
        dm.add_field(name="👤  Username",      value=f"`{admin_user}`",     inline=True)
        dm.add_field(name="📧  Email",         value=f"`{admin_email}`",    inline=True)
        dm.add_field(name="🔑  Password",      value=f"```{admin_pass}```", inline=False)
        dm.add_field(name="🗄️  Database",      value=f"`{db_name}`",        inline=True)
        dm.add_field(name="👤  DB User",       value=f"`{db_user}`",        inline=True)
        dm.add_field(name="🔒  DB Password",   value=f"```{db_pass}```",    inline=False)
        dm.add_field(name="📦  Version",       value=f"`{version}`",        inline=True)
        dm.add_field(name="🖥️  Container",     value=f"`{container}`",      inline=True)
        _set_footer(dm, logo)
        await user.send(embed=dm)
    except discord.Forbidden:
        log.warning(f"[panel] Could not DM credentials to {user}")
    except Exception as e:
        log.error(f"[panel] Credentials DM error: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Wings installation runner  —  ONLY installs Wings, never Panel
# ─────────────────────────────────────────────────────────────────────────────
async def _run_wings_installation(
    user:               discord.User | discord.Member,
    container:          str,
    domain:             str,
    ssl_mode:           str,   # "http" | "letsencrypt" | "custom" | "localhost-ssl"
    cert_path:          str,
    key_path:           str,
    msg:                discord.Message,
    logo:               str,
    node_id:            str = None,
    panel_url:          str = "",   # Pterodactyl Panel URL for auto-connect
    panel_api_key:      str = "",   # Admin Application API key
    post_success_view:  discord.ui.View | None = None,  # shown after install when panel not connected
) -> None:
    title = "🚀  Installing Wings"
    steps = WINGS_STEPS
    _exec = _make_exec(container, node_id)

    async def _upd(idx: int, failed_at: int = -1, error: str = "") -> None:
        try:
            await msg.edit(embed=_progress_embed(title, steps, idx, logo, failed_at, error))
        except Exception as e:
            log.warning(f"[wings] embed update failed: {e}")

    _step = _make_step(_exec, _upd, steps, "wings")

    # 0 — Preparing
    if not await _step(0,
        "export DEBIAN_FRONTEND=noninteractive && apt-get update -qq 2>&1 | tail -5",
        timeout=120): return

    # 1 — Dependencies
    if not await _step(1,
        "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl wget tar 2>&1 | tail -5",
        timeout=120): return

    # 2 — Download Wings binary
    # NOTE: wings does NOT support --version; verify by checking the binary is
    # executable and has a sane file size (the release binary is typically >20 MB).
    arch_cmd = "\n".join([
        'ARCH=$(uname -m)',
        'if [ "$ARCH" = "x86_64" ]; then WINGS_ARCH="amd64";',
        'elif [ "$ARCH" = "aarch64" ]; then WINGS_ARCH="arm64";',
        'else WINGS_ARCH="amd64"; fi',
        'curl -L "https://github.com/pterodactyl/wings/releases/latest/download/wings_linux_${WINGS_ARCH}"'
        ' -o /usr/local/bin/wings 2>&1 | tail -3',
        "chmod +x /usr/local/bin/wings",
        # Confirm the file exists and is a real binary (>1 MB)
        'SIZE=$(stat -c%s /usr/local/bin/wings 2>/dev/null || echo 0)',
        'if [ "$SIZE" -gt 1000000 ]; then echo "wings_downloaded OK size=${SIZE}"; else echo "wings_download_failed size=${SIZE}"; exit 1; fi',
    ])
    if not await _step(2, arch_cmd, timeout=180): return

    # 3 — Create directories
    if not await _step(3, "\n".join([
        "mkdir -p /etc/pterodactyl",
        "mkdir -p /var/lib/pterodactyl/volumes",
        "mkdir -p /var/lib/pterodactyl/archives",
        "mkdir -p /var/lib/pterodactyl/backups",
        "mkdir -p /var/log/pterodactyl",
        "mkdir -p /tmp/pterodactyl",
    ]), timeout=30): return

    # 4 — SSL setup
    ssl_enabled = "false"
    ssl_cert    = ""
    ssl_key     = ""

    if ssl_mode == "localhost-ssl":
        # Self-signed certificate via hopingboyz/localhost-ssl method.
        # Creates a 10-year generic cert — no domain, no CA, no ports needed.
        # Used with Cloudflare Tunnel (origin HTTPS + No TLS Verify).
        localhost_ssl_cmd = "\n".join([
            "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq openssl 2>&1 | tail -3",
            "mkdir -p /etc/certs",
            (
                "openssl req -new -newkey rsa:4096 -days 3650 -nodes -x509 "
                "-subj \"/C=NA/ST=NA/L=NA/O=NA/CN=Generic SSL Certificate\" "
                "-keyout /etc/certs/privkey.pem -out /etc/certs/fullchain.pem 2>&1 | tail -5"
            ),
            "chmod 600 /etc/certs/privkey.pem /etc/certs/fullchain.pem",
            "test -f /etc/certs/fullchain.pem && test -f /etc/certs/privkey.pem "
            "&& echo CERT_OK || echo CERT_FAIL",
        ])
        if not await _step(4, localhost_ssl_cmd, timeout=60): return
        ssl_enabled = "true"
        ssl_cert    = "/etc/certs/fullchain.pem"
        ssl_key     = "/etc/certs/privkey.pem"
    elif ssl_mode == "letsencrypt":
        le_cmd = "\n".join([
            "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq certbot 2>&1 | tail -5",
            f"certbot certonly --standalone -d {domain} --non-interactive --agree-tos "
            f"--email admin@{domain} --preferred-challenges http 2>&1 | tail -10",
            f"test -f /etc/letsencrypt/live/{domain}/fullchain.pem && echo CERT_OK || echo CERT_FAIL",
        ])
        if not await _step(4, le_cmd, timeout=300): return
        ssl_enabled = "true"
        ssl_cert    = f"/etc/letsencrypt/live/{domain}/fullchain.pem"
        ssl_key     = f"/etc/letsencrypt/live/{domain}/privkey.pem"
    elif ssl_mode == "custom":
        check_cmd = f"test -f {cert_path} && test -f {key_path} && echo CERTS_OK || echo CERTS_MISSING"
        ok, out = await _exec(check_cmd, timeout=10)
        if not ok or "CERTS_MISSING" in out:
            await _upd(4, failed_at=4, error=f"Certificate files not found:\n  cert: {cert_path}\n  key:  {key_path}")
            return
        await _upd(5)
        ssl_enabled = "true"
        ssl_cert    = cert_path
        ssl_key     = key_path
    else:
        await _upd(5)  # HTTP — skip SSL step

    scheme = "https" if ssl_mode != "http" else "http"

    # 5 — Write placeholder Wings configuration
    # This placeholder gets replaced in step 7 if the user provided Panel credentials.
    wings_cfg = (
        f"debug: true\n"
        f'app_name: "Pterodactyl Wings"\n'
        f'uuid: ""\n'
        f'token_id: ""\n'
        f'token: ""\n'
        f"api:\n"
        f'  host: "0.0.0.0"\n'
        f"  port: 8080\n"
        f"  ssl:\n"
        f"    enabled: {ssl_enabled}\n"
        f'    cert: "{ssl_cert}"\n'
        f'    key: "{ssl_key}"\n'
        f"  upload_limit: 100\n"
        f"  disable_remote_download: false\n"
        f"system:\n"
        f'  data: "/var/lib/pterodactyl"\n'
        f"  sftp:\n"
        f"    bind_port: 2022\n"
        f"docker:\n"
        f"  network:\n"
        f'    interface: "172.18.0.1"\n'
        f"    name: pterodactyl_nw\n"
        f'  timezone: ""\n'
        f"  use_performant_inspect: true\n"
        f"ignore_panel_config_updates: false\n"
    )
    if not await _step(5, _write_file_cmd("/etc/pterodactyl/config.yml", wings_cfg), timeout=15): return

    # 6 — Install systemd service (uses --debug so logs are verbose and visible)
    wings_svc = "\n".join([
        "[Unit]",
        "Description=Pterodactyl Wings Daemon",
        "After=docker.service",
        "Requires=docker.service",
        "PartOf=docker.service",
        "",
        "[Service]",
        "User=root",
        "WorkingDirectory=/etc/pterodactyl",
        "LimitNOFILE=4096",
        "ExecStart=/usr/local/bin/wings --debug",
        "Restart=on-failure",
        "StartLimitInterval=180",
        "StartLimitBurst=30",
        "RestartSec=5s",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
    ])
    if not await _step(6, "\n".join([
        _write_file_cmd("/etc/systemd/system/wings.service", wings_svc),
        "systemctl daemon-reload",
        "systemctl enable wings 2>&1",
    ]), timeout=30): return

    # 7 — Connect to Panel (reuse/create location + node, write real config)
    panel_connected = False
    panel_node_label = ""
    if panel_url and panel_api_key:
        await _upd(7)
        try:
            node_memory, node_disk = _vps_limits(container)
            if node_memory < 1 or node_disk < 1:
                await _upd(
                    7,
                    failed_at=7,
                    error=(
                        "This VPS has invalid resource limits for Panel node creation. "
                        f"Panel requires memory >= 1 MB and disk >= 1 MB; "
                        f"the VPS record is RAM={node_memory} GB, disk={node_disk} GB. "
                        "Update the VPS record/specs and run Wings again."
                    ),
                )
                return

            # 7a — Create location
            location_id = None
            location_created = False
            status, locations_body = await _panel_api(
                "GET", panel_url, panel_api_key, "locations",
            )
            if status == 200 and isinstance(locations_body, dict):
                for item in locations_body.get("data", []):
                    attrs = item.get("attributes", {}) if isinstance(item, dict) else {}
                    if attrs.get("short") == _brand_slug():
                        location_id = attrs.get("id")
                        break
            if not location_id:
                status, body = await _panel_api(
                    "POST", panel_url, panel_api_key, "locations",
                    {"short": _brand_slug(), "long": f"{_brand()} ({container})"},
                )
                if status not in (200, 201):
                    await _upd(
                        7, failed_at=7,
                        error=f"Failed to create Panel location (HTTP {status}):\n"
                              f"{_panel_error(body)[:500]}",
                    )
                    return
                location_created = True
                location_id = (body if isinstance(body, dict) else {}).get("attributes", {}).get("id")
            if not location_id:
                await _upd(7, failed_at=7, error=f"Panel returned no location ID.\nBody: {str(body)[:300]}")
                return

            # 7b — Create node
            panel_node_name = f"{_brand()}-{container[:40]}"
            node_data = {
                "name":               panel_node_name,
                "location_id":        location_id,
                "fqdn":               domain,
                "scheme":             scheme,
                "memory":             max(1, node_memory * 1024),
                "memory_overallocate": -1,
                "disk":               max(1, node_disk * 1024),
                "disk_overallocate":  -1,
                "upload_size":        100,
                "daemon_sftp":        2022,
                "daemon_listen":      8080,
                "daemon_base":        "/var/lib/pterodactyl",
                "public":             True,
            }
            panel_node_id = None
            panel_node_label = panel_node_name
            status, nodes_body = await _panel_api(
                "GET", panel_url, panel_api_key, "nodes",
            )
            if status == 200 and isinstance(nodes_body, dict):
                for item in nodes_body.get("data", []):
                    candidate = item.get("attributes", {}) if isinstance(item, dict) else {}
                    if candidate.get("name") == panel_node_name:
                        panel_node_id = candidate.get("id")
                        panel_node_label = candidate.get("name", panel_node_name)
                        break
            if not panel_node_id:
                status, body = await _panel_api("POST", panel_url, panel_api_key, "nodes", node_data)
                if status not in (200, 201):
                    if location_created:
                        await _panel_api("DELETE", panel_url, panel_api_key, f"locations/{location_id}")
                    await _upd(
                        7, failed_at=7,
                        error=f"Failed to create Panel node (HTTP {status}):\n"
                              f"{_panel_error(body)[:500]}",
                    )
                    return
                attrs = (body if isinstance(body, dict) else {}).get("attributes", {})
                panel_node_id = attrs.get("id")
                panel_node_label = attrs.get("name", panel_node_name)
            else:
                attrs = {"id": panel_node_id, "name": panel_node_label}
            panel_node_id = attrs.get("id")
            if not panel_node_id:
                await _upd(7, failed_at=7, error=f"Panel returned no node ID.\nBody: {str(body)[:300]}")
                return
            panel_node_label = attrs.get("name", f"node-{panel_node_id}")

            # 7c — Fetch node configuration YAML from the Panel
            status, cfg_body = await _panel_api("GET", panel_url, panel_api_key, f"nodes/{panel_node_id}/configuration")
            if status != 200:
                err_text = cfg_body if isinstance(cfg_body, str) else str(cfg_body)
                await _upd(7, failed_at=7, error=f"Failed to fetch node config (HTTP {status}):\n{err_text[:300]}")
                return
            # The endpoint returns plain YAML text
            config_yaml = cfg_body if isinstance(cfg_body, str) else ""
            if not config_yaml or len(config_yaml) < 50:
                await _upd(7, failed_at=7, error=f"Node config YAML looks empty or too short:\n{config_yaml[:200]}")
                return

            # 7d — Write the real config to /etc/pterodactyl/config.yml
            ok, out = await _exec(_write_file_cmd("/etc/pterodactyl/config.yml", config_yaml), timeout=15)
            if not ok:
                await _upd(7, failed_at=7, error=f"Failed to write node config to VPS:\n{out[:300]}")
                return

            panel_connected = True
            log.info(f"[wings] Auto-connected to Panel: location={location_id} node={panel_node_id} ({panel_node_label})")

        except Exception as exc:
            log.error(f"[wings] Panel connect error: {exc}", exc_info=True)
            await _upd(7, failed_at=7, error=f"Unexpected error during Panel connect:\n{exc}")
            return
    else:
        # No panel credentials — skip step 7, advance counter
        await _upd(8)

    # 8 — Start Wings only after a valid Panel configuration exists.
    # A placeholder config cannot authenticate to Panel; claiming it is
    # running made manual installs look successful while systemd was failing.
    if panel_connected:
        if not await _step(
            8,
            "systemctl restart wings 2>&1 && sleep 3 && systemctl is-active wings 2>&1",
            timeout=60,
        ):
            return
    else:
        await _upd(9)

    # 9 — Verify the binary and, for auto-connected installs, the service.
    ok, out = await _exec(
        "test -x /usr/local/bin/wings && "
        "(systemctl is-active --quiet wings && echo wings_ok || echo wings_config_pending)",
        timeout=10,
    )
    if not ok or ("wings_ok" not in out and panel_connected):
        await _upd(9, failed_at=9, error=f"Wings is not active:\n{out[:500]}")
        return

    await _upd(len(steps))

    # ── Success embed ──────────────────────────────────────────────────────────
    if panel_connected:
        desc = (
            f"Wings is installed and **connected to your Panel** on `{container}`.\n\n"
            f"🌐 **Wings address:** `{scheme}://{domain}:8080`\n"
            f"📋 **Panel node:** `{panel_node_label}`\n\n"
            f"> 🟢 The node should appear green in your Panel shortly.\n"
            f"> 📬 A summary has been sent to your DMs."
        )
    else:
        desc = (
            f"Wings is installed on `{container}` but is **waiting for Panel configuration**.\n\n"
            f"🌐 **Wings address:** `{scheme}://{domain}:8080`\n\n"
            f"> Configure the node from Panel, paste its generated config, then start Wings.\n"
            f"> 📬 The exact steps have been sent to your DMs."
        )
    done = discord.Embed(title="✅  Wings Installed!", description=desc, color=0x57F287)
    _set_footer(done, logo)
    try:
        # Show the Panel connect button when no panel was connected and one was supplied
        view = post_success_view if (not panel_connected and post_success_view) else None
        await msg.edit(embed=done, view=view)
    except Exception:
        pass

    # ── DM guide ──────────────────────────────────────────────────────────────
    try:
        if panel_connected:
            dm = discord.Embed(
                title="🪽  Wings — Connected to Panel",
                description=(
                    "Wings is running with **debug mode** enabled and is connected to your "
                    f"Pterodactyl Panel.\n\n"
                    f"🌐 **Wings address:** `{scheme}://{domain}:8080`\n"
                    f"📋 **Panel node:** `{panel_node_label}`\n\n"
                    "Check your Panel → Admin → Nodes — the node should show a **green heartbeat**.\n"
                    "If it's still grey, wait ~30 seconds and refresh."
                ),
                color=0x57F287,
            )
            dm.add_field(name="🖥️  VPS Container",  value=f"`{container}`",              inline=True)
            dm.add_field(name="🌐  Wings Address",   value=f"`{scheme}://{domain}:8080`", inline=False)
            dm.add_field(
                name="🔧  Useful SSH Commands",
                value=(
                    "```bash\n"
                    "# Check Wings status:\n"
                    "systemctl status wings\n\n"
                    "# Follow Wings debug logs:\n"
                    "journalctl -u wings -f\n\n"
                    "# Restart Wings:\n"
                    "systemctl restart wings\n"
                    "```"
                ),
                inline=False,
            )
        else:
            dm = discord.Embed(
                title="🪽  Wings — Manual Setup Required",
                description=(
                    "Wings is installed, but it is **not started yet** because it has no "
                    "Panel-generated configuration.\n\n"
                    "**Complete these steps from your Panel:**"
                ),
                color=0x000000,
            )
            dm.add_field(name="🖥️  VPS Container",  value=f"`{container}`",              inline=True)
            dm.add_field(name="🌐  Wings Address",   value=f"`{scheme}://{domain}:8080`", inline=False)
            dm.add_field(
                name="📋  How to Connect Wings to Your Panel",
                value=(
                    "1. **Panel** → Admin → Locations → **Create Location**\n"
                    "2. **Panel** → Admin → Nodes → **Create Node**\n"
                    f"   • FQDN: `{domain}`   Scheme: `{scheme}`\n"
                    "3. Open the **Configuration** tab on the new node\n"
                    "4. Copy the YAML → paste to `/etc/pterodactyl/config.yml` on this VPS\n"
                    "5. `systemctl enable --now wings`\n"
                    "6. Node turns **green** in your Panel within about 30 seconds"
                ),
                inline=False,
            )
            dm.add_field(
                name="🔧  Useful SSH Commands",
                value=(
                    "```bash\n"
                    "# Edit Wings config:\n"
                    "nano /etc/pterodactyl/config.yml\n\n"
                    "# Restart Wings:\n"
                    "systemctl restart wings\n\n"
                    "# Follow debug logs:\n"
                    "journalctl -u wings -f\n"
                    "```"
                ),
                inline=False,
            )
        _set_footer(dm, logo)
        await user.send(embed=dm)
    except discord.Forbidden:
        log.warning(f"[wings] Could not DM guide to {user}")
    except Exception as e:
        log.error(f"[wings] DM error: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Cloudflare Tunnel installation runner
# ─────────────────────────────────────────────────────────────────────────────
async def _run_cloudflare_installation(
    user:         discord.User | discord.Member,
    container:    str,
    tunnel_name:  str,
    tunnel_token: str,
    msg:          discord.Message,
    logo:         str,
    node_id:      str = None,
    vps_info:     dict = None,
    wings_domain: str = "",
    panel_url:    str = "",
    panel_api_key: str = "",
) -> None:
    title = "🚀  Installing Cloudflare Tunnel"
    steps = CLOUDFLARE_STEPS
    _exec = _make_exec(container, node_id)

    async def _upd(idx: int, failed_at: int = -1, error: str = "") -> None:
        try:
            await msg.edit(embed=_progress_embed(title, steps, idx, logo, failed_at, error))
        except Exception as e:
            log.warning(f"[cloudflare] embed update failed: {e}")

    _step = _make_step(_exec, _upd, steps, "cloudflare")

    # 0 — Preparing
    if not await _step(0,
        "export DEBIAN_FRONTEND=noninteractive && apt-get update -qq 2>&1 | tail -5",
        timeout=120): return

    # 1 — Install cloudflared
    install_cmd = "\n".join([
        "curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg "
        "| tee /usr/share/keyrings/cloudflare-main.gpg > /dev/null",
        "echo \"deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] "
        "https://pkg.cloudflare.com/cloudflared any main\" "
        "| tee /etc/apt/sources.list.d/cloudflared.list",
        "apt-get update -qq 2>&1 | tail -3",
        "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq cloudflared 2>&1 | tail -8",
        "cloudflared --version",
    ])
    if not await _step(1, install_cmd, timeout=240): return

    # 2 — Validate token (quick non-blocking check)
    await _upd(2)
    validate_cmd = (
        f"cloudflared tunnel run --token {shlex.quote(tunnel_token)} --no-autoupdate "
        "--pidfile /tmp/cf-check.pid & sleep 6; "
        "kill $(cat /tmp/cf-check.pid 2>/dev/null) 2>/dev/null; "
        "rm -f /tmp/cf-check.pid; echo 'token_check_done'"
    )
    ok, out = await _exec(validate_cmd, timeout=30)
    if not ok and "error" in out.lower() and "token" in out.lower():
        await _upd(2, failed_at=2, error=f"Token rejected by Cloudflare:\n{out[-800:]}")
        return
    await _upd(3)

    # 3 — Install systemd service
    if not await _step(3, "\n".join([
        f"cloudflared service install {tunnel_token} 2>&1",
        "systemctl daemon-reload",
        "systemctl enable cloudflared 2>&1 || true",
    ]), timeout=60): return

    # 4 — Start tunnel
    if not await _step(4, "systemctl start cloudflared 2>&1", timeout=30): return

    # 5 — Verify tunnel is active
    await _upd(5)
    ok, out = await _exec("systemctl is-active cloudflared 2>&1", timeout=20)
    if not ok or out.strip() != "active":
        _, logs_out = await _exec("journalctl -u cloudflared -n 30 --no-pager 2>&1 || true", timeout=20)
        await _upd(5, failed_at=5, error=logs_out[-800:] if logs_out else out)
        return
    await _upd(6)

    # 6 — Detect local services for the guide
    panel_detected, panel_port, panel_service = False, 80, "HTTP"
    wings_detected, wings_port, wings_service = False, 8080, "HTTP"

    _, nginx_active = await _exec("systemctl is-active nginx 2>&1", timeout=10)
    _, panel_dir    = await _exec("test -d /var/www/pterodactyl/public && echo FOUND", timeout=10)
    if nginx_active.strip() == "active" and "FOUND" in panel_dir:
        panel_detected = True
        _, ssl_check = await _exec(
            "grep -r 'listen 443' /etc/nginx/sites-enabled/ 2>/dev/null && echo HAS_SSL || true",
            timeout=10,
        )
        if "HAS_SSL" in ssl_check:
            panel_port, panel_service = 443, "HTTPS"

    _, wings_active = await _exec("systemctl is-active wings 2>&1", timeout=10)
    if wings_active.strip() == "active":
        wings_detected = True
        _, cfg_port = await _exec(
            "grep -E '^\\s*port:' /etc/pterodactyl/config.yml 2>/dev/null | head -1 | awk '{print $2}'",
            timeout=10,
        )
        if cfg_port.strip().isdigit():
            wings_port = int(cfg_port.strip())

    await _upd(len(steps))

    # ── Standalone Cloudflare install (no Wings) ──────────────────────────────
    if not wings_domain:
        done = discord.Embed(
            title="✅  Cloudflare Tunnel Installed!",
            description=(
                f"Tunnel **`{tunnel_name}`** is running on `{container}`.\n\n"
                "📬 A routing guide has been sent to your DMs."
            ),
            color=0x57F287,
        )
        _set_footer(done, logo)
        try:
            await msg.edit(embed=done, view=None)
        except Exception:
            pass
        await _send_cloudflare_guide_dm(
            user=user,
            tunnel_name=tunnel_name,
            container=container,
            panel_detected=panel_detected,
            panel_port=panel_port,
            panel_service=panel_service,
            wings_detected=wings_detected,
            wings_port=wings_port,
            wings_service=wings_service,
            logo=logo,
            wings_domain=wings_domain,
        )
        return

    # ── Wings path — tunnel is ready, offer Wings + Panel setup ──────────────
    # If panel credentials were supplied (legacy / direct call), proceed fully
    # automatically.  Otherwise show the "Connect Wings to Panel" button so the
    # user can enter Panel URL + API key in that dedicated step.
    if panel_url and panel_api_key:
        done = discord.Embed(
            title="✅  Cloudflare Tunnel Installed!",
            description=(
                f"Tunnel is running on `{container}`.\n\n"
                f"Cloudflare hostname: `{wings_domain}` → HTTPS → `localhost:8080` (No TLS Verify)\n\n"
                "Installing Wings and connecting to your Panel now…"
            ),
            color=0x57F287,
        )
        _set_footer(done, logo)
        try:
            await msg.edit(embed=done, view=None)
        except Exception:
            pass
        await msg.edit(
            embed=_progress_embed("🚀  Installing and connecting Wings", WINGS_STEPS, 0, logo)
        )
        asyncio.create_task(_run_wings_installation(
            user, container, wings_domain, "localhost-ssl", "", "",
            msg, logo, node_id,
            panel_url=panel_url,
            panel_api_key=panel_api_key,
        ))
    else:
        # Tunnel installed — prompt user to connect Wings to Panel
        done = discord.Embed(
            title="✅  Cloudflare Tunnel Installed!",
            description=(
                f"Tunnel is running on `{container}`.\n\n"
                f"🌐 Cloudflare hostname: `{wings_domain}` → HTTPS → `localhost:8080` (No TLS Verify)\n\n"
                "Click **Configure Wings automatically** below to install Wings and connect it to your Panel."
            ),
            color=0x57F287,
        )
        _set_footer(done, logo)
        try:
            await msg.edit(
                embed=done,
                view=_CloudflareWingsSetupView(container, vps_info or {}, wings_domain),
            )
        except Exception:
            pass

    await _send_cloudflare_guide_dm(
        user=user,
        tunnel_name=tunnel_name,
        container=container,
        panel_detected=panel_detected,
        panel_port=panel_port,
        panel_service=panel_service,
        wings_detected=wings_detected,
        wings_port=wings_port,
        wings_service=wings_service,
        logo=logo,
        wings_domain=wings_domain,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Cloudflare DM Guide — opinionated step-by-step for no-public-IP setup
# Assumes: Panel installed with HTTP (port 80), Wings installed with HTTP (port 8080)
# Cloudflare Tunnel handles all HTTPS — no SSL certificates needed on the VPS.
# ─────────────────────────────────────────────────────────────────────────────
async def _send_cloudflare_guide_dm(
    user:            discord.User | discord.Member,
    tunnel_name:     str,
    container:       str,
    panel_detected:  bool,
    panel_port:      int,
    panel_service:   str,
    wings_detected:  bool,
    wings_port:      int,
    wings_service:   str,
    logo:            str,
    wings_domain:    str = "",
) -> None:
    try:
        # ── Step 1: Overview ──────────────────────────────────────────────
        overview = discord.Embed(
            title="☁️  Cloudflare Tunnel — Full Setup Guide",
            description=(
                f"**Tunnel `{tunnel_name}` is running on `{container}`.**\n\n"
                "Follow these steps in order to get Panel + Wings fully working "
                "through Cloudflare Tunnel with no public IP required.\n\n"
                "**Recommended install order:**\n"
                "① Install Cloudflare Tunnel ✅ *(done)*\n"
                "② Install Pterodactyl Panel — choose **HTTP (No SSL)**\n"
                "③ Install Pterodactyl Wings — choose **HTTP (No SSL)**\n"
                "④ Configure Cloudflare hostnames *(this guide)*\n"
                "⑤ Connect Wings to Panel *(this guide)*\n\n"
                f"**Auto-detected on `{container}`:**\n"
                + ("✅  Panel running (port 80)" if panel_detected else "⬜  Panel — not yet installed") + "\n"
                + ("✅  Wings running (port 8080)" if wings_detected else "⬜  Wings — not yet installed")
            ),
            color=0xF6821F,
        )
        _set_footer(overview, logo)
        await user.send(embed=overview)

        # ── Step 2: Configure Panel hostname ──────────────────────────────
        panel_embed = discord.Embed(
            title="🦖  Step 1 — Expose Panel via Cloudflare",
            description=(
                "Go to **[dash.cloudflare.com](https://dash.cloudflare.com)**\n"
                f"→ Zero Trust → Networks → Tunnels → **`{tunnel_name}`** → Public Hostnames → **Add a Public Hostname**"
            ),
            color=0x000000,
        )
        panel_embed.add_field(
            name="Hostname settings",
            value=(
                "```\n"
                "Subdomain : panel\n"
                "Domain    : yourdomain.com\n"
                "Service   : HTTP\n"
                "URL       : 127.0.0.1:80\n"
                "```"
            ),
            inline=False,
        )
        panel_embed.add_field(
            name="SSL/TLS mode",
            value=(
                "In the Cloudflare dashboard → **SSL/TLS** → set mode to **Full**.\n"
                "*(Not Flexible, not Full Strict — just Full.)*"
            ),
            inline=False,
        )
        panel_embed.add_field(
            name="Result",
            value="Panel will be live at **`https://panel.yourdomain.com`** — Cloudflare handles the HTTPS certificate automatically.",
            inline=False,
        )
        _set_footer(panel_embed, logo)
        await user.send(embed=panel_embed)

        # ── Step 3: Configure Wings hostname ──────────────────────────────
        wings_embed = discord.Embed(
            title="🪽  Step 2 — Expose Wings via Cloudflare",
            description=(
                "In the same tunnel:\n"
                f"→ **`{tunnel_name}`** → Public Hostnames → **Add a Public Hostname**\n\n"
                "⚠️ Wings uses a **self-signed SSL cert** (localhost-ssl), so you must "
                "use **HTTPS** as the service type and enable **No TLS Verify**."
            ),
            color=0x000000,
        )
        wings_embed.add_field(
            name="Hostname settings",
            value=(
                "```\n"
                "Subdomain : wings\n"
                "Domain    : yourdomain.com\n"
                "Service   : HTTPS\n"
                "URL       : localhost:8080\n"
                "```"
            ),
            inline=False,
        )
        wings_embed.add_field(
            name="⚠️  Required — No TLS Verify",
            value=(
                "Expand **Additional application settings** → **TLS** section:\n"
                "→ ✅ **No TLS Verify** — enable this\n\n"
                "Wings runs with a self-signed cert at `/etc/certs/`. "
                "Without this Cloudflare will reject the connection."
            ),
            inline=False,
        )
        wings_embed.add_field(
            name="SSL/TLS mode (domain-wide)",
            value=(
                "Cloudflare dashboard → **SSL/TLS** → set to **Full**.\n"
                "*(Full = Cloudflare encrypts to the visitor and forwards to Wings over HTTPS. "
                "Full Strict would fail because the cert is self-signed.)*"
            ),
            inline=False,
        )
        wings_embed.add_field(
            name="Result",
            value="Wings will be reachable at **`https://wings.yourdomain.com`**",
            inline=False,
        )
        _set_footer(wings_embed, logo)
        await user.send(embed=wings_embed)

        # ── Step 4: Connect Wings to Panel ────────────────────────────────
        connect_embed = discord.Embed(
            title="🔗  Step 3 — Connect Wings to your Panel",
            description=(
                "If you used the **automated Wings setup** (via `/template` → Pterodactyl → Wings), "
                "the bot already created the Panel node, wrote the config, and started Wings — "
                "**you can skip to Verify below.**\n\n"
                "If Wings was installed without Panel credentials, follow these steps."
            ),
            color=0x000000,
        )
        connect_embed.add_field(
            name="1️⃣  Create the node in Panel",
            value=(
                "Panel → Admin → **Nodes** → **Create New**\n"
                "```\n"
                "Name        : (any name)\n"
                "FQDN        : wings.yourdomain.com\n"
                "Scheme      : https\n"
                "Daemon Port : 8080\n"
                "```"
            ),
            inline=False,
        )
        connect_embed.add_field(
            name="2️⃣  Copy the config to Wings",
            value=(
                "On the new node page, click the **Configuration** tab.\n"
                "Copy the YAML config shown there.\n\n"
                "Paste it into the Wings VPS:\n"
                "```bash\nnano /etc/pterodactyl/config.yml\n```\n"
                "*(Replace the entire file with the copied YAML)*"
            ),
            inline=False,
        )
        connect_embed.add_field(
            name="3️⃣  Restart Wings",
            value="```bash\nsystemctl restart wings\n```",
            inline=False,
        )
        connect_embed.add_field(
            name="4️⃣  Verify",
            value=(
                "Back in Panel → Nodes — the node heartbeat should turn **green** ✅ within 30 seconds.\n\n"
                "If it stays red, check Wings logs:\n"
                "```bash\njournalctl -u wings -n 50 --no-pager\n```"
            ),
            inline=False,
        )
        _set_footer(connect_embed, logo)
        await user.send(embed=connect_embed)

    except discord.Forbidden:
        log.warning(f"[cloudflare] Could not DM guide to {user}")
    except Exception as e:
        log.error(f"[cloudflare] DM error: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# /cloudflare  —  standalone opinionated guide (no VPS / no install required)
# Single path: Panel + Wings both installed with HTTP, Cloudflare provides HTTPS
# ─────────────────────────────────────────────────────────────────────────────
def _cf_guide_main_embed(logo: str) -> discord.Embed:
    e = discord.Embed(
        title="☁️  Wings over Cloudflare Tunnel — Setup Guide",
        description=(
            "Recommended no-public-IP path for Wings.\n\n"
            "Wings runs with a **self-signed SSL cert** (localhost-ssl). "
            "Cloudflare connects to it over HTTPS with No TLS Verify and provides "
            "the public HTTPS hostname. The bot handles everything automatically.\n\n"
            "**Your steps:**\n"
            "1. Create one Cloudflare Tunnel.\n"
            "2. Add `wings.yourdomain.com` → **HTTPS** → `localhost:8080` + **No TLS Verify**.\n"
            "3. Run `/template` → Pterodactyl → Wings and follow the guide.\n"
            "4. Let the bot install cloudflared, generate the cert, install Wings, and connect it to Panel."
        ),
        color=0xF6821F,
    )
    e.add_field(
        name="🦖  Before starting",
        value=(
            "Install Pterodactyl Panel first and make sure it is reachable at its "
            "HTTPS URL. Create an Admin Application API key in Panel → Admin → Application API."
        ),
        inline=True,
    )
    e.add_field(
        name="🪽  What the bot does",
        value=(
            "Installs cloudflared, generates a self-signed cert at `/etc/certs/`, "
            "installs Wings with HTTPS, creates/reuses the brand location and node, "
            "writes `config.yml`, and starts Wings."
        ),
        inline=True,
    )
    _set_footer(e, logo)
    return e


def _cf_guide_panel_embed(logo: str) -> discord.Embed:
    e = discord.Embed(
        title="🦖  Exposing Pterodactyl Panel via Cloudflare Tunnel",
        description=(
            "**Assumes:** Panel was installed with **HTTP (No SSL)**.\n"
            "Cloudflare provides the HTTPS certificate — no cert needed on the server."
        ),
        color=0x000000,
    )
    e.add_field(
        name="1️⃣  Open Cloudflare Zero Trust",
        value=(
            "**[dash.cloudflare.com](https://dash.cloudflare.com)**\n"
            "→ Zero Trust → Networks → Tunnels → *(your tunnel)*\n"
            "→ Public Hostnames → **Add a Public Hostname**"
        ),
        inline=False,
    )
    e.add_field(
        name="2️⃣  Hostname settings",
        value=(
            "```\n"
            "Subdomain : panel\n"
            "Domain    : yourdomain.com\n"
            "Service   : HTTP\n"
            "URL       : 127.0.0.1:80\n"
            "```"
        ),
        inline=False,
    )
    e.add_field(
        name="3️⃣  SSL/TLS mode",
        value=(
            "Cloudflare dashboard → **SSL/TLS** → set to **Full**.\n"
            "*(Full = Cloudflare encrypts to the visitor; connects to origin over HTTP. Correct for this setup.)*"
        ),
        inline=False,
    )
    e.add_field(
        name="✅  Done",
        value=(
            "Panel is now live at **`https://panel.yourdomain.com`**\n"
            "The CNAME record is created automatically — no DNS changes needed."
        ),
        inline=False,
    )
    _set_footer(e, logo)
    return e


def _cf_guide_wings_embed(logo: str) -> discord.Embed:
    e = discord.Embed(
        title="🪽  Automated Wings Connection",
        description=(
            "Use this single supported path. Do not create the Panel node manually "
            "and do not copy YAML by hand.\n\n"
            "Wings uses a **self-signed SSL cert** generated by localhost-ssl. "
            "The Cloudflare hostname must use **HTTPS** with **No TLS Verify** enabled."
        ),
        color=0x000000,
    )
    e.add_field(
        name="1️⃣  Create the Cloudflare route",
        value=(
            "Cloudflare Zero Trust → Networks → Tunnels → *(your tunnel)* → "
            "Public Hostnames → **Add a public hostname**\n"
            "```\n"
            "Hostname : wings.yourdomain.com\n"
            "Service  : HTTPS\n"
            "URL      : localhost:8080\n"
            "```\n"
            "Expand **Additional application settings** → **TLS** → ✅ **No TLS Verify**"
        ),
        inline=False,
    )
    e.add_field(
        name="2️⃣  Run the bot setup",
        value=(
            "Run `/template` → select your VPS → **🦖 Pterodactyl** → **🪽 Wings**.\n"
            "Follow the step-by-step guide and click **I'm Ready — Set Up Wings Automatically**.\n"
            "Enter the Wings hostname and tunnel token — the bot handles the rest, then prompts for Panel credentials."
        ),
        inline=False,
    )
    e.add_field(
        name="3️⃣  What the bot does",
        value=(
            "① Installs `cloudflared` as a systemd service\n"
            "② Generates a self-signed SSL cert → `/etc/certs/fullchain.pem` + `privkey.pem`\n"
            "③ Downloads and installs the Wings binary\n"
            "④ Creates or reuses the Panel location + node via API\n"
            "⑤ Writes the Panel-generated config to `/etc/pterodactyl/config.yml`\n"
            "⑥ Starts Wings"
        ),
        inline=False,
    )
    e.add_field(
        name="4️⃣  Verify",
        value=(
            "The node should turn green in Panel within about 30 seconds. "
            "If it does not, check:\n"
            "```bash\njournalctl -u wings -n 50 --no-pager\n```"
        ),
        inline=False,
    )
    _set_footer(e, logo)
    return e


class _CloudflareGuideView(discord.ui.View):
    """Interactive guide view for the standalone /cloudflare command."""

    def __init__(self, logo: str):
        super().__init__(timeout=300)
        self._logo = logo

    @discord.ui.button(label="🦖 Panel Guide", style=discord.ButtonStyle.primary, row=0)
    async def panel_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_cf_guide_panel_embed(self._logo),
            view=_CloudflareGuideBackView(self._logo),
        )

    @discord.ui.button(label="🪽 Wings Guide", style=discord.ButtonStyle.secondary, row=0)
    async def wings_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_cf_guide_wings_embed(self._logo),
            view=_CloudflareGuideBackView(self._logo),
        )


class _CloudflareGuideBackView(discord.ui.View):
    """Back button shown on the Panel/Wings detail pages."""

    def __init__(self, logo: str):
        super().__init__(timeout=300)
        self._logo = logo

    @discord.ui.button(label="↩ Back", style=discord.ButtonStyle.danger, row=0)
    async def back_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_cf_guide_main_embed(self._logo),
            view=_CloudflareGuideView(self._logo),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Command registration  —  called once from bot.py
# ─────────────────────────────────────────────────────────────────────────────
def register_commands(bot: ext_commands.Bot) -> None:

    @bot.tree.command(
        name="template",
        description="Install a software template (Pterodactyl Panel, Wings, Cloudflare Tunnel, …) on your VPS",
    )
    async def template_cmd(interaction: discord.Interaction) -> None:
        try:
            user_id  = str(interaction.user.id)
            vps_list = (_vps_data or {}).get(user_id, [])
            logo     = _logo()

            if not vps_list:
                embed = discord.Embed(
                    title="❌  No VPS Found",
                    description="You don't have any VPS instances yet.\nUse `!manage` to deploy one first.",
                    color=0xED4245,
                )
                _set_footer(embed, logo)
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

            await interaction.response.send_message(
                embed=_embed_vps_select(logo),
                view=_VPSSelectView(vps_list),
                ephemeral=True,
            )
        except Exception as exc:
            log.error(f"[template] /template command error for {interaction.user}: {exc}", exc_info=True)
            err = discord.Embed(
                title="⚠️  Template Error",
                description=f"Something went wrong opening the template installer.\n```{str(exc)[:300]}```",
                color=0xED4245,
            )
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(embed=err, ephemeral=True)
                else:
                    await interaction.followup.send(embed=err, ephemeral=True)
            except Exception:
                pass

    # ── /cloudflare  ──────────────────────────────────────────────────────────
    @bot.tree.command(
        name="cloudflare",
        description="Get step-by-step Cloudflare Tunnel routing instructions for Pterodactyl Panel and Wings",
    )
    async def cloudflare_slash(interaction: discord.Interaction) -> None:
        logo = _logo()
        await interaction.response.send_message(
            embed=_cf_guide_main_embed(logo),
            view=_CloudflareGuideView(logo),
            ephemeral=True,
        )

    # ── !cloudflare prefix command  ───────────────────────────────────────────
    @bot.command(name="cloudflare", aliases=["cf", "cftunnel"])
    async def cloudflare_prefix(ctx: ext_commands.Context) -> None:
        logo = _logo()
        await ctx.send(
            embed=_cf_guide_main_embed(logo),
            view=_CloudflareGuideView(logo),
        )
