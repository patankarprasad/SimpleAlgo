#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# SimpleAlgo — Complete VPS Setup Script
# Tested on: Ubuntu 22.04 (Oracle Cloud Free Tier)
# App port:  8880 (Kite auth + web dashboard)
#
# Usage: bash deploy/vps_setup.sh
#   (Run from the project root after cloning, or let the script clone for you)
# ─────────────────────────────────────────────────────────────────────────────

set -e

# ── Configuration ─────────────────────────────────────────────────────────────
INSTALL_DIR=/home/ubuntu/SimpleAlgo
REPO_URL="https://github.com/YOUR_USERNAME/SimpleAlgo.git"   # ← update this
APP_PORT=8880
DOMAIN="trade.yourdomain.com"                                 # ← update this
SERVICE_NAME=simplealgo

# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 — Timezone (CRITICAL: all market schedules are IST/Asia/Kolkata)
# ─────────────────────────────────────────────────────────────────────────────
echo "── Setting timezone to Asia/Kolkata ──"
sudo timedatectl set-timezone Asia/Kolkata
timedatectl

# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 — System packages
# ─────────────────────────────────────────────────────────────────────────────
echo "── Updating system packages ──"
sudo apt-get update -y
sudo apt-get upgrade -y
sudo apt-get install -y python3 python3-pip python3-venv git nginx certbot python3-certbot-nginx

# ─────────────────────────────────────────────────────────────────────────────
# STEP 3 — Clone or update repository
#
# MANUAL ALTERNATIVE: If you prefer to upload files via scp/sftp instead:
#   scp -r /local/SimpleAlgo ubuntu@<VPS_IP>:/home/ubuntu/SimpleAlgo
# ─────────────────────────────────────────────────────────────────────────────
echo "── Setting up project directory ──"
if [ -d "$INSTALL_DIR/.git" ]; then
    echo "   Repo already exists — pulling latest..."
    cd "$INSTALL_DIR" && git pull
elif [ -d "$INSTALL_DIR" ]; then
    echo "   Directory exists (manually uploaded) — skipping clone."
    cd "$INSTALL_DIR"
else
    sudo git clone "$REPO_URL" "$INSTALL_DIR"
    sudo chown -R ubuntu:ubuntu "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi
cd "$INSTALL_DIR"

# ─────────────────────────────────────────────────────────────────────────────
# STEP 4 — Python virtual environment + dependencies
# ─────────────────────────────────────────────────────────────────────────────
echo "── Creating virtualenv and installing dependencies ──"
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# ─────────────────────────────────────────────────────────────────────────────
# STEP 5 — .env file
#
# This is a MANUAL step — secrets cannot be scripted.
# Template is copied here; you must fill in all values.
#
# IMPORTANT: No inline comments after values in .env.
#   systemd EnvironmentFile includes everything after = as the value.
#   WRONG:   KITE_AUTH_PORT=8880    # port the auth server listens on  ← BREAKS
#   CORRECT: KITE_AUTH_PORT=8880
#
# Required variables:
#   ANGEL_API_KEY, ANGEL_USERNAME, ANGEL_PIN, ANGEL_TOTP_SECRET
#   KITE_API_KEY, KITE_API_SECRET, KITE_USER_ID, KITE_PASSWORD, KITE_PIN
#   KITE_AUTH_PORT=8880
#   KITE_AUTH_PIN         ← Zerodha 2FA PIN
#   KITE_AUTH_SECRET_KEY  ← random 32-char string (Flask session signing key)
#   WEB_USERNAME, WEB_PASSWORD
#   DRY_RUN=false
#   SERVER_BASE_URL=https://<DOMAIN>
#   TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID  (optional)
#   STOCK_FUTURES, STOCK_FUTURES_QTY, STOCK_FUTURES_PRODUCT  (optional)
# ─────────────────────────────────────────────────────────────────────────────
if [ ! -f "$INSTALL_DIR/.env" ]; then
    echo "── Creating .env from template ──"
    cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
    echo ""
    echo "  !! ACTION REQUIRED: Edit .env before starting the service:"
    echo "     nano $INSTALL_DIR/.env"
    echo ""
else
    echo "── .env already exists — skipping (verify values are current) ──"
fi

# ─────────────────────────────────────────────────────────────────────────────
# STEP 6 — Firewall
#
# Oracle Cloud requires THREE separate firewall layers. Missing any one keeps
# the port blocked even if the other two are open.
#
# Layer A — Oracle Console Security List (MANUAL — cannot be scripted):
#   Oracle Cloud Console
#     → Networking → Virtual Cloud Networks → your VCN
#     → Security Lists → Default Security List
#     → Add Ingress Rules (Source CIDR: 0.0.0.0/0, Protocol: TCP):
#         Port 22   (SSH)
#         Port 80   (HTTP)
#         Port 443  (HTTPS)
#         Port 8880 (app direct access)
#
# Layers B and C below are scripted:
# ─────────────────────────────────────────────────────────────────────────────
echo "── Configuring UFW firewall (Layer B) ──"
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw allow "$APP_PORT"/tcp
sudo ufw --force enable
sudo ufw reload
sudo ufw status

echo "── Configuring iptables (Layer C — Oracle's own OS firewall) ──"
sudo iptables -I INPUT -p tcp --dport 22 -j ACCEPT
sudo iptables -I INPUT -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT -p tcp --dport 443 -j ACCEPT
sudo iptables -I INPUT -p tcp --dport "$APP_PORT" -j ACCEPT
sudo apt-get install -y iptables-persistent
sudo netfilter-persistent save

# ─────────────────────────────────────────────────────────────────────────────
# STEP 7 — Nginx reverse proxy
# ─────────────────────────────────────────────────────────────────────────────
echo "── Configuring Nginx reverse proxy ──"
sudo tee /etc/nginx/sites-available/$SERVICE_NAME > /dev/null <<EOF
server {
    listen 80;
    server_name $DOMAIN;

    location / {
        proxy_pass http://127.0.0.1:$APP_PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF

sudo ln -sf /etc/nginx/sites-available/$SERVICE_NAME /etc/nginx/sites-enabled/$SERVICE_NAME
sudo nginx -t
sudo systemctl enable nginx
sudo systemctl reload nginx

# ─────────────────────────────────────────────────────────────────────────────
# STEP 8 — Cloudflare DNS (MANUAL)
#
# In your Cloudflare DNS dashboard:
#   Type: A | Name: trade | Value: <this VPS IP> | Proxy: OFF (grey cloud)
#
# IMPORTANT: Leave Cloudflare proxy OFF during SSL setup in Step 9.
#   Turn it back ON (orange cloud) AFTER certbot successfully issues a cert.
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# STEP 9 — SSL / Let's Encrypt (MANUAL — run after DNS is pointing to this IP)
#
# Prerequisites before running certbot:
#   - DNS A record is pointing to this VPS IP (Step 8)
#   - Cloudflare proxy is OFF (grey cloud)
#   - Nginx is running and port 80 is accessible from the internet
#
# Run:
#   sudo certbot --nginx -d $DOMAIN
#
# Certbot rewrites the nginx config to add HTTPS automatically.
# Verify auto-renewal: sudo certbot renew --dry-run
# After cert is issued, you can re-enable Cloudflare proxy (orange cloud).
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# STEP 10 — systemd service
#
# NOTE: The service file is written inline here (not copied from deploy/) so
#   that EnvironmentFile is included. Without EnvironmentFile the .env secrets
#   are NOT loaded by systemd and the app will fail on startup.
# ─────────────────────────────────────────────────────────────────────────────
echo "── Installing systemd service ──"
sudo tee /etc/systemd/system/$SERVICE_NAME.service > /dev/null <<EOF
[Unit]
Description=SimpleAlgo — Supertrend Algo + Web Dashboard
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/venv/bin/python main.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=$INSTALL_DIR/.env

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable $SERVICE_NAME

# ─────────────────────────────────────────────────────────────────────────────
# Done
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "══════════════════════════════════════════════════════════════════"
echo " Setup complete — complete these MANUAL steps before starting:"
echo "══════════════════════════════════════════════════════════════════"
echo ""
echo "  1. Fill in secrets:    nano $INSTALL_DIR/.env"
echo "     (KITE_AUTH_SECRET_KEY must be a random 32-char string)"
echo ""
echo "  2. Oracle Console:     Open ports 22/80/443/$APP_PORT in Security List"
echo "     Networking → VCN → Security Lists → Add Ingress Rules"
echo ""
echo "  3. Cloudflare DNS:     A record: $DOMAIN → <this IP>, proxy OFF"
echo ""
echo "  4. Issue SSL cert:     sudo certbot --nginx -d $DOMAIN"
echo "     (run after DNS propagates, then re-enable Cloudflare proxy)"
echo ""
echo "  5. Start the service:  sudo systemctl start $SERVICE_NAME"
echo "     Check status:       sudo systemctl status $SERVICE_NAME"
echo "     Live logs:          sudo journalctl -u $SERVICE_NAME -f"
echo ""
echo "  6. Daily Kite login:   https://$DOMAIN/kite/login"
echo "     (or http://<vps-ip>:$APP_PORT/kite/login if no SSL yet)"
echo ""
