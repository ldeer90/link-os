from __future__ import annotations

import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus, urljoin, urlparse
from urllib.request import Request, urlopen

from .classifier import classify_niche, classify_opportunity, quality_tier, spam_reject_reason, why_relevant
from .db import connect, init_db
from .email_discovery import (
    CONTACT_PATHS,
    FREE_EMAIL_DOMAINS,
    classify_email_type,
    confidence,
    email_allowed,
    emails_from_html,
    emails_from_mailto,
    page_type_for_url,
)
from .paths import QA_DIR, REVIEW_CSV
from .search import SEARCH_CACHE_DIR, SearchResult, parse_search_results, run_search, search_bing, search_duckduckgo
from .utils import clean_domain, is_au_domain, normalize_url, now_iso, root_domain_from_url, safe_slug, write_csv
from .web import USER_AGENT, fetch_html, parse_page, same_site, site_name_from_title


CORE_PHRASES = [
    "write for us",
    "guest post",
    "guest article",
    "guest posting",
    "submit a guest post",
    "submit an article",
    "submit article",
    "submit your article",
    "contribute",
    "become a contributor",
    "become an author",
    "editorial guidelines",
    "contributor guidelines",
    "submission guidelines",
]
COMMERCIAL_PHRASES = [
    "advertise with us",
    "media kit",
    "media pack",
    "rate card",
    "advertising rates",
    "advertising",
    "sponsored post",
    "sponsored article",
    "sponsored posts",
    "sponsored content",
    "advertorial",
    "native advertising",
]
ROUTING_PHRASES = [
    "work with us",
    "partnerships",
    "brand collaborations",
    "collaborate with us",
    "partner with us",
    "pr friendly",
]
NICHES = [
    "travel",
    "parenting",
    "finance",
    "business",
    "lifestyle",
    "health",
    "food",
    "home",
    "tech",
    "local news",
    "construction",
    "education",
    "real estate",
    "automotive",
    "beauty",
    "fashion",
    "sports",
    "pets",
    "wedding",
    "startup",
    "marketing",
    "small business",
    "renovation",
    "music",
    "arts",
    "entertainment",
    "events",
    "gig guide",
    "culture",
]
PATH_PROBES = [
    "/write-for-us",
    "/guest-post",
    "/guest-posts",
    "/guest-article",
    "/submit-an-article",
    "/submit-article",
    "/submit",
    "/submissions",
    "/contribute",
    "/contributors",
    "/become-a-contributor",
    "/become-an-author",
    "/editorial-guidelines",
    "/contributor-guidelines",
    "/submission-guidelines",
    "/advertise",
    "/advertise-with-us",
    "/advertising",
    "/advertising-rates",
    "/media-kit",
    "/mediakit",
    "/media-pack",
    "/media",
    "/sponsored-posts",
    "/sponsored-post",
    "/sponsored-content",
    "/advertorial",
    "/advertising/",
    "/work-with-us",
    "/partnerships",
    "/collaborate",
    "/collaborate-with-us",
    "/pr-friendly",
    "/contact",
    "/contact-us",
    "/about",
]
SUPPLEMENTAL_PHRASES = [
    "guest blogger",
    "guest blogging",
    "guest contributor",
    "become a guest blogger",
    "guest post guidelines",
    "blog submission guidelines",
    "article submission guidelines",
    "submit your story",
    "submit a story",
    "share your story",
    "send us your story",
    "pitch us",
    "pitch an article",
    "pitch a story",
    "write for our blog",
    "contribute to our blog",
    "contributors wanted",
    "call for contributors",
    "looking for contributors",
    "sponsored content enquiry",
    "sponsored content enquiries",
    "sponsored content opportunities",
    "sponsorship opportunities",
    "advertising enquiry",
    "advertising enquiries",
    "advertising packages",
    "advertising opportunities",
    "media rates",
    "media guide",
    "media kit download",
    "download media kit",
    "brand partnership",
    "brand partnerships",
    "partnership opportunities",
    "collaboration enquiry",
    "collaboration enquiries",
    "work with brands",
]
SUPPLEMENTAL_NICHES = [
    "mums",
    "mums blog",
    "motherhood",
    "family magazine",
    "local mums",
    "kids activities",
    "women",
    "wellbeing",
    "mental health",
    "seniors",
    "retirement",
    "gardening",
    "interiors",
    "architecture",
    "sustainability",
    "eco",
    "outdoors",
    "adventure",
    "cycling",
    "running",
    "hospitality",
    "restaurants",
    "wine",
    "beer",
    "education",
    "schools",
    "students",
    "careers",
    "hr",
    "legal",
    "law",
    "accounting",
    "mortgage broker",
    "insurance",
    "property",
    "investing",
    "trades",
    "building",
    "solar",
    "electric vehicles",
    "startups",
    "saas",
    "digital marketing",
    "seo",
    "wordpress",
    "ecommerce",
]
SUPPLEMENTAL_PATH_STEMS = [
    "guest-blogger",
    "guest-blogging",
    "guest-contributor",
    "guest-contributors",
    "guest-post-guidelines",
    "blog-submission-guidelines",
    "article-submission-guidelines",
    "submit-your-story",
    "submit-a-story",
    "share-your-story",
    "pitch-us",
    "pitch-an-article",
    "pitch-a-story",
    "write-for-our-blog",
    "contribute-to-our-blog",
    "contributors-wanted",
    "call-for-contributors",
    "advertising-enquiry",
    "advertising-enquiries",
    "advertising-packages",
    "advertising-opportunities",
    "sponsored-content-enquiry",
    "sponsorship-opportunities",
    "media-rates",
    "media-guide",
    "download-media-kit",
    "brand-partnerships",
    "partnership-opportunities",
    "collaboration-enquiry",
    "partner-guest-posts",
]
AU_GAP_PHRASES = [
    "write for us",
    "guest post",
    "guest posts",
    "guest article",
    "guest blogger",
    "guest contributor",
    "guest post guidelines",
    "submit a guest post",
    "submit an article",
    "submit your story",
    "pitch us",
    "pitch a story",
    "contribute to our blog",
    "editorial guidelines",
    "advertise with us",
    "advertising enquiry",
    "advertising packages",
    "media kit",
    "media kit download",
    "rate card",
    "sponsored post",
    "sponsored article",
    "sponsored content",
    "brand partnerships",
    "partnership opportunities",
    "collaboration enquiry",
]
AU_GAP_GEO_TERMS = [
    "Australia",
    "Australian",
    "Melbourne",
    "Sydney",
    "Brisbane",
    "Perth",
    "Adelaide",
    "Canberra",
    "Gold Coast",
    "Sunshine Coast",
    "regional Australia",
]
AU_GAP_PATH_STEMS = [
    "write-for-us",
    "guest-post",
    "guest-posts",
    "guest-blogger",
    "guest-contributor",
    "guest-post-guidelines",
    "submit-your-story",
    "pitch-us",
    "advertise-with-us",
    "advertising-enquiry",
    "media-kit",
    "sponsored-post",
    "sponsored-content",
    "brand-partnerships",
    "collaboration-enquiry",
]
NEGATIVE_SEARCH_FILTERS = "-jobs -job -career -careers -pdf -doc -docx -template -sample -examples"
RESOURCE_LINK_HINTS = (
    "blog",
    "resource",
    "resources",
    "links",
    "partners",
    "media",
    "magazine",
    "news",
    "directory",
)


def off_domain_business_email_allowed(email: str) -> bool:
    if not email or "@" not in email:
        return False
    email_type = classify_email_type(email)
    if not email_type:
        return False
    domain = email.rsplit("@", 1)[-1].lower()
    if domain in FREE_EMAIL_DOMAINS:
        return False
    return domain.endswith(".com.au")

UNIVERSE_DOMAIN_FIELDS = [
    "root_domain",
    "quality_tier",
    "site_name",
    "best_evidence_url",
    "matched_phrase",
    "opportunity_type",
    "niche",
    "contact_email",
    "email_type",
    "why_relevant",
    "reject_reason",
    "candidate_url_count",
    "confirmed_evidence_count",
    "first_source_type",
    "first_source_query",
    "first_seen_at",
    "last_seen_at",
]

UNIVERSE_EVIDENCE_FIELDS = [
    "root_domain",
    "evidence_url",
    "source_type",
    "source_query",
    "matched_phrase",
    "opportunity_type",
    "confidence_score",
    "crawl_timestamp",
]


@dataclass(frozen=True)
class CommonCrawlRecord:
    url: str
    root_domain: str
    source_query: str
    raw_index: str
    matched_phrase: str


def generate_search_queries() -> list[str]:
    queries: list[str] = []
    all_phrases = CORE_PHRASES + COMMERCIAL_PHRASES + ROUTING_PHRASES
    for phrase in CORE_PHRASES + COMMERCIAL_PHRASES:
        queries.extend(
            [
                f'site:.com.au "{phrase}"',
                f'site:.com.au "{phrase}" {NEGATIVE_SEARCH_FILTERS}',
                f'site:.com.au intitle:"{phrase}"',
                f'site:.com.au intext:"{phrase}"',
                f'site:.com.au allintitle:{phrase}',
            ]
        )
    for phrase in ROUTING_PHRASES:
        queries.extend(
            [
                f'site:.com.au "{phrase}" blog',
                f'site:.com.au "{phrase}" "sponsored"',
                f'site:.com.au intitle:"{phrase}" blog',
            ]
        )
    for niche in NICHES:
        for phrase in all_phrases:
            if phrase in {"contribute", "partnerships"}:
                queries.append(f'site:.com.au "{phrase}" "{niche}"')
            else:
                queries.append(f'site:.com.au "{phrase}" {niche}')
        queries.extend(
            [
                f'site:.com.au "{niche} blog" "write for us"',
                f'site:.com.au "{niche} magazine" "media kit"',
                f'site:.com.au "{niche}" "advertise with us"',
            ]
        )
    for path in PATH_PROBES:
        stem = path.strip("/").replace("-", " ")
        path_part = path.strip("/")
        queries.extend(
            [
                f'site:.com.au inurl:{path_part} "{stem}"',
                f"site:.com.au allinurl:{path_part}",
                f'site:.com.au inurl:{path_part} "contact"',
            ]
        )
    for phrase in SUPPLEMENTAL_PHRASES:
        queries.extend(
            [
                f'site:.com.au "{phrase}"',
                f'site:.com.au "{phrase}" {NEGATIVE_SEARCH_FILTERS}',
                f'site:.com.au intitle:"{phrase}"',
                f'site:.com.au intext:"{phrase}"',
            ]
        )
    for niche in SUPPLEMENTAL_NICHES:
        for phrase in [
            "write for us",
            "guest post",
            "guest blogger",
            "submit your story",
            "pitch us",
            "advertise with us",
            "media kit",
            "sponsored content",
            "brand partnerships",
        ]:
            queries.append(f'site:.com.au "{phrase}" "{niche}"')
    for stem in SUPPLEMENTAL_PATH_STEMS:
        readable = stem.replace("-", " ")
        queries.extend(
            [
                f'site:.com.au inurl:{stem}',
                f'site:.com.au inurl:{stem} "{readable}"',
                f'site:.com.au inurl:{stem} "contact"',
                f"site:.com.au allinurl:{stem}",
            ]
        )
    for phrase in AU_GAP_PHRASES:
        queries.extend(
            [
                f'site:.au "{phrase}"',
                f'site:.au intitle:"{phrase}"',
                f'site:.au intext:"{phrase}"',
            ]
        )
        for geo in AU_GAP_GEO_TERMS[:2]:
            queries.append(f'site:.com "{phrase}" "{geo}"')
        for geo in AU_GAP_GEO_TERMS[2:]:
            queries.append(f'site:.com "{phrase}" {geo}')
    for stem in AU_GAP_PATH_STEMS:
        readable = stem.replace("-", " ")
        queries.extend(
            [
                f"site:.au inurl:{stem}",
                f'site:.au inurl:{stem} "{readable}"',
                f'site:.com inurl:{stem} Australia',
                f'site:.com inurl:{stem} Australian',
            ]
        )
    seen: set[str] = set()
    output: list[str] = []
    for query in queries:
        if query not in seen:
            seen.add(query)
            output.append(query)
    return output


def start_run(source_type: str, parameters: dict[str, object]) -> str:
    init_db()
    run_id = f"{source_type}_{uuid.uuid4().hex[:12]}"
    with connect() as connection:
        connection.execute(
            """
            insert into discovery_runs (id, source_type, status, parameters_json, started_at)
            values (?, ?, 'running', ?, ?)
            """,
            (run_id, source_type, json.dumps(parameters, sort_keys=True), now_iso()),
        )
        connection.commit()
    return run_id


def finish_run(run_id: str, status: str = "completed", notes: str = "") -> None:
    with connect() as connection:
        connection.execute(
            "update discovery_runs set status = ?, finished_at = ?, notes = ? where id = ?",
            (status, now_iso(), notes, run_id),
        )
        connection.commit()


def record_source(
    *,
    run_id: str,
    source_type: str,
    source_key: str,
    source_query: str = "",
    status: str = "",
    result_count: int = 0,
    cache_path: str = "",
    error_message: str = "",
) -> None:
    with connect() as connection:
        connection.execute(
            """
            insert into discovery_sources (
                discovery_run_id, source_type, source_key, source_query, fetched_at, status, result_count, cache_path, error_message
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(discovery_run_id, source_type, source_key) do update set
                fetched_at = excluded.fetched_at,
                status = excluded.status,
                result_count = excluded.result_count,
                cache_path = excluded.cache_path,
                error_message = excluded.error_message
            """,
            (run_id, source_type, source_key, source_query, now_iso(), status, result_count, cache_path, error_message),
        )
        connection.commit()


def upsert_candidate_url(
    *,
    url: str,
    source_type: str,
    source_query: str = "",
    run_id: str = "",
    matched_phrase: str = "",
    title: str = "",
    status: str = "new",
    evidence_score: int = 0,
) -> bool:
    normalized = normalize_url(url)
    root_domain = clean_domain(root_domain_from_url(normalized))
    if not normalized or not is_au_domain(root_domain):
        return False
    timestamp = now_iso()
    with connect() as connection:
        connection.execute(
            """
            insert into candidate_urls (
                url, root_domain, source_type, source_query, source_run_id, matched_phrase, title,
                discovered_at, last_seen_at, status, evidence_score
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(url) do update set
                last_seen_at = excluded.last_seen_at,
                evidence_score = max(candidate_urls.evidence_score, excluded.evidence_score),
                matched_phrase = case when candidate_urls.matched_phrase = '' then excluded.matched_phrase else candidate_urls.matched_phrase end,
                title = case when candidate_urls.title = '' then excluded.title else candidate_urls.title end
            """,
            (normalized, root_domain, source_type, source_query, run_id, matched_phrase, title, timestamp, timestamp, status, evidence_score),
        )
        connection.execute(
            """
            insert into candidate_domains (
                root_domain, first_source_type, first_source_query, first_seen_at, last_seen_at,
                candidate_url_count, updated_at
            ) values (?, ?, ?, ?, ?, 1, ?)
            on conflict(root_domain) do update set
                last_seen_at = excluded.last_seen_at,
                candidate_url_count = (
                    select count(*) from candidate_urls where candidate_urls.root_domain = excluded.root_domain
                ),
                updated_at = excluded.updated_at
            """,
            (root_domain, source_type, source_query, timestamp, timestamp, timestamp),
        )
        connection.commit()
    return True


def enqueue_url(url: str, root_domain: str | None = None, *, reason: str = "", priority: int = 50) -> bool:
    normalized = normalize_url(url)
    domain = clean_domain(root_domain or root_domain_from_url(normalized))
    if not normalized or not is_au_domain(domain):
        return False
    timestamp = now_iso()
    with connect() as connection:
        connection.execute(
            """
            insert into crawl_queue (url, root_domain, reason, priority, status, created_at, updated_at)
            values (?, ?, ?, ?, 'pending', ?, ?)
            on conflict(url) do update set
                priority = min(crawl_queue.priority, excluded.priority),
                reason = case when crawl_queue.reason = '' then excluded.reason else crawl_queue.reason end,
                updated_at = excluded.updated_at
            """,
            (normalized, domain, reason, priority, timestamp, timestamp),
        )
        connection.commit()
    return True


def import_seed_domains(items: list[str], *, source_label: str = "manual_seed") -> dict[str, object]:
    run_id = start_run("manual_seed", {"items": len(items), "source_label": source_label})
    saved = 0
    queued = 0
    for item in items:
        raw = item.strip()
        if not raw:
            continue
        if raw.startswith(("http://", "https://")):
            url = normalize_url(raw)
            domain = clean_domain(root_domain_from_url(url))
        else:
            domain = clean_domain(raw)
            url = f"https://{domain}/"
        if not is_au_domain(domain):
            continue
        match = classify_opportunity(source_label, url)
        if upsert_candidate_url(
            url=url,
            source_type="manual_seed",
            source_query=source_label,
            run_id=run_id,
            matched_phrase=match.matched_phrase,
            title=domain,
            evidence_score=40,
        ):
            saved += 1
        if enqueue_url(url, domain, reason=source_label, priority=5):
            queued += 1
        for path in PATH_PROBES:
            probe_url = f"https://{domain}{path}"
            probe_match = classify_opportunity(path, probe_url)
            if upsert_candidate_url(
                url=probe_url,
                source_type="manual_seed_path_probe",
                source_query=source_label,
                run_id=run_id,
                matched_phrase=probe_match.matched_phrase,
                title=domain,
                evidence_score=12,
            ):
                saved += 1
            if enqueue_url(probe_url, domain, reason=f"{source_label}:path_probe", priority=8):
                queued += 1
    record_source(run_id=run_id, source_type="manual_seed", source_key=source_label, source_query=source_label, status="ok", result_count=saved)
    finish_run(run_id)
    return {"discovery_run_id": run_id, "input_items": len(items), "saved_candidate_urls": saved, "queued_urls": queued}


def enqueue_candidate_urls(limit: int = 0) -> int:
    sql = """
        select url, root_domain, matched_phrase
        from candidate_urls
        order by evidence_score desc, discovered_at
    """
    params: tuple[object, ...] = ()
    if limit:
        sql += " limit ?"
        params = (limit,)
    count = 0
    with connect() as connection:
        rows = [dict(row) for row in connection.execute(sql, params).fetchall()]
    for row in rows:
        if enqueue_url(row["url"], row["root_domain"], reason=f"candidate:{row.get('matched_phrase') or 'source'}", priority=25):
            count += 1
    return count


def discover_from_search(
    *,
    search_limit: int,
    pages_per_query: int,
    refresh: bool,
    delay_seconds: float,
    query_limit: int = 0,
    query_offset: int = 0,
    timeout: int = 6,
) -> dict[str, object]:
    queries = generate_search_queries()
    if query_offset:
        queries = queries[query_offset:]
    if query_limit:
        queries = queries[:query_limit]
    run_id = start_run(
        "search",
        {
            "search_limit": search_limit,
            "pages_per_query": pages_per_query,
            "refresh": refresh,
            "delay_seconds": delay_seconds,
            "query_offset": query_offset,
            "query_count": len(queries),
            "timeout": timeout,
        },
    )
    try:
        saved = 0
        total_results = 0
        seen_domains: set[str] = set()
        for query in queries:
            if search_limit and saved >= search_limit:
                break
            try:
                results = search_bing(query, pages=pages_per_query, refresh=refresh, delay_seconds=delay_seconds, timeout=timeout)
                if not results:
                    results = search_duckduckgo(query, pages=pages_per_query, refresh=refresh, delay_seconds=delay_seconds, timeout=timeout)
                query_saved = 0
                for result in results:
                    if result.root_domain in seen_domains:
                        continue
                    seen_domains.add(result.root_domain)
                    total_results += 1
                    if upsert_candidate_url(
                        url=result.result_url,
                        source_type="search",
                        source_query=result.source_query,
                        run_id=run_id,
                        matched_phrase=result.matched_phrase,
                        title=result.title,
                        evidence_score=35 if result.matched_phrase else 20,
                    ):
                        enqueue_url(result.result_url, result.root_domain, reason="search_result", priority=20)
                        saved += 1
                        query_saved += 1
                    if search_limit and saved >= search_limit:
                        break
                record_source(run_id=run_id, source_type="search", source_key=query, source_query=query, status="ok", result_count=query_saved)
            except Exception as exc:
                record_source(run_id=run_id, source_type="search", source_key=query, source_query=query, status="error", error_message=str(exc))
        finish_run(run_id)
        return {"discovery_run_id": run_id, "queries": len(queries), "results": total_results, "saved_candidate_urls": saved}
    except Exception as exc:
        finish_run(run_id, "failed", str(exc))
        raise


def import_cached_search_results(*, limit_files: int = 0) -> dict[str, object]:
    init_db()
    cache_files = sorted(SEARCH_CACHE_DIR.glob("*.html"), key=lambda path: path.stat().st_mtime, reverse=True)
    if limit_files:
        cache_files = cache_files[:limit_files]
    run_id = start_run("search_cache_import", {"limit_files": limit_files, "cache_files": len(cache_files)})
    saved = 0
    parsed_results = 0
    try:
        for path in cache_files:
            name = path.name
            engine = "duckduckgo" if name.startswith("duckduckgo_") else "bing" if name.startswith("bing_") else "search_cache"
            source_query = path.stem
            try:
                html = path.read_text(encoding="utf-8", errors="replace")
                results = parse_search_results(html, engine, source_query, path)
                file_saved = 0
                for result in results:
                    parsed_results += 1
                    if upsert_candidate_url(
                        url=result.result_url,
                        source_type="search_cache",
                        source_query=source_query,
                        run_id=run_id,
                        matched_phrase=result.matched_phrase,
                        title=result.title,
                        evidence_score=25 if result.matched_phrase else 12,
                    ):
                        enqueue_url(result.result_url, result.root_domain, reason="search_cache", priority=28)
                        saved += 1
                        file_saved += 1
                record_source(
                    run_id=run_id,
                    source_type="search_cache",
                    source_key=str(path),
                    source_query=source_query,
                    status="ok",
                    result_count=file_saved,
                    cache_path=str(path),
                )
            except Exception as exc:
                record_source(
                    run_id=run_id,
                    source_type="search_cache",
                    source_key=str(path),
                    source_query=source_query,
                    status="error",
                    cache_path=str(path),
                    error_message=str(exc),
                )
        finish_run(run_id)
        return {
            "discovery_run_id": run_id,
            "cache_files": len(cache_files),
            "parsed_results": parsed_results,
            "saved_candidate_urls": saved,
        }
    except Exception as exc:
        finish_run(run_id, "failed", str(exc))
        raise


def common_crawl_patterns() -> list[str]:
    patterns: list[str] = []
    for path in PATH_PROBES:
        clean = path.strip("/")
        patterns.extend([f"*.com.au/{clean}*", f"*.com.au/*/{clean}*"])
    seen: set[str] = set()
    output: list[str] = []
    for pattern in patterns:
        if pattern not in seen:
            seen.add(pattern)
            output.append(pattern)
    return output


def latest_common_crawl_index(timeout: int = 20) -> str:
    request = Request("https://index.commoncrawl.org/collinfo.json", headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    for item in data:
        index_id = str(item.get("id") or "")
        if index_id:
            return index_id
    return "CC-MAIN-2026-18"


def parse_cdxj_lines(raw: str, source_query: str, index_id: str) -> list[CommonCrawlRecord]:
    records: list[CommonCrawlRecord] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        url = normalize_url(str(payload.get("url") or ""))
        domain = clean_domain(root_domain_from_url(url))
        if not is_au_domain(domain) or url in seen:
            continue
        seen.add(url)
        match = classify_opportunity(source_query, url)
        records.append(CommonCrawlRecord(url=url, root_domain=domain, source_query=source_query, raw_index=index_id, matched_phrase=match.matched_phrase))
    return records


def fetch_common_crawl_pattern(index_id: str, pattern: str, *, timeout: int = 40) -> str:
    url = f"https://index.commoncrawl.org/{quote_plus(index_id)}/index?url={quote_plus(pattern)}&output=json&filter=status:200"
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=timeout) as response:
        return response.read(5_000_000).decode("utf-8", errors="replace")


def discover_from_common_crawl(*, limit: int, index_id: str = "", pattern_limit: int = 0, delay_seconds: float = 1.0) -> dict[str, object]:
    resolved_index = index_id or latest_common_crawl_index()
    patterns = common_crawl_patterns()
    if pattern_limit:
        patterns = patterns[:pattern_limit]
    run_id = start_run("common_crawl", {"index_id": resolved_index, "limit": limit, "patterns": len(patterns), "delay_seconds": delay_seconds})
    saved = 0
    try:
        for pattern in patterns:
            if limit and saved >= limit:
                break
            try:
                if delay_seconds:
                    time.sleep(delay_seconds)
                raw = fetch_common_crawl_pattern(resolved_index, pattern)
                records = parse_cdxj_lines(raw, pattern, resolved_index)
                record_source(run_id=run_id, source_type="common_crawl", source_key=f"{resolved_index}:{pattern}", source_query=pattern, status="ok", result_count=len(records))
                for record in records:
                    if limit and saved >= limit:
                        break
                    if upsert_candidate_url(
                        url=record.url,
                        source_type="common_crawl",
                        source_query=record.source_query,
                        run_id=run_id,
                        matched_phrase=record.matched_phrase,
                        evidence_score=30 if record.matched_phrase else 15,
                    ):
                        enqueue_url(record.url, record.root_domain, reason="common_crawl", priority=30)
                        saved += 1
            except Exception as exc:
                record_source(run_id=run_id, source_type="common_crawl", source_key=f"{resolved_index}:{pattern}", source_query=pattern, status="error", error_message=str(exc))
        finish_run(run_id)
        return {"discovery_run_id": run_id, "index_id": resolved_index, "patterns": len(patterns), "saved_candidate_urls": saved}
    except Exception as exc:
        finish_run(run_id, "failed", str(exc))
        raise


def probe_candidate_paths(*, domain_limit: int = 0) -> dict[str, object]:
    run_id = start_run("path_probe", {"domain_limit": domain_limit, "path_count": len(PATH_PROBES)})
    sql = "select root_domain from candidate_domains order by confirmed_evidence_count desc, candidate_url_count desc, root_domain"
    params: tuple[object, ...] = ()
    if domain_limit:
        sql += " limit ?"
        params = (domain_limit,)
    with connect() as connection:
        domains = [row["root_domain"] for row in connection.execute(sql, params).fetchall()]
    saved = 0
    for domain in domains:
        for path in PATH_PROBES:
            url = f"https://{domain}{path}"
            match = classify_opportunity(path, url)
            if upsert_candidate_url(url=url, source_type="path_probe", source_query=path, run_id=run_id, matched_phrase=match.matched_phrase, evidence_score=10):
                saved += 1
            enqueue_url(url, domain, reason="path_probe", priority=35)
    record_source(run_id=run_id, source_type="path_probe", source_key="known_paths", result_count=saved, status="ok")
    finish_run(run_id)
    return {"discovery_run_id": run_id, "domains": len(domains), "queued_urls": saved}


def outward_links_from_page(base_url: str, root_domain: str, html: str) -> list[str]:
    parser = parse_page(html)
    output: list[str] = []
    seen: set[str] = set()
    for href, anchor in parser.links:
        if href.lower().startswith(("mailto:", "tel:", "#")):
            continue
        absolute = normalize_url(urljoin(base_url, href))
        parsed = urlparse(absolute)
        if parsed.scheme not in {"http", "https"}:
            continue
        domain = clean_domain(parsed.netloc)
        if not is_au_domain(domain) or domain == root_domain:
            continue
        context = f"{absolute} {anchor}".lower()
        if not any(hint in context for hint in RESOURCE_LINK_HINTS):
            continue
        if absolute not in seen:
            seen.add(absolute)
            output.append(absolute)
    return output


def crawl_and_score_universe(*, limit: int, retry_errors: bool, timeout: int, delay_seconds: float) -> dict[str, object]:
    run_id = start_run("crawl_score", {"limit": limit, "retry_errors": retry_errors, "timeout": timeout, "delay_seconds": delay_seconds})
    statuses = ("pending", "error") if retry_errors else ("pending",)
    placeholders = ",".join("?" for _ in statuses)
    sql = f"""
        select *
        from crawl_queue
        where status in ({placeholders}) and attempts < 3
        order by priority asc, updated_at asc
    """
    params: list[object] = list(statuses)
    if limit:
        sql += " limit ?"
        params.append(limit)
    with connect() as connection:
        queue = [dict(row) for row in connection.execute(sql, params).fetchall()]
    crawled = 0
    evidence_count = 0
    for item in queue:
        if delay_seconds:
            time.sleep(delay_seconds)
        url = item["url"]
        root_domain = item["root_domain"]
        timestamp = now_iso()
        page = fetch_html(url, timeout=timeout)
        parser = parse_page(page.html)
        text = parser.visible_text
        match = classify_opportunity(f"{url} {parser.title} {text}", page.final_url or url)
        niche = classify_niche(f"{parser.title} {text}")
        outbound = outward_links_from_page(page.final_url or url, root_domain, page.html)[:100] if page.html else []
        status = "done" if page.html else "error"
        confidence_score = 0
        if match.matched_phrase:
            confidence_score += 50
        if match.has_media_kit or match.has_write_for_us_page or match.has_sponsored_post_language:
            confidence_score += 20
        if match.opportunity_type in {"advertising_media_kit", "sponsored_post", "niche_edit_possible"}:
            confidence_score += 20
        if page.status.startswith("http_2"):
            confidence_score += 10
        with connect() as connection:
            connection.execute(
                """
                update crawl_queue
                set status = ?, attempts = attempts + 1, last_error = ?, updated_at = ?
                where url = ?
                """,
                (status, page.error_message, timestamp, url),
            )
            connection.execute(
                """
                insert into crawl_results (
                    url, root_domain, final_url, status, title, text_excerpt, matched_phrase, opportunity_type,
                    niche, outbound_links_json, error_message, crawled_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(url) do update set
                    final_url = excluded.final_url,
                    status = excluded.status,
                    title = excluded.title,
                    text_excerpt = excluded.text_excerpt,
                    matched_phrase = excluded.matched_phrase,
                    opportunity_type = excluded.opportunity_type,
                    niche = excluded.niche,
                    outbound_links_json = excluded.outbound_links_json,
                    error_message = excluded.error_message,
                    crawled_at = excluded.crawled_at
                """,
                (
                    url,
                    root_domain,
                    page.final_url,
                    page.status,
                    parser.title[:250],
                    text[:1200],
                    match.matched_phrase,
                    match.opportunity_type,
                    niche,
                    json.dumps(outbound),
                    page.error_message,
                    timestamp,
                ),
            )
            if match.matched_phrase and page.html:
                source_row = connection.execute("select source_type, source_query from candidate_urls where url = ?", (url,)).fetchone()
                source_type = source_row["source_type"] if source_row else "crawl"
                source_query = source_row["source_query"] if source_row else ""
                connection.execute(
                    """
                    insert into domain_evidence (
                        root_domain, evidence_url, source_type, source_query, matched_phrase, opportunity_type,
                        confidence_score, crawl_timestamp
                    ) values (?, ?, ?, ?, ?, ?, ?, ?)
                    on conflict(root_domain, evidence_url, matched_phrase, source_type) do update set
                        confidence_score = max(domain_evidence.confidence_score, excluded.confidence_score),
                        crawl_timestamp = excluded.crawl_timestamp
                    """,
                    (root_domain, page.final_url or url, source_type, source_query, match.matched_phrase, match.opportunity_type, confidence_score, timestamp),
                )
                evidence_count += 1
            for email in sorted(emails_from_mailto(page.html) | emails_from_html(page.html)):
                if not email_allowed(email, root_domain) and not off_domain_business_email_allowed(email):
                    continue
                email_type = classify_email_type(email)
                page_type = page_type_for_url(page.final_url or url)
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
                    (root_domain, email, email_type, page.final_url or url, page_type, confidence(email, page_type, email in emails_from_mailto(page.html)), timestamp),
                )
            connection.commit()
        crawled += 1
    rescore_domains()
    record_source(run_id=run_id, source_type="crawl_score", source_key="crawl_queue", status="ok", result_count=crawled)
    finish_run(run_id)
    return {"discovery_run_id": run_id, "crawled": crawled, "evidence_items": evidence_count}


def crawl_queue_rows(limit: int, retry_errors: bool) -> list[dict[str, object]]:
    statuses = ("pending", "error") if retry_errors else ("pending",)
    placeholders = ",".join("?" for _ in statuses)
    sql = f"""
        select *
        from crawl_queue
        where status in ({placeholders}) and attempts < 3
        order by priority asc, updated_at asc
    """
    params: list[object] = list(statuses)
    if limit:
        sql += " limit ?"
        params.append(limit)
    with connect() as connection:
        return [dict(row) for row in connection.execute(sql, params).fetchall()]


def fetch_crawl_item(item: dict[str, object], timeout: int) -> dict[str, object]:
    url = str(item["url"])
    root_domain = str(item["root_domain"])
    timestamp = now_iso()
    page = fetch_html(url, timeout=timeout)
    parser = parse_page(page.html)
    text = parser.visible_text
    match = classify_opportunity(f"{url} {parser.title} {text}", page.final_url or url)
    niche = classify_niche(f"{parser.title} {text}")
    outbound = outward_links_from_page(page.final_url or url, root_domain, page.html)[:100] if page.html else []
    confidence_score = 0
    if match.matched_phrase:
        confidence_score += 50
    if match.has_media_kit or match.has_write_for_us_page or match.has_sponsored_post_language:
        confidence_score += 20
    if match.opportunity_type in {"advertising_media_kit", "sponsored_post", "niche_edit_possible"}:
        confidence_score += 20
    if page.status.startswith("http_2"):
        confidence_score += 10
    mailto_emails = emails_from_mailto(page.html)
    visible_emails = emails_from_html(page.html)
    email_rows = []
    page_type = page_type_for_url(page.final_url or url)
    for email in sorted(mailto_emails | visible_emails):
        if not email_allowed(email, root_domain) and not off_domain_business_email_allowed(email):
            continue
        email_rows.append(
            {
                "email": email,
                "email_type": classify_email_type(email),
                "source_url": page.final_url or url,
                "source_page_type": page_type,
                "confidence": confidence(email, page_type, email in mailto_emails),
            }
        )
    return {
        "url": url,
        "root_domain": root_domain,
        "timestamp": timestamp,
        "page": page,
        "title": parser.title[:250],
        "text_excerpt": text[:1200],
        "matched_phrase": match.matched_phrase,
        "opportunity_type": match.opportunity_type,
        "niche": niche,
        "outbound": outbound,
        "confidence_score": confidence_score,
        "email_rows": email_rows,
        "queue_status": "done" if page.html else "error",
    }


def save_crawl_item_result(result: dict[str, object]) -> int:
    page = result["page"]
    assert hasattr(page, "html")
    evidence_count = 0
    with connect() as connection:
        connection.execute(
            """
            update crawl_queue
            set status = ?, attempts = attempts + 1, last_error = ?, updated_at = ?
            where url = ?
            """,
            (result["queue_status"], page.error_message, result["timestamp"], result["url"]),
        )
        connection.execute(
            """
            insert into crawl_results (
                url, root_domain, final_url, status, title, text_excerpt, matched_phrase, opportunity_type,
                niche, outbound_links_json, error_message, crawled_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(url) do update set
                final_url = excluded.final_url,
                status = excluded.status,
                title = excluded.title,
                text_excerpt = excluded.text_excerpt,
                matched_phrase = excluded.matched_phrase,
                opportunity_type = excluded.opportunity_type,
                niche = excluded.niche,
                outbound_links_json = excluded.outbound_links_json,
                error_message = excluded.error_message,
                crawled_at = excluded.crawled_at
            """,
            (
                result["url"],
                result["root_domain"],
                page.final_url,
                page.status,
                result["title"],
                result["text_excerpt"],
                result["matched_phrase"],
                result["opportunity_type"],
                result["niche"],
                json.dumps(result["outbound"]),
                page.error_message,
                result["timestamp"],
            ),
        )
        if result["matched_phrase"] and page.html:
            source_row = connection.execute("select source_type, source_query from candidate_urls where url = ?", (result["url"],)).fetchone()
            source_type = source_row["source_type"] if source_row else "crawl"
            source_query = source_row["source_query"] if source_row else ""
            connection.execute(
                """
                insert into domain_evidence (
                    root_domain, evidence_url, source_type, source_query, matched_phrase, opportunity_type,
                    confidence_score, crawl_timestamp
                ) values (?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(root_domain, evidence_url, matched_phrase, source_type) do update set
                    confidence_score = max(domain_evidence.confidence_score, excluded.confidence_score),
                    crawl_timestamp = excluded.crawl_timestamp
                """,
                (
                    result["root_domain"],
                    page.final_url or result["url"],
                    source_type,
                    source_query,
                    result["matched_phrase"],
                    result["opportunity_type"],
                    result["confidence_score"],
                    result["timestamp"],
                ),
            )
            evidence_count = 1
        for email_row in result["email_rows"]:
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
                    result["root_domain"],
                    email_row["email"],
                    email_row["email_type"],
                    email_row["source_url"],
                    email_row["source_page_type"],
                    email_row["confidence"],
                    result["timestamp"],
                ),
            )
        connection.commit()
    return evidence_count


def crawl_and_score_universe_parallel(*, limit: int, retry_errors: bool, timeout: int, workers: int) -> dict[str, object]:
    run_id = start_run("crawl_score_parallel", {"limit": limit, "retry_errors": retry_errors, "timeout": timeout, "workers": workers})
    queue = crawl_queue_rows(limit, retry_errors)
    crawled = 0
    evidence_count = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {executor.submit(fetch_crawl_item, item, timeout): item for item in queue}
        for future in as_completed(futures):
            item = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                with connect() as connection:
                    connection.execute(
                        """
                        update crawl_queue
                        set status = 'error', attempts = attempts + 1, last_error = ?, updated_at = ?
                        where url = ?
                        """,
                        (str(exc), now_iso(), item["url"]),
                    )
                    connection.commit()
                crawled += 1
                continue
            evidence_count += save_crawl_item_result(result)
            crawled += 1
    rescore_domains()
    record_source(run_id=run_id, source_type="crawl_score_parallel", source_key="crawl_queue", status="ok", result_count=crawled)
    finish_run(run_id)
    return {"discovery_run_id": run_id, "crawled": crawled, "evidence_items": evidence_count, "workers": workers}


def best_email(connection, root_domain: str, opportunity_type: str = "") -> dict[str, object]:
    if opportunity_type in {"advertising_media_kit", "sponsored_post", "niche_edit_possible"}:
        order = """
            case email_type
                when 'advertising' then 1
                when 'partnerships' then 2
                when 'media' then 3
                when 'sponsored' then 4
                when 'editor' then 5
                when 'content' then 6
                when 'generic' then 9
                else 10
            end
        """
    else:
        order = """
            case email_type
                when 'editor' then 1
                when 'content' then 2
                when 'advertising' then 3
                when 'media' then 4
                when 'partnerships' then 5
                when 'sponsored' then 6
                when 'generic' then 9
                else 10
            end
        """
    row = connection.execute(
        f"""
        select *
        from guest_post_contact_emails
        where root_domain = ?
        order by
            {order},
            confidence desc,
            email
        limit 1
        """,
        (root_domain,),
    ).fetchone()
    return dict(row) if row else {}


def rescore_domains() -> None:
    timestamp = now_iso()
    with connect() as connection:
        domains = [row["root_domain"] for row in connection.execute("select root_domain from candidate_domains").fetchall()]
        for domain in domains:
            evidence = connection.execute(
                """
                select *
                from domain_evidence
                where root_domain = ?
                order by confidence_score desc, crawl_timestamp desc
                limit 1
                """,
                (domain,),
            ).fetchone()
            evidence_count = connection.execute("select count(*) from domain_evidence where root_domain = ?", (domain,)).fetchone()[0]
            candidate_count = connection.execute("select count(*) from candidate_urls where root_domain = ?", (domain,)).fetchone()[0]
            if evidence:
                evidence_dict = dict(evidence)
                email = best_email(connection, domain, evidence_dict["opportunity_type"])
                crawl = connection.execute("select title, text_excerpt, niche from crawl_results where final_url = ? or url = ? limit 1", (evidence_dict["evidence_url"], evidence_dict["evidence_url"])).fetchone()
                text_context = f"{crawl['title'] if crawl else ''} {crawl['text_excerpt'] if crawl else ''}"
                reject = spam_reject_reason(text_context, domain)
                tier = quality_tier(
                    opportunity_type=evidence_dict["opportunity_type"],
                    best_email_type=str(email.get("email_type") or ""),
                    has_clear_opportunity_page=True,
                    reject_reason=reject,
                )
                if tier == "Reject" and not reject:
                    tier = "Unconfirmed"
                niche = (crawl["niche"] if crawl else "") or classify_niche(text_context)
                site_name = site_name_from_title(crawl["title"] if crawl else "", domain)
                connection.execute(
                    """
                    update candidate_domains
                    set candidate_url_count = ?,
                        confirmed_evidence_count = ?,
                        best_evidence_url = ?,
                        matched_phrase = ?,
                        opportunity_type = ?,
                        niche = ?,
                        quality_tier = ?,
                        contact_email = ?,
                        email_type = ?,
                        why_relevant = ?,
                        reject_reason = ?,
                        updated_at = ?
                    where root_domain = ?
                    """,
                    (
                        candidate_count,
                        evidence_count,
                        evidence_dict["evidence_url"],
                        evidence_dict["matched_phrase"],
                        evidence_dict["opportunity_type"],
                        niche,
                        tier,
                        email.get("email", ""),
                        email.get("email_type", ""),
                        why_relevant(site_name, niche, evidence_dict["opportunity_type"]) if tier in {"A", "B", "C"} else "",
                        reject,
                        timestamp,
                        domain,
                    ),
                )
            else:
                connection.execute(
                    """
                    update candidate_domains
                    set candidate_url_count = ?,
                        confirmed_evidence_count = 0,
                        quality_tier = case when quality_tier = 'Reject' then 'Reject' else 'Unconfirmed' end,
                        updated_at = ?
                    where root_domain = ?
                    """,
                    (candidate_count, timestamp, domain),
                )
        connection.commit()


def expand_from_confirmed_sites(*, domain_limit: int, per_domain_limit: int) -> dict[str, object]:
    run_id = start_run("recursive_expansion", {"domain_limit": domain_limit, "per_domain_limit": per_domain_limit})
    sql = """
        select cd.root_domain, cd.best_evidence_url, cr.outbound_links_json
        from candidate_domains cd
        left join crawl_results cr on cr.url = cd.best_evidence_url or cr.final_url = cd.best_evidence_url
        where cd.quality_tier in ('A','B','C')
        order by cd.confirmed_evidence_count desc, cd.root_domain
    """
    params: tuple[object, ...] = ()
    if domain_limit:
        sql += " limit ?"
        params = (domain_limit,)
    with connect() as connection:
        rows = [dict(row) for row in connection.execute(sql, params).fetchall()]
    saved = 0
    for row in rows:
        try:
            links = json.loads(row.get("outbound_links_json") or "[]")
        except json.JSONDecodeError:
            links = []
        for link in links[:per_domain_limit]:
            match = classify_opportunity(link, link)
            if upsert_candidate_url(url=link, source_type="recursive_expansion", source_query=row["root_domain"], run_id=run_id, matched_phrase=match.matched_phrase, evidence_score=12):
                enqueue_url(link, reason=f"expanded_from:{row['root_domain']}", priority=55)
                saved += 1
    record_source(run_id=run_id, source_type="recursive_expansion", source_key="outbound_links", status="ok", result_count=saved)
    finish_run(run_id)
    return {"discovery_run_id": run_id, "source_domains": len(rows), "saved_candidate_urls": saved}


def coverage_summary() -> dict[str, object]:
    rescore_domains()
    with connect() as connection:
        quality = {row["quality_tier"]: row["count"] for row in connection.execute("select quality_tier, count(*) count from candidate_domains group by quality_tier")}
        by_source = {row["source_type"]: row["count"] for row in connection.execute("select source_type, count(distinct root_domain) count from candidate_urls group by source_type")}
        by_phrase = {row["matched_phrase"]: row["count"] for row in connection.execute("select matched_phrase, count(distinct root_domain) count from domain_evidence where matched_phrase != '' group by matched_phrase order by count desc")}
        niches = {row["niche"]: row["count"] for row in connection.execute("select niche, count(*) count from candidate_domains where niche != '' group by niche order by count desc limit 20")}
        opportunities = {row["opportunity_type"]: row["count"] for row in connection.execute("select opportunity_type, count(*) count from candidate_domains where opportunity_type != '' group by opportunity_type order by count desc")}
        total_domains = connection.execute("select count(*) from candidate_domains").fetchone()[0]
        confirmed = connection.execute("select count(*) from candidate_domains where confirmed_evidence_count > 0").fetchone()[0]
        rejected = quality.get("Reject", 0)
        unconfirmed = quality.get("Unconfirmed", 0)
        overlap_rows = connection.execute(
            """
            select source_count, count(*) count
            from (
                select root_domain, count(distinct source_type) source_count
                from candidate_urls
                group by root_domain
            )
            group by source_count
            order by source_count
            """
        ).fetchall()
    summary = {
        "total_candidate_domains": total_domains,
        "confirmed_domains": confirmed,
        "quality_counts": quality,
        "domains_by_source": by_source,
        "confirmed_domains_by_phrase": by_phrase,
        "top_niches": niches,
        "opportunity_types": opportunities,
        "source_overlap": {str(row["source_count"]): row["count"] for row in overlap_rows},
        "unconfirmed_or_rejected_ratio": round((unconfirmed + rejected) / total_domains, 4) if total_domains else 0,
        "calculated_at": now_iso(),
    }
    with connect() as connection:
        for key, value in summary.items():
            connection.execute(
                """
                insert into coverage_metrics (metric_key, metric_value, calculated_at)
                values (?, ?, ?)
                on conflict(metric_key) do update set
                    metric_value = excluded.metric_value,
                    calculated_at = excluded.calculated_at
                """,
                (key, json.dumps(value, sort_keys=True), summary["calculated_at"]),
            )
        connection.commit()
    return summary


def export_universe() -> dict[str, object]:
    summary = coverage_summary()
    with connect() as connection:
        domain_rows = [
            dict(row)
            for row in connection.execute(
                """
                select
                    cd.root_domain,
                    cd.quality_tier,
                    coalesce((select title from crawl_results where root_domain = cd.root_domain and title != '' order by crawled_at desc limit 1), cd.root_domain) site_name,
                    cd.best_evidence_url,
                    cd.matched_phrase,
                    cd.opportunity_type,
                    cd.niche,
                    cd.contact_email,
                    cd.email_type,
                    cd.why_relevant,
                    cd.reject_reason,
                    cd.candidate_url_count,
                    cd.confirmed_evidence_count,
                    cd.first_source_type,
                    cd.first_source_query,
                    cd.first_seen_at,
                    cd.last_seen_at
                from candidate_domains cd
                order by
                    case cd.quality_tier when 'A' then 1 when 'B' then 2 when 'C' then 3 when 'Unconfirmed' then 4 else 5 end,
                    cd.root_domain
                """
            ).fetchall()
        ]
        evidence_rows = [dict(row) for row in connection.execute("select * from domain_evidence order by root_domain, confidence_score desc").fetchall()]
    review_rows = [
        {
            "root_domain": row["root_domain"],
            "site_name": row["site_name"],
            "source_url": row["best_evidence_url"],
            "matched_phrase": row["matched_phrase"],
            "opportunity_type": row["opportunity_type"],
            "niche": row["niche"],
            "email": row["contact_email"],
            "email_type": row["email_type"],
            "quality_tier": row["quality_tier"],
            "why_relevant": row["why_relevant"],
            "reject_reason": row["reject_reason"],
        }
        for row in domain_rows
        if row["quality_tier"] in {"A", "B", "C", "Reject", "Unconfirmed"}
    ]
    universe_path = REVIEW_CSV.parent / "au_guest_post_universe_domains.csv"
    evidence_path = REVIEW_CSV.parent / "au_guest_post_universe_evidence.csv"
    rejected_path = REVIEW_CSV.parent / "rejected_or_unconfirmed_domains.csv"
    summary_path = REVIEW_CSV.parent / "coverage_summary.json"
    write_csv(universe_path, domain_rows, UNIVERSE_DOMAIN_FIELDS)
    write_csv(evidence_path, evidence_rows, UNIVERSE_EVIDENCE_FIELDS)
    write_csv(rejected_path, [row for row in domain_rows if row["quality_tier"] in {"Reject", "Unconfirmed"}], UNIVERSE_DOMAIN_FIELDS)
    write_csv(REVIEW_CSV, review_rows, ["root_domain", "site_name", "source_url", "matched_phrase", "opportunity_type", "niche", "email", "email_type", "quality_tier", "why_relevant", "reject_reason"])
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    QA_DIR.mkdir(parents=True, exist_ok=True)
    (QA_DIR / "coverage_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "review_csv": str(REVIEW_CSV),
        "universe_domains_csv": str(universe_path),
        "universe_evidence_csv": str(evidence_path),
        "rejected_or_unconfirmed_csv": str(rejected_path),
        "coverage_summary": str(summary_path),
        "rows": len(domain_rows),
        "confirmed": summary["confirmed_domains"],
    }
