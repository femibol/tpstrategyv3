#!/bin/bash
# ============================================================================
# IB Gateway / TWS Startup Helper
# ============================================================================
#
# This script helps start IB Gateway or TWS for the trading bot.
# It checks connection status and provides clear instructions.
#
# Usage:
#   ./scripts/start_ibgateway.sh          # Check status + show help
#   ./scripts/start_ibgateway.sh docker   # Start via Docker (headless)
#   ./scripts/start_ibgateway.sh status   # Just check connection status
#
# Ports:
#   7497 = TWS Paper Trading
#   7496 = TWS Live Trading
#   4002 = IB Gateway Paper
#   4001 = IB Gateway Live
# ============================================================================

set -e

# Load .env if present
if [ -f "$(dirname "$0")/../.env" ]; then
    source "$(dirname "$0")/../.env"
fi

IBKR_HOST="${IBKR_HOST:-127.0.0.1}"
IBKR_PORT="${IBKR_PORT:-7497}"
TRADING_MODE="${TRADING_MODE:-paper}"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo ""
echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}  IBKR Connection Helper${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""

# ============================================================================
# Check current connection status
# ============================================================================
check_connection() {
    echo -e "Checking IBKR connection at ${YELLOW}${IBKR_HOST}:${IBKR_PORT}${NC}..."
    echo ""

    if command -v nc &>/dev/null; then
        if nc -z -w 2 "$IBKR_HOST" "$IBKR_PORT" 2>/dev/null; then
            echo -e "  ${GREEN}CONNECTED${NC} - IBKR is accepting connections on port $IBKR_PORT"

            if [ "$IBKR_PORT" = "7497" ] || [ "$IBKR_PORT" = "4002" ]; then
                echo -e "  Mode: ${YELLOW}PAPER TRADING${NC}"
            elif [ "$IBKR_PORT" = "7496" ] || [ "$IBKR_PORT" = "4001" ]; then
                echo -e "  Mode: ${RED}LIVE TRADING${NC}"
            fi
            echo ""
            echo -e "  ${GREEN}Ready to run the bot!${NC}"
            echo "  python run.py $TRADING_MODE"
            return 0
        else
            echo -e "  ${RED}NOT CONNECTED${NC} - Nothing listening on port $IBKR_PORT"
            return 1
        fi
    elif command -v python3 &>/dev/null; then
        if python3 -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(2)
try:
    s.connect(('$IBKR_HOST', $IBKR_PORT))
    s.close()
    exit(0)
except:
    exit(1)
" 2>/dev/null; then
            echo -e "  ${GREEN}CONNECTED${NC} - IBKR is accepting connections on port $IBKR_PORT"
            return 0
        else
            echo -e "  ${RED}NOT CONNECTED${NC} - Nothing listening on port $IBKR_PORT"
            return 1
        fi
    else
        echo -e "  ${YELLOW}Cannot check${NC} - neither nc nor python3 available"
        return 1
    fi
}

# ============================================================================
# Show setup instructions
# ============================================================================
show_help() {
    echo ""
    echo -e "${BLUE}=== Option 1: TWS Desktop (Easiest) ===${NC}"
    echo ""
    echo "  1. Open Trader Workstation (TWS)"
    echo "  2. Login with your IBKR credentials"
    echo "  3. Go to: Edit > Global Config > API > Settings"
    echo "  4. Check 'Enable ActiveX and Socket Clients'"
    echo "  5. Set Socket Port: 7497 (paper) or 7496 (live)"
    echo "  6. Uncheck 'Read-Only API'"
    echo "  7. Click Apply + OK"
    echo ""
    echo -e "${BLUE}=== Option 2: IB Gateway (Lighter) ===${NC}"
    echo ""
    echo "  1. Download IB Gateway from:"
    echo "     https://www.interactivebrokers.com/en/trading/ibgateway-stable.php"
    echo "  2. Install and launch"
    echo "  3. Login with your IBKR credentials"
    echo "  4. Select 'Paper Trading' or 'Live Trading'"
    echo "  5. API port auto-configured: 4002 (paper) or 4001 (live)"
    echo ""
    echo -e "${BLUE}=== Option 3: Docker (Headless/Cloud) ===${NC}"
    echo ""
    echo "  # Run IB Gateway in Docker (auto-login, no GUI needed)"
    echo "  docker run -d \\"
    echo "    --name ibgateway \\"
    echo "    -p 4002:4002 \\"
    echo "    -e TWS_USERID=your_ibkr_username \\"
    echo "    -e TWS_PASSWORD=your_ibkr_password \\"
    echo "    -e TRADING_MODE=paper \\"
    echo "    -e TWS_ACCEPT_INCOMING=accept \\"
    echo "    ghcr.io/gnzsnz/ib-gateway:stable"
    echo ""
    echo "  Or use: ./scripts/start_ibgateway.sh docker"
    echo ""
    echo -e "${BLUE}=== .env Configuration ===${NC}"
    echo ""
    echo "  # For TWS:"
    echo "  IBKR_HOST=127.0.0.1"
    echo "  IBKR_PORT=7497   # paper=7497, live=7496"
    echo ""
    echo "  # For IB Gateway:"
    echo "  IBKR_HOST=127.0.0.1"
    echo "  IBKR_PORT=4002   # paper=4002, live=4001"
    echo ""
    echo "  # For Docker IB Gateway:"
    echo "  IBKR_HOST=127.0.0.1"
    echo "  IBKR_PORT=4002"
    echo ""
}

# ============================================================================
# Docker startup
# ============================================================================
start_docker() {
    echo -e "${BLUE}Starting IB Gateway via Docker...${NC}"
    echo ""

    if ! command -v docker &>/dev/null; then
        echo -e "${RED}Docker not installed!${NC}"
        echo "Install Docker: https://docs.docker.com/get-docker/"
        exit 1
    fi

    # Check if already running
    if docker ps --format '{{.Names}}' | grep -q '^ibgateway$'; then
        echo -e "${GREEN}IB Gateway Docker container already running${NC}"
        check_connection
        return
    fi

    # Check for credentials
    if [ -z "$IBKR_USERNAME" ] && [ -z "$TWS_USERID" ]; then
        echo -e "${YELLOW}IBKR credentials needed for Docker auto-login.${NC}"
        echo ""
        read -p "IBKR Username: " IBKR_USERNAME
        read -sp "IBKR Password: " IBKR_PASSWORD
        echo ""
    fi

    TWS_USER="${IBKR_USERNAME:-$TWS_USERID}"
    TWS_PASS="${IBKR_PASSWORD:-$TWS_PASSWORD}"
    DOCKER_MODE="${TRADING_MODE:-paper}"

    # Map port based on mode
    if [ "$DOCKER_MODE" = "live" ]; then
        GW_PORT=4001
    else
        GW_PORT=4002
    fi

    echo "Starting IB Gateway ($DOCKER_MODE mode) on port $GW_PORT..."

    docker run -d \
        --name ibgateway \
        --restart unless-stopped \
        -p "$GW_PORT:$GW_PORT" \
        -e TWS_USERID="$TWS_USER" \
        -e TWS_PASSWORD="$TWS_PASS" \
        -e TRADING_MODE="$DOCKER_MODE" \
        -e TWS_ACCEPT_INCOMING=accept \
        -e READ_ONLY_API=no \
        ghcr.io/gnzsnz/ib-gateway:stable

    echo ""
    echo "Waiting for IB Gateway to initialize (30s)..."
    sleep 30

    # Update .env port if needed
    if [ "$IBKR_PORT" != "$GW_PORT" ]; then
        echo -e "${YELLOW}Note: Update your .env file:${NC}"
        echo "  IBKR_PORT=$GW_PORT"
    fi

    # Check connection
    IBKR_PORT=$GW_PORT check_connection
}

# ============================================================================
# Main
# ============================================================================
case "${1:-}" in
    docker)
        start_docker
        ;;
    status)
        check_connection
        ;;
    stop)
        echo "Stopping IB Gateway Docker container..."
        docker stop ibgateway 2>/dev/null && docker rm ibgateway 2>/dev/null
        echo -e "${GREEN}Stopped${NC}"
        ;;
    *)
        if check_connection; then
            echo ""
        else
            show_help
        fi
        ;;
esac
