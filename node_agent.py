#!/usr/bin/env python3
"""
DarkNodes Node Agent  —  HTTP Polling Edition
══════════════════════════════════════════════════════════════════════════════
Connects this machine to your DarkNodes bot using the Node Manager HTTP API.
No Discord token needed. No public IP required on this machine.

  Remote machine ──── HTTPS ──▶ Node Manager (via Cloudflare Tunnel) ──▶ Bot

Requirements:
  • Python 3.8+  (no extra packages — uses only the standard library)
  • The DNODE token (one-time, shown by /node add in Discord)
  • Docker  (for VPS management)
  • Outbound HTTPS to the tunnel URL

Setup
─────
1.  Run  /node add  in Discord (admin only).
2.  Copy the one-liner shown and run it on this machine as root:
        curl -fsSL 'https://nodes.example.com/install.sh?token=DNODE_xxx' | sudo bash

    Or manually:
3.  Set the DNODE token:
        export DNODE_TOKEN="DNODE_eyJh..."
4.  Register once:
        python3 node_agent.py --dnode-token "$DNODE_TOKEN"
5.  Future restarts (credentials saved to node_agent.json automatically):
        python3 node_agent.py
"""
from __future__ import annotations

import argparse
import base64
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

# ── Agent config ──────────────────────────────────────────────────────────────
DEFAULT_CONFIG    = "node_agent.json"
HEARTBEAT_SECS    = 30      # seconds between heartbeats
POLL_SECS         = 3       # seconds between job-poll cycles
RECONNECT_SECS    = 20      # wait after repeated failures
MAX_FAILURES      = 5       # consecutive failures before slowing down
JOB_MAX_AGE_SECS  = 600     # skip jobs older than this (10 minutes)


# ══════════════════════════════════════════════════════════════════════════════
# HTTP helpers  (pure stdlib — no external deps)
# ══════════════════════════════════════════════════════════════════════════════

def _http(
    method:  str,
    url:     str,
    payload: dict | None = None,
    timeout: int = 15,
) -> dict:
    """Make a JSON HTTP call.  Returns the parsed response dict."""
    data    = json.dumps(payload).encode() if payload is not None else None
    headers = {
        "Content-Type": "application/json",
        "User-Agent":   "DarkNodes-Agent/5.0 (HTTP-polling; no-discord-token)",
    }
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode(errors="replace")
            return json.loads(body) if body.strip() else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:300]
        raise RuntimeError(f"HTTP {exc.code}: {body}")
    except Exception as exc:
        raise RuntimeError(str(exc))


def _get_public_ip() -> str:
    for url in ["https://api.ipify.org", "https://icanhazip.com", "https://ifconfig.me/ip"]:
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                return r.read().decode().strip()
        except Exception:
            pass
    return ""


# ══════════════════════════════════════════════════════════════════════════════
# System stats
# ══════════════════════════════════════════════════════════════════════════════

def _cpu_percent() -> float:
    try:
        def _snap():
            with open("/proc/stat") as f:
                parts = list(map(int, f.readline().split()[1:]))
            return sum(parts), parts[3]
        t1, i1 = _snap(); time.sleep(0.25); t2, i2 = _snap()
        return round((1 - (i2 - i1) / max(t2 - t1, 1)) * 100, 1)
    except Exception:
        return 0.0


def _ram_mb() -> tuple:
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


def _disk_gb() -> tuple:
    try:
        usage = shutil.disk_usage("/")
        gb = 1024 ** 3
        return (usage.total - usage.free) / gb, usage.total / gb
    except Exception:
        return 0.0, 0.0


def _running_vps() -> int:
    try:
        r = subprocess.run(
            'docker ps --filter "label=darknodes.vps=true" -q',
            shell=True, capture_output=True, text=True, timeout=10,
        )
        return len([l for l in r.stdout.strip().splitlines() if l.strip()]) if r.returncode == 0 else 0
    except Exception:
        return 0


def _collect_stats() -> dict:
    used_mb, tot_mb = _ram_mb()
    used_gb, tot_gb = _disk_gb()
    return {
        "cpu":           _cpu_percent(),
        "ram_used_mb":   used_mb,
        "ram_total_mb":  tot_mb,
        "disk_used_gb":  round(used_gb, 2),
        "disk_total_gb": round(tot_gb, 2),
        "running_vps":   _running_vps(),
    }


def _collect_vps_metadata() -> list:
    try:
        r = subprocess.run(
            'docker ps -a --filter label=darknodes.vps=true '
            '--format \'{"container_name":"{{.Names}}","status":"{{.Status}}","image":"{{.Image}}"}\'',
            shell=True, capture_output=True, text=True, timeout=15,
        )
        result = []
        for line in r.stdout.strip().splitlines():
            try:
                result.append(json.loads(line))
            except Exception:
                pass
        return result
    except Exception:
        return []


# ══════════════════════════════════════════════════════════════════════════════
# Job execution
# ══════════════════════════════════════════════════════════════════════════════

def _execute_job(command: str, timeout: int) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=min(timeout, 3600),
        )
        output = (result.stdout + result.stderr).strip()
        return result.returncode == 0, output[:8000]
    except subprocess.TimeoutExpired:
        return False, f"Command timed out after {timeout}s"
    except Exception as exc:
        return False, str(exc)


# ══════════════════════════════════════════════════════════════════════════════
# DNODE token
# ══════════════════════════════════════════════════════════════════════════════

def _decode_dnode_token(dnode: str) -> dict | None:
    """Decode a DNODE_xxx token.  Returns {node_id, reg_token, manager_url}."""
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


# ══════════════════════════════════════════════════════════════════════════════
# Config persistence
# ══════════════════════════════════════════════════════════════════════════════

def _load_config(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_config(path: str, data: dict) -> None:
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# Registration  (one-time, using DNODE token)
# ══════════════════════════════════════════════════════════════════════════════

def register(dnode_token: str, config_path: str, custom_name: str = "") -> dict:
    """
    Register this node with the Node Manager via HTTP.
    Returns the config dict with node_id, node_api_key, manager_url.
    Saves config to disk so future restarts don't need the token.
    """
    decoded = _decode_dnode_token(dnode_token)
    if not decoded or not decoded.get("manager_url"):
        raise RuntimeError(
            "Invalid DNODE token — token must start with DNODE_ and contain a manager URL.\n"
            "Run  /node add  in Discord to get a fresh token."
        )

    manager_url = decoded["manager_url"].rstrip("/")
    hostname    = custom_name or socket.gethostname()
    public_ip   = _get_public_ip()

    logger.info(f"Registering with Node Manager at {manager_url} ...")

    resp = _http(
        "POST",
        f"{manager_url}/api/register",
        {
            "dnode_token": dnode_token,
            "hostname":    hostname,
            "public_ip":   public_ip,
        },
        timeout=30,
    )

    node_id      = resp.get("node_id", "")
    node_api_key = resp.get("node_api_key", "")

    if not node_id or not node_api_key:
        raise RuntimeError(f"Registration failed — unexpected server response: {resp}")

    cfg = {
        "node_id":       node_id,
        "node_api_key":  node_api_key,
        "manager_url":   manager_url,
        "hostname":      hostname,
        "registered_at": datetime.utcnow().isoformat(),
    }
    _save_config(config_path, cfg)
    logger.info(f"Registration complete. Node ID: {node_id}")
    return cfg


# ══════════════════════════════════════════════════════════════════════════════
# Main polling loop
# ══════════════════════════════════════════════════════════════════════════════

def run(cfg: dict) -> None:
    """
    Main agent loop.
    • Heartbeat every HEARTBEAT_SECS.
    • Poll for jobs every POLL_SECS, execute them, post results.
    • VPS metadata sync every 5 minutes.
    """
    node_id      = cfg["node_id"]
    node_api_key = cfg["node_api_key"]
    manager_url  = cfg["manager_url"].rstrip("/")

    auth = {"node_id": node_id, "node_api_key": node_api_key}

    logger.info(
        f"Agent running — node_id={node_id}  manager={manager_url}"
        f"  heartbeat={HEARTBEAT_SECS}s  poll={POLL_SECS}s"
    )

    last_hb       = 0.0
    last_vsync    = 0.0
    consecutive   = 0

    while True:
        now = time.time()

        # ── Heartbeat ─────────────────────────────────────────────────────────
        if now - last_hb >= HEARTBEAT_SECS:
            try:
                stats = _collect_stats()
                _http("POST", f"{manager_url}/api/heartbeat", {**auth, "stats": stats})
                last_hb     = now
                consecutive = 0
                logger.debug(
                    f"Heartbeat: CPU={stats['cpu']}%  "
                    f"RAM={stats['ram_used_mb']}/{stats['ram_total_mb']}MB"
                )
            except Exception as exc:
                consecutive += 1
                logger.warning(f"Heartbeat failed ({consecutive}/{MAX_FAILURES}): {exc}")

        # ── VPS metadata sync (every 5 min) ────────────────────────────────────
        if now - last_vsync >= 300:
            try:
                vps_list = _collect_vps_metadata()
                if vps_list:
                    _http("POST", f"{manager_url}/api/vsync", {**auth, "vps_list": vps_list})
                last_vsync = now
            except Exception as exc:
                logger.debug(f"VPS sync failed: {exc}")

        # ── Job poll ───────────────────────────────────────────────────────────
        try:
            resp = _http("POST", f"{manager_url}/api/jobs", auth)
            jobs = resp.get("jobs", [])
        except Exception as exc:
            consecutive += 1
            if consecutive <= MAX_FAILURES:
                logger.warning(f"Job poll failed ({consecutive}/{MAX_FAILURES}): {exc}")
            delay = RECONNECT_SECS if consecutive > MAX_FAILURES else POLL_SECS
            time.sleep(delay)
            continue

        consecutive = 0

        for job in jobs:
            job_id     = job.get("job_id", "")
            command    = job.get("command", "")
            timeout    = int(job.get("timeout", 120))
            created_at = job.get("created_at", 0)

            if not job_id or not command:
                continue

            # Skip stale jobs
            if created_at and (time.time() - created_at) > JOB_MAX_AGE_SECS:
                logger.warning(f"Skipping stale job {job_id} (age>{JOB_MAX_AGE_SECS}s)")
                try:
                    _http("POST", f"{manager_url}/api/result", {
                        **auth,
                        "job_id":  job_id,
                        "success": False,
                        "output":  f"Job skipped — arrived too late (>{JOB_MAX_AGE_SECS}s old)",
                    })
                except Exception:
                    pass
                continue

            logger.info(f"Executing job {job_id}: {command[:80]!r}")
            success, output = _execute_job(command, timeout)
            logger.info(
                f"Job {job_id} done: success={success}  output={output[:60]!r}"
            )

            try:
                _http("POST", f"{manager_url}/api/result", {
                    **auth,
                    "job_id":  job_id,
                    "success": success,
                    "output":  output,
                })
            except Exception as exc:
                logger.error(f"Failed to post result for job {job_id}: {exc}")

        time.sleep(POLL_SECS)


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description="DarkNodes Node Agent — HTTP Polling Edition",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--dnode-token",
        default=os.environ.get("DNODE_TOKEN", ""),
        metavar="TOKEN",
        help="DNODE registration token from /node add in Discord (one-time)",
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG,
        help=f"Path to the saved credentials file (default: {DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--name",
        default="",
        help="Custom display name for this node (optional)",
    )
    args = parser.parse_args()

    config_path = args.config

    # ── Load existing credentials or register ─────────────────────────────────
    cfg = _load_config(config_path)

    if not (cfg.get("node_id") and cfg.get("node_api_key") and cfg.get("manager_url")):
        dnode_token = args.dnode_token.strip()
        if not dnode_token:
            print(
                "No credentials found and no DNODE token provided.\n"
                "\n"
                "Run  /node add  in Discord to get a token, then either:\n"
                "  • Use the one-liner install command shown by the bot (recommended)\n"
                "  • Or run manually:\n"
                "      export DNODE_TOKEN='DNODE_eyJh...'\n"
                "      python3 node_agent.py --dnode-token \"$DNODE_TOKEN\"\n",
                file=sys.stderr,
            )
            sys.exit(1)

        try:
            cfg = register(dnode_token, config_path, custom_name=args.name)
        except Exception as exc:
            print(f"ERROR: Registration failed: {exc}", file=sys.stderr)
            sys.exit(1)
    else:
        logger.info(
            f"Loaded credentials from {config_path} — node_id={cfg['node_id']}"
        )

    # ── Main loop (restart on unexpected crash) ────────────────────────────────
    while True:
        try:
            run(cfg)
        except KeyboardInterrupt:
            logger.info("Stopped.")
            sys.exit(0)
        except Exception as exc:
            logger.error(f"Unexpected error: {exc}")
            logger.info(f"Restarting in {RECONNECT_SECS}s...")
            time.sleep(RECONNECT_SECS)


if __name__ == "__main__":
    main()
