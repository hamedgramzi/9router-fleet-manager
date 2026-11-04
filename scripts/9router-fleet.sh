#!/usr/bin/env bash
# ==========================================================
# ⚡ 9Router Fleet Manager - Command Line Control Tool (CLI)
# ==========================================================

INSTALL_DIR="/opt/9router-fleet"
SERVICE_NAME="9router-fleet.service"
GUARDIAN_CONTAINER="9router-guardian"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
PLAIN='\033[0m'

confirm() {
    read -rp "$1 [y/N]: " resp
    case "$resp" in
        [yY][eE][sS]|[yY]) return 0 ;;
        *) return 1 ;;
    esac
}

show_status() {
    echo -e "${CYAN}=== 9Router Fleet Service Status ===${PLAIN}"
    systemctl status "$SERVICE_NAME" --no-pager
    echo ""
    echo -e "${CYAN}=== Guardian Container Status ===${PLAIN}"
    if docker ps -a --format '{{.Names}}' | grep -q "^${GUARDIAN_CONTAINER}$"; then
        docker ps --filter "name=${GUARDIAN_CONTAINER}" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
    else
        echo -e "${RED}Guardian container is not running.${PLAIN}"
    fi
}

start_service() {
    echo -e "${BLUE}Starting 9Router Fleet Manager...${PLAIN}"
    systemctl start "$SERVICE_NAME"
    if docker ps -a --format '{{.Names}}' | grep -q "^${GUARDIAN_CONTAINER}$"; then
        docker start "$GUARDIAN_CONTAINER" >/dev/null 2>&1 || true
    fi
    echo -e "${GREEN}Started successfully.${PLAIN}"
}

stop_service() {
    echo -e "${BLUE}Stopping 9Router Fleet Manager...${PLAIN}"
    systemctl stop "$SERVICE_NAME"
    echo -e "${GREEN}Service stopped.${PLAIN}"
}

restart_service() {
    echo -e "${BLUE}Restarting 9Router Fleet Manager...${PLAIN}"
    systemctl restart "$SERVICE_NAME"
    if docker ps -a --format '{{.Names}}' | grep -q "^${GUARDIAN_CONTAINER}$"; then
        docker restart "$GUARDIAN_CONTAINER" >/dev/null 2>&1 || true
    fi
    echo -e "${GREEN}Restarted successfully.${PLAIN}"
}

show_logs() {
    echo -e "${CYAN}Streaming logs (Ctrl+C to exit)...${PLAIN}"
    journalctl -u "$SERVICE_NAME" -f
}

show_creds() {
    local cred_file="${INSTALL_DIR}/panel/data/admin.json"
    local env_file="${INSTALL_DIR}/.env"
    
    local domain="localhost"
    local port="21000"
    if [ -f "$env_file" ]; then
        domain=$(grep -E "^DOMAIN_NAME=" "$env_file" | cut -d'=' -f2- | tr -d '"' | tr -d "'" || echo "localhost")
        port=$(grep -E "^PANEL_PORT=" "$env_file" | cut -d'=' -f2- | tr -d '"' | tr -d "'" || echo "21000")
    fi

    if [ -f "$cred_file" ]; then
        local user pass
        user=$(grep -o '"username": *"[^"]*"' "$cred_file" | cut -d'"' -f4)
        pass=$(grep -o '"plain_initial_pass": *"[^"]*"' "$cred_file" | cut -d'"' -f4)
        
        echo -e "${GREEN}=====================================================${PLAIN}"
        echo -e "${GREEN} ⚡ 9Router Fleet Manager Credentials                ${PLAIN}"
        echo -e "${GREEN}=====================================================${PLAIN}"
        echo -e "Web Panel URL: ${CYAN}http://${domain}:${port}${PLAIN}"
        echo -e "Admin Username: ${YELLOW}${user}${PLAIN}"
        echo -e "Initial Password: ${YELLOW}${pass}${PLAIN}"
        echo -e "${GREEN}=====================================================${PLAIN}"
    else
        echo -e "${RED}Credentials file not found at ${cred_file}.${PLAIN}"
        echo -e "Please ensure the service has run at least once."
    fi
}

update_app() {
    echo -e "${BLUE}Updating 9Router Fleet Manager...${PLAIN}"
    cd "$INSTALL_DIR" || exit 1
    if [ -d ".git" ]; then
        git pull origin main || git pull
    fi
    if [ -f "venv/bin/pip" ]; then
        ./venv/bin/pip install --upgrade -r requirements.txt
    fi
    systemctl restart "$SERVICE_NAME"
    echo -e "${GREEN}Update completed successfully!${PLAIN}"
}

uninstall_app() {
    echo -e "${RED}=====================================================${PLAIN}"
    echo -e "${RED}           ⚠️  UNINSTALL 9ROUTER FLEET ⚠️            ${PLAIN}"
    echo -e "${RED}=====================================================${PLAIN}"
    echo "This will stop the management panel service and remove the CLI tool."
    if ! confirm "Are you sure you want to proceed?"; then
        echo -e "${YELLOW}Cancelled.${PLAIN}"
        return
    fi

    local purge_data=false
    if confirm "Do you also want to remove all tenant data, containers, and databases?"; then
        purge_data=true
    fi

    echo -e "${BLUE}Stopping and disabling service...${PLAIN}"
    systemctl stop "$SERVICE_NAME" >/dev/null 2>&1 || true
    systemctl disable "$SERVICE_NAME" >/dev/null 2>&1 || true
    rm -f "/etc/systemd/system/${SERVICE_NAME}"
    systemctl daemon-reload

    echo -e "${BLUE}Stopping Guardian container...${PLAIN}"
    docker stop "$GUARDIAN_CONTAINER" >/dev/null 2>&1 || true
    docker rm -f "$GUARDIAN_CONTAINER" >/dev/null 2>&1 || true

    if [ "$purge_data" = true ]; then
        echo -e "${BLUE}Removing all tenant containers (9router-*)...${PLAIN}"
        local tenant_containers
        tenant_containers=$(docker ps -a --filter "name=9router-" --format "{{.Names}}" || true)
        if [ -n "$tenant_containers" ]; then
            echo "$tenant_containers" | xargs -r docker rm -f >/dev/null 2>&1 || true
        fi
        echo -e "${BLUE}Purging installation directory ${INSTALL_DIR}...${PLAIN}"
        rm -rf "$INSTALL_DIR"
    else
        echo -e "${YELLOW}Tenant files and databases kept in ${INSTALL_DIR}.${PLAIN}"
    fi

    rm -f /usr/local/bin/9router-fleet
    rm -f /usr/local/bin/9router
    echo -e "${GREEN}9Router Fleet Manager has been uninstalled successfully.${PLAIN}"
    exit 0
}

show_menu() {
    clear
    echo -e "
  ${CYAN}⚡ 9Router Fleet Manager Management CLI${PLAIN}
  ${BLUE}---------------------------------------${PLAIN}
  ${GREEN}1.${PLAIN} Start Service
  ${GREEN}2.${PLAIN} Stop Service
  ${GREEN}3.${PLAIN} Restart Service
  ${GREEN}4.${PLAIN} Check Status
  ${GREEN}5.${PLAIN} View Logs
  ${BLUE}---------------------------------------${PLAIN}
  ${GREEN}6.${PLAIN} Show Web Panel URL & Credentials
  ${GREEN}7.${PLAIN} Update Fleet Manager
  ${RED}8.${PLAIN} Uninstall Fleet Manager
  ${BLUE}---------------------------------------${PLAIN}
  ${GREEN}0.${PLAIN} Exit
"
    read -rp "Please enter an option [0-8]: " opt
    case "$opt" in
        1) start_service ;;
        2) stop_service ;;
        3) restart_service ;;
        4) show_status ;;
        5) show_logs ;;
        6) show_creds ;;
        7) update_app ;;
        8) uninstall_app ;;
        0) exit 0 ;;
        *) echo -e "${RED}Invalid option.${PLAIN}" ;;
    esac
}

# Subcommand dispatch
case "${1:-}" in
    start) start_service ;;
    stop) stop_service ;;
    restart) restart_service ;;
    status) show_status ;;
    logs|log) show_logs ;;
    creds|credentials|info) show_creds ;;
    update) update_app ;;
    uninstall) uninstall_app ;;
    *) show_menu ;;
esac
