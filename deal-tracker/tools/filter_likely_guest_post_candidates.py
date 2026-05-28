#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import html
import json
import re
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.import_csv_prospects_to_instantly import clean_domain  # noqa: E402


DEFAULT_INPUT = ROOT / "generated" / "imports" / "combined_link_prospects_contact_discovery_review.csv"
DEFAULT_OUTPUT = ROOT / "generated" / "imports" / "likely_guest_post_candidates_strict.csv"
DEFAULT_SUMMARY = ROOT / "generated" / "imports" / "likely_guest_post_candidates_strict_summary.json"

USER_AGENT = "LD Search strict guest post filter/1.0 (+https://ldsearch.com.au)"
MAX_HTML_BYTES = 900_000

ROLE_LOCALS = {
    "advertising",
    "advertise",
    "ads",
    "media",
    "partnership",
    "partnerships",
    "partner",
    "partners",
    "sponsor",
    "sponsored",
    "sponsorship",
    "editor",
    "editorial",
    "content",
    "submission",
    "submissions",
    "submit",
    "contributor",
    "contributors",
}

GENERIC_LOCALS = {"info", "hello", "contact", "admin", "support", "enquiries", "enquiry"}

PUBLISHER_TYPES = ["media / editorial", "blog / guide", "reviews / comparison", "deals / affiliate"]

COMMERCIAL_TERMS = [
    "advertise with us",
    "advertising with us",
    "advertising opportunities",
    "advertising packages",
    "advertising package",
    "advertorial",
    "sponsored post",
    "sponsored posts",
    "sponsored article",
    "sponsored articles",
    "native advertising",
    "commercial content",
    "paid post",
    "paid article",
    "media kit",
    "rate card",
    "ratecard",
]

EDITORIAL_TERMS = [
    "write for us",
    "guest post",
    "guest posts",
    "guest article",
    "guest blogger",
    "contributor",
    "contributors",
    "contribute",
    "submit an article",
    "submit article",
    "submit a story",
    "submissions",
    "article submission",
    "editorial guidelines",
    "editorial policy",
]

PARTNERSHIP_TERMS = [
    "partner with us",
    "partnership opportunities",
    "partnerships",
    "brand partnerships",
    "sponsorship opportunities",
    "sponsorship",
]

URL_INTENT_TERMS = [
    "advertis",
    "media-kit",
    "mediakit",
    "rate-card",
    "sponsor",
    "commercial-content",
    "native-advertising",
    "editorial",
    "submission",
    "submit",
    "write-for-us",
    "guest-post",
    "contributor",
    "contribute",
    "partner-with-us",
    "partnership",
]

PRODUCT_SERVICE_URL_TERMS = [
    "/product",
    "/products",
    "/shop",
    "/cart",
    "/checkout",
    "/service",
    "/services",
    "/portfolio",
    "/case-stud",
    "/jobs",
    "/careers",
    "/privacy",
    "/terms",
]

CORPORATE_ONLY_TERMS = [
    "media release",
    "media releases",
    "press release",
    "press releases",
    "investor relations",
    "newsroom",
]


@dataclass
class SourceRow:
    root_domain: str
    site_name: str
    email: str
    email_type: str
    confidence: int
    source_url: str
    discovery_method: str
    is_off_domain_email: bool
    best_type: str
    paid_link_likelihood_score: int
    brands: str
    example_source_urls: str
    all_candidates: str
    recommended_action: str


@dataclass
class Evidence:
    final_url: str
    text: str
    fetched: bool
    error: str = ""


@dataclass
class ScoreResult:
    score: int
    tier: str
    acceptance_signals: list[str]
    risk_flags: list[str]
    reject_reason: str
    evidence_excerpt: str


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def to_int(value: str) -> int:
    try:
        return int(float(value or 0))
    except ValueError:
        return 0


def read_email_rows(path: Path) -> list[SourceRow]:
    rows: list[SourceRow] = []
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
        for row in csv.DictReader(handle):
            email = (row.get("email") or "").strip().lower()
            domain = clean_domain(row.get("root_domain") or "")
            if not email or not domain:
                continue
            rows.append(
                SourceRow(
                    root_domain=domain,
                    site_name=(row.get("site_name") or "").strip() or domain,
                    email=email,
                    email_type=(row.get("email_type") or "").strip(),
                    confidence=to_int(row.get("confidence") or ""),
                    source_url=(row.get("source_url") or "").strip(),
                    discovery_method=(row.get("discovery_method") or "").strip(),
                    is_off_domain_email=(row.get("is_off_domain_email") or "").strip().lower() == "yes",
                    best_type=(row.get("best_type") or "").strip(),
                    paid_link_likelihood_score=to_int(row.get("paid_link_likelihood_score") or ""),
                    brands=(row.get("brands") or "").strip(),
                    example_source_urls=(row.get("example_source_urls") or "").strip(),
                    all_candidates=(row.get("all_candidates") or "").strip(),
                    recommended_action=(row.get("recommended_action") or "").strip(),
                )
            )
    return rows


def group_by_domain(rows: list[SourceRow]) -> dict[str, list[SourceRow]]:
    grouped: dict[str, list[SourceRow]] = defaultdict(list)
    for row in rows:
        grouped[row.root_domain].append(row)
    return dict(grouped)


def local_part(email: str) -> str:
    return email.split("@", 1)[0].lower() if "@" in email else ""


def role_email(row: SourceRow) -> bool:
    local = local_part(row.email)
    return any(term in local for term in ROLE_LOCALS) or row.email_type in {"advertising", "editorial", "partnerships"}


def generic_email(row: SourceRow) -> bool:
    return local_part(row.email) in GENERIC_LOCALS or row.email_type == "generic"


def publisherish(best_type: str) -> bool:
    text = best_type.lower()
    return any(term in text for term in PUBLISHER_TYPES)


def contains_any(text: str, terms: list[str] | set[str]) -> list[str]:
    lowered = text.lower()
    return [term for term in terms if term in lowered]


def html_to_text(body: str) -> str:
    body = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", body)
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    body = html.unescape(body)
    return re.sub(r"\s+", " ", body).strip()


def fetch_evidence(url: str, timeout: float) -> Evidence:
    if not url.startswith(("http://", "https://")):
        return Evidence(final_url=url, text="", fetched=False, error="invalid_url")
    req = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
        },
    )
    try:
        with urlopen(req, timeout=timeout) as response:
            content_type = response.headers.get("content-type", "")
            if "text/html" not in content_type and "text/plain" not in content_type and not content_type:
                return Evidence(final_url=response.geturl(), text="", fetched=False, error=f"non_text_content:{content_type}")
            raw = response.read(MAX_HTML_BYTES)
            charset = response.headers.get_content_charset() or "utf-8"
            text = html_to_text(raw.decode(charset, errors="replace"))
            return Evidence(final_url=response.geturl(), text=text, fetched=True)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        return Evidence(final_url=url, text="", fetched=False, error=exc.__class__.__name__)


def batch_fetch_evidence(rows: list[SourceRow], workers: int, timeout: float) -> dict[str, Evidence]:
    urls = sorted({row.source_url for row in rows if row.source_url.startswith(("http://", "https://"))})
    evidence: dict[str, Evidence] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {executor.submit(fetch_evidence, url, timeout): url for url in urls}
        completed = 0
        for future in as_completed(futures):
            url = futures[future]
            completed += 1
            try:
                evidence[url] = future.result()
            except Exception as exc:  # noqa: BLE001
                evidence[url] = Evidence(final_url=url, text="", fetched=False, error=str(exc)[:120])
            if completed % 250 == 0 or completed == len(urls):
                print(f"Fetched evidence {completed}/{len(urls)}", flush=True)
    return evidence


def excerpt_for(text: str, terms: list[str]) -> str:
    if not text:
        return ""
    lowered = text.lower()
    indexes = [lowered.find(term) for term in terms if lowered.find(term) >= 0]
    index = min(indexes) if indexes else 0
    start = max(0, index - 180)
    end = min(len(text), index + 320)
    return text[start:end].strip()


def source_url_has_intent(url: str) -> bool:
    return bool(contains_any(url.lower(), URL_INTENT_TERMS))


def score_row(row: SourceRow, evidence: Evidence) -> ScoreResult:
    haystack = " ".join([row.source_url, row.best_type, row.example_source_urls, evidence.text]).lower()
    source_url = row.source_url.lower()
    signals: list[str] = []
    risks: list[str] = []
    score = 0

    commercial = contains_any(haystack, COMMERCIAL_TERMS)
    editorial = contains_any(haystack, EDITORIAL_TERMS)
    partnership = contains_any(haystack, PARTNERSHIP_TERMS)
    url_intent = contains_any(source_url, URL_INTENT_TERMS)
    publisher = publisherish(row.best_type)
    role = role_email(row)
    generic = generic_email(row)

    if row.recommended_action == "ready_for_import":
        score += 10
        signals.append("ready_for_import_email")
    if row.confidence >= 90:
        score += 12
        signals.append("email_confidence_90_plus")
    elif row.confidence >= 80:
        score += 8
        signals.append("email_confidence_80_plus")
    else:
        score -= 25
        risks.append("email_confidence_below_80")

    if row.is_off_domain_email:
        score -= 25
        risks.append("off_domain_email")
    else:
        score += 8
        signals.append("on_domain_email")

    if role:
        score += 22
        signals.append("role_relevant_email")
    elif generic:
        score -= 6
        risks.append("generic_email")

    if commercial:
        score += 35
        signals.append("commercial_placement_terms:" + ", ".join(commercial[:4]))
    if editorial:
        score += 30
        signals.append("editorial_submission_terms:" + ", ".join(editorial[:4]))
    if partnership:
        score += 18
        signals.append("partnership_terms:" + ", ".join(partnership[:3]))
    if url_intent:
        score += 18
        signals.append("intent_url:" + ", ".join(url_intent[:3]))
    if publisher:
        score += 14
        signals.append("publisher_like_link_source")

    if source_url_has_intent(source_url) and publisher and not (commercial or editorial or partnership):
        score += 10
        signals.append("strong_url_intent_with_publisher_context")

    if not evidence.fetched:
        score -= 6
        risks.append("evidence_fetch_failed:" + evidence.error)

    if generic and not (commercial or editorial or partnership or url_intent):
        score -= 22
        risks.append("generic_contact_without_placement_evidence")

    corporate_only = contains_any(haystack, CORPORATE_ONLY_TERMS)
    if corporate_only and not (commercial or editorial or partnership or "advertis" in source_url):
        score -= 18
        risks.append("corporate_media_relations_only")

    product_url = contains_any(source_url, PRODUCT_SERVICE_URL_TERMS)
    if product_url and not (commercial or editorial or partnership or "sponsor" in source_url or "advertis" in source_url):
        score -= 25
        risks.append("product_or_service_page")

    has_placement_intent = bool(commercial or editorial or partnership or url_intent)
    has_contactability = row.confidence >= 80 and not row.is_off_domain_email
    score = max(0, min(100, score))

    if not has_contactability:
        tier = "Manual Review"
        reject_reason = "Contact is off-domain or below confidence threshold."
    elif not has_placement_intent:
        tier = "Exclude"
        reject_reason = "No meaningful guest post, sponsored, contributor, advertising, or editorial placement signal."
    elif score >= 82 and role and (commercial or editorial or "advertis" in source_url or "media-kit" in source_url):
        tier = "Tier 1"
        reject_reason = ""
    elif score >= 68 and publisher and (role or commercial or editorial or partnership or url_intent):
        tier = "Tier 2"
        reject_reason = ""
    elif score >= 54 and publisher:
        tier = "Tier 3"
        reject_reason = ""
    else:
        tier = "Manual Review"
        reject_reason = "Placement signal is ambiguous or weak."

    if any(flag in risks for flag in ["product_or_service_page", "generic_contact_without_placement_evidence"]) and tier == "Tier 3":
        tier = "Manual Review"
        reject_reason = "Likely normal business contact rather than publisher placement route."

    excerpt_terms = commercial + editorial + partnership + url_intent
    return ScoreResult(
        score=score,
        tier=tier,
        acceptance_signals=signals,
        risk_flags=risks,
        reject_reason=reject_reason,
        evidence_excerpt=excerpt_for(evidence.text, excerpt_terms),
    )


def all_contacts(rows: list[SourceRow]) -> str:
    seen: set[str] = set()
    contacts: list[str] = []
    for row in sorted(rows, key=lambda item: (item.confidence, role_email(item), not item.is_off_domain_email), reverse=True):
        if row.email in seen:
            continue
        seen.add(row.email)
        contacts.append(f"{row.email} ({row.email_type}, {row.confidence}, {row.source_url})")
    return "; ".join(contacts)


def choose_domain_candidate(rows: list[SourceRow], evidence_map: dict[str, Evidence]) -> tuple[SourceRow, ScoreResult, Evidence]:
    scored: list[tuple[int, int, SourceRow, ScoreResult, Evidence]] = []
    tier_rank = {"Tier 1": 5, "Tier 2": 4, "Tier 3": 3, "Manual Review": 2, "Exclude": 1}
    for row in rows:
        evidence = evidence_map.get(row.source_url) or Evidence(final_url=row.source_url, text="", fetched=False, error="missing_evidence")
        result = score_row(row, evidence)
        scored.append((tier_rank.get(result.tier, 0), result.score, row, result, evidence))
    scored.sort(key=lambda item: (item[0], item[1], item[2].confidence, role_email(item[2]), not item[2].is_off_domain_email), reverse=True)
    _, _, row, result, evidence = scored[0]
    return row, result, evidence


def output_row(row: SourceRow, result: ScoreResult, evidence: Evidence, contacts: str) -> dict[str, Any]:
    return {
        "root_domain": row.root_domain,
        "site_name": row.site_name,
        "guest_post_fit_score": result.score,
        "guest_post_fit_tier": result.tier,
        "acceptance_signals": "; ".join(result.acceptance_signals),
        "risk_flags": "; ".join(result.risk_flags),
        "reject_reason": result.reject_reason,
        "best_contact_email": row.email,
        "best_contact_type": row.email_type,
        "email_confidence": row.confidence,
        "is_off_domain_email": "yes" if row.is_off_domain_email else "no",
        "evidence_url": evidence.final_url or row.source_url,
        "evidence_excerpt": result.evidence_excerpt,
        "source_url": row.source_url,
        "discovery_method": row.discovery_method,
        "best_type": row.best_type,
        "paid_link_likelihood_score": row.paid_link_likelihood_score,
        "brands": row.brands,
        "example_source_urls": row.example_source_urls,
        "all_contacts": contacts,
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "root_domain",
        "site_name",
        "guest_post_fit_score",
        "guest_post_fit_tier",
        "acceptance_signals",
        "risk_flags",
        "reject_reason",
        "best_contact_email",
        "best_contact_type",
        "email_confidence",
        "is_off_domain_email",
        "evidence_url",
        "evidence_excerpt",
        "source_url",
        "discovery_method",
        "best_type",
        "paid_link_likelihood_score",
        "brands",
        "example_source_urls",
        "all_contacts",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def write_summary(path: Path, input_rows: int, domains: int, all_scored_rows: list[dict[str, Any]], output_rows: list[dict[str, Any]]) -> dict[str, Any]:
    summary = {
        "generated_at": now_iso(),
        "input_email_row_count": input_rows,
        "input_domain_count": domains,
        "output_domain_count": len(output_rows),
        "all_scored_domain_count": len(all_scored_rows),
        "omitted_exclude_count": sum(1 for row in all_scored_rows if row["guest_post_fit_tier"] == "Exclude" and row not in output_rows),
        "rows_by_tier_all_scored": dict(Counter(row["guest_post_fit_tier"] for row in all_scored_rows)),
        "rows_by_tier_output": dict(Counter(row["guest_post_fit_tier"] for row in output_rows)),
        "rows_by_contact_type": dict(Counter(row["best_contact_type"] or "none" for row in output_rows)),
        "tier_1_2_3_count": sum(1 for row in output_rows if row["guest_post_fit_tier"] in {"Tier 1", "Tier 2", "Tier 3"}),
        "manual_review_count": sum(1 for row in output_rows if row["guest_post_fit_tier"] == "Manual Review"),
        "exclude_count": sum(1 for row in all_scored_rows if row["guest_post_fit_tier"] == "Exclude"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Strictly filter discovered contacts down to likely guest post/link-placement candidates.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--workers", type=int, default=48)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--include-excluded", action="store_true", help="Write Exclude rows too. Default writes Tier/Manual Review rows only.")
    args = parser.parse_args()

    source_rows = read_email_rows(args.input)
    grouped = group_by_domain(source_rows)
    print(f"Input email rows: {len(source_rows)}", flush=True)
    print(f"Input domains with emails: {len(grouped)}", flush=True)
    evidence_map = batch_fetch_evidence(source_rows, workers=args.workers, timeout=args.timeout)

    all_scored_rows: list[dict[str, Any]] = []
    for domain_rows in grouped.values():
        row, result, evidence = choose_domain_candidate(domain_rows, evidence_map)
        all_scored_rows.append(output_row(row, result, evidence, all_contacts(domain_rows)))

    tier_order = {"Tier 1": 0, "Tier 2": 1, "Tier 3": 2, "Manual Review": 3, "Exclude": 4}
    output_rows = all_scored_rows if args.include_excluded else [row for row in all_scored_rows if row["guest_post_fit_tier"] != "Exclude"]
    output_rows.sort(
        key=lambda row: (
            tier_order.get(row["guest_post_fit_tier"], 9),
            -int(row["guest_post_fit_score"]),
            -int(row["paid_link_likelihood_score"]),
            row["root_domain"],
        )
    )
    write_csv(args.output, output_rows)
    summary = write_summary(args.summary, len(source_rows), len(grouped), all_scored_rows, output_rows)
    print(json.dumps(summary, indent=2, sort_keys=True), flush=True)
    print(f"Output: {args.output}", flush=True)
    print(f"Summary: {args.summary}", flush=True)


if __name__ == "__main__":
    main()
