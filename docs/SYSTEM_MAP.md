# LINK OS System Map

## Runtime

```text
Browser / Codex CLI
        |
        v
FastAPI + compiled React console  <---- SSE status stream
        |
        v
PostgreSQL (authoritative state + leased jobs)
        |
        v
Durable worker / scheduler
   |            |          |          |
SE Ranking   public web  Instantly  monday.com
discovery    scraping    authority  deals-only projection
```

Docker Compose runs three services: `postgres`, `api`, and `worker`. No Redis or second application database is required.

## Source-of-truth Boundaries

- PostgreSQL owns backlink analyses, credit usage, candidates and review decisions plus imports, domains, lifecycle events, evidence, contacts, suppressions, verification, jobs, campaign reservations, replies, offers, publisher entities, listings, mappings, worker runs, users, sessions, and audit logs.
- SE Ranking is the paid backlink-data provider. LINK OS owns caps, caching, provider request hashes, checkpoints and review state; credentials are never stored in PostgreSQL.
- Instantly is authoritative for sender connection, remote campaign and lead state, delivery analytics, email threads, and replies. Reconciliation is idempotent by provider IDs.
- monday.com is a one-way, deals-only projection for Publisher Inventory, Publisher Entities, Reply Review, and Sync Log.
- BigQuery may receive aggregate analytics only. It does not operate queues and does not receive new raw email or reply content.
- Legacy SQLite databases are read-only recovery sources after snapshot-first migration.

## Lifecycle

```text
imported -> queued -> scraping -> email_found / no_email / failed
         -> verified / suppressed -> outreach_ready -> campaign_queued
         -> uploaded -> contacted -> replied -> offer_approved / offer_review
         -> listed / lost
```

Transitions are recorded as domain events. Contact and domain uniqueness use normalized emails and registrable domains. Shared publisher contacts are represented separately so one email can cover several sites without duplicate outreach.

## Intake and Crawling

The API accepts pasted domains/URLs and streamed CSV/TSV files without a business-level row cap. Imports return an ID immediately and are processed in bounded chunks. The crawler accepts valid public domains across TLDs, obeys robots rules, uses one request per host with a host delay, and only records emails visibly present on public pages.

## Backlink Discovery

Competitor mining and client-profile audits use the same API, PostgreSQL queue, audit log and SSE stream as every other LINK OS operation. Initial competitor runs request one authority-ranked example per referring domain. Thirty-day cache hits cost zero; later refreshes use new referring-domain history from the last checkpoint. Local protected-domain exclusions run before Codex review.

Rejected candidates remain discovery records and never create canonical domains. Approved competitor candidates enter the normal import pipeline. Client-profile audit candidates are permanently audit-only. The default run cap is 500 credits, with a 2,500 hard run maximum and 10,000 configurable monthly LINK OS cap.

## Outreach Control

Campaign membership is transactionally reserved in PostgreSQL before a paused Instantly campaign is created. Upload counts, lead readback, verification state, sender exclusivity, daily limits, safety metrics, global pause, full reconciliation, and legacy-pause confirmation are all reread before activation.

The scheduler polls replies every ten minutes, analytics and managed delivery state every fifteen minutes, and performs a full workspace membership reconciliation daily. Public webhooks can replace polling later when a callback URL exists.

Monday receives a single-flight, one-way deals projection to four pinned boards. Destination access, schema/status labels, canonical mappings, remote item locations, and duplicate targets are preflighted before writes. Full reply correspondence remains in LINK OS.

## Operator Surfaces

- React console at `/ops` for overview, intake, domains, scraping, backlink discovery, campaigns, replies/offers, inventory, agencies, settings, and integration health.
- Versioned JSON APIs under `/api/v1` plus server-sent events.
- `link_os_cli.py` for health, imports, backlink estimates/execution/review, reconciliation, job retry, pause/resume, and migration.
