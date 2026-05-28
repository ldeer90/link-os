# Guest Post Deal Tracker

Standalone local app for tracking successful guest post / paid link deals from Instantly replies.

## Setup

```bash
cd "/Users/laurencedeer/Desktop/Guest Post Deal Tracker"
python3 -m pip install -r requirements.txt
cp .env.example .env
```

Add `INSTANTLY_API_KEY` to `.env`, or export it in the shell.

## Sync Replies

```bash
python3 tools/sync_instantly_replies_to_deals.py
```

Default campaigns:

- Active: `7bc7d5e3-924d-463b-b875-8e8301be264b`
- Old paused: `945b0afd-7c53-492d-9afe-4c44f57173ad`

Inbound replies are stored in `data/link_deals.db`. Deals are only created when a reply includes pricing, requirements, or a clear paid-placement path.

## Run Webapp

```bash
python3 -m uvicorn app.main:app --reload --port 8766
```

Open:

```text
http://127.0.0.1:8766
```

## Local Admin And Agency Catalogue

The app now has two surfaces:

- Admin: `http://127.0.0.1:8766/admin`
- Agency catalogue: `http://127.0.0.1:8766/catalogue`

If no admin exists, the app creates a local bootstrap admin from `.env`:

```text
APP_BOOTSTRAP_ADMIN_EMAIL=admin@guest-post.local
APP_BOOTSTRAP_ADMIN_PASSWORD=change-me-now
```

Change those values before first startup if you want different bootstrap credentials. Agency accounts are created from `/admin/agencies`.

Instantly auto-sync runs on startup when the last successful sync is older than `SYNC_INTERVAL_DAYS`, then checks hourly and syncs again when due. The default interval is 3 days. Manual sync remains available at `/admin/sync`.

## Notes

- This project is self-contained and does not import from the prospecting repo.
- Domain Trust is a manual field for now. Use SE Ranking values later.
- Mamamia-style auto-replies are stored as reply history, not confirmed deals.
- New synced deals are private by default: `needs_review` and not listed until approved in admin.
