"""Canonical PostgreSQL control-plane primitives for LINK OS."""

from .db import create_database_engine, make_session_factory, session_scope
from .enums import LifecycleStage
from .gates import (
    CampaignActivationDecision,
    CampaignUploadSnapshot,
    ContactEligibility,
    GateDecision,
    SenderSnapshot,
    evaluate_campaign_activation,
    evaluate_contact_eligibility,
)
from .models import Base
from .pricing import (
    PRICING_RULE_VERSION,
    OfferExtraction,
    PricingResult,
    assess_offer_for_auto_approval,
    calculate_reseller_price,
)
from .repositories import (
    CampaignRepository,
    DomainRepository,
    JobQueue,
    ensure_fail_closed_defaults,
    is_outreach_paused,
    set_outreach_paused,
)

__all__ = [
    "Base",
    "CampaignActivationDecision",
    "CampaignRepository",
    "CampaignUploadSnapshot",
    "ContactEligibility",
    "DomainRepository",
    "GateDecision",
    "JobQueue",
    "LifecycleStage",
    "OfferExtraction",
    "PRICING_RULE_VERSION",
    "PricingResult",
    "SenderSnapshot",
    "assess_offer_for_auto_approval",
    "calculate_reseller_price",
    "create_database_engine",
    "ensure_fail_closed_defaults",
    "evaluate_campaign_activation",
    "evaluate_contact_eligibility",
    "is_outreach_paused",
    "make_session_factory",
    "session_scope",
    "set_outreach_paused",
]
