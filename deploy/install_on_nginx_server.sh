#!/usr/bin/env bash
# =============================================================================
# Run THIS on the nginx server (192.168.1.102), not on the Windows app server.
#
# Idempotent:
#   * Installs nginx + certbot only if missing
#   * Replaces the site config (no duplication)
#   * Skips certbot if a valid cert is already present
#   * Reloads (not restarts) nginx so existing connections aren't dropped
#
# Required:
#   * sudo (this script will re-exec with sudo if not run as root)
#   * Internet egress from the nginx server (for Let's Encrypt + apt)
#   * DNS for traderapp.dyndns.org pointing to this server's public IP
#     (port 80 must be reachable from the internet for the http-01 challenge)
#
# Usage:
#   chmod +x install_on_nginx_server.sh
#   sudo ./install_on_nginx_server.sh
# =============================================================================
set -euo pipefail

DOMAIN="traderapp.dyndns.org"
CONF_NAME="${DOMAIN}"
EMAIL="${EMAIL:-admin@${DOMAIN}}"   # override: EMAIL=you@host.tld ./install_on_nginx_server.sh

# ---------- Re-exec with sudo if needed ----------
if [ "$EUID" -ne 0 ]; then
    exec sudo -E "$0" "$@"
fi

log()  { printf '\033[1;36m[nginx-setup]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[ok]\033[0m %s\n' "$*"; }
skip() { printf '\033[1;33m[skip]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

# ---------- 1. Install nginx ----------
log "[1/6] nginx"
if command -v nginx >/dev/null 2>&1; then
    skip "nginx already installed ($(nginx -v 2>&1))"
else
    log "installing nginx ..."
    apt-get update -qq
    apt-get install -y nginx
    ok "nginx installed"
fi

# ---------- 2. Install certbot + nginx plugin ----------
log "[2/6] certbot + python3-certbot-nginx"
if command -v certbot >/dev/null 2>&1; then
    skip "certbot already installed ($(certbot --version 2>&1))"
else
    log "installing certbot ..."
    apt-get install -y certbot python3-certbot-nginx
    ok "certbot installed"
fi

# ---------- 3. Drop the site config ----------
log "[3/6] /etc/nginx/sites-available/${CONF_NAME}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_CONF="${SCRIPT_DIR}/nginx_traderapp.conf"
[ -f "$SRC_CONF" ] || die "Missing $SRC_CONF — did you scp it next to this script?"

# Reset to a no-SSL bootstrap version on first run so `nginx -t` passes
# (certbot will rewrite it to add the SSL lines after issuing the cert).
DST="/etc/nginx/sites-available/${CONF_NAME}"
LINK="/etc/nginx/sites-enabled/${CONF_NAME}"

if [ -L "${LINK}" ] && [ -f "/etc/letsencrypt/live/${DOMAIN}/fullchain.pem" ]; then
    # Cert already exists — keep the existing config to preserve certbot edits.
    skip "site already enabled with active SSL cert"
else
    log "writing bootstrap config ..."
    # Write an HTTP-only bootstrap that lets certbot complete its challenge.
    cat >"${DST}" <<NGINX
server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN};

    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }
    location / {
        return 200 "Bootstrap. Re-run install_on_nginx_server.sh after cert is issued.\n";
        add_header Content-Type text/plain;
    }
}
NGINX
    mkdir -p /var/www/certbot
    ln -sf "${DST}" "${LINK}"
    nginx -t
    systemctl reload nginx
    ok "bootstrap config live on port 80"
fi

# ---------- 4. Get/renew SSL cert ----------
log "[4/6] Let's Encrypt cert for ${DOMAIN}"
if [ -f "/etc/letsencrypt/live/${DOMAIN}/fullchain.pem" ]; then
    skip "cert already exists (cron handles renewal)"
else
    log "requesting cert (this contacts Let's Encrypt) ..."
    certbot --nginx --non-interactive --agree-tos -m "${EMAIL}" \
            -d "${DOMAIN}" --redirect
    ok "cert installed and nginx config updated by certbot"
fi

# ---------- 5. Install the full production config ----------
log "[5/6] full production config (proxy_pass, security headers, caching)"
cp "${SRC_CONF}" "${DST}"
ln -sf "${DST}" "${LINK}"
nginx -t
systemctl reload nginx
ok "production config live"

# ---------- 6. Verify cert auto-renewal ----------
log "[6/6] auto-renewal"
if systemctl list-timers | grep -q certbot; then
    skip "systemd timer 'certbot.timer' already scheduled"
else
    log "ensuring certbot.timer is enabled ..."
    systemctl enable --now certbot.timer
    ok "auto-renewal enabled"
fi

cat <<DONE

================================================================
  Setup complete.

  Site:  https://${DOMAIN}/
  Conf:  ${DST}
  Cert:  /etc/letsencrypt/live/${DOMAIN}/

  Test the cert renewal pipeline without actually renewing:
      sudo certbot renew --dry-run

  Tail the access log:
      sudo tail -f /var/log/nginx/access.log
================================================================
DONE
