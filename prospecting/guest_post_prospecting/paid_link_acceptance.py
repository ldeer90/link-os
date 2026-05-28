from __future__ import annotations

import csv
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .paths import REVIEW_DIR, SEARCH_HARVEST_DIR
from .utils import clean_domain


DEFAULT_DETAILED_REVIEW_CSV = SEARCH_HARVEST_DIR / "apify_google_detailed_review_harvest_0_2373_emails_0_2373.csv"
PAID_LINK_REVIEW_CSV = REVIEW_DIR / "paid_link_acceptance_review.csv"
PAID_LINK_SUMMARY_JSON = REVIEW_DIR / "paid_link_acceptance_summary.json"

PAID_LINK_FIELDS = [
    "paid_link_likelihood",
    "paid_link_score",
    "paid_link_evidence",
    "transaction_type",
    "money_terms_found",
    "seller_style_flags",
    "contactability",
    "recommended_next_action",
]

DIRECT_EMAIL_TYPES = {"advertising", "media", "partnerships", "sponsored", "editor", "content"}
GENERIC_EMAIL_TYPES = {"generic"}

TERM_PATTERNS: list[tuple[str, str, int, str]] = [
    ("link insertion", r"\blink insertions?\b", 35, "niche_edit"),
    ("niche edit", r"\bniche edits?\b", 35, "niche_edit"),
    ("buy guest post", r"\bbuy (?:a )?guest posts?\b", 35, "marketplace"),
    ("paid guest post", r"\bpaid guest posts?\b", 34, "marketplace"),
    ("guest post pricing", r"\bguest post(?:ing)? (?:pricing|price|packages?|rates?)\b", 34, "marketplace"),
    ("sponsored post", r"\bsponsored posts?\b", 30, "sponsored_post"),
    ("sponsored article", r"\bsponsored articles?\b", 30, "sponsored_post"),
    ("sponsored content", r"\bsponsored content\b", 28, "sponsored_post"),
    ("advertorial", r"\badvertorials?\b", 30, "advertorial"),
    ("advertising packages", r"\badvertising packages?\b", 28, "advertising_package"),
    ("advertising opportunities", r"\badvertising opportunities\b", 24, "advertising_package"),
    ("advertising enquiry", r"\badvertising enquir(?:y|ies)\b", 23, "advertising_package"),
    ("advertise with us", r"\badvertise with us\b", 28, "advertising_package"),
    ("media kit", r"\bmedia kits?\b", 26, "media_kit"),
    ("media rates", r"\bmedia rates?\b", 25, "media_kit"),
    ("rate card", r"\brate cards?\b", 26, "media_kit"),
    ("pricing", r"\bpricing\b", 18, "unclear"),
    ("packages", r"\bpackages?\b", 16, "unclear"),
    ("guest post guidelines", r"\bguest post(?:ing)? guidelines?\b", 24, "guest_post"),
    ("write for us", r"\bwrite for us\b", 22, "guest_post"),
    ("guest blogger", r"\bguest blogg(?:er|ing)\b", 20, "guest_post"),
    ("guest contributor", r"\bguest contributor\b", 20, "guest_post"),
    ("guest post", r"\bguest posts?\b", 20, "guest_post"),
    ("guest article", r"\bguest articles?\b", 19, "guest_post"),
    ("submit article", r"\bsubmit (?:an? |your )?articles?\b", 18, "guest_post"),
    ("submit story", r"\bsubmit (?:a |your )?story\b", 18, "guest_post"),
    ("pitch us", r"\bpitch us\b", 17, "guest_post"),
    ("editorial guidelines", r"\beditorial guidelines?\b", 17, "guest_post"),
    ("contributor guidelines", r"\bcontributor guidelines?\b", 17, "guest_post"),
    ("contribute", r"\bcontribut(?:e|or|ors|ing)\b", 14, "guest_post"),
]

MARKETPLACE_PATTERNS: list[tuple[str, str]] = [
    ("pbn_like", r"\bpbn\b|\bprivate blog network\b|\bda\s*\d+\s*guest post\b|\bdr\s*\d+\s*guest post\b"),
    ("guest_post_marketplace", r"\bguest posting sites?\b|\bguest post sites? list\b|\ball niche\b|\bbuy (?:a )?guest posts?\b|\bpaid guest posts?\b|\bguest post packages?\b"),
    ("seo_agency", r"\blink building\b|\bseo agency\b|\bseo services?\b|\bdigital marketing agency\b|\bbacklinks?\b"),
]

STYLE_PATTERNS: list[tuple[str, str]] = [
    ("directory", r"\bdirectory\b|\blisting\b|\bclassifieds?\b"),
    ("publisher", r"\bmagazine\b|\bnews\b|\bjournal\b|\bpublication\b|\beditorial\b|\bmedia\b"),
    ("business_blog", r"\bblog\b|\binsights\b|\bresources\b"),
]

CONTACT_PATH_RE = re.compile(r"\b(contact|contact-us|enquir(?:y|ies)|advertise|media-kit|partnership)\b", re.I)


@dataclass(frozen=True)
class PaidLinkScore:
    paid_link_likelihood: str
    paid_link_score: int
    paid_link_evidence: str
    transaction_type: str
    money_terms_found: str
    seller_style_flags: str
    contactability: str
    recommended_next_action: str


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def write_csv_rows(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def unique_join(values: list[str]) -> str:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        text = (value or "").strip()
        if text and text not in seen:
            seen.add(text)
            output.append(text)
    return " | ".join(output)


def haystacks(row: dict[str, str]) -> tuple[str, str, str]:
    strong = " ".join(
        [
            row.get("root_domain", ""),
            row.get("source_url", ""),
            row.get("title", ""),
            row.get("all_result_urls", ""),
            row.get("all_titles", ""),
            row.get("all_matched_phrases", ""),
            row.get("opportunity_type", ""),
            row.get("matched_phrase", ""),
        ]
    ).lower()
    query = row.get("query", "") + " " + row.get("all_queries", "")
    contact = row.get("email", "") + " " + row.get("email_type", "") + " " + row.get("all_emails", "") + " " + row.get("all_email_types", "")
    return strong, query.lower(), contact.lower()


def term_matches(text: str) -> list[tuple[str, int, str]]:
    matches: list[tuple[str, int, str]] = []
    for label, pattern, score, transaction_type in TERM_PATTERNS:
        if re.search(pattern, text, re.I):
            matches.append((label, score, transaction_type))
    return matches


def classify_transaction_type(matches: list[tuple[str, int, str]], opportunity_type: str) -> str:
    priority = ["marketplace", "niche_edit", "advertorial", "sponsored_post", "media_kit", "advertising_package", "guest_post"]
    found = {transaction_type for _label, _score, transaction_type in matches}
    for transaction_type in priority:
        if transaction_type in found:
            return transaction_type
    if opportunity_type in {"guest_post", "sponsored_post", "advertising_media_kit", "niche_edit_possible"}:
        return {
            "advertising_media_kit": "advertising_package",
            "niche_edit_possible": "niche_edit",
        }.get(opportunity_type, opportunity_type)
    return "unclear"


def seller_style_flags(row: dict[str, str], text: str, contactability: str) -> list[str]:
    flags: list[str] = []
    for flag, pattern in MARKETPLACE_PATTERNS + STYLE_PATTERNS:
        if re.search(pattern, text, re.I):
            flags.append(flag)
    domain = clean_domain(row.get("root_domain", ""))
    if domain.endswith(".com") and not domain.endswith(".com.au"):
        flags.append("global_com_noise")
    if contactability == "no_contact_found":
        flags.append("no_contact_found")
    return sorted(set(flags))


def contactability(row: dict[str, str]) -> str:
    email_type = row.get("email_type", "")
    all_email_types = set(filter(None, re.split(r"\s*\|\s*", row.get("all_email_types", ""))))
    if email_type in DIRECT_EMAIL_TYPES or all_email_types & DIRECT_EMAIL_TYPES:
        return "direct_role_email"
    if email_type in GENERIC_EMAIL_TYPES or all_email_types & GENERIC_EMAIL_TYPES or row.get("email"):
        return "generic_email"
    if CONTACT_PATH_RE.search(" ".join([row.get("source_url", ""), row.get("all_result_urls", ""), row.get("all_queries", "")])):
        return "contact_path_only"
    return "no_contact_found"


def likelihood_from_score(score: int, has_meaningful_evidence: bool) -> str:
    if not has_meaningful_evidence or score < 8:
        return "Out Of Scope"
    if score >= 65:
        return "Very Likely"
    if score >= 40:
        return "Likely"
    if score >= 25:
        return "Possible"
    return "Weak"


def recommended_next_action(likelihood: str, contact_status: str) -> str:
    if likelihood == "Out Of Scope":
        return "ignore_for_now"
    if contact_status in {"direct_role_email", "generic_email"} and likelihood in {"Very Likely", "Likely", "Possible"}:
        return "email_pricing_request"
    if contact_status in {"contact_path_only", "no_contact_found"} and likelihood in {"Very Likely", "Likely", "Possible"}:
        return "manual_contact_check"
    return "manual_quality_review"


def score_paid_link_acceptance(row: dict[str, str]) -> PaidLinkScore:
    strong_text, query_text, contact_text = haystacks(row)
    full_text = f"{strong_text} {query_text} {contact_text}"
    strong_matches = term_matches(strong_text)
    query_matches = term_matches(query_text)
    all_matches = strong_matches + [match for match in query_matches if match[0] not in {label for label, _score, _kind in strong_matches}]
    terms = unique_join([label for label, _score, _kind in all_matches])

    score = 0
    evidence: list[str] = []
    if strong_matches:
        best = max(score for _label, score, _kind in strong_matches)
        score += best
        evidence.append(f"strong placement term: {max(strong_matches, key=lambda item: item[1])[0]}")
        if len(strong_matches) > 1:
            score += min(18, (len(strong_matches) - 1) * 5)
    if query_matches and not strong_matches:
        best_query = max(score for _label, score, _kind in query_matches)
        score += max(10, int(best_query * 0.55))
        evidence.append(f"query-level placement term: {max(query_matches, key=lambda item: item[1])[0]}")
    elif query_matches:
        score += min(10, len(query_matches) * 2)

    status = contactability(row)
    if status == "direct_role_email":
        score += 18
        evidence.append("direct role email")
    elif status == "generic_email":
        score += 12
        evidence.append("generic email")
    elif status == "contact_path_only":
        score += 5
        evidence.append("contact path")
    else:
        score -= 12
        evidence.append("no contact found")

    email_count = int(row.get("email_count") or 0)
    if email_count > 1:
        score += min(8, email_count)

    flags = seller_style_flags(row, full_text, status)
    if {"pbn_like", "guest_post_marketplace", "seo_agency"} & set(flags):
        score += 18
        evidence.append("seller/marketplace style")
    if "global_com_noise" in flags:
        score -= 10
        evidence.append("Australian-context .com")

    if row.get("quality_tier") == "A":
        score += 8
    elif row.get("quality_tier") == "B":
        score += 6
    elif row.get("quality_tier") == "Reject" and row.get("reject_reason") == "no_usable_business_email":
        score -= 3

    has_meaningful_evidence = bool(strong_matches or query_matches or row.get("opportunity_type") not in {"", "unclear_contact_only"})
    if has_meaningful_evidence and query_matches and not strong_matches and score < 8:
        score = 8
    score = max(0, min(100, score))
    likelihood = likelihood_from_score(score, has_meaningful_evidence)
    transaction_type = classify_transaction_type(all_matches, row.get("opportunity_type", ""))
    action = recommended_next_action(likelihood, status)
    if not evidence:
        evidence.append("no paid-placement evidence")
    return PaidLinkScore(
        paid_link_likelihood=likelihood,
        paid_link_score=score,
        paid_link_evidence="; ".join(evidence),
        transaction_type=transaction_type,
        money_terms_found=terms,
        seller_style_flags=" | ".join(flags),
        contactability=status,
        recommended_next_action=action,
    )


def score_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    for row in rows:
        score = score_paid_link_acceptance(row)
        scored = {
            "paid_link_likelihood": score.paid_link_likelihood,
            "paid_link_score": str(score.paid_link_score),
            "paid_link_evidence": score.paid_link_evidence,
            "transaction_type": score.transaction_type,
            "money_terms_found": score.money_terms_found,
            "seller_style_flags": score.seller_style_flags,
            "contactability": score.contactability,
            "recommended_next_action": score.recommended_next_action,
        }
        scored.update(row)
        output.append(scored)
    return sorted(output, key=sort_key)


def domain_priority(domain: str) -> int:
    cleaned = clean_domain(domain)
    if cleaned.endswith(".com.au"):
        return 0
    if cleaned.endswith(".au"):
        return 1
    if cleaned.endswith(".com"):
        return 2
    return 3


def sort_key(row: dict[str, str]) -> tuple[int, int, int, int, int, str]:
    likelihood_rank = {"Very Likely": 0, "Likely": 1, "Possible": 2, "Weak": 3, "Out Of Scope": 4}
    contact_rank = {"direct_role_email": 0, "generic_email": 1, "contact_path_only": 2, "no_contact_found": 3}
    explicit_money = 0 if any(term in row.get("money_terms_found", "") for term in ["sponsored", "advertise", "media kit", "rate card", "advertorial", "link insertion", "pricing", "packages"]) else 1
    return (
        likelihood_rank.get(row.get("paid_link_likelihood", ""), 9),
        -int(row.get("paid_link_score") or 0),
        contact_rank.get(row.get("contactability", ""), 9),
        explicit_money,
        domain_priority(row.get("root_domain", "")),
        row.get("root_domain", ""),
    )


def summary_for_rows(rows: list[dict[str, str]]) -> dict[str, object]:
    flag_counter: Counter[str] = Counter()
    for row in rows:
        for flag in re.split(r"\s*\|\s*", row.get("seller_style_flags", "")):
            if flag:
                flag_counter[flag] += 1
    return {
        "total_rows": len(rows),
        "by_paid_link_likelihood": dict(Counter(row.get("paid_link_likelihood", "") for row in rows)),
        "by_transaction_type": dict(Counter(row.get("transaction_type", "") for row in rows)),
        "by_contactability": dict(Counter(row.get("contactability", "") for row in rows)),
        "by_recommended_next_action": dict(Counter(row.get("recommended_next_action", "") for row in rows)),
        "seller_style_flags": dict(flag_counter),
        "rows_with_email": sum(1 for row in rows if row.get("email")),
        "rows_with_direct_role_email": sum(1 for row in rows if row.get("contactability") == "direct_role_email"),
    }


def score_paid_link_file(input_path: Path, output_path: Path = PAID_LINK_REVIEW_CSV, summary_path: Path = PAID_LINK_SUMMARY_JSON) -> dict[str, object]:
    rows = read_csv_rows(input_path)
    scored = score_rows(rows)
    original_fields = list(rows[0].keys()) if rows else []
    write_csv_rows(output_path, scored, PAID_LINK_FIELDS + [field for field in original_fields if field not in PAID_LINK_FIELDS])
    summary = summary_for_rows(scored)
    summary.update({"input": str(input_path), "output": str(output_path), "summary": str(summary_path)})
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    return summary
