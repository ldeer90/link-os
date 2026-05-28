#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import html
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from deal_tracker.instantly import ACTIVE_CAMPAIGN_ID, list_campaign_leads, request  # noqa: E402
from tools.import_csv_prospects_to_instantly import (  # noqa: E402
    CONTACT_PATHS,
    ROLE_PRIORITY,
    clean_domain,
    decode_cloudflare_email,
    email_type,
    extract_emails,
    rank_email,
)


DEFAULT_OUTPUT = ROOT / "generated" / "imports" / "seranking_link_candidates_import_results.csv"
USER_AGENT = "Mozilla/5.0 (compatible; LDSearchSERankingContactBot/1.0; +https://ldsearch.com.au)"
FETCH_TIMEOUT = 3.5
MAX_HTML_BYTES = 700_000

EXCLUDED_SOURCE_DOMAINS = {
    "au.com.au",
    "cartridgesdirect.com.au",
    "quickfitblindsandcurtains.com.au",
    "quickfit.com.au",
    "google.com.au",
    "eventbrite.com.au",
    "yellowpages.com.au",
    "hotfrog.com.au",
    "bunnings.com.au",
    "anz.com.au",
    "nab.com.au",
    "westpac.com.au",
}

LOW_VALUE_TYPES = {
    "directory",
    "coupon",
    "deals / affiliate",
}

BAD_EMAIL_TOKENS = {
    "noreply",
    "no-reply",
    "no_reply",
    "donotreply",
    "do-not-reply",
    "sentry.io",
    "ingest.sentry",
    "example.",
    "postmaster@",
    "webmaster@",
    "privacy@",
    "abuse@",
}

OFFDOMAIN_ROLE_LOCALS = {
    "advertising",
    "advertise",
    "ads",
    "media",
    "partnerships",
    "partners",
    "sponsored",
    "sponsorship",
    "editor",
    "editorial",
    "content",
    "submissions",
    "sales",
    "hello",
    "info",
    "contact",
}


@dataclass
class Candidate:
    root_domain: str
    brands: set[str] = field(default_factory=set)
    best_types: set[str] = field(default_factory=set)
    example_source_urls: list[str] = field(default_factory=list)
    example_target_urls: list[str] = field(default_factory=list)
    anchors: set[str] = field(default_factory=set)
    source_files: set[str] = field(default_factory=set)
    best_domain_inlink_rank: float = 0
    best_inlink_rank: float = 0
    row_count: int = 0

    @property
    def site_name(self) -> str:
        label = self.root_domain.split(".")[0].replace("-", " ").replace("_", " ")
        return " ".join(part.capitalize() for part in label.split()) or self.root_domain


@dataclass
class EmailCandidate:
    email: str
    source_url: str
    method: str
    confidence: int
    email_type: str


def to_float(value: str) -> float:
    try:
        return float(str(value or "").replace(",", ""))
    except ValueError:
        return 0


def read_candidates(paths: list[Path]) -> dict[str, Candidate]:
    candidates: dict[str, Candidate] = {}
    for path in paths:
        if not path.exists():
            print(f"Missing file: {path}")
            continue
        with path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                domain = clean_domain(row.get("source_root_domain") or row.get("source_host") or "")
                if not domain or "." not in domain or domain in EXCLUDED_SOURCE_DOMAINS:
                    continue
                candidate = candidates.setdefault(domain, Candidate(root_domain=domain))
                candidate.row_count += 1
                candidate.source_files.add(path.name)
                for brand in re.split(r";|,", row.get("brands") or row.get("brand") or ""):
                    brand = brand.strip()
                    if brand:
                        candidate.brands.add(brand)
                best_type = (row.get("best_type") or "").strip()
                if best_type:
                    candidate.best_types.add(best_type)
                for field_name, target_list in [
                    ("example_source_url", candidate.example_source_urls),
                    ("source_url", candidate.example_source_urls),
                    ("example_target_url", candidate.example_target_urls),
                    ("target_url", candidate.example_target_urls),
                ]:
                    value = (row.get(field_name) or "").strip()
                    if value and value not in target_list:
                        target_list.append(value)
                anchor = (row.get("example_anchor") or row.get("anchor") or "").strip()
                if anchor:
                    candidate.anchors.add(anchor[:160])
                candidate.best_domain_inlink_rank = max(candidate.best_domain_inlink_rank, to_float(row.get("best_domain_inlink_rank") or ""))
                candidate.best_inlink_rank = max(candidate.best_inlink_rank, to_float(row.get("best_inlink_rank") or row.get("inlink_rank") or ""))
    return candidates


def lead_domains(lead: dict[str, Any]) -> set[str]:
    payload = lead.get("payload") if isinstance(lead.get("payload"), dict) else {}
    domains = set()
    for value in [lead.get("company_domain"), lead.get("website"), lead.get("email"), payload.get("root_domain"), payload.get("website")]:
        text = str(value or "")
        if "@" in text and not text.startswith("http"):
            text = text.split("@")[-1]
        domain = clean_domain(text)
        if domain and "." in domain:
            domains.add(domain)
    return domains


def fetch_url(url: str) -> tuple[str, str]:
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    with urlopen(req, timeout=FETCH_TIMEOUT) as response:
        content_type = response.headers.get("content-type", "")
        if "text/html" not in content_type and "text/plain" not in content_type:
            return response.geturl(), ""
        raw = response.read(MAX_HTML_BYTES)
        charset = response.headers.get_content_charset() or "utf-8"
        return response.geturl(), raw.decode(charset, errors="replace")


def seed_urls(candidate: Candidate) -> list[str]:
    urls = [f"https://{candidate.root_domain}"]
    urls.extend(candidate.example_source_urls[:2])
    urls.extend(urljoin(f"https://{candidate.root_domain}", path) for path in CONTACT_PATHS)
    urls.extend(urljoin(f"https://{candidate.root_domain}", path) for path in ["/contact-us", "/advertising", "/about", "/about-us", "/editorial", "/submissions"])
    seen = set()
    output = []
    for url in urls:
        parsed = urlparse(url)
        if not parsed.netloc or clean_domain(parsed.netloc) != candidate.root_domain:
            continue
        key = url.split("#", 1)[0]
        if key not in seen:
            seen.add(key)
            output.append(key)
    return output[:14]


def discover_emails(candidate: Candidate) -> list[EmailCandidate]:
    found: dict[str, EmailCandidate] = {}
    for url in seed_urls(candidate):
        try:
            final_url, body = fetch_url(url)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError):
            continue
        if not body:
            continue
        for email in extract_emails(body):
            if not acceptable_email(candidate, email, final_url):
                continue
            score = rank_email(email, candidate.root_domain, final_url)
            local = email.split("@", 1)[0].lower()
            if any(role in local for role in ROLE_PRIORITY[:8]):
                score += 10
            item = EmailCandidate(
                email=email,
                source_url=final_url,
                method="seranking_site_scrape",
                confidence=min(score, 100),
                email_type=email_type(email),
            )
            current = found.get(email)
            if not current or item.confidence > current.confidence:
                found[email] = item
        if any(item.confidence >= 92 for item in found.values()):
            break
        time.sleep(0.05)
    return sorted(found.values(), key=lambda item: item.confidence, reverse=True)


def acceptable_email(candidate: Candidate, email: str, source_url: str) -> bool:
    lowered = email.lower()
    if any(token in lowered for token in BAD_EMAIL_TOKENS):
        return False
    if "@" not in lowered:
        return False
    local, domain = lowered.split("@", 1)
    domain = clean_domain(domain)
    root = candidate.root_domain
    if domain == root or domain.endswith("." + root) or domain in root or root in domain:
        return True
    if not any(role in local for role in OFFDOMAIN_ROLE_LOCALS):
        return False
    source_hint = source_url.lower()
    return any(hint in source_hint for hint in ["contact", "advertis", "media", "about", "sponsor", "partner"])


def best_candidate(candidates: list[EmailCandidate]) -> EmailCandidate | None:
    if not candidates:
        return None
    return candidates[0]


def opportunity_type(candidate: Candidate) -> str:
    types = " ".join(candidate.best_types).lower()
    if "media" in types or "editorial" in types:
        return "editorial_placement"
    if "affiliate" in types or "deals" in types:
        return "deals_affiliate"
    if "citation" in types:
        return "link_building_citation"
    return "link_building_outreach"


def build_lead(candidate: Candidate, email: EmailCandidate) -> dict[str, Any]:
    best_type = "; ".join(sorted(candidate.best_types))[:200]
    brands = "; ".join(sorted(candidate.brands))[:400]
    source_url = candidate.example_source_urls[0] if candidate.example_source_urls else f"https://{candidate.root_domain}"
    return {
        "email": email.email,
        "first_name": "there",
        "company_name": candidate.site_name,
        "website": f"https://{candidate.root_domain}",
        "personalization": (
            f"I found {candidate.site_name} while reviewing backlink opportunities from SE Ranking; "
            "the site appears to have linked to comparable Australian brands or publishers."
        ),
        "custom_variables": {
            "root_domain": candidate.root_domain,
            "site_name": candidate.site_name,
            "opportunity_type": opportunity_type(candidate),
            "source_url": source_url,
            "niche": "link building prospect",
            "contact_email_type": email.email_type,
            "why_relevant": "SE Ranking backlink export shows this domain has linked to comparable Australian sites or brands",
            "contact_source": email.source_url,
            "contact_source_method": email.method,
            "seranking_best_type": best_type,
            "seranking_brands": brands,
            "seranking_source_files": "; ".join(sorted(candidate.source_files)),
            "seranking_row_count": str(candidate.row_count),
            "seranking_domain_inlink_rank": f"{candidate.best_domain_inlink_rank:g}",
            "seranking_inlink_rank": f"{candidate.best_inlink_rank:g}",
        },
    }


def add_leads(campaign_id: str, leads: list[dict[str, Any]]) -> dict[str, Any]:
    if not leads:
        return {}
    return request(
        "/leads/add",
        method="POST",
        body={
            "campaign_id": campaign_id,
            "leads": leads,
            "verify_leads_on_import": False,
            "skip_if_in_workspace": True,
            "skip_if_in_campaign": True,
        },
    )


def write_results(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "root_domain",
        "status",
        "email",
        "email_type",
        "email_source_url",
        "confidence",
        "campaign_id",
        "lead_id",
        "best_type",
        "brands",
        "example_source_url",
        "source_files",
        "notes",
        "all_candidates",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Import SE Ranking link candidate contacts into Instantly.")
    parser.add_argument("csv_paths", nargs="+", type=Path)
    parser.add_argument("--campaign-id", default=ACTIVE_CAMPAIGN_ID)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--workers", type=int, default=32)
    parser.add_argument("--max-domains", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    candidates = read_candidates(args.csv_paths)
    ordered = sorted(
        candidates.values(),
        key=lambda item: (item.best_domain_inlink_rank, item.best_inlink_rank, item.row_count),
        reverse=True,
    )
    if args.max_domains:
        ordered = ordered[: args.max_domains]

    existing_leads = list_campaign_leads(args.campaign_id)
    existing_domains = set()
    for lead in existing_leads.values():
        existing_domains.update(lead_domains(lead))

    to_scrape = [candidate for candidate in ordered if candidate.root_domain not in existing_domains]
    rows: list[dict[str, Any]] = []
    for candidate in ordered:
        if candidate.root_domain in existing_domains:
            rows.append(
                {
                    "root_domain": candidate.root_domain,
                    "status": "already_in_campaign",
                    "campaign_id": args.campaign_id,
                    "best_type": "; ".join(sorted(candidate.best_types)),
                    "brands": "; ".join(sorted(candidate.brands)),
                    "example_source_url": candidate.example_source_urls[0] if candidate.example_source_urls else "",
                    "source_files": "; ".join(sorted(candidate.source_files)),
                    "notes": "Skipped by root domain dedupe.",
                    "all_candidates": "",
                }
            )

    discoveries: dict[str, list[EmailCandidate]] = {}
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        future_map = {executor.submit(discover_emails, candidate): candidate for candidate in to_scrape}
        for future in as_completed(future_map):
            candidate = future_map[future]
            try:
                discoveries[candidate.root_domain] = future.result()
            except Exception as exc:  # noqa: BLE001
                discoveries[candidate.root_domain] = []
                rows.append(
                    {
                        "root_domain": candidate.root_domain,
                        "status": "scrape_error",
                        "best_type": "; ".join(sorted(candidate.best_types)),
                        "brands": "; ".join(sorted(candidate.brands)),
                        "example_source_url": candidate.example_source_urls[0] if candidate.example_source_urls else "",
                        "source_files": "; ".join(sorted(candidate.source_files)),
                        "notes": str(exc)[:500],
                        "all_candidates": "",
                    }
                )

    leads: list[dict[str, Any]] = []
    selected_by_email: dict[str, tuple[Candidate, EmailCandidate, list[EmailCandidate]]] = {}
    for candidate in to_scrape:
        email_candidates = discoveries.get(candidate.root_domain, [])
        selected = best_candidate(email_candidates)
        all_candidates = "; ".join(f"{item.email} ({item.email_type}, {item.confidence}, {item.source_url})" for item in email_candidates[:8])
        base = {
            "root_domain": candidate.root_domain,
            "best_type": "; ".join(sorted(candidate.best_types)),
            "brands": "; ".join(sorted(candidate.brands)),
            "example_source_url": candidate.example_source_urls[0] if candidate.example_source_urls else "",
            "source_files": "; ".join(sorted(candidate.source_files)),
            "all_candidates": all_candidates,
        }
        if not selected:
            rows.append({**base, "status": "no_email_found", "notes": "No public email found from quick scrape."})
            continue
        leads.append(build_lead(candidate, selected))
        selected_by_email[selected.email.lower()] = (candidate, selected, email_candidates)
        if args.dry_run:
            rows.append(
                {
                    **base,
                    "status": "dry_run_would_add",
                    "email": selected.email,
                    "email_type": selected.email_type,
                    "email_source_url": selected.source_url,
                    "confidence": selected.confidence,
                    "campaign_id": args.campaign_id,
                }
            )

    response: dict[str, Any] = {}
    if leads and not args.dry_run:
        response = add_leads(args.campaign_id, leads)
        created = response.get("created_leads") if isinstance(response.get("created_leads"), list) else []
        ids = {str(item.get("email") or "").lower(): str(item.get("id") or "") for item in created if isinstance(item, dict)}
        for lead in leads:
            email = str(lead["email"]).lower()
            candidate, selected, email_candidates = selected_by_email[email]
            rows.append(
                {
                    "root_domain": candidate.root_domain,
                    "status": "added_to_campaign",
                    "email": selected.email,
                    "email_type": selected.email_type,
                    "email_source_url": selected.source_url,
                    "confidence": selected.confidence,
                    "campaign_id": args.campaign_id,
                    "lead_id": ids.get(email, ""),
                    "best_type": "; ".join(sorted(candidate.best_types)),
                    "brands": "; ".join(sorted(candidate.brands)),
                    "example_source_url": candidate.example_source_urls[0] if candidate.example_source_urls else "",
                    "source_files": "; ".join(sorted(candidate.source_files)),
                    "notes": "Added via /leads/add.",
                    "all_candidates": "; ".join(f"{item.email} ({item.email_type}, {item.confidence}, {item.source_url})" for item in email_candidates[:8]),
                }
            )

    status_rank = {"added_to_campaign": 0, "dry_run_would_add": 0, "already_in_campaign": 1, "no_email_found": 2, "scrape_error": 3}
    rows.sort(key=lambda row: (status_rank.get(row.get("status", ""), 9), row.get("root_domain", "")))
    write_results(args.output, rows)

    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        counts[row.get("status", "")] += 1
    print(f"Unique candidate domains: {len(candidates)}")
    print(f"Processed domains: {len(ordered)}")
    print(f"Already in campaign: {counts['already_in_campaign']}")
    print(f"Added: {counts['added_to_campaign']}")
    print(f"Would add: {counts['dry_run_would_add']}")
    print(f"No email found: {counts['no_email_found']}")
    print(f"Scrape errors: {counts['scrape_error']}")
    print(f"Output: {args.output}")
    if response:
        print(
            {
                key: response.get(key)
                for key in [
                    "status",
                    "total_sent",
                    "leads_uploaded",
                    "duplicated_leads",
                    "skipped_count",
                    "invalid_email_count",
                    "remaining_in_plan",
                ]
            }
        )


if __name__ == "__main__":
    main()
