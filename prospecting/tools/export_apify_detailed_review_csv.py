#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from guest_post_prospecting.classifier import (
    classify_niche,
    classify_opportunity,
    quality_tier,
    spam_reject_reason,
    why_relevant,
)
from guest_post_prospecting.paths import REVIEW_CSV


DETAIL_FIELDS = [
    "root_domain",
    "site_name",
    "source_url",
    "matched_phrase",
    "opportunity_type",
    "niche",
    "email",
    "email_type",
    "email_confidence",
    "email_source_url",
    "email_source_page_type",
    "quality_tier",
    "why_relevant",
    "reject_reason",
    "query",
    "query_family",
    "operator_type",
    "query_offset",
    "title",
    "source_engine",
    "harvested_at",
    "result_url_count",
    "all_result_urls",
    "all_titles",
    "all_queries",
    "all_matched_phrases",
    "all_operator_types",
    "email_count",
    "all_emails",
    "all_email_types",
    "all_email_source_urls",
    "all_email_source_page_types",
    "all_email_confidences",
    "has_clear_opportunity_page",
    "has_direct_email",
    "has_generic_email",
    "harvest_row_count",
    "email_row_count",
]

DIRECT_EMAIL_TYPES = {"editor", "content", "advertising", "media", "partnerships", "sponsored"}
GENERIC_EMAIL_TYPES = {"generic"}
EMAIL_TYPE_RANK = {
    "advertising": 0,
    "media": 1,
    "partnerships": 2,
    "sponsored": 3,
    "editor": 4,
    "content": 5,
    "generic": 6,
    "": 7,
}


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def uniq(values: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        text = (value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        output.append(text)
    return output


def join_values(values: list[str]) -> str:
    return " | ".join(uniq(values))


def site_name_from_row(row: dict[str, str]) -> str:
    title = (row.get("title") or "").strip()
    title = re.split(r"\s+[-|–]\s+", title, maxsplit=1)[0].strip()
    if title and len(title) <= 80:
        return title
    domain = row.get("root_domain", "")
    return domain.removesuffix(".com.au").replace("-", " ").title()


def best_harvest_row(rows: list[dict[str, str]]) -> dict[str, str]:
    def score(row: dict[str, str]) -> tuple[int, int, int]:
        url = row.get("result_url", "").lower()
        phrase = row.get("matched_phrase", "")
        clear_url = any(token in url for token in ["write-for-us", "guest", "contribut", "advertis", "media-kit", "sponsor", "editorial"])
        return (1 if phrase else 0, 1 if clear_url else 0, -len(url))

    return sorted(rows, key=score, reverse=True)[0] if rows else {}


def best_email_row(rows: list[dict[str, str]]) -> dict[str, str]:
    def score(row: dict[str, str]) -> tuple[int, int, int]:
        email_type = row.get("email_type", "")
        confidence = int(row.get("confidence") or 0)
        page_type = row.get("source_page_type", "")
        page_score = 1 if page_type in {"advertising", "editorial", "contact"} else 0
        return (-EMAIL_TYPE_RANK.get(email_type, 8), confidence, page_score)

    return sorted(rows, key=score, reverse=True)[0] if rows else {}


def build_detailed_rows(harvest_rows: list[dict[str, str]], email_rows: list[dict[str, str]]) -> list[dict[str, str]]:
    harvest_by_domain: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in harvest_rows:
        domain = (row.get("root_domain") or "").strip()
        if domain:
            harvest_by_domain[domain].append(row)

    email_by_domain: dict[str, list[dict[str, str]]] = defaultdict(list)
    seen_email_rows: set[tuple[str, str, str]] = set()
    for row in email_rows:
        domain = (row.get("root_domain") or "").strip()
        email = (row.get("email") or "").strip()
        source_url = (row.get("scraped_url") or "").strip()
        key = (domain, email, source_url)
        if domain and email and key not in seen_email_rows:
            seen_email_rows.add(key)
            email_by_domain[domain].append(row)

    output: list[dict[str, str]] = []
    for domain in sorted(harvest_by_domain):
        domain_harvest = harvest_by_domain[domain]
        domain_emails = email_by_domain.get(domain, [])
        primary = best_harvest_row(domain_harvest)
        best_email = best_email_row(domain_emails)
        site_name = site_name_from_row(primary)
        combined_text = " ".join(
            uniq(
                [domain]
                + [row.get("title", "") for row in domain_harvest]
                + [row.get("matched_phrase", "") for row in domain_harvest]
                + [row.get("query", "") for row in domain_harvest]
                + [row.get("result_url", "") for row in domain_harvest]
            )
        )
        match = classify_opportunity(combined_text, primary.get("result_url", ""))
        matched_phrase = primary.get("matched_phrase") or match.matched_phrase
        niche = classify_niche(combined_text)
        reject_reason = spam_reject_reason(combined_text, domain)
        if not best_email and not reject_reason:
            reject_reason = "no_usable_business_email"
        best_email_type = best_email.get("email_type", "")
        has_clear_opportunity_page = any(
            row.get("matched_phrase")
            or any(token in row.get("result_url", "").lower() for token in ["write-for-us", "guest", "contribut", "advertis", "media-kit", "sponsor", "editorial"])
            for row in domain_harvest
        )
        tier = quality_tier(
            opportunity_type=match.opportunity_type,
            best_email_type=best_email_type,
            has_clear_opportunity_page=has_clear_opportunity_page,
            reject_reason=reject_reason,
        )
        direct_email = any(row.get("email_type") in DIRECT_EMAIL_TYPES for row in domain_emails)
        generic_email = any(row.get("email_type") in GENERIC_EMAIL_TYPES for row in domain_emails)
        unique_emails = uniq([row.get("email", "") for row in domain_emails])

        output.append(
            {
                "root_domain": domain,
                "site_name": site_name,
                "source_url": primary.get("result_url", ""),
                "matched_phrase": matched_phrase,
                "opportunity_type": match.opportunity_type,
                "niche": niche,
                "email": best_email.get("email", ""),
                "email_type": best_email_type,
                "email_confidence": best_email.get("confidence", ""),
                "email_source_url": best_email.get("scraped_url", ""),
                "email_source_page_type": best_email.get("source_page_type", ""),
                "quality_tier": tier,
                "why_relevant": why_relevant(site_name, niche, match.opportunity_type),
                "reject_reason": reject_reason,
                "query": primary.get("query", ""),
                "query_family": primary.get("query_family", ""),
                "operator_type": primary.get("operator_type", ""),
                "query_offset": primary.get("query_offset", ""),
                "title": primary.get("title", ""),
                "source_engine": primary.get("source_engine", ""),
                "harvested_at": primary.get("harvested_at", ""),
                "result_url_count": str(len(uniq([row.get("result_url", "") for row in domain_harvest]))),
                "all_result_urls": join_values([row.get("result_url", "") for row in domain_harvest]),
                "all_titles": join_values([row.get("title", "") for row in domain_harvest]),
                "all_queries": join_values([row.get("query", "") for row in domain_harvest]),
                "all_matched_phrases": join_values([row.get("matched_phrase", "") for row in domain_harvest]),
                "all_operator_types": join_values([row.get("operator_type", "") for row in domain_harvest]),
                "email_count": str(len(unique_emails)),
                "all_emails": " | ".join(unique_emails),
                "all_email_types": join_values([row.get("email_type", "") for row in domain_emails]),
                "all_email_source_urls": join_values([row.get("scraped_url", "") for row in domain_emails]),
                "all_email_source_page_types": join_values([row.get("source_page_type", "") for row in domain_emails]),
                "all_email_confidences": join_values([row.get("confidence", "") for row in domain_emails]),
                "has_clear_opportunity_page": "yes" if has_clear_opportunity_page else "no",
                "has_direct_email": "yes" if direct_email else "no",
                "has_generic_email": "yes" if generic_email else "no",
                "harvest_row_count": str(len(domain_harvest)),
                "email_row_count": str(len(domain_emails)),
            }
        )
    return output


def write_rows(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=DETAIL_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Export a detailed domain-level review CSV from Apify harvest and email CSVs.")
    parser.add_argument("--harvest-csv", type=Path, required=True)
    parser.add_argument("--emails-csv", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--also-write-review-csv", action="store_true")
    args = parser.parse_args()

    rows = build_detailed_rows(read_csv_rows(args.harvest_csv), read_csv_rows(args.emails_csv))
    write_rows(args.output, rows)
    if args.also_write_review_csv:
        write_rows(REVIEW_CSV, rows)
    stats = {
        "output": str(args.output),
        "review_csv": str(REVIEW_CSV) if args.also_write_review_csv else "",
        "domains": len(rows),
        "domains_with_email": sum(1 for row in rows if row["email"]),
        "a_rows": sum(1 for row in rows if row["quality_tier"] == "A"),
        "b_rows": sum(1 for row in rows if row["quality_tier"] == "B"),
        "c_rows": sum(1 for row in rows if row["quality_tier"] == "C"),
        "reject_rows": sum(1 for row in rows if row["quality_tier"] == "Reject"),
    }
    print(json.dumps(stats, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
