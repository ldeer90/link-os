from __future__ import annotations

import csv
import hashlib
import json
import re
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

from .apify_google import (
    ApifyGoogleHarvestConfig,
    item_query,
    load_env_value,
    organic_results,
    result_snippet,
    result_title,
    result_url,
    run_apify_google_search,
)
from .email_discovery import (
    FREE_EMAIL_DOMAINS,
    classify_email_type,
    confidence,
    email_allowed,
    emails_from_html,
    emails_from_mailto,
    local_prefix,
    page_type_for_url,
)
from .paid_link_acceptance import PAID_LINK_REVIEW_CSV, read_csv_rows
from .paths import CONTACT_DISCOVERY_DIR, REVIEW_DIR
from .search_email_scrape import LINK_HINTS
from .universe import off_domain_business_email_allowed
from .utils import clean_domain, normalize_url, now_iso, root_domain_from_url, safe_slug
from .web import PageFetch, fetch_html, parse_page, same_site


MISSING_EMAIL_REVIEW_CSV = REVIEW_DIR / "missing_email_discovery_review.csv"
MISSING_EMAIL_SUMMARY_JSON = REVIEW_DIR / "missing_email_discovery_summary.json"

MISSING_EMAIL_FIELDS = [
    "root_domain",
    "site_name",
    "paid_link_likelihood",
    "paid_link_score",
    "transaction_type",
    "source_url",
    "email",
    "email_type",
    "email_confidence",
    "email_source_url",
    "email_source_method",
    "discovery_stage",
    "is_verified_public_email",
    "is_role_pattern_guess",
    "is_off_domain_email",
    "source_domain",
    "page_type",
    "notes",
    "recommended_contact",
    "recommended_next_action",
]

LIKELIHOOD_RANK = {"Very Likely": 0, "Likely": 1, "Possible": 2, "Weak": 3, "Out Of Scope": 4}
DIRECT_TYPES = {"advertising", "media", "partnerships", "sponsored", "editor", "content"}
GENERIC_TYPES = {"generic"}

KNOWN_PATHS = [
    "/contact",
    "/contact-us",
    "/enquiry",
    "/enquiries",
    "/advertise",
    "/advertise-with-us",
    "/advertising",
    "/advertising-enquiry",
    "/advertising-enquiries",
    "/media-kit",
    "/mediakit",
    "/media-pack",
    "/media-rates",
    "/sponsored-post",
    "/sponsored-posts",
    "/sponsored-content",
    "/write-for-us",
    "/guest-post",
    "/guest-posts",
    "/guest-post-guidelines",
    "/contribute",
    "/submit",
    "/submit-an-article",
    "/submissions",
    "/partnerships",
    "/partner-with-us",
    "/work-with-us",
    "/about",
    "/about-us",
]

ROLE_PREFIXES = [
    "advertising",
    "advertise",
    "media",
    "partnerships",
    "partners",
    "sponsored",
    "sponsor",
    "editor",
    "editorial",
    "content",
    "submissions",
    "hello",
    "info",
    "contact",
    "admin",
    "sales",
]

PUBLIC_PROFILE_HOST_HINTS = (
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "twitter.com",
    "x.com",
    "youtube.com",
    "linktr.ee",
    "linktree",
    "beacons.ai",
    "crunchbase.com",
    "muckrack.com",
    "about.me",
    "medium.com",
    "substack.com",
    "podcasts.apple.com",
    "spotify.com",
    "truelocal.com.au",
    "yellowpages.com.au",
    "whitepages.com.au",
)

NO_ROLE_GUESS_DOMAINS = {
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "au.linkedin.com",
    "twitter.com",
    "x.com",
    "youtube.com",
    "reddit.com",
    "amazon.com.au",
    "amazon.com",
    "ebay.com.au",
    "freelancer.com.au",
}


@dataclass(frozen=True)
class MissingEmailConfig:
    input: Path = PAID_LINK_REVIEW_CSV
    output: Path = MISSING_EMAIL_REVIEW_CSV
    summary: Path = MISSING_EMAIL_SUMMARY_JSON
    workers: int = 16
    timeout: int = 5
    max_domains: int = 0
    include_out_of_scope: bool = False
    use_apify_search: bool = True
    public_web_brand_search: bool = True
    scrape_social_profiles: bool = True
    role_pattern_guesses: bool = True
    max_pages_per_domain: int = 30
    max_apify_domains: int = 0


@dataclass(frozen=True)
class EmailCandidate:
    root_domain: str
    site_name: str
    paid_link_likelihood: str
    paid_link_score: str
    transaction_type: str
    source_url: str
    email: str
    email_type: str
    email_confidence: int
    email_source_url: str
    email_source_method: str
    discovery_stage: str
    is_verified_public_email: bool
    is_role_pattern_guess: bool
    is_off_domain_email: bool
    source_domain: str
    page_type: str
    notes: str

    def to_row(self) -> dict[str, str]:
        return {
            "root_domain": self.root_domain,
            "site_name": self.site_name,
            "paid_link_likelihood": self.paid_link_likelihood,
            "paid_link_score": self.paid_link_score,
            "transaction_type": self.transaction_type,
            "source_url": self.source_url,
            "email": self.email,
            "email_type": self.email_type,
            "email_confidence": str(self.email_confidence),
            "email_source_url": self.email_source_url,
            "email_source_method": self.email_source_method,
            "discovery_stage": self.discovery_stage,
            "is_verified_public_email": "yes" if self.is_verified_public_email else "no",
            "is_role_pattern_guess": "yes" if self.is_role_pattern_guess else "no",
            "is_off_domain_email": "yes" if self.is_off_domain_email else "no",
            "source_domain": self.source_domain,
            "page_type": self.page_type,
            "notes": self.notes,
            "recommended_contact": "yes" if recommended_contact(self) else "no",
            "recommended_next_action": recommended_next_action_for_candidate(self),
        }


def select_missing_rows(rows: list[dict[str, str]], *, include_out_of_scope: bool = False, max_domains: int = 0) -> list[dict[str, str]]:
    selected = [
        row
        for row in rows
        if not row.get("email")
        and (include_out_of_scope or row.get("paid_link_likelihood") != "Out Of Scope")
    ]
    selected.sort(
        key=lambda row: (
            LIKELIHOOD_RANK.get(row.get("paid_link_likelihood", ""), 9),
            -int(row.get("paid_link_score") or 0),
            row.get("root_domain", ""),
        )
    )
    return selected[:max_domains] if max_domains else selected


def known_path_urls(root_domain: str, source_url: str = "") -> list[str]:
    parsed = urlparse(source_url if source_url else f"https://{root_domain}/")
    scheme = parsed.scheme if parsed.scheme in {"http", "https"} else "https"
    netloc = parsed.netloc or root_domain
    base = f"{scheme}://{netloc}"
    urls = [normalize_url(source_url)] if source_url else []
    urls.append(f"https://{root_domain}/")
    urls.append(f"https://www.{root_domain}/")
    urls.extend(urljoin(base + "/", path.lstrip("/")) for path in KNOWN_PATHS)
    return dedupe_urls(urls)


def dedupe_urls(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for url in urls:
        normalized = normalize_url(url.strip()) if url else ""
        if normalized and normalized not in seen:
            seen.add(normalized)
            output.append(normalized)
    return output


def cache_path_for_url(url: str) -> Path:
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    return CONTACT_DISCOVERY_DIR / f"{safe_slug(root_domain_from_url(url))}_{digest}.html"


def cached_fetch(url: str, timeout: int) -> PageFetch:
    CONTACT_DISCOVERY_DIR.mkdir(parents=True, exist_ok=True)
    path = cache_path_for_url(url)
    if path.exists():
        return PageFetch(requested_url=url, final_url=url, status="cached", html=path.read_text(encoding="utf-8", errors="replace"))
    page = fetch_html(url, timeout=timeout)
    if page.html:
        path.write_text(page.html, encoding="utf-8")
    return page


def hinted_same_site_links(root_domain: str, base_url: str, html: str, limit: int) -> list[str]:
    parser = parse_page(html)
    urls: list[str] = []
    for href, anchor in parser.links:
        if href.lower().startswith("mailto:"):
            continue
        context = f"{href} {anchor}".lower()
        if not any(hint in context for hint in LINK_HINTS):
            continue
        url = normalize_url(urljoin(base_url, href))
        if same_site(url, root_domain):
            urls.append(url)
    return dedupe_urls(urls)[:limit]


def parse_sitemap_urls(xml_text: str) -> list[str]:
    urls: list[str] = []
    if not xml_text:
        return urls
    try:
        root = ElementTree.fromstring(xml_text.encode("utf-8"))
        for element in root.iter():
            if element.tag.lower().endswith("loc") and element.text:
                urls.append(element.text.strip())
    except Exception:
        urls.extend(re.findall(r"<loc>\s*([^<]+?)\s*</loc>", xml_text, flags=re.I))
    return dedupe_urls(urls)


def parse_robots_sitemaps(robots_text: str) -> list[str]:
    urls: list[str] = []
    for line in (robots_text or "").splitlines():
        if line.lower().startswith("sitemap:"):
            urls.append(line.split(":", 1)[1].strip())
    return dedupe_urls(urls)


def sitemap_candidate_urls(root_domain: str, timeout: int, limit: int = 40) -> list[str]:
    candidates = [f"https://{root_domain}/robots.txt", f"https://{root_domain}/sitemap.xml", f"https://www.{root_domain}/sitemap.xml"]
    sitemap_urls: list[str] = []
    output: list[str] = []
    robots = cached_fetch(candidates[0], timeout=timeout)
    sitemap_urls.extend(parse_robots_sitemaps(robots.html))
    sitemap_urls.extend(candidates[1:])
    for sitemap_url in dedupe_urls(sitemap_urls)[:5]:
        page = cached_fetch(sitemap_url, timeout=timeout)
        locs = parse_sitemap_urls(page.html)
        nested = [url for url in locs if "sitemap" in url.lower()][:3]
        for nested_url in nested:
            nested_page = cached_fetch(nested_url, timeout=timeout)
            locs.extend(parse_sitemap_urls(nested_page.html))
        for url in locs:
            lowered = url.lower()
            if any(hint in lowered for hint in LINK_HINTS):
                output.append(url)
            if len(output) >= limit:
                return dedupe_urls(output)[:limit]
    return dedupe_urls(output)[:limit]


def brand_tokens(row: dict[str, str]) -> set[str]:
    domain = clean_domain(row.get("root_domain", ""))
    stem = domain.split(".", 1)[0]
    text = f"{row.get('site_name', '')} {stem}".lower()
    return {token for token in re.findall(r"[a-z0-9]{3,}", text) if token not in {"the", "and", "com", "comau", "australia", "australian"}}


def brand_search_queries(row: dict[str, str], *, public_web: bool = True) -> list[str]:
    domain = clean_domain(row.get("root_domain", ""))
    site_name = (row.get("site_name") or domain).replace('"', "")
    queries = [
        f"site:{domain} email",
        f"site:{domain} contact",
        f"site:{domain} advertising",
        f"site:{domain} media kit",
        f"site:{domain} editor",
    ]
    if public_web:
        queries.extend(
            [
                f'"{site_name}" email',
                f'"{site_name}" "contact email"',
                f'"{site_name}" "advertising email"',
                f'"{site_name}" "media kit"',
                f'"{domain}" email',
            ]
        )
    return queries


def is_public_profile_url(url: str) -> bool:
    host = clean_domain(urlparse(url).netloc)
    return any(host == hint or host.endswith(f".{hint}") for hint in PUBLIC_PROFILE_HOST_HINTS)


def should_scrape_public_result(row: dict[str, str], url: str, title: str, snippet: str, *, scrape_social_profiles: bool) -> bool:
    if same_site(url, row.get("root_domain", "")):
        return True
    if scrape_social_profiles and is_public_profile_url(url):
        text = f"{url} {title} {snippet}".lower()
        tokens = brand_tokens(row)
        return any(token in text for token in tokens)
    text = f"{url} {title} {snippet}".lower()
    return any(token in text for token in brand_tokens(row)) and any(marker in text for marker in ["email", "contact", "advertis", "media"])


def apify_candidate_urls(row: dict[str, str], config: MissingEmailConfig) -> list[tuple[str, str, str]]:
    if not config.use_apify_search:
        return []
    token = load_env_value("APIFY_API_TOKEN") or load_env_value("APIFY_TOKEN")
    if not token:
        return []
    queries = brand_search_queries(row, public_web=config.public_web_brand_search)
    api_config = ApifyGoogleHarvestConfig(pages_per_query=1, batch_size=len(queries), wait_timeout_seconds=180)
    try:
        items = run_apify_google_search(queries, api_config, token)
    except Exception:
        return []
    output: list[tuple[str, str, str]] = []
    for item in items:
        query = item_query(item, "")
        for result in organic_results(item):
            url = result_url(result)
            title = result_title(result)
            snippet = result_snippet(result)
            if url and should_scrape_public_result(row, url, title, snippet, scrape_social_profiles=config.scrape_social_profiles):
                output.append((url, "apify_public_web_search" if not same_site(url, row.get("root_domain", "")) else "apify_site_search", query))
    return [(url, stage, query) for url, stage, query in dedupe_tuple_urls(output)[:20]]


def dedupe_tuple_urls(items: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    seen: set[str] = set()
    output: list[tuple[str, str, str]] = []
    for url, stage, note in items:
        normalized = normalize_url(url)
        if normalized and normalized not in seen:
            seen.add(normalized)
            output.append((normalized, stage, note))
    return output


def is_business_email(email: str) -> bool:
    domain = email.rsplit("@", 1)[-1].lower() if "@" in email else ""
    return domain not in FREE_EMAIL_DOMAINS and bool(classify_email_type(email))


def email_domain(email: str) -> str:
    return clean_domain(email.rsplit("@", 1)[-1]) if "@" in email else ""


def public_email_allowed(row: dict[str, str], email: str, source_url: str, page_text: str, *, stage: str) -> bool:
    root_domain = row.get("root_domain", "")
    if email_allowed(email, root_domain):
        return True
    email_type = classify_email_type(email)
    if off_domain_business_email_allowed(email) and email_type in DIRECT_TYPES:
        return True
    if not is_business_email(email):
        return False
    if is_public_profile_url(source_url):
        haystack = f"{source_url} {page_text}".lower()
        return any(token in haystack for token in brand_tokens(row))
    return False


def candidate_from_email(row: dict[str, str], email: str, source_url: str, method: str, stage: str, page_type: str, from_mailto: bool, notes: str = "") -> EmailCandidate:
    root_domain = row.get("root_domain", "")
    email_type = classify_email_type(email)
    source_domain = root_domain_from_url(source_url)
    return EmailCandidate(
        root_domain=root_domain,
        site_name=row.get("site_name", ""),
        paid_link_likelihood=row.get("paid_link_likelihood", ""),
        paid_link_score=row.get("paid_link_score", ""),
        transaction_type=row.get("transaction_type", ""),
        source_url=row.get("source_url", ""),
        email=email,
        email_type=email_type,
        email_confidence=confidence(email, page_type, from_mailto),
        email_source_url=source_url,
        email_source_method=method,
        discovery_stage=stage,
        is_verified_public_email=True,
        is_role_pattern_guess=False,
        is_off_domain_email=email_domain(email) != clean_domain(root_domain),
        source_domain=source_domain,
        page_type=page_type,
        notes=notes,
    )


def scrape_urls_for_emails(row: dict[str, str], url_items: list[tuple[str, str, str]], timeout: int, max_pages: int) -> list[EmailCandidate]:
    candidates: list[EmailCandidate] = []
    seen_email_source: set[tuple[str, str]] = set()
    for url, stage, note in url_items[:max_pages]:
        page = cached_fetch(url, timeout=timeout)
        if not page.html:
            continue
        parser = parse_page(page.html)
        final_url = page.final_url or url
        page_type = page_type_for_url(final_url)
        mailto = emails_from_mailto(page.html)
        visible = emails_from_html(page.html)
        page_text = f"{parser.title} {parser.visible_text}"
        for email in sorted(mailto | visible):
            if not public_email_allowed(row, email, final_url, page_text, stage=stage):
                continue
            key = (email, final_url)
            if key in seen_email_source:
                continue
            seen_email_source.add(key)
            candidates.append(candidate_from_email(row, email, final_url, "page_scrape", stage, page_type, email in mailto, note))
    return candidates


def role_pattern_candidates(row: dict[str, str]) -> list[EmailCandidate]:
    root_domain = clean_domain(row.get("root_domain", ""))
    if not root_domain or root_domain in NO_ROLE_GUESS_DOMAINS or any(root_domain.endswith(f".{domain}") for domain in NO_ROLE_GUESS_DOMAINS):
        return []
    if root_domain.count(".") > 2:
        return []
    candidates: list[EmailCandidate] = []
    for prefix in ROLE_PREFIXES:
        email = f"{prefix}@{root_domain}"
        email_type = classify_email_type(email)
        score = 45 if email_type in DIRECT_TYPES else 35
        candidates.append(
            EmailCandidate(
                root_domain=root_domain,
                site_name=row.get("site_name", ""),
                paid_link_likelihood=row.get("paid_link_likelihood", ""),
                paid_link_score=row.get("paid_link_score", ""),
                transaction_type=row.get("transaction_type", ""),
                source_url=row.get("source_url", ""),
                email=email,
                email_type=email_type,
                email_confidence=score,
                email_source_url=f"https://{root_domain}/",
                email_source_method="role_pattern_unverified",
                discovery_stage="role_pattern_guess",
                is_verified_public_email=False,
                is_role_pattern_guess=True,
                is_off_domain_email=False,
                source_domain=root_domain,
                page_type="role_pattern",
                notes="unverified role-pattern guess",
            )
        )
    return candidates


def discovery_urls_for_row(row: dict[str, str], config: MissingEmailConfig) -> list[tuple[str, str, str]]:
    root_domain = row.get("root_domain", "")
    urls = [(url, "known_path_crawl", "") for url in known_path_urls(root_domain, row.get("source_url", ""))]
    source_pages = urls[:4]
    for url, _stage, _note in source_pages:
        page = cached_fetch(url, timeout=config.timeout)
        if page.html:
            urls.extend((link, "on_page_expansion", url) for link in hinted_same_site_links(root_domain, page.final_url or url, page.html, 20))
    urls.extend((url, "sitemap_robots_crawl", "") for url in sitemap_candidate_urls(root_domain, config.timeout, 40))
    urls.extend(apify_candidate_urls(row, config))
    return dedupe_tuple_urls(urls)[: config.max_pages_per_domain]


def discover_row_emails(row: dict[str, str], config: MissingEmailConfig) -> list[EmailCandidate]:
    url_items = discovery_urls_for_row(row, config)
    candidates = scrape_urls_for_emails(row, url_items, config.timeout, config.max_pages_per_domain)
    if config.role_pattern_guesses:
        candidates.extend(role_pattern_candidates(row))
    return candidates


def recommended_contact(candidate: EmailCandidate) -> bool:
    if candidate.is_role_pattern_guess:
        return False
    if candidate.email_type in DIRECT_TYPES:
        return True
    return candidate.email_type in GENERIC_TYPES and candidate.email_confidence >= 60


def recommended_next_action_for_candidate(candidate: EmailCandidate) -> str:
    if candidate.is_role_pattern_guess:
        return "verify_role_pattern_before_use"
    if recommended_contact(candidate):
        return "email_pricing_request"
    return "manual_contact_review"


def sort_candidates(candidates: list[EmailCandidate]) -> list[EmailCandidate]:
    stage_rank = {
        "known_path_crawl": 0,
        "sitemap_robots_crawl": 1,
        "on_page_expansion": 2,
        "apify_site_search": 3,
        "apify_public_web_search": 4,
        "role_pattern_guess": 5,
    }
    return sorted(
        candidates,
        key=lambda item: (
            LIKELIHOOD_RANK.get(item.paid_link_likelihood, 9),
            1 if item.is_role_pattern_guess else 0,
            0 if item.email_type in DIRECT_TYPES else 1,
            stage_rank.get(item.discovery_stage, 9),
            -item.email_confidence,
            item.root_domain,
            item.email,
        ),
    )


def write_missing_email_rows(path: Path, candidates: list[EmailCandidate]) -> None:
    rows = [candidate.to_row() for candidate in sort_candidates(candidates)]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=MISSING_EMAIL_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def summary_for(
    candidates: list[EmailCandidate],
    selected_rows: list[dict[str, str]],
    input_missing_count: int,
    output: Path,
    summary_path: Path,
    *,
    completed_domain_count: int | None = None,
    partial: bool = False,
) -> dict[str, object]:
    public_domains = {candidate.root_domain for candidate in candidates if candidate.is_verified_public_email}
    guess_domains = {candidate.root_domain for candidate in candidates if candidate.is_role_pattern_guess}
    social_candidates = [candidate for candidate in candidates if candidate.discovery_stage == "apify_public_web_search" or is_public_profile_url(candidate.email_source_url)]
    remaining_high = [
        row.get("root_domain", "")
        for row in selected_rows
        if row.get("paid_link_likelihood") in {"Very Likely", "Likely", "Possible"} and row.get("root_domain", "") not in public_domains and row.get("root_domain", "") not in guess_domains
    ]
    return {
        "input_missing_count": input_missing_count,
        "processed_domain_count": len(selected_rows),
        "completed_domain_count": completed_domain_count if completed_domain_count is not None else len(selected_rows),
        "partial": partial,
        "candidate_rows": len(candidates),
        "domains_with_new_public_emails": len(public_domains),
        "domains_with_only_role_pattern_guesses": len(guess_domains - public_domains),
        "rows_by_discovery_stage": dict(Counter(candidate.discovery_stage for candidate in candidates)),
        "rows_by_email_type": dict(Counter(candidate.email_type for candidate in candidates)),
        "social_profile_source_email_count": len(social_candidates),
        "remaining_missing_high_likelihood_domains": len(remaining_high),
        "remaining_missing_high_likelihood_domain_samples": remaining_high[:50],
        "output": str(output),
        "summary": str(summary_path),
        "generated_at": now_iso(),
    }


def discover_missing_emails(config: MissingEmailConfig) -> dict[str, object]:
    input_rows = read_csv_rows(config.input)
    missing_count = sum(1 for row in input_rows if not row.get("email"))
    selected = select_missing_rows(input_rows, include_out_of_scope=config.include_out_of_scope, max_domains=config.max_domains)
    if config.use_apify_search and config.max_apify_domains:
        apify_allowed = {row.get("root_domain") for row in selected[: config.max_apify_domains]}
    else:
        apify_allowed = {row.get("root_domain") for row in selected}

    started = time.time()
    candidates: list[EmailCandidate] = []
    errors = 0
    completed = 0
    write_missing_email_rows(config.output, candidates)
    with ThreadPoolExecutor(max_workers=max(1, config.workers)) as executor:
        futures = {}
        for row in selected:
            row_config = config
            if row.get("root_domain") not in apify_allowed:
                row_config = MissingEmailConfig(**{**config.__dict__, "use_apify_search": False})
            futures[executor.submit(discover_row_emails, row, row_config)] = row
        for future in as_completed(futures):
            try:
                candidates.extend(future.result())
            except Exception:
                errors += 1
            completed += 1
            if completed == 1 or completed % 25 == 0 or completed == len(selected):
                write_missing_email_rows(config.output, candidates)
                partial_summary = summary_for(candidates, selected, missing_count, config.output, config.summary, completed_domain_count=completed, partial=completed < len(selected))
                partial_summary.update({"errors": errors, "elapsed_seconds": round(time.time() - started, 2)})
                config.summary.parent.mkdir(parents=True, exist_ok=True)
                config.summary.write_text(json.dumps(partial_summary, indent=2, sort_keys=True), encoding="utf-8")
                print(json.dumps({"completed": completed, "total": len(selected), "candidate_rows": len(candidates), "errors": errors}, sort_keys=True), flush=True)
    write_missing_email_rows(config.output, candidates)
    summary = summary_for(candidates, selected, missing_count, config.output, config.summary, completed_domain_count=completed, partial=False)
    summary.update({"errors": errors, "elapsed_seconds": round(time.time() - started, 2)})
    config.summary.parent.mkdir(parents=True, exist_ok=True)
    config.summary.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    return summary
