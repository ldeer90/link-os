"""Stable string enums for the LINK OS control plane.

The values in this module are persisted, so they must be extended rather than
renamed once production data exists.
"""

from __future__ import annotations

from enum import StrEnum


class LifecycleStage(StrEnum):
    IMPORTED = "imported"
    QUEUED = "queued"
    SCRAPING = "scraping"
    EMAIL_FOUND = "email_found"
    NO_EMAIL = "no_email"
    FAILED = "failed"
    VERIFIED = "verified"
    SUPPRESSED = "suppressed"
    OUTREACH_READY = "outreach_ready"
    CAMPAIGN_QUEUED = "campaign_queued"
    UPLOADED = "uploaded"
    CONTACTED = "contacted"
    REPLIED = "replied"
    OFFER_APPROVED = "offer_approved"
    OFFER_REVIEW = "offer_review"
    LISTED = "listed"
    LOST = "lost"


class ImportStatus(StrEnum):
    RECEIVED = "received"
    PROCESSING = "processing"
    COMPLETE = "complete"
    FAILED = "failed"


class ImportItemStatus(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    INVALID = "invalid"
    SUPPRESSED = "suppressed"
    PREVIOUSLY_CONTACTED = "previously_contacted"
    REFRESH_ELIGIBLE = "refresh_eligible"


class JobStatus(StrEnum):
    QUEUED = "queued"
    LEASED = "leased"
    SUCCEEDED = "succeeded"
    DEAD = "dead"
    CANCELLED = "cancelled"


class ScrapeStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    NO_EMAIL = "no_email"
    FAILED = "failed"


class VerificationStatus(StrEnum):
    PENDING = "pending"
    VERIFIED = "verified"
    INVALID = "invalid"
    RISKY = "risky"
    CATCH_ALL = "catch_all"
    UNKNOWN = "unknown"


class CampaignBatchStatus(StrEnum):
    DRAFT = "draft"
    RESERVED = "reserved"
    UPLOADING = "uploading"
    PAUSED = "paused"
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"


class CampaignLaunchStatus(StrEnum):
    DRAFT = "draft"
    PREPARING = "preparing"
    PAUSED = "paused"
    ACTIVE = "active"
    PARTIAL = "partial"
    COMPLETED = "completed"
    FAILED = "failed"


class CampaignMemberStatus(StrEnum):
    RESERVED = "reserved"
    UPLOADED = "uploaded"
    CONTACTED = "contacted"
    REPLIED = "replied"
    RELEASED = "released"
    FAILED = "failed"


class OfferStatus(StrEnum):
    REVIEW = "review"
    APPROVED = "approved"
    REJECTED = "rejected"
    LOST = "lost"


class ListingStatus(StrEnum):
    PRIVATE = "private"
    LISTED = "listed"
    HIDDEN = "hidden"
    ARCHIVED = "archived"


class WorkerRunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ABANDONED = "abandoned"


class BacklinkAnalysisMode(StrEnum):
    COMPETITOR_PROSPECTING = "competitor_prospecting"
    CLIENT_PROFILE_AUDIT = "client_profile_audit"


class BacklinkAnalysisStatus(StrEnum):
    DRAFT = "draft"
    AWAITING_CREDIT_CONFIRMATION = "awaiting_credit_confirmation"
    QUEUED = "queued"
    FETCHING = "fetching"
    FILTERING = "filtering"
    AWAITING_CODEX_REVIEW = "awaiting_codex_review"
    QUEUEING_SCRAPE = "queueing_scrape"
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    CREDIT_STATE_UNKNOWN = "credit_state_unknown"


class BacklinkCandidateStatus(StrEnum):
    HARD_REJECTED = "hard_rejected"
    AWAITING_CODEX_REVIEW = "awaiting_codex_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    MANUAL_REVIEW = "manual_review"
    AUDIT_ONLY = "audit_only"
    IMPORT_QUEUED = "import_queued"
    IMPORTED = "imported"
