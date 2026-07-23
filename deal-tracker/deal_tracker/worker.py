from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import socket
import time
import traceback
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from deal_tracker.crawler import CrawlConfig, crawl_domain
from deal_tracker.backlink_discovery import mark_import_complete, run_analysis_job
from deal_tracker.env import load_env_value
from deal_tracker.integrations.instantly import (
    COMPLETED_CAMPAIGN_STATUS,
    CAMPAIGN_COPY_VERSION,
    PUBLIC_EVIDENCE_PILOT_COPY_VERSION,
    MANAGED_NAME_PREFIX,
    InstantlyControl,
    matching_campaigns,
    campaign_is_link_building,
    reply_sync_campaigns,
    safe_campaign_payload,
    sender_is_healthy,
    verification_is_eligible,
)
from deal_tracker.integrations.monday import DealsOnlyProjector
from deal_tracker.offer_review import evaluate_offer
from deal_tracker.platform.enums import (
    CampaignBatchStatus,
    CampaignLaunchStatus,
    CampaignMemberStatus,
    ImportItemStatus,
    ImportStatus,
    JobStatus,
    LifecycleStage,
    OfferStatus,
    ScrapeStatus,
    VerificationStatus,
    WorkerRunStatus,
)
from deal_tracker.platform.gates import (
    CampaignUploadSnapshot,
    SenderSnapshot,
    evaluate_campaign_activation,
)
from deal_tracker.platform.lifecycle import NEVER_AUTOMATICALLY_RECONTACT, is_refresh_eligible
from deal_tracker.platform.models import (
    AuditLog,
    CampaignBatch,
    CampaignLaunch,
    CampaignMember,
    Contact,
    Domain,
    DomainContactEvidence,
    ImportBatch,
    ImportItem,
    InstantlyEvent,
    Job,
    Offer,
    PublisherDomain,
    PublisherEntity,
    Reply,
    ScrapeAttempt,
    SenderAccount,
    Suppression,
    SystemSetting,
    VerificationResult,
    WorkerRun,
    utcnow,
)
from deal_tracker.platform.normalization import normalize_domain, normalize_email
from deal_tracker.platform.pricing import calculate_reseller_price
from deal_tracker.platform.repositories import (
    CampaignRepository,
    CampaignReservationError,
    PUBLIC_EVIDENCE_CONTACT_POLICY,
    STRICT_VERIFIED_CONTACT_POLICY,
    DomainRepository,
    JobLeaseError,
    JobQueue,
    is_outreach_paused,
    set_outreach_paused,
)
from deal_tracker.platform_runtime import engine as platform_engine
from deal_tracker.platform_runtime import platform_session, session_factory


DOMAIN_COLUMNS = ("domain", "website", "url", "referring_domain", "source_domain", "publisher_url", "root_domain")
EMAIL_COLUMNS = ("contact_email", "email", "best_contact_email", "selected_email")
SPLIT_VALUES = re.compile(r"[\s,;\t]+")
PILOT_BATCH_SIZE = 25
NORMAL_BATCH_SIZE = 250
HEALTH_GATE_HOURS = 72


class NonRetryableJobError(RuntimeError):
    """A durable job failure that requires an operator or account-state change."""


@dataclass(frozen=True)
class WorkerConfig:
    concurrency: int = 8
    idle_seconds: float = 2.0
    lease_minutes: int = 20
    import_chunk_size: int = 1000
    scrape_max_pages: int = 20
    scrape_timeout_seconds: float = 10.0
    scrape_max_attempts: int = 3
    scrape_host_delay_seconds: float = 1.0
    scrape_max_runtime_seconds: float = 900.0

    @classmethod
    def from_env(cls) -> "WorkerConfig":
        return cls(
            concurrency=max(1, int(os.getenv("SCRAPE_WORKERS", "8"))),
            idle_seconds=max(0.25, float(os.getenv("WORKER_IDLE_SECONDS", "2"))),
            lease_minutes=max(5, int(os.getenv("JOB_LEASE_MINUTES", "20"))),
            import_chunk_size=max(100, int(os.getenv("IMPORT_CHUNK_SIZE", "1000"))),
            scrape_max_pages=max(1, int(os.getenv("SCRAPE_MAX_PAGES_PER_DOMAIN", "20"))),
            scrape_timeout_seconds=max(1, float(os.getenv("SCRAPE_TIMEOUT_SECONDS", "10"))),
            scrape_max_attempts=max(1, int(os.getenv("SCRAPE_MAX_ATTEMPTS", "3"))),
            scrape_host_delay_seconds=max(0, float(os.getenv("SCRAPE_HOST_DELAY_SECONDS", "1"))),
            scrape_max_runtime_seconds=max(60, float(os.getenv("SCRAPE_MAX_RUNTIME_SECONDS", "900"))),
        )


@dataclass(frozen=True)
class ImportRow:
    row_number: int
    raw_value: str
    domain_value: str
    contact_email: str = ""
    metadata: dict[str, Any] | None = None


def _first(row: dict[str, Any], names: tuple[str, ...]) -> str:
    lowered = {str(key).strip().lower(): value for key, value in row.items()}
    for name in names:
        value = str(lowered.get(name) or "").strip()
        if value:
            return value
    return ""


def iter_import_rows(path: Path) -> Iterator[ImportRow]:
    suffix = path.suffix.lower()
    if suffix in {".csv", ".tsv"}:
        delimiter = "\t" if suffix == ".tsv" else ","
        with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
            reader = csv.DictReader(handle, delimiter=delimiter)
            fields = {str(field or "").strip().lower() for field in reader.fieldnames or []}
            if fields.intersection(DOMAIN_COLUMNS + EMAIL_COLUMNS):
                for number, row in enumerate(reader, 1):
                    domain_value = _first(row, DOMAIN_COLUMNS)
                    contact_email = _first(row, EMAIL_COLUMNS)
                    raw = domain_value or contact_email
                    if raw:
                        yield ImportRow(number, raw, domain_value or contact_email, contact_email, {"columns": sorted(fields)})
                return
    number = 0
    with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
        for line in handle:
            for value in SPLIT_VALUES.split(line.strip()):
                cleaned = value.strip().strip('"\'')
                if not cleaned:
                    continue
                number += 1
                yield ImportRow(number, cleaned, cleaned, cleaned if "@" in cleaned else "")


def _batched_rows(rows: Iterator[ImportRow], size: int) -> Iterator[list[ImportRow]]:
    batch: list[ImportRow] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _active_suppression(session: Session, *, domain_id: str, contact_id: str | None = None) -> bool:
    now = utcnow()
    statement = select(Suppression.id).where(
        Suppression.active.is_(True),
        (Suppression.domain_id == domain_id) | ((Suppression.contact_id == contact_id) if contact_id else (Suppression.id == "")),
        (Suppression.expires_at.is_(None)) | (Suppression.expires_at > now),
    )
    return session.scalar(statement.limit(1)) is not None


def _get_or_create_pasted_contact(session: Session, email: str) -> Contact:
    normalized = normalize_email(email)
    contact = session.scalar(select(Contact).where(Contact.normalized_email == normalized))
    if contact is None:
        contact = Contact(
            normalized_email=normalized,
            original_email=email,
            email_domain=normalized.rsplit("@", 1)[-1],
            role_class=normalized.split("@", 1)[0],
            is_guessed=False,
            attributes={"source": "pasted_import", "public_evidence": False},
        )
        session.add(contact)
        session.flush()
    return contact


def _scrape_job_key(domain: Domain, *, refresh: bool) -> str:
    return f"scrape_domain:{domain.id}:{'refresh:' + utcnow().date().isoformat() if refresh else 'initial'}"


def process_import_job(job_id: str, worker_id: str, config: WorkerConfig) -> dict[str, int]:
    factory = session_factory()
    with factory() as initial:
        job = initial.get(Job, job_id)
        if job is None:
            raise LookupError(f"job {job_id} disappeared")
        import_id = str(job.payload.get("import_id") or job.subject_id or "")
        path = Path(str(job.payload.get("path") or ""))
        queue_enabled = job.payload.get("queue", True) is not False
        batch = initial.get(ImportBatch, import_id)
        if batch is None:
            raise LookupError(f"import {import_id} does not exist")
        if not path.is_file():
            raise FileNotFoundError(f"import source is missing: {path}")
        batch.status = ImportStatus.PROCESSING
        initial.commit()

    with factory() as session:
        existing_values = list(
            session.execute(
                select(ImportItem.normalized_value, ImportItem.domain_id).where(
                    ImportItem.import_id == import_id,
                    ImportItem.normalized_value.is_not(None),
                )
            )
        )
        seen = {value for value, _domain_id in existing_values if value}
        domain_cache = {value: domain_id for value, domain_id in existing_values if value and domain_id}
        processed_rows = set(session.scalars(select(ImportItem.row_number).where(ImportItem.import_id == import_id)))

    for chunk in _batched_rows(iter_import_rows(path), config.import_chunk_size):
        with factory() as session:
            batch = session.get(ImportBatch, import_id)
            if batch is None:
                raise LookupError(f"import {import_id} disappeared")
            for item in chunk:
                if item.row_number in processed_rows:
                    continue
                status = ImportItemStatus.INVALID
                explanation = "Invalid or missing public domain"
                domain: Domain | None = None
                contact: Contact | None = None
                normalized_value = ""
                try:
                    normalized = normalize_domain(item.domain_value)
                    normalized_value = normalized.registrable_domain
                    if item.contact_email:
                        contact = _get_or_create_pasted_contact(session, item.contact_email)
                except ValueError:
                    normalized = None

                if normalized is not None:
                    if normalized_value in seen:
                        status = ImportItemStatus.DUPLICATE
                        explanation = "Repeated in this import"
                        cached_domain_id = domain_cache.get(normalized_value)
                        domain = session.get(Domain, cached_domain_id) if cached_domain_id else session.scalar(
                            select(Domain).where(Domain.normalized_domain == normalized_value)
                        )
                        if domain:
                            domain_cache[normalized_value] = domain.id
                    else:
                        seen.add(normalized_value)
                        domain, created = DomainRepository(session).get_or_create(item.domain_value, source=f"import:{import_id}")
                        domain_cache[normalized_value] = domain.id
                        if _active_suppression(session, domain_id=domain.id, contact_id=contact.id if contact else None):
                            status = ImportItemStatus.SUPPRESSED
                            explanation = "Domain or contact is suppressed"
                        elif domain.last_contacted_at is not None or domain.lifecycle_stage in NEVER_AUTOMATICALLY_RECONTACT:
                            status = ImportItemStatus.PREVIOUSLY_CONTACTED
                            explanation = "Protected by previous outreach or terminal lifecycle state"
                        elif created:
                            status = ImportItemStatus.ACCEPTED
                            explanation = "New canonical domain"
                        elif is_refresh_eligible(domain.lifecycle_stage, last_attempt_at=domain.last_scraped_at):
                            status = ImportItemStatus.REFRESH_ELIGIBLE
                            explanation = "Previous no-email or failed result is at least 90 days old"
                        else:
                            status = ImportItemStatus.DUPLICATE
                            explanation = "Already present in LINK OS"

                        if queue_enabled and status in {ImportItemStatus.ACCEPTED, ImportItemStatus.REFRESH_ELIGIBLE}:
                            refresh = status == ImportItemStatus.REFRESH_ELIGIBLE
                            if domain.lifecycle_stage != LifecycleStage.QUEUED:
                                DomainRepository(session).transition(
                                    domain.id,
                                    LifecycleStage.QUEUED,
                                    source=f"import:{import_id}",
                                    reason="refresh eligible" if refresh else "accepted import",
                                )
                            JobQueue(session).enqueue(
                                kind="scrape_domain",
                                idempotency_key=_scrape_job_key(domain, refresh=refresh),
                                subject_type="domain",
                                subject_id=domain.id,
                                payload={"domain_id": domain.id},
                                priority=50,
                                max_attempts=config.scrape_max_attempts,
                            )
                session.add(
                    ImportItem(
                        import_id=import_id,
                        row_number=item.row_number,
                        raw_value=item.raw_value,
                        normalized_value=normalized_value or None,
                        status=status,
                        domain_id=domain.id if domain else None,
                        contact_id=contact.id if contact else None,
                        explanation=explanation,
                        row_metadata=item.metadata or {},
                    )
                )
                processed_rows.add(item.row_number)
            batch.processed_rows = len(processed_rows)
            session.commit()
        with factory() as heartbeat:
            JobQueue(heartbeat).heartbeat(
                job_id,
                worker_id=worker_id,
                lease_for=timedelta(minutes=config.lease_minutes),
            )
            heartbeat.commit()

    with factory() as session:
        batch = session.get(ImportBatch, import_id)
        grouped = {
            status: int(count)
            for status, count in session.execute(
                select(ImportItem.status, func.count(ImportItem.id))
                .where(ImportItem.import_id == import_id)
                .group_by(ImportItem.status)
            )
        }
        total = sum(grouped.values())
        batch.total_rows = total
        batch.processed_rows = total
        batch.accepted_count = grouped.get(ImportItemStatus.ACCEPTED, 0)
        batch.duplicate_count = grouped.get(ImportItemStatus.DUPLICATE, 0)
        batch.invalid_count = grouped.get(ImportItemStatus.INVALID, 0)
        batch.suppressed_count = grouped.get(ImportItemStatus.SUPPRESSED, 0)
        batch.previously_contacted_count = grouped.get(ImportItemStatus.PREVIOUSLY_CONTACTED, 0)
        batch.refresh_eligible_count = grouped.get(ImportItemStatus.REFRESH_ELIGIBLE, 0)
        batch.status = ImportStatus.COMPLETE
        batch.completed_at = utcnow()
        session.add(
            AuditLog(
                actor_type="worker",
                actor_id=worker_id,
                action="import.completed",
                entity_type="import",
                entity_id=import_id,
                after={status.value: count for status, count in grouped.items()},
            )
        )
        mark_import_complete(session, batch)
        session.commit()
    return {status.value: count for status, count in grouped.items()}


def _role_class(email: str) -> str:
    local = email.split("@", 1)[0]
    for role in ("editorial", "editor", "advertising", "media", "sales", "contributor", "submissions", "partnerships", "contact", "info", "hello"):
        if role in local:
            return role
    return "person_or_other"


def _persist_scrape_progress(
    *,
    attempt_id: str,
    domain_id: str,
    domain_name: str,
    job_id: str,
    worker_id: str | None,
    progress: dict[str, Any],
) -> None:
    """Persist a privacy-safe crawler heartbeat and expose it through the audit SSE stream."""
    event_type = str(progress.get("event_type") or "page_crawled")
    with session_factory()() as session:
        attempt = session.get(ScrapeAttempt, attempt_id)
        if attempt is None or attempt.status != ScrapeStatus.RUNNING:
            return
        attempt.pages_requested = max(attempt.pages_requested, int(progress.get("pages_attempted") or 0))
        attempt.pages_crawled = max(attempt.pages_crawled, int(progress.get("pages_crawled") or 0))
        attempt.emails_found = max(attempt.emails_found, int(progress.get("emails_found") or 0))
        event_at = utcnow()
        current_url = str(progress.get("url") or "")[:2048]
        attempt.metrics = {
            **(attempt.metrics or {}),
            "current_url": current_url,
            "last_event": event_type,
            "last_event_at": event_at.isoformat(),
            "page_status": str(progress.get("page_status") or ""),
            "max_pages": int(progress.get("max_pages") or 0),
        }
        event_payload = {
            "attempt_id": attempt_id,
            "domain_id": domain_id,
            "domain": domain_name,
            "job_id": job_id,
            "event_type": event_type,
            "current_url": current_url,
            "page_status": str(progress.get("page_status") or ""),
            "status_code": progress.get("status_code"),
            "pages_attempted": attempt.pages_requested,
            "pages_crawled": attempt.pages_crawled,
            "emails_found": attempt.emails_found,
            "max_pages": int(progress.get("max_pages") or 0),
            "occurred_at": event_at.isoformat(),
        }
        session.add(
            AuditLog(
                actor_type="worker",
                actor_id=worker_id,
                action=event_type,
                entity_type="scrape_attempt",
                entity_id=attempt_id,
                after=event_payload,
                reason="Live public-page crawl progress",
            )
        )
        session.commit()


def scrape_domain_job(job_id: str, config: WorkerConfig) -> dict[str, Any]:
    factory = session_factory()
    with factory() as session:
        job = session.get(Job, job_id)
        if job is None:
            raise LookupError(f"job {job_id} disappeared")
        domain_id = str(job.payload.get("domain_id") or job.subject_id or "")
        domain = session.get(Domain, domain_id)
        if domain is None:
            raise LookupError(f"domain {domain_id} does not exist")
        if domain.lifecycle_stage not in {LifecycleStage.QUEUED, LifecycleStage.SCRAPING}:
            return {"skipped": True, "stage": domain.lifecycle_stage.value}
        if domain.lifecycle_stage == LifecycleStage.SCRAPING:
            interrupted_attempts = session.scalars(
                select(ScrapeAttempt).where(
                    ScrapeAttempt.domain_id == domain.id,
                    ScrapeAttempt.status == ScrapeStatus.RUNNING,
                )
            ).all()
            for interrupted in interrupted_attempts:
                interrupted.status = ScrapeStatus.FAILED
                interrupted.completed_at = utcnow()
                interrupted.error_code = "worker_interrupted"
                interrupted.error_detail = "Recovered after the prior worker lease ended before completion."
            session.add(
                AuditLog(
                    actor_type="worker",
                    actor_id=job.lease_owner,
                    action="scrape.interrupted_recovered",
                    entity_type="domain",
                    entity_id=domain.id,
                    after={"job_id": job_id, "attempts_closed": len(interrupted_attempts)},
                    reason="Reclaimed durable scrape job after worker interruption",
                )
            )
        else:
            DomainRepository(session).transition(domain.id, LifecycleStage.SCRAPING, source="worker", reason=f"job:{job_id}")
        attempt_number = int(session.scalar(select(func.count(ScrapeAttempt.id)).where(ScrapeAttempt.domain_id == domain.id)) or 0) + 1
        attempt = ScrapeAttempt(
            domain_id=domain.id,
            job_id=job_id,
            attempt_number=attempt_number,
            status=ScrapeStatus.RUNNING,
        )
        session.add(attempt)
        session.flush()
        session.add(
            AuditLog(
                actor_type="worker",
                actor_id=job.lease_owner,
                action="scrape_started",
                entity_type="scrape_attempt",
                entity_id=attempt.id,
                after={
                    "attempt_id": attempt.id,
                    "domain_id": domain.id,
                    "domain": domain.normalized_domain,
                    "job_id": job_id,
                    "event_type": "scrape_started",
                    "pages_attempted": 0,
                    "pages_crawled": 0,
                    "emails_found": 0,
                    "max_pages": config.scrape_max_pages,
                    "occurred_at": utcnow().isoformat(),
                },
                reason="Crawler leased the domain",
            )
        )
        session.commit()
        attempt_id = attempt.id
        domain_name = domain.normalized_domain
        worker_id = job.lease_owner

    def record_progress(progress: dict[str, Any]) -> None:
        try:
            _persist_scrape_progress(
                attempt_id=attempt_id,
                domain_id=domain_id,
                domain_name=domain_name,
                job_id=job_id,
                worker_id=worker_id,
                progress=progress,
            )
        except Exception:
            # Progress telemetry must never interrupt the durable crawl itself.
            traceback.print_exc()

    result = crawl_domain(
        domain_name,
        config=CrawlConfig(
            max_pages=config.scrape_max_pages,
            timeout_seconds=config.scrape_timeout_seconds,
            max_attempts=config.scrape_max_attempts,
            host_delay_seconds=config.scrape_host_delay_seconds,
            max_runtime_seconds=config.scrape_max_runtime_seconds,
        ),
        on_progress=record_progress,
    )

    with factory() as session:
        domain = session.get(Domain, domain_id)
        attempt = session.get(ScrapeAttempt, attempt_id)
        if domain is None or attempt is None:
            raise LookupError("scrape state disappeared")
        attempt.completed_at = utcnow()
        attempt.pages_requested = result.pages_attempted
        attempt.pages_crawled = result.pages_succeeded
        attempt.emails_found = 0
        attempt.metrics = {
            **(attempt.metrics or {}),
            "attempts": [entry.__dict__ for entry in result.attempts],
            "current_url": "",
            "last_event": "scrape_completed" if result.status != "failed" else "scrape_failed",
            "last_event_at": utcnow().isoformat(),
        }
        domain.last_scraped_at = utcnow()
        final_status = result.status
        accepted_contacts = 0

        if result.status == "email_found":
            valid_evidence: list[tuple[Any, str]] = []
            rejected_contacts = 0
            for evidence in result.contacts:
                try:
                    normalized_email = normalize_email(evidence.email)
                except (AttributeError, TypeError, ValueError):
                    rejected_contacts += 1
                    continue
                valid_evidence.append((evidence, normalized_email))
            attempt.metrics["rejected_contacts"] = rejected_contacts
            attempt.emails_found = len(valid_evidence)
            accepted_contacts = len(valid_evidence)

            for evidence, normalized_email in valid_evidence:
                contact = session.scalar(select(Contact).where(Contact.normalized_email == normalized_email))
                if contact is None:
                    contact = Contact(
                        normalized_email=normalized_email,
                        original_email=evidence.email,
                        email_domain=normalized_email.rsplit("@", 1)[-1],
                        role_class=_role_class(normalized_email),
                        is_guessed=False,
                        attributes={"discovery_method": evidence.discovery_method},
                    )
                    session.add(contact)
                    session.flush()
                evidence_key = hashlib.sha256(
                    f"{domain.id}|{contact.id}|{evidence.source_url}|{evidence.evidence_text}".encode("utf-8")
                ).hexdigest()
                existing = session.scalar(select(DomainContactEvidence).where(DomainContactEvidence.evidence_key == evidence_key))
                if existing is None:
                    session.add(
                        DomainContactEvidence(
                            domain_id=domain.id,
                            contact_id=contact.id,
                            evidence_key=evidence_key,
                            source_url=evidence.source_url,
                            context_snippet=evidence.evidence_text,
                            confidence=evidence.rank / 100,
                            is_public_page=True,
                        )
                    )
                JobQueue(session).enqueue(
                    kind="verify_contact",
                    idempotency_key=f"verify_contact:{contact.id}:initial",
                    subject_type="contact",
                    subject_id=contact.id,
                    payload={"contact_id": contact.id, "poll_count": 0},
                    priority=40,
                    max_attempts=3,
                )
            if valid_evidence:
                attempt.status = ScrapeStatus.SUCCEEDED
                DomainRepository(session).transition(domain.id, LifecycleStage.EMAIL_FOUND, source="crawler", reason=f"{len(valid_evidence)} publicly evidenced contacts")
                domain.next_refresh_at = None
            else:
                final_status = "no_email"
                attempt.status = ScrapeStatus.NO_EMAIL
                DomainRepository(session).transition(domain.id, LifecycleStage.NO_EMAIL, source="crawler", reason="No valid public email found")
                domain.next_refresh_at = utcnow() + timedelta(days=90)
        elif result.status == "no_email":
            attempt.status = ScrapeStatus.NO_EMAIL
            DomainRepository(session).transition(domain.id, LifecycleStage.NO_EMAIL, source="crawler", reason="No public email found")
            domain.next_refresh_at = utcnow() + timedelta(days=90)
        else:
            attempt.status = ScrapeStatus.FAILED
            attempt.error_code = result.error or "crawl_failed"
            attempt.error_detail = result.error
            DomainRepository(session).transition(domain.id, LifecycleStage.FAILED, source="crawler", reason=result.error)
            domain.next_refresh_at = utcnow() + timedelta(days=90)
        final_event = "scrape_failed" if final_status == "failed" else "scrape_completed"
        session.add(
            AuditLog(
                actor_type="worker",
                actor_id=worker_id,
                action=final_event,
                entity_type="scrape_attempt",
                entity_id=attempt.id,
                after={
                    "attempt_id": attempt.id,
                    "domain_id": domain.id,
                    "domain": domain.normalized_domain,
                    "job_id": job_id,
                    "event_type": final_event,
                    "status": final_status,
                    "pages_attempted": attempt.pages_requested,
                    "pages_crawled": attempt.pages_crawled,
                    "emails_found": attempt.emails_found,
                    "max_pages": config.scrape_max_pages,
                    "error_code": attempt.error_code,
                    "occurred_at": utcnow().isoformat(),
                },
                reason="Crawler completed the domain attempt",
            )
        )
        session.commit()
    return {"domain": domain_name, "status": final_status, "pages": result.pages_succeeded, "contacts": accepted_contacts}


def verify_contact_job(job_id: str) -> dict[str, Any]:
    factory = session_factory()
    with factory() as session:
        job = session.get(Job, job_id)
        if job is None:
            raise LookupError(f"job {job_id} disappeared")
        contact_id = str(job.payload.get("contact_id") or job.subject_id or "")
        poll_count = int(job.payload.get("poll_count") or 0)
        contact = session.get(Contact, contact_id)
        if contact is None:
            raise LookupError(f"contact {contact_id} does not exist")
        email = contact.normalized_email

    control = InstantlyControl()
    try:
        result = control.request_verification(email) if poll_count == 0 else control.verification(email)
    except RuntimeError as exc:
        message = str(exc)
        if "HTTP 403" in message and "No credits available" in message:
            raise NonRetryableJobError(
                "Instantly email verification is blocked: no verification credits available"
            ) from exc
        raise
    raw_status = str(result.get("verification_status") or "unknown")
    status = VerificationStatus(raw_status) if raw_status in {item.value for item in VerificationStatus} else VerificationStatus.UNKNOWN
    catch_all = result.get("catch_all")
    if isinstance(catch_all, str):
        catch_all = catch_all.lower() == "true"
    eligible = verification_is_eligible({"verification_status": raw_status, "catch_all": catch_all})

    with factory() as session:
        contact = session.get(Contact, contact_id)
        checked = utcnow()
        session.add(
            VerificationResult(
                contact_id=contact_id,
                provider="instantly",
                provider_request_id=str(result.get("id") or f"{email}:{checked.isoformat()}"),
                status=status,
                catch_all=catch_all if isinstance(catch_all, bool) else None,
                risky=False if eligible else None,
                checked_at=checked,
                expires_at=checked + timedelta(days=90) if status == VerificationStatus.VERIFIED else None,
                result_summary={key: value for key, value in result.items() if key not in {"raw", "body"}},
            )
        )
        domain_ids = list(
            session.scalars(
                select(DomainContactEvidence.domain_id)
                .where(DomainContactEvidence.contact_id == contact_id, DomainContactEvidence.is_public_page.is_(True))
                .distinct()
            )
        )
        if eligible:
            for domain_id in domain_ids:
                domain = session.get(Domain, domain_id)
                if domain and domain.lifecycle_stage == LifecycleStage.EMAIL_FOUND:
                    repo = DomainRepository(session)
                    repo.transition(domain.id, LifecycleStage.VERIFIED, source="instantly_verification")
                    repo.transition(domain.id, LifecycleStage.OUTREACH_READY, source="eligibility_gate")
        elif status in {VerificationStatus.INVALID, VerificationStatus.CATCH_ALL} or catch_all is True:
            if session.scalar(select(Suppression.id).where(Suppression.contact_id == contact_id, Suppression.active.is_(True))) is None:
                session.add(Suppression(contact_id=contact_id, scope="contact", reason="instantly_verification_ineligible", source="instantly", permanent=True, active=True))
            for domain_id in domain_ids:
                domain = session.get(Domain, domain_id)
                if domain and domain.lifecycle_stage == LifecycleStage.EMAIL_FOUND:
                    DomainRepository(session).transition(domain.id, LifecycleStage.SUPPRESSED, source="instantly_verification", reason="invalid or catch-all contact")
        elif status == VerificationStatus.PENDING and poll_count < 12:
            JobQueue(session).enqueue(
                kind="verify_contact",
                idempotency_key=f"verify_contact:{contact_id}:poll:{poll_count + 1}",
                subject_type="contact",
                subject_id=contact_id,
                payload={"contact_id": contact_id, "poll_count": poll_count + 1},
                priority=35,
                max_attempts=3,
                available_at=utcnow() + timedelta(minutes=5),
            )
        session.commit()
    return {"contact_id": contact_id, "status": status.value, "eligible": eligible, "poll_count": poll_count}


def reconcile_instantly_job(job_id: str | None = None, *, force_full: bool = False) -> dict[str, Any]:
    control = InstantlyControl()
    accounts = control.list_accounts()
    campaigns = control.list_campaigns()
    analytics = control.analytics()
    melbourne_today = datetime.now(ZoneInfo("Australia/Melbourne")).date().isoformat()
    today_analytics = control.analytics(
        start_date=melbourne_today,
        end_date=melbourne_today,
    )
    analytics_by_id = {str(row.get("campaign_id") or ""): row for row in analytics}
    today_analytics_by_id = {
        str(row.get("campaign_id") or ""): row for row in today_analytics
    }
    full = force_full
    if job_id:
        with session_factory()() as job_session:
            job = job_session.get(Job, job_id)
            full = full or bool(job and job.payload.get("full"))
    matched = matching_campaigns(campaigns)
    managed_by_id = {
        str(campaign.get("id") or ""): campaign
        for campaign in campaigns
        if str(campaign.get("name") or "").startswith(MANAGED_NAME_PREFIX)
    }
    pause_campaign_ids: set[str] = set()
    with platform_session() as session:
        for account in accounts:
            external_id = str(account.get("id") or account.get("email") or "")
            sender = session.scalar(select(SenderAccount).where(SenderAccount.external_id == external_id))
            if sender is None:
                sender = SenderAccount(external_id=external_id)
                session.add(sender)
            healthy = sender_is_healthy(account)
            sender.sender_email = account.get("email")
            sender.provider_status = account.get("status")
            if healthy and not sender.connected:
                sender.healthy_since = utcnow()
            if not healthy:
                sender.healthy_since = None
            sender.connected = healthy
            sender.last_checked_at = utcnow()
            sender.last_error = None if healthy else json.dumps(account.get("status_message") or {"status": account.get("status")})[:2000]
            sender.new_leads_sent_today = 0
            sender.total_emails_sent_today = 0
            sender.bounce_protection_triggered = False

        globally_paused = is_outreach_paused(session)
        known_managed_ids = {
            str(value)
            for value in session.scalars(
                select(CampaignBatch.external_campaign_id).where(
                    CampaignBatch.external_campaign_id.is_not(None)
                )
            )
        }
        orphan_active_ids = {
            campaign_id
            for campaign_id, campaign in managed_by_id.items()
            if campaign_id not in known_managed_ids and campaign.get("status") in {1, 4}
        }
        if orphan_active_ids:
            pause_campaign_ids.update(orphan_active_ids)
            set_outreach_paused(
                session,
                paused=True,
                actor="link-os-worker",
                reason="Unmapped active LINK OS campaign detected in Instantly",
            )
            globally_paused = True
        for batch in session.scalars(select(CampaignBatch).where(CampaignBatch.external_campaign_id.is_not(None))):
            metrics = analytics_by_id.get(str(batch.external_campaign_id), {})
            today_metrics = today_analytics_by_id.get(str(batch.external_campaign_id), {})
            sent = max(int(metrics.get("emails_sent_count") or 0), 1)
            remote = managed_by_id.get(str(batch.external_campaign_id))
            remote_status = remote.get("status") if remote else None
            if (
                batch.status == CampaignBatchStatus.FAILED
                and batch.paused_reason == "retired_after_bounce"
            ):
                batch.active_assignment = False
                continue
            if batch.sender_account_id:
                sender = session.get(SenderAccount, batch.sender_account_id)
                if sender:
                    sender.rolling_bounce_rate = int(metrics.get("bounced_count") or 0) / sent
                    sender.rolling_unsubscribe_rate = int(metrics.get("unsubscribed_count") or 0) / sent
                    batch.hard_bounce_count = int(metrics.get("bounced_count") or 0)
                    batch.unsubscribe_count = int(metrics.get("unsubscribed_count") or 0)
                    sender.new_leads_sent_today += int(today_metrics.get("new_leads_contacted_count") or 0)
                    sender.total_emails_sent_today += int(today_metrics.get("emails_sent_count") or 0)
                    sender.bounce_protection_triggered = (
                        sender.bounce_protection_triggered or remote_status == -2
                    )
                    unhealthy = not (sender.connected and sender.provider_status == 1 and not sender.last_error)
                    launch = session.get(CampaignLaunch, batch.launch_id) if batch.launch_id else None
                    local_launch_safety = bool(
                        launch
                        and (
                            batch.hard_bounce_count >= launch.hard_bounce_limit
                            or batch.unsubscribe_count >= 1
                            or sender.bounce_protection_triggered
                        )
                    )
                    unsafe_metrics = local_launch_safety or (
                        launch is None
                        and (
                            sender.rolling_bounce_rate >= 0.03
                            or sender.rolling_unsubscribe_rate >= 0.01
                            or sender.bounce_protection_triggered
                        )
                    )
                    remotely_running = remote_status in {1, 4}
                    remote_safety_pause = remote_status in {-99, -1, -2}
                    if remotely_running and (globally_paused or unhealthy or unsafe_metrics):
                        pause_campaign_ids.add(str(batch.external_campaign_id))
                    if (
                        remotely_running and (globally_paused or unhealthy or unsafe_metrics)
                    ) or remote_safety_pause:
                        batch.status = CampaignBatchStatus.PAUSED
                        batch.paused_at = utcnow()
                        reasons = []
                        if globally_paused:
                            reasons.append("global_outreach_pause")
                        if unhealthy:
                            reasons.append("sender_unhealthy")
                        if launch and batch.hard_bounce_count >= launch.hard_bounce_limit:
                            reasons.append("three_hard_bounces")
                        elif not launch and sender.rolling_bounce_rate >= 0.03:
                            reasons.append("bounce_rate_at_or_above_3_percent")
                        if launch and batch.unsubscribe_count >= 1:
                            reasons.append("campaign_unsubscribe_recorded")
                        elif not launch and sender.rolling_unsubscribe_rate >= 0.01:
                            reasons.append("unsubscribe_rate_at_or_above_1_percent")
                        if sender.bounce_protection_triggered:
                            reasons.append("bounce_protection_triggered")
                        if remote_status == -99:
                            reasons.append("account_suspended")
                        if remote_status == -1:
                            reasons.append("accounts_unhealthy")
                        batch.paused_reason = ",".join(reasons)
                        if launch is None:
                            set_outreach_paused(
                                session,
                                paused=True,
                                actor="link-os-worker",
                                reason=f"Managed campaign safety pause: {batch.paused_reason}",
                            )
            if remote_status in {1, 4} and str(batch.external_campaign_id) not in pause_campaign_ids:
                batch.status = CampaignBatchStatus.ACTIVE
                batch.active_assignment = True
            elif remote_status in {0, 2}:
                batch.status = CampaignBatchStatus.PAUSED
                batch.active_assignment = True
            elif remote_status == COMPLETED_CAMPAIGN_STATUS:
                batch.status = CampaignBatchStatus.COMPLETED
                batch.active_assignment = False
                for member in session.scalars(
                    select(CampaignMember).where(
                        CampaignMember.campaign_batch_id == batch.id,
                        CampaignMember.reservation_active.is_(True),
                    )
                ):
                    member.reservation_active = False
        session.add(
            AuditLog(
                actor_type="worker",
                actor_id=socket.gethostname(),
                action="instantly.reconciled",
                entity_type="integration",
                entity_id="instantly",
                after={
                    "accounts": len(accounts),
                    "campaigns": len(campaigns),
                    "analytics": len(analytics),
                    "today_analytics": len(today_analytics),
                    "full": full,
                },
            )
        )
        for launch in session.scalars(select(CampaignLaunch)):
            launch_batches = list(
                session.scalars(
                    select(CampaignBatch).where(CampaignBatch.launch_id == launch.id)
                )
            )
            launch_block_reasons = {
                "three_hard_bounces",
                "campaign_unsubscribe_recorded",
                "bounce_protection_triggered",
                "sender_unhealthy",
                "account_suspended",
                "accounts_unhealthy",
            }
            if launch_batches and all(
                batch.status in {CampaignBatchStatus.PAUSED, CampaignBatchStatus.FAILED}
                and bool(
                    launch_block_reasons.intersection(
                        (batch.paused_reason or "").split(",")
                    )
                )
                for batch in launch_batches
            ):
                launch.status = CampaignLaunchStatus.FAILED
                set_outreach_paused(
                    session,
                    paused=True,
                    actor="link-os-worker",
                    reason=f"All campaigns in launch {launch.id} are blocked",
                )

    if pause_campaign_ids and os.getenv("LINK_OS_MODE", "shadow").strip().lower() == "live":
        for campaign_id in sorted(pause_campaign_ids):
            control.pause_campaign(campaign_id)

    membership_count = 0
    contacted_domains = 0
    campaigns_to_reconcile = campaigns if full else list(managed_by_id.values())
    if campaigns_to_reconcile:
        factory = session_factory()
        for campaign in campaigns_to_reconcile:
            campaign_id = str(campaign.get("id") or "")
            if not campaign_id:
                continue
            leads = control.list_campaign_leads(campaign_id)
            is_managed = str(campaign.get("name") or "").startswith(MANAGED_NAME_PREFIX)
            with factory() as session:
                for lead in leads:
                    raw_email = str(lead.get("email") or "").strip().lower()
                    try:
                        email = normalize_email(raw_email)
                    except ValueError:
                        continue
                    contact = session.scalar(select(Contact).where(Contact.normalized_email == email))
                    if contact is None:
                        contact = Contact(
                            normalized_email=email,
                            original_email=raw_email,
                            email_domain=email.rsplit("@", 1)[-1],
                            role_class=_role_class(email),
                            is_guessed=False,
                            attributes={"source": "instantly_full_reconcile"},
                        )
                        session.add(contact)
                        session.flush()
                    domain_name = _lead_domain(lead, email)
                    domain = None
                    if domain_name:
                        domain, _created = DomainRepository(session).get_or_create(
                            domain_name,
                            source="instantly_full_reconcile",
                        )
                    external_lead_id = str(lead.get("id") or "")
                    dedupe_key = f"membership:{campaign_id}:{external_lead_id or email}"
                    event = session.scalar(select(InstantlyEvent).where(InstantlyEvent.dedupe_key == dedupe_key))
                    evidence_payload = _lead_delivery_evidence_payload(
                        lead,
                        managed=is_managed,
                        campaign_name=str(campaign.get("name") or ""),
                        link_building_scope=campaign_is_link_building(campaign),
                    )
                    if event is None:
                        event = InstantlyEvent(
                            dedupe_key=dedupe_key,
                            external_event_id=external_lead_id or None,
                            event_type="campaign_membership_reconciled",
                            external_campaign_id=campaign_id,
                            external_lead_id=external_lead_id or None,
                            domain_id=domain.id if domain else None,
                            contact_id=contact.id,
                            event_at=_message_at(lead),
                            payload=evidence_payload,
                        )
                        session.add(event)
                        membership_count += 1
                    else:
                        event.payload = {
                            **(event.payload or {}),
                            **evidence_payload,
                        }
                    # Historical workspace membership is a hard dedupe signal,
                    # but membership alone is not proof that an email was sent.
                    # Only Instantly's explicit last-contact timestamp may
                    # advance a legacy lead to CONTACTED.
                    if not is_managed:
                        if session.scalar(
                            select(Suppression.id).where(
                                Suppression.contact_id == contact.id,
                                Suppression.active.is_(True),
                                Suppression.reason == "historical_campaign_membership",
                            )
                        ) is None:
                            session.add(
                                Suppression(
                                    contact_id=contact.id,
                                    domain_id=domain.id if domain else None,
                                    scope="contact",
                                    reason="historical_campaign_membership",
                                    source="instantly",
                                    permanent=True,
                                    active=True,
                                )
                            )
                        contacted_at = _lead_last_contact_at(lead)
                        if domain and contacted_at and domain.lifecycle_stage not in {
                            LifecycleStage.SUPPRESSED,
                            LifecycleStage.CONTACTED,
                            LifecycleStage.REPLIED,
                            LifecycleStage.OFFER_REVIEW,
                            LifecycleStage.OFFER_APPROVED,
                            LifecycleStage.LISTED,
                            LifecycleStage.LOST,
                        }:
                            _authoritative_stage(session, domain, LifecycleStage.CONTACTED, source="instantly_membership")
                            domain.last_contacted_at = domain.last_contacted_at or contacted_at
                            contacted_domains += 1
                    else:
                        try:
                            lead_status = int(lead.get("status"))
                        except (TypeError, ValueError):
                            lead_status = 0
                        status_stamp = str(
                            lead.get("timestamp_updated")
                            or lead.get("timestamp_last_contact")
                            or lead_status
                        )
                        status_key = (
                            f"lead_status:{campaign_id}:{external_lead_id or email}:"
                            f"{lead_status}:{status_stamp}"
                        )
                        if session.scalar(
                            select(InstantlyEvent.id).where(
                                InstantlyEvent.dedupe_key == status_key
                            )
                        ) is None:
                            session.add(
                                InstantlyEvent(
                                    dedupe_key=status_key,
                                    external_event_id=external_lead_id or None,
                                    event_type="managed_lead_status",
                                    external_campaign_id=campaign_id,
                                    external_lead_id=external_lead_id or None,
                                    domain_id=domain.id if domain else None,
                                    contact_id=contact.id,
                                    event_at=_message_at(lead),
                                    payload={"status": lead_status},
                                )
                            )
                        batch = session.scalar(
                            select(CampaignBatch).where(
                                CampaignBatch.external_campaign_id == campaign_id
                            )
                        )
                        member = (
                            session.scalar(
                                select(CampaignMember).where(
                                    CampaignMember.campaign_batch_id == batch.id,
                                    CampaignMember.contact_id == contact.id,
                                )
                            )
                            if batch
                            else None
                        )
                        if lead_status in {-1, -2}:
                            reason = "instantly_bounced" if lead_status == -1 else "instantly_unsubscribed"
                            if session.scalar(
                                select(Suppression.id).where(
                                    Suppression.contact_id == contact.id,
                                    Suppression.active.is_(True),
                                    Suppression.reason == reason,
                                )
                            ) is None:
                                session.add(
                                    Suppression(
                                        contact_id=contact.id,
                                        domain_id=domain.id if domain else None,
                                        scope="contact",
                                        reason=reason,
                                        source="instantly",
                                        permanent=True,
                                        active=True,
                                    )
                                )
                            if member:
                                member.status = CampaignMemberStatus.FAILED
                                member.reservation_active = False
                                member.failure_reason = reason
                            if domain and domain.lifecycle_stage not in {
                                LifecycleStage.REPLIED,
                                LifecycleStage.OFFER_REVIEW,
                                LifecycleStage.OFFER_APPROVED,
                                LifecycleStage.LISTED,
                                LifecycleStage.LOST,
                            }:
                                target = (
                                    LifecycleStage.SUPPRESSED
                                    if lead_status == -2
                                    else LifecycleStage.CONTACTED
                                )
                                _authoritative_stage(
                                    session,
                                    domain,
                                    target,
                                    source=reason,
                                )
                                domain.last_contacted_at = domain.last_contacted_at or utcnow()
                        elif lead_status == 3:
                            if member:
                                member.status = CampaignMemberStatus.CONTACTED
                            if domain and domain.lifecycle_stage not in {
                                LifecycleStage.REPLIED,
                                LifecycleStage.OFFER_REVIEW,
                                LifecycleStage.OFFER_APPROVED,
                                LifecycleStage.LISTED,
                                LifecycleStage.LOST,
                            }:
                                _authoritative_stage(
                                    session,
                                    domain,
                                    LifecycleStage.CONTACTED,
                                    source="instantly_lead_completed",
                                )
                                domain.last_contacted_at = domain.last_contacted_at or utcnow()
                                contacted_domains += 1
                session.commit()
        if full:
            with platform_session() as session:
                setting = session.get(SystemSetting, "instantly.full_reconcile_completed")
                value = {
                    "complete": True,
                    "completed_at": utcnow().isoformat(),
                    "campaigns": len(campaigns),
                    "legacy_campaigns": len(matched),
                    "memberships": membership_count,
                }
                if setting is None:
                    session.add(
                        SystemSetting(
                            key="instantly.full_reconcile_completed",
                            value=value,
                            updated_by="link-os-worker",
                        )
                    )
                else:
                    setting.value = value
                    setting.version += 1
                    setting.updated_by = "link-os-worker"

    return {
        "accounts": len(accounts),
        "campaigns": len(campaigns),
        "matched_campaigns": len(matched),
        "analytics": len(analytics),
        "today_analytics": len(today_analytics),
        "full": full,
        "memberships_imported": membership_count,
        "contacted_domains": contacted_domains,
        "campaigns_paused_for_safety": len(pause_campaign_ids),
    }


def _campaign_candidates(
    session: Session,
    limit: int,
    *,
    contact_policy: str = STRICT_VERIFIED_CONTACT_POLICY,
) -> list[tuple[str, str]]:
    repository = CampaignRepository(session)
    allowed_stages = [LifecycleStage.OUTREACH_READY]
    if contact_policy == PUBLIC_EVIDENCE_CONTACT_POLICY:
        allowed_stages.extend([LifecycleStage.EMAIL_FOUND, LifecycleStage.VERIFIED])
    rows = session.execute(
        select(DomainContactEvidence.domain_id, DomainContactEvidence.contact_id)
        .join(Domain, Domain.id == DomainContactEvidence.domain_id)
        .where(
            Domain.lifecycle_stage.in_(allowed_stages),
            DomainContactEvidence.is_public_page.is_(True),
        )
        .order_by(DomainContactEvidence.confidence.desc(), Domain.created_at)
    )
    members: list[tuple[str, str]] = []
    seen_domains: set[str] = set()
    seen_contacts: set[str] = set()
    for domain_id, contact_id in rows:
        if domain_id in seen_domains or contact_id in seen_contacts:
            continue
        if repository.eligibility_reasons(
            domain_id, contact_id, contact_policy=contact_policy
        ):
            continue
        members.append((domain_id, contact_id))
        seen_domains.add(domain_id)
        seen_contacts.add(contact_id)
        if len(members) >= limit:
            break
    return members


def _as_aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _pilot_has_launched(session: Session) -> bool:
    return any(
        bool((batch.settings_snapshot or {}).get("pilot", True))
        and batch.activated_at is not None
        for batch in session.scalars(select(CampaignBatch))
    )


def _pilot_is_in_flight(session: Session) -> bool:
    in_flight = {
        CampaignBatchStatus.RESERVED,
        CampaignBatchStatus.UPLOADING,
        CampaignBatchStatus.PAUSED,
        CampaignBatchStatus.ACTIVE,
    }
    return any(
        bool((batch.settings_snapshot or {}).get("pilot", True))
        and batch.status in in_flight
        for batch in session.scalars(select(CampaignBatch))
    )


def _healthy_sender_for_batch(
    session: Session,
    batch: CampaignBatch | None = None,
    *,
    require_health_gate: bool = False,
    now: datetime | None = None,
) -> SenderAccount | None:
    instant = now or utcnow()

    def passes_health_gate(sender: SenderAccount) -> bool:
        healthy_since = _as_aware(sender.healthy_since)
        return not require_health_gate or bool(
            healthy_since is not None
            and instant - healthy_since >= timedelta(hours=HEALTH_GATE_HOURS)
        )

    if batch and batch.sender_account_id:
        sender = session.get(SenderAccount, batch.sender_account_id)
        if (
            sender
            and sender.connected
            and sender.provider_status == 1
            and not sender.last_error
            and passes_health_gate(sender)
        ):
            return sender
        return None
    senders = list(
        session.scalars(
            select(SenderAccount)
            .where(
                SenderAccount.connected.is_(True),
                SenderAccount.provider_status == 1,
                SenderAccount.last_error.is_(None),
            )
            .order_by(SenderAccount.sender_email)
        )
    )
    for sender in senders:
        if not passes_health_gate(sender):
            continue
        assigned = session.scalar(
            select(CampaignBatch.id)
            .where(
                CampaignBatch.sender_account_id == sender.id,
                CampaignBatch.active_assignment.is_(True),
            )
            .limit(1)
        )
        if assigned is None:
            return sender
    return None


def _next_campaign_request(
    session: Session,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Choose the next safe batch shape without reserving any contacts."""

    instant = now or utcnow()
    pilot_launched = _pilot_has_launched(session)
    if not pilot_launched:
        if _pilot_is_in_flight(session):
            return None
        sender = _healthy_sender_for_batch(session, now=instant)
        return {"pilot": True, "target_size": PILOT_BATCH_SIZE} if sender else None
    sender = _healthy_sender_for_batch(
        session,
        require_health_gate=True,
        now=instant,
    )
    return {"pilot": False, "target_size": NORMAL_BATCH_SIZE} if sender else None


def prepare_campaign_batch_job(job_id: str) -> dict[str, Any]:
    """Reserve, build paused, upload, read back, and gate one managed batch.

    A prepare-only request may build and upload a paused pilot while LINK OS is
    in shadow mode and globally paused. Activation still requires live mode,
    an explicit resume, and every sender/campaign safety gate.
    """

    factory = session_factory()
    with factory() as session:
        job = session.get(Job, job_id)
        if job is None:
            raise LookupError(f"job {job_id} disappeared")
        prepare_only = job.payload.get("prepare_only") is True
        requested_policy = str(
            job.payload.get("contact_policy") or STRICT_VERIFIED_CONTACT_POLICY
        )
        mode = os.getenv("LINK_OS_MODE", "shadow").strip().lower()
        if mode != "live" and not prepare_only:
            return {"blocked": True, "reasons": ["shadow_mode_enabled"]}
        if is_outreach_paused(session) and not prepare_only:
            return {"blocked": True, "reasons": ["global_outreach_pause_enabled"]}
        reconciled = session.get(SystemSetting, "instantly.full_reconcile_completed")
        if reconciled is None or reconciled.value.get("complete") is not True:
            return {"blocked": True, "reasons": ["full_instantly_reconciliation_required"]}

        batch_id = str(job.payload.get("batch_id") or "").strip()
        batch = session.get(CampaignBatch, batch_id) if batch_id else None
        if batch is not None and batch.status == CampaignBatchStatus.ACTIVE:
            return {"batch_id": batch.id, "activated": True, "idempotent": True}
        if batch is None:
            batch = session.scalar(
                select(CampaignBatch)
                .where(
                    CampaignBatch.status.in_(
                        [CampaignBatchStatus.RESERVED, CampaignBatchStatus.UPLOADING, CampaignBatchStatus.PAUSED]
                    )
                )
                .order_by(CampaignBatch.created_at)
                .limit(1)
            )
        if batch is None:
            pilot = job.payload.get("pilot", True) is not False
            contact_policy = requested_policy
        else:
            pilot = bool((batch.settings_snapshot or {}).get("pilot", True))
            contact_policy = str(
                (batch.settings_snapshot or {}).get("contact_policy")
                or requested_policy
            )
        if contact_policy == PUBLIC_EVIDENCE_CONTACT_POLICY and not pilot:
            return {
                "blocked": True,
                "reasons": ["verification_skipped_policy_is_pilot_only"],
            }
        requested_size = int(
            job.payload.get("target_size")
            or (PILOT_BATCH_SIZE if pilot else NORMAL_BATCH_SIZE)
        )
        if pilot and requested_size > PILOT_BATCH_SIZE:
            return {"blocked": True, "reasons": ["pilot_batch_cannot_exceed_25_contacts"]}
        if not pilot and not _pilot_has_launched(session):
            return {"blocked": True, "reasons": ["successful_pilot_launch_required"]}
        sender = _healthy_sender_for_batch(
            session,
            batch,
            require_health_gate=not pilot,
        )
        if sender is None:
            reason = (
                "no_unassigned_sender_with_72_healthy_hours"
                if not pilot
                else "no_unassigned_healthy_sender"
            )
            return {"blocked": True, "reasons": [reason]}

        if batch is None:
            target_size = max(1, min(requested_size, NORMAL_BATCH_SIZE))
            members = _campaign_candidates(
                session, target_size, contact_policy=contact_policy
            )
            if not members:
                return {"blocked": True, "reasons": ["no_eligible_outreach_contacts"]}
            if pilot and len(members) != PILOT_BATCH_SIZE:
                return {
                    "blocked": True,
                    "reasons": ["pilot_requires_exactly_25_eligible_contacts"],
                    "eligible_contacts": len(members),
                }
            digest = hashlib.sha256(
                "|".join(f"{domain_id}:{contact_id}" for domain_id, contact_id in members).encode()
            ).hexdigest()[:24]
            batch_number = int(session.scalar(select(func.count(CampaignBatch.id))) or 0) + 1
            deterministic_key = f"{'pilot' if pilot else 'batch'}:{digest}"
            config_version = (
                PUBLIC_EVIDENCE_PILOT_COPY_VERSION
                if contact_policy == PUBLIC_EVIDENCE_CONTACT_POLICY
                else CAMPAIGN_COPY_VERSION
            )
            campaign_label = (
                "Public Evidence Pilot"
                if contact_policy == PUBLIC_EVIDENCE_CONTACT_POLICY
                else "Verified Relaunch"
            )
            name = f"{MANAGED_NAME_PREFIX} {campaign_label} | Batch {batch_number:04d} | {config_version}"
            try:
                batch, _created = CampaignRepository(session).reserve_batch(
                    deterministic_key=deterministic_key,
                    name=name,
                    config_version=config_version,
                    members=members,
                    contact_policy=contact_policy,
                )
            except CampaignReservationError as exc:
                return {"blocked": True, "reasons": [str(exc)]}
            batch.sender_account_id = sender.id
            batch.active_assignment = True
            batch.settings_snapshot = {
                **(batch.settings_snapshot or {}),
                "pilot": pilot,
                "batch_number": batch_number,
                "target_size": len(members),
                "contact_policy": contact_policy,
                "deliverability_verification": (
                    "skipped"
                    if contact_policy == PUBLIC_EVIDENCE_CONTACT_POLICY
                    else "instantly_required"
                ),
                "prepare_only": prepare_only,
            }
            job.payload = {**job.payload, "batch_id": batch.id}
            session.commit()
        else:
            batch_number = int((batch.settings_snapshot or {}).get("batch_number") or 1)
            batch.sender_account_id = sender.id
            batch.active_assignment = True
            session.commit()

        batch_id = batch.id
        sender_id = sender.id
        sender_external_id = sender.external_id
        sender_email = sender.sender_email or ""

    control = InstantlyControl()
    accounts = control.list_accounts()
    account = next(
        (
            row
            for row in accounts
            if str(row.get("id") or row.get("email") or "") == sender_external_id
            or str(row.get("email") or "").lower() == sender_email.lower()
        ),
        None,
    )
    if not account or not sender_is_healthy(account):
        with factory() as session:
            batch = session.get(CampaignBatch, batch_id)
            if batch:
                batch.status = CampaignBatchStatus.PAUSED
                batch.paused_at = utcnow()
                batch.paused_reason = "sender_not_healthy"
            set_outreach_paused(
                session,
                paused=True,
                actor="link-os-worker",
                reason="Managed campaign sender became unhealthy",
            )
            session.commit()
        return {"blocked": True, "batch_id": batch_id, "reasons": ["sender_not_healthy"]}

    with factory() as session:
        batch = session.get(CampaignBatch, batch_id)
        sender = session.get(SenderAccount, sender_id)
        if batch is None or sender is None:
            raise LookupError("campaign reservation disappeared")
        payload = safe_campaign_payload(
            batch_number=batch_number,
            sender=sender.sender_email or str(account.get("email") or ""),
            pilot=pilot,
        )
        payload["name"] = batch.name
        batch.settings_snapshot = {**(batch.settings_snapshot or {}), "campaign": payload}
        members = list(
            session.execute(
                select(CampaignMember, Contact, Domain)
                .join(Contact, Contact.id == CampaignMember.contact_id)
                .join(Domain, Domain.id == CampaignMember.domain_id)
                .where(CampaignMember.campaign_batch_id == batch.id)
                .order_by(CampaignMember.reserved_at)
            )
        )
        lead_payloads = [
            {
                "email": contact.normalized_email,
                "first_name": "there",
                "company_name": domain.normalized_domain,
                "website": f"https://{domain.normalized_domain}",
                "custom_variables": {
                    "root_domain": domain.normalized_domain,
                    "site_name": domain.normalized_domain,
                },
            }
            for _member, contact, domain in members
        ]
        requested_emails = {contact.normalized_email for _member, contact, _domain in members}
        session.commit()

    campaigns = control.list_campaigns()
    existing_campaign = next((row for row in campaigns if str(row.get("name") or "") == batch.name), None)
    if existing_campaign is not None:
        external_campaign_id = str(existing_campaign.get("id") or "")
        campaign_readback = existing_campaign
        if campaign_readback.get("status") == 1:
            campaign_readback = control.pause_campaign(external_campaign_id)
    else:
        campaign_readback = control.create_paused_campaign(payload)
        external_campaign_id = str(campaign_readback.get("id") or "")
    if not external_campaign_id:
        raise RuntimeError("managed campaign has no Instantly id")

    with factory() as session:
        batch = session.get(CampaignBatch, batch_id)
        if batch is None:
            raise LookupError("campaign batch disappeared")
        batch.external_campaign_id = external_campaign_id
        batch.status = CampaignBatchStatus.UPLOADING
        session.commit()

    current_leads = control.list_campaign_leads(external_campaign_id)
    current_emails = {str(row.get("email") or "").lower() for row in current_leads}
    missing_leads = [lead for lead in lead_payloads if str(lead["email"]).lower() not in current_emails]
    upload_result = (
        control.upload_leads(
            external_campaign_id,
            missing_leads,
            verify_leads_on_import=(
                contact_policy != PUBLIC_EVIDENCE_CONTACT_POLICY
            ),
        )
        if missing_leads
        else {"requested": 0, "total_sent": 0, "matches": True, "results": []}
    )
    readback_leads = control.list_campaign_leads(external_campaign_id)
    readback_by_email = {
        str(row.get("email") or "").lower(): row
        for row in readback_leads
        if str(row.get("email") or "").lower() in requested_emails
    }
    pending = sum(
        1
        for row in readback_by_email.values()
        if str(row.get("verification_status") or "").lower() == "pending"
    ) if contact_policy == STRICT_VERIFIED_CONTACT_POLICY else 0
    analytics_rows = control.analytics(external_campaign_id)
    analytics = analytics_rows[0] if analytics_rows else {}
    sent = int(analytics.get("emails_sent_count") or 0)
    bounce_rate = (int(analytics.get("bounced_count") or 0) / sent) if sent else 0.0
    unsubscribe_rate = (int(analytics.get("unsubscribed_count") or 0) / sent) if sent else 0.0

    with factory() as session:
        batch = session.get(CampaignBatch, batch_id)
        sender = session.get(SenderAccount, sender_id)
        if batch is None or sender is None:
            raise LookupError("campaign state disappeared")
        batch.uploaded_member_count = len(readback_by_email)
        batch.readback_member_count = len(readback_by_email)
        batch.pending_verification_count = pending
        batch.status = CampaignBatchStatus.PAUSED
        sender.rolling_bounce_rate = bounce_rate
        sender.rolling_unsubscribe_rate = unsubscribe_rate
        for member, contact, domain in session.execute(
            select(CampaignMember, Contact, Domain)
            .join(Contact, Contact.id == CampaignMember.contact_id)
            .join(Domain, Domain.id == CampaignMember.domain_id)
            .where(CampaignMember.campaign_batch_id == batch.id)
        ):
            lead = readback_by_email.get(contact.normalized_email)
            if lead:
                member.status = CampaignMemberStatus.UPLOADED
                member.external_lead_id = str(lead.get("id") or "") or None
                member.uploaded_at = utcnow()
                if domain.lifecycle_stage == LifecycleStage.CAMPAIGN_QUEUED:
                    DomainRepository(session).transition(
                        domain.id,
                        LifecycleStage.UPLOADED,
                        source="instantly_upload_readback",
                    )
        other_active = int(
            session.scalar(
                select(func.count(CampaignBatch.id)).where(
                    CampaignBatch.sender_account_id == sender.id,
                    CampaignBatch.active_assignment.is_(True),
                    CampaignBatch.id != batch.id,
                )
            )
            or 0
        )
        decision = evaluate_campaign_activation(
            outreach_paused=is_outreach_paused(session),
            sender=SenderSnapshot(
                external_id=sender.external_id,
                connected=sender.connected,
                provider_status=sender.provider_status,
                healthy_since=sender.healthy_since,
                active_link_os_campaigns=other_active,
                new_leads_sent_today=sender.new_leads_sent_today,
                total_emails_sent_today=sender.total_emails_sent_today,
                bounce_rate=sender.rolling_bounce_rate,
                unsubscribe_rate=sender.rolling_unsubscribe_rate,
                has_error=bool(sender.last_error),
                bounce_protection_triggered=sender.bounce_protection_triggered,
            ),
            campaign=CampaignUploadSnapshot(
                expected_members=batch.expected_member_count,
                uploaded_members=batch.uploaded_member_count,
                readback_members=batch.readback_member_count,
                built_paused=campaign_readback.get("status") != 1,
                pending_verifications=batch.pending_verification_count,
                tracking_enabled=bool(payload.get("open_tracking") or payload.get("link_tracking")),
                unsubscribe_header_enabled=payload.get("insert_unsubscribe_header") is True,
                stop_on_reply=payload.get("stop_on_reply") is True,
                stop_on_auto_reply=payload.get("stop_on_auto_reply") is True,
                stop_on_company_reply=payload.get("stop_for_company") is True,
                bounce_protection_enabled=payload.get("disable_bounce_protect") is False,
                risky_contacts_enabled=payload.get("allow_risky_contacts") is True,
            ),
        )
        if not upload_result.get("matches") and len(readback_by_email) != batch.expected_member_count:
            reasons = ("campaign_upload_response_mismatch", *decision.reasons)
        else:
            reasons = decision.reasons
        if prepare_only and len(readback_by_email) == batch.expected_member_count:
            reasons = (
                "prepared_paused_only",
                *(
                    ("deliverability_verification_skipped",)
                    if contact_policy == PUBLIC_EVIDENCE_CONTACT_POLICY
                    else ()
                ),
            )
        batch.paused_reason = ",".join(dict.fromkeys(reasons)) or None
        batch.paused_at = utcnow()
        session.commit()

    if prepare_only and len(readback_by_email) == batch.expected_member_count:
        with factory() as session:
            prepared_batch = session.get(CampaignBatch, batch_id)
            if prepared_batch and prepared_batch.launch_id:
                launch = session.get(CampaignLaunch, prepared_batch.launch_id)
                launch_batches = list(session.scalars(select(CampaignBatch).where(CampaignBatch.launch_id == prepared_batch.launch_id)))
                if launch and len(launch_batches) == 3 and all(
                    item.status == CampaignBatchStatus.PAUSED
                    and item.readback_member_count == item.expected_member_count
                    for item in launch_batches
                ):
                    launch.status = CampaignLaunchStatus.PAUSED
                    session.commit()
        return {
            "batch_id": batch_id,
            "external_campaign_id": external_campaign_id,
            "prepared": True,
            "activated": False,
            "contact_policy": contact_policy,
            "verification_skipped": contact_policy == PUBLIC_EVIDENCE_CONTACT_POLICY,
            "uploaded": len(readback_by_email),
            "remote_status": campaign_readback.get("status"),
        }

    if reasons:
        return {
            "batch_id": batch_id,
            "external_campaign_id": external_campaign_id,
            "blocked": True,
            "reasons": list(reasons),
            "uploaded": len(readback_by_email),
        }

    # Close the decision-to-activation gap with fresh local and provider
    # readbacks. Any uncertainty leaves the campaign paused.
    live_accounts = control.list_accounts()
    live_account = next(
        (
            row
            for row in live_accounts
            if str(row.get("id") or row.get("email") or "") == sender_external_id
            or str(row.get("email") or "").lower() == sender_email.lower()
        ),
        None,
    )
    live_campaign = control.get_campaign(external_campaign_id)
    live_campaigns = control.list_campaigns()
    active_remote_others = [
        row
        for row in live_campaigns
        if str(row.get("id") or "") != external_campaign_id
        and str(row.get("name") or "").startswith(MANAGED_NAME_PREFIX)
        and row.get("status") in {1, 4}
        and sender_email.lower()
        in {str(value).lower() for value in (row.get("email_list") or [])}
    ]
    local_day = datetime.now(ZoneInfo("Australia/Melbourne")).date().isoformat()
    live_today_rows = control.analytics(start_date=local_day, end_date=local_day)
    campaign_accounts = {
        str(row.get("id") or ""): {
            str(value).lower() for value in (row.get("email_list") or [])
        }
        for row in live_campaigns
    }
    sender_today_rows = [
        row
        for row in live_today_rows
        if sender_email.lower()
        in campaign_accounts.get(str(row.get("campaign_id") or ""), set())
    ]
    with factory() as session:
        batch = session.get(CampaignBatch, batch_id)
        sender = session.get(SenderAccount, sender_id)
        if batch is None or sender is None:
            raise LookupError("campaign state disappeared before activation")
        sender.new_leads_sent_today = sum(
            int(row.get("new_leads_contacted_count") or 0)
            for row in sender_today_rows
        )
        sender.total_emails_sent_today = sum(
            int(row.get("emails_sent_count") or 0) for row in sender_today_rows
        )
        other_active = int(
            session.scalar(
                select(func.count(CampaignBatch.id)).where(
                    CampaignBatch.sender_account_id == sender.id,
                    CampaignBatch.active_assignment.is_(True),
                    CampaignBatch.id != batch.id,
                )
            )
            or 0
        )
        preflight = evaluate_campaign_activation(
            outreach_paused=is_outreach_paused(session),
            sender=SenderSnapshot(
                external_id=sender.external_id,
                connected=bool(live_account and sender_is_healthy(live_account)),
                provider_status=live_account.get("status") if live_account else None,
                healthy_since=sender.healthy_since,
                active_link_os_campaigns=other_active + len(active_remote_others),
                new_leads_sent_today=sender.new_leads_sent_today,
                total_emails_sent_today=sender.total_emails_sent_today,
                bounce_rate=sender.rolling_bounce_rate,
                unsubscribe_rate=sender.rolling_unsubscribe_rate,
                has_error=not bool(live_account and sender_is_healthy(live_account)),
                bounce_protection_triggered=(
                    sender.bounce_protection_triggered
                    or live_campaign.get("status") == -2
                ),
            ),
            campaign=CampaignUploadSnapshot(
                expected_members=batch.expected_member_count,
                uploaded_members=batch.uploaded_member_count,
                readback_members=batch.readback_member_count,
                built_paused=live_campaign.get("status") not in {1, 4},
                pending_verifications=batch.pending_verification_count,
                tracking_enabled=bool(payload.get("open_tracking") or payload.get("link_tracking")),
                unsubscribe_header_enabled=payload.get("insert_unsubscribe_header") is True,
                stop_on_reply=payload.get("stop_on_reply") is True,
                stop_on_auto_reply=payload.get("stop_on_auto_reply") is True,
                stop_on_company_reply=payload.get("stop_for_company") is True,
                bounce_protection_enabled=payload.get("disable_bounce_protect") is False,
                risky_contacts_enabled=payload.get("allow_risky_contacts") is True,
            ),
        )
        preflight_reasons = list(preflight.reasons)
        full_reconcile = session.get(SystemSetting, "instantly.full_reconcile_completed")
        legacy_pause = session.get(SystemSetting, "instantly.legacy_campaigns_paused")
        if full_reconcile is None or full_reconcile.value.get("complete") is not True:
            preflight_reasons.append("full_instantly_reconciliation_required")
        if legacy_pause is None or legacy_pause.value.get("complete") is not True:
            preflight_reasons.append("legacy_campaign_pause_confirmation_required")
        batch.paused_reason = ",".join(dict.fromkeys(preflight_reasons)) or None
        batch.paused_at = utcnow()
        session.commit()

    if preflight_reasons:
        if live_campaign.get("status") in {1, 4}:
            control.pause_campaign(external_campaign_id)
        return {
            "batch_id": batch_id,
            "external_campaign_id": external_campaign_id,
            "blocked": True,
            "reasons": list(dict.fromkeys(preflight_reasons)),
            "uploaded": len(readback_by_email),
        }

    control.activate(external_campaign_id, blockers=[])
    with factory() as session:
        batch = session.get(CampaignBatch, batch_id)
        if batch is None:
            raise LookupError("campaign batch disappeared after activation")
        batch.status = CampaignBatchStatus.ACTIVE
        batch.active_assignment = True
        batch.activated_at = utcnow()
        batch.paused_at = None
        batch.paused_reason = None
        session.add(
            AuditLog(
                actor_type="worker",
                actor_id=socket.gethostname(),
                action="campaign.activated",
                entity_type="campaign_batch",
                entity_id=batch.id,
                after={"external_campaign_id": external_campaign_id, "members": batch.expected_member_count},
            )
        )
        if batch.launch_id:
            launch = session.get(CampaignLaunch, batch.launch_id)
            if launch:
                launch.status = CampaignLaunchStatus.ACTIVE
                launch.activated_at = launch.activated_at or utcnow()
        session.commit()
    return {
        "batch_id": batch_id,
        "external_campaign_id": external_campaign_id,
        "activated": True,
        "members": len(readback_by_email),
    }


def _email_body(message: dict[str, Any]) -> str:
    body = message.get("body")
    if isinstance(body, dict):
        return str(body.get("text") or body.get("html") or "")
    return str(body or message.get("body_text") or message.get("content") or "")


def _message_at(message: dict[str, Any]) -> datetime:
    raw = str(message.get("timestamp_email") or message.get("timestamp_created") or "")
    if raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return utcnow()


def _lead_last_contact_at(lead: dict[str, Any]) -> datetime | None:
    """Return Instantly's explicit send evidence for a lead, if present."""

    raw = str(lead.get("timestamp_last_contact") or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _lead_delivery_evidence_payload(
    lead: dict[str, Any],
    *,
    managed: bool,
    campaign_name: str = "",
    link_building_scope: bool = False,
) -> dict[str, Any]:
    """Keep delivery indicators while excluding message bodies and addresses."""

    return {
        "status": lead.get("status"),
        "verification_status": lead.get("verification_status"),
        "managed": managed,
        "campaign_name": campaign_name,
        "link_building_scope": link_building_scope,
        "timestamp_last_contact": lead.get("timestamp_last_contact"),
        "timestamp_last_open": lead.get("timestamp_last_open"),
        "timestamp_last_reply": lead.get("timestamp_last_reply"),
        "timestamp_last_click": lead.get("timestamp_last_click"),
        "last_step_timestamp_executed": lead.get("last_step_timestamp_executed"),
        "last_step_from": lead.get("last_step_from"),
        "last_contacted_from": lead.get("last_contacted_from"),
        "email_open_count": lead.get("email_open_count"),
        "email_reply_count": lead.get("email_reply_count"),
        "email_click_count": lead.get("email_click_count"),
    }


def _lead_domain(lead: dict[str, Any], email: str) -> str:
    custom = lead.get("custom_variables") or lead.get("payload") or {}
    if not isinstance(custom, dict):
        custom = {}
    values = (
        custom.get("root_domain"),
        custom.get("website"),
        lead.get("website"),
        lead.get("company_domain"),
    )
    for value in values:
        if value:
            try:
                return normalize_domain(str(value)).registrable_domain
            except ValueError:
                continue
    if "@" in email:
        try:
            domain = normalize_domain(email.rsplit("@", 1)[-1]).registrable_domain
        except ValueError:
            return ""
        if domain not in {"gmail.com", "outlook.com", "hotmail.com", "yahoo.com", "icloud.com", "live.com"}:
            return domain
    return ""


def _fx_to_aud(currency: str) -> tuple[Decimal | None, date | None]:
    code = currency.upper()
    if code == "AUD":
        return Decimal("1"), utcnow().date()
    try:
        response = httpx.get(
            "https://api.frankfurter.dev/v1/latest",
            params={"base": code, "symbols": "AUD"},
            timeout=8,
            headers={"User-Agent": "LinkOS/1.0"},
        )
        response.raise_for_status()
        payload = response.json()
        rate = payload.get("rates", {}).get("AUD")
        rate_date = date.fromisoformat(str(payload.get("date")))
        return (Decimal(str(rate)), rate_date) if rate else (None, None)
    except (httpx.HTTPError, ValueError, TypeError):
        return None, None


def _publisher_for(session: Session, contact: Contact, lead: dict[str, Any], domain: Domain) -> PublisherEntity:
    key = f"email:{contact.normalized_email}"
    publisher = session.scalar(select(PublisherEntity).where(PublisherEntity.normalized_key == key))
    if publisher is None:
        name = str(lead.get("company_name") or domain.normalized_domain)
        publisher = PublisherEntity(
            canonical_name=name[:255],
            normalized_key=key,
            primary_contact_id=contact.id,
            attributes={"source": "instantly_reply"},
        )
        session.add(publisher)
        session.flush()
    relationship = session.scalar(select(PublisherDomain).where(PublisherDomain.domain_id == domain.id))
    if relationship is None:
        session.add(PublisherDomain(publisher_id=publisher.id, domain_id=domain.id, relationship_type="represented", confidence=1.0))
    return publisher


def _authoritative_stage(session: Session, domain: Domain, target: LifecycleStage, *, source: str) -> None:
    if domain.lifecycle_stage == target:
        return
    before = domain.lifecycle_stage
    domain.lifecycle_stage = target
    domain.stage_changed_at = utcnow()
    session.add(
        AuditLog(
            actor_type="worker",
            actor_id=socket.gethostname(),
            action="domain.authoritative_stage",
            entity_type="domain",
            entity_id=domain.id,
            before={"stage": before.value},
            after={"stage": target.value, "source": source},
            reason="Live Instantly delivery/reply state overrides an incomplete local lifecycle",
        )
    )


def _reply_checkpoint(session: Session, campaign_id: str) -> str:
    batch = session.scalar(
        select(CampaignBatch).where(CampaignBatch.external_campaign_id == campaign_id)
    )
    if batch is not None:
        return str(batch.reply_checkpoint or "")
    setting = session.get(SystemSetting, f"instantly.reply_checkpoint.{campaign_id}")
    return str((setting.value if setting else {}).get("timestamp_created") or "")


def _store_reply_checkpoint(session: Session, campaign_id: str, timestamp_created: str) -> None:
    batch = session.scalar(
        select(CampaignBatch).where(CampaignBatch.external_campaign_id == campaign_id)
    )
    if batch is not None:
        batch.reply_checkpoint = timestamp_created
        return
    key = f"instantly.reply_checkpoint.{campaign_id}"
    setting = session.get(SystemSetting, key)
    value = {"timestamp_created": timestamp_created, "updated_at": utcnow().isoformat()}
    if setting is None:
        session.add(SystemSetting(key=key, value=value, updated_by="link-os-worker"))
    else:
        setting.value = value
        setting.version += 1
        setting.updated_by = "link-os-worker"


def _merge_thread_messages(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for group in groups:
        for message in group:
            key = str(message.get("id") or message.get("message_id") or "")
            if key:
                by_id[key] = message
    return sorted(by_id.values(), key=_message_at)


def sync_replies_job() -> dict[str, Any]:
    control = InstantlyControl()
    campaigns = reply_sync_campaigns(control.list_campaigns())
    inserted = 0
    offers_created = 0
    reviewed = 0
    factory = session_factory()

    for campaign in campaigns:
        campaign_id = str(campaign.get("id") or "")
        if not campaign_id:
            continue
        with factory() as session:
            checkpoint = _reply_checkpoint(session, campaign_id)
        messages = control.list_campaign_emails(
            campaign_id,
            min_timestamp_created=checkpoint,
        )
        newest_checkpoint = max(
            (str(message.get("timestamp_created") or "") for message in messages),
            default="",
        )
        leads = {str(lead.get("email") or "").lower(): lead for lead in control.list_campaign_leads(campaign_id)}
        if str(campaign.get("name") or "").startswith(MANAGED_NAME_PREFIX):
            sent_messages = [
                message
                for message in messages
                if message.get("ue_type") == 1
                or str(message.get("direction") or "").lower() == "outbound"
            ]
            for message in sent_messages:
                external_email_id = str(message.get("id") or "")
                if not external_email_id:
                    continue
                with factory() as session:
                    dedupe_key = f"email_sent:{external_email_id}"
                    if session.scalar(
                        select(InstantlyEvent.id).where(
                            InstantlyEvent.dedupe_key == dedupe_key
                        )
                    ) is not None:
                        continue
                    lead_email = str(message.get("lead") or "").lower()
                    lead = leads.get(lead_email, {})
                    contact = None
                    try:
                        normalized_email = normalize_email(lead_email)
                    except ValueError:
                        normalized_email = ""
                    if normalized_email:
                        contact = session.scalar(
                            select(Contact).where(
                                Contact.normalized_email == normalized_email
                            )
                        )
                    domain_name = _lead_domain(lead, lead_email)
                    domain = session.scalar(
                        select(Domain).where(Domain.normalized_domain == domain_name)
                    ) if domain_name else None
                    session.add(
                        InstantlyEvent(
                            dedupe_key=dedupe_key,
                            external_event_id=external_email_id,
                            event_type="email_sent",
                            external_campaign_id=campaign_id,
                            external_lead_id=str(lead.get("id") or "") or None,
                            external_email_id=external_email_id,
                            external_thread_id=str(message.get("thread_id") or "") or None,
                            domain_id=domain.id if domain else None,
                            contact_id=contact.id if contact else None,
                            event_at=_message_at(message),
                            payload=message,
                        )
                    )
                    if domain and domain.lifecycle_stage not in {
                        LifecycleStage.REPLIED,
                        LifecycleStage.OFFER_REVIEW,
                        LifecycleStage.OFFER_APPROVED,
                        LifecycleStage.LISTED,
                        LifecycleStage.LOST,
                    }:
                        _authoritative_stage(
                            session,
                            domain,
                            LifecycleStage.CONTACTED,
                            source="instantly_email_sent",
                        )
                        domain.last_contacted_at = domain.last_contacted_at or _message_at(message)
                    if contact:
                        batch = session.scalar(
                            select(CampaignBatch).where(
                                CampaignBatch.external_campaign_id == campaign_id
                            )
                        )
                        if batch:
                            member = session.scalar(
                                select(CampaignMember).where(
                                    CampaignMember.campaign_batch_id == batch.id,
                                    CampaignMember.contact_id == contact.id,
                                )
                            )
                            if member:
                                member.status = CampaignMemberStatus.CONTACTED
                                member.contacted_at = _message_at(message)
                    session.commit()
        thread_map: dict[str, list[dict[str, Any]]] = {}
        for message in messages:
            thread_map.setdefault(str(message.get("thread_id") or ""), []).append(message)
        inbound = [
            message
            for message in messages
            if message.get("ue_type") == 2 or str(message.get("direction") or "").lower() == "inbound"
        ]
        inbound.sort(key=_message_at)
        for message in inbound:
            external_email_id = str(message.get("id") or "")
            if not external_email_id:
                continue
            with factory() as session:
                if session.scalar(select(Reply.id).where(Reply.external_email_id == external_email_id)) is not None:
                    continue
                lead_email = str(message.get("lead") or message.get("from_address_email") or "").lower()
                lead = leads.get(lead_email, {})
                contact: Contact | None = None
                if lead_email:
                    try:
                        normalized_email = normalize_email(lead_email)
                    except ValueError:
                        normalized_email = ""
                    if normalized_email:
                        contact = session.scalar(select(Contact).where(Contact.normalized_email == normalized_email))
                        if contact is None:
                            contact = Contact(
                                normalized_email=normalized_email,
                                original_email=lead_email,
                                email_domain=normalized_email.rsplit("@", 1)[-1],
                                role_class=_role_class(normalized_email),
                                is_guessed=False,
                                attributes={"source": "instantly_reply"},
                            )
                            session.add(contact)
                            session.flush()
                domain_name = _lead_domain(lead, lead_email)
                domain: Domain | None = None
                if domain_name:
                    domain, _created = DomainRepository(session).get_or_create(domain_name, source="instantly_reply")
                    _authoritative_stage(session, domain, LifecycleStage.REPLIED, source="instantly")

                thread_id = str(message.get("thread_id") or "")
                evidence_messages = thread_map.get(thread_id, [message])
                if checkpoint and thread_id:
                    prior_reply = session.scalar(
                        select(Reply)
                        .where(Reply.external_thread_id == thread_id)
                        .order_by(Reply.received_at.desc())
                        .limit(1)
                    )
                    prior_messages = list(
                        ((prior_reply.thread_evidence or {}).get("messages") or [])
                    ) if prior_reply else []
                    if prior_messages:
                        evidence_messages = _merge_thread_messages(
                            prior_messages,
                            evidence_messages,
                        )
                    else:
                        evidence_messages = control.list_thread_emails(thread_id)
                event = InstantlyEvent(
                    dedupe_key=f"email:{external_email_id}",
                    external_event_id=external_email_id,
                    event_type="reply_received",
                    external_campaign_id=campaign_id,
                    external_lead_id=str(lead.get("id") or "") or None,
                    external_email_id=external_email_id,
                    external_thread_id=thread_id or None,
                    domain_id=domain.id if domain else None,
                    contact_id=contact.id if contact else None,
                    event_at=_message_at(message),
                    payload=message,
                )
                session.add(event)
                session.flush()
                body = _email_body(message)
                attachment_payload = message.get("attachment_json") or {}
                attachments = list(
                    message.get("attachments")
                    or (attachment_payload.get("files") if isinstance(attachment_payload, dict) else [])
                    or []
                )
                reply = Reply(
                    external_email_id=external_email_id,
                    external_thread_id=thread_id or None,
                    instantly_event_id=event.id,
                    domain_id=domain.id if domain else None,
                    contact_id=contact.id if contact else None,
                    from_address=str(message.get("from_address_email") or lead_email) or None,
                    to_address=str(message.get("to_address_email_list") or "") or None,
                    subject=str(message.get("subject") or "") or None,
                    body_text=body,
                    received_at=_message_at(message),
                    attachment_only=bool(attachments and not body.strip()),
                    thread_evidence={"campaign": {"id": campaign_id, "name": campaign.get("name")}, "messages": evidence_messages, "attachments": attachments},
                )
                session.add(reply)
                session.flush()
                inserted += 1

                if domain is not None and contact is not None:
                    decision = evaluate_offer(body, expected_domain=domain.normalized_domain, attachments=attachments)
                    if decision.amount is not None and decision.currency and decision.placement_type:
                        fx_rate, fx_date = _fx_to_aud(decision.currency)
                        auto_approved = decision.auto_approved and fx_rate is not None and fx_date is not None
                        pricing = (
                            calculate_reseller_price(
                                amount=decision.amount,
                                currency=decision.currency,
                                fx_rate_to_aud=fx_rate,
                                fx_rate_date=fx_date,
                            )
                            if fx_rate is not None and fx_date is not None
                            else None
                        )
                        publisher = _publisher_for(session, contact, lead, domain)
                        offer = Offer(
                            reply_id=reply.id,
                            domain_id=domain.id,
                            publisher_id=publisher.id,
                            status=OfferStatus.APPROVED if auto_approved else OfferStatus.REVIEW,
                            placement_type=decision.placement_type,
                            original_amount=decision.amount,
                            original_currency=decision.currency,
                            extraction_confidence=1.0 if decision.auto_approved else 0.5,
                            attachment_only="attachment_only_or_attachment_dependent" in decision.review_reasons,
                            redirect_ambiguity="redirected_contact" in decision.review_reasons,
                            network_ambiguity="agency_or_network_ambiguity" in decision.review_reasons,
                            conflicting_prices="multiple_prices" in decision.review_reasons,
                            cost_aud=pricing.cost_aud if pricing else None,
                            fx_rate_to_aud=pricing.fx_rate_to_aud if pricing else None,
                            fx_rate_date=pricing.fx_rate_date if pricing else None,
                            reseller_price_aud=pricing.reseller_price_aud if pricing else None,
                            pricing_rule_version=pricing.rule_version if pricing else None,
                            extracted_terms={"review_reasons": list(decision.review_reasons), "source": "strict_reply_parser"},
                            approved_at=utcnow() if auto_approved else None,
                            approved_by="link-os:auto" if auto_approved else None,
                        )
                        session.add(offer)
                        offers_created += 1
                        if auto_approved:
                            _authoritative_stage(session, domain, LifecycleStage.OFFER_APPROVED, source="strict_offer_parser")
                        else:
                            reviewed += 1
                            _authoritative_stage(session, domain, LifecycleStage.OFFER_REVIEW, source="strict_offer_parser")
                        JobQueue(session).enqueue(
                            kind="reconcile_monday",
                            idempotency_key=f"reconcile_monday:reply_batch:{utcnow().strftime('%Y%m%d%H%M')}",
                            payload={"reply_id": reply.id},
                            priority=10,
                            max_attempts=5,
                        )
                reply.processed_at = utcnow()
                session.commit()
        if newest_checkpoint:
            with factory() as session:
                _store_reply_checkpoint(session, campaign_id, newest_checkpoint)
                session.commit()
    return {"campaigns": len(campaigns), "replies_inserted": inserted, "offers_created": offers_created, "offers_for_review": reviewed}


def reconcile_monday_job() -> dict[str, Any]:
    if not os.getenv("MONDAY_API_KEY") and not load_env_value("MONDAY_API_KEY"):
        raise RuntimeError("MONDAY_API_KEY is not configured; projection remains retryable")
    database_engine = platform_engine()
    if database_engine.dialect.name != "postgresql":
        with platform_session() as session:
            return DealsOnlyProjector().reconcile(session, execute=True)
    with database_engine.connect() as lock_connection:
        acquired = bool(
            lock_connection.scalar(
                text("SELECT pg_try_advisory_lock(hashtext(:lock_key))"),
                {"lock_key": "link-os:monday-reconcile"},
            )
        )
        if not acquired:
            raise RuntimeError("Another Monday reconciliation holds the single-flight lock")
        try:
            with platform_session() as session:
                return DealsOnlyProjector().reconcile(session, execute=True)
        finally:
            lock_connection.execute(
                text("SELECT pg_advisory_unlock(hashtext(:lock_key))"),
                {"lock_key": "link-os:monday-reconcile"},
            )


class LinkOSWorker:
    def __init__(self, config: WorkerConfig | None = None, *, worker_id: str | None = None) -> None:
        self.config = config or WorkerConfig.from_env()
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self.handlers = {
            "process_import": lambda job_id: process_import_job(job_id, self.worker_id, self.config),
            "scrape_domain": lambda job_id: scrape_domain_job(job_id, self.config),
            "verify_contact": verify_contact_job,
            "prepare_campaign_batch": prepare_campaign_batch_job,
            "reconcile_instantly": lambda reconcile_job_id: reconcile_instantly_job(reconcile_job_id),
            "sync_replies": lambda _job_id: sync_replies_job(),
            "reconcile_monday": lambda _job_id: reconcile_monday_job(),
            "backlink_analysis": lambda backlink_job_id: run_analysis_job(backlink_job_id),
        }

    def schedule(self) -> None:
        now = utcnow()
        with platform_session() as session:
            queue = JobQueue(session)
            ten_minute = now.replace(minute=(now.minute // 10) * 10, second=0, microsecond=0)
            fifteen_minute = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
            five_minute = now.replace(minute=(now.minute // 5) * 5, second=0, microsecond=0)
            queue.enqueue(
                kind="sync_replies",
                idempotency_key=f"sync_replies:{ten_minute.isoformat()}",
                payload={"scheduled_at": ten_minute.isoformat()},
                priority=15,
            )
            queue.enqueue(
                kind="reconcile_instantly",
                idempotency_key=f"reconcile_instantly:{fifteen_minute.isoformat()}",
                payload={"scheduled_at": fifteen_minute.isoformat()},
                priority=10,
            )
            full_reconcile = session.get(SystemSetting, "instantly.full_reconcile_completed")
            next_campaign = _next_campaign_request(session, now=now)
            if (
                os.getenv("LINK_OS_MODE", "shadow").strip().lower() == "live"
                and not is_outreach_paused(session)
                and full_reconcile is not None
                and full_reconcile.value.get("complete") is True
                and next_campaign is not None
            ):
                queue.enqueue(
                    kind="prepare_campaign_batch",
                    idempotency_key=f"prepare_campaign_batch:{five_minute.isoformat()}",
                    payload=next_campaign,
                    priority=25,
                )
            if now.hour == 3:
                queue.enqueue(
                    kind="reconcile_instantly",
                    idempotency_key=f"reconcile_instantly:daily:{now.date().isoformat()}",
                    payload={"full": True},
                    priority=20,
                )
                queue.enqueue(
                    kind="reconcile_monday",
                    idempotency_key=f"reconcile_monday:daily:{now.date().isoformat()}",
                    payload={"scheduled_at": now.isoformat(), "source": "daily_recovery"},
                    priority=15,
                    max_attempts=5,
                )

    def _run_job(self, job_id: str) -> tuple[str, bool, dict[str, Any] | str]:
        factory = session_factory()
        with factory() as session:
            job = session.get(Job, job_id)
            if job is None:
                return job_id, False, "job disappeared"
            kind = job.kind
        handler = self.handlers.get(kind)
        if handler is None:
            error = f"unsupported job kind: {kind}"
            with factory() as session:
                JobQueue(session).fail(
                    job_id,
                    worker_id=self.worker_id,
                    error=error,
                    retryable=not isinstance(exc, NonRetryableJobError),
                )
                session.commit()
            return job_id, False, error
        try:
            result = handler(job_id)
            with factory() as session:
                JobQueue(session).complete(job_id, worker_id=self.worker_id)
                session.commit()
            return job_id, True, result
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"[:10000]
            with factory() as session:
                try:
                    JobQueue(session).fail(job_id, worker_id=self.worker_id, error=error)
                    session.commit()
                except Exception:
                    session.rollback()
            return job_id, False, str(exc)

    def run_once(self) -> dict[str, Any]:
        with platform_session() as session:
            run = WorkerRun(worker_name=self.worker_id, run_type="batch", status=WorkerRunStatus.RUNNING, lease_owner=self.worker_id)
            session.add(run)
            session.flush()
            run_id = run.id
        leased_total = 0
        results: list[tuple[str, bool, dict[str, Any] | str]] = []

        def lease_jobs(limit: int) -> list[str]:
            if limit <= 0:
                return []
            with platform_session() as session:
                jobs = JobQueue(session).lease(
                    worker_id=self.worker_id,
                    limit=limit,
                    lease_for=timedelta(minutes=self.config.lease_minutes),
                )
                return [job.id for job in jobs]

        def heartbeat_jobs(job_ids: list[str]) -> None:
            if not job_ids:
                return
            with platform_session() as session:
                queue = JobQueue(session)
                for job_id in job_ids:
                    try:
                        queue.heartbeat(
                            job_id,
                            worker_id=self.worker_id,
                            lease_for=timedelta(minutes=self.config.lease_minutes),
                        )
                    except JobLeaseError:
                        # A future can finish between the caller's ``done``
                        # check and this transaction. Its completion clears
                        # the lease, so there is nothing left to heartbeat.
                        continue

        with ThreadPoolExecutor(max_workers=self.config.concurrency) as executor:
            futures: dict[Future[tuple[str, bool, dict[str, Any] | str]], str] = {}
            heartbeat_interval = max(30.0, self.config.lease_minutes * 60 / 3)
            while True:
                heartbeat_jobs([job_id for future, job_id in futures.items() if not future.done()])
                available_slots = self.config.concurrency - len(futures)
                job_ids = lease_jobs(available_slots)
                leased_total += len(job_ids)
                for job_id in job_ids:
                    futures[executor.submit(self._run_job, job_id)] = job_id

                if not futures:
                    break

                completed, _pending = wait(
                    futures,
                    timeout=heartbeat_interval,
                    return_when=FIRST_COMPLETED,
                )
                for future in completed:
                    futures.pop(future, None)
                    results.append(future.result())

                with platform_session() as session:
                    run = session.get(WorkerRun, run_id)
                    if run:
                        failures = sum(1 for _job_id, ok, _result in results if not ok)
                        run.heartbeat_at = utcnow()
                        run.counters = {
                            "leased": leased_total,
                            "succeeded": len(results) - failures,
                            "failed": failures,
                            "active": len(futures),
                        }
        with platform_session() as session:
            run = session.get(WorkerRun, run_id)
            if run:
                failures = sum(1 for _job_id, ok, _result in results if not ok)
                run.status = WorkerRunStatus.FAILED if failures else WorkerRunStatus.SUCCEEDED
                run.completed_at = utcnow()
                run.heartbeat_at = utcnow()
                run.counters = {"leased": leased_total, "succeeded": len(results) - failures, "failed": failures, "active": 0}
        return {"leased": leased_total, "results": results}

    def run_forever(self) -> None:
        while True:
            try:
                self.schedule()
                result = self.run_once()
            except Exception:
                traceback.print_exc()
                result = {"leased": 0}
            if not result.get("leased"):
                time.sleep(self.config.idle_seconds)


def main() -> None:
    LinkOSWorker().run_forever()


if __name__ == "__main__":
    main()
