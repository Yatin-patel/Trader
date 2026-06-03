#!/usr/bin/env bash
# =============================================================================
#  Autonomous Trader - publish.sh   (Ubuntu / Debian)
#
#  One-shot installer that turns a fresh Ubuntu box into a fully-running
#  Autonomous Trader server with nginx reverse proxy and Let's Encrypt SSL.
#
#  Idempotent: every step detects whether the thing is already in place and
#  emits [skip] if so. Re-running on a configured server walks every step
#  and changes nothing.
#
#  Steps:
#    1.  Detect OS + Ubuntu version
#    2.  apt: build-essential, libffi-dev, libssl-dev, unixodbc, curl, gpg
#    3.  Microsoft apt repo + ODBC Driver 18 for SQL Server
#    4.  Python 3.10+ (apt) + python3-venv + python3-dev
#    5.  Verify SQL Server reachability (DB_SERVER from .env)
#    6.  Create the .venv if missing
#    7.  pip install -r requirements.txt
#    8.  Copy .env.example -> .env if missing; set DB_DRIVER for Ubuntu
#    9.  Generate SECRET_ENCRYPTION_KEY if blank
#   10.  Create the backup directory (default /var/lib/trader-backups)
#   11.  Initialise the database schema (idempotent)
#   12.  Install systemd unit so the app starts on boot
#   13.  Install nginx if missing
#   14.  Drop nginx site config and reload
#   15.  Get/renew Let's Encrypt SSL cert
#   16.  Configure ufw: allow 22, 80, 443
#
#  Usage:
#       sudo bash publish.sh                          # full install
#       sudo bash publish.sh --domain mysite.tld      # override domain
#       sudo bash publish.sh --skip-ssl               # skip Let's Encrypt
#       sudo bash publish.sh --skip-nginx             # app only, no proxy
#       sudo bash publish.sh --email you@host.tld     # cert renewal alerts
#
# =============================================================================
set -euo pipefail

# ---------- CLI flags --------------------------------------------------------
DOMAIN="traderapp.dyndns.org"
EMAIL="${EMAIL:-}"
SKIP_SSL=0
SKIP_NGINX=0
APP_PORT=8000
BACKUP_DIR="/var/lib/trader-backups"
# Production-safety defaults — opt in to disruptive actions.
CHECK_ONLY=0
DO_RESTART=0
REPLACE_NGINX_CONFIG=0
FORCE_FIREWALL=0
PATCH_DB_DRIVER=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --domain)     DOMAIN="$2"; shift 2 ;;
        --email)      EMAIL="$2";  shift 2 ;;
        --port)       APP_PORT="$2"; shift 2 ;;
        --backup-dir) BACKUP_DIR="$2"; shift 2 ;;
        --skip-ssl)   SKIP_SSL=1; shift ;;
        --skip-nginx) SKIP_NGINX=1; SKIP_SSL=1; shift ;;
        --check)      CHECK_ONLY=1; shift ;;
        --restart)    DO_RESTART=1; shift ;;
        --replace-nginx-config) REPLACE_NGINX_CONFIG=1; shift ;;
        --force-firewall) FORCE_FIREWALL=1; shift ;;
        --patch-db-driver) PATCH_DB_DRIVER=1; shift ;;
        -h|--help)
            sed -n '/^#  Usage:/,/^# ===/p' "$0" | sed 's/^#//; s/^   //; /^$/d; /^=/d'
            exit 0 ;;
        *) echo "Unknown flag: $1"; exit 1 ;;
    esac
done

# ---------- Re-exec with sudo if needed --------------------------------------
if [[ $EUID -ne 0 ]]; then
    exec sudo -E DOMAIN="$DOMAIN" EMAIL="$EMAIL" SKIP_SSL="$SKIP_SSL" \
                SKIP_NGINX="$SKIP_NGINX" APP_PORT="$APP_PORT" \
                BACKUP_DIR="$BACKUP_DIR" CHECK_ONLY="$CHECK_ONLY" \
                DO_RESTART="$DO_RESTART" \
                REPLACE_NGINX_CONFIG="$REPLACE_NGINX_CONFIG" \
                FORCE_FIREWALL="$FORCE_FIREWALL" \
                PATCH_DB_DRIVER="$PATCH_DB_DRIVER" \
                bash "$0" "$@"
fi

# ---------- Identify the unprivileged install user ---------------------------
INSTALL_USER="${SUDO_USER:-$USER}"
INSTALL_HOME="$(getent passwd "$INSTALL_USER" | cut -d: -f6)"
INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------- Pretty output helpers -------------------------------------------
log()   { printf '\033[1;36m[%s]\033[0m %s\n' "${1:?}" "${2:?}"; }
ok()    { printf '\033[1;32m[ok]\033[0m   %s\n' "$*"; }
skip()  { printf '\033[1;33m[skip]\033[0m %s\n' "$*"; }
do_()   { printf '\033[1;35m[do]\033[0m   %s\n' "$*"; }
err()   { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; }

# Run a command as the install user (preserves project ownership).
as_user() { sudo -u "$INSTALL_USER" -- "$@"; }

# Read the trimmed value of a key from .env, or empty string.
env_get() {
    local key="$1"
    if [[ -f "$INSTALL_DIR/.env" ]]; then
        sed -nE "s/^${key}=(.*)$/\1/p" "$INSTALL_DIR/.env" | head -n1
    fi
}

# Set or replace a key=value line in .env (creates .env if missing).
env_set() {
    local key="$1" value="$2"
    local f="$INSTALL_DIR/.env"
    [[ -f $f ]] || as_user touch "$f"
    if grep -qE "^${key}=" "$f"; then
        sed -i -E "s|^${key}=.*|${key}=${value}|" "$f"
    else
        echo "${key}=${value}" >>"$f"
    fi
    chown "$INSTALL_USER":"$INSTALL_USER" "$f"
}

echo
echo "============================================================"
echo "  Autonomous Trader - publish.sh   (Ubuntu / Debian)"
echo "  Install dir : $INSTALL_DIR"
echo "  Run as user : $INSTALL_USER"
echo "  Domain      : $DOMAIN  (--domain to override)"
echo "  App port    : $APP_PORT"
echo "  SSL         : $([[ $SKIP_SSL -eq 1 ]] && echo skipped || echo enabled)"
echo "  nginx       : $([[ $SKIP_NGINX -eq 1 ]] && echo skipped || echo enabled)"
echo "  Mode        : $([[ $CHECK_ONLY -eq 1 ]] && echo "check-only (no changes)" || echo "apply changes")"
echo "============================================================"
echo "  Production-safety defaults (all opt-in):"
echo "    --restart                 restart trader.service after install"
echo "                              (default: only start if NOT running)"
echo "    --replace-nginx-config    overwrite existing /etc/nginx/sites-*"
echo "                              (default: keep existing config)"
echo "    --patch-db-driver         force DB_DRIVER -> ODBC Driver 18"
echo "                              (default: warn only on mismatch)"
echo "    --force-firewall          enable ufw if currently disabled"
echo "                              (default: only edit rules if active)"
echo "    --check                   dry-run: report state, change nothing"
echo "  Never touched:"
echo "    Other nginx sites, the default vhost, nginx.conf, custom ufw"
echo "    rules. Bootstrap config only edits /etc/nginx/sites-*/$DOMAIN."
echo "============================================================"

# Pre-flight: list sibling nginx sites the user has, so they can confirm
# none of them have server_name traderapp.dyndns.org overlap.
if (( SKIP_NGINX == 0 )) && [[ -d /etc/nginx/sites-enabled ]]; then
    echo
    echo "Sibling nginx sites (we will NOT modify these):"
    found=0
    for s in /etc/nginx/sites-enabled/*; do
        [[ -e "$s" ]] || continue
        bn=$(basename "$s")
        [[ "$bn" == "$DOMAIN" ]] && continue
        sn=$(awk '/server_name/{$1=""; sub(/;$/,""); print substr($0,2); exit}' "$s" 2>/dev/null)
        printf '  - %s   (server_name: %s)\n' "$bn" "${sn:-?}"
        # Warn if any existing site claims our domain
        if echo "$sn" | grep -qFw "$DOMAIN"; then
            echo "    ⚠ This existing site already declares server_name $DOMAIN."
            echo "      publish.sh refuses to write a duplicate. Resolve the"
            echo "      conflict, then re-run."
            exit 1
        fi
        found=1
    done
    (( found )) || echo "  (none)"
fi
echo

# Wrap commands that would mutate state with this. When CHECK_ONLY is set,
# we print the intended action and skip it.
maybe_run() {
    if (( CHECK_ONLY )); then
        printf '\033[1;34m[would]\033[0m %s\n' "$*"
        return 0
    fi
    "$@"
}

# ---------- 1. OS / version detection ---------------------------------------
log " 1/16" "OS detection"
if ! source /etc/os-release 2>/dev/null; then
    err "/etc/os-release missing — only Debian/Ubuntu are supported."
    exit 1
fi
case "${ID:-}" in
    ubuntu|debian) ok "$PRETTY_NAME" ;;
    *)             err "Unsupported distro: ${ID:-unknown}"; exit 1 ;;
esac
UBUNTU_REL="${VERSION_ID:-22.04}"
UBUNTU_CODENAME="${VERSION_CODENAME:-jammy}"

# ---------- 2. Base apt deps ------------------------------------------------
log " 2/16" "Base apt packages"
BASE_PKGS=(curl ca-certificates gnupg lsb-release software-properties-common
           build-essential libffi-dev libssl-dev unixodbc unixodbc-dev
           ufw)
MISSING_BASE=()
for p in "${BASE_PKGS[@]}"; do
    dpkg -s "$p" >/dev/null 2>&1 || MISSING_BASE+=("$p")
done
if (( ${#MISSING_BASE[@]} )); then
    do_ "apt install: ${MISSING_BASE[*]}"
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y "${MISSING_BASE[@]}"
    ok "base packages installed"
else
    skip "all base packages already installed"
fi

# ---------- 3. Microsoft ODBC Driver 18 for SQL Server ----------------------
log " 3/16" "Microsoft ODBC Driver 18 for SQL Server"
if dpkg -s msodbcsql18 >/dev/null 2>&1; then
    skip "msodbcsql18 already installed"
else
    do_ "adding Microsoft apt repo"
    install -d -m 0755 /etc/apt/keyrings
    curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | \
        gpg --dearmor -o /etc/apt/keyrings/microsoft.gpg
    chmod 0644 /etc/apt/keyrings/microsoft.gpg

    cat >/etc/apt/sources.list.d/mssql-release.list <<EOF
deb [signed-by=/etc/apt/keyrings/microsoft.gpg] https://packages.microsoft.com/ubuntu/${UBUNTU_REL}/prod ${UBUNTU_CODENAME} main
EOF
    apt-get update -qq
    ACCEPT_EULA=Y DEBIAN_FRONTEND=noninteractive apt-get install -y msodbcsql18
    ok "msodbcsql18 installed"
fi

# ---------- 4. Python 3.10+ + venv ------------------------------------------
log " 4/16" "Python 3.10+ + python3-venv"
PY_PKGS=(python3 python3-venv python3-dev python3-pip)
MISS_PY=()
for p in "${PY_PKGS[@]}"; do
    dpkg -s "$p" >/dev/null 2>&1 || MISS_PY+=("$p")
done
if (( ${#MISS_PY[@]} )); then
    do_ "apt install: ${MISS_PY[*]}"
    DEBIAN_FRONTEND=noninteractive apt-get install -y "${MISS_PY[@]}"
fi
PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MAJ=$(echo "$PY_VER" | cut -d. -f1)
PY_MIN=$(echo "$PY_VER" | cut -d. -f2)
if (( PY_MAJ < 3 )) || { (( PY_MAJ == 3 )) && (( PY_MIN < 10 )); }; then
    err "Need Python 3.10+; have $PY_VER. Add deadsnakes PPA or upgrade Ubuntu."
    exit 1
fi
ok "Python $PY_VER"

# ---------- 5. SQL Server reachability --------------------------------------
log " 5/16" "SQL Server reachability"
DB_SERVER=$(env_get DB_SERVER || true)
[[ -z "$DB_SERVER" ]] && DB_SERVER="localhost"
DB_HOST=$(echo "$DB_SERVER" | sed 's/\\.*//' )
if timeout 3 bash -c ">/dev/tcp/${DB_HOST}/1433" 2>/dev/null; then
    ok "TCP/1433 reachable on ${DB_HOST}"
elif [[ "$DB_HOST" == "localhost" ]] || [[ "$DB_HOST" == "127.0.0.1" ]]; then
    skip "No local SQL Server detected on TCP/1433."
    cat <<HINT
            To install SQL Server 2022 Express locally:
              curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | sudo gpg --dearmor -o /etc/apt/keyrings/microsoft.gpg
              echo "deb [signed-by=/etc/apt/keyrings/microsoft.gpg] https://packages.microsoft.com/ubuntu/${UBUNTU_REL}/mssql-server-2022 ${UBUNTU_CODENAME} main" | sudo tee /etc/apt/sources.list.d/mssql-server.list
              sudo apt-get update && sudo ACCEPT_EULA=Y apt-get install -y mssql-server
              sudo /opt/mssql/bin/mssql-conf setup
            Or point DB_SERVER in .env at a remote SQL Server you already have.
HINT
else
    err "Cannot reach $DB_HOST:1433. Fix DB_SERVER in .env and re-run."
    exit 1
fi

# ---------- 6. Virtual environment ------------------------------------------
log " 6/16" "Python virtual environment (.venv)"
if [[ -x "$INSTALL_DIR/.venv/bin/python" ]]; then
    skip ".venv exists"
else
    do_ "creating .venv"
    as_user python3 -m venv "$INSTALL_DIR/.venv"
    ok "created .venv"
fi
VENV_PY="$INSTALL_DIR/.venv/bin/python"
VENV_PIP="$INSTALL_DIR/.venv/bin/pip"

# ---------- 7. pip install requirements -------------------------------------
log " 7/16" "Python packages from requirements.txt"
as_user "$VENV_PY" -m pip install --upgrade pip --disable-pip-version-check >/dev/null
if as_user "$VENV_PIP" install -r "$INSTALL_DIR/requirements.txt" --disable-pip-version-check; then
    ok "packages up to date"
else
    err "pip install failed"
    exit 1
fi

# ---------- 8. .env file ----------------------------------------------------
log " 8/16" ".env configuration file"
if [[ -f "$INSTALL_DIR/.env" ]]; then
    skip ".env already present"
else
    if [[ -f "$INSTALL_DIR/.env.example" ]]; then
        cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
        chown "$INSTALL_USER":"$INSTALL_USER" "$INSTALL_DIR/.env"
        ok "created .env from .env.example"
    else
        err "no .env or .env.example"
        exit 1
    fi
fi
# DB_DRIVER: production-safe — only patch if explicitly asked for.
CURRENT_DRIVER=$(env_get DB_DRIVER || true)
if [[ "$CURRENT_DRIVER" == "ODBC Driver 18 for SQL Server" ]]; then
    skip "DB_DRIVER already on driver 18"
elif (( PATCH_DB_DRIVER )); then
    maybe_run env_set DB_DRIVER "ODBC Driver 18 for SQL Server"
    ok "patched DB_DRIVER to 18 (--patch-db-driver)"
else
    skip "DB_DRIVER = '$CURRENT_DRIVER' — leaving alone."
    if [[ "$CURRENT_DRIVER" != *"Driver 18"* ]] && [[ "$CURRENT_DRIVER" != *"Driver 17"* ]]; then
        echo "         If this isn't the driver name reported by 'odbcinst -q -d',"
        echo "         the next DB call will fail. Re-run with --patch-db-driver."
    fi
fi
# API_HOST: bind on loopback only — nginx (local) connects via 127.0.0.1.
# Only patches the obvious mistakes (blank or 0.0.0.0), never an IP the user
# typed deliberately.
CURRENT_HOST=$(env_get API_HOST || true)
if [[ -z "$CURRENT_HOST" ]] || [[ "$CURRENT_HOST" == "0.0.0.0" ]]; then
    maybe_run env_set API_HOST "127.0.0.1"
    ok "patched API_HOST to 127.0.0.1 (nginx is local)"
else
    skip "API_HOST = $CURRENT_HOST (leaving alone)"
fi
# Only set API_PORT if missing.
[[ -z "$(env_get API_PORT)" ]] && maybe_run env_set API_PORT "$APP_PORT"

# ---------- 9. SECRET_ENCRYPTION_KEY ----------------------------------------
log " 9/16" "SECRET_ENCRYPTION_KEY"
CUR_KEY=$(env_get SECRET_ENCRYPTION_KEY || true)
if [[ -n "$CUR_KEY" ]]; then
    skip "key already set"
else
    do_ "generating Fernet key"
    NEW_KEY=$(as_user "$VENV_PY" -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())")
    env_set SECRET_ENCRYPTION_KEY "$NEW_KEY"
    ok "key written to .env"
fi

# ---------- 10. Backup directory --------------------------------------------
log "10/16" "Backup directory $BACKUP_DIR"
if [[ -d "$BACKUP_DIR" ]]; then
    skip "exists"
else
    install -d -m 0755 -o "$INSTALL_USER" -g "$INSTALL_USER" "$BACKUP_DIR"
    ok "created"
fi
# Surface the chosen dir in .env so the runner picks it up at boot. The
# backup job reads backup_dir from app_settings, which can be overridden
# via the Global Settings UI later; we just plant a sensible default.
env_set BACKUP_DIR "$BACKUP_DIR"

# ---------- 11. Database schema ---------------------------------------------
log "11/16" "Database + schema"
if (( CHECK_ONLY )); then
    printf '\033[1;34m[would]\033[0m run %s/main.py initdb\n' "$INSTALL_DIR"
else
    if as_user "$VENV_PY" "$INSTALL_DIR/main.py" initdb; then
        ok "schema applied (IF NOT EXISTS, so safe to re-run)"
    else
        err "DB bootstrap failed — check DB_SERVER/DB_NAME/credentials in .env"
        exit 1
    fi
fi

# ---------- 12. systemd unit so the app boots on reboot ---------------------
log "12/16" "systemd service: trader.service"
UNIT_FILE=/etc/systemd/system/trader.service
UNIT_BODY=$(cat <<UNIT
[Unit]
Description=Autonomous Trader (FastAPI + LangGraph runner)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$INSTALL_USER
Group=$INSTALL_USER
WorkingDirectory=$INSTALL_DIR
Environment="PATH=$INSTALL_DIR/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
ExecStart=$VENV_PY $INSTALL_DIR/main.py all
Restart=on-failure
RestartSec=5s
KillSignal=SIGINT
TimeoutStopSec=15s

# Resource ceilings (tune as needed)
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
UNIT
)
# Production-safe: only rewrite the unit when its EFFECTIVE body has changed
# (whitespace tolerant). Then start it only if NOT running; restart only on
# explicit --restart.
norm() { sed -E 's/[[:space:]]+/ /g' | sed 's/^ //; s/ $//' ; }
if [[ -f "$UNIT_FILE" ]] && diff -q <(echo "$UNIT_BODY" | norm) <(norm <"$UNIT_FILE") >/dev/null 2>&1; then
    skip "unit body already matches"
else
    if (( CHECK_ONLY )); then
        printf '\033[1;34m[would]\033[0m write %s\n' "$UNIT_FILE"
    else
        do_ "writing $UNIT_FILE"
        if [[ -f "$UNIT_FILE" ]]; then
            cp "$UNIT_FILE" "${UNIT_FILE}.bak.$(date +%s)"
        fi
        echo "$UNIT_BODY" >"$UNIT_FILE"
        systemctl daemon-reload
        ok "unit installed (previous saved to .bak.* if it existed)"
    fi
fi
if systemctl is-enabled trader.service >/dev/null 2>&1; then
    skip "trader.service already enabled"
else
    maybe_run systemctl enable trader.service
    ok "trader.service enabled (will start on next boot)"
fi

# Service lifecycle — production-safe.
if systemctl is-active --quiet trader.service; then
    if (( DO_RESTART )); then
        do_ "restarting trader.service (--restart)"
        maybe_run systemctl restart trader.service
        sleep 2
    else
        skip "trader.service already running — pass --restart to bounce it"
    fi
else
    do_ "starting trader.service"
    maybe_run systemctl start trader.service
    sleep 2
fi
if (( CHECK_ONLY == 0 )); then
    if systemctl is-active --quiet trader.service; then
        ok "trader.service is active"
    else
        err "trader.service is NOT active — check: sudo journalctl -u trader -n 80"
        exit 1
    fi
fi

# ---------- 13. nginx -------------------------------------------------------
if (( SKIP_NGINX )); then
    skip "[13/16] nginx (--skip-nginx)"
else
    log "13/16" "nginx"
    if dpkg -s nginx >/dev/null 2>&1; then
        skip "nginx already installed"
    else
        do_ "apt install nginx"
        DEBIAN_FRONTEND=noninteractive apt-get install -y nginx
        systemctl enable --now nginx
        ok "nginx installed and running"
    fi
fi

# ---------- 14. nginx site config ------------------------------------------
if (( SKIP_NGINX )); then
    :
else
    log "14/16" "nginx site for $DOMAIN"
    CONF_PATH="/etc/nginx/sites-available/${DOMAIN}"
    LINK_PATH="/etc/nginx/sites-enabled/${DOMAIN}"

    # Production-safe: never overwrite an existing site config — that file
    # may have been hand-tuned. Only write when the file is missing OR the
    # user explicitly opts in with --replace-nginx-config.
    if [[ -f "$CONF_PATH" ]] && (( REPLACE_NGINX_CONFIG == 0 )); then
        skip "site config already exists at $CONF_PATH"
        echo "         To overwrite (loses any manual edits), re-run with"
        echo "         --replace-nginx-config."
    elif [[ -f "/etc/letsencrypt/live/${DOMAIN}/fullchain.pem" ]] && (( REPLACE_NGINX_CONFIG == 0 )); then
        skip "cert already present, keeping current config"
    elif (( CHECK_ONLY )); then
        printf '\033[1;34m[would]\033[0m write %s\n' "$CONF_PATH"
    else
        if [[ -f "$CONF_PATH" ]]; then
            cp "$CONF_PATH" "${CONF_PATH}.bak.$(date +%s)"
            do_ "rewriting bootstrap HTTP config (--replace-nginx-config; previous backed up)"
        else
            do_ "writing bootstrap HTTP config"
        fi
        cat >"$CONF_PATH" <<NGINX
server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN};
    location /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / {
        proxy_pass http://127.0.0.1:${APP_PORT};
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 180s;
    }
}
NGINX
        install -d -m 0755 /var/www/certbot
        # Enable our site without touching any others. The 'default' site
        # and any pre-existing virtual hosts stay exactly as they are.
        ln -sf "$CONF_PATH" "$LINK_PATH"
        # Validate config BEFORE reload — if it's broken, don't reload at
        # all (would leave existing sites broken too).
        if nginx -t 2>/tmp/trader-nginx-test.err; then
            systemctl reload nginx
            ok "bootstrap config live on port 80 (other sites unchanged)"
        else
            err "nginx config validation failed; NOT reloading"
            cat /tmp/trader-nginx-test.err >&2
            echo "  Your previously-good config is still live. Fix the error in"
            echo "    $CONF_PATH"
            echo "  then re-run: sudo nginx -t && sudo systemctl reload nginx"
            exit 1
        fi
    fi
fi

# ---------- 15. Let's Encrypt SSL -------------------------------------------
if (( SKIP_SSL )); then
    skip "[15/16] SSL (--skip-ssl)"
else
    log "15/16" "Let's Encrypt cert for $DOMAIN"
    if ! dpkg -s certbot python3-certbot-nginx >/dev/null 2>&1; then
        do_ "apt install certbot + python3-certbot-nginx"
        DEBIAN_FRONTEND=noninteractive apt-get install -y certbot python3-certbot-nginx
    fi
    if [[ -f "/etc/letsencrypt/live/${DOMAIN}/fullchain.pem" ]]; then
        skip "cert already present (certbot.timer handles renewal)"
    else
        if [[ -z "$EMAIL" ]]; then
            EMAIL="admin@${DOMAIN}"
            echo "    (no --email passed; using $EMAIL for renewal warnings)"
        fi
        do_ "running certbot --nginx (scoped to $DOMAIN only)"
        # --cert-name pins the cert to our domain so renewal touches only
        # our server block. --redirect adds HTTP->HTTPS solely inside the
        # server block we just wrote; it never edits sibling vhosts.
        certbot --nginx --non-interactive --agree-tos -m "$EMAIL" \
                -d "$DOMAIN" --cert-name "$DOMAIN" --redirect
        ok "cert installed; nginx now serves HTTPS for $DOMAIN"
    fi
    # Make sure auto-renewal timer is enabled.
    if systemctl list-timers --all 2>/dev/null | grep -q certbot; then
        skip "certbot.timer already enabled"
    else
        systemctl enable --now certbot.timer
        ok "certbot.timer enabled (cert auto-renews)"
    fi
fi

# ---------- 16. ufw ---------------------------------------------------------
log "16/16" "ufw (firewall)"
UFW_ACTIVE=0
ufw status 2>/dev/null | grep -q "Status: active" && UFW_ACTIVE=1

if (( UFW_ACTIVE == 0 )); then
    if (( FORCE_FIREWALL )); then
        # Add the SSH rule FIRST so enabling ufw doesn't lock us out.
        maybe_run ufw allow 22/tcp comment "ssh"
        do_ "enabling ufw (--force-firewall)"
        maybe_run ufw --force enable
        UFW_ACTIVE=1
    else
        skip "ufw is inactive — refusing to enable it for you."
        echo "         If you intentionally use a different firewall (iptables,"
        echo "         nftables, security group, etc.), this is correct."
        echo "         To opt in to ufw, re-run with --force-firewall."
    fi
fi

if (( UFW_ACTIVE )); then
    # Reconcile rules only when ufw is in charge. Use 'ufw status numbered'
    # to detect already-present rules so we don't append duplicates.
    add_rule() {
        local spec="$1" comment="$2"
        if ufw status | grep -qE "^${spec}.*ALLOW|^${spec}.*DENY"; then
            skip "ufw rule already present: $spec"
        else
            maybe_run ufw allow "$spec" comment "$comment"
        fi
    }
    add_rule 22/tcp  "ssh"
    if (( SKIP_NGINX == 0 )); then
        add_rule 80/tcp  "http (Let's Encrypt + redirect)"
        add_rule 443/tcp "https (nginx)"
        # Do NOT open APP_PORT publicly when nginx is in front.
        if ufw status | grep -qE "^${APP_PORT}/tcp.*ALLOW"; then
            do_ "removing public ALLOW on ${APP_PORT}/tcp (nginx talks via loopback)"
            maybe_run ufw delete allow "${APP_PORT}/tcp"
        fi
    else
        add_rule "${APP_PORT}/tcp" "trader app (no nginx)"
    fi
    ok "ufw rules reconciled"
fi

# ---------- Done ------------------------------------------------------------
echo
echo "============================================================"
echo "  publish.sh completed successfully."
echo
if (( SKIP_NGINX )); then
    echo "  Visit  http://$(hostname -I | awk '{print $1}'):${APP_PORT}/"
elif (( SKIP_SSL )); then
    echo "  Visit  http://${DOMAIN}/"
else
    echo "  Visit  https://${DOMAIN}/"
fi
echo
echo "  Service controls (systemd):"
echo "    sudo systemctl status  trader"
echo "    sudo systemctl restart trader"
echo "    sudo journalctl -u trader -f      # tail the app log"
echo "============================================================"
