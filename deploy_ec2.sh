#!/usr/bin/env bash
# ================================================
# deploy_ec2.sh
# Sets up the app as a systemd service behind Nginx on an Ubuntu EC2
# free-tier instance. Run from the project root: sudo bash deploy_ec2.sh
#
# Assumes:
#   - Ubuntu 22.04/24.04 free-tier EC2 instance
#   - You've already run setup_ec2_swap.sh
#   - Code is checked out at the current working directory
#   - .env file exists in this directory with GOOGLE_API_KEY etc.
# ================================================
set -euo pipefail

APP_DIR="$(pwd)"
APP_USER="${SUDO_USER:-ubuntu}"
SERVICE_NAME="ai-email-assistant"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
NGINX_CONF="/etc/nginx/sites-available/${SERVICE_NAME}"

echo "==> Installing system packages (python3-venv, nginx)..."
apt-get update -y
apt-get install -y python3-venv python3-pip nginx

echo "==> Creating virtualenv and installing Python dependencies..."
python3 -m venv "${APP_DIR}/venv"
"${APP_DIR}/venv/bin/pip" install --upgrade pip
"${APP_DIR}/venv/bin/pip" install -r "${APP_DIR}/requirements.txt"

echo "==> Writing systemd unit at ${SERVICE_FILE}..."
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Correspondent — AI Email Assistant (FastAPI)
After=network.target

[Service]
Type=simple
User=${APP_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
ExecStart=${APP_DIR}/venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000 --workers 1
Restart=on-failure
RestartSec=5
# Free-tier RAM guardrail — restart cleanly instead of taking the box down.
MemoryMax=700M

[Install]
WantedBy=multi-user.target
EOF

echo "==> Writing Nginx reverse-proxy config at ${NGINX_CONF}..."
cat > "$NGINX_CONF" <<'EOF'
server {
    listen 80;
    server_name _;

    client_max_body_size 5M;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Needed for SSE streaming (/api/chat/stream) to flush promptly.
        proxy_buffering off;
        proxy_read_timeout 300s;
    }
}
EOF

ln -sf "$NGINX_CONF" "/etc/nginx/sites-enabled/${SERVICE_NAME}"
[ -f /etc/nginx/sites-enabled/default ] && rm -f /etc/nginx/sites-enabled/default

echo "==> Reloading systemd and starting services..."
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"
nginx -t && systemctl restart nginx

echo ""
echo "Done. Service status:"
systemctl status "$SERVICE_NAME" --no-pager -l | head -n 12
echo ""
echo "App should now be reachable on http://<your-ec2-public-ip>/"
echo "Remember to set REDIRECT_URI in .env to that same address + /api/oauth/callback,"
echo "and register it in Google Cloud Console → Credentials."
echo ""
echo "Logs: journalctl -u ${SERVICE_NAME} -f"
