#!/usr/bin/env bash
# ==========================================================
# ⚡ 9Router Fleet Manager - Automated Installer
# ==========================================================

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
PLAIN='\033[0m'

INSTALL_DIR="/opt/9router-fleet"
SERVICE_FILE="/etc/systemd/system/9router-fleet.service"
CLI_TARGET="/usr/local/bin/9router-fleet"

REPO_URL="https://github.com/hamedgramzi/9router-fleet-manager.git"

echo -e "${CYAN}
===========================================================
    ⚡ 9Router Fleet Manager - Automated Installation
===========================================================
${PLAIN}"

# 1. Root check
if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}Error: This script must be run as root. Please run with sudo or as root user.${PLAIN}"
    exit 1
fi

# 2. Detect Package Manager & install base tools
echo -e "${BLUE}▶ Checking system packages and installing dependencies...${PLAIN}"
if command -v apt-get >/dev/null 2>&1; then
    apt-get update -y
    apt-get install -y curl wget git python3 python3-pip python3-venv sqlite3 ca-certificates
elif command -v yum >/dev/null 2>&1; then
    yum install -y curl wget git python3 python3-pip sqlite ca-certificates
elif command -v dnf >/dev/null 2>&1; then
    dnf install -y curl wget git python3 python3-pip sqlite ca-certificates
fi

# 3. Check and install Docker if missing
if ! command -v docker >/dev/null 2>&1; then
    echo -e "${YELLOW}Docker is not installed. Installing Docker automatically via get.docker.com...${PLAIN}"
    curl -fsSL https://get.docker.com | sh
    systemctl enable --now docker
    echo -e "${GREEN}Docker installed successfully.${PLAIN}"
else
    echo -e "${GREEN}Docker is already installed.${PLAIN}"
fi

# 4. Clone or update repository into /opt/9router-fleet
mkdir -p "$INSTALL_DIR"
if [ -d "$INSTALL_DIR/.git" ]; then
    echo -e "${BLUE}▶ Updating existing repository in ${INSTALL_DIR}...${PLAIN}"
    cd "$INSTALL_DIR"
    git pull origin main || git pull
else
    # If the installer is run inside a local cloned folder, copy files over
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    if [ -f "$SCRIPT_DIR/requirements.txt" ] && [ -d "$SCRIPT_DIR/panel" ]; then
        echo -e "${BLUE}▶ Copying local files to ${INSTALL_DIR}...${PLAIN}"
        cp -r "$SCRIPT_DIR"/* "$INSTALL_DIR"/
        cp "$SCRIPT_DIR"/.env.example "$INSTALL_DIR"/.env.example 2>/dev/null || true
    else
        echo -e "${BLUE}▶ Cloning repository to ${INSTALL_DIR}...${PLAIN}"
        rm -rf "${INSTALL_DIR:?}"/*
        git clone "$REPO_URL" "$INSTALL_DIR"
    fi
fi

cd "$INSTALL_DIR"

# 5. Setup Python virtual environment
echo -e "${BLUE}▶ Setting up isolated Python virtual environment...${PLAIN}"
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install -r requirements.txt

# 6. Configure .env
if [ ! -f "$INSTALL_DIR/.env" ]; then
    echo -e "${BLUE}▶ Configuring environment (.env)...${PLAIN}"
    cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
    
    # Optional prompt for domain if running interactively
    if [ -t 0 ]; then
        read -rp "Enter your Public Domain for TLS (e.g. ai.example.com) [default: localhost]: " user_domain
        if [ -n "$user_domain" ]; then
            sed -i "s|DOMAIN_NAME=.*|DOMAIN_NAME=${user_domain}|g" "$INSTALL_DIR/.env"
        fi
        read -rp "Enter Port for Fleet Manager Panel [default: 21000]: " user_panel_port
        if [ -n "$user_panel_port" ]; then
            sed -i "s|PANEL_PORT=.*|PANEL_PORT=${user_panel_port}|g" "$INSTALL_DIR/.env"
        fi
    fi
fi

# Source .env
set -a
source "$INSTALL_DIR/.env"
set +a

# 7. Initialize Docker network & Guardian container
DOCKER_NETWORK="${DOCKER_NETWORK:-9router-fleet-net}"
if ! docker network ls | grep -q "$DOCKER_NETWORK"; then
    echo -e "${BLUE}▶ Creating Docker network: ${DOCKER_NETWORK}...${PLAIN}"
    docker network create "$DOCKER_NETWORK"
fi

TARGET_IMAGE="${ROUTER_IMAGE:-decolua/9router:latest}"
echo -e "${BLUE}▶ Pulling 9Router Docker image (${TARGET_IMAGE})...${PLAIN}"
if ! docker pull "$TARGET_IMAGE"; then
    echo -e "${YELLOW}⚠️ Failed to pull ${TARGET_IMAGE} directly from Docker Hub.${PLAIN}"
    if [ -t 0 ]; then
        while true; do
            echo -e "${CYAN}You can specify an alternative mirror, custom proxy image, or private registry repository.${PLAIN}"
            read -rp "Enter custom Docker image/mirror (e.g. your-mirror.com/decolua/9router:latest) [or 'q' to abort]: " custom_image
            if [ -z "$custom_image" ] || [ "$custom_image" = "q" ] || [ "$custom_image" = "Q" ]; then
                echo -e "${RED}Error: Docker pull aborted by user.${PLAIN}"
                exit 1
            fi

            echo -e "${BLUE}▶ Attempting pull from custom registry: ${custom_image}...${PLAIN}"
            if docker pull "$custom_image"; then
                docker tag "$custom_image" "$TARGET_IMAGE"
                echo -e "${GREEN}✅ Successfully pulled and tagged image as ${TARGET_IMAGE}.${PLAIN}"
                break
            else
                echo -e "${RED}❌ Failed to pull from ${custom_image}. Please try another mirror or check network.${PLAIN}"
            fi
        done
    else
        echo -e "${RED}Error: Failed to pull ${TARGET_IMAGE} and non-interactive shell cannot prompt for custom registry.${PLAIN}"
        exit 1
    fi
fi

# Ensure Guardian Nginx container is running
if ! docker ps -a --format '{{.Names}}' | grep -q "^9router-guardian$"; then
    echo -e "${BLUE}▶ Starting Nginx Port Guardian container...${PLAIN}"
    mkdir -p "$INSTALL_DIR/nginx/conf.d"
    docker run -d \
        --name 9router-guardian \
        --restart unless-stopped \
        --network "$DOCKER_NETWORK" \
        -p "${PORT_RANGE_START:-21001}-${PORT_RANGE_END:-21050}:${PORT_RANGE_START:-21001}-${PORT_RANGE_END:-21050}" \
        -v "$INSTALL_DIR/nginx/nginx.conf:/etc/nginx/nginx.conf:ro" \
        -v "$INSTALL_DIR/nginx/conf.d:/etc/nginx/conf.d:ro" \
        -v "${SSL_CERTS_DIR:-/etc/letsencrypt}:/etc/letsencrypt:ro" \
        nginx:alpine
else
    echo -e "${GREEN}Guardian container 9router-guardian is already running.${PLAIN}"
fi

# 8. Create and register Systemd service
echo -e "${BLUE}▶ Registering systemd service (${SERVICE_FILE})...${PLAIN}"
cat <<EOF > "$SERVICE_FILE"
[Unit]
Description=9Router Fleet Manager
After=network.target docker.service
Requires=docker.service

[Service]
Type=simple
User=root
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${INSTALL_DIR}/.env
ExecStart=${INSTALL_DIR}/venv/bin/uvicorn panel.server:app --host 0.0.0.0 --port ${PANEL_PORT:-21000}
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now 9router-fleet.service

# 9. Install CLI command
echo -e "${BLUE}▶ Installing 9router-fleet CLI tool to ${CLI_TARGET}...${PLAIN}"
cp "$INSTALL_DIR/scripts/9router-fleet.sh" "$CLI_TARGET"
chmod +x "$CLI_TARGET"
ln -sf "$CLI_TARGET" /usr/local/bin/9router

# 10. Wait a moment for initial credentials generation
sleep 2

# Extract generated credentials
ADMIN_FILE="$INSTALL_DIR/panel/data/admin.json"
ADMIN_USER="admin"
ADMIN_PASS=""
if [ -f "$ADMIN_FILE" ]; then
    ADMIN_USER=$(grep -o '"username": *"[^"]*"' "$ADMIN_FILE" | cut -d'"' -f4)
    ADMIN_PASS=$(grep -o '"plain_initial_pass": *"[^"]*"' "$ADMIN_FILE" | cut -d'"' -f4)
fi

SERVER_IP=$(curl -s4 ifconfig.me || curl -s4 icanhazip.com || echo "YOUR_SERVER_IP")

echo -e "
${GREEN}================================================================${PLAIN}
${GREEN} 🎉 9Router Fleet Manager successfully installed!               ${PLAIN}
${GREEN}================================================================${PLAIN}

  ${CYAN}Web Panel URL:${PLAIN}   http://${SERVER_IP}:${PANEL_PORT:-21000}
  ${CYAN}Username:${PLAIN}        ${YELLOW}${ADMIN_USER}${PLAIN}
  ${CYAN}Password:${PLAIN}        ${YELLOW}${ADMIN_PASS}${PLAIN}

  ${BLUE}Command Line Tool:${PLAIN}
  Simply type: ${GREEN}9router-fleet${PLAIN} or ${GREEN}9router${PLAIN} anytime to:
  - Start / Stop / Restart the service
  - View live logs and system status
  - Retrieve panel credentials
  - Update or uninstall the fleet manager

${GREEN}================================================================${PLAIN}
"
