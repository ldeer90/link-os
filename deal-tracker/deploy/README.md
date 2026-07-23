# Legacy DigitalOcean Droplet Deployment

> Recovery reference only. LINK OS now runs the root Docker Compose PostgreSQL/API/worker stack documented in `docs/OPERATIONS_RUNBOOK.md`.

Recommended target: Ubuntu 24.04 LTS droplet, 1 GB RAM minimum, Sydney/Singapore region.

## One-Time Server Setup

```bash
cd "/Users/laurencedeer/Projects/Codex/link-os/deal-tracker"
bash deploy/deploy_to_droplet.sh root@DROPLET_IP
```

The deploy script uses `~/.ssh/guest_post_deal_tracker_do` automatically when present. To use a different key:

```bash
SSH_KEY=~/.ssh/another_key bash deploy/deploy_to_droplet.sh root@DROPLET_IP
```

On the droplet, create `/opt/guest-post-deal-tracker/.env` from `deploy/production.env.example` and add real secrets. The systemd service can start without this file, but Instantly sync and bootstrap credentials need it for production use.

## Caddy

For a real domain, copy `deploy/Caddyfile.example` to `/etc/caddy/Caddyfile`, replace `deals.example.com`, then point DNS A record at the droplet IP.

For temporary IP access, use the `:80` block in the example.

```bash
sudo systemctl restart guest-post-deal-tracker caddy
sudo systemctl status guest-post-deal-tracker --no-pager
```

## Data

SQLite lives at:

```text
/opt/guest-post-deal-tracker/data/link_deals.db
```

Back this file up before major deploys.
