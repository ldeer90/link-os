#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import gzip
import html
import re
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from deal_tracker.instantly import ACTIVE_CAMPAIGN_ID, list_campaign_leads, request  # noqa: E402
from tools.import_csv_prospects_to_instantly import clean_domain, email_type, extract_emails, rank_email  # noqa: E402
from tools.import_seranking_link_candidates_to_instantly import acceptable_email, lead_domains, opportunity_type  # noqa: E402


DEFAULT_INPUT = ROOT / "generated" / "imports" / "seranking_link_candidates_import_results.csv"
DEFAULT_OUTPUT = ROOT / "generated" / "imports" / "seranking_enhanced_email_discovery_results.csv"
USER_AGENT = "Mozilla/5.0 (compatible; LDSearchEnhancedContactBot/1.0; +https://ldsearch.com.au)"
FETCH_TIMEOUT = 6
MAX_HTML_BYTES = 900_000
MAX_SITEMAP_BYTES = 2_000_000

URL_HINTS = [
    "contact",
    "advertis",
    "media",
    "sponsor",
    "partnership",
    "partner",
    "about",
    "editor",
    "submission",
    "submit",
    "contribute",
    "write-for-us",
    "guest-post",
    "rate-card",
    "commercial",
    "sales",
]

SEARCH_QUERIES = [
    'site:{domain} email',
    'site:{domain} contact email',
    'site:{domain} advertising email',
    'site:{domain} media email',
    'site:{domain} "advertise with us"',
    '"{domain}" email',
    '"{site_name}" "advertising"',
    '"{site_name}" "media kit"',
    '"{site_name}" "contact"',
]


@dataclass
class AuditCandidate:
    root_domain: str
    site_name: str
    best_type: str
    brands: str
    example_source_url: str
    source_files: str


@dataclass
class EmailHit:
    email: str
    source_url: str
    method: str
    confidence: int
    email_type: str


class CandidateAdapter:
    def __init__(self, audit: AuditCandidate):
        self.root_domain = audit.root_domain
        self.best_types = {audit.best_type} if audit.best_type else set()


def fetch_url(url: str, max_bytes: int = MAX_HTML_BYTES) -> tuple[str, str]:
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,text/plain,application/xml"})
    with urlopen(req, timeout=FETCH_TIMEOUT) as response:
        raw = response.read(max_bytes)
        charset = response.headers.get_content_charset() or "utf-8"
        if urlparse(response.geturl()).path.endswith(".gz"):
            raw = gzip.decompress(raw)
        return response.geturl(), raw.decode(charset, errors="replace")


def read_unresolved(path: Path, max_domains: int = 0) -> list[AuditCandidate]:
    rows: list[AuditCandidate] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row.get("status") != "no_email_found":
                continue
            domain = clean_domain(row.get("root_domain") or "")
            if not domain:
                continue
            site_name = " ".join(part.capitalize() for part in domain.split(".")[0].replace("-", " ").split())
            rows.append(
                AuditCandidate(
                    root_domain=domain,
                    site_name=site_name or domain,
                    best_type=row.get("best_type") or "",
                    brands=row.get("brands") or "",
                    example_source_url=row.get("example_source_url") or "",
                    source_files=row.get("source_files") or "",
                )
            )
    if max_domains:
        rows = rows[:max_domains]
    return rows


def extract_links(body: str, base_url: str, root_domain: str, hinted_only: bool = True, limit: int = 80) -> list[str]:
    links: list[str] = []
    seen: set[str] = set()
    for href in re.findall(r"""href=["']([^"']+)["']""", body, flags=re.I):
        href = html.unescape(href)
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        absolute = urljoin(base_url, href).split("#", 1)[0]
        parsed = urlparse(absolute)
        if parsed.scheme not in {"http", "https"}:
            continue
        if clean_domain(parsed.netloc) != root_domain:
            continue
        lowered = absolute.lower()
        if hinted_only and not any(hint in lowered for hint in URL_HINTS):
            continue
        if absolute not in seen:
            links.append(absolute)
            seen.add(absolute)
        if len(links) >= limit:
            break
    return links


def robots_sitemaps(root_domain: str) -> list[str]:
    urls = []
    for scheme in ["https", "http"]:
        try:
            final_url, body = fetch_url(f"{scheme}://{root_domain}/robots.txt", max_bytes=200_000)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError):
            continue
        for line in body.splitlines():
            if line.lower().startswith("sitemap:"):
                sitemap = line.split(":", 1)[1].strip()
                if sitemap:
                    urls.append(sitemap)
        break
    urls.extend([f"https://{root_domain}/sitemap.xml", f"https://{root_domain}/sitemap_index.xml"])
    return list(dict.fromkeys(urls))


def sitemap_urls(root_domain: str) -> list[str]:
    results: list[str] = []
    queue = robots_sitemaps(root_domain)[:6]
    seen: set[str] = set()
    while queue and len(results) < 80:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            _, body = fetch_url(url, max_bytes=MAX_SITEMAP_BYTES)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, gzip.BadGzipFile):
            continue
        try:
            root = ET.fromstring(body.strip().encode("utf-8"))
            locs = [el.text.strip() for el in root.iter() if el.tag.endswith("loc") and el.text]
        except ET.ParseError:
            locs = re.findall(r"<loc>(.*?)</loc>", body, flags=re.I | re.S)
        for loc in locs:
            loc = html.unescape(loc.strip())
            if clean_domain(urlparse(loc).netloc) != root_domain:
                continue
            if loc.endswith((".xml", ".xml.gz")) and len(queue) < 20:
                queue.append(loc)
                continue
            if any(hint in loc.lower() for hint in URL_HINTS):
                results.append(loc)
                if len(results) >= 80:
                    break
    return list(dict.fromkeys(results))


def search_urls(query: str) -> list[str]:
    urls: list[str] = []
    engines = [
        f"https://www.bing.com/search?q={quote_plus(query)}",
        f"https://html.duckduckgo.com/html/?q={quote_plus(query)}",
    ]
    for search_url in engines:
        try:
            _, body = fetch_url(search_url, max_bytes=700_000)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError):
            continue
        urls.extend(extract_search_result_urls(body))
        # Emails in snippets sometimes save a page fetch.
        if extract_emails(body):
            urls.append(search_url)
        time.sleep(0.15)
        if urls:
            break
    return list(dict.fromkeys(urls))[:8]


def extract_search_result_urls(body: str) -> list[str]:
    urls: list[str] = []
    for raw in re.findall(r"""href=["']([^"']+)["']""", body, flags=re.I):
        href = html.unescape(raw)
        if href.startswith("/"):
            parsed = urlparse(href)
            qs = parse_qs(parsed.query)
            if "uddg" in qs:
                href = qs["uddg"][0]
            elif "u" in qs:
                href = qs["u"][0]
        href = unquote(href)
        if href.startswith("http") and not any(skip in href for skip in ["bing.com", "duckduckgo.com", "microsoft.com"]):
            urls.append(href.split("#", 1)[0])
    return urls


def add_hit(found: dict[str, EmailHit], audit: AuditCandidate, email: str, source_url: str, method: str, score_bonus: int = 0) -> None:
    adapter = CandidateAdapter(audit)
    if not acceptable_email(adapter, email, source_url):
        return
    score = min(100, rank_email(email, audit.root_domain, source_url) + score_bonus)
    hit = EmailHit(email=email.lower(), source_url=source_url, method=method, confidence=score, email_type=email_type(email))
    current = found.get(hit.email)
    if not current or hit.confidence > current.confidence:
        found[hit.email] = hit


def scan_page(audit: AuditCandidate, url: str, method: str, found: dict[str, EmailHit], expand_links: bool = False) -> list[str]:
    try:
        final_url, body = fetch_url(url)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError):
        return []
    for email in extract_emails(body):
        add_hit(found, audit, email, final_url, method, score_bonus=8 if any(h in final_url.lower() for h in URL_HINTS) else 0)
    if expand_links:
        return extract_links(body, final_url, audit.root_domain, hinted_only=True, limit=45)
    return []


def discover(audit: AuditCandidate, use_search: bool = True) -> list[EmailHit]:
    found: dict[str, EmailHit] = {}
    roots = [f"https://{audit.root_domain}", f"http://{audit.root_domain}"]
    direct_paths = [
        "/contact", "/contact-us", "/about", "/about-us", "/advertise", "/advertise-with-us", "/advertising",
        "/media-kit", "/media", "/sponsorship", "/partnerships", "/editorial", "/submissions", "/write-for-us",
        "/guest-post", "/contribute", "/commercial", "/sales",
    ]
    queue: list[tuple[str, str, bool]] = []
    if audit.example_source_url:
        queue.append((audit.example_source_url, "seranking_example_source_expanded", True))
    for root in roots:
        queue.append((root, "homepage_expanded", True))
    for path in direct_paths:
        queue.append((urljoin(f"https://{audit.root_domain}", path), "known_contact_path", False))
    for url in sitemap_urls(audit.root_domain)[:35]:
        queue.append((url, "sitemap_hint_page", False))

    seen: set[str] = set()
    for url, method, expand in queue[:90]:
        if url in seen:
            continue
        seen.add(url)
        extra_links = scan_page(audit, url, method, found, expand_links=expand)
        for extra in extra_links[:25]:
            if extra not in seen:
                seen.add(extra)
                scan_page(audit, extra, "same_site_hint_link", found, expand_links=False)
        if any(hit.confidence >= 95 for hit in found.values()):
            break
        time.sleep(0.03)

    if use_search and not any(hit.confidence >= 90 for hit in found.values()):
        for template in SEARCH_QUERIES:
            query = template.format(domain=audit.root_domain, site_name=audit.site_name)
            for result_url in search_urls(query):
                if result_url.startswith("https://www.bing.com") or result_url.startswith("https://html.duckduckgo.com"):
                    try:
                        _, search_body = fetch_url(result_url)
                    except Exception:
                        continue
                    for email in extract_emails(search_body):
                        add_hit(found, audit, email, result_url, "search_snippet", score_bonus=4)
                    continue
                parsed = urlparse(result_url)
                result_domain = clean_domain(parsed.netloc)
                if result_domain == audit.root_domain or result_domain.endswith("." + audit.root_domain):
                    scan_page(audit, result_url, "search_same_site_result", found, expand_links=True)
                elif any(host in result_domain for host in ["facebook.com", "linkedin.com", "instagram.com", "linktr.ee", "muckrack.com", "crunchbase.com"]):
                    scan_page(audit, result_url, "search_public_profile_result", found, expand_links=False)
            if any(hit.confidence >= 90 for hit in found.values()):
                break
            time.sleep(0.2)

    return sorted(found.values(), key=lambda hit: hit.confidence, reverse=True)


def build_lead(audit: AuditCandidate, hit: EmailHit) -> dict[str, Any]:
    return {
        "email": hit.email,
        "first_name": "there",
        "company_name": audit.site_name,
        "website": f"https://{audit.root_domain}",
        "personalization": (
            f"I found {audit.site_name} while reviewing SE Ranking backlink opportunities; "
            "the site appears relevant for link building or editorial outreach."
        ),
        "custom_variables": {
            "root_domain": audit.root_domain,
            "site_name": audit.site_name,
            "opportunity_type": "link_building_outreach",
            "source_url": audit.example_source_url or f"https://{audit.root_domain}",
            "niche": "link building prospect",
            "contact_email_type": hit.email_type,
            "why_relevant": "SE Ranking backlink export shows this domain as a potential backlink/link-building prospect",
            "contact_source": hit.source_url,
            "contact_source_method": hit.method,
            "seranking_best_type": audit.best_type,
            "seranking_brands": audit.brands,
            "seranking_source_files": audit.source_files,
            "email_discovery_pass": "enhanced_second_pass",
        },
    }


def write_results(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "root_domain", "status", "email", "email_type", "confidence", "source_url", "method", "lead_id",
        "best_type", "brands", "example_source_url", "notes", "all_candidates",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Enhanced second-pass email discovery for unresolved SE Ranking domains.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--campaign-id", default=ACTIVE_CAMPAIGN_ID)
    parser.add_argument("--workers", type=int, default=20)
    parser.add_argument("--max-domains", type=int, default=0)
    parser.add_argument("--no-search", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    audits = read_unresolved(args.input, max_domains=args.max_domains)
    leads = list_campaign_leads(args.campaign_id)
    existing_domains: set[str] = set()
    for lead in leads.values():
        existing_domains.update(lead_domains(lead))
    audits = [audit for audit in audits if audit.root_domain not in existing_domains]

    discoveries: dict[str, list[EmailHit]] = {}
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        future_map = {executor.submit(discover, audit, not args.no_search): audit for audit in audits}
        for future in as_completed(future_map):
            audit = future_map[future]
            try:
                discoveries[audit.root_domain] = future.result()
            except Exception as exc:  # noqa: BLE001
                discoveries[audit.root_domain] = []
                print(f"error {audit.root_domain}: {exc}")

    rows: list[dict[str, Any]] = []
    leads_to_add: list[dict[str, Any]] = []
    selected: dict[str, tuple[AuditCandidate, EmailHit, list[EmailHit]]] = {}
    for audit in audits:
        hits = discoveries.get(audit.root_domain, [])
        all_hits = "; ".join(f"{hit.email} ({hit.email_type}, {hit.confidence}, {hit.method}, {hit.source_url})" for hit in hits[:8])
        base = {
            "root_domain": audit.root_domain,
            "best_type": audit.best_type,
            "brands": audit.brands,
            "example_source_url": audit.example_source_url,
            "all_candidates": all_hits,
        }
        if not hits:
            rows.append({**base, "status": "still_no_email", "notes": "No email found in enhanced pass."})
            continue
        best = hits[0]
        selected[best.email] = (audit, best, hits)
        leads_to_add.append(build_lead(audit, best))
        if args.dry_run:
            rows.append(
                {
                    **base,
                    "status": "dry_run_would_add",
                    "email": best.email,
                    "email_type": best.email_type,
                    "confidence": best.confidence,
                    "source_url": best.source_url,
                    "method": best.method,
                }
            )

    response: dict[str, Any] = {}
    if leads_to_add and not args.dry_run:
        response = request(
            "/leads/add",
            method="POST",
            body={
                "campaign_id": args.campaign_id,
                "leads": leads_to_add,
                "verify_leads_on_import": False,
                "skip_if_in_workspace": True,
                "skip_if_in_campaign": True,
            },
        )
        created = response.get("created_leads") if isinstance(response.get("created_leads"), list) else []
        ids = {str(item.get("email") or "").lower(): str(item.get("id") or "") for item in created if isinstance(item, dict)}
        for email, (audit, hit, hits) in selected.items():
            rows.append(
                {
                    "root_domain": audit.root_domain,
                    "status": "added_to_campaign",
                    "email": hit.email,
                    "email_type": hit.email_type,
                    "confidence": hit.confidence,
                    "source_url": hit.source_url,
                    "method": hit.method,
                    "lead_id": ids.get(email, ""),
                    "best_type": audit.best_type,
                    "brands": audit.brands,
                    "example_source_url": audit.example_source_url,
                    "notes": "Added by enhanced second-pass discovery.",
                    "all_candidates": "; ".join(f"{item.email} ({item.email_type}, {item.confidence}, {item.method}, {item.source_url})" for item in hits[:8]),
                }
            )

    rows.sort(key=lambda row: (row.get("status", ""), row.get("root_domain", "")))
    write_results(args.output, rows)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.get("status", "")] = counts.get(row.get("status", ""), 0) + 1
    print(f"Input unresolved domains: {len(read_unresolved(args.input, max_domains=args.max_domains))}")
    print(f"Processed after campaign dedupe: {len(audits)}")
    print(f"Found/addable: {counts.get('dry_run_would_add', 0) or counts.get('added_to_campaign', 0)}")
    print(f"Still no email: {counts.get('still_no_email', 0)}")
    print(f"Output: {args.output}")
    if response:
        print({k: response.get(k) for k in ["status", "total_sent", "leads_uploaded", "skipped_count", "invalid_email_count", "duplicate_email_count"]})


if __name__ == "__main__":
    main()
