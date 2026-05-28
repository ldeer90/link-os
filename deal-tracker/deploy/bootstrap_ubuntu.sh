#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/guest-post-deal-tracker"
APP_USER="guestpost"

sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip rsync curl debian-keyring debian-archive-keyring apt-transport-https

if ! id "$APP_USER" >/dev/null 2>&1; then
  sudo useradd --system --create-home --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
fi

sudo mkdir -p "$APP_DIR/data"
sudo chown -R "$APP_USER:$APP_USER" "$APP_DIR"

if ! command -v caddy >/dev/null 2>&1; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
  sudo apt-get update
  sudo apt-get install -y caddy
fi

cd "$APP_DIR"
sudo -u "$APP_USER" python3 -m venv .venv
sudo -u "$APP_USER" .venv/bin/python -m pip install --upgrade pip
sudo -u "$APP_USER" .venv/bin/python -m pip install -r requirements.txt

sudo cp deploy/guest-post-deal-tracker.service /etc/systemd/system/guest-post-deal-tracker.service
sudo systemctl daemon-reload
sudo systemctl enable guest-post-deal-tracker

echo "Bootstrap complete. Add /opt/guest-post-deal-tracker/.env, configure /etc/caddy/Caddyfile, then run:"
echo "sudo systemctl restart guest-post-deal-tracker caddy"
