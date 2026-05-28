#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import gzip
import html
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from deal_tracker.db import connect, init_db  # noqa: E402
from deal_tracker.env import load_env_value  # noqa: E402
from deal_tracker.instantly import DEFAULT_CAMPAIGN_IDS, list_campaign_leads  # noqa: E402
from tools.import_csv_prospects_to_instantly import clean_domain, email_type, extract_emails, rank_email  # noqa: E402
from tools.import_seranking_link_candidates_to_instantly import acceptable_email, lead_domains  # noqa: E402


DEFAULT_INPUT = ROOT / "generated" / "imports" / "combined_filtered_link_prospects_review.csv"
DEFAULT_OUTPUT = ROOT / "generated" / "imports" / "combined_link_prospects_contact_discovery_review.csv"
DEFAULT_SUMMARY = ROOT / "generated" / "imports" / "combined_link_prospects_contact_discovery_summary.json"

USER_AGENT = "LD Search contact discovery/1.0 (+https://ldsearch.com.au)"
MAX_HTML_BYTES = 900_000
MAX_SITEMAP_BYTES = 2_500_000

URL_HINTS = [
    "contact",
    "contact-us",
    "advertis",
    "media",
    "mediakit",
    "media-kit",
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
    "enquiry",
    "inquiry",
]

KNOWN_PATHS = [
    "/contact",
    "/contact-us",
    "/about",
    "/about-us",
    "/advertise",
    "/advertise-with-us",
    "/advertising",
    "/media-kit",
    "/mediakit",
    "/media",
    "/editorial",
    "/submissions",
    "/submit",
    "/submit-an-article",
    "/write-for-us",
    "/guest-post",
    "/contribute",
    "/partnerships",
    "/partners",
    "/sponsored-posts",
    "/sponsorship",
    "/sales",
    "/enquiry",
]

SEARCH_TEMPLATES = [
    "site:{domain} email",
    "site:{domain} contact",
    "site:{domain} advertising",
    "site:{domain} media",
    "site:{domain} editor",
    '"{domain}" email',
    '"{site_name}" contact',
]

PROFILE_HOST_HINTS = [
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "linktr.ee",
    "beacons.ai",
    "muckrack.com",
    "crunchbase.com",
    "youtube.com",
    "x.com",
    "twitter.com",
]


@dataclass(frozen=True)
class CombinedProspect:
    root_domain: str
    site_name: str
    paid_link_likelihood_score: int
    best_type: str
    brands: str
    example_source_urls: list[str]
    source_files: str


@dataclass
class ContactHit:
    email: str
    email_type: str
    confidence: int
    source_url: str
    discovery_method: str
    is_off_domain_email: bool


@dataclass
class ContactRoute:
    source_url: str
    discovery_method: str
    page_type: str


@dataclass
class DiscoveryResult:
    prospect: CombinedProspect
    email_hits: list[ContactHit]
    contact_routes: list[ContactRoute]
    notes: str = ""


class CandidateAdapter:
    def __init__(self, prospect: CombinedProspect):
        self.root_domain = prospect.root_domain
        self.best_types = {prospect.best_type} if prospect.best_type else set()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def split_values(value: str) -> list[str]:
    parts = re.split(r"\s+\|\s+|;|,", value or "")
    return [part.strip() for part in parts if part.strip()]


def site_name_from_domain(domain: str) -> str:
    base = domain.split(".")[0].replace("-", " ").replace("_", " ")
    return " ".join(part.capitalize() for part in base.split()) or domain


def read_combined_prospects(path: Path, max_domains: int = 0) -> list[CombinedProspect]:
    prospects: list[CombinedProspect] = []
    seen: set[str] = set()
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if (row.get("status") or "").strip() and row.get("status") != "kept":
                continue
            domain = clean_domain(row.get("root_domain") or "")
            if not domain or "." not in domain or domain in seen:
                continue
            seen.add(domain)
            try:
                score = int(float(row.get("paid_link_likelihood_score") or 0))
            except ValueError:
                score = 0
            prospects.append(
                CombinedProspect(
                    root_domain=domain,
                    site_name=site_name_from_domain(domain),
                    paid_link_likelihood_score=score,
                    best_type=(row.get("link_types") or "").strip(),
                    brands=(row.get("brands") or "").strip(),
                    example_source_urls=split_values(row.get("example_source_urls") or "")[:8],
                    source_files=(row.get("source_files") or "").strip(),
                )
            )
    if max_domains:
        prospects = prospects[:max_domains]
    return prospects


def existing_domains_from_tracker() -> set[str]:
    domains: set[str] = set()
    init_db()
    with connect() as connection:
        rows = connection.execute("select root_domain, publisher_url, contact_email from link_deals").fetchall()
    for row in rows:
        for value in [row["root_domain"], row["publisher_url"], row["contact_email"]]:
            domain = clean_domain(value or "")
            if domain:
                domains.add(domain)
    return domains


def existing_domains_from_instantly(campaign_ids: list[str]) -> set[str]:
    if not load_env_value("INSTANTLY_API_KEY"):
        return set()
    domains: set[str] = set()
    for campaign_id in campaign_ids:
        try:
            leads = list_campaign_leads(campaign_id)
        except Exception as exc:  # noqa: BLE001
            print(f"Could not read Instantly campaign {campaign_id}: {exc}", file=sys.stderr)
            continue
        for lead in leads.values():
            domains.update(lead_domains(lead))
    return domains


def existing_domains(campaign_ids: list[str], use_live_dedupe: bool) -> set[str]:
    domains = existing_domains_from_tracker()
    if use_live_dedupe:
        domains.update(existing_domains_from_instantly(campaign_ids))
    return domains


def fetch_url(url: str, timeout: float, max_bytes: int = MAX_HTML_BYTES) -> tuple[str, str, str]:
    req = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,text/plain,application/xml;q=0.9,*/*;q=0.7",
        },
    )
    with urlopen(req, timeout=timeout) as response:
        content_type = response.headers.get("content-type", "")
        raw = response.read(max_bytes)
        if urlparse(response.geturl()).path.endswith(".gz"):
            raw = gzip.decompress(raw)
        charset = response.headers.get_content_charset() or "utf-8"
        return response.geturl(), raw.decode(charset, errors="replace"), content_type


def hinted_url(url: str) -> bool:
    lowered = url.lower()
    return any(hint in lowered for hint in URL_HINTS)


def page_type_for(url: str, body: str = "") -> str:
    lowered = f"{url} {body[:4000]}".lower()
    if "advertis" in lowered or "rate card" in lowered:
        return "advertising"
    if "media" in lowered:
        return "media"
    if "sponsor" in lowered:
        return "sponsored"
    if "write-for-us" in lowered or "guest-post" in lowered or "guest post" in lowered:
        return "guest_post"
    if "submit" in lowered or "contribute" in lowered or "editor" in lowered:
        return "editorial"
    if "contact" in lowered or "enquiry" in lowered or "inquiry" in lowered:
        return "contact"
    if "about" in lowered:
        return "about"
    return "hint_page"


def extract_links(body: str, base_url: str, root_domain: str, limit: int) -> list[str]:
    links: list[str] = []
    seen: set[str] = set()
    for raw in re.findall(r"""href=["']([^"']+)["']""", body, flags=re.I):
        href = html.unescape(raw)
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        absolute = urljoin(base_url, href).split("#", 1)[0]
        parsed = urlparse(absolute)
        if parsed.scheme not in {"http", "https"}:
            continue
        if clean_domain(parsed.netloc) != root_domain:
            continue
        if not hinted_url(absolute):
            continue
        if absolute not in seen:
            links.append(absolute)
            seen.add(absolute)
        if len(links) >= limit:
            break
    return links


def robots_sitemaps(root_domain: str, timeout: float) -> list[str]:
    urls: list[str] = []
    for scheme in ["https", "http"]:
        try:
            _, body, _ = fetch_url(f"{scheme}://{root_domain}/robots.txt", timeout, max_bytes=200_000)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, gzip.BadGzipFile):
            continue
        for line in body.splitlines():
            if line.lower().startswith("sitemap:"):
                sitemap = line.split(":", 1)[1].strip()
                if sitemap:
                    urls.append(sitemap)
        break
    urls.extend([f"https://{root_domain}/sitemap.xml", f"https://{root_domain}/sitemap_index.xml"])
    return list(dict.fromkeys(urls))


def sitemap_hint_urls(root_domain: str, timeout: float, limit: int) -> list[str]:
    results: list[str] = []
    queue = robots_sitemaps(root_domain, timeout)[:8]
    seen: set[str] = set()
    while queue and len(results) < limit:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            _, body, _ = fetch_url(url, timeout, max_bytes=MAX_SITEMAP_BYTES)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, gzip.BadGzipFile):
            continue
        try:
            root = ET.fromstring(body.strip().encode("utf-8"))
            locs = [el.text.strip() for el in root.iter() if el.tag.endswith("loc") and el.text]
        except ET.ParseError:
            locs = re.findall(r"<loc>(.*?)</loc>", body, flags=re.I | re.S)
        for loc in locs:
            loc = html.unescape(loc.strip())
            parsed = urlparse(loc)
            if clean_domain(parsed.netloc) != root_domain:
                continue
            if loc.endswith((".xml", ".xml.gz")) and len(queue) < 25:
                queue.append(loc)
                continue
            if hinted_url(loc):
                results.append(loc)
                if len(results) >= limit:
                    break
    return list(dict.fromkeys(results))


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
        if not href.startswith("http"):
            continue
        host = clean_domain(urlparse(href).netloc)
        if any(skip in host for skip in ["bing.com", "duckduckgo.com", "microsoft.com", "google.com"]):
            continue
        urls.append(href.split("#", 1)[0])
    return list(dict.fromkeys(urls))


def search_result_urls(query: str, timeout: float, max_result_urls: int) -> tuple[list[str], list[tuple[str, str]]]:
    urls: list[str] = []
    snippets: list[tuple[str, str]] = []
    engines = [
        f"https://www.bing.com/search?q={quote_plus(query)}",
        f"https://html.duckduckgo.com/html/?q={quote_plus(query)}",
    ]
    for search_url in engines:
        try:
            final_url, body, _ = fetch_url(search_url, timeout, max_bytes=700_000)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError):
            continue
        urls.extend(extract_search_result_urls(body))
        snippets.append((final_url, body))
        if urls:
            break
        time.sleep(0.15)
    return list(dict.fromkeys(urls))[:max_result_urls], snippets


def malformed_email(email: str) -> bool:
    if not re.fullmatch(r"[\w.+-]{1,64}@[\w.-]+\.[a-zA-Z]{2,24}", email or ""):
        return True
    lowered = email.lower()
    return any(token in lowered for token in ["u003e", "u003c", "example.", "sentry", "ingest", "noreply", "no-reply"])


def add_email_hit(found: dict[str, ContactHit], prospect: CombinedProspect, email: str, source_url: str, method: str, score_bonus: int = 0) -> None:
    email = email.strip().lower()
    if malformed_email(email):
        return
    adapter = CandidateAdapter(prospect)
    if not acceptable_email(adapter, email, source_url):
        return
    domain = clean_domain(email.split("@", 1)[1])
    is_off_domain = not (domain == prospect.root_domain or domain.endswith("." + prospect.root_domain))
    confidence = min(100, rank_email(email, prospect.root_domain, source_url) + score_bonus)
    hit = ContactHit(
        email=email,
        email_type=email_type(email),
        confidence=confidence,
        source_url=source_url,
        discovery_method=method,
        is_off_domain_email=is_off_domain,
    )
    current = found.get(email)
    if not current or (hit.confidence, len(hit.source_url)) > (current.confidence, len(current.source_url)):
        found[email] = hit


def scan_page(
    prospect: CombinedProspect,
    url: str,
    method: str,
    timeout: float,
    found: dict[str, ContactHit],
    routes: dict[str, ContactRoute],
    expand_links: bool = False,
    same_site_link_limit: int = 30,
) -> list[str]:
    try:
        final_url, body, content_type = fetch_url(url, timeout)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, gzip.BadGzipFile):
        return []
    if body and ("html" in content_type or "text/plain" in content_type or "xml" in content_type or not content_type):
        if hinted_url(final_url) or re.search(r"<form\b", body, flags=re.I):
            routes[final_url] = ContactRoute(final_url, method, page_type_for(final_url, body))
        for email in extract_emails(body):
            bonus = 10 if hinted_url(final_url) else 0
            add_email_hit(found, prospect, email, final_url, method, bonus)
        if expand_links:
            return extract_links(body, final_url, prospect.root_domain, same_site_link_limit)
    return []


def direct_urls(prospect: CombinedProspect, max_direct_urls: int) -> list[tuple[str, str, bool]]:
    queue: list[tuple[str, str, bool]] = []
    for source_url in prospect.example_source_urls[:2]:
        parsed = urlparse(source_url)
        if parsed.scheme in {"http", "https"} and clean_domain(parsed.netloc) == prospect.root_domain:
            queue.append((source_url, "example_source_expanded", True))
    queue.extend([(f"https://{prospect.root_domain}", "homepage_expanded", True), (f"http://{prospect.root_domain}", "homepage_expanded", True)])
    queue.extend((urljoin(f"https://{prospect.root_domain}", path), "known_contact_path", False) for path in KNOWN_PATHS)
    seen: set[str] = set()
    output: list[tuple[str, str, bool]] = []
    for url, method, expand in queue:
        key = url.split("#", 1)[0].rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        output.append((url, method, expand))
        if len(output) >= max_direct_urls:
            break
    return output


def should_fetch_search_result(result_url: str, root_domain: str) -> bool:
    host = clean_domain(urlparse(result_url).netloc)
    if host == root_domain or host.endswith("." + root_domain):
        return True
    return any(profile in host for profile in PROFILE_HOST_HINTS)


def discover_for(
    prospect: CombinedProspect,
    *,
    timeout: float,
    domain_time_budget: float,
    max_direct_urls: int,
    max_sitemap_urls: int,
    max_same_site_links: int,
    max_search_result_urls: int,
    use_search: bool,
) -> DiscoveryResult:
    found: dict[str, ContactHit] = {}
    routes: dict[str, ContactRoute] = {}
    seen_pages: set[str] = set()
    same_site_seen: set[str] = set()
    deadline = time.monotonic() + domain_time_budget if domain_time_budget > 0 else 0

    def budget_remaining() -> bool:
        return not deadline or time.monotonic() < deadline

    for url, method, expand in direct_urls(prospect, max_direct_urls):
        if not budget_remaining():
            break
        if url in seen_pages:
            continue
        seen_pages.add(url)
        links = scan_page(prospect, url, method, timeout, found, routes, expand_links=expand, same_site_link_limit=max_same_site_links)
        for link in links:
            if len(same_site_seen) >= max_same_site_links:
                break
            if link in seen_pages or link in same_site_seen:
                continue
            same_site_seen.add(link)
            scan_page(prospect, link, "same_site_hint_link", timeout, found, routes)
        if any(hit.confidence >= 96 for hit in found.values()):
            break
        time.sleep(0.02)

    if not any(hit.confidence >= 92 for hit in found.values()):
        if not budget_remaining():
            return DiscoveryResult(prospect=prospect, email_hits=sorted(found.values(), key=lambda hit: hit.confidence, reverse=True), contact_routes=sorted(routes.values(), key=lambda route: route.source_url)[:5], notes="domain_time_budget_reached")
        for url in sitemap_hint_urls(prospect.root_domain, timeout, max_sitemap_urls):
            if not budget_remaining():
                break
            if url in seen_pages:
                continue
            seen_pages.add(url)
            scan_page(prospect, url, "sitemap_hint_page", timeout, found, routes)
            if any(hit.confidence >= 94 for hit in found.values()):
                break
            time.sleep(0.02)

    if use_search and not any(hit.confidence >= 90 for hit in found.values()):
        fetched_search_urls = 0
        for template in SEARCH_TEMPLATES:
            if not budget_remaining():
                break
            query = template.format(domain=prospect.root_domain, site_name=prospect.site_name)
            result_urls, snippets = search_result_urls(query, timeout, max_search_result_urls)
            for search_url, body in snippets:
                for email in extract_emails(body):
                    add_email_hit(found, prospect, email, search_url, "search_snippet", 4)
            for result_url in result_urls:
                if not budget_remaining():
                    break
                if fetched_search_urls >= max_search_result_urls:
                    break
                if not should_fetch_search_result(result_url, prospect.root_domain):
                    continue
                fetched_search_urls += 1
                method = "search_same_site_result" if clean_domain(urlparse(result_url).netloc) == prospect.root_domain else "search_public_profile_result"
                scan_page(prospect, result_url, method, timeout, found, routes)
            if any(hit.confidence >= 90 for hit in found.values()):
                break
            time.sleep(0.15)

    hits = sorted(found.values(), key=lambda hit: (hit.confidence, not hit.is_off_domain_email, hit.email_type), reverse=True)
    route_rows = sorted(routes.values(), key=lambda route: (route.page_type != "contact", route.source_url))[:5]
    return DiscoveryResult(prospect=prospect, email_hits=hits, contact_routes=route_rows)


def recommended_action(hit: ContactHit | None, routes: list[ContactRoute]) -> str:
    if hit:
        if not hit.is_off_domain_email and hit.confidence >= 75:
            return "ready_for_import"
        return "manual_review"
    if routes:
        return "contact_form_only"
    return "no_contact_found"


def confidence_bucket(confidence: int) -> str:
    if confidence >= 90:
        return "90-100"
    if confidence >= 80:
        return "80-89"
    if confidence >= 70:
        return "70-79"
    if confidence >= 60:
        return "60-69"
    if confidence > 0:
        return "1-59"
    return "none"


def row_context(prospect: CombinedProspect, all_candidates: str) -> dict[str, Any]:
    return {
        "root_domain": prospect.root_domain,
        "site_name": prospect.site_name,
        "best_type": prospect.best_type,
        "paid_link_likelihood_score": prospect.paid_link_likelihood_score,
        "brands": prospect.brands,
        "example_source_urls": " | ".join(prospect.example_source_urls),
        "all_candidates": all_candidates,
    }


def rows_for_result(result: DiscoveryResult) -> list[dict[str, Any]]:
    prospect = result.prospect
    all_candidates = "; ".join(
        f"{hit.email} ({hit.email_type}, {hit.confidence}, {hit.discovery_method}, {hit.source_url})"
        for hit in result.email_hits[:12]
    )
    base = row_context(prospect, all_candidates)
    rows: list[dict[str, Any]] = []
    if result.email_hits:
        for hit in result.email_hits:
            rows.append(
                {
                    **base,
                    "email": hit.email,
                    "email_type": hit.email_type,
                    "confidence": hit.confidence,
                    "source_url": hit.source_url,
                    "discovery_method": hit.discovery_method,
                    "is_off_domain_email": "yes" if hit.is_off_domain_email else "no",
                    "recommended_action": recommended_action(hit, result.contact_routes),
                    "notes": result.notes,
                }
            )
        return rows

    if result.contact_routes:
        route = result.contact_routes[0]
        return [
            {
                **base,
                "email": "",
                "email_type": "",
                "confidence": "",
                "source_url": route.source_url,
                "discovery_method": route.discovery_method,
                "is_off_domain_email": "",
                "recommended_action": "contact_form_only",
                "notes": f"No email found; best contact route page_type={route.page_type}.",
            }
        ]

    return [
        {
            **base,
            "email": "",
            "email_type": "",
            "confidence": "",
            "source_url": f"https://{prospect.root_domain}",
            "discovery_method": "",
            "is_off_domain_email": "",
            "recommended_action": "no_contact_found",
            "notes": "No public email or contact route found within balanced limits.",
        }
    ]


def write_review_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "root_domain",
        "site_name",
        "email",
        "email_type",
        "confidence",
        "source_url",
        "discovery_method",
        "is_off_domain_email",
        "best_type",
        "paid_link_likelihood_score",
        "brands",
        "example_source_urls",
        "all_candidates",
        "recommended_action",
        "notes",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_summary(path: Path, *, input_count: int, skipped_count: int, results: list[DiscoveryResult], rows: list[dict[str, Any]]) -> dict[str, Any]:
    domains_with_email = {row["root_domain"] for row in rows if row.get("email")}
    contact_only = {row["root_domain"] for row in rows if row.get("recommended_action") == "contact_form_only"}
    no_contact = {row["root_domain"] for row in rows if row.get("recommended_action") == "no_contact_found"}
    summary = {
        "generated_at": now_iso(),
        "input_domain_count": input_count,
        "skipped_already_known_domain_count": skipped_count,
        "processed_domain_count": len(results),
        "output_row_count": len(rows),
        "domains_with_email_count": len(domains_with_email),
        "domains_contact_form_only_count": len(contact_only),
        "domains_no_contact_found_count": len(no_contact),
        "rows_by_recommended_action": dict(Counter(row.get("recommended_action") or "" for row in rows)),
        "rows_by_discovery_method": dict(Counter(row.get("discovery_method") or "none" for row in rows)),
        "rows_by_email_type": dict(Counter(row.get("email_type") or "none" for row in rows)),
        "rows_by_confidence_bucket": dict(Counter(confidence_bucket(int(float(row["confidence"]))) if row.get("confidence") else "none" for row in rows)),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Review-only balanced contact discovery for combined link prospect domains.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--timeout", type=float, default=6.0)
    parser.add_argument("--domain-time-budget", type=float, default=45.0)
    parser.add_argument("--max-domains", type=int, default=0)
    parser.add_argument("--max-direct-urls", type=int, default=20)
    parser.add_argument("--max-sitemap-urls", type=int, default=40)
    parser.add_argument("--max-same-site-links", type=int, default=30)
    parser.add_argument("--max-search-result-urls", type=int, default=8)
    parser.add_argument("--no-search", action="store_true")
    parser.add_argument("--no-live-dedupe", action="store_true")
    args = parser.parse_args()

    prospects = read_combined_prospects(args.input, args.max_domains)
    already = existing_domains(DEFAULT_CAMPAIGN_IDS, use_live_dedupe=not args.no_live_dedupe)
    to_process = [prospect for prospect in prospects if prospect.root_domain not in already]
    skipped_count = len(prospects) - len(to_process)
    print(f"Input domains: {len(prospects)}", flush=True)
    print(f"Skipped already known/contacted: {skipped_count}", flush=True)
    print(f"Processing domains: {len(to_process)}", flush=True)

    results: list[DiscoveryResult] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(
                discover_for,
                prospect,
                timeout=args.timeout,
                domain_time_budget=args.domain_time_budget,
                max_direct_urls=args.max_direct_urls,
                max_sitemap_urls=args.max_sitemap_urls,
                max_same_site_links=args.max_same_site_links,
                max_search_result_urls=args.max_search_result_urls,
                use_search=not args.no_search,
            ): prospect
            for prospect in to_process
        }
        completed = 0
        for future in as_completed(futures):
            prospect = futures[future]
            completed += 1
            try:
                results.append(future.result())
            except Exception as exc:  # noqa: BLE001
                results.append(DiscoveryResult(prospect=prospect, email_hits=[], contact_routes=[], notes=f"scrape_error: {exc!s}"[:500]))
            if completed % 10 == 0 or completed == len(to_process):
                print(f"Completed {completed}/{len(to_process)}", flush=True)

    order = {prospect.root_domain: index for index, prospect in enumerate(to_process)}
    results.sort(key=lambda result: order.get(result.prospect.root_domain, 999999))
    rows: list[dict[str, Any]] = []
    for result in results:
        rows.extend(rows_for_result(result))
    action_rank = {"ready_for_import": 0, "manual_review": 1, "contact_form_only": 2, "no_contact_found": 3}
    rows.sort(
        key=lambda row: (
            action_rank.get(row.get("recommended_action", ""), 9),
            -int(float(row.get("confidence") or 0)),
            -int(float(row.get("paid_link_likelihood_score") or 0)),
            row.get("root_domain", ""),
        )
    )
    write_review_csv(args.output, rows)
    summary = write_summary(args.summary, input_count=len(prospects), skipped_count=skipped_count, results=results, rows=rows)
    print(json.dumps(summary, indent=2, sort_keys=True), flush=True)
    print(f"Review CSV: {args.output}", flush=True)
    print(f"Summary JSON: {args.summary}", flush=True)


if __name__ == "__main__":
    main()
