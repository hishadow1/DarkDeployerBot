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
from typing import Callable, Awaitable, Optional

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
_LOCAL_NODE_ID:        str                                = "local"


def init(
    docker_exec_fn:         Callable[..., Awaitable],
    get_logo_url_fn:        Callable[[], str],
    vps_data_ref:           dict,
    get_brand_name_fn:      Optional[Callable[[], str]] = None,
    run_container_cmd_fn:   Optional[Callable[..., Awaitable]] = None,
    local_node_id:          str = "local",
) -> None:
    global _docker_exec, _run_container_cmd, _get_logo_url, _get_brand_name, _vps_data, _LOCAL_NODE_ID
    _docker_exec       = docker_exec_fn
    _run_container_cmd = run_container_cmd_fn
    _get_logo_url      = get_logo_url_fn
    _get_brand_name    = get_brand_name_fn
    _vps_data          = vps_data_ref
    _LOCAL_NODE_ID     = local_node_id


def _brand() -> str:
    return _get_brand_name() if _get_brand_name else "DarkNodes"

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
    "Preparing System",
    "Installing Dependencies",
    "Downloading Wings Binary",
    "Creating Directories",
    "Setting Up SSL",
    "Writing Configuration",
    "Installing Service",
    "Starting Wings",
    "Verifying Install",
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
    return {
        "admin_user":  "dn_" + _secure(8),
        "admin_email": f"admin_{_secure(10)}@darknodes.internal",
        "admin_pass":  _secure(22, "!@#$%"),
        "db_name":     "ptero_" + _secure(8),
        "db_user":     "pterouser_" + _secure(6),
        "db_pass":     _secure(24, "!@#$"),
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
            "**Panel** and **Wings** are completely independent installers.\n"
            "Selecting one will **never** install or modify the other."
        ),
        color=0x5865F2,
    )
    e.add_field(
        name="🖥️  Panel",
        value="Web interface + API + database. Full game panel management.",
        inline=False,
    )
    e.add_field(
        name="🪽  Wings",
        value="Daemon that runs game server containers. Connect it to a Panel after install.",
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


def _embed_cloudflare_info(container: str, logo: str) -> discord.Embed:
    e = discord.Embed(
        title="☁️  Cloudflare Tunnel Setup",
        description=(
            f"Installing on: `{container}`\n\n"
            "A Cloudflare Tunnel exposes your VPS services through your domain "
            "**without opening firewall ports**. Cloudflare handles HTTPS automatically.\n\n"
            "**Get your tunnel token first:**"
        ),
        color=0x000000,
    )
    e.add_field(
        name="📋  How to get your token",
        value=(
            "1. Go to **[dash.cloudflare.com](https://dash.cloudflare.com)**\n"
            "2. Click **Zero Trust → Networks → Tunnels**\n"
            "3. Click **Create a Tunnel** → choose **Cloudflared** → Next\n"
            "4. Give it a name (e.g. `my-vps`) → Save\n"
            "5. Copy the **token** (long string after `--token`)\n"
            "6. Click **Enter Token** below and paste it"
        ),
        inline=False,
    )
    e.add_field(
        name="✅  What gets installed",
        value=(
            "`cloudflared` as a systemd service\n"
            "Auto-starts on reboot\n"
            "After install you receive a DM guide for routing Panel + Wings through the tunnel"
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
        self._vps_map = {v["container_name"]: v for v in vps_list}
        options = []
        for vps in vps_list[:25]:
            name   = vps.get("container_name", "?")
            status = vps.get("status", "unknown")
            emoji  = "🟢" if status == "running" else "🔴"
            ram    = vps.get("ram", "?")
            cpu    = vps.get("cpu", "?")
            options.append(discord.SelectOption(
                label=name[:100],
                description=f"RAM: {ram}  •  CPU: {cpu} core(s)  •  {status}"[:100],
                value=name,
                emoji=emoji,
            ))
        super().__init__(placeholder="Select a VPS…", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        container = self.values[0]
        vps_info  = self._vps_map[container]
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
        await interaction.response.send_modal(
            _WingsDomainModal(self._container, self._vps_info)
        )

    @discord.ui.button(label="↩ Back", style=discord.ButtonStyle.danger, row=1)
    async def back_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_embed_template_select(self._container, _logo()),
            view=_TemplateSelectView(self._container, self._vps_info),
        )


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
            embed=_embed_https_question(self._container, "Wings", domain, _logo()),
            view=_WingsHTTPSView(self._container, self._vps_info, domain),
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
        logo    = _logo()
        node_id = self._vps_info.get("node_id")
        embed   = _progress_embed("🚀  Installing Wings", WINGS_STEPS, 0, logo)
        msg     = await _start_progress(interaction, embed)
        asyncio.create_task(_run_wings_installation(
            interaction.user, self._container, self._domain, "http", "", "", msg, logo, node_id
        ))


class _WingsSSLMethodView(_SSLMethodView):
    def __init__(self, container: str, vps_info: dict, domain: str):
        super().__init__(container, vps_info, domain, "Wings")

    async def _on_letsencrypt(self, interaction: discord.Interaction):
        logo    = _logo()
        node_id = self._vps_info.get("node_id")
        embed   = _progress_embed("🚀  Installing Wings", WINGS_STEPS, 0, logo)
        msg     = await _start_progress(interaction, embed)
        asyncio.create_task(_run_wings_installation(
            interaction.user, self._container, self._domain, "letsencrypt", "", "", msg, logo, node_id
        ))

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
        logo    = _logo()
        node_id = self._vps_info.get("node_id")
        embed   = _progress_embed("🚀  Installing Wings", WINGS_STEPS, 0, logo)
        msg     = await _start_progress(interaction, embed)
        asyncio.create_task(_run_wings_installation(
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
        ))


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
    sql = (
        f"CREATE DATABASE IF NOT EXISTS `{db_name}`; "
        f"CREATE USER IF NOT EXISTS '{db_user}'@'127.0.0.1' IDENTIFIED BY '{db_pass}'; "
        f"GRANT ALL PRIVILEGES ON `{db_name}`.* TO '{db_user}'@'127.0.0.1'; "
        "FLUSH PRIVILEGES;"
    )
    if not await _step(9, f'mysql -e "{sql}" 2>&1', timeout=30): return

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
    user_cmd = (
        f"cd /var/www/pterodactyl && php artisan p:user:make "
        f'--email="{admin_email}" '
        f'--username="{admin_user}" '
        f'--name-first="Dark" '
        f'--name-last="Admin" '
        f'--password="{admin_pass}" '
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
        ("db-connect",  f'mysql -u{db_user} -p{db_pass} -h 127.0.0.1 {db_name} -e "SELECT 1" 2>&1'),
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
    arch_cmd = "\n".join([
        'ARCH=$(uname -m)',
        'if [ "$ARCH" = "x86_64" ]; then WINGS_ARCH="amd64";',
        'elif [ "$ARCH" = "aarch64" ]; then WINGS_ARCH="arm64";',
        'else WINGS_ARCH="amd64"; fi',
        "curl -L https://github.com/pterodactyl/wings/releases/latest/download/wings_linux_${WINGS_ARCH} "
        "-o /usr/local/bin/wings 2>&1 | tail -3",
        "chmod +x /usr/local/bin/wings",
        "wings --version 2>&1 | head -2",
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

    if ssl_mode == "letsencrypt":
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

    # 5 — Write Wings configuration
    wings_cfg = f"""debug: false
app_name: "Pterodactyl Wings"
uuid: ""
token_id: ""
token: ""
api:
  host: "0.0.0.0"
  port: 8080
  ssl:
    enabled: {ssl_enabled}
    cert: "{ssl_cert}"
    key: "{ssl_key}"
  upload_limit: 100
  disable_remote_download: false
system:
  data: "/var/lib/pterodactyl"
  sftp:
    bind_port: 2022
docker:
  network:
    interface: "172.18.0.1"
    name: pterodactyl_nw
  timezone: ""
  use_performant_inspect: true
ignore_panel_config_updates: false
"""
    if not await _step(5, _write_file_cmd("/etc/pterodactyl/config.yml", wings_cfg), timeout=15): return

    # 6 — Install systemd service
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
        "PIDFile=/var/run/wings/daemon.pid",
        "ExecStart=/usr/local/bin/wings",
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

    # 7 — Start Wings
    # Wings will start but may not reach "active" until config.yml has real Panel creds.
    # That's expected — the user connects it from the Panel after install.
    await _upd(7)
    await _exec("systemctl start wings 2>&1; sleep 2; true", timeout=20)
    await _upd(8)

    # 8 — Verify binary
    ok, out = await _exec("wings --version 2>&1 | head -1", timeout=10)
    if not ok or "wings" not in out.lower():
        await _upd(8, failed_at=8, error=f"Wings binary check failed:\n{out}")
        return

    await _upd(len(steps))

    scheme = "https" if ssl_mode != "http" else "http"
    done = discord.Embed(
        title="✅  Wings Installed!",
        description=(
            f"Wings is installed on `{container}`.\n\n"
            f"🌐 **Wings address:** `{scheme}://{domain}:8080`\n\n"
            f"> 📬 Next steps have been sent to your DMs."
        ),
        color=0x57F287,
    )
    _set_footer(done, logo)
    try:
        await msg.edit(embed=done, view=None)
    except Exception:
        pass

    # DM next-steps guide
    try:
        dm = discord.Embed(
            title="🪽  Wings Installed — Next Steps",
            description=(
                "Wings is installed and ready to be connected to your Pterodactyl Panel.\n\n"
                "**Complete the connection from your Panel admin area:**"
            ),
            color=0x000000,
        )
        dm.add_field(name="🖥️  Container",    value=f"`{container}`",              inline=True)
        dm.add_field(name="🌐  Wings Address", value=f"`{scheme}://{domain}:8080`", inline=False)
        dm.add_field(
            name="📋  How to Connect Wings to Your Panel",
            value=(
                "1. Open your **Pterodactyl Panel** → Admin → **Nodes** → Create New Node\n"
                f"2. Set **FQDN** to `{domain}`\n"
                f"3. Set **Scheme** to `{scheme}`\n"
                "4. Open the **Configuration** tab on the node\n"
                "5. Copy the YAML config → paste to `/etc/pterodactyl/config.yml` on this VPS\n"
                "6. Run `systemctl restart wings` on the VPS\n"
                "7. The node status in your Panel turns **green** ✅"
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
                "# Check Wings status:\n"
                "systemctl status wings\n"
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
        f"cloudflared tunnel run --token {tunnel_token} --no-autoupdate "
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
    wings_detected, wings_port, wings_service = False, 8080, "HTTPS"

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

    done = discord.Embed(
        title="✅  Cloudflare Tunnel Installed!",
        description=(
            f"Tunnel **`{tunnel_name}`** is running on `{container}`.\n\n"
            "📬 A detailed routing guide has been sent to your DMs."
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
                f"→ **`{tunnel_name}`** → Public Hostnames → **Add a Public Hostname**"
            ),
            color=0x000000,
        )
        wings_embed.add_field(
            name="Hostname settings",
            value=(
                "```\n"
                "Subdomain : wings\n"
                "Domain    : yourdomain.com\n"
                "Service   : HTTP\n"
                "URL       : 127.0.0.1:8080\n"
                "```"
            ),
            inline=False,
        )
        wings_embed.add_field(
            name="Advanced settings",
            value=(
                "Click **Additional application settings** → **HTTP Settings**:\n"
                "→ **HTTP2 Connection** → **Enabled**\n\n"
                "This enables WebSocket support for Wings through the tunnel."
            ),
            inline=False,
        )
        wings_embed.add_field(
            name="SSL/TLS mode",
            value="Same as Panel — set to **Full** in SSL/TLS settings.",
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
                "Once both hostnames are live, configure the Wings node in your Panel."
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
                "Daemon Port : 443\n"
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
        title="☁️  Pterodactyl + Cloudflare Tunnel — Setup Guide",
        description=(
            "This guide covers the **recommended setup** for running Pterodactyl "
            "**without a public IP** using Cloudflare Tunnel.\n\n"
            "Cloudflare handles all HTTPS — **no SSL certificates needed on the VPS**.\n\n"
            "**Setup order:**\n"
            "① `/template` → Cloudflare Tunnel\n"
            "② `/template` → Pterodactyl → Panel → **HTTP (No SSL)**\n"
            "③ `/template` → Pterodactyl → Wings → **HTTP (No SSL)**\n"
            "④ Configure Cloudflare hostnames *(see below)*\n"
            "⑤ Connect Wings to Panel *(see below)*"
        ),
        color=0xF6821F,
    )
    e.add_field(
        name="🦖  Panel Setup",
        value="How to expose Panel through Cloudflare and what settings to use.",
        inline=True,
    )
    e.add_field(
        name="🪽  Wings + Connect",
        value="How to expose Wings and connect it to your Panel.",
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
        title="🪽  Exposing Wings + Connecting to Panel",
        description=(
            "**Assumes:** Wings was installed with **HTTP (No SSL)**.\n"
            "Two steps: expose Wings via Cloudflare, then register the node in Panel."
        ),
        color=0x000000,
    )
    e.add_field(
        name="1️⃣  Add Wings hostname in Cloudflare",
        value=(
            "Tunnels → *(your tunnel)* → Public Hostnames → **Add a Public Hostname**\n"
            "```\n"
            "Subdomain : wings\n"
            "Domain    : yourdomain.com\n"
            "Service   : HTTP\n"
            "URL       : 127.0.0.1:8080\n"
            "```"
        ),
        inline=False,
    )
    e.add_field(
        name="2️⃣  Enable HTTP/2 for WebSocket support",
        value=(
            "Still on that hostname page:\n"
            "**Additional application settings** → **HTTP Settings**\n"
            "→ **HTTP2 Connection** → **Enabled**\n\n"
            "This is what allows the Panel ↔ Wings WebSocket connection to work."
        ),
        inline=False,
    )
    e.add_field(
        name="3️⃣  SSL/TLS mode",
        value="Cloudflare dashboard → **SSL/TLS** → set to **Full** (same as Panel).",
        inline=False,
    )
    e.add_field(
        name="4️⃣  Register the node in Panel",
        value=(
            "Panel → Admin → **Nodes** → **Create New**\n"
            "```\n"
            "FQDN        : wings.yourdomain.com\n"
            "Scheme      : https\n"
            "Daemon Port : 443\n"
            "```"
        ),
        inline=False,
    )
    e.add_field(
        name="5️⃣  Push config to Wings",
        value=(
            "Panel → Nodes → *(your node)* → **Configuration** tab → copy the YAML.\n\n"
            "On the Wings VPS:\n"
            "```bash\nnano /etc/pterodactyl/config.yml\n```\n"
            "Paste the copied YAML, save, then:\n"
            "```bash\nsystemctl restart wings\n```"
        ),
        inline=False,
    )
    e.add_field(
        name="✅  Done",
        value=(
            "The node heartbeat in Panel should turn **green** within 30 seconds.\n\n"
            "If it stays red:\n"
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
