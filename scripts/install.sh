#!/usr/bin/env bash
set -euo pipefail

echo "=========================================================="
echo "   🚀 9Router Fleet Manager - Automated Setup Script      "
echo "=========================================================="

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SCRIPT_DIR"

# 1. Check prerequisites
command -v docker >/dev/null 2>&1 || { echo "❌ Error: Docker is not installed. Please install Docker first."; exit 1; }

# 2. Check or create .env
if [ ! -f .env ]; then
    echo "📄 Creating .env from .env.example..."
    cp .env.example .env
    echo "⚠️ Please review and configure .env before starting production instances."
fi

# Source .env
set -a
source .env
set +a

DOCKER_NETWORK="${DOCKER_NETWORK:-9router-fleet-net}"

# 3. Create Docker network if not existing
if ! docker network ls | grep -q "$DOCKER_NETWORK"; then
    echo "🌐 Creating Docker network: $DOCKER_NETWORK..."
    docker network create "$DOCKER_NETWORK"
else
    echo "✅ Docker network $DOCKER_NETWORK already exists."
fi

# 4. Create necessary local directories
mkdir -p panel/data nginx/conf.d users

# 5. Pull default 9Router image
echo "📦 Pulling 9Router Docker image (${ROUTER_IMAGE:-decolua/9router:latest})..."
docker pull "${ROUTER_IMAGE:-decolua/9router:latest}"

# 6. Deploy Guardian Nginx container (if not already running)
if ! docker ps -a --format '{{.Names}}' | grep -q "^9router-guardian$"; then
    echo "🛡️ Starting Nginx Port Guardian container..."
    docker run -d \
        --name 9router-guardian \
        --restart unless-stopped \
        --network "$DOCKER_NETWORK" \
        -p "${PORT_RANGE_START:-21001}-${PORT_RANGE_END:-21050}:${PORT_RANGE_START:-21001}-${PORT_RANGE_END:-21050}" \
        -v "$SCRIPT_DIR/nginx/nginx.conf:/etc/nginx/nginx.conf:ro" \
        -v "$SCRIPT_DIR/nginx/conf.d:/etc/nginx/conf.d:ro" \
        -v "/etc/letsencrypt:/etc/letsencrypt:ro" \
        nginx:alpine
    echo "✅ Guardian container started."
else
    echo "✅ Guardian container 9router-guardian is already running."
fi

echo ""
echo "🎉 Setup complete!"
echo "To run the Fleet Manager Panel directly via Python systemd or virtualenv:"
echo "   pip3 install -r requirements.txt"
echo "   uvicorn panel.server:app --host 0.0.0.0 --port ${PANEL_PORT:-21000}"
echo ""
echo "Or use Docker Compose:"
echo "   docker compose up -d"
echo "=========================================================="
