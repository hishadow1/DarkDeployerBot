"""
DarkNodes Template System
─────────────────────────────────────────────────────────────────────────────
Modular application installer for VPS containers.

Adding a new template:
  1. Add an entry to TEMPLATES dict below.
  2. Implement _run_<name>_installation() coroutine.
  3. Wire a new component view / handler.

Registered into bot.py via:
    import template_system
    template_system.init(docker_exec, get_logo_url, vps_data)
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
from discord import app_commands

logger = logging.getLogger("template_system")

# ─────────────────────────────────────────────────────────────────────────────
# Module-level injection — set by init() called from bot.py
# ─────────────────────────────────────────────────────────────────────────────
_docker_exec:     Optional[Callable[..., Awaitable]] = None
_get_logo_url:    Optional[Callable[[], str]]        = None
_get_brand_name:  Optional[Callable[[], str]]        = None
_vps_data:        Optional[dict]                     = None


def init(
    docker_exec_fn:    Callable[..., Awaitable],
    get_logo_url_fn:   Callable[[], str],
    vps_data_ref:      dict,
    get_brand_name_fn: Optional[Callable[[], str]] = None,
) -> None:
    """Called once from bot.py after imports, before commands are registered."""
    global _docker_exec, _get_logo_url, _get_brand_name, _vps_data
    _docker_exec    = docker_exec_fn
    _get_logo_url   = get_logo_url_fn
    _get_brand_name = get_brand_name_fn
    _vps_data       = vps_data_ref


def _brand() -> str:
    """Return the current brand name; falls back to 'DarkNodes'."""
    return _get_brand_name() if _get_brand_name else "DarkNodes"


# ─────────────────────────────────────────────────────────────────────────────
# Template registry  (add new templates here)
# ─────────────────────────────────────────────────────────────────────────────
TEMPLATES: dict[str, dict] = {
    "pterodactyl": {
        "label":       "🦖  Pterodactyl",
        "description": "Game panel + Wings node daemon",
        "emoji":       "🦖",
    },
    "cloudflare_tunnel": {
        "label":       "☁️  Cloudflare Tunnel",
        "description": "Zero Trust tunnel via cloudflared",
        "emoji":       "☁️",
    },
    # Uncomment / extend as needed:
    # "portainer": {"label": "🐳  Portainer", "description": "Docker management UI", "emoji": "🐳"},
    # "nodejs_pm2": {"label": "🟢  Node.js + PM2", "description": "Node.js process manager", "emoji": "🟢"},
    # "minecraft":  {"label": "⛏️  Minecraft",  "description": "Paper/Spigot server", "emoji": "⛏️"},
}

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
    "Creating Admin User",
    "Setting Permissions",
    "Configuring Nginx",
    "Setting Up SSL",
    "Enabling Services",
    "Verifying Installation",
]

WINGS_STEPS = [
    "Preparing System",
    "Installing Dependencies",
    "Downloading Wings",
    "Creating Directories",
    "Configuring Wings",
    "Installing Systemd Service",
    "Starting Wings",
    "Verifying Wings",
]

CLOUDFLARE_STEPS = [
    "Preparing System",
    "Installing cloudflared",
    "Configuring Tunnel Token",
    "Installing Systemd Service",
    "Starting Tunnel",
    "Verifying Tunnel",
    "Detecting Local Services",
]

# ─────────────────────────────────────────────────────────────────────────────
# Credential generator
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


# ─────────────────────────────────────────────────────────────────────────────
# Progress embed builder
# ─────────────────────────────────────────────────────────────────────────────
def _bar(done: int, total: int, w: int = 10) -> str:
    n = int(done / total * w) if total else 0
    return "█" * n + "░" * (w - n)


def _make_progress_embed(
    title:     str,
    steps:     list[str],
    current:   int,
    logo:      str = "",
    failed_at: int = -1,
    error:     str = "",
) -> discord.Embed:
    total = len(steps)

    if failed_at >= 0:
        color  = 0xED4245
        pct    = int(failed_at / total * 100)
        bar    = _bar(failed_at, total)
        status = f"❌  Failed at **{steps[failed_at]}**"
    elif current >= total:
        color  = 0x57F287
        pct    = 100
        bar    = "█" * 10
        status = "✅  All steps complete!"
    else:
        color  = 0x000000
        pct    = int(current / total * 100)
        bar    = _bar(current, total)
        status = f"⏳  {steps[current]}"

    lines: list[str] = []
    for i, s in enumerate(steps):
        if failed_at >= 0 and i == failed_at:
            lines.append(f"❌  {s}")
        elif i < current or (current >= total and failed_at < 0):
            lines.append(f"✅  {s}")
        elif i == current and failed_at < 0:
            lines.append(f"⏳  {s}")
        else:
            lines.append(f"⬜  {s}")

    mid  = (len(lines) + 1) // 2
    col1 = "\n".join(lines[:mid])
    col2 = "\n".join(lines[mid:]) if lines[mid:] else None

    embed = discord.Embed(
        title=title,
        description=f"`{bar}` **{pct}%**\n\n{status}",
        color=color,
    )
    embed.add_field(name="\u200b", value=col1, inline=True)
    if col2:
        embed.add_field(name="\u200b", value=col2, inline=True)

    if error:
        snippet = error[-800:] if len(error) > 800 else error
        embed.add_field(
            name="⚠️  Error Output",
            value=f"```{snippet}```",
            inline=False,
        )

    kw: dict = {"text": f"{_brand()}  •  Template Installer  •  Keep credentials private"}
    if logo:
        embed.set_author(name=f"{_brand()}  •  Template System", icon_url=logo)
        kw["icon_url"] = logo
    embed.set_footer(**kw)
    return embed


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
# Safe file-write helper (base64 avoids all heredoc escaping issues)
# ─────────────────────────────────────────────────────────────────────────────
def _write_file_cmd(path: str, content: str) -> str:
    encoded = base64.b64encode(content.encode()).decode()
    return f"echo '{encoded}' | base64 -d > {path}"


# ─────────────────────────────────────────────────────────────────────────────
# Embed helpers (info pages)
# ─────────────────────────────────────────────────────────────────────────────
def _vps_select_embed(logo: str) -> discord.Embed:
    embed = discord.Embed(
        title="🖥️  Template Installer",
        description=(
            "Select a **VPS** from the dropdown below to install a template on.\n\n"
            "> Only your running VPS instances are shown."
        ),
        color=0x000000,
    )
    if logo:
        embed.set_author(name=f"{_brand()}  •  Template System", icon_url=logo)
        embed.set_footer(text=f"{_brand()}  •  Template System", icon_url=logo)
    else:
        embed.set_footer(text=f"{_brand()}  •  Template System")
    return embed


def _template_select_embed(container: str, logo: str) -> discord.Embed:
    embed = discord.Embed(
        title="📦  Choose a Template",
        description=(
            f"Installing on: `{container}`\n\n"
            "Select the template you want to install from the dropdown."
        ),
        color=0x000000,
    )
    if logo:
        embed.set_author(name=f"{_brand()}  •  Template System", icon_url=logo)
        embed.set_footer(text=f"{_brand()}  •  Template System", icon_url=logo)
    else:
        embed.set_footer(text=f"{_brand()}  •  Template System")
    return embed


def _ptero_component_embed(container: str, logo: str) -> discord.Embed:
    embed = discord.Embed(
        title="🦖  Pterodactyl Installer",
        description=(
            f"Installing on: `{container}`\n\n"
            "Pterodactyl has two parts — choose what to install:"
        ),
        color=0x000000,
    )
    embed.add_field(
        name="🖥️  Panel  —  install this FIRST",
        value=(
            "The **web dashboard** where you manage everything.\n"
            "Installs: PHP, MySQL, Redis, Nginx + Pterodactyl UI.\n"
            "➜ You'll get a URL like `https://panel.yourdomain.com`"
        ),
        inline=False,
    )
    embed.add_field(
        name="🔧  Wings  —  install AFTER the Panel",
        value=(
            "The **game server daemon** that runs on this VPS.\n"
            "Connects to your Panel using a node token.\n"
            "➜ You need a Panel installed *somewhere* first."
        ),
        inline=False,
    )
    embed.add_field(
        name="📌  First time?",
        value="Install **Panel** on one VPS, then **Wings** on this one (or the same one).",
        inline=False,
    )
    if logo:
        embed.set_author(name=f"{_brand()}  •  Template System", icon_url=logo)
        embed.set_footer(text=f"{_brand()}  •  Template System", icon_url=logo)
    else:
        embed.set_footer(text=f"{_brand()}  •  Template System")
    return embed


def _cloudflare_config_embed(container: str, logo: str) -> discord.Embed:
    embed = discord.Embed(
        title="☁️  Cloudflare Tunnel Setup",
        description=(
            f"Installing on: `{container}`\n\n"
            "A Cloudflare Tunnel lets your VPS services (Panel, Wings, any website) be accessible "
            "via your domain **without opening any firewall ports**. "
            "Cloudflare handles HTTPS automatically.\n\n"
            "**Before clicking the button below, get your tunnel token:**"
        ),
        color=0x000000,
    )
    embed.add_field(
        name="📋  Step-by-step: Get your token",
        value=(
            "1. Go to **[dash.cloudflare.com](https://dash.cloudflare.com)**\n"
            "2. Click **Zero Trust** (left sidebar)\n"
            "3. Go to **Networks → Tunnels**\n"
            "4. Click **Create a Tunnel** → choose **Cloudflared** → click Next\n"
            "5. Give it a name (e.g. `my-vps`) → click Save\n"
            "6. On the next page — **copy the token** (the long string after `--token`)\n"
            "7. Click **Enter Token** below and paste it"
        ),
        inline=False,
    )
    embed.add_field(
        name="✅  What this installs",
        value=(
            "`cloudflared` daemon installed as a systemd service\n"
            "Tunnel auto-starts on reboot\n"
            "After install, you'll get a routing guide to connect your domain"
        ),
        inline=False,
    )
    if logo:
        embed.set_author(name=f"{_brand()}  •  Template System", icon_url=logo)
        embed.set_footer(text=f"{_brand()}  •  Template System", icon_url=logo)
    else:
        embed.set_footer(text=f"{_brand()}  •  Template System")
    return embed


def _ssl_select_embed(container: str, domain: str, logo: str) -> discord.Embed:
    embed = discord.Embed(
        title="🔒  How should your Panel be accessed?",
        description=(
            f"**VPS:** `{container}`\n"
            f"**Domain:** `{domain}`\n\n"
            "Pick one of the options below:"
        ),
        color=0x000000,
    )
    embed.add_field(
        name="🔒  Auto-HTTPS  ✅  Recommended",
        value=(
            "Free SSL certificate via **Let's Encrypt** — auto-renewed forever.\n"
            "**Requirement:** your domain DNS must already point to this VPS's IP.\n"
            f"**Result:** `https://{domain}` — padlock in browser, fully secure."
        ),
        inline=False,
    )
    embed.add_field(
        name="🌐  HTTP Only  —  No domain setup needed",
        value=(
            "No SSL. Simpler to set up right now.\n"
            "**Use this if:** your domain isn't pointed to this VPS yet, or you plan to use Cloudflare Tunnel for HTTPS later.\n"
            f"**Result:** `http://{domain}` — works, but no padlock."
        ),
        inline=False,
    )
    embed.add_field(
        name="📜  I already have SSL certs  —  Advanced",
        value=(
            "Use your own existing certificate & private key files already on the VPS.\n"
            "Only choose this if you know the exact file paths on disk."
        ),
        inline=False,
    )
    if logo:
        embed.set_author(name=f"{_brand()}  •  Template System", icon_url=logo)
        embed.set_footer(text=f"{_brand()}  •  Not sure? Pick Auto-HTTPS if your domain is ready, or HTTP Only to install now and add HTTPS later.", icon_url=logo)
    else:
        embed.set_footer(text=f"{_brand()}  •  Not sure? Pick Auto-HTTPS if your domain is ready, or HTTP Only to install now.")
    return embed


# ─────────────────────────────────────────────────────────────────────────────
# Retry view
# ─────────────────────────────────────────────────────────────────────────────
class RetryView(discord.ui.View):
    def __init__(self, coro_factory: Callable[[], Awaitable]):
        super().__init__(timeout=600)
        self._coro_factory = coro_factory

    @discord.ui.button(label="🔄  Retry Installation", style=discord.ButtonStyle.primary)
    async def retry_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        button.disabled = True
        self.stop()
        await interaction.response.edit_message(view=self)
        asyncio.create_task(self._coro_factory())


# ─────────────────────────────────────────────────────────────────────────────
# UI — VPS select
# ─────────────────────────────────────────────────────────────────────────────
class _VPSDropdown(discord.ui.Select):
    def __init__(self, vps_list: list[dict]):
        self._vps_map = {v["container_name"]: v for v in vps_list}
        options = []
        for vps in vps_list[:25]:
            name   = vps.get("container_name", "?")
            ram    = vps.get("ram", "?")
            cpu    = vps.get("cpu", "?")
            status = vps.get("status", "unknown")
            emoji  = "🟢" if status == "running" else "🔴"
            options.append(discord.SelectOption(
                label=name[:100],
                description=f"RAM: {ram}  |  CPU: {cpu} core(s)  |  {status}"[:100],
                value=name,
                emoji=emoji,
            ))
        super().__init__(placeholder="Select a VPS…", options=options)

    async def callback(self, interaction: discord.Interaction):
        container = self.values[0]
        vps_info  = self._vps_map[container]
        logo      = _get_logo_url() if _get_logo_url else ""
        view      = _TemplateSelectView(container, vps_info)
        embed     = _template_select_embed(container, logo)
        await interaction.response.edit_message(embed=embed, view=view)


class _VPSSelectView(discord.ui.View):
    def __init__(self, vps_list: list[dict]):
        super().__init__(timeout=300)
        self.add_item(_VPSDropdown(vps_list))


# ─────────────────────────────────────────────────────────────────────────────
# UI — Template select
# ─────────────────────────────────────────────────────────────────────────────
class _TemplateDropdown(discord.ui.Select):
    def __init__(self, container: str, vps_info: dict):
        self._container = container
        self._vps_info  = vps_info
        options = [
            discord.SelectOption(
                label=v["label"][:100],
                description=v["description"][:100],
                value=k,
                emoji=v.get("emoji", "📦"),
            )
            for k, v in TEMPLATES.items()
        ]
        super().__init__(placeholder="Choose a template…", options=options)

    async def callback(self, interaction: discord.Interaction):
        template = self.values[0]
        logo     = _get_logo_url() if _get_logo_url else ""
        if template == "pterodactyl":
            view  = _PterodactylComponentView(self._container, self._vps_info)
            embed = _ptero_component_embed(self._container, logo)
            await interaction.response.edit_message(embed=embed, view=view)
        elif template == "cloudflare_tunnel":
            embed = _cloudflare_config_embed(self._container, logo)
            view  = _CloudflareStartView(self._container, self._vps_info)
            await interaction.response.edit_message(embed=embed, view=view)
        else:
            await interaction.response.send_message(
                "❌  This template is not yet available.", ephemeral=True
            )


class _TemplateSelectView(discord.ui.View):
    def __init__(self, container: str, vps_info: dict):
        super().__init__(timeout=300)
        self.add_item(_TemplateDropdown(container, vps_info))
        self._container = container
        self._vps_info  = vps_info

    @discord.ui.button(label="↩  Back", style=discord.ButtonStyle.danger, row=1)
    async def back_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        logo = _get_logo_url() if _get_logo_url else ""
        # Rebuild VPS list for current user
        user_id  = str(interaction.user.id)
        vps_list = (_vps_data or {}).get(user_id, [])
        view  = _VPSSelectView(vps_list)
        embed = _vps_select_embed(logo)
        await interaction.response.edit_message(embed=embed, view=view)


# ─────────────────────────────────────────────────────────────────────────────
# UI — Pterodactyl: Panel vs Wings
# ─────────────────────────────────────────────────────────────────────────────
class _PterodactylComponentView(discord.ui.View):
    def __init__(self, container: str, vps_info: dict):
        super().__init__(timeout=300)
        self._container = container
        self._vps_info  = vps_info

    @discord.ui.button(label="🖥️  Panel", style=discord.ButtonStyle.primary, row=0)
    async def panel_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        await interaction.response.send_modal(
            _PanelDomainModal(self._container, self._vps_info)
        )

    @discord.ui.button(label="🔧  Wings", style=discord.ButtonStyle.secondary, row=0)
    async def wings_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        await interaction.response.send_modal(
            _WingsSetupModal(self._container, self._vps_info)
        )

    @discord.ui.button(label="↩  Back", style=discord.ButtonStyle.danger, row=0)
    async def back_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        logo = _get_logo_url() if _get_logo_url else ""
        view  = _TemplateSelectView(self._container, self._vps_info)
        embed = _template_select_embed(self._container, logo)
        await interaction.response.edit_message(embed=embed, view=view)


# ─────────────────────────────────────────────────────────────────────────────
# Modal — Panel domain
# ─────────────────────────────────────────────────────────────────────────────
class _PanelDomainModal(discord.ui.Modal, title="🦖  Pterodactyl Panel — Domain"):
    domain = discord.ui.TextInput(
        label="Your Panel Domain",
        placeholder="panel.yourdomain.com   (just the domain, no https://)",
        min_length=4,
        max_length=253,
    )

    def __init__(self, container: str, vps_info: dict):
        super().__init__()
        self._container = container
        self._vps_info  = vps_info

    async def on_submit(self, interaction: discord.Interaction):
        raw    = self.domain.value.strip()
        domain = raw.removeprefix("https://").removeprefix("http://").rstrip("/")
        logo   = _get_logo_url() if _get_logo_url else ""
        view   = _PanelSSLSelectView(self._container, self._vps_info, domain)
        embed  = _ssl_select_embed(self._container, domain, logo)
        await interaction.response.edit_message(embed=embed, view=view)


# ─────────────────────────────────────────────────────────────────────────────
# View — SSL selection
# ─────────────────────────────────────────────────────────────────────────────
class _PanelSSLSelectView(discord.ui.View):
    def __init__(self, container: str, vps_info: dict, domain: str):
        super().__init__(timeout=300)
        self._container = container
        self._vps_info  = vps_info
        self._domain    = domain

    @discord.ui.button(label="🔒  Auto-HTTPS (Let's Encrypt)  ✅ Recommended", style=discord.ButtonStyle.success, row=0)
    async def le_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        await _begin_panel_install(
            interaction, self._container, self._vps_info,
            self._domain, "letsencrypt"
        )

    @discord.ui.button(label="🌐  HTTP Only  (no SSL, simpler)", style=discord.ButtonStyle.secondary, row=0)
    async def http_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        await _begin_panel_install(
            interaction, self._container, self._vps_info,
            self._domain, "http"
        )

    @discord.ui.button(label="📜  I already have SSL certs  (advanced)", style=discord.ButtonStyle.primary, row=1)
    async def custom_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        await interaction.response.send_modal(
            _CustomSSLModal(self._container, self._vps_info, self._domain)
        )


# ─────────────────────────────────────────────────────────────────────────────
# Modal — Custom SSL paths
# ─────────────────────────────────────────────────────────────────────────────
class _CustomSSLModal(discord.ui.Modal, title="📜  Your SSL Certificate Paths"):
    cert_path = discord.ui.TextInput(
        label="Certificate Path (fullchain.pem)",
        placeholder="/etc/ssl/certs/fullchain.pem",
        min_length=5, max_length=255,
    )
    key_path = discord.ui.TextInput(
        label="Private Key Path (privkey.pem)",
        placeholder="/etc/ssl/private/privkey.pem",
        min_length=5, max_length=255,
    )

    def __init__(self, container: str, vps_info: dict, domain: str):
        super().__init__()
        self._container = container
        self._vps_info  = vps_info
        self._domain    = domain

    async def on_submit(self, interaction: discord.Interaction):
        await _begin_panel_install(
            interaction, self._container, self._vps_info, self._domain,
            "custom",
            cert_path=self.cert_path.value.strip(),
            key_path=self.key_path.value.strip(),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Modal — Wings setup
# ─────────────────────────────────────────────────────────────────────────────
class _WingsSetupModal(discord.ui.Modal, title="🔧  Wings — Connect to your Panel"):
    panel_url = discord.ui.TextInput(
        label="Panel URL  (where your Panel is installed)",
        placeholder="https://panel.yourdomain.com",
        min_length=8, max_length=255,
    )
    wings_token = discord.ui.TextInput(
        label="Wings Token  (from Panel → Admin → Nodes)",
        placeholder=(
            "How to get this: Panel → Admin → Nodes → Create Node → "
            "go to the Configuration tab → copy the token shown there"
        ),
        min_length=10, max_length=512,
        style=discord.TextStyle.paragraph,
    )

    def __init__(self, container: str, vps_info: dict):
        super().__init__()
        self._container = container
        self._vps_info  = vps_info

    async def on_submit(self, interaction: discord.Interaction):
        await _begin_wings_install(
            interaction, self._container, self._vps_info,
            self.panel_url.value.strip(),
            self.wings_token.value.strip(),
        )


# ─────────────────────────────────────────────────────────────────────────────
# UI — Cloudflare Tunnel: info view + modal
# ─────────────────────────────────────────────────────────────────────────────
class _CloudflareStartView(discord.ui.View):
    """Shown after selecting the Cloudflare Tunnel template.
    Two buttons: open the setup modal or go back."""

    def __init__(self, container: str, vps_info: dict):
        super().__init__(timeout=300)
        self._container = container
        self._vps_info  = vps_info

    @discord.ui.button(label="☁️  I have my token — Enter it now", style=discord.ButtonStyle.primary, row=0)
    async def setup_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        await interaction.response.send_modal(
            _CloudflareTunnelModal(self._container, self._vps_info)
        )

    @discord.ui.button(label="↩  Back", style=discord.ButtonStyle.danger, row=0)
    async def back_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        logo = _get_logo_url() if _get_logo_url else ""
        view  = _TemplateSelectView(self._container, self._vps_info)
        embed = _template_select_embed(self._container, logo)
        await interaction.response.edit_message(embed=embed, view=view)


class _CloudflareTunnelModal(discord.ui.Modal, title="☁️  Paste your Cloudflare Tunnel Token"):
    tunnel_token = discord.ui.TextInput(
        label="Tunnel Token  (from Cloudflare dashboard Step 2)",
        placeholder="eyJhI...  (the long string after --token in the dashboard)",
        min_length=20,
        max_length=2000,
        style=discord.TextStyle.paragraph,
    )
    tunnel_name = discord.ui.TextInput(
        label="Tunnel Name  (the name you gave it in Cloudflare)",
        placeholder="e.g. my-vps  — used in your setup guide after install",
        min_length=2,
        max_length=64,
    )

    def __init__(self, container: str, vps_info: dict):
        super().__init__()
        self._container = container
        self._vps_info  = vps_info

    async def on_submit(self, interaction: discord.Interaction):
        await _begin_cloudflare_install(
            interaction,
            self._container,
            self._vps_info,
            self.tunnel_name.value.strip(),
            self.tunnel_token.value.strip(),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Installation starters — send progress message then fire background task
# ─────────────────────────────────────────────────────────────────────────────
async def _begin_panel_install(
    interaction: discord.Interaction,
    container:   str,
    vps_info:    dict,
    domain:      str,
    ssl_mode:    str,
    cert_path:   str = "",
    key_path:    str = "",
) -> None:
    logo  = _get_logo_url() if _get_logo_url else ""
    embed = _make_progress_embed("🚀  Installing Pterodactyl Panel", PANEL_STEPS, 0, logo)
    await interaction.response.edit_message(embed=embed, view=None)
    # Fetch the actual Message object so we can edit it indefinitely (no webhook TTL)
    resp = await interaction.original_response()
    try:
        msg = await interaction.channel.fetch_message(resp.id)
    except Exception:
        msg = resp  # fallback to webhook message

    asyncio.create_task(
        _run_panel_installation(
            interaction.user, container, domain, ssl_mode,
            cert_path, key_path, msg, logo
        )
    )


async def _begin_wings_install(
    interaction: discord.Interaction,
    container:   str,
    vps_info:    dict,
    panel_url:   str,
    wings_token: str,
) -> None:
    logo  = _get_logo_url() if _get_logo_url else ""
    embed = _make_progress_embed("🚀  Installing Wings", WINGS_STEPS, 0, logo)
    await interaction.response.edit_message(embed=embed, view=None)
    resp = await interaction.original_response()
    try:
        msg = await interaction.channel.fetch_message(resp.id)
    except Exception:
        msg = resp

    asyncio.create_task(
        _run_wings_installation(
            interaction.user, container, panel_url, wings_token, msg, logo
        )
    )


# ─────────────────────────────────────────────────────────────────────────────
# Panel installation runner
# ─────────────────────────────────────────────────────────────────────────────
async def _run_panel_installation(
    user:      discord.User | discord.Member,
    container: str,
    domain:    str,
    ssl_mode:  str,
    cert_path: str,
    key_path:  str,
    msg:       discord.Message,
    logo:      str,
) -> None:
    title  = "🚀  Installing Pterodactyl Panel"
    steps  = PANEL_STEPS
    creds  = _gen_panel_creds()

    db_name     = creds["db_name"]
    db_user     = creds["db_user"]
    db_pass     = creds["db_pass"]
    admin_user  = creds["admin_user"]
    admin_email = creds["admin_email"]
    admin_pass  = creds["admin_pass"]
    app_url     = f"https://{domain}" if ssl_mode != "http" else f"http://{domain}"

    async def _update(idx: int, failed_at: int = -1, error: str = "") -> None:
        try:
            embed = _make_progress_embed(title, steps, idx, logo, failed_at, error)
            await msg.edit(embed=embed)
        except Exception as e:
            logger.warning(f"[template] embed update failed: {e}")

    async def _exec(cmd: str, timeout: int = 180) -> tuple[bool, str]:
        if not _docker_exec:
            return False, "docker_exec not initialised"
        try:
            out, err, rc = await _docker_exec(container, cmd, timeout=timeout)
            combined = ((out or "") + "\n" + (err or "")).strip()
            return rc == 0, combined
        except Exception as exc:
            return False, str(exc)

    async def _step(idx: int, cmd: str, timeout: int = 180) -> bool:
        """Run a step; update embed on both success and failure."""
        await _update(idx)
        ok, out = await _exec(cmd, timeout)
        if not ok:
            tail = out[-800:] if len(out) > 800 else out
            await _update(idx, failed_at=idx, error=tail)
            logger.error(f"[template][panel] step {idx} ({steps[idx]}) failed:\n{tail}")
        else:
            await _update(idx + 1)
        return ok

    # ── Step 0: Preparing System ───────────────────────────────────────────
    if not await _step(0,
        "export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a NEEDRESTART_SUSPEND=1 && "
        "apt-get update -qq 2>&1 | tail -5",
        timeout=120,
    ): return

    # ── Step 1: Dependencies ───────────────────────────────────────────────
    if not await _step(1,
        "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "
        "curl wget tar unzip git software-properties-common "
        "apt-transport-https ca-certificates gnupg2 lsb-release cron 2>&1 | tail -8",
        timeout=240,
    ): return

    # ── Step 2: PHP 8.3 ───────────────────────────────────────────────────
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

    # ── Step 3: MariaDB ────────────────────────────────────────────────────
    if not await _step(3, "\n".join([
        "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq mariadb-server 2>&1 | tail -5",
        "systemctl enable mariadb 2>&1 || true",
        "systemctl start  mariadb 2>&1 || true",
        'mysql -e "SELECT 1" 2>&1',
    ]), timeout=240): return

    # ── Step 4: Redis ──────────────────────────────────────────────────────
    if not await _step(4, "\n".join([
        "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq redis-server 2>&1 | tail -5",
        "systemctl enable redis-server 2>&1 || true",
        "systemctl start  redis-server 2>&1 || true",
        "redis-cli ping",
    ]), timeout=120): return

    # ── Step 5: Nginx ──────────────────────────────────────────────────────
    if not await _step(5, "\n".join([
        "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nginx 2>&1 | tail -5",
        "systemctl enable nginx 2>&1 || true",
        "systemctl start  nginx 2>&1 || true",
    ]), timeout=120): return

    # ── Step 6: Composer ───────────────────────────────────────────────────
    if not await _step(6, "\n".join([
        "curl -sS https://getcomposer.org/installer | php -- --install-dir=/usr/local/bin --filename=composer 2>&1 | tail -3",
        "composer --version",
    ]), timeout=120): return

    # ── Step 7: Download Pterodactyl ───────────────────────────────────────
    if not await _step(7, "\n".join([
        "mkdir -p /var/www/pterodactyl",
        "curl -Lo /var/www/pterodactyl/panel.tar.gz "
        "https://github.com/pterodactyl/panel/releases/latest/download/panel.tar.gz 2>&1 | tail -3",
        "tar -xzf /var/www/pterodactyl/panel.tar.gz -C /var/www/pterodactyl/ 2>&1 | tail -3",
        "rm -f /var/www/pterodactyl/panel.tar.gz",
    ]), timeout=180): return

    # ── Step 8: Composer install ───────────────────────────────────────────
    if not await _step(8,
        "cd /var/www/pterodactyl && "
        "COMPOSER_ALLOW_SUPERUSER=1 composer install --no-dev --optimize-autoloader --no-interaction 2>&1 | tail -10",
        timeout=360,
    ): return

    # ── Step 9: Configure database ─────────────────────────────────────────
    sql = (
        f"CREATE DATABASE IF NOT EXISTS `{db_name}`; "
        f"CREATE USER IF NOT EXISTS '{db_user}'@'127.0.0.1' IDENTIFIED BY '{db_pass}'; "
        f"GRANT ALL PRIVILEGES ON `{db_name}`.* TO '{db_user}'@'127.0.0.1'; "
        f"FLUSH PRIVILEGES;"
    )
    if not await _step(9, f'mysql -e "{sql}" 2>&1', timeout=60): return

    # ── Step 10: Configure .env ────────────────────────────────────────────
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

    # ── Step 11: Migrations ────────────────────────────────────────────────
    if not await _step(11,
        "cd /var/www/pterodactyl && php artisan migrate --seed --force 2>&1 | tail -12",
        timeout=240,
    ): return

    # ── Step 12: Create admin user ─────────────────────────────────────────
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

    # ── Step 13: Set permissions ───────────────────────────────────────────
    if not await _step(13, "\n".join([
        "chown -R www-data:www-data /var/www/pterodactyl/",
        "chmod -R 755 /var/www/pterodactyl/storage/",
        "chmod -R 755 /var/www/pterodactyl/bootstrap/cache/",
    ]), timeout=60): return

    # ── Step 14: Configure Nginx ───────────────────────────────────────────
    # Start with HTTP config (certbot will upgrade to HTTPS for Let's Encrypt)
    if ssl_mode == "custom":
        nginx_content = _nginx_https(domain, cert_path, key_path)
    else:
        nginx_content = _nginx_http(domain)

    nginx_cmds = "\n".join([
        _write_file_cmd("/etc/nginx/sites-available/pterodactyl.conf", nginx_content),
        "ln -sf /etc/nginx/sites-available/pterodactyl.conf /etc/nginx/sites-enabled/pterodactyl.conf",
        "rm -f /etc/nginx/sites-enabled/default",
        "nginx -t 2>&1",
        "systemctl reload nginx 2>&1 || systemctl restart nginx 2>&1",
    ])
    if not await _step(14, nginx_cmds, timeout=60): return

    # ── Step 15: SSL ───────────────────────────────────────────────────────
    if ssl_mode == "letsencrypt":
        le_cmd = "\n".join([
            "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq certbot python3-certbot-nginx 2>&1 | tail -5",
            f"certbot --nginx -d {domain} --non-interactive --agree-tos "
            f"--email {admin_email} --redirect 2>&1 | tail -15",
        ])
        if not await _step(15, le_cmd, timeout=300): return
    else:
        # No SSL action needed — just advance the step
        await _update(16)

    # ── Step 16: Enable services ───────────────────────────────────────────
    pteroq = "\n".join([
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
        "ExecStart=/usr/bin/php /var/www/pterodactyl/artisan queue:work --queue=high,standard,low --sleep=3 --tries=3 --max-time=3600",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
    ])
    cron_entry = "* * * * * php /var/www/pterodactyl/artisan schedule:run >> /dev/null 2>&1"
    svc_cmds = "\n".join([
        _write_file_cmd("/etc/systemd/system/pteroq.service", pteroq),
        "systemctl daemon-reload",
        "systemctl enable --now pteroq 2>&1 || true",
        "systemctl enable cron 2>&1 && systemctl start cron 2>&1 || true",
        f'(crontab -u www-data -l 2>/dev/null; echo "{cron_entry}") | crontab -u www-data -',
    ])
    if not await _step(16, svc_cmds, timeout=60): return

    # ── Step 17: Verify ────────────────────────────────────────────────────
    await _update(17)
    verify_checks = [
        ("nginx",        "systemctl is-active nginx"),
        ("php8.3-fpm",   "systemctl is-active php8.3-fpm"),
        ("mariadb",      "systemctl is-active mariadb"),
        ("redis-server", "systemctl is-active redis-server"),
        ("pteroq",       "systemctl is-active pteroq"),
        ("db-connect",   f'mysql -u{db_user} -p{db_pass} -h 127.0.0.1 {db_name} -e "SELECT 1" 2>&1'),
        ("panel-http",   "curl -s -o /dev/null -w '%{http_code}' http://localhost/ 2>&1 | grep -qE '^(200|301|302)' && echo OK || echo FAIL"),
    ]
    failures: list[str] = []
    for label, cmd in verify_checks:
        ok, out = await _exec(cmd, timeout=30)
        passed = ok and (out.strip() == "active" or "1" in out or "ok" in out.lower())
        if not passed:
            failures.append(f"{label}: {out[:120]}")

    if failures:
        fail_text = "\n".join(failures)
        await _update(17, failed_at=17, error=f"Verification checks failed:\n{fail_text}")
        return

    # ─── Complete: show success and DM credentials ─────────────────────────
    await _update(len(steps))  # all done

    # Completion embed in channel
    panel_url_str = app_url
    done_embed = discord.Embed(
        title="✅  Pterodactyl Panel Installed!",
        description=(
            f"Installation is **complete** on `{container}`.\n\n"
            f"🌐 **Panel URL:** {panel_url_str}\n\n"
            f"> 📬 Your credentials have been sent to your DMs — keep them safe."
        ),
        color=0x57F287,
    )
    if logo:
        done_embed.set_author(name=f"{_brand()}  •  Template System", icon_url=logo)
        done_embed.set_footer(text=f"{_brand()}  •  Keep credentials private", icon_url=logo)
    else:
        done_embed.set_footer(text=f"{_brand()}  •  Keep credentials private")

    try:
        await msg.edit(embed=done_embed, view=None)
    except Exception:
        pass

    # DM credentials to owner
    try:
        # Retrieve Pterodactyl version
        ver_out, _, _ = await _docker_exec(
            container,
            "cd /var/www/pterodactyl && composer show pterodactyl/panel 2>/dev/null | grep versions | head -1 || echo 'latest'",
            timeout=30,
        )
        version = ver_out.strip()[:40] if ver_out else "latest"

        dm_embed = discord.Embed(
            title="🦖  Pterodactyl Credentials",
            description=(
                "Your Pterodactyl Panel has been installed.\n"
                "**Keep these credentials safe and private.**"
            ),
            color=0x000000,
        )
        dm_embed.add_field(name="🌐  Panel URL",  value=panel_url_str, inline=False)
        dm_embed.add_field(name="👤  Username",   value=f"`{admin_user}`",  inline=True)
        dm_embed.add_field(name="📧  Email",      value=f"`{admin_email}`", inline=True)
        dm_embed.add_field(name="🔑  Password",   value=f"```{admin_pass}```", inline=False)
        dm_embed.add_field(name="🗄️  Database",   value=f"`{db_name}`",    inline=True)
        dm_embed.add_field(name="👤  DB User",    value=f"`{db_user}`",    inline=True)
        dm_embed.add_field(name="🔒  DB Password",value=f"```{db_pass}```", inline=False)
        dm_embed.add_field(name="📦  Version",    value=f"`{version}`",     inline=True)
        dm_embed.add_field(name="🖥️  Container",  value=f"`{container}`",   inline=True)
        if logo:
            dm_embed.set_author(name=f"{_brand()}  •  Template System", icon_url=logo)
            dm_embed.set_footer(text=f"{_brand()}  •  Never share these credentials", icon_url=logo)
        else:
            dm_embed.set_footer(text=f"{_brand()}  •  Never share these credentials")

        await user.send(embed=dm_embed)
    except discord.Forbidden:
        logger.warning(f"[template] Could not DM credentials to {user}")
    except Exception as e:
        logger.error(f"[template] Error sending credentials DM: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Wings installation runner
# ─────────────────────────────────────────────────────────────────────────────
async def _run_wings_installation(
    user:        discord.User | discord.Member,
    container:   str,
    panel_url:   str,
    wings_token: str,
    msg:         discord.Message,
    logo:        str,
) -> None:
    title = "🚀  Installing Wings"
    steps = WINGS_STEPS

    async def _update(idx: int, failed_at: int = -1, error: str = "") -> None:
        try:
            embed = _make_progress_embed(title, steps, idx, logo, failed_at, error)
            await msg.edit(embed=embed)
        except Exception as e:
            logger.warning(f"[template] embed update failed: {e}")

    async def _exec(cmd: str, timeout: int = 120) -> tuple[bool, str]:
        if not _docker_exec:
            return False, "docker_exec not initialised"
        try:
            out, err, rc = await _docker_exec(container, cmd, timeout=timeout)
            combined = ((out or "") + "\n" + (err or "")).strip()
            return rc == 0, combined
        except Exception as exc:
            return False, str(exc)

    async def _step(idx: int, cmd: str, timeout: int = 120) -> bool:
        await _update(idx)
        ok, out = await _exec(cmd, timeout)
        if not ok:
            tail = out[-800:] if len(out) > 800 else out
            await _update(idx, failed_at=idx, error=tail)
        else:
            await _update(idx + 1)
        return ok

    # ── Step 0: Preparing ──────────────────────────────────────────────────
    if not await _step(0,
        "export DEBIAN_FRONTEND=noninteractive && apt-get update -qq 2>&1 | tail -5",
        timeout=120,
    ): return

    # ── Step 1: Dependencies ───────────────────────────────────────────────
    if not await _step(1,
        "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl wget tar 2>&1 | tail -5",
        timeout=120,
    ): return

    # ── Step 2: Download Wings ─────────────────────────────────────────────
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

    # ── Step 3: Create directories ─────────────────────────────────────────
    if not await _step(3, "\n".join([
        "mkdir -p /etc/pterodactyl",
        "mkdir -p /var/log/pterodactyl",
        "mkdir -p /tmp/pterodactyl",
    ]), timeout=30): return

    # ── Step 4: Configure Wings ────────────────────────────────────────────
    if not await _step(4,
        f"wings configure --panel-url {panel_url} --token {wings_token} --allow-insecure 2>&1",
        timeout=60,
    ): return

    # ── Step 5: Install systemd service ───────────────────────────────────
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
    svc_cmds = "\n".join([
        _write_file_cmd("/etc/systemd/system/wings.service", wings_svc),
        "systemctl daemon-reload",
        "systemctl enable wings 2>&1",
    ])
    if not await _step(5, svc_cmds, timeout=30): return

    # ── Step 6: Start Wings ────────────────────────────────────────────────
    if not await _step(6, "systemctl start wings 2>&1", timeout=30): return

    # ── Step 7: Verify ─────────────────────────────────────────────────────
    await _update(7)
    ok, out = await _exec("systemctl is-active wings 2>&1", timeout=30)
    if not ok or out.strip() != "active":
        await _update(7, failed_at=7, error=out)
        return

    await _update(len(steps))

    # Completion embed
    done_embed = discord.Embed(
        title="✅  Wings Installed!",
        description=(
            f"Wings daemon is **running** on `{container}`.\n\n"
            f"🔗 Connected to: `{panel_url}`\n\n"
            "> 📬 Installation details have been sent to your DMs."
        ),
        color=0x57F287,
    )
    if logo:
        done_embed.set_author(name=f"{_brand()}  •  Template System", icon_url=logo)
        done_embed.set_footer(text=f"{_brand()}  •  Template System", icon_url=logo)

    try:
        await msg.edit(embed=done_embed, view=None)
    except Exception:
        pass

    # DM details
    try:
        dm_embed = discord.Embed(
            title="🔧  Wings Installation Details",
            description="Wings has been installed and connected to your Panel.",
            color=0x000000,
        )
        dm_embed.add_field(name="🖥️  Container",   value=f"`{container}`",  inline=True)
        dm_embed.add_field(name="🔗  Panel URL",   value=panel_url,         inline=False)
        dm_embed.add_field(name="📡  Status",      value="✅  Active",       inline=True)
        dm_embed.add_field(
            name="📌  Next Steps",
            value=(
                "1. Go to **Panel Admin → Nodes → [your node]**\n"
                "2. Confirm Wings is shown as **Online**\n"
                "3. Create an **Allocation** for the node\n"
                "4. You can now deploy game servers!"
            ),
            inline=False,
        )
        if logo:
            dm_embed.set_author(name=f"{_brand()}  •  Template System", icon_url=logo)
            dm_embed.set_footer(text=f"{_brand()}  •  Template System", icon_url=logo)
        await user.send(embed=dm_embed)
    except discord.Forbidden:
        pass
    except Exception as e:
        logger.error(f"[template] Wings DM error: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Cloudflare Tunnel — starter
# ─────────────────────────────────────────────────────────────────────────────
async def _begin_cloudflare_install(
    interaction:  discord.Interaction,
    container:    str,
    vps_info:     dict,
    tunnel_name:  str,
    tunnel_token: str,
) -> None:
    logo  = _get_logo_url() if _get_logo_url else ""
    embed = _make_progress_embed("🚀  Installing Cloudflare Tunnel", CLOUDFLARE_STEPS, 0, logo)
    await interaction.response.edit_message(embed=embed, view=None)
    resp = await interaction.original_response()
    try:
        msg = await interaction.channel.fetch_message(resp.id)
    except Exception:
        msg = resp

    asyncio.create_task(
        _run_cloudflare_installation(
            interaction.user, container, tunnel_name, tunnel_token, msg, logo
        )
    )


# ─────────────────────────────────────────────────────────────────────────────
# Cloudflare Tunnel — installation runner
# ─────────────────────────────────────────────────────────────────────────────
async def _run_cloudflare_installation(
    user:         discord.User | discord.Member,
    container:    str,
    tunnel_name:  str,
    tunnel_token: str,
    msg:          discord.Message,
    logo:         str,
) -> None:
    title = "🚀  Installing Cloudflare Tunnel"
    steps = CLOUDFLARE_STEPS

    async def _update(idx: int, failed_at: int = -1, error: str = "") -> None:
        try:
            embed = _make_progress_embed(title, steps, idx, logo, failed_at, error)
            await msg.edit(embed=embed)
        except Exception as e:
            logger.warning(f"[cloudflare] embed update failed: {e}")

    async def _exec(cmd: str, timeout: int = 120) -> tuple[bool, str]:
        if not _docker_exec:
            return False, "docker_exec not initialised"
        try:
            out, err, rc = await _docker_exec(container, cmd, timeout=timeout)
            combined = ((out or "") + "\n" + (err or "")).strip()
            return rc == 0, combined
        except Exception as exc:
            return False, str(exc)

    async def _step(idx: int, cmd: str, timeout: int = 120) -> bool:
        await _update(idx)
        ok, out = await _exec(cmd, timeout)
        if not ok:
            tail = out[-800:] if len(out) > 800 else out
            await _update(idx, failed_at=idx, error=tail)
            logger.error(f"[cloudflare] step {idx} ({steps[idx]}) failed:\n{tail}")
        else:
            await _update(idx + 1)
        return ok

    # ── Step 0: Preparing System ───────────────────────────────────────────
    if not await _step(0,
        "export DEBIAN_FRONTEND=noninteractive && apt-get update -qq 2>&1 | tail -5",
        timeout=120,
    ): return

    # ── Step 1: Install cloudflared ────────────────────────────────────────
    # Official Cloudflare repo method (works on Debian/Ubuntu amd64 + arm64)
    install_cmd = "\n".join([
        'ARCH=$(dpkg --print-architecture)',
        'curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg '
        '| tee /usr/share/keyrings/cloudflare-main.gpg > /dev/null',
        'echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] '
        'https://pkg.cloudflare.com/cloudflared any main" '
        '| tee /etc/apt/sources.list.d/cloudflared.list',
        'apt-get update -qq 2>&1 | tail -3',
        'DEBIAN_FRONTEND=noninteractive apt-get install -y -qq cloudflared 2>&1 | tail -8',
        'cloudflared --version',
    ])
    if not await _step(1, install_cmd, timeout=240): return

    # ── Step 2: Configure tunnel token ────────────────────────────────────
    # Validate the token can connect before writing the service
    validate_cmd = (
        f"cloudflared tunnel run --token {tunnel_token} --no-autoupdate "
        f"--pidfile /tmp/cf-check.pid & sleep 6; "
        f"kill $(cat /tmp/cf-check.pid 2>/dev/null) 2>/dev/null; "
        f"rm -f /tmp/cf-check.pid; echo 'token_check_done'"
    )
    ok, out = await _exec(validate_cmd, timeout=30)
    # We just need cloudflared to not error immediately — it will connect fine
    # (even a transient "still connecting" is acceptable here)
    if not ok and "error" in out.lower() and "token" in out.lower():
        tail = out[-800:] if len(out) > 800 else out
        await _update(2, failed_at=2, error=f"Token rejected by Cloudflare:\n{tail}")
        return
    await _update(3)

    # ── Step 3: Install systemd service ───────────────────────────────────
    # cloudflared service install uses the token directly — no config file needed
    svc_install_cmd = "\n".join([
        f"cloudflared service install {tunnel_token} 2>&1",
        "systemctl daemon-reload",
        "systemctl enable cloudflared 2>&1 || true",
    ])
    if not await _step(3, svc_install_cmd, timeout=60): return

    # ── Step 4: Start tunnel ───────────────────────────────────────────────
    if not await _step(4, "systemctl start cloudflared 2>&1", timeout=30): return

    # ── Step 5: Verify ─────────────────────────────────────────────────────
    await _update(5)
    ok, out = await _exec("systemctl is-active cloudflared 2>&1", timeout=20)
    if not ok or out.strip() != "active":
        logs_ok, logs_out = await _exec(
            "journalctl -u cloudflared -n 30 --no-pager 2>&1 || true", timeout=20
        )
        await _update(5, failed_at=5, error=logs_out[-800:] if logs_out else out)
        return
    await _update(6)

    # ── Step 6: Detect local services ─────────────────────────────────────
    # Check whether Pterodactyl Panel and/or Wings are installed so we can
    # build accurate Cloudflare Zero Trust routing instructions for the DM.

    # Panel: nginx serving on 80 (HTTP) or 443 (HTTPS via Let's Encrypt)
    panel_detected   = False
    panel_port       = 80
    panel_service    = "HTTP"
    panel_host       = "127.0.0.1"

    _, panel_active = await _exec("systemctl is-active nginx 2>&1", timeout=10)
    _, panel_dir    = await _exec("test -d /var/www/pterodactyl/public && echo FOUND", timeout=10)
    if panel_active.strip() == "active" and "FOUND" in panel_dir:
        panel_detected = True
        # Check if certbot set up HTTPS on port 443
        _, ssl_check = await _exec(
            "grep -r 'listen 443' /etc/nginx/sites-enabled/ 2>/dev/null && echo HAS_SSL || true",
            timeout=10,
        )
        if "HAS_SSL" in ssl_check:
            panel_port    = 443
            panel_service = "HTTPS"

    # Wings: default 8080 but may be customised in /etc/pterodactyl/config.yml
    wings_detected = False
    wings_port     = 8080
    wings_service  = "HTTPS"
    wings_host     = "127.0.0.1"

    _, wings_active = await _exec("systemctl is-active wings 2>&1", timeout=10)
    if wings_active.strip() == "active":
        wings_detected = True
        _, cfg_port = await _exec(
            "grep -E '^\\s*port:' /etc/pterodactyl/config.yml 2>/dev/null | head -1 | awk '{print $2}'",
            timeout=10,
        )
        cfg_port = cfg_port.strip()
        if cfg_port.isdigit():
            wings_port = int(cfg_port)
        # Wings always uses TLS on its API port
        wings_service = "HTTPS"

    await _update(len(steps))  # all done

    # ─── Completion embed in channel ──────────────────────────────────────
    done_embed = discord.Embed(
        title="✅  Cloudflare Tunnel Installed!",
        description=(
            f"The tunnel **`{tunnel_name}`** is running on `{container}`.\n\n"
            f"📬 Configuration instructions have been sent to your DMs."
        ),
        color=0x57F287,
    )
    if logo:
        done_embed.set_author(name=f"{_brand()}  •  Template System", icon_url=logo)
        done_embed.set_footer(text=f"{_brand()}  •  Keep your tunnel token private", icon_url=logo)
    else:
        done_embed.set_footer(text=f"{_brand()}  •  Keep your tunnel token private")

    try:
        await msg.edit(embed=done_embed, view=None)
    except Exception:
        pass

    # ─── DM — interactive Zero Trust routing guide ────────────────────────
    await _send_cloudflare_guide_dm(
        user          = user,
        tunnel_name   = tunnel_name,
        container     = container,
        panel_detected= panel_detected,
        panel_port    = panel_port,
        panel_service = panel_service,
        wings_detected= wings_detected,
        wings_port    = wings_port,
        wings_service = wings_service,
        logo          = logo,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Cloudflare Guide — embed builders
# ─────────────────────────────────────────────────────────────────────────────

def _cf_main_embed(
    tunnel_name:    str,
    container:      str,
    panel_detected: bool,
    panel_port:     int,
    panel_service:  str,
    wings_detected: bool,
    wings_port:     int,
    wings_service:  str,
    logo:           str = "",
) -> discord.Embed:
    b = _brand()
    status: list[str] = []
    if panel_detected:
        status.append(f"✅  Panel detected — {panel_service} :{panel_port}")
    else:
        status.append("⬜  Pterodactyl Panel — not detected (generic guide available)")
    if wings_detected:
        status.append(f"✅  Wings detected — {wings_service} :{wings_port}")
    else:
        status.append("⬜  Pterodactyl Wings — not detected (generic guide available)")

    # Only say "is running" when we know the real tunnel name (post-install flow).
    # For the generic /cloudflare guide, show a neutral introduction instead.
    _is_generic = (tunnel_name == "your-tunnel" and not container)
    if _is_generic:
        desc = (
            "Select a service below to get **step-by-step** instructions for routing it "
            "through your Cloudflare Zero Trust Tunnel.\n\n"
            + "\n".join(status)
        )
    else:
        desc = (
            f"Your tunnel **`{tunnel_name}`**"
            + (f" on `{container}`" if container else "")
            + " is running.\n\n"
            "Use the dropdown below to select a service and get **step-by-step** "
            "instructions for the Cloudflare Zero Trust Dashboard.\n\n"
            + "\n".join(status)
        )
    embed = discord.Embed(title="☁️  Cloudflare Tunnel — Setup Guide", description=desc, color=0x000000)
    embed.add_field(
        name="📍  Dashboard Path",
        value=(
            "**dash.cloudflare.com**\n"
            f"→ Zero Trust → Networks → Tunnels → **`{tunnel_name}`**\n"
            "→ Public Hostnames → **Add a Public Hostname**"
        ),
        inline=False,
    )
    if logo:
        embed.set_author(name=f"{b}  •  Cloudflare Guide", icon_url=logo)
        embed.set_footer(text=f"{b}  •  Select a service below", icon_url=logo)
    else:
        embed.set_footer(text=f"{b}  •  Select a service below")
    return embed


def _cf_panel_embed(
    tunnel_name: str, port: int, service: str, detected: bool, logo: str = ""
) -> discord.Embed:
    b      = _brand()
    scheme = "https" if service == "HTTPS" else "http"
    note   = "✅  Auto-detected from your VPS" if detected else "ℹ️  Generic guide — adjust port/service if needed"
    embed  = discord.Embed(title="🦖  Pterodactyl Panel — Cloudflare Setup", color=0x000000)
    embed.add_field(name="🔍  Detection", value=note, inline=False)
    embed.add_field(
        name="1️⃣  Open Cloudflare Dashboard",
        value=(
            "**dash.cloudflare.com**\n"
            f"Zero Trust → Networks → Tunnels → **`{tunnel_name}`**\n"
            "→ Public Hostnames → **Add a Public Hostname**"
        ),
        inline=False,
    )
    embed.add_field(
        name="2️⃣  Enter These Values",
        value=(
            "```\n"
            "Hostname : panel.yourdomain.com\n"
            f"Service  : {service}\n"
            "URL/Host : 127.0.0.1\n"
            f"Port     : {port}\n"
            "```"
        ),
        inline=False,
    )
    embed.add_field(
        name="3️⃣  Advanced Settings",
        value="→ **TLS Verify** → **OFF**\nLeave everything else at its default.",
        inline=False,
    )
    embed.add_field(
        name="4️⃣  Click Save",
        value=f"Your Panel will be live at:\n**`{scheme}://panel.yourdomain.com`**",
        inline=False,
    )
    if logo:
        embed.set_author(name=f"{b}  •  Cloudflare Guide", icon_url=logo)
        embed.set_footer(text=f"{b}  •  Replace yourdomain.com with your actual domain", icon_url=logo)
    else:
        embed.set_footer(text=f"{b}  •  Replace yourdomain.com with your actual domain")
    return embed


def _cf_wings_embed(
    tunnel_name: str, port: int, service: str, detected: bool, logo: str = ""
) -> discord.Embed:
    b    = _brand()
    note = "✅  Auto-detected from your VPS" if detected else "ℹ️  Generic guide — default Wings port is 8080"
    embed = discord.Embed(title="🪽  Pterodactyl Wings — Cloudflare Setup", color=0x000000)
    embed.add_field(name="🔍  Detection", value=note, inline=False)
    embed.add_field(
        name="1️⃣  Open Cloudflare Dashboard",
        value=(
            "**dash.cloudflare.com**\n"
            f"Zero Trust → Networks → Tunnels → **`{tunnel_name}`**\n"
            "→ Public Hostnames → **Add a Public Hostname**"
        ),
        inline=False,
    )
    embed.add_field(
        name="2️⃣  Enter These Values",
        value=(
            "```\n"
            "Hostname : wings.yourdomain.com\n"
            f"Service  : {service}\n"
            "URL/Host : 127.0.0.1\n"
            f"Port     : {port}\n"
            "```"
        ),
        inline=False,
    )
    embed.add_field(
        name="3️⃣  Advanced Settings",
        value="→ **TLS Verify** → **OFF**\nLeave everything else at its default.",
        inline=False,
    )
    embed.add_field(
        name="4️⃣  Click Save",
        value="Your Wings node will be reachable at:\n**`https://wings.yourdomain.com`**",
        inline=False,
    )
    embed.add_field(
        name="5️⃣  Connect to Panel",
        value=(
            "Panel → **Admin → Nodes → [your node] → Settings**\n"
            "Set **FQDN** to `wings.yourdomain.com`\n"
            "Enable **Behind Cloudflare Proxy** if the option appears."
        ),
        inline=False,
    )
    if logo:
        embed.set_author(name=f"{b}  •  Cloudflare Guide", icon_url=logo)
        embed.set_footer(text=f"{b}  •  Replace yourdomain.com with your actual domain", icon_url=logo)
    else:
        embed.set_footer(text=f"{b}  •  Replace yourdomain.com with your actual domain")
    return embed


def _cf_custom_embed(tunnel_name: str, logo: str = "") -> discord.Embed:
    b     = _brand()
    embed = discord.Embed(title="🌐  Custom Service — Cloudflare Setup", color=0x000000)
    embed.add_field(
        name="1️⃣  Open Cloudflare Dashboard",
        value=(
            "**dash.cloudflare.com**\n"
            f"Zero Trust → Networks → Tunnels → **`{tunnel_name}`**\n"
            "→ Public Hostnames → **Add a Public Hostname**"
        ),
        inline=False,
    )
    embed.add_field(
        name="2️⃣  Enter These Values",
        value=(
            "```\n"
            "Hostname : yourapp.yourdomain.com\n"
            "Service  : HTTP  (or HTTPS if your app uses TLS)\n"
            "URL/Host : 127.0.0.1\n"
            "Port     : <your app's local port>\n"
            "```"
        ),
        inline=False,
    )
    embed.add_field(
        name="3️⃣  Advanced Settings  (HTTPS only)",
        value="→ **TLS Verify** → **OFF**\nOnly needed if your service runs HTTPS locally.",
        inline=False,
    )
    embed.add_field(
        name="4️⃣  Click Save",
        value="Your service will be live at:\n**`https://yourapp.yourdomain.com`**",
        inline=False,
    )
    if logo:
        embed.set_author(name=f"{b}  •  Cloudflare Guide", icon_url=logo)
        embed.set_footer(text=f"{b}  •  Replace values with your actual service details", icon_url=logo)
    else:
        embed.set_footer(text=f"{b}  •  Replace values with your actual service details")
    return embed


# ─────────────────────────────────────────────────────────────────────────────
# Cloudflare Guide — interactive dropdown view
# ─────────────────────────────────────────────────────────────────────────────

class _CloudflareGuideView(discord.ui.View):
    """Interactive guide — sent to DMs or as ephemeral in guild channels."""

    def __init__(
        self,
        tunnel_name:    str  = "your-tunnel",
        container:      str  = "",
        panel_detected: bool = False,
        panel_port:     int  = 80,
        panel_service:  str  = "HTTP",
        wings_detected: bool = False,
        wings_port:     int  = 8080,
        wings_service:  str  = "HTTPS",
        logo:           str  = "",
    ):
        super().__init__(timeout=3600)   # 1-hour window; avoids ghost views after bot restarts
        self.tunnel_name    = tunnel_name
        self.container      = container
        self.panel_detected = panel_detected
        self.panel_port     = panel_port
        self.panel_service  = panel_service
        self.wings_detected = wings_detected
        self.wings_port     = wings_port
        self.wings_service  = wings_service
        self.logo           = logo
        self.add_item(_CloudflareGuideSelect(self))

    def main_embed(self) -> discord.Embed:
        return _cf_main_embed(
            self.tunnel_name, self.container,
            self.panel_detected, self.panel_port, self.panel_service,
            self.wings_detected, self.wings_port, self.wings_service,
            self.logo,
        )


class _CloudflareGuideSelect(discord.ui.Select):
    def __init__(self, parent: _CloudflareGuideView):
        self._parent = parent
        p = parent
        options = [
            discord.SelectOption(
                label="🦖  Pterodactyl Panel",
                description=(
                    f"Detected — {p.panel_service} :{p.panel_port}"
                    if p.panel_detected else
                    f"Generic guide — HTTP :{p.panel_port}"
                ),
                value="panel",
                emoji="🦖",
            ),
            discord.SelectOption(
                label="🪽  Pterodactyl Wings",
                description=(
                    f"Detected — {p.wings_service} :{p.wings_port}"
                    if p.wings_detected else
                    f"Generic guide — HTTPS :{p.wings_port}"
                ),
                value="wings",
                emoji="🪽",
            ),
            discord.SelectOption(
                label="🌐  Custom Service",
                description="Route any other local service through the tunnel",
                value="custom",
                emoji="🌐",
            ),
        ]
        super().__init__(
            placeholder="Select a service to configure…",
            options=options,
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction):
        try:
            p      = self._parent
            choice = self.values[0]
            if choice == "panel":
                embed = _cf_panel_embed(p.tunnel_name, p.panel_port, p.panel_service, p.panel_detected, p.logo)
            elif choice == "wings":
                embed = _cf_wings_embed(p.tunnel_name, p.wings_port, p.wings_service, p.wings_detected, p.logo)
            else:
                embed = _cf_custom_embed(p.tunnel_name, p.logo)
            await interaction.response.edit_message(embed=embed, view=_CloudflareGuideDetailView(p))
        except discord.NotFound:
            # Original message no longer exists (e.g. deleted)
            await interaction.response.send_message(
                "⚠️  This guide has expired. Run `/cloudflare` again to open a fresh one.",
                ephemeral=True,
            )
        except Exception as exc:
            logger.error(f"[cloudflare] guide select callback error: {exc}")
            try:
                await interaction.response.send_message(
                    "❌  Something went wrong. Please run `/cloudflare` again.",
                    ephemeral=True,
                )
            except Exception:
                pass


class _CloudflareGuideDetailView(discord.ui.View):
    """Detail screen — just a Back button that returns to the main menu."""

    def __init__(self, parent: _CloudflareGuideView):
        super().__init__(timeout=None)
        self._parent = parent

    @discord.ui.button(label="← Back to Menu", style=discord.ButtonStyle.secondary)
    async def back_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        p    = self._parent
        view = _CloudflareGuideView(
            tunnel_name    = p.tunnel_name,
            container      = p.container,
            panel_detected = p.panel_detected,
            panel_port     = p.panel_port,
            panel_service  = p.panel_service,
            wings_detected = p.wings_detected,
            wings_port     = p.wings_port,
            wings_service  = p.wings_service,
            logo           = p.logo,
        )
        await interaction.response.edit_message(embed=view.main_embed(), view=view)


# ─────────────────────────────────────────────────────────────────────────────
# Cloudflare Guide — DM sender (used by installer and standalone commands)
# ─────────────────────────────────────────────────────────────────────────────

async def _send_cloudflare_guide_dm(
    user:           discord.User | discord.Member,
    tunnel_name:    str  = "your-tunnel",
    container:      str  = "",
    panel_detected: bool = False,
    panel_port:     int  = 80,
    panel_service:  str  = "HTTP",
    wings_detected: bool = False,
    wings_port:     int  = 8080,
    wings_service:  str  = "HTTPS",
    logo:           str  = "",
) -> bool:
    """Send the interactive Cloudflare guide to a user's DMs. Returns True on success."""
    try:
        view = _CloudflareGuideView(
            tunnel_name    = tunnel_name,
            container      = container,
            panel_detected = panel_detected,
            panel_port     = panel_port,
            panel_service  = panel_service,
            wings_detected = wings_detected,
            wings_port     = wings_port,
            wings_service  = wings_service,
            logo           = logo,
        )
        await user.send(embed=view.main_embed(), view=view)
        return True
    except discord.Forbidden:
        logger.warning(f"[cloudflare] Could not DM guide to {user}")
        return False
    except Exception as exc:
        logger.error(f"[cloudflare] Guide DM error: {exc}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Command registration — called from bot.py
# ─────────────────────────────────────────────────────────────────────────────
def register_commands(bot: discord.ext.commands.Bot) -> None:
    @bot.tree.command(
        name="template",
        description="Install a ready-made template (Pterodactyl, etc.) on one of your VPS instances",
    )
    async def template_cmd(interaction: discord.Interaction) -> None:
        user_id  = str(interaction.user.id)
        vps_list = (_vps_data or {}).get(user_id, [])

        logo = _get_logo_url() if _get_logo_url else ""

        if not vps_list:
            embed = discord.Embed(
                title="❌  No VPS Found",
                description=(
                    "You don't have any VPS instances yet.\n"
                    "Use `!manage` to deploy one first."
                ),
                color=0xED4245,
            )
            if logo:
                embed.set_author(name=f"{_brand()}  •  Template System", icon_url=logo)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        embed = _vps_select_embed(logo)
        view  = _VPSSelectView(vps_list)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    # ── /cloudflare slash command ──────────────────────────────────────────
    @bot.tree.command(
        name="cloudflare",
        description="Get step-by-step Cloudflare Tunnel routing instructions (works in DMs too)",
    )
    async def cloudflare_slash(interaction: discord.Interaction) -> None:
        logo = _get_logo_url() if _get_logo_url else ""
        view = _CloudflareGuideView(logo=logo)
        # Works in a guild channel (ephemeral) or in a DM
        try:
            await interaction.response.send_message(
                embed=view.main_embed(), view=view, ephemeral=True
            )
        except discord.HTTPException:
            # DM context — ephemeral not supported, send normally
            await interaction.response.send_message(embed=view.main_embed(), view=view)

    # ── !cloudflare prefix command ─────────────────────────────────────────
    # Works in guild channels and in the bot's DMs.
    @bot.command(name="cloudflare", aliases=["cf", "cftunnel"])
    async def cloudflare_prefix(ctx: discord.ext.commands.Context) -> None:
        logo = _get_logo_url() if _get_logo_url else ""
        view = _CloudflareGuideView(logo=logo)
        await ctx.send(embed=view.main_embed(), view=view)
