from __future__ import annotations

import csv
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

from .email_discovery import classify_email_type, confidence, email_allowed, emails_from_html, emails_from_mailto, page_type_for_url
from .paths import SEARCH_HARVEST_DIR
from .search_harvest import read_harvest_csv
from .universe import off_domain_business_email_allowed
from .utils import is_australian_candidate_domain, normalize_url, now_iso, root_domain_from_url
from .web import fetch_html, parse_page, same_site


EMAIL_SCRAPE_FIELDS = [
    "root_domain",
    "source_result_url",
    "scraped_url",
    "email",
    "email_type",
    "source_page_type",
    "confidence",
    "matched_phrase",
    "query",
    "scraped_at",
]

LINK_HINTS = (
    "contact",
    "advertis",
    "media",
    "sponsor",
    "partner",
    "collaborat",
    "write for us",
    "guest",
    "contribut",
    "editorial",
)


@dataclass(frozen=True)
class EmailScrapeConfig:
    harvest_csvs: list[Path]
    workers: int = 12
    timeout: int = 4
    max_pages_per_domain: int = 6
    output: Path | None = None


def default_email_output_path() -> Path:
    SEARCH_HARVEST_DIR.mkdir(parents=True, exist_ok=True)
    slug = now_iso().replace(":", "").replace("+", "Z")
    return SEARCH_HARVEST_DIR / f"search_domain_emails_{slug}.csv"


def harvest_domain_rows(paths: list[Path]) -> list[dict[str, str]]:
    by_domain: dict[str, dict[str, str]] = {}
    for path in paths:
        for row in read_harvest_csv(path):
            domain = (row.get("root_domain") or "").strip().lower()
            url = (row.get("result_url") or "").strip()
            if not domain and url:
                domain = root_domain_from_url(url)
            if not domain or not is_australian_candidate_domain(domain, f"{url} {row.get('query', '')} {row.get('title', '')}") or not url:
                continue
            previous = by_domain.get(domain)
            if previous is None or (row.get("matched_phrase") and not previous.get("matched_phrase")):
                by_domain[domain] = row
    return list(by_domain.values())


def candidate_urls_from_page(root_domain: str, source_url: str, html: str, max_pages: int) -> list[str]:
    urls = [source_url]
    homepage = f"https://{root_domain}/"
    if normalize_url(homepage) != normalize_url(source_url):
        urls.append(homepage)
    parser = parse_page(html)
    for href, anchor in parser.links:
        context = f"{href} {anchor}".lower()
        if not any(hint in context for hint in LINK_HINTS):
            continue
        url = normalize_url(urljoin(source_url, href))
        if same_site(url, root_domain):
            urls.append(url)
        if len(urls) >= max_pages:
            break
    seen: set[str] = set()
    output: list[str] = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            output.append(url)
    return output[:max_pages]


def scrape_domain_emails(row: dict[str, str], *, timeout: int, max_pages_per_domain: int) -> list[dict[str, str]]:
    root_domain = row.get("root_domain") or root_domain_from_url(row.get("result_url", ""))
    source_url = normalize_url(row.get("result_url", ""))
    if not root_domain or not source_url:
        return []
    first_page = fetch_html(source_url, timeout=timeout)
    urls = candidate_urls_from_page(root_domain, source_url, first_page.html, max_pages_per_domain)
    rows: list[dict[str, str]] = []
    scraped_at = now_iso()
    seen: set[tuple[str, str]] = set()
    for url in urls:
        page = first_page if normalize_url(url) == normalize_url(source_url) else fetch_html(url, timeout=timeout)
        if not page.html:
            continue
        mailto = emails_from_mailto(page.html)
        visible = emails_from_html(page.html)
        page_type = page_type_for_url(page.final_url or url)
        for email in sorted(mailto | visible):
            if not email_allowed(email, root_domain) and not off_domain_business_email_allowed(email):
                continue
            key = (email, page.final_url or url)
            if key in seen:
                continue
            seen.add(key)
            email_type = classify_email_type(email)
            rows.append(
                {
                    "root_domain": root_domain,
                    "source_result_url": source_url,
                    "scraped_url": page.final_url or url,
                    "email": email,
                    "email_type": email_type,
                    "source_page_type": page_type,
                    "confidence": str(confidence(email, page_type, email in mailto)),
                    "matched_phrase": row.get("matched_phrase", ""),
                    "query": row.get("query", ""),
                    "scraped_at": scraped_at,
                }
            )
    return rows


def write_email_rows(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=EMAIL_SCRAPE_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def scrape_emails_from_harvest(config: EmailScrapeConfig) -> dict[str, object]:
    output = config.output or default_email_output_path()
    domain_rows = harvest_domain_rows(config.harvest_csvs)
    started = time.time()
    rows: list[dict[str, str]] = []
    errors = 0
    with ThreadPoolExecutor(max_workers=max(1, config.workers)) as executor:
        futures = {
            executor.submit(
                scrape_domain_emails,
                row,
                timeout=config.timeout,
                max_pages_per_domain=config.max_pages_per_domain,
            ): row
            for row in domain_rows
        }
        for future in as_completed(futures):
            try:
                rows.extend(future.result())
            except Exception:
                errors += 1
    rows.sort(key=lambda row: (row["root_domain"], row["email"], row["scraped_url"]))
    write_email_rows(output, rows)
    return {
        "output": str(output),
        "input_domains": len(domain_rows),
        "email_rows": len(rows),
        "unique_email_domains": len({row["root_domain"] for row in rows}),
        "errors": errors,
        "elapsed_seconds": round(time.time() - started, 2),
    }
