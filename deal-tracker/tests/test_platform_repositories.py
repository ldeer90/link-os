from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from deal_tracker.platform.enums import (
    JobStatus,
    LifecycleStage,
    VerificationStatus,
)
from deal_tracker.platform.models import (
    AuditLog,
    Base,
    CampaignMember,
    Contact,
    DomainContactEvidence,
    VerificationResult,
)
from deal_tracker.platform.repositories import (
    CampaignRepository,
    CampaignReservationError,
    DomainRepository,
    JobLeaseError,
    JobQueue,
    is_outreach_paused,
    set_outreach_paused,
)


NOW = datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc)


@pytest.fixture()
def session() -> Session:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db_session:
        yield db_session
        db_session.rollback()
    engine.dispose()


def test_schema_contains_all_canonical_control_plane_tables(session: Session) -> None:
    expected = {
        "imports",
        "import_items",
        "domains",
        "domain_events",
        "scrape_attempts",
        "contacts",
        "domain_contact_evidence",
        "suppressions",
        "verification_results",
        "jobs",
        "sender_accounts",
        "campaign_batches",
        "campaign_members",
        "instantly_events",
        "replies",
        "offers",
        "publisher_entities",
        "publisher_domains",
        "catalogue_listings",
        "monday_mappings",
        "worker_runs",
        "audit_logs",
        "system_settings",
    }
    assert expected <= set(Base.metadata.tables)


def test_domain_upsert_is_registrable_domain_idempotent(session: Session) -> None:
    repository = DomainRepository(session)
    first, created = repository.get_or_create(
        "https://news.example.com.au/contact", source="test", now=NOW
    )
    duplicate, duplicate_created = repository.get_or_create(
        "www.example.com.au", source="test", now=NOW
    )
    assert created
    assert not duplicate_created
    assert first.id == duplicate.id
    assert first.normalized_domain == "example.com.au"


def test_job_queue_is_idempotent_and_releases_expired_worker_lease(
    session: Session,
) -> None:
    queue = JobQueue(session)
    job, created = queue.enqueue(
        kind="scrape_domain",
        subject_type="domain",
        subject_id="domain-1",
        idempotency_key="scrape:domain-1:v1",
        available_at=NOW,
        max_attempts=3,
    )
    duplicate, duplicate_created = queue.enqueue(
        kind="scrape_domain",
        idempotency_key="scrape:domain-1:v1",
        available_at=NOW,
    )
    assert created
    assert not duplicate_created
    assert duplicate.id == job.id

    leased = queue.lease(worker_id="worker-a", now=NOW, limit=1)
    assert [item.id for item in leased] == [job.id]
    assert job.attempts == 1
    with pytest.raises(JobLeaseError):
        queue.complete(job.id, worker_id="worker-b", now=NOW)

    recovered = queue.lease(
        worker_id="worker-b", now=NOW + timedelta(minutes=6), limit=1
    )
    assert [item.id for item in recovered] == [job.id]
    assert job.attempts == 2
    queue.complete(job.id, worker_id="worker-b", now=NOW + timedelta(minutes=6))
    assert job.status == JobStatus.SUCCEEDED


def test_job_failure_retries_then_moves_to_dead_letter(session: Session) -> None:
    queue = JobQueue(session)
    job, _ = queue.enqueue(
        kind="sync",
        idempotency_key="sync:1",
        available_at=NOW,
        max_attempts=2,
    )
    queue.lease(worker_id="worker-a", now=NOW)
    queue.fail(
        job.id,
        worker_id="worker-a",
        error="temporary",
        now=NOW,
        base_backoff=timedelta(seconds=1),
    )
    assert job.status == JobStatus.QUEUED
    queue.lease(worker_id="worker-a", now=NOW + timedelta(seconds=2))
    queue.fail(job.id, worker_id="worker-a", error="permanent", now=NOW)
    assert job.status == JobStatus.DEAD


def test_nonretryable_job_failure_moves_directly_to_dead_letter(session: Session) -> None:
    queue = JobQueue(session)
    job, _ = queue.enqueue(
        kind="verify_contact",
        idempotency_key="verify:no-credits",
        available_at=NOW,
        max_attempts=3,
    )
    queue.lease(worker_id="worker-a", now=NOW)
    queue.fail(
        job.id,
        worker_id="worker-a",
        error="verification credits unavailable",
        retryable=False,
        now=NOW,
    )
    assert job.status == JobStatus.DEAD
    assert job.attempts == 1


def test_expired_final_worker_lease_moves_to_dead_letter(session: Session) -> None:
    queue = JobQueue(session)
    job, _ = queue.enqueue(
        kind="scrape",
        idempotency_key="scrape:crash-on-final-attempt",
        available_at=NOW,
        max_attempts=1,
    )
    queue.lease(worker_id="worker-a", now=NOW)
    assert queue.lease(worker_id="worker-b", now=NOW + timedelta(minutes=6)) == []
    assert job.status == JobStatus.DEAD
    assert job.last_error == "worker lease expired after final attempt"


def test_global_outreach_pause_defaults_closed_and_changes_are_audited(
    session: Session,
) -> None:
    assert is_outreach_paused(session)
    setting = set_outreach_paused(
        session,
        paused=False,
        actor="admin@example.test",
        reason="verified local shadow-mode test",
        now=NOW,
    )
    assert setting.value["paused"] is False
    assert not is_outreach_paused(session)
    assert session.scalar(select(func.count(AuditLog.id))) == 1


def test_campaign_reservation_requires_public_evidence_and_instantly_verified(
    session: Session,
) -> None:
    domains = DomainRepository(session)
    domain, _ = domains.get_or_create("example.com", source="test", now=NOW)
    domain.lifecycle_stage = LifecycleStage.OUTREACH_READY
    contact = Contact(
        normalized_email="editor@example.com",
        original_email="editor@example.com",
        email_domain="example.com",
        is_guessed=False,
    )
    session.add(contact)
    session.flush()
    session.add(
        DomainContactEvidence(
            domain_id=domain.id,
            contact_id=contact.id,
            evidence_key="evidence-1",
            source_url="https://example.com/contact",
            context_snippet="Email our editor",
            is_public_page=True,
            confidence=0.99,
        )
    )
    session.add(
        VerificationResult(
            contact_id=contact.id,
            provider="Instantly",
            provider_request_id="verify-1",
            status=VerificationStatus.VERIFIED,
            catch_all=False,
            risky=False,
            checked_at=NOW,
        )
    )
    session.flush()

    repository = CampaignRepository(session)
    batch, created = repository.reserve_batch(
        deterministic_key="link-os:2026-07-15:001",
        name="LINK OS Pilot 001",
        config_version="verified-relaunch-v2",
        members=[(domain.id, contact.id)],
        now=NOW,
    )
    assert created
    assert domain.lifecycle_stage == LifecycleStage.CAMPAIGN_QUEUED
    membership = session.scalar(
        select(CampaignMember).where(CampaignMember.campaign_batch_id == batch.id)
    )
    assert membership is not None and membership.reservation_active

    duplicate, duplicate_created = repository.reserve_batch(
        deterministic_key="link-os:2026-07-15:001",
        name="LINK OS Pilot 001",
        config_version="verified-relaunch-v2",
        members=[(domain.id, contact.id)],
        now=NOW,
    )
    assert not duplicate_created
    assert duplicate.id == batch.id


def test_campaign_reservation_rejects_unevidenced_contact(session: Session) -> None:
    domains = DomainRepository(session)
    domain, _ = domains.get_or_create("example.org", source="test", now=NOW)
    domain.lifecycle_stage = LifecycleStage.OUTREACH_READY
    contact = Contact(
        normalized_email="editor@example.org",
        original_email="editor@example.org",
        email_domain="example.org",
        is_guessed=False,
    )
    session.add(contact)
    session.flush()
    with pytest.raises(CampaignReservationError, match="missing_public_evidence"):
        CampaignRepository(session).reserve_batch(
            deterministic_key="link-os:bad",
            name="Unsafe batch",
            config_version="verified-relaunch-v2",
            members=[(domain.id, contact.id)],
            now=NOW,
        )


def test_public_evidence_policy_skips_verification_but_requires_strong_evidence(
    session: Session,
) -> None:
    domains = DomainRepository(session)
    domain, _ = domains.get_or_create("public.example.com", source="test", now=NOW)
    domain.lifecycle_stage = LifecycleStage.EMAIL_FOUND
    contact = Contact(
        normalized_email="editor@public.example.com",
        original_email="editor@public.example.com",
        email_domain="public.example.com",
        is_guessed=False,
    )
    session.add(contact)
    session.flush()
    evidence = DomainContactEvidence(
        domain_id=domain.id,
        contact_id=contact.id,
        evidence_key="public-policy-evidence",
        source_url="https://public.example.com/contact",
        context_snippet="Email our editorial team",
        is_public_page=True,
        confidence=0.74,
    )
    session.add(evidence)
    session.flush()

    repository = CampaignRepository(session)
    assert "insufficient_public_evidence" in repository.eligibility_reasons(
        domain.id,
        contact.id,
        contact_policy="public_evidence_verification_skipped",
        now=NOW,
    )
    evidence.confidence = 0.95
    session.flush()
    batch, created = repository.reserve_batch(
        deterministic_key="public-evidence-pilot:test",
        name="LINK OS | Public Evidence Pilot",
        config_version="public-evidence-pilot-v1.0",
        members=[(domain.id, contact.id)],
        contact_policy="public_evidence_verification_skipped",
        now=NOW,
    )
    assert created
    assert batch.settings_snapshot["contact_policy"] == "public_evidence_verification_skipped"
    assert domain.lifecycle_stage == LifecycleStage.CAMPAIGN_QUEUED
