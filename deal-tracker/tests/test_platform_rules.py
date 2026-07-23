from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from deal_tracker.platform.gates import (
    CampaignUploadSnapshot,
    ContactEligibility,
    SenderSnapshot,
    evaluate_campaign_activation,
    evaluate_contact_eligibility,
)
from deal_tracker.platform.lifecycle import (
    InvalidLifecycleTransition,
    can_automatically_contact,
    is_refresh_eligible,
    validate_transition,
)
from deal_tracker.platform.enums import LifecycleStage
from deal_tracker.platform.normalization import normalize_domain, normalize_email
from deal_tracker.platform.pricing import (
    OfferExtraction,
    assess_offer_for_auto_approval,
    calculate_reseller_price,
)


NOW = datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc)


def test_domain_normalization_accepts_global_public_domains() -> None:
    assert normalize_domain("https://WWW.Example.co.uk/contact").registrable_domain == (
        "example.co.uk"
    )
    result = normalize_domain("https://news.publisher.com.au/about")
    assert result.registrable_domain == "publisher.com.au"
    assert result.country_code == "AU"
    assert normalize_domain("example.de").registrable_domain == "example.de"


@pytest.mark.parametrize("value", ["localhost", "127.0.0.1", "bad_domain.test"])
def test_domain_normalization_rejects_non_public_hosts(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_domain(value)


def test_email_normalization_deduplicates_case_and_idna_domain() -> None:
    assert normalize_email("  EDITOR@Example.COM ") == "editor@example.com"


def test_lifecycle_protects_skipped_steps_and_terminal_domains() -> None:
    assert (
        validate_transition(LifecycleStage.IMPORTED, LifecycleStage.QUEUED)
        == LifecycleStage.QUEUED
    )
    with pytest.raises(InvalidLifecycleTransition):
        validate_transition(LifecycleStage.IMPORTED, LifecycleStage.CONTACTED)
    assert not can_automatically_contact(LifecycleStage.CONTACTED)
    assert not can_automatically_contact(LifecycleStage.SUPPRESSED)


def test_failed_or_no_email_refresh_requires_90_days() -> None:
    assert not is_refresh_eligible(
        LifecycleStage.FAILED, last_attempt_at=NOW - timedelta(days=89), now=NOW
    )
    assert is_refresh_eligible(
        LifecycleStage.NO_EMAIL, last_attempt_at=NOW - timedelta(days=90), now=NOW
    )
    assert not is_refresh_eligible(
        LifecycleStage.CONTACTED, last_attempt_at=NOW - timedelta(days=900), now=NOW
    )


def test_contact_gate_requires_public_evidence_and_strict_verification() -> None:
    allowed = evaluate_contact_eligibility(
        ContactEligibility(
            has_public_evidence=True,
            verification_status="verified",
            catch_all=False,
            risky=False,
        )
    )
    assert allowed.allowed

    blocked = evaluate_contact_eligibility(
        ContactEligibility(
            has_public_evidence=False,
            verification_status="verified",
            catch_all=None,
            risky=False,
            guessed=True,
        )
    )
    assert not blocked.allowed
    assert set(blocked.reasons) == {
        "missing_public_evidence",
        "catch_all_or_unknown",
        "guessed_email",
    }


def _safe_campaign() -> CampaignUploadSnapshot:
    return CampaignUploadSnapshot(
        expected_members=25,
        uploaded_members=25,
        readback_members=25,
        built_paused=True,
        pending_verifications=0,
        tracking_enabled=False,
        unsubscribe_header_enabled=True,
        stop_on_reply=True,
        stop_on_auto_reply=True,
        stop_on_company_reply=True,
        bounce_protection_enabled=True,
        risky_contacts_enabled=False,
    )


def test_campaign_gate_opens_only_for_a_healthy_sender_and_safe_readback() -> None:
    decision = evaluate_campaign_activation(
        outreach_paused=False,
        sender=SenderSnapshot(
            external_id="sender-1",
            connected=True,
            provider_status=1,
            healthy_since=NOW - timedelta(hours=73),
            active_link_os_campaigns=0,
            new_leads_sent_today=0,
            total_emails_sent_today=0,
            bounce_rate=0.0,
            unsubscribe_rate=0.0,
        ),
        campaign=_safe_campaign(),
        now=NOW,
    )
    assert decision.allowed
    assert decision.health_gate_complete
    assert decision.new_lead_daily_limit == 15
    assert decision.total_email_daily_limit == 30


def test_campaign_gate_fails_closed_on_pause_unknown_health_or_upload_mismatch() -> None:
    unsafe_campaign = replace(_safe_campaign(), readback_members=24)
    decision = evaluate_campaign_activation(
        outreach_paused=None,
        sender=SenderSnapshot(
            external_id=None,
            connected=False,
            provider_status=-1,
            healthy_since=None,
            active_link_os_campaigns=0,
            new_leads_sent_today=0,
            total_emails_sent_today=0,
            bounce_rate=None,
            unsubscribe_rate=None,
        ),
        campaign=unsafe_campaign,
        now=NOW,
    )
    assert not decision.allowed
    assert "global_outreach_pause" in decision.reasons
    assert "sender_status_unhealthy_or_unknown" in decision.reasons
    assert "upload_count_mismatch" in decision.reasons


def test_reseller_formula_uses_greater_rule_and_rounds_up_to_ten() -> None:
    low_cost = calculate_reseller_price(
        amount=Decimal("100"),
        currency="AUD",
        fx_rate_to_aud=Decimal("1"),
        fx_rate_date=date(2026, 7, 15),
    )
    assert low_cost.cost_aud == Decimal("100.00")
    assert low_cost.reseller_price_aud == Decimal("200.00")

    high_cost = calculate_reseller_price(
        amount=Decimal("331"),
        currency="USD",
        fx_rate_to_aud=Decimal("1"),
        fx_rate_date=date(2026, 7, 15),
    )
    assert high_cost.reseller_price_aud == Decimal("500.00")


def test_offer_auto_approval_rejects_ambiguous_multi_price_reply() -> None:
    confident = assess_offer_for_auto_approval(
        OfferExtraction(
            domain="example.com",
            amount=Decimal("250"),
            currency="AUD",
            placement_type="guest_post",
            confidence=Decimal("0.96"),
        )
    )
    assert confident.auto_approvable

    ambiguous = assess_offer_for_auto_approval(
        OfferExtraction(
            domain="example.com",
            amount=Decimal("250"),
            currency="AUD",
            placement_type="guest_post",
            confidence=Decimal("0.96"),
            conflicting_prices=True,
        )
    )
    assert not ambiguous.auto_approvable
    assert ambiguous.reasons == ("conflicting_prices",)
