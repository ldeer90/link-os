from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, unquote, urlparse
from urllib.request import Request, urlopen

from .classifier import classify_opportunity
from .paths import SEARCH_CACHE_DIR
from .utils import clean_domain, is_australian_candidate_domain, normalize_url, root_domain_from_url, safe_slug
from .web import USER_AGENT


DEFAULT_QUERIES = [
    'site:.com.au "write for us"',
    'site:.com.au "guest post"',
    'site:.com.au "contribute"',
    'site:.com.au "submit an article"',
    'site:.com.au "advertise with us"',
    'site:.com.au "sponsored post"',
    'site:.com.au "media kit"',
    'site:.com.au "editorial guidelines"',
]

IGNORED_HOSTS = {
    "bing.com",
    "www.bing.com",
    "microsoft.com",
    "duckduckgo.com",
    "www.duckduckgo.com",
    "google.com",
    "www.google.com",
    "youtube.com",
    "www.youtube.com",
    "facebook.com",
    "www.facebook.com",
    "linkedin.com",
    "www.linkedin.com",
}


@dataclass(frozen=True)
class SearchResult:
    search_engine: str
    source_query: str
    result_url: str
    root_domain: str
    title: str
    snippet: str
    matched_phrase: str
    raw_cache_path: str


class AnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href = ""
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        for key, value in attrs:
            if key.lower() == "href" and value:
                self._href = value.strip()
                self._parts = []
                break

    def handle_data(self, data: str) -> None:
        if self._href:
            text = " ".join(data.split())
            if text:
                self._parts.append(text)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href:
            self.links.append((self._href, " ".join(self._parts).strip()))
            self._href = ""
            self._parts = []


def fetch_search_html(url: str, timeout: int = 20) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-AU,en;q=0.9",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read(2_000_000).decode(charset, errors="replace")


def cache_path(engine: str, query: str, page: int) -> Path:
    digest = hashlib.sha1(f"{engine}|{query}|{page}".encode("utf-8")).hexdigest()[:12]
    return SEARCH_CACHE_DIR / f"{safe_slug(engine)}_{safe_slug(query)[:80]}_{page}_{digest}.html"


def load_or_fetch(engine: str, query: str, page: int, url: str, *, refresh: bool, delay_seconds: float, timeout: int = 20) -> tuple[str, Path]:
    SEARCH_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = cache_path(engine, query, page)
    if path.exists() and not refresh:
        return path.read_text(encoding="utf-8", errors="replace"), path
    if delay_seconds:
        time.sleep(delay_seconds)
    html = fetch_search_html(url, timeout=timeout)
    path.write_text(html, encoding="utf-8")
    return html, path


def unwrap_result_url(href: str) -> str:
    if not href:
        return ""
    parsed = urlparse(href)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        return unquote(target)
    if parsed.netloc.endswith("google.com") and parsed.path.startswith("/url"):
        target = parse_qs(parsed.query).get("q", [""])[0]
        if target.startswith("http"):
            return unquote(target)
    if parsed.netloc.endswith("bing.com") and parsed.path.startswith("/ck/a"):
        params = parse_qs(parsed.query)
        for key in ("u", "url"):
            value = params.get(key, [""])[0]
            if value.startswith("a1"):
                # Bing sometimes prefixes base64-ish opaque values; direct decoding is not reliable.
                continue
            if value.startswith("http"):
                return unquote(value)
    if parsed.scheme in {"http", "https"}:
        return href
    return ""


def parse_search_results(html: str, engine: str, query: str, raw_cache_path: Path) -> list[SearchResult]:
    parser = AnchorParser()
    try:
        parser.feed(html or "")
    except Exception:
        pass
    results: list[SearchResult] = []
    seen_urls: set[str] = set()
    for href, anchor_text in parser.links:
        url = normalize_url(unwrap_result_url(href))
        if not url or url in seen_urls:
            continue
        domain = clean_domain(root_domain_from_url(url))
        if not is_australian_candidate_domain(domain, f"{query} {anchor_text} {url}"):
            continue
        if domain in IGNORED_HOSTS:
            continue
        seen_urls.add(url)
        match = classify_opportunity(f"{query} {anchor_text}", url)
        results.append(
            SearchResult(
                search_engine=engine,
                source_query=query,
                result_url=url,
                root_domain=domain,
                title=anchor_text[:250],
                snippet="",
                matched_phrase=match.matched_phrase,
                raw_cache_path=str(raw_cache_path),
            )
        )
    return results


def search_bing(query: str, *, pages: int, refresh: bool, delay_seconds: float, timeout: int = 20) -> list[SearchResult]:
    output: list[SearchResult] = []
    for page in range(pages):
        first = page * 50 + 1
        url = f"https://www.bing.com/search?q={quote_plus(query)}&count=50&first={first}"
        try:
            html, raw_path = load_or_fetch("bing", query, page + 1, url, refresh=refresh, delay_seconds=delay_seconds, timeout=timeout)
            output.extend(parse_search_results(html, "bing", query, raw_path))
        except Exception:
            continue
    return output


def search_duckduckgo(query: str, *, pages: int, refresh: bool, delay_seconds: float, timeout: int = 20) -> list[SearchResult]:
    # DuckDuckGo HTML paging is intentionally conservative here; one page per query is enough as a fallback.
    output: list[SearchResult] = []
    for page in range(min(pages, 1)):
        url = f"https://duckduckgo.com/html/?q={quote_plus(query)}"
        try:
            html, raw_path = load_or_fetch("duckduckgo", query, page + 1, url, refresh=refresh, delay_seconds=delay_seconds, timeout=timeout)
            output.extend(parse_search_results(html, "duckduckgo", query, raw_path))
        except Exception:
            continue
    return output


def search_google(query: str, *, pages: int, refresh: bool, delay_seconds: float, timeout: int = 20) -> list[SearchResult]:
    output: list[SearchResult] = []
    for page in range(pages):
        start = page * 10
        url = f"https://www.google.com/search?q={quote_plus(query)}&num=10&start={start}&hl=en-AU&pws=0"
        try:
            html, raw_path = load_or_fetch("google", query, page + 1, url, refresh=refresh, delay_seconds=delay_seconds, timeout=timeout)
            output.extend(parse_search_results(html, "google", query, raw_path))
        except Exception:
            continue
    return output


def run_search(
    *,
    queries: list[str] | None = None,
    search_limit: int = 250,
    pages_per_query: int = 2,
    refresh: bool = False,
    delay_seconds: float = 2.0,
    timeout: int = 20,
) -> list[SearchResult]:
    queries = queries or DEFAULT_QUERIES
    output: list[SearchResult] = []
    seen_domains: set[str] = set()
    for query in queries:
        engine_results = search_bing(query, pages=pages_per_query, refresh=refresh, delay_seconds=delay_seconds, timeout=timeout)
        if not engine_results:
            engine_results = search_duckduckgo(query, pages=pages_per_query, refresh=refresh, delay_seconds=delay_seconds, timeout=timeout)
        for result in engine_results:
            if result.root_domain in seen_domains:
                continue
            seen_domains.add(result.root_domain)
            output.append(result)
            if search_limit and len(output) >= search_limit:
                return output
    return output
