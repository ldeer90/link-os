# LINK OS Operations and Cutover Runbook

> **Authoritative runtime:** LINK OS runs natively on the Mac mini through SSH
> alias `agencyos-mini`. The stopped local Docker stack is rollback-only and
> must not run alongside the native API or worker. See `MAC_MINI_RUNBOOK.md`.

## 1. Launch and Health

Compose reads the root `.env`. It must contain local PostgreSQL, session, CLI, Instantly, and monday.com configuration; never paste values into commands or documentation.

```bash
curl -fsS http://127.0.0.1:8766/healthz
ssh agencyos-mini 'launchctl print gui/$(id -u)/com.linkos.api'
ssh agencyos-mini 'launchctl print gui/$(id -u)/com.linkos.worker'
ssh agencyos-mini 'cd /Users/laurencedeer/Projects/Codex/link-os && .venv/bin/python deal-tracker/link_os_native.py cli doctor'
```

The API runs Alembic migrations on startup. Open `http://127.0.0.1:8766`, log in, and use `/ops`; the local launch agent maintains an SSH tunnel to the Mac mini. Restart with `launchctl kickstart -k gui/$(id -u)/com.linkos.api` and the equivalent worker label on the Mac mini. Never delete the stopped local Docker volume.

Key controls:

- `LINK_OS_MODE=shadow` blocks managed campaign creation and activation.
- `LINK_OS_MODE=live` only removes the shadow blocker; database and health gates still apply.
- The global outreach pause is the PostgreSQL setting `outreach.global_pause`, not an environment flag.
- Crawl and worker limits are configured by `SCRAPE_*`, `IMPORT_CHUNK_SIZE`, `WORKER_IDLE_SECONDS`, and `JOB_LEASE_MINUTES`.
- Backlink discovery uses `SE_RANKING_API_KEY`, `SE_RANKING_MONTHLY_CREDIT_CAP` (default 10,000), and `SE_RANKING_CACHE_DAYS` (default 30). Never print the key.

## 2. CLI Rules

The CLI is loopback-only, emits JSON, exits non-zero on rejection, and defaults to dry-run. A mutating command requires `--execute`.

```bash
ssh agencyos-mini 'cd /Users/laurencedeer/Projects/Codex/link-os && .venv/bin/python deal-tracker/link_os_native.py cli doctor'
ssh agencyos-mini 'cd /Users/laurencedeer/Projects/Codex/link-os && .venv/bin/python deal-tracker/link_os_native.py cli import --value example.com --value example.org'
ssh agencyos-mini 'cd /Users/laurencedeer/Projects/Codex/link-os && .venv/bin/python deal-tracker/link_os_native.py cli import --value example.com --value example.org --execute'
ssh agencyos-mini 'cd /Users/laurencedeer/Projects/Codex/link-os && .venv/bin/python deal-tracker/link_os_native.py cli reconcile-instantly'
ssh agencyos-mini 'cd /Users/laurencedeer/Projects/Codex/link-os && .venv/bin/python deal-tracker/link_os_native.py cli prepare-campaign --public-evidence --confirm-verification-skipped'
ssh agencyos-mini 'cd /Users/laurencedeer/Projects/Codex/link-os && .venv/bin/python deal-tracker/link_os_native.py cli activate-campaign BATCH_ID'
ssh agencyos-mini 'cd /Users/laurencedeer/Projects/Codex/link-os && .venv/bin/python deal-tracker/link_os_native.py cli campaign-set-estimate'
ssh agencyos-mini 'cd /Users/laurencedeer/Projects/Codex/link-os && .venv/bin/python deal-tracker/link_os_native.py cli prepare-campaign-set --confirm-verification-skipped'
ssh agencyos-mini 'cd /Users/laurencedeer/Projects/Codex/link-os && .venv/bin/python deal-tracker/link_os_native.py cli activate-campaign-set LAUNCH_ID'
ssh agencyos-mini 'cd /Users/laurencedeer/Projects/Codex/link-os && .venv/bin/python deal-tracker/link_os_native.py cli pause-campaign BATCH_ID'
ssh agencyos-mini 'cd /Users/laurencedeer/Projects/Codex/link-os && .venv/bin/python deal-tracker/link_os_native.py cli reconcile-monday'
ssh agencyos-mini 'cd /Users/laurencedeer/Projects/Codex/link-os && .venv/bin/python deal-tracker/link_os_native.py cli retry-job JOB_ID --execute'
ssh agencyos-mini 'cd /Users/laurencedeer/Projects/Codex/link-os && .venv/bin/python deal-tracker/link_os_native.py cli pause-outreach --reason "Safety review" --execute'
ssh agencyos-mini 'cd /Users/laurencedeer/Projects/Codex/link-os && .venv/bin/python deal-tracker/link_os_native.py cli resume-outreach --reason "Approved pilot" --confirm-sender-health --execute'
```

For host-side migration, export only the local CLI token into the shell without printing it, then run `python3 deal-tracker/link_os_cli.py migrate ...`. Migration sources must remain accessible on the host.

## 3. Backlink Discovery

Open `/ops/backlinks` or use the same canonical API through the CLI. An estimate is read-only and may call the zero-credit subscription endpoint:

```bash
ssh agencyos-mini 'cd /Users/laurencedeer/Projects/Codex/link-os && .venv/bin/python deal-tracker/link_os_native.py cli backlink-analysis \
  --mode competitor_prospecting \
  --client client.com \
  --competitor competitor-one.com \
  --competitor competitor-two.com \
  --credit-cap 500'

ssh agencyos-mini 'cd /Users/laurencedeer/Projects/Codex/link-os && .venv/bin/python deal-tracker/link_os_native.py cli backlink-analysis \
  --mode competitor_prospecting \
  --client client.com \
  --competitor competitor-one.com \
  --credit-cap 500 \
  --source-url-filter .com.au \
  --execute --confirm-credit-cap 500'

ssh agencyos-mini 'cd /Users/laurencedeer/Projects/Codex/link-os && .venv/bin/python deal-tracker/link_os_native.py cli backlink-candidates --limit 100'
ssh agencyos-mini 'cd /Users/laurencedeer/Projects/Codex/link-os && .venv/bin/python deal-tracker/link_os_native.py cli review-backlinks ANALYSIS_ID \
  --decisions-file /approved/remote/path/decisions.json --execute'
```

Use `client_profile_audit` to inspect a submitted site's existing referring domains. Audit runs can never approve candidates or create scrape jobs. `competitor_prospecting` accepts up to five explicit competitors; automatic competitor discovery is not used.

Use the optional `--source-url-filter` flag (also available as “Referring URL filter” in the console) for targeted expansions such as `.com.au`. LINK OS records the filter on the run and provider request, includes it in the cache key, and applies it as an SE Ranking referring-URL `contains` filter. It is a preference filter over source URLs, not a domain-suffix validator.

Use `--authority-floor` with the optional `--authority-ceiling` to request a non-overlapping authority slice. For example, `--authority-floor 0 --authority-ceiling 19` expands below an earlier rank-20+ run without paying for the high-authority records again. Both boundaries are persisted on the analysis and provider request and participate in the cache key.

New prospecting runs default to a maximum Domain InLink Rank of 80. Historical analysis showed that roughly 70% of candidate records above 80 were rejected, while those records consumed credits before more attainable publishers because provider results are authority-ranked. Use `--authority-ceiling 100` only for an explicit high-authority audit; existing historical evidence is retained.

LINK OS classifies the anchor text and target path already returned with backlink evidence as `commercial`, `commercial likely`, `brand`, `editorial`, `generic`, `naked URL`, `empty`, or `other`. This is a local, deterministic layer and spends no additional provider credits. Commercial intent adds a modest relevance signal during review and is filterable in the candidate workspace, but it is not sufficient by itself to approve a publisher.

Every paid run requires exact confirmation, uses an advisory lock, enforces the 2,500 per-run and configured monthly caps, and records predicted/actual usage plus balance readbacks. LINK OS first spends two credits per target on `/backlinks/refdomains/count`, stores the raw unique referring-domain denominator, and uses the remainder of the confirmed cap for authority-ranked evidence. The confirmed cap remains absolute: a 500-credit single-target run reserves two count credits and can retrieve at most 498 domains. The console reports raw profile coverage as captured evidence divided by the provider's complete referring-domain count; this denominator includes weak domains below the authority floor. Initial discovery uses one highest-authority backlink per referring domain. Matching evidence requests are cached for 30 days; later refreshes request only new referring domains from the last checkpoint. A transport failure after transmission becomes `credit_state_unknown` and must not be retried without operator acknowledgement.

Approved candidates require public editorial/publisher relevance evidence and confidence of at least 0.75. They create a source-linked canonical import and then pass normal normalization, suppression, previous-contact, dedupe and refresh gates. No discovery path writes to Instantly. The on-demand Codex drain automation works in bounded 100-candidate API submissions, continues across submissions and scheduled invocations until both `awaiting_codex_review` and `manual_review` are empty, then pauses itself. In user-authorized drain mode, inconclusive manual holds receive a final approve-or-reject decision; insufficient positive evidence is rejected rather than left indefinitely. The automation cannot make paid provider calls.

## 4. Snapshot-first Migration

The four required read-only sources are defined in `deal_tracker/ops/migration.py`:

1. standalone Guest Post Deal Tracker
2. standalone Guest Post Outreach crawler
3. packaged LINK OS crawler
4. current LINK OS deal tracker

Use an explicit protected snapshot directory:

```bash
cd /Users/laurencedeer/Projects/Codex/link-os
python3 deal-tracker/link_os_cli.py migrate \
  --snapshot-dir deal-tracker/data/platform/migration-snapshots

python3 deal-tracker/link_os_cli.py migrate \
  --snapshot-dir deal-tracker/data/platform/migration-snapshots \
  --batch-size 500 \
  --execute
```

Execution refuses missing required sources, snapshots every SQLite database before reading, uses a digest-derived idempotent migration ID, and validates every batch readback. Manual approved/listed values outrank extraction; current external state outranks stale history. Retain all SQLite originals and snapshots.

The completed cutover report is stored under `deal-tracker/data/platform/migration-snapshots/` with the migration ID in its filename.

## 5. Instantly Cutover

Keep `LINK_OS_MODE=shadow` and outreach paused while performing these steps:

```bash
# Read-only: validates all 12 required IDs, the 13-match guard, and remote status.
ssh agencyos-mini 'cd /Users/laurencedeer/Projects/Codex/link-os && .venv/bin/python deal-tracker/link_os_native.py cli reconcile-instantly \
  --pause-legacy --expected-legacy-count 13'

# Explicitly converts unhealthy/active legacy campaigns to a manual pause,
# reads every campaign back, then performs full workspace membership dedupe.
ssh agencyos-mini 'cd /Users/laurencedeer/Projects/Codex/link-os && .venv/bin/python deal-tracker/link_os_native.py cli reconcile-instantly \
  --pause-legacy --expected-legacy-count 13 --execute'
```

The full reconciliation scans all campaigns and leads, protects historical members from recontact, and records a completion checkpoint. Future reply polls cover guarded legacy campaigns and every `LINK OS |` managed campaign.

LINK OS retains membership from every Instantly campaign for workspace-wide duplicate-contact protection, but operational funnel reporting is limited to guest-post and link-building campaigns. That reporting scope includes the fixed historical publisher campaigns, the historical AU publisher follow-up campaign, and future `LINK OS |` managed campaigns. SEO audits, referral-partner outreach, and other unrelated campaigns remain queryable under **Domains → Other Instantly campaigns only** and do not inflate the link-building funnel.

For visual clarity, the historical funnel assumes that a scoped legacy Instantly contact passed through discovery and contact capture even when the old workflow did not retain its source page. An offer or listing also implies a publisher response. Upload and send stages remain based on actual Instantly evidence. This keeps the displayed funnel monotonic without inflating provider-backed activity. Strict public-page and verification evidence remain available as separate Domains filters for audits and future send eligibility.

The Overview reports outreach evidence separately from the domain lifecycle in
plain language. `Instantly leads uploaded` means a lead was added to a campaign.
`Sent confirmed` requires recorded delivery activity: a contact/open/reply/click
timestamp, an executed campaign step, a completed/bounced/unsubscribed lead
state, or a canonical sent-email event. `Uploaded — no send record` means none
of those signals exists. These leads remain protected from automatic recontact
without being counted as sent. The same breakdown is shown for strict domains
ending in `.com.au`.

The Overview funnel has two intentionally different views. `Historical reach`
starts with every canonical domain and counts durable milestone evidence from
scrape attempts, public contact evidence, verification, Instantly delivery,
replies, offers and listings. Legacy imports can retain a later milestone
without an earlier LINK OS crawl record. `Current state` is the mutually
exclusive lifecycle distribution and always sums to the canonical domain total.
Every funnel row uses the same server-side filter as the Domains registry.

Reconnect at least one Instantly sender manually. Do not resume until health shows the sender connected, all legacy campaigns manually paused, full reconciliation complete, and no upload, verification, or safety blocker.

When explicitly approved, a public-evidence pilot can be staged without spending Instantly verification credits:

```bash
# Dry-run validation only.
ssh agencyos-mini 'cd /Users/laurencedeer/Projects/Codex/link-os && .venv/bin/python deal-tracker/link_os_native.py cli prepare-campaign \
  --public-evidence --confirm-verification-skipped'

# Creates one 25-contact Instantly campaign, uploads with verification disabled,
# reads back all members, and leaves the campaign paused.
ssh agencyos-mini 'cd /Users/laurencedeer/Projects/Codex/link-os && .venv/bin/python deal-tracker/link_os_native.py cli prepare-campaign \
  --public-evidence --confirm-verification-skipped --execute'
```

This policy requires public-page evidence confidence of at least 0.75, excludes guessed, suppressed, previously contacted and already reserved contacts, and retains Instantly workspace dedupe/blocklist checks. “Verification skipped” is not represented as verified deliverability. Preparation is allowed while LINK OS remains in shadow mode and globally paused; activation is not.

After switching LINK OS to live mode and using the canonical resume command, activate only the reviewed prepared batch:

```bash
ssh agencyos-mini 'cd /Users/laurencedeer/Projects/Codex/link-os && .venv/bin/python deal-tracker/link_os_native.py cli activate-campaign BATCH_ID'
ssh agencyos-mini 'cd /Users/laurencedeer/Projects/Codex/link-os && .venv/bin/python deal-tracker/link_os_native.py cli activate-campaign BATCH_ID --reason "Approved pilot" --execute'
```

Activation is dry-run by default and is rejected unless the batch is paused, upload/readback counts match, pending verification is zero, the sender is healthy, the workspace reconciliation and legacy-pause gates are complete, and the global pause has been explicitly lifted.

Then set live mode, rebuild/restart, and launch only the 25-contact pilot. The worker uses 10 new leads and 30 total emails per sender per day. Normal 250-contact batches and 15 new leads per day require 72 continuous healthy hours after reconnection. Each sender can participate in only one active LINK OS campaign.

### Segmented public-evidence launch

The segmented launch reserves 75 contacts transactionally: 25 `.com.au`
publishers and two distinct 25-contact travel/global batches sourced from the
recorded travel backlink profiles. It requires public-page confidence of at
least 0.90 and a submissions, contributor, editorial, editor, media, or
advertising role classification. Instantly verification remains explicitly
skipped and must be confirmed during preparation.

Run `campaign-set-estimate`, then prepare with
`prepare-campaign-set --confirm-verification-skipped --execute`. Preparation
creates three paused campaigns, assigns three distinct healthy senders, uploads
25 members to each, and verifies provider readback. After all jobs succeed,
resume outreach through the normal health gate and use
`activate-campaign-set LAUNCH_ID --execute`.

This launch pauses only the affected campaign after three hard bounces, one
unsubscribe, or provider bounce protection. Other clean launch campaigns may
continue. Systemic integrity failures and an entirely blocked launch retain the
global emergency pause.

## 6. Emergency Pause and Incidents

```bash
ssh agencyos-mini 'cd /Users/laurencedeer/Projects/Codex/link-os && .venv/bin/python deal-tracker/link_os_native.py cli pause-outreach \
  --reason "Describe the incident" --execute'
```

This persists the PostgreSQL pause first, then pauses and reads back every remotely active managed campaign. If Instantly is unavailable, the local pause remains authoritative and the response reports a remote failure. Reconciliation retries remote pauses in live mode.

Outreach remains paused for:

- sender connection or provider errors
- an unmapped active LINK OS campaign
- upload/readback mismatch
- pending verification
- bounce protection
- rolling bounce rate at or above 3%
- rolling unsubscribe rate at or above 1%
- sender daily limit reached
- missing full-reconciliation or legacy-pause confirmation

Inspect `/ops/campaigns`, `/ops/settings`, API health, and worker logs. Fix the underlying condition, reconcile again, and only then use `resume-outreach --confirm-sender-health --execute`.

## 7. Worker and Job Recovery

The worker uses leased PostgreSQL jobs. A crashed lease becomes available after expiry until `max_attempts`; exhausted jobs become `dead`.

```bash
ssh agencyos-mini 'tail -200 "$HOME/Library/Logs/LinkOS/worker-error.log"'
ssh agencyos-mini 'cd /Users/laurencedeer/Projects/Codex/link-os && .venv/bin/python deal-tracker/link_os_native.py cli retry-job JOB_ID'
ssh agencyos-mini 'cd /Users/laurencedeer/Projects/Codex/link-os && .venv/bin/python deal-tracker/link_os_native.py cli retry-job JOB_ID --execute'
```

Reply sync runs every ten minutes, analytics/managed delivery reconciliation every fifteen minutes, and a full reconciliation at 03:00 UTC daily. Jobs are idempotent by stable keys.

## 8. monday.com Projection

Monday is not an outreach queue. It receives deals only: Publisher Inventory, Publisher Entities, Reply Review exceptions, and Sync Log.

```bash
ssh agencyos-mini 'cd /Users/laurencedeer/Projects/Codex/link-os && .venv/bin/python deal-tracker/link_os_native.py cli reconcile-monday'
ssh agencyos-mini 'cd /Users/laurencedeer/Projects/Codex/link-os && .venv/bin/python deal-tracker/link_os_native.py cli reconcile-monday --execute'
```

The dry run must report `projection_ready: true`. It pins the four destination IDs from canonical mappings (or explicit `MONDAY_*_BOARD_ID` settings), verifies the exact names/workspace and that no enabled guest subscribes to a shareable board, checks compatible column types and status labels, reads back every mapped external item, and blocks duplicate targets. It never creates boards or columns.

Reply Review projects one item per canonical reply even when a reply produced several offer candidates. Full correspondence stays in the admin-only LINK OS console; Monday receives a safe evidence reference, extracted state, and review action. Resolved, rejected, and lost lifecycle changes update previously projected items instead of leaving stale review or confirmed rows.

Execution reruns that live preflight before it enqueues one idempotent worker job. PostgreSQL permits only one Monday reconciliation at a time. New items use a deterministic name that is searched before create, allowing a retry to recover a remote item created immediately before a crash. Each confirmed item write is committed with its stable mapping before the next item, so a partial failure can leave an auditable partial projection. Fix the integration issue and rerun; committed mappings prevent duplicate projection rows. A daily full reconciliation provides recovery coverage. Review the mapping counts and Sync Log after completion.

## 9. Backup and Recovery

Pause outreach before database maintenance.

```bash
ssh agencyos-mini 'cd /Users/laurencedeer/Projects/Codex/link-os && .venv/bin/python deal-tracker/link_os_backup.py'
ssh agencyos-mini 'cd /Users/laurencedeer/Projects/Codex/link-os/deal-tracker && DATABASE_URL=postgresql+psycopg:///link_os ../.venv/bin/alembic current'
```

Copy dumps to an approved private backup location without printing contents. For restore, stop the worker, restore into a fresh PostgreSQL database/volume, run Alembic, start the API, run `doctor`, compare canonical counts, then start the worker while outreach remains paused.

Rollback means restoring a prior PostgreSQL dump and validating it; never write back into the legacy SQLite sources. Keep the migration snapshots and reconciliation report with the recovery set.

## 10. Acceptance Checks

```bash
ssh agencyos-mini 'cd /Users/laurencedeer/Projects/Codex/link-os/deal-tracker && PYTHONPATH=.:../prospecting ../.venv/bin/pytest -q'
ssh agencyos-mini 'cd /Users/laurencedeer/Projects/Codex/link-os/deal-tracker && RUN_LINK_OS_LARGE_IMPORT_TEST=1 PYTHONPATH=.:../prospecting ../.venv/bin/pytest -q tests/test_large_import.py'
ssh agencyos-mini 'cd /Users/laurencedeer/Projects/Codex/link-os/web && export PATH=/opt/homebrew/bin:$PATH && npm test && npm run lint && npm run build && npm audit'
curl -fsS http://127.0.0.1:8766/healthz
```

Before any pilot, read back sender health, legacy manual-pause status, global pause, pilot membership count, remote campaign status, and Instantly analytics. Do not automatically reply to or negotiate with publishers.
