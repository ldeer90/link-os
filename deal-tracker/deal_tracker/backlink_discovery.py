"""Canonical SE Ranking backlink discovery, review, and intake orchestration."""

from __future__ import annotations

import hashlib
import json
import os
from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence
from urllib.parse import urlsplit

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from deal_tracker.anchor_intent import aggregate_anchor_intent
from deal_tracker.integrations.seranking import CreditStateUnknown, SERankingClient, SERankingError
from deal_tracker.platform.enums import (
    BacklinkAnalysisMode,
    BacklinkAnalysisStatus,
    BacklinkCandidateStatus,
    JobStatus,
    LifecycleStage,
)
from deal_tracker.platform.lifecycle import NEVER_AUTOMATICALLY_RECONTACT
from deal_tracker.platform.models import (
    AuditLog,
    BacklinkAnalysisRun,
    BacklinkAnalysisTarget,
    BacklinkCandidate,
    BacklinkCreditUsage,
    BacklinkEvidence,
    BacklinkProviderRequest,
    BacklinkReviewDecision,
    CampaignMember,
    CatalogueListing,
    Domain,
    ImportBatch,
    Job,
    Offer,
    Suppression,
    SystemSetting,
    utcnow,
)
from deal_tracker.platform.normalization import normalize_domain
from deal_tracker.platform.repositories import JobQueue
from deal_tracker.platform_runtime import engine as platform_engine
from deal_tracker.platform_runtime import session_factory


DEFAULT_CREDIT_CAP = 500
MAX_CREDIT_CAP = 2500
DEFAULT_MONTHLY_CAP = 10_000
DEFAULT_AUTHORITY_FLOOR = 20
DEFAULT_AUTHORITY_CEILING = 80
DEFAULT_CACHE_DAYS = 30
REFDOMAIN_COUNT_CREDITS = 2
PROVIDER_ENDPOINT = "/v1/backlinks/all"
PROVIDER_REFRESH_ENDPOINT = "/v1/backlinks/refdomains/history"
PROVIDER_COUNT_ENDPOINT = "/v1/backlinks/refdomains/count"
BALANCE_SETTING = "seranking.last_subscription_check"

EXACT_BLOCKED_DOMAINS = {
    "facebook.com", "instagram.com", "linkedin.com", "pinterest.com", "reddit.com",
    "tiktok.com", "twitter.com", "x.com", "youtube.com", "google.com", "bing.com",
    "yahoo.com", "duckduckgo.com", "wikipedia.org", "wordpress.com", "blogspot.com",
    "medium.com", "tumblr.com", "quora.com", "github.com", "gitlab.com", "amazon.com",
}
BLOCKED_LABELS = {
    "directory", "directories", "profile", "profiles", "forum", "forums", "classifieds",
    "casino", "poker", "betting", "viagra", "crypto-airdrop", "linkfarm", "free-links",
}
PUBLIC_SECTOR_SUFFIXES = (".gov", ".gov.au", ".gov.uk", ".edu", ".edu.au", ".ac.uk", ".mil")
POSITIVE_TERMS = {
    "news", "editor", "editorial", "magazine", "journal", "blog", "media", "article",
    "review", "guide", "contributor", "author", "press", "resources",
}


class BacklinkValidationError(ValueError):
    pass


def monthly_cap() -> int:
    return max(1, int(os.getenv("SE_RANKING_MONTHLY_CREDIT_CAP", str(DEFAULT_MONTHLY_CAP))))


def cache_days() -> int:
    return max(1, int(os.getenv("SE_RANKING_CACHE_DAYS", str(DEFAULT_CACHE_DAYS))))


def normalize_analysis_inputs(
    *, mode: str, client_domain: str, competitor_domains: Sequence[str] | None = None
) -> tuple[BacklinkAnalysisMode, str, list[str]]:
    try:
        parsed_mode = BacklinkAnalysisMode(mode)
    except ValueError as exc:
        raise BacklinkValidationError("mode must be competitor_prospecting or client_profile_audit") from exc
    try:
        client = normalize_domain(client_domain).registrable_domain
    except ValueError as exc:
        raise BacklinkValidationError("client_domain must be a valid public domain") from exc
    competitors: list[str] = []
    for value in competitor_domains or []:
        try:
            normalized = normalize_domain(value).registrable_domain
        except ValueError as exc:
            raise BacklinkValidationError(f"invalid competitor domain: {value}") from exc
        if normalized != client and normalized not in competitors:
            competitors.append(normalized)
    if len(competitors) > 5:
        raise BacklinkValidationError("at most five competitor domains are allowed")
    if parsed_mode == BacklinkAnalysisMode.COMPETITOR_PROSPECTING and not competitors:
        raise BacklinkValidationError("competitor_prospecting requires at least one competitor")
    return parsed_mode, client, competitors


def validate_credit_cap(value: int) -> int:
    if value < 1 or value > MAX_CREDIT_CAP:
        raise BacklinkValidationError(f"credit cap must be between 1 and {MAX_CREDIT_CAP}")
    return value


def normalize_source_url_filter(value: str | None) -> str | None:
    normalized = (value or "").strip().lower()
    if not normalized:
        return None
    if len(normalized) > 255 or any(ord(character) < 32 for character in normalized):
        raise BacklinkValidationError("source_url_filter must be a safe value of at most 255 characters")
    return normalized


def target_allocations(mode: BacklinkAnalysisMode, client: str, competitors: Sequence[str], cap: int) -> list[tuple[str, str, int]]:
    targets = [(client, "client")] if mode == BacklinkAnalysisMode.CLIENT_PROFILE_AUDIT else [(value, "competitor") for value in competitors[:5]]
    base, remainder = divmod(cap, len(targets))
    return [(domain, role, base + (1 if index < remainder else 0)) for index, (domain, role) in enumerate(targets)]


def request_hash(
    target: str,
    allocation: int,
    authority_floor: int,
    *,
    since: date | None = None,
    source_url_filter: str | None = None,
    authority_ceiling: int | None = None,
) -> str:
    payload = {
        "endpoint": PROVIDER_REFRESH_ENDPOINT if since else PROVIDER_ENDPOINT,
        "target": target,
        "mode": "domain",
        "per_domain": 1,
        "limit": allocation,
        "order_by": "domain_inlink_rank",
        "domain_inlink_rank_from": authority_floor,
        "domain_inlink_rank_to": authority_ceiling,
        "date_from": since.isoformat() if since else None,
        "url_from_filter": source_url_filter,
        "url_from_filter_mode": "contains" if source_url_filter else None,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def month_usage(session: Session, *, now: datetime | None = None) -> int:
    instant = now or utcnow()
    month_start = datetime(instant.year, instant.month, 1, tzinfo=timezone.utc)
    return int(session.scalar(select(func.coalesce(func.sum(BacklinkCreditUsage.actual_credits), 0)).where(BacklinkCreditUsage.created_at >= month_start)) or 0)


def _cached_request(session: Session, request_digest: str) -> BacklinkProviderRequest | None:
    threshold = utcnow() - timedelta(days=cache_days())
    return session.scalar(
        select(BacklinkProviderRequest)
        .where(
            BacklinkProviderRequest.request_hash == request_digest,
            BacklinkProviderRequest.status.in_(["complete", "cached"]),
            BacklinkProviderRequest.completed_at >= threshold,
        )
        .order_by(BacklinkProviderRequest.completed_at.desc())
        .limit(1)
    )


def _incremental_checkpoint(
    session: Session,
    *,
    target_domain: str,
    authority_floor: int,
) -> date | None:
    """Return a safe refresh checkpoint only after the historical snapshot is complete.

    A snapshot that returns exactly its requested limit is truncated. A later run
    must expand `/backlinks/all` instead of treating the pilot date as a complete
    historical checkpoint. Incremental requests can continue from their latest
    checkpoint even when their own result limit was saturated.
    """

    previous = session.execute(
        select(BacklinkAnalysisTarget, BacklinkProviderRequest.endpoint)
        .join(BacklinkAnalysisRun, BacklinkAnalysisRun.id == BacklinkAnalysisTarget.run_id)
        .join(BacklinkProviderRequest, BacklinkProviderRequest.target_id == BacklinkAnalysisTarget.id)
        .where(
            BacklinkAnalysisTarget.target_domain == target_domain,
            BacklinkAnalysisTarget.status.in_(["complete", "cached"]),
            BacklinkProviderRequest.status.in_(["complete", "cached"]),
            BacklinkAnalysisRun.authority_floor == authority_floor,
        )
        .order_by(BacklinkProviderRequest.completed_at.desc(), BacklinkProviderRequest.id.desc())
        .limit(1)
    ).first()
    if previous is None:
        return None
    target, endpoint = previous
    if endpoint == PROVIDER_REFRESH_ENDPOINT:
        return target.checkpoint_date
    if target.returned_count < target.allocated_credits:
        return target.checkpoint_date
    return None


def estimate_analysis(
    session: Session,
    *,
    mode: str,
    client_domain: str,
    competitor_domains: Sequence[str] | None,
    credit_cap: int = DEFAULT_CREDIT_CAP,
    authority_floor: int = DEFAULT_AUTHORITY_FLOOR,
    authority_ceiling: int | None = None,
    source_url_filter: str | None = None,
    refresh_balance: bool = True,
    provider: SERankingClient | None = None,
) -> dict[str, Any]:
    parsed_mode, client, competitors = normalize_analysis_inputs(
        mode=mode, client_domain=client_domain, competitor_domains=competitor_domains
    )
    cap = validate_credit_cap(int(credit_cap))
    normalized_source_filter = normalize_source_url_filter(source_url_filter)
    if not 0 <= authority_floor <= 100:
        raise BacklinkValidationError("authority_floor must be between 0 and 100")
    if authority_ceiling is not None and not authority_floor <= authority_ceiling <= 100:
        raise BacklinkValidationError("authority_ceiling must be between authority_floor and 100")
    allocations = target_allocations(parsed_mode, client, competitors, cap)
    minimum_cap = len(allocations) * (REFDOMAIN_COUNT_CREDITS + 1)
    if cap < minimum_cap:
        raise BacklinkValidationError(
            f"credit cap must be at least {minimum_cap} for {len(allocations)} target(s): "
            f"two count credits plus one referring-domain record per target"
        )
    targets = []
    predicted = 0
    for domain, role, allocation in allocations:
        retrieval_cap = allocation - REFDOMAIN_COUNT_CREDITS
        previous_checkpoint = None if normalized_source_filter or authority_ceiling is not None else _incremental_checkpoint(
            session, target_domain=domain, authority_floor=authority_floor
        )
        digest = request_hash(
            domain,
            retrieval_cap,
            authority_floor,
            since=previous_checkpoint,
            source_url_filter=normalized_source_filter,
            authority_ceiling=authority_ceiling,
        )
        cached = _cached_request(session, digest) is not None
        predicted += REFDOMAIN_COUNT_CREDITS + (0 if cached else retrieval_cap)
        targets.append({
            "domain": domain,
            "role": role,
            "credit_cap": allocation,
            "count_credit_cost": REFDOMAIN_COUNT_CREDITS,
            "retrieval_credit_cap": retrieval_cap,
            "cache_hit": cached,
            "request_hash": digest,
            "refresh_since": previous_checkpoint.isoformat() if previous_checkpoint else None,
            "endpoint": PROVIDER_REFRESH_ENDPOINT if previous_checkpoint else PROVIDER_ENDPOINT,
            "source_url_filter": normalized_source_filter,
            "authority_ceiling": authority_ceiling,
        })

    usage = month_usage(session)
    configured_cap = monthly_cap()
    provider_client = provider or SERankingClient()
    balance: int | None = None
    balance_error: str | None = None
    if refresh_balance and provider_client.configured:
        try:
            balance = provider_client.subscription().balance
            _store_balance(session, balance=balance, status="ok")
        except SERankingError as exc:
            balance_error = exc.code
            _store_balance(session, balance=None, status=exc.code)
    else:
        setting = session.get(SystemSetting, BALANCE_SETTING)
        if setting:
            balance = setting.value.get("balance")
    allowed = usage + predicted <= configured_cap and (balance is None or balance >= predicted)
    blockers = []
    if usage + predicted > configured_cap:
        blockers.append("monthly_credit_cap_exceeded")
    if balance is not None and balance < predicted:
        blockers.append("insufficient_provider_balance")
    if not provider_client.configured:
        blockers.append("seranking_not_configured")
        allowed = False
    return {
        "mode": parsed_mode.value,
        "client_domain": client,
        "competitor_domains": competitors,
        "credit_cap": cap,
        "predicted_credits": predicted,
        "count_credit_cost": len(allocations) * REFDOMAIN_COUNT_CREDITS,
        "maximum_paid_records": sum(
            target["retrieval_credit_cap"]
            for target in targets
            if not target["cache_hit"]
        ),
        "authority_floor": authority_floor,
        "authority_ceiling": authority_ceiling,
        "source_url_filter": normalized_source_filter,
        "monthly_usage": usage,
        "monthly_cap": configured_cap,
        "monthly_remaining": max(0, configured_cap - usage),
        "balance": balance,
        "balance_error": balance_error,
        "allowed": allowed,
        "blocking_reasons": blockers,
        "targets": targets,
        "confirmation_required": True,
    }


def _store_balance(session: Session, *, balance: int | None, status: str) -> None:
    value = {"balance": balance, "status": status, "checked_at": utcnow().isoformat()}
    setting = session.get(SystemSetting, BALANCE_SETTING)
    if setting is None:
        session.add(SystemSetting(key=BALANCE_SETTING, value=value, updated_by="seranking_adapter"))
    else:
        setting.value = value
        setting.version += 1
        setting.updated_by = "seranking_adapter"
    session.flush()


def create_analysis(
    session: Session,
    *,
    mode: str,
    client_domain: str,
    competitor_domains: Sequence[str] | None,
    credit_cap: int,
    confirmed_credit_cap: int | None,
    authority_floor: int,
    source_url_filter: str | None,
    actor: str,
    authority_ceiling: int | None = None,
    source: str = "console",
) -> BacklinkAnalysisRun:
    estimate = estimate_analysis(
        session,
        mode=mode,
        client_domain=client_domain,
        competitor_domains=competitor_domains,
        credit_cap=credit_cap,
        authority_floor=authority_floor,
        source_url_filter=source_url_filter,
        authority_ceiling=authority_ceiling,
        refresh_balance=True,
    )
    if confirmed_credit_cap != credit_cap:
        raise BacklinkValidationError("confirmed_credit_cap must exactly match credit_cap")
    if not estimate["allowed"]:
        raise BacklinkValidationError(", ".join(estimate["blocking_reasons"]) or "analysis is blocked")
    run = BacklinkAnalysisRun(
        mode=BacklinkAnalysisMode(estimate["mode"]),
        status=BacklinkAnalysisStatus.QUEUED,
        client_domain=estimate["client_domain"],
        competitor_domains=estimate["competitor_domains"],
        source_url_filter=estimate["source_url_filter"],
        requested_by=actor,
        request_source=source,
        credit_cap=credit_cap,
        confirmed_credit_cap=confirmed_credit_cap,
        authority_floor=authority_floor,
        authority_ceiling=estimate["authority_ceiling"],
        monthly_credit_cap=estimate["monthly_cap"],
        estimated_credits=estimate["predicted_credits"],
        balance_before=estimate["balance"],
        counters={"total_referring_domains": 0, "returned": 0, "coverage_percent": 0.0, "cached": 0, "excluded": 0, "pending_review": 0, "approved": 0, "imported": 0, "scrape_queued": 0},
    )
    session.add(run)
    session.flush()
    for target in estimate["targets"]:
        session.add(
            BacklinkAnalysisTarget(
                run_id=run.id,
                target_domain=target["domain"],
                target_role=target["role"],
                allocated_credits=target["retrieval_credit_cap"],
                request_hash=target["request_hash"],
                cache_hit=target["cache_hit"],
                checkpoint_date=_date(target.get("refresh_since")),
            )
        )
    JobQueue(session).enqueue(
        kind="backlink_analysis",
        idempotency_key=f"backlink_analysis:{run.id}",
        subject_type="backlink_analysis",
        subject_id=run.id,
        payload={"run_id": run.id},
        priority=40,
        max_attempts=1,
    )
    session.add(AuditLog(actor_type="user" if source == "console" else "codex", actor_id=actor, action="backlink_analysis.queued", entity_type="backlink_analysis", entity_id=run.id, after={"mode": run.mode.value, "credit_cap": credit_cap, "targets": len(estimate["targets"]), "source_url_filter": run.source_url_filter}))
    session.flush()
    return run


def _pick(item: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if item.get(key) is not None:
            return item[key]
    return None


def _integer(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _boolean(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "dofollow", "follow"}


def _date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _source_domain(item: dict[str, Any]) -> str:
    raw = str(_pick(item, "source_domain", "domain", "referring_domain", "source", "url_from", "source_url") or "")
    return normalize_domain(raw).registrable_domain


def _evidence_key(source_domain: str, source_url: str, target_url: str | None) -> str:
    return hashlib.sha256(f"{source_domain}\n{source_url}\n{target_url or ''}".encode()).hexdigest()


def _persist_evidence(session: Session, run: BacklinkAnalysisRun, target: BacklinkAnalysisTarget, item: dict[str, Any]) -> BacklinkEvidence | None:
    try:
        source_domain = _source_domain(item)
    except ValueError:
        return None
    source_url = str(_pick(item, "source_url", "url_from", "url", "page_url") or f"https://{source_domain}")
    target_url_value = _pick(item, "target_url", "url_to", "destination_url")
    target_url = str(target_url_value) if target_url_value else None
    key = _evidence_key(source_domain, source_url, target_url)
    evidence = session.scalar(select(BacklinkEvidence).where(BacklinkEvidence.run_id == run.id, BacklinkEvidence.evidence_key == key))
    if evidence:
        return evidence
    nofollow_value = _pick(item, "nofollow", "is_nofollow", "no_follow")
    link_type = str(_pick(item, "link_type", "type") or "").lower()
    evidence = BacklinkEvidence(
        run_id=run.id,
        target_id=target.id,
        source_domain=source_domain,
        source_url=source_url,
        target_url=target_url,
        page_title=str(_pick(item, "page_title", "title") or "") or None,
        anchor_text=str(_pick(item, "anchor", "anchor_text") or "") or None,
        nofollow=_boolean(nofollow_value) if nofollow_value is not None else ("nofollow" in link_type if link_type else None),
        image_link=_boolean(_pick(item, "image", "is_image", "image_link")),
        inlink_rank=_integer(_pick(item, "inlink_rank", "page_inlink_rank", "rank")),
        domain_inlink_rank=_integer(_pick(item, "domain_inlink_rank", "domain_rank", "domain_trust")),
        first_seen=_date(_pick(item, "first_seen", "first_seen_date")),
        last_visited=_date(_pick(item, "last_visited", "last_seen", "last_checked")),
        provider_metadata={"provider": "seranking", "target_domain": target.target_domain},
        evidence_key=key,
    )
    session.add(evidence)
    session.flush()
    return evidence


def _hard_exclusion(session: Session, run: BacklinkAnalysisRun, domain_name: str) -> list[str]:
    reasons: list[str] = []
    if domain_name in {run.client_domain, *run.competitor_domains}:
        reasons.append("submitted_target")
    if domain_name in EXACT_BLOCKED_DOMAINS:
        reasons.append("platform_or_ugc_host")
    if domain_name.endswith(PUBLIC_SECTOR_SUFFIXES):
        reasons.append("public_sector_or_education")
    labels = set(domain_name.replace("-", ".").split("."))
    if labels.intersection(BLOCKED_LABELS):
        reasons.append("directory_ugc_or_spam_pattern")
    domain = session.scalar(select(Domain).where(Domain.normalized_domain == domain_name))
    if domain:
        if session.scalar(select(Suppression.id).where(Suppression.domain_id == domain.id, Suppression.active.is_(True)).limit(1)):
            reasons.append("suppressed")
        if domain.last_contacted_at is not None or domain.lifecycle_stage in NEVER_AUTOMATICALLY_RECONTACT:
            reasons.append("previously_contacted_or_protected")
        if session.scalar(select(CampaignMember.id).where(CampaignMember.domain_id == domain.id, CampaignMember.reservation_active.is_(True)).limit(1)):
            reasons.append("active_outreach")
        if session.scalar(select(Offer.id).where(Offer.domain_id == domain.id).limit(1)):
            reasons.append("existing_offer")
        if session.scalar(select(CatalogueListing.id).where(CatalogueListing.domain_id == domain.id).limit(1)):
            reasons.append("existing_listing")
    return sorted(set(reasons))


def _build_candidates(session: Session, run: BacklinkAnalysisRun) -> None:
    grouped: dict[str, list[BacklinkEvidence]] = defaultdict(list)
    for evidence in session.scalars(select(BacklinkEvidence).where(BacklinkEvidence.run_id == run.id)):
        grouped[evidence.source_domain].append(evidence)
    for domain_name, evidence_rows in grouped.items():
        candidate = session.scalar(select(BacklinkCandidate).where(BacklinkCandidate.run_id == run.id, BacklinkCandidate.normalized_domain == domain_name))
        if candidate:
            continue
        reasons = _hard_exclusion(session, run, domain_name)
        existing_domain = session.scalar(select(Domain).where(Domain.normalized_domain == domain_name))
        text_evidence = " ".join(filter(None, [row.page_title for row in evidence_rows] + [row.source_url for row in evidence_rows] + [row.anchor_text for row in evidence_rows])).lower()
        rank = max((row.domain_inlink_rank or 0 for row in evidence_rows), default=0)
        dofollow = any(row.nofollow is False for row in evidence_rows)
        anchor_intent = aggregate_anchor_intent(evidence_rows)
        positive_matches = sorted(term for term in POSITIVE_TERMS if term in text_evidence)
        score = min(1.0, 0.25 + min(rank, 100) / 200 + min(len(evidence_rows), 3) * 0.05 + (0.1 if dofollow else 0) + min(len(positive_matches), 2) * 0.05 + anchor_intent.score * 0.15)
        if reasons:
            status = BacklinkCandidateStatus.HARD_REJECTED
        elif run.mode == BacklinkAnalysisMode.CLIENT_PROFILE_AUDIT:
            status = BacklinkCandidateStatus.AUDIT_ONLY
            reasons = ["client_profile_audit_no_outreach"]
        else:
            status = BacklinkCandidateStatus.AWAITING_CODEX_REVIEW
            reasons = positive_matches or ["requires_publisher_relevance_review"]
            if anchor_intent.classification == "commercial":
                reasons = sorted(set([*reasons, "commercial_anchor_evidence"]))
            elif anchor_intent.classification == "commercial_likely":
                reasons = sorted(set([*reasons, "possible_commercial_anchor"]))
        session.add(
            BacklinkCandidate(
                run_id=run.id,
                normalized_domain=domain_name,
                existing_domain_id=existing_domain.id if existing_domain else None,
                status=status,
                occurrence_count=len({row.target_id for row in evidence_rows}),
                highest_domain_inlink_rank=rank or None,
                has_dofollow=dofollow,
                local_score=round(score, 3),
                commercial_anchor_class=anchor_intent.classification,
                commercial_anchor_score=anchor_intent.score,
                commercial_anchor_signals=list(anchor_intent.signals),
                reason_codes=reasons,
            )
        )
    session.flush()


def _refresh_counters(session: Session, run: BacklinkAnalysisRun) -> None:
    candidate_counts = {status.value: int(count) for status, count in session.execute(select(BacklinkCandidate.status, func.count(BacklinkCandidate.id)).where(BacklinkCandidate.run_id == run.id).group_by(BacklinkCandidate.status))}
    returned = int(session.scalar(select(func.count(BacklinkEvidence.id)).where(BacklinkEvidence.run_id == run.id)) or 0)
    total_referring_domains = int(session.scalar(select(func.coalesce(func.sum(BacklinkAnalysisTarget.referring_domain_count), 0)).where(BacklinkAnalysisTarget.run_id == run.id)) or 0)
    counted_targets = int(session.scalar(select(func.count(BacklinkAnalysisTarget.id)).where(BacklinkAnalysisTarget.run_id == run.id, BacklinkAnalysisTarget.referring_domain_count.is_not(None))) or 0)
    run.counters = {
        "total_referring_domains": total_referring_domains,
        "counted_targets": counted_targets,
        "returned": returned,
        "coverage_percent": round(min(100.0, returned * 100 / total_referring_domains), 1) if total_referring_domains else 0.0,
        "cached": int(session.scalar(select(func.count(BacklinkAnalysisTarget.id)).where(BacklinkAnalysisTarget.run_id == run.id, BacklinkAnalysisTarget.cache_hit.is_(True))) or 0),
        "excluded": candidate_counts.get(BacklinkCandidateStatus.HARD_REJECTED.value, 0),
        "pending_review": candidate_counts.get(BacklinkCandidateStatus.AWAITING_CODEX_REVIEW.value, 0),
        "approved": candidate_counts.get(BacklinkCandidateStatus.APPROVED.value, 0) + candidate_counts.get(BacklinkCandidateStatus.IMPORT_QUEUED.value, 0) + candidate_counts.get(BacklinkCandidateStatus.IMPORTED.value, 0),
        "manual_review": candidate_counts.get(BacklinkCandidateStatus.MANUAL_REVIEW.value, 0),
        "imported": candidate_counts.get(BacklinkCandidateStatus.IMPORTED.value, 0),
        "scrape_queued": candidate_counts.get(BacklinkCandidateStatus.IMPORTED.value, 0),
        "audit_only": candidate_counts.get(BacklinkCandidateStatus.AUDIT_ONLY.value, 0),
    }


@contextmanager
def _provider_lock() -> Iterator[None]:
    database_engine = platform_engine()
    if database_engine.dialect.name != "postgresql":
        yield
        return
    with database_engine.connect() as connection:
        acquired = bool(connection.scalar(text("SELECT pg_try_advisory_lock(hashtext(:key))"), {"key": "link-os:seranking-backlinks"}))
        if not acquired:
            raise RuntimeError("Another SE Ranking backlink request is active")
        try:
            yield
        finally:
            connection.execute(text("SELECT pg_advisory_unlock(hashtext(:key))"), {"key": "link-os:seranking-backlinks"})


def run_analysis_job(job_id: str, *, provider: SERankingClient | None = None) -> dict[str, Any]:
    factory = session_factory()
    client = provider or SERankingClient()
    with factory() as session:
        job = session.get(Job, job_id)
        if not job:
            raise LookupError(f"job {job_id} disappeared")
        run_id = str(job.payload.get("run_id") or job.subject_id or "")
        run = session.get(BacklinkAnalysisRun, run_id)
        if not run:
            raise LookupError(f"backlink analysis {run_id} disappeared")
        if run.status == BacklinkAnalysisStatus.CANCELLED:
            return {"cancelled": True}
        if run.confirmed_credit_cap != run.credit_cap:
            raise BacklinkValidationError("paid execution is missing exact credit confirmation")
        run.status = BacklinkAnalysisStatus.FETCHING
        run.started_at = run.started_at or utcnow()
        run.error_code = None
        run.error_detail = None
        session.commit()

    try:
        with _provider_lock():
            with factory() as session:
                run = session.get(BacklinkAnalysisRun, run_id)
                used = month_usage(session)
                if used + run.estimated_credits > run.monthly_credit_cap:
                    raise BacklinkValidationError("monthly_credit_cap_exceeded")
                before = client.subscription().balance
                run.balance_before = before
                if before is not None and before < run.estimated_credits:
                    raise BacklinkValidationError("insufficient_provider_balance")
                _store_balance(session, balance=before, status="ok")
                session.commit()

            targets = []
            with factory() as session:
                targets = [target.id for target in session.scalars(select(BacklinkAnalysisTarget).where(BacklinkAnalysisTarget.run_id == run_id).order_by(BacklinkAnalysisTarget.id))]
            for target_id in targets:
                with factory() as session:
                    run = session.get(BacklinkAnalysisRun, run_id)
                    target = session.get(BacklinkAnalysisTarget, target_id)
                    if run.status == BacklinkAnalysisStatus.CANCELLED or run.counters.get("cancel_requested"):
                        run.status = BacklinkAnalysisStatus.CANCELLED
                        run.completed_at = utcnow()
                        session.commit()
                        return {"cancelled": True}
                    target.started_at = utcnow()

                    if target.referring_domain_count is None:
                        count_balance_before = client.subscription().balance
                        if count_balance_before is not None and count_balance_before < REFDOMAIN_COUNT_CREDITS:
                            raise BacklinkValidationError("insufficient_provider_balance")
                        count_request = BacklinkProviderRequest(
                            run_id=run.id,
                            target_id=target.id,
                            endpoint=PROVIDER_COUNT_ENDPOINT,
                            request_hash=hashlib.sha256(
                                f"{PROVIDER_COUNT_ENDPOINT}|{target.target_domain}|domain".encode()
                            ).hexdigest(),
                            request_parameters={"target": target.target_domain, "mode": "domain"},
                            status="started",
                            predicted_credits=REFDOMAIN_COUNT_CREDITS,
                            balance_before=count_balance_before,
                        )
                        session.add(count_request)
                        session.commit()
                        count_request_id = count_request.id
                        count_target_domain = target.target_domain
                    else:
                        count_request_id = None
                        count_target_domain = target.target_domain

                if count_request_id:
                    referring_domain_count = client.referring_domains_count(count_target_domain)
                    try:
                        count_balance_after = client.subscription().balance
                    except SERankingError:
                        count_balance_after = None
                    with factory() as session:
                        run = session.get(BacklinkAnalysisRun, run_id)
                        target = session.get(BacklinkAnalysisTarget, target_id)
                        count_request = session.get(BacklinkProviderRequest, count_request_id)
                        count_request.status = "complete"
                        count_request.actual_credits = REFDOMAIN_COUNT_CREDITS
                        count_request.balance_after = count_balance_after
                        count_request.response_digest = hashlib.sha256(
                            str(referring_domain_count).encode()
                        ).hexdigest()
                        count_request.completed_at = utcnow()
                        target.referring_domain_count = referring_domain_count
                        target.count_actual_credits = REFDOMAIN_COUNT_CREDITS
                        target.count_checked_at = utcnow()
                        target.actual_credits += REFDOMAIN_COUNT_CREDITS
                        run.actual_credits += REFDOMAIN_COUNT_CREDITS
                        count_observed_delta = None
                        if count_request.balance_before is not None and count_balance_after is not None:
                            count_observed_delta = max(
                                0, count_request.balance_before - count_balance_after
                            )
                        session.add(BacklinkCreditUsage(
                            run_id=run.id,
                            provider_request_id=count_request.id,
                            event_type="referring_domain_count",
                            predicted_credits=REFDOMAIN_COUNT_CREDITS,
                            actual_credits=REFDOMAIN_COUNT_CREDITS,
                            balance_before=count_request.balance_before,
                            balance_after=count_balance_after,
                            observed_delta=count_observed_delta,
                            mismatch=(
                                count_observed_delta is not None
                                and count_observed_delta != REFDOMAIN_COUNT_CREDITS
                            ),
                        ))
                        session.commit()

                with factory() as session:
                    run = session.get(BacklinkAnalysisRun, run_id)
                    target = session.get(BacklinkAnalysisTarget, target_id)
                    if target.status in {"complete", "cached"} and session.scalar(select(BacklinkEvidence.id).where(BacklinkEvidence.target_id == target.id).limit(1)):
                        continue
                    cached = _cached_request(session, target.request_hash)
                    if cached and cached.run_id != run.id:
                        previous_rows = list(session.scalars(select(BacklinkEvidence).where(BacklinkEvidence.target_id == cached.target_id)))
                        for previous in previous_rows:
                            _persist_evidence(session, run, target, {
                                "source_domain": previous.source_domain, "source_url": previous.source_url,
                                "target_url": previous.target_url, "page_title": previous.page_title,
                                "anchor": previous.anchor_text, "nofollow": previous.nofollow,
                                "image_link": previous.image_link, "inlink_rank": previous.inlink_rank,
                                "domain_inlink_rank": previous.domain_inlink_rank, "first_seen": previous.first_seen,
                                "last_visited": previous.last_visited,
                            })
                        target.cache_hit = True
                        target.status = "cached"
                        target.returned_count = len(previous_rows)
                        target.completed_at = utcnow()
                        endpoint = PROVIDER_REFRESH_ENDPOINT if target.checkpoint_date else PROVIDER_ENDPOINT
                        session.add(BacklinkProviderRequest(run_id=run.id, target_id=target.id, endpoint=endpoint, request_hash=target.request_hash, request_parameters={"target": target.target_domain, "mode": "domain", "per_domain": 1 if not target.checkpoint_date else None, "limit": target.allocated_credits, "order_by": "domain_inlink_rank", "domain_inlink_rank_from": run.authority_floor if not target.checkpoint_date else None, "domain_inlink_rank_to": run.authority_ceiling if not target.checkpoint_date else None, "date_from": target.checkpoint_date.isoformat() if target.checkpoint_date else None, "url_from_filter": run.source_url_filter, "url_from_filter_mode": "contains" if run.source_url_filter else None}, status="cached", predicted_credits=0, actual_credits=0, completed_at=utcnow()))
                        session.commit()
                        continue
                    refresh_since = target.checkpoint_date
                    endpoint = PROVIDER_REFRESH_ENDPOINT if refresh_since else PROVIDER_ENDPOINT
                    request_balance_before = client.subscription().balance
                    if request_balance_before is not None and request_balance_before < target.allocated_credits:
                        raise BacklinkValidationError("insufficient_provider_balance")
                    provider_request = BacklinkProviderRequest(
                        run_id=run.id, target_id=target.id, endpoint=endpoint,
                        request_hash=target.request_hash,
                        request_parameters={"target": target.target_domain, "mode": "domain", "per_domain": 1 if not refresh_since else None, "limit": target.allocated_credits, "order_by": "domain_inlink_rank", "domain_inlink_rank_from": run.authority_floor if not refresh_since else None, "domain_inlink_rank_to": run.authority_ceiling if not refresh_since else None, "date_from": refresh_since.isoformat() if refresh_since else None, "url_from_filter": run.source_url_filter, "url_from_filter_mode": "contains" if run.source_url_filter else None},
                        status="started", predicted_credits=target.allocated_credits, balance_before=request_balance_before,
                    )
                    session.add(provider_request)
                    session.commit()
                    request_id = provider_request.id
                    target_domain = target.target_domain
                    allocation = target.allocated_credits
                    authority_floor = run.authority_floor
                    authority_ceiling = run.authority_ceiling
                    refresh_since_value = refresh_since.isoformat() if refresh_since else None
                    source_url_filter = run.source_url_filter
                backlink_options: dict[str, Any] = {
                    "limit": allocation,
                    "authority_floor": authority_floor,
                    "since": refresh_since_value,
                }
                if authority_ceiling is not None:
                    backlink_options["authority_ceiling"] = authority_ceiling
                if source_url_filter:
                    backlink_options["source_url_filter"] = source_url_filter
                records = client.backlinks(target_domain, **backlink_options)
                try:
                    request_balance_after = client.subscription().balance
                except SERankingError:
                    request_balance_after = None
                with factory() as session:
                    run = session.get(BacklinkAnalysisRun, run_id)
                    target = session.get(BacklinkAnalysisTarget, target_id)
                    provider_request = session.get(BacklinkProviderRequest, request_id)
                    persisted = sum(1 for item in records if _persist_evidence(session, run, target, item) is not None)
                    response_digest = hashlib.sha256(json.dumps(records, sort_keys=True, default=str).encode()).hexdigest()
                    provider_request.status = "complete"
                    provider_request.actual_credits = len(records)
                    provider_request.balance_after = request_balance_after
                    provider_request.response_digest = response_digest
                    provider_request.completed_at = utcnow()
                    target.status = "complete"
                    target.returned_count = persisted
                    target.actual_credits += len(records)
                    target.checkpoint_date = utcnow().date()
                    target.completed_at = utcnow()
                    run.actual_credits += len(records)
                    observed_delta = None
                    if provider_request.balance_before is not None and request_balance_after is not None:
                        observed_delta = max(0, provider_request.balance_before - request_balance_after)
                    session.add(BacklinkCreditUsage(run_id=run.id, provider_request_id=provider_request.id, event_type="records_returned", predicted_credits=allocation, actual_credits=len(records), balance_before=provider_request.balance_before, balance_after=request_balance_after, observed_delta=observed_delta, mismatch=len(records) > allocation or (observed_delta is not None and observed_delta != len(records))))
                    session.commit()

            with factory() as session:
                run = session.get(BacklinkAnalysisRun, run_id)
                run.status = BacklinkAnalysisStatus.FILTERING
                _build_candidates(session, run)
                _refresh_counters(session, run)
                pending = run.counters.get("pending_review", 0)
                run.status = BacklinkAnalysisStatus.AWAITING_CODEX_REVIEW if pending else BacklinkAnalysisStatus.COMPLETE
                if not pending:
                    run.completed_at = utcnow()
                try:
                    after = client.subscription().balance
                    run.balance_after = after
                    _store_balance(session, balance=after, status="ok")
                    if run.balance_before is not None and after is not None:
                        observed = max(0, run.balance_before - after)
                        if observed != run.actual_credits:
                            session.add(BacklinkCreditUsage(run_id=run.id, event_type="balance_reconciliation", predicted_credits=run.actual_credits, actual_credits=0, balance_before=run.balance_before, balance_after=after, observed_delta=observed, mismatch=True))
                except SERankingError:
                    pass
                session.add(AuditLog(actor_type="worker", actor_id="backlink_worker", action="backlink_analysis.review_ready" if pending else "backlink_analysis.completed", entity_type="backlink_analysis", entity_id=run.id, after=run.counters))
                session.commit()
                return dict(run.counters)
    except CreditStateUnknown as exc:
        with factory() as session:
            run = session.get(BacklinkAnalysisRun, run_id)
            run.status = BacklinkAnalysisStatus.CREDIT_STATE_UNKNOWN
            run.error_code = exc.code
            run.error_detail = str(exc)
            request = session.scalar(select(BacklinkProviderRequest).where(BacklinkProviderRequest.run_id == run_id, BacklinkProviderRequest.status == "started").order_by(BacklinkProviderRequest.started_at.desc()).limit(1))
            if request:
                request.status = "credit_state_unknown"
                request.error_code = exc.code
                request.error_detail = str(exc)
                target = session.get(BacklinkAnalysisTarget, request.target_id)
                if target:
                    target.status = "credit_state_unknown"
                    target.error_code = exc.code
                    target.error_detail = str(exc)
            session.commit()
        return {"credit_state_unknown": True}
    except Exception as exc:
        with factory() as session:
            run = session.get(BacklinkAnalysisRun, run_id)
            if run and run.status != BacklinkAnalysisStatus.CANCELLED:
                run.status = BacklinkAnalysisStatus.FAILED
                run.error_code = getattr(exc, "code", "analysis_failed")
                run.error_detail = str(exc)[:1000]
                run.completed_at = utcnow()
                request = session.scalar(select(BacklinkProviderRequest).where(BacklinkProviderRequest.run_id == run_id, BacklinkProviderRequest.status == "started").order_by(BacklinkProviderRequest.started_at.desc()).limit(1))
                if request:
                    request.status = "failed"
                    request.error_code = run.error_code
                    request.error_detail = run.error_detail
                    request.completed_at = utcnow()
                    target = session.get(BacklinkAnalysisTarget, request.target_id)
                    if target:
                        target.status = "failed"
                        target.error_code = run.error_code
                        target.error_detail = run.error_detail
                        target.completed_at = utcnow()
                session.commit()
        raise


def _analysis_import_path(import_id: str) -> Path:
    root = Path(os.getenv("LINK_OS_DATA_DIR", Path(__file__).resolve().parents[2] / "data" / "platform")) / "imports"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"backlinks-{import_id}.txt"


def apply_reviews(
    session: Session,
    *,
    run_id: str,
    decisions: Sequence[dict[str, Any]],
    actor_type: str,
    actor_id: str,
) -> dict[str, Any]:
    run = session.get(BacklinkAnalysisRun, run_id)
    if not run:
        raise BacklinkValidationError("analysis run not found")
    approved: list[BacklinkCandidate] = []
    applied = 0
    for payload in decisions:
        candidate = session.get(BacklinkCandidate, str(payload.get("candidate_id") or ""))
        if not candidate or candidate.run_id != run.id:
            raise BacklinkValidationError("candidate does not belong to this analysis")
        decision = str(payload.get("decision") or "").strip().lower()
        if decision not in {"approved", "rejected", "manual_review"}:
            raise BacklinkValidationError("decision must be approved, rejected or manual_review")
        confidence = float(payload.get("confidence", 0))
        reasons = [str(item)[:80] for item in payload.get("reason_codes", []) if str(item).strip()]
        summary = str(payload.get("summary") or "").strip()[:1000]
        if not summary or not reasons or not 0 <= confidence <= 1:
            raise BacklinkValidationError("each review needs confidence, reason_codes and a short evidence summary")
        if decision == "approved":
            if candidate.status == BacklinkCandidateStatus.HARD_REJECTED:
                raise BacklinkValidationError("hard-excluded protected candidates cannot be approved")
            if run.mode != BacklinkAnalysisMode.COMPETITOR_PROSPECTING:
                raise BacklinkValidationError("client profile audits cannot approve outreach prospects")
            if confidence < 0.75:
                raise BacklinkValidationError("approved candidates require confidence of at least 0.75")
            if candidate.status == BacklinkCandidateStatus.IMPORTED:
                new_status = BacklinkCandidateStatus.IMPORTED
            elif candidate.import_id:
                new_status = BacklinkCandidateStatus.IMPORT_QUEUED
            else:
                new_status = BacklinkCandidateStatus.APPROVED
                approved.append(candidate)
        elif decision == "rejected":
            new_status = BacklinkCandidateStatus.REJECTED
        else:
            new_status = BacklinkCandidateStatus.MANUAL_REVIEW
        prior = candidate.status.value
        candidate.status = new_status
        candidate.codex_confidence = confidence
        candidate.reason_codes = reasons
        candidate.decision_summary = summary
        candidate.decided_by = actor_id
        candidate.decided_at = utcnow()
        session.add(BacklinkReviewDecision(run_id=run.id, candidate_id=candidate.id, prior_status=prior, decision=decision, confidence=confidence, reason_codes=reasons, summary=summary, actor_type=actor_type, actor_id=actor_id))
        session.add(AuditLog(actor_type=actor_type, actor_id=actor_id, action="backlink_candidate.reviewed", entity_type="backlink_candidate", entity_id=candidate.id, before={"status": prior}, after={"status": new_status.value, "confidence": confidence, "reason_codes": reasons}, reason=summary))
        applied += 1
    session.flush()

    import_batch = None
    to_import = [candidate for candidate in approved if candidate.import_id is None]
    if to_import:
        import_batch = ImportBatch(source_type="backlink_discovery", source_metadata={"backlink_analysis_id": run.id, "queue": True, "actor": actor_id})
        session.add(import_batch)
        session.flush()
        path = _analysis_import_path(import_batch.id)
        path.write_text("\n".join(sorted({candidate.normalized_domain for candidate in to_import})) + "\n", encoding="utf-8")
        import_batch.filename = path.name
        import_batch.source_metadata = {**import_batch.source_metadata, "input_path": str(path)}
        JobQueue(session).enqueue(kind="process_import", idempotency_key=f"process_import:{import_batch.id}", subject_type="import", subject_id=import_batch.id, payload={"import_id": import_batch.id, "path": str(path), "queue": True}, priority=45, max_attempts=3)
        run.import_id = import_batch.id
        for candidate in to_import:
            candidate.import_id = import_batch.id
            candidate.status = BacklinkCandidateStatus.IMPORT_QUEUED
        run.status = BacklinkAnalysisStatus.QUEUEING_SCRAPE
        session.add(AuditLog(actor_type=actor_type, actor_id=actor_id, action="backlink_analysis.import_queued", entity_type="backlink_analysis", entity_id=run.id, after={"import_id": import_batch.id, "domains": len(to_import)}))
    _refresh_counters(session, run)
    if import_batch is None:
        if run.counters.get("pending_review", 0):
            run.status = BacklinkAnalysisStatus.AWAITING_CODEX_REVIEW
            run.completed_at = None
        else:
            run.status = BacklinkAnalysisStatus.COMPLETE
            run.completed_at = run.completed_at or utcnow()
    session.flush()
    return {"applied": applied, "approved": len(approved), "import_id": import_batch.id if import_batch else run.import_id, "status": run.status.value}


def mark_import_complete(session: Session, import_batch: ImportBatch) -> None:
    run_id = str((import_batch.source_metadata or {}).get("backlink_analysis_id") or "")
    if not run_id:
        return
    run = session.get(BacklinkAnalysisRun, run_id)
    if not run:
        return
    candidates = list(session.scalars(select(BacklinkCandidate).where(BacklinkCandidate.run_id == run.id, BacklinkCandidate.import_id == import_batch.id)))
    for candidate in candidates:
        candidate.status = BacklinkCandidateStatus.IMPORTED
        domain = session.scalar(select(Domain).where(Domain.normalized_domain == candidate.normalized_domain))
        if domain:
            candidate.existing_domain_id = domain.id
    # Candidate status changes must be visible to the grouped counter query in
    # the same transaction on every supported SQLAlchemy/database runtime.
    session.flush()
    _refresh_counters(session, run)
    pending = int(run.counters.get("pending_review", 0))
    if pending:
        run.status = BacklinkAnalysisStatus.AWAITING_CODEX_REVIEW
        run.completed_at = None
        action = "backlink_analysis.review_ready"
    else:
        run.status = BacklinkAnalysisStatus.COMPLETE
        run.completed_at = utcnow()
        action = "backlink_analysis.completed"
    session.add(AuditLog(actor_type="worker", actor_id="import_worker", action=action, entity_type="backlink_analysis", entity_id=run.id, after={**run.counters, "import_id": import_batch.id}))


def cancel_analysis(session: Session, run: BacklinkAnalysisRun, *, actor: str) -> None:
    if run.status in {BacklinkAnalysisStatus.COMPLETE, BacklinkAnalysisStatus.CANCELLED}:
        raise BacklinkValidationError("completed or cancelled runs cannot be cancelled")
    if run.status == BacklinkAnalysisStatus.FETCHING:
        run.counters = {**run.counters, "cancel_requested": True}
    else:
        run.status = BacklinkAnalysisStatus.CANCELLED
        run.completed_at = utcnow()
        for job in session.scalars(select(Job).where(Job.subject_type == "backlink_analysis", Job.subject_id == run.id, Job.status == JobStatus.QUEUED)):
            job.status = JobStatus.CANCELLED
    session.add(AuditLog(actor_type="user", actor_id=actor, action="backlink_analysis.cancel_requested", entity_type="backlink_analysis", entity_id=run.id))


def retry_analysis(session: Session, run: BacklinkAnalysisRun, *, actor: str, acknowledge_unknown_credit: bool = False) -> Job:
    if run.status not in {BacklinkAnalysisStatus.FAILED, BacklinkAnalysisStatus.PARTIAL, BacklinkAnalysisStatus.CREDIT_STATE_UNKNOWN}:
        raise BacklinkValidationError("only failed, partial or credit-unknown runs can be retried")
    if run.status == BacklinkAnalysisStatus.CREDIT_STATE_UNKNOWN and not acknowledge_unknown_credit:
        raise BacklinkValidationError("credit_state_unknown retries require explicit acknowledgement")
    run.status = BacklinkAnalysisStatus.QUEUED
    run.completed_at = None
    run.error_code = None
    run.error_detail = None
    job, _ = JobQueue(session).enqueue(kind="backlink_analysis", idempotency_key=f"backlink_analysis:{run.id}:retry:{utcnow().isoformat()}", subject_type="backlink_analysis", subject_id=run.id, payload={"run_id": run.id}, priority=40, max_attempts=1)
    session.add(AuditLog(actor_type="user", actor_id=actor, action="backlink_analysis.retried", entity_type="backlink_analysis", entity_id=run.id, after={"acknowledged_unknown_credit": acknowledge_unknown_credit}))
    return job
