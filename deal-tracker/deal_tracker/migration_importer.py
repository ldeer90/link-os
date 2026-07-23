"""Materialise read-only legacy snapshots into the canonical PostgreSQL model.

The migration CLI has already selected one winning record per natural key.  This
module adds a second idempotency boundary inside PostgreSQL and maps every known
legacy category either to its canonical table or to a retained provenance row.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from deal_tracker.platform.enums import (
    ImportItemStatus,
    ImportStatus,
    JobStatus,
    LifecycleStage,
    ListingStatus,
    OfferStatus,
    ScrapeStatus,
    WorkerRunStatus,
)
from deal_tracker.platform.models import (
    AgencyEnquiry,
    AuditLog,
    CatalogueListing,
    Contact,
    Domain,
    DomainContactEvidence,
    DomainEvent,
    ImportBatch,
    ImportItem,
    Job,
    MigrationSourceRecord,
    MondayMapping,
    Offer,
    PublisherDomain,
    PublisherEntity,
    Reply,
    ScrapeAttempt,
    Suppression,
    SystemSetting,
    UserAccount,
    WorkerRun,
    utcnow,
)
from deal_tracker.platform.normalization import normalize_domain, normalize_email
from deal_tracker.platform.pricing import calculate_reseller_price
from deal_tracker.platform.repositories import DomainRepository, JobQueue


LIFECYCLE_RANK = {stage: index for index, stage in enumerate(LifecycleStage)}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return _text(value).lower() in {"1", "true", "yes", "on", "active", "listed"}


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result > 0 else None


def _datetime(value: Any, *, fallback: datetime | None = None) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        raw = _text(value)
        try:
            result = datetime.fromisoformat(raw.replace("Z", "+00:00")) if raw else (fallback or utcnow())
        except ValueError:
            result = fallback or utcnow()
    return result if result.tzinfo else result.replace(tzinfo=timezone.utc)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _domain_value(payload: Mapping[str, Any]) -> str:
    for key in ("root_domain", "publisher_url", "source_url", "url", "evidence_url", "primary_domain"):
        value = _text(payload.get(key))
        if value:
            try:
                return normalize_domain(value).registrable_domain
            except ValueError:
                continue
    return ""


def _email_value(payload: Mapping[str, Any]) -> str:
    for key in ("email", "contact_email", "lead_email", "primary_email", "from_email"):
        value = _text(payload.get(key))
        if value:
            try:
                return normalize_email(value)
            except ValueError:
                continue
    return ""


def _public_url(value: Any) -> str:
    raw = _text(value)
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw if "://" in raw else f"https://{raw}")
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    return raw if "://" in raw else f"https://{raw}"


def _source_rank(record: Mapping[str, Any]) -> tuple[Any, ...]:
    category = _text(record.get("category"))
    manual = 1 if _bool(record.get("manual_authoritative")) else 0
    precedence = int(record.get("source_precedence") or 0)
    freshness = _text(record.get("freshness"))
    return (manual, freshness, precedence) if category == "users" else (manual, precedence, freshness)


def _stored_rank(record: MigrationSourceRecord) -> tuple[Any, ...]:
    manual = 1 if record.manual_authoritative else 0
    if record.category == "users":
        return manual, record.freshness, record.source_precedence
    return manual, record.source_precedence, record.freshness


def _ensure_domain(session: Session, value: str, payload: Mapping[str, Any], source: str) -> Domain | None:
    if not value:
        return None
    try:
        domain, _created = DomainRepository(session).get_or_create(value, source=source)
    except ValueError:
        return None
    attributes = dict(domain.attributes or {})
    attributes.setdefault("migration_sources", [])
    if source not in attributes["migration_sources"]:
        attributes["migration_sources"] = [*attributes["migration_sources"], source]
    for key in ("site_name", "niche", "opportunity_type", "quality_tier", "notes", "source_name"):
        if payload.get(key) not in (None, ""):
            attributes.setdefault(key, payload.get(key))
    domain.attributes = attributes
    return domain


def _ensure_contact(session: Session, email: str, payload: Mapping[str, Any], source: str) -> Contact | None:
    if not email:
        return None
    try:
        normalized = normalize_email(email)
    except ValueError:
        return None
    contact = session.scalar(select(Contact).where(Contact.normalized_email == normalized))
    if contact is None:
        contact = Contact(
            normalized_email=normalized,
            original_email=email,
            email_domain=normalized.rsplit("@", 1)[-1],
            role_class=_text(payload.get("email_type")) or _text(payload.get("role_class")) or None,
            is_guessed=False,
            attributes={"migration_source": source},
        )
        session.add(contact)
        session.flush()
    return contact


def _advance_stage(session: Session, domain: Domain, target: LifecycleStage, source: str, reason: str) -> None:
    if LIFECYCLE_RANK[target] <= LIFECYCLE_RANK[domain.lifecycle_stage]:
        return
    before = domain.lifecycle_stage
    domain.lifecycle_stage = target
    domain.stage_changed_at = utcnow()
    if LIFECYCLE_RANK[target] >= LIFECYCLE_RANK[LifecycleStage.CONTACTED] and domain.last_contacted_at is None:
        domain.last_contacted_at = utcnow()
    session.add(
        DomainEvent(
            domain_id=domain.id,
            event_type="legacy_state_imported",
            from_stage=before,
            to_stage=target,
            source=source,
            reason=reason,
        )
    )


def _publisher(
    session: Session,
    *,
    payload: Mapping[str, Any],
    contact: Contact | None = None,
    domain: Domain | None = None,
    source: str,
) -> PublisherEntity:
    key = _text(payload.get("group_key")).lower()
    if not key and contact:
        key = f"email:{contact.normalized_email}"
    if not key and domain:
        key = f"domain:{domain.normalized_domain}"
    if not key:
        key = f"legacy:{hashlib.sha256(repr(sorted(payload.items())).encode()).hexdigest()[:24]}"
    entity = session.scalar(select(PublisherEntity).where(PublisherEntity.normalized_key == key))
    if entity is None:
        entity = PublisherEntity(
            canonical_name=_text(payload.get("name") or payload.get("site_name"))
            or (domain.normalized_domain if domain else "Legacy publisher"),
            normalized_key=key[:255],
            primary_contact_id=contact.id if contact else None,
            notes=_text(payload.get("notes")) or None,
            attributes={"migration_source": source, "legacy_id": payload.get("id")},
        )
        session.add(entity)
        session.flush()
    elif contact and entity.primary_contact_id is None:
        entity.primary_contact_id = contact.id
    if domain and session.scalar(select(PublisherDomain.id).where(PublisherDomain.domain_id == domain.id)) is None:
        session.add(
            PublisherDomain(
                publisher_id=entity.id,
                domain_id=domain.id,
                relationship_type="represented",
                confidence=1.0,
            )
        )
    return entity


def _legacy_reply_for_offer(
    session: Session,
    *,
    natural_key: str,
    payload: Mapping[str, Any],
    domain: Domain,
    contact: Contact | None,
) -> Reply:
    thread_id = _text(payload.get("instantly_thread_id"))
    reply = None
    if thread_id:
        reply = session.scalar(
            select(Reply).where(Reply.external_thread_id == thread_id).order_by(Reply.received_at.desc()).limit(1)
        )
    if reply is None:
        external_id = f"legacy-offer:{hashlib.sha256(natural_key.encode()).hexdigest()}"
        reply = session.scalar(select(Reply).where(Reply.external_email_id == external_id))
    if reply is None:
        reply = Reply(
            external_email_id=external_id,
            external_thread_id=thread_id or None,
            domain_id=domain.id,
            contact_id=contact.id if contact else None,
            from_address=contact.normalized_email if contact else None,
            subject="Migrated publisher offer",
            body_text=_text(payload.get("reply_evidence_text") or payload.get("reply_summary")),
            received_at=_datetime(payload.get("last_seen_from_instantly") or payload.get("updated_at") or payload.get("created_at")),
            thread_evidence={"source": "legacy_deal", "summary": _text(payload.get("reply_summary"))},
            processed_at=utcnow(),
        )
        session.add(reply)
        session.flush()
    return reply


def _setting(session: Session, key: str, payload: Mapping[str, Any], source: str) -> SystemSetting:
    safe_key = key if len(key) <= 255 else f"legacy:{hashlib.sha256(key.encode()).hexdigest()}"
    setting = session.get(SystemSetting, safe_key)
    value = {"source": source, "payload": dict(payload)}
    if setting is None:
        setting = SystemSetting(key=safe_key, value=value, updated_by="migration")
        session.add(setting)
    else:
        setting.value = value
        setting.version += 1
        setting.updated_by = "migration"
    session.flush()
    return setting


def _materialise(
    session: Session,
    *,
    category: str,
    natural_key: str,
    payload: Mapping[str, Any],
    source: str,
) -> tuple[str, str] | None:
    domain_name = _domain_value(payload)
    email = _email_value(payload)

    if category == "domains":
        domain = _ensure_domain(session, domain_name, payload, source)
        if domain is None:
            return None
        stage = _text(payload.get("lifecycle_stage"))
        if stage in {item.value for item in LifecycleStage}:
            _advance_stage(session, domain, LifecycleStage(stage), source, "legacy domain state")
        return "domain", domain.id

    if category == "contacts":
        contact = _ensure_contact(session, email, payload, source)
        return ("contact", contact.id) if contact else None

    if category == "domain_contacts":
        domain = _ensure_domain(session, domain_name, payload, source)
        contact = _ensure_contact(session, email, payload, source)
        evidence_url = _public_url(
            payload.get("evidence_url") or payload.get("email_source_url") or payload.get("source_url")
        )
        if not domain or not contact or not evidence_url:
            return None
        evidence_key = hashlib.sha256(f"{domain.id}|{contact.id}|{evidence_url}".encode()).hexdigest()
        evidence = session.scalar(
            select(DomainContactEvidence).where(DomainContactEvidence.evidence_key == evidence_key)
        )
        if evidence is None:
            evidence = DomainContactEvidence(
                domain_id=domain.id,
                contact_id=contact.id,
                evidence_key=evidence_key,
                source_url=evidence_url,
                context_snippet=_text(payload.get("matched_phrase")) or None,
                confidence=float(payload.get("confidence") or 0),
                is_public_page=True,
            )
            session.add(evidence)
            session.flush()
        return "domain_contact_evidence", evidence.id

    if category in {"domain_evidence", "candidate_urls"}:
        domain = _ensure_domain(session, domain_name, payload, source)
        if domain is None:
            return None
        event = DomainEvent(
            domain_id=domain.id,
            event_type="legacy_domain_evidence" if category == "domain_evidence" else "legacy_candidate_url",
            source=source,
            reason=_text(payload.get("matched_phrase") or payload.get("reason")) or None,
            payload={
                "url": _text(payload.get("evidence_url") or payload.get("url")),
                "source_type": payload.get("source_type"),
                "status": payload.get("status"),
            },
            created_at=_datetime(payload.get("crawl_timestamp") or payload.get("discovered_at")),
        )
        session.add(event)
        session.flush()
        return "domain_event", event.id

    if category == "suppressions":
        domain = _ensure_domain(session, domain_name, payload, source)
        if domain is None:
            return None
        existing = session.scalar(
            select(Suppression).where(Suppression.domain_id == domain.id, Suppression.active.is_(True)).limit(1)
        )
        if existing is None:
            existing = Suppression(
                domain_id=domain.id,
                scope="domain",
                reason=_text(payload.get("reason")) or "legacy_suppression",
                source=source,
                permanent=True,
                active=True,
            )
            session.add(existing)
            session.flush()
        _advance_stage(session, domain, LifecycleStage.SUPPRESSED, source, "legacy suppression")
        return "suppression", existing.id

    if category == "imports":
        batch = ImportBatch(
            source_type=_text(payload.get("source_type")) or "legacy",
            filename=_text(payload.get("source_name")) or source,
            status=ImportStatus.COMPLETE,
            total_rows=int(payload.get("total_rows") or 0),
            processed_rows=int(payload.get("total_rows") or 0),
            accepted_count=int(payload.get("accepted_rows") or 0),
            duplicate_count=int(payload.get("duplicate_rows") or 0),
            invalid_count=int(payload.get("rejected_rows") or 0),
            source_metadata={"legacy_source": source, "legacy_id": payload.get("id")},
            completed_at=_datetime(payload.get("created_at")),
        )
        session.add(batch)
        session.flush()
        return "import", batch.id

    if category == "import_rows":
        legacy_import_id = _text(payload.get("import_id"))
        source_row = session.scalar(
            select(MigrationSourceRecord)
            .where(
                MigrationSourceRecord.category == "imports",
                MigrationSourceRecord.source_label == source,
                MigrationSourceRecord.natural_key.like(f"%:{legacy_import_id}"),
            )
            .limit(1)
        )
        if source_row is None or not source_row.target_id:
            return None
        domain = _ensure_domain(session, domain_name, payload, source)
        contact = _ensure_contact(session, email, payload, source)
        raw_status = _text(payload.get("row_status"))
        status_map = {
            "accepted": ImportItemStatus.ACCEPTED,
            "duplicate": ImportItemStatus.DUPLICATE,
            "rejected": ImportItemStatus.INVALID,
            "invalid": ImportItemStatus.INVALID,
            "queued": ImportItemStatus.ACCEPTED,
        }
        row_number = int(payload.get("id") or 0) or int(
            session.scalar(select(func.count(ImportItem.id)).where(ImportItem.import_id == source_row.target_id)) or 0
        ) + 1
        item = ImportItem(
            import_id=source_row.target_id,
            row_number=row_number,
            raw_value=_text(payload.get("input_value")),
            normalized_value=domain.normalized_domain if domain else None,
            status=status_map.get(raw_status, ImportItemStatus.INVALID),
            domain_id=domain.id if domain else None,
            contact_id=contact.id if contact else None,
            explanation=_text(payload.get("reason")) or None,
            row_metadata={"legacy_source": source},
            created_at=_datetime(payload.get("created_at")),
        )
        session.add(item)
        session.flush()
        return "import_item", item.id

    if category == "scrape_jobs":
        domain = _ensure_domain(session, domain_name, payload, source)
        digest = hashlib.sha256(natural_key.encode()).hexdigest()
        job, _created = JobQueue(session).enqueue(
            kind="legacy_scrape_record",
            idempotency_key=f"legacy_scrape:{digest}",
            subject_type="domain" if domain else None,
            subject_id=domain.id if domain else None,
            payload={"source": source, "url": _text(payload.get("url"))},
            max_attempts=max(1, int(payload.get("attempts") or 1)),
        )
        raw_status = _text(payload.get("status")).lower()
        job.status = JobStatus.SUCCEEDED if raw_status in {"done", "complete", "completed", "success", "succeeded"} else (
            JobStatus.DEAD if raw_status in {"failed", "error"} else JobStatus.CANCELLED
        )
        job.completed_at = _datetime(payload.get("updated_at"))
        job.last_error = _text(payload.get("last_error")) or None
        session.flush()
        return "job", job.id

    if category == "scrape_attempts":
        domain = _ensure_domain(session, domain_name, payload, source)
        if domain is None:
            return None
        number = int(session.scalar(select(func.count(ScrapeAttempt.id)).where(ScrapeAttempt.domain_id == domain.id)) or 0) + 1
        raw_status = _text(payload.get("status")).lower()
        status = ScrapeStatus.SUCCEEDED if raw_status in {"success", "succeeded", "done", "complete"} else (
            ScrapeStatus.NO_EMAIL if raw_status in {"no_email", "empty"} else ScrapeStatus.FAILED
        )
        attempt = ScrapeAttempt(
            domain_id=domain.id,
            attempt_number=number,
            status=status,
            started_at=_datetime(payload.get("crawled_at")),
            completed_at=_datetime(payload.get("crawled_at")),
            pages_requested=1,
            pages_crawled=1 if status != ScrapeStatus.FAILED else 0,
            emails_found=0,
            error_detail=_text(payload.get("error_message")) or None,
            metrics={"url": _text(payload.get("url")), "final_url": _text(payload.get("final_url"))},
        )
        session.add(attempt)
        domain.last_scraped_at = attempt.completed_at
        session.flush()
        return "scrape_attempt", attempt.id

    if category == "replies":
        external_id = _text(payload.get("email_id")) or natural_key
        reply = session.scalar(select(Reply).where(Reply.external_email_id == external_id))
        if reply is None:
            contact = _ensure_contact(session, email, payload, source)
            reply = Reply(
                external_email_id=external_id,
                external_thread_id=_text(payload.get("thread_id")) or None,
                contact_id=contact.id if contact else None,
                from_address=_text(payload.get("from_email")) or None,
                to_address=_text(payload.get("to_email")) or None,
                subject=_text(payload.get("subject")) or None,
                body_text=_text(payload.get("body_text")),
                received_at=_datetime(payload.get("received_at") or payload.get("processed_at")),
                thread_evidence={
                    "source": source,
                    "campaign_id": payload.get("campaign_id"),
                    "classification": payload.get("classification"),
                    "extracted": _json_object(payload.get("extracted_json")),
                },
                processed_at=_datetime(payload.get("processed_at")),
            )
            session.add(reply)
            session.flush()
        return "reply", reply.id

    if category == "publisher_entities":
        contact = _ensure_contact(session, email, payload, source)
        domain = _ensure_domain(session, domain_name, payload, source)
        entity = _publisher(session, payload=payload, contact=contact, domain=domain, source=source)
        return "publisher_entity", entity.id

    if category == "offers":
        domain = _ensure_domain(session, domain_name, payload, source)
        if domain is None:
            return None
        contact = _ensure_contact(session, email, payload, source)
        reply = _legacy_reply_for_offer(
            session, natural_key=natural_key, payload=payload, domain=domain, contact=contact
        )
        entity = _publisher(session, payload=payload, contact=contact, domain=domain, source=source)
        review_status = _text(payload.get("admin_review_status")).lower()
        deal_status = _text(payload.get("deal_status")).lower()
        approved = _bool(payload.get("is_listed")) or review_status in {"approved", "listed", "confirmed"}
        if deal_status in {"lost", "rejected", "declined"}:
            status = OfferStatus.LOST if deal_status == "lost" else OfferStatus.REJECTED
        else:
            status = OfferStatus.APPROVED if approved else OfferStatus.REVIEW
        amount = _decimal(payload.get("publisher_cost_amount")) or _decimal(payload.get("price_amount"))
        currency = _text(payload.get("publisher_cost_currency") or payload.get("price_currency") or "AUD").upper()[:3]
        reseller = _decimal(payload.get("reseller_price_amount"))
        reseller_currency = _text(payload.get("reseller_price_currency") or "AUD").upper()
        cost_aud = amount if amount is not None and currency == "AUD" else None
        if reseller is None and cost_aud is not None:
            reseller = calculate_reseller_price(
                amount=amount,
                currency="AUD",
                fx_rate_to_aud=Decimal("1"),
                fx_rate_date=_datetime(payload.get("updated_at")).date(),
            ).reseller_price_aud
            reseller_currency = "AUD"
        offer = Offer(
            reply_id=reply.id,
            domain_id=domain.id,
            publisher_id=entity.id,
            status=status,
            placement_type=_text(payload.get("placement_type")) or "other",
            original_amount=amount,
            original_currency=currency if amount else None,
            extraction_confidence=1.0 if approved else 0.5,
            cost_aud=cost_aud,
            fx_rate_to_aud=Decimal("1") if cost_aud is not None else None,
            fx_rate_date=_datetime(payload.get("updated_at") or payload.get("created_at")).date() if cost_aud is not None else None,
            reseller_price_aud=reseller if reseller_currency == "AUD" else None,
            pricing_rule_version="legacy-manual" if reseller is not None else None,
            extracted_terms={
                key: payload.get(key)
                for key in (
                    "price_notes", "writing_requirements", "link_requirements", "turnaround_time",
                    "link_insertion_notes", "additional_notes", "industry", "domain_trust", "quality_notes",
                )
                if payload.get(key) not in (None, "")
            },
            manual_override=True,
            approved_at=_datetime(payload.get("updated_at")) if approved else None,
            approved_by="legacy-admin" if approved else None,
        )
        session.add(offer)
        session.flush()
        _advance_stage(
            session,
            domain,
            LifecycleStage.OFFER_APPROVED if approved else LifecycleStage.OFFER_REVIEW,
            source,
            "legacy offer",
        )
        return "offer", offer.id

    if category == "catalogue_listings":
        offer_source = session.scalar(
            select(MigrationSourceRecord).where(
                MigrationSourceRecord.category == "offers",
                MigrationSourceRecord.natural_key == natural_key,
            )
        )
        if offer_source is None or not offer_source.target_id:
            return None
        offer = session.get(Offer, offer_source.target_id)
        if offer is None or offer.reseller_price_aud is None:
            return None
        listing = session.scalar(select(CatalogueListing).where(CatalogueListing.offer_id == offer.id))
        if listing is None:
            listing = CatalogueListing(
                offer_id=offer.id,
                publisher_id=offer.publisher_id,
                domain_id=offer.domain_id,
                status=ListingStatus.LISTED,
                visibility_tier=_text(payload.get("visibility_min_tier")) or "basic",
                title=_text(payload.get("site_name")) or None,
                agency_price_aud=offer.reseller_price_aud,
                public_terms={
                    key: payload.get(key)
                    for key in ("industry", "domain_trust", "quality_notes", "writing_requirements", "link_requirements", "turnaround_time")
                    if payload.get(key) not in (None, "")
                },
                listed_at=_datetime(payload.get("updated_at") or payload.get("created_at")),
            )
            session.add(listing)
            session.flush()
        domain = session.get(Domain, offer.domain_id)
        if domain:
            _advance_stage(session, domain, LifecycleStage.LISTED, source, "legacy catalogue listing")
        return "catalogue_listing", listing.id

    if category == "users":
        if not email or not _text(payload.get("password_hash")):
            return None
        user = session.scalar(select(UserAccount).where(UserAccount.email == email))
        if user is None:
            user = UserAccount(
                email=email,
                password_hash=_text(payload.get("password_hash")),
                role=_text(payload.get("role")) or "agency",
                visibility_tier=_text(payload.get("visibility_tier")) or "basic",
                is_active=_bool(payload.get("is_active")),
                last_login_at=_datetime(payload.get("last_login_at")) if payload.get("last_login_at") else None,
                source_metadata={"source": source, "legacy_id": payload.get("id")},
            )
            session.add(user)
        else:
            user.password_hash = _text(payload.get("password_hash"))
            user.role = _text(payload.get("role")) or user.role
            user.visibility_tier = _text(payload.get("visibility_tier")) or user.visibility_tier
            user.is_active = _bool(payload.get("is_active"))
        session.flush()
        return "user_account", str(user.id)

    if category == "agency_enquiries":
        legacy_user_id = payload.get("user_id")
        legacy_deal_id = payload.get("deal_id")
        users = list(session.scalars(select(UserAccount)))
        user = next((item for item in users if (item.source_metadata or {}).get("legacy_id") == legacy_user_id), None)
        listing_sources = list(
            session.scalars(select(MigrationSourceRecord).where(MigrationSourceRecord.category == "catalogue_listings"))
        )
        listing_source = next((item for item in listing_sources if (item.source_payload or {}).get("id") == legacy_deal_id), None)
        if user is None or listing_source is None or not listing_source.target_id:
            return None
        enquiry = AgencyEnquiry(
            user_id=user.id,
            listing_id=listing_source.target_id,
            message=_text(payload.get("message")),
            status=_text(payload.get("status")) or "new",
            created_at=_datetime(payload.get("created_at")),
        )
        session.add(enquiry)
        session.flush()
        return "agency_enquiry", str(enquiry.id)

    if category == "monday_mappings":
        board_key = _text(payload.get("board_key")) or "legacy"
        board_setting = session.get(SystemSetting, f"legacy:monday_board:{board_key}")
        board_id = _text((board_setting.value.get("payload") if board_setting else {}).get("board_id")) or f"legacy:{board_key}"
        item_id = _text(payload.get("monday_item_id"))
        if not item_id:
            return None
        mapping = session.scalar(select(MondayMapping).where(MondayMapping.stable_key == natural_key))
        if mapping is None:
            mapping = MondayMapping(
                stable_key=natural_key[:255],
                entity_type=_text(payload.get("object_type")) or "legacy",
                entity_id=_text(payload.get("local_id"))[:36] or "legacy",
                board_key=board_key,
                external_board_id=board_id,
                external_item_id=item_id,
                last_payload_hash=hashlib.sha256(_text(payload.get("raw_json")).encode()).hexdigest(),
                last_synced_at=_datetime(payload.get("updated_at")),
                sync_status="imported",
            )
            session.add(mapping)
            session.flush()
        return "monday_mapping", mapping.id

    if category == "worker_runs":
        raw_status = _text(payload.get("status")).lower()
        status = WorkerRunStatus.SUCCEEDED if raw_status in {"complete", "completed", "success", "succeeded"} else (
            WorkerRunStatus.RUNNING if raw_status == "running" else WorkerRunStatus.FAILED
        )
        run = WorkerRun(
            worker_name=f"legacy:{source}",
            run_type=_text(payload.get("trigger_type") or payload.get("action")) or "legacy",
            status=status,
            started_at=_datetime(payload.get("started_at")),
            completed_at=_datetime(payload.get("finished_at")) if payload.get("finished_at") else None,
            counters={
                "inbound_reply_count": payload.get("inbound_reply_count"),
                "new_deal_count": payload.get("new_deal_count"),
            },
            error=_text(payload.get("error_message")) or None,
        )
        session.add(run)
        session.flush()
        return "worker_run", run.id

    if category == "audit_logs":
        log = AuditLog(
            actor_type="legacy_worker",
            actor_id=source,
            action=f"legacy.{_text(payload.get('event_type') or payload.get('stage')) or 'event'}"[:120],
            entity_type="legacy_run",
            entity_id=_text(payload.get("sync_run_id") or payload.get("run_id")) or None,
            after={
                "level": payload.get("level"),
                "message": _text(payload.get("message"))[:4000],
                "payload": _json_object(payload.get("payload_json")),
            },
            created_at=_datetime(payload.get("created_at")),
        )
        session.add(log)
        session.flush()
        return "audit_log", log.id

    if category in {
        "campaign_checkpoints", "campaign_drafts", "monday_boards", "monday_thread_comments"
    }:
        prefix = {
            "campaign_checkpoints": "legacy:campaign_checkpoint",
            "campaign_drafts": "legacy:campaign_draft",
            "monday_boards": "legacy:monday_board",
            "monday_thread_comments": "legacy:monday_thread_comment",
        }[category]
        key_part = _text(payload.get("board_key")) if category == "monday_boards" else natural_key
        setting = _setting(session, f"{prefix}:{key_part}", payload, source)
        return "system_setting", setting.key

    return None


def import_migration_records(
    session: Session,
    records: list[Mapping[str, Any]],
    *,
    migration_id: str,
) -> dict[str, Any]:
    """Import at most 1,000 planner records without exposing their payloads."""

    if not migration_id:
        raise ValueError("migration_id is required")
    if not 1 <= len(records) <= 1_000:
        raise ValueError("migration record batches must contain 1 to 1000 records")

    counts: Counter[str] = Counter()
    categories: Counter[str] = Counter()
    for raw in records:
        category = _text(raw.get("category"))
        natural_key = _text(raw.get("natural_key"))
        payload = raw.get("payload")
        if not category or not natural_key or not isinstance(payload, Mapping):
            counts["invalid"] += 1
            continue
        categories[category] += 1
        source = _text(raw.get("source_label")) or "legacy"
        source_row = session.scalar(
            select(MigrationSourceRecord).where(
                MigrationSourceRecord.category == category,
                MigrationSourceRecord.natural_key == natural_key,
            )
        )
        if source_row is not None and source_row.status == "materialized" and _stored_rank(source_row) >= _source_rank(raw):
            counts["skipped"] += 1
            continue

        if source_row is None:
            source_row = MigrationSourceRecord(
                migration_id=migration_id,
                category=category,
                natural_key=natural_key,
                source_label=source,
                source_precedence=int(raw.get("source_precedence") or 0),
                source_table=_text(raw.get("source_table")) or "unknown",
                source_payload={},
                manual_authoritative=_bool(raw.get("manual_authoritative")),
                freshness=_text(raw.get("freshness")),
                status="accepted",
            )
            session.add(source_row)
            session.flush()
            counts["accepted"] += 1
        else:
            counts["updated"] += 1

        source_row.migration_id = migration_id
        source_row.source_label = source
        source_row.source_precedence = int(raw.get("source_precedence") or 0)
        source_row.source_table = _text(raw.get("source_table")) or "unknown"
        source_row.manual_authoritative = _bool(raw.get("manual_authoritative"))
        source_row.freshness = _text(raw.get("freshness"))
        source_row.error = None
        try:
            target = _materialise(
                session,
                category=category,
                natural_key=natural_key,
                payload=payload,
                source=source,
            )
            # Authentication hashes are materialised but never duplicated into
            # the provenance JSON ledger.
            stored_payload = dict(payload)
            stored_payload.pop("password_hash", None)
            source_row.source_payload = stored_payload
            if target is None:
                source_row.status = "retained"
                source_row.target_type = "migration_source_record"
                source_row.target_id = source_row.id
                counts["retained"] += 1
            else:
                source_row.status = "materialized"
                source_row.target_type, source_row.target_id = target
                counts["materialized"] += 1
        except Exception as exc:
            source_row.status = "error"
            source_row.error = f"{type(exc).__name__}: {exc}"[:2000]
            counts["errors"] += 1
        session.flush()

    return {
        "migration_id": migration_id,
        "accepted": int(counts["accepted"]),
        "updated": int(counts["updated"]),
        "skipped": int(counts["skipped"]),
        "invalid": int(counts["invalid"]),
        "materialized": int(counts["materialized"]),
        "retained": int(counts["retained"]),
        "errors": int(counts["errors"]),
        "categories": dict(sorted(categories.items())),
        "status": "completed",
    }
