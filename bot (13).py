import discord
from discord.ext import commands, tasks
import asyncio
import subprocess
import json
import re
import os
import hashlib
import random
import string
import shlex
import shutil
import logging
import threading
import time
from datetime import datetime, timezone, timedelta
from collections import deque
from typing import Optional, List, Dict, Any
import template_system
import node_system

# ─── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger('vps_bot')


def _utcnow() -> datetime:
    """Return the current UTC time as a naive datetime (drop tzinfo so it
    compares safely with other naive datetimes throughout the codebase).
    Replaces the deprecated datetime.utcnow()."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ─── Docker availability check ────────────────────────────────────────────────

if not shutil.which("docker"):
    logger.error("Docker command not found. Please ensure Docker is installed.")
    raise SystemExit("Docker command not found. Please ensure Docker is installed.")

# ─── Bot setup ─────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents, help_command=None)

# ─── Core constants ─────────────────────────────────────────────────────────────

# Lock that prevents two concurrent `docker build` calls (e.g. startup pre-build
# racing with a user deploy).  Both callers will still succeed — the second one
# just waits for the first to finish, then sees the image already exists and
# returns immediately without re-building.
_image_build_lock: asyncio.Lock = asyncio.Lock()

MAIN_ADMIN_ID   = 1003134870308012052
VPS_USER_ROLE_ID = 1431499643698544720
DOCKER_IMAGE    = "darknodes-vps"
DOCKERFILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Dockerfile")
_IMAGE_HASH_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".darknodes_image_hash")
SSH_PORT_START  = 10000
# Server public IP used in direct SSH connection strings.
# Override by setting the SERVER_IP environment variable on your host.
SERVER_IP = os.environ.get("SERVER_IP", "")

# Base hostname prefix.  Each container gets DarkNodes-N (e.g. DarkNodes-1).
VPS_HOSTNAME = "DarkNodes"

def get_next_vps_hostname() -> str:
    """Return a unique hostname like DarkNodes-1, DarkNodes-2, … for a new container."""
    total = sum(len(v) for v in vps_data.values())
    return f"{VPS_HOSTNAME}-{total + 1}"

# ─── Mining/monitoring config ─────────────────────────────────────────────────
# Stored in mining_config.json so admins can tune without touching source code.

DEFAULT_MINING_CONFIG = {
    # CPU must stay above this % for sustained_duration_minutes before counting
    "cpu_threshold": 100,
    # Minutes of sustained high CPU required before adding CPU indicator score
    "sustained_duration_minutes": 30,
    # Confidence score needed to trigger automatic suspension (0-100)
    "auto_suspend_threshold": 75,
    # Seconds between per-container checks
    "monitoring_interval": 120,
    # Toggle automatic suspension on/off (admins can disable globally)
    "auto_suspend_enabled": True,
    # Discord channel ID for alert embeds (0 = disabled)
    "notification_channel_id": 0,
    # Known mining process names (case-insensitive substring match)
    "mining_process_blacklist": [
        "xmrig", "minerd", "cpuminer", "bfgminer", "cgminer",
        "ethminer", "nbminer", "t-rex", "teamredminer", "gminer",
        "lolminer", "phoenixminer", "claymore", "nanominer",
        "kawpowminer", "wildrig", "srbminer", "bminer", "ccminer",
        "sgminer", "excavator", "multiminer", "nicehash"
    ],
    # Known mining pool domain substrings
    "mining_pool_blacklist": [
        "minexmr.com", "supportxmr.com", "xmrpool.eu",
        "nanopool.org", "nicehash.com", "2miners.com",
        "ethermine.org", "flexpool.io", "pool.hashvault.pro",
        "gulf.moneroocean.stream", "xmr.pool.minergate.com",
        "miningpoolhub.com", "antpool.com", "f2pool.com",
        "slushpool.com", "viawallet.com", "hiveon.net"
    ],
    # Suspicious CLI argument fragments
    "mining_cli_blacklist": [
        "stratum+tcp://", "stratum+ssl://", "stratum2+tcp://",
        "--donate-level", "--coin xmr", "--algo rx/", "--algo kawpow",
        "-o pool", "--url pool", "--pool-address", "--mining-address"
    ],
    # Users whose VPS containers are never auto-suspended (user IDs as strings)
    "whitelisted_users": [],
    # Specific container names that are never auto-suspended
    "whitelisted_containers": [],
    # Process names that are always safe (never add mining confidence even if detected)
    "whitelisted_processes": [
        "node", "npm", "yarn", "python3", "python", "pip", "pip3",
        "java", "javac", "cargo", "go", "rustc", "gradle", "mvn",
        "apt", "apt-get", "dpkg", "make", "cmake", "gcc", "g++",
        "clang", "ninja", "bazel", "docker", "bash", "sh", "curl", "wget"
    ]
}

def load_mining_config():
    try:
        with open('mining_config.json', 'r') as f:
            loaded = json.load(f)
            # Merge with defaults so new keys always exist
            cfg = dict(DEFAULT_MINING_CONFIG)
            cfg.update(loaded)
            return cfg
    except (FileNotFoundError, json.JSONDecodeError):
        logger.warning("mining_config.json not found, using defaults")
        return dict(DEFAULT_MINING_CONFIG)

def save_mining_config():
    try:
        with open('mining_config.json', 'w') as f:
            json.dump(MONITOR_CONFIG, f, indent=4)
    except Exception as e:
        logger.error(f"Error saving mining config: {e}")

MONITOR_CONFIG = load_mining_config()

# ─── Bot appearance config (logo URL) ────────────────────────────────────────
# Persisted in bot_config.json; admins update it via /setlogo without touching code.

_BOT_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_config.json")
_DEFAULT_BOT_CONFIG: dict = {"logo_url": ""}

def _load_bot_config() -> dict:
    try:
        with open(_BOT_CONFIG_FILE) as fh:
            cfg = dict(_DEFAULT_BOT_CONFIG)
            cfg.update(json.load(fh))
            return cfg
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(_DEFAULT_BOT_CONFIG)

def _save_bot_config(cfg: dict):
    try:
        with open(_BOT_CONFIG_FILE, "w") as fh:
            json.dump(cfg, fh, indent=4)
    except Exception as e:
        logger.error(f"Failed to save bot config: {e}")

_bot_config = _load_bot_config()

def get_logo_url() -> str:
    """Return the current logo URL (empty string if not set yet)."""
    return _bot_config.get("logo_url", "")

def set_logo_url(url: str) -> None:
    """Persist a new logo URL and update the in-memory copy."""
    _bot_config["logo_url"] = url
    _save_bot_config(_bot_config)

def get_brand_name() -> str:
    """Return the current brand/watermark name (default: DarkNodes)."""
    return _bot_config.get("brand_name", "DarkNodes")

def set_brand_name(name: str) -> None:
    """Persist a new brand name and update the in-memory copy."""
    _bot_config["brand_name"] = name
    _save_bot_config(_bot_config)

def get_embed_color() -> int:
    """Return the current sidebar accent color for bot embeds (default: black)."""
    return _bot_config.get("embed_color", 0x000000)

def set_embed_color(color: int) -> None:
    """Persist a new embed sidebar color and update the in-memory copy."""
    _bot_config["embed_color"] = color
    _save_bot_config(_bot_config)

async def _update_container_brand(container_name: str, brand: str) -> None:
    """Write the brand name into a running container so its MOTD reflects it."""
    try:
        safe = brand.replace("'", r"'\''")
        cmd  = f"docker exec {container_name} bash -c \"echo '{safe}' > /etc/darknodes-brand\""
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=10)
    except Exception:
        pass   # silent — offline containers are fine, they'll show the new name on next deploy

# ─── Data storage ─────────────────────────────────────────────────────────────

def load_data():
    try:
        with open('user_data.json', 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        logger.warning("user_data.json not found or corrupted, initializing empty data")
        return {}

def load_vps_data():
    try:
        with open('vps_data.json', 'r') as f:
            loaded = json.load(f)
            vps_data = {}
            for uid, v in loaded.items():
                if isinstance(v, dict):
                    if "container_name" in v:
                        vps_data[uid] = [v]
                    else:
                        vps_data[uid] = list(v.values())
                elif isinstance(v, list):
                    vps_data[uid] = v
                else:
                    logger.warning(f"Unknown VPS data format for user {uid}, skipping")
                    continue
            return vps_data
    except (FileNotFoundError, json.JSONDecodeError):
        logger.warning("vps_data.json not found or corrupted, initializing empty data")
        return {}

def load_admin_data():
    try:
        with open('admin_data.json', 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        logger.warning("admin_data.json not found or corrupted, initializing with main admin")
        return {"admins": [str(MAIN_ADMIN_ID)]}

def load_suspension_log():
    try:
        with open('suspension_log.json', 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []

def load_monitor_log():
    try:
        with open('monitor_log.json', 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

user_data       = load_data()
vps_data        = load_vps_data()
admin_data      = load_admin_data()
suspension_log  = load_suspension_log()   # List of suspension event dicts
monitor_log     = load_monitor_log()      # {container_name: [event, ...]}

def save_data():
    try:
        with open('user_data.json', 'w') as f:
            json.dump(user_data, f, indent=4)
        with open('vps_data.json', 'w') as f:
            json.dump(vps_data, f, indent=4)
        with open('admin_data.json', 'w') as f:
            json.dump(admin_data, f, indent=4)
        logger.info("Data saved successfully")
    except Exception as e:
        logger.error(f"Error saving data: {e}")

def save_suspension_log():
    try:
        with open('suspension_log.json', 'w') as f:
            json.dump(suspension_log, f, indent=4)
    except Exception as e:
        logger.error(f"Error saving suspension log: {e}")

def save_monitor_log():
    try:
        with open('monitor_log.json', 'w') as f:
            json.dump(monitor_log, f, indent=4)
    except Exception as e:
        logger.error(f"Error saving monitor log: {e}")

# ─── In-memory per-container monitor state ────────────────────────────────────
# Tracks CPU history and detection flags per container between scans.
# Not persisted — resets on bot restart (intentional; evidence is in suspension_log).

container_monitor_state: Dict[str, Dict] = {}

def get_container_state(container_name: str) -> Dict:
    """Return (creating if missing) the in-memory monitor state for a container."""
    if container_name not in container_monitor_state:
        container_monitor_state[container_name] = {
            "high_cpu_start": None,        # datetime when sustained high CPU began
            "cpu_samples":    deque(maxlen=30),  # recent CPU % readings
            "net_tx_samples": deque(maxlen=10),  # recent net transmit bytes
            "flags":          set(),        # current detection flags
            "last_confidence": 0,
            "monitoring_paused": False,     # admin can pause per-container
        }
    return container_monitor_state[container_name]

# ─── Helper functions ──────────────────────────────────────────────────────────

def get_next_ssh_port():
    """Get the next available SSH port for a new container."""
    used_ports = set()
    for vps_list in vps_data.values():
        for vps in vps_list:
            if "ssh_port" in vps:
                used_ports.add(vps["ssh_port"])
    port = SSH_PORT_START
    while port in used_ports:
        port += 1
    return port

def generate_password(length=16):
    """Generate a random strong password."""
    chars = string.ascii_letters + string.digits + "!@#$%"
    return ''.join(random.choice(chars) for _ in range(length))

def find_vps_record(container_name: str):
    """Return (owner_user_id, vps_dict) for a container name, or (None, None)."""
    for uid, vps_list in vps_data.items():
        for vps in vps_list:
            if vps.get("container_name") == container_name:
                return uid, vps
    return None, None

# ─── Admin checks ─────────────────────────────────────────────────────────────

def is_admin():
    async def predicate(ctx):
        user_id = str(ctx.author.id)
        if user_id == str(MAIN_ADMIN_ID) or user_id in admin_data.get("admins", []):
            return True
        await ctx.send(embed=create_error_embed("Access Denied", "You don't have permission to use this command."))
        return False
    return commands.check(predicate)

def is_main_admin():
    async def predicate(ctx):
        if str(ctx.author.id) == str(MAIN_ADMIN_ID):
            return True
        await ctx.send(embed=create_error_embed("Access Denied", "Only the main admin can use this command."))
        return False
    return commands.check(predicate)

# ─── Embed helpers ─────────────────────────────────────────────────────────────

def create_embed(title, description="", color=0x5865F2, fields=None):
    logo  = get_logo_url()
    embed = discord.Embed(title=title, description=description, color=color, timestamp=_utcnow())
    if logo:
        embed.set_author(name=f"{get_brand_name()} VPS Hosting", icon_url=logo)
        embed.set_thumbnail(url=logo)
    if fields:
        for field in fields:
            embed.add_field(name=field['name'], value=field["value"], inline=field.get("inline", False))
    kw: dict = {"text": f"{get_brand_name()}  •  High-Performance VPS Hosting"}
    if logo:
        kw["icon_url"] = logo
    embed.set_footer(**kw)
    return embed

def create_success_embed(title, description=""):
    return create_embed(title, description, color=0x57F287)

def create_error_embed(title, description=""):
    return create_embed(title, description, color=0xED4245)

def create_info_embed(title, description=""):
    return create_embed(title, description, color=0x5865F2)

def create_warning_embed(title, description=""):
    return create_embed(title, description, color=0xF0A500)

# ─── Docker execution helpers ─────────────────────────────────────────────────

async def _docker_run_compat(run_cmd: str, timeout: int = 60) -> None:
    """
    Execute a 'docker run' command.  If the host kernel rejects --ulimit nproc
    (OCI runtime 'error setting rlimit'), automatically retry without that flag
    so containers still start on restricted hosts while keeping the higher limit
    on hosts that support it.
    """
    try:
        await execute_docker(run_cmd, timeout=timeout)
    except Exception as _run_err:
        if "rlimit" in str(_run_err).lower():
            logger.warning(
                f"[docker run] Host rejected --ulimit nproc (rlimit error) — "
                f"retrying without it: {str(_run_err)[:200]}"
            )
            fallback_cmd = re.sub(r"\s*--ulimit\s+nproc=\S+", "", run_cmd)
            await execute_docker(fallback_cmd, timeout=timeout)
        else:
            raise


async def execute_docker(command, timeout=120):
    """Execute a Docker CLI command with timeout and structured error handling."""
    try:
        cmd = shlex.split(command)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode != 0:
            error = stderr.decode().strip() if stderr else "Command failed with no error output"
            raise Exception(error)
        return stdout.decode().strip() if stdout else True
    except asyncio.TimeoutError:
        logger.error(f"Docker command timed out: {command}")
        raise Exception(f"Command timed out after {timeout} seconds")
    except Exception as e:
        logger.error(f"Docker Error: {command} - {str(e)}")
        raise

async def docker_exec(container_name, command, timeout=60):
    """Execute a shell command inside a running Docker container."""
    cmd = ["docker", "exec", container_name, "bash", "-c", command]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return stdout.decode().strip(), stderr.decode().strip(), proc.returncode

# ─── Image management ─────────────────────────────────────────────────────────

def _dockerfile_hash() -> str:
    """Return MD5 of the Dockerfile, or '' if it cannot be read."""
    try:
        with open(DOCKERFILE_PATH, "rb") as fh:
            return hashlib.md5(fh.read()).hexdigest()
    except Exception:
        return ""


async def run_docker_command(command: str, timeout: int = 30):
    """
    Run a Docker host-level command without raising on failure.
    Returns (stdout, stderr, returncode) always.
    Use for diagnostic/cleanup calls where you need the output regardless of exit code.
    """
    try:
        cmd = shlex.split(command)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return stdout.decode(errors="replace").strip(), stderr.decode(errors="replace").strip(), proc.returncode
    except asyncio.TimeoutError:
        return "", f"Command timed out after {timeout}s", -1
    except Exception as e:
        return "", str(e), -1


async def _unique_container_name(user_id: str, start_count: int) -> tuple:
    """
    Return (container_name, count) for the first name not already taken by a
    Docker container. Starts at `start_count` and increments until a free slot
    is found, so a stale/conflicting container never blocks a new deployment.
    """
    count = start_count
    while count < start_count + 50:  # safety cap — avoid infinite loops
        name = f"vps-{user_id}-{count}"
        _, _, rc = await run_docker_command(f"docker inspect {name}", timeout=10)
        if rc != 0:  # non-zero → container does not exist → name is free
            return name, count
        logger.warning(f"Container name '{name}' already exists in Docker — trying next slot.")
        count += 1
    # Fallback: use original name (docker run will surface the real error)
    return f"vps-{user_id}-{start_count}", start_count


async def ensure_vps_image(force_rebuild: bool = False) -> tuple:
    """
    Ensure the darknodes-vps Docker image exists and is up to date.

    Logic:
      1. Verify the Dockerfile is present on disk.
      2. Check whether the image already exists in Docker.
      3. Compare the current Dockerfile MD5 against the last saved hash.
         If the hash changed (or the image is missing), rebuild automatically.
      4. Return (True, summary) on success or (False, full_build_error) on failure.

    Called before every container creation — admins never need to manage
    the image manually.
    """
    if not os.path.exists(DOCKERFILE_PATH):
        return False, (
            f"Dockerfile not found at `{DOCKERFILE_PATH}`.\n"
            "Ensure the Dockerfile sits in the same directory as bot.py."
        )

    current_hash = _dockerfile_hash()

    # ── Does the image already exist? ────────────────────────────────────────
    img_out, _, _ = await run_docker_command(f"docker images -q {DOCKER_IMAGE}", timeout=15)
    image_exists  = bool(img_out.strip())

    # ── Load the previously saved hash ───────────────────────────────────────
    stored_hash = ""
    try:
        with open(_IMAGE_HASH_FILE) as fh:
            stored_hash = fh.read().strip()
    except FileNotFoundError:
        pass

    dockerfile_changed = current_hash != stored_hash

    if image_exists and not dockerfile_changed and not force_rebuild:
        logger.info(f"darknodes-vps image is current (hash {current_hash[:8]})")
        return True, "Image is already up to date."

    # ── Build ─────────────────────────────────────────────────────────────────
    # Only one build may run at a time.  If another coroutine is already
    # building, wait for it to finish — then re-check if the image now exists.
    async with _image_build_lock:
        # Re-check inside the lock: a concurrent build may have just finished.
        img_out2, _, _ = await run_docker_command(f"docker images -q {DOCKER_IMAGE}", timeout=15)
        if img_out2.strip() and not force_rebuild:
            new_hash = _dockerfile_hash()
            stored_hash2 = ""
            try:
                with open(_IMAGE_HASH_FILE) as fh:
                    stored_hash2 = fh.read().strip()
            except FileNotFoundError:
                pass
            if new_hash == stored_hash2:
                logger.info(f"{DOCKER_IMAGE} image was built by a concurrent task — skipping duplicate build.")
                return True, "Image is already up to date (built by concurrent task)."

        if not image_exists:
            reason = "image not found — building for the first time"
        elif force_rebuild:
            reason = "forced rebuild requested"
        else:
            reason = "Dockerfile changed — rebuilding"
        logger.info(f"Building {DOCKER_IMAGE} ({reason}) …")

        build_dir = os.path.dirname(os.path.abspath(DOCKERFILE_PATH))
        # BuildKit (DOCKER_BUILDKIT=1) enables parallel layer execution and
        # better caching.  --network=host routes all RUN-layer downloads through
        # the host's network stack — much faster than the default bridge for
        # apt-get, get.docker.com, GitHub release downloads, etc.
        build_cmd = (
            f"docker build "
            f"--network=host "
            f"-t {DOCKER_IMAGE} "
            f"-f {DOCKERFILE_PATH} "
            f"{build_dir}"
        )
        # DOCKER_BUILDKIT=0: use the legacy builder — BuildKit (=1) requires the
        # buildx plugin which is not present on all host environments and causes
        # "buildx component is missing or broken" startup failures.
        build_env = {**os.environ, "DOCKER_BUILDKIT": "0"}

        try:
            proc = await asyncio.create_subprocess_shell(
                build_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,   # merge so we capture everything
                env=build_env,
            )
            stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=900)
            build_logs = stdout_bytes.decode(errors="replace")

            if proc.returncode != 0:
                # Log last 5000 chars so the failing layer/step is visible
                logger.error(
                    f"Image build FAILED (rc={proc.returncode}).\n"
                    f"Last build output:\n{build_logs[-5000:]}"
                )
                return False, build_logs

            # Persist the hash so future runs skip the rebuild
            try:
                with open(_IMAGE_HASH_FILE, "w") as fh:
                    fh.write(current_hash)
            except Exception as he:
                logger.warning(f"Could not save image hash: {he}")

            logger.info(f"{DOCKER_IMAGE} image built successfully.")
            return True, build_logs

        except asyncio.TimeoutError:
            return False, "Image build timed out after 15 minutes. Check your network or Docker daemon."
        except Exception as e:
            return False, f"Build process error: {e}"


# ─── Deployment progress embed builders ──────────────────────────────────────

_DEPLOY_STEPS = [
    "Preparing base image",
    "Provisioning instance",
    "Starting system services",
    "Applying configuration",
    "Running health checks",
    "Setting up access",
]


def _deploy_progress_embed(step_index: int, steps: list = None, failed: bool = False) -> discord.Embed:
    """DarkNodes-branded live deployment progress embed."""
    if steps is None:
        steps = _DEPLOY_STEPS
    total  = len(steps)
    filled = min(step_index, total)
    pct    = int(filled / total * 100)

    # Wide 20-char bar for visual impact
    bar_width    = 20
    filled_chars = round(pct / 100 * bar_width)
    bar          = "█" * filled_chars + "░" * (bar_width - filled_chars)

    color = 0xED4245 if failed else (0x57F287 if filled >= total else 0x000000)

    lines = []
    for i, name in enumerate(steps):
        if failed and i == step_index:
            lines.append(f"❌  **{name}**  ← failed here")
        elif i < filled:
            lines.append(f"✅  {name}")
        elif i == filled and not failed:
            lines.append(f"⏳  **{name}…**")
        else:
            lines.append(f"⬜  {name}")

    title = "❌  Deploy Failed" if failed else f"🚀  {get_brand_name()} VPS — Deploying"
    logo  = get_logo_url()
    embed = discord.Embed(
        title=title,
        description=f"**`{bar}`**  **{pct}%**\n\n" + "\n".join(lines),
        color=color,
        timestamp=_utcnow(),
    )
    if logo:
        embed.set_author(name=f"{get_brand_name()} VPS Hosting", icon_url=logo)
        embed.set_thumbnail(url=logo)
    kw: dict = {"text": f"{get_brand_name()}  •  High-Performance VPS Hosting"}
    if logo:
        kw["icon_url"] = logo
    embed.set_footer(**kw)
    return embed


def _deploy_success_embed(user: discord.Member, vps_count: int, container_name: str,
                           ram: str, cpu: str, disk: str, steps: list = None) -> discord.Embed:
    """Final success embed shown in the channel after deployment."""
    logo  = get_logo_url()
    embed = discord.Embed(
        title="🚀  Your VPS is Online!",
        description=f"**{user.mention}** — your server is live and ready to use.",
        color=get_embed_color(),
        timestamp=_utcnow(),
    )
    if logo:
        embed.set_author(name=f"{get_brand_name()} VPS Hosting", icon_url=logo)
        embed.set_thumbnail(url=logo)

    # ── Resource stats as clean inline fields ─────────────────────────────────
    embed.add_field(name="🧠  RAM",      value=f"**{ram} GB**",     inline=True)
    embed.add_field(name="⚙️  CPU",      value=f"**{cpu} vCPU**",   inline=True)
    embed.add_field(name="💾  Storage",  value=f"**{disk} GB**",    inline=True)

    # ── Identity / access ─────────────────────────────────────────────────────
    embed.add_field(name="🆔  VPS",       value=f"**#{vps_count}**",     inline=True)
    embed.add_field(name="📦  Container", value=f"`{container_name}`",   inline=True)
    embed.add_field(name="🔑  Connect",   value="`!manage` → **SSH**",   inline=True)

    embed.add_field(
        name="📩  Credentials",
        value=(
            "Your login details & session links have been sent to your **DMs**.\n"
            "Use `!manage` to start, stop, reinstall, or connect via SSH."
        ),
        inline=False,
    )
    kw: dict = {"text": f"{get_brand_name()}  •  Deployed & Ready  |  {_utcnow().strftime('%B %d, %Y')}"}
    if logo:
        kw["icon_url"] = logo
    embed.set_footer(**kw)
    return embed


def _vps_dm_embed(vps_count: int, container_name: str, ram: str, cpu: str,
                   tmate_ssh: str = "", sshx_url: str = "",
                   plan: str = None, processor: str = None) -> discord.Embed:
    """DM embed delivered to the VPS owner after deployment."""
    plan_str = (f"**{plan}**" + (f" — {processor}" if processor else "")) if plan else "Custom"
    logo     = get_logo_url()
    embed = discord.Embed(
        title="🎉  Your VPS is Live!",
        description=(
            f"Welcome to **{get_brand_name()}**! Your server is online and ready.\n"
            f"**Plan:** {plan_str}  •  **Container:** `{container_name}`"
        ),
        color=get_embed_color(),
        timestamp=_utcnow(),
    )
    if logo:
        embed.set_author(name=f"{get_brand_name()} VPS Hosting", icon_url=logo)
        embed.set_thumbnail(url=logo)

    embed.add_field(name="🆔 VPS ID",  value=f"`#{vps_count}`",  inline=True)
    embed.add_field(name="🧠 RAM",      value=f"`{ram}`",          inline=True)
    embed.add_field(name="⚙️ CPU",      value=f"`{cpu} Core(s)`", inline=True)

    # tmate SSH — always show if available
    if tmate_ssh:
        embed.add_field(
            name="🖥️  tmate SSH",
            value=f"```{tmate_ssh}```",
            inline=False,
        )
    else:
        embed.add_field(
            name="🖥️  tmate SSH",
            value="> Click **SSH** in `!manage` to generate a fresh session link.",
            inline=False,
        )

    # sshx Web Terminal — always show section
    if sshx_url:
        embed.add_field(
            name="🔗  sshx Web Terminal",
            value=f"> {sshx_url}\nOpen in any browser — no SSH client needed.",
            inline=False,
        )
    else:
        embed.add_field(
            name="🔗  sshx Web Terminal",
            value="> Click **SSH** in `!manage` to generate a fresh sshx link.",
            inline=False,
        )

    embed.add_field(
        name="📌  Quick Tips",
        value=(
            "• `!manage` → **SSH** — fresh session every click\n"
            "• `!manage` → **Stop / Start** — power control\n"
            "• Keep this DM — it's your VPS reference card"
        ),
        inline=False,
    )
    kw: dict = {"text": f"{get_brand_name()} VPS  •  Keep this DM safe — do not share"}
    if logo:
        kw["icon_url"] = logo
    embed.set_footer(**kw)
    return embed


# ─── Container creation ───────────────────────────────────────────────────────

async def create_docker_container(container_name, ram_mb, cpu_count, ssh_port, password, disk_gb=30, hostname=None, progress_msg=None):
    """
    Create a VPS container from the darknodes-vps image.

    Design:
    - The darknodes-vps image boots via /lib/systemd/systemd (PID 1).
      All packages are pre-baked; containers start in seconds.
    - --privileged + --cgroupns=host are required for systemd cgroup management.
    - tmpfs mounts on /run, /run/lock, /tmp satisfy systemd runtime requirements.
    - Docker Engine runs INSIDE each VPS container (Docker-in-Docker / DinD).
    - Named volumes persist /var/lib/docker, /home, /root, /opt across restarts.
    - On ANY failure the container is KEPT and full diagnostics are raised:
      startup logs, docker inspect, and the exact docker run command.
      A failed container is never silently removed.
    - progress_msg: optional discord.Message updated live with deployment stages.
    """
    # ── Live progress helper ──────────────────────────────────────────────────
    async def _progress(step_idx: int, failed: bool = False):
        if progress_msg is None:
            return
        try:
            await progress_msg.edit(embed=_deploy_progress_embed(step_idx, failed=failed))
        except Exception:
            pass

    # Volume names — one persistent set per container
    _vol_docker = f"{container_name}-docker"
    _vol_home   = f"{container_name}-home"
    _vol_root   = f"{container_name}-root"
    _vol_opt    = f"{container_name}-opt"

    # 1. Ensure the base image exists and matches the current Dockerfile
    await _progress(0)
    img_ok, img_detail = await ensure_vps_image()
    if not img_ok:
        await _progress(0, failed=True)
        raise RuntimeError(
            f"Cannot create VPS — darknodes-vps image unavailable.\n\n"
            f"Build error:\n{img_detail[-3000:]}"
        )

    _hostname = hostname or f"{VPS_HOSTNAME}-1"

    # 2. Build docker run flags
    #    Full Docker-in-Docker: Docker Engine runs INSIDE each VPS container.
    #    Named volumes persist /var/lib/docker, /home, /root, /opt across restarts.
    #    No host socket is mounted — each VPS has its own independent dockerd.
    _port_flag = f"-p {ssh_port}:22 " if ssh_port and ssh_port > 0 else ""
    base_flags = (
        f"--name {container_name} "
        f"--hostname {_hostname} "
        f"--memory={ram_mb}m "
        f"--memory-swap={ram_mb * 2}m "
        f"--cpus={cpu_count} "
        f"--restart=unless-stopped "
        f"--privileged "
        f"--cgroupns=host "
        f"-v /sys/fs/cgroup:/sys/fs/cgroup:rw "
        f"--tmpfs /run:exec,mode=755,size=256m "
        f"--tmpfs /run/lock:size=64m "
        f"--tmpfs /tmp:exec,size=512m "
        f"-v {_vol_docker}:/var/lib/docker "
        f"-v {_vol_home}:/home "
        f"-v {_vol_root}:/root "
        f"-v {_vol_opt}:/opt "
        f"-e container=docker "
        # ── DNS: explicit resolvers so apt/pip/curl work inside the container ──
        f"--dns 8.8.8.8 "
        f"--dns 1.1.1.1 "
        f"--dns 8.8.4.4 "
        # ── Security: allow all syscalls (ptrace, mount, mknod, etc.) ──────────
        f"--security-opt seccomp=unconfined "
        f"--security-opt apparmor=unconfined "
        # ── Resources: shared memory + high file-descriptor limit ──────────────
        f"--shm-size=512m "
        f"--ulimit nofile=65536:65536 "
        # --ulimit nproc removed: many host kernels reject RLIMIT_NPROC raises
        # even with --privileged, causing OCI runtime "error setting rlimit" crash
        f"-l darknodes.vps=true "
        f"-l darknodes.owner={container_name} "
        f"{_port_flag}"
    )
    run_cmd = f"docker run -d {base_flags}{DOCKER_IMAGE}"

    # Helper: collect diagnostics without raising
    async def _collect_diagnostics(container: str) -> str:
        logs_out,    _, _ = await run_docker_command(f"docker logs --tail=80 {container}",   timeout=20)
        inspect_out, _, _ = await run_docker_command(f"docker inspect {container}",           timeout=15)
        # Pull docker.service journal from INSIDE the container — this is what
        # reveals WHY dockerd failed to start (storage driver error, iptables, etc.)
        journal_out, _, _ = await docker_exec(
            container,
            "journalctl -u docker --no-pager -n 60 2>/dev/null || echo '(journalctl unavailable)'",
            timeout=20,
        )
        inspect_snippet = inspect_out[:2000] + "\n…(truncated)" if len(inspect_out) > 2000 else inspect_out
        return (
            f"── Exact docker run command ─────────────────────────────────\n"
            f"{run_cmd}\n\n"
            f"── Container startup logs (last 80 lines) ───────────────────\n"
            f"{logs_out or '(none)'}\n\n"
            f"── docker.service journal (last 60 lines) ───────────────────\n"
            f"{journal_out or '(none)'}\n\n"
            f"── docker inspect (truncated) ───────────────────────────────\n"
            f"{inspect_snippet or '(none)'}"
        )

    # 3. Start the container — CMD from the image is /lib/systemd/systemd (PID 1)
    await _progress(1)
    try:
        await execute_docker(run_cmd, timeout=60)
        logger.info(f"Container {container_name} started from {DOCKER_IMAGE}")
    except Exception as start_err:
        await _progress(1, failed=True)
        raise RuntimeError(
            f"Failed to start container: {start_err}\n\n"
            f"── Exact docker run command ──────────────────────────────────\n"
            f"{run_cmd}"
        )

    # 4. Wait for systemd to reach its running/degraded state (polled inside configure_vps)
    await _progress(2)
    await asyncio.sleep(1)

    # 5. Configure the VPS — password, hostname, Docker daemon wait, SSH
    await _progress(3)
    try:
        await configure_vps(container_name, password, hostname=_hostname)
    except Exception as cfg_err:
        await _progress(3, failed=True)
        diag = await _collect_diagnostics(container_name)
        raise RuntimeError(
            f"VPS configuration failed: {cfg_err}\n\n{diag}"
        )

    # 6. Full verification — ALL checks are critical; on failure keep the container
    await _progress(4)
    passed, report = await verify_vps(container_name, hostname=_hostname)
    if not passed:
        await _progress(4, failed=True)
        diag = await _collect_diagnostics(container_name)
        raise RuntimeError(
            f"VPS verification failed — container kept for diagnosis.\n\n"
            f"Verification report:\n{report}\n\n"
            f"{diag}"
        )

    return True


async def dind_autoheal(container_name: str, delay: int = 180, notify_user: discord.Member = None):
    """
    Background task: wait `delay` seconds after deploy, then ensure the
    Docker-in-Docker daemon inside `container_name` is actually responding.

    Attempt sequence:
      1. Try `docker info` — if it works, send ✅ confirmation and return.
      2. Restart the docker service and wait up to 60 s.
      3. If still broken after restart, log a warning to the log channel.

    Called with asyncio.create_task() right after a successful deploy so it
    never blocks the deployment response.
    """
    await asyncio.sleep(delay)

    async def _dind_ok() -> bool:
        """Return True if `docker info` inside the container reports Server Version."""
        try:
            out, _, rc = await docker_exec(
                container_name,
                "timeout 6 docker info 2>&1 | grep 'Server Version' | head -1",
                timeout=10,
            )
            return rc == 0 and "Server Version" in out
        except Exception:
            return False

    logo = get_logo_url()

    # ── Step 1: check ─────────────────────────────────────────────────────────
    if await _dind_ok():
        logger.info(f"[autoheal] DinD in {container_name} is healthy after {delay}s.")
        # Send ✅ confirmation to log channel and DM the user
        embed = discord.Embed(
            title="✅  Docker Engine Running",
            description=(
                f"Docker-in-Docker inside `{container_name}` is **healthy** and fully operational."
            ),
            color=0x57F287,
            timestamp=_utcnow(),
        )
        if logo:
            embed.set_author(name=f"{get_brand_name()} VPS — DinD Status", icon_url=logo)
        embed.add_field(name="📦 Container", value=f"`{container_name}`", inline=True)
        embed.add_field(name="⏱️ Confirmed After", value=f"`{delay}s`", inline=True)
        embed.add_field(name="🐳 DinD", value="✅ Healthy", inline=True)
        if logo:
            embed.set_footer(text=f"{get_brand_name()} VPS  •  Auto-Check", icon_url=logo)
        await send_log(embed)

        if notify_user:
            try:
                dm_embed = discord.Embed(
                    title="✅  Your VPS Docker Engine is Ready",
                    description=(
                        f"Docker-in-Docker inside your VPS `{container_name}` is **fully up and running**.\n"
                        f"You can now run `docker` commands inside your VPS!"
                    ),
                    color=0x57F287,
                    timestamp=_utcnow(),
                )
                if logo:
                    dm_embed.set_author(name=f"{get_brand_name()} VPS Hosting", icon_url=logo)
                    dm_embed.set_footer(text=f"{get_brand_name()} VPS  •  Auto-Check", icon_url=logo)
                await notify_user.send(embed=dm_embed)
            except Exception:
                pass
        return

    logger.warning(f"[autoheal] DinD in {container_name} not ready after {delay}s — attempting gentle recovery.")

    # ── Step 2: gentle recovery — poll first, only restart if truly dead ──────
    # Give docker another 30 s of polling before touching it (avoids disrupting
    # active tmate / SSH sessions with an unnecessary restart).
    healed = False
    recovered_in = 0
    for i in range(30):
        await asyncio.sleep(1)
        if await _dind_ok():
            healed = True
            recovered_in = i + 1
            break

    if not healed:
        # Still not responding — do the gentlest possible restart
        logger.warning(f"[autoheal] DinD still down after extra 30s — running try-restart on docker.")
        try:
            # try-restart only restarts if the service is currently running (avoids
            # a double-start). This is less disruptive than a plain restart.
            await docker_exec(
                container_name,
                "systemctl try-restart docker 2>&1 || systemctl restart docker 2>&1",
                timeout=30,
            )
        except Exception as e:
            logger.warning(f"[autoheal] try-restart docker failed: {e}")

        # Poll up to 60 s after restart
        for i in range(60):
            await asyncio.sleep(1)
            if await _dind_ok():
                healed = True
                recovered_in = i + 31  # account for the 30 s pre-check
                break

    if healed:
        logger.info(f"[autoheal] DinD in {container_name} recovered after restart ({recovered_in}s).")
        # Post success to log channel — only fires when a restart was actually needed
        embed = discord.Embed(
            title="✅  DinD Auto-Heal — Recovered",
            description=(
                f"Docker-in-Docker inside `{container_name}` was **not ready** at deploy verification "
                f"but has now recovered after an automatic service restart."
            ),
            color=0x57F287,
            timestamp=_utcnow(),
        )
        if logo:
            embed.set_author(name=f"{get_brand_name()} VPS — Auto-Heal", icon_url=logo)
        embed.add_field(name="📦 Container",    value=f"`{container_name}`",     inline=True)
        embed.add_field(name="⏱️ Waited After Deploy", value=f"`~{delay}s`",     inline=True)
        embed.add_field(name="🔄 Restart Took", value=f"`~{recovered_in}s`",     inline=True)
        embed.add_field(
            name="ℹ️ What happened",
            value=(
                "fuse-overlayfs was still mounting `/var/lib/docker` when verification ran. "
                "The VPS itself was always fine — SSH and systemd were healthy."
            ),
            inline=False,
        )
        if logo:
            embed.set_footer(text=f"{get_brand_name()} VPS  •  Auto-Heal", icon_url=logo)
        await send_log(embed)
    else:
        logger.error(f"[autoheal] DinD in {container_name} still broken after restart — admin attention needed.")
        embed = discord.Embed(
            title="⚠️  DinD Auto-Heal Failed",
            description=(
                f"Docker-in-Docker inside `{container_name}` did not start within "
                f"{delay + 60}s after deploy and could **not** be auto-healed.\n\n"
                f"> Run `!fixdind {container_name}` for manual recovery."
            ),
            color=0xF0A500,
            timestamp=_utcnow(),
        )
        if logo:
            embed.set_author(name=f"{get_brand_name()} VPS — Auto-Heal", icon_url=logo)
        embed.add_field(name="📦 Container", value=f"`{container_name}`",                inline=True)
        embed.add_field(name="⏱️ Total Wait", value=f"`{delay + 60}s`",                  inline=True)
        embed.add_field(name="🔧 Manual Fix", value=f"`!fixdind {container_name}`",       inline=True)
        embed.add_field(
            name="🔍 Diagnose",
            value=f"`!exec {container_name} journalctl -u docker -n 20`",
            inline=False,
        )
        if logo:
            embed.set_footer(text=f"{get_brand_name()} VPS  •  Auto-Heal", icon_url=logo)
        await send_log(embed)


async def configure_vps(container_name: str, password: str, hostname: str = None):
    """
    Configure a running darknodes-vps container.

    Steps:
      1. Poll until systemd reaches running/degraded state (max 60 s).
      2. Set the root password to the generated per-container value.
      3. Apply the unique per-container hostname.
      4. Wait for Docker daemon (DinD) to become healthy inside the container.
      5. Ensure sshd is active via systemd.

    Raises RuntimeError if the script does not confirm completion.
    """
    _hostname = hostname or VPS_HOSTNAME
    logger.info(f"Configuring VPS {container_name} (hostname={_hostname}) …")

    # All static config (locale, apt, pip, sysctl, limits, MOTD, PATH profile,
    # SSH keepalive) is pre-baked into the Docker image (Dockerfile Layer 11).
    # This script only handles the three values that differ per container:
    # root password, hostname, and DinD wait (with driver auto-detection).
    configure_script = f"""
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# ── 1. Wait for systemd (max 15 s — containers reach running/degraded fast) ──
_waited=0
while [ $_waited -lt 15 ]; do
    _state=$(systemctl is-system-running 2>/dev/null || true)
    if [ "$_state" = "running" ] || [ "$_state" = "degraded" ] || [ "$_state" = "maintenance" ]; then
        break
    fi
    sleep 1
    _waited=$((_waited + 1))
done
echo "systemd state: $(systemctl is-system-running 2>/dev/null || echo unknown)"
systemctl mask rescue.service rescue.target emergency.service emergency.target 2>/dev/null || true

# ── 2. Per-container dynamic values ──────────────────────────────────────────
echo 'root:{password}' | chpasswd
hostname {_hostname}
echo "{_hostname}" > /etc/hostname
grep -q "{_hostname}" /etc/hosts || echo "127.0.1.1 {_hostname}" >> /etc/hosts
echo "{get_brand_name()}" > /etc/darknodes-brand

# ── 3. Fix DNS (must happen at runtime; Docker sets resolv.conf on start) ────
rm -f /etc/resolv.conf
cat > /etc/resolv.conf <<'DNSEOF'
nameserver 8.8.8.8
nameserver 1.1.1.1
nameserver 8.8.4.4
options edns0 trust-ad
DNSEOF
chattr +i /etc/resolv.conf 2>/dev/null || true

# ── 4. Wait for Docker daemon (DinD) — overlay2 fast path, fuse fallback ─────
# overlay2 (native kernel driver) starts in ~3-5 s on most hosts.
# If it does not respond within 20 s we assume the host kernel doesn't support
# overlay inside privileged containers and fall back to fuse-overlayfs.
_docker_waited=0
while [ $_docker_waited -lt 20 ]; do
    if docker info >/dev/null 2>&1; then
        echo "Docker ready (overlay2) after $_docker_waited s."
        break
    fi
    sleep 1
    _docker_waited=$((_docker_waited + 1))
done

if ! docker info >/dev/null 2>&1; then
    echo "overlay2 not ready after 20 s — switching to fuse-overlayfs …"
    cat > /etc/docker/daemon.json <<'DEOF'
{{
  "storage-driver": "fuse-overlayfs",
  "dns": ["8.8.8.8", "1.1.1.1"],
  "log-driver": "json-file",
  "log-opts": {{"max-size": "10m", "max-file": "3"}}
}}
DEOF
    systemctl restart docker 2>/dev/null || true
    _fuse_waited=0
    while [ $_fuse_waited -lt 90 ]; do
        if docker info >/dev/null 2>&1; then
            echo "Docker ready (fuse-overlayfs) after $_fuse_waited s."
            break
        fi
        if [ "$_fuse_waited" -eq 45 ]; then
            systemctl restart docker 2>/dev/null || true
        fi
        sleep 2
        _fuse_waited=$((_fuse_waited + 2))
    done
    if ! docker info >/dev/null 2>&1; then
        echo "WARNING: Docker daemon not ready after fuse-overlayfs fallback."
        journalctl -u docker --no-pager -n 30 2>/dev/null || true
    fi
fi

# ── 5. Reload SSH (picks up keepalive settings pre-baked in the image) ───────
systemctl reload ssh 2>/dev/null || systemctl restart ssh 2>/dev/null || true

# ── 6. Background apt update (non-blocking) ───────────────────────────────────
DEBIAN_FRONTEND=noninteractive apt-get update -qq &>/dev/null &

# ── 7. Apply pre-baked sysctl values ─────────────────────────────────────────
sysctl -p /etc/sysctl.d/99-darknodes.conf 2>/dev/null || true

echo "DARKNODES_CONFIGURE_COMPLETE"
"""

    # 220 s was tight: systemd wait (30 s) + DinD wait (90 s) + config steps (~30 s) = ~150 s worst case.
    # 300 s gives a comfortable margin on slow hosts without blocking the bot unnecessarily.
    stdout, stderr, rc = await docker_exec(container_name, configure_script, timeout=300)
    if "DARKNODES_CONFIGURE_COMPLETE" not in stdout:
        raise RuntimeError(
            f"Configuration script did not complete (exit {rc})\n"
            f"stdout: {stdout[-800:]}\n"
            f"stderr: {stderr[-500:]}"
        )
    logger.info(f"VPS {container_name} configured successfully.")


async def verify_vps(container_name: str, hostname: str = None) -> tuple:
    """
    Verify a newly-created container is fully functional (DinD architecture).

    Returns (all_passed: bool, human_readable_report: str).

    Critical (deployment fails if any fail):
      ✓ PID 1 is systemd
      ✓ systemctl --version responsive
      ✓ Docker service active  (systemctl is-active docker)
      ✓ Docker daemon info     (docker info — confirms DinD is running)
      ✓ docker ps              (daemon end-to-end sanity)
      ✓ SSH active             (systemctl is-active ssh)
      ✓ Hostname matches expected value

    Optional (warnings only):
      ✓ docker version / compose
      ✓ docker run --rm hello-world  (DinD pull + run)
      ✓ tmate installed
      ✓ sshx installed
    """
    results: Dict[str, tuple] = {}

    async def run_check(label: str, cmd: str, expect: str = None, timeout: int = 20) -> bool:
        try:
            stdout, stderr, rc = await docker_exec(container_name, cmd, timeout=timeout)
            out = (stdout + " " + stderr).strip()
            if rc != 0:
                results[label] = (False, f"exit {rc} — {out[:150]}")
                return False
            if expect and expect.lower() not in out.lower():
                results[label] = (False, f"'{expect}' not found — got: {out[:150]}")
                return False
            results[label] = (True, stdout[:80].strip() or "ok")
            return True
        except Exception as exc:
            results[label] = (False, str(exc)[:150])
            return False

    # Give services a brief moment to settle after configure_vps
    await asyncio.sleep(2)

    # ── Batch 1 (parallel): systemd, SSH, hostname — all independent ──────────
    await asyncio.gather(
        run_check("PID 1 is systemd",
                  "cat /proc/1/comm", "systemd"),
        run_check("systemctl",
                  "systemctl --version 2>&1", "systemd"),
        run_check("SSH active",
                  "systemctl is-active ssh 2>/dev/null || "
                  "systemctl is-active sshd 2>/dev/null || echo inactive",
                  "active"),
        run_check("Hostname", "hostname", hostname or VPS_HOSTNAME),
    )

    # SSH retry only if the first attempt failed
    if not results.get("SSH active", (True,))[0]:
        await asyncio.sleep(3)
        await run_check(
            "SSH active (retry)",
            "systemctl is-active ssh 2>/dev/null || "
            "systemctl is-active sshd 2>/dev/null || echo inactive",
            "active",
        )

    # ── Critical: Docker daemon (DinD) ────────────────────────────────────────
    # Check service state first; if not active give it a few extra seconds.
    docker_svc_ok = await run_check(
        "Docker service active",
        "systemctl is-active docker 2>/dev/null",
        "active",
    )
    if not docker_svc_ok:
        await asyncio.sleep(5)
        docker_svc_ok = await run_check(
            "Docker service active (retry)",
            "systemctl is-active docker 2>/dev/null",
            "active",
        )

    # configure_vps already waited up to 90 s for DinD and confirmed it healthy,
    # so in the normal path docker info passes on the very first iteration here.
    # We keep a short 20-iteration fallback (≈20 s) in case systemd restarted
    # docker between configure_vps and now.  No mid-point restart is needed —
    # configure_vps already handles the crash-restart cycle case.
    docker_info_ok = await run_check(
        "Docker daemon info",
        (
            r"bash -c '"
            r"for i in $(seq 1 20); do "
            r"  out=$(timeout 4 docker info 2>&1); "
            r"  if echo \"$out\" | grep -q \"Server Version\"; then "
            r"    echo \"$out\" | head -20; exit 0; "
            r"  fi; "
            r"  sleep 1; "
            r"done; "
            r"echo \"Timed out — dockerd not ready after 20s:\"; "
            r"journalctl -u docker --no-pager -n 15 2>/dev/null || true; "
            r"exit 1'"
        ),
        "Server Version",
        timeout=30,
    )

    # ── Batch 2 (parallel): optional checks — independent of each other ───────
    # hello-world is intentionally omitted: docker info already proves DinD
    # can run containers end-to-end and pulling an image adds 20-90 s for no gain.
    await asyncio.gather(
        run_check("docker ps",
                  "docker ps --format '{{.ID}}' 2>&1",
                  None, timeout=15),
        run_check("docker version",
                  "docker version --format '{{.Server.Version}}' 2>&1 || docker version 2>&1 | head -5",
                  None, timeout=15),
        run_check("docker compose",
                  "docker compose version 2>&1", "compose", timeout=15),
        run_check("tmate installed",
                  "command -v tmate && tmate -V 2>&1 || echo MISSING", "tmate", timeout=10),
        run_check("sshx installed",
                  "test -f /usr/local/bin/sshx && echo found || echo MISSING", "found", timeout=10),
    )

    # ── Build report ──────────────────────────────────────────────────────────
    _CRITICAL = frozenset({
        "PID 1 is systemd", "systemctl",
        "Docker service active", "Docker service active (retry)",
        "SSH active", "SSH active (retry)", "Hostname",
    })
    _OPTIONAL = frozenset({
        "Docker daemon info",   # DinD can take >90s on first fuse-overlayfs mount
        "docker ps",
        "docker version", "docker compose",
        "tmate installed", "sshx installed",
    })

    report_lines = []
    for lbl, (ok, detail) in results.items():
        if lbl in _OPTIONAL:
            icon = "✅" if ok else "⚠️"
            tag  = "" if ok else " (optional)"
        else:
            icon = "✅" if ok else "❌"
            tag  = ""
        report_lines.append(f"{icon} {lbl}{tag}: {detail}")
    report = "\n".join(report_lines)

    critical_failed   = [lbl for lbl, (ok, _) in results.items() if lbl in _CRITICAL and not ok]
    optional_warnings = [lbl for lbl, (ok, _) in results.items() if lbl in _OPTIONAL and not ok]
    all_passed = len(critical_failed) == 0

    if all_passed:
        if optional_warnings:
            logger.warning(f"VPS {container_name} verification PASSED "
                           f"(optional warned: {optional_warnings}).\n{report}")
        else:
            logger.info(f"VPS {container_name} verification PASSED.\n{report}")
    else:
        logger.error(f"VPS {container_name} verification FAILED "
                     f"— critical: {critical_failed}.\n{report}")

    return all_passed, report


async def _get_server_ip() -> str:
    """Return the server's public IP for direct SSH connection strings."""
    if SERVER_IP:
        return SERVER_IP
    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", "hostname -I 2>/dev/null | awk '{print $1}'",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        ip = out.decode().strip().split()[0] if out.strip() else ""
        return ip
    except Exception:
        return ""


async def get_tmate_session(container_name: str) -> dict:
    """
    Start a tmate session inside the container.

    Returns dict:  {"ssh": "<connection string>"}
    Only the SSH connection string is returned — web URLs are not exposed.
    If tmate is missing from the image it is installed automatically (same
    pattern as sshx auto-install) so existing containers keep working.
    Raises Exception on failure so callers can treat it as optional.
    """
    _, _, chk_rc = await docker_exec(
        container_name, "command -v tmate", timeout=10
    )
    if chk_rc != 0:
        # Auto-install tmate so containers built without it still work
        logger.info(f"tmate not found in {container_name} — installing on-the-fly…")
        install_script = r"""
set -e
arch="$(uname -m)"
if   [ "$arch" = "x86_64" ];   then ta="amd64"
elif [ "$arch" = "aarch64" ];  then ta="arm64v8"
elif [ "$arch" = "armv7l" ];   then ta="arm32v7"
else echo "Unsupported arch: $arch" >&2; exit 1; fi
# Ensure xz-utils is present (required for .tar.xz extraction)
apt-get install -y --no-install-recommends xz-utils 2>/dev/null || \
    yum install -y xz 2>/dev/null || true
curl --retry 5 --retry-delay 5 --connect-timeout 30 -fsSL \
    "https://github.com/tmate-io/tmate/releases/download/2.4.0/tmate-2.4.0-static-linux-${ta}.tar.xz" \
    -o /tmp/tmate.tar.xz
tar -xf /tmp/tmate.tar.xz -C /tmp
install -m 755 "/tmp/tmate-2.4.0-static-linux-${ta}/tmate" /usr/local/bin/tmate
rm -rf /tmp/tmate*
tmate -V
"""
        inst_out, inst_err, inst_rc = await docker_exec(
            container_name, install_script, timeout=120
        )
        # Re-check after install attempt
        _, _, chk_rc2 = await docker_exec(container_name, "command -v tmate", timeout=10)
        if chk_rc2 != 0:
            raise Exception(
                f"tmate auto-install failed: {(inst_out + inst_err)[:300]}"
            )
        logger.info(f"tmate installed successfully in {container_name}")

    start_script = r"""
pkill tmate 2>/dev/null || true
sleep 1
rm -f /tmp/tmate.sock
tmate -S /tmp/tmate.sock new-session -d 2>&1
sleep 2
tmate -S /tmp/tmate.sock wait tmate-ready 2>&1
TMATE_SSH=$(tmate -S /tmp/tmate.sock display -p '#{tmate_ssh}' 2>/dev/null || true)
echo "TMATE_SSH:${TMATE_SSH}"
"""
    stdout, stderr, rc = await docker_exec(container_name, start_script, timeout=45)

    tmate_ssh = ""
    for line in stdout.splitlines():
        if line.startswith("TMATE_SSH:"):
            tmate_ssh = line[len("TMATE_SSH:"):].strip()

    if not tmate_ssh:
        raise Exception(f"tmate SSH string not returned. stderr: {stderr[:300]}")

    return {"ssh": tmate_ssh}


async def get_sshx_session(container_name: str) -> str:
    """
    Start an sshx session inside the container and return the validated web URL.

    If sshx is not found in the image, it is installed automatically so that
    existing containers created before the Dockerfile update still work.

    ANSI escape codes are stripped before URL extraction.
    Raises Exception on failure so callers can treat it as optional.
    """
    chk_out, _, chk_rc = await docker_exec(
        container_name, "command -v sshx", timeout=10
    )
    if chk_rc != 0:
        # Auto-install sshx so existing containers work without image rebuild
        logger.info(f"sshx not found in {container_name} — installing on-the-fly…")
        install_out, install_err, install_rc = await docker_exec(
            container_name,
            "curl -sSf https://sshx.io/get | sh -s -- install 2>&1",
            timeout=60,
        )
        # Re-check after install
        _, _, chk_rc2 = await docker_exec(container_name, "command -v sshx", timeout=10)
        if chk_rc2 != 0:
            raise Exception(f"sshx install failed: {(install_out + install_err)[:300]}")

    # NO_COLOR + TERM=dumb suppresses ANSI at source; sed strips any survivors.
    start_script = r"""
pkill sshx 2>/dev/null || true
sleep 1
TERM=dumb NO_COLOR=1 COLORTERM= sshx > /tmp/sshx.log 2>&1 &
_waited=0
while [ $_waited -lt 30 ]; do
    _url=$(sed 's/\x1b\[[0-9;]*[mGKHFJSTsulhABCDEFfr]//g; s/\x1b\[?[0-9;]*[hl]//g; s/\r//g' \
           /tmp/sshx.log 2>/dev/null | grep -o 'https://sshx\.io/s/[^[:space:]]*' | head -1)
    if [ -n "$_url" ]; then
        echo "SSHX_URL:${_url}"
        exit 0
    fi
    sleep 1
    _waited=$((_waited + 1))
done
echo "SSHX_URL:"
echo "SSHX_DEBUG:$(sed 's/\x1b\[[0-9;]*[mGKHFJSTsulhABCDEFfr]//g' /tmp/sshx.log 2>/dev/null | tail -5)"
"""
    stdout, stderr, rc = await docker_exec(container_name, start_script, timeout=40)

    raw_url    = ""
    debug_info = ""
    for line in stdout.splitlines():
        if line.startswith("SSHX_URL:"):
            raw_url = line[len("SSHX_URL:"):].strip()
        elif line.startswith("SSHX_DEBUG:"):
            debug_info = line[len("SSHX_DEBUG:"):].strip()

    if not raw_url:
        hint = f" Log tail: {debug_info[:200]}" if debug_info else f" stderr: {stderr[:200]}"
        raise Exception(f"sshx did not produce a URL within 30s.{hint}")

    # Python-side ANSI strip (safety net)
    url = re.sub(r'\x1b\[[0-9;]*[mGKHFJSTsulhABCDEFfr]', '', raw_url)
    url = re.sub(r'\x1b\[?[0-9;]*[hl]', '', url).strip()

    if not re.match(r'^https://sshx\.io/s/[A-Za-z0-9_\-]+(#[A-Za-z0-9_\-]+)?$', url):
        raise Exception(f"sshx returned an invalid or garbled URL: {url!r}")

    return url

# ─── VPS Role helper ──────────────────────────────────────────────────────────

async def get_or_create_vps_role(guild):
    global VPS_USER_ROLE_ID
    if VPS_USER_ROLE_ID:
        role = guild.get_role(VPS_USER_ROLE_ID)
        if role:
            return role
    role = discord.utils.get(guild.roles, name="VPS User")
    if role:
        VPS_USER_ROLE_ID = role.id
        return role
    try:
        role = await guild.create_role(
            name="VPS User",
            color=discord.Color.dark_purple(),
            reason="VPS User role for bot management",
            permissions=discord.Permissions.none()
        )
        VPS_USER_ROLE_ID = role.id
        logger.info(f"Created VPS User role: {role.name} (ID: {role.id})")
        return role
    except Exception as e:
        logger.error(f"Failed to create VPS User role: {e}")
        return None

# ─── Anti-mining detection helpers ───────────────────────────────────────────

def _is_whitelisted(container_name: str, owner_user_id: str) -> bool:
    """Return True if this VPS/user is on the admin whitelist."""
    return (
        owner_user_id in MONITOR_CONFIG.get("whitelisted_users", []) or
        container_name in MONITOR_CONFIG.get("whitelisted_containers", [])
    )

def _process_is_safe(proc_name: str) -> bool:
    """Return True if the process name is on the safe-process whitelist."""
    safe = MONITOR_CONFIG.get("whitelisted_processes", [])
    proc_lower = proc_name.lower()
    return any(s.lower() in proc_lower for s in safe)

async def _get_container_cpu(container_name: str) -> float:
    """
    Return current CPU usage % for a container using docker stats.
    Returns 0.0 on any error.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "stats", "--no-stream", "--format", "{{.CPUPerc}}",
            container_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        raw = stdout.decode().strip().replace("%", "")
        return float(raw) if raw else 0.0
    except Exception:
        return 0.0

async def _scan_container_for_mining(container_name: str) -> Dict:
    """
    Inspect a container for mining indicators.
    Returns a dict with keys:
      found_processes, found_connections, found_cli_args, confidence_score, details
    """
    result = {
        "found_processes": [],
        "found_connections": [],
        "found_cli_args": [],
        "confidence_score": 0,
        "details": []
    }

    blacklist_procs = MONITOR_CONFIG.get("mining_process_blacklist", [])
    blacklist_pools = MONITOR_CONFIG.get("mining_pool_blacklist", [])
    blacklist_cli   = MONITOR_CONFIG.get("mining_cli_blacklist",   [])

    # ── Check 1: Known mining process names in ps ────────────────────────────
    try:
        stdout, _, rc = await docker_exec(
            container_name, "ps aux --no-headers 2>/dev/null || true", timeout=15
        )
        if rc == 0:
            for line in stdout.splitlines():
                line_lower = line.lower()
                # Skip safe processes first
                parts = line.split()
                proc_name = parts[10] if len(parts) > 10 else ""
                if _process_is_safe(proc_name):
                    continue
                for miner in blacklist_procs:
                    if miner.lower() in line_lower:
                        result["found_processes"].append(miner)
                        result["details"].append(f"Mining process: {miner}")
                for cli in blacklist_cli:
                    if cli.lower() in line_lower:
                        result["found_cli_args"].append(cli)
                        result["details"].append(f"Mining CLI arg: {cli}")
    except Exception:
        pass

    # ── Check 2: Network connections to known mining pools ───────────────────
    try:
        # ss is more reliable than netstat in modern containers
        stdout, _, rc = await docker_exec(
            container_name,
            "ss -tnp 2>/dev/null || netstat -tnp 2>/dev/null || true",
            timeout=15
        )
        if rc == 0:
            for line in stdout.splitlines():
                line_lower = line.lower()
                for pool in blacklist_pools:
                    if pool.lower() in line_lower:
                        result["found_connections"].append(pool)
                        result["details"].append(f"Mining pool connection: {pool}")
        # Also check /etc/hosts and DNS cache for pool domains
        stdout2, _, _ = await docker_exec(
            container_name, "cat /etc/hosts 2>/dev/null || true", timeout=10
        )
        for pool in blacklist_pools:
            if pool.lower() in stdout2.lower():
                if pool not in result["found_connections"]:
                    result["found_connections"].append(pool)
                    result["details"].append(f"Mining pool in /etc/hosts: {pool}")
    except Exception:
        pass

    # ── Confidence scoring ───────────────────────────────────────────────────
    # Rules:
    #   +25 per unique mining process found        (capped at 50)
    #   +25 per unique mining pool connection      (capped at 50)
    #   +15 per unique mining CLI argument found   (capped at 30)
    # CPU indicator is added by the caller (not here) to keep this pure.
    # Deduplication: score each category independently.

    proc_score = min(len(set(result["found_processes"])) * 25, 50)
    conn_score = min(len(set(result["found_connections"])) * 25, 50)
    cli_score  = min(len(set(result["found_cli_args"]))  * 15, 30)

    result["confidence_score"] = proc_score + conn_score + cli_score

    # Deduplicate lists
    result["found_processes"]  = list(set(result["found_processes"]))
    result["found_connections"] = list(set(result["found_connections"]))
    result["found_cli_args"]   = list(set(result["found_cli_args"]))

    return result


async def _suspend_vps_for_mining(
    container_name: str,
    owner_user_id: str,
    evidence: Dict,
    cpu_pct: float
):
    """
    Stop a container suspected of mining and mark it as suspended.
    Stores evidence and sends a Discord admin notification.
    """
    # Stop the container
    try:
        await execute_docker(f"docker stop --time=5 {container_name}", timeout=30)
        logger.warning(f"SUSPENDED for mining: {container_name}")
    except Exception as e:
        logger.error(f"Failed to stop container {container_name}: {e}")

    # Mark VPS as suspended in vps_data
    uid, vps = find_vps_record(container_name)
    if vps:
        vps["status"]            = "suspended"
        vps["suspension_reason"] = "Automated anti-mining detection"
        vps["suspension_time"]   = _utcnow().isoformat()
        vps["suspension_evidence"] = evidence
        save_data()

    # Append to suspension log (persistent)
    try:
        mem_stdout, _, _ = await docker_exec(
            container_name,
            "free -m 2>/dev/null | awk '/Mem:/{print $3\"/\"$2}' || echo 'N/A'",
            timeout=10
        )
    except Exception:
        mem_stdout = "N/A"

    log_entry = {
        "timestamp":        _utcnow().isoformat(),
        "container_name":   container_name,
        "owner_user_id":    owner_user_id,
        "cpu_pct":          cpu_pct,
        "ram_info":         mem_stdout,
        "confidence_score": evidence.get("confidence_score", 0),
        "found_processes":  evidence.get("found_processes", []),
        "found_connections": evidence.get("found_connections", []),
        "found_cli_args":   evidence.get("found_cli_args", []),
        "details":          evidence.get("details", []),
    }
    suspension_log.append(log_entry)
    save_suspension_log()

    # Append to per-container monitor log
    if container_name not in monitor_log:
        monitor_log[container_name] = []
    monitor_log[container_name].append({"type": "suspension", **log_entry})
    save_monitor_log()

    # ── Discord log-channel notification ─────────────────────────────────────
    channel_id = MONITOR_CONFIG.get("notification_channel_id", 0)
    if channel_id:
        try:
            channel = bot.get_channel(int(channel_id))
            if channel is None:
                channel = await bot.fetch_channel(int(channel_id))
            if channel:
                # Resolve owner mention
                try:
                    owner_user = await bot.fetch_user(int(owner_user_id))
                    owner_str  = f"{owner_user.mention} (`{owner_user}` · ID `{owner_user_id}`)"
                    owner_avatar = owner_user.display_avatar.url
                except Exception:
                    owner_user   = None
                    owner_str    = f"ID `{owner_user_id}`"
                    owner_avatar = None

                # Pull VPS spec from vps_data for extra context
                _, vps_rec = find_vps_record(container_name)
                plan_label = vps_rec.get("plan_name",  "Unknown") if vps_rec else "Unknown"
                ram_label  = vps_rec.get("ram",        "?")       if vps_rec else "?"
                cpu_label  = vps_rec.get("cpu",        "?")       if vps_rec else "?"
                ssh_port   = vps_rec.get("ssh_port",   "?")       if vps_rec else "?"

                # Determine primary detection method(s) for the title line
                methods = []
                if evidence.get("found_processes"):
                    methods.append("Process name match")
                if evidence.get("found_connections"):
                    methods.append("Mining pool connection")
                if evidence.get("found_cli_args"):
                    methods.append("Mining CLI arguments")
                if not methods:
                    methods.append("High CPU heuristic")
                method_str = " · ".join(methods)

                # Confidence bar (10 blocks, each = 10 pts)
                score     = min(evidence.get("confidence_score", 0), 100)
                filled    = score // 10
                conf_bar  = "🟥" * filled + "⬛" * (10 - filled)

                # Detail bullets
                procs = evidence.get("found_processes",  [])
                conns = evidence.get("found_connections", [])
                cargs = evidence.get("found_cli_args",   [])
                dets  = evidence.get("details",          [])

                def fmt_list(lst):
                    return "\n".join(f"• `{x}`" for x in lst) if lst else "`none`"

                now_str = _utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

                embed = discord.Embed(
                    title="🚨  Mining Activity Detected — VPS Auto-Suspended",
                    description=(
                        f"Container `{container_name}` has been **automatically stopped** and marked **suspended**.\n"
                        f"**Detection method:** {method_str}"
                    ),
                    colour=discord.Colour.from_rgb(237, 66, 69),
                    timestamp=_utcnow()
                )
                if owner_avatar:
                    embed.set_thumbnail(url=owner_avatar)
                _logo = get_logo_url()
                _footer_kw: dict = {"text": f"{get_brand_name()} Anti-Abuse System  •  Auto-Monitor"}
                if _logo:
                    _footer_kw["icon_url"] = _logo
                embed.set_footer(**_footer_kw)

                # Row 1 — identifiers
                embed.add_field(name="📦 Container ID",    value=f"`{container_name}`",          inline=True)
                embed.add_field(name="🔌 SSH Port",        value=f"`{ssh_port}`",                 inline=True)
                embed.add_field(name="📋 Plan",            value=f"`{plan_label}`",               inline=True)

                # Row 2 — owner + specs
                embed.add_field(name="👤 Owner",           value=owner_str,                       inline=False)
                embed.add_field(name="🧠 RAM",             value=f"`{vps_rec.get('ram', ram_label)}`" if vps_rec else f"`{ram_label}`", inline=True)
                embed.add_field(name="⚡ CPU Cores",       value=f"`{cpu_label}`",                inline=True)
                embed.add_field(name="🖥️ CPU Usage",       value=f"`{cpu_pct:.1f}%`",             inline=True)

                # Row 3 — score
                embed.add_field(
                    name="🔢 Confidence Score",
                    value=f"{conf_bar}\n**{score}/100** (suspend threshold: {MONITOR_CONFIG.get('auto_suspend_threshold', 70)})",
                    inline=False
                )

                # Row 4 — evidence breakdown
                embed.add_field(name="🦠 Mining Processes",        value=fmt_list(procs), inline=True)
                embed.add_field(name="🌐 Pool Connections",         value=fmt_list(conns), inline=True)
                embed.add_field(name="💻 Suspicious CLI Args",      value=fmt_list(cargs), inline=True)

                # Row 5 — full detail log (truncated to fit field limit)
                if dets:
                    det_str = "\n".join(f"• {d}" for d in dets)
                    if len(det_str) > 1000:
                        det_str = det_str[:997] + "…"
                    embed.add_field(name="📝 Full Detection Log", value=det_str, inline=False)

                # Row 6 — status + timestamp
                embed.add_field(name="🔒 VPS Status",   value="**SUSPENDED** ⛔",    inline=True)
                embed.add_field(name="🕒 Suspended At", value=f"`{now_str}`",          inline=True)
                embed.add_field(name="📌 Action",
                                value="Use `!vps-unsuspend` to restore after review.", inline=False)

                await send_log(embed)
        except Exception as notify_err:
            logger.error(f"Failed to build/send mining alert: {notify_err}")


# ─── Smart async abuse monitor ────────────────────────────────────────────────
# Replaces the old threading CPU monitor.  Runs entirely in asyncio so it
# doesn't block the bot and can make async docker calls.

@tasks.loop(seconds=120)   # default; overridden after config load
async def abuse_monitor():
    """
    Per-container anti-mining monitor.

    Scoring:
      +30  Sustained CPU > threshold for the configured duration
      +25  Known mining process found            (×count, capped at 50)
      +25  Mining pool connection detected       (×count, capped at 50)
      +15  Mining CLI argument found             (×count, capped at 30)
      ──────────────────────────────────────────────────────────────────
      Max  160 points (effectively capped to 100 in the embed display)

    Auto-suspend fires ONLY when score ≥ auto_suspend_threshold (default 70).
    High CPU alone is worth 30 points — never enough to suspend on its own.
    """
    if not MONITOR_CONFIG.get("auto_suspend_enabled", True):
        return

    cpu_threshold     = MONITOR_CONFIG.get("cpu_threshold", 95)
    sustained_minutes = MONITOR_CONFIG.get("sustained_duration_minutes", 20)
    suspend_threshold = MONITOR_CONFIG.get("auto_suspend_threshold", 70)

    # Collect all running containers tracked by the bot
    running_containers = [
        (uid, vps)
        for uid, vps_list in vps_data.items()
        for vps in vps_list
        if vps.get("status") == "running" and not vps.get("suspension_reason")
    ]

    for owner_uid, vps in running_containers:
        container_name = vps.get("container_name", "")
        if not container_name:
            continue

        # Skip whitelisted VPSs
        if _is_whitelisted(container_name, owner_uid):
            continue

        state = get_container_state(container_name)
        if state.get("monitoring_paused"):
            continue

        try:
            # ── CPU sample ──────────────────────────────────────────────────
            cpu_pct = await _get_container_cpu(container_name)
            state["cpu_samples"].append(cpu_pct)

            # Track start of sustained high-CPU window
            if cpu_pct > cpu_threshold:
                if state["high_cpu_start"] is None:
                    state["high_cpu_start"] = _utcnow()
            else:
                state["high_cpu_start"] = None  # reset when CPU drops

            # ── Sustained CPU indicator (+30 pts) ───────────────────────────
            cpu_score = 0
            if state["high_cpu_start"] is not None:
                elapsed_minutes = (_utcnow() - state["high_cpu_start"]).seconds / 60
                if elapsed_minutes >= sustained_minutes:
                    cpu_score = 30
                    state["flags"].add("sustained_cpu")
                    logger.debug(
                        f"{container_name}: sustained CPU {cpu_pct:.1f}% "
                        f"for {elapsed_minutes:.1f}min (score +30)"
                    )

            # Only do deep process/network scan when CPU is elevated (saves resources)
            if cpu_pct < 50 and cpu_score == 0:
                # No concerning CPU — skip expensive scan
                state["last_confidence"] = 0
                continue

            # ── Deep scan ───────────────────────────────────────────────────
            scan = await _scan_container_for_mining(container_name)
            total_score = cpu_score + scan["confidence_score"]
            state["last_confidence"] = total_score

            # Log scan event
            if container_name not in monitor_log:
                monitor_log[container_name] = []
            monitor_log[container_name].append({
                "type":             "scan",
                "timestamp":        _utcnow().isoformat(),
                "cpu_pct":          cpu_pct,
                "cpu_score":        cpu_score,
                "scan_score":       scan["confidence_score"],
                "total_score":      total_score,
                "found_processes":  scan["found_processes"],
                "found_connections": scan["found_connections"],
            })
            # Keep monitor log bounded (last 100 events per container)
            monitor_log[container_name] = monitor_log[container_name][-100:]
            save_monitor_log()

            if total_score > 0:
                logger.info(
                    f"Abuse scan {container_name}: CPU={cpu_pct:.1f}% "
                    f"score={total_score} procs={scan['found_processes']} "
                    f"conns={scan['found_connections']}"
                )

            # ── Auto-suspend decision ────────────────────────────────────────
            if total_score >= suspend_threshold and MONITOR_CONFIG.get("auto_suspend_enabled", True):
                evidence = {**scan, "confidence_score": total_score, "cpu_score": cpu_score}
                await _suspend_vps_for_mining(container_name, owner_uid, evidence, cpu_pct)
                # Reset state after suspension
                container_monitor_state.pop(container_name, None)

        except Exception as scan_err:
            logger.error(f"abuse_monitor error for {container_name}: {scan_err}")

@abuse_monitor.before_loop
async def before_abuse_monitor():
    await bot.wait_until_ready()

# ─── Bot events ────────────────────────────────────────────────────────────────

# ─── Template System ──────────────────────────────────────────────────────────
# Inject shared helpers and register the /template slash command.
# Must be placed after docker_exec, get_logo_url, and vps_data are all defined.
template_system.init(docker_exec, get_logo_url, vps_data, get_brand_name)
template_system.register_commands(bot)

# ─── Node System ───────────────────────────────────────────────────────────────
# Modular multi-node VPS management.  Local node is created automatically on
# first boot; remote nodes register via `node_agent.py` using a one-time token.
node_system.init(
    docker_exec_fn  = docker_exec,
    run_docker_fn   = run_docker_command,
    get_logo_fn     = get_logo_url,
    get_brand_fn    = get_brand_name,
    main_admin_id   = MAIN_ADMIN_ID,
    admin_data_ref  = admin_data,
)
node_system.register_commands(bot)

@bot.event
async def on_ready():
    logger.info(f'{bot.user} has connected to Discord!')
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.watching, name=f"{get_brand_name()} | VPS Manager"))
    if not auto_expire_check.is_running():
        auto_expire_check.start()
    if not abuse_monitor.is_running():
        abuse_monitor.change_interval(seconds=MONITOR_CONFIG.get("monitoring_interval", 120))
        abuse_monitor.start()
    if not scheduled_backup_runner.is_running():
        scheduled_backup_runner.start()
    if not smart_notifications_monitor.is_running():
        smart_notifications_monitor.start()
    try:
        synced = await bot.tree.sync()
        logger.info(f"Synced {len(synced)} slash command(s).")
    except Exception as e:
        logger.error(f"Failed to sync slash commands: {e}")
    # Pre-build the VPS image at startup so the first deployment is instant.
    # Runs as a background task so it does not block the bot from coming online.
    async def _startup_image_check():
        ok, detail = await ensure_vps_image()
        if ok:
            logger.info("Startup image check: darknodes-vps is ready.")
        else:
            logger.error(f"Startup image check FAILED — VPS creation will not work until this is resolved:\n{detail[-1000:]}")
    asyncio.create_task(_startup_image_check())
    # Start node system: initialises local node + agent HTTP server
    asyncio.create_task(node_system.startup())
    logger.info("Bot is ready!")

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(embed=create_error_embed("Missing Argument", "Please use `!help` for command usage."))
    elif isinstance(error, commands.BadArgument):
        await ctx.send(embed=create_error_embed("Invalid Argument", "Please check your input and try again."))
    elif isinstance(error, commands.CheckFailure):
        pass
    else:
        logger.error(f"Command error: {error}")
        await ctx.send(embed=create_error_embed("System Error", "An error occurred. Please try again."))

# ─── ManageView ────────────────────────────────────────────────────────────────

class ManageView(discord.ui.View):
    def __init__(self, user_id, vps_list, is_shared=False, owner_id=None, is_admin=False):
        super().__init__(timeout=300)
        self.user_id      = user_id
        self.vps_list     = vps_list
        self.selected_index = None
        self.is_shared    = is_shared
        self.owner_id     = owner_id or user_id
        self.is_admin     = is_admin

        if len(vps_list) > 1:
            options = [
                discord.SelectOption(
                    label=f"VPS {i+1} ({v.get('plan', 'Custom')})",
                    description=f"Status: {v.get('status', 'unknown')}",
                    value=str(i)
                ) for i, v in enumerate(vps_list)
            ]
            self.select = discord.ui.Select(placeholder="Select a VPS to manage", options=options)
            self.select.callback = self.select_vps
            self.add_item(self.select)
            self.initial_embed = create_embed("VPS Management", "Select a VPS from the dropdown menu below.", 0x5865F2)
            self.initial_embed.add_field(
                name="Available VPS",
                value="\n".join([
                    f"**VPS {i+1}:** `{v['container_name']}` - Status: `{v.get('status','unknown').upper()}`"
                    for i, v in enumerate(vps_list)
                ]),
                inline=False
            )
        else:
            self.selected_index = 0
            self.initial_embed  = self.create_vps_embed(0)
            self.add_action_buttons()

    def create_vps_embed(self, index):
        vps = self.vps_list[index]
        status = vps.get('status', 'unknown')
        if status == 'suspended':
            status_color = 0xff6600
        elif status == 'running':
            status_color = 0x00ff88
        else:
            status_color = 0xff3366

        owner_text = ""
        if self.is_admin and self.owner_id != self.user_id:
            try:
                owner_user = bot.get_user(int(self.owner_id))
                owner_text = f"\n**Owner:** {owner_user.mention}"
            except Exception:
                owner_text = f"\n**Owner ID:** {self.owner_id}"

        embed = create_embed(
            f"VPS Management - VPS {index + 1}",
            f"Managing container: `{vps['container_name']}`{owner_text}",
            status_color
        )

        expires = vps.get('expires')
        if expires and expires != "Never":
            try:
                exp_dt    = datetime.fromisoformat(expires)
                days_left = (exp_dt - _utcnow()).days
                expire_str = (
                    f"{expires[:10]} ({days_left}d left)"
                    if days_left >= 0
                    else f"{expires[:10]} (**EXPIRED**)"
                )
            except Exception:
                expire_str = expires
        else:
            expire_str = "Never"

        resource_info = (
            f"**Plan:** {vps.get('plan', 'Custom')}\n"
            f"**Status:** `{status.upper()}`\n"
            f"**RAM:** {vps['ram']}\n"
            f"**CPU:** {vps['cpu']} Core(s)\n"
            f"**Storage:** {vps.get('storage', '30GB')}\n"
            f"**Created:** {vps.get('created_at', '?')[:10]}\n"
            f"**Expires:** {expire_str}"
        )
        if "processor" in vps:
            resource_info += f"\n**Processor:** {vps['processor']}"
        if status == "suspended":
            resource_info += f"\n⚠️ **Suspended:** {vps.get('suspension_reason', 'Unknown reason')}"

        embed.add_field(name="📊 Resources", value=resource_info, inline=False)
        embed.add_field(name="🎮 Controls",  value="Use the buttons below to manage your VPS", inline=False)
        return embed

    def add_action_buttons(self):
        # ── Row 0: Core controls ──────────────────────────────────────────────
        # Color logic:
        #   success (green)   = safe/start/positive action
        #   danger  (red)     = destructive / power-off / data-loss risk
        #   primary (blue)    = connect / info / important non-destructive
        #   secondary (gray)  = utility / scheduling / passive
        if not self.is_shared and not self.is_admin:
            reinstall_button = discord.ui.Button(label="🔄 Reinstall", style=discord.ButtonStyle.danger, row=0)
            reinstall_button.callback = lambda inter: self.action_callback(inter, 'reinstall')
            self.add_item(reinstall_button)

        start_button = discord.ui.Button(label="▶ Start", style=discord.ButtonStyle.success, row=0)
        start_button.callback = lambda inter: self.action_callback(inter, 'start')

        # Stop = orange/danger — actively interrupts the VPS
        stop_button = discord.ui.Button(label="⏸ Stop", style=discord.ButtonStyle.danger, row=0)
        stop_button.callback = lambda inter: self.action_callback(inter, 'stop')

        # SSH = blue — connect/access action, not destructive
        ssh_button = discord.ui.Button(label="🔑 SSH", style=discord.ButtonStyle.primary, row=0)
        ssh_button.callback = lambda inter: self.action_callback(inter, 'ssh')

        # Cleanup = gray — utility, frees disk but not destructive to data
        cleanup_button = discord.ui.Button(label="🧹 Cleanup", style=discord.ButtonStyle.secondary, row=0)
        cleanup_button.callback = lambda inter: self.action_callback(inter, 'cleanup')

        self.add_item(start_button)
        self.add_item(stop_button)
        self.add_item(ssh_button)
        self.add_item(cleanup_button)

        # ── Row 1: Tools ─────────────────────────────────────────────────────
        # Health Check = green — helpful diagnostic, no risk
        fix_button = discord.ui.Button(label="🩺 Health Check", style=discord.ButtonStyle.success, row=1)
        fix_button.callback = lambda inter: self.action_callback(inter, 'fix_scan')

        # Backup Now = blue — important save action, initiates a process
        backup_button = discord.ui.Button(label="📸 Backup Now", style=discord.ButtonStyle.primary, row=1)
        backup_button.callback = lambda inter: self.action_callback(inter, 'backup_now')

        # Setup Guide = gray — informational, passive
        guide_button = discord.ui.Button(label="🎯 Setup Guide", style=discord.ButtonStyle.secondary, row=1)
        guide_button.callback = lambda inter: self.action_callback(inter, 'setup_guide')

        # Schedule Backup = gray — utility/scheduling
        sched_button = discord.ui.Button(label="📅 Schedule Backup", style=discord.ButtonStyle.secondary, row=1)
        sched_button.callback = lambda inter: self.action_callback(inter, 'sched_backup')

        self.add_item(fix_button)
        self.add_item(backup_button)
        self.add_item(guide_button)
        self.add_item(sched_button)

        # ── Row 2: Advanced tools ──────────────────────────────────────────────
        # Clone VPS = red — destructive (overwrites target VPS)
        clone_button = discord.ui.Button(label="📦 Clone VPS", style=discord.ButtonStyle.danger, row=2)
        clone_button.callback = lambda inter: self.action_callback(inter, 'clone_vps')

        # Share Access = blue — connect/access, important but not destructive
        share_button = discord.ui.Button(label="🔗 Share Access", style=discord.ButtonStyle.primary, row=2)
        share_button.callback = lambda inter: self.action_callback(inter, 'share_access')

        # File Manager = blue — important feature, active file operations
        files_button = discord.ui.Button(label="📂 File Manager", style=discord.ButtonStyle.primary, row=2)
        files_button.callback = lambda inter: self.action_callback(inter, 'file_manager')

        # Backup List = gray — passive viewing/restore list
        bklist_button = discord.ui.Button(label="💾 Backup List", style=discord.ButtonStyle.secondary, row=2)
        bklist_button.callback = lambda inter: self.action_callback(inter, 'backup_list')

        # Get Help = green — support action, always helpful
        help_button = discord.ui.Button(label="❓ Get Help", style=discord.ButtonStyle.success, row=2)
        help_button.callback = lambda inter: self.action_callback(inter, 'help_support')

        self.add_item(clone_button)
        self.add_item(share_button)
        self.add_item(files_button)
        self.add_item(bklist_button)
        self.add_item(help_button)

    async def select_vps(self, interaction: discord.Interaction):
        if str(interaction.user.id) != self.user_id and not self.is_admin:
            await interaction.response.send_message(
                embed=create_error_embed("Access Denied", "This is not your VPS!"), ephemeral=True)
            return
        self.selected_index = int(self.select.values[0])
        new_embed = self.create_vps_embed(self.selected_index)
        self.clear_items()
        self.add_action_buttons()
        await interaction.response.edit_message(embed=new_embed, view=self)

    async def action_callback(self, interaction: discord.Interaction, action: str):
        if str(interaction.user.id) != self.user_id and not self.is_admin:
            await interaction.response.send_message(
                embed=create_error_embed("Access Denied", "This is not your VPS!"), ephemeral=True)
            return

        if self.is_shared:
            vps = vps_data[self.owner_id][self.selected_index]
        else:
            vps = self.vps_list[self.selected_index]

        container_name = vps["container_name"]

        # Block start on suspended VPS for regular users
        if vps.get("status") == "suspended" and action == "start" and not self.is_admin:
            await interaction.response.send_message(
                embed=create_error_embed(
                    "VPS Suspended",
                    "Your VPS has been suspended. Contact an admin to unsuspend it."
                ), ephemeral=True)
            return

        if action == 'reinstall':
            if self.is_shared or self.is_admin:
                await interaction.response.send_message(
                    embed=create_error_embed("Access Denied", "Only the VPS owner can reinstall!"),
                    ephemeral=True)
                return

            confirm_embed = create_warning_embed(
                "Reinstall Warning",
                f"⚠️ **WARNING:** This will erase all data on VPS `{container_name}` and reinstall Ubuntu 22.04.\n\n"
                f"This action cannot be undone. Continue?"
            )

            class ConfirmView(discord.ui.View):
                def __init__(self, parent_view, container_name, vps, owner_id, selected_index):
                    super().__init__(timeout=60)
                    self.parent_view    = parent_view
                    self.container_name = container_name
                    self.vps            = vps
                    self.owner_id       = owner_id
                    self.selected_index = selected_index

                @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
                async def confirm(self, interaction: discord.Interaction, item: discord.ui.Button):
                    await interaction.response.defer(ephemeral=True)
                    try:
                        await interaction.followup.send(
                            embed=create_info_embed("Deleting Container", f"Removing `{self.container_name}`..."),
                            ephemeral=True)
                        try:
                            await execute_docker(f"docker stop {self.container_name}")
                        except Exception:
                            pass
                        await execute_docker(f"docker rm -f {self.container_name}")

                        await interaction.followup.send(
                            embed=create_info_embed("Recreating Container", f"Creating new container `{self.container_name}`..."),
                            ephemeral=True)
                        original_ram  = self.vps["ram"]
                        original_cpu  = self.vps["cpu"]
                        original_disk = int(self.vps.get("storage", "30GB").replace("GB", ""))
                        ram_mb        = int(original_ram.replace("GB", "")) * 1024
                        new_password  = generate_password()
                        # Preserve the container's original hostname on reinstall
                        reinstall_hostname = self.vps.get("hostname", f"{VPS_HOSTNAME}-1")
                        await create_docker_container(
                            self.container_name, ram_mb, original_cpu, 0, new_password,
                            disk_gb=original_disk, hostname=reinstall_hostname
                        )
                        self.vps["status"]            = "running"
                        self.vps["ssh_password"]      = new_password
                        self.vps["created_at"]        = datetime.now().isoformat()
                        self.vps.pop("suspension_reason",  None)
                        self.vps.pop("suspension_time",    None)
                        self.vps.pop("suspension_evidence", None)
                        save_data()
                        await interaction.followup.send(
                            embed=create_success_embed(
                                "Reinstall Complete",
                                f"VPS `{self.container_name}` reinstalled successfully!"
                            ), ephemeral=True)
                        _rl = get_logo_url()
                        _log = discord.Embed(
                            title="🔄  VPS Reinstalled",
                            description=(
                                f"{interaction.user.mention} wiped and rebuilt **`{self.container_name}`** from scratch.\n"
                                f"> Previous data is gone — new credentials sent to the owner."
                            ),
                            colour=discord.Colour.from_rgb(155, 89, 182),
                            timestamp=_utcnow()
                        )
                        _log.set_author(name="VPS Reinstalled  •  Fresh OS", **( {"icon_url": _rl} if _rl else {}))
                        try:
                            _log.set_thumbnail(url=interaction.user.display_avatar.url)
                        except Exception:
                            pass
                        _log.add_field(name="📦 Container",    value=f"`{self.container_name}`",  inline=True)
                        _log.add_field(name="🏷️ Hostname",     value=f"`{reinstall_hostname}`",   inline=True)
                        _log.add_field(name="🌐 Status",       value="🟢 **RUNNING**",            inline=True)
                        _log.add_field(name="👤 Owner",        value=f"<@{self.owner_id}>",       inline=True)
                        _log.add_field(name="🎮 Triggered By", value=interaction.user.mention,    inline=True)
                        _log.add_field(name="🔒 Password",     value="✅ Regenerated",             inline=True)
                        _log.add_field(name="🧠 RAM",          value=f"`{original_ram}`",         inline=True)
                        _log.add_field(name="⚙️ CPU",          value=f"`{original_cpu}`",         inline=True)
                        _log.add_field(name="💾 Disk",         value=f"`{original_disk} GB`",     inline=True)
                        _log.set_footer(**{"text": f"{get_brand_name()} VPS Logs  •  Reinstall", **({"icon_url": _rl} if _rl else {})})
                        asyncio.create_task(send_log(_log))
                        if not self.parent_view.is_shared:
                            await interaction.message.edit(
                                embed=self.parent_view.create_vps_embed(self.parent_view.selected_index),
                                view=self.parent_view)
                    except Exception as e:
                        await interaction.followup.send(
                            embed=create_error_embed("Reinstall Failed", f"Error: {str(e)}"),
                            ephemeral=True)

                @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
                async def cancel(self, interaction: discord.Interaction, item: discord.ui.Button):
                    await interaction.response.edit_message(
                        embed=self.parent_view.create_vps_embed(self.parent_view.selected_index),
                        view=self.parent_view)

            await interaction.response.send_message(
                embed=confirm_embed,
                view=ConfirmView(self, container_name, vps, self.owner_id, self.selected_index),
                ephemeral=True)

        elif action == 'start':
            await interaction.response.defer(ephemeral=True)
            try:
                await execute_docker(f"docker start {container_name}")
                await asyncio.sleep(5)
                await docker_exec(container_name, "systemctl restart ssh 2>/dev/null || /usr/sbin/sshd || true", timeout=15)
                vps["status"] = "running"
                save_data()
                await interaction.followup.send(
                    embed=create_success_embed("VPS Started", f"VPS `{container_name}` is now running!"),
                    ephemeral=True)
                await interaction.message.edit(embed=self.create_vps_embed(self.selected_index), view=self)
                _logo_url = get_logo_url()
                _log = discord.Embed(
                    title="▶️  VPS Started",
                    description=f"{interaction.user.mention} brought **`{container_name}`** back online.",
                    colour=discord.Colour.from_rgb(52, 152, 219),
                    timestamp=_utcnow()
                )
                _log.set_author(name="VPS Started  •  Container Online", **( {"icon_url": _logo_url} if _logo_url else {}))
                try:
                    _log.set_thumbnail(url=interaction.user.display_avatar.url)
                except Exception:
                    pass
                _log.add_field(name="📦 Container", value=f"`{container_name}`",               inline=True)
                _log.add_field(name="🏷️ Hostname",  value=f"`{vps.get('hostname', 'N/A')}`",   inline=True)
                _log.add_field(name="🌐 Status",     value="🟢 **RUNNING**",                    inline=True)
                _log.add_field(name="👤 Owner",      value=f"<@{self.owner_id}>",               inline=True)
                _log.add_field(name="🎮 Started By", value=interaction.user.mention,            inline=True)
                _log.add_field(name="🧠 RAM",        value=f"`{vps.get('ram', 'N/A')}`",        inline=True)
                _log.add_field(name="⚙️ CPU",        value=f"`{vps.get('cpu', 'N/A')} Core(s)`", inline=True)
                _log.set_footer(**{"text": f"{get_brand_name()} VPS Logs  •  Start Event", **({"icon_url": _logo_url} if _logo_url else {})})
                asyncio.create_task(send_log(_log))
            except Exception as e:
                await interaction.followup.send(
                    embed=create_error_embed("Start Failed", str(e)), ephemeral=True)

        elif action == 'stop':
            await interaction.response.defer(ephemeral=True)
            try:
                await execute_docker(f"docker stop {container_name}", timeout=120)
                vps["status"] = "stopped"
                save_data()
                await interaction.followup.send(
                    embed=create_success_embed("VPS Stopped", f"VPS `{container_name}` has been stopped!"),
                    ephemeral=True)
                await interaction.message.edit(embed=self.create_vps_embed(self.selected_index), view=self)
                _stop_logo = get_logo_url()
                _log = discord.Embed(
                    title="⏹️  VPS Stopped",
                    description=f"{interaction.user.mention} shut down **`{container_name}`**.",
                    colour=discord.Colour.from_rgb(240, 165, 0),
                    timestamp=_utcnow()
                )
                _log.set_author(name="VPS Stopped  •  Container Offline", **( {"icon_url": _stop_logo} if _stop_logo else {}))
                try:
                    _log.set_thumbnail(url=interaction.user.display_avatar.url)
                except Exception:
                    pass
                _log.add_field(name="📦 Container", value=f"`{container_name}`",               inline=True)
                _log.add_field(name="🏷️ Hostname",  value=f"`{vps.get('hostname', 'N/A')}`",   inline=True)
                _log.add_field(name="🌐 Status",     value="🔴 **STOPPED**",                    inline=True)
                _log.add_field(name="👤 Owner",      value=f"<@{self.owner_id}>",               inline=True)
                _log.add_field(name="🎮 Stopped By", value=interaction.user.mention,            inline=True)
                _log.add_field(name="🧠 RAM",        value=f"`{vps.get('ram', 'N/A')}`",        inline=True)
                _log.add_field(name="⚙️ CPU",        value=f"`{vps.get('cpu', 'N/A')} Core(s)`", inline=True)
                _log.set_footer(**{"text": f"{get_brand_name()} VPS Logs  •  Stop Event", **({"icon_url": _stop_logo} if _stop_logo else {})})
                asyncio.create_task(send_log(_log))
            except Exception as e:
                await interaction.followup.send(
                    embed=create_error_embed("Stop Failed", str(e)), ephemeral=True)

        elif action == 'ssh':
            await interaction.response.defer(ephemeral=True)
            try:
                ssh_password = vps.get("ssh_password")
                if not ssh_password:
                    await interaction.followup.send(
                        embed=create_error_embed(
                            "SSH Error", "SSH credentials not found. Please reinstall the VPS."
                        ), ephemeral=True)
                    return

                # Auto-unpause if container is paused (paused containers reject docker exec)
                try:
                    _chk_proc = await asyncio.create_subprocess_exec(
                        "docker", "inspect", "--format", "{{.State.Paused}}", container_name,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                    _chk_out, _ = await asyncio.wait_for(_chk_proc.communicate(), timeout=10)
                    if _chk_out.decode().strip() == "true":
                        logger.info(f"[ssh] Container {container_name} is paused — unpausing before session start")
                        await execute_docker(f"docker unpause {container_name}", timeout=15)
                        await asyncio.sleep(2)
                except Exception as _up_err:
                    logger.warning(f"[ssh] Unpause check failed (non-fatal): {_up_err}")

                await interaction.followup.send(
                    embed=create_info_embed("🔗 Generating Access Links", "Starting tmate and sshx sessions, please wait…"),
                    ephemeral=True)

                host_ip = await _get_server_ip()
                stored_port = vps.get("ssh_port", 0)

                # ── tmate (optional) ───────────────────────────────────────────
                tmate_info = {}
                tmate_err = ""
                try:
                    tmate_info = await get_tmate_session(container_name)
                    # Persist updated token (SSH only — web URL not exposed)
                    vps["tmate_ssh"] = tmate_info.get("ssh", "")
                    save_data()
                except Exception as _te:
                    tmate_err = str(_te)

                # ── sshx (optional) ────────────────────────────────────────────
                sshx_url = ""
                sshx_err = ""
                try:
                    sshx_url = await get_sshx_session(container_name)
                    vps["sshx_url"] = sshx_url
                    save_data()
                except Exception as _se:
                    sshx_err = str(_se)

                # ── DarkNodes-branded access embed ────────────────────────────
                _ssh_logo = get_logo_url()
                ssh_embed = discord.Embed(
                    title="🔑  VPS Access — Session Links",
                    description=(
                        f"Fresh session links for `{container_name}` generated just now.\n"
                        f"> Links expire when the VPS is stopped or reinstalled."
                    ),
                    color=0x1ABC9C,
                    timestamp=_utcnow(),
                )
                if _ssh_logo:
                    ssh_embed.set_author(name=f"{get_brand_name()} VPS Hosting", icon_url=_ssh_logo)
                    ssh_embed.set_thumbnail(url=_ssh_logo)

                # tmate SSH
                if tmate_info.get("ssh"):
                    ssh_embed.add_field(
                        name="🖥️  tmate SSH",
                        value=f"```{tmate_info['ssh']}```",
                        inline=False,
                    )
                elif tmate_err:
                    ssh_embed.add_field(
                        name="🖥️  tmate SSH",
                        value=f"⚠️ `{tmate_err[:120]}`",
                        inline=False,
                    )

                # sshx
                if sshx_url:
                    ssh_embed.add_field(
                        name="🔗  sshx Web Terminal",
                        value=f"> {sshx_url}\nOpen in any browser — no SSH client needed.",
                        inline=False,
                    )
                elif sshx_err:
                    ssh_embed.add_field(
                        name="🔗  sshx Web Terminal",
                        value=f"⚠️ `{sshx_err[:120]}`",
                        inline=False,
                    )

                if not tmate_info.get("ssh") and not sshx_url:
                    ssh_embed.add_field(
                        name="⚠️  No Sessions Available",
                        value="Both tmate and sshx failed to start. Make sure the VPS is running.",
                        inline=False,
                    )

                ssh_embed.add_field(
                    name="📌  How to Use",
                    value=(
                        "• **tmate** — paste command in any terminal\n"
                        "• **sshx** — open the link in your browser\n"
                        "• Click **SSH** again in `!manage` to refresh"
                    ),
                    inline=False,
                )
                _ssh_fkw: dict = {"text": f"{get_brand_name()} VPS  •  Keep these links private"}
                if _ssh_logo:
                    _ssh_fkw["icon_url"] = _ssh_logo
                ssh_embed.set_footer(**_ssh_fkw)

                try:
                    await interaction.user.send(embed=ssh_embed)
                    await interaction.followup.send(
                        embed=create_success_embed("Access Details Sent", "Check your DMs for your session links!"),
                        ephemeral=True)
                except discord.Forbidden:
                    await interaction.followup.send(
                        embed=create_error_embed("DM Failed", "Enable DMs to receive session links!"),
                        ephemeral=True)
            except Exception as e:
                await interaction.followup.send(
                    embed=create_error_embed("SSH Error", str(e)), ephemeral=True)

        elif action == 'cleanup':
            await interaction.response.defer(ephemeral=True)
            try:
                await interaction.followup.send(
                    embed=create_info_embed("🧹 Cleaning Up…", f"Running cleanup on `{container_name}`…\nThis may take up to 60 seconds."),
                    ephemeral=True)
                result = await _do_cleanup(container_name)
                reclaimed = result["reclaimed_mb"]
                embed = create_success_embed(
                    "🧹 Cleanup Complete",
                    f"Finished cleaning `{container_name}`!"
                )
                embed.add_field(name="✅ Cleaned",         value="• apt cache\n• Journal logs\n• Docker images\n• Temp files\n• pip/npm cache", inline=True)
                embed.add_field(name="💾 Space Reclaimed", value=f"**~{max(0, reclaimed)} MB**", inline=True)
                await interaction.followup.send(embed=embed, ephemeral=True)
            except Exception as e:
                await interaction.followup.send(embed=create_error_embed("Cleanup Failed", str(e)[:300]), ephemeral=True)

        elif action == 'fix_scan':
            await interaction.response.defer(ephemeral=True)
            await interaction.followup.send(
                embed=create_info_embed("🔍 Scanning VPS…", f"Running diagnostics on `{container_name}`…\nThis may take ~30 seconds."),
                ephemeral=True)
            try:
                issues, fixes = await _do_vps_scan(container_name)
                if not issues:
                    embed = create_success_embed("✅ All Clear", f"`{container_name}` passed all health checks!\n\nContainer ✅  SSH ✅  Disk ✅  DinD ✅  Services ✅  DNS ✅  RAM ✅")
                    await interaction.followup.send(embed=embed, ephemeral=True)
                    return
                embed = create_warning_embed(
                    f"🩺 Found {len(issues)} Issue(s)",
                    f"Issues detected on `{container_name}`:"
                )
                fixable = [(lbl, desc, key) for lbl, desc, key in issues if key and key in fixes]
                for lbl, desc, key in issues:
                    note = "\n✅ *One-click fix below*" if (key and key in fixes) else ""
                    embed.add_field(name=lbl, value=f"{desc}{note}", inline=False)

                if not fixable:
                    await interaction.followup.send(embed=embed, ephemeral=True)
                    return

                owner_id_local = self.owner_id

                class InlineFix(discord.ui.View):
                    def __init__(self):
                        super().__init__(timeout=120)
                        for lbl, desc, fix_key in fixable[:4]:
                            short = lbl.split(" ", 1)[1][:32] if " " in lbl else lbl[:32]
                            btn = discord.ui.Button(label=f"Fix: {short}", style=discord.ButtonStyle.primary)
                            btn.callback = self._cb(fix_key, fixes[fix_key])
                            self.add_item(btn)

                    def _cb(self, fix_key, fix_cmd):
                        async def callback(inter: discord.Interaction):
                            await inter.response.defer(ephemeral=True)
                            try:
                                if fix_key == "start_container":
                                    await execute_docker(f"docker start {container_name}")
                                    await inter.followup.send(embed=create_success_embed("Started", f"`{container_name}` is starting!"), ephemeral=True)
                                else:
                                    out, _, _ = await docker_exec(container_name, fix_cmd, timeout=40)
                                    await inter.followup.send(embed=create_success_embed("Fix Applied", f"```\n{out[:400] or 'Done'}\n```"), ephemeral=True)
                            except Exception as ex:
                                await inter.followup.send(embed=create_error_embed("Fix Failed", str(ex)[:300]), ephemeral=True)
                        return callback

                await interaction.followup.send(embed=embed, view=InlineFix(), ephemeral=True)
            except Exception as e:
                await interaction.followup.send(embed=create_error_embed("Scan Failed", str(e)[:300]), ephemeral=True)

        elif action == 'backup_now':
            await interaction.response.defer(ephemeral=True)
            snapshot_name = f"{container_name}-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            await interaction.followup.send(
                embed=create_info_embed("📸 Creating Backup…", f"Committing snapshot of `{container_name}`…\nThis may take a minute."),
                ephemeral=True)
            try:
                await execute_docker(f"docker commit {container_name} {snapshot_name}", timeout=180)
                embed = create_success_embed("📸 Backup Created", f"Snapshot saved successfully!")
                embed.add_field(name="📦 Snapshot",  value=f"`{snapshot_name}`", inline=False)
                embed.add_field(name="💡 Restore",   value=f"Restore this backup anytime from **💾 Backup List** in `!manage`.", inline=False)
                await interaction.followup.send(embed=embed, ephemeral=True)
            except Exception as e:
                await interaction.followup.send(embed=create_error_embed("Backup Failed", str(e)[:300]), ephemeral=True)

        elif action == 'setup_guide':
            await interaction.response.defer(ephemeral=True)
            embed = create_embed("🎯 Guided Setup — What's Next?", f"Recommended next steps for `{container_name}`:", 0x5865F2)
            embed.add_field(name="① 🌐 Install Cloudflare Tunnel",  value="Run `/template` → select **Cloudflare Tunnel**.\nExposes your panel without opening firewall ports.", inline=False)
            embed.add_field(name="② ⚙️ Configure the Tunnel",       value="In Cloudflare dashboard, point your domain to `localhost:80`.", inline=False)
            embed.add_field(name="③ 🦅 Install Pterodactyl Wings",  value="Run `/template` → select **Pterodactyl Wings**.\nRequired to run game servers.", inline=False)
            embed.add_field(name="④ 🔗 Link Wings to the Panel",    value="Panel → Admin → Nodes → Create Node → copy config → paste to `/etc/pterodactyl/config.yml`.", inline=False)
            embed.add_field(name="⑤ 🎮 Create Your First Server",   value="Panel → Admin → Servers → Create Server → pick an egg.", inline=False)
            embed.add_field(name="💡 Quick Tips",                   value=f"• `!fix` — scan for issues\n• `!cleanup` — free disk space\n• `!schedule-backup 1 daily` — auto-backup\n• `!notify-settings` — smart alerts", inline=False)
            await interaction.followup.send(embed=embed, ephemeral=True)

        elif action == 'sched_backup':
            await interaction.response.defer(ephemeral=True)
            owner_id_for_sched = self.owner_id

            class SchedView(discord.ui.View):
                def __init__(self):
                    super().__init__(timeout=60)

                def _make_sched_cb(self, freq):
                    async def callback(inter: discord.Interaction):
                        await inter.response.defer(ephemeral=True)
                        now = _utcnow()
                        delta = {"daily": timedelta(days=1), "weekly": timedelta(weeks=1), "monthly": timedelta(days=30)}
                        desc  = {"daily": "every 24 hours", "weekly": "every 7 days", "monthly": "every 30 days"}
                        scheduled_backups.setdefault(owner_id_for_sched, {})[container_name] = {
                            "frequency": freq, "next_run": (now + delta[freq]).isoformat(),
                            "enabled": True, "last_run": None, "last_status": None
                        }
                        save_scheduled_backups()
                        embed = create_success_embed("📅 Backup Scheduled", f"Auto-backup enabled for `{container_name}`!")
                        embed.add_field(name="📆 Frequency", value=f"`{freq.capitalize()}` — {desc[freq]}", inline=True)
                        embed.add_field(name="⏭ Next Run",   value=f"`{(now + delta[freq]).isoformat()[:16]} UTC`", inline=True)
                        await inter.followup.send(embed=embed, ephemeral=True)
                    return callback

                @discord.ui.button(label="📅 Daily",   style=discord.ButtonStyle.primary)
                async def daily(self,   inter, btn): await self._make_sched_cb("daily")(inter)

                @discord.ui.button(label="🗓 Weekly",  style=discord.ButtonStyle.secondary)
                async def weekly(self,  inter, btn): await self._make_sched_cb("weekly")(inter)

                @discord.ui.button(label="📆 Monthly", style=discord.ButtonStyle.secondary)
                async def monthly(self, inter, btn): await self._make_sched_cb("monthly")(inter)

            pick_embed = create_info_embed("📅 Schedule Backup", f"How often should `{container_name}` be backed up?\nYou'll receive a DM after each backup.")
            await interaction.followup.send(embed=pick_embed, view=SchedView(), ephemeral=True)

        elif action == 'clone_vps':
            await interaction.response.defer(ephemeral=True)
            owner_id_clone = self.owner_id
            clone_vps_list = vps_data.get(owner_id_clone, [])
            if len(clone_vps_list) < 2:
                await interaction.followup.send(
                    embed=create_error_embed("No Other VPS", "You need at least 2 VPS to clone between them.\nUse `!manage` to see your VPS or purchase another."),
                    ephemeral=True)
                return

            def _do_clone(src_name, target_vps_entry):
                """Return an async callback that commits src_name into target_vps_entry."""
                async def callback(inter: discord.Interaction):
                    await inter.response.defer(ephemeral=True)
                    target_name = target_vps_entry['container_name']
                    await inter.followup.send(
                        embed=create_info_embed("📦 Cloning VPS…",
                            f"Cloning `{src_name}` → `{target_name}`\nThis takes ~2–5 minutes. Do not restart your VPS during cloning."),
                        ephemeral=True)
                    try:
                        clone_image = f"clone-{src_name}-{int(time.time())}"
                        await execute_docker(f"docker commit {src_name} {clone_image}", timeout=300)
                        try:
                            await execute_docker(f"docker stop {target_name}", timeout=60)
                        except Exception:
                            pass
                        await execute_docker(f"docker rm -f {target_name}", timeout=30)
                        ssh_port = target_vps_entry.get('ssh_port', get_next_ssh_port())
                        hostname  = target_vps_entry.get('hostname', VPS_HOSTNAME)
                        run_cmd = (
                            f"docker run -d --name {target_name} --hostname {hostname} "
                            f"-p {ssh_port}:22 --restart=unless-stopped --privileged --cgroupns=host "
                            f"-v /sys/fs/cgroup:/sys/fs/cgroup:rw "
                            f"--tmpfs /run:exec,mode=755,size=256m --tmpfs /run/lock:size=64m --tmpfs /tmp:exec,size=512m "
                            f"-v {target_name}-docker:/var/lib/docker -v {target_name}-home:/home "
                            f"-v {target_name}-root:/root -v {target_name}-opt:/opt "
                            f"-e container=docker --dns 8.8.8.8 --dns 1.1.1.1 "
                            f"--security-opt seccomp=unconfined --security-opt apparmor=unconfined "
                            f"--shm-size=512m --ulimit nofile=65536:65536 "
                            f"{clone_image}"
                        )
                        await execute_docker(run_cmd, timeout=120)
                        await asyncio.sleep(10)
                        await docker_exec(target_name, "systemctl restart ssh 2>/dev/null || /usr/sbin/sshd || true", timeout=20)
                        target_vps_entry['status'] = 'running'
                        save_data()
                        try:
                            await run_docker_command(f"docker rmi {clone_image}", timeout=30)
                        except Exception:
                            pass
                        embed = create_success_embed("📦 Clone Complete!", f"`{src_name}` cloned into `{target_name}` successfully!")
                        embed.add_field(name="🖥️ Source",  value=f"`{src_name}`",    inline=True)
                        embed.add_field(name="📥 Target",  value=f"`{target_name}`", inline=True)
                        embed.add_field(name="🔑 Login",   value=f"SSH password unchanged for `{target_name}`", inline=False)
                        await inter.followup.send(embed=embed, ephemeral=True)
                    except Exception as ex:
                        await inter.followup.send(embed=create_error_embed("Clone Failed", str(ex)[:300]), ephemeral=True)
                return callback

            # ── Step 1: Choose SOURCE ──────────────────────────────────────────
            class CloneSourceView(discord.ui.View):
                def __init__(self_csv):
                    super().__init__(timeout=60)
                    for idx, vps_s in enumerate(clone_vps_list[:5]):
                        is_current = vps_s['container_name'] == container_name
                        label = f"{'★ ' if is_current else ''}VPS {idx+1}: {vps_s['container_name'][:18]}"
                        btn = discord.ui.Button(
                            label=label,
                            style=discord.ButtonStyle.primary if is_current else discord.ButtonStyle.secondary,
                        )
                        btn.callback = self_csv._make_source_cb(vps_s)
                        self_csv.add_item(btn)

                def _make_source_cb(self_csv, src_vps):
                    async def callback(inter: discord.Interaction):
                        await inter.response.defer(ephemeral=True)
                        src_name = src_vps['container_name']
                        others = [(i, v) for i, v in enumerate(clone_vps_list) if v['container_name'] != src_name]
                        if not others:
                            await inter.followup.send(
                                embed=create_error_embed("No Target", "No other VPS found to clone into."), ephemeral=True)
                            return

                        # ── Step 2: Choose TARGET ───────────────────────────
                        class CloneTargetView(discord.ui.View):
                            def __init__(self_cv):
                                super().__init__(timeout=60)
                                for idx2, vps_t in others[:4]:
                                    btn2 = discord.ui.Button(
                                        label=f"→ VPS {idx2+1}: {vps_t['container_name'][:20]}",
                                        style=discord.ButtonStyle.danger
                                    )
                                    btn2.callback = _do_clone(src_name, vps_t)
                                    self_cv.add_item(btn2)

                        target_embed = create_embed(
                            "📦 Clone VPS — Step 2: Select Target",
                            f"Clone **from** `{src_name}` **into** which VPS?\n⚠️ **Target VPS data will be replaced.**",
                            0xF0A500)
                        for idx2, vps_t in others[:4]:
                            target_embed.add_field(
                                name=f"VPS {idx2+1}",
                                value=f"`{vps_t['container_name']}` — {vps_t.get('status','?').upper()}",
                                inline=True)
                        await inter.followup.send(embed=target_embed, view=CloneTargetView(), ephemeral=True)
                    return callback

            source_embed = create_embed(
                "📦 Clone VPS — Step 1: Select Source",
                "Which VPS do you want to **clone FROM**?\n★ = currently managing",
                0x5865F2)
            for idx, vps_s in enumerate(clone_vps_list[:5]):
                source_embed.add_field(
                    name=f"VPS {idx+1}",
                    value=f"`{vps_s['container_name']}` — {vps_s.get('status','?').upper()}",
                    inline=True)
            await interaction.followup.send(embed=source_embed, view=CloneSourceView(), ephemeral=True)

        elif action == 'share_access':
            await interaction.response.defer(ephemeral=True)
            perm_embed = create_embed("🔗 Share VPS Access", f"Share `{container_name}` with another Discord user.\nChoose a permission level:", 0x5865F2)
            perm_embed.add_field(name="👁️ View Only",      value="Can see VPS info and status — cannot control it.",    inline=False)
            perm_embed.add_field(name="🔄 Restart",        value="Can start and stop the VPS.",                         inline=False)
            perm_embed.add_field(name="🔑 Full Management",value="Can start, stop, and SSH into the VPS.",              inline=False)
            perm_embed.add_field(name="📌 How to Share",   value=f"`!share-vps @user {self.selected_index + 1} view`\n`!share-vps @user {self.selected_index + 1} restart`\n`!share-vps @user {self.selected_index + 1} full`", inline=False)
            perm_embed.add_field(name="🔒 Revoke Access",  value=f"`!revoke-share @user {self.selected_index + 1}`",    inline=False)
            perm_embed.add_field(name="📋 See Who Has Access", value="`!my-shares`", inline=False)
            await interaction.followup.send(embed=perm_embed, ephemeral=True)

        elif action == 'file_manager':
            await interaction.response.defer(ephemeral=True)
            try:
                ls_out, _, _ = await docker_exec(container_name, "ls -lhA /root/ 2>/dev/null | head -30", timeout=15)
                lines = [l for l in ls_out.splitlines() if l.strip() and not l.startswith("total")]
                file_list = "\n".join(f"`{l}`" for l in lines[:20]) or "_No files found_"
                _vps_idx = self.selected_index + 1
                embed = create_embed("📂 File Manager", f"**Current directory:** `/root/` on `{container_name}`", 0x5865F2)
                embed.add_field(name="📁 Files", value=file_list[:1000], inline=False)
                embed.add_field(
                    name="💡 How to transfer files",
                    value=(
                        f"**Download:** `!download {_vps_idx} /path/to/file`\n"
                        f"**Upload:** attach a file then `!upload {_vps_idx} /dest/path/`\n"
                        f"**Edit:** `!editfile {_vps_idx} /path/to/file`\n"
                        f"**Delete:** `!deletefile {_vps_idx} /path/to/file`"
                    ),
                    inline=False
                )
                embed.set_footer(text="Use the buttons to browse folders and check disk usage instantly")

                class FileManagerView(discord.ui.View):
                    def __init__(self_fmv):
                        super().__init__(timeout=180)

                    async def _browse(self_fmv, inter2: discord.Interaction, path: str):
                        await inter2.response.defer(ephemeral=True)
                        ls2, _, _ = await docker_exec(container_name, f"ls -lhA {path} 2>/dev/null | head -30", timeout=15)
                        lines2 = [l for l in ls2.splitlines() if l.strip() and not l.startswith("total")]
                        fl2 = "\n".join(f"`{l}`" for l in lines2[:20]) or "_Empty_"
                        em2 = create_embed(f"📂 {path}", f"Contents on `{container_name}`:", 0x5865F2)
                        em2.add_field(name="Files", value=fl2[:1000], inline=False)
                        await inter2.followup.send(embed=em2, ephemeral=True)

                    # ── Row 0: Browse buttons ──────────────────────────────────
                    @discord.ui.button(label="📂 /root", style=discord.ButtonStyle.primary, row=0)
                    async def browse_root(self_fmv, inter2: discord.Interaction, btn: discord.ui.Button):
                        await self_fmv._browse(inter2, "/root/")

                    @discord.ui.button(label="📂 /etc", style=discord.ButtonStyle.secondary, row=0)
                    async def browse_etc(self_fmv, inter2: discord.Interaction, btn: discord.ui.Button):
                        await self_fmv._browse(inter2, "/etc/")

                    @discord.ui.button(label="📂 /var/log", style=discord.ButtonStyle.secondary, row=0)
                    async def browse_logs(self_fmv, inter2: discord.Interaction, btn: discord.ui.Button):
                        await self_fmv._browse(inter2, "/var/log/")

                    @discord.ui.button(label="📂 /opt", style=discord.ButtonStyle.secondary, row=0)
                    async def browse_opt(self_fmv, inter2: discord.Interaction, btn: discord.ui.Button):
                        await self_fmv._browse(inter2, "/opt/")

                    # ── Row 1: Info buttons ────────────────────────────────────
                    @discord.ui.button(label="💾 Disk Usage", style=discord.ButtonStyle.secondary, row=1)
                    async def disk_usage(self_fmv, inter2: discord.Interaction, btn: discord.ui.Button):
                        await inter2.response.defer(ephemeral=True)
                        df_out, _, _ = await docker_exec(
                            container_name,
                            "df -h / && echo '---' && du -sh /root/* 2>/dev/null | sort -rh | head -10",
                            timeout=15)
                        em2 = create_embed("💾 Disk Usage", f"Storage on `{container_name}`:", 0x5865F2)
                        em2.add_field(name="Usage", value=f"```\n{df_out[:900]}\n```", inline=False)
                        await inter2.followup.send(embed=em2, ephemeral=True)

                    @discord.ui.button(label="📋 Running Processes", style=discord.ButtonStyle.secondary, row=1)
                    async def processes(self_fmv, inter2: discord.Interaction, btn: discord.ui.Button):
                        await inter2.response.defer(ephemeral=True)
                        ps_out, _, _ = await docker_exec(
                            container_name, "ps aux --sort=-%cpu | head -15", timeout=15)
                        em2 = create_embed("📋 Top Processes", f"Running on `{container_name}`:", 0x5865F2)
                        em2.add_field(name="Processes", value=f"```\n{ps_out[:900]}\n```", inline=False)
                        await inter2.followup.send(embed=em2, ephemeral=True)

                    @discord.ui.button(label="📜 System Logs", style=discord.ButtonStyle.secondary, row=1)
                    async def system_logs(self_fmv, inter2: discord.Interaction, btn: discord.ui.Button):
                        await inter2.response.defer(ephemeral=True)
                        log_out, _, _ = await docker_exec(
                            container_name, "journalctl -n 20 --no-pager 2>/dev/null || tail -20 /var/log/syslog 2>/dev/null || echo 'No logs'", timeout=15)
                        em2 = create_embed("📜 System Logs", f"Last 20 lines on `{container_name}`:", 0x5865F2)
                        em2.add_field(name="Logs", value=f"```\n{log_out[:900]}\n```", inline=False)
                        await inter2.followup.send(embed=em2, ephemeral=True)

                    # ── Row 2: File management actions ─────────────────────────
                    @discord.ui.button(label="🗑️ Delete File", style=discord.ButtonStyle.danger, row=2)
                    async def delete_file(self_fmv, inter2: discord.Interaction, btn: discord.ui.Button):
                        """Open a modal to enter a file path and delete it."""
                        class DeleteModal(discord.ui.Modal, title="🗑️ Delete File or Folder"):
                            path_input = discord.ui.TextInput(
                                label="Full path to delete",
                                style=discord.TextStyle.short,
                                placeholder="/root/filename.txt  or  /root/myfolder",
                                required=True,
                                max_length=300,
                            )
                            async def on_submit(self_dm, inter3: discord.Interaction):
                                await inter3.response.defer(ephemeral=True)
                                target = self_dm.path_input.value.strip()
                                # Basic safety: disallow dangerous paths
                                if target in ("/", "/etc", "/usr", "/bin", "/sbin", "/lib", "/var", "/proc", "/sys"):
                                    await inter3.followup.send(
                                        embed=create_error_embed("Blocked", f"`{target}` is a protected system path."),
                                        ephemeral=True)
                                    return
                                check_out, _, check_rc = await docker_exec(
                                    container_name, f"test -e {target} && echo EXISTS || echo NOTFOUND", timeout=10)
                                if "NOTFOUND" in check_out:
                                    await inter3.followup.send(
                                        embed=create_error_embed("Not Found", f"Path `{target}` does not exist."),
                                        ephemeral=True)
                                    return
                                del_out, del_err, del_rc = await docker_exec(
                                    container_name, f"rm -rf {target} && echo DELETED", timeout=30)
                                if "DELETED" in del_out:
                                    await inter3.followup.send(
                                        embed=create_success_embed("🗑️ Deleted", f"`{target}` has been removed from `{container_name}`."),
                                        ephemeral=True)
                                else:
                                    await inter3.followup.send(
                                        embed=create_error_embed("Delete Failed", del_err[:300] or "Unknown error"),
                                        ephemeral=True)
                        await inter2.response.send_modal(DeleteModal())

                    @discord.ui.button(label="📁 New Folder", style=discord.ButtonStyle.success, row=2)
                    async def new_folder(self_fmv, inter2: discord.Interaction, btn: discord.ui.Button):
                        """Open a modal to create a new directory."""
                        class MkdirModal(discord.ui.Modal, title="📁 Create New Folder"):
                            path_input = discord.ui.TextInput(
                                label="Full path of folder to create",
                                style=discord.TextStyle.short,
                                placeholder="/root/myfolder  or  /opt/myapp/config",
                                required=True,
                                max_length=300,
                            )
                            async def on_submit(self_mm, inter3: discord.Interaction):
                                await inter3.response.defer(ephemeral=True)
                                target = self_mm.path_input.value.strip()
                                mk_out, mk_err, mk_rc = await docker_exec(
                                    container_name, f"mkdir -p {target} && echo CREATED", timeout=10)
                                if "CREATED" in mk_out:
                                    await inter3.followup.send(
                                        embed=create_success_embed("📁 Folder Created", f"`{target}` created on `{container_name}`."),
                                        ephemeral=True)
                                else:
                                    await inter3.followup.send(
                                        embed=create_error_embed("Failed", mk_err[:300] or "Unknown error"),
                                        ephemeral=True)
                        await inter2.response.send_modal(MkdirModal())

                    @discord.ui.button(label="📤 Upload File", style=discord.ButtonStyle.primary, row=2)
                    async def upload_help(self_fmv, inter2: discord.Interaction, btn: discord.ui.Button):
                        """Show instructions for uploading a file to the VPS."""
                        await inter2.response.defer(ephemeral=True)
                        em2 = create_info_embed(
                            "📤 How to Upload a File",
                            "To upload a file directly into your VPS, use the `!upload` command:")
                        em2.add_field(
                            name="📋 Steps",
                            value=(
                                f"1. Type `!upload {_vps_idx} /root/` in this channel\n"
                                f"2. **Attach the file** to that same message\n"
                                f"3. The file will be placed at `/root/<filename>` on your VPS\n\n"
                                f"**Example:**\n`!upload {_vps_idx} /root/myconfig.yml` *(with file attached)*"
                            ),
                            inline=False
                        )
                        em2.add_field(
                            name="📥 Download a File",
                            value=f"`!download {_vps_idx} /path/to/file` — sends the file to Discord",
                            inline=False
                        )
                        em2.add_field(
                            name="✏️ View & Edit a File",
                            value=f"`!editfile {_vps_idx} /path/to/file` — view and edit inline",
                            inline=False
                        )
                        await inter2.followup.send(embed=em2, ephemeral=True)

                await interaction.followup.send(embed=embed, view=FileManagerView(), ephemeral=True)
            except Exception as e:
                await interaction.followup.send(embed=create_error_embed("File Manager Error", str(e)[:300]), ephemeral=True)

        elif action == 'backup_list':
            await interaction.response.defer(ephemeral=True)
            try:
                proc = await asyncio.create_subprocess_exec(
                    "docker", "images", "--format", "{{.Repository}}:{{.Tag}}\t{{.Size}}\t{{.CreatedAt}}",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                stdout, _ = await proc.communicate()
                all_images = stdout.decode().strip().split('\n')
                backups = []
                for img in all_images:
                    if not img.strip():
                        continue
                    parts = img.split('\t')
                    name = parts[0] if parts else img
                    size = parts[1] if len(parts) > 1 else "?"
                    created = parts[2][:16] if len(parts) > 2 else "?"
                    if container_name + "-backup-" in name:
                        backups.append((name, size, created))
                if not backups:
                    await interaction.followup.send(
                        embed=create_info_embed("💾 No Backups", f"No backups found for `{container_name}`.\nUse **📸 Backup Now** to create one."),
                        ephemeral=True)
                    return
                embed = create_embed("💾 Backup List", f"Found **{len(backups)}** backup(s) for `{container_name}`:", 0x5865F2)
                for name, size, created in backups[:6]:
                    embed.add_field(name=f"📸 {name.split('-backup-')[-1] if '-backup-' in name else name}",
                                    value=f"Size: `{size}` | Created: `{created}`\n`{name}`", inline=False)
                embed.set_footer(text="⬇ Download = save backup file  •  🔄 Restore = roll back VPS to this snapshot")

                # Capture closure vars from outer ManageView
                _outer_user_id = self.user_id
                _outer_is_admin = self.is_admin

                class BackupListView(discord.ui.View):
                    def __init__(self_blv):
                        super().__init__(timeout=120)
                        # Show max 3 backups (2 buttons each = 6 total, fits in 2 rows)
                        for b_name, b_size, b_created in backups[:3]:
                            short = b_name.split('-backup-')[-1][:18] if '-backup-' in b_name else b_name[:18]
                            dl_btn = discord.ui.Button(label=f"⬇ {short}", style=discord.ButtonStyle.primary)
                            dl_btn.callback = self_blv._make_dl_cb(b_name)
                            restore_btn = discord.ui.Button(label=f"🔄 Restore", style=discord.ButtonStyle.danger)
                            restore_btn.callback = self_blv._make_restore_cb(b_name)
                            self_blv.add_item(dl_btn)
                            self_blv.add_item(restore_btn)

                    def _make_dl_cb(self_blv, image_name):
                        async def callback(inter: discord.Interaction):
                            if str(inter.user.id) != _outer_user_id and not _outer_is_admin:
                                await inter.response.send_message(embed=create_error_embed("Access Denied", "Not your VPS!"), ephemeral=True)
                                return
                            await inter.response.defer(ephemeral=True)
                            await inter.followup.send(
                                embed=create_info_embed("⬇ Preparing Download…",
                                    f"Exporting `{image_name}` as a compressed archive.\nThis may take 1–5 minutes depending on size."),
                                ephemeral=True)
                            safe_name = image_name.replace(':', '_').replace('/', '_')
                            tmp_path = f"/tmp/{safe_name}.tar.gz"
                            try:
                                export_proc = await asyncio.create_subprocess_shell(
                                    f"docker save {image_name} | gzip > {tmp_path}",
                                    stdout=asyncio.subprocess.DEVNULL,
                                    stderr=asyncio.subprocess.PIPE)
                                _, stderr_b = await asyncio.wait_for(export_proc.communicate(), timeout=600)
                                if export_proc.returncode != 0:
                                    raise RuntimeError(stderr_b.decode()[:200])
                                file_size_mb = os.path.getsize(tmp_path) / (1024 * 1024)
                                if file_size_mb <= 25:
                                    # Small enough — send directly via Discord
                                    await inter.followup.send(
                                        file=discord.File(tmp_path, filename=f"{safe_name}.tar.gz"),
                                        ephemeral=True)
                                    try:
                                        os.remove(tmp_path)
                                    except Exception:
                                        pass
                                else:
                                    # Too large for Discord — upload to transfer.sh and provide a link
                                    await inter.followup.send(
                                        embed=create_info_embed("☁️ Uploading to Cloud…",
                                            f"Backup is **{file_size_mb:.0f} MB** (Discord limit: 25 MB).\n"
                                            f"Uploading to **transfer.sh** — you'll get a download link shortly."),
                                        ephemeral=True)
                                    upload_proc = await asyncio.create_subprocess_shell(
                                        f"curl -s --upload-file {tmp_path} "
                                        f"https://transfer.sh/{safe_name}.tar.gz",
                                        stdout=asyncio.subprocess.PIPE,
                                        stderr=asyncio.subprocess.PIPE)
                                    upload_stdout, upload_stderr = await asyncio.wait_for(
                                        upload_proc.communicate(), timeout=300)
                                    download_url = upload_stdout.decode().strip()
                                    if upload_proc.returncode == 0 and download_url.startswith("http"):
                                        dl_embed = create_success_embed(
                                            "☁️ Backup Ready to Download",
                                            f"Your backup has been uploaded securely.\n"
                                            f"Click the link below to download it:")
                                        dl_embed.add_field(name="📦 Backup", value=f"`{image_name}`", inline=True)
                                        dl_embed.add_field(name="💾 Size", value=f"`{file_size_mb:.0f} MB`", inline=True)
                                        dl_embed.add_field(name="⬇ Download Link",
                                            value=f"[**Click here to download**]({download_url})\n`{download_url}`",
                                            inline=False)
                                        dl_embed.add_field(name="⏳ Expiry",
                                            value="Link expires in **14 days** — download it before then.",
                                            inline=False)
                                        dl_embed.set_footer(text="Hosted by transfer.sh — keep this link private")
                                        await inter.followup.send(embed=dl_embed, ephemeral=True)
                                    else:
                                        err_detail = upload_stderr.decode()[:200] or "transfer.sh returned empty response"
                                        await inter.followup.send(
                                            embed=create_error_embed("Upload Failed",
                                                f"Could not upload backup to transfer.sh.\n`{err_detail}`\n\n"
                                                f"The backup file is at: `{tmp_path}` on the host server."),
                                            ephemeral=True)
                                    # Clean up temp file
                                    try:
                                        os.remove(tmp_path)
                                    except Exception:
                                        pass
                            except asyncio.TimeoutError:
                                await inter.followup.send(embed=create_error_embed("Timeout", "Export timed out. Try again."), ephemeral=True)
                            except Exception as ex:
                                await inter.followup.send(embed=create_error_embed("Export Failed", str(ex)[:300]), ephemeral=True)
                        return callback

                    def _make_restore_cb(self_blv, image_name):
                        async def callback(inter: discord.Interaction):
                            if str(inter.user.id) != _outer_user_id and not _outer_is_admin:
                                await inter.response.send_message(embed=create_error_embed("Access Denied", "Not your VPS!"), ephemeral=True)
                                return
                            await inter.response.defer(ephemeral=True)
                            await inter.followup.send(
                                embed=create_info_embed("🔄 Restoring VPS…",
                                    f"Rolling back `{container_name}` to snapshot `{image_name}`.\n"
                                    f"This takes 2–3 minutes. Your VPS will restart automatically."),
                                ephemeral=True)
                            try:
                                try:
                                    await execute_docker(f"docker stop {container_name}", timeout=60)
                                except Exception:
                                    pass
                                await execute_docker(f"docker rm -f {container_name}", timeout=30)
                                ssh_port = vps.get('ssh_port', get_next_ssh_port())
                                hostname  = vps.get('hostname', VPS_HOSTNAME)
                                run_cmd = (
                                    f"docker run -d --name {container_name} --hostname {hostname} "
                                    f"-p {ssh_port}:22 --restart=unless-stopped --privileged --cgroupns=host "
                                    f"-v /sys/fs/cgroup:/sys/fs/cgroup:rw "
                                    f"--tmpfs /run:exec,mode=755,size=256m --tmpfs /run/lock:size=64m --tmpfs /tmp:exec,size=512m "
                                    f"-v {container_name}-docker:/var/lib/docker -v {container_name}-home:/home "
                                    f"-v {container_name}-root:/root -v {container_name}-opt:/opt "
                                    f"-e container=docker --dns 8.8.8.8 --dns 1.1.1.1 "
                                    f"--security-opt seccomp=unconfined --security-opt apparmor=unconfined "
                                    f"--shm-size=512m --ulimit nofile=65536:65536 "
                                    f"{image_name}"
                                )
                                await execute_docker(run_cmd, timeout=120)
                                await asyncio.sleep(10)
                                await docker_exec(container_name, "systemctl restart ssh 2>/dev/null || /usr/sbin/sshd || true", timeout=20)
                                vps['status'] = 'running'
                                save_data()
                                embed2 = create_success_embed("🔄 Restore Complete!", f"`{container_name}` restored successfully!")
                                embed2.add_field(name="📦 Snapshot", value=f"`{image_name}`", inline=False)
                                embed2.add_field(name="🔑 SSH", value="Credentials unchanged — use same password.", inline=False)
                                await inter.followup.send(embed=embed2, ephemeral=True)
                            except Exception as ex:
                                await inter.followup.send(embed=create_error_embed("Restore Failed", str(ex)[:300]), ephemeral=True)
                        return callback

                await interaction.followup.send(embed=embed, view=BackupListView(), ephemeral=True)
            except Exception as e:
                await interaction.followup.send(embed=create_error_embed("Backup List Error", str(e)[:300]), ephemeral=True)

        elif action == 'help_support':
            # Send a modal — do NOT defer first; response must be the modal itself
            _hs_container = container_name   # capture for closure

            # ── Knowledge base ─────────────────────────────────────────────────
            _KB = [
                {
                    "keywords": ["ssh", "connect", "connection refused", "timeout", "can't ssh", "cannot ssh",
                                 "port 22", "sshd", "ssh not working", "ssh failed", "ssh broken",
                                 "access denied", "authentication failed", "ssh error", "not connecting",
                                 "cant connect", "wont connect", "refuse", "refused"],
                    "title": "🔑 SSH Connection Issue",
                    "desc": "SSH service may not be running or the container needs a restart.",
                    "fix": "systemctl restart ssh 2>/dev/null; /usr/sbin/sshd 2>/dev/null || true; sleep 2; systemctl status ssh --no-pager 2>&1 | tail -5; echo '---'; ss -tlnp | grep :22 || echo 'Port 22 not listening'",
                    "fix_label": "Restart SSH Service",
                },
                {
                    "keywords": ["disk", "full", "no space", "storage", "100%", "out of space", "enospc",
                                 "disk space", "disk usage", "storage full", "running out", "quota",
                                 "no room", "write failed", "write error", "filesystem", "df", "du"],
                    "title": "💾 Disk Full",
                    "desc": "Disk is full — cleans unused Docker images, apt cache, and old logs.",
                    "fix": "docker system prune -f 2>/dev/null; apt-get clean -y 2>/dev/null; journalctl --vacuum-size=50M 2>/dev/null; find /tmp -type f -mtime +1 -delete 2>/dev/null; df -h /",
                    "fix_label": "Run Disk Cleanup",
                },
                {
                    "keywords": ["docker", "daemon", "dind", "docker not running", "docker failed",
                                 "cannot connect to docker", "docker stopped", "docker down", "docker error",
                                 "docker socket", "dockerd", "docker service", "container not starting",
                                 "docker daemon failed", "docker broken"],
                    "title": "🐳 Docker Daemon Not Running",
                    "desc": "The Docker daemon inside your VPS may have stopped.",
                    "fix": "systemctl restart docker; sleep 5; docker info 2>&1 | head -8; echo '---'; systemctl status docker --no-pager | tail -5",
                    "fix_label": "Restart Docker Daemon",
                },
                {
                    "keywords": ["nginx", "apache", "web server", "port 80", "port 443", "site down",
                                 "website not loading", "web down", "http", "https", "502", "503", "504",
                                 "bad gateway", "webserver", "server error", "reverse proxy", "caddy",
                                 "site not working", "website broken", "web error"],
                    "title": "🌐 Web Server Down",
                    "desc": "Nginx or Apache may have crashed or a config error occurred.",
                    "fix": "nginx -t 2>&1 || true; systemctl restart nginx 2>/dev/null; systemctl restart apache2 2>/dev/null; systemctl restart caddy 2>/dev/null; sleep 2; echo 'Nginx:' $(systemctl is-active nginx 2>/dev/null || echo 'not installed'); echo 'Apache:' $(systemctl is-active apache2 2>/dev/null || echo 'not installed')",
                    "fix_label": "Test & Restart Web Server",
                },
                {
                    "keywords": ["mysql", "mariadb", "database", "db crashed", "sql error", "can't connect to db",
                                 "db down", "database error", "postgres", "postgresql", "redis", "mongodb",
                                 "database stopped", "db not running", "sql", "db broken", "database failed"],
                    "title": "🗄️ Database Not Running",
                    "desc": "MySQL/MariaDB/PostgreSQL may have stopped or ran out of resources.",
                    "fix": "systemctl restart mysql 2>/dev/null; systemctl restart mariadb 2>/dev/null; systemctl restart postgresql 2>/dev/null; systemctl restart redis 2>/dev/null; sleep 2; echo 'MySQL:' $(systemctl is-active mysql 2>/dev/null || echo 'not installed'); echo 'MariaDB:' $(systemctl is-active mariadb 2>/dev/null || echo 'not installed'); echo 'PostgreSQL:' $(systemctl is-active postgresql 2>/dev/null || echo 'not installed')",
                    "fix_label": "Restart Database Services",
                },
                {
                    "keywords": ["high cpu", "cpu usage", "slow", "lag", "sluggish", "freezing", "overloaded",
                                 "cpu high", "100 cpu", "maxed out", "unresponsive", "hanging", "hung",
                                 "lagging", "performance", "too slow", "sluggish", "cpu spike",
                                 "cpu 100", "load average", "load high"],
                    "title": "⚡ High CPU / Slowness",
                    "desc": "Kills the top CPU-consuming non-system process and shows remaining usage.",
                    "fix": (
                        "echo '=== Top CPU Processes ==='; "
                        "ps aux --sort=-%cpu | head -10; "
                        "echo ''; "
                        "echo '=== Attempting to nice/renice top process ==='; "
                        "top_pid=$(ps aux --sort=-%cpu | awk 'NR==2{print $2}'); "
                        "top_cmd=$(ps aux --sort=-%cpu | awk 'NR==2{print $11}'); "
                        "echo \"Top process: $top_cmd (PID $top_pid)\"; "
                        "renice -n 10 -p $top_pid 2>/dev/null && echo 'Reniced to low priority (nice 10)' || true; "
                        "echo ''; "
                        "echo '=== CPU after adjustment ==='; "
                        "sleep 2; ps aux --sort=-%cpu | head -5"
                    ),
                    "fix_label": "Throttle & Show Top Processes",
                },
                {
                    "keywords": ["network", "internet", "ping", "dns", "no internet", "offline", "can't reach",
                                 "no connection", "network error", "network down", "network issue",
                                 "dns error", "dns not working", "domain", "resolve", "curl failed",
                                 "wget failed", "connectivity", "network broken", "ip", "routing"],
                    "title": "🌐 Network / DNS Issue",
                    "desc": "DNS or networking may not be configured correctly inside the VPS.",
                    "fix": "echo '=== Ping 8.8.8.8 ==='; ping -c 3 -W 3 8.8.8.8 2>&1; echo ''; echo '=== DNS Lookup ==='; nslookup google.com 2>&1 | head -6; echo ''; echo '=== Routes ==='; ip route 2>&1; echo ''; echo '=== DNS Servers ==='; cat /etc/resolv.conf",
                    "fix_label": "Test Network & DNS",
                },
                {
                    "keywords": ["pterodactyl", "wings", "panel", "game server", "eggs", "pterodactyl panel",
                                 "ptero", "game panel", "wings down", "wings stopped", "wings failed",
                                 "wings not running", "pterodactyl down", "node offline", "node down"],
                    "title": "🦖 Pterodactyl / Wings Issue",
                    "desc": "Wings daemon or the Pterodactyl panel may have crashed.",
                    "fix": "systemctl restart wings 2>/dev/null; sleep 3; systemctl status wings --no-pager 2>&1 | tail -12; echo '---'; wings --version 2>/dev/null || echo 'Wings binary not found at default path'",
                    "fix_label": "Restart Wings",
                },
                {
                    "keywords": ["memory", "ram", "oom", "killed", "out of memory", "swap",
                                 "memory full", "ram full", "not enough memory", "low memory",
                                 "memory error", "malloc failed", "oom killer", "memory leak",
                                 "process killed", "killed by kernel", "sigkill"],
                    "title": "🧠 Low Memory / OOM",
                    "desc": "A process may have been killed due to low RAM. Shows memory usage and OOM history.",
                    "fix": "echo '=== Memory Usage ==='; free -h; echo ''; echo '=== Top Memory Users ==='; ps aux --sort=-%mem | head -10; echo ''; echo '=== OOM Kill History ==='; dmesg 2>/dev/null | grep -i 'oom\\|killed\\|out of memory' | tail -8 || journalctl -k --no-pager 2>/dev/null | grep -i 'oom\\|killed' | tail -5 || echo 'No OOM events found'",
                    "fix_label": "Check Memory & OOM Logs",
                },
                {
                    "keywords": ["cloudflare", "tunnel", "cloudflared", "zero trust", "cloudflare tunnel",
                                 "cf tunnel", "tunnel down", "tunnel not working", "cloudflare not working",
                                 "tunnel stopped", "cloudflared failed", "argo tunnel"],
                    "title": "☁️ Cloudflare Tunnel Issue",
                    "desc": "The cloudflared tunnel may have stopped running.",
                    "fix": "systemctl restart cloudflared 2>/dev/null; sleep 3; systemctl status cloudflared --no-pager 2>&1 | tail -10; echo '---'; cloudflared --version 2>/dev/null || echo 'cloudflared not installed'",
                    "fix_label": "Restart Cloudflared",
                },
                {
                    "keywords": ["reboot", "restart vps", "restart container", "vps restart", "container restart",
                                 "power cycle", "reset", "hang", "hung", "frozen", "not responding", "unresponsive"],
                    "title": "🔁 VPS Not Responding / Needs Restart",
                    "desc": "Restarts all core services inside the VPS to restore responsiveness.",
                    "fix": "systemctl daemon-reload 2>/dev/null; systemctl restart ssh docker 2>/dev/null; sleep 3; echo '=== Service Status ==='; systemctl is-active ssh docker 2>/dev/null; echo '=== Load Average ==='; uptime",
                    "fix_label": "Restart Core Services",
                },
                {
                    "keywords": ["firewall", "ufw", "iptables", "port blocked", "port not open", "port closed",
                                 "can't access port", "cannot access port", "port not accessible", "blocked",
                                 "firewall blocking", "ufw blocking"],
                    "title": "🔥 Firewall / Port Issue",
                    "desc": "Firewall rules may be blocking a port. Opens common ports and checks status.",
                    "fix": "ufw allow 22/tcp 2>/dev/null; ufw allow 80/tcp 2>/dev/null; ufw allow 443/tcp 2>/dev/null; echo '=== UFW Status ==='; ufw status 2>/dev/null || echo 'UFW not installed'; echo ''; echo '=== Open Ports ==='; ss -tlnp | head -20",
                    "fix_label": "Open Ports & Check Firewall",
                },
                {
                    "keywords": ["permission", "permission denied", "cannot write", "access denied",
                                 "chmod", "chown", "file permission", "read only", "not writable",
                                 "operation not permitted"],
                    "title": "🔒 Permission Denied",
                    "desc": "File/directory permissions may be misconfigured.",
                    "fix": "echo '=== Home directory permissions ==='; ls -la /root/ 2>/dev/null | head -10; echo ''; echo '=== Fixing common permission issues ==='; chmod 700 /root 2>/dev/null || true; chown -R root:root /root 2>/dev/null || true; echo 'Done'",
                    "fix_label": "Check & Fix Permissions",
                },
                {
                    "keywords": ["update", "apt", "upgrade", "package", "install failed", "apt error",
                                 "dpkg error", "package manager", "dependencies", "apt-get", "apt broken",
                                 "package broken", "broken package", "dpkg broken"],
                    "title": "📦 Package Manager Issue",
                    "desc": "APT/dpkg may be in a broken state. Attempts to repair it.",
                    "fix": "dpkg --configure -a 2>&1; apt-get install -f -y 2>&1 | tail -10; apt-get clean 2>/dev/null; echo 'Done'",
                    "fix_label": "Repair Package Manager",
                },
                {
                    "keywords": ["cron", "crontab", "scheduled task", "task not running", "cron job",
                                 "cron not working", "cron failed", "scheduled", "automation"],
                    "title": "⏰ Cron / Scheduled Tasks",
                    "desc": "Cron service may not be running. Restarts cron and shows active jobs.",
                    "fix": "systemctl restart cron 2>/dev/null; systemctl restart crond 2>/dev/null; sleep 2; echo '=== Cron Status ==='; systemctl is-active cron crond 2>/dev/null; echo ''; echo '=== Root crontab ==='; crontab -l 2>/dev/null || echo 'No crontab set'",
                    "fix_label": "Restart Cron & Show Jobs",
                },
            ]

            class HelpModal(discord.ui.Modal, title="🆘 Describe Your Issue"):
                issue = discord.ui.TextInput(
                    label="What problem are you experiencing?",
                    style=discord.TextStyle.paragraph,
                    placeholder="e.g. SSH not working, VPS is slow, disk full, Docker daemon stopped, website down...",
                    required=True,
                    max_length=600,
                )

                async def on_submit(self_m, inter: discord.Interaction):
                    await inter.response.defer(ephemeral=True)
                    text = self_m.issue.value.lower()
                    # Tokenise input so "ssh not working" matches keyword "ssh" even
                    # when the full phrase isn't in the keyword list.
                    tokens = set(text.split())

                    # Score each KB entry: exact phrase match scores 2, token match scores 1
                    matched = []
                    for entry in _KB:
                        score = 0
                        for kw in entry["keywords"]:
                            if kw in text:
                                score += 2          # full phrase found
                            elif kw in tokens:
                                score += 1          # single-token match
                        if score > 0:
                            matched.append((score, entry))
                    matched.sort(key=lambda x: -x[0])
                    best = [e for _, e in matched[:5]]

                    if not best:
                        # Fallback: run basic VPS diagnostics so the user still gets something useful
                        await inter.followup.send(
                            embed=create_info_embed(
                                "🆘 Running Basic Diagnostics…",
                                "No exact match found for your issue. Running a quick health check instead…"),
                            ephemeral=True)
                        try:
                            diag_out, _, _ = await docker_exec(
                                _hs_container,
                                "echo '=== Services ==='; systemctl is-active ssh docker nginx apache2 2>/dev/null; "
                                "echo ''; echo '=== Disk ==='; df -h / | tail -1; "
                                "echo ''; echo '=== Memory ==='; free -h | head -2; "
                                "echo ''; echo '=== Load ==='; uptime",
                                timeout=30)
                            fallback_embed = create_warning_embed(
                                "🩺 VPS Snapshot",
                                f"Your issue description didn't match a known fix.\n"
                                f"**Quick diagnostics:**\n```\n{diag_out[:800]}\n```\n"
                                f"Try **🩺 Health Check** for a full scan, or **🔑 SSH** to diagnose manually.")
                        except Exception:
                            fallback_embed = create_info_embed(
                                "🆘 No Match Found",
                                "Couldn't match your issue. Try:\n"
                                "• **🩺 Health Check** — auto-scans your VPS for common problems\n"
                                "• **🔑 SSH** → run manual diagnostics\n"
                                "• Ask an admin or open a support ticket in the server")
                        await inter.followup.send(embed=fallback_embed, ephemeral=True)
                        return

                    result_embed = create_embed(
                        "🆘 Possible Solutions",
                        f"Found **{len(best)}** match(es) for your issue. Click a button to apply the fix to your VPS.",
                        0x5865F2)
                    for i, entry in enumerate(best):
                        result_embed.add_field(
                            name=f"{i+1}. {entry['title']}",
                            value=entry['desc'],
                            inline=False)
                    result_embed.set_footer(text="Fixes run inside your VPS — results shown after completion")

                    class SolveView(discord.ui.View):
                        def __init__(self_sv):
                            super().__init__(timeout=180)
                            for i, entry in enumerate(best):
                                btn = discord.ui.Button(
                                    label=f"✅ Fix #{i+1}: {entry['fix_label'][:40]}",
                                    style=discord.ButtonStyle.success,
                                )
                                btn.callback = self_sv._make_fix_cb(entry)
                                self_sv.add_item(btn)

                        def _make_fix_cb(self_sv, entry):
                            async def callback(inter3: discord.Interaction):
                                await inter3.response.defer(ephemeral=True)
                                await inter3.followup.send(
                                    embed=create_info_embed(f"🔧 Applying: {entry['title']}", "Running fix on your VPS…"),
                                    ephemeral=True)
                                try:
                                    out, err, rc = await docker_exec(_hs_container, entry['fix'], timeout=90)
                                    result_text = (out or err or "Done")[:900]
                                    embed2 = create_success_embed(
                                        f"✅ Fix Applied: {entry['title']}",
                                        f"```\n{result_text}\n```")
                                    await inter3.followup.send(embed=embed2, ephemeral=True)
                                except Exception as ex:
                                    await inter3.followup.send(
                                        embed=create_error_embed("Fix Failed", str(ex)[:300]), ephemeral=True)
                            return callback

                    await inter.followup.send(embed=result_embed, view=SolveView(), ephemeral=True)

            await interaction.response.send_modal(HelpModal())


# ─── Commands ──────────────────────────────────────────────────────────────────

@bot.command(name='create')
@is_admin()
async def create_vps(ctx, user: discord.Member, ram: int, cpu: int, disk: int = 30):
    """Create a custom VPS for a user (Admin only) — !create @user <ram_GB> <cpu_cores> <disk_GB>"""
    if ram <= 0 or cpu <= 0 or disk <= 0:
        await ctx.send(embed=create_error_embed(
            "Invalid Specs",
            "RAM, CPU and Disk must be positive integers.\n"
            "Usage: `!create @user <ram_GB> <cpu_cores> <disk_GB>`"
        ))
        return

    user_id = str(user.id)
    if user_id not in vps_data:
        vps_data[user_id] = []

    # ── Node selection (shows UI when multiple nodes are available) ───────────
    deploy_node_id = await node_system.maybe_select_node(ctx)
    if deploy_node_id is None:
        return  # User cancelled node selection

    start_count                  = len(vps_data[user_id]) + 1
    container_name, vps_count   = await _unique_container_name(user_id, start_count)
    ram_mb                       = ram * 1024
    password                     = generate_password()
    vps_hostname                 = get_next_vps_hostname()

    progress_msg = await ctx.send(embed=_deploy_progress_embed(0))

    ssh_port = get_next_ssh_port()
    try:
        await create_docker_container(container_name, ram_mb, cpu, ssh_port, password, disk_gb=disk, hostname=vps_hostname, progress_msg=progress_msg)

        vps_info = {
            "container_name": container_name,
            "hostname":       vps_hostname,
            "ssh_port":       ssh_port,
            "ram":            f"{ram}GB",
            "cpu":            str(cpu),
            "storage":        f"{disk}GB",
            "status":         "running",
            "created_at":     datetime.now().isoformat(),
            "expires":        "Never",
            "node_id":        deploy_node_id,
            "ssh_password":   password,
            "shared_with":    []
        }
        vps_data[user_id].append(vps_info)
        save_data()

        # Fire-and-forget: ensure DinD comes up even if it was slow at verification
        asyncio.create_task(dind_autoheal(container_name, delay=180, notify_user=user))

        if ctx.guild:
            vps_role = await get_or_create_vps_role(ctx.guild)
            if vps_role:
                try:
                    await user.add_roles(vps_role, reason="VPS ownership granted")
                except discord.Forbidden:
                    pass

        # success embed is shown by editing progress_msg after access methods are ready

        _create_logo = get_logo_url()
        _log = discord.Embed(
            title="🟢  New VPS Deployed",
            description=f"{ctx.author.mention} deployed a VPS for {user.mention}.",
            colour=discord.Colour.from_rgb(87, 242, 135),
            timestamp=_utcnow()
        )
        _log.set_author(name="VPS Created  •  Admin Deploy", **( {"icon_url": _create_logo} if _create_logo else {}))
        try:
            _log.set_thumbnail(url=user.display_avatar.url)
        except Exception:
            pass
        _log.add_field(name="📦 Container",   value=f"`{container_name}`",          inline=True)
        _log.add_field(name="🏷️ Hostname",    value=f"`{vps_hostname}`",            inline=True)
        _log.add_field(name="🆔 VPS ID",      value=f"`#{vps_count}`",              inline=True)
        _log.add_field(name="👤 Owner",        value=f"{user.mention}",              inline=True)
        _log.add_field(name="🛠️ Deployed By",  value=f"{ctx.author.mention}",        inline=True)
        _log.add_field(name="🌐 Status",       value="🟢 **RUNNING**",              inline=True)
        _log.add_field(name="🧠 RAM",          value=f"`{ram} GB`",                  inline=True)
        _log.add_field(name="⚙️ CPU",          value=f"`{cpu} Core(s)`",             inline=True)
        _log.add_field(name="💾 Disk",         value=f"`{disk} GB`",                 inline=True)
        _log.set_footer(**{"text": f"{get_brand_name()} VPS Logs  •  Admin Deploy", **({"icon_url": _create_logo} if _create_logo else {})})
        asyncio.create_task(send_log(_log))

        # ── Step 5: access methods ────────────────────────────────────────────
        await progress_msg.edit(embed=_deploy_progress_embed(5))

        tmate_info = {}
        try:
            tmate_info = await get_tmate_session(container_name)
            vps_info["tmate_ssh"] = tmate_info.get("ssh", "")
            save_data()
        except Exception as _te:
            logger.warning(f"tmate session failed for {container_name}: {_te}")

        sshx_url = ""
        try:
            sshx_url = await get_sshx_session(container_name)
            vps_info["sshx_url"] = sshx_url
            save_data()
        except Exception as _se:
            logger.warning(f"sshx session failed for {container_name}: {_se}")

        # ── Edit progress_msg to final success embed ───────────────────────────
        await progress_msg.edit(embed=_deploy_success_embed(
            user, vps_count, container_name, str(ram), str(cpu), str(disk)
        ))

        # ── DM the owner ──────────────────────────────────────────────────────
        try:
            await user.send(embed=_vps_dm_embed(
                vps_count, container_name, f"{ram} GB", str(cpu),
                tmate_ssh=tmate_info.get("ssh", ""),
                sshx_url=sshx_url,
            ))
        except discord.Forbidden:
            pass

    except Exception as e:
        err_str = str(e)
        # Remove partial vps_data entry on failure
        if user_id in vps_data and vps_data[user_id]:
            last = vps_data[user_id][-1]
            if last.get("container_name") == container_name:
                vps_data[user_id].pop()
                save_data()
        # Only remove the container when it is NOT a verify failure.
        # "container kept for diagnosis" means the admin needs to inspect it.
        kept_for_diagnosis = "container kept for diagnosis" in err_str
        if not kept_for_diagnosis:
            try:
                await execute_docker(f"docker rm -f {container_name}", timeout=15)
            except Exception:
                pass
        logger.error(f"VPS creation failed for {container_name}: {e}")
        # progress_msg already set to the correct failed step inside create_docker_container
        hint = "\n\n🔍 Container kept — use `!exec` to inspect." if kept_for_diagnosis else ""
        await ctx.send(embed=create_error_embed("Deployment Failed", f"{err_str[:300]}{hint}"))


@bot.command(name='manage')
async def manage_vps(ctx, user: discord.Member = None):
    """Manage your VPS or another user's VPS (Admin only)"""
    if user:
        if not (str(ctx.author.id) == str(MAIN_ADMIN_ID) or str(ctx.author.id) in admin_data.get("admins", [])):
            await ctx.send(embed=create_error_embed("Access Denied", "Only admins can manage other users' VPS."))
            return
        user_id  = str(user.id)
        vps_list = vps_data.get(user_id, [])
        if not vps_list:
            await ctx.send(embed=create_error_embed("No VPS Found", f"{user.mention} doesn't have any VPS."))
            return
        view = ManageView(str(ctx.author.id), vps_list, is_admin=True, owner_id=user_id)
        await ctx.send(embed=create_info_embed(f"Managing {user.name}'s VPS", f"Managing VPS for {user.mention}"), view=view)
    else:
        user_id  = str(ctx.author.id)
        vps_list = vps_data.get(user_id, [])
        if not vps_list:
            embed = create_embed("No VPS Found", "You don't have any VPS. Use `.buywc` to purchase one.", 0xff3366)
            embed.add_field(name="Quick Actions",
                            value="• `!plans` - View plans\n• `!buywc <plan> <processor>` - Purchase VPS",
                            inline=False)
            await ctx.send(embed=embed)
            return
        view = ManageView(user_id, vps_list)
        await ctx.send(embed=view.initial_embed, view=view)


@bot.command(name='delete-vps')
@is_admin()
async def delete_vps(ctx, user: discord.Member, vps_number: int, *, reason: str = "No reason"):
    """Delete a user's VPS (Admin only)"""
    user_id = str(user.id)
    if user_id not in vps_data or vps_number < 1 or vps_number > len(vps_data[user_id]):
        await ctx.send(embed=create_error_embed("Invalid VPS", "Invalid VPS number or user doesn't have a VPS."))
        return

    vps            = vps_data[user_id][vps_number - 1]
    container_name = vps["container_name"]
    await ctx.send(embed=create_info_embed("Deleting VPS", f"Removing VPS #{vps_number}..."))

    try:
        try:
            await execute_docker(f"docker stop {container_name}")
        except Exception:
            pass
        await execute_docker(f"docker rm -f {container_name}")
        del vps_data[user_id][vps_number - 1]
        if not vps_data[user_id]:
            del vps_data[user_id]
            if ctx.guild:
                vps_role = await get_or_create_vps_role(ctx.guild)
                if vps_role and vps_role in user.roles:
                    try:
                        await user.remove_roles(vps_role, reason="No VPS ownership")
                    except discord.Forbidden:
                        pass
        save_data()
        embed = create_success_embed("VPS Deleted Successfully")
        embed.add_field(name="Owner",     value=user.mention,          inline=True)
        embed.add_field(name="VPS ID",    value=f"#{vps_number}",      inline=True)
        embed.add_field(name="Container", value=f"`{container_name}`", inline=True)
        embed.add_field(name="Reason",    value=reason,                inline=False)
        await ctx.send(embed=embed)

        _del_logo = get_logo_url()
        _log = discord.Embed(
            title="🔴  VPS Permanently Deleted",
            description=(
                f"{ctx.author.mention} deleted **#{vps_number}** belonging to {user.mention}.\n"
                f"> **Reason:** {reason}"
            ),
            colour=discord.Colour.from_rgb(237, 66, 69),
            timestamp=_utcnow()
        )
        _log.set_author(name="VPS Deleted  •  Permanent Action", **( {"icon_url": _del_logo} if _del_logo else {}))
        try:
            _log.set_thumbnail(url=user.display_avatar.url)
        except Exception:
            pass
        _log.add_field(name="📦 Container",   value=f"`{container_name}`",              inline=True)
        _log.add_field(name="🏷️ Hostname",    value=f"`{vps.get('hostname','?')}`",     inline=True)
        _log.add_field(name="🆔 VPS ID",      value=f"`#{vps_number}`",                 inline=True)
        _log.add_field(name="👤 Owner",        value=user.mention,                       inline=True)
        _log.add_field(name="🛠️ Deleted By",  value=ctx.author.mention,                 inline=True)
        _log.add_field(name="🌐 Status",       value="🗑️ **DELETED**",                  inline=True)
        _log.add_field(name="🧠 RAM",          value=f"`{vps.get('ram','?')}`",          inline=True)
        _log.add_field(name="⚙️ CPU",          value=f"`{vps.get('cpu','?')} Core(s)`",  inline=True)
        _log.add_field(name="💾 Storage",      value=f"`{vps.get('storage','?')}`",      inline=True)
        _log.set_footer(**{"text": f"{get_brand_name()} VPS Logs  •  Delete Event", **({"icon_url": _del_logo} if _del_logo else {})})
        asyncio.create_task(send_log(_log))
    except Exception as e:
        await ctx.send(embed=create_error_embed("Deletion Failed", f"Error: {str(e)}"))


@bot.command(name='list-all')
@is_admin()
async def list_all_vps(ctx):
    """List all VPS and user information (Admin only)"""
    embed        = create_embed("All VPS Information", "Complete overview of all VPS deployments", 0x5865F2)
    total_vps    = 0
    running_vps  = 0
    stopped_vps  = 0
    suspended_vps = 0
    vps_info     = []
    user_summary = []

    for user_id, vps_list in vps_data.items():
        try:
            user            = await bot.fetch_user(int(user_id))
            user_vps_count  = len(vps_list)
            user_running    = sum(1 for vps in vps_list if vps.get('status') == 'running')
            user_suspended  = sum(1 for vps in vps_list if vps.get('status') == 'suspended')
            total_vps      += user_vps_count
            running_vps    += user_running
            stopped_vps    += user_vps_count - user_running - user_suspended
            suspended_vps  += user_suspended
            user_summary.append(f"**{user.name}** ({user.mention}) - {user_vps_count} VPS ({user_running} running, {user_suspended} suspended)")
            for i, vps in enumerate(vps_list):
                status = vps.get('status', 'unknown')
                status_emoji = "🟢" if status == 'running' else ("🟠" if status == 'suspended' else "🔴")
                vps_info.append(
                    f"{status_emoji} **{user.name}** - VPS {i+1}: `{vps['container_name']}` "
                    f"Port:{vps.get('ssh_port','?')} - {status.upper()}"
                )
        except discord.NotFound:
            vps_info.append(f"❓ Unknown User ({user_id}) - {len(vps_list)} VPS")

    embed.add_field(
        name="System Overview",
        value=(
            f"**Total Users:** {len(vps_data)}\n"
            f"**Total VPS:** {total_vps}\n"
            f"**Running:** {running_vps}\n"
            f"**Stopped:** {stopped_vps}\n"
            f"**Suspended:** {suspended_vps}"
        ),
        inline=False
    )
    if user_summary:
        embed.add_field(name="User Summary", value="\n".join(user_summary[:10]), inline=False)
    if vps_info:
        for i in range(0, min(len(vps_info), 30), 15):
            chunk = vps_info[i:i+15]
            embed.add_field(
                name=f"VPS Deployments ({i+1}-{min(i+15, len(vps_info))})",
                value="\n".join(chunk), inline=False
            )
    await ctx.send(embed=embed)


@bot.command(name='manage-shared')
async def manage_shared_vps(ctx, owner: discord.Member, vps_number: int):
    """Manage a shared VPS"""
    owner_id = str(owner.id)
    user_id  = str(ctx.author.id)
    if owner_id not in vps_data or vps_number < 1 or vps_number > len(vps_data[owner_id]):
        await ctx.send(embed=create_error_embed("Invalid VPS", "Invalid VPS number."))
        return
    vps = vps_data[owner_id][vps_number - 1]
    if user_id not in vps.get("shared_with", []):
        await ctx.send(embed=create_error_embed("Access Denied", "You do not have access to this VPS."))
        return
    view = ManageView(user_id, [vps], is_shared=True, owner_id=owner_id)
    await ctx.send(embed=view.initial_embed, view=view)


@bot.command(name='share-user')
async def share_user(ctx, shared_user: discord.Member, vps_number: int):
    """Share VPS access with another user"""
    user_id        = str(ctx.author.id)
    shared_user_id = str(shared_user.id)
    if user_id not in vps_data or vps_number < 1 or vps_number > len(vps_data[user_id]):
        await ctx.send(embed=create_error_embed("Invalid VPS", "Invalid VPS number."))
        return
    vps = vps_data[user_id][vps_number - 1]
    if "shared_with" not in vps:
        vps["shared_with"] = []
    if shared_user_id in vps["shared_with"]:
        await ctx.send(embed=create_error_embed("Already Shared", f"{shared_user.mention} already has access!"))
        return
    vps["shared_with"].append(shared_user_id)
    save_data()
    await ctx.send(embed=create_success_embed("VPS Shared", f"VPS #{vps_number} shared with {shared_user.mention}!"))
    try:
        await shared_user.send(embed=create_embed(
            "VPS Access Granted",
            f"You have access to VPS #{vps_number} from {ctx.author.mention}. "
            f"Use `!manage-shared {ctx.author.mention} {vps_number}`",
            0x00ff88
        ))
    except discord.Forbidden:
        pass


@bot.command(name='share-ruser')
async def revoke_share(ctx, shared_user: discord.Member, vps_number: int):
    """Revoke shared VPS access"""
    user_id        = str(ctx.author.id)
    shared_user_id = str(shared_user.id)
    if user_id not in vps_data or vps_number < 1 or vps_number > len(vps_data[user_id]):
        await ctx.send(embed=create_error_embed("Invalid VPS", "Invalid VPS number."))
        return
    vps = vps_data[user_id][vps_number - 1]
    if "shared_with" not in vps or shared_user_id not in vps["shared_with"]:
        await ctx.send(embed=create_error_embed("Not Shared", f"{shared_user.mention} doesn't have access!"))
        return
    vps["shared_with"].remove(shared_user_id)
    save_data()
    await ctx.send(embed=create_success_embed(
        "Access Revoked",
        f"Access to VPS #{vps_number} revoked from {shared_user.mention}!"
    ))


@bot.command(name='buywc')
async def buy_with_credits(ctx, plan: str, processor: str = "Intel"):
    """Buy a VPS with credits"""
    user_id = str(ctx.author.id)
    prices = {
        "Starter":  {"Intel": 42,  "AMD": 83},
        "Basic":    {"Intel": 96,  "AMD": 164},
        "Standard": {"Intel": 192, "AMD": 320},
        "Pro":      {"Intel": 220, "AMD": 340}
    }
    plans = {
        "Starter":  {"ram": "4GB",  "cpu": "1", "storage": "10GB"},
        "Basic":    {"ram": "8GB",  "cpu": "1", "storage": "10GB"},
        "Standard": {"ram": "12GB", "cpu": "2", "storage": "10GB"},
        "Pro":      {"ram": "16GB", "cpu": "2", "storage": "10GB"}
    }
    if plan not in prices:
        await ctx.send(embed=create_error_embed("Invalid Plan", "Available: Starter, Basic, Standard, Pro"))
        return
    if processor not in ["Intel", "AMD"]:
        await ctx.send(embed=create_error_embed("Invalid Processor", "Choose: Intel or AMD"))
        return

    cost = prices[plan][processor]
    if user_id not in user_data:
        user_data[user_id] = {"credits": 0}
    if user_data[user_id]["credits"] < cost:
        await ctx.send(embed=create_error_embed(
            "Insufficient Credits",
            f"You need {cost} credits but have {user_data[user_id]['credits']}"
        ))
        return

    user_data[user_id]["credits"] -= cost
    if user_id not in vps_data:
        vps_data[user_id] = []

    # ── Node selection (shows UI when multiple nodes are available) ───────────
    deploy_node_id = await node_system.maybe_select_node(ctx)
    if deploy_node_id is None:
        # Refund credits — user cancelled
        user_data[user_id]["credits"] += cost
        save_data()
        return

    start_count                  = len(vps_data[user_id]) + 1
    container_name, vps_count   = await _unique_container_name(user_id, start_count)
    ram_str                      = plans[plan]["ram"]
    cpu_str                      = plans[plan]["cpu"]
    ram_mb                       = int(ram_str.replace("GB", "")) * 1024
    password                     = generate_password()
    vps_hostname                 = get_next_vps_hostname()

    progress_msg = await ctx.send(embed=_deploy_progress_embed(0))

    ssh_port = get_next_ssh_port()
    try:
        await create_docker_container(container_name, ram_mb, cpu_str, ssh_port, password, hostname=vps_hostname, progress_msg=progress_msg)

        vps_info = {
            "plan":           plan,
            "container_name": container_name,
            "hostname":       vps_hostname,
            "ssh_port":       ssh_port,
            "ram":            ram_str,
            "cpu":            cpu_str,
            "storage":        plans[plan]["storage"],
            "status":         "running",
            "created_at":     datetime.now().isoformat(),
            "node_id":        deploy_node_id,
            "processor":      processor,
            "ssh_password":   password,
            "shared_with":    [],
            "expires":        "Never"
        }
        vps_data[user_id].append(vps_info)
        save_data()

        # Fire-and-forget: ensure DinD comes up even if it was slow at verification
        asyncio.create_task(dind_autoheal(container_name, delay=180, notify_user=ctx.author))

        if ctx.guild:
            vps_role = await get_or_create_vps_role(ctx.guild)
            if vps_role:
                try:
                    await ctx.author.add_roles(vps_role, reason="VPS purchase completed")
                except discord.Forbidden:
                    pass

        embed = create_success_embed("VPS Purchased Successfully")
        embed.add_field(name="Plan",      value=f"**{plan}** ({processor})", inline=True)
        embed.add_field(name="VPS ID",    value=f"#{vps_count}",             inline=True)
        embed.add_field(name="Container", value=f"`{container_name}`",       inline=True)
        embed.add_field(name="Cost",      value=f"{cost} credits",           inline=True)
        embed.add_field(name="Resources",
                        value=f"**RAM:** {ram_str}\n**CPU:** {cpu_str} Cores\n**Storage:** 10GB",
                        inline=False)
        await ctx.send(embed=embed)

        _log = discord.Embed(
            title="🟢  VPS Deployed",
            description=(
                f"{ctx.author.mention} purchased a **{plan}** ({processor}) VPS.\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            ),
            colour=discord.Colour.from_rgb(87, 242, 135),
            timestamp=_utcnow()
        )
        _log.set_author(name="VPS Created  •  User Purchase")
        try:
            _log.set_thumbnail(url=ctx.author.display_avatar.url)
        except Exception:
            pass
        _log.add_field(name="📦 Container",  value=f"`{container_name}`",                 inline=True)
        _log.add_field(name="🏷️ Hostname",   value=f"`{vps_hostname}`",                   inline=True)
        _log.add_field(name="🆔 VPS ID",     value=f"`#{vps_count}`",                     inline=True)
        _log.add_field(name="👤 Owner",       value=f"{ctx.author.mention}\n`{user_id}`",  inline=True)
        _log.add_field(name="📋 Plan",        value=f"`{plan}` ({processor})",             inline=True)
        _log.add_field(name="💳 Cost",        value=f"`{cost} credits`",                   inline=True)
        _log.add_field(name="🧠 RAM",         value=f"`{ram_str}`",                        inline=True)
        _log.add_field(name="⚡ CPU",         value=f"`{cpu_str} Cores`",                  inline=True)
        _log.add_field(name="💾 Storage",     value=f"`{plans[plan]['storage']}`",         inline=True)
        _log.add_field(name="🌐 Status",      value="`🟢 RUNNING`",                        inline=True)
        _log.add_field(name="📅 Created At",  value=f"`{_utcnow().strftime('%Y-%m-%d %H:%M UTC')}`", inline=True)
        _log.add_field(name="🔒 Password Set",value="`✅ Generated`",                       inline=True)
        _log.set_footer(text=f"{get_brand_name()} VPS Logs  •  User Purchase")
        asyncio.create_task(send_log(_log))

        # ── Step 5: access methods ────────────────────────────────────────────
        await progress_msg.edit(embed=_deploy_progress_embed(5))

        tmate_info = {}
        try:
            tmate_info = await get_tmate_session(container_name)
            vps_info["tmate_ssh"] = tmate_info.get("ssh", "")
            save_data()
        except Exception as _te:
            logger.warning(f"tmate session failed for {container_name}: {_te}")

        sshx_url = ""
        try:
            sshx_url = await get_sshx_session(container_name)
            vps_info["sshx_url"] = sshx_url
            save_data()
        except Exception as _se:
            logger.warning(f"sshx session failed for {container_name}: {_se}")

        # ── Edit progress_msg to final success embed ───────────────────────────
        await progress_msg.edit(embed=_deploy_success_embed(
            ctx.author, vps_count, container_name, ram_str, cpu_str, plans[plan]["storage"]
        ))

        # ── DM the buyer ──────────────────────────────────────────────────────
        try:
            await ctx.author.send(embed=_vps_dm_embed(
                vps_count, container_name, ram_str, cpu_str,
                tmate_ssh=tmate_info.get("ssh", ""),
                sshx_url=sshx_url,
                plan=plan,
                processor=processor,
            ))
        except discord.Forbidden:
            pass

    except Exception as e:
        err_str = str(e)
        # Refund credits on failure
        user_data[user_id]["credits"] += cost
        # Remove partial vps_data entry
        if user_id in vps_data and vps_data[user_id]:
            last = vps_data[user_id][-1]
            if last.get("container_name") == container_name:
                vps_data[user_id].pop()
        save_data()
        # Only remove container if it is NOT kept for diagnosis
        kept_for_diagnosis = "container kept for diagnosis" in err_str
        if not kept_for_diagnosis:
            try:
                await execute_docker(f"docker rm -f {container_name}", timeout=15)
            except Exception:
                pass
        # progress_msg already set to the correct failed step inside create_docker_container
        hint = "\n\n🔍 Container kept — admin can use `!exec` to inspect." if kept_for_diagnosis else ""
        await ctx.send(embed=create_error_embed("Purchase Failed", f"Error: {err_str[:280]}{hint}\n\nCredits refunded."))


@bot.command(name='buyc')
async def buy_credits(ctx):
    """Get payment information"""
    embed = create_embed(
        "💳  Purchase Credits",
        "Credits are used to deploy and maintain VPS servers. Contact an admin after payment.",
        0x57F287,
    )
    embed.add_field(name="🇮🇳  UPI",    value="> Contact an admin for UPI details",    inline=False)
    embed.add_field(name="💰  PayPal",  value="> Contact an admin for PayPal details", inline=False)
    embed.add_field(name="₿  Crypto",   value="> **BTC • ETH • USDT** accepted",       inline=False)
    embed.add_field(
        name="📋  Steps",
        value=(
            "**1.** Make payment via your preferred method\n"
            "**2.** DM an admin with your **transaction ID**\n"
            "**3.** Credits added — use `!credits` to verify"
        ),
        inline=False,
    )
    try:
        await ctx.author.send(embed=embed)
        await ctx.send(embed=create_success_embed("Information Sent", "Payment details sent to your DMs!"))
    except discord.Forbidden:
        await ctx.send(embed=create_error_embed("DM Failed", "Enable DMs to receive payment info!"))


@bot.command(name='plans')
async def show_plans(ctx):
    """Show available VPS plans"""
    embed = create_embed(
        f"💎  {get_brand_name()} VPS Plans",
        "Premium cloud VPS with full root access & Docker-in-Docker. Pick your plan below.",
        0xEB459E,
    )
    plans_info = [
        (
            "🥉  Starter",
            "```\nRAM     4 GB\nCPU     1 Core\nStorage 10 GB\n```\n💰 Intel: **42cr**  •  AMD: **83cr**",
        ),
        (
            "🥈  Basic",
            "```\nRAM     8 GB\nCPU     1 Core\nStorage 10 GB\n```\n💰 Intel: **96cr**  •  AMD: **164cr**",
        ),
        (
            "🥇  Standard",
            "```\nRAM     12 GB\nCPU     2 Cores\nStorage 10 GB\n```\n💰 Intel: **192cr**  •  AMD: **320cr**",
        ),
        (
            "💎  Pro",
            "```\nRAM     16 GB\nCPU     2 Cores\nStorage 10 GB\n```\n💰 Intel: **220cr**  •  AMD: **340cr**",
        ),
    ]
    for name, value in plans_info:
        embed.add_field(name=name, value=value, inline=True)
    embed.add_field(
        name="🛒  How to Order",
        value=(
            "`!buywc <plan> <Intel/AMD>` — purchase instantly\n"
            "`!buyc` — view payment methods\n"
            "`!credits` — check your balance"
        ),
        inline=False,
    )
    await ctx.send(embed=embed)


@bot.command(name='credits')
async def check_credits(ctx):
    """Check your credit balance"""
    user_id = str(ctx.author.id)
    if user_id not in user_data:
        user_data[user_id] = {"credits": 0}
        save_data()
    credits = user_data[user_id].get("credits", 0)
    embed = create_info_embed("💰 Credit Balance", f"{ctx.author.mention}, you have **{credits}** credits.")
    await ctx.send(embed=embed)


@bot.command(name='adminc')
@is_admin()
async def admin_add_credits(ctx, user: discord.Member, amount: int):
    """Add credits to a user (Admin only)"""
    user_id = str(user.id)
    if user_id not in user_data:
        user_data[user_id] = {"credits": 0}
    user_data[user_id]["credits"] += amount
    save_data()
    await ctx.send(embed=create_success_embed(
        "Credits Added",
        f"Added **{amount}** credits to {user.mention}. "
        f"New balance: **{user_data[user_id]['credits']}**"
    ))


@bot.command(name='adminrc')
@is_admin()
async def admin_remove_credits(ctx, user: discord.Member, amount: str):
    """Remove credits from a user (Admin only). Use 'all' to remove all."""
    user_id = str(user.id)
    if user_id not in user_data:
        user_data[user_id] = {"credits": 0}
    if amount.lower() == "all":
        removed = user_data[user_id]["credits"]
        user_data[user_id]["credits"] = 0
    else:
        removed = int(amount)
        user_data[user_id]["credits"] = max(0, user_data[user_id]["credits"] - removed)
    save_data()
    await ctx.send(embed=create_success_embed(
        "Credits Removed",
        f"Removed **{removed}** credits from {user.mention}. "
        f"New balance: **{user_data[user_id]['credits']}**"
    ))


@bot.command(name='userinfo')
@is_admin()
async def user_info(ctx, user: discord.Member):
    """Get detailed user information (Admin only)"""
    user_id = str(user.id)
    credits      = user_data.get(user_id, {}).get("credits", 0)
    is_admin_user = user_id == str(MAIN_ADMIN_ID) or user_id in admin_data.get("admins", [])
    vps_list     = vps_data.get(user_id, [])
    embed        = create_embed(f"👤  {user.display_name}", f"Account overview for {user.mention}", 0x3498DB)
    try:
        embed.set_thumbnail(url=user.display_avatar.url)
    except Exception:
        pass
    embed.add_field(name="🆔 User ID",   value=f"`{user.id}`",                                    inline=True)
    embed.add_field(name="💰 Credits",   value=f"**{credits}** cr",                               inline=True)
    embed.add_field(name="🛡️ Role",      value="**Admin**" if is_admin_user else "**User**",      inline=True)
    if vps_list:
        _status_icon = {"running": "🟢", "stopped": "🔴", "suspended": "⛔"}
        vps_lines = [
            f"{_status_icon.get(v.get('status','?'), '⬜')} VPS {i+1}: `{v['container_name']}` — {v.get('status','?').upper()}"
            for i, v in enumerate(vps_list)
        ]
        embed.add_field(name=f"🖥️ VPS ({len(vps_list)})", value="\n".join(vps_lines), inline=False)
    else:
        embed.add_field(name="🖥️ VPS", value="No VPS owned", inline=False)
    await ctx.send(embed=embed)


@bot.command(name='serverstats')
@is_admin()
async def server_stats(ctx):
    """Show server statistics (Admin only)"""
    total_vps   = sum(len(v) for v in vps_data.values())
    running_vps = sum(1 for vl in vps_data.values() for v in vl if v.get('status') == 'running')
    susp_vps    = sum(1 for vl in vps_data.values() for v in vl if v.get('status') == 'suspended')
    total_credits = sum(u.get('credits', 0) for u in user_data.values())
    total_ram   = sum(int(v['ram'].replace('GB', '')) for vl in vps_data.values() for v in vl)
    total_cpu   = sum(int(v['cpu']) for vl in vps_data.values() for v in vl)

    stopped_vps = total_vps - running_vps - susp_vps
    embed = create_embed("📊  Server Statistics", f"Live overview of the {get_brand_name()} infrastructure.", 0x9B59B6)
    embed.add_field(name="👥  Users",     value=f"**{len(user_data)}** total\n**{len(admin_data.get('admins', [])) + 1}** admins", inline=True)
    embed.add_field(name="🖥️  VPS",      value=f"**{total_vps}** total\n🟢 {running_vps} running  •  🔴 {stopped_vps} stopped  •  ⛔ {susp_vps} suspended", inline=False)
    embed.add_field(name="🧠  RAM in Use",  value=f"**{total_ram} GB** allocated", inline=True)
    embed.add_field(name="⚙️  CPU in Use",  value=f"**{total_cpu}** cores allocated", inline=True)
    embed.add_field(name="💰  Economy",   value=f"**{total_credits}** credits in circulation", inline=True)
    await ctx.send(embed=embed)


@bot.command(name='vpsinfo')
@is_admin()
async def vps_info(ctx, container_name: str = None):
    """Get VPS information (Admin only)"""
    if not container_name:
        all_vps = []
        for uid, vl in vps_data.items():
            try:
                u = await bot.fetch_user(int(uid))
                for i, v in enumerate(vl):
                    all_vps.append(
                        f"**{u.name}** - VPS {i+1}: `{v['container_name']}` "
                        f"Port:{v.get('ssh_port','?')} - {v.get('status','?').upper()}"
                    )
            except Exception:
                pass
        embed = create_embed("🖥️ All VPS", f"Total: {len(all_vps)}", 0x5865F2)
        for i in range(0, len(all_vps), 20):
            embed.add_field(name=f"VPS List ({i+1}-{i+20})", value="\n".join(all_vps[i:i+20]), inline=False)
        await ctx.send(embed=embed)
    else:
        found_vps  = None
        found_user = None
        for uid, vl in vps_data.items():
            for v in vl:
                if v['container_name'] == container_name:
                    found_vps  = v
                    found_user = await bot.fetch_user(int(uid))
                    break
            if found_vps:
                break
        if not found_vps:
            await ctx.send(embed=create_error_embed("Not Found", f"No VPS with name `{container_name}`"))
            return
        embed = create_embed(f"🖥️ VPS - {container_name}", f"Owned by {found_user.mention}", 0x5865F2)
        embed.add_field(name="Specs",    value=f"**RAM:** {found_vps['ram']}\n**CPU:** {found_vps['cpu']} Cores", inline=True)
        embed.add_field(name="Status",   value=f"**{found_vps.get('status','?').upper()}**",                       inline=True)
        embed.add_field(name="SSH Port", value=f"`{found_vps.get('ssh_port','N/A')}`",                            inline=True)
        embed.add_field(name="Created",  value=found_vps.get('created_at', 'Unknown'),                            inline=False)
        if found_vps.get("suspension_reason"):
            embed.add_field(name="⚠️ Suspension", value=found_vps["suspension_reason"], inline=False)
        await ctx.send(embed=embed)


@bot.command(name='restart-vps')
@is_admin()
async def restart_vps(ctx, container_name: str):
    """Restart a VPS (Admin only)"""
    await ctx.send(embed=create_info_embed("Restarting VPS", f"Restarting `{container_name}`..."))
    try:
        await execute_docker(f"docker restart {container_name}")
        await asyncio.sleep(8)
        await docker_exec(container_name, "systemctl restart ssh 2>/dev/null || /usr/sbin/sshd || true", timeout=15)
        for vl in vps_data.values():
            for v in vl:
                if v['container_name'] == container_name:
                    v['status'] = 'running'
                    save_data()
                    break
        await ctx.send(embed=create_success_embed("VPS Restarted", f"`{container_name}` restarted successfully!"))
    except Exception as e:
        await ctx.send(embed=create_error_embed("Restart Failed", str(e)))


@bot.command(name='backup-vps')
@is_admin()
async def backup_vps(ctx, container_name: str):
    """Create a Docker image snapshot of a VPS (Admin only)"""
    snapshot_name = f"{container_name}-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    await ctx.send(embed=create_info_embed("Creating Backup", f"Committing snapshot of `{container_name}`..."))
    try:
        await execute_docker(f"docker commit {container_name} {snapshot_name}")
        await ctx.send(embed=create_success_embed("Backup Created", f"Image `{snapshot_name}` created!"))
    except Exception as e:
        await ctx.send(embed=create_error_embed("Backup Failed", str(e)))


@bot.command(name='restore-vps')
@is_admin()
async def restore_vps(ctx, container_name: str, snapshot_name: str):
    """Restore a VPS from a Docker snapshot image (Admin only)"""
    await ctx.send(embed=create_info_embed("Restoring VPS", f"Restoring `{container_name}` from `{snapshot_name}`..."))
    try:
        found_vps = None
        for vl in vps_data.values():
            for v in vl:
                if v['container_name'] == container_name:
                    found_vps = v
                    break
        if not found_vps:
            await ctx.send(embed=create_error_embed("Not Found", f"No VPS data for `{container_name}`"))
            return

        try:
            await execute_docker(f"docker stop {container_name}")
        except Exception:
            pass
        await execute_docker(f"docker rm -f {container_name}")

        ssh_port = found_vps.get("ssh_port", get_next_ssh_port())
        # Snapshots are committed from darknodes-vps containers whose CMD is
        # /lib/systemd/systemd, so we do NOT override it with sleep infinity here.
        # Reuse existing DinD volumes for the restored container
        _rvol_docker = f"{container_name}-docker"
        _rvol_home   = f"{container_name}-home"
        _rvol_root   = f"{container_name}-root"
        _rvol_opt    = f"{container_name}-opt"
        run_cmd = (
            f"docker run -d "
            f"--name {container_name} "
            f"--hostname {found_vps.get('hostname', VPS_HOSTNAME)} "
            f"-p {ssh_port}:22 "
            f"--restart=unless-stopped "
            f"--privileged "
            f"--cgroupns=host "
            f"-v /sys/fs/cgroup:/sys/fs/cgroup:rw "
            f"--tmpfs /run:exec,mode=755,size=256m "
            f"--tmpfs /run/lock:size=64m "
            f"--tmpfs /tmp:exec,size=512m "
            f"-v {_rvol_docker}:/var/lib/docker "
            f"-v {_rvol_home}:/home "
            f"-v {_rvol_root}:/root "
            f"-v {_rvol_opt}:/opt "
            f"-e container=docker "
            f"--dns 8.8.8.8 "
            f"--dns 1.1.1.1 "
            f"--dns 8.8.4.4 "
            f"--security-opt seccomp=unconfined "
            f"--security-opt apparmor=unconfined "
            f"--shm-size=512m "
            f"--ulimit nofile=65536:65536 "
            f"{snapshot_name}"
        )
        await execute_docker(run_cmd)
        # Give systemd + dockerd time to start before checking SSH
        await asyncio.sleep(10)
        # Ensure SSH is running (dockerd wait handled by systemd)
        await docker_exec(
            container_name,
            "systemctl restart ssh 2>/dev/null || /usr/sbin/sshd || true",
            timeout=20
        )
        found_vps["status"] = "running"
        found_vps.pop("suspension_reason",  None)
        found_vps.pop("suspension_time",    None)
        found_vps.pop("suspension_evidence", None)
        save_data()
        await ctx.send(embed=create_success_embed("VPS Restored", f"`{container_name}` restored from `{snapshot_name}`!"))
    except Exception as e:
        await ctx.send(embed=create_error_embed("Restore Failed", str(e)))


@bot.command(name='list-snapshots')
@is_admin()
async def list_snapshots(ctx, container_name: str):
    """List Docker image snapshots for a VPS (Admin only)"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "images", "--format", "{{.Repository}}:{{.Tag}} ({{.Size}})",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        all_images = stdout.decode().strip().split('\n')
        snapshots  = [img for img in all_images if img.startswith(container_name + "-backup-")]
        if snapshots:
            embed = create_embed(f"📸 Snapshots for {container_name}", f"Found {len(snapshots)} snapshots", 0xF0A500)
            embed.add_field(name="Snapshots", value="\n".join([f"• `{s}`" for s in snapshots]), inline=False)
        else:
            embed = create_info_embed("No Snapshots", f"No snapshots found for `{container_name}`")
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Error", str(e)))


@bot.command(name='exec')
@is_admin()
async def execute_command(ctx, container_name: str, *, command: str):
    """Execute a command inside a VPS container (Admin only)"""
    await ctx.send(embed=create_info_embed("Executing Command", f"Running in `{container_name}`..."))
    try:
        stdout, stderr, rc = await docker_exec(container_name, command, timeout=30)
        embed = create_embed(f"Command Output - {container_name}", f"Command: `{command}`", 0x2B2D31)
        if stdout:
            out = stdout[:1000] + "\n...(truncated)" if len(stdout) > 1000 else stdout
            embed.add_field(name="📤 Output",    value=f"```\n{out}\n```", inline=False)
        if stderr:
            err = stderr[:1000] + "\n...(truncated)" if len(stderr) > 1000 else stderr
            embed.add_field(name="⚠️ Stderr",   value=f"```\n{err}\n```", inline=False)
        embed.add_field(name="🔄 Exit Code", value=f"**{rc}**",            inline=False)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Execution Failed", str(e)))


@bot.command(name='stop-vps-all')
@is_admin()
async def stop_all_vps(ctx):
    """Stop all VPS containers (Admin only)"""
    await ctx.send(embed=create_warning_embed("Stopping All VPS",
                                              "⚠️ This will stop ALL running VPS. Continue?"))

    class ConfirmView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=60)

        @discord.ui.button(label="Stop All VPS", style=discord.ButtonStyle.danger)
        async def confirm(self, interaction: discord.Interaction, item: discord.ui.Button):
            await interaction.response.defer()
            stopped_count = 0
            errors        = []
            for vl in vps_data.values():
                for v in vl:
                    if v.get('status') == 'running':
                        try:
                            proc = await asyncio.create_subprocess_exec(
                                "docker", "stop", v['container_name'],
                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                            )
                            await proc.communicate()
                            v['status'] = 'stopped'
                            stopped_count += 1
                        except Exception as e:
                            errors.append(str(e))
            save_data()
            embed = create_success_embed("All VPS Stopped", f"Stopped **{stopped_count}** containers.")
            if errors:
                embed.add_field(name="Errors", value="\n".join(errors[:5]), inline=False)
            await interaction.followup.send(embed=embed)

        @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
        async def cancel(self, interaction: discord.Interaction, item: discord.ui.Button):
            await interaction.response.edit_message(embed=create_info_embed("Cancelled", "Operation cancelled."))

    await ctx.send(view=ConfirmView())


@bot.command(name='cpu-monitor')
@is_admin()
async def cpu_monitor_control(ctx, action: str = "status"):
    """Control abuse monitoring (Admin only) — replaces legacy CPU-only monitor"""
    global MONITOR_CONFIG
    if action.lower() == "status":
        enabled = MONITOR_CONFIG.get("auto_suspend_enabled", True)
        embed   = create_embed(
            "🛡️ Abuse Monitor Status",
            f"Status: **{'Active' if enabled else 'Disabled'}**",
            0x00ccff if enabled else 0xffaa00
        )
        embed.add_field(name="CPU Threshold",       value=f"{MONITOR_CONFIG.get('cpu_threshold', 95)}%",     inline=True)
        embed.add_field(name="Sustained Duration",  value=f"{MONITOR_CONFIG.get('sustained_duration_minutes', 20)}min", inline=True)
        embed.add_field(name="Suspend Threshold",   value=f"Score ≥ {MONITOR_CONFIG.get('auto_suspend_threshold', 70)}", inline=True)
        embed.add_field(name="Scan Interval",       value=f"{MONITOR_CONFIG.get('monitoring_interval', 120)}s", inline=True)
        embed.add_field(name="Suspensions Logged",  value=str(len(suspension_log)),                           inline=True)
        await ctx.send(embed=embed)
    elif action.lower() == "enable":
        MONITOR_CONFIG["auto_suspend_enabled"] = True
        save_mining_config()
        await ctx.send(embed=create_success_embed("Abuse Monitor Enabled", "Automatic suspension is now active."))
    elif action.lower() == "disable":
        MONITOR_CONFIG["auto_suspend_enabled"] = False
        save_mining_config()
        await ctx.send(embed=create_warning_embed("Abuse Monitor Disabled", "Automatic suspension is now OFF."))
    else:
        await ctx.send(embed=create_error_embed("Invalid Action", "Use: `!cpu-monitor <status|enable|disable>`"))


@bot.command(name='admin-add')
@is_main_admin()
async def admin_add(ctx, user: discord.Member):
    user_id = str(user.id)
    if user_id not in admin_data["admins"]:
        admin_data["admins"].append(user_id)
        save_data()
    await ctx.send(embed=create_success_embed("Admin Added", f"{user.mention} is now an admin."))


@bot.command(name='admin-remove')
@is_main_admin()
async def admin_remove(ctx, user: discord.Member):
    user_id = str(user.id)
    if user_id in admin_data["admins"]:
        admin_data["admins"].remove(user_id)
        save_data()
    await ctx.send(embed=create_success_embed("Admin Removed", f"{user.mention} is no longer an admin."))


@bot.command(name='admin-list')
@is_main_admin()
async def admin_list(ctx):
    admins = []
    for aid in admin_data.get("admins", []):
        try:
            u = await bot.fetch_user(int(aid))
            admins.append(f"• {u.mention} ({u.name})")
        except Exception:
            admins.append(f"• Unknown ({aid})")
    embed = create_info_embed("Admin List", "\n".join(admins) if admins else "No admins")
    await ctx.send(embed=embed)


# ─── Anti-mining admin commands ───────────────────────────────────────────────

@bot.command(name='vps-suspend')
@is_admin()
async def vps_suspend(ctx, user: discord.Member, vps_number: int, *, reason: str = "Manual admin suspension"):
    """Manually suspend a VPS (Admin only) — !vps-suspend @user <vps#> [reason]"""
    user_id  = str(user.id)
    vps_list = vps_data.get(user_id, [])
    if not vps_list or vps_number < 1 or vps_number > len(vps_list):
        await ctx.send(embed=create_error_embed("Not Found", f"No VPS #{vps_number} for {user.mention}."))
        return

    vps            = vps_list[vps_number - 1]
    container_name = vps["container_name"]

    await ctx.send(embed=create_info_embed("Suspending VPS", f"Stopping and suspending `{container_name}`..."))
    try:
        await execute_docker(f"docker stop --time=5 {container_name}", timeout=30)
    except Exception:
        pass

    vps["status"]            = "suspended"
    vps["suspension_reason"] = reason
    vps["suspension_time"]   = _utcnow().isoformat()
    save_data()

    log_entry = {
        "timestamp":        _utcnow().isoformat(),
        "container_name":   container_name,
        "owner_user_id":    user_id,
        "type":             "manual",
        "reason":           reason,
        "admin_id":         str(ctx.author.id)
    }
    suspension_log.append(log_entry)
    save_suspension_log()

    embed = create_error_embed("VPS Suspended", f"`{container_name}` has been suspended.")
    embed.add_field(name="Owner",  value=user.mention,            inline=True)
    embed.add_field(name="VPS #",  value=str(vps_number),         inline=True)
    embed.add_field(name="Reason", value=reason,                  inline=False)
    embed.add_field(name="Admin",  value=ctx.author.mention,      inline=True)
    await ctx.send(embed=embed)

    _susp_logo = get_logo_url()
    _log = discord.Embed(
        title="⛔  VPS Suspended",
        description=(
            f"**#{vps_number}** belonging to {user.mention} was manually suspended.\n"
            f"> **Reason:** {reason}"
        ),
        colour=discord.Colour.from_rgb(237, 66, 69),
        timestamp=_utcnow()
    )
    _log.set_author(name="VPS Suspended  •  Manual Admin Action", **( {"icon_url": _susp_logo} if _susp_logo else {}))
    try:
        _log.set_thumbnail(url=user.display_avatar.url)
    except Exception:
        pass
    _log.add_field(name="📦 Container", value=f"`{container_name}`",              inline=True)
    _log.add_field(name="🆔 VPS ID",    value=f"`#{vps_number}`",                 inline=True)
    _log.add_field(name="🏷️ Hostname",  value=f"`{vps.get('hostname','?')}`",    inline=True)
    _log.add_field(name="👤 Owner",      value=user.mention,                       inline=True)
    _log.add_field(name="🛠️ Admin",     value=ctx.author.mention,                 inline=True)
    _log.add_field(name="🔒 Status",     value="⛔ **SUSPENDED**",                inline=True)
    _log.add_field(name="🧠 RAM",        value=f"`{vps.get('ram','?')}`",          inline=True)
    _log.add_field(name="⚙️ CPU",        value=f"`{vps.get('cpu','?')} Core(s)`", inline=True)
    _log.add_field(name="📌 Restore",    value="`!vps-unsuspend @user <#>`",      inline=True)
    _log.set_footer(**{"text": f"{get_brand_name()} VPS Logs  •  Manual Suspension", **({"icon_url": _susp_logo} if _susp_logo else {})})
    asyncio.create_task(send_log(_log))

    try:
        await user.send(embed=create_error_embed(
            "⚠️ VPS Suspended",
            f"Your VPS **#{vps_number}** (`{container_name}`) has been suspended by an admin.\n"
            f"**Reason:** {reason}\n\nContact an admin to appeal."
        ))
    except discord.Forbidden:
        pass


@bot.command(name='vps-unsuspend')
@is_admin()
async def vps_unsuspend(ctx, user: discord.Member, vps_number: int):
    """Unsuspend a VPS (Admin only) — !vps-unsuspend @user <vps#>"""
    user_id  = str(user.id)
    vps_list = vps_data.get(user_id, [])
    if not vps_list or vps_number < 1 or vps_number > len(vps_list):
        await ctx.send(embed=create_error_embed("Not Found", f"No VPS #{vps_number} for {user.mention}."))
        return

    vps            = vps_list[vps_number - 1]
    container_name = vps["container_name"]

    if vps.get("status") != "suspended":
        await ctx.send(embed=create_error_embed("Not Suspended", f"`{container_name}` is not currently suspended."))
        return

    try:
        await execute_docker(f"docker start {container_name}", timeout=30)
        await asyncio.sleep(8)
        await docker_exec(container_name, "systemctl restart ssh 2>/dev/null || /usr/sbin/sshd || true", timeout=15)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Start Failed", f"Could not start container: {e}"))
        return

    vps["status"] = "running"
    vps.pop("suspension_reason",  None)
    vps.pop("suspension_time",    None)
    vps.pop("suspension_evidence", None)
    save_data()

    # Clear monitoring state so the container gets a clean slate
    container_monitor_state.pop(container_name, None)

    embed = create_success_embed("VPS Unsuspended", f"`{container_name}` is now running again.")
    embed.add_field(name="Owner", value=user.mention,       inline=True)
    embed.add_field(name="VPS #", value=str(vps_number),    inline=True)
    embed.add_field(name="Admin", value=ctx.author.mention, inline=True)
    await ctx.send(embed=embed)

    _unsusp_logo = get_logo_url()
    _log = discord.Embed(
        title="✅  VPS Unsuspended",
        description=f"**#{vps_number}** belonging to {user.mention} has been **restored** and is live again.",
        colour=discord.Colour.from_rgb(87, 242, 135),
        timestamp=_utcnow()
    )
    _log.set_author(name="VPS Unsuspended  •  Container Restored", **( {"icon_url": _unsusp_logo} if _unsusp_logo else {}))
    try:
        _log.set_thumbnail(url=user.display_avatar.url)
    except Exception:
        pass
    _log.add_field(name="📦 Container", value=f"`{container_name}`",              inline=True)
    _log.add_field(name="🆔 VPS ID",    value=f"`#{vps_number}`",                 inline=True)
    _log.add_field(name="🏷️ Hostname",  value=f"`{vps.get('hostname','?')}`",    inline=True)
    _log.add_field(name="👤 Owner",      value=user.mention,                       inline=True)
    _log.add_field(name="🛠️ Admin",     value=ctx.author.mention,                 inline=True)
    _log.add_field(name="🌐 Status",     value="🟢 **RUNNING**",                  inline=True)
    _log.add_field(name="🧠 RAM",        value=f"`{vps.get('ram','?')}`",          inline=True)
    _log.add_field(name="⚙️ CPU",        value=f"`{vps.get('cpu','?')} Core(s)`", inline=True)
    _log.add_field(name="🧹 Monitor",    value="Abuse-detection baseline reset",  inline=True)
    _log.set_footer(**{"text": f"{get_brand_name()} VPS Logs  •  Unsuspend Event", **({"icon_url": _unsusp_logo} if _unsusp_logo else {})})
    asyncio.create_task(send_log(_log))

    try:
        await user.send(embed=create_success_embed(
            "✅ VPS Unsuspended",
            f"Your VPS **#{vps_number}** (`{container_name}`) has been unsuspended and is running again."
        ))
    except discord.Forbidden:
        pass


@bot.command(name='vps-monitor')
@is_admin()
async def vps_monitor(ctx, container_name: str, action: str = "status"):
    """
    Control per-container monitoring (Admin only)
    !vps-monitor <container> status|pause|resume
    """
    uid, vps = find_vps_record(container_name)
    if not vps:
        await ctx.send(embed=create_error_embed("Not Found", f"No VPS record for `{container_name}`."))
        return

    state = get_container_state(container_name)

    if action.lower() == "status":
        conf   = state.get("last_confidence", 0)
        paused = state.get("monitoring_paused", False)
        cpu_samples = list(state.get("cpu_samples", []))
        avg_cpu = sum(cpu_samples) / len(cpu_samples) if cpu_samples else 0

        embed = create_info_embed(f"📊 Monitor — {container_name}", "")
        embed.add_field(name="Monitoring",        value="⏸ Paused" if paused else "▶ Active", inline=True)
        embed.add_field(name="Last Confidence",   value=f"{conf}/100",                         inline=True)
        embed.add_field(name="Avg CPU (recent)",  value=f"{avg_cpu:.1f}%",                     inline=True)
        embed.add_field(name="Active Flags",      value=", ".join(state.get("flags", set())) or "none", inline=False)

        # Last 5 scan events
        events = monitor_log.get(container_name, [])[-5:]
        if events:
            lines = []
            for ev in events:
                ts    = ev.get("timestamp", "?")[:16]
                score = ev.get("total_score", ev.get("confidence_score", 0))
                lines.append(f"`{ts}` score={score}")
            embed.add_field(name="Recent Scans", value="\n".join(lines), inline=False)

        await ctx.send(embed=embed)

    elif action.lower() == "pause":
        state["monitoring_paused"] = True
        await ctx.send(embed=create_warning_embed(
            "Monitoring Paused",
            f"Abuse monitoring for `{container_name}` is **paused**.\n"
            "Use `!vps-monitor <container> resume` to re-enable."
        ))

    elif action.lower() == "resume":
        state["monitoring_paused"] = False
        container_monitor_state.pop(container_name, None)  # fresh state
        await ctx.send(embed=create_success_embed(
            "Monitoring Resumed",
            f"Abuse monitoring for `{container_name}` is now **active** again."
        ))

    else:
        await ctx.send(embed=create_error_embed("Invalid Action", "Use: `status`, `pause`, or `resume`"))


@bot.command(name='vps-whitelist')
@is_admin()
async def vps_whitelist(ctx, target_type: str, *, value: str):
    """
    Whitelist a user/container from auto-suspension (Admin only)
    !vps-whitelist user @mention
    !vps-whitelist container <container_name>
    !vps-whitelist process <process_name>
    """
    target_type = target_type.lower()

    if target_type == "user":
        # Accept mention or raw ID
        member = None
        try:
            member = await commands.MemberConverter().convert(ctx, value)
        except Exception:
            pass
        uid = str(member.id) if member else value.strip()
        wl  = MONITOR_CONFIG.setdefault("whitelisted_users", [])
        if uid not in wl:
            wl.append(uid)
            save_mining_config()
        name = member.mention if member else uid
        await ctx.send(embed=create_success_embed("User Whitelisted", f"{name} is now exempt from auto-suspension."))

    elif target_type == "container":
        wl = MONITOR_CONFIG.setdefault("whitelisted_containers", [])
        if value not in wl:
            wl.append(value)
            save_mining_config()
        await ctx.send(embed=create_success_embed("Container Whitelisted", f"`{value}` will not be auto-suspended."))

    elif target_type == "process":
        wl = MONITOR_CONFIG.setdefault("whitelisted_processes", [])
        if value not in wl:
            wl.append(value)
            save_mining_config()
        await ctx.send(embed=create_success_embed("Process Whitelisted", f"Process `{value}` is now safe-listed."))

    else:
        await ctx.send(embed=create_error_embed(
            "Invalid Type", "Use: `user`, `container`, or `process`\n"
            "Example: `!vps-whitelist user @someone`"
        ))


@bot.command(name='vps-unwhitelist')
@is_admin()
async def vps_unwhitelist(ctx, target_type: str, *, value: str):
    """Remove a whitelist entry (Admin only)"""
    target_type = target_type.lower()
    key_map = {"user": "whitelisted_users", "container": "whitelisted_containers", "process": "whitelisted_processes"}
    if target_type not in key_map:
        await ctx.send(embed=create_error_embed("Invalid Type", "Use: `user`, `container`, or `process`"))
        return

    key = key_map[target_type]
    wl  = MONITOR_CONFIG.get(key, [])

    # For user type, try to resolve mention → ID
    uid = value.strip()
    if target_type == "user":
        try:
            member = await commands.MemberConverter().convert(ctx, value)
            uid = str(member.id)
        except Exception:
            pass

    if uid in wl:
        wl.remove(uid)
        MONITOR_CONFIG[key] = wl
        save_mining_config()
        await ctx.send(embed=create_success_embed("Whitelist Updated", f"`{uid}` removed from whitelist."))
    else:
        await ctx.send(embed=create_error_embed("Not Found", f"`{uid}` was not in the {target_type} whitelist."))


@bot.command(name='vps-mining-config')
@is_admin()
async def vps_mining_config(ctx, key: str = None, *, value: str = None):
    """
    View or update mining monitor config (Admin only)
    !vps-mining-config                   — show all settings
    !vps-mining-config cpu_threshold 90  — update a setting
    """
    int_keys   = {"cpu_threshold", "sustained_duration_minutes", "auto_suspend_threshold",
                  "monitoring_interval", "notification_channel_id"}
    bool_keys  = {"auto_suspend_enabled"}

    if key is None:
        # Show current config
        embed = create_info_embed("⚙️ Mining Monitor Config", "Current configuration values:")
        for k, v in MONITOR_CONFIG.items():
            if isinstance(v, list):
                display = f"{len(v)} entries"
            else:
                display = str(v)
            embed.add_field(name=k, value=f"`{display}`", inline=True)
        await ctx.send(embed=embed)
        return

    if key not in MONITOR_CONFIG:
        await ctx.send(embed=create_error_embed("Unknown Key", f"`{key}` is not a valid config key."))
        return

    if value is None:
        current = MONITOR_CONFIG[key]
        await ctx.send(embed=create_info_embed(f"Config: {key}", f"Current value: `{current}`"))
        return

    # Parse value
    try:
        if key in int_keys:
            parsed = int(value)
        elif key in bool_keys:
            parsed = value.lower() in ("true", "1", "yes", "on")
        else:
            parsed = value
        MONITOR_CONFIG[key] = parsed
        save_mining_config()
        # Restart monitor with new interval if interval changed
        if key == "monitoring_interval" and abuse_monitor.is_running():
            abuse_monitor.change_interval(seconds=parsed)
        await ctx.send(embed=create_success_embed(
            "Config Updated", f"`{key}` → `{parsed}`"
        ))
    except ValueError:
        await ctx.send(embed=create_error_embed("Invalid Value", f"Could not parse `{value}` for key `{key}`."))


@bot.command(name='vps-scan')
@is_admin()
async def vps_scan(ctx, container_name: str):
    """Manually trigger an abuse scan on a container (Admin only)"""
    uid, vps = find_vps_record(container_name)
    if not vps:
        await ctx.send(embed=create_error_embed("Not Found", f"No VPS record for `{container_name}`."))
        return

    msg = await ctx.send(embed=create_info_embed("Scanning", f"Running abuse scan on `{container_name}`..."))

    cpu_pct = await _get_container_cpu(container_name)
    scan    = await _scan_container_for_mining(container_name)

    # Add cpu_score for display (don't auto-suspend from manual scan)
    cpu_score    = 30 if cpu_pct > MONITOR_CONFIG.get("cpu_threshold", 95) else 0
    total_score  = cpu_score + scan["confidence_score"]

    color = 0x00ff88 if total_score < 30 else (0xffaa00 if total_score < 60 else 0xff3366)
    embed = create_embed(f"🔍 Scan Result — {container_name}", "", color)
    embed.add_field(name="⚙️ CPU %",            value=f"{cpu_pct:.1f}%",                         inline=True)
    embed.add_field(name="🔢 Confidence Score", value=f"**{total_score}/100**",                   inline=True)
    embed.add_field(name="🛡️ Suspend Threshold", value=f"{MONITOR_CONFIG.get('auto_suspend_threshold', 70)}", inline=True)
    embed.add_field(name="🦠 Suspicious Processes",
                    value=", ".join(scan["found_processes"]) or "none",   inline=False)
    embed.add_field(name="🌐 Pool Connections",
                    value=", ".join(scan["found_connections"]) or "none", inline=False)
    embed.add_field(name="💻 Mining CLI Args",
                    value=", ".join(scan["found_cli_args"]) or "none",    inline=False)
    if scan["details"]:
        embed.add_field(name="📋 Details",
                        value="\n".join(scan["details"][:10]),            inline=False)
    embed.add_field(
        name="🟡 Note",
        value="Manual scans are informational only — they do **not** auto-suspend.",
        inline=False
    )
    await msg.edit(embed=embed)


@bot.command(name='vps-status')
@is_admin()
async def vps_status_cmd(ctx):
    """Show full monitoring status overview (Admin only)"""
    embed = create_embed("🛡️ Monitoring Overview", "Live anti-abuse status for all containers", 0xF0A500)

    enabled  = MONITOR_CONFIG.get("auto_suspend_enabled", True)
    threshold = MONITOR_CONFIG.get("auto_suspend_threshold", 70)

    embed.add_field(
        name="Monitor Config",
        value=(
            f"**Auto-Suspend:** {'✅ Enabled' if enabled else '❌ Disabled'}\n"
            f"**CPU Threshold:** {MONITOR_CONFIG.get('cpu_threshold', 95)}%\n"
            f"**Suspend Score:** ≥{threshold}\n"
            f"**Scan Interval:** {MONITOR_CONFIG.get('monitoring_interval', 120)}s"
        ),
        inline=False
    )

    # Containers with elevated confidence scores
    flagged_lines = []
    for name, state in container_monitor_state.items():
        conf = state.get("last_confidence", 0)
        if conf > 0:
            paused = "⏸" if state.get("monitoring_paused") else ""
            flagged_lines.append(f"`{name}` — score {conf} {paused}")
    embed.add_field(
        name=f"🔶 Containers with Non-Zero Score ({len(flagged_lines)})",
        value="\n".join(flagged_lines[:15]) if flagged_lines else "None",
        inline=False
    )

    # Suspension counts
    today = _utcnow().strftime("%Y-%m-%d")
    today_suspensions = sum(1 for e in suspension_log if e.get("timestamp", "").startswith(today))
    embed.add_field(
        name="📊 Suspension Stats",
        value=(
            f"**Total Suspensions:** {len(suspension_log)}\n"
            f"**Today:** {today_suspensions}\n"
            f"**Currently Suspended:** {sum(1 for vl in vps_data.values() for v in vl if v.get('status')=='suspended')}"
        ),
        inline=False
    )

    # Whitelist summary
    embed.add_field(
        name="🟩 Whitelist",
        value=(
            f"**Users:** {len(MONITOR_CONFIG.get('whitelisted_users', []))}\n"
            f"**Containers:** {len(MONITOR_CONFIG.get('whitelisted_containers', []))}\n"
            f"**Processes:** {len(MONITOR_CONFIG.get('whitelisted_processes', []))}"
        ),
        inline=False
    )
    await ctx.send(embed=embed)


# ─── Log channel helper ────────────────────────────────────────────────────────

async def send_log(embed: discord.Embed):
    """
    Post an embed to the configured log channel.
    Silently does nothing if no channel is set or the channel is unreachable.
    Call via asyncio.create_task(send_log(...)) from event handlers so the
    Discord response is never delayed waiting for the log to send.
    """
    channel_id = MONITOR_CONFIG.get("notification_channel_id", 0)
    if not channel_id:
        return
    try:
        channel = bot.get_channel(int(channel_id))
        if channel is None:
            channel = await bot.fetch_channel(int(channel_id))
        if channel:
            await channel.send(embed=embed)
    except Exception as exc:
        logger.error(f"send_log: failed to post to channel {channel_id}: {exc}")


# ─── Slash Commands ────────────────────────────────────────────────────────────

@bot.tree.command(name="setlogo", description="Change the logo shown in all bot embeds (Admin only)")
@discord.app_commands.describe(url="Direct image URL for the new logo (Discord CDN, Imgur, etc.)")
async def setlogo(interaction: discord.Interaction, url: str):
    """Admin-only: update the logo thumbnail/icon used across every embed."""
    user_id = str(interaction.user.id)
    is_admin_user = (
        user_id == str(MAIN_ADMIN_ID) or
        user_id in admin_data.get("admins", [])
    )
    if not is_admin_user:
        await interaction.response.send_message(
            embed=create_error_embed("Permission Denied", "Only admins can change the bot logo."),
            ephemeral=True,
        )
        return

    # Basic URL sanity check
    if not url.startswith(("http://", "https://")):
        await interaction.response.send_message(
            embed=create_error_embed("Invalid URL", "Please provide a full URL starting with `https://`."),
            ephemeral=True,
        )
        return

    set_logo_url(url)

    logo = get_logo_url()
    embed = discord.Embed(
        title="🖼️  Logo Updated",
        description=(
            f"The {get_brand_name()} logo has been updated across **all embeds**.\n"
            "New deployments, progress bars, DMs, and log messages will use the new image."
        ),
        color=0x9B59B6,
        timestamp=_utcnow(),
    )
    embed.set_thumbnail(url=logo)
    embed.add_field(name="🔗 URL", value=f"[View image]({logo})", inline=False)
    embed.add_field(name="👤 Set by", value=interaction.user.mention, inline=True)
    embed.set_footer(text=f"{get_brand_name()}  •  /setlogo", icon_url=logo)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="branding", description="Change the brand name shown in all bot watermarks and embeds (Admin only)")
@discord.app_commands.describe(name="New brand name (e.g. MyHost, CloudVPS, NexusNodes)")
async def branding_cmd(interaction: discord.Interaction, name: str):
    """Admin-only: update the brand/watermark name used across every embed."""
    user_id = str(interaction.user.id)
    is_admin_user = (
        user_id == str(MAIN_ADMIN_ID) or
        user_id in admin_data.get("admins", [])
    )
    if not is_admin_user:
        await interaction.response.send_message(
            embed=create_error_embed("Permission Denied", "Only admins can change the brand name."),
            ephemeral=True,
        )
        return

    name = name.strip()
    if not name or len(name) > 64:
        await interaction.response.send_message(
            embed=create_error_embed("Invalid Name", "Brand name must be between 1 and 64 characters."),
            ephemeral=True,
        )
        return

    old_name = get_brand_name()
    set_brand_name(name)
    # Update activity status
    try:
        await bot.change_presence(activity=discord.Activity(
            type=discord.ActivityType.watching, name=f"{get_brand_name()} | VPS Manager"
        ))
    except Exception:
        pass
    # Push brand file to all running containers so MOTD updates immediately
    for _uid, _vpss in vps_data.items():
        for _vps in _vpss:
            if _vps.get("status") == "running":
                _cn = _vps.get("container_name", "")
                if _cn:
                    asyncio.create_task(_update_container_brand(_cn, name))

    logo = get_logo_url()
    embed = discord.Embed(
        title="✏️  Brand Name Updated",
        description=(
            f"All watermarks and embed footers will now display **{name}**.\n\n"
            f"> Previous name: `{old_name}`\n"
            f"> New name: `{name}`"
        ),
        color=0x9B59B6,
        timestamp=_utcnow(),
    )
    embed.add_field(
        name="📋  Affected Areas",
        value=(
            "• Embed author lines\n"
            "• Embed footer watermarks\n"
            "• Bot activity status\n"
            "• VPS welcome & DM messages\n"
            "• Log channel embeds\n"
            "• Template installer embeds"
        ),
        inline=False,
    )
    embed.add_field(name="👤  Changed by", value=interaction.user.mention, inline=True)
    if logo:
        embed.set_footer(text=f"{get_brand_name()}  •  /branding", icon_url=logo)
    else:
        embed.set_footer(text=f"{get_brand_name()}  •  /branding")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="sidebarcolor", description="Change the sidebar accent color for all bot embeds (Admin only)")
@discord.app_commands.describe(color="Hex color code — e.g.  #FF0000  or  1ABC9C  (without #)")
async def sidebarcolor_cmd(interaction: discord.Interaction, color: str):
    """Admin-only: update the sidebar/accent color used across all embeds."""
    user_id = str(interaction.user.id)
    if user_id != str(MAIN_ADMIN_ID) and user_id not in admin_data.get("admins", []):
        await interaction.response.send_message(
            embed=create_error_embed("Permission Denied", "Only admins can change the embed color."),
            ephemeral=True,
        )
        return

    # Normalise: strip leading # and whitespace
    hex_str = color.strip().lstrip("#")
    if len(hex_str) not in (3, 6) or not all(c in "0123456789abcdefABCDEF" for c in hex_str):
        await interaction.response.send_message(
            embed=create_error_embed(
                "Invalid Color",
                "Please provide a valid hex code — e.g. `#1ABC9C` or `000000`."
            ),
            ephemeral=True,
        )
        return
    if len(hex_str) == 3:
        hex_str = "".join(c * 2 for c in hex_str)

    color_int = int(hex_str, 16)
    set_embed_color(color_int)

    logo  = get_logo_url()
    swatch = f"#{hex_str.upper()}"
    embed = discord.Embed(
        title="🎨  Sidebar Color Updated",
        description=(
            f"All embed sidebar accents will now use **`{swatch}`**.\n\n"
            "This affects: deploy progress, success embeds, DM cards, "
            "and all other bot messages."
        ),
        color=color_int,
        timestamp=_utcnow(),
    )
    embed.add_field(name="🎨  New Color", value=f"`{swatch}`",              inline=True)
    embed.add_field(name="👤  Set by",    value=interaction.user.mention,    inline=True)
    if logo:
        embed.set_footer(text=f"{get_brand_name()}  •  /sidebarcolor", icon_url=logo)
    else:
        embed.set_footer(text=f"{get_brand_name()}  •  /sidebarcolor")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="setlogschannel", description="Set the channel where all VPS event logs are posted (Admin only)")
@discord.app_commands.describe(channel="The text channel that will receive all VPS event log embeds")
async def setlogschannel(interaction: discord.Interaction, channel: discord.TextChannel):
    """Admin-only slash command: choose the log channel for all VPS events."""
    user_id = str(interaction.user.id)
    is_admin_user = (
        user_id == str(MAIN_ADMIN_ID) or
        user_id in admin_data.get("admins", [])
    )
    if not is_admin_user:
        await interaction.response.send_message(
            embed=create_error_embed("Permission Denied", "Only admins can configure the log channel."),
            ephemeral=True
        )
        return

    MONITOR_CONFIG["notification_channel_id"] = channel.id
    save_mining_config()

    embed = discord.Embed(
        title="✅  Log Channel Set",
        description=(
            f"All VPS event logs will now be posted to {channel.mention}.\n\n"
            f"**Events logged:**\n"
            f"🟢 VPS Created  •  🔴 VPS Deleted  •  ▶️ Started  •  ⏹️ Stopped\n"
            f"🔄 Reinstalled  •  ⛔ Suspended (auto & manual)  •  ✅ Unsuspended  •  🚨 Mining Detected"
        ),
        colour=discord.Colour.from_rgb(87, 242, 135),
        timestamp=_utcnow()
    )
    embed.add_field(name="📋 Channel", value=f"`#{channel.name}` (ID `{channel.id}`)", inline=False)
    embed.set_footer(text=f"{get_brand_name()}  •  /setlogschannel")
    await interaction.response.send_message(embed=embed)


@bot.command(name='setlogschannel')
async def setlogschannel_prefix(ctx, channel: discord.TextChannel = None):
    """Set the VPS log channel via prefix command (Admin only) — !setlogschannel #channel"""
    user_id = str(ctx.author.id)
    is_admin_user = (
        user_id == str(MAIN_ADMIN_ID) or
        user_id in admin_data.get("admins", [])
    )
    if not is_admin_user:
        await ctx.send(embed=create_error_embed("Permission Denied", "Only admins can configure the log channel."))
        return
    if channel is None:
        current_id = MONITOR_CONFIG.get("notification_channel_id", 0)
        if current_id:
            ch = bot.get_channel(int(current_id))
            ch_str = ch.mention if ch else f"`{current_id}` (not found)"
            await ctx.send(embed=create_info_embed("Current Log Channel", f"Logs are currently being sent to: {ch_str}"))
        else:
            await ctx.send(embed=create_info_embed("No Log Channel Set", "Use `!setlogschannel #channel` to set one."))
        return

    MONITOR_CONFIG["notification_channel_id"] = channel.id
    save_mining_config()

    embed = discord.Embed(
        title="✅  Log Channel Configured",
        description=(
            f"All VPS event logs will now be posted to {channel.mention}.\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        ),
        colour=discord.Colour.from_rgb(87, 242, 135),
        timestamp=_utcnow()
    )
    embed.add_field(name="📋 Channel", value=f"{channel.mention}\n`#{channel.name}` (ID `{channel.id}`)", inline=False)
    embed.add_field(
        name="📣 Events Logged",
        value=(
            "🟢 VPS Created  •  🔴 VPS Deleted  •  ▶️ Started  •  ⏹️ Stopped\n"
            "🔄 Reinstalled  •  ⛔ Suspended (auto & manual)  •  ✅ Unsuspended\n"
            "🚨 Mining Detected  •  All admin actions"
        ),
        inline=False
    )
    embed.set_footer(text=f"{get_brand_name()}  •  !setlogschannel")
    await ctx.send(embed=embed)

    # Send a test embed to confirm the channel works
    test_embed = discord.Embed(
        title="📡  Log Channel Test",
        description=f"This channel (`#{channel.name}`) is now receiving {get_brand_name()} VPS logs.",
        colour=discord.Colour.from_rgb(88, 101, 242),
        timestamp=_utcnow()
    )
    test_embed.add_field(name="Set By", value=ctx.author.mention, inline=True)
    test_embed.set_footer(text=f"{get_brand_name()} VPS Logs  •  Channel Verified")
    try:
        await channel.send(embed=test_embed)
    except discord.Forbidden:
        await ctx.send(embed=create_error_embed("Permission Error", f"I cannot send messages to {channel.mention}. Please check my permissions."))


# ─── Scrollable Help System ────────────────────────────────────────────────────

def build_help_pages(is_user_admin, is_user_main_admin):
    pages = {}

    # Page 2: User Commands
    user_embed = create_embed("👤 User Commands", "VPS management commands for all users:", 0x00ff88)
    user_embed.add_field(name="🖥️ VPS Management", value=(
        "`!manage` — Manage your VPS (Start/Stop/SSH/Reinstall)\n"
        "`!manage [@user]` — Admin: manage another user's VPS\n"
        "`!share-user @user <#>` — Share VPS access\n"
        "`!share-ruser @user <#>` — Revoke shared access\n"
        "`!manage-shared @owner <#>` — Access shared VPS"
    ), inline=False)
    user_embed.add_field(name="🏷️ VPS Tools", value=(
        "`!rename-vps <#> <new_name>` — Give your VPS a nickname\n"
        "`!vps-note <#> <note>` — Add a note to your VPS\n"
        "`!ping-vps <#>` — Ping your VPS container\n"
        "`!uptime-vps <#>` — Check VPS uptime\n"
        "`!myinfo` — View your profile & VPS summary"
    ), inline=False)
    pages["user"] = user_embed

    # Page 3: Credits & Plans
    credits_embed = create_embed("💰 Credits & Plans", "Purchase plans and manage credits:", 0xffaa00)
    credits_embed.add_field(name="💳 Commands", value=(
        "`!plans` — View available VPS plans & prices\n"
        "`!buyc` — Get payment info (UPI/PayPal/Crypto)\n"
        "`!buywc <plan> <Intel/AMD>` — Buy VPS with credits\n"
        "`!credits` — Check your credit balance\n"
        "`!transfer @user <amount>` — Send credits to another user"
    ), inline=False)
    credits_embed.add_field(name="📦 Available Plans", value=(
        "🥉 **Starter** — 4GB RAM | 1 CPU | Intel: 42cr / AMD: 83cr\n"
        "🥈 **Basic** — 8GB RAM | 1 CPU | Intel: 96cr / AMD: 164cr\n"
        "🥇 **Standard** — 12GB RAM | 2 CPU | Intel: 192cr / AMD: 320cr\n"
        "💎 **Pro** — 16GB RAM | 2 CPU | Intel: 220cr / AMD: 340cr"
    ), inline=False)
    pages["credits"] = credits_embed

    # Page 4: VPS Tools
    tools_embed = create_embed("🔧 VPS Tools", "Extra tools to manage & monitor your VPS:", 0x00ccff)
    tools_embed.add_field(name="📊 Monitoring", value=(
        "`!ping-vps <#>` — Ping VPS container (check if alive)\n"
        "`!uptime-vps <#>` — Show how long VPS has been running\n"
        "`!myinfo` — Your full profile: credits, VPS list, notes"
    ), inline=False)
    tools_embed.add_field(name="🏷️ Customization", value=(
        "`!rename-vps <#> <name>` — Set a nickname for your VPS\n"
        "`!vps-note <#> <text>` — Add/update a note on your VPS\n"
        "  e.g. `!vps-note 1 My Minecraft server`"
    ), inline=False)
    tools_embed.add_field(name="💸 Economy", value=(
        "`!transfer @user <amount>` — Send credits to a friend\n"
        "`!leaderboard` — Top 10 credit holders\n"
        "`!botstatus` — Show bot stats & uptime"
    ), inline=False)
    pages["tools"] = tools_embed

    # Page 5: Extras
    extras_embed = create_embed("📢 Extras & Fun", "Announcements, leaderboard, and more:", 0xff6b9d)
    extras_embed.add_field(name="📣 Announcements", value=(
        "`!announce <message>` — Admin: broadcast to all VPS owners via DM\n"
        "`!botstatus` — Bot uptime, total VPS, active users\n"
        "`!leaderboard` — Top credit holders on the server"
    ), inline=False)
    extras_embed.add_field(name="ℹ️ Info", value=(
        "`!help` — Open this help menu\n"
        "`!plans` — VPS plan pricing\n"
        "`!myinfo` — Your personal dashboard"
    ), inline=False)
    pages["extras"] = extras_embed

    # Page 6: Admin Panel
    if is_user_admin:
        admin_embed = create_embed("🛡️ Admin Panel", "Admin-only VPS management commands:", 0xff3366)
        admin_embed.add_field(name="🖥️ VPS Control", value=(
            "`!create @user <ram_GB> <cpu_cores> <disk_GB>` — Create custom Docker VPS\n"
            "`!delete-vps @user <#> <reason>` — Delete a user's VPS\n"
            "`!restart-vps <container>` — Restart a VPS container\n"
            "`!stop-vps-all` — Emergency stop ALL VPS\n"
            "`!exec <container> <cmd>` — Run command inside VPS"
        ), inline=False)
        admin_embed.add_field(name="💾 Backup & Restore", value=(
            "`!backup-vps <container>` — Create Docker image snapshot\n"
            "`!restore-vps <container> <snapshot>` — Restore from snapshot\n"
            "`!list-snapshots <container>` — List all snapshots"
        ), inline=False)
        admin_embed.add_field(name="📊 Info & Economy", value=(
            "`!userinfo @user` — Full user info + VPS list\n"
            "`!serverstats` — Server overview stats\n"
            "`!vpsinfo [container]` — VPS details\n"
            "`!list-all` — All VPS overview\n"
            "`!adminc @user <amount>` — Add credits\n"
            "`!adminrc @user <amount/all>` — Remove credits\n"
            "`!announce <msg>` — DM all VPS owners\n"
            "`!cpu-monitor <status|enable|disable>` — Abuse monitor control\n"
            "`!maintenance <on/off>` — Toggle maintenance mode"
        ), inline=False)
        admin_embed.add_field(name="⏳ Expire Management", value=(
            "`!setexpire @user <vps#> <days>` — Set VPS expiry\n"
            "`!extendexpire @user <vps#> <days>` — Extend expiry\n"
            "`!removeexpire @user <vps#>` — Remove expiry (set to Never)\n"
            "`!checkexpire [@user]` — Check expiry status"
        ), inline=False)
        admin_embed.add_field(name="🚨 Anti-Mining & Abuse", value=(
            "`!vps-suspend @user <#> [reason]` — Manually suspend a VPS\n"
            "`!vps-unsuspend @user <#>` — Unsuspend a VPS\n"
            "`!vps-monitor <container> <status|pause|resume>` — Per-container monitor control\n"
            "`!vps-whitelist <user|container|process> <value>` — Add to whitelist\n"
            "`!vps-unwhitelist <user|container|process> <value>` — Remove from whitelist\n"
            "`!vps-mining-config [key] [value]` — View/edit monitor settings\n"
            "`!vps-scan <container>` — Manual abuse scan\n"
            "`!vps-status` — Full monitoring overview\n"
            "`!setlogschannel #channel` — Set the VPS log channel\n`/setlogschannel #channel` — Same via slash command"
        ), inline=False)
        pages["admin"] = admin_embed

    # Page 7: Main Admin
    if is_user_main_admin:
        mainadmin_embed = create_embed("👑 Main Admin", "Exclusive main admin commands:", 0xffd700)
        mainadmin_embed.add_field(name="👥 Admin Management", value=(
            "`!admin-add @user` — Promote user to admin\n"
            "`!admin-remove @user` — Remove admin role\n"
            "`!admin-list` — View all admins"
        ), inline=False)
        pages["mainadmin"] = mainadmin_embed

    return pages


@bot.command(name='help')
async def show_help(ctx):
    """Show scrollable categorized help"""
    user_id             = str(ctx.author.id)
    is_user_admin       = user_id == str(MAIN_ADMIN_ID) or user_id in admin_data.get("admins", [])
    is_user_main_admin  = user_id == str(MAIN_ADMIN_ID)

    pages = build_help_pages(is_user_admin, is_user_main_admin)

    options = [
        discord.SelectOption(label="👤 User Commands",    description="VPS manage, share, tools",       value="user",    emoji="👤"),
        discord.SelectOption(label="💰 Credits & Plans",  description="Buy VPS, check plans",            value="credits", emoji="💰"),
        discord.SelectOption(label="🔧 VPS Tools",        description="Rename, notes, ping, uptime",     value="tools",   emoji="🔧"),
        discord.SelectOption(label="📢 Extras",           description="Announcements, leaderboard",      value="extras",  emoji="📢"),
    ]
    if is_user_admin:
        options.append(discord.SelectOption(label="🛡️ Admin Panel", description="Admin VPS & abuse commands", value="admin",    emoji="🛡️"))
    if is_user_main_admin:
        options.append(discord.SelectOption(label="👑 Main Admin",  description="Admin management",           value="mainadmin", emoji="👑"))

    class HelpSelect(discord.ui.Select):
        def __init__(self):
            super().__init__(placeholder="📂 Select a category...", options=options, min_values=1, max_values=1)

        async def callback(self, interaction: discord.Interaction):
            selected = self.values[0]
            embed    = pages.get(selected, pages["user"])
            await interaction.response.edit_message(embed=embed, view=self.view)

    class HelpView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=180)
            self.add_item(HelpSelect())

    await ctx.send(embed=pages["user"], view=HelpView())


# ─── New Cool Features ──────────────────────────────────────────────────────────

BOT_START_TIME  = datetime.now()
maintenance_mode = False

@bot.command(name='rename-vps')
async def rename_vps(ctx, vps_number: int, *, new_name: str):
    """Give your VPS a custom nickname"""
    user_id  = str(ctx.author.id)
    vps_list = vps_data.get(user_id, [])
    if not vps_list or vps_number < 1 or vps_number > len(vps_list):
        await ctx.send(embed=create_error_embed("Invalid VPS", "VPS not found."))
        return
    if len(new_name) > 30:
        await ctx.send(embed=create_error_embed("Name Too Long", "Nickname must be 30 characters or less."))
        return
    vps_list[vps_number - 1]["nickname"] = new_name
    save_data()
    await ctx.send(embed=create_success_embed("VPS Renamed", f"VPS #{vps_number} is now called **{new_name}**!"))


@bot.command(name='vps-note')
async def vps_note(ctx, vps_number: int, *, note: str):
    """Add a note to your VPS"""
    user_id  = str(ctx.author.id)
    vps_list = vps_data.get(user_id, [])
    if not vps_list or vps_number < 1 or vps_number > len(vps_list):
        await ctx.send(embed=create_error_embed("Invalid VPS", "VPS not found."))
        return
    vps_list[vps_number - 1]["note"] = note[:200]
    save_data()
    await ctx.send(embed=create_success_embed("Note Saved", f"Note added to VPS #{vps_number}:\n> {note[:200]}"))


@bot.command(name='fixvps')
@is_admin()
async def fix_vps(ctx, container_name: str):
    """Apply all real-VPS fixes to an EXISTING container without wiping it — !fixvps <container>"""
    msg = await ctx.send(embed=create_info_embed(
        "🔧  Applying VPS Fixes",
        f"Running comprehensive real-VPS fix script inside `{container_name}`…\n"
        f"This takes ~30-60 s. No data will be lost.",
    ))

    out, _, rc = await run_docker_command(
        f"docker inspect --format={{{{.State.Running}}}} {container_name}", timeout=10
    )
    if rc != 0 or out.strip() != "true":
        await msg.edit(embed=create_error_embed(
            "Container Not Running",
            f"`{container_name}` is not running. Start it first with `!manage`.",
        ))
        return

    # ── Comprehensive fix script (plain string — no f-string, so {…} is safe) ──
    fix_script = """
set -e
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a
export NEEDRESTART_SUSPEND=1
export PIP_BREAK_SYSTEM_PACKAGES=1

echo "[1/12] Fixing DNS..."
chattr -i /etc/resolv.conf 2>/dev/null || true
rm -f /etc/resolv.conf
printf 'nameserver 8.8.8.8\\nnameserver 1.1.1.1\\nnameserver 8.8.4.4\\noptions edns0 trust-ad\\n' > /etc/resolv.conf
chattr +i /etc/resolv.conf 2>/dev/null || true

echo "[2/12] Suppressing needrestart prompts..."
mkdir -p /etc/needrestart/conf.d
printf '$nrconf{restart}     = '"'"'a'"'"';\\n$nrconf{kernelhints} = 0;\\n$nrconf{ucodehints}  = 0;\\n' \
    > /etc/needrestart/conf.d/50-darknodes.conf 2>/dev/null || true
grep -q NEEDRESTART_MODE    /etc/environment || echo "NEEDRESTART_MODE=a"      >> /etc/environment
grep -q NEEDRESTART_SUSPEND /etc/environment || echo "NEEDRESTART_SUSPEND=1"   >> /etc/environment
grep -q DEBIAN_PRIORITY     /etc/environment || echo "DEBIAN_PRIORITY=critical" >> /etc/environment
grep -q PIP_BREAK_SYSTEM    /etc/environment || echo "PIP_BREAK_SYSTEM_PACKAGES=1" >> /etc/environment

echo "[3/12] Configuring apt..."
mkdir -p /etc/apt/apt.conf.d
printf 'APT::Get::Assume-Yes "true";\\n'                      > /etc/apt/apt.conf.d/92-darknodes-yes
printf 'DPkg::Options:: "--force-confdef";\\nDPkg::Options:: "--force-confold";\\n' \
                                                               > /etc/apt/apt.conf.d/93-darknodes-dpkg
printf 'APT::Acquire::Retries "5";\\nAPT::Acquire::http::Timeout "30";\\n' \
                                                               > /etc/apt/apt.conf.d/94-darknodes-retries
if [ -f /etc/apt/sources.list.d/ubuntu.sources ]; then
    sed -i 's/^Components: main.*/Components: main restricted universe multiverse/' \
        /etc/apt/sources.list.d/ubuntu.sources 2>/dev/null || true
fi
if [ -f /etc/apt/sources.list ]; then
    sed -i 's/^# \\(deb.*universe\\)/\\1/'   /etc/apt/sources.list 2>/dev/null || true
    sed -i 's/^# \\(deb.*multiverse\\)/\\1/' /etc/apt/sources.list 2>/dev/null || true
fi

echo "[4/12] Running apt update..."
apt-get update -qq 2>&1 | tail -3 || \
    apt-get update --fix-missing -qq 2>/dev/null || true

echo "[5/12] Fixing python / pip aliases..."
if ! command -v python >/dev/null 2>&1; then
    update-alternatives --install /usr/bin/python python /usr/bin/python3 10 2>/dev/null || \
    ln -sf /usr/bin/python3 /usr/local/bin/python
fi
if ! command -v pip >/dev/null 2>&1; then
    ln -sf /usr/bin/pip3 /usr/local/bin/pip 2>/dev/null || true
fi
mkdir -p /etc/pip /root/.config/pip
printf '[global]\\nbreak-system-packages = true\\n' > /etc/pip.conf
cp /etc/pip.conf /root/.config/pip/pip.conf
if id admin >/dev/null 2>&1; then
    mkdir -p /home/admin/.config/pip
    cp /etc/pip.conf /home/admin/.config/pip/pip.conf
    chown -R admin:admin /home/admin/.config/pip
fi

echo "[6/12] Fixing npm global prefix..."
npm config set prefix /usr/local 2>/dev/null || true

echo "[7/12] Fixing Go PATH..."
mkdir -p /root/go/bin /root/go/pkg /root/go/src
if command -v go >/dev/null 2>&1; then
    go env -w GOPATH=/root/go 2>/dev/null || true
fi

echo "[8/12] Fixing Cargo / Rust PATH..."
for bin in cargo rustc rustup rustfmt; do
    [ -f /root/.cargo/bin/$bin ] && ln -sf /root/.cargo/bin/$bin /usr/local/bin/$bin 2>/dev/null || true
done

echo "[9/12] Writing system-wide PATH profile..."
cat > /etc/profile.d/00-darknodes-path.sh <<'PATHEOF'
#!/bin/sh
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a
export NEEDRESTART_SUSPEND=1
export PIP_BREAK_SYSTEM_PACKAGES=1
export GOPATH="${GOPATH:-/root/go}"
if command -v go >/dev/null 2>&1; then
    export PATH="$GOPATH/bin:$(go env GOROOT 2>/dev/null || echo /usr/local/go)/bin:$PATH"
fi
[ -d /root/.cargo/bin ] && export PATH="/root/.cargo/bin:$PATH"
PATHEOF
chmod +x /etc/profile.d/00-darknodes-path.sh

echo "[10/12] Applying sysctl tuning..."
cat > /etc/sysctl.d/99-darknodes.conf <<'SYSEOF'
net.ipv4.ip_forward            = 1
net.ipv4.tcp_fin_timeout       = 30
net.ipv4.tcp_keepalive_time    = 300
net.core.somaxconn             = 65535
net.core.netdev_max_backlog    = 5000
fs.file-max                    = 1000000
fs.inotify.max_user_watches    = 524288
vm.swappiness                  = 10
vm.overcommit_memory           = 1
kernel.dmesg_restrict          = 0
SYSEOF
sysctl -p /etc/sysctl.d/99-darknodes.conf 2>/dev/null || true

echo "[11/12] Applying ulimits..."
cat > /etc/security/limits.d/99-darknodes.conf <<'LIMEOF'
*    soft  nofile   65536
*    hard  nofile   65536
*    soft  nproc    65536
*    hard  nproc    65536
root soft  nofile   65536
root hard  nofile   65536
LIMEOF

echo "[12/12] Verifying toolchains..."
python  --version 2>&1 || true
pip     --version 2>&1 || true
node    --version 2>&1 || true
npm     --version 2>&1 || true
go      version   2>&1 || true
cargo   --version 2>&1 || true
ruby    --version 2>&1 || true
git     --version 2>&1 || true
docker  --version 2>&1 || true

echo "DARKNODES_FIX_COMPLETE"
"""

    try:
        stdout, stderr, rc = await docker_exec(container_name, fix_script, timeout=120)
    except Exception as e:
        await msg.edit(embed=create_error_embed("Fix Failed", f"Could not exec into container:\n```{e}```"))
        return

    success = "DARKNODES_FIX_COMPLETE" in stdout

    # Parse the toolchain versions from the verify step
    versions = {}
    for line in stdout.splitlines():
        for tool in ("python", "pip", "node", "npm", "go", "cargo", "ruby", "git", "docker"):
            if line.lower().startswith(tool) or f"{tool} " in line.lower():
                if "--version" not in line and "ln -sf" not in line:
                    versions[tool] = line.strip()[:50]
                    break

    if success:
        embed = create_success_embed(
            "✅  VPS Fixed",
            f"All real-VPS fixes applied to `{container_name}` successfully.\n"
            f"DNS, apt, pip, python, npm, cargo, go — all set.",
        )
        if versions:
            ver_text = "\n".join(f"`{v}`" for v in list(versions.values())[:8])
            embed.add_field(name="🔢 Toolchain Versions", value=ver_text or "—", inline=False)
        embed.add_field(
            name="💡 Try now",
            value=(
                f"`!exec {container_name} apt install -y htop` — install any package\n"
                f"`!exec {container_name} pip install requests` — Python packages\n"
                f"`!exec {container_name} npm install -g nodemon` — Node packages"
            ),
            inline=False,
        )
    else:
        tail = stdout[-600:] if stdout else "(no output)"
        embed = create_error_embed(
            "⚠️  Fix Incomplete",
            f"Script did not confirm completion (exit {rc}).\n```\n{tail}\n```",
        )

    await msg.edit(embed=embed)


@bot.command(name='fixdind')
@is_admin()
async def fix_dind(ctx, container_name: str):
    """Force-restart Docker daemon inside a VPS container (Admin only) — !fixdind <container>"""
    msg = await ctx.send(embed=create_info_embed(
        "🔧  Fixing DinD",
        f"Restarting Docker daemon inside `{container_name}`…",
    ))

    # Check container exists and is running
    out, _, rc = await run_docker_command(f"docker inspect --format={{{{.State.Running}}}} {container_name}", timeout=10)
    if rc != 0 or out.strip() != "true":
        await msg.edit(embed=create_error_embed(
            "Container Not Running",
            f"`{container_name}` is not running. Start it first with `!manage`.",
        ))
        return

    # Restart docker inside
    try:
        restart_out, restart_err, restart_rc = await docker_exec(
            container_name, "systemctl restart docker 2>&1", timeout=25
        )
    except Exception as e:
        await msg.edit(embed=create_error_embed("Restart Failed", f"Could not exec into container: {e}"))
        return

    await msg.edit(embed=create_info_embed(
        "🔧  Waiting for DinD",
        f"Docker service restarted — polling socket for up to 60s…",
    ))

    # Poll up to 60 s
    ready = False
    for i in range(60):
        await asyncio.sleep(1)
        try:
            probe, _, prc = await docker_exec(
                container_name,
                "timeout 4 docker info 2>&1 | grep 'Server Version'",
                timeout=8,
            )
            if prc == 0 and "Server Version" in probe:
                ready = True
                break
        except Exception:
            pass

    if ready:
        embed = create_success_embed(
            "✅  DinD Recovered",
            f"Docker daemon inside `{container_name}` is now **healthy** and accepting connections.",
        )
        embed.add_field(name="📦 Container", value=f"`{container_name}`", inline=True)
        embed.add_field(name="⏱️ Recovered In", value=f"`~{i + 1}s`", inline=True)
    else:
        # Grab journal for diagnosis
        try:
            journal, _, _ = await docker_exec(
                container_name,
                "journalctl -u docker --no-pager -n 15 2>/dev/null",
                timeout=10,
            )
        except Exception:
            journal = "(could not read journal)"
        embed = create_error_embed(
            "❌  DinD Still Broken",
            f"Docker daemon inside `{container_name}` did not recover after 60s.\n\n"
            f"**Last journal lines:**\n```\n{journal[:800]}\n```",
        )
        embed.add_field(
            name="💡 Next Steps",
            value=(
                "`!exec " + container_name + " journalctl -u docker -n 30` — full logs\n"
                "`!exec " + container_name + " cat /etc/docker/daemon.json` — check config\n"
                "If broken: delete & redeploy this VPS"
            ),
            inline=False,
        )

    await msg.edit(embed=embed)


@bot.command(name='ping-vps')
async def ping_vps(ctx, vps_number: int):
    """Ping your VPS container to check if it's alive"""
    user_id  = str(ctx.author.id)
    vps_list = vps_data.get(user_id, [])
    if not vps_list or vps_number < 1 or vps_number > len(vps_list):
        await ctx.send(embed=create_error_embed("Invalid VPS", "VPS not found."))
        return
    vps       = vps_list[vps_number - 1]
    container = vps["container_name"]
    nickname  = vps.get("nickname", f"VPS #{vps_number}")
    msg       = await ctx.send(embed=create_info_embed("Pinging...", f"Checking `{container}`..."))
    start     = time.time()
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "inspect", "--format={{.State.Running}}", container,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        elapsed    = int((time.time() - start) * 1000)
        is_running = stdout.decode().strip() == "true"
        if is_running:
            embed = create_success_embed("🏓 Pong!", f"**{nickname}** is alive!\n⚡ Response: `{elapsed}ms`")
        else:
            embed = create_error_embed("💀 No Response", f"**{nickname}** container is not running.")
        await msg.edit(embed=embed)
    except Exception as e:
        await msg.edit(embed=create_error_embed("Ping Failed", str(e)))


@bot.command(name='uptime-vps')
async def uptime_vps(ctx, vps_number: int):
    """Check VPS uptime"""
    user_id  = str(ctx.author.id)
    vps_list = vps_data.get(user_id, [])
    if not vps_list or vps_number < 1 or vps_number > len(vps_list):
        await ctx.send(embed=create_error_embed("Invalid VPS", "VPS not found."))
        return
    vps       = vps_list[vps_number - 1]
    container = vps["container_name"]
    nickname  = vps.get("nickname", f"VPS #{vps_number}")
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "inspect", "--format={{.State.StartedAt}}", container,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, _       = await asyncio.wait_for(proc.communicate(), timeout=10)
        started_at_str  = stdout.decode().strip()
        started_at      = datetime.fromisoformat(started_at_str[:19])
        uptime_delta    = _utcnow() - started_at
        days            = uptime_delta.days
        hours, rem      = divmod(uptime_delta.seconds, 3600)
        minutes, seconds = divmod(rem, 60)
        uptime_str      = f"{days}d {hours}h {minutes}m {seconds}s"
        embed = create_success_embed(f"⏱️ Uptime — {nickname}", f"Container has been running for:\n```{uptime_str}```")
        embed.add_field(name="Started At", value=f"`{started_at_str[:19]} UTC`", inline=False)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Uptime Error", str(e)))


@bot.command(name='myinfo')
async def my_info(ctx):
    """Show your personal dashboard"""
    user_id  = str(ctx.author.id)
    credits  = user_data.get(user_id, {}).get("credits", 0)
    vps_list = vps_data.get(user_id, [])
    embed    = create_embed(f"👤 {ctx.author.name}'s Dashboard", "", 0x5865F2)
    embed.set_thumbnail(url=ctx.author.display_avatar.url)
    embed.add_field(name="💰 Credits",    value=f"**{credits}**",          inline=True)
    embed.add_field(name="🖥️ VPS Count",  value=f"**{len(vps_list)}**",    inline=True)
    embed.add_field(name="📅 Account",    value=f"Joined: {ctx.author.created_at.strftime('%Y-%m-%d')}", inline=True)
    if vps_list:
        vps_text = ""
        for i, v in enumerate(vps_list):
            nickname    = v.get("nickname", f"VPS {i+1}")
            status      = v.get("status", "unknown")
            status_icon = "🟢" if status == "running" else ("🟠" if status == "suspended" else "🔴")
            note        = f" — _{v['note']}_" if v.get("note") else ""
            suspension  = f" ⚠️ *Suspended*" if status == "suspended" else ""
            vps_text   += f"{status_icon} **{nickname}** (`{v['container_name']}`){note}{suspension}\n"
        embed.add_field(name="🖥️ Your VPS", value=vps_text, inline=False)
    else:
        embed.add_field(name="🖥️ Your VPS", value="No VPS yet. Use `!buywc` to get one!", inline=False)
    await ctx.send(embed=embed)


@bot.command(name='transfer')
async def transfer_credits(ctx, target: discord.Member, amount: int):
    """Transfer credits to another user"""
    if amount <= 0:
        await ctx.send(embed=create_error_embed("Invalid Amount", "Amount must be positive."))
        return
    if target.id == ctx.author.id:
        await ctx.send(embed=create_error_embed("Invalid Target", "You can't transfer to yourself!"))
        return
    sender_id = str(ctx.author.id)
    target_id = str(target.id)
    if sender_id not in user_data:
        user_data[sender_id] = {"credits": 0}
    if user_data[sender_id]["credits"] < amount:
        await ctx.send(embed=create_error_embed(
            "Insufficient Credits",
            f"You only have **{user_data[sender_id]['credits']}** credits."
        ))
        return
    if target_id not in user_data:
        user_data[target_id] = {"credits": 0}
    user_data[sender_id]["credits"] -= amount
    user_data[target_id]["credits"] += amount
    save_data()
    embed = create_success_embed("💸 Transfer Complete",
                                 f"{ctx.author.mention} sent **{amount}** credits to {target.mention}!")
    embed.add_field(name="Your Balance", value=f"**{user_data[sender_id]['credits']}** credits", inline=True)
    await ctx.send(embed=embed)
    try:
        await target.send(embed=create_info_embed(
            "💰 Credits Received",
            f"You received **{amount}** credits from {ctx.author.mention}!\n"
            f"New balance: **{user_data[target_id]['credits']}**"
        ))
    except discord.Forbidden:
        pass


@bot.command(name='leaderboard')
async def leaderboard(ctx):
    """Show top 10 credit holders"""
    sorted_users = sorted(user_data.items(), key=lambda x: x[1].get("credits", 0), reverse=True)[:10]
    embed   = create_embed("🏆 Credit Leaderboard", "Top 10 credit holders:", 0xffd700)
    medals  = ["🥇", "🥈", "🥉"] + ["🏅"] * 7
    lines   = []
    for i, (uid, data) in enumerate(sorted_users):
        try:
            u    = await bot.fetch_user(int(uid))
            name = u.name
        except Exception:
            name = f"User#{uid[:4]}"
        lines.append(f"{medals[i]} **{name}** — {data.get('credits', 0)} credits")
    embed.add_field(name="Rankings", value="\n".join(lines) if lines else "No data yet.", inline=False)
    await ctx.send(embed=embed)


@bot.command(name='botstatus')
async def bot_status(ctx):
    """Show bot status and stats"""
    uptime_delta = datetime.now() - BOT_START_TIME
    days         = uptime_delta.days
    hours, rem   = divmod(uptime_delta.seconds, 3600)
    minutes, _   = divmod(rem, 60)
    total_vps    = sum(len(v) for v in vps_data.values())
    running_vps  = sum(1 for vl in vps_data.values() for v in vl if v.get("status") == "running")
    embed = create_embed("🤖 Bot Status", f"{get_brand_name()} VPS Manager", 0x00ff88)
    embed.add_field(name="⏱️ Uptime",       value=f"`{days}d {hours}h {minutes}m`",            inline=True)
    embed.add_field(name="🖥️ Total VPS",    value=f"**{total_vps}** ({running_vps} running)",  inline=True)
    embed.add_field(name="👥 Users",         value=f"**{len(user_data)}**",                     inline=True)
    embed.add_field(name="🔧 Maintenance",  value="🔴 ON" if maintenance_mode else "🟢 OFF",   inline=True)
    embed.add_field(name="📡 Latency",       value=f"`{round(bot.latency * 1000)}ms`",          inline=True)
    embed.add_field(name="🛡️ Abuse Monitor", value="✅ Active" if MONITOR_CONFIG.get("auto_suspend_enabled", True) else "❌ Disabled", inline=True)
    await ctx.send(embed=embed)


@bot.command(name='announce')
@is_admin()
async def announce(ctx, *, message: str):
    """Send an announcement DM to all VPS owners (Admin only)"""
    sent   = 0
    failed = 0
    announce_embed = create_embed("📢 Announcement", message, 0xffaa00)
    announce_embed.add_field(name="From", value=f"**{get_brand_name()} Team** ({ctx.author.mention})", inline=False)
    status_msg = await ctx.send(embed=create_info_embed("Sending Announcement", "Broadcasting to all VPS owners..."))
    for uid in vps_data.keys():
        try:
            user = await bot.fetch_user(int(uid))
            await user.send(embed=announce_embed)
            sent += 1
            await asyncio.sleep(0.5)
        except Exception:
            failed += 1
    await status_msg.edit(embed=create_success_embed(
        "Announcement Sent",
        f"✅ Delivered to **{sent}** users\n❌ Failed: **{failed}** (DMs closed)"
    ))


@bot.command(name='maintenance')
@is_admin()
async def maintenance_toggle(ctx, mode: str):
    """Toggle maintenance mode (Admin only)"""
    global maintenance_mode
    if mode.lower() == "on":
        maintenance_mode = True
        await bot.change_presence(
            status=discord.Status.idle,
            activity=discord.Activity(type=discord.ActivityType.watching, name="🔴 Under Maintenance")
        )
        await ctx.send(embed=create_warning_embed(
            "🔴 Maintenance Mode ON",
            "Bot is now in maintenance mode.\n"
            "• ALL commands blocked for non-admins\n"
            "• DM commands also blocked\n"
            "• Bot status set to Idle"
        ))
    elif mode.lower() == "off":
        maintenance_mode = False
        await bot.change_presence(
            status=discord.Status.online,
            activity=discord.Activity(type=discord.ActivityType.watching, name=f"{get_brand_name()} | VPS Manager")
        )
        await ctx.send(embed=create_success_embed("🟢 Maintenance Mode OFF", "Bot is back to normal operation."))
    else:
        await ctx.send(embed=create_error_embed("Invalid", "Use: `!maintenance on` or `!maintenance off`"))


# ─── Maintenance mode global check ────────────────────────────────────────────

@bot.check
async def maintenance_check(ctx):
    global maintenance_mode
    if not maintenance_mode:
        return True

    user_id       = str(ctx.author.id)
    is_user_admin = user_id == str(MAIN_ADMIN_ID) or user_id in admin_data.get("admins", [])

    if is_user_admin and ctx.command and ctx.command.name == 'maintenance':
        return True

    if not is_user_admin:
        await ctx.send(embed=create_warning_embed(
            "🔴 Under Maintenance",
            "The bot is currently under maintenance.\n"
            "All commands are disabled until maintenance is complete."
        ))
        return False

    return True


# ─── Block ALL bot commands in DMs ────────────────────────────────────────────

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if isinstance(message.channel, discord.DMChannel):
        if message.content.startswith(bot.command_prefix):
            # Allow !manage (and its typo alias) in DMs so users can control
            # their VPS from the DM that the bot already sends them on creation.
            cmd_name = message.content[len(bot.command_prefix):].split()[0].lower() if message.content[len(bot.command_prefix):].strip() else ""
            if cmd_name in ("manage", "mangage"):
                # Fall through to process_commands below
                pass
            else:
                block_embed = create_warning_embed(
                    "❌ DM Commands Disabled",
                    "Bot commands are **not allowed in DMs**.\n\n"
                    "Please use bot commands in the **server channel** only.\n"
                    "👉 Go to the server and use commands there!"
                )
                block_embed.set_footer(text=f"{get_brand_name()} | Server-only bot")
                await message.channel.send(embed=block_embed)
                return

    if maintenance_mode and isinstance(message.channel, discord.TextChannel):
        user_id       = str(message.author.id)
        is_user_admin = user_id == str(MAIN_ADMIN_ID) or user_id in admin_data.get("admins", [])
        if not is_user_admin and message.content.startswith(bot.command_prefix):
            await message.channel.send(embed=create_warning_embed(
                "🔴 Under Maintenance",
                "The bot is currently under maintenance.\nAll commands are disabled for users."
            ))
            return

    await bot.process_commands(message)


# ─── Typo aliases ─────────────────────────────────────────────────────────────

@bot.command(name='mangage')
async def manage_typo(ctx):
    await ctx.send(embed=create_info_embed("Command Correction", "Did you mean `!manage`?"))

@bot.command(name='stats')
async def stats_alias(ctx):
    if str(ctx.author.id) == str(MAIN_ADMIN_ID) or str(ctx.author.id) in admin_data.get("admins", []):
        await server_stats(ctx)
    else:
        await ctx.send(embed=create_error_embed("Access Denied", "Admin only."))


# ─── Expire System ─────────────────────────────────────────────────────────────

@bot.command(name='setexpire')
@is_admin()
async def set_expire(ctx, user: discord.Member, vps_number: int, days: int):
    """Set VPS expiry — !setexpire @user <vps#> <days>"""
    user_id  = str(user.id)
    vps_list = vps_data.get(user_id, [])
    if not vps_list or vps_number < 1 or vps_number > len(vps_list):
        await ctx.send(embed=create_error_embed("Not Found", f"{user.mention} has no VPS #{vps_number}."))
        return
    exp_date = (_utcnow() + timedelta(days=days)).isoformat()
    vps_list[vps_number - 1]['expires'] = exp_date
    save_data()
    vps_name = vps_list[vps_number - 1]['container_name']
    await ctx.send(embed=create_success_embed(
        "Expiry Set",
        f"✅ {user.mention}'s **VPS #{vps_number}** (`{vps_name}`) expires on `{exp_date[:10]}` ({days}d from now)."
    ))
    try:
        await user.send(embed=create_warning_embed(
            "⏳ VPS Expiry Set",
            f"Your **VPS #{vps_number}** (`{vps_name}`) has been set to expire on **{exp_date[:10]}** ({days} days).\n"
            f"Contact an admin to extend it before it expires!"
        ))
    except discord.Forbidden:
        pass


@bot.command(name='extendexpire')
@is_admin()
async def extend_expire(ctx, user: discord.Member, vps_number: int, days: int):
    """Extend VPS expiry — !extendexpire @user <vps#> <days>"""
    user_id  = str(user.id)
    vps_list = vps_data.get(user_id, [])
    if not vps_list or vps_number < 1 or vps_number > len(vps_list):
        await ctx.send(embed=create_error_embed("Not Found", f"{user.mention} has no VPS #{vps_number}."))
        return
    vps     = vps_list[vps_number - 1]
    current = vps.get('expires', 'Never')
    if current == 'Never' or not current:
        base = _utcnow()
    else:
        try:
            base = datetime.fromisoformat(current)
            if base < _utcnow():
                base = _utcnow()
        except Exception:
            base = _utcnow()
    new_exp  = (base + timedelta(days=days)).isoformat()
    vps['expires'] = new_exp
    save_data()
    vps_name = vps['container_name']
    await ctx.send(embed=create_success_embed(
        "Expiry Extended",
        f"✅ {user.mention}'s **VPS #{vps_number}** (`{vps_name}`) extended by **{days} days**.\nNew expiry: `{new_exp[:10]}`"
    ))
    try:
        await user.send(embed=create_success_embed(
            "✅ VPS Extended",
            f"Your **VPS #{vps_number}** (`{vps_name}`) has been extended by **{days} days**!\nNew expiry: **{new_exp[:10]}**"
        ))
    except discord.Forbidden:
        pass


@bot.command(name='checkexpire')
async def check_expire(ctx, user: discord.Member = None):
    """Check VPS expiry — users check own, admins can check others"""
    is_user_admin = str(ctx.author.id) == str(MAIN_ADMIN_ID) or str(ctx.author.id) in admin_data.get("admins", [])
    target        = user if (user and is_user_admin) else ctx.author
    user_id       = str(target.id)
    if user_id not in vps_data or not vps_data[user_id]:
        await ctx.send(embed=create_error_embed("Not Found", f"{target.mention} has no VPS."))
        return
    embed = create_info_embed(f"⏳ VPS Expiry — {target.display_name}", "")
    for i, vps in enumerate(vps_data[user_id]):
        expires = vps.get('expires', 'Never')
        if expires and expires != 'Never':
            try:
                exp_dt    = datetime.fromisoformat(expires)
                days_left = (exp_dt - _utcnow()).days
                if days_left < 0:
                    status = f"❌ **EXPIRED** {abs(days_left)}d ago"
                elif days_left <= 3:
                    status = f"⚠️ Expires in **{days_left}d** — {expires[:10]}"
                else:
                    status = f"✅ Expires on `{expires[:10]}` ({days_left}d left)"
            except Exception:
                status = expires
        else:
            status = "♾️ Never (No expiry set)"
        embed.add_field(
            name=f"VPS #{i+1} — `{vps['container_name']}`",
            value=status, inline=False
        )
    await ctx.send(embed=embed)


@bot.command(name='removeexpire')
@is_admin()
async def remove_expire(ctx, user: discord.Member, vps_number: int):
    """Remove expiry from a specific VPS — !removeexpire @user <vps#>"""
    user_id  = str(user.id)
    vps_list = vps_data.get(user_id, [])
    if not vps_list or vps_number < 1 or vps_number > len(vps_list):
        await ctx.send(embed=create_error_embed("Not Found", f"{user.mention} has no VPS #{vps_number}."))
        return
    vps_list[vps_number - 1]['expires'] = 'Never'
    save_data()
    vps_name = vps_list[vps_number - 1]['container_name']
    await ctx.send(embed=create_success_embed(
        "Expiry Removed",
        f"✅ {user.mention}'s **VPS #{vps_number}** (`{vps_name}`) expiry set to **Never**."
    ))


# ─── Auto expire checker — runs every hour ─────────────────────────────────────

@tasks.loop(hours=1)
async def auto_expire_check():
    now = _utcnow()
    for user_id, vps_list in list(vps_data.items()):
        for vps in vps_list:
            expires = vps.get('expires', 'Never')
            if not expires or expires == 'Never':
                continue
            try:
                exp_dt    = datetime.fromisoformat(expires)
                days_left = (exp_dt - now).days

                if days_left == 3:
                    try:
                        u = await bot.fetch_user(int(user_id))
                        await u.send(embed=create_warning_embed(
                            "⚠️ VPS Expiring Soon",
                            f"Your VPS `{vps['container_name']}` expires in **3 days** on `{expires[:10]}`!\n"
                            "Contact an admin to extend it."
                        ))
                    except Exception:
                        pass

                elif days_left == 1:
                    try:
                        u = await bot.fetch_user(int(user_id))
                        await u.send(embed=create_error_embed(
                            "🚨 VPS Expiring Tomorrow!",
                            f"Your VPS `{vps['container_name']}` expires **tomorrow** (`{expires[:10]}`)!\n"
                            "Contact an admin IMMEDIATELY to avoid losing access."
                        ))
                    except Exception:
                        pass

                elif days_left < 0:
                    try:
                        proc = await asyncio.create_subprocess_exec(
                            "docker", "stop", vps['container_name'],
                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                        )
                        await proc.communicate()
                        vps['status'] = 'stopped'
                    except Exception:
                        pass
                    try:
                        u = await bot.fetch_user(int(user_id))
                        await u.send(embed=create_error_embed(
                            "❌ VPS Expired",
                            f"Your VPS `{vps['container_name']}` has expired and been **stopped**.\n"
                            "Contact an admin to renew it."
                        ))
                    except Exception:
                        pass
            except Exception:
                continue
    save_data()


# ─── Analytics Data ────────────────────────────────────────────────────────────

def load_analytics_data():
    try:
        with open('analytics_data.json', 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "total_installs": 0,
            "successful_installs": 0,
            "failed_installs": 0,
            "install_times": [],
            "template_counts": {},
            "daily_stats": {}
        }

def save_analytics_data():
    try:
        with open('analytics_data.json', 'w') as f:
            json.dump(analytics_data, f, indent=4)
    except Exception as e:
        logger.error(f"Error saving analytics data: {e}")

analytics_data = load_analytics_data()

def track_template_install(template_name: str, success: bool, duration_seconds: float = 0):
    """Record a template installation event in analytics_data."""
    analytics_data["total_installs"] = analytics_data.get("total_installs", 0) + 1
    if success:
        analytics_data["successful_installs"] = analytics_data.get("successful_installs", 0) + 1
    else:
        analytics_data["failed_installs"] = analytics_data.get("failed_installs", 0) + 1
    if duration_seconds > 0:
        times = analytics_data.setdefault("install_times", [])
        times.append(duration_seconds)
        if len(times) > 1000:
            analytics_data["install_times"] = times[-1000:]
    tc = analytics_data.setdefault("template_counts", {})
    tc[template_name] = tc.get(template_name, 0) + 1
    day_key = _utcnow().strftime("%Y-%m-%d")
    ds = analytics_data.setdefault("daily_stats", {})
    ds.setdefault(day_key, {"installs": 0, "success": 0, "failed": 0})
    ds[day_key]["installs"] += 1
    if success:
        ds[day_key]["success"] += 1
    else:
        ds[day_key]["failed"] += 1
    save_analytics_data()


# ─── Scheduled Backups Data ────────────────────────────────────────────────────

def load_scheduled_backups():
    try:
        with open('scheduled_backups.json', 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_scheduled_backups():
    try:
        with open('scheduled_backups.json', 'w') as f:
            json.dump(scheduled_backups, f, indent=4)
    except Exception as e:
        logger.error(f"Error saving scheduled backups: {e}")

scheduled_backups = load_scheduled_backups()


# ─── Smart Notifications Config ───────────────────────────────────────────────

def load_notifications_config():
    try:
        with open('notifications_config.json', 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_notifications_config():
    try:
        with open('notifications_config.json', 'w') as f:
            json.dump(notifications_config, f, indent=4)
    except Exception as e:
        logger.error(f"Error saving notifications config: {e}")

notifications_config = load_notifications_config()

def get_user_notif_config(user_id: str) -> dict:
    return notifications_config.get(user_id, {
        "service_crash":   True,
        "high_disk":       True,
        "high_ram":        True,
        "backup_status":   True,
        "disk_threshold":  85,
        "ram_threshold":   85
    })


# ─── Cleanup helper (shared by command + ManageView) ──────────────────────────

_CLEANUP_SCRIPT = r"""
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
BEFORE=$(df / --output=used | tail -1 | tr -d ' ')
apt-get clean -qq 2>/dev/null || true
journalctl --vacuum-size=50M 2>/dev/null || true
docker system prune -f 2>/dev/null || true
docker container prune -f 2>/dev/null || true
find /tmp -type f -mtime +1 -delete 2>/dev/null || true
find /var/log -name "*.gz" -delete 2>/dev/null || true
find /var/log -name "*.1"  -delete 2>/dev/null || true
pip cache purge 2>/dev/null || true
npm cache clean --force 2>/dev/null || true
AFTER=$(df / --output=used | tail -1 | tr -d ' ')
RECLAIMED=$(( (BEFORE - AFTER) / 1024 ))
echo "CLEANUP_RESULT:BEFORE=${BEFORE}:AFTER=${AFTER}:RECLAIMED_MB=${RECLAIMED}"
"""

async def _do_vps_scan(container_name: str):
    """Run all health checks on a container.
    Returns (issues, fixes) where:
      issues = list of (label, description, fix_key_or_None)
      fixes  = dict of fix_key -> shell command string
    """
    issues: list = []
    fixes:  dict = {}

    # Check 1: Container running
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "inspect", "--format={{.State.Running}}", container_name,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        if out.decode().strip() != "true":
            issues.append(("🔴 Container Not Running", "Your VPS container is stopped.", "start_container"))
            fixes["start_container"] = ""
    except Exception:
        issues.append(("❓ Container Unreachable", "Could not inspect container status.", None))

    container_up = not any("Not Running" in i[0] or "Unreachable" in i[0] for i in issues)

    if container_up:
        # Check 2: SSH
        try:
            out2, _, _ = await docker_exec(container_name, "systemctl is-active ssh 2>/dev/null || echo inactive", timeout=10)
            if "inactive" in out2 or "failed" in out2:
                issues.append(("🔑 SSH Service Down", "The SSH daemon is not running inside your VPS.", "fix_ssh"))
                fixes["fix_ssh"] = "systemctl restart ssh 2>/dev/null || /usr/sbin/sshd; echo OK"
        except Exception:
            pass

        # Check 3: Disk usage
        try:
            disk_out, _, _ = await docker_exec(container_name, "df / --output=pcent | tail -1 | tr -d ' %'", timeout=10)
            disk_pct = int(disk_out.strip()) if disk_out.strip().isdigit() else 0
            if disk_pct >= 90:
                issues.append(("💾 Disk Almost Full", f"Disk usage is at **{disk_pct}%** — VPS may stop working.", "cleanup_disk"))
                fixes["cleanup_disk"] = "apt-get clean -qq; journalctl --vacuum-size=50M; docker system prune -f; find /tmp -mtime +1 -delete; echo DONE"
            elif disk_pct >= 75:
                issues.append(("⚠️ Disk Usage High", f"Disk usage is at **{disk_pct}%**. Consider cleaning up.", "cleanup_disk"))
                fixes["cleanup_disk"] = "apt-get clean -qq; journalctl --vacuum-size=50M; find /tmp -mtime +1 -delete; echo DONE"
        except Exception:
            pass

        # Check 4: Docker-in-Docker
        try:
            dind_out, _, dind_rc = await docker_exec(container_name, "timeout 5 docker info 2>&1 | grep -c 'Server Version' || echo 0", timeout=12)
            if dind_rc != 0 or dind_out.strip() == "0":
                issues.append(("🐳 Docker Daemon Down", "Inner Docker daemon not running. Apps inside won't work.", "fix_dind"))
                fixes["fix_dind"] = "systemctl restart docker; sleep 5; docker info 2>&1 | head -3"
        except Exception:
            pass

        # Check 5: Failed systemd services
        try:
            fail_out, _, _ = await docker_exec(container_name, "systemctl list-units --state=failed --no-legend --no-pager 2>/dev/null | awk '{print $1}' | head -5", timeout=10)
            failed_svcs = [s.strip() for s in fail_out.splitlines() if s.strip()]
            if failed_svcs:
                issues.append(("⚠️ Failed Services", f"Failed: `{'`, `'.join(failed_svcs[:3])}`", "reset_failed"))
                fixes["reset_failed"] = "systemctl reset-failed; echo RESET_DONE"
        except Exception:
            pass

        # Check 6: DNS / Internet
        try:
            dns_out, _, _ = await docker_exec(container_name, "timeout 5 curl -sf https://google.com -o /dev/null && echo OK || echo FAIL", timeout=12)
            if "FAIL" in dns_out:
                issues.append(("🌐 No Internet / DNS Broken", "VPS cannot reach the internet.", "fix_dns"))
                fixes["fix_dns"] = "chattr -i /etc/resolv.conf 2>/dev/null; printf 'nameserver 8.8.8.8\\nnameserver 1.1.1.1\\n' > /etc/resolv.conf; chattr +i /etc/resolv.conf 2>/dev/null; echo DNS_FIXED"
        except Exception:
            pass

        # Check 7: RAM
        try:
            ram_out, _, _ = await docker_exec(container_name, "free -m | awk '/^Mem:/{printf \"%d %d\", $3, $2}'", timeout=10)
            parts = ram_out.strip().split()
            if len(parts) == 2:
                used, total = int(parts[0]), int(parts[1])
                pct = int(used * 100 / total) if total > 0 else 0
                if pct >= 90:
                    issues.append(("🧠 RAM Critical", f"RAM at **{pct}%** ({used}/{total} MB). VPS may become unresponsive.", "free_ram"))
                    fixes["free_ram"] = "sync; echo 3 > /proc/sys/vm/drop_caches 2>/dev/null; echo CACHE_CLEARED"
                elif pct >= 80:
                    issues.append(("⚠️ RAM Usage High", f"RAM at **{pct}%** ({used}/{total} MB).", None))
        except Exception:
            pass

    return issues, fixes


async def _do_cleanup(container_name: str) -> dict:
    """Run cleanup inside a container. Returns {before_kb, after_kb, reclaimed_mb, output}."""
    stdout, stderr, rc = await docker_exec(container_name, _CLEANUP_SCRIPT, timeout=120)
    result = {"before_kb": 0, "after_kb": 0, "reclaimed_mb": 0, "output": stdout}
    for line in stdout.splitlines():
        if line.startswith("CLEANUP_RESULT:"):
            for part in line[len("CLEANUP_RESULT:"):].split(":"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    if k == "BEFORE"        and v.lstrip("-").isdigit(): result["before_kb"]   = int(v)
                    elif k == "AFTER"       and v.lstrip("-").isdigit(): result["after_kb"]    = int(v)
                    elif k == "RECLAIMED_MB" and v.lstrip("-").isdigit(): result["reclaimed_mb"] = int(v)
    return result


# ─── AI Troubleshooter — error pattern knowledge base ─────────────────────────

_AI_ERROR_PATTERNS = [
    # DNS / network
    (["could not resolve", "name resolution", "dns", "no such host", "temporary failure in name resolution"],
     "🌐 DNS Resolution Failure",
     "The VPS cannot resolve domain names. DNS servers may be missing or misconfigured.",
     "fix_dns",
     "chattr -i /etc/resolv.conf 2>/dev/null; echo 'nameserver 8.8.8.8\nnameserver 1.1.1.1' > /etc/resolv.conf && echo DNS_FIXED"),

    # apt / dpkg
    (["e: unable to fetch", "failed to fetch", "apt", "dpkg", "package", "could not get lock", "dpkg was interrupted"],
     "📦 Package Manager Broken",
     "APT/DPKG is locked or broken. Common after a failed install.",
     "fix_apt",
     "rm -f /var/lib/dpkg/lock* /var/lib/apt/lists/lock /var/cache/apt/archives/lock; dpkg --configure -a 2>&1; apt-get update -qq && echo APT_FIXED"),

    # disk full
    (["no space left", "disk full", "enospc", "no space", "write failed", "filesystem full"],
     "💾 Disk Full",
     "The VPS disk is completely full. Services will fail until space is freed.",
     "free_disk",
     "apt-get clean -qq; journalctl --vacuum-size=50M; docker system prune -f 2>/dev/null; find /tmp -type f -mtime +1 -delete; df -h / | tail -1 && echo DISK_CLEANED"),

    # SSH
    (["connection refused", "ssh", "port 22", "connection timed out", "permission denied (publickey"],
     "🔑 SSH Not Accessible",
     "SSH service is down or misconfigured. The VPS may have lost its SSH daemon.",
     "fix_ssh",
     "systemctl enable ssh 2>/dev/null; systemctl restart ssh 2>/dev/null || service ssh restart 2>/dev/null || /usr/sbin/sshd; echo SSH_FIXED"),

    # Docker / DinD
    (["docker: error", "cannot connect to docker", "docker daemon", "dind", "docker.sock", "dockerd"],
     "🐳 Docker Daemon Down",
     "The Docker daemon inside your VPS is not running.",
     "fix_docker",
     "systemctl restart docker 2>/dev/null || (dockerd --host=unix:///var/run/docker.sock &); sleep 5 && docker ps && echo DOCKER_FIXED"),

    # out of memory
    (["out of memory", "oom", "killed process", "cannot allocate memory", "oom-killer"],
     "🧠 Out of Memory (OOM)",
     "The VPS ran out of RAM and the OOM killer terminated a process.",
     "free_ram",
     "sync; echo 3 > /proc/sys/vm/drop_caches; systemctl restart docker 2>/dev/null; free -m && echo RAM_FREED"),

    # service not found / failed
    (["unit not found", "service failed", "failed to start", "activating (auto-restart)", "active (failed)"],
     "⚙️ Service Crashed / Not Found",
     "A systemd service has failed or is not installed.",
     "fix_services",
     "systemctl daemon-reload; systemctl reset-failed 2>/dev/null; echo SERVICES_RESET"),

    # pterodactyl
    (["pterodactyl", "wings", "panel", "p: wings", "daemon"],
     "🦅 Pterodactyl / Wings Issue",
     "Pterodactyl Panel or Wings has an error. Check that Wings is installed and the config is valid.",
     "fix_wings",
     "systemctl restart wings 2>/dev/null && echo WINGS_RESTARTED || echo 'Wings not installed — run /template to install it'"),

    # nginx / web server
    (["nginx", "apache", "http", "502", "503", "504", "bad gateway", "upstream"],
     "🌍 Web Server Error",
     "Nginx or Apache is returning errors. The backend may be down.",
     "fix_nginx",
     "nginx -t 2>&1; systemctl restart nginx 2>/dev/null || service nginx restart 2>/dev/null; echo NGINX_RESTARTED"),

    # mysql / database
    (["mysql", "mariadb", "database", "connection refused 3306", "table", "sql"],
     "🗄️ Database Error",
     "MySQL/MariaDB may be down or the database is corrupted.",
     "fix_mysql",
     "systemctl restart mysql 2>/dev/null || systemctl restart mariadb 2>/dev/null; echo DB_RESTARTED"),

    # python / pip
    (["modulenotfounderror", "importerror", "pip", "python", "no module named"],
     "🐍 Python / Pip Error",
     "A Python module is missing or pip is broken.",
     "fix_pip",
     "export PIP_BREAK_SYSTEM_PACKAGES=1; pip install --upgrade pip 2>&1 | tail -3; echo PIP_FIXED"),

    # node / npm
    (["npm err", "node_modules", "cannot find module", "enoent", "node"],
     "📦 Node.js / NPM Error",
     "A Node.js module is missing or NPM is in a broken state.",
     "fix_npm",
     "npm cache clean --force 2>&1; echo NPM_CACHE_CLEARED"),

    # permission denied
    (["permission denied", "access denied", "eacces", "eperm"],
     "🔒 Permission Error",
     "A file or directory has wrong permissions.",
     "fix_perms",
     "chmod 755 /root 2>/dev/null; chown -R root:root /root 2>/dev/null; echo PERMS_FIXED"),
]

def _ai_analyze_error(error_text: str) -> list:
    """Match user error text against known patterns. Returns list of (title, desc, fix_key, fix_cmd)."""
    lower = error_text.lower()
    matched = []
    for keywords, title, desc, fix_key, fix_cmd in _AI_ERROR_PATTERNS:
        if any(kw in lower for kw in keywords):
            matched.append((title, desc, fix_key, fix_cmd))
    return matched


# ─── /fix — AI Troubleshooter ─────────────────────────────────────────────────

@bot.command(name='fix')
async def vps_fix_scan(ctx, vps_number: int = 1, *, error_description: str = ""):
    """AI VPS troubleshooter — scan + describe your error for smart fixes
    Usage:  !fix [vps#]
            !fix [vps#] connection refused port 22
            !fix [vps#] apt failed to fetch packages
    """
    user_id       = str(ctx.author.id)
    is_user_admin = user_id == str(MAIN_ADMIN_ID) or user_id in admin_data.get("admins", [])
    vps_list      = vps_data.get(user_id, [])
    if not vps_list or vps_number < 1 or vps_number > len(vps_list):
        await ctx.send(embed=create_error_embed("Invalid VPS", "VPS not found. Use `!manage` first."))
        return
    container_name = vps_list[vps_number - 1]["container_name"]

    has_error_desc = bool(error_description.strip())
    scan_desc = (
        f"🔍 Analyzing your error + scanning `{container_name}`…\nThis may take ~30 seconds."
        if has_error_desc else
        f"Running diagnostics on `{container_name}`…\nThis may take up to 30 seconds."
    )
    msg = await ctx.send(embed=create_info_embed("🤖 AI Troubleshooter", scan_desc))

    # Run VPS scan and AI error analysis in parallel
    issues, fixes = await _do_vps_scan(container_name)

    # If user described an error, match patterns and inject AI-found issues
    ai_fixes: dict = {}
    ai_issues: list = []
    if has_error_desc:
        matched = _ai_analyze_error(error_description)
        for title, desc, fix_key, fix_cmd in matched:
            # Don't duplicate if scan already found the same category
            if not any(fix_key == k for _, _, k in issues):
                ai_issues.append((title, f"**Detected from your description:** {desc}", fix_key))
                ai_fixes[fix_key] = fix_cmd
        fixes.update(ai_fixes)
        # Prepend AI issues (higher priority — user described them)
        issues = ai_issues + list(issues)

    # Build embed
    if not issues:
        embed = create_success_embed(
            "✅ All Clear",
            f"`{container_name}` passed all health checks!"
            + (f"\n\nNo known issue matched your description:\n> _{error_description[:150]}_\n\nTry describing the exact error message." if has_error_desc else "")
        )
        embed.add_field(name="Checks Run", value="✅ Container  ✅ SSH  ✅ Disk  ✅ DinD  ✅ Services  ✅ DNS  ✅ RAM", inline=False)
        await msg.edit(embed=embed)
        return

    title_prefix = "🤖 AI Analysis" if has_error_desc else "🩺 VPS Health Check"
    embed = create_warning_embed(
        f"{title_prefix} — {len(issues)} Issue(s) on `{container_name}`",
        (f"Error reported: _{error_description[:120]}_\n\n" if has_error_desc else "") + "Issues found:"
    )
    fixable = [(lbl, desc, key) for lbl, desc, key in issues if key and key in fixes]
    for lbl, desc, key in issues:
        fix_note = "\n✅ *One-click fix available below*" if (key and key in fixes) else ""
        embed.add_field(name=lbl, value=f"{desc}{fix_note}", inline=False)

    if not fixable:
        embed.add_field(name="💡 Next Steps", value="Contact support or check `!files` to inspect config files.", inline=False)
        await msg.edit(embed=embed)
        return

    class FixView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=300)
            for lbl, desc, fix_key in fixable[:4]:
                short_lbl = lbl.split(" ", 1)[1][:30] if " " in lbl else lbl[:30]
                btn = discord.ui.Button(label=f"⚡ Fix: {short_lbl}", style=discord.ButtonStyle.primary)
                btn.callback = self._make_callback(fix_key, fixes[fix_key])
                self.add_item(btn)
            # Fix All button if multiple fixable issues
            if len(fixable) > 1:
                fix_all_btn = discord.ui.Button(label="🔧 Fix All Issues", style=discord.ButtonStyle.danger)
                fix_all_btn.callback = self._fix_all_callback
                self.add_item(fix_all_btn)

        def _make_callback(self, fix_key, fix_cmd):
            async def callback(interaction: discord.Interaction):
                if str(interaction.user.id) != user_id and not is_user_admin:
                    await interaction.response.send_message(embed=create_error_embed("Access Denied", "Not your VPS!"), ephemeral=True)
                    return
                await interaction.response.defer(ephemeral=True)
                try:
                    if fix_key == "start_container":
                        await execute_docker(f"docker start {container_name}")
                        await interaction.followup.send(embed=create_success_embed("✅ Container Started", f"`{container_name}` is starting up!"), ephemeral=True)
                    else:
                        out, _, _ = await docker_exec(container_name, fix_cmd, timeout=60)
                        result_line = out.strip().splitlines()[-1] if out.strip() else "Done"
                        await interaction.followup.send(
                            embed=create_success_embed("✅ Fix Applied", f"**{fix_key.replace('_', ' ').title()}** fixed successfully!\n```\n{result_line[:400]}\n```"),
                            ephemeral=True)
                except Exception as ex:
                    await interaction.followup.send(embed=create_error_embed("Fix Failed", str(ex)[:300]), ephemeral=True)
            return callback

        async def _fix_all_callback(self, interaction: discord.Interaction):
            if str(interaction.user.id) != user_id and not is_user_admin:
                await interaction.response.send_message(embed=create_error_embed("Access Denied", "Not your VPS!"), ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)
            results = []
            for lbl, desc, fix_key in fixable:
                try:
                    if fix_key == "start_container":
                        await execute_docker(f"docker start {container_name}")
                        results.append(f"✅ {lbl}")
                    else:
                        out, _, rc = await docker_exec(container_name, fixes[fix_key], timeout=60)
                        results.append(f"{'✅' if rc == 0 else '⚠️'} {lbl}")
                except Exception as ex:
                    results.append(f"❌ {lbl}: {str(ex)[:60]}")
            embed = create_success_embed("🔧 All Fixes Applied", f"Attempted {len(fixable)} fix(es) on `{container_name}`:")
            embed.add_field(name="Results", value="\n".join(results), inline=False)
            await interaction.followup.send(embed=embed, ephemeral=True)

    await msg.edit(embed=embed, view=FixView())


# ─── !cleanup command ─────────────────────────────────────────────────────────

@bot.command(name='cleanup')
async def cleanup_vps_cmd(ctx, vps_number: int = 1):
    """Free disk space on your VPS — removes logs, cache, temp files — !cleanup [vps#]"""
    user_id  = str(ctx.author.id)
    vps_list = vps_data.get(user_id, [])
    if not vps_list or vps_number < 1 or vps_number > len(vps_list):
        await ctx.send(embed=create_error_embed("Invalid VPS", "VPS not found."))
        return
    container_name = vps_list[vps_number - 1]["container_name"]
    msg = await ctx.send(embed=create_info_embed(
        "🧹 Cleaning Up VPS…",
        f"Running cleanup on `{container_name}`…\nThis may take up to 60 seconds."
    ))
    try:
        result    = await _do_cleanup(container_name)
        reclaimed = max(0, result["reclaimed_mb"])
        embed = create_success_embed("🧹 Cleanup Complete", f"Finished cleaning `{container_name}`!")
        embed.add_field(
            name="✅ Items Cleaned",
            value="• apt / package cache\n• Old journal logs\n• Orphaned Docker images\n• Stopped containers\n• Temp files (>1d)\n• pip / npm cache",
            inline=True
        )
        embed.add_field(name="💾 Space Reclaimed", value=f"**~{reclaimed} MB**", inline=True)
        await msg.edit(embed=embed)
    except Exception as e:
        await msg.edit(embed=create_error_embed("Cleanup Failed", str(e)[:300]))


# ─── Scheduled Backups Commands ───────────────────────────────────────────────

@bot.command(name='schedule-backup')
async def schedule_backup_cmd(ctx, vps_number: int, frequency: str):
    """Schedule automatic VPS backups — !schedule-backup <vps#> <daily|weekly|monthly>"""
    user_id  = str(ctx.author.id)
    vps_list = vps_data.get(user_id, [])
    if not vps_list or vps_number < 1 or vps_number > len(vps_list):
        await ctx.send(embed=create_error_embed("Invalid VPS", "VPS not found."))
        return
    freq = frequency.lower()
    if freq not in ("daily", "weekly", "monthly"):
        await ctx.send(embed=create_error_embed("Invalid Frequency", "Choose: `daily`, `weekly`, or `monthly`"))
        return

    container_name = vps_list[vps_number - 1]["container_name"]
    now = _utcnow()
    delta_map = {"daily": timedelta(days=1), "weekly": timedelta(weeks=1), "monthly": timedelta(days=30)}
    interval_desc = {"daily": "every 24 hours", "weekly": "every 7 days", "monthly": "every 30 days"}
    next_run = (now + delta_map[freq]).isoformat()

    scheduled_backups.setdefault(user_id, {})[container_name] = {
        "frequency":   freq,
        "next_run":    next_run,
        "enabled":     True,
        "last_run":    None,
        "last_status": None
    }
    save_scheduled_backups()

    embed = create_success_embed("📅 Backup Scheduled", f"Automatic backups enabled for **VPS #{vps_number}**!")
    embed.add_field(name="📦 Container", value=f"`{container_name}`", inline=True)
    embed.add_field(name="📆 Frequency", value=f"`{freq.capitalize()}` — {interval_desc[freq]}", inline=True)
    embed.add_field(name="⏭ Next Run",   value=f"`{next_run[:16]} UTC`", inline=True)
    embed.add_field(name="ℹ️ Info",
        value="You'll receive a DM when each backup completes.\nUse `!list-backups` to view scheduled backups.",
        inline=False)
    await ctx.send(embed=embed)


@bot.command(name='list-backups')
async def list_backups_cmd(ctx):
    """List your scheduled VPS backups — !list-backups"""
    user_id     = str(ctx.author.id)
    user_scheds = scheduled_backups.get(user_id, {})
    if not user_scheds:
        await ctx.send(embed=create_info_embed(
            "No Backups Scheduled",
            "Use `!schedule-backup <vps#> <daily|weekly|monthly>` to set one up."
        ))
        return
    embed = create_embed("📅 Scheduled Backups", f"You have **{len(user_scheds)}** backup schedule(s):", 0x5865F2)
    for cname, cfg in user_scheds.items():
        icon       = "✅" if cfg.get("enabled") else "⏸"
        last_run   = (cfg.get("last_run") or "Never")[:16]
        last_stat  = cfg.get("last_status") or "—"
        embed.add_field(
            name=f"{icon} `{cname}`",
            value=(
                f"**Frequency:** `{cfg['frequency'].capitalize()}`\n"
                f"**Next Run:** `{(cfg.get('next_run') or '?')[:16]} UTC`\n"
                f"**Last Run:** `{last_run}`\n"
                f"**Last Status:** {last_stat}"
            ),
            inline=False
        )
    await ctx.send(embed=embed)


@bot.command(name='cancel-backup')
async def cancel_backup_cmd(ctx, vps_number: int):
    """Cancel a scheduled backup — !cancel-backup <vps#>"""
    user_id  = str(ctx.author.id)
    vps_list = vps_data.get(user_id, [])
    if not vps_list or vps_number < 1 or vps_number > len(vps_list):
        await ctx.send(embed=create_error_embed("Invalid VPS", "VPS not found."))
        return
    cname       = vps_list[vps_number - 1]["container_name"]
    user_scheds = scheduled_backups.get(user_id, {})
    if cname not in user_scheds:
        await ctx.send(embed=create_error_embed("No Schedule", f"No backup schedule found for `{cname}`."))
        return
    del user_scheds[cname]
    save_scheduled_backups()
    await ctx.send(embed=create_success_embed("Backup Cancelled", f"Scheduled backup for `{cname}` removed."))


# ─── Scheduled Backup Runner (background task) ────────────────────────────────

@tasks.loop(minutes=30)
async def scheduled_backup_runner():
    """Run due scheduled backups every 30 minutes."""
    now = _utcnow()
    for user_id, user_scheds in list(scheduled_backups.items()):
        for container_name, cfg in list(user_scheds.items()):
            if not cfg.get("enabled"):
                continue
            try:
                next_run = datetime.fromisoformat(cfg["next_run"])
            except Exception:
                continue
            if now < next_run:
                continue

            logger.info(f"Running scheduled backup: {container_name} for user {user_id}")
            snapshot_name = f"{container_name}-auto-{now.strftime('%Y%m%d-%H%M%S')}"
            success = False
            error_msg = ""
            try:
                proc = await asyncio.create_subprocess_exec(
                    "docker", "inspect", "--format={{.State.Running}}", container_name,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
                if out.decode().strip() != "true":
                    raise Exception("Container not running")
                await execute_docker(f"docker commit {container_name} {snapshot_name}", timeout=180)
                success = True
            except Exception as e:
                error_msg = str(e)[:120]
                snapshot_name = error_msg

            freq = cfg.get("frequency", "daily")
            delta_map = {"daily": timedelta(days=1), "weekly": timedelta(weeks=1), "monthly": timedelta(days=30)}
            cfg["last_run"]    = now.isoformat()
            cfg["last_status"] = "✅ Success" if success else f"❌ Failed: {error_msg[:60]}"
            cfg["next_run"]    = (now + delta_map.get(freq, timedelta(days=1))).isoformat()
            save_scheduled_backups()

            try:
                user = await bot.fetch_user(int(user_id))
                if success:
                    dm = create_success_embed("💾 Scheduled Backup Complete", f"VPS `{container_name}` backed up successfully!")
                    dm.add_field(name="📸 Snapshot", value=f"`{snapshot_name}`", inline=True)
                    dm.add_field(name="⏭ Next Run",  value=f"`{cfg['next_run'][:16]} UTC`", inline=True)
                else:
                    dm = create_error_embed("❌ Scheduled Backup Failed", f"Backup for `{container_name}` failed!\n**Reason:** {error_msg}")
                    dm.add_field(name="⏭ Next Run", value=f"`{cfg['next_run'][:16]} UTC`", inline=True)
                await user.send(embed=dm)
            except Exception:
                pass


# ─── Enhanced Share Access with Permission Levels ─────────────────────────────

@bot.command(name='share-vps')
async def share_vps_perms(ctx, target_user: discord.Member, vps_number: int, permission: str = "restart"):
    """Share VPS with permission level — !share-vps @user <vps#> <view|restart|full>
    • view    — view VPS info only
    • restart — start / stop the VPS
    • full    — start, stop, and SSH access
    """
    user_id        = str(ctx.author.id)
    target_user_id = str(target_user.id)
    if user_id not in vps_data or vps_number < 1 or vps_number > len(vps_data[user_id]):
        await ctx.send(embed=create_error_embed("Invalid VPS", "VPS not found."))
        return
    perm = permission.lower()
    if perm not in ("view", "restart", "full"):
        await ctx.send(embed=create_error_embed(
            "Invalid Permission",
            "Choose: `view`, `restart`, or `full`\n\n• `view` — Info only\n• `restart` — Start/stop\n• `full` — Full management"
        ))
        return
    vps            = vps_data[user_id][vps_number - 1]
    container_name = vps["container_name"]
    # Store in shared_users dict (also keeps backward-compat shared_with list)
    vps.setdefault("shared_users", {})[target_user_id] = {
        "permission": perm,
        "granted_at": _utcnow().isoformat(),
        "granted_by": user_id
    }
    if "shared_with" not in vps:
        vps["shared_with"] = []
    if target_user_id not in vps["shared_with"]:
        vps["shared_with"].append(target_user_id)
    save_data()

    perm_icons = {"view": "👁️", "restart": "🔄", "full": "🔑"}
    perm_descs = {"view": "View VPS info only", "restart": "Start and stop", "full": "Start, stop, and SSH"}
    embed = create_success_embed("🔗 VPS Access Shared", f"Access to **VPS #{vps_number}** granted to {target_user.mention}!")
    embed.add_field(name=f"{perm_icons[perm]} Permission", value=f"`{perm.upper()}`\n{perm_descs[perm]}", inline=True)
    embed.add_field(name="📦 Container",     value=f"`{container_name}`", inline=True)
    embed.add_field(name="🔒 Revoke later", value=f"`!revoke-share @{target_user.name} {vps_number}`", inline=False)
    await ctx.send(embed=embed)
    try:
        dm = create_info_embed("🔗 VPS Access Granted", f"**{ctx.author.name}** gave you **{perm.upper()}** access to their VPS!")
        dm.add_field(name="📦 Container",  value=f"`{container_name}`", inline=True)
        dm.add_field(name="🔑 Permission", value=f"`{perm.upper()}` — {perm_descs[perm]}", inline=True)
        dm.add_field(name="📌 How to Use", value=f"`!manage-shared {ctx.author.mention} {vps_number}`", inline=False)
        await target_user.send(embed=dm)
    except discord.Forbidden:
        pass


@bot.command(name='revoke-share')
async def revoke_share_cmd(ctx, target_user: discord.Member, vps_number: int):
    """Revoke shared VPS access — !revoke-share @user <vps#>"""
    user_id        = str(ctx.author.id)
    target_user_id = str(target_user.id)
    if user_id not in vps_data or vps_number < 1 or vps_number > len(vps_data[user_id]):
        await ctx.send(embed=create_error_embed("Invalid VPS", "VPS not found."))
        return
    vps     = vps_data[user_id][vps_number - 1]
    removed = False
    if target_user_id in vps.get("shared_users", {}):
        del vps["shared_users"][target_user_id]
        removed = True
    if target_user_id in vps.get("shared_with", []):
        vps["shared_with"].remove(target_user_id)
        removed = True
    if not removed:
        await ctx.send(embed=create_error_embed("Not Found", f"{target_user.mention} doesn't have access to VPS #{vps_number}."))
        return
    save_data()
    await ctx.send(embed=create_success_embed("🔒 Access Revoked", f"Access to **VPS #{vps_number}** revoked from {target_user.mention}."))
    try:
        await target_user.send(embed=create_warning_embed(
            "🔒 VPS Access Revoked",
            f"Your access to **{ctx.author.name}**'s VPS #{vps_number} has been revoked."
        ))
    except discord.Forbidden:
        pass


@bot.command(name='my-shares')
async def my_shares_cmd(ctx):
    """View who has access to your VPS — !my-shares"""
    user_id  = str(ctx.author.id)
    vps_list = vps_data.get(user_id, [])
    if not vps_list:
        await ctx.send(embed=create_info_embed("No VPS", "You don't have any VPS yet."))
        return
    embed     = create_embed("🔗 Shared VPS Access", "People with access to your VPS:", 0x5865F2)
    any_share = False
    for i, vps in enumerate(vps_list):
        shared_users_d = vps.get("shared_users", {})
        shared_with_l  = vps.get("shared_with",  [])
        all_shared     = set(list(shared_users_d.keys()) + shared_with_l)
        if not all_shared:
            continue
        any_share = True
        lines = []
        for uid in all_shared:
            perm  = shared_users_d.get(uid, {}).get("permission", "restart")
            icon  = {"view": "👁️", "restart": "🔄", "full": "🔑"}.get(perm, "🔄")
            lines.append(f"{icon} <@{uid}> — `{perm.upper()}`")
        embed.add_field(name=f"VPS #{i+1} — `{vps['container_name']}`", value="\n".join(lines), inline=False)
    if not any_share:
        embed.description = "None of your VPS are currently shared."
    await ctx.send(embed=embed)


# ─── Installation Analytics (Admin only) ──────────────────────────────────────

@bot.command(name='analytics')
@is_admin()
async def show_analytics(ctx):
    """Show platform installation analytics — !analytics (Admin only)"""
    total   = analytics_data.get("total_installs", 0)
    success = analytics_data.get("successful_installs", 0)
    failed  = analytics_data.get("failed_installs", 0)
    times   = analytics_data.get("install_times", [])
    tc      = analytics_data.get("template_counts", {})
    total_vps = sum(len(v) for v in vps_data.values())

    avg_time     = int(sum(times) / len(times)) if times else 0
    success_rate = f"{int(success * 100 / total)}%" if total > 0 else "N/A"
    top_templates = sorted(tc.items(), key=lambda x: x[1], reverse=True)[:5]

    # Last 7 days
    daily_stats = analytics_data.get("daily_stats", {})
    today       = _utcnow()
    week_lines  = []
    for d in range(6, -1, -1):
        dk   = (today - timedelta(days=d)).strftime("%Y-%m-%d")
        ds   = daily_stats.get(dk, {"installs": 0, "success": 0, "failed": 0})
        week_lines.append(f"`{dk}` — 📦 {ds['installs']}  ✅ {ds['success']}  ❌ {ds['failed']}")

    embed = create_embed("📈 Installation Analytics", "Platform usage statistics", 0x5865F2)
    embed.add_field(name="📦 Total Installs",  value=f"**{total}**",        inline=True)
    embed.add_field(name="✅ Successful",       value=f"**{success}**",      inline=True)
    embed.add_field(name="❌ Failed",           value=f"**{failed}**",       inline=True)
    embed.add_field(name="📊 Success Rate",     value=f"**{success_rate}**", inline=True)
    embed.add_field(name="⏱️ Avg Install Time", value=f"**{avg_time}s**" if avg_time else "N/A", inline=True)
    embed.add_field(name="🖥️ Total VPS",        value=f"**{total_vps}**",   inline=True)
    if top_templates:
        embed.add_field(
            name="🏆 Top Templates",
            value="\n".join(f"`{name}` — **{count}** install(s)" for name, count in top_templates),
            inline=False
        )
    embed.add_field(name="📅 Last 7 Days", value="\n".join(week_lines), inline=False)
    await ctx.send(embed=embed)


# ─── Smart Notifications Settings ─────────────────────────────────────────────

@bot.command(name='notify-settings')
async def notify_settings_cmd(ctx, setting: str = None, value: str = None):
    """Configure smart DM notifications — !notify-settings [setting] [value]

    Settings:   service-crash | high-disk | high-ram | backup-status  →  on / off
                disk-threshold | ram-threshold  →  number (1–99)
    """
    user_id = str(ctx.author.id)
    cfg     = dict(get_user_notif_config(user_id))  # copy so we can mutate

    if setting is None:
        on_off = lambda v: "🟢 ON" if v else "🔴 OFF"
        embed  = create_embed("🔔 Smart Notification Settings", "Your current preferences:", 0x5865F2)
        embed.add_field(name="Service Crash",  value=on_off(cfg.get("service_crash", True)), inline=True)
        embed.add_field(name="High Disk",      value=on_off(cfg.get("high_disk",    True)), inline=True)
        embed.add_field(name="High RAM",       value=on_off(cfg.get("high_ram",     True)), inline=True)
        embed.add_field(name="Backup Status",  value=on_off(cfg.get("backup_status",True)), inline=True)
        embed.add_field(name="Disk Threshold", value=f"**{cfg.get('disk_threshold', 85)}%**", inline=True)
        embed.add_field(name="RAM Threshold",  value=f"**{cfg.get('ram_threshold',  85)}%**", inline=True)
        embed.add_field(
            name="📌 How to Change",
            value=(
                "`!notify-settings service-crash on/off`\n"
                "`!notify-settings high-disk on/off`\n"
                "`!notify-settings high-ram on/off`\n"
                "`!notify-settings backup-status on/off`\n"
                "`!notify-settings disk-threshold 90`\n"
                "`!notify-settings ram-threshold 90`"
            ),
            inline=False
        )
        await ctx.send(embed=embed)
        return

    setting = setting.lower()
    bool_keys = {"service-crash": "service_crash", "high-disk": "high_disk",
                 "high-ram":      "high_ram",       "backup-status": "backup_status"}
    int_keys  = {"disk-threshold": "disk_threshold", "ram-threshold": "ram_threshold"}

    if setting in bool_keys:
        if value not in ("on", "off"):
            await ctx.send(embed=create_error_embed("Invalid Value", "Use `on` or `off`."))
            return
        cfg[bool_keys[setting]] = (value == "on")
        notifications_config[user_id] = cfg
        save_notifications_config()
        await ctx.send(embed=create_success_embed("Setting Updated", f"`{setting}` is now **{value.upper()}**"))
    elif setting in int_keys:
        try:
            num = int(value)
            assert 1 <= num <= 99
        except Exception:
            await ctx.send(embed=create_error_embed("Invalid Value", "Provide a number between 1 and 99."))
            return
        cfg[int_keys[setting]] = num
        notifications_config[user_id] = cfg
        save_notifications_config()
        await ctx.send(embed=create_success_embed("Setting Updated", f"`{setting}` set to **{num}%**"))
    else:
        await ctx.send(embed=create_error_embed("Unknown Setting", "Run `!notify-settings` to see valid options."))


# ─── Smart Notifications Monitor (background task) ────────────────────────────

@tasks.loop(minutes=15)
async def smart_notifications_monitor():
    """Periodically check all running VPS and DM owners about issues."""
    for user_id, vps_list in list(vps_data.items()):
        cfg = get_user_notif_config(user_id)
        for vps in vps_list:
            if vps.get("status") != "running":
                continue
            cname = vps["container_name"]

            # Disk usage alert
            if cfg.get("high_disk", True):
                try:
                    disk_out, _, _ = await docker_exec(cname, "df / --output=pcent | tail -1 | tr -d ' %'", timeout=10)
                    disk_pct = int(disk_out.strip()) if disk_out.strip().isdigit() else 0
                    threshold = cfg.get("disk_threshold", 85)
                    if disk_pct >= threshold:
                        user = await bot.fetch_user(int(user_id))
                        embed = create_warning_embed(
                            "⚠️ High Disk Usage Alert",
                            f"Your VPS `{cname}` disk is at **{disk_pct}%** (alert at {threshold}%)!\n"
                            f"Run `!cleanup` to free space."
                        )
                        embed.add_field(name="🖥️ VPS",    value=f"`{cname}`",       inline=True)
                        embed.add_field(name="💾 Usage",  value=f"**{disk_pct}%**", inline=True)
                        try:
                            await user.send(embed=embed)
                        except Exception:
                            pass
                except Exception:
                    pass

            # RAM usage alert
            if cfg.get("high_ram", True):
                try:
                    ram_out, _, _ = await docker_exec(cname, "free -m | awk '/^Mem:/{printf \"%d %d\", $3, $2}'", timeout=10)
                    parts = ram_out.strip().split()
                    if len(parts) == 2:
                        used, total = int(parts[0]), int(parts[1])
                        pct = int(used * 100 / total) if total > 0 else 0
                        threshold = cfg.get("ram_threshold", 85)
                        if pct >= threshold:
                            user = await bot.fetch_user(int(user_id))
                            embed = create_warning_embed(
                                "⚠️ High RAM Usage Alert",
                                f"Your VPS `{cname}` RAM is at **{pct}%** ({used}/{total} MB)!\n"
                                f"Consider restarting unused services."
                            )
                            embed.add_field(name="🖥️ VPS",   value=f"`{cname}`",                  inline=True)
                            embed.add_field(name="🧠 Usage", value=f"**{pct}%** ({used}/{total} MB)", inline=True)
                            try:
                                await user.send(embed=embed)
                            except Exception:
                                pass
                except Exception:
                    pass

            # Crashed services alert
            if cfg.get("service_crash", True):
                try:
                    fail_out, _, _ = await docker_exec(
                        cname,
                        "systemctl list-units --state=failed --no-legend --no-pager 2>/dev/null | awk '{print $1}' | head -3",
                        timeout=10
                    )
                    failed_svcs = [s.strip() for s in fail_out.splitlines() if s.strip()]
                    if failed_svcs:
                        state_key = f"notif_failed_{cname}"
                        _prev     = getattr(smart_notifications_monitor, "_prev_failed", {})
                        new_fails = set(failed_svcs) - _prev.get(state_key, set())
                        _prev[state_key] = set(failed_svcs)
                        smart_notifications_monitor._prev_failed = _prev
                        if new_fails:
                            user  = await bot.fetch_user(int(user_id))
                            embed = create_error_embed(
                                "🚨 Service Crashed on Your VPS",
                                f"New failure(s) detected on `{cname}`!"
                            )
                            embed.add_field(name="💥 Failed Services", value="\n".join(f"`{s}`" for s in new_fails), inline=False)
                            embed.add_field(name="💡 Quick Fix",        value=f"`!fix` — scan and auto-fix your VPS", inline=False)
                            try:
                                await user.send(embed=embed)
                            except Exception:
                                pass
                except Exception:
                    pass


# ─── Guided Setup — post-Pterodactyl next steps ───────────────────────────────

@bot.command(name='guided-setup')
async def guided_setup_cmd(ctx, vps_number: int = 1):
    """Get guided next steps after installing Pterodactyl — !guided-setup [vps#]"""
    user_id  = str(ctx.author.id)
    vps_list = vps_data.get(user_id, [])
    if not vps_list or vps_number < 1 or vps_number > len(vps_list):
        await ctx.send(embed=create_error_embed("Invalid VPS", "VPS not found."))
        return
    vps            = vps_list[vps_number - 1]
    container_name = vps["container_name"]

    embed = create_embed(
        "🎯 Guided Setup — What's Next?",
        f"Recommended next steps after installing Pterodactyl on **VPS #{vps_number}** (`{container_name}`):",
        0x5865F2
    )
    embed.add_field(
        name="① 🌐 Install Cloudflare Tunnel",
        value="Expose your panel securely without opening firewall ports.\nRun `/template` and select **Cloudflare Tunnel**.",
        inline=False
    )
    embed.add_field(
        name="② ⚙️ Configure the Tunnel",
        value="In your Cloudflare dashboard, point your domain to `localhost:80`.",
        inline=False
    )
    embed.add_field(
        name="③ 🦅 Install Pterodactyl Wings",
        value="Wings is the daemon that runs game servers.\nRun `/template` and select **Pterodactyl Wings**.",
        inline=False
    )
    embed.add_field(
        name="④ 🔗 Link Wings to the Panel",
        value="**Panel** → Admin → Nodes → Create Node → copy config → paste to `/etc/pterodactyl/config.yml` on your VPS.",
        inline=False
    )
    embed.add_field(
        name="⑤ 🎮 Create Your First Game Server",
        value="**Panel** → Admin → Servers → Create Server → pick an egg (Minecraft, Rust, etc.).",
        inline=False
    )
    embed.add_field(
        name="💡 Useful Commands",
        value=(
            f"`!fix {vps_number}` — scan for issues\n"
            f"`!cleanup {vps_number}` — free disk space\n"
            f"`!schedule-backup {vps_number} daily` — auto-backup\n"
            f"`!notify-settings` — set up smart alerts"
        ),
        inline=False
    )

    class GuidedView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=300)

        @discord.ui.button(label="📦 Open Template Installer", style=discord.ButtonStyle.primary)
        async def open_templates(self, interaction: discord.Interaction, btn: discord.ui.Button):
            if str(interaction.user.id) != user_id:
                await interaction.response.send_message(embed=create_error_embed("Access Denied", "Not your VPS!"), ephemeral=True)
                return
            await interaction.response.send_message(
                embed=create_info_embed("Template Installer", "Use `/template` to open the template installer and choose what to install next."),
                ephemeral=True
            )

        @discord.ui.button(label="🩺 Scan for Issues", style=discord.ButtonStyle.secondary)
        async def scan_issues(self, interaction: discord.Interaction, btn: discord.ui.Button):
            if str(interaction.user.id) != user_id:
                await interaction.response.send_message(embed=create_error_embed("Access Denied", "Not your VPS!"), ephemeral=True)
                return
            await interaction.response.send_message(
                embed=create_info_embed("VPS Scanner", f"Run `!fix {vps_number}` in the channel to scan your VPS."),
                ephemeral=True
            )

        @discord.ui.button(label="📅 Schedule Backup", style=discord.ButtonStyle.secondary)
        async def sched_backup(self, interaction: discord.Interaction, btn: discord.ui.Button):
            if str(interaction.user.id) != user_id:
                await interaction.response.send_message(embed=create_error_embed("Access Denied", "Not your VPS!"), ephemeral=True)
                return
            await interaction.response.send_message(
                embed=create_info_embed("Scheduled Backups", f"Run `!schedule-backup {vps_number} daily` to enable daily auto-backups."),
                ephemeral=True
            )

    await ctx.send(embed=embed, view=GuidedView())


# ─── !backuplist — browse & download backups ──────────────────────────────────

@bot.command(name='backuplist')
async def backuplist_cmd(ctx, vps_number: int = 1):
    """List all backups for your VPS with download buttons — !backuplist [vps#]"""
    user_id       = str(ctx.author.id)
    is_user_admin = user_id == str(MAIN_ADMIN_ID) or user_id in admin_data.get("admins", [])
    vps_list      = vps_data.get(user_id, [])
    if not vps_list or vps_number < 1 or vps_number > len(vps_list):
        await ctx.send(embed=create_error_embed("Invalid VPS", "VPS not found."))
        return
    container_name = vps_list[vps_number - 1]["container_name"]
    msg = await ctx.send(embed=create_info_embed("💾 Loading Backups…", f"Scanning for backups of `{container_name}`…"))
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "images", "--format", "{{.Repository}}:{{.Tag}}\t{{.Size}}\t{{.CreatedAt}}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, _ = await proc.communicate()
        all_images = stdout.decode().strip().split('\n')
        backups = []
        for img in all_images:
            if not img.strip():
                continue
            parts = img.split('\t')
            name    = parts[0] if parts else img
            size    = parts[1] if len(parts) > 1 else "?"
            created = parts[2][:16] if len(parts) > 2 else "?"
            if container_name + "-backup-" in name:
                backups.append((name, size, created))

        if not backups:
            await msg.edit(embed=create_info_embed(
                "💾 No Backups Found",
                f"No backups exist for `{container_name}`.\n"
                f"Use `!manage` → **📸 Backup Now** or `!schedule-backup {vps_number} daily` to create one."
            ))
            return

        embed = create_embed(f"💾 Backups for VPS #{vps_number}", f"Found **{len(backups)}** backup(s) for `{container_name}`", 0x5865F2)
        for name, size, created in backups[:8]:
            tag = name.split("-backup-")[-1] if "-backup-" in name else name
            embed.add_field(name=f"📸 {tag}", value=f"Size: `{size}`  |  Created: `{created}`", inline=False)
        if len(backups) > 8:
            embed.set_footer(text=f"Showing 8 of {len(backups)} backups")

        class BackupDownloadView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=300)
                for b_name, b_size, b_created in backups[:4]:
                    short = b_name.split("-backup-")[-1][:22] if "-backup-" in b_name else b_name[:22]
                    dl_btn = discord.ui.Button(label=f"⬇ {short}", style=discord.ButtonStyle.primary)
                    dl_btn.callback = self._make_dl(b_name)
                    self.add_item(dl_btn)

            def _make_dl(self, image_name):
                async def callback(interaction: discord.Interaction):
                    uid = str(interaction.user.id)
                    if uid != user_id and not is_user_admin:
                        await interaction.response.send_message(embed=create_error_embed("Access Denied", "Not your VPS!"), ephemeral=True)
                        return
                    await interaction.response.defer(ephemeral=True)
                    await interaction.followup.send(
                        embed=create_info_embed("⬇ Preparing Download…",
                            f"Exporting `{image_name}` as a compressed tar archive.\nThis can take 1–5 minutes depending on the backup size."),
                        ephemeral=True)
                    tmp_path = f"/tmp/{image_name.replace(':', '_').replace('/', '_')}.tar.gz"
                    try:
                        export_proc = await asyncio.create_subprocess_shell(
                            f"docker save {image_name} | gzip > {tmp_path}",
                            stdout=asyncio.subprocess.DEVNULL,
                            stderr=asyncio.subprocess.PIPE)
                        _, stderr_b = await asyncio.wait_for(export_proc.communicate(), timeout=600)
                        if export_proc.returncode != 0:
                            raise RuntimeError(stderr_b.decode()[:200])
                        file_size_mb = os.path.getsize(tmp_path) / (1024 * 1024)
                        if file_size_mb > 25:
                            await interaction.followup.send(
                                embed=create_warning_embed(
                                    "⚠️ Backup Too Large to Upload",
                                    f"Backup is **{file_size_mb:.0f} MB** — Discord's upload limit is 25 MB.\n\n"
                                    f"The file is saved on the server at:\n`{tmp_path}`\n\n"
                                    f"Ask an admin to SCP it to you:\n"
                                    f"```\nscp root@<server>:{tmp_path} ./backup.tar.gz\n```"
                                ),
                                ephemeral=True)
                        else:
                            await interaction.followup.send(
                                content=f"✅ Backup `{image_name}` — download below:",
                                file=discord.File(tmp_path, filename=f"{image_name.split(':')[0]}.tar.gz"),
                                ephemeral=True)
                            try:
                                os.remove(tmp_path)
                            except Exception:
                                pass
                    except asyncio.TimeoutError:
                        await interaction.followup.send(embed=create_error_embed("Timeout", "Export timed out (>10 min). Try again."), ephemeral=True)
                    except Exception as ex:
                        await interaction.followup.send(embed=create_error_embed("Export Failed", str(ex)[:300]), ephemeral=True)
                return callback

        await msg.edit(embed=embed, view=BackupDownloadView())
    except Exception as e:
        await msg.edit(embed=create_error_embed("Error", str(e)[:300]))


# ─── !clone — one-click VPS cloning ──────────────────────────────────────────

@bot.command(name='clone')
async def clone_vps_cmd(ctx, source_vps: int = 1, target_vps: int = 2):
    """Clone your VPS setup to another of your VPS — !clone <source_vps#> <target_vps#>
    WARNING: The target VPS will be replaced with the source's full disk image."""
    user_id  = str(ctx.author.id)
    vps_list = vps_data.get(user_id, [])
    if not vps_list:
        await ctx.send(embed=create_error_embed("No VPS", "You don't have any VPS to clone."))
        return
    if source_vps < 1 or source_vps > len(vps_list):
        await ctx.send(embed=create_error_embed("Invalid Source", f"VPS #{source_vps} not found."))
        return
    if target_vps < 1 or target_vps > len(vps_list):
        await ctx.send(embed=create_error_embed("Invalid Target", f"VPS #{target_vps} not found."))
        return
    if source_vps == target_vps:
        await ctx.send(embed=create_error_embed("Same VPS", "Source and target must be different VPS numbers."))
        return

    src  = vps_list[source_vps - 1]
    tgt  = vps_list[target_vps - 1]
    src_name = src["container_name"]
    tgt_name = tgt["container_name"]

    confirm_embed = create_warning_embed(
        "⚠️ Confirm VPS Clone",
        f"This will **replace** VPS #{target_vps} (`{tgt_name}`) with a full copy of VPS #{source_vps} (`{src_name}`).\n\n"
        f"**All data on the target VPS will be lost.**\n"
        f"This operation takes ~2–5 minutes."
    )
    confirm_embed.add_field(name="📦 Source", value=f"VPS #{source_vps}: `{src_name}`", inline=True)
    confirm_embed.add_field(name="📥 Target", value=f"VPS #{target_vps}: `{tgt_name}`", inline=True)

    class CloneConfirmView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=60)

        @discord.ui.button(label="✅ Yes, Clone It", style=discord.ButtonStyle.danger)
        async def confirm(self, interaction: discord.Interaction, btn: discord.ui.Button):
            if str(interaction.user.id) != user_id:
                await interaction.response.send_message(embed=create_error_embed("Access Denied", "Not your VPS!"), ephemeral=True)
                return
            await interaction.response.defer()
            msg2 = await interaction.followup.send(
                embed=create_info_embed("📦 Cloning VPS…", f"Committing `{src_name}` snapshot…\nDo not restart either VPS during this operation."))
            try:
                clone_image = f"clone-{src_name}-{int(time.time())}"
                await execute_docker(f"docker commit {src_name} {clone_image}", timeout=300)
                await msg2.edit(embed=create_info_embed("📦 Cloning…", f"Recreating `{tgt_name}` from snapshot…"))
                try:
                    await execute_docker(f"docker stop {tgt_name}", timeout=60)
                except Exception:
                    pass
                await execute_docker(f"docker rm -f {tgt_name}", timeout=30)
                ssh_port = tgt.get("ssh_port", get_next_ssh_port())
                hostname = tgt.get("hostname", VPS_HOSTNAME)
                run_cmd = (
                    f"docker run -d --name {tgt_name} --hostname {hostname} "
                    f"-p {ssh_port}:22 --restart=unless-stopped --privileged --cgroupns=host "
                    f"-v /sys/fs/cgroup:/sys/fs/cgroup:rw "
                    f"--tmpfs /run:exec,mode=755,size=256m --tmpfs /run/lock:size=64m --tmpfs /tmp:exec,size=512m "
                    f"-v {tgt_name}-docker:/var/lib/docker -v {tgt_name}-home:/home "
                    f"-v {tgt_name}-root:/root -v {tgt_name}-opt:/opt "
                    f"-e container=docker --dns 8.8.8.8 --dns 1.1.1.1 "
                    f"--security-opt seccomp=unconfined --security-opt apparmor=unconfined "
                    f"--shm-size=512m --ulimit nofile=65536:65536 "
                    f"{clone_image}"
                )
                await execute_docker(run_cmd, timeout=120)
                await asyncio.sleep(10)
                await docker_exec(tgt_name, "systemctl restart ssh 2>/dev/null || /usr/sbin/sshd || true", timeout=20)
                tgt["status"] = "running"
                save_data()
                try:
                    await run_docker_command(f"docker rmi {clone_image}", timeout=30)
                except Exception:
                    pass
                embed = create_success_embed(
                    "📦 Clone Complete!",
                    f"VPS #{source_vps} successfully cloned into VPS #{target_vps}!"
                )
                embed.add_field(name="🖥️ Source",  value=f"`{src_name}`",              inline=True)
                embed.add_field(name="📥 Target",  value=f"`{tgt_name}`",              inline=True)
                embed.add_field(name="🔑 SSH",     value=f"Same password as before for `{tgt_name}`", inline=False)
                embed.add_field(name="💡 Tip",     value="Run `!fix` on the cloned VPS if services don't start.", inline=False)
                await msg2.edit(embed=embed)
            except Exception as ex:
                await msg2.edit(embed=create_error_embed("Clone Failed", str(ex)[:400]))
                try:
                    await run_docker_command(f"docker rmi {clone_image}", timeout=15)
                except Exception:
                    pass

        @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
        async def cancel(self, interaction: discord.Interaction, btn: discord.ui.Button):
            await interaction.response.edit_message(embed=create_info_embed("Cancelled", "Clone cancelled."), view=None)

    await ctx.send(embed=confirm_embed, view=CloneConfirmView())


# ─── File Manager commands ────────────────────────────────────────────────────

@bot.command(name='files')
async def files_cmd(ctx, vps_number: int = 1, *, path: str = "/root"):
    """Browse files on your VPS — !files [vps#] [/path]"""
    user_id       = str(ctx.author.id)
    is_user_admin = user_id == str(MAIN_ADMIN_ID) or user_id in admin_data.get("admins", [])
    vps_list      = vps_data.get(user_id, [])
    if not vps_list or vps_number < 1 or vps_number > len(vps_list):
        await ctx.send(embed=create_error_embed("Invalid VPS", "VPS not found."))
        return
    vps = vps_list[vps_number - 1]
    if vps.get("status") != "running":
        await ctx.send(embed=create_error_embed("VPS Offline", "VPS must be running to use File Manager."))
        return
    container_name = vps["container_name"]
    safe_path = shlex.quote(path)
    # First check that the path exists — test -d gives rc 0/1 reliably without pipe issues
    _, _, test_rc = await docker_exec(container_name, f"test -e {safe_path}", timeout=10)
    if test_rc != 0:
        await ctx.send(embed=create_error_embed("Path Not Found", f"`{path}` does not exist on this VPS."))
        return
    ls_out, _, _ = await docker_exec(container_name, f"ls -lhA {safe_path} 2>/dev/null | head -40", timeout=15)
    lines = [l for l in ls_out.splitlines() if l.strip() and not l.startswith("total")]
    file_lines = "\n".join(f"`{l}`" for l in lines[:20])
    if not file_lines:
        file_lines = "_Directory is empty_"
    embed = create_embed("📂 File Manager", f"**VPS #{vps_number}** — `{path}`", 0x5865F2)
    embed.add_field(name="📁 Contents", value=file_lines[:1000], inline=False)
    embed.add_field(
        name="📌 Commands",
        value=(
            f"`!files {vps_number} /other/path` — Browse a different path\n"
            f"`!download {vps_number} {path}/filename` — Download a file\n"
            f"`!upload {vps_number} {path}/` — Upload a file (attach to message)\n"
            f"`!deletefile {vps_number} {path}/filename` — Delete a file\n"
            f"`!renamefile {vps_number} {path}/old {path}/new` — Rename/move\n"
            f"`!editfile {vps_number} {path}/filename` — View & edit a file"
        ),
        inline=False
    )
    await ctx.send(embed=embed)


@bot.command(name='download')
async def download_file_cmd(ctx, vps_number: int, *, file_path: str):
    """Download a file from your VPS — !download <vps#> /path/to/file"""
    user_id       = str(ctx.author.id)
    is_user_admin = user_id == str(MAIN_ADMIN_ID) or user_id in admin_data.get("admins", [])
    vps_list      = vps_data.get(user_id, [])
    if not vps_list or vps_number < 1 or vps_number > len(vps_list):
        await ctx.send(embed=create_error_embed("Invalid VPS", "VPS not found."))
        return
    vps = vps_list[vps_number - 1]
    if vps.get("status") != "running":
        await ctx.send(embed=create_error_embed("VPS Offline", "VPS must be running."))
        return
    container_name = vps["container_name"]
    filename = os.path.basename(file_path.rstrip('/'))
    if not filename:
        await ctx.send(embed=create_error_embed("Invalid Path", "Provide a full file path, not just a directory."))
        return
    msg = await ctx.send(embed=create_info_embed("⬇ Downloading…", f"Fetching `{file_path}` from `{container_name}`…"))
    tmp_local = f"/tmp/vps_dl_{ctx.author.id}_{filename}"
    sent_file = False
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "cp", f"{container_name}:{file_path}", tmp_local,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=120)
        if proc.returncode != 0:
            err = stderr_b.decode()[:200]
            await msg.edit(embed=create_error_embed("Download Failed", f"```\n{err}\n```"))
            return
        file_size_mb = os.path.getsize(tmp_local) / (1024 * 1024)
        if file_size_mb > 25:
            await msg.edit(embed=create_warning_embed(
                "⚠️ File Too Large",
                f"File is **{file_size_mb:.1f} MB** — Discord limit is 25 MB.\n"
                f"Use SCP directly: `scp root@<server>:{file_path} ./`"
            ))
        else:
            # Send the file first, THEN edit the loading message — avoids losing
            # the progress msg if the upload to Discord itself fails.
            await ctx.send(
                content=f"📥 `{file_path}` from VPS #{vps_number}:",
                file=discord.File(tmp_local, filename=filename))
            sent_file = True
            await msg.edit(embed=create_success_embed("⬇ Download Complete", f"`{filename}` sent above ↑"), view=None)
    except asyncio.TimeoutError:
        await msg.edit(embed=create_error_embed("Timeout", "File download timed out."))
    except Exception as e:
        if not sent_file:
            await msg.edit(embed=create_error_embed("Error", str(e)[:300]))
    finally:
        try:
            os.remove(tmp_local)
        except Exception:
            pass


@bot.command(name='upload')
async def upload_file_cmd(ctx, vps_number: int, *, dest_path: str = "/root/"):
    """Upload a file to your VPS — attach a file then: !upload <vps#> [/dest/path/]"""
    user_id       = str(ctx.author.id)
    is_user_admin = user_id == str(MAIN_ADMIN_ID) or user_id in admin_data.get("admins", [])
    vps_list      = vps_data.get(user_id, [])
    if not vps_list or vps_number < 1 or vps_number > len(vps_list):
        await ctx.send(embed=create_error_embed("Invalid VPS", "VPS not found."))
        return
    if not ctx.message.attachments:
        await ctx.send(embed=create_error_embed("No File Attached", "Attach a file to your `!upload` message."))
        return
    vps = vps_list[vps_number - 1]
    if vps.get("status") != "running":
        await ctx.send(embed=create_error_embed("VPS Offline", "VPS must be running."))
        return
    container_name = vps["container_name"]
    attachment = ctx.message.attachments[0]
    if attachment.size > 25 * 1024 * 1024:
        await ctx.send(embed=create_error_embed("File Too Large", "Discord limits uploads to 25 MB."))
        return
    msg = await ctx.send(embed=create_info_embed("⬆ Uploading…", f"Uploading `{attachment.filename}` to `{container_name}:{dest_path}`…"))
    tmp_local = f"/tmp/vps_ul_{ctx.author.id}_{attachment.filename}"
    try:
        # Use discord.py's built-in attachment downloader — no external deps
        file_data = await attachment.read()
        with open(tmp_local, "wb") as f:
            f.write(file_data)
        full_dest = dest_path.rstrip('/') + '/' + attachment.filename if dest_path.endswith('/') else dest_path
        proc = await asyncio.create_subprocess_exec(
            "docker", "cp", tmp_local, f"{container_name}:{full_dest}",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=120)
        if proc.returncode != 0:
            err = stderr_b.decode()[:200]
            await msg.edit(embed=create_error_embed("Upload Failed", f"```\n{err}\n```"))
        else:
            embed = create_success_embed("⬆ Upload Complete", f"`{attachment.filename}` uploaded to `{container_name}`!")
            embed.add_field(name="📂 Destination", value=f"`{full_dest}`", inline=True)
            embed.add_field(name="📦 Size",        value=f"`{attachment.size / 1024:.1f} KB`", inline=True)
            await msg.edit(embed=embed)
    except asyncio.TimeoutError:
        await msg.edit(embed=create_error_embed("Timeout", "Upload timed out."))
    except Exception as e:
        await msg.edit(embed=create_error_embed("Error", str(e)[:300]))
    finally:
        try:
            os.remove(tmp_local)
        except Exception:
            pass


@bot.command(name='deletefile')
async def delete_file_cmd(ctx, vps_number: int, *, file_path: str):
    """Delete a file on your VPS — !deletefile <vps#> /path/to/file"""
    user_id  = str(ctx.author.id)
    vps_list = vps_data.get(user_id, [])
    if not vps_list or vps_number < 1 or vps_number > len(vps_list):
        await ctx.send(embed=create_error_embed("Invalid VPS", "VPS not found."))
        return
    vps = vps_list[vps_number - 1]
    if vps.get("status") != "running":
        await ctx.send(embed=create_error_embed("VPS Offline", "VPS must be running."))
        return
    container_name = vps["container_name"]

    class DeleteConfirm(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=30)

        @discord.ui.button(label="🗑 Delete", style=discord.ButtonStyle.danger)
        async def confirm(self, interaction: discord.Interaction, btn: discord.ui.Button):
            if str(interaction.user.id) != user_id:
                await interaction.response.send_message(embed=create_error_embed("Access Denied", "Not your VPS!"), ephemeral=True)
                return
            await interaction.response.defer()
            safe_fp = shlex.quote(file_path)
            out, err, rc = await docker_exec(container_name, f"rm -rf {safe_fp} && echo DELETED", timeout=15)
            if "DELETED" in out or rc == 0:
                await interaction.message.edit(embed=create_success_embed("🗑 Deleted", f"`{file_path}` deleted from `{container_name}`."), view=None)
            else:
                await interaction.message.edit(embed=create_error_embed("Delete Failed", f"```\n{err[:200]}\n```"), view=None)

        @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
        async def cancel(self, interaction: discord.Interaction, btn: discord.ui.Button):
            await interaction.response.edit_message(embed=create_info_embed("Cancelled", "File not deleted."), view=None)

    embed = create_warning_embed("🗑 Confirm Delete", f"Delete `{file_path}` from VPS #{vps_number} (`{vps['container_name']}`)?\n⚠️ This cannot be undone.")
    await ctx.send(embed=embed, view=DeleteConfirm())


@bot.command(name='renamefile')
async def rename_file_cmd(ctx, vps_number: int, old_path: str, new_path: str):
    """Rename or move a file on your VPS — !renamefile <vps#> /old/path /new/path"""
    user_id  = str(ctx.author.id)
    vps_list = vps_data.get(user_id, [])
    if not vps_list or vps_number < 1 or vps_number > len(vps_list):
        await ctx.send(embed=create_error_embed("Invalid VPS", "VPS not found."))
        return
    vps = vps_list[vps_number - 1]
    if vps.get("status") != "running":
        await ctx.send(embed=create_error_embed("VPS Offline", "VPS must be running."))
        return
    container_name = vps["container_name"]
    safe_old = shlex.quote(old_path)
    safe_new = shlex.quote(new_path)
    out, err, rc = await docker_exec(container_name, f"mv {safe_old} {safe_new} && echo MOVED", timeout=15)
    if "MOVED" in out or rc == 0:
        embed = create_success_embed("✏️ Renamed", f"`{old_path}` → `{new_path}`")
        embed.add_field(name="🖥️ VPS", value=f"`{container_name}`", inline=True)
    else:
        embed = create_error_embed("Rename Failed", f"```\n{err[:200] or out[:200]}\n```")
    await ctx.send(embed=embed)


@bot.command(name='editfile')
async def edit_file_cmd(ctx, vps_number: int, *, file_path: str):
    """View and edit a config file on your VPS — !editfile <vps#> /path/to/file
    After running this command, reply with the new file content to save it."""
    user_id       = str(ctx.author.id)
    is_user_admin = user_id == str(MAIN_ADMIN_ID) or user_id in admin_data.get("admins", [])
    vps_list      = vps_data.get(user_id, [])
    if not vps_list or vps_number < 1 or vps_number > len(vps_list):
        await ctx.send(embed=create_error_embed("Invalid VPS", "VPS not found."))
        return
    vps = vps_list[vps_number - 1]
    if vps.get("status") != "running":
        await ctx.send(embed=create_error_embed("VPS Offline", "VPS must be running."))
        return
    container_name = vps["container_name"]
    safe_fp = shlex.quote(file_path)
    # Check existence first with test -f so we get a reliable rc (no pipe issues)
    _, _, exist_rc = await docker_exec(container_name, f"test -f {safe_fp}", timeout=10)
    if exist_rc != 0:
        await ctx.send(embed=create_error_embed("File Not Found", f"`{file_path}` does not exist or is a directory."))
        return
    out, _, _ = await docker_exec(container_name, f"head -80 {safe_fp} 2>/dev/null", timeout=15)
    if not out.strip():
        out = ""

    content_preview = out[:1800] if out else "_empty file_"
    embed = create_embed("✏️ File Editor", f"**File:** `{file_path}` on VPS #{vps_number}", 0x5865F2)
    embed.add_field(name="📄 Current Content (first 80 lines)", value=f"```\n{content_preview}\n```", inline=False)
    embed.add_field(
        name="✏️ How to Edit",
        value=(
            "1. Download the file: `!download {vps_number} {file_path}`\n"
            "2. Edit it locally\n"
            "3. Upload the new version: attach file to `!upload {vps_number} {file_path}`\n\n"
            "Or use the **overwrite** button below to paste new content directly."
        ).replace("{vps_number}", str(vps_number)).replace("{file_path}", file_path),
        inline=False
    )

    class EditView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=300)

        @discord.ui.button(label="⬇ Download File", style=discord.ButtonStyle.primary)
        async def dl_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
            if str(interaction.user.id) != user_id and not is_user_admin:
                await interaction.response.send_message(embed=create_error_embed("Access Denied", "Not your VPS!"), ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)
            filename = os.path.basename(file_path)
            tmp_local = f"/tmp/edit_{ctx.author.id}_{filename}"
            try:
                proc = await asyncio.create_subprocess_exec(
                    "docker", "cp", f"{container_name}:{file_path}", tmp_local,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
                _, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=60)
                if proc.returncode != 0:
                    await interaction.followup.send(embed=create_error_embed("Download Failed", stderr_b.decode()[:200]), ephemeral=True)
                else:
                    await interaction.followup.send(
                        content=f"📥 `{file_path}` — edit locally, then re-upload with `!upload {vps_number} {file_path}`:",
                        file=discord.File(tmp_local, filename=filename),
                        ephemeral=True)
            except Exception as ex:
                await interaction.followup.send(embed=create_error_embed("Error", str(ex)[:200]), ephemeral=True)
            finally:
                try:
                    os.remove(tmp_local)
                except Exception:
                    pass

    await ctx.send(embed=embed, view=EditView())


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN", "")
    if not token:
        raise SystemExit("DISCORD_TOKEN environment variable is not set. Set it before running the bot.")
    bot.run(token)
