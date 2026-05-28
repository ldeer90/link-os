from __future__ import annotations

import re
from dataclasses import dataclass

from .utils import is_australian_candidate_domain


PHRASES: list[tuple[str, str, str]] = [
    ("write for us", "guest_post", "write for us"),
    ("guest post", "guest_post", "guest post"),
    ("guest article", "guest_post", "guest article"),
    ("submit an article", "editorial_contribution", "submit an article"),
    ("contribute", "editorial_contribution", "contribute"),
    ("editorial guidelines", "editorial_contribution", "editorial guidelines"),
    ("sponsored post", "sponsored_post", "sponsored post"),
    ("sponsored article", "sponsored_post", "sponsored article"),
    ("sponsored content", "sponsored_post", "sponsored content"),
    ("advertise with us", "advertising_media_kit", "advertise with us"),
    ("advertising", "advertising_media_kit", "advertising"),
    ("media kit", "advertising_media_kit", "media kit"),
    ("rate card", "advertising_media_kit", "rate card"),
    ("native advertising", "advertising_media_kit", "native advertising"),
    ("link insertion", "niche_edit_possible", "link insertion"),
    ("link insertions", "niche_edit_possible", "link insertions"),
    ("resource page", "resource_page", "resource page"),
    ("links page", "resource_page", "links page"),
]

RISKY_RE = re.compile(r"\b(casino|pokies|betting|gambling|adult|porn|escort|essay|assignment help|pharma|cbd|payday)\b", re.I)
LOW_QUALITY_RE = re.compile(r"\b(pbn|write for us all niches|all niche guest post|da\s*\d+\s*guest post|cheap guest post)\b", re.I)

NICHE_PATTERNS: list[tuple[str, str]] = [
    ("travel", r"\b(travel|tourism|destination|hotel|holiday|caravan|camping)\b"),
    ("health/wellness", r"\b(health|wellness|fitness|medical|dental|nutrition|yoga)\b"),
    ("finance/business", r"\b(finance|business|startup|accounting|insurance|mortgage|property investing)\b"),
    ("parenting/family", r"\b(parenting|family|mum|mums|kids|baby|child)\b"),
    ("home/lifestyle", r"\b(home|garden|interior|lifestyle|fashion|beauty|food|recipe)\b"),
    ("tech", r"\b(tech|software|digital|gadget|it services|cyber)\b"),
    ("local/community", r"\b(local|community|news|magazine|what's on|events)\b"),
    ("trade/construction", r"\b(builder|construction|trade|plumbing|electrical|renovation)\b"),
]


@dataclass(frozen=True)
class OpportunityMatch:
    opportunity_type: str
    matched_phrase: str
    has_media_kit: bool
    has_write_for_us_page: bool
    has_sponsored_post_language: bool


def classify_opportunity(text: str, url: str = "") -> OpportunityMatch:
    url_haystack = re.sub(r"[-_/]+", " ", (url or "").lower())
    for phrase, kind, label in PHRASES:
        if phrase in url_haystack:
            return OpportunityMatch(
                opportunity_type=kind,
                matched_phrase=label,
                has_media_kit="media kit" in url_haystack or "rate card" in url_haystack,
                has_write_for_us_page="write for us" in url_haystack or "guest post" in url_haystack or "submit an article" in url_haystack,
                has_sponsored_post_language="sponsored" in url_haystack or "advertorial" in url_haystack,
            )
    haystack = f"{url} {text}".lower()
    haystack = re.sub(r"[-_/]+", " ", haystack)
    matches = [(phrase, kind, label) for phrase, kind, label in PHRASES if phrase in haystack]
    if not matches:
        return OpportunityMatch("unclear_contact_only", "", False, False, False)
    phrase, kind, label = matches[0]
    return OpportunityMatch(
        opportunity_type=kind,
        matched_phrase=label,
        has_media_kit="media kit" in haystack or "rate card" in haystack,
        has_write_for_us_page="write for us" in haystack or "guest post" in haystack or "submit an article" in haystack,
        has_sponsored_post_language="sponsored" in haystack or "advertorial" in haystack,
    )


def classify_niche(text: str) -> str:
    for niche, pattern in NICHE_PATTERNS:
        if re.search(pattern, text or "", re.I):
            return niche
    return "general"


def spam_reject_reason(text: str, root_domain: str) -> str:
    if not is_australian_candidate_domain(root_domain, text):
        return "not_australian_domain_or_context"
    if RISKY_RE.search(text or ""):
        return "risky_niche"
    if LOW_QUALITY_RE.search(text or ""):
        return "low_quality_guest_post_marketplace_language"
    return ""


def quality_tier(
    *,
    opportunity_type: str,
    best_email_type: str,
    has_clear_opportunity_page: bool,
    reject_reason: str,
) -> str:
    if reject_reason:
        return "Reject"
    direct_types = {"editor", "content", "advertising", "media", "partnerships", "sponsored"}
    generic_types = {"generic"}
    if has_clear_opportunity_page and best_email_type in direct_types:
        return "A"
    if opportunity_type != "unclear_contact_only" and (best_email_type in direct_types or best_email_type in generic_types):
        return "B"
    if best_email_type in direct_types or best_email_type in generic_types:
        return "C"
    return "Reject"


def why_relevant(site_name: str, niche: str, opportunity_type: str) -> str:
    label = site_name or "the site"
    if opportunity_type == "advertising_media_kit":
        return f"{label} appears to publish Australian {niche} content and has advertising/media kit language."
    if opportunity_type == "sponsored_post":
        return f"{label} appears to publish Australian {niche} content and mentions sponsored placements."
    if opportunity_type in {"guest_post", "editorial_contribution"}:
        return f"{label} appears to publish Australian {niche} content and has contribution/editorial language."
    return f"{label} appears to be an Australian {niche} site with a public business contact path."
