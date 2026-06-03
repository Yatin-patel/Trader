# Autonomous Trader - Windows Server Deployment Script
# Run this on the Windows server (192.168.1.102)

param(
    [string]$InstallPath = "C:\trader",
    [int]$Port = 8000,
    [switch]$CreateService
)

$ErrorActionPreference = "Stop"

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "Autonomous Trader - Deployment Script" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

# Check if running as administrator
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "WARNING: Not running as Administrator. Some operations may fail." -ForegroundColor Yellow
}

# Step 1: Extract archive (assumes trader_deploy.tar.gz is in current directory or temp)
$archivePath = Join-Path $env:TEMP "trader_deploy.tar.gz"
if (-not (Test-Path $archivePath)) {
    $archivePath = ".\trader_deploy.tar.gz"
}

if (Test-Path $archivePath) {
    Write-Host "Extracting deployment archive..." -ForegroundColor Yellow

    if (-not (Test-Path $InstallPath)) {
        New-Item -ItemType Directory -Path $InstallPath -Force | Out-Null
    }

    Push-Location $InstallPath
    tar -xzf $archivePath
    Pop-Location

    Write-Host "Archive extracted to $InstallPath" -ForegroundColor Green
} else {
    Write-Host "No archive found. Assuming files are already in place." -ForegroundColor Yellow
}

# Step 2: Create Python virtual environment
Write-Host "Setting up Python virtual environment..." -ForegroundColor Yellow
Push-Location $InstallPath

if (-not (Test-Path ".venv")) {
    python -m venv .venv
    Write-Host "Virtual environment created." -ForegroundColor Green
} else {
    Write-Host "Virtual environment already exists." -ForegroundColor Yellow
}

# Activate venv
& .\.venv\Scripts\Activate.ps1

# Step 3: Install dependencies
Write-Host "Installing Python dependencies..." -ForegroundColor Yellow
pip install --upgrade pip
pip install -r requirements.txt

# Step 4: Configure environment
Write-Host "Configuring environment..." -ForegroundColor Yellow
$envFile = Join-Path $InstallPath ".env"

if (-not (Test-Path $envFile)) {
    @"
# Autonomous Trader Configuration
API_HOST=0.0.0.0
API_PORT=$Port

# Database connection (SQL Server Express)
DB_CONNECTION_STRING=mssql+pyodbc://./TraderDB?driver=ODBC+Driver+17+for+SQL+Server&TrustServerCertificate=yes

# Logging
LOG_LEVEL=INFO

# Encryption key (generate a new one for production!)
# Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# SECRET_ENCRYPTION_KEY=

"@ | Out-File -FilePath $envFile -Encoding UTF8
    Write-Host ".env file created. Edit it to add your configuration." -ForegroundColor Green
} else {
    # Update port in existing .env
    $content = Get-Content $envFile -Raw
    if ($content -match "API_PORT=\d+") {
        $content = $content -replace "API_PORT=\d+", "API_PORT=$Port"
        $content | Set-Content $envFile
        Write-Host "Updated API_PORT to $Port in .env" -ForegroundColor Green
    }
}

# Step 5: Initialize database
Write-Host "Initializing database schema..." -ForegroundColor Yellow
python main.py initdb
Write-Host "Database initialized." -ForegroundColor Green

# Step 6: Configure firewall
Write-Host "Configuring Windows Firewall..." -ForegroundColor Yellow
$firewallRule = Get-NetFirewallRule -DisplayName "Trader API" -ErrorAction SilentlyContinue

if (-not $firewallRule) {
    New-NetFirewallRule -DisplayName "Trader API" -Direction Inbound -LocalPort $Port -Protocol TCP -Action Allow | Out-Null
    Write-Host "Firewall rule created for port $Port" -ForegroundColor Green
} else {
    Write-Host "Firewall rule already exists." -ForegroundColor Yellow
}

# Step 7: Create Windows service (optional)
if ($CreateService) {
    Write-Host "Creating Windows service..." -ForegroundColor Yellow

    # Check for NSSM
    $nssm = Get-Command nssm -ErrorAction SilentlyContinue
    if (-not $nssm) {
        Write-Host "NSSM not found. Install it from https://nssm.cc/ to create a Windows service." -ForegroundColor Yellow
        Write-Host "Alternatively, run manually: python main.py" -ForegroundColor Yellow
    } else {
        $serviceName = "AutonomousTrader"
        $existingService = Get-Service -Name $serviceName -ErrorAction SilentlyContinue

        if ($existingService) {
            Write-Host "Stopping existing service..." -ForegroundColor Yellow
            nssm stop $serviceName
            nssm remove $serviceName confirm
        }

        $pythonPath = Join-Path $InstallPath ".venv\Scripts\python.exe"
        $mainPath = Join-Path $InstallPath "main.py"

        nssm install $serviceName $pythonPath $mainPath
        nssm set $serviceName AppDirectory $InstallPath
        nssm set $serviceName AppStdout (Join-Path $InstallPath "service_stdout.log")
        nssm set $serviceName AppStderr (Join-Path $InstallPath "service_stderr.log")
        nssm set $serviceName AppRotateFiles 1
        nssm set $serviceName AppRotateBytes 10485760

        Write-Host "Starting service..." -ForegroundColor Yellow
        nssm start $serviceName
        Write-Host "Windows service '$serviceName' created and started." -ForegroundColor Green
    }
}

Pop-Location

# Summary
Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "Deployment Complete!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Installation Path: $InstallPath" -ForegroundColor White
Write-Host "API Port: $Port" -ForegroundColor White
Write-Host ""
Write-Host "To start manually:" -ForegroundColor Yellow
Write-Host "  cd $InstallPath" -ForegroundColor White
Write-Host "  .\.venv\Scripts\Activate.ps1" -ForegroundColor White
Write-Host "  python main.py" -ForegroundColor White
Write-Host ""
Write-Host "Access the API at: http://192.168.1.102:$Port" -ForegroundColor Cyan
