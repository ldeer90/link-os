"""Read-only legacy inventory and fail-closed migration helpers.

The legacy databases are opened with SQLite ``mode=ro`` and ``query_only``.
Execution is a two-phase operation: every available SQLite source is backed up
first, then deduplicated records are sent in bounded batches to the local LINK OS
API.  Source databases are never attached to, updated, vacuumed, or migrated in
place.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence
from urllib.parse import urlsplit

from .api import LinkOsApiClient
from .models import (
    MigrationRecord,
    ReconciliationReport,
    SnapshotResult,
    SourceInventory,
    SourceSpec,
    utc_now_iso,
)


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def default_source_specs() -> list[SourceSpec]:
    """Return the stable source list in descending authority order.

    Live Instantly delivery state is deliberately absent: it is reconciled via
    the live integration command and remains authoritative over every historical
    store.  The BigQuery entry accepts an optional aggregate manifest only.
    """

    projects = Path("/Users/laurencedeer/Projects/Codex")
    return [
        SourceSpec(
            label="standalone_deal_tracker",
            kind="deal_tracker",
            path=projects / "Guest Post Deal Tracker/data/link_deals.db",
            precedence=400,
            required=True,
        ),
        SourceSpec(
            label="standalone_crawler",
            kind="crawler",
            path=projects / "Guest Post Outreach/data/guest_post_prospecting.db",
            precedence=300,
            required=True,
        ),
        SourceSpec(
            label="packaged_crawler",
            kind="crawler",
            path=projects / "link-os/prospecting/data/guest_post_prospecting.db",
            precedence=290,
            required=True,
        ),
        SourceSpec(
            label="current_link_os",
            kind="current_link_os",
            path=projects / "link-os/deal-tracker/data/link_deals.db",
            precedence=200,
            required=True,
        ),
    ]


def normalize_email(value: Any) -> str:
    email = str(value or "").strip().lower()
    if email.count("@") != 1 or any(character.isspace() for character in email):
        return ""
    local, domain = email.rsplit("@", 1)
    if not local or "." not in domain:
        return ""
    try:
        domain = domain.encode("idna").decode("ascii")
    except UnicodeError:
        return ""
    return f"{local}@{domain}"


def normalize_domain(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    candidate = raw if "://" in raw else f"//{raw}"
    try:
        host = urlsplit(candidate).hostname or ""
    except ValueError:
        return ""
    host = host.strip(".")
    if host.startswith("www."):
        host = host[4:]
    if not host or "." not in host or any(character.isspace() for character in host):
        return ""
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return ""


def _stable_hash(*parts: Any) -> str:
    packed = "\x1f".join(str(part or "").strip().lower() for part in parts)
    return hashlib.sha256(packed.encode("utf-8")).hexdigest()


def _freshness(row: Mapping[str, Any]) -> str:
    for key in (
        "updated_at",
        "last_seen_from_instantly",
        "received_at",
        "processed_at",
        "crawled_at",
        "discovered_at",
        "created_at",
    ):
        value = row.get(key)
        if value:
            return str(value)
    return ""


def _json_safe(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"encoding": "hex", "value": value.hex()}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _payload(row: Mapping[str, Any]) -> dict[str, Any]:
    return {key: _json_safe(value) for key, value in row.items()}


class SQLiteAdapter:
    def __init__(self, spec: SourceSpec) -> None:
        self.spec = spec

    def _connect(self) -> sqlite3.Connection:
        if not self.spec.path.is_file():
            raise FileNotFoundError(self.spec.path)
        uri = self.spec.path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        return connection

    def _table_names(self, connection: sqlite3.Connection) -> list[str]:
        rows = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        return [str(row[0]) for row in rows]

    def inventory(self) -> SourceInventory:
        if not self.spec.path.is_file():
            return SourceInventory(
                label=self.spec.label,
                kind=self.spec.kind,
                path=self.spec.path,
                precedence=self.spec.precedence,
                available=False,
                warnings=("source_not_found",),
            )
        counts: dict[str, int] = {}
        warnings: list[str] = []
        try:
            with self._connect() as connection:
                for table in self._table_names(connection):
                    if not _IDENTIFIER.fullmatch(table):
                        warnings.append(f"ignored_unsafe_table_name:{table}")
                        continue
                    counts[table] = int(
                        connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                    )
        except sqlite3.Error:
            return SourceInventory(
                label=self.spec.label,
                kind=self.spec.kind,
                path=self.spec.path,
                precedence=self.spec.precedence,
                available=False,
                size_bytes=self.spec.path.stat().st_size,
                warnings=("sqlite_inventory_failed",),
            )
        return SourceInventory(
            label=self.spec.label,
            kind=self.spec.kind,
            path=self.spec.path,
            precedence=self.spec.precedence,
            available=True,
            size_bytes=self.spec.path.stat().st_size,
            table_counts=counts,
            warnings=tuple(warnings),
        )

    def _rows(self, connection: sqlite3.Connection, table: str) -> Iterator[dict[str, Any]]:
        if not _IDENTIFIER.fullmatch(table):
            return
        tables = set(self._table_names(connection))
        if table not in tables:
            return
        cursor = connection.execute(f'SELECT * FROM "{table}"')
        for row in cursor:
            yield dict(row)

    def records(self) -> Iterator[MigrationRecord]:
        raise NotImplementedError

    def _record(
        self,
        category: str,
        natural_key: str,
        table: str,
        row: Mapping[str, Any],
        *,
        manual_authoritative: bool = False,
    ) -> MigrationRecord | None:
        if not natural_key:
            return None
        return MigrationRecord(
            category=category,
            natural_key=natural_key,
            source_label=self.spec.label,
            source_precedence=self.spec.precedence,
            source_table=table,
            payload=_payload(row),
            manual_authoritative=manual_authoritative,
            freshness=_freshness(row),
        )

    def _tracker_records(self, connection: sqlite3.Connection) -> Iterator[MigrationRecord]:
        for row in self._rows(connection, "instantly_reply_sync"):
            key = str(row.get("email_id") or "").strip()
            if not key:
                key = _stable_hash(
                    row.get("thread_id"),
                    row.get("campaign_id"),
                    row.get("from_email"),
                    row.get("received_at"),
                    row.get("body_text"),
                )
            record = self._record("replies", key, "instantly_reply_sync", row)
            if record:
                yield record

        for row in self._rows(connection, "link_deals"):
            domain = normalize_domain(row.get("root_domain") or row.get("publisher_url"))
            thread_id = str(row.get("instantly_thread_id") or "").strip()
            placement = str(row.get("placement_type") or "unknown").strip().lower()
            # One network reply can legitimately create many domain-level offers,
            # so a thread ID alone is not a deal key.  Domain + thread + placement
            # preserves those rows while still deduplicating copied databases.
            if domain and thread_id:
                key = f"{domain}|{thread_id}|{placement}"
            elif domain:
                key = f"{domain}|legacy:{row.get('id')}|{placement}"
            elif thread_id:
                key = f"thread:{thread_id}|{placement}"
            else:
                key = f"{self.spec.label}:legacy:{row.get('id')}"
            review_status = str(row.get("admin_review_status") or "").lower()
            listed = bool(row.get("is_listed"))
            manual = listed or review_status in {"approved", "listed", "confirmed"}
            record = self._record(
                "offers",
                key,
                "link_deals",
                row,
                manual_authoritative=manual,
            )
            if record:
                yield record
            if listed:
                listing = self._record(
                    "catalogue_listings",
                    key,
                    "link_deals",
                    row,
                    manual_authoritative=True,
                )
                if listing:
                    yield listing

        for row in self._rows(connection, "publisher_entities"):
            key = str(row.get("group_key") or "").strip().lower()
            key = key or normalize_email(row.get("primary_email"))
            key = key or normalize_domain(row.get("primary_domain"))
            key = key or f"legacy:{row.get('id')}"
            record = self._record(
                "publisher_entities",
                key,
                "publisher_entities",
                row,
                manual_authoritative=True,
            )
            if record:
                yield record

        for row in self._rows(connection, "users"):
            email = normalize_email(row.get("email"))
            record = self._record(
                "users",
                email,
                "users",
                row,
                manual_authoritative=True,
            )
            if record:
                yield record

        for row in self._rows(connection, "agency_enquiries"):
            key = str(row.get("id") or "").strip()
            key = f"{self.spec.label}:{key}" if key else _stable_hash(*row.values())
            record = self._record(
                "agency_enquiries",
                key,
                "agency_enquiries",
                row,
                manual_authoritative=True,
            )
            if record:
                yield record

        for row in self._rows(connection, "instantly_campaign_checkpoints"):
            campaign_id = str(row.get("campaign_id") or "").strip()
            record = self._record(
                "campaign_checkpoints",
                campaign_id,
                "instantly_campaign_checkpoints",
                row,
            )
            if record:
                yield record

        for table, prefix, key_fields in (
            ("sync_runs", "sync", ("started_at", "trigger_type", "id")),
            ("outreach_runs", "outreach", ("started_at", "action", "label")),
        ):
            for row in self._rows(connection, table):
                parts = [str(row.get(field) or "").strip() for field in key_fields]
                key = f"{prefix}:{'|'.join(parts)}"
                record = self._record("worker_runs", key, table, row)
                if record:
                    yield record

        for table, prefix, key_fields in (
            (
                "sync_events",
                "sync",
                ("sync_run_id", "event_type", "created_at", "message"),
            ),
            (
                "outreach_run_events",
                "outreach",
                ("run_id", "stage", "created_at", "message", "payload_json"),
            ),
        ):
            for row in self._rows(connection, table):
                key = f"{prefix}:{_stable_hash(*(row.get(field) for field in key_fields))}"
                record = self._record("audit_logs", key, table, row)
                if record:
                    yield record

        for table, category, key_fields in (
            ("monday_boards", "monday_boards", ("board_key", "board_id")),
            (
                "monday_item_map",
                "monday_mappings",
                ("board_key", "object_type", "local_id", "external_key"),
            ),
            (
                "monday_thread_comments",
                "monday_thread_comments",
                ("deal_id", "monday_item_id", "thread_id"),
            ),
        ):
            for row in self._rows(connection, table):
                key = "|".join(str(row.get(field) or "").strip() for field in key_fields)
                record = self._record(
                    category,
                    key,
                    table,
                    row,
                    manual_authoritative=True,
                )
                if record:
                    yield record


class DealTrackerAdapter(SQLiteAdapter):
    def records(self) -> Iterator[MigrationRecord]:
        if not self.spec.path.is_file():
            return
        with self._connect() as connection:
            yield from self._tracker_records(connection)


class CurrentLinkOsAdapter(SQLiteAdapter):
    def records(self) -> Iterator[MigrationRecord]:
        if not self.spec.path.is_file():
            return
        with self._connect() as connection:
            for row in self._rows(connection, "prospect_records"):
                domain = normalize_domain(row.get("root_domain") or row.get("source_url"))
                domain_record = self._record("domains", domain, "prospect_records", row)
                if domain_record:
                    yield domain_record
                email = normalize_email(row.get("contact_email"))
                if email:
                    contact = self._record("contacts", email, "prospect_records", row)
                    if contact:
                        yield contact
                    association = self._record(
                        "domain_contacts",
                        f"{domain}|{email}",
                        "prospect_records",
                        {**row, "evidence_url": row.get("source_url")},
                    )
                    if association and domain:
                        yield association

            for row in self._rows(connection, "prospect_imports"):
                key = f"{self.spec.label}:{row.get('id')}"
                record = self._record("imports", key, "prospect_imports", row)
                if record:
                    yield record

            for row in self._rows(connection, "prospect_import_rows"):
                key = f"{self.spec.label}:{row.get('import_id')}:{row.get('id')}"
                record = self._record("import_rows", key, "prospect_import_rows", row)
                if record:
                    yield record

            yield from self._tracker_records(connection)


class CrawlerAdapter(SQLiteAdapter):
    def records(self) -> Iterator[MigrationRecord]:
        if not self.spec.path.is_file():
            return
        with self._connect() as connection:
            for row in self._rows(connection, "candidate_domains"):
                domain = normalize_domain(row.get("root_domain"))
                record = self._record("domains", domain, "candidate_domains", row)
                if record:
                    yield record

            for row in self._rows(connection, "guest_post_contact_emails"):
                domain = normalize_domain(row.get("root_domain"))
                email = normalize_email(row.get("email"))
                contact = self._record("contacts", email, "guest_post_contact_emails", row)
                if contact:
                    yield contact
                association = self._record(
                    "domain_contacts",
                    f"{domain}|{email}",
                    "guest_post_contact_emails",
                    {**row, "evidence_url": row.get("email_source_url")},
                )
                if association and domain and email:
                    yield association

            for row in self._rows(connection, "domain_evidence"):
                domain = normalize_domain(row.get("root_domain"))
                url = str(row.get("evidence_url") or "").strip()
                record = self._record(
                    "domain_evidence",
                    f"{domain}|{url}",
                    "domain_evidence",
                    row,
                )
                if record and domain and url:
                    yield record

            for row in self._rows(connection, "guest_post_suppression"):
                domain = normalize_domain(row.get("root_domain"))
                record = self._record(
                    "suppressions",
                    domain,
                    "guest_post_suppression",
                    row,
                    manual_authoritative=True,
                )
                if record:
                    yield record

            for row in self._rows(connection, "crawl_queue"):
                url = str(row.get("url") or "").strip()
                record = self._record("scrape_jobs", url, "crawl_queue", row)
                if record:
                    yield record

            for row in self._rows(connection, "crawl_results"):
                url = str(row.get("url") or "").strip()
                key = f"{url}|{row.get('crawled_at') or ''}"
                record = self._record("scrape_attempts", key, "crawl_results", row)
                if record and url:
                    yield record

            for row in self._rows(connection, "candidate_urls"):
                url = str(row.get("url") or "").strip()
                record = self._record("candidate_urls", url, "candidate_urls", row)
                if record:
                    yield record

            for row in self._rows(connection, "guest_post_campaign_drafts"):
                key = str(row.get("campaign_id") or row.get("id") or "").strip()
                key = key or _stable_hash(*row.values())
                record = self._record(
                    "campaign_drafts",
                    key,
                    "guest_post_campaign_drafts",
                    row,
                )
                if record:
                    yield record

            yield from self._tracker_records(connection)


class BigQueryAggregateAdapter:
    """Aggregate-only placeholder; never reads or imports raw BigQuery content."""

    def __init__(self, spec: SourceSpec) -> None:
        self.spec = spec

    def _manifest(self) -> dict[str, Any]:
        if not self.spec.path.is_file():
            return {}
        try:
            value = json.loads(self.spec.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def inventory(self) -> SourceInventory:
        manifest = self._manifest()
        if not manifest:
            return SourceInventory(
                label=self.spec.label,
                kind=self.spec.kind,
                path=self.spec.path,
                precedence=self.spec.precedence,
                available=False,
                warnings=("aggregate_manifest_unavailable",),
            )
        raw_counts = manifest.get("counts", {})
        counts = {
            str(key): max(0, int(value))
            for key, value in raw_counts.items()
            if isinstance(value, (int, float))
        }
        return SourceInventory(
            label=self.spec.label,
            kind=self.spec.kind,
            path=self.spec.path,
            precedence=self.spec.precedence,
            available=True,
            size_bytes=self.spec.path.stat().st_size,
            table_counts=counts,
            warnings=("aggregate_only_no_raw_rows_imported",),
        )

    def records(self) -> Iterator[MigrationRecord]:
        return iter(())

    def campaign_memberships(self) -> dict[str, int]:
        raw = self._manifest().get("campaign_memberships", {})
        if not isinstance(raw, dict):
            return {}
        return {
            str(key): max(0, int(value))
            for key, value in raw.items()
            if isinstance(value, (int, float))
        }


Adapter = SQLiteAdapter | BigQueryAggregateAdapter


def adapter_for(spec: SourceSpec) -> Adapter:
    if spec.kind == "deal_tracker":
        return DealTrackerAdapter(spec)
    if spec.kind == "current_link_os":
        return CurrentLinkOsAdapter(spec)
    if spec.kind == "crawler":
        return CrawlerAdapter(spec)
    if spec.kind == "bigquery_aggregate":
        return BigQueryAggregateAdapter(spec)
    raise ValueError(f"Unsupported migration source kind: {spec.kind}")


@dataclass
class MigrationPlan:
    records: list[MigrationRecord]
    report: ReconciliationReport


class MigrationPlanner:
    def __init__(self, specs: Sequence[SourceSpec]) -> None:
        self.specs = sorted(specs, key=lambda spec: spec.precedence, reverse=True)

    @staticmethod
    def _rank(record: MigrationRecord) -> tuple[Any, ...]:
        # Password changes are operational state rather than historical deal
        # evidence.  Prefer the freshest local user record so an explicitly
        # changed LINK OS password is not replaced by an older standalone copy.
        try:
            freshness = datetime.fromisoformat(record.freshness.replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            freshness = float("-inf")
        return (
            1 if record.manual_authoritative else 0,
            freshness if record.category == "users" else record.source_precedence,
            record.source_precedence if record.category == "users" else freshness,
        )

    def build(self) -> MigrationPlan:
        inventories: list[SourceInventory] = []
        source_counts: dict[str, Counter[str]] = defaultdict(Counter)
        winners: dict[tuple[str, str], MigrationRecord] = {}
        duplicates: Counter[str] = Counter()
        warnings: list[str] = [
            "Live Instantly must be reconciled after import and overrides "
            "historical delivery state."
        ]
        campaign_memberships: dict[str, int] = {}

        for spec in self.specs:
            adapter = adapter_for(spec)
            inventory = adapter.inventory()
            inventories.append(inventory)
            if not inventory.available:
                message = f"Migration source unavailable: {spec.label}"
                warnings.append(message)
                if spec.required:
                    warnings.append(f"Required source unavailable: {spec.label}")
                continue
            if isinstance(adapter, BigQueryAggregateAdapter):
                campaign_memberships.update(adapter.campaign_memberships())
            for record in adapter.records():
                source_counts[spec.label][record.category] += 1
                key = (record.category, record.natural_key)
                current = winners.get(key)
                if current is None:
                    winners[key] = record
                    continue
                duplicates[record.category] += 1
                if self._rank(record) > self._rank(current):
                    winners[key] = record

        dependency_order = {
            "users": 10,
            "domains": 20,
            "contacts": 30,
            "domain_contacts": 40,
            "domain_evidence": 41,
            "candidate_urls": 42,
            "suppressions": 45,
            "imports": 50,
            "import_rows": 51,
            "scrape_jobs": 60,
            "scrape_attempts": 61,
            "replies": 70,
            "publisher_entities": 71,
            "offers": 72,
            "catalogue_listings": 73,
            "agency_enquiries": 74,
            "monday_boards": 80,
            "monday_mappings": 81,
            "monday_thread_comments": 82,
            "campaign_checkpoints": 90,
            "campaign_drafts": 91,
            "worker_runs": 95,
            "audit_logs": 100,
        }
        records = sorted(
            winners.values(),
            key=lambda record: (
                dependency_order.get(record.category, 500),
                record.category,
                record.natural_key,
            ),
        )
        final_counts = Counter(record.category for record in records)
        contacted = {
            normalize_domain(record.payload.get("root_domain"))
            for record in records
            if record.category in {"offers", "catalogue_listings"}
        }
        contacted.discard("")
        associated_emails = {
            normalize_email(record.payload.get("email") or record.payload.get("contact_email"))
            for record in records
            if record.category == "domain_contacts"
        }
        associated_emails.discard("")
        missing_evidence = 0
        for record in records:
            if record.category == "contacts":
                email = normalize_email(
                    record.payload.get("email") or record.payload.get("contact_email")
                )
                if email and email not in associated_emails:
                    missing_evidence += 1
            elif record.category == "domain_contacts" and not (
                record.payload.get("evidence_url")
                or record.payload.get("email_source_url")
                or record.payload.get("source_url")
            ):
                missing_evidence += 1

        report = ReconciliationReport(
            source_inventories=inventories,
            source_counts={label: dict(counts) for label, counts in source_counts.items()},
            final_counts=dict(final_counts),
            duplicates_discarded=dict(duplicates),
            contacted_domains=len(contacted),
            missing_contact_evidence=missing_evidence,
            campaign_memberships=campaign_memberships,
            precedence=[
                "live_instantly_delivery_state",
                *[spec.label for spec in self.specs],
            ],
            warnings=warnings,
        )
        return MigrationPlan(records=records, report=report)


def snapshot_sqlite(spec: SourceSpec, snapshot_dir: Path) -> SnapshotResult:
    if spec.kind == "bigquery_aggregate":
        raise ValueError("Aggregate manifests are not SQLite snapshot sources")
    if not spec.path.is_file():
        raise FileNotFoundError(spec.path)
    snapshot_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(snapshot_dir, 0o700)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = snapshot_dir / f"{spec.label}-{stamp}-{uuid.uuid4().hex[:8]}.db"
    source_uri = spec.path.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(source_uri, uri=True, timeout=30) as source:
        source.execute("PRAGMA query_only = ON")
        with sqlite3.connect(destination) as target:
            source.backup(target)
    os.chmod(destination, 0o600)
    digest = hashlib.sha256()
    with destination.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return SnapshotResult(
        source_label=spec.label,
        source_path=spec.path,
        snapshot_path=destination,
        size_bytes=destination.stat().st_size,
        sha256=digest.hexdigest(),
        created_at=utc_now_iso(),
    )


@dataclass
class MigrationExecutionResult:
    migration_id: str
    snapshots: list[SnapshotResult]
    report_path: Path
    batch_count: int
    accepted_records: int
    api_summaries: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "migration_id": self.migration_id,
            "snapshots": [snapshot.to_dict() for snapshot in self.snapshots],
            "report_path": str(self.report_path),
            "batch_count": self.batch_count,
            "accepted_records": self.accepted_records,
            "api_summaries": self.api_summaries,
        }


def execute_migration(
    api: LinkOsApiClient,
    specs: Sequence[SourceSpec],
    *,
    snapshot_dir: Path,
    batch_size: int = 500,
) -> MigrationExecutionResult:
    if batch_size < 1 or batch_size > 1_000:
        raise ValueError("Migration batch size must be between 1 and 1000")
    adapters = [adapter_for(spec) for spec in specs]
    inventories = [adapter.inventory() for adapter in adapters]
    missing_required = [
        spec.label
        for spec, inventory in zip(specs, inventories, strict=True)
        if spec.required and not inventory.available
    ]
    if missing_required:
        raise RuntimeError("Required migration source unavailable; migration aborted")

    snapshots: list[SnapshotResult] = []
    for spec, inventory in zip(specs, inventories, strict=True):
        if inventory.available and spec.kind != "bigquery_aggregate":
            snapshots.append(snapshot_sqlite(spec, snapshot_dir))
    if not snapshots:
        raise RuntimeError("No SQLite sources were available to snapshot; migration aborted")

    snapshot_by_label = {snapshot.source_label: snapshot for snapshot in snapshots}
    snapshot_specs = [
        SourceSpec(
            label=spec.label,
            kind=spec.kind,
            path=snapshot_by_label[spec.label].snapshot_path,
            precedence=spec.precedence,
            required=spec.required,
        )
        if spec.kind != "bigquery_aggregate" and spec.label in snapshot_by_label
        else spec
        for spec in specs
    ]
    plan = MigrationPlanner(snapshot_specs).build()
    if not plan.records:
        raise RuntimeError("Migration plan is empty; migration aborted")
    snapshot_digest = hashlib.sha256(
        "|".join(snapshot.sha256 for snapshot in snapshots).encode("utf-8")
    ).hexdigest()[:16]
    migration_id = f"link-os-{snapshot_digest}"
    batch_count = math.ceil(len(plan.records) / batch_size)
    summaries: list[dict[str, Any]] = []
    accepted_records = 0
    for batch_index in range(batch_count):
        start = batch_index * batch_size
        batch = plan.records[start : start + batch_size]
        response = api.import_migration_records(
            [record.api_dict() for record in batch],
            migration_id=migration_id,
            batch_number=batch_index + 1,
            final_batch=batch_index + 1 == batch_count,
        )
        safe_summary = {
            key: response[key]
            for key in (
                "accepted",
                "updated",
                "skipped",
                "materialized",
                "retained",
                "invalid",
                "errors",
                "migration_id",
            )
            if key in response
        }
        summaries.append(safe_summary)
        if int(response.get("invalid", 0)) or int(response.get("errors", 0)):
            raise RuntimeError(
                f"Migration batch {batch_index + 1} failed canonical validation"
            )
        processed = sum(int(response.get(key, 0)) for key in ("accepted", "updated", "skipped"))
        if processed != len(batch):
            raise RuntimeError(
                f"Migration batch {batch_index + 1} readback mismatch: expected {len(batch)}, got {processed}"
            )
        accepted_records += processed

    report_payload = {
        "migration_id": migration_id,
        "snapshots": [snapshot.to_dict() for snapshot in snapshots],
        "reconciliation": plan.report.to_dict(),
        "execution": {
            "batch_count": batch_count,
            "accepted_records": accepted_records,
            "api_summaries": summaries,
        },
    }
    report_path = snapshot_dir / f"{migration_id}-reconciliation.json"
    report_path.write_text(
        json.dumps(report_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(report_path, 0o600)
    return MigrationExecutionResult(
        migration_id=migration_id,
        snapshots=snapshots,
        report_path=report_path,
        batch_count=batch_count,
        accepted_records=accepted_records,
        api_summaries=summaries,
    )
