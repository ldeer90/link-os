from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from deal_tracker.integrations.instantly import InstantlyControl, PUBLIC_EVIDENCE_PILOT_COPY_VERSION
from deal_tracker.platform.enums import CampaignBatchStatus, CampaignLaunchStatus, CampaignMemberStatus
from deal_tracker.platform.models import (
    AuditLog,
    BacklinkAnalysisRun,
    BacklinkCandidate,
    CampaignBatch,
    CampaignLaunch,
    CampaignMember,
    Contact,
    Domain,
    DomainContactEvidence,
    ImportItem,
    Job,
    SenderAccount,
    Suppression,
    utcnow,
)
from deal_tracker.platform.repositories import (
    CampaignRepository,
    JobQueue,
    PUBLIC_EVIDENCE_CONTACT_POLICY,
)


SEGMENTED_CONFIG_VERSION = "public-evidence-segmented-v2.0"
SEGMENTED_SAFETY_VERSION = "three-hard-bounces-local-v1.0"
STRONG_PUBLIC_ROLES = frozenset(
    {"submissions", "contributor", "editorial", "editor", "media", "advertising"}
)
TRAVEL_SEED_DOMAINS = frozenset(
    {
        "airalo.com", "worldnomads.com", "safetywing.com", "discovercars.com",
        "ivisa.com", "holafly.com", "qantas.com", "getnomad.app", "alosim.com",
        "simify.com", "saily.com", "virginaustralia.com", "fastcover.com.au",
        "1cover.com.au", "bounce.com", "insureandgo.com.au", "flightcentre.com.au",
        "battleface.com", "webjet.com.au", "travelinsurancedirect.com.au",
    }
)


def _travel_domain_ids(session: Session) -> set[str]:
    run_ids: list[str] = []
    import_ids: list[str] = []
    for run in session.scalars(select(BacklinkAnalysisRun)):
        if set(run.competitor_domains or []) & TRAVEL_SEED_DOMAINS:
            run_ids.append(str(run.id))
            if run.import_id:
                import_ids.append(str(run.import_id))
    domain_ids = {
        str(value)
        for value in session.scalars(
            select(ImportItem.domain_id).where(
                ImportItem.import_id.in_(import_ids),
                ImportItem.domain_id.is_not(None),
            )
        )
    } if import_ids else set()
    if run_ids:
        domain_ids.update(
            str(value)
            for value in session.scalars(
                select(BacklinkCandidate.existing_domain_id).where(
                    BacklinkCandidate.run_id.in_(run_ids),
                    BacklinkCandidate.existing_domain_id.is_not(None),
                )
            )
        )
    return domain_ids


def segmented_candidates(session: Session, segment: str, limit: int) -> list[tuple[str, str]]:
    if segment not in {"au_publishers", "travel_global"}:
        raise ValueError("Unknown campaign segment")
    travel_ids = _travel_domain_ids(session) if segment == "travel_global" else set()
    repository = CampaignRepository(session)
    rows = session.execute(
        select(
            DomainContactEvidence.domain_id,
            DomainContactEvidence.contact_id,
            func.max(DomainContactEvidence.confidence).label("confidence"),
        )
        .join(Domain, Domain.id == DomainContactEvidence.domain_id)
        .join(Contact, Contact.id == DomainContactEvidence.contact_id)
        .where(
            DomainContactEvidence.is_public_page.is_(True),
            DomainContactEvidence.confidence >= 0.90,
            Contact.role_class.in_(STRONG_PUBLIC_ROLES),
        )
        .group_by(DomainContactEvidence.domain_id, DomainContactEvidence.contact_id, Domain.created_at)
        .order_by(func.max(DomainContactEvidence.confidence).desc(), Domain.created_at)
    )
    members: list[tuple[str, str]] = []
    seen_domains: set[str] = set()
    seen_contacts: set[str] = set()
    for domain_id, contact_id, _confidence in rows:
        domain = session.get(Domain, domain_id)
        if domain is None:
            continue
        is_au = domain.normalized_domain.endswith(".com.au")
        if segment == "au_publishers" and not is_au:
            continue
        if segment == "travel_global" and (is_au or domain_id not in travel_ids):
            continue
        if domain_id in seen_domains or contact_id in seen_contacts:
            continue
        if repository.eligibility_reasons(
            domain_id, contact_id, contact_policy=PUBLIC_EVIDENCE_CONTACT_POLICY
        ):
            continue
        members.append((domain_id, contact_id))
        seen_domains.add(domain_id)
        seen_contacts.add(contact_id)
        if len(members) >= limit:
            break
    return members


def campaign_set_estimate(session: Session, *, batch_size: int = 25) -> dict[str, Any]:
    au = segmented_candidates(session, "au_publishers", 100000)
    travel = segmented_candidates(session, "travel_global", 100000)
    senders = eligible_launch_senders(session)
    return {
        "batch_size": batch_size,
        "campaign_count": 3,
        "required_contacts": batch_size * 3,
        "available": {"au_publishers": len(au), "travel_global": len(travel)},
        "required": {"au_publishers": batch_size, "travel_global": batch_size * 2},
        "healthy_unassigned_senders": len(senders),
        "allowed": len(au) >= batch_size and len(travel) >= batch_size * 2 and len(senders) >= 3,
        "verification_skipped": True,
        "minimum_evidence_confidence": 0.90,
        "allowed_role_classes": sorted(STRONG_PUBLIC_ROLES),
    }


def eligible_launch_senders(session: Session) -> list[SenderAccount]:
    assigned_ids = {
        value
        for value in session.scalars(
            select(CampaignBatch.sender_account_id).where(
                CampaignBatch.active_assignment.is_(True),
                CampaignBatch.status.not_in([CampaignBatchStatus.FAILED, CampaignBatchStatus.COMPLETED]),
                CampaignBatch.sender_account_id.is_not(None),
            )
        )
    }
    rows = list(
        session.scalars(
            select(SenderAccount)
            .where(
                SenderAccount.connected.is_(True),
                SenderAccount.provider_status == 1,
                SenderAccount.last_error.is_(None),
                SenderAccount.bounce_protection_triggered.is_(False),
            )
            .order_by(SenderAccount.healthy_since, SenderAccount.sender_email)
        )
    )
    return [
        row
        for row in rows
        if row.id not in assigned_ids
        and (row.rolling_bounce_rate is None or row.rolling_bounce_rate < 0.03)
        and (row.rolling_unsubscribe_rate is None or row.rolling_unsubscribe_rate < 0.01)
    ]


def retire_failed_public_evidence_pilot(session: Session, *, actor: str) -> list[str]:
    retired: list[str] = []
    control = InstantlyControl()
    for batch in session.scalars(
        select(CampaignBatch).where(
            CampaignBatch.launch_id.is_(None),
            CampaignBatch.config_version == PUBLIC_EVIDENCE_PILOT_COPY_VERSION,
            CampaignBatch.status.in_([CampaignBatchStatus.PAUSED, CampaignBatchStatus.FAILED]),
        )
    ):
        already_retired = (
            batch.status == CampaignBatchStatus.FAILED
            and batch.paused_reason == "retired_after_bounce"
        )
        if not already_retired and batch.hard_bounce_count <= 0 and "bounce" not in (batch.paused_reason or ""):
            continue
        batch.status = CampaignBatchStatus.FAILED
        batch.active_assignment = False
        batch.paused_reason = "retired_after_bounce"
        bounced_lead_ids: set[str] = set()
        if batch.external_campaign_id:
            control.pause_campaign(batch.external_campaign_id)
            bounced_lead_ids = {
                str(lead.get("id") or "")
                for lead in control.list_campaign_leads(batch.external_campaign_id)
                if lead.get("status") == -1
            }
        for member in session.scalars(
            select(CampaignMember).where(CampaignMember.campaign_batch_id == batch.id)
        ):
            member_bounced = (
                (member.external_lead_id and member.external_lead_id in bounced_lead_ids)
                or (
                    member.status == CampaignMemberStatus.FAILED
                    and "bounce" in (member.failure_reason or "")
                )
            )
            if member_bounced:
                member.status = CampaignMemberStatus.FAILED
                member.failure_reason = "hard_bounce"
                exists = session.scalar(
                    select(Suppression.id).where(
                        Suppression.contact_id == member.contact_id,
                        Suppression.active.is_(True),
                    )
                )
                if exists is None:
                    session.add(
                        Suppression(
                            contact_id=member.contact_id,
                            scope="contact",
                            reason="pilot_hard_bounce",
                            source="instantly",
                            permanent=True,
                            active=True,
                        )
                    )
            if member.status in {CampaignMemberStatus.CONTACTED, CampaignMemberStatus.FAILED}:
                domain_suppressed = session.scalar(
                    select(Suppression.id).where(
                        Suppression.scope == "domain",
                        Suppression.domain_id == member.domain_id,
                        Suppression.active.is_(True),
                    )
                )
                if domain_suppressed is None:
                    session.add(
                        Suppression(
                            domain_id=member.domain_id,
                            scope="domain",
                            reason="pilot_contacted_do_not_recontact",
                            source="instantly",
                            permanent=True,
                            active=True,
                        )
                    )
        session.add(
            AuditLog(
                actor_type="user", actor_id=actor,
                action="campaign.pilot_retired_after_bounce",
                entity_type="campaign_batch", entity_id=batch.id,
                after={"status": "failed", "reason": "retired_after_bounce"},
            )
        )
        retired.append(batch.id)
    return retired


def prepare_campaign_set(session: Session, *, actor: str, batch_size: int = 25) -> tuple[CampaignLaunch, list[Job]]:
    estimate = campaign_set_estimate(session, batch_size=batch_size)
    if not estimate["allowed"]:
        raise ValueError("Campaign set does not have enough eligible contacts or senders")
    au = segmented_candidates(session, "au_publishers", batch_size)
    travel = segmented_candidates(session, "travel_global", batch_size * 2)
    senders = eligible_launch_senders(session)[:3]
    digest = hashlib.sha256(
        "|".join(f"{d}:{c}" for d, c in [*au, *travel]).encode()
    ).hexdigest()[:24]
    key = f"segmented-public-evidence:{digest}"
    existing = session.scalar(select(CampaignLaunch).where(CampaignLaunch.deterministic_key == key))
    if existing is not None:
        jobs = list(session.scalars(select(Job).where(Job.payload["launch_id"].as_string() == existing.id)))
        return existing, jobs
    launch = CampaignLaunch(
        deterministic_key=key,
        name="LINK OS Segmented Public Evidence Launch",
        status=CampaignLaunchStatus.PREPARING,
        contact_policy=PUBLIC_EVIDENCE_CONTACT_POLICY,
        config_version=SEGMENTED_CONFIG_VERSION,
        safety_policy_version=SEGMENTED_SAFETY_VERSION,
        batch_size=batch_size,
        daily_new_leads_per_sender=10,
        hard_bounce_limit=3,
        settings_snapshot={"segments": ["au_publishers", "travel_global", "travel_global"], **estimate},
    )
    session.add(launch)
    session.flush()
    definitions = [
        ("au_publishers", au, 1),
        ("travel_global", travel[:batch_size], 1),
        ("travel_global", travel[batch_size:], 2),
    ]
    jobs: list[Job] = []
    for index, ((segment, members, segment_number), sender) in enumerate(zip(definitions, senders, strict=True), start=1):
        label = "AU Publishers" if segment == "au_publishers" else f"Travel Global {segment_number}"
        batch, _ = CampaignRepository(session).reserve_batch(
            deterministic_key=f"{key}:{segment}:{segment_number}",
            name=f"LINK OS | {label} | Batch {index:04d} | {SEGMENTED_CONFIG_VERSION}",
            config_version=SEGMENTED_CONFIG_VERSION,
            members=members,
            contact_policy=PUBLIC_EVIDENCE_CONTACT_POLICY,
        )
        batch.launch_id = launch.id
        batch.segment = segment
        batch.sender_account_id = sender.id
        batch.active_assignment = True
        batch.settings_snapshot = {
            **(batch.settings_snapshot or {}),
            "pilot": True,
            "segmented_launch": True,
            "segment": segment,
            "contact_policy": PUBLIC_EVIDENCE_CONTACT_POLICY,
            "deliverability_verification": "skipped",
            "hard_bounce_limit": 3,
            "daily_new_leads": 10,
        }
        job, _ = JobQueue(session).enqueue(
            kind="prepare_campaign_batch",
            idempotency_key=f"prepare_campaign_launch:{launch.id}:{batch.id}",
            subject_type="campaign_batch",
            subject_id=batch.id,
            payload={
                "launch_id": launch.id, "batch_id": batch.id, "pilot": True,
                "target_size": batch_size, "contact_policy": PUBLIC_EVIDENCE_CONTACT_POLICY,
                "prepare_only": True, "confirm_verification_skipped": True,
                "requested_by": actor,
            },
            priority=35,
            max_attempts=5,
        )
        jobs.append(job)
    session.add(
        AuditLog(
            actor_type="user", actor_id=actor, action="campaign_launch.prepared",
            entity_type="campaign_launch", entity_id=launch.id,
            after={"campaigns": 3, "contacts": batch_size * 3, "verification_skipped": True},
        )
    )
    return launch, jobs
