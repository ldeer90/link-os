# Backlink Provider Integrations

The system should normalize competitor backlink data before filtering. CSV import is the default; APIs can be added behind the same schema.

## Normalized Schema

```text
client_domain
competitor_domain
source_domain
source_url
target_url
anchor_text
link_type
first_seen
last_seen
domain_metric_name
domain_metric_value
traffic_metric
provider
source_file_or_api_run
```

## SE Ranking

Supported now through CSV-style exports. API support should map SE Ranking backlink/referring-domain fields into the normalized schema.

Environment:

```text
SE_RANKING_API_KEY=
```

## Semrush

Semrush backlink API reports are available from:

```text
https://api.semrush.com/analytics/v1/
```

Use `SEMRUSH_API_KEY` and normalize reports such as:

- `backlinks_refdomains`
- `backlinks_pages`
- `backlinks_competitors`
- `backlinks_comparison`

Docs: https://developer.semrush.com/api/seo/backlinks/

## Ahrefs

Ahrefs API v3 Site Explorer exposes backlink data through endpoints such as:

```text
GET https://api.ahrefs.com/v3/site-explorer/all-backlinks
```

Use:

```text
Authorization: Bearer $AHREFS_API_KEY
```

Normalize fields such as `url_from`, `url_to`, `anchor`, `domain_rating_source`, `is_dofollow`, and `is_nofollow`.

Docs: https://docs.ahrefs.com/en/api/reference/site-explorer/get-all-backlinks

## Import Command Shape

Future provider adapter command:

```bash
python tools/import_backlink_prospects.py --provider csv_seranking --input exports/client.csv
python tools/import_backlink_prospects.py --provider api_semrush --client-domain example.com.au --competitor competitor.com.au
python tools/import_backlink_prospects.py --provider api_ahrefs --client-domain example.com.au --competitor competitor.com.au
```
