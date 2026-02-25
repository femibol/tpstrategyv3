# ============================================================================
# IB Gateway / TWS Connection Helper (Windows PowerShell)
# ============================================================================
#
# Usage:
#   .\scripts\start_ibgateway.ps1           # Check status + show help
#   .\scripts\start_ibgateway.ps1 docker    # Start via Docker (headless)
#   .\scripts\start_ibgateway.ps1 status    # Just check connection
#
# Ports:
#   7497 = TWS Paper Trading
#   7496 = TWS Live Trading
#   4002 = IB Gateway Paper
#   4001 = IB Gateway Live
# ============================================================================

param(
    [string]$Action = ""
)

# Load .env if present
$envFile = Join-Path $PSScriptRoot "..\.env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*([^#][^=]+)=(.*)$') {
            $key = $matches[1].Trim()
            $val = $matches[2].Trim()
            if (-not [System.Environment]::GetEnvironmentVariable($key)) {
                [System.Environment]::SetEnvironmentVariable($key, $val, "Process")
            }
        }
    }
}

$IBKR_HOST = if ($env:IBKR_HOST) { $env:IBKR_HOST } else { "127.0.0.1" }
$IBKR_PORT = if ($env:IBKR_PORT) { [int]$env:IBKR_PORT } else { 7497 }
$TRADING_MODE = if ($env:TRADING_MODE) { $env:TRADING_MODE } else { "paper" }

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  IBKR Connection Helper" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

function Test-IBKRConnection {
    Write-Host "Checking IBKR connection at $IBKR_HOST`:$IBKR_PORT..." -ForegroundColor Yellow
    Write-Host ""

    try {
        $tcp = New-Object System.Net.Sockets.TcpClient
        $tcp.ConnectAsync($IBKR_HOST, $IBKR_PORT).Wait(2000) | Out-Null

        if ($tcp.Connected) {
            $tcp.Close()
            Write-Host "  CONNECTED - IBKR is accepting connections on port $IBKR_PORT" -ForegroundColor Green

            if ($IBKR_PORT -eq 7497 -or $IBKR_PORT -eq 4002) {
                Write-Host "  Mode: PAPER TRADING" -ForegroundColor Yellow
            } elseif ($IBKR_PORT -eq 7496 -or $IBKR_PORT -eq 4001) {
                Write-Host "  Mode: LIVE TRADING" -ForegroundColor Red
            }

            Write-Host ""
            Write-Host "  Ready to run the bot!" -ForegroundColor Green
            Write-Host "  python run.py $TRADING_MODE"
            return $true
        } else {
            $tcp.Close()
            Write-Host "  NOT CONNECTED - Nothing listening on port $IBKR_PORT" -ForegroundColor Red
            return $false
        }
    } catch {
        Write-Host "  NOT CONNECTED - Nothing listening on port $IBKR_PORT" -ForegroundColor Red
        return $false
    }
}

function Show-Help {
    Write-Host ""
    Write-Host "=== Option 1: TWS Desktop (Easiest) ===" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  1. Open Trader Workstation (TWS)"
    Write-Host "  2. Login with your IBKR credentials"
    Write-Host "  3. Go to: Edit > Global Config > API > Settings"
    Write-Host "  4. Check 'Enable ActiveX and Socket Clients'"
    Write-Host "  5. Set Socket Port: 7497 (paper) or 7496 (live)"
    Write-Host "  6. Uncheck 'Read-Only API'"
    Write-Host "  7. Click Apply + OK"
    Write-Host ""
    Write-Host "=== Option 2: IB Gateway (Lighter) ===" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  1. Download IB Gateway from:"
    Write-Host "     https://www.interactivebrokers.com/en/trading/ibgateway-stable.php"
    Write-Host "  2. Install and launch"
    Write-Host "  3. Login with your IBKR credentials"
    Write-Host "  4. Select 'Paper Trading' or 'Live Trading'"
    Write-Host "  5. API port auto-configured: 4002 (paper) or 4001 (live)"
    Write-Host ""
    Write-Host "=== Option 3: Docker (Headless) ===" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  docker run -d ``"
    Write-Host "    --name ibgateway ``"
    Write-Host "    -p 4002:4002 ``"
    Write-Host "    -e TWS_USERID=your_ibkr_username ``"
    Write-Host "    -e TWS_PASSWORD=your_ibkr_password ``"
    Write-Host "    -e TRADING_MODE=paper ``"
    Write-Host "    -e TWS_ACCEPT_INCOMING=accept ``"
    Write-Host "    ghcr.io/gnzsnz/ib-gateway:stable"
    Write-Host ""
    Write-Host "  Or use: .\scripts\start_ibgateway.ps1 docker"
    Write-Host ""
    Write-Host "=== .env Configuration ===" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  # For TWS:"
    Write-Host "  IBKR_HOST=127.0.0.1"
    Write-Host "  IBKR_PORT=7497   # paper=7497, live=7496"
    Write-Host ""
    Write-Host "  # For IB Gateway:"
    Write-Host "  IBKR_HOST=127.0.0.1"
    Write-Host "  IBKR_PORT=4002   # paper=4002, live=4001"
    Write-Host ""
}

function Start-Docker {
    Write-Host "Starting IB Gateway via Docker..." -ForegroundColor Cyan
    Write-Host ""

    # Check Docker
    try {
        docker --version | Out-Null
    } catch {
        Write-Host "Docker not installed!" -ForegroundColor Red
        Write-Host "Install Docker Desktop: https://docs.docker.com/desktop/install/windows-install/"
        return
    }

    # Check if already running
    $running = docker ps --format '{{.Names}}' 2>$null | Where-Object { $_ -eq "ibgateway" }
    if ($running) {
        Write-Host "IB Gateway Docker container already running" -ForegroundColor Green
        Test-IBKRConnection
        return
    }

    # Get credentials
    $user = Read-Host "IBKR Username"
    $pass = Read-Host "IBKR Password" -AsSecureString
    $plainPass = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($pass)
    )

    $gwPort = if ($TRADING_MODE -eq "live") { 4001 } else { 4002 }

    Write-Host "Starting IB Gateway ($TRADING_MODE mode) on port $gwPort..."

    docker run -d `
        --name ibgateway `
        --restart unless-stopped `
        -p "${gwPort}:${gwPort}" `
        -e "TWS_USERID=$user" `
        -e "TWS_PASSWORD=$plainPass" `
        -e "TRADING_MODE=$TRADING_MODE" `
        -e "TWS_ACCEPT_INCOMING=accept" `
        -e "READ_ONLY_API=no" `
        ghcr.io/gnzsnz/ib-gateway:stable

    Write-Host ""
    Write-Host "Waiting for IB Gateway to initialize (30s)..." -ForegroundColor Yellow
    Start-Sleep -Seconds 30

    if ($IBKR_PORT -ne $gwPort) {
        Write-Host "Note: Update your .env file: IBKR_PORT=$gwPort" -ForegroundColor Yellow
    }

    $script:IBKR_PORT = $gwPort
    Test-IBKRConnection
}

# Main
switch ($Action.ToLower()) {
    "docker" { Start-Docker }
    "status" { Test-IBKRConnection }
    "stop" {
        Write-Host "Stopping IB Gateway Docker container..."
        docker stop ibgateway 2>$null
        docker rm ibgateway 2>$null
        Write-Host "Stopped" -ForegroundColor Green
    }
    default {
        $connected = Test-IBKRConnection
        if (-not $connected) {
            Show-Help
        }
    }
}
