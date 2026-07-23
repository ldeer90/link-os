from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from deal_tracker.crawler import registrable_domain
from deal_tracker.platform.pricing import (
    PRICING_RULE_VERSION,
    reseller_price_from_cost_aud,
)


PRICE_PATTERNS = (
    re.compile(r"\b(?P<currency>AUD|USD|NZD|CAD|GBP|EUR)\s*\$?\s*(?P<amount>\d+(?:[.,]\d{1,2})?)", re.I),
    re.compile(r"(?P<symbol>[$£€])\s*(?P<amount>\d+(?:[.,]\d{1,2})?)\s*(?P<currency>AUD|USD|NZD|CAD|GBP|EUR)\b", re.I),
    re.compile(r"(?P<symbol>[£€])\s*(?P<amount>\d+(?:[.,]\d{1,2})?)", re.I),
)
DOMAIN_PATTERN = re.compile(r"(?<![@\w-])(?:https?://)?(?:www\.)?([a-z0-9-]+(?:\.[a-z0-9-]+)+)", re.I)
REDIRECT_PATTERN = re.compile(r"\b(?:contact|email|reach out to|speak to|forward(?:ed)? to)\b.{0,80}@", re.I | re.S)
NETWORK_PATTERN = re.compile(r"\b(?:our sites|site network|publisher network|portfolio of sites|agency rate|reseller list)\b", re.I)


@dataclass(frozen=True)
class ExtractedPrice:
    amount: Decimal
    currency: str


@dataclass(frozen=True)
class OfferDecision:
    auto_approved: bool
    review_reasons: tuple[str, ...]
    domain: str
    amount: Decimal | None = None
    currency: str = ""
    placement_type: str = ""
    confidence: str = "review"


def extract_prices(text: str) -> list[ExtractedPrice]:
    found: dict[tuple[Decimal, str], ExtractedPrice] = {}
    occupied: list[tuple[int, int]] = []
    for pattern in PRICE_PATTERNS:
        for match in pattern.finditer(text or ""):
            if any(match.start() < end and match.end() > start for start, end in occupied):
                continue
            amount = Decimal(match.group("amount").replace(",", ""))
            currency = str(match.groupdict().get("currency") or "").upper()
            symbol = str(match.groupdict().get("symbol") or "")
            if not currency:
                currency = {"£": "GBP", "€": "EUR"}.get(symbol, "")
            if currency:
                item = ExtractedPrice(amount=amount, currency=currency)
                found[(amount, currency)] = item
                occupied.append((match.start(), match.end()))
    return list(found.values())


def _placement_types(text: str) -> set[str]:
    lowered = (text or "").lower()
    found: set[str] = set()
    if any(term in lowered for term in ("link insertion", "link placement", "existing article")):
        found.add("link_insertion")
    if any(term in lowered for term in ("guest post", "sponsored article", "sponsored post", "advertorial", "article submission")):
        found.add("guest_post")
    return found


def _mentioned_domains(text: str) -> set[str]:
    domains: set[str] = set()
    for match in DOMAIN_PATTERN.finditer(text or ""):
        domain = registrable_domain(match.group(1))
        if domain:
            domains.add(domain)
    return domains


def evaluate_offer(
    text: str,
    *,
    expected_domain: str,
    attachments: Iterable[object] = (),
) -> OfferDecision:
    domain = registrable_domain(expected_domain)
    prices = extract_prices(text)
    placements = _placement_types(text)
    mentioned = _mentioned_domains(text)
    reasons: list[str] = []

    if not domain:
        reasons.append("missing_or_invalid_domain")
    if len(prices) != 1:
        reasons.append("missing_or_conflicting_price" if not prices else "multiple_prices")
    if len(placements) != 1:
        reasons.append("missing_or_ambiguous_placement_type")
    if attachments and not prices:
        reasons.append("attachment_only_or_attachment_dependent")
    if REDIRECT_PATTERN.search(text or ""):
        reasons.append("redirected_contact")
    if NETWORK_PATTERN.search(text or ""):
        reasons.append("agency_or_network_ambiguity")
    if mentioned - ({domain} if domain else set()):
        reasons.append("multiple_or_conflicting_domains")

    unique_reasons = tuple(dict.fromkeys(reasons))
    if unique_reasons:
        return OfferDecision(
            auto_approved=False,
            review_reasons=unique_reasons,
            domain=domain,
            amount=prices[0].amount if len(prices) == 1 else None,
            currency=prices[0].currency if len(prices) == 1 else "",
            placement_type=next(iter(placements)) if len(placements) == 1 else "",
        )
    price = prices[0]
    return OfferDecision(
        auto_approved=True,
        review_reasons=(),
        domain=domain,
        amount=price.amount,
        currency=price.currency,
        placement_type=next(iter(placements)),
        confidence="high",
    )


def reseller_price_aud(cost_amount: Decimal | float | int, fx_to_aud: Decimal | float | int) -> Decimal:
    cost_aud = Decimal(str(cost_amount)) * Decimal(str(fx_to_aud))
    return reseller_price_from_cost_aud(cost_aud)
