#!/bin/bash
# Autonomous Trader - Linux (Ubuntu) Deployment Script
# Run this on the target Ubuntu server (192.168.1.102)

set -e

INSTALL_PATH="${1:-/var/www/trader}"
PORT="${2:-8000}"
SERVICE_NAME="trader"
USER="${3:-$USER}"

echo "========================================"
echo "Autonomous Trader - Linux Deployment"
echo "========================================"
echo ""
echo "Install Path: $INSTALL_PATH"
echo "API Port: $PORT"
echo "Service User: $USER"
echo ""

# Check if running with sufficient privileges
if [ "$EUID" -ne 0 ] && [ ! -w "$INSTALL_PATH" ]; then
    echo "Warning: May need sudo for some operations"
fi

# Step 1: Create installation directory
echo "[1/7] Creating installation directory..."
sudo mkdir -p "$INSTALL_PATH"
sudo chown "$USER:$USER" "$INSTALL_PATH"

# Step 2: Extract archive (if provided)
ARCHIVE_PATH="/tmp/trader_deploy.tar.gz"
if [ -f "$ARCHIVE_PATH" ]; then
    echo "[2/7] Extracting deployment archive..."
    tar -xzf "$ARCHIVE_PATH" -C "$INSTALL_PATH"
    echo "Archive extracted to $INSTALL_PATH"
else
    echo "[2/7] No archive found at $ARCHIVE_PATH - skipping extraction"
fi

cd "$INSTALL_PATH"

# Step 3: Install system dependencies
echo "[3/7] Installing system dependencies..."
sudo apt-get update -qq
sudo apt-get install -y python3-venv python3-pip python3-dev \
    unixodbc-dev libpq-dev build-essential

# Step 4: Create Python virtual environment
echo "[4/7] Setting up Python virtual environment..."
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
    echo "Virtual environment created."
else
    echo "Virtual environment already exists."
fi

# Activate and install dependencies
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# Step 5: Configure environment
echo "[5/7] Configuring environment..."
if [ ! -f ".env" ]; then
    cat > .env << EOF
# Autonomous Trader Configuration
API_HOST=0.0.0.0
API_PORT=$PORT

# Database connection
# For SQL Server on network:
# DB_CONNECTION_STRING=mssql+pyodbc://user:pass@192.168.1.xxx/TraderDB?driver=ODBC+Driver+17+for+SQL+Server

# For local PostgreSQL:
# DB_CONNECTION_STRING=postgresql://user:pass@localhost/traderdb

# Logging
LOG_LEVEL=INFO

# Encryption key (generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
# SECRET_ENCRYPTION_KEY=

EOF
    echo ".env file created. Edit it to add your configuration."
else
    # Update port in existing .env
    sed -i "s/^API_PORT=.*/API_PORT=$PORT/" .env
    echo "Updated API_PORT to $PORT"
fi

# Step 6: Create systemd service
echo "[6/7] Creating systemd service..."
sudo tee /etc/systemd/system/${SERVICE_NAME}.service > /dev/null << EOF
[Unit]
Description=Autonomous Trader API
After=network.target

[Service]
Type=simple
User=$USER
Group=$USER
WorkingDirectory=$INSTALL_PATH
Environment="PATH=$INSTALL_PATH/.venv/bin"
ExecStart=$INSTALL_PATH/.venv/bin/python main.py
Restart=always
RestartSec=10

# Logging
StandardOutput=append:$INSTALL_PATH/server.log
StandardError=append:$INSTALL_PATH/server.log

# Security
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable ${SERVICE_NAME}

# Step 7: Configure firewall (if ufw is active)
echo "[7/7] Configuring firewall..."
if command -v ufw &> /dev/null && sudo ufw status | grep -q "Status: active"; then
    sudo ufw allow $PORT/tcp comment "Autonomous Trader API"
    echo "Firewall rule added for port $PORT"
else
    echo "UFW not active or not installed - skipping firewall configuration"
fi

# Summary
echo ""
echo "========================================"
echo "Deployment Complete!"
echo "========================================"
echo ""
echo "Installation Path: $INSTALL_PATH"
echo "API Port: $PORT"
echo ""
echo "Service commands:"
echo "  sudo systemctl start ${SERVICE_NAME}"
echo "  sudo systemctl stop ${SERVICE_NAME}"
echo "  sudo systemctl status ${SERVICE_NAME}"
echo "  sudo journalctl -u ${SERVICE_NAME} -f"
echo ""
echo "To start the service now:"
echo "  sudo systemctl start ${SERVICE_NAME}"
echo ""
echo "Or run manually:"
echo "  cd $INSTALL_PATH"
echo "  source .venv/bin/activate"
echo "  python main.py"
echo ""
echo "Access the API at: http://192.168.1.102:$PORT"
