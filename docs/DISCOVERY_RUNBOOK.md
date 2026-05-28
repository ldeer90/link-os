# Discovery Runbook

Run these from the repo root with Docker, or from each subfolder with local Python.

## Search Operator Harvest

```bash
docker compose run --rm prospecting python tools/search_operator_harvest.py \
  --query-offset 0 \
  --query-limit 300 \
  --workers 8 \
  --timeout 3 \
  --engine google
```

Use Apify Google when direct scraping is weak:

```bash
docker compose run --rm prospecting python tools/apify_google_operator_harvest.py \
  --query-offset 0 \
  --query-limit 100 \
  --pages-per-query 1 \
  --batch-size 5
```

Then scrape emails:

```bash
docker compose run --rm prospecting python tools/scrape_emails_from_search_harvest.py \
  generated/search_harvest/*.csv \
  --workers 12 \
  --timeout 4
```

## Paid-Link Scoring

```bash
docker compose run --rm prospecting python tools/score_paid_link_acceptance.py
docker compose run --rm prospecting python tools/discover_missing_paid_link_emails.py --workers 16 --timeout 5
```

## Competitor Backlink Mining

Put provider exports outside Git, then import them into the review workflow.

```bash
docker compose run --rm deal-tracker python tools/build_combined_link_prospect_csv.py
docker compose run --rm deal-tracker python tools/discover_combined_link_prospect_contacts.py --workers 16 --timeout 6
docker compose run --rm deal-tracker python tools/filter_likely_guest_post_candidates.py
```

## Review Rules

Keep rows when there is contactability plus placement intent:

- Explicit pricing, sponsored post, media kit, rate card, advertorial, native advertising, or link insertion language.
- Editorial/contributor evidence plus a relevant email.
- Competitor backlink evidence from a publisher-like domain.

Downgrade or exclude:

- Directories
- `.gov` and public-sector sites
- Generic corporate support/sales pages
- Already-contacted domains
- Existing deal-tracker inventory
