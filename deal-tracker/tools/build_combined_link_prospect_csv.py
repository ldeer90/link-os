#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from deal_tracker.db import connect, init_db  # noqa: E402
from deal_tracker.instantly import DEFAULT_CAMPAIGN_IDS, list_campaign_leads  # noqa: E402
from tools.import_csv_prospects_to_instantly import clean_domain  # noqa: E402
from tools.import_seranking_link_candidates_to_instantly import lead_domains  # noqa: E402


DEFAULT_OUTPUT = ROOT / "generated" / "imports" / "combined_filtered_link_prospects_review.csv"
DEFAULT_EXCLUDED = ROOT / "generated" / "imports" / "combined_filtered_link_prospects_excluded.csv"

DIRECTORY_DOMAINS = {
    "yellowpages.com.au",
    "hotfrog.com.au",
    "truelocal.com.au",
    "whitepages.com.au",
    "startlocal.com.au",
    "localbusinessguide.com.au",
    "pinkpages.com.au",
    "dlook.com.au",
    "cylex-australia.com",
    "businesslistings.net.au",
    "australianplanet.com",
    "atozpages.com.au",
    "local.com.au",
}

PLATFORM_DOMAINS = {
    "eventbrite.com.au",
    "google.com.au",
    "facebook.com",
    "linkedin.com",
    "instagram.com",
    "youtube.com",
    "wikipedia.org",
    "x.com",
    "twitter.com",
}

CORPORATE_KEYWORDS = [
    "airport",
    "airline",
    "bank",
    "finance",
    "insurance",
    "superannuation",
    "telstra",
    "optus",
    "vodafone",
    "energy",
    "water",
    "transport",
    "transit",
    "council",
    "university",
    "tafe",
    "school",
    "hospital",
    "health",
    "police",
    "library",
    "museum",
]

CORPORATE_DOMAINS = {
    "anz.com.au",
    "nab.com.au",
    "westpac.com.au",
    "commbank.com.au",
    "sydneyairport.com.au",
    "adelaideairport.com.au",
    "melbourneairport.com.au",
    "qantas.com.au",
    "jetstar.com.au",
    "virginaustralia.com",
    "bunnings.com.au",
}

PUBLISHER_HINTS = [
    "news",
    "times",
    "daily",
    "magazine",
    "media",
    "review",
    "reviews",
    "blog",
    "guide",
    "guides",
    "mums",
    "mum",
    "family",
    "travel",
    "traveller",
    "weekender",
    "agenda",
    "business",
    "marketing",
    "startup",
    "smart",
    "choice",
    "finder",
    "coupon",
    "deals",
    "advertiser",
    "chronicle",
    "post",
    "leader",
    "mail",
    "courier",
]

KEEP_LINK_TYPES = {
    "media / editorial",
    "blog / guide",
    "reviews / comparison",
    "deals / affiliate",
    "general citation",
}

REJECT_LINK_TYPE_BITS = ["directory", "pdf citation", "policy"]


@dataclass
class Prospect:
    root_domain: str
    link_types: set[str] = field(default_factory=set)
    brands: set[str] = field(default_factory=set)
    source_urls: list[str] = field(default_factory=list)
    source_titles: set[str] = field(default_factory=set)
    target_urls: set[str] = field(default_factory=set)
    anchors: set[str] = field(default_factory=set)
    source_files: set[str] = field(default_factory=set)
    max_domain_inlink_rank: float = 0
    max_inlink_rank: float = 0
    target_count: int = 0
    dofollow_count: int = 0
    row_count: int = 0


def to_float(value: str) -> float:
    try:
        return float(str(value or "").replace(",", ""))
    except ValueError:
        return 0


def to_int(value: str) -> int:
    try:
        return int(float(str(value or "0").replace(",", "")))
    except ValueError:
        return 0


def split_values(value: str) -> list[str]:
    return [part.strip() for part in re.split(r";|\||,", value or "") if part.strip()]


def add_url(urls: list[str], value: str, limit: int = 8) -> None:
    value = (value or "").strip()
    if value and value not in urls and len(urls) < limit:
        urls.append(value)


def read_source_csvs(paths: list[Path]) -> dict[str, Prospect]:
    prospects: dict[str, Prospect] = {}
    for path in paths:
        if not path.exists():
            print(f"Missing file: {path}")
            continue
        with path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                domain = clean_domain(row.get("source_root_domain") or row.get("source_host") or row.get("root_domain") or "")
                if not domain or "." not in domain:
                    continue
                prospect = prospects.setdefault(domain, Prospect(root_domain=domain))
                prospect.row_count += 1
                prospect.source_files.add(path.name)
                link_type = (row.get("link_type") or row.get("best_type") or "").strip()
                if link_type:
                    prospect.link_types.add(link_type)
                for brand in split_values(row.get("linked_target_brands") or row.get("brands") or row.get("brand") or ""):
                    prospect.brands.add(brand)
                add_url(prospect.source_urls, row.get("source_url") or row.get("example_source_url") or "")
                for url in split_values(row.get("example_target_urls") or row.get("example_target_url") or row.get("target_url") or ""):
                    prospect.target_urls.add(url)
                for anchor in split_values(row.get("example_anchors") or row.get("example_anchor") or row.get("anchor") or ""):
                    prospect.anchors.add(anchor[:180])
                title = (row.get("source_title") or "").strip()
                if title:
                    prospect.source_titles.add(title[:250])
                prospect.max_domain_inlink_rank = max(prospect.max_domain_inlink_rank, to_float(row.get("best_domain_inlink_rank") or ""))
                prospect.max_inlink_rank = max(prospect.max_inlink_rank, to_float(row.get("best_inlink_rank") or row.get("inlink_rank") or ""))
                prospect.target_count += to_int(row.get("target_count") or row.get("targets_linked") or "")
                prospect.dofollow_count += to_int(row.get("dofollow_count") or row.get("dofollow_samples") or "")
    return prospects


def contacted_domains(campaign_ids: list[str]) -> set[str]:
    domains: set[str] = set()
    for campaign_id in campaign_ids:
        try:
            leads = list_campaign_leads(campaign_id)
        except Exception as exc:  # noqa: BLE001
            print(f"Could not load campaign {campaign_id}: {exc}")
            continue
        for lead in leads.values():
            domains.update(lead_domains(lead))
    init_db()
    with connect() as connection:
        for row in connection.execute("select root_domain, publisher_url, contact_email from link_deals").fetchall():
            for value in [row["root_domain"], row["publisher_url"], row["contact_email"]]:
                domain = clean_domain(value or "")
                if domain:
                    domains.add(domain)
    return domains


def domain_tld(domain: str) -> str:
    parts = domain.split(".")
    if len(parts) >= 3 and parts[-1] == "au":
        return ".".join(parts[-2:])
    return parts[-1] if len(parts) > 1 else ""


def is_publisherish(prospect: Prospect) -> bool:
    haystack = " ".join(
        [
            prospect.root_domain,
            " ".join(prospect.link_types),
            " ".join(prospect.source_titles),
            " ".join(prospect.source_urls),
        ]
    ).lower()
    return any(hint in haystack for hint in PUBLISHER_HINTS)


def reject_reason(prospect: Prospect, already_contacting: set[str]) -> str:
    domain = prospect.root_domain
    lowered_domain = domain.lower()
    link_types = " ; ".join(sorted(prospect.link_types)).lower()
    haystack = " ".join(
        [
            lowered_domain,
            link_types,
            " ".join(prospect.source_titles).lower(),
            " ".join(prospect.source_urls).lower(),
        ]
    )

    if domain in already_contacting:
        return "already_in_instantly_or_deal_tracker"
    if lowered_domain.endswith((".gov.au", ".gov", ".edu.au", ".edu")) or ".gov." in lowered_domain or ".edu." in lowered_domain:
        return "government_or_education_domain"
    if domain in DIRECTORY_DOMAINS or any(bit in link_types for bit in REJECT_LINK_TYPE_BITS):
        return "directory_policy_or_pdf_source"
    if domain in PLATFORM_DOMAINS:
        return "platform_not_link_outreach_target"
    if domain in CORPORATE_DOMAINS:
        return "large_corporate_not_paid_guest_post_target"
    if any(keyword in lowered_domain for keyword in ["airport", "airlines", "qantas", "jetstar", "bank", "university", "council"]):
        return "clear_corporate_public_sector_or_travel_operator"
    if "partner / directory" in link_types:
        return "partner_directory_not_guest_post_target"
    if not prospect.link_types.intersection(KEEP_LINK_TYPES) and not is_publisherish(prospect):
        return "not_publisher_or_paid_placement_like"
    if any(keyword in haystack for keyword in ["terms and conditions", "privacy policy", "airline directory", "member benefits"]) and not is_publisherish(prospect):
        return "administrative_or_policy_page"
    if any(keyword in lowered_domain for keyword in CORPORATE_KEYWORDS) and not is_publisherish(prospect):
        return "corporate_or_institutional_site"
    return ""


def likely_acceptance(prospect: Prospect) -> tuple[int, str]:
    score = 30
    reasons: list[str] = []
    link_types = " ".join(prospect.link_types).lower()
    haystack = " ".join([prospect.root_domain, link_types, " ".join(prospect.source_titles), " ".join(prospect.source_urls)]).lower()
    if "media / editorial" in link_types:
        score += 25
        reasons.append("media/editorial source")
    if "blog / guide" in link_types:
        score += 20
        reasons.append("blog/guide source")
    if "reviews / comparison" in link_types:
        score += 18
        reasons.append("review/comparison source")
    if "deals / affiliate" in link_types:
        score += 12
        reasons.append("commercial deals/affiliate source")
    if is_publisherish(prospect):
        score += 15
        reasons.append("publisher-like domain/title")
    if prospect.dofollow_count:
        score += min(10, prospect.dofollow_count)
        reasons.append("has dofollow examples")
    if any(term in haystack for term in ["advertise", "sponsored", "guest", "contribute", "media kit"]):
        score += 20
        reasons.append("commercial/editorial language")
    return min(score, 100), "; ".join(reasons) or "potential link prospect"


def row_for(prospect: Prospect, status: str = "kept", reason: str = "") -> dict[str, Any]:
    score, score_reason = likely_acceptance(prospect)
    return {
        "root_domain": prospect.root_domain,
        "status": status,
        "exclude_reason": reason,
        "paid_link_likelihood_score": score,
        "paid_link_likelihood_reason": score_reason,
        "tld": domain_tld(prospect.root_domain),
        "link_types": "; ".join(sorted(prospect.link_types)),
        "brands": "; ".join(sorted(prospect.brands))[:800],
        "best_domain_inlink_rank": f"{prospect.max_domain_inlink_rank:g}",
        "best_inlink_rank": f"{prospect.max_inlink_rank:g}",
        "target_count": prospect.target_count,
        "dofollow_count": prospect.dofollow_count,
        "row_count": prospect.row_count,
        "example_source_urls": " | ".join(prospect.source_urls),
        "source_titles": " | ".join(sorted(prospect.source_titles))[:1000],
        "example_target_urls": " | ".join(sorted(prospect.target_urls))[:1000],
        "example_anchors": " | ".join(sorted(prospect.anchors))[:1000],
        "source_files": "; ".join(sorted(prospect.source_files)),
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "root_domain",
        "status",
        "exclude_reason",
        "paid_link_likelihood_score",
        "paid_link_likelihood_reason",
        "tld",
        "link_types",
        "brands",
        "best_domain_inlink_rank",
        "best_inlink_rank",
        "target_count",
        "dofollow_count",
        "row_count",
        "example_source_urls",
        "source_titles",
        "example_target_urls",
        "example_anchors",
        "source_files",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Combine, dedupe, and filter link prospect exports.")
    parser.add_argument("csv_paths", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--excluded-output", type=Path, default=DEFAULT_EXCLUDED)
    parser.add_argument("--no-live-dedupe", action="store_true")
    args = parser.parse_args()

    prospects = read_source_csvs(args.csv_paths)
    already = set() if args.no_live_dedupe else contacted_domains(DEFAULT_CAMPAIGN_IDS)
    kept: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for prospect in prospects.values():
        reason = reject_reason(prospect, already)
        if reason:
            excluded.append(row_for(prospect, "excluded", reason))
        else:
            kept.append(row_for(prospect, "kept", ""))

    kept.sort(key=lambda row: (float(row["paid_link_likelihood_score"]), float(row["best_domain_inlink_rank"]), int(row["dofollow_count"] or 0)), reverse=True)
    excluded.sort(key=lambda row: (row["exclude_reason"], row["root_domain"]))
    write_csv(args.output, kept)
    write_csv(args.excluded_output, excluded)
    print(f"Input files: {len(args.csv_paths)}")
    print(f"Unique domains: {len(prospects)}")
    print(f"Kept: {len(kept)}")
    print(f"Excluded: {len(excluded)}")
    print(f"Output: {args.output}")
    print(f"Excluded output: {args.excluded_output}")
    reason_counts: dict[str, int] = {}
    for row in excluded:
        reason_counts[row["exclude_reason"]] = reason_counts.get(row["exclude_reason"], 0) + 1
    print(reason_counts)


if __name__ == "__main__":
    main()
