#!/usr/bin/env bash
set -euo pipefail

# ─── colours ─────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

banner() {
  echo -e "${CYAN}${BOLD}"
  echo "  ██████╗  █████╗ ██████╗ ██╗  ██╗███╗   ██╗ ██████╗ ██████╗ ███████╗███████╗"
  echo "  ██╔══██╗██╔══██╗██╔══██╗██║ ██╔╝████╗  ██║██╔═══██╗██╔══██╗██╔════╝██╔════╝"
  echo "  ██║  ██║███████║██████╔╝█████╔╝ ██╔██╗ ██║██║   ██║██║  ██║█████╗  ███████╗"
  echo "  ██║  ██║██╔══██║██╔══██╗██╔═██╗ ██║╚██╗██║██║   ██║██║  ██║██╔══╝  ╚════██║"
  echo "  ██████╔╝██║  ██║██║  ██║██║  ██╗██║ ╚████║╚██████╔╝██████╔╝███████╗███████║"
  echo "  ╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═══╝ ╚═════╝ ╚═════╝ ╚══════╝╚══════╝"
  echo -e "${NC}"
  echo -e "  ${BOLD}DarkNodes Installer${NC} — by ${CYAN}SHADOW GAMER${NC}"
  echo ""
}

step() { echo -e "\n${GREEN}[•]${NC} ${BOLD}$*${NC}"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
die()  { echo -e "${RED}[✗]${NC} $*" >&2; exit 1; }

# ─── root check ──────────────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
  warn "Not running as root. systemd install will be skipped if chosen."
fi

banner

# ─── dependencies ────────────────────────────────────────────────────────────
step "Checking dependencies..."

command -v git     >/dev/null 2>&1 || die "git is not installed. Run: sudo apt install git"
command -v python3 >/dev/null 2>&1 || die "python3 is not installed."
command -v docker  >/dev/null 2>&1 || warn "Docker not found — VPS creation will not work without it."

if command -v pip3 >/dev/null 2>&1; then
  PIP=pip3
elif command -v pip >/dev/null 2>&1; then
  PIP=pip
else
  die "pip is not installed. Run: sudo apt install python3-pip"
fi

echo -e "  ${GREEN}✓${NC} All required tools found. (pip command: ${PIP})"

# ─── clone ───────────────────────────────────────────────────────────────────
INSTALL_DIR="/opt/darknodes"
step "Cloning DarkNodes into ${INSTALL_DIR}..."

if [[ -d "$INSTALL_DIR/.git" ]]; then
  warn "Directory already exists — pulling latest changes instead."
  git -C "$INSTALL_DIR" pull --ff-only
else
  git clone https://github.com/hishadow1/DarkDeployerBot "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"
echo -e "  ${GREEN}✓${NC} Repository ready at ${INSTALL_DIR}"

# ─── python deps ─────────────────────────────────────────────────────────────
step "Installing Python dependencies..."
$PIP install -r requirements.txt --break-system-packages -q 2>/dev/null \
  || $PIP install -r requirements.txt -q
echo -e "  ${GREEN}✓${NC} Python packages installed."

# ─── credentials ─────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}──────────────────────────────────────────${NC}"
echo -e "${BOLD}  Bot Configuration${NC}"
echo -e "${BOLD}──────────────────────────────────────────${NC}"

while true; do
  read -rp "$(echo -e "  ${CYAN}Your Discord User ID (Main Admin):${NC} ")" MAIN_ADMIN_ID
  [[ "$MAIN_ADMIN_ID" =~ ^[0-9]{17,20}$ ]] && break
  warn "That doesn't look like a valid Discord user ID (17–20 digits). Try again."
done

while true; do
  read -rp "$(echo -e "  ${CYAN}Discord Bot Token:${NC} ")" DISCORD_TOKEN
  [[ -n "$DISCORD_TOKEN" ]] && break
  warn "Token cannot be empty. Try again."
done

export DISCORD_TOKEN
export MAIN_ADMIN_ID

echo -e "  ${GREEN}✓${NC} Credentials exported for this session."

# ─── launch choice ───────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}──────────────────────────────────────────${NC}"
echo -e "${BOLD}  How do you want to run the bot?${NC}"
echo -e "${BOLD}──────────────────────────────────────────${NC}"
echo -e "  ${CYAN}1)${NC} python3  — run directly in this terminal (foreground)"
echo -e "  ${CYAN}2)${NC} systemd  — install as a system service (auto-starts on reboot)"
echo ""

while true; do
  read -rp "$(echo -e "  ${CYAN}Enter 1 or 2:${NC} ")" CHOICE
  [[ "$CHOICE" == "1" || "$CHOICE" == "2" ]] && break
  warn "Please enter 1 or 2."
done

# ─── option 1: python ────────────────────────────────────────────────────────
if [[ "$CHOICE" == "1" ]]; then
  step "Starting bot with python3..."
  echo -e "  ${YELLOW}Press Ctrl+C to stop.${NC}\n"
  cd "$INSTALL_DIR"
  exec python3 bot.py
fi

# ─── option 2: systemd ───────────────────────────────────────────────────────
if [[ "$CHOICE" == "2" ]]; then
  [[ $EUID -ne 0 ]] && die "systemd install requires root. Re-run the installer with sudo."

  step "Installing systemd service..."

  SERVICE_FILE="/etc/systemd/system/darknodes.service"

  cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=DarkNodes Discord Bot
After=network-online.target docker.service
Wants=network-online.target
Requires=docker.service

[Service]
Type=simple
User=root
WorkingDirectory=${INSTALL_DIR}
Environment=DISCORD_TOKEN=${DISCORD_TOKEN}
Environment=MAIN_ADMIN_ID=${MAIN_ADMIN_ID}
ExecStart=/usr/bin/python3 bot.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable darknodes
  systemctl restart darknodes

  echo ""
  echo -e "${GREEN}${BOLD}  ✓ DarkNodes is running!${NC}"
  echo ""
  echo -e "  Check status : ${CYAN}sudo systemctl status darknodes${NC}"
  echo -e "  Live logs    : ${CYAN}sudo journalctl -u darknodes -f${NC}"
  echo -e "  Stop bot     : ${CYAN}sudo systemctl stop darknodes${NC}"
  echo ""
fi
