from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
from pathlib import Path

from deal_tracker.ops.migration import (
    MigrationPlanner,
    execute_migration,
    normalize_domain,
    normalize_email,
    snapshot_sqlite,
)
from deal_tracker.ops.models import SourceSpec


def _create_current_link_os(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE prospect_records (
                id INTEGER PRIMARY KEY,
                root_domain TEXT,
                source_url TEXT,
                contact_email TEXT,
                lifecycle_stage TEXT,
                updated_at TEXT
            );
            CREATE TABLE instantly_reply_sync (
                id INTEGER PRIMARY KEY,
                campaign_id TEXT,
                email_id TEXT,
                thread_id TEXT,
                from_email TEXT,
                body_text TEXT,
                received_at TEXT
            );
            INSERT INTO prospect_records
              (root_domain, source_url, contact_email, lifecycle_stage, updated_at)
            VALUES
              ('www.Example.com', 'https://example.com/contact',
               'Shared@Example.com', 'queued', '2026-01-01T00:00:00Z');
            INSERT INTO instantly_reply_sync
              (campaign_id, email_id, thread_id, from_email, body_text, received_at)
            VALUES
              ('campaign-old', 'email-private', 'thread-private',
               'shared@example.com', 'PRIVATE REPLY BODY', '2026-01-02T00:00:00Z');
            """
        )


def _create_crawler(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE candidate_domains (
                root_domain TEXT PRIMARY KEY,
                contact_email TEXT,
                updated_at TEXT
            );
            CREATE TABLE guest_post_contact_emails (
                root_domain TEXT,
                email TEXT,
                email_source_url TEXT,
                discovered_at TEXT
            );
            CREATE TABLE domain_evidence (
                id INTEGER PRIMARY KEY,
                root_domain TEXT,
                evidence_url TEXT,
                crawl_timestamp TEXT
            );
            CREATE TABLE guest_post_suppression (
                root_domain TEXT PRIMARY KEY,
                reason TEXT,
                added_at TEXT
            );
            INSERT INTO candidate_domains VALUES
              ('example.com', 'shared@example.com', '2026-02-01T00:00:00Z'),
              ('example.net', 'ads@example.net', '2026-02-01T00:00:00Z');
            INSERT INTO guest_post_contact_emails VALUES
              ('example.com', 'shared@example.com', 'https://example.com/advertise',
               '2026-02-01T00:00:00Z'),
              ('example.net', 'ads@example.net', '', '2026-02-01T00:00:00Z');
            INSERT INTO domain_evidence
              (root_domain, evidence_url, crawl_timestamp)
            VALUES ('example.com', 'https://example.com/advertise',
                    '2026-02-01T00:00:00Z');
            INSERT INTO guest_post_suppression VALUES
              ('never-contact.example', 'manual', '2026-02-01T00:00:00Z');
            """
        )


def _create_deal_tracker(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE instantly_reply_sync (
                id INTEGER PRIMARY KEY,
                campaign_id TEXT,
                email_id TEXT,
                thread_id TEXT,
                from_email TEXT,
                body_text TEXT,
                received_at TEXT
            );
            CREATE TABLE link_deals (
                id INTEGER PRIMARY KEY,
                root_domain TEXT,
                contact_email TEXT,
                instantly_thread_id TEXT,
                placement_type TEXT,
                price_amount REAL,
                price_currency TEXT,
                admin_review_status TEXT,
                is_listed INTEGER,
                updated_at TEXT
            );
            CREATE TABLE publisher_entities (
                id INTEGER PRIMARY KEY,
                group_key TEXT,
                primary_email TEXT,
                primary_domain TEXT,
                updated_at TEXT
            );
            CREATE TABLE monday_item_map (
                object_type TEXT,
                local_id TEXT,
                external_key TEXT,
                board_key TEXT,
                monday_item_id TEXT,
                updated_at TEXT
            );
            INSERT INTO instantly_reply_sync
              (campaign_id, email_id, thread_id, from_email, body_text, received_at)
            VALUES ('campaign-live', 'email-private', 'thread-private',
                    'shared@example.com', 'NEW PRIVATE BODY',
                    '2026-03-01T00:00:00Z');
            INSERT INTO link_deals
              (root_domain, contact_email, instantly_thread_id, placement_type,
               price_amount, price_currency, admin_review_status, is_listed, updated_at)
            VALUES ('example.com', 'shared@example.com', 'thread-private',
                    'guest_post', 250, 'AUD', 'approved', 1,
                    '2026-03-01T00:00:00Z');
            INSERT INTO publisher_entities
              (group_key, primary_email, primary_domain, updated_at)
            VALUES ('entity:shared', 'shared@example.com', 'example.com',
                    '2026-03-01T00:00:00Z');
            INSERT INTO monday_item_map VALUES
              ('deal', '1', 'thread-private', 'publisher_inventory', '12345',
               '2026-03-01T00:00:00Z');
            """
        )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_normalization_accepts_international_public_domains() -> None:
    assert normalize_domain("https://WWW.Example.co.uk/path") == "example.co.uk"
    assert normalize_domain("münich.example") == "xn--mnich-kva.example"
    assert normalize_email(" Editor@MÜNICH.example ") == (
        "editor@xn--mnich-kva.example"
    )


def test_migration_plan_applies_precedence_and_never_reports_private_payloads(
    tmp_path: Path,
) -> None:
    current = tmp_path / "current.db"
    crawler = tmp_path / "crawler.db"
    tracker = tmp_path / "tracker.db"
    manifest = tmp_path / "bigquery-counts.json"
    _create_current_link_os(current)
    _create_crawler(crawler)
    _create_deal_tracker(tracker)
    manifest.write_text(
        json.dumps(
            {
                "counts": {"domains": 6206, "selected_emails": 5433},
                "campaign_memberships": {
                    "rows": 33697,
                    "distinct_emails": 5002,
                    "emails_in_multiple_campaigns": 4065,
                },
            }
        ),
        encoding="utf-8",
    )
    specs = [
        SourceSpec("tracker", "deal_tracker", tracker, 400),
        SourceSpec("crawler", "crawler", crawler, 300),
        SourceSpec("current", "current_link_os", current, 200),
        SourceSpec("bigquery", "bigquery_aggregate", manifest, 100),
    ]

    plan = MigrationPlanner(specs).build()
    replies = [record for record in plan.records if record.category == "replies"]
    assert len(replies) == 1
    assert replies[0].source_label == "tracker"
    assert plan.report.duplicates_discarded["replies"] == 1
    assert plan.report.final_counts["offers"] == 1
    assert plan.report.final_counts["catalogue_listings"] == 1
    assert plan.report.final_counts["publisher_entities"] == 1
    assert plan.report.final_counts["monday_mappings"] == 1
    assert plan.report.contacted_domains == 1
    assert plan.report.missing_contact_evidence == 1
    assert plan.report.campaign_memberships["rows"] == 33697

    safe_report = json.dumps(plan.report.to_dict())
    assert "PRIVATE REPLY BODY" not in safe_report
    assert "NEW PRIVATE BODY" not in safe_report


def test_snapshot_is_private_consistent_and_does_not_change_source(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    _create_current_link_os(source)
    before_hash = _sha256(source)
    spec = SourceSpec("current", "current_link_os", source, 200)

    snapshot = snapshot_sqlite(spec, tmp_path / "snapshots")

    assert _sha256(source) == before_hash
    assert snapshot.snapshot_path.is_file()
    assert stat.S_IMODE(snapshot.snapshot_path.stat().st_mode) == 0o600
    with sqlite3.connect(snapshot.snapshot_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM prospect_records").fetchone()[0] == 1


class FakeMigrationApi:
    def __init__(self, snapshot_dir: Path) -> None:
        self.snapshot_dir = snapshot_dir
        self.batches: list[list[dict]] = []

    def import_migration_records(
        self,
        records,
        *,
        migration_id,
        batch_number,
        final_batch,
    ):
        assert list(self.snapshot_dir.glob("*.db")), "source snapshot must exist first"
        self.batches.append(list(records))
        return {
            "migration_id": migration_id,
            "accepted": len(records),
            "skipped": 0,
        }


def test_execute_migration_snapshots_before_batched_local_import(tmp_path: Path) -> None:
    source = tmp_path / "current.db"
    snapshots = tmp_path / "snapshots"
    _create_current_link_os(source)
    api = FakeMigrationApi(snapshots)
    specs = [SourceSpec("current", "current_link_os", source, 200, required=True)]

    execution = execute_migration(
        api,  # type: ignore[arg-type]
        specs,
        snapshot_dir=snapshots,
        batch_size=2,
    )

    assert execution.snapshots
    assert execution.accepted_records == sum(len(batch) for batch in api.batches)
    assert execution.batch_count >= 1
    assert execution.report_path.is_file()
    report_text = execution.report_path.read_text(encoding="utf-8")
    assert "PRIVATE REPLY BODY" not in report_text
    assert stat.S_IMODE(execution.report_path.stat().st_mode) == 0o600
