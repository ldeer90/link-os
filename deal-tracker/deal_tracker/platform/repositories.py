"""Transactional repositories for the canonical LINK OS database."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .enums import (
    CampaignBatchStatus,
    CampaignMemberStatus,
    JobStatus,
    LifecycleStage,
    VerificationStatus,
)
from .lifecycle import validate_transition
from .models import (
    AuditLog,
    CampaignBatch,
    CampaignMember,
    Contact,
    Domain,
    DomainContactEvidence,
    DomainEvent,
    Job,
    Suppression,
    SystemSetting,
    VerificationResult,
    utcnow,
)
from .normalization import normalize_domain


OUTREACH_PAUSE_KEY = "outreach.global_pause"
STRICT_VERIFIED_CONTACT_POLICY = "strict_verified"
PUBLIC_EVIDENCE_CONTACT_POLICY = "public_evidence_verification_skipped"
CONTACT_POLICIES = frozenset(
    {STRICT_VERIFIED_CONTACT_POLICY, PUBLIC_EVIDENCE_CONTACT_POLICY}
)
PUBLIC_EVIDENCE_MIN_CONFIDENCE = 0.75


class JobLeaseError(RuntimeError):
    pass


class CampaignReservationError(RuntimeError):
    pass


def _now(value: datetime | None = None) -> datetime:
    instant = value or datetime.now(timezone.utc)
    return instant if instant.tzinfo is not None else instant.replace(tzinfo=timezone.utc)


def _locked(statement: Any, session: Session) -> Any:
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        return statement.with_for_update()
    return statement


class DomainRepository:
    def __init__(self, session: Session):
        self.session = session

    def get_or_create(
        self,
        value: str,
        *,
        source: str,
        now: datetime | None = None,
    ) -> tuple[Domain, bool]:
        normalized = normalize_domain(value)
        existing = self.session.scalar(
            select(Domain).where(
                Domain.normalized_domain == normalized.registrable_domain
            )
        )
        if existing is not None:
            return existing, False

        domain = Domain(
            normalized_domain=normalized.registrable_domain,
            first_input_value=value,
            tld=normalized.tld,
            country_code=normalized.country_code,
            lifecycle_stage=LifecycleStage.IMPORTED,
            stage_changed_at=_now(now),
        )
        try:
            with self.session.begin_nested():
                self.session.add(domain)
                self.session.flush()
        except IntegrityError:
            # A parallel import won the unique-key race.
            existing = self.session.scalar(
                select(Domain).where(
                    Domain.normalized_domain == normalized.registrable_domain
                )
            )
            if existing is None:
                raise
            return existing, False

        self.session.add(
            DomainEvent(
                domain_id=domain.id,
                event_type="domain_imported",
                to_stage=LifecycleStage.IMPORTED,
                source=source,
                created_at=_now(now),
            )
        )
        return domain, True

    def transition(
        self,
        domain_id: str,
        target: LifecycleStage | str,
        *,
        source: str,
        reason: str | None = None,
        payload: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> Domain:
        statement = select(Domain).where(Domain.id == domain_id)
        domain = self.session.scalar(_locked(statement, self.session))
        if domain is None:
            raise LookupError(f"domain {domain_id!r} does not exist")
        old_stage = domain.lifecycle_stage
        new_stage = validate_transition(old_stage, target)
        if old_stage == new_stage:
            return domain

        changed_at = _now(now)
        domain.lifecycle_stage = new_stage
        domain.stage_changed_at = changed_at
        if new_stage == LifecycleStage.CONTACTED:
            domain.last_contacted_at = changed_at
        self.session.add(
            DomainEvent(
                domain_id=domain.id,
                event_type="lifecycle_transition",
                from_stage=old_stage,
                to_stage=new_stage,
                reason=reason,
                source=source,
                payload=payload or {},
                created_at=changed_at,
            )
        )
        self.session.flush()
        return domain


class JobQueue:
    """Database-backed leased jobs with PostgreSQL SKIP LOCKED semantics."""

    def __init__(self, session: Session):
        self.session = session

    def enqueue(
        self,
        *,
        kind: str,
        idempotency_key: str,
        payload: dict[str, Any] | None = None,
        subject_type: str | None = None,
        subject_id: str | None = None,
        priority: int = 0,
        max_attempts: int = 3,
        available_at: datetime | None = None,
    ) -> tuple[Job, bool]:
        existing = self.session.scalar(
            select(Job).where(Job.idempotency_key == idempotency_key)
        )
        if existing is not None:
            return existing, False
        job = Job(
            kind=kind,
            idempotency_key=idempotency_key,
            payload=payload or {},
            subject_type=subject_type,
            subject_id=subject_id,
            priority=priority,
            max_attempts=max_attempts,
            available_at=_now(available_at),
        )
        try:
            with self.session.begin_nested():
                self.session.add(job)
                self.session.flush()
        except IntegrityError:
            existing = self.session.scalar(
                select(Job).where(Job.idempotency_key == idempotency_key)
            )
            if existing is None:
                raise
            return existing, False
        return job, True

    def lease(
        self,
        *,
        worker_id: str,
        limit: int = 1,
        lease_for: timedelta = timedelta(minutes=5),
        kinds: Iterable[str] | None = None,
        now: datetime | None = None,
    ) -> list[Job]:
        if limit < 1:
            return []
        instant = _now(now)
        exhausted_statement = select(Job).where(
            Job.status == JobStatus.LEASED,
            Job.leased_until < instant,
            Job.attempts >= Job.max_attempts,
        )
        if self.session.bind is not None and self.session.bind.dialect.name == "postgresql":
            exhausted_statement = exhausted_statement.with_for_update(skip_locked=True)
        for exhausted in self.session.scalars(exhausted_statement):
            exhausted.status = JobStatus.DEAD
            exhausted.completed_at = instant
            exhausted.lease_owner = None
            exhausted.leased_until = None
            if not exhausted.last_error:
                exhausted.last_error = "worker lease expired after final attempt"
        self.session.flush()

        eligible = or_(
            and_(Job.status == JobStatus.QUEUED, Job.available_at <= instant),
            and_(Job.status == JobStatus.LEASED, Job.leased_until < instant),
        )
        statement = (
            select(Job)
            .where(eligible, Job.attempts < Job.max_attempts)
            .order_by(Job.priority.desc(), Job.available_at, Job.created_at)
            .limit(limit)
        )
        if kinds:
            statement = statement.where(Job.kind.in_(tuple(kinds)))
        if self.session.bind is not None and self.session.bind.dialect.name == "postgresql":
            statement = statement.with_for_update(skip_locked=True)
        jobs = list(self.session.scalars(statement))
        leased_until = instant + lease_for
        for job in jobs:
            job.status = JobStatus.LEASED
            job.lease_owner = worker_id
            job.leased_until = leased_until
            job.attempts += 1
        self.session.flush()
        return jobs

    def heartbeat(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_for: timedelta = timedelta(minutes=5),
        now: datetime | None = None,
    ) -> Job:
        job = self._owned_lease(job_id, worker_id)
        job.leased_until = _now(now) + lease_for
        self.session.flush()
        return job

    def complete(
        self,
        job_id: str,
        *,
        worker_id: str,
        now: datetime | None = None,
    ) -> Job:
        job = self._owned_lease(job_id, worker_id)
        job.status = JobStatus.SUCCEEDED
        job.completed_at = _now(now)
        job.lease_owner = None
        job.leased_until = None
        self.session.flush()
        return job

    def fail(
        self,
        job_id: str,
        *,
        worker_id: str,
        error: str,
        retryable: bool = True,
        now: datetime | None = None,
        base_backoff: timedelta = timedelta(seconds=30),
    ) -> Job:
        job = self._owned_lease(job_id, worker_id)
        instant = _now(now)
        job.last_error = error[:10000]
        job.lease_owner = None
        job.leased_until = None
        if not retryable or job.attempts >= job.max_attempts:
            job.status = JobStatus.DEAD
            job.completed_at = instant
        else:
            job.status = JobStatus.QUEUED
            multiplier = 2 ** max(job.attempts - 1, 0)
            job.available_at = instant + base_backoff * multiplier
        self.session.flush()
        return job

    def retry(
        self,
        job_id: str,
        *,
        actor: str,
        now: datetime | None = None,
    ) -> Job:
        statement = select(Job).where(Job.id == job_id)
        job = self.session.scalar(_locked(statement, self.session))
        if job is None:
            raise LookupError(f"job {job_id!r} does not exist")
        if job.status not in {JobStatus.DEAD, JobStatus.CANCELLED}:
            raise JobLeaseError("only dead or cancelled jobs can be manually retried")
        before = job.status.value
        job.status = JobStatus.QUEUED
        job.attempts = 0
        job.available_at = _now(now)
        job.completed_at = None
        job.lease_owner = None
        job.leased_until = None
        self.session.add(
            AuditLog(
                actor_type="user",
                actor_id=actor,
                action="job.retry",
                entity_type="job",
                entity_id=job.id,
                before={"status": before},
                after={"status": JobStatus.QUEUED.value},
            )
        )
        self.session.flush()
        return job

    def _owned_lease(self, job_id: str, worker_id: str) -> Job:
        statement = select(Job).where(Job.id == job_id)
        job = self.session.scalar(_locked(statement, self.session))
        if job is None:
            raise LookupError(f"job {job_id!r} does not exist")
        if job.status != JobStatus.LEASED or job.lease_owner != worker_id:
            raise JobLeaseError(
                f"job {job_id!r} is not leased by worker {worker_id!r}"
            )
        return job


class CampaignRepository:
    """Reserve qualified, evidenced contacts in deterministic 250-row batches."""

    def __init__(self, session: Session):
        self.session = session

    def reserve_batch(
        self,
        *,
        deterministic_key: str,
        name: str,
        config_version: str,
        members: Sequence[tuple[str, str]],
        contact_policy: str = STRICT_VERIFIED_CONTACT_POLICY,
        now: datetime | None = None,
    ) -> tuple[CampaignBatch, bool]:
        if contact_policy not in CONTACT_POLICIES:
            raise CampaignReservationError(f"unknown contact policy: {contact_policy}")
        existing = self.session.scalar(
            select(CampaignBatch).where(
                CampaignBatch.deterministic_key == deterministic_key
            )
        )
        if existing is not None:
            return existing, False
        if not members:
            raise CampaignReservationError("a campaign batch cannot be empty")
        if len(members) > 250:
            raise CampaignReservationError("campaign batches are limited to 250 contacts")
        domain_ids = [domain_id for domain_id, _ in members]
        if len(domain_ids) != len(set(domain_ids)):
            raise CampaignReservationError("a domain may only appear once per batch")
        contact_ids = [contact_id for _, contact_id in members]
        if len(contact_ids) != len(set(contact_ids)):
            raise CampaignReservationError("a shared contact may only appear once per batch")

        instant = _now(now)
        eligibility_errors: dict[str, tuple[str, ...]] = {}
        for domain_id, contact_id in members:
            reasons = self._eligibility_reasons(
                domain_id, contact_id, instant, contact_policy=contact_policy
            )
            if reasons:
                eligibility_errors[f"{domain_id}:{contact_id}"] = tuple(reasons)
        if eligibility_errors:
            formatted = "; ".join(
                f"{key}={','.join(reasons)}"
                for key, reasons in sorted(eligibility_errors.items())
            )
            raise CampaignReservationError(f"ineligible campaign members: {formatted}")

        batch = CampaignBatch(
            deterministic_key=deterministic_key,
            name=name,
            status=CampaignBatchStatus.RESERVED,
            target_size=len(members),
            expected_member_count=len(members),
            config_version=config_version,
            active_assignment=False,
            settings_snapshot={"contact_policy": contact_policy},
        )
        try:
            with self.session.begin_nested():
                self.session.add(batch)
                self.session.flush()
                for domain_id, contact_id in members:
                    self.session.add(
                        CampaignMember(
                            campaign_batch_id=batch.id,
                            contact_id=contact_id,
                            domain_id=domain_id,
                            status=CampaignMemberStatus.RESERVED,
                            reservation_active=True,
                            reserved_at=instant,
                        )
                    )
                self.session.flush()
        except IntegrityError as exc:
            raise CampaignReservationError(
                "a contact is already reserved or the deterministic batch already exists"
            ) from exc

        domain_repository = DomainRepository(self.session)
        for domain_id, _ in members:
            domain = self.session.get(Domain, domain_id)
            if (
                contact_policy == PUBLIC_EVIDENCE_CONTACT_POLICY
                and domain is not None
                and domain.lifecycle_stage in {
                    LifecycleStage.EMAIL_FOUND,
                    LifecycleStage.VERIFIED,
                }
            ):
                domain_repository.transition(
                    domain_id,
                    LifecycleStage.OUTREACH_READY,
                    source="public_evidence_policy",
                    reason=(
                        "public-site evidence approved for pilot; "
                        "deliverability verification skipped"
                    ),
                    now=instant,
                )
            domain_repository.transition(
                domain_id,
                LifecycleStage.CAMPAIGN_QUEUED,
                source="campaign_reservation",
                reason=f"reserved in batch {batch.id}",
                now=instant,
            )
        self.session.flush()
        return batch, True

    def release_batch(
        self,
        batch_id: str,
        *,
        reason: str,
        now: datetime | None = None,
    ) -> int:
        instant = _now(now)
        batch = self.session.get(CampaignBatch, batch_id)
        if batch is None:
            raise LookupError(f"campaign batch {batch_id!r} does not exist")
        members = list(
            self.session.scalars(
                select(CampaignMember).where(
                    CampaignMember.campaign_batch_id == batch_id,
                    CampaignMember.reservation_active.is_(True),
                )
            )
        )
        for member in members:
            member.reservation_active = False
            member.status = CampaignMemberStatus.RELEASED
            member.released_at = instant
            member.failure_reason = reason
        batch.active_assignment = False
        batch.status = CampaignBatchStatus.FAILED
        batch.paused_reason = reason
        self.session.flush()
        return len(members)

    def eligibility_reasons(
        self,
        domain_id: str,
        contact_id: str,
        *,
        contact_policy: str = STRICT_VERIFIED_CONTACT_POLICY,
        now: datetime | None = None,
    ) -> tuple[str, ...]:
        """Expose the same fail-closed checks used by transactional reserve."""

        return tuple(
            self._eligibility_reasons(
                domain_id,
                contact_id,
                _now(now),
                contact_policy=contact_policy,
            )
        )

    def _eligibility_reasons(
        self,
        domain_id: str,
        contact_id: str,
        instant: datetime,
        *,
        contact_policy: str,
    ) -> list[str]:
        reasons: list[str] = []
        domain = self.session.get(Domain, domain_id)
        contact = self.session.get(Contact, contact_id)
        if domain is None:
            return ["domain_missing"]
        if contact is None:
            return ["contact_missing"]
        permitted_stages = {LifecycleStage.OUTREACH_READY}
        if contact_policy == PUBLIC_EVIDENCE_CONTACT_POLICY:
            permitted_stages.update(
                {LifecycleStage.EMAIL_FOUND, LifecycleStage.VERIFIED}
            )
        if domain.lifecycle_stage not in permitted_stages:
            reasons.append("domain_not_outreach_ready")
        if domain.last_contacted_at is not None:
            reasons.append("domain_previously_contacted")
        if contact.is_guessed:
            reasons.append("guessed_email")

        evidence_conditions = [
            DomainContactEvidence.domain_id == domain_id,
            DomainContactEvidence.contact_id == contact_id,
            DomainContactEvidence.is_public_page.is_(True),
        ]
        if contact_policy == PUBLIC_EVIDENCE_CONTACT_POLICY:
            evidence_conditions.append(
                DomainContactEvidence.confidence >= PUBLIC_EVIDENCE_MIN_CONFIDENCE
            )
        evidence_count = self.session.scalar(
            select(func.count(DomainContactEvidence.id)).where(
                *evidence_conditions,
            )
        )
        if not evidence_count:
            reasons.append(
                "insufficient_public_evidence"
                if contact_policy == PUBLIC_EVIDENCE_CONTACT_POLICY
                else "missing_public_evidence"
            )

        if contact_policy == STRICT_VERIFIED_CONTACT_POLICY:
            verification = self.session.scalar(
                select(VerificationResult)
                .where(
                    VerificationResult.contact_id == contact_id,
                    func.lower(VerificationResult.provider) == "instantly",
                )
                .order_by(VerificationResult.checked_at.desc())
                .limit(1)
            )
            if verification is None:
                reasons.append("instantly_verification_missing")
            else:
                expires_at = verification.expires_at
                if expires_at is not None and expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                if not (
                    verification.status == VerificationStatus.VERIFIED
                    and verification.catch_all is False
                    and verification.risky is False
                    and (expires_at is None or expires_at > instant)
                ):
                    reasons.append("instantly_verification_ineligible")

        active_suppression = self.session.scalar(
            select(Suppression.id)
            .where(
                Suppression.active.is_(True),
                or_(
                    Suppression.domain_id == domain_id,
                    Suppression.contact_id == contact_id,
                ),
                or_(Suppression.expires_at.is_(None), Suppression.expires_at > instant),
            )
            .limit(1)
        )
        if active_suppression is not None:
            reasons.append("suppressed")
        active_membership = self.session.scalar(
            select(CampaignMember.id)
            .where(
                CampaignMember.contact_id == contact_id,
                CampaignMember.reservation_active.is_(True),
            )
            .limit(1)
        )
        if active_membership is not None:
            reasons.append("contact_already_reserved")
        return reasons


def is_outreach_paused(session: Session) -> bool:
    """Missing or malformed state is treated as paused."""

    setting = session.get(SystemSetting, OUTREACH_PAUSE_KEY)
    if setting is None or not isinstance(setting.value, dict):
        return True
    return setting.value.get("paused") is not False


def set_outreach_paused(
    session: Session,
    *,
    paused: bool,
    actor: str,
    reason: str,
    now: datetime | None = None,
) -> SystemSetting:
    instant = _now(now)
    setting = session.get(SystemSetting, OUTREACH_PAUSE_KEY)
    before = {"paused": True} if setting is None else dict(setting.value)
    after = {"paused": bool(paused), "reason": reason, "changed_at": instant.isoformat()}
    if setting is None:
        setting = SystemSetting(
            key=OUTREACH_PAUSE_KEY,
            value=after,
            version=1,
            updated_at=instant,
            updated_by=actor,
        )
        session.add(setting)
    else:
        setting.value = after
        setting.version += 1
        setting.updated_at = instant
        setting.updated_by = actor
    session.add(
        AuditLog(
            actor_type="user",
            actor_id=actor,
            action="outreach.pause" if paused else "outreach.resume",
            entity_type="system_setting",
            entity_id=OUTREACH_PAUSE_KEY,
            before=before,
            after=after,
            reason=reason,
            created_at=instant,
        )
    )
    session.flush()
    return setting


def ensure_fail_closed_defaults(session: Session) -> None:
    if session.get(SystemSetting, OUTREACH_PAUSE_KEY) is None:
        session.add(
            SystemSetting(
                key=OUTREACH_PAUSE_KEY,
                value={
                    "paused": True,
                    "reason": "initial fail-closed default",
                    "changed_at": utcnow().isoformat(),
                },
                version=1,
                updated_by="migration",
            )
        )
        session.flush()
