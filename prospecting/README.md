# Guest Post Outreach

The Link OS prospecting engine for Australian `.com.au` websites and manually reviewed international domains that may accept guest posts, sponsored articles, editorial placements, niche edits, or media/advertising packages.

The pipeline is review-first. Discovery creates a local SQLite database and review outputs. The Link OS dashboard can now queue pasted or uploaded domains into this database, and campaign tooling can build drafts or explicitly create paused Instantly campaigns after review.

## Quick Start

Small v1 review crawl:

```bash
python3 tools/scrape_guest_post_domains.py --search-limit 250
python3 tools/discover_guest_post_emails.py --workers 8
python3 tools/classify_guest_post_prospects.py
python3 tools/export_guest_post_review_csv.py
```

Universe workflow:

```bash
python3 tools/discover_from_search.py --search-limit 1000 --pages-per-query 2
python3 tools/discover_from_common_crawl.py --limit 1000
python3 tools/probe_candidate_paths.py
python3 tools/crawl_and_score_universe.py --enqueue-candidates --limit 500
python3 tools/expand_from_confirmed_sites.py --per-domain-limit 25
python3 tools/crawl_and_score_universe.py --limit 500
python3 tools/export_universe.py
```

Fast iteration loop after a discovery pass:

```bash
python3 tools/fast_iterate.py --crawl-limit 300 --workers 16 --timeout 3
```

CSV-first search-operator harvest:

```bash
python3 tools/search_operator_harvest.py \
  --query-offset 0 \
  --query-limit 300 \
  --workers 8 \
  --timeout 3 \
  --engine google

python3 tools/scrape_emails_from_search_harvest.py generated/search_harvest/*.csv \
  --workers 12 \
  --timeout 4

python3 tools/import_search_harvest_csv.py generated/search_harvest/*.csv
python3 tools/fast_iterate.py --crawl-limit 300 --workers 16 --timeout 3
```

Real Google results through Apify, still CSV-first and no database writes:

```bash
python3 tools/apify_google_operator_harvest.py \
  --query-offset 1 \
  --query-limit 25 \
  --pages-per-query 1 \
  --batch-size 1 \
  --output generated/search_harvest/apify_google_results.csv

python3 tools/scrape_emails_from_search_harvest.py generated/search_harvest/apify_google_results.csv \
  --workers 12 \
  --timeout 4
```

For live search, use short timeouts and query chunks:

```bash
python3 tools/discover_from_search.py --query-offset 250 --query-limit 100 --timeout 4 --delay-seconds 0
```

Final review file:

```text
generated/guest_post_prospecting/au_guest_post_candidates_review.csv
```

Universe exports:

```text
generated/guest_post_prospecting/au_guest_post_universe_domains.csv
generated/guest_post_prospecting/au_guest_post_universe_evidence.csv
generated/guest_post_prospecting/rejected_or_unconfirmed_domains.csv
generated/guest_post_prospecting/coverage_summary.json
```

Local database:

```text
data/guest_post_prospecting.db
```

## Notes

- This project is standalone and does not read or write the BuiltWith databases.
- Search scraping and Common Crawl index lookups are best-effort. Search results are cached under `data/raw_domains/search_cache/`.
- Universe discovery is resumable. Each discovery command writes a `discovery_run_id`; failed crawl URLs remain retryable in `crawl_queue`.
- The pipeline prefers public role/business emails such as `editor@`, `advertising@`, `media@`, `partnerships@`, `hello@`, `info@`, and `contact@`.
- Instantly campaign creation is an explicit post-review step. `tools/create_article_submission_instantly_campaign.py` defaults to a local draft; `--execute` creates a paused campaign and uploads the selected leads.
