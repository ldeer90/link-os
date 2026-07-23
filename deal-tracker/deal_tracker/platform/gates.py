"""Pure, fail-closed gates for contacts and Instantly campaign activation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass(frozen=True, slots=True)
class ContactEligibility:
    has_public_evidence: bool
    verification_status: str | None
    catch_all: bool | None
    risky: bool | None
    guessed: bool = False
    suppressed: bool = False
    previously_contacted: bool = False


@dataclass(frozen=True, slots=True)
class GateDecision:
    allowed: bool
    reasons: tuple[str, ...]


def evaluate_contact_eligibility(snapshot: ContactEligibility) -> GateDecision:
    reasons: list[str] = []
    if not snapshot.has_public_evidence:
        reasons.append("missing_public_evidence")
    if snapshot.verification_status != "verified":
        reasons.append("not_verified")
    if snapshot.catch_all is not False:
        reasons.append("catch_all_or_unknown")
    if snapshot.risky is not False:
        reasons.append("risky_or_unknown")
    if snapshot.guessed:
        reasons.append("guessed_email")
    if snapshot.suppressed:
        reasons.append("suppressed")
    if snapshot.previously_contacted:
        reasons.append("previously_contacted")
    return GateDecision(not reasons, tuple(reasons))


@dataclass(frozen=True, slots=True)
class SenderSnapshot:
    external_id: str | None
    connected: bool
    provider_status: int | None
    healthy_since: datetime | None
    active_link_os_campaigns: int
    new_leads_sent_today: int
    total_emails_sent_today: int
    bounce_rate: float | None
    unsubscribe_rate: float | None
    has_error: bool = False
    bounce_protection_triggered: bool = False


@dataclass(frozen=True, slots=True)
class CampaignUploadSnapshot:
    expected_members: int
    uploaded_members: int
    readback_members: int
    built_paused: bool
    pending_verifications: int
    tracking_enabled: bool
    unsubscribe_method_present: bool
    stop_on_reply: bool
    stop_on_auto_reply: bool
    stop_on_company_reply: bool
    bounce_protection_enabled: bool
    risky_contacts_enabled: bool


@dataclass(frozen=True, slots=True)
class CampaignActivationDecision:
    allowed: bool
    reasons: tuple[str, ...]
    new_lead_daily_limit: int
    total_email_daily_limit: int
    health_gate_complete: bool


def evaluate_campaign_activation(
    *,
    outreach_paused: bool | None,
    sender: SenderSnapshot,
    campaign: CampaignUploadSnapshot,
    now: datetime | None = None,
) -> CampaignActivationDecision:
    """Return an activation decision; uncertain values always block sending."""

    instant = now or datetime.now(timezone.utc)
    healthy_since = sender.healthy_since
    if healthy_since is not None and healthy_since.tzinfo is None:
        healthy_since = healthy_since.replace(tzinfo=timezone.utc)
    health_gate_complete = bool(
        healthy_since is not None and instant - healthy_since >= timedelta(hours=72)
    )
    new_lead_limit = 15 if health_gate_complete else 10
    total_email_limit = 30
    reasons: list[str] = []

    if outreach_paused is not False:
        reasons.append("global_outreach_pause")
    if not sender.external_id or not sender.connected:
        reasons.append("sender_not_connected")
    if sender.provider_status is None or sender.provider_status < 0:
        reasons.append("sender_status_unhealthy_or_unknown")
    if sender.healthy_since is None:
        reasons.append("sender_health_unconfirmed")
    if sender.has_error:
        reasons.append("sender_error")
    if sender.active_link_os_campaigns > 0:
        reasons.append("sender_already_has_active_campaign")
    if sender.bounce_rate is None or sender.bounce_rate >= 0.03:
        reasons.append("bounce_rate_high_or_unknown")
    if sender.unsubscribe_rate is None or sender.unsubscribe_rate >= 0.01:
        reasons.append("unsubscribe_rate_high_or_unknown")
    if sender.bounce_protection_triggered:
        reasons.append("bounce_protection_triggered")
    if sender.new_leads_sent_today >= new_lead_limit:
        reasons.append("daily_new_lead_limit_reached")
    if sender.total_emails_sent_today >= total_email_limit:
        reasons.append("daily_total_email_limit_reached")

    if campaign.expected_members <= 0:
        reasons.append("campaign_empty")
    if not (
        campaign.expected_members
        == campaign.uploaded_members
        == campaign.readback_members
    ):
        reasons.append("upload_count_mismatch")
    if not campaign.built_paused:
        reasons.append("campaign_not_built_paused")
    if campaign.pending_verifications:
        reasons.append("pending_verification")
    if campaign.tracking_enabled:
        reasons.append("tracking_must_be_disabled")
    if not campaign.unsubscribe_method_present:
        reasons.append("unsubscribe_method_missing")
    if not campaign.stop_on_reply:
        reasons.append("stop_on_reply_disabled")
    if not campaign.stop_on_auto_reply:
        reasons.append("stop_on_auto_reply_disabled")
    if not campaign.stop_on_company_reply:
        reasons.append("stop_on_company_reply_disabled")
    if not campaign.bounce_protection_enabled:
        reasons.append("bounce_protection_disabled")
    if campaign.risky_contacts_enabled:
        reasons.append("risky_contacts_enabled")

    return CampaignActivationDecision(
        allowed=not reasons,
        reasons=tuple(reasons),
        new_lead_daily_limit=new_lead_limit,
        total_email_daily_limit=total_email_limit,
        health_gate_complete=health_gate_complete,
    )
