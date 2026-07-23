"""Conservative offer approval and AUD reseller pricing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_CEILING


PRICING_RULE_VERSION = "aud-flex-v1"


@dataclass(frozen=True, slots=True)
class PricingResult:
    original_amount: Decimal
    original_currency: str
    fx_rate_to_aud: Decimal
    fx_rate_date: date
    cost_aud: Decimal
    reseller_price_aud: Decimal
    rule_version: str = PRICING_RULE_VERSION


@dataclass(frozen=True, slots=True)
class OfferExtraction:
    domain: str | None
    amount: Decimal | None
    currency: str | None
    placement_type: str | None
    confidence: Decimal = Decimal("0")
    attachment_only: bool = False
    redirect: bool = False
    agency_or_network_ambiguity: bool = False
    conflicting_prices: bool = False


@dataclass(frozen=True, slots=True)
class OfferApprovalDecision:
    auto_approvable: bool
    reasons: tuple[str, ...]


def calculate_reseller_price(
    *,
    amount: Decimal | int | str,
    currency: str,
    fx_rate_to_aud: Decimal | int | str,
    fx_rate_date: date,
) -> PricingResult:
    """Apply max(1.5x cost, cost + AUD 100), rounded up to AUD 10."""

    original = Decimal(str(amount))
    rate = Decimal(str(fx_rate_to_aud))
    normalized_currency = currency.strip().upper()
    if original <= 0:
        raise ValueError("offer amount must be greater than zero")
    if rate <= 0:
        raise ValueError("FX rate must be greater than zero")
    if len(normalized_currency) != 3 or not normalized_currency.isalpha():
        raise ValueError("currency must be a three-letter code")

    cost_aud = (original * rate).quantize(Decimal("0.01"))
    raw_reseller = max(cost_aud * Decimal("1.5"), cost_aud + Decimal("100"))
    rounded = (raw_reseller / Decimal("10")).to_integral_value(
        rounding=ROUND_CEILING
    ) * Decimal("10")
    return PricingResult(
        original_amount=original.quantize(Decimal("0.01")),
        original_currency=normalized_currency,
        fx_rate_to_aud=rate,
        fx_rate_date=fx_rate_date,
        cost_aud=cost_aud,
        reseller_price_aud=rounded.quantize(Decimal("0.01")),
    )


def assess_offer_for_auto_approval(
    extraction: OfferExtraction,
    *,
    minimum_confidence: Decimal = Decimal("0.90"),
) -> OfferApprovalDecision:
    reasons: list[str] = []
    if not extraction.domain:
        reasons.append("missing_domain")
    if extraction.amount is None or extraction.amount <= 0:
        reasons.append("missing_or_invalid_amount")
    if not extraction.currency:
        reasons.append("missing_currency")
    if not extraction.placement_type:
        reasons.append("missing_placement_type")
    if extraction.confidence < minimum_confidence:
        reasons.append("low_confidence")
    if extraction.attachment_only:
        reasons.append("attachment_only")
    if extraction.redirect:
        reasons.append("redirect")
    if extraction.agency_or_network_ambiguity:
        reasons.append("agency_or_network_ambiguity")
    if extraction.conflicting_prices:
        reasons.append("conflicting_prices")
    return OfferApprovalDecision(not reasons, tuple(reasons))
