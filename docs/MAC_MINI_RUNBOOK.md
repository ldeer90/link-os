# LINK OS Mac mini Runtime

The Mac mini is the authoritative LINK OS host. It runs PostgreSQL 17, the
FastAPI/UI process and one durable worker as native macOS services. The API is
bound to the Mac mini's loopback interface and is reached from the operator Mac
through an SSH tunnel.

## Locations

- Repository: `/Users/laurencedeer/Projects/Codex/link-os`
- Private configuration: `/Users/laurencedeer/Projects/Codex/link-os/.env`
- Python environment: `/Users/laurencedeer/Projects/Codex/link-os/.venv`
- PostgreSQL database: `link_os` on the local Homebrew PostgreSQL 17 service
- Runtime files: `deal-tracker/data/platform`
- Backups: `/Users/laurencedeer/Library/Application Support/LinkOS/backups`
- Service logs: `/Users/laurencedeer/Library/Logs/LinkOS`
- Backups: verified daily at 04:15 local time with the newest 30 retained

## Services

On the Mac mini:

```bash
launchctl print gui/$(id -u)/com.linkos.api
launchctl print gui/$(id -u)/com.linkos.worker
launchctl print gui/$(id -u)/com.linkos.backup
curl -fsS http://127.0.0.1:8766/healthz
```

On the operator Mac:

```bash
launchctl print gui/$(id -u)/com.linkos.tunnel
curl -fsS http://127.0.0.1:8766/healthz
```

The console remains available at `http://127.0.0.1:8766/ops` on the operator
Mac. The tunnel uses the existing `agencyos-mini` SSH configuration.

## Commands

Run a command on the authoritative host without exposing credentials:

```bash
ssh agencyos-mini 'cd /Users/laurencedeer/Projects/Codex/link-os && .venv/bin/python deal-tracker/link_os_native.py cli doctor'
```

Other CLI subcommands follow `cli` and retain their existing dry-run and
explicit-execution safeguards.

## Restart

```bash
ssh agencyos-mini 'launchctl kickstart -k gui/$(id -u)/com.linkos.api'
ssh agencyos-mini 'launchctl kickstart -k gui/$(id -u)/com.linkos.worker'
```

Never run a second LINK OS worker or API against the rollback Docker database.
Keep outreach paused during database restoration or host migration.

## Backup

On the Mac mini:

```bash
mkdir -p "$HOME/Library/Application Support/LinkOS/backups"
/opt/homebrew/opt/postgresql@17/bin/pg_dump -Fc link_os \
  -f "$HOME/Library/Application Support/LinkOS/backups/link-os-$(date -u +%Y%m%dT%H%M%SZ).dump"
```

Restore only while the API and worker are stopped. Validate the dump, database
row counts, Alembic revision, `doctor`, and the global outreach pause before
starting the worker.
