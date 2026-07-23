"""Canonical SQLAlchemy models for LINK OS.

PostgreSQL is the operational source of truth. The models intentionally use
portable SQLAlchemy types so unit and repository tests can run on SQLite.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum as SqlEnum,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .enums import (
    BacklinkAnalysisMode,
    BacklinkAnalysisStatus,
    BacklinkCandidateStatus,
    CampaignBatchStatus,
    CampaignLaunchStatus,
    CampaignMemberStatus,
    ImportItemStatus,
    ImportStatus,
    JobStatus,
    LifecycleStage,
    ListingStatus,
    OfferStatus,
    ScrapeStatus,
    VerificationStatus,
    WorkerRunStatus,
)


NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid.uuid4())


def enum_column(enum_class: type, name: str) -> SqlEnum:
    return SqlEnum(
        enum_class,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        values_callable=lambda cls: [entry.value for entry in cls],
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class ImportBatch(TimestampMixin, Base):
    __tablename__ = "imports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_type: Mapped[str] = mapped_column(String(40), nullable=False)
    filename: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[ImportStatus] = mapped_column(
        enum_column(ImportStatus, "import_status"),
        default=ImportStatus.RECEIVED,
        nullable=False,
        index=True,
    )
    total_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    processed_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    accepted_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    invalid_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    suppressed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    previously_contacted_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    refresh_eligible_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    source_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, default=dict, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class Domain(TimestampMixin, Base):
    __tablename__ = "domains"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    normalized_domain: Mapped[str] = mapped_column(
        String(253), unique=True, nullable=False, index=True
    )
    first_input_value: Mapped[str | None] = mapped_column(String(2048))
    tld: Mapped[str] = mapped_column(String(63), nullable=False, index=True)
    country_code: Mapped[str | None] = mapped_column(String(2), index=True)
    lifecycle_stage: Mapped[LifecycleStage] = mapped_column(
        enum_column(LifecycleStage, "domain_lifecycle_stage"),
        default=LifecycleStage.IMPORTED,
        nullable=False,
        index=True,
    )
    stage_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    last_scraped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_contacted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    next_refresh_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class Contact(TimestampMixin, Base):
    __tablename__ = "contacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    normalized_email: Mapped[str] = mapped_column(
        String(320), unique=True, nullable=False, index=True
    )
    original_email: Mapped[str] = mapped_column(String(320), nullable=False)
    email_domain: Mapped[str] = mapped_column(String(253), nullable=False, index=True)
    role_class: Mapped[str | None] = mapped_column(String(50), index=True)
    display_name: Mapped[str | None] = mapped_column(String(255))
    is_guessed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class ImportItem(Base):
    __tablename__ = "import_items"
    __table_args__ = (
        UniqueConstraint("import_id", "row_number", name="uq_import_items_import_row"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    import_id: Mapped[str] = mapped_column(
        ForeignKey("imports.id", ondelete="CASCADE"), nullable=False, index=True
    )
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_value: Mapped[str | None] = mapped_column(Text)
    normalized_value: Mapped[str | None] = mapped_column(String(320), index=True)
    status: Mapped[ImportItemStatus] = mapped_column(
        enum_column(ImportItemStatus, "import_item_status"), nullable=False, index=True
    )
    domain_id: Mapped[str | None] = mapped_column(
        ForeignKey("domains.id", ondelete="SET NULL"), index=True
    )
    contact_id: Mapped[str | None] = mapped_column(
        ForeignKey("contacts.id", ondelete="SET NULL"), index=True
    )
    explanation: Mapped[str | None] = mapped_column(Text)
    row_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, default=dict, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class DomainEvent(Base):
    __tablename__ = "domain_events"
    __table_args__ = (
        Index("ix_domain_events_domain_created", "domain_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    domain_id: Mapped[str] = mapped_column(
        ForeignKey("domains.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    from_stage: Mapped[LifecycleStage | None] = mapped_column(
        enum_column(LifecycleStage, "domain_event_from_stage")
    )
    to_stage: Mapped[LifecycleStage | None] = mapped_column(
        enum_column(LifecycleStage, "domain_event_to_stage")
    )
    reason: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class Job(TimestampMixin, Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint("max_attempts > 0", name="positive_max_attempts"),
        CheckConstraint("attempts >= 0", name="nonnegative_attempts"),
        Index("ix_jobs_due", "status", "available_at", "priority"),
        Index("ix_jobs_lease", "status", "leased_until"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    subject_type: Mapped[str | None] = mapped_column(String(80), index=True)
    subject_id: Mapped[str | None] = mapped_column(String(128), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        enum_column(JobStatus, "job_status"),
        default=JobStatus.QUEUED,
        nullable=False,
        index=True,
    )
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    lease_owner: Mapped[str | None] = mapped_column(String(255), index=True)
    leased_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False, index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BacklinkAnalysisRun(TimestampMixin, Base):
    __tablename__ = "backlink_analysis_runs"
    __table_args__ = (
        CheckConstraint("credit_cap > 0 AND credit_cap <= 2500", name="valid_credit_cap"),
        CheckConstraint("authority_floor >= 0 AND authority_floor <= 100", name="valid_authority_floor"),
        Index("ix_backlink_analysis_runs_status_created", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    mode: Mapped[BacklinkAnalysisMode] = mapped_column(
        enum_column(BacklinkAnalysisMode, "backlink_analysis_mode"), nullable=False, index=True
    )
    status: Mapped[BacklinkAnalysisStatus] = mapped_column(
        enum_column(BacklinkAnalysisStatus, "backlink_analysis_status"),
        default=BacklinkAnalysisStatus.DRAFT,
        nullable=False,
        index=True,
    )
    client_domain: Mapped[str] = mapped_column(String(253), nullable=False, index=True)
    competitor_domains: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    source_url_filter: Mapped[str | None] = mapped_column(String(255))
    requested_by: Mapped[str] = mapped_column(String(255), nullable=False)
    request_source: Mapped[str] = mapped_column(String(80), default="console", nullable=False)
    credit_cap: Mapped[int] = mapped_column(Integer, default=500, nullable=False)
    confirmed_credit_cap: Mapped[int | None] = mapped_column(Integer)
    authority_floor: Mapped[int] = mapped_column(Integer, default=20, nullable=False)
    authority_ceiling: Mapped[int | None] = mapped_column(Integer)
    monthly_credit_cap: Mapped[int] = mapped_column(Integer, default=10000, nullable=False)
    estimated_credits: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    actual_credits: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    balance_before: Mapped[int | None] = mapped_column(Integer)
    balance_after: Mapped[int | None] = mapped_column(Integer)
    counters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(80), index=True)
    error_detail: Mapped[str | None] = mapped_column(Text)
    import_id: Mapped[str | None] = mapped_column(
        ForeignKey("imports.id", ondelete="SET NULL"), index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BacklinkAnalysisTarget(Base):
    __tablename__ = "backlink_analysis_targets"
    __table_args__ = (
        UniqueConstraint("run_id", "target_domain", name="uq_backlink_target_run_domain"),
        Index("ix_backlink_targets_run_status", "run_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("backlink_analysis_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_domain: Mapped[str] = mapped_column(String(253), nullable=False, index=True)
    target_role: Mapped[str] = mapped_column(String(40), nullable=False)
    allocated_credits: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="queued", nullable=False, index=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    returned_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    actual_credits: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    referring_domain_count: Mapped[int | None] = mapped_column(Integer)
    count_actual_credits: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    count_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checkpoint_date: Mapped[date | None] = mapped_column(Date)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_detail: Mapped[str | None] = mapped_column(Text)


class BacklinkProviderRequest(Base):
    __tablename__ = "backlink_provider_requests"
    __table_args__ = (
        Index("ix_backlink_provider_request_hash_completed", "request_hash", "completed_at"),
        Index("ix_backlink_provider_run_status", "run_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("backlink_analysis_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_id: Mapped[str] = mapped_column(
        ForeignKey("backlink_analysis_targets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(40), default="seranking", nullable=False)
    endpoint: Mapped[str] = mapped_column(String(160), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    request_parameters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="started", nullable=False, index=True)
    predicted_credits: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    actual_credits: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    balance_before: Mapped[int | None] = mapped_column(Integer)
    balance_after: Mapped[int | None] = mapped_column(Integer)
    response_digest: Mapped[str | None] = mapped_column(String(64))
    attempt_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_detail: Mapped[str | None] = mapped_column(Text)


class BacklinkCreditUsage(Base):
    __tablename__ = "backlink_credit_usage"
    __table_args__ = (Index("ix_backlink_credit_usage_provider_created", "provider", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("backlink_analysis_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider_request_id: Mapped[str | None] = mapped_column(
        ForeignKey("backlink_provider_requests.id", ondelete="SET NULL"), index=True
    )
    provider: Mapped[str] = mapped_column(String(40), default="seranking", nullable=False)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    predicted_credits: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    actual_credits: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    balance_before: Mapped[int | None] = mapped_column(Integer)
    balance_after: Mapped[int | None] = mapped_column(Integer)
    observed_delta: Mapped[int | None] = mapped_column(Integer)
    mismatch: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class BacklinkEvidence(Base):
    __tablename__ = "backlink_evidence"
    __table_args__ = (
        UniqueConstraint("run_id", "evidence_key", name="uq_backlink_evidence_run_key"),
        Index("ix_backlink_evidence_run_domain", "run_id", "source_domain"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("backlink_analysis_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_id: Mapped[str] = mapped_column(
        ForeignKey("backlink_analysis_targets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_domain: Mapped[str] = mapped_column(String(253), nullable=False, index=True)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    target_url: Mapped[str | None] = mapped_column(Text)
    page_title: Mapped[str | None] = mapped_column(Text)
    anchor_text: Mapped[str | None] = mapped_column(Text)
    nofollow: Mapped[bool | None] = mapped_column(Boolean)
    image_link: Mapped[bool | None] = mapped_column(Boolean)
    inlink_rank: Mapped[int | None] = mapped_column(Integer)
    domain_inlink_rank: Mapped[int | None] = mapped_column(Integer, index=True)
    first_seen: Mapped[date | None] = mapped_column(Date)
    last_visited: Mapped[date | None] = mapped_column(Date)
    provider_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    evidence_key: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class BacklinkCandidate(TimestampMixin, Base):
    __tablename__ = "backlink_candidates"
    __table_args__ = (
        UniqueConstraint("run_id", "normalized_domain", name="uq_backlink_candidate_run_domain"),
        Index("ix_backlink_candidates_run_status", "run_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("backlink_analysis_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    normalized_domain: Mapped[str] = mapped_column(String(253), nullable=False, index=True)
    existing_domain_id: Mapped[str | None] = mapped_column(
        ForeignKey("domains.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[BacklinkCandidateStatus] = mapped_column(
        enum_column(BacklinkCandidateStatus, "backlink_candidate_status"),
        nullable=False,
        index=True,
    )
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    highest_domain_inlink_rank: Mapped[int | None] = mapped_column(Integer)
    has_dofollow: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    local_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    commercial_anchor_class: Mapped[str] = mapped_column(
        String(32), default="unknown", nullable=False, index=True
    )
    commercial_anchor_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    commercial_anchor_signals: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    codex_confidence: Mapped[float | None] = mapped_column(Float)
    reason_codes: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    decision_summary: Mapped[str | None] = mapped_column(Text)
    decided_by: Mapped[str | None] = mapped_column(String(255))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    import_id: Mapped[str | None] = mapped_column(
        ForeignKey("imports.id", ondelete="SET NULL"), index=True
    )


class BacklinkReviewDecision(Base):
    __tablename__ = "backlink_review_decisions"
    __table_args__ = (Index("ix_backlink_review_candidate_created", "candidate_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("backlink_analysis_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("backlink_candidates.id", ondelete="CASCADE"), nullable=False, index=True
    )
    prior_status: Mapped[str] = mapped_column(String(40), nullable=False)
    decision: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    reason_codes: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    actor_type: Mapped[str] = mapped_column(String(40), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ScrapeAttempt(Base):
    __tablename__ = "scrape_attempts"
    __table_args__ = (
        UniqueConstraint(
            "domain_id", "attempt_number", name="uq_scrape_attempts_domain_number"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    domain_id: Mapped[str] = mapped_column(
        ForeignKey("domains.id", ondelete="CASCADE"), nullable=False, index=True
    )
    job_id: Mapped[str | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), index=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[ScrapeStatus] = mapped_column(
        enum_column(ScrapeStatus, "scrape_status"), nullable=False, index=True
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pages_requested: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pages_crawled: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    robots_allowed: Mapped[bool | None] = mapped_column(Boolean)
    emails_found: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_detail: Mapped[str | None] = mapped_column(Text)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class DomainContactEvidence(Base):
    __tablename__ = "domain_contact_evidence"
    __table_args__ = (
        UniqueConstraint("evidence_key", name="uq_domain_contact_evidence_key"),
        Index(
            "ix_domain_contact_evidence_domain_contact",
            "domain_id",
            "contact_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    domain_id: Mapped[str] = mapped_column(
        ForeignKey("domains.id", ondelete="CASCADE"), nullable=False
    )
    contact_id: Mapped[str] = mapped_column(
        ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    evidence_key: Mapped[str] = mapped_column(String(64), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    page_title: Mapped[str | None] = mapped_column(Text)
    context_snippet: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    is_public_page: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class Suppression(Base):
    __tablename__ = "suppressions"
    __table_args__ = (
        CheckConstraint(
            "domain_id IS NOT NULL OR contact_id IS NOT NULL OR publisher_id IS NOT NULL",
            name="has_target",
        ),
        Index("ix_suppressions_domain_active", "domain_id", "active"),
        Index("ix_suppressions_contact_active", "contact_id", "active"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    domain_id: Mapped[str | None] = mapped_column(
        ForeignKey("domains.id", ondelete="CASCADE")
    )
    contact_id: Mapped[str | None] = mapped_column(
        ForeignKey("contacts.id", ondelete="CASCADE")
    )
    # Deliberately not a foreign key to avoid a DDL cycle; publisher IDs are
    # application-validated and retained if an entity is merged.
    publisher_id: Mapped[str | None] = mapped_column(String(36))
    scope: Mapped[str] = mapped_column(String(30), nullable=False)
    reason: Mapped[str] = mapped_column(String(120), nullable=False)
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    permanent: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lifted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)


class VerificationResult(Base):
    __tablename__ = "verification_results"
    __table_args__ = (
        UniqueConstraint(
            "provider", "provider_request_id", name="uq_verification_provider_request"
        ),
        Index("ix_verification_contact_checked", "contact_id", "checked_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    contact_id: Mapped[str] = mapped_column(
        ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    provider_request_id: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[VerificationStatus] = mapped_column(
        enum_column(VerificationStatus, "verification_status"),
        nullable=False,
        index=True,
    )
    catch_all: Mapped[bool | None] = mapped_column(Boolean)
    risky: Mapped[bool | None] = mapped_column(Boolean)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result_summary: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False
    )


class SenderAccount(TimestampMixin, Base):
    __tablename__ = "sender_accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    external_id: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False, index=True
    )
    sender_email: Mapped[str | None] = mapped_column(String(320), index=True)
    provider_status: Mapped[int | None] = mapped_column(Integer, index=True)
    connected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    healthy_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    new_leads_sent_today: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_emails_sent_today: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    rolling_bounce_rate: Mapped[float | None] = mapped_column(Float)
    rolling_unsubscribe_rate: Mapped[float | None] = mapped_column(Float)
    bounce_protection_triggered: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )


class CampaignLaunch(TimestampMixin, Base):
    __tablename__ = "campaign_launches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    deterministic_key: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[CampaignLaunchStatus] = mapped_column(
        enum_column(CampaignLaunchStatus, "campaign_launch_status"),
        default=CampaignLaunchStatus.DRAFT,
        nullable=False,
        index=True,
    )
    contact_policy: Mapped[str] = mapped_column(String(80), nullable=False)
    config_version: Mapped[str] = mapped_column(String(80), nullable=False)
    safety_policy_version: Mapped[str] = mapped_column(String(80), nullable=False)
    batch_size: Mapped[int] = mapped_column(Integer, default=25, nullable=False)
    daily_new_leads_per_sender: Mapped[int] = mapped_column(
        Integer, default=10, nullable=False
    )
    hard_bounce_limit: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    settings_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CampaignBatch(TimestampMixin, Base):
    __tablename__ = "campaign_batches"
    __table_args__ = (
        CheckConstraint("target_size > 0 AND target_size <= 250", name="valid_target_size"),
        Index(
            "uq_campaign_batches_active_sender",
            "sender_account_id",
            unique=True,
            postgresql_where=text("active_assignment"),
            sqlite_where=text("active_assignment = 1"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    deterministic_key: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False, index=True
    )
    launch_id: Mapped[str | None] = mapped_column(
        ForeignKey("campaign_launches.id", ondelete="SET NULL"), index=True
    )
    segment: Mapped[str | None] = mapped_column(String(40), index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    external_campaign_id: Mapped[str | None] = mapped_column(
        String(255), unique=True, index=True
    )
    status: Mapped[CampaignBatchStatus] = mapped_column(
        enum_column(CampaignBatchStatus, "campaign_batch_status"),
        default=CampaignBatchStatus.DRAFT,
        nullable=False,
        index=True,
    )
    target_size: Mapped[int] = mapped_column(Integer, default=250, nullable=False)
    sender_account_id: Mapped[str | None] = mapped_column(
        ForeignKey("sender_accounts.id", ondelete="SET NULL"), index=True
    )
    active_assignment: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    config_version: Mapped[str] = mapped_column(String(80), nullable=False)
    managed_by_link_os: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    expected_member_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    uploaded_member_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    readback_member_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pending_verification_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    hard_bounce_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unsubscribe_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paused_reason: Mapped[str | None] = mapped_column(Text)
    reply_checkpoint: Mapped[str | None] = mapped_column(String(512))
    delivery_checkpoint: Mapped[str | None] = mapped_column(String(512))
    settings_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False
    )


class CampaignMember(Base):
    __tablename__ = "campaign_members"
    __table_args__ = (
        UniqueConstraint(
            "campaign_batch_id", "contact_id", name="uq_campaign_members_batch_contact"
        ),
        Index(
            "uq_campaign_members_active_contact",
            "contact_id",
            unique=True,
            postgresql_where=text("reservation_active"),
            sqlite_where=text("reservation_active = 1"),
        ),
        Index(
            "uq_campaign_members_active_domain",
            "domain_id",
            unique=True,
            postgresql_where=text("reservation_active"),
            sqlite_where=text("reservation_active = 1"),
        ),
        Index("ix_campaign_members_domain_status", "domain_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    campaign_batch_id: Mapped[str] = mapped_column(
        ForeignKey("campaign_batches.id", ondelete="CASCADE"), nullable=False, index=True
    )
    contact_id: Mapped[str] = mapped_column(
        ForeignKey("contacts.id", ondelete="RESTRICT"), nullable=False
    )
    domain_id: Mapped[str] = mapped_column(
        ForeignKey("domains.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[CampaignMemberStatus] = mapped_column(
        enum_column(CampaignMemberStatus, "campaign_member_status"),
        default=CampaignMemberStatus.RESERVED,
        nullable=False,
        index=True,
    )
    reservation_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    external_lead_id: Mapped[str | None] = mapped_column(String(255), index=True)
    reserved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    contacted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_reason: Mapped[str | None] = mapped_column(Text)


class InstantlyEvent(Base):
    __tablename__ = "instantly_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    dedupe_key: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False, index=True
    )
    external_event_id: Mapped[str | None] = mapped_column(String(255), index=True)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    external_campaign_id: Mapped[str | None] = mapped_column(String(255), index=True)
    external_lead_id: Mapped[str | None] = mapped_column(String(255), index=True)
    external_email_id: Mapped[str | None] = mapped_column(String(255), index=True)
    external_thread_id: Mapped[str | None] = mapped_column(String(255), index=True)
    domain_id: Mapped[str | None] = mapped_column(
        ForeignKey("domains.id", ondelete="SET NULL"), index=True
    )
    contact_id: Mapped[str | None] = mapped_column(
        ForeignKey("contacts.id", ondelete="SET NULL"), index=True
    )
    event_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class Reply(TimestampMixin, Base):
    __tablename__ = "replies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    external_email_id: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False, index=True
    )
    external_thread_id: Mapped[str | None] = mapped_column(String(255), index=True)
    instantly_event_id: Mapped[str | None] = mapped_column(
        ForeignKey("instantly_events.id", ondelete="SET NULL"), unique=True
    )
    campaign_batch_id: Mapped[str | None] = mapped_column(
        ForeignKey("campaign_batches.id", ondelete="SET NULL"), index=True
    )
    campaign_member_id: Mapped[str | None] = mapped_column(
        ForeignKey("campaign_members.id", ondelete="SET NULL"), index=True
    )
    domain_id: Mapped[str | None] = mapped_column(
        ForeignKey("domains.id", ondelete="SET NULL"), index=True
    )
    contact_id: Mapped[str | None] = mapped_column(
        ForeignKey("contacts.id", ondelete="SET NULL"), index=True
    )
    from_address: Mapped[str | None] = mapped_column(String(320))
    to_address: Mapped[str | None] = mapped_column(String(320))
    subject: Mapped[str | None] = mapped_column(Text)
    body_text: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    attachment_only: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    thread_evidence: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PublisherEntity(TimestampMixin, Base):
    __tablename__ = "publisher_entities"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    canonical_name: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_key: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False, index=True
    )
    primary_contact_id: Mapped[str | None] = mapped_column(
        ForeignKey("contacts.id", ondelete="SET NULL"), index=True
    )
    notes: Mapped[str | None] = mapped_column(Text)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class PublisherDomain(Base):
    __tablename__ = "publisher_domains"
    __table_args__ = (
        UniqueConstraint("domain_id", name="uq_publisher_domains_domain"),
        UniqueConstraint(
            "publisher_id", "domain_id", name="uq_publisher_domains_publisher_domain"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    publisher_id: Mapped[str] = mapped_column(
        ForeignKey("publisher_entities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    domain_id: Mapped[str] = mapped_column(
        ForeignKey("domains.id", ondelete="CASCADE"), nullable=False
    )
    relationship_type: Mapped[str] = mapped_column(
        String(40), default="owned", nullable=False
    )
    confidence: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class Offer(TimestampMixin, Base):
    __tablename__ = "offers"
    __table_args__ = (
        CheckConstraint(
            "original_amount IS NULL OR original_amount > 0", name="positive_original_amount"
        ),
        CheckConstraint(
            "reseller_price_aud IS NULL OR reseller_price_aud > 0",
            name="positive_reseller_price",
        ),
        Index("ix_offers_domain_status", "domain_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    reply_id: Mapped[str] = mapped_column(
        ForeignKey("replies.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    domain_id: Mapped[str] = mapped_column(
        ForeignKey("domains.id", ondelete="RESTRICT"), nullable=False
    )
    publisher_id: Mapped[str | None] = mapped_column(
        ForeignKey("publisher_entities.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[OfferStatus] = mapped_column(
        enum_column(OfferStatus, "offer_status"),
        default=OfferStatus.REVIEW,
        nullable=False,
        index=True,
    )
    placement_type: Mapped[str | None] = mapped_column(String(80))
    original_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    original_currency: Mapped[str | None] = mapped_column(String(3))
    extraction_confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    attachment_only: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    redirect_ambiguity: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    network_ambiguity: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    conflicting_prices: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    cost_aud: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    fx_rate_to_aud: Mapped[Decimal | None] = mapped_column(Numeric(16, 8))
    fx_rate_date: Mapped[date | None] = mapped_column(Date)
    reseller_price_aud: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    pricing_rule_version: Mapped[str | None] = mapped_column(String(80))
    extracted_terms: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False
    )
    manual_override: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[str | None] = mapped_column(String(255))


class CatalogueListing(TimestampMixin, Base):
    __tablename__ = "catalogue_listings"
    __table_args__ = (
        CheckConstraint("agency_price_aud > 0", name="positive_agency_price"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    offer_id: Mapped[str] = mapped_column(
        ForeignKey("offers.id", ondelete="RESTRICT"), unique=True, nullable=False
    )
    publisher_id: Mapped[str | None] = mapped_column(
        ForeignKey("publisher_entities.id", ondelete="SET NULL"), index=True
    )
    domain_id: Mapped[str] = mapped_column(
        ForeignKey("domains.id", ondelete="RESTRICT"), index=True
    )
    status: Mapped[ListingStatus] = mapped_column(
        enum_column(ListingStatus, "listing_status"),
        default=ListingStatus.PRIVATE,
        nullable=False,
        index=True,
    )
    visibility_tier: Mapped[str] = mapped_column(
        String(40), default="private", nullable=False, index=True
    )
    title: Mapped[str | None] = mapped_column(String(255))
    agency_price_aud: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    public_terms: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    listed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MondayMapping(TimestampMixin, Base):
    __tablename__ = "monday_mappings"
    __table_args__ = (
        UniqueConstraint(
            "external_board_id", "external_item_id", name="uq_monday_board_item"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    stable_key: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False, index=True
    )
    entity_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    entity_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    board_key: Mapped[str] = mapped_column(String(80), nullable=False)
    external_board_id: Mapped[str] = mapped_column(String(255), nullable=False)
    external_item_id: Mapped[str] = mapped_column(String(255), nullable=False)
    last_payload_hash: Mapped[str | None] = mapped_column(String(64))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sync_status: Mapped[str] = mapped_column(
        String(40), default="pending", nullable=False, index=True
    )
    last_error: Mapped[str | None] = mapped_column(Text)


class WorkerRun(Base):
    __tablename__ = "worker_runs"
    __table_args__ = (
        Index("ix_worker_runs_worker_started", "worker_name", "started_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    worker_name: Mapped[str] = mapped_column(String(255), nullable=False)
    run_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    status: Mapped[WorkerRunStatus] = mapped_column(
        enum_column(WorkerRunStatus, "worker_run_status"),
        default=WorkerRunStatus.RUNNING,
        nullable=False,
        index=True,
    )
    lease_owner: Mapped[str | None] = mapped_column(String(255))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    counters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_entity_created", "entity_type", "entity_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_type: Mapped[str] = mapped_column(String(40), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(255))
    request_id: Mapped[str | None] = mapped_column(String(255), index=True)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class SystemSetting(Base):
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
    updated_by: Mapped[str | None] = mapped_column(String(255))


class UserAccount(TimestampMixin, Base):
    """Canonical LINK OS login and catalogue account.

    Integer identifiers intentionally preserve compatibility with the original
    local catalogue routes while PostgreSQL replaces the legacy SQLite auth
    tables.
    """

    __tablename__ = "user_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(40), default="agency", nullable=False, index=True)
    visibility_tier: Mapped[str] = mapped_column(String(40), default="basic", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, default=dict, nullable=False
    )


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("user_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class AgencyEnquiry(Base):
    __tablename__ = "agency_enquiries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("user_accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    listing_id: Mapped[str] = mapped_column(
        ForeignKey("catalogue_listings.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="new", nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


class MigrationSourceRecord(TimestampMixin, Base):
    """Idempotency and provenance ledger for legacy-source cutover records."""

    __tablename__ = "migration_source_records"
    __table_args__ = (
        UniqueConstraint("category", "natural_key", name="uq_migration_source_category_key"),
        Index("ix_migration_source_run_status", "migration_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    migration_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    natural_key: Mapped[str] = mapped_column(String(512), nullable=False)
    source_label: Mapped[str] = mapped_column(String(120), nullable=False)
    source_precedence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    source_table: Mapped[str] = mapped_column(String(120), nullable=False)
    source_payload: Mapped[dict[str, Any]] = mapped_column(
        "payload", JSON, default=dict, nullable=False
    )
    manual_authoritative: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    freshness: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(80), index=True)
    target_id: Mapped[str | None] = mapped_column(String(255), index=True)
    status: Mapped[str] = mapped_column(String(40), default="accepted", nullable=False, index=True)
    error: Mapped[str | None] = mapped_column(Text)
