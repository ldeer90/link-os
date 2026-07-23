from __future__ import annotations

import asyncio
import hmac
import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, case, exists, func, inspect, or_, select
from sqlalchemy.orm import Session

from deal_tracker.anchor_intent import classify_anchor_intent
from deal_tracker.auth import current_user, list_users
from deal_tracker.backlink_discovery import (
    DEFAULT_AUTHORITY_CEILING,
    BacklinkValidationError,
    apply_reviews,
    cancel_analysis,
    create_analysis,
    estimate_analysis,
    month_usage,
    monthly_cap,
    retry_analysis,
)
from deal_tracker.integrations.instantly import HISTORICAL_LINK_BUILDING_CAMPAIGN_IDS, InstantlyControl, matching_campaigns, sender_is_healthy
from deal_tracker.integrations.monday import DealsOnlyProjector
from deal_tracker.integrations.seranking import SERankingClient, SERankingError
from deal_tracker.inventory import ensure_private_listing
from deal_tracker.migration_importer import import_migration_records
from deal_tracker.platform.enums import BacklinkCandidateStatus, CampaignBatchStatus, CampaignLaunchStatus, JobStatus, LifecycleStage, ListingStatus, OfferStatus, ScrapeStatus, VerificationStatus
from deal_tracker.platform.models import (
    AuditLog,
    BacklinkAnalysisRun,
    BacklinkAnalysisTarget,
    BacklinkCandidate,
    BacklinkEvidence,
    BacklinkProviderRequest,
    CampaignBatch,
    CampaignLaunch,
    CampaignMember,
    CatalogueListing,
    Contact,
    Domain,
    DomainContactEvidence,
    DomainEvent,
    ImportBatch,
    ImportItem,
    InstantlyEvent,
    Job,
    MondayMapping,
    MigrationSourceRecord,
    Offer,
    Reply,
    ScrapeAttempt,
    SenderAccount,
    Suppression,
    SystemSetting,
    VerificationResult,
    utcnow,
)
from deal_tracker.platform.lifecycle import NEVER_AUTOMATICALLY_RECONTACT, is_refresh_eligible
from deal_tracker.platform.repositories import JobQueue, OUTREACH_PAUSE_KEY
from deal_tracker.platform_runtime import platform_configured, session_factory
from deal_tracker.campaign_sets import (
    campaign_set_estimate,
    prepare_campaign_set,
    retire_failed_public_evidence_pilot,
)


router = APIRouter(prefix="/api/v1", tags=["link-os-v1"])
DATA_DIR = Path(os.getenv("LINK_OS_DATA_DIR", Path(__file__).resolve().parents[1] / "data" / "platform"))


def _link_building_instantly_event() -> Any:
    managed_campaign_ids = select(CampaignBatch.external_campaign_id).where(
        CampaignBatch.external_campaign_id.is_not(None)
    )
    return or_(
        InstantlyEvent.external_campaign_id.in_(
            HISTORICAL_LINK_BUILDING_CAMPAIGN_IDS
        ),
        InstantlyEvent.external_campaign_id.in_(managed_campaign_ids),
        InstantlyEvent.payload["link_building_scope"].as_boolean().is_(True),
        InstantlyEvent.payload["managed"].as_boolean().is_(True),
    )


def _link_building_domain_scope() -> Any:
    return or_(
        exists(
            select(InstantlyEvent.id).where(
                InstantlyEvent.domain_id == Domain.id,
                InstantlyEvent.event_type == "campaign_membership_reconciled",
                _link_building_instantly_event(),
            )
        ),
        exists(
            select(DomainEvent.id).where(
                DomainEvent.domain_id == Domain.id,
                DomainEvent.source != "instantly_full_reconcile",
            )
        ),
        exists(select(ScrapeAttempt.id).where(ScrapeAttempt.domain_id == Domain.id)),
        exists(
            select(DomainContactEvidence.id).where(
                DomainContactEvidence.domain_id == Domain.id
            )
        ),
        exists(select(Reply.id).where(Reply.domain_id == Domain.id)),
        exists(select(Offer.id).where(Offer.domain_id == Domain.id)),
        exists(
            select(CatalogueListing.id).where(
                CatalogueListing.domain_id == Domain.id
            )
        ),
    )


def _json(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {str(key): _json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(item) for item in value]
    return value


def row_dict(row: Any, *, exclude: set[str] | None = None) -> dict[str, Any]:
    hidden = exclude or set()
    return {
        attribute.columns[0].name: _json(getattr(row, attribute.key))
        for attribute in inspect(row).mapper.column_attrs
        if attribute.columns[0].name not in hidden and attribute.key not in hidden
    }


def require_admin(request: Request) -> dict[str, Any]:
    if os.getenv("LINK_OS_API_AUTH_REQUIRED", "true").strip().lower() in {"0", "false", "no"}:
        return {"id": "local", "email": "local", "role": "admin"}
    expected_cli_token = os.getenv("LINK_OS_CLI_TOKEN", "").strip()
    supplied_cli_token = request.headers.get("x-link-os-cli-token", "").strip()
    if expected_cli_token and supplied_cli_token and hmac.compare_digest(expected_cli_token, supplied_cli_token):
        return {"id": "local-cli", "email": "local-cli", "role": "admin"}
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Admin login required")
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def db_session(_admin: dict[str, Any] = Depends(require_admin)) -> Any:
    if not platform_configured():
        raise HTTPException(status_code=503, detail="Canonical DATABASE_URL is not configured")
    factory = session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _pause_setting(session: Session) -> bool:
    setting = session.get(SystemSetting, OUTREACH_PAUSE_KEY)
    if setting is None:
        return True
    return setting.value.get("paused") is not False


def _set_pause(session: Session, *, paused: bool, actor: str, reason: str) -> dict[str, Any]:
    setting = session.get(SystemSetting, OUTREACH_PAUSE_KEY)
    before = setting.value if setting else {"paused": True}
    after = {"paused": paused, "reason": reason, "changed_at": utcnow().isoformat()}
    if setting is None:
        setting = SystemSetting(key=OUTREACH_PAUSE_KEY, value=after, updated_by=actor)
        session.add(setting)
    else:
        setting.value = after
        setting.version += 1
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
        )
    )
    session.flush()
    return after


def _actionable_dead_job_conditions(session: Session) -> tuple[Any, ...]:
    conditions: list[Any] = [
        Job.status == JobStatus.DEAD,
        Job.kind != "legacy_scrape_record",
    ]
    latest_monday_success = session.scalar(
        select(func.max(Job.completed_at)).where(
            Job.kind == "reconcile_monday",
            Job.status == JobStatus.SUCCEEDED,
        )
    )
    if latest_monday_success is not None:
        conditions.append(
            or_(
                Job.kind != "reconcile_monday",
                Job.updated_at > latest_monday_success,
            )
        )
    return tuple(conditions)


def _health_payload(session: Session) -> dict[str, Any]:
    senders = list(session.scalars(select(SenderAccount).order_by(SenderAccount.sender_email)))
    healthy = [sender for sender in senders if sender.connected and sender.provider_status == 1 and not sender.last_error]
    operational_dead_jobs = session.scalar(
        select(func.count(Job.id)).where(*_actionable_dead_job_conditions(session))
    ) or 0
    total_dead_jobs = session.scalar(
        select(func.count(Job.id)).where(Job.status == JobStatus.DEAD)
    ) or 0
    historical_dead_jobs = int(total_dead_jobs) - int(operational_dead_jobs)
    queued_jobs = session.scalar(select(func.count(Job.id)).where(Job.status == JobStatus.QUEUED)) or 0
    migration_record_count = session.scalar(select(func.count(MigrationSourceRecord.id))) or 0
    migration_run_count = session.scalar(
        select(func.count(func.distinct(MigrationSourceRecord.migration_id)))
    ) or 0
    paused = _pause_setting(session)
    mode = os.getenv("LINK_OS_MODE", "shadow")
    pause_setting = session.get(SystemSetting, OUTREACH_PAUSE_KEY)
    full_reconcile = session.get(SystemSetting, "instantly.full_reconcile_completed")
    legacy_pause = session.get(SystemSetting, "instantly.legacy_campaigns_paused")
    batches = list(session.scalars(select(CampaignBatch)))
    blocked_batches = [
        batch
        for batch in batches
        if batch.status
        in {
            CampaignBatchStatus.RESERVED,
            CampaignBatchStatus.UPLOADING,
            CampaignBatchStatus.PAUSED,
            CampaignBatchStatus.ACTIVE,
        }
        and (
            batch.pending_verification_count > 0
            or batch.uploaded_member_count != batch.expected_member_count
            or batch.readback_member_count != batch.expected_member_count
        )
    ]
    locally_isolated_sender_ids = {
        batch.sender_account_id
        for batch in batches
        if batch.sender_account_id
        and (batch.launch_id is not None or batch.status == CampaignBatchStatus.FAILED)
    }
    unsafe_senders = [
        sender
        for sender in senders
        if sender.id not in locally_isolated_sender_ids
        if sender.bounce_protection_triggered
        or (
            sender.rolling_bounce_rate is not None
            and sender.rolling_bounce_rate >= 0.03
        )
        or (
            sender.rolling_unsubscribe_rate is not None
            and sender.rolling_unsubscribe_rate >= 0.01
        )
    ]
    sender_items = [
        {
            "id": sender.id,
            "email": sender.sender_email or "Unknown sender",
            "status": "healthy" if sender in healthy else "error",
            "provider_status": sender.provider_status,
            "connected": sender.connected,
            "healthy": sender in healthy,
            "healthy_since": _json(sender.healthy_since),
            "last_checked_at": _json(sender.last_checked_at),
            "blocking_reason": sender.last_error,
            "sent_today": sender.total_emails_sent_today,
            "daily_limit": 30,
            "bounce_rate": sender.rolling_bounce_rate,
            "unsubscribe_rate": sender.rolling_unsubscribe_rate,
        }
        for sender in senders
    ]
    blockers = [
        reason
        for condition, reason in (
            (mode != "live", "shadow_mode_enabled"),
            (paused, "global_outreach_pause_enabled"),
            (not healthy, "no_healthy_sender"),
            (
                full_reconcile is None
                or full_reconcile.value.get("complete") is not True,
                "full_instantly_reconciliation_required",
            ),
            (
                legacy_pause is None or legacy_pause.value.get("complete") is not True,
                "legacy_campaign_pause_confirmation_required",
            ),
            (bool(blocked_batches), "campaign_upload_or_verification_blocked"),
            (bool(unsafe_senders), "sender_safety_threshold_blocked"),
        )
        if condition
    ]
    safe_to_resume = not [
        reason for reason in blockers if reason != "global_outreach_pause_enabled"
    ]
    seranking_setting = session.get(SystemSetting, "seranking.last_subscription_check")
    seranking_configured = bool(os.getenv("SE_RANKING_API_KEY", "").strip())
    seranking_value = seranking_setting.value if seranking_setting else {}
    return {
        "status": "degraded" if not healthy else "ok",
        "mode": mode,
        "shadow_mode": mode != "live",
        "database": {"name": "PostgreSQL", "status": "ok", "message": "Authoritative operational database"},
        "worker": {
            "name": "Background worker",
            "status": "degraded" if operational_dead_jobs else "ok",
            "message": f"{int(queued_jobs)} queued · {int(operational_dead_jobs)} actionable failures",
        },
        "outreach_paused": paused,
        "pause_reason": (pause_setting.value.get("reason") if pause_setting else "Fail-closed default"),
        "senders": sender_items,
        "sender_summary": {
            "total": len(senders),
            "healthy": len(healthy),
            "unhealthy": len(senders) - len(healthy),
        },
        "jobs": {
            "queued": int(queued_jobs),
            "failed": int(operational_dead_jobs),
            "historical_failed": int(historical_dead_jobs),
        },
        "migration": {
            "complete": bool(migration_record_count),
            "record_count": int(migration_record_count),
            "run_count": int(migration_run_count),
            "recovery_sources_mounted": False,
        },
        "activation_allowed": safe_to_resume and not paused,
        "blocking_reasons": blockers,
        "outreach": {"paused": paused, "safe_to_resume": safe_to_resume, "blocking_reasons": blockers},
        "integrations": [
            {
                "name": "Instantly",
                "status": "healthy" if healthy else "error",
                "message": f"{len(healthy)} of {len(senders)} sending accounts healthy",
                "checked_at": _json(max((sender.last_checked_at for sender in senders if sender.last_checked_at), default=None)),
                "writable": safe_to_resume,
            },
            {
                "name": "Monday",
                "status": "configured" if os.getenv("MONDAY_API_KEY") else "not_configured",
                "message": "Deals-only projection" if os.getenv("MONDAY_API_KEY") else "API key not configured in this runtime",
                "writable": bool(os.getenv("MONDAY_API_KEY")),
            },
            {
                "name": "SE Ranking",
                "status": "configured" if seranking_configured else "not_configured",
                "message": (
                    f"Backlink discovery ready · {month_usage(session)}/{monthly_cap()} monthly credits used"
                    if seranking_configured
                    else "API key not configured in this runtime"
                ),
                "checked_at": seranking_value.get("checked_at"),
                "balance": seranking_value.get("balance"),
                "writable": seranking_configured,
            },
            {"name": "BigQuery replica", "status": "optional", "message": "Aggregate analytics only", "writable": False},
        ],
        "checked_at": utcnow().isoformat(),
    }


@router.get("/health")
@router.get("/system/health")
def health(session: Session = Depends(db_session)) -> dict[str, Any]:
    return _health_payload(session)


@router.get("/integrations/seranking/health")
def seranking_health(
    refresh: bool = False,
    session: Session = Depends(db_session),
) -> dict[str, Any]:
    client = SERankingClient()
    setting = session.get(SystemSetting, "seranking.last_subscription_check")
    value = dict(setting.value) if setting else {}
    error = None
    if refresh and client.configured:
        try:
            subscription = client.subscription()
            value = {
                "balance": subscription.balance,
                "status": "ok",
                "checked_at": utcnow().isoformat(),
                "plan": subscription.raw_plan,
            }
            if setting is None:
                setting = SystemSetting(key="seranking.last_subscription_check", value=value, updated_by="health_check")
                session.add(setting)
            else:
                setting.value = value
                setting.version += 1
                setting.updated_by = "health_check"
        except SERankingError as exc:
            error = exc.code
    used = month_usage(session)
    cap = monthly_cap()
    return {
        "configured": client.configured,
        "status": "ok" if client.configured and not error else (error or "not_configured"),
        "balance": value.get("balance"),
        "plan": value.get("plan"),
        "checked_at": value.get("checked_at"),
        "monthly_usage": used,
        "monthly_cap": cap,
        "monthly_remaining": max(0, cap - used),
        "default_run_cap": 500,
        "default_authority_ceiling": DEFAULT_AUTHORITY_CEILING,
        "hard_run_cap": 2500,
        "cache_days": max(1, int(os.getenv("SE_RANKING_CACHE_DAYS", "30"))),
        "credentials_exposed": False,
    }


@router.post("/backlink-analyses/estimate")
async def backlink_analysis_estimate(
    request: Request,
    session: Session = Depends(db_session),
) -> dict[str, Any]:
    payload = await request.json()
    try:
        return estimate_analysis(
            session,
            mode=str(payload.get("mode") or ""),
            client_domain=str(payload.get("client_domain") or payload.get("client") or ""),
            competitor_domains=payload.get("competitor_domains") or payload.get("competitors") or [],
            credit_cap=int(payload.get("credit_cap") or 500),
            authority_floor=(int(payload["authority_floor"]) if payload.get("authority_floor") is not None else 20),
            authority_ceiling=(int(payload["authority_ceiling"]) if payload.get("authority_ceiling") is not None else DEFAULT_AUTHORITY_CEILING),
            source_url_filter=str(payload.get("source_url_filter") or "") or None,
            refresh_balance=payload.get("refresh_balance", True) is not False,
        )
    except (BacklinkValidationError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/backlink-analyses", status_code=202)
async def create_backlink_analysis(
    request: Request,
    session: Session = Depends(db_session),
    admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    payload = await request.json()
    actor = str(admin.get("email") or admin.get("id"))
    try:
        run = create_analysis(
            session,
            mode=str(payload.get("mode") or ""),
            client_domain=str(payload.get("client_domain") or payload.get("client") or ""),
            competitor_domains=payload.get("competitor_domains") or payload.get("competitors") or [],
            credit_cap=int(payload.get("credit_cap") or 500),
            confirmed_credit_cap=(int(payload["confirmed_credit_cap"]) if payload.get("confirmed_credit_cap") is not None else None),
            authority_floor=(int(payload["authority_floor"]) if payload.get("authority_floor") is not None else 20),
            authority_ceiling=(int(payload["authority_ceiling"]) if payload.get("authority_ceiling") is not None else DEFAULT_AUTHORITY_CEILING),
            source_url_filter=str(payload.get("source_url_filter") or "") or None,
            actor=actor,
            source="cli" if actor == "local-cli" else "console",
        )
    except BacklinkValidationError as exc:
        status_code = 409 if any(code in str(exc) for code in ("monthly_credit", "insufficient", "not_configured")) else 422
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    return {"id": run.id, "status": run.status.value, "credit_cap": run.credit_cap, "estimated_credits": run.estimated_credits}


@router.get("/backlink-analyses")
def backlink_analyses(
    status: str = "",
    mode: str = "",
    limit: int = 50,
    offset: int = 0,
    session: Session = Depends(db_session),
) -> dict[str, Any]:
    statement = select(BacklinkAnalysisRun)
    if status.strip():
        statement = statement.where(BacklinkAnalysisRun.status == status.strip())
    if mode.strip():
        statement = statement.where(BacklinkAnalysisRun.mode == mode.strip())
    total = int(session.scalar(select(func.count()).select_from(statement.subquery())) or 0)
    rows = list(session.scalars(statement.order_by(BacklinkAnalysisRun.created_at.desc()).offset(max(0, offset)).limit(max(1, min(limit, 250)))))
    return {"items": [row_dict(row) for row in rows], "count": total, "offset": max(0, offset)}


def _backlink_run_payload(session: Session, run: BacklinkAnalysisRun) -> dict[str, Any]:
    targets = list(session.scalars(select(BacklinkAnalysisTarget).where(BacklinkAnalysisTarget.run_id == run.id).order_by(BacklinkAnalysisTarget.target_domain)))
    requests = list(session.scalars(select(BacklinkProviderRequest).where(BacklinkProviderRequest.run_id == run.id).order_by(BacklinkProviderRequest.started_at.desc())))
    return {
        **row_dict(run),
        "targets": [
            {
                **row_dict(target),
                "coverage_percent": (
                    round(min(100.0, target.returned_count * 100 / target.referring_domain_count), 1)
                    if target.referring_domain_count
                    else 0.0
                ),
            }
            for target in targets
        ],
        "provider_requests": [row_dict(item) for item in requests],
    }


@router.get("/backlink-analyses/{analysis_id}")
def backlink_analysis_detail(analysis_id: str, session: Session = Depends(db_session)) -> dict[str, Any]:
    run = session.get(BacklinkAnalysisRun, analysis_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Backlink analysis not found")
    return _backlink_run_payload(session, run)


@router.get("/backlink-analyses/{analysis_id}/candidates")
def backlink_candidates(
    analysis_id: str,
    status: str = "",
    search: str = "",
    competitor: str = "",
    link_type: str = "",
    anchor_intent: str = "",
    min_authority: int = 0,
    email_status: str = "",
    crawl_status: str = "",
    limit: int = 100,
    offset: int = 0,
    session: Session = Depends(db_session),
) -> dict[str, Any]:
    run = session.get(BacklinkAnalysisRun, analysis_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Backlink analysis not found")
    statement = select(BacklinkCandidate).where(BacklinkCandidate.run_id == run.id)
    if status.strip():
        statement = statement.where(BacklinkCandidate.status == status.strip())
    if search.strip():
        pattern = f"%{search.strip()}%"
        matching_evidence = select(BacklinkEvidence.id).where(
            BacklinkEvidence.run_id == run.id,
            BacklinkEvidence.source_domain == BacklinkCandidate.normalized_domain,
            or_(
                BacklinkEvidence.source_url.ilike(pattern),
                BacklinkEvidence.page_title.ilike(pattern),
                BacklinkEvidence.anchor_text.ilike(pattern),
                BacklinkEvidence.target_url.ilike(pattern),
            ),
        )
        statement = statement.where(
            or_(
                BacklinkCandidate.normalized_domain.ilike(pattern),
                exists(matching_evidence),
            )
        )
    competitor_value = competitor.strip().lower()
    if competitor_value:
        competitor_evidence = (
            select(BacklinkEvidence.id)
            .join(
                BacklinkAnalysisTarget,
                BacklinkAnalysisTarget.id == BacklinkEvidence.target_id,
            )
            .where(
                BacklinkEvidence.run_id == run.id,
                BacklinkEvidence.source_domain == BacklinkCandidate.normalized_domain,
                BacklinkAnalysisTarget.target_domain == competitor_value,
            )
        )
        statement = statement.where(exists(competitor_evidence))
    link_type_value = link_type.strip().lower()
    if link_type_value not in {"", "follow", "nofollow"}:
        raise HTTPException(status_code=422, detail="link_type must be follow or nofollow")
    if link_type_value:
        matching_link = select(BacklinkEvidence.id).where(
            BacklinkEvidence.run_id == run.id,
            BacklinkEvidence.source_domain == BacklinkCandidate.normalized_domain,
            BacklinkEvidence.nofollow.is_(link_type_value == "nofollow"),
        )
        statement = statement.where(exists(matching_link))
    anchor_intent_value = anchor_intent.strip().lower()
    anchor_classes = {
        "commercial": ("commercial", "commercial_likely"),
        "strong_commercial": ("commercial",),
        "likely_commercial": ("commercial_likely",),
        "brand": ("brand",),
        "editorial": ("editorial",),
        "generic": ("generic", "naked_url"),
        "empty": ("empty",),
        "unknown": ("unknown", "other"),
    }
    if anchor_intent_value:
        if anchor_intent_value not in anchor_classes:
            raise HTTPException(status_code=422, detail="anchor_intent is not a valid anchor classification")
        statement = statement.where(
            BacklinkCandidate.commercial_anchor_class.in_(anchor_classes[anchor_intent_value])
        )
    authority_value = max(0, min(int(min_authority), 100))
    if authority_value:
        statement = statement.where(
            BacklinkCandidate.highest_domain_inlink_rank >= authority_value
        )
    public_email = select(DomainContactEvidence.id).where(
        DomainContactEvidence.domain_id == BacklinkCandidate.existing_domain_id,
        DomainContactEvidence.is_public_page.is_(True),
    )
    email_status_value = email_status.strip().lower()
    if email_status_value == "found":
        statement = statement.where(exists(public_email))
    elif email_status_value == "not_found":
        statement = statement.where(
            BacklinkCandidate.existing_domain_id.is_not(None), ~exists(public_email)
        )
    elif email_status_value == "not_canonical":
        statement = statement.where(BacklinkCandidate.existing_domain_id.is_(None))
    elif email_status_value:
        raise HTTPException(
            status_code=422,
            detail="email_status must be found, not_found or not_canonical",
        )
    crawl_status_value = crawl_status.strip().lower()
    if crawl_status_value:
        valid_lifecycle = {item.value for item in LifecycleStage}
        if crawl_status_value not in valid_lifecycle:
            raise HTTPException(status_code=422, detail="crawl_status is not a valid lifecycle stage")
        matching_domain = select(Domain.id).where(
            Domain.id == BacklinkCandidate.existing_domain_id,
            Domain.lifecycle_stage == LifecycleStage(crawl_status_value),
        )
        statement = statement.where(exists(matching_domain))
    total = int(session.scalar(select(func.count()).select_from(statement.subquery())) or 0)
    rows = list(session.scalars(statement.order_by(BacklinkCandidate.highest_domain_inlink_rank.desc().nullslast(), BacklinkCandidate.normalized_domain).offset(max(0, offset)).limit(max(1, min(limit, 500)))))
    target_domains = dict(
        session.execute(
            select(BacklinkAnalysisTarget.id, BacklinkAnalysisTarget.target_domain).where(
                BacklinkAnalysisTarget.run_id == run.id
            )
        ).all()
    )
    items = []
    for candidate in rows:
        evidence = list(session.scalars(select(BacklinkEvidence).where(BacklinkEvidence.run_id == run.id, BacklinkEvidence.source_domain == candidate.normalized_domain).order_by(BacklinkEvidence.domain_inlink_rank.desc().nullslast()).limit(10)))
        evidence_payload = []
        for item in evidence:
            anchor_assessment = classify_anchor_intent(
                item.anchor_text,
                target_url=item.target_url,
                nofollow=item.nofollow,
                image_link=item.image_link,
            )
            evidence_payload.append(
                {
                    **row_dict(item, exclude={"provider_metadata"}),
                    "competitor_domain": target_domains.get(item.target_id),
                    **anchor_assessment.as_dict(),
                }
            )
        domain = session.get(Domain, candidate.existing_domain_id) if candidate.existing_domain_id else None
        public_email_count = 0
        if domain is not None:
            public_email_count = int(
                session.scalar(
                    select(func.count(func.distinct(DomainContactEvidence.contact_id))).where(
                        DomainContactEvidence.domain_id == domain.id,
                        DomainContactEvidence.is_public_page.is_(True),
                    )
                )
                or 0
            )
        items.append(
            {
                **row_dict(candidate),
                "domain": candidate.normalized_domain,
                "competitor_domains": sorted(
                    {
                        target_domains[item.target_id]
                        for item in evidence
                        if item.target_id in target_domains
                    }
                ),
                "crawl_status": domain.lifecycle_stage.value if domain else None,
                "public_email_count": public_email_count,
                "evidence": evidence_payload,
            }
        )
    return {
        "items": items,
        "count": total,
        "offset": max(0, offset),
        "run_status": run.status.value,
        "available_competitors": sorted(target_domains.values()),
    }


@router.post("/backlink-analyses/{analysis_id}/reviews")
async def review_backlink_candidates(
    analysis_id: str,
    request: Request,
    session: Session = Depends(db_session),
    admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    payload = await request.json()
    decisions = payload.get("decisions")
    if not isinstance(decisions, list) or not decisions:
        raise HTTPException(status_code=422, detail="decisions must be a non-empty list")
    actor = str(admin.get("email") or admin.get("id"))
    try:
        return apply_reviews(session, run_id=analysis_id, decisions=decisions, actor_type="codex" if actor == "local-cli" else "user", actor_id=actor)
    except BacklinkValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/backlink-analyses/{analysis_id}/cancel")
async def cancel_backlink_analysis(
    analysis_id: str,
    request: Request,
    session: Session = Depends(db_session),
    admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    run = session.get(BacklinkAnalysisRun, analysis_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Backlink analysis not found")
    try:
        cancel_analysis(session, run, actor=str(admin.get("email") or admin.get("id")))
    except BacklinkValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"id": run.id, "status": run.status.value, "cancel_requested": bool(run.counters.get("cancel_requested"))}


@router.post("/backlink-analyses/{analysis_id}/retry", status_code=202)
async def retry_backlink_analysis(
    analysis_id: str,
    request: Request,
    session: Session = Depends(db_session),
    admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    run = session.get(BacklinkAnalysisRun, analysis_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Backlink analysis not found")
    payload = await request.json()
    try:
        job = retry_analysis(session, run, actor=str(admin.get("email") or admin.get("id")), acknowledge_unknown_credit=payload.get("acknowledge_unknown_credit") is True)
    except BacklinkValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"id": run.id, "status": run.status.value, "job_id": job.id}


def _outreach_evidence_summary(
    session: Session,
    *,
    australian_only: bool = False,
) -> dict[str, Any]:
    """Return mutually exclusive outreach evidence tiers from canonical records.

    Historical campaign membership remains a permanent dedupe signal, but it is
    not treated as proof of a send. Stronger evidence wins in this order:
    listing, offer/deal, reply, provider contact timestamp, membership only.
    """

    provider_contact = case(
        (
            _instantly_delivery_signal(),
            1,
        ),
        else_=0,
    )
    memberships = (
        select(
            InstantlyEvent.domain_id.label("domain_id"),
            func.max(provider_contact).label("provider_contact"),
        )
        .where(
            InstantlyEvent.event_type == "campaign_membership_reconciled",
            InstantlyEvent.domain_id.is_not(None),
            _link_building_instantly_event(),
        )
        .group_by(InstantlyEvent.domain_id)
        .subquery()
    )
    sent_event_domains = (
        select(InstantlyEvent.domain_id.label("domain_id"))
        .where(
            InstantlyEvent.event_type == "email_sent",
            InstantlyEvent.domain_id.is_not(None),
            _link_building_instantly_event(),
        )
        .distinct()
        .subquery()
    )
    reply_domains = (
        select(Reply.domain_id.label("domain_id"))
        .where(Reply.domain_id.is_not(None))
        .distinct()
        .subquery()
    )
    offer_domains = select(Offer.domain_id.label("domain_id")).distinct().subquery()
    listed_domains = (
        select(CatalogueListing.domain_id.label("domain_id"))
        .where(CatalogueListing.status == ListingStatus.LISTED)
        .distinct()
        .subquery()
    )
    tier = case(
        (listed_domains.c.domain_id.is_not(None), "listed"),
        (offer_domains.c.domain_id.is_not(None), "offer_or_deal"),
        (reply_domains.c.domain_id.is_not(None), "replied"),
        (
            and_(
                or_(
                    memberships.c.provider_contact == 1,
                    sent_event_domains.c.domain_id.is_not(None),
                ),
                or_(
                    memberships.c.domain_id.is_not(None),
                    sent_event_domains.c.domain_id.is_not(None),
                ),
            ),
            "provider_contact_confirmed",
        ),
        (
            memberships.c.domain_id.is_not(None),
            "historical_membership_unverified",
        ),
        else_="no_outreach_evidence",
    )
    base = (
        select(tier.label("tier"), func.count(Domain.id).label("count"))
        .select_from(Domain)
        .outerjoin(memberships, memberships.c.domain_id == Domain.id)
        .outerjoin(sent_event_domains, sent_event_domains.c.domain_id == Domain.id)
        .outerjoin(reply_domains, reply_domains.c.domain_id == Domain.id)
        .outerjoin(offer_domains, offer_domains.c.domain_id == Domain.id)
        .outerjoin(listed_domains, listed_domains.c.domain_id == Domain.id)
    )
    domain_filter = Domain.normalized_domain.like("%.com.au")
    if australian_only:
        base = base.where(domain_filter)
    base = base.where(_link_building_domain_scope())
    tiers = {
        str(name): int(count)
        for name, count in session.execute(base.group_by(tier))
    }
    for name in (
        "listed",
        "offer_or_deal",
        "replied",
        "provider_contact_confirmed",
        "historical_membership_unverified",
        "no_outreach_evidence",
    ):
        tiers.setdefault(name, 0)

    def distinct_domains(statement: Any) -> int:
        query = select(func.count()).select_from(statement.subquery())
        return int(session.scalar(query) or 0)

    membership_query = select(memberships.c.domain_id).join(
        Domain, Domain.id == memberships.c.domain_id
    )
    confirmed_query = (
        select(Domain.id)
        .select_from(Domain)
        .outerjoin(memberships, memberships.c.domain_id == Domain.id)
        .outerjoin(sent_event_domains, sent_event_domains.c.domain_id == Domain.id)
        .where(
            or_(
                memberships.c.provider_contact == 1,
                sent_event_domains.c.domain_id.is_not(None),
            )
        )
    )
    replies_query = select(reply_domains.c.domain_id).join(
        Domain, Domain.id == reply_domains.c.domain_id
    )
    offers_query = select(offer_domains.c.domain_id).join(
        Domain, Domain.id == offer_domains.c.domain_id
    )
    listings_query = select(listed_domains.c.domain_id).join(
        Domain, Domain.id == listed_domains.c.domain_id
    )
    protected_query = (
        select(Suppression.domain_id)
        .join(Domain, Domain.id == Suppression.domain_id)
        .where(
            Suppression.active.is_(True),
            Suppression.domain_id.is_not(None),
            _link_building_domain_scope(),
        )
        .distinct()
    )
    if australian_only:
        membership_query = membership_query.where(domain_filter)
        confirmed_query = confirmed_query.where(domain_filter)
        replies_query = replies_query.where(domain_filter)
        offers_query = offers_query.where(domain_filter)
        listings_query = listings_query.where(domain_filter)
        protected_query = protected_query.where(domain_filter)

    evidence_backed = sum(
        tiers[name]
        for name in ("provider_contact_confirmed", "replied", "offer_or_deal", "listed")
    )
    workspace_memberships = select(InstantlyEvent.domain_id).where(
        InstantlyEvent.event_type == "campaign_membership_reconciled",
        InstantlyEvent.domain_id.is_not(None),
    ).distinct()
    unrelated_only_memberships = select(Domain.id).where(
        exists(
            select(InstantlyEvent.id).where(
                InstantlyEvent.domain_id == Domain.id,
                InstantlyEvent.event_type == "campaign_membership_reconciled",
            )
        ),
        ~exists(
            select(InstantlyEvent.id).where(
                InstantlyEvent.domain_id == Domain.id,
                InstantlyEvent.event_type == "campaign_membership_reconciled",
                _link_building_instantly_event(),
            )
        ),
    )
    if australian_only:
        workspace_memberships = workspace_memberships.join(
            Domain, Domain.id == InstantlyEvent.domain_id
        ).where(domain_filter)
        unrelated_only_memberships = unrelated_only_memberships.where(domain_filter)
    return {
        "scope": "link_building_com.au" if australian_only else "link_building",
        "total_domains": sum(tiers.values()),
        "historical_memberships": distinct_domains(membership_query),
        "contact_confirmed": distinct_domains(confirmed_query),
        "membership_only": tiers["historical_membership_unverified"],
        "instantly_uploaded": distinct_domains(membership_query),
        "sent_confirmed": distinct_domains(confirmed_query),
        "uploaded_no_send_record": tiers["historical_membership_unverified"],
        "evidence_backed_engaged": evidence_backed,
        "reply_domains": distinct_domains(replies_query),
        "offer_domains": distinct_domains(offers_query),
        "listed_domains": distinct_domains(listings_query),
        "protected_domains": distinct_domains(protected_query),
        "workspace_instantly_uploaded": distinct_domains(workspace_memberships),
        "excluded_unrelated_campaign_domains": distinct_domains(unrelated_only_memberships),
        "link_building_campaigns": int(
            session.scalar(
                select(func.count(func.distinct(InstantlyEvent.external_campaign_id))).where(
                    InstantlyEvent.event_type == "campaign_membership_reconciled",
                    _link_building_instantly_event(),
                )
            )
            or 0
        ),
        "workspace_campaigns": int(
            session.scalar(
                select(func.count(func.distinct(InstantlyEvent.external_campaign_id))).where(
                    InstantlyEvent.event_type == "campaign_membership_reconciled"
                )
            )
            or 0
        ),
        "tiers": tiers,
    }


def _instantly_delivery_signal() -> Any:
    return or_(
        func.nullif(InstantlyEvent.payload["timestamp_last_contact"].as_string(), "").is_not(None),
        func.nullif(InstantlyEvent.payload["timestamp_last_open"].as_string(), "").is_not(None),
        func.nullif(InstantlyEvent.payload["timestamp_last_reply"].as_string(), "").is_not(None),
        func.nullif(InstantlyEvent.payload["timestamp_last_click"].as_string(), "").is_not(None),
        and_(
            InstantlyEvent.payload["last_step_from"].as_string() == "campaign",
            func.nullif(InstantlyEvent.payload["last_step_timestamp_executed"].as_string(), "").is_not(None),
        ),
        func.coalesce(InstantlyEvent.payload["email_open_count"].as_integer(), 0) > 0,
        func.coalesce(InstantlyEvent.payload["email_reply_count"].as_integer(), 0) > 0,
        func.coalesce(InstantlyEvent.payload["email_click_count"].as_integer(), 0) > 0,
        InstantlyEvent.payload["status"].as_integer().in_((3, -1, -2)),
    )


def _payload_confirms_send(payload: dict[str, Any]) -> bool:
    timestamp_fields = (
        "timestamp_last_contact",
        "timestamp_last_open",
        "timestamp_last_reply",
        "timestamp_last_click",
    )
    if any(str(payload.get(field) or "").strip() for field in timestamp_fields):
        return True
    if (
        payload.get("last_step_from") == "campaign"
        and str(payload.get("last_step_timestamp_executed") or "").strip()
    ):
        return True
    if any(int(payload.get(field) or 0) > 0 for field in ("email_open_count", "email_reply_count", "email_click_count")):
        return True
    try:
        return int(payload.get("status")) in {3, -1, -2}
    except (TypeError, ValueError):
        return False


def _outreach_evidence_predicates() -> dict[str, Any]:
    uploaded = exists(
        select(InstantlyEvent.id).where(
            InstantlyEvent.domain_id == Domain.id,
            InstantlyEvent.event_type == "campaign_membership_reconciled",
            _link_building_instantly_event(),
        )
    )
    sent = or_(
        exists(
            select(InstantlyEvent.id).where(
                InstantlyEvent.domain_id == Domain.id,
                InstantlyEvent.event_type == "campaign_membership_reconciled",
                _link_building_instantly_event(),
                _instantly_delivery_signal(),
            )
        ),
        exists(
            select(InstantlyEvent.id).where(
                InstantlyEvent.domain_id == Domain.id,
                InstantlyEvent.event_type == "email_sent",
                _link_building_instantly_event(),
            )
        ),
    )
    replied = exists(select(Reply.id).where(Reply.domain_id == Domain.id))
    offer = exists(select(Offer.id).where(Offer.domain_id == Domain.id))
    listed = exists(
        select(CatalogueListing.id).where(
            CatalogueListing.domain_id == Domain.id,
            CatalogueListing.status == ListingStatus.LISTED,
        )
    )
    return {
        "instantly_uploaded": uploaded,
        "sent_confirmed": sent,
        "uploaded_no_send_record": and_(uploaded, ~sent, ~replied, ~offer, ~listed),
        "replied": replied,
        "offer_or_deal": offer,
        "listed": listed,
        "engagement_confirmed": or_(sent, replied, offer, listed),
        "no_outreach_evidence": and_(~uploaded, ~sent, ~replied, ~offer, ~listed),
    }


def _historical_funnel_predicates() -> dict[str, Any]:
    outreach = _outreach_evidence_predicates()
    link_building_scope = _link_building_domain_scope()
    public_contact = exists(
        select(DomainContactEvidence.id).where(
            DomainContactEvidence.domain_id == Domain.id,
            DomainContactEvidence.is_public_page.is_(True),
        )
    )
    verified_contact = exists(
        select(VerificationResult.id)
        .select_from(VerificationResult)
        .join(
            DomainContactEvidence,
            DomainContactEvidence.contact_id == VerificationResult.contact_id,
        )
        .where(
            DomainContactEvidence.domain_id == Domain.id,
            DomainContactEvidence.is_public_page.is_(True),
            VerificationResult.status == VerificationStatus.VERIFIED,
            VerificationResult.catch_all.is_(False),
        )
    )
    scoped_instantly_contact = exists(
        select(InstantlyEvent.id).where(
            InstantlyEvent.domain_id == Domain.id,
            InstantlyEvent.contact_id.is_not(None),
            InstantlyEvent.event_type == "campaign_membership_reconciled",
            _link_building_instantly_event(),
        )
    )
    # Historical sources did not always retain every intermediate event. For the
    # visual funnel, a later milestone therefore implies passage through the
    # earlier stages while the strict evidence filters remain separately usable.
    listed = outreach["listed"]
    offer = or_(outreach["offer_or_deal"], listed)
    replied = or_(outreach["replied"], offer)
    # Upload and send remain provider-backed facts. Only discovery/contact capture
    # use the agreed historical assumption for legacy link-building leads.
    sent = outreach["sent_confirmed"]
    uploaded = outreach["instantly_uploaded"]
    contact_captured = and_(
        link_building_scope,
        or_(public_contact, scoped_instantly_contact, uploaded),
    )
    scrape_processed = and_(
        link_building_scope,
        or_(
            exists(select(ScrapeAttempt.id).where(ScrapeAttempt.domain_id == Domain.id)),
            contact_captured,
        ),
    )
    return {
        "entered_system": link_building_scope,
        "scrape_attempted": scrape_processed,
        "contact_identity_captured": contact_captured,
        "public_contact_captured": and_(link_building_scope, public_contact),
        "verified_contact": and_(link_building_scope, verified_contact),
        "instantly_uploaded": and_(link_building_scope, uploaded),
        "sent_confirmed": and_(link_building_scope, sent),
        "replied": and_(link_building_scope, replied),
        "offer_recorded": and_(link_building_scope, offer),
        "listed": and_(link_building_scope, listed),
    }


def _historical_funnel_summary(session: Session) -> dict[str, int]:
    return {
        name: int(
            session.scalar(select(func.count(Domain.id)).where(predicate)) or 0
        )
        for name, predicate in _historical_funnel_predicates().items()
    }


def _current_state_summary(session: Session) -> dict[str, int]:
    counts = {
        str(stage): int(count)
        for stage, count in session.execute(
            select(Domain.lifecycle_stage, func.count(Domain.id))
            .where(_link_building_domain_scope())
            .group_by(Domain.lifecycle_stage)
        )
    }
    for stage in LifecycleStage:
        counts.setdefault(stage.value, 0)
    return counts


def _domain_outreach_evidence_map(
    session: Session,
    domain_ids: list[str],
) -> dict[str, dict[str, Any]]:
    state = {
        domain_id: {
            "uploaded": False,
            "sent": False,
            "replied": False,
            "offer": False,
            "listed": False,
            "latest_at": None,
            "suppression_reasons": [],
            "active_campaign_reservation": False,
            "terminal_lifecycle": False,
            "refresh_eligible": False,
        }
        for domain_id in domain_ids
    }
    if not domain_ids:
        return {}

    for domain in session.scalars(select(Domain).where(Domain.id.in_(domain_ids))):
        state[domain.id]["terminal_lifecycle"] = domain.lifecycle_stage in NEVER_AUTOMATICALLY_RECONTACT
        state[domain.id]["refresh_eligible"] = is_refresh_eligible(
            domain.lifecycle_stage,
            last_attempt_at=domain.last_scraped_at,
        )

    def note(domain_id: str, happened_at: datetime | None) -> None:
        if happened_at is None:
            return
        current = state[domain_id]["latest_at"]
        if current is None or happened_at > current:
            state[domain_id]["latest_at"] = happened_at

    for event in session.scalars(
        select(InstantlyEvent).where(
            InstantlyEvent.domain_id.in_(domain_ids),
            InstantlyEvent.event_type.in_(("campaign_membership_reconciled", "email_sent")),
        )
    ):
        if not event.domain_id:
            continue
        if event.event_type == "campaign_membership_reconciled":
            state[event.domain_id]["uploaded"] = True
            if _payload_confirms_send(event.payload or {}):
                state[event.domain_id]["sent"] = True
                note(event.domain_id, event.event_at)
        else:
            state[event.domain_id]["sent"] = True
            note(event.domain_id, event.event_at)
    for reply in session.scalars(select(Reply).where(Reply.domain_id.in_(domain_ids))):
        if reply.domain_id:
            state[reply.domain_id]["replied"] = True
            note(reply.domain_id, reply.received_at)
    for offer in session.scalars(select(Offer).where(Offer.domain_id.in_(domain_ids))):
        state[offer.domain_id]["offer"] = True
        note(offer.domain_id, offer.created_at)
    for listing in session.scalars(
        select(CatalogueListing).where(
            CatalogueListing.domain_id.in_(domain_ids),
            CatalogueListing.status == ListingStatus.LISTED,
        )
    ):
        state[listing.domain_id]["listed"] = True
        note(listing.domain_id, listing.listed_at or listing.updated_at)
    for suppression in session.scalars(
        select(Suppression).where(
            Suppression.domain_id.in_(domain_ids),
            Suppression.active.is_(True),
        )
    ):
        if suppression.domain_id:
            state[suppression.domain_id]["suppression_reasons"].append(suppression.reason)
    for domain_id in session.scalars(
        select(CampaignMember.domain_id).where(
            CampaignMember.domain_id.in_(domain_ids),
            CampaignMember.reservation_active.is_(True),
        ).distinct()
    ):
        state[domain_id]["active_campaign_reservation"] = True

    result: dict[str, dict[str, Any]] = {}
    for domain_id, evidence in state.items():
        if evidence["listed"]:
            status, label, reason = "listed", "Listed", "This domain is listed in the private catalogue."
        elif evidence["offer"]:
            status, label, reason = "offer_or_deal", "Offer or deal", "Publisher pricing or placement terms are stored."
        elif evidence["replied"]:
            status, label, reason = "replied", "Reply received", "An inbound publisher reply is stored."
        elif evidence["sent"]:
            status, label, reason = "sent_confirmed", "Sent confirmed", "Instantly records delivery activity for this lead."
        elif evidence["uploaded"]:
            status, label, reason = "uploaded_no_send_record", "Uploaded — no send record", "The lead was uploaded to Instantly, but no delivery activity is recorded."
        else:
            status, label, reason = "no_outreach_evidence", "No outreach recorded", "No Instantly upload, send, reply, offer or listing is recorded."
        protection_reasons = ["One normalized domain record is reused across every import and discovery source."]
        if evidence["uploaded"]:
            protection_reasons.append("Existing Instantly lead blocks automatic duplicate outreach.")
        if evidence["suppression_reasons"]:
            protection_reasons.append("An active suppression blocks automatic outreach.")
        if evidence["active_campaign_reservation"]:
            protection_reasons.append("An active campaign reservation blocks another campaign assignment.")
        if evidence["replied"] or evidence["offer"] or evidence["listed"]:
            protection_reasons.append("Reply, deal or catalogue history blocks automatic recontact.")
        if evidence["terminal_lifecycle"]:
            protection_reasons.append("The current lifecycle stage blocks automatic recontact.")
        elif evidence["refresh_eligible"]:
            protection_reasons.append("A failed or no-email crawl may refresh after 90 days without creating another domain record.")
        else:
            protection_reasons.append("Duplicate imports do not create another scrape job.")
        result[domain_id] = {
            "outreach_evidence_status": status,
            "outreach_evidence_label": label,
            "outreach_evidence_reason": reason,
            "outreach_evidence_at": _json(evidence["latest_at"]),
            "canonical_dedupe": True,
            "automatic_recontact_blocked": bool(
                evidence["uploaded"]
                or evidence["suppression_reasons"]
                or evidence["active_campaign_reservation"]
                or evidence["replied"]
                or evidence["offer"]
                or evidence["listed"]
                or evidence["terminal_lifecycle"]
            ),
            "duplicate_protection_reasons": protection_reasons,
        }
    return result


def _domain_source_map(
    session: Session,
    domain_ids: list[str],
) -> dict[str, list[dict[str, Any]]]:
    """Return every canonical intake/discovery source for the requested domains."""
    sources: dict[str, list[dict[str, Any]]] = {
        domain_id: [] for domain_id in domain_ids
    }
    if not domain_ids:
        return sources

    seen: dict[str, set[tuple[str, str]]] = {
        domain_id: set() for domain_id in domain_ids
    }

    def add(domain_id: str, source: dict[str, Any]) -> None:
        key = (str(source.get("type") or ""), str(source.get("id") or ""))
        if key in seen[domain_id]:
            return
        seen[domain_id].add(key)
        sources[domain_id].append(source)

    backlink_rows = session.execute(
        select(BacklinkCandidate, BacklinkAnalysisRun)
        .join(BacklinkAnalysisRun, BacklinkAnalysisRun.id == BacklinkCandidate.run_id)
        .where(BacklinkCandidate.existing_domain_id.in_(domain_ids))
        .order_by(BacklinkAnalysisRun.created_at.desc())
    )
    for candidate, run in backlink_rows:
        competitors = [str(value) for value in (run.competitor_domains or [])]
        profile_label = ", ".join(competitors) or "Unknown profile"
        detail_parts = [f"Client: {run.client_domain}"]
        if run.source_url_filter:
            detail_parts.append(f"Referring URL preference: {run.source_url_filter}")
        detail_parts.append(f"Authority: {run.authority_floor}+" if run.authority_ceiling is None else f"Authority: {run.authority_floor}–{run.authority_ceiling}")
        add(
            candidate.existing_domain_id,
            {
                "id": run.id,
                "type": "backlink_profile",
                "label": f"Backlink profile: {profile_label}",
                "detail": " · ".join(detail_parts),
                "analysis_id": run.id,
                "client_domain": run.client_domain,
                "competitor_domains": competitors,
                "source_url_filter": run.source_url_filter,
                "authority_floor": run.authority_floor,
                "authority_ceiling": run.authority_ceiling,
                "discovered_at": _json(candidate.created_at),
            },
        )

    import_rows = session.execute(
        select(ImportItem, ImportBatch)
        .join(ImportBatch, ImportBatch.id == ImportItem.import_id)
        .where(ImportItem.domain_id.in_(domain_ids))
        .order_by(ImportBatch.created_at.desc())
    )
    for item, batch in import_rows:
        # The richer backlink analysis record above is the canonical provenance
        # for discovery imports; do not duplicate it as a generic file import.
        if batch.source_type == "backlink_discovery":
            continue
        metadata = batch.source_metadata or {}
        source_type = str(batch.source_type or "import")
        if source_type in {"paste", "manual", "manual_paste"}:
            label = "Manual paste"
        elif source_type in {"upload", "csv", "file", "csv_and_paste"}:
            label = f"Uploaded file: {batch.filename}" if batch.filename else "Uploaded file"
        elif source_type in {"migration", "legacy"} or metadata.get("legacy_source"):
            legacy_source = str(metadata.get("legacy_source") or metadata.get("source") or "historical source")
            label = f"Historical migration: {legacy_source}"
        else:
            label = source_type.replace("_", " ").title()
        add(
            item.domain_id,
            {
                "id": batch.id,
                "type": "manual_import" if source_type in {"paste", "manual", "manual_paste"} else source_type,
                "label": label,
                "detail": batch.filename or str(metadata.get("source_name") or "Canonical intake"),
                "import_id": batch.id,
                "filename": batch.filename,
                "discovered_at": _json(batch.created_at),
            },
        )

    for domain_id in domain_ids:
        if not sources[domain_id]:
            sources[domain_id].append(
                {
                    "id": f"canonical:{domain_id}",
                    "type": "canonical_history",
                    "label": "Canonical historical record",
                    "detail": "The original intake source predates retained source metadata.",
                    "discovered_at": None,
                }
            )
    return sources


def _domain_contact_summary_map(
    session: Session,
    domain_ids: list[str],
) -> dict[str, dict[str, Any]]:
    summaries = {
        domain_id: {"best_email": None, "evidence_count": 0}
        for domain_id in domain_ids
    }
    if not domain_ids:
        return summaries
    rows = session.execute(
        select(
            DomainContactEvidence.domain_id,
            Contact.normalized_email,
            DomainContactEvidence.confidence,
        )
        .join(Contact, Contact.id == DomainContactEvidence.contact_id)
        .where(DomainContactEvidence.domain_id.in_(domain_ids))
        .order_by(
            DomainContactEvidence.domain_id,
            DomainContactEvidence.confidence.desc(),
        )
    )
    for domain_id, normalized_email, _confidence in rows:
        summaries[domain_id]["evidence_count"] += 1
        if summaries[domain_id]["best_email"] is None:
            summaries[domain_id]["best_email"] = normalized_email
    return summaries


def _overview_pipeline(
    session: Session,
    evidence: dict[str, Any] | None = None,
) -> dict[str, int]:
    pipeline = {
        str(stage): int(count)
        for stage, count in session.execute(
            select(Domain.lifecycle_stage, func.count(Domain.id)).group_by(Domain.lifecycle_stage)
        )
    }
    summary = evidence or _outreach_evidence_summary(session)
    tiers = summary["tiers"]
    pipeline[LifecycleStage.CONTACTED.value] = tiers["provider_contact_confirmed"]
    pipeline["historical_membership_unverified"] = tiers[
        "historical_membership_unverified"
    ]
    return pipeline


@router.get("/overview")
def overview(session: Session = Depends(db_session)) -> dict[str, Any]:
    domain_total = session.scalar(select(func.count(Domain.id))) or 0
    outreach_evidence = _outreach_evidence_summary(session)
    australian_outreach_evidence = _outreach_evidence_summary(
        session,
        australian_only=True,
    )
    pipeline = _overview_pipeline(session, outreach_evidence)
    historical_funnel = _historical_funnel_summary(session)
    jobs = {
        str(status): int(count)
        for status, count in session.execute(select(Job.status, func.count(Job.id)).group_by(Job.status))
    }
    hour_ago = utcnow() - timedelta(hours=1)
    velocity = session.scalar(
        select(func.count(Job.id)).where(Job.status == JobStatus.SUCCEEDED, Job.completed_at >= hour_ago)
    ) or 0
    failed_last_hour = session.scalar(
        select(func.count(Job.id)).where(
            *_actionable_dead_job_conditions(session),
            Job.updated_at >= hour_ago,
        )
    ) or 0
    failures = list(
        session.scalars(
            select(Job)
            .where(*_actionable_dead_job_conditions(session))
            .order_by(Job.updated_at.desc())
            .limit(8)
        )
    )
    recent_replies = list(session.scalars(select(Reply).order_by(Reply.received_at.desc()).limit(8)))
    return {
        "domains_total": int(domain_total),
        "pipeline": pipeline,
        "historical_funnel": historical_funnel,
        "current_state": _current_state_summary(session),
        "outreach_evidence": outreach_evidence,
        "australian_outreach_evidence": australian_outreach_evidence,
        "jobs": jobs,
        "queue_velocity_per_hour": int(velocity),
        "queue": {
            "pending": int(jobs.get("queued", 0)),
            "active": int(jobs.get("leased", 0)),
            "completed_last_hour": int(velocity),
            "velocity_per_hour": int(velocity),
            "failed_last_hour": int(failed_last_hour),
        },
        "totals": {"domains": int(domain_total), "link_building_domains": historical_funnel["entered_system"], "replies": int(session.scalar(select(func.count(Reply.id))) or 0), "offers": int(session.scalar(select(func.count(Offer.id))) or 0), "listings": int(session.scalar(select(func.count(CatalogueListing.id)).where(CatalogueListing.status == ListingStatus.LISTED)) or 0)},
        "senders": _health_payload(session)["senders"],
        "health": _health_payload(session),
        "failures": [row_dict(job, exclude={"payload"}) for job in failures],
        "recent_replies": [
            {
                "id": reply.id,
                "domain": session.get(Domain, reply.domain_id).normalized_domain if reply.domain_id and session.get(Domain, reply.domain_id) else None,
                "from_email": reply.from_address,
                "subject": reply.subject,
                "snippet": (reply.body_text or "")[:240],
                "received_at": _json(reply.received_at),
                "has_attachments": reply.attachment_only,
            }
            for reply in recent_replies
        ],
    }


def _write_values(path: Path, values: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(values, str):
        payload = values
    elif isinstance(values, list):
        payload = "\n".join(str(value) for value in values)
    else:
        raise HTTPException(status_code=422, detail="values must be a string or list")
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(payload)
        if payload and not payload.endswith("\n"):
            handle.write("\n")


@router.post("/imports", status_code=202)
async def create_import(
    request: Request,
    session: Session = Depends(db_session),
    admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "").lower()
    source_type = "paste"
    source_name = "pasted domains"
    queue = True
    batch = ImportBatch(source_type=source_type, filename=source_name)
    session.add(batch)
    session.flush()
    import_dir = DATA_DIR / "imports"
    path = import_dir / f"{batch.id}.txt"

    if "multipart/form-data" in content_type:
        form = await request.form()
        upload = form.get("file")
        pasted = form.get("values") or form.get("domains") or form.get("pasted") or ""
        source_name = str(form.get("source_name") or getattr(upload, "filename", "") or source_name)[:512]
        queue = str(form.get("queue", "true")).lower() not in {"false", "0", "no"}
        if upload is not None and hasattr(upload, "read"):
            suffix = Path(str(getattr(upload, "filename", "") or "upload.csv")).suffix.lower()
            path = import_dir / f"{batch.id}{suffix if suffix in {'.csv', '.tsv', '.txt'} else '.txt'}"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("wb") as handle:
                while True:
                    chunk = await upload.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
            if str(pasted).strip():
                # Preserve both inputs without loading an arbitrarily large
                # upload into memory.  CSV/TSV uploads remain parseable; pasted
                # values follow as additional newline-delimited rows.
                with path.open("ab") as handle:
                    handle.write(b"\n")
                    handle.write(str(pasted).encode("utf-8"))
                    if not str(pasted).endswith("\n"):
                        handle.write(b"\n")
            source_type = "upload"
        else:
            _write_values(path, str(pasted))
    else:
        payload = await request.json()
        source_name = str(payload.get("source_name") or payload.get("source") or source_name)[:512]
        source_type = str(payload.get("source_type") or payload.get("source") or source_type)[:40]
        queue = bool(payload.get("queue", True))
        _write_values(path, payload.get("values", payload.get("domains", [])))

    if not path.exists() or path.stat().st_size == 0:
        raise HTTPException(status_code=422, detail="No import values were supplied")
    batch.source_type = source_type
    batch.filename = source_name
    batch.source_metadata = {"queue": queue, "input_path": str(path), "actor": str(admin.get("email") or admin.get("id"))}
    job, created = JobQueue(session).enqueue(
        kind="process_import",
        idempotency_key=f"process_import:{batch.id}",
        subject_type="import",
        subject_id=batch.id,
        payload={"import_id": batch.id, "path": str(path), "queue": queue},
        priority=100,
        max_attempts=3,
    )
    session.add(
        AuditLog(
            actor_type="user",
            actor_id=str(admin.get("email") or admin.get("id")),
            action="import.created",
            entity_type="import",
            entity_id=batch.id,
            after={"source_type": source_type, "source_name": source_name, "queue": queue},
        )
    )
    session.flush()
    return {"id": batch.id, "status": batch.status.value, "job_id": job.id, "job_created": created, "queue": queue}


@router.get("/imports")
def imports(limit: int = 50, session: Session = Depends(db_session)) -> dict[str, Any]:
    limit = max(1, min(limit, 250))
    rows = list(session.scalars(select(ImportBatch).order_by(ImportBatch.created_at.desc()).limit(limit)))
    return {"items": [row_dict(row) for row in rows], "count": len(rows)}


@router.get("/imports/{import_id}")
def import_detail(import_id: str, item_limit: int = 100, session: Session = Depends(db_session)) -> dict[str, Any]:
    batch = session.get(ImportBatch, import_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Import not found")
    items = list(
        session.scalars(
            select(ImportItem).where(ImportItem.import_id == import_id).order_by(ImportItem.row_number).limit(max(1, min(item_limit, 500)))
        )
    )
    payload = row_dict(batch)
    payload.update(
        {
            "accepted": batch.accepted_count,
            "duplicates": batch.duplicate_count,
            "invalid": batch.invalid_count,
            "suppressed": batch.suppressed_count,
            "previously_contacted": batch.previously_contacted_count,
            "refresh_eligible": batch.refresh_eligible_count,
            "items": [row_dict(item) for item in items],
        }
    )
    return payload


@router.get("/domains")
def domains(
    q: str = "",
    stage: str = "",
    search: str = "",
    status: str = "",
    evidence: str = "",
    history: str = "",
    scope: str = "",
    limit: int = 100,
    page_size: int | None = None,
    offset: int = 0,
    session: Session = Depends(db_session),
) -> dict[str, Any]:
    statement = select(Domain)
    scope_filter = scope.strip()
    if scope_filter == "link_building":
        statement = statement.where(_link_building_domain_scope())
    elif scope_filter == "unrelated_instantly_only":
        statement = statement.where(
            ~_link_building_domain_scope(),
            exists(
                select(InstantlyEvent.id).where(
                    InstantlyEvent.domain_id == Domain.id,
                    InstantlyEvent.event_type == "campaign_membership_reconciled",
                )
            ),
        )
    elif scope_filter:
        raise HTTPException(status_code=422, detail="Unknown domain scope filter")
    query_text = (search or q).strip()
    lifecycle = (status or stage).strip()
    if query_text:
        statement = statement.where(Domain.normalized_domain.ilike(f"%{query_text}%"))
    if lifecycle:
        statement = statement.where(Domain.lifecycle_stage == lifecycle)
    evidence_filter = evidence.strip()
    predicates = _outreach_evidence_predicates()
    if evidence_filter:
        if evidence_filter not in predicates:
            raise HTTPException(status_code=422, detail="Unknown outreach evidence filter")
        statement = statement.where(predicates[evidence_filter])
    history_filter = history.strip()
    history_predicates = _historical_funnel_predicates()
    if history_filter:
        if history_filter not in history_predicates:
            raise HTTPException(status_code=422, detail="Unknown historical funnel filter")
        statement = statement.where(history_predicates[history_filter])
    total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    requested_page_size = page_size if page_size is not None else limit
    selected_page_size = max(1, min(requested_page_size, 500))
    selected_offset = max(offset, 0)
    rows = list(session.scalars(statement.order_by(Domain.updated_at.desc()).offset(selected_offset).limit(selected_page_size)))
    outreach_map = _domain_outreach_evidence_map(session, [row.id for row in rows])
    source_map = _domain_source_map(session, [row.id for row in rows])
    contact_map = _domain_contact_summary_map(session, [row.id for row in rows])
    items = []
    for row in rows:
        items.append(
            {
                **row_dict(row),
                "domain": row.normalized_domain,
                "registrable_domain": row.normalized_domain,
                "status": row.lifecycle_stage.value,
                "country": row.country_code,
                "last_attempt_at": _json(row.last_scraped_at),
                "refresh_eligible_at": _json(row.next_refresh_at),
                "best_email": contact_map[row.id]["best_email"],
                "evidence_count": contact_map[row.id]["evidence_count"],
                "sources": source_map[row.id],
                "source_label": source_map[row.id][0]["label"],
                "source_count": len(source_map[row.id]),
                **outreach_map[row.id],
            }
        )
    return {
        "items": items,
        "count": int(total),
        "total": int(total),
        "offset": selected_offset,
        "page_size": selected_page_size,
        "evidence_filter": evidence_filter or None,
        "history_filter": history_filter or None,
        "scope_filter": scope_filter or None,
    }


@router.get("/domains/{domain_id}")
def domain_detail(domain_id: str, session: Session = Depends(db_session)) -> dict[str, Any]:
    domain = session.get(Domain, domain_id)
    if domain is None:
        raise HTTPException(status_code=404, detail="Domain not found")
    events = list(session.scalars(select(DomainEvent).where(DomainEvent.domain_id == domain_id).order_by(DomainEvent.created_at.desc()).limit(250)))
    evidence_rows = list(
        session.execute(
            select(DomainContactEvidence, Contact)
            .join(Contact, Contact.id == DomainContactEvidence.contact_id)
            .where(DomainContactEvidence.domain_id == domain_id)
            .order_by(DomainContactEvidence.confidence.desc())
        )
    )
    outreach = _domain_outreach_evidence_map(session, [domain_id])[domain_id]
    sources = _domain_source_map(session, [domain_id])[domain_id]
    return {
        **row_dict(domain),
        "domain": domain.normalized_domain,
        "registrable_domain": domain.normalized_domain,
        "status": domain.lifecycle_stage.value,
        "country": domain.country_code,
        "last_attempt_at": _json(domain.last_scraped_at),
        "refresh_eligible_at": _json(domain.next_refresh_at),
        "best_email": evidence_rows[0][1].normalized_email if evidence_rows else None,
        "evidence_count": len(evidence_rows),
        "sources": sources,
        "source_label": sources[0]["label"],
        "source_count": len(sources),
        **outreach,
        "events": [row_dict(event) for event in events],
        "evidence": [
            {**row_dict(evidence), "contact": row_dict(contact, exclude={"attributes"})}
            for evidence, contact in evidence_rows
        ],
    }


@router.get("/jobs")
def jobs(
    status: str = "",
    kind: str = "",
    job_type: str = "",
    limit: int = 100,
    page_size: int | None = None,
    offset: int = 0,
    session: Session = Depends(db_session),
) -> dict[str, Any]:
    filters: list[Any] = []
    selected_kind = (job_type or kind).strip()
    if selected_kind:
        if selected_kind == "scrape":
            filters.append(Job.kind == "scrape_domain")
        else:
            filters.append(Job.kind == selected_kind)

    grouped = {
        job_status.value: int(count)
        for job_status, count in session.execute(
            select(Job.status, func.count(Job.id))
            .where(*filters)
            .group_by(Job.status)
        )
    }
    summary = {
        "queued": grouped.get("queued", 0),
        "active": grouped.get("leased", 0),
        "completed": grouped.get("succeeded", 0),
        "failed": grouped.get("dead", 0),
        "cancelled": grouped.get("cancelled", 0),
    }

    filtered = list(filters)
    if status:
        status_aliases = {
            "completed": "succeeded",
            "failed": "dead",
            "error": "dead",
            "active": "leased",
            "running": "leased",
            "pending": "queued",
        }
        filtered.append(Job.status == status_aliases.get(status, status))
    statement = select(Job).where(*filtered)
    total = int(session.scalar(select(func.count()).select_from(statement.subquery())) or 0)
    requested_limit = page_size if page_size is not None else limit
    rows = list(
        session.scalars(
            statement.order_by(Job.created_at.desc())
            .offset(max(offset, 0))
            .limit(max(1, min(requested_limit, 500)))
        )
    )
    items = [
        {
            **row_dict(row),
            "job_type": row.kind,
            "run_after": _json(row.available_at),
            "domain": session.get(Domain, row.subject_id).normalized_domain if row.subject_type == "domain" and row.subject_id and session.get(Domain, row.subject_id) else None,
        }
        for row in rows
    ]
    return {"items": items, "count": len(rows), "total": total, "offset": max(offset, 0), "summary": summary}


@router.post("/jobs/{job_id}/retry")
def retry_job(
    job_id: str,
    session: Session = Depends(db_session),
    admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    try:
        job = JobQueue(session).retry(job_id, actor=str(admin.get("email") or admin.get("id")))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return row_dict(job)


def _scrape_activity_item(attempt: ScrapeAttempt, domain: Domain, job: Job | None) -> dict[str, Any]:
    finished_at = attempt.completed_at or utcnow()
    started_at = attempt.started_at
    if started_at.tzinfo is None and finished_at.tzinfo is not None:
        finished_at = finished_at.replace(tzinfo=None)
    elif started_at.tzinfo is not None and finished_at.tzinfo is None:
        finished_at = finished_at.replace(tzinfo=started_at.tzinfo)
    metrics = attempt.metrics or {}
    return {
        "id": attempt.id,
        "domain_id": domain.id,
        "domain": domain.normalized_domain,
        "status": attempt.status.value,
        "attempt_number": attempt.attempt_number,
        "started_at": _json(attempt.started_at),
        "completed_at": _json(attempt.completed_at),
        "elapsed_seconds": max(0, int((finished_at - started_at).total_seconds())),
        "pages_requested": attempt.pages_requested,
        "pages_crawled": attempt.pages_crawled,
        "emails_found": attempt.emails_found,
        "error_code": attempt.error_code,
        "error_detail": attempt.error_detail,
        "job_id": job.id if job else attempt.job_id,
        "job_status": job.status.value if job else None,
        "job_attempts": job.attempts if job else None,
        "job_max_attempts": job.max_attempts if job else None,
        "leased_until": _json(job.leased_until) if job else None,
        "current_url": metrics.get("current_url") or None,
        "last_event": metrics.get("last_event") or None,
        "last_event_at": metrics.get("last_event_at") or None,
        "page_status": metrics.get("page_status") or None,
        "max_pages": metrics.get("max_pages") or None,
    }


SCRAPE_EVENT_ACTIONS = frozenset(
    {"scrape_started", "page_started", "page_crawled", "email_found", "scrape_failed", "scrape_completed"}
)


def _scrape_event_item(event: AuditLog) -> dict[str, Any]:
    payload = dict(event.after or {})
    payload.update(
        {
            "id": event.id,
            "event_type": event.action,
            "attempt_id": payload.get("attempt_id") or event.entity_id,
            "occurred_at": payload.get("occurred_at") or _json(event.created_at),
        }
    )
    return payload


@router.get("/scraping/activity")
def scraping_activity(limit: int = 40, session: Session = Depends(db_session)) -> dict[str, Any]:
    bounded_limit = max(1, min(limit, 100))
    active_rows = list(
        session.execute(
            select(ScrapeAttempt, Domain, Job)
            .join(Domain, Domain.id == ScrapeAttempt.domain_id)
            .outerjoin(Job, Job.id == ScrapeAttempt.job_id)
            .where(ScrapeAttempt.status == ScrapeStatus.RUNNING)
            .order_by(ScrapeAttempt.started_at)
        )
    )
    recent_rows = list(
        session.execute(
            select(ScrapeAttempt, Domain, Job)
            .join(Domain, Domain.id == ScrapeAttempt.domain_id)
            .outerjoin(Job, Job.id == ScrapeAttempt.job_id)
            .where(ScrapeAttempt.status != ScrapeStatus.RUNNING)
            .order_by(ScrapeAttempt.completed_at.desc(), ScrapeAttempt.started_at.desc())
            .limit(bounded_limit)
        )
    )
    event_rows = list(
        session.scalars(
            select(AuditLog)
            .where(AuditLog.action.in_(SCRAPE_EVENT_ACTIONS))
            .order_by(AuditLog.created_at.desc())
            .limit(bounded_limit)
        )
    )
    return {
        "active": [_scrape_activity_item(attempt, domain, job) for attempt, domain, job in active_rows],
        "recent": [_scrape_activity_item(attempt, domain, job) for attempt, domain, job in recent_rows],
        "events": [_scrape_event_item(event) for event in event_rows],
        "generated_at": _json(utcnow()),
    }


@router.get("/campaigns")
def campaigns(limit: int = 100, session: Session = Depends(db_session)) -> dict[str, Any]:
    rows = list(session.scalars(select(CampaignBatch).order_by(CampaignBatch.created_at.desc()).limit(max(1, min(limit, 500)))))
    items = []
    for row in rows:
        sender = session.get(SenderAccount, row.sender_account_id) if row.sender_account_id else None
        settings = row.settings_snapshot or {}
        items.append({**row_dict(row), "managed": row.managed_by_link_os, "member_count": row.expected_member_count, "uploaded_count": row.uploaded_member_count, "sender_email": sender.sender_email if sender else None, "blocking_reasons": [row.paused_reason] if row.paused_reason else [], "hard_bounce_limit": int(settings.get("hard_bounce_limit") or 3), "ignore_hard_bounces": settings.get("ignore_hard_bounces") is True, "provider_bounce_protection_disabled": settings.get("provider_bounce_protection_disabled") is True})
    return {"items": items, "count": len(rows), "total": len(rows), "outreach_paused": _pause_setting(session)}


@router.post("/campaign-sets/estimate")
def estimate_segmented_campaign_set(
    session: Session = Depends(db_session),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    return campaign_set_estimate(session, batch_size=25)


@router.post("/campaign-sets", status_code=202)
async def create_segmented_campaign_set(
    request: Request,
    session: Session = Depends(db_session),
    admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    payload = await request.json()
    if payload.get("confirm_verification_skipped") is not True:
        raise HTTPException(status_code=422, detail="confirm_verification_skipped=true is required")
    if int(payload.get("batch_size") or 25) != 25:
        raise HTTPException(status_code=422, detail="This launch requires 25 contacts per campaign")
    actor = str(admin.get("email") or admin.get("id"))
    retired = retire_failed_public_evidence_pilot(session, actor=actor)
    try:
        launch, jobs = prepare_campaign_set(session, actor=actor, batch_size=25)
        session.commit()
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "id": launch.id,
        "status": launch.status.value,
        "batch_size": launch.batch_size,
        "campaign_count": 3,
        "job_ids": [job.id for job in jobs],
        "retired_pilot_ids": retired,
        "verification_skipped": True,
    }


@router.get("/campaign-sets/{launch_id}")
def get_segmented_campaign_set(
    launch_id: str,
    session: Session = Depends(db_session),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    launch = session.get(CampaignLaunch, launch_id)
    if launch is None:
        raise HTTPException(status_code=404, detail="Campaign set not found")
    batches = list(session.scalars(select(CampaignBatch).where(CampaignBatch.launch_id == launch.id).order_by(CampaignBatch.created_at)))
    return {**row_dict(launch), "batches": [{**row_dict(batch), "member_count": batch.expected_member_count, "uploaded_count": batch.uploaded_member_count, "hard_bounce_limit": launch.hard_bounce_limit} for batch in batches]}


@router.post("/campaign-sets/{launch_id}/activate", status_code=202)
async def activate_segmented_campaign_set(
    launch_id: str,
    request: Request,
    session: Session = Depends(db_session),
    admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    payload = await request.json()
    if payload.get("confirm_activation") is not True:
        raise HTTPException(status_code=422, detail="confirm_activation=true is required")
    launch = session.get(CampaignLaunch, launch_id)
    if launch is None:
        raise HTTPException(status_code=404, detail="Campaign set not found")
    batches = list(session.scalars(select(CampaignBatch).where(CampaignBatch.launch_id == launch.id).order_by(CampaignBatch.created_at)))
    if len(batches) != 3 or any(
        batch.status != CampaignBatchStatus.PAUSED
        or batch.expected_member_count != 25
        or batch.uploaded_member_count != 25
        or batch.readback_member_count != 25
        or batch.pending_verification_count != 0
        for batch in batches
    ):
        raise HTTPException(status_code=409, detail="All three paused campaigns must have exact 25/25 readbacks")
    jobs = []
    actor = str(admin.get("email") or admin.get("id"))
    for batch in batches:
        job, _ = JobQueue(session).enqueue(
            kind="prepare_campaign_batch",
            idempotency_key=f"activate_campaign_launch:{launch.id}:{batch.id}:{utcnow().strftime('%Y%m%d%H%M%S')}",
            subject_type="campaign_batch", subject_id=batch.id,
            payload={"launch_id": launch.id, "batch_id": batch.id, "pilot": True, "target_size": 25, "contact_policy": launch.contact_policy, "prepare_only": False, "confirm_activation": True, "requested_by": actor},
            priority=40, max_attempts=3,
        )
        jobs.append(job)
    launch.status = CampaignLaunchStatus.PREPARING
    session.add(AuditLog(actor_type="user", actor_id=actor, action="campaign_launch.activation_requested", entity_type="campaign_launch", entity_id=launch.id, after={"jobs": len(jobs)}))
    session.commit()
    return {"id": launch.id, "status": launch.status.value, "job_ids": [job.id for job in jobs]}


@router.post("/campaigns/{batch_id}/pause")
async def pause_one_managed_campaign(
    batch_id: str,
    request: Request,
    session: Session = Depends(db_session),
    admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    batch = session.get(CampaignBatch, batch_id)
    if batch is None or not batch.external_campaign_id:
        raise HTTPException(status_code=404, detail="Managed campaign not found")
    payload = await request.json()
    reason = str(payload.get("reason") or "Operator paused campaign")[:500]
    readback = await asyncio.to_thread(InstantlyControl().pause_campaign, batch.external_campaign_id)
    batch.status = CampaignBatchStatus.PAUSED
    batch.paused_at = utcnow()
    batch.paused_reason = reason
    session.add(AuditLog(actor_type="user", actor_id=str(admin.get("email") or admin.get("id")), action="campaign.paused", entity_type="campaign_batch", entity_id=batch.id, after={"remote_status": readback.get("status")}, reason=reason))
    session.commit()
    return {"id": batch.id, "status": batch.status.value, "remote_status": readback.get("status")}


@router.post("/campaigns/prepare", status_code=202)
async def prepare_campaign(
    request: Request,
    session: Session = Depends(db_session),
    admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    payload = await request.json()
    pilot = payload.get("pilot", True) is not False
    target_size = int(payload.get("target_size") or (25 if pilot else 250))
    contact_policy = str(payload.get("contact_policy") or "strict_verified")
    prepare_only = payload.get("prepare_only") is True
    allowed_policies = {
        "strict_verified",
        "public_evidence_verification_skipped",
    }
    if contact_policy not in allowed_policies:
        raise HTTPException(status_code=422, detail="Unknown contact_policy")
    if contact_policy == "public_evidence_verification_skipped":
        if not pilot or target_size > 25:
            raise HTTPException(
                status_code=422,
                detail="Verification-skipped contacts are limited to a 25-contact pilot",
            )
        if payload.get("confirm_verification_skipped") is not True:
            raise HTTPException(
                status_code=422,
                detail="confirm_verification_skipped=true is required",
            )
        if not prepare_only:
            raise HTTPException(
                status_code=422,
                detail="Verification-skipped pilots must be prepared paused first",
            )
    if not 1 <= target_size <= 250:
        raise HTTPException(status_code=422, detail="target_size must be between 1 and 250")
    job, created = JobQueue(session).enqueue(
        kind="prepare_campaign_batch",
        idempotency_key=f"prepare_campaign_batch:manual:{utcnow().strftime('%Y%m%d%H%M%S')}",
        payload={
            "pilot": pilot,
            "target_size": target_size,
            "contact_policy": contact_policy,
            "prepare_only": prepare_only,
            "confirm_verification_skipped": payload.get("confirm_verification_skipped") is True,
            "requested_by": str(admin.get("email") or admin.get("id")),
        },
        priority=30,
        max_attempts=5,
    )
    return {
        "job_id": job.id,
        "job_created": created,
        "status": job.status.value,
        "pilot": pilot,
        "target_size": target_size,
        "contact_policy": contact_policy,
        "prepare_only": prepare_only,
    }


@router.post("/campaigns/{batch_id}/activate", status_code=202)
async def activate_campaign_batch(
    batch_id: str,
    request: Request,
    session: Session = Depends(db_session),
    admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    payload = await request.json()
    if payload.get("confirm_activation") is not True:
        raise HTTPException(status_code=422, detail="confirm_activation=true is required")
    batch = session.get(CampaignBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Campaign batch not found")
    if batch.status == CampaignBatchStatus.ACTIVE:
        return {
            "batch_id": batch.id,
            "status": batch.status.value,
            "activated": True,
            "idempotent": True,
        }
    if batch.status != CampaignBatchStatus.PAUSED:
        raise HTTPException(status_code=409, detail="Only a prepared paused batch can be activated")
    if (
        batch.expected_member_count <= 0
        or batch.uploaded_member_count != batch.expected_member_count
        or batch.readback_member_count != batch.expected_member_count
        or batch.pending_verification_count != 0
    ):
        raise HTTPException(status_code=409, detail="Campaign member readback is incomplete")
    settings = dict(batch.settings_snapshot or {})
    job, created = JobQueue(session).enqueue(
        kind="prepare_campaign_batch",
        idempotency_key=(
            f"activate_campaign_batch:{batch.id}:"
            f"{utcnow().strftime('%Y%m%d%H%M%S')}"
        ),
        payload={
            "batch_id": batch.id,
            "pilot": settings.get("pilot", True) is not False,
            "target_size": batch.expected_member_count,
            "contact_policy": settings.get("contact_policy") or "strict_verified",
            "prepare_only": False,
            "confirm_activation": True,
            "requested_by": str(admin.get("email") or admin.get("id")),
        },
        priority=40,
        max_attempts=3,
    )
    session.add(
        AuditLog(
            actor_type="user",
            actor_id=str(admin.get("email") or admin.get("id")),
            action="campaign.activation_requested",
            entity_type="campaign_batch",
            entity_id=batch.id,
            before={"status": batch.status.value},
            after={"job_id": job.id, "confirmed": True},
            reason=str(payload.get("reason") or "Operator-approved campaign activation"),
        )
    )
    return {
        "batch_id": batch.id,
        "job_id": job.id,
        "job_created": created,
        "status": job.status.value,
        "activated": False,
    }


@router.get("/replies")
def replies(limit: int = 100, session: Session = Depends(db_session)) -> dict[str, Any]:
    rows = list(session.scalars(select(Reply).order_by(Reply.received_at.desc()).limit(max(1, min(limit, 500)))))
    items = []
    for row in rows:
        domain = session.get(Domain, row.domain_id) if row.domain_id else None
        items.append({**row_dict(row, exclude={"thread_evidence", "body_text"}), "domain": domain.normalized_domain if domain else None, "from_email": row.from_address, "snippet": (row.body_text or "")[:300], "has_attachments": row.attachment_only, "review_status": "review" if row.processed_at is None else "resolved"})
    return {"items": items, "count": len(rows), "total": len(rows)}


@router.get("/replies/{reply_id}")
def reply_detail(reply_id: str, session: Session = Depends(db_session)) -> dict[str, Any]:
    reply = session.get(Reply, reply_id)
    if reply is None:
        raise HTTPException(status_code=404, detail="Reply not found")
    offers = list(session.scalars(select(Offer).where(Offer.reply_id == reply_id)))
    return {"reply": row_dict(reply), "offers": [row_dict(offer) for offer in offers]}


@router.get("/offers")
def offers(status: str = "", limit: int = 100, session: Session = Depends(db_session)) -> dict[str, Any]:
    statement = select(Offer)
    if status:
        statement = statement.where(Offer.status == status)
    rows = list(session.scalars(statement.order_by(Offer.created_at.desc()).limit(max(1, min(limit, 500)))))
    items = []
    for row in rows:
        domain = session.get(Domain, row.domain_id)
        reply = session.get(Reply, row.reply_id)
        contact = session.get(Contact, reply.contact_id) if reply and reply.contact_id else None
        listing = session.scalar(
            select(CatalogueListing).where(CatalogueListing.offer_id == row.id)
        )
        listed = listing is not None and listing.status == ListingStatus.LISTED
        items.append({**row_dict(row), "domain": domain.normalized_domain if domain else "Unknown domain", "publisher_email": contact.normalized_email if contact else None, "confidence": row.extraction_confidence, "evidence": (reply.body_text or "")[:500] if reply else "", "private": not listed, "listed": listed, "listing_id": listing.id if listing else None})
    return {"items": items, "count": len(rows), "total": len(rows)}


@router.patch("/offers/{offer_id}")
async def update_offer(
    offer_id: str,
    request: Request,
    session: Session = Depends(db_session),
    admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    offer = session.get(Offer, offer_id)
    if offer is None:
        raise HTTPException(status_code=404, detail="Offer not found")
    payload = await request.json()
    before = row_dict(offer)
    listing: CatalogueListing | None = None
    if "status" in payload:
        try:
            offer.status = OfferStatus(str(payload["status"]))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid offer status") from exc
        if offer.status == OfferStatus.APPROVED:
            offer.approved_at = utcnow()
            offer.approved_by = str(admin.get("email") or admin.get("id"))
            listing = ensure_private_listing(session, offer)
        elif offer.status in {OfferStatus.REJECTED, OfferStatus.LOST}:
            listing = session.scalar(
                select(CatalogueListing).where(CatalogueListing.offer_id == offer.id)
            )
            if listing is not None:
                listing.status = ListingStatus.HIDDEN
                listing.listed_at = None
    if payload.get("list") is True:
        if offer.status != OfferStatus.APPROVED or offer.reseller_price_aud is None:
            raise HTTPException(status_code=409, detail="Only approved, priced offers can be listed")
        listing = listing or session.scalar(select(CatalogueListing).where(CatalogueListing.offer_id == offer.id))
        if listing is None:
            listing = CatalogueListing(
                offer_id=offer.id,
                publisher_id=offer.publisher_id,
                domain_id=offer.domain_id,
                status=ListingStatus.LISTED,
                visibility_tier=str(payload.get("visibility_tier") or "basic"),
                agency_price_aud=offer.reseller_price_aud,
                listed_at=utcnow(),
            )
            session.add(listing)
        else:
            listing.status = ListingStatus.LISTED
            listing.visibility_tier = str(
                payload.get("visibility_tier")
                or (
                    listing.visibility_tier
                    if listing.visibility_tier != "private"
                    else "basic"
                )
            )
            listing.agency_price_aud = offer.reseller_price_aud
            listing.listed_at = utcnow()
    session.add(
        AuditLog(
            actor_type="user",
            actor_id=str(admin.get("email") or admin.get("id")),
            action="offer.updated",
            entity_type="offer",
            entity_id=offer.id,
            before=before,
            after=payload,
        )
    )
    JobQueue(session).enqueue(
        kind="reconcile_monday",
        idempotency_key=f"reconcile_monday:offer_update:{utcnow().strftime('%Y%m%d%H%M')}",
        payload={"offer_id": offer.id, "source": "manual_offer_update"},
        priority=20,
        max_attempts=5,
    )
    session.flush()
    return row_dict(offer)


@router.get("/listings")
def listings(limit: int = 200, session: Session = Depends(db_session)) -> dict[str, Any]:
    rows = list(session.scalars(select(CatalogueListing).order_by(CatalogueListing.created_at.desc()).limit(max(1, min(limit, 500)))))
    items = []
    for row in rows:
        domain = session.get(Domain, row.domain_id)
        offer = session.get(Offer, row.offer_id)
        cost_aud = offer.cost_aud if offer else None
        if (
            cost_aud is None
            and offer
            and offer.original_currency == "AUD"
        ):
            cost_aud = offer.original_amount
        items.append({**row_dict(row), "domain": domain.normalized_domain if domain else "Unknown domain", "visibility": row.status.value, "publisher_cost": offer.original_amount if offer else None, "publisher_currency": offer.original_currency if offer else None, "cost_aud": cost_aud, "reseller_price_aud": row.agency_price_aud, "placement_type": offer.placement_type if offer else None, "pricing_rule_version": offer.pricing_rule_version if offer else None, "enquiries": 0})
    return {"items": items, "count": len(rows), "total": len(rows), "private_by_default": True}


@router.patch("/listings/{listing_id}")
async def update_listing(
    listing_id: str,
    request: Request,
    session: Session = Depends(db_session),
    admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    listing = session.get(CatalogueListing, listing_id)
    if listing is None:
        raise HTTPException(status_code=404, detail="Listing not found")
    payload = await request.json()
    requested = str(payload.get("status") or payload.get("visibility") or "private")
    target = ListingStatus.LISTED if requested == "listed" else ListingStatus.PRIVATE
    before = row_dict(listing)
    listing.status = target
    listing.visibility_tier = str(payload.get("visibility_tier") or (listing.visibility_tier if target == ListingStatus.LISTED else "private"))
    listing.listed_at = utcnow() if target == ListingStatus.LISTED else None
    session.add(AuditLog(actor_type="user", actor_id=str(admin.get("email") or admin.get("id")), action="listing.visibility_changed", entity_type="listing", entity_id=listing.id, before=before, after={"status": target.value}))
    session.flush()
    domain = session.get(Domain, listing.domain_id)
    offer = session.get(Offer, listing.offer_id)
    return {**row_dict(listing), "domain": domain.normalized_domain if domain else "Unknown domain", "visibility": target.value, "reseller_price_aud": listing.agency_price_aud, "placement_type": offer.placement_type if offer else None}


@router.get("/agencies")
def agencies(_admin: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    rows = [row for row in list_users() if row.get("role") == "agency"]
    safe_rows = [
        {
            **{key: value for key, value in row.items() if key not in {"password_hash"}},
            "name": str(row.get("email") or "Agency").split("@", 1)[0].replace(".", " ").title(),
            "status": "active" if row.get("is_active") else "inactive",
            "last_active_at": row.get("last_login_at"),
            "enquiry_count": 0,
        }
        for row in rows
    ]
    return {"items": safe_rows, "count": len(safe_rows), "storage": "legacy_auth_during_shadow_cutover"}


@router.post("/outreach/pause")
@router.post("/system/outreach/pause")
async def pause_outreach(
    request: Request,
    session: Session = Depends(db_session),
    admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    payload = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    reason = str(payload.get("reason") or "manual emergency pause")[:500]
    state = _set_pause(session, paused=True, actor=str(admin.get("email") or admin.get("id")), reason=reason)
    session.commit()

    local_batches = list(
        session.scalars(
            select(CampaignBatch).where(
                CampaignBatch.external_campaign_id.is_not(None),
                CampaignBatch.status == CampaignBatchStatus.ACTIVE,
            )
        )
    )
    for batch in local_batches:
        batch.status = CampaignBatchStatus.PAUSED
        batch.paused_at = utcnow()
        batch.paused_reason = reason
    session.commit()

    remote_attempted = 0
    remote_paused = 0
    remote_failures = 0
    try:
        control = InstantlyControl()
        campaigns = await asyncio.to_thread(control.list_campaigns)
        active_managed = [
            campaign
            for campaign in campaigns
            if str(campaign.get("name") or "").startswith("LINK OS |")
            and campaign.get("status") in {1, 4}
        ]
        remote_attempted = len(active_managed)
        for campaign in active_managed:
            try:
                await asyncio.to_thread(
                    control.pause_campaign,
                    str(campaign.get("id") or ""),
                )
                remote_paused += 1
            except Exception:
                remote_failures += 1
    except Exception:
        remote_failures = max(remote_failures, 1)
    session.add(
        AuditLog(
            actor_type="user",
            actor_id=str(admin.get("email") or admin.get("id")),
            action="outreach.remote_pause_readback",
            entity_type="integration",
            entity_id="instantly",
            after={
                "attempted": remote_attempted,
                "paused": remote_paused,
                "failures": remote_failures,
            },
            reason=reason,
        )
    )
    return {
        "ok": remote_failures == 0,
        **state,
        "remote_managed_campaigns": {
            "attempted": remote_attempted,
            "paused": remote_paused,
            "failures": remote_failures,
        },
    }


@router.post("/outreach/resume")
@router.post("/system/outreach/resume")
async def resume_outreach(
    request: Request,
    session: Session = Depends(db_session),
    admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    payload = await request.json()
    if payload.get("confirm") is not True and payload.get("confirm_sender_health") is not True:
        raise HTTPException(status_code=422, detail="confirm=true is required")
    health_state = _health_payload(session)
    blockers = [reason for reason in health_state["blocking_reasons"] if reason != "global_outreach_pause_enabled"]
    if blockers:
        raise HTTPException(status_code=409, detail={"message": "Outreach remains paused", "blocking_reasons": blockers})
    state = _set_pause(
        session,
        paused=False,
        actor=str(admin.get("email") or admin.get("id")),
        reason=str(payload.get("reason") or "manual resume after health readback")[:500],
    )
    return {"ok": True, **state}


def _upsert_sender_snapshot(session: Session, account: dict[str, Any]) -> SenderAccount:
    external_id = str(account.get("id") or account.get("email") or "")
    sender = session.scalar(select(SenderAccount).where(SenderAccount.external_id == external_id))
    connected = sender_is_healthy(account)
    now = utcnow()
    if sender is None:
        sender = SenderAccount(external_id=external_id)
        session.add(sender)
    was_connected = sender.connected
    sender.sender_email = account.get("email")
    sender.provider_status = account.get("status")
    sender.connected = connected
    sender.last_checked_at = now
    status_message = account.get("status_message")
    sender.last_error = None if connected else json.dumps(status_message or {"status": account.get("status")})[:2000]
    if connected and not was_connected:
        sender.healthy_since = now
    elif not connected:
        sender.healthy_since = None
    return sender


@router.post("/integrations/instantly/reconcile")
async def reconcile_instantly(
    request: Request,
    session: Session = Depends(db_session),
    admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    payload = await request.json()
    dry_run = payload.get("dry_run", True) is not False
    pause_legacy = payload.get("pause_legacy", False) is True
    control = InstantlyControl()
    accounts = await asyncio.to_thread(control.list_accounts)
    campaigns = await asyncio.to_thread(control.list_campaigns)
    matched = matching_campaigns(campaigns)
    pause_result = None
    full_result = None
    expected_count = int(payload.get("expected_legacy_count") or 13)
    if pause_legacy:
        pause_result = await asyncio.to_thread(
            control.pause_legacy_campaigns,
            execute=not dry_run,
            expected_count=expected_count,
        )
    if not dry_run:
        for account in accounts:
            _upsert_sender_snapshot(session, account)
        session.flush()
        session.commit()
        if pause_legacy:
            pause_setting = session.get(
                SystemSetting,
                "instantly.legacy_campaigns_paused",
            )
            pause_value = {
                "complete": True,
                "completed_at": utcnow().isoformat(),
                "campaigns": int(pause_result.get("matched") or 0),
                "expected_campaigns": expected_count,
            }
            if pause_setting is None:
                session.add(
                    SystemSetting(
                        key="instantly.legacy_campaigns_paused",
                        value=pause_value,
                        updated_by=str(admin.get("email") or admin.get("id")),
                    )
                )
            else:
                pause_setting.value = pause_value
                pause_setting.version += 1
                pause_setting.updated_by = str(admin.get("email") or admin.get("id"))
            session.commit()
        if payload.get("full_reconcile", True) is not False:
            from deal_tracker.worker import reconcile_instantly_job

            full_result = await asyncio.to_thread(reconcile_instantly_job, None, force_full=True)
        session.add(
            AuditLog(
                actor_type="user",
                actor_id=str(admin.get("email") or admin.get("id")),
                action="instantly.reconciled",
                entity_type="integration",
                entity_id="instantly",
                after={"accounts": len(accounts), "campaigns": len(campaigns), "matched": len(matched), "legacy_paused": bool(pause_result)},
            )
        )
    return {
        "dry_run": dry_run,
        "accounts": len(accounts),
        "healthy_accounts": sum(1 for account in accounts if sender_is_healthy(account)),
        "campaigns": len(campaigns),
        "matched_legacy_campaigns": len(matched),
        "managed_campaigns": sum(1 for campaign in campaigns if str(campaign.get("name") or "").startswith("LINK OS |")),
        "pause_result": pause_result,
        "full_reconcile": full_result,
    }


@router.post("/integrations/monday/reconcile", status_code=202)
async def reconcile_monday(
    request: Request,
    session: Session = Depends(db_session),
) -> dict[str, Any]:
    payload = await request.json()
    dry_run = payload.get("dry_run", True) is not False
    mappings = session.scalar(select(func.count(MondayMapping.id))) or 0
    preview = DealsOnlyProjector().reconcile(session, execute=False)
    if dry_run:
        return {**preview, "existing_mappings": int(mappings)}
    if preview.get("projection_ready") is not True:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Monday projection blocked by live preflight",
                "destination_errors": preview.get("destination_errors", []),
                "schema_errors": preview.get("schema_errors", []),
                "mapping_errors": preview.get("mapping_errors", []),
            },
        )
    job, created = JobQueue(session).enqueue(
        kind="reconcile_monday",
        idempotency_key=f"reconcile_monday:{utcnow().strftime('%Y%m%d%H%M')}",
        payload={"requested_at": utcnow().isoformat()},
        priority=20,
    )
    return {"dry_run": False, "job_id": job.id, "job_created": created, "existing_mappings": int(mappings)}


@router.post("/migrations/import-records", status_code=202)
async def migrate_records(request: Request, session: Session = Depends(db_session)) -> dict[str, Any]:
    payload = await request.json()
    records = payload.get("records")
    migration_id = str(payload.get("migration_id") or "").strip()
    if not isinstance(records, list):
        raise HTTPException(status_code=422, detail="records must be a JSON list")
    try:
        result = import_migration_records(session, records, migration_id=migration_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    result.update(
        {
            "batch_number": int(payload.get("batch_number") or 0),
            "final_batch": payload.get("final_batch") is True,
        }
    )
    return result


async def _event_stream(request: Request, factory: Any) -> AsyncIterator[str]:
    last_seen = utcnow() - timedelta(seconds=5)
    heartbeat = 0
    while not await request.is_disconnected():
        session = factory()
        try:
            events = list(
                session.scalars(
                    select(AuditLog).where(AuditLog.created_at > last_seen).order_by(AuditLog.created_at).limit(100)
                )
            )
            for event in events:
                last_seen = max(last_seen, event.created_at)
                event_name = event.action if event.action in SCRAPE_EVENT_ACTIONS else "update"
                yield f"id: {event.id}\nevent: {event_name}\ndata: {json.dumps(row_dict(event), separators=(',', ':'))}\n\n"
        finally:
            session.close()
        heartbeat += 1
        if not events or heartbeat % 10 == 0:
            yield f"event: heartbeat\ndata: {json.dumps({'at': utcnow().isoformat()})}\n\n"
        await asyncio.sleep(2)


@router.get("/events")
@router.get("/events/stream")
async def events_stream(request: Request, _admin: dict[str, Any] = Depends(require_admin)) -> StreamingResponse:
    if not platform_configured():
        raise HTTPException(status_code=503, detail="Canonical DATABASE_URL is not configured")
    return StreamingResponse(
        _event_stream(request, session_factory()),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
