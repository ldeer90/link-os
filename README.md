# LINK OS

LINK OS is the single local operating system for credit-controlled backlink prospect discovery, publisher prospecting, public-email discovery, verified outreach, reply and offer review, pricing, and the private agency link catalogue.

PostgreSQL is the authoritative operational database. FastAPI serves the versioned API and compiled React console, while a durable worker processes imports, scraping, verification, Instantly reconciliation, replies, and the deals-only monday.com projection. The older SQLite projects remain untouched as recovery sources.

## Launch

The authoritative long-running installation is hosted natively on the Mac mini.
From the operator Mac, open [http://127.0.0.1:8766](http://127.0.0.1:8766); a
launchd-managed SSH tunnel forwards that loopback address to the Mac mini.
Operational and recovery commands are documented in the
[Mac mini runbook](docs/MAC_MINI_RUNBOOK.md).

Docker remains a rollback-only local runtime after the Mac mini cutover. Do not
start it alongside the native worker.

### Recovery-only Docker launch

The local stack uses PostgreSQL, the API/UI, and one worker:

```bash
docker compose up -d --build
docker compose ps
```

Open [http://127.0.0.1:8766](http://127.0.0.1:8766), sign in, and use `/ops`. The API health endpoint is `/healthz`; authenticated platform health is `/api/v1/system/health`.

Never run `docker compose down -v`: the `-v` flag deletes the PostgreSQL volume. A normal stop is:

```bash
docker compose stop
```

## Operator CLI

Commands return structured JSON and are dry-run by default. Run them on the authoritative Mac mini through the native launcher so the private configuration is inherited without printing it:

```bash
ssh agencyos-mini 'cd /Users/laurencedeer/Projects/Codex/link-os && .venv/bin/python deal-tracker/link_os_native.py cli doctor'
ssh agencyos-mini 'cd /Users/laurencedeer/Projects/Codex/link-os && .venv/bin/python deal-tracker/link_os_native.py cli import --value example.com --value publisher.com'
ssh agencyos-mini 'cd /Users/laurencedeer/Projects/Codex/link-os && .venv/bin/python deal-tracker/link_os_native.py cli import --value example.com --value publisher.com --execute'
ssh agencyos-mini 'cd /Users/laurencedeer/Projects/Codex/link-os && .venv/bin/python deal-tracker/link_os_native.py cli reconcile-instantly --pause-legacy --expected-legacy-count 13'
ssh agencyos-mini 'cd /Users/laurencedeer/Projects/Codex/link-os && .venv/bin/python deal-tracker/link_os_native.py cli prepare-campaign --public-evidence --confirm-verification-skipped'
ssh agencyos-mini 'cd /Users/laurencedeer/Projects/Codex/link-os && .venv/bin/python deal-tracker/link_os_native.py cli activate-campaign BATCH_ID'
ssh agencyos-mini 'cd /Users/laurencedeer/Projects/Codex/link-os && .venv/bin/python deal-tracker/link_os_native.py cli campaign-set-estimate'
ssh agencyos-mini 'cd /Users/laurencedeer/Projects/Codex/link-os && .venv/bin/python deal-tracker/link_os_native.py cli prepare-campaign-set --confirm-verification-skipped'
ssh agencyos-mini 'cd /Users/laurencedeer/Projects/Codex/link-os && .venv/bin/python deal-tracker/link_os_native.py cli activate-campaign-set LAUNCH_ID'
ssh agencyos-mini 'cd /Users/laurencedeer/Projects/Codex/link-os && .venv/bin/python deal-tracker/link_os_native.py cli reconcile-monday'
ssh agencyos-mini 'cd /Users/laurencedeer/Projects/Codex/link-os && .venv/bin/python deal-tracker/link_os_native.py cli pause-outreach --reason "Operator pause" --execute'
ssh agencyos-mini 'cd /Users/laurencedeer/Projects/Codex/link-os && .venv/bin/python deal-tracker/link_os_native.py cli backlink-analysis --mode competitor_prospecting --client client.com --competitor competitor.com --credit-cap 500'
ssh agencyos-mini 'cd /Users/laurencedeer/Projects/Codex/link-os && .venv/bin/python deal-tracker/link_os_native.py cli backlink-analysis --mode competitor_prospecting --client client.com.au --competitor competitor.com --credit-cap 500 --source-url-filter .com.au'
ssh agencyos-mini 'cd /Users/laurencedeer/Projects/Codex/link-os && .venv/bin/python deal-tracker/link_os_native.py cli backlink-candidates --limit 100'
```

`resume-outreach` remains fail-closed until LINK OS is in live mode, the full Instantly membership reconciliation is complete, all guarded legacy campaigns have a confirmed manual pause, and at least one sender is healthy.

`backlink-analysis` is also dry-run by default. Paid execution requires both `--execute` and an exact `--confirm-credit-cap N`. Configure `SE_RANKING_API_KEY` privately in the root `.env`; the key is only passed to the API and worker containers and is never returned by health endpoints.

## Safety State

- Outreach starts globally paused in PostgreSQL.
- Strict batches require publicly evidenced emails with Instantly status `verified` and `catch_all=false`.
- An explicitly confirmed 25-contact public-evidence pilot may skip Instantly verification. It is labelled as unverified, uploaded with provider verification disabled, and prepared paused before any separate activation decision.
- A single 25-contact pilot is required before 250-contact batches.
- Normal batches require 72 continuous healthy sender hours.
- A sender can have only one active LINK OS campaign.
- Bounce rate at or above 3%, unsubscribe rate at or above 1%, bounce protection, sender errors, verification pending, or upload/readback mismatch pauses outreach.
- LINK OS never automatically replies to or negotiates with publishers.
- Catalogue inventory stays private until separately listed.
- SE Ranking discovery defaults to 500 credits, is capped at 2,500 per run and 10,000 per month, reserves two credits per target to measure total referring domains, reports raw profile coverage, reuses matching evidence for 30 days, and never sends a candidate directly to Instantly.
- Prospect discovery defaults to Domain InLink Rank 20–80 so authority-ranked provider results do not spend the first credits on very high-authority sites that rarely accept paid placements; an explicit ceiling of 100 remains available for audits.
- Existing backlink anchor text is classified locally for commercial intent, exposed as a candidate filter, and used as a modest review signal without consuming additional SE Ranking credits.

## Repository Layout

- `deal-tracker/`: canonical backend, worker, migrations, integrations, CLI, and tests.
- `web/`: React/Vite/TypeScript operations console.
- `prospecting/`: retained discovery adapters and historical utilities used through LINK OS.
- `docs/`: architecture and operating runbooks.

## Verification

```bash
cd deal-tracker
PYTHONPATH=.:../prospecting pytest -q
RUN_LINK_OS_LARGE_IMPORT_TEST=1 PYTHONPATH=.:../prospecting pytest -q tests/test_large_import.py

cd ../web
npm test
npm run lint
npm run build
npm audit --omit=dev
```

The large-import acceptance test streams 100,000 rows into a disposable database and verifies exact row counts, canonical dedupe, and one scrape job per domain.

## Documentation

- [Operations and cutover runbook](docs/OPERATIONS_RUNBOOK.md)
- [System map](docs/SYSTEM_MAP.md)
- [Discovery reference](docs/DISCOVERY_RUNBOOK.md)
- [Backlink-provider reference](docs/BACKLINK_PROVIDER_INTEGRATIONS.md)

The older deployment, deal-tracker, and Instantly runbooks describe recovery-era workflows; use the operations runbook for the canonical platform.

## Data Safety

Secrets, PostgreSQL data, SQLite snapshots, generated imports, cached HTML, and publisher reply evidence are excluded from Git. Do not print or commit `.env` values. BigQuery is optional aggregate analytics only and does not receive new raw emails or replies.
