from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlparse

from .classifier import classify_niche, classify_opportunity, quality_tier, spam_reject_reason, why_relevant
from .db import connect, init_db
from .email_discovery import (
    CandidateEmail,
    candidate_page_urls,
    classify_email_type,
    confidence,
    email_allowed,
    emails_from_html,
    emails_from_mailto,
    page_type_for_url,
)
from .paths import QA_DIR, REVIEW_CSV, ensure_project_dirs
from .search import SearchResult, run_search
from .utils import now_iso, write_csv
from .web import fetch_html, parse_page, resolve_homepage, site_name_from_title


REVIEW_FIELDS = [
    "root_domain",
    "site_name",
    "source_url",
    "matched_phrase",
    "opportunity_type",
    "niche",
    "email",
    "email_type",
    "quality_tier",
    "why_relevant",
    "reject_reason",
]


@dataclass
class CrawledPage:
    root_domain: str
    url: str
    final_url: str
    status: str
    title: str
    matched_phrases: str
    opportunity_type: str
    has_media_kit: int
    has_write_for_us_page: int
    has_sponsored_post_language: int
    text_excerpt: str
    error_message: str
    crawled_at: str


@dataclass
class DomainDiscovery:
    root_domain: str
    site_name: str
    pages: list[CrawledPage]
    emails: list[CandidateEmail]
    combined_text: str


def save_search_results(results: list[SearchResult]) -> None:
    init_db()
    timestamp = now_iso()
    with connect() as connection:
        for result in results:
            connection.execute(
                """
                insert into guest_post_search_results (
                    search_engine, source_query, result_url, root_domain, title, snippet, matched_phrase, raw_cache_path, fetched_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(search_engine, source_query, result_url) do update set
                    root_domain = excluded.root_domain,
                    title = excluded.title,
                    snippet = excluded.snippet,
                    matched_phrase = excluded.matched_phrase,
                    raw_cache_path = excluded.raw_cache_path,
                    fetched_at = excluded.fetched_at
                """,
                (
                    result.search_engine,
                    result.source_query,
                    result.result_url,
                    result.root_domain,
                    result.title,
                    result.snippet,
                    result.matched_phrase,
                    result.raw_cache_path,
                    timestamp,
                ),
            )
            connection.execute(
                """
                insert into guest_post_prospects (
                    root_domain, site_name, url_found, source_url, matched_phrase, source_query, scraped_at, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(root_domain) do update set
                    url_found = case when guest_post_prospects.url_found = '' then excluded.url_found else guest_post_prospects.url_found end,
                    source_url = case when guest_post_prospects.source_url = '' then excluded.source_url else guest_post_prospects.source_url end,
                    matched_phrase = case when guest_post_prospects.matched_phrase = '' then excluded.matched_phrase else guest_post_prospects.matched_phrase end,
                    source_query = case when guest_post_prospects.source_query = '' then excluded.source_query else guest_post_prospects.source_query end,
                    updated_at = excluded.updated_at
                """,
                (
                    result.root_domain,
                    result.title,
                    result.result_url,
                    result.result_url,
                    result.matched_phrase,
                    result.source_query,
                    timestamp,
                    timestamp,
                ),
            )
        connection.commit()


def scrape_domains(search_limit: int, pages_per_query: int, refresh: bool, delay_seconds: float) -> dict[str, Any]:
    ensure_project_dirs()
    results = run_search(
        search_limit=search_limit,
        pages_per_query=pages_per_query,
        refresh=refresh,
        delay_seconds=delay_seconds,
    )
    save_search_results(results)
    return {
        "search_results": len(results),
        "unique_domains": len({result.root_domain for result in results}),
        "database": "data/guest_post_prospecting.db",
    }


def prospect_rows(limit: int = 0) -> list[dict[str, Any]]:
    init_db()
    sql = """
        select root_domain, source_url
        from guest_post_prospects
        where root_domain not in (select root_domain from guest_post_suppression)
        order by root_domain
    """
    params: tuple[Any, ...] = ()
    if limit:
        sql += " limit ?"
        params = (limit,)
    with connect() as connection:
        return [dict(row) for row in connection.execute(sql, params).fetchall()]


def discover_domain(row: dict[str, Any], *, max_pages: int, timeout: int) -> DomainDiscovery:
    root_domain = row["root_domain"]
    seed_url = row.get("source_url") or f"https://{root_domain}/"
    homepage = resolve_homepage(root_domain, timeout=timeout)
    homepage_url = homepage.final_url or f"https://{root_domain}/"
    homepage_html = homepage.html
    pages_to_fetch = candidate_page_urls(root_domain, seed_url, homepage_url, homepage_html)[:max_pages]
    pages: list[CrawledPage] = []
    emails_by_key: dict[tuple[str, str], CandidateEmail] = {}
    text_parts: list[str] = []
    site_name = ""
    timestamp = now_iso()

    for url in pages_to_fetch:
        page = homepage if url == homepage_url else fetch_html(url, timeout=timeout)
        html = page.html
        parser = parse_page(html)
        text = parser.visible_text
        if parser.title and not site_name:
            site_name = site_name_from_title(parser.title, root_domain)
        text_parts.append(f"{url} {parser.title} {text[:5000]}")
        match = classify_opportunity(text, page.final_url or url)
        pages.append(
            CrawledPage(
                root_domain=root_domain,
                url=url,
                final_url=page.final_url,
                status=page.status,
                title=parser.title[:250],
                matched_phrases=match.matched_phrase,
                opportunity_type=match.opportunity_type,
                has_media_kit=int(match.has_media_kit),
                has_write_for_us_page=int(match.has_write_for_us_page),
                has_sponsored_post_language=int(match.has_sponsored_post_language),
                text_excerpt=text[:1000],
                error_message=page.error_message,
                crawled_at=timestamp,
            )
        )
        mailto = emails_from_mailto(html)
        visible = emails_from_html(html)
        source_page_type = page_type_for_url(page.final_url or url)
        for email in sorted(mailto | visible):
            if not email_allowed(email, root_domain):
                continue
            item = CandidateEmail(
                email=email,
                email_type=classify_email_type(email),
                source_url=page.final_url or url,
                source_page_type=source_page_type,
                confidence=confidence(email, source_page_type, email in mailto),
            )
            key = (item.email, item.source_url)
            previous = emails_by_key.get(key)
            if previous is None or item.confidence > previous.confidence:
                emails_by_key[key] = item

    if not site_name:
        site_name = site_name_from_title("", root_domain)
    return DomainDiscovery(
        root_domain=root_domain,
        site_name=site_name,
        pages=pages,
        emails=sorted(emails_by_key.values(), key=lambda item: (-item.confidence, item.email)),
        combined_text=" ".join(text_parts),
    )


def save_domain_discovery(discovery: DomainDiscovery) -> None:
    timestamp = now_iso()
    with connect() as connection:
        for page in discovery.pages:
            data = asdict(page)
            connection.execute(
                """
                insert into guest_post_crawl_pages (
                    root_domain, url, final_url, status, title, matched_phrases, opportunity_type, has_media_kit,
                    has_write_for_us_page, has_sponsored_post_language, text_excerpt, error_message, crawled_at
                ) values (
                    :root_domain, :url, :final_url, :status, :title, :matched_phrases, :opportunity_type, :has_media_kit,
                    :has_write_for_us_page, :has_sponsored_post_language, :text_excerpt, :error_message, :crawled_at
                )
                on conflict(root_domain, url) do update set
                    final_url = excluded.final_url,
                    status = excluded.status,
                    title = excluded.title,
                    matched_phrases = excluded.matched_phrases,
                    opportunity_type = excluded.opportunity_type,
                    has_media_kit = excluded.has_media_kit,
                    has_write_for_us_page = excluded.has_write_for_us_page,
                    has_sponsored_post_language = excluded.has_sponsored_post_language,
                    text_excerpt = excluded.text_excerpt,
                    error_message = excluded.error_message,
                    crawled_at = excluded.crawled_at
                """,
                data,
            )
        for email in discovery.emails:
            connection.execute(
                """
                insert into guest_post_contact_emails (
                    root_domain, email, email_type, email_source_url, email_source_method, source_page_type, confidence, discovered_at
                ) values (?, ?, ?, ?, 'page_scrape', ?, ?, ?)
                on conflict(root_domain, email, email_source_method) do update set
                    email_type = excluded.email_type,
                    email_source_url = excluded.email_source_url,
                    source_page_type = excluded.source_page_type,
                    confidence = excluded.confidence,
                    discovered_at = excluded.discovered_at
                """,
                (
                    discovery.root_domain,
                    email.email,
                    email.email_type,
                    email.source_url,
                    email.source_page_type,
                    email.confidence,
                    timestamp,
                ),
            )
        connection.execute(
            """
            update guest_post_prospects
            set site_name = case when ? != '' then ? else site_name end,
                updated_at = ?
            where root_domain = ?
            """,
            (discovery.site_name, discovery.site_name, timestamp, discovery.root_domain),
        )
        connection.commit()


def discover_emails(workers: int, limit: int, max_pages: int, timeout: int) -> dict[str, Any]:
    init_db()
    rows = prospect_rows(limit=limit)
    completed = 0
    errors: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {executor.submit(discover_domain, row, max_pages=max_pages, timeout=timeout): row for row in rows}
        for future in as_completed(futures):
            row = futures[future]
            try:
                discovery = future.result()
                save_domain_discovery(discovery)
            except Exception as exc:
                errors.append({"root_domain": row["root_domain"], "error": str(exc)})
            completed += 1
    return {"domains_attempted": len(rows), "domains_completed": completed, "errors": errors[:20]}


def best_contact_for_domain(root_domain: str, connection: Any) -> dict[str, Any]:
    row = connection.execute(
        """
        select *
        from guest_post_contact_emails
        where root_domain = ?
        order by
            case email_type
                when 'editor' then 1
                when 'content' then 2
                when 'advertising' then 3
                when 'media' then 4
                when 'partnerships' then 5
                when 'sponsored' then 6
                when 'generic' then 9
                else 10
            end,
            confidence desc,
            email
        limit 1
        """,
        (root_domain,),
    ).fetchone()
    return dict(row) if row else {}


def classify_prospects() -> dict[str, Any]:
    init_db()
    timestamp = now_iso()
    changed = 0
    with connect() as connection:
        prospects = [dict(row) for row in connection.execute("select * from guest_post_prospects order by root_domain").fetchall()]
        for prospect in prospects:
            root_domain = prospect["root_domain"]
            pages = [
                dict(row)
                for row in connection.execute(
                    "select * from guest_post_crawl_pages where root_domain = ? order by has_write_for_us_page desc, has_media_kit desc, url",
                    (root_domain,),
                ).fetchall()
            ]
            page_text = " ".join(f"{page.get('url','')} {page.get('title','')} {page.get('text_excerpt','')}" for page in pages)
            source_context = f"{prospect.get('source_url','')} {prospect.get('matched_phrase','')} {page_text}"
            match = classify_opportunity(source_context, prospect.get("source_url", ""))
            best_email = best_contact_for_domain(root_domain, connection)
            best_page = pages[0] if pages else {}
            site_name = prospect.get("site_name") or site_name_from_title(str(best_page.get("title") or ""), root_domain)
            niche = classify_niche(source_context)
            reject = spam_reject_reason(source_context, root_domain)
            if not best_email:
                reject = reject or "no_usable_business_email"
            tier = quality_tier(
                opportunity_type=match.opportunity_type,
                best_email_type=str(best_email.get("email_type") or ""),
                has_clear_opportunity_page=bool(match.matched_phrase),
                reject_reason=reject,
            )
            connection.execute(
                """
                update guest_post_prospects
                set site_name = ?,
                    source_url = case when source_url = '' and ? != '' then ? else source_url end,
                    matched_phrase = case when ? != '' then ? else matched_phrase end,
                    opportunity_type = ?,
                    niche = ?,
                    domain_type = ?,
                    contact_email = ?,
                    contact_page = ?,
                    has_media_kit = ?,
                    has_write_for_us_page = ?,
                    has_sponsored_post_language = ?,
                    estimated_quality = ?,
                    quality_tier = ?,
                    spam_risk = ?,
                    why_relevant = ?,
                    reject_reason = ?,
                    updated_at = ?
                where root_domain = ?
                """,
                (
                    site_name,
                    best_page.get("final_url", ""),
                    best_page.get("final_url", ""),
                    match.matched_phrase,
                    match.matched_phrase,
                    match.opportunity_type,
                    niche,
                    "publisher_or_content_site" if match.opportunity_type != "unclear_contact_only" else "unclear_contact_site",
                    best_email.get("email", ""),
                    best_email.get("email_source_url", ""),
                    int(any(page.get("has_media_kit") for page in pages) or match.has_media_kit),
                    int(any(page.get("has_write_for_us_page") for page in pages) or match.has_write_for_us_page),
                    int(any(page.get("has_sponsored_post_language") for page in pages) or match.has_sponsored_post_language),
                    tier,
                    tier,
                    "high" if reject in {"risky_niche", "low_quality_guest_post_marketplace_language"} else "",
                    why_relevant(site_name, niche, match.opportunity_type) if tier != "Reject" else "",
                    reject,
                    timestamp,
                    root_domain,
                ),
            )
            changed += 1
        connection.commit()
    return {"prospects_classified": changed}


def export_review_csv() -> dict[str, Any]:
    init_db()
    with connect() as connection:
        rows = [
            {
                "root_domain": row["root_domain"],
                "site_name": row["site_name"],
                "source_url": row["source_url"] or row["url_found"],
                "matched_phrase": row["matched_phrase"],
                "opportunity_type": row["opportunity_type"],
                "niche": row["niche"],
                "email": row["contact_email"],
                "email_type": (best_contact_for_domain(row["root_domain"], connection).get("email_type") or ""),
                "quality_tier": row["quality_tier"],
                "why_relevant": row["why_relevant"],
                "reject_reason": row["reject_reason"],
            }
            for row in connection.execute(
                """
                select *
                from guest_post_prospects
                order by
                    case quality_tier when 'A' then 1 when 'B' then 2 when 'C' then 3 else 4 end,
                    root_domain
                """
            ).fetchall()
        ]
    write_csv(REVIEW_CSV, rows, REVIEW_FIELDS)
    summary = {
        "output": str(REVIEW_CSV),
        "rows": len(rows),
        "quality_counts": {tier: sum(1 for row in rows if row["quality_tier"] == tier) for tier in ["A", "B", "C", "Reject", ""]},
    }
    QA_DIR.mkdir(parents=True, exist_ok=True)
    (QA_DIR / "au_guest_post_candidates_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary
