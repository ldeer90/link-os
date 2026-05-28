# AU Link Desk

AU Link Desk is a private operating system for finding Australian paid-link and guest-post opportunities, contacting publishers through Instantly, and turning successful replies into an agency-facing placement catalogue.

It packages two systems:

- `prospecting/`: search-operator discovery, competitor backlink mining, email scraping, scoring, and Instantly lead preparation.
- `deal-tracker/`: FastAPI admin app, agency catalogue, Instantly reply sync, publisher deal database, pricing, and scorecards.

## Quick Start With Docker

```bash
cp .env.example .env
# Add real keys/passwords to .env, then export them for docker compose:
set -a && source .env && set +a

docker compose build
docker compose up -d deal-tracker
```

Open the local app:

```text
http://localhost:8766
```

Run prospecting commands:

```bash
docker compose run --rm prospecting python tools/search_operator_harvest.py --query-offset 0 --query-limit 100
docker compose run --rm prospecting python tools/scrape_emails_from_search_harvest.py generated/search_harvest/*.csv --workers 12 --timeout 4
```

Run Instantly reply sync:

```bash
docker compose run --rm deal-tracker python tools/sync_instantly_replies_to_deals.py
```

## Local Python Development

```bash
cd deal-tracker
python3 -m pip install -r requirements.txt pytest
python3 -m uvicorn app.main:app --reload --port 8766
```

```bash
cd prospecting
python3 -m pip install -r requirements.txt pytest
python3 tools/search_operator_harvest.py --query-offset 0 --query-limit 25
```

## Main Workflows

1. Discover candidate publishers with search operators and competitor backlink exports.
2. Filter out directories, public-sector/corporate noise, already-contacted domains, and existing inventory.
3. Scrape contact routes and score likely paid-link acceptance.
4. Import approved leads to Instantly.
5. Sync replies into the deal tracker.
6. Convert pricing replies into structured publisher inventory.
7. Approve/list catalogue rows for agency users.

## Documentation

- [System map](docs/SYSTEM_MAP.md)
- [Discovery runbook](docs/DISCOVERY_RUNBOOK.md)
- [Instantly runbook](docs/INSTANTLY_RUNBOOK.md)
- [Backlink provider integrations](docs/BACKLINK_PROVIDER_INTEGRATIONS.md)
- [Deployment runbook](docs/DEPLOYMENT_RUNBOOK.md)

## Data Safety

Real databases, generated lead exports, cached HTML, API keys, and publisher reply evidence are intentionally ignored by Git. Keep production data in SQLite volumes, local `data/`, or private backups, not in commits.
