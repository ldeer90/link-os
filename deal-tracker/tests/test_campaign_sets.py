from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from deal_tracker.campaign_sets import campaign_set_estimate, segmented_candidates
from deal_tracker.platform.enums import (
    BacklinkAnalysisMode,
    BacklinkAnalysisStatus,
    BacklinkCandidateStatus,
    ImportItemStatus,
    ImportStatus,
    LifecycleStage,
)
from deal_tracker.platform.models import (
    BacklinkAnalysisRun,
    BacklinkCandidate,
    Base,
    Contact,
    Domain,
    DomainContactEvidence,
    ImportBatch,
    ImportItem,
    SenderAccount,
)


def _publisher(session, domain_name: str, email: str, *, role: str, confidence: float):
    domain = Domain(
        normalized_domain=domain_name,
        first_input_value=domain_name,
        tld=domain_name.rsplit(".", 1)[-1],
        lifecycle_stage=LifecycleStage.EMAIL_FOUND,
    )
    contact = Contact(
        normalized_email=email,
        original_email=email,
        email_domain=email.split("@", 1)[1],
        role_class=role,
        is_guessed=False,
    )
    session.add_all([domain, contact])
    session.flush()
    session.add(
        DomainContactEvidence(
            domain_id=domain.id,
            contact_id=contact.id,
            evidence_key=f"{domain.id}:{contact.id}",
            source_url=f"https://{domain_name}/contact",
            is_public_page=True,
            confidence=confidence,
            context_snippet="Public editorial contact",
        )
    )
    return domain, contact


def test_segmented_candidates_require_strong_role_confidence_and_travel_source() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine, expire_on_commit=False)
    with Session() as session:
        au, _ = _publisher(session, "publisher.com.au", "editor@publisher.com.au", role="editor", confidence=0.94)
        travel, _ = _publisher(session, "travelpress.com", "media@travelpress.com", role="media", confidence=0.96)
        linked_travel, _ = _publisher(session, "linkedtravel.com", "editor@linkedtravel.com", role="editor", confidence=0.95)
        _publisher(session, "manualpress.com", "editor@manualpress.com", role="editor", confidence=0.96)
        _publisher(session, "weaktravel.com", "sales@weaktravel.com", role="sales", confidence=0.96)
        imported = ImportBatch(source_type="backlink_discovery", status=ImportStatus.COMPLETE)
        session.add(imported)
        session.flush()
        run = BacklinkAnalysisRun(
                mode=BacklinkAnalysisMode.COMPETITOR_PROSPECTING,
                status=BacklinkAnalysisStatus.COMPLETE,
                client_domain="travelkon.com.au",
                competitor_domains=["airalo.com"],
                requested_by="test",
                credit_cap=25,
                authority_floor=20,
                monthly_credit_cap=10000,
                import_id=imported.id,
            )
        session.add(run)
        session.flush()
        session.add(
            ImportItem(
                import_id=imported.id,
                row_number=1,
                raw_value=travel.normalized_domain,
                normalized_value=travel.normalized_domain,
                status=ImportItemStatus.ACCEPTED,
                domain_id=travel.id,
            )
        )
        session.add(
            BacklinkCandidate(
                run_id=run.id,
                normalized_domain=linked_travel.normalized_domain,
                existing_domain_id=linked_travel.id,
                status=BacklinkCandidateStatus.IMPORTED,
            )
        )
        for index in range(3):
            session.add(
                SenderAccount(
                    external_id=f"sender-{index}", sender_email=f"sender{index}@example.com",
                    connected=True, provider_status=1,
                )
            )
        session.commit()

        assert segmented_candidates(session, "au_publishers", 25) == [(au.id, session.query(Contact).filter_by(normalized_email="editor@publisher.com.au").one().id)]
        assert {domain_id for domain_id, _ in segmented_candidates(session, "travel_global", 25)} == {travel.id, linked_travel.id}
        estimate = campaign_set_estimate(session)
        assert estimate["healthy_unassigned_senders"] == 3
        assert estimate["available"] == {"au_publishers": 1, "travel_global": 2}
        assert estimate["allowed"] is False
