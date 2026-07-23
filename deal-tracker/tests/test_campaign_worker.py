from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from deal_tracker.platform.enums import CampaignBatchStatus, LifecycleStage, VerificationStatus
from deal_tracker.platform.models import (
    Base,
    CampaignBatch,
    Contact,
    Domain,
    DomainContactEvidence,
    Job,
    SenderAccount,
    SystemSetting,
    VerificationResult,
    utcnow,
)
from deal_tracker.platform.repositories import JobQueue, set_outreach_paused
from deal_tracker.worker import _next_campaign_request, prepare_campaign_batch_job


class FakeInstantly:
    def __init__(self) -> None:
        self.campaigns: list[dict] = []
        self.leads: list[dict] = []
        self.activated = False

    def list_accounts(self):
        return [{"id": "sender-1", "email": "sender@example.com", "status": 1, "setup_pending": False, "autofix_failed": False}]

    def list_campaigns(self):
        return list(self.campaigns)

    def create_paused_campaign(self, payload):
        row = {"id": "campaign-1", "name": payload["name"], "status": 0}
        self.campaigns = [row]
        return row

    def pause_campaign(self, campaign_id):
        self.campaigns[0]["status"] = 0
        return dict(self.campaigns[0])

    def list_campaign_leads(self, campaign_id):
        return list(self.leads)

    def upload_leads(self, campaign_id, leads, *, verify_leads_on_import=True):
        self.verify_leads_on_import = verify_leads_on_import
        self.leads.extend(
            {"id": f"lead-{index}", **lead, "verification_status": "verified"}
            for index, lead in enumerate(leads, len(self.leads) + 1)
        )
        return {"requested": len(leads), "total_sent": len(leads), "matches": True, "results": []}

    def analytics(self, campaign_id=None, **_kwargs):
        return [{"campaign_id": campaign_id, "emails_sent_count": 0, "bounced_count": 0, "unsubscribed_count": 0}]

    def get_campaign(self, campaign_id):
        return dict(self.campaigns[0])

    def activate(self, campaign_id, *, blockers):
        assert blockers == []
        self.activated = True
        self.campaigns[0]["status"] = 1
        return {"campaign": dict(self.campaigns[0])}


def test_campaign_worker_builds_paused_reads_back_then_activates(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setenv("LINK_OS_MODE", "live")
    fake = FakeInstantly()

    with Session() as session:
        for index in range(25):
            domain_name = f"publisher-{index}.example"
            email = f"editor-{index}@{domain_name}"
            domain = Domain(
                normalized_domain=domain_name,
                first_input_value=domain_name,
                tld="example",
                lifecycle_stage=LifecycleStage.OUTREACH_READY,
            )
            contact = Contact(
                normalized_email=email,
                original_email=email,
                email_domain=domain_name,
                is_guessed=False,
            )
            session.add_all([domain, contact])
            session.flush()
            session.add(
                DomainContactEvidence(
                    domain_id=domain.id,
                    contact_id=contact.id,
                    evidence_key=f"evidence-{index}",
                    source_url=f"https://{domain_name}/contact",
                    confidence=0.95,
                    is_public_page=True,
                )
            )
            session.add(
                VerificationResult(
                    contact_id=contact.id,
                    provider="instantly",
                    provider_request_id=f"verify-{index}",
                    status=VerificationStatus.VERIFIED,
                    catch_all=False,
                    risky=False,
                )
            )
        session.add(
            SenderAccount(
                external_id="sender-1",
                sender_email="sender@example.com",
                provider_status=1,
                connected=True,
                healthy_since=utcnow() - timedelta(hours=1),
            )
        )
        session.add(
            SystemSetting(
                key="instantly.full_reconcile_completed",
                value={"complete": True},
                updated_by="test",
            )
        )
        session.add(
            SystemSetting(
                key="instantly.legacy_campaigns_paused",
                value={"complete": True},
                updated_by="test",
            )
        )
        set_outreach_paused(session, paused=False, actor="test", reason="test sender healthy")
        job, _ = JobQueue(session).enqueue(
            kind="prepare_campaign_batch",
            idempotency_key="campaign-test",
            payload={"pilot": True, "target_size": 25},
        )
        session.commit()
        job_id = job.id

    with patch("deal_tracker.worker.session_factory", return_value=Session), patch(
        "deal_tracker.worker.InstantlyControl", return_value=fake
    ):
        result = prepare_campaign_batch_job(job_id)

    assert result["activated"] is True
    assert fake.activated is True
    assert fake.verify_leads_on_import is True
    with Session() as session:
        batch = session.scalar(select(CampaignBatch))
        assert batch.status == CampaignBatchStatus.ACTIVE
        assert batch.expected_member_count == 25
        assert batch.uploaded_member_count == 25
        assert batch.readback_member_count == 25
        job = session.get(Job, job_id)
        assert job.payload["batch_id"] == batch.id


def test_public_evidence_pilot_prepares_paused_without_instantly_verification(
    monkeypatch,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setenv("LINK_OS_MODE", "shadow")
    fake = FakeInstantly()

    with Session() as session:
        for index in range(25):
            domain_name = f"public-{index}.example"
            email = f"editor-{index}@{domain_name}"
            domain = Domain(
                normalized_domain=domain_name,
                first_input_value=domain_name,
                tld="example",
                lifecycle_stage=LifecycleStage.EMAIL_FOUND,
            )
            contact = Contact(
                normalized_email=email,
                original_email=email,
                email_domain=domain_name,
                is_guessed=False,
            )
            session.add_all([domain, contact])
            session.flush()
            session.add(
                DomainContactEvidence(
                    domain_id=domain.id,
                    contact_id=contact.id,
                    evidence_key=f"public-evidence-{index}",
                    source_url=f"https://{domain_name}/contact",
                    confidence=0.95,
                    is_public_page=True,
                )
            )
        session.add(
            SenderAccount(
                external_id="sender-1",
                sender_email="sender@example.com",
                provider_status=1,
                connected=True,
                healthy_since=utcnow() - timedelta(hours=1),
            )
        )
        session.add(
            SystemSetting(
                key="instantly.full_reconcile_completed",
                value={"complete": True},
                updated_by="test",
            )
        )
        set_outreach_paused(
            session,
            paused=True,
            actor="test",
            reason="prepare without sending",
        )
        job, _ = JobQueue(session).enqueue(
            kind="prepare_campaign_batch",
            idempotency_key="public-evidence-campaign-test",
            payload={
                "pilot": True,
                "target_size": 25,
                "prepare_only": True,
                "contact_policy": "public_evidence_verification_skipped",
                "confirm_verification_skipped": True,
            },
        )
        session.commit()
        job_id = job.id

    with patch("deal_tracker.worker.session_factory", return_value=Session), patch(
        "deal_tracker.worker.InstantlyControl", return_value=fake
    ):
        result = prepare_campaign_batch_job(job_id)

    assert result["prepared"] is True
    assert result["activated"] is False
    assert result["verification_skipped"] is True
    assert fake.activated is False
    assert fake.verify_leads_on_import is False
    with Session() as session:
        batch = session.scalar(select(CampaignBatch))
        assert batch.status == CampaignBatchStatus.PAUSED
        assert batch.uploaded_member_count == 25
        assert batch.settings_snapshot["deliverability_verification"] == "skipped"
        assert "deliverability_verification_skipped" in (batch.paused_reason or "")
        assert all(
            domain.lifecycle_stage == LifecycleStage.UPLOADED
            for domain in session.scalars(select(Domain))
        )


def test_scheduler_graduates_from_one_pilot_to_250_after_health_gate() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine, expire_on_commit=False)
    now = utcnow()

    with Session() as session:
        pilot_sender = SenderAccount(
            external_id="pilot-sender",
            sender_email="pilot@example.com",
            provider_status=1,
            connected=True,
            healthy_since=now - timedelta(hours=1),
        )
        session.add(pilot_sender)
        session.flush()
        assert _next_campaign_request(session, now=now) == {
            "pilot": True,
            "target_size": 25,
        }

        pilot = CampaignBatch(
            deterministic_key="pilot:test",
            name="LINK OS | Pilot",
            status=CampaignBatchStatus.ACTIVE,
            target_size=25,
            expected_member_count=25,
            config_version="test",
            sender_account_id=pilot_sender.id,
            active_assignment=True,
            activated_at=now,
            settings_snapshot={"pilot": True},
        )
        scaled_sender = SenderAccount(
            external_id="scaled-sender",
            sender_email="scaled@example.com",
            provider_status=1,
            connected=True,
            healthy_since=now - timedelta(hours=73),
        )
        session.add_all([pilot, scaled_sender])
        session.flush()
        assert _next_campaign_request(session, now=now) == {
            "pilot": False,
            "target_size": 250,
        }
