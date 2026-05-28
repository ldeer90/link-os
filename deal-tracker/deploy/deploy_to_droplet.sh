#!/usr/bin/env bash
set -euo pipefail

if [ "${1:-}" = "" ]; then
  echo "Usage: deploy/deploy_to_droplet.sh root@DROPLET_IP"
  exit 1
fi

TARGET="$1"
APP_DIR="/opt/guest-post-deal-tracker"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/guest_post_deal_tracker_do}"
SSH_OPTS=(-o StrictHostKeyChecking=accept-new)

if [ -f "$SSH_KEY" ]; then
  SSH_OPTS+=(-i "$SSH_KEY")
fi

rsync -az --delete -e "ssh ${SSH_OPTS[*]}" \
  --exclude '.venv' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude '.env' \
  --exclude 'data/link_deals.db-*' \
  ./ "$TARGET:$APP_DIR/"

ssh "${SSH_OPTS[@]}" "$TARGET" "cd $APP_DIR && bash deploy/bootstrap_ubuntu.sh && sudo systemctl restart guest-post-deal-tracker"
