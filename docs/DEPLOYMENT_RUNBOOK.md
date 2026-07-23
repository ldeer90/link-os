# Legacy Deployment Runbook

> Recovery reference only. The canonical PostgreSQL/API/worker launch and cutover procedure is in [OPERATIONS_RUNBOOK.md](OPERATIONS_RUNBOOK.md).

## Docker Local

```bash
cp .env.example .env
set -a && source .env && set +a
docker compose build
docker compose up -d deal-tracker
```

Open:

```text
http://localhost:8766
```

## Production VPS

Recommended v1 target:

- Ubuntu 24.04
- 1GB RAM minimum
- Docker + Docker Compose
- Caddy or another reverse proxy
- Private `.env` stored on the server

Deploy by pulling Git:

```bash
git clone https://github.com/ldeer90/link-os.git /opt/link-os
cd /opt/link-os
cp .env.example .env
# edit .env
docker compose up -d --build deal-tracker
```

## SQLite Backups

The Docker volume `link_os_deal_data` stores the deal tracker SQLite database.

Create a backup:

```bash
docker compose exec deal-tracker python - <<'PY'
from pathlib import Path
import shutil, time
src = Path('/app/data/link_deals.db')
dst = Path('/app/data') / f'link_deals.backup-{int(time.time())}.db'
if src.exists():
    shutil.copy2(src, dst)
    print(dst)
else:
    print('No database found yet')
PY
```

## Updating Live

```bash
cd /opt/link-os
git pull
docker compose up -d --build deal-tracker
```

Do not overwrite production SQLite from Git. Real data lives in the Docker volume or server backups.
