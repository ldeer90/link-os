# Deal Tracker Runbook

## Run

```bash
docker compose up -d deal-tracker
```

Admin:

```text
http://localhost:8766/admin
```

Agency catalogue:

```text
http://localhost:8766/catalogue
```

## Sync

```bash
docker compose run --rm deal-tracker python tools/sync_instantly_replies_to_deals.py
```

New synced deals default to:

- `admin_review_status=needs_review`
- `is_listed=false`

## Review Checklist

Before listing a deal:

- Confirm exact domain.
- Confirm dofollow/nofollow pricing.
- Enter reseller price.
- Enter Domain Trust if available.
- Check placement type.
- Check excluded niches.
- Clean public-facing requirements.
- Keep private publisher contact/cost hidden from agencies.

## Publisher Entities

Publisher networks should be grouped under one managing entity. If a reply lists multiple sites, create one row per domain and keep the same contact email/entity.
