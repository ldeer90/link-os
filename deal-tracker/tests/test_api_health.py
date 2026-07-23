from __future__ import annotations

from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api_v1 import _current_state_summary, _health_payload, _historical_funnel_summary, _outreach_evidence_summary, _overview_pipeline, backlink_candidates, domains as list_domains, jobs, scraping_activity
from deal_tracker.platform.enums import BacklinkAnalysisMode, BacklinkAnalysisStatus, BacklinkCandidateStatus, ImportItemStatus, JobStatus, LifecycleStage, ScrapeStatus
from deal_tracker.platform.models import (
    AuditLog,
    BacklinkAnalysisRun,
    BacklinkAnalysisTarget,
    BacklinkCandidate,
    BacklinkEvidence,
    Base,
    Contact,
    Domain,
    DomainContactEvidence,
    ImportBatch,
    ImportItem,
    InstantlyEvent,
    Job,
    ScrapeAttempt,
    utcnow,
)


def _job(kind: str, status: JobStatus, key: str, *, at):
    return Job(
        kind=kind,
        status=status,
        idempotency_key=key,
        available_at=at,
        created_at=at,
        updated_at=at,
        completed_at=at if status == JobStatus.SUCCEEDED else None,
    )


def test_health_archives_migrated_failures_and_superseded_monday_failures() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine, expire_on_commit=False)
    now = utcnow()

    with Session() as session:
        session.add_all(
            [
                _job("legacy_scrape_record", JobStatus.DEAD, "legacy-dead", at=now - timedelta(days=1)),
                _job("reconcile_monday", JobStatus.DEAD, "monday-dead", at=now - timedelta(hours=2)),
                _job("reconcile_monday", JobStatus.SUCCEEDED, "monday-success", at=now - timedelta(hours=1)),
                _job("verify_contact", JobStatus.DEAD, "verify-dead", at=now),
            ]
        )
        session.commit()

        health = _health_payload(session)

    assert health["jobs"] == {"queued": 0, "failed": 1, "historical_failed": 2}
    assert health["worker"]["status"] == "degraded"


def test_jobs_active_filter_uses_leases_and_returns_global_kind_summary() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine, expire_on_commit=False)
    now = utcnow()

    with Session() as session:
        session.add_all(
            [
                _job("scrape_domain", JobStatus.QUEUED, "scrape-queued", at=now),
                _job("scrape_domain", JobStatus.LEASED, "scrape-active", at=now),
                _job("scrape_domain", JobStatus.SUCCEEDED, "scrape-complete", at=now),
                _job("verify_contact", JobStatus.LEASED, "verify-active", at=now),
            ]
        )
        session.commit()

        payload = jobs(status="active", job_type="scrape", page_size=100, session=session)

    assert payload["total"] == 1
    assert payload["count"] == 1
    assert payload["items"][0]["status"] == "leased"
    assert payload["summary"] == {
        "queued": 1,
        "active": 1,
        "completed": 1,
        "failed": 0,
        "cancelled": 0,
    }


def test_scraping_activity_returns_current_domains_and_recent_outcomes() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine, expire_on_commit=False)
    now = utcnow()

    with Session() as session:
        active_domain = Domain(normalized_domain="active.example", first_input_value="active.example", tld="example", lifecycle_stage=LifecycleStage.SCRAPING, stage_changed_at=now)
        recent_domain = Domain(normalized_domain="recent.example", first_input_value="recent.example", tld="example", lifecycle_stage=LifecycleStage.EMAIL_FOUND, stage_changed_at=now)
        session.add_all([active_domain, recent_domain])
        session.flush()
        active_job = _job("scrape_domain", JobStatus.LEASED, "activity-active", at=now)
        active_job.subject_type = "domain"
        active_job.subject_id = active_domain.id
        recent_job = _job("scrape_domain", JobStatus.SUCCEEDED, "activity-recent", at=now)
        recent_job.subject_type = "domain"
        recent_job.subject_id = recent_domain.id
        session.add_all([active_job, recent_job])
        session.flush()
        active_attempt = ScrapeAttempt(
            domain_id=active_domain.id,
            job_id=active_job.id,
            attempt_number=1,
            status=ScrapeStatus.RUNNING,
            started_at=now,
            pages_requested=2,
            pages_crawled=1,
            metrics={"current_url": "https://active.example/contact", "last_event": "page_started", "max_pages": 20},
        )
        session.add_all([
            active_attempt,
            ScrapeAttempt(domain_id=recent_domain.id, job_id=recent_job.id, attempt_number=1, status=ScrapeStatus.SUCCEEDED, started_at=now - timedelta(seconds=12), completed_at=now, pages_requested=3, pages_crawled=3, emails_found=1),
        ])
        session.flush()
        session.add(
            AuditLog(
                actor_type="worker",
                action="page_started",
                entity_type="scrape_attempt",
                entity_id=active_attempt.id,
                after={"attempt_id": active_attempt.id, "domain": "active.example", "current_url": "https://active.example/contact", "event_type": "page_started", "occurred_at": now.isoformat()},
            )
        )
        session.commit()

        payload = scraping_activity(limit=20, session=session)

    assert payload["active"][0]["domain"] == "active.example"
    assert payload["active"][0]["job_status"] == "leased"
    assert payload["active"][0]["current_url"] == "https://active.example/contact"
    assert payload["recent"][0]["domain"] == "recent.example"
    assert payload["recent"][0]["emails_found"] == 1
    assert payload["events"][0]["event_type"] == "page_started"
    assert payload["events"][0]["current_url"] == "https://active.example/contact"


def test_overview_separates_unverified_membership_from_contacted() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine, expire_on_commit=False)
    now = utcnow()

    with Session() as session:
        unverified = Domain(
            normalized_domain="membership.example",
            first_input_value="membership.example",
            tld="example",
            lifecycle_stage=LifecycleStage.CONTACTED,
            stage_changed_at=now,
        )
        confirmed = Domain(
            normalized_domain="confirmed.com.au",
            first_input_value="confirmed.com.au",
            tld="au",
            lifecycle_stage=LifecycleStage.CONTACTED,
            stage_changed_at=now,
        )
        unrelated = Domain(
            normalized_domain="unrelated-sales.example",
            first_input_value="unrelated-sales.example",
            tld="example",
            lifecycle_stage=LifecycleStage.CONTACTED,
            stage_changed_at=now,
        )
        session.add_all([unverified, confirmed, unrelated])
        session.flush()
        session.add_all(
            [
                InstantlyEvent(
                        dedupe_key="membership-unverified",
                        event_type="campaign_membership_reconciled",
                        external_campaign_id="f6b0f05e-d85c-41a3-b3d6-c261cfca1f7a",
                        domain_id=unverified.id,
                    event_at=now,
                    payload={"timestamp_last_contact": None},
                ),
                    InstantlyEvent(
                        dedupe_key="membership-confirmed",
                        event_type="campaign_membership_reconciled",
                        external_campaign_id="f6b0f05e-d85c-41a3-b3d6-c261cfca1f7a",
                    domain_id=confirmed.id,
                    event_at=now,
                    payload={"timestamp_last_contact": now.isoformat()},
                ),
                InstantlyEvent(
                    dedupe_key="membership-unrelated",
                    event_type="campaign_membership_reconciled",
                    external_campaign_id="unrelated-seo-campaign",
                    domain_id=unrelated.id,
                    event_at=now,
                    payload={"timestamp_last_contact": now.isoformat()},
                ),
            ]
        )
        session.commit()

        evidence = _outreach_evidence_summary(session)
        australian = _outreach_evidence_summary(session, australian_only=True)
        pipeline = _overview_pipeline(session, evidence)
        historical = _historical_funnel_summary(session)
        current = _current_state_summary(session)
        no_send = list_domains(evidence="uploaded_no_send_record", session=session)
        sent = list_domains(evidence="sent_confirmed", session=session)
        entered = list_domains(history="entered_system", session=session)
        uploaded_history = list_domains(history="instantly_uploaded", session=session)
        unrelated_scope = list_domains(scope="unrelated_instantly_only", session=session)

    assert pipeline["contacted"] == 1
    assert pipeline["historical_membership_unverified"] == 1
    assert current["contacted"] == 2
    assert historical["entered_system"] == 2
    assert historical["instantly_uploaded"] == 2
    assert historical["sent_confirmed"] == 1
    assert evidence["historical_memberships"] == 2
    assert evidence["workspace_instantly_uploaded"] == 3
    assert evidence["excluded_unrelated_campaign_domains"] == 1
    assert evidence["contact_confirmed"] == 1
    assert evidence["membership_only"] == 1
    assert evidence["instantly_uploaded"] == 2
    assert evidence["sent_confirmed"] == 1
    assert evidence["uploaded_no_send_record"] == 1
    assert evidence["evidence_backed_engaged"] == 1
    assert evidence["protected_domains"] == 0
    assert australian["total_domains"] == 1
    assert australian["contact_confirmed"] == 1
    assert no_send["total"] == 1
    assert no_send["items"][0]["outreach_evidence_label"] == "Uploaded — no send record"
    assert no_send["items"][0]["canonical_dedupe"] is True
    assert sent["total"] == 1
    assert sent["items"][0]["outreach_evidence_label"] == "Sent confirmed"
    assert entered["total"] == 2
    assert uploaded_history["total"] == 2
    assert unrelated_scope["total"] == 1


def test_domains_paginate_and_expose_manual_and_backlink_provenance() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine, expire_on_commit=False)
    now = utcnow()

    with Session() as session:
        manual_domain = Domain(
            normalized_domain="manual.example",
            first_input_value="manual.example",
            tld="example",
            lifecycle_stage=LifecycleStage.IMPORTED,
            stage_changed_at=now,
            created_at=now - timedelta(minutes=2),
            updated_at=now - timedelta(minutes=2),
        )
        backlink_domain = Domain(
            normalized_domain="publisher.com.au",
            first_input_value="publisher.com.au",
            tld="au",
            lifecycle_stage=LifecycleStage.EMAIL_FOUND,
            stage_changed_at=now,
            created_at=now,
            updated_at=now,
        )
        session.add_all([manual_domain, backlink_domain])
        session.flush()
        manual_import = ImportBatch(source_type="paste", source_metadata={"actor": "test"})
        session.add(manual_import)
        session.flush()
        session.add(ImportItem(import_id=manual_import.id, row_number=1, raw_value="manual.example", normalized_value="manual.example", status=ImportItemStatus.ACCEPTED, domain_id=manual_domain.id))
        run = BacklinkAnalysisRun(
            mode=BacklinkAnalysisMode.COMPETITOR_PROSPECTING,
            status=BacklinkAnalysisStatus.COMPLETE,
            client_domain="client.example",
            competitor_domains=["seed.example"],
            source_url_filter=".com.au",
            requested_by="test",
            credit_cap=20,
            confirmed_credit_cap=20,
            authority_floor=20,
            monthly_credit_cap=10_000,
            estimated_credits=20,
        )
        session.add(run)
        session.flush()
        session.add(BacklinkCandidate(run_id=run.id, normalized_domain=backlink_domain.normalized_domain, existing_domain_id=backlink_domain.id, status=BacklinkCandidateStatus.IMPORTED, reason_codes=["editorial"]))
        session.commit()

        first_page = list_domains(page_size=1, offset=0, session=session)
        second_page = list_domains(page_size=1, offset=1, session=session)

    assert first_page["total"] == 2
    assert first_page["page_size"] == 1
    assert first_page["offset"] == 0
    assert first_page["items"][0]["domain"] == "publisher.com.au"
    assert first_page["items"][0]["source_label"] == "Backlink profile: seed.example"
    assert first_page["items"][0]["sources"][0]["analysis_id"] == run.id
    assert second_page["items"][0]["domain"] == "manual.example"
    assert second_page["items"][0]["source_label"] == "Manual paste"
    assert second_page["items"][0]["sources"][0]["import_id"] == manual_import.id


def test_backlink_candidates_filter_by_competitor_quality_and_crawl_state() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine, expire_on_commit=False)
    now = utcnow()

    with Session() as session:
        run = BacklinkAnalysisRun(
            mode=BacklinkAnalysisMode.COMPETITOR_PROSPECTING,
            status=BacklinkAnalysisStatus.COMPLETE,
            client_domain="client.example",
            competitor_domains=["one.example", "two.example"],
            requested_by="test",
            credit_cap=20,
            confirmed_credit_cap=20,
            authority_floor=20,
            monthly_credit_cap=10_000,
            estimated_credits=20,
        )
        session.add(run)
        session.flush()
        first_target = BacklinkAnalysisTarget(
            run_id=run.id,
            target_domain="one.example",
            target_role="competitor",
            allocated_credits=10,
            request_hash="target-one",
            status="complete",
        )
        second_target = BacklinkAnalysisTarget(
            run_id=run.id,
            target_domain="two.example",
            target_role="competitor",
            allocated_credits=10,
            request_hash="target-two",
            status="complete",
        )
        session.add_all([first_target, second_target])
        session.flush()
        found_domain = Domain(
            normalized_domain="editorial.example",
            first_input_value="editorial.example",
            tld="example",
            lifecycle_stage=LifecycleStage.EMAIL_FOUND,
            stage_changed_at=now,
        )
        empty_domain = Domain(
            normalized_domain="directory.example",
            first_input_value="directory.example",
            tld="example",
            lifecycle_stage=LifecycleStage.NO_EMAIL,
            stage_changed_at=now,
        )
        session.add_all([found_domain, empty_domain])
        session.flush()
        contact = Contact(
            normalized_email="editor@editorial.example",
            original_email="editor@editorial.example",
            email_domain="editorial.example",
        )
        session.add(contact)
        session.flush()
        session.add(
            DomainContactEvidence(
                domain_id=found_domain.id,
                contact_id=contact.id,
                evidence_key="public-editorial-contact",
                source_url="https://editorial.example/contact",
                confidence=0.9,
                is_public_page=True,
            )
        )
        first_candidate = BacklinkCandidate(
            run_id=run.id,
            normalized_domain="editorial.example",
            existing_domain_id=found_domain.id,
            status=BacklinkCandidateStatus.IMPORTED,
            highest_domain_inlink_rank=64,
            has_dofollow=True,
            local_score=0.9,
            commercial_anchor_class="commercial",
            commercial_anchor_score=0.84,
            commercial_anchor_signals=["commercial_anchor:esim"],
        )
        second_candidate = BacklinkCandidate(
            run_id=run.id,
            normalized_domain="directory.example",
            existing_domain_id=empty_domain.id,
            status=BacklinkCandidateStatus.REJECTED,
            highest_domain_inlink_rank=32,
            has_dofollow=False,
            local_score=0.4,
        )
        session.add_all([first_candidate, second_candidate])
        session.flush()
        session.add_all(
            [
                BacklinkEvidence(
                    run_id=run.id,
                    target_id=first_target.id,
                    source_domain="editorial.example",
                    source_url="https://editorial.example/bali-guide",
                    target_url="https://one.example/esim",
                    page_title="Bali connectivity guide",
                    anchor_text="travel eSIM",
                    nofollow=False,
                    domain_inlink_rank=64,
                    evidence_key="editorial-evidence",
                ),
                BacklinkEvidence(
                    run_id=run.id,
                    target_id=second_target.id,
                    source_domain="directory.example",
                    source_url="https://directory.example/report",
                    target_url="https://two.example",
                    page_title="Domain report",
                    anchor_text="two.example",
                    nofollow=True,
                    domain_inlink_rank=32,
                    evidence_key="directory-evidence",
                ),
            ]
        )
        session.commit()

        payload = backlink_candidates(
            run.id,
            status="",
            search="connectivity",
            competitor="one.example",
            link_type="follow",
            anchor_intent="commercial",
            min_authority=50,
            email_status="found",
            crawl_status="email_found",
            limit=100,
            offset=0,
            session=session,
        )

    assert payload["count"] == 1
    assert payload["available_competitors"] == ["one.example", "two.example"]
    assert payload["items"][0]["domain"] == "editorial.example"
    assert payload["items"][0]["competitor_domains"] == ["one.example"]
    assert payload["items"][0]["crawl_status"] == "email_found"
    assert payload["items"][0]["public_email_count"] == 1
    assert payload["items"][0]["commercial_anchor_class"] == "commercial"
    assert payload["items"][0]["commercial_anchor_score"] == 0.84
    assert payload["items"][0]["evidence"][0]["competitor_domain"] == "one.example"
    assert payload["items"][0]["evidence"][0]["anchor_class"] == "commercial"
    assert payload["items"][0]["evidence"][0]["anchor_commercial_score"] >= 0.7
