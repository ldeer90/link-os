from __future__ import annotations

import ipaddress
import re
import socket
import time
from collections import deque
from dataclasses import dataclass, field
from html import unescape
from typing import Any, Callable, Iterable
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import httpx
import tldextract
from bs4 import BeautifulSoup


USER_AGENT = "LinkOSPublicContactCrawler/1.0 (+https://ldsearch.com.au)"
EMAIL_PATTERN = re.compile(r"(?<![\w.+-])([a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+)", re.I)
OBFUSCATED_AT = re.compile(r"\s*(?:\[at\]|\(at\)|\sat\s)\s*", re.I)
OBFUSCATED_DOT = re.compile(r"\s*(?:\[dot\]|\(dot\)|\sdot\s)\s*", re.I)
BLOCKED_PATH = re.compile(r"/(?:admin|login|log-in|signin|sign-in|account|checkout|wp-admin|private)(?:/|$)", re.I)
BAD_LOCAL_PARTS = {"noreply", "no-reply", "donotreply", "do-not-reply", "mailer-daemon"}
BAD_EMAIL_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js")
SEED_PATHS = (
    "/",
    "/contact",
    "/contact-us",
    "/advertise",
    "/advertise-with-us",
    "/write-for-us",
    "/contribute",
    "/partnerships",
    "/media-kit",
    "/about",
)

_extract = tldextract.TLDExtract(suffix_list_urls=())


@dataclass(frozen=True)
class CrawlConfig:
    max_pages: int = 20
    timeout_seconds: float = 10.0
    max_attempts: int = 3
    host_delay_seconds: float = 1.0
    resolve_public_dns: bool = True
    max_response_bytes: int = 2_000_000
    max_runtime_seconds: float = 900.0


@dataclass(frozen=True)
class FetchResult:
    requested_url: str
    final_url: str
    status_code: int
    content_type: str
    text: str


@dataclass(frozen=True)
class EmailEvidence:
    email: str
    source_url: str
    evidence_text: str
    discovery_method: str
    rank: int


@dataclass(frozen=True)
class PageAttempt:
    url: str
    status: str
    status_code: int | None = None
    error: str = ""


@dataclass
class CrawlResult:
    domain: str
    status: str
    pages_attempted: int = 0
    pages_succeeded: int = 0
    contacts: list[EmailEvidence] = field(default_factory=list)
    attempts: list[PageAttempt] = field(default_factory=list)
    error: str = ""


Fetcher = Callable[[str, float, int], FetchResult]
RobotsCheck = Callable[[str], bool]
Sleeper = Callable[[float], None]
ProgressCallback = Callable[[dict[str, Any]], None]


def registrable_domain(value: str) -> str:
    raw = value.strip().lower()
    parsed = urlparse(raw if "://" in raw else f"//{raw}")
    host = (parsed.hostname or "").strip(".")
    if not host:
        return ""
    try:
        ipaddress.ip_address(host)
        return ""
    except ValueError:
        pass
    extracted = _extract(host)
    if not extracted.domain or not extracted.suffix:
        return ""
    return f"{extracted.domain}.{extracted.suffix}".encode("idna").decode("ascii")


def host_is_public(host: str) -> bool:
    try:
        addresses = {row[4][0] for row in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
    except OSError:
        return False
    if not addresses:
        return False
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            return False
    return True


def _normalized_page_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    path = parsed.path or "/"
    if BLOCKED_PATH.search(path):
        return ""
    return urlunparse((parsed.scheme, parsed.netloc.lower(), path, "", parsed.query, ""))


def _same_site(url: str, domain: str) -> bool:
    host = (urlparse(url).hostname or "").lower().strip(".")
    return bool(host and registrable_domain(host) == domain)


def _default_fetch(url: str, timeout: float, max_bytes: int) -> FetchResult:
    with httpx.Client(
        follow_redirects=True,
        timeout=timeout,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
    ) as client:
        deadline = time.monotonic() + timeout
        with client.stream("GET", url) as response:
            body = bytearray()
            for chunk in response.iter_bytes():
                if time.monotonic() > deadline:
                    raise TimeoutError("response exceeded total fetch deadline")
                remaining = max_bytes - len(body)
                if remaining <= 0:
                    break
                body.extend(chunk[:remaining])
                if len(body) >= max_bytes:
                    break
            encoding = response.encoding or "utf-8"
            text = bytes(body).decode(encoding, errors="replace")
            return FetchResult(
                requested_url=url,
                final_url=str(response.url),
                status_code=response.status_code,
                content_type=response.headers.get("content-type", ""),
                text=text,
            )


def robots_checker(base_url: str, *, timeout: float = 10.0) -> RobotsCheck:
    parsed = urlparse(base_url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    parser = RobotFileParser(robots_url)
    try:
        request = httpx.get(robots_url, timeout=timeout, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
        if request.status_code >= 400:
            return lambda _url: True
        parser.parse(request.text.splitlines())
    except (httpx.HTTPError, OSError):
        return lambda _url: True
    return lambda url: parser.can_fetch(USER_AGENT, url)


def _clean_email(value: str) -> str:
    email = unescape(value).strip().strip(".,;:()[]<>\"'").lower()
    if len(email) > 254 or "@" not in email or email.endswith(BAD_EMAIL_SUFFIXES):
        return ""
    local, host = email.rsplit("@", 1)
    if local in BAD_LOCAL_PARTS or not registrable_domain(host):
        return ""
    return email


def email_rank(email: str, page_url: str, evidence: str) -> int:
    local = email.split("@", 1)[0]
    groups = (
        (100, ("editor", "editorial", "newsroom", "pitch")),
        (95, ("advertis", "media", "sales", "commercial")),
        (90, ("contribut", "submission", "guest", "article")),
        (85, ("partner", "collab", "sponsor", "marketing", "pr@")),
        (70, ("contact", "hello", "info", "enquir")),
    )
    for score, terms in groups:
        if any(term in local for term in terms):
            return score
    haystack = f"{page_url} {evidence}".lower()
    for score, terms in groups:
        if any(term in haystack for term in terms):
            return score - 10
    return 50


def extract_public_email_evidence(html: str, source_url: str) -> list[EmailEvidence]:
    soup = BeautifulSoup(html, "html.parser")
    for node in soup(["script", "style", "noscript", "template"]):
        node.decompose()
    visible = " ".join(soup.stripped_strings)
    normalized_visible = OBFUSCATED_DOT.sub(".", OBFUSCATED_AT.sub("@", visible))
    candidates: list[tuple[str, str, str]] = []
    for match in EMAIL_PATTERN.finditer(normalized_visible):
        start = max(0, match.start() - 100)
        end = min(len(normalized_visible), match.end() + 100)
        candidates.append((match.group(1), normalized_visible[start:end], "visible_text"))
    for anchor in soup.select("a[href^='mailto:']"):
        raw = str(anchor.get("href") or "")[7:].split("?", 1)[0]
        evidence = " ".join(anchor.parent.stripped_strings)[:240] if anchor.parent else raw
        candidates.append((raw, evidence, "mailto"))

    best: dict[str, EmailEvidence] = {}
    for raw, evidence, method in candidates:
        email = _clean_email(raw)
        if not email:
            continue
        item = EmailEvidence(
            email=email,
            source_url=source_url,
            evidence_text=" ".join(evidence.split())[:500],
            discovery_method=method,
            rank=email_rank(email, source_url, evidence),
        )
        current = best.get(email)
        if current is None or (item.rank, item.discovery_method == "mailto") > (current.rank, current.discovery_method == "mailto"):
            best[email] = item
    return sorted(best.values(), key=lambda item: (-item.rank, item.email))


def _candidate_links(html: str, current_url: str, domain: str) -> Iterable[str]:
    soup = BeautifulSoup(html, "html.parser")
    ranked: list[tuple[int, str]] = []
    for anchor in soup.select("a[href]"):
        href = str(anchor.get("href") or "").strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        try:
            url = _normalized_page_url(urljoin(current_url, href))
        except ValueError:
            # Public pages occasionally contain malformed bracketed hosts that
            # urllib correctly rejects as invalid IPv6. One broken link must
            # not abort the rest of an otherwise valid domain crawl.
            continue
        if not url or not _same_site(url, domain):
            continue
        label = f"{url} {' '.join(anchor.stripped_strings)}".lower()
        priority = 0 if any(term in label for term in ("contact", "advertis", "write-for", "contribut", "media-kit", "partner", "sponsor")) else 1
        ranked.append((priority, url))
    seen: set[str] = set()
    for _priority, url in sorted(ranked):
        if url not in seen:
            seen.add(url)
            yield url


def crawl_domain(
    value: str,
    *,
    config: CrawlConfig = CrawlConfig(),
    fetcher: Fetcher = _default_fetch,
    allowed_by_robots: RobotsCheck | None = None,
    sleeper: Sleeper = time.sleep,
    on_progress: ProgressCallback | None = None,
) -> CrawlResult:
    domain = registrable_domain(value)
    if not domain:
        return CrawlResult(domain="", status="failed", error="invalid_public_domain")
    if config.resolve_public_dns and not host_is_public(domain):
        return CrawlResult(domain=domain, status="failed", error="domain_does_not_resolve_to_public_ip")

    base = f"https://{domain}"
    robots = allowed_by_robots or robots_checker(base, timeout=config.timeout_seconds)
    queue = deque(_normalized_page_url(urljoin(base, path)) for path in SEED_PATHS)
    queued = set(queue)
    visited: set[str] = set()
    attempts: list[PageAttempt] = []
    evidence: dict[str, EmailEvidence] = {}
    succeeded = 0
    last_request_at = 0.0
    crawl_deadline = time.monotonic() + max(60.0, config.max_runtime_seconds)

    def publish(event_type: str, *, url: str, page_status: str = "", status_code: int | None = None) -> None:
        if on_progress is None:
            return
        on_progress(
            {
                "event_type": event_type,
                "url": url,
                "page_status": page_status,
                "status_code": status_code,
                "pages_attempted": len(attempts),
                "pages_crawled": succeeded,
                "emails_found": len(evidence),
                "max_pages": max(1, config.max_pages),
            }
        )

    while queue and len(visited) < max(1, config.max_pages):
        url = queue.popleft()
        if not url or url in visited:
            continue
        if time.monotonic() >= crawl_deadline:
            attempts.append(PageAttempt(url=url, status="deadline_exceeded", error="domain crawl exceeded total runtime limit"))
            publish("page_crawled", url=url, page_status="deadline_exceeded")
            break
        visited.add(url)
        publish("page_started", url=url, page_status="fetching")
        if not robots(url):
            attempts.append(PageAttempt(url=url, status="robots_denied"))
            publish("page_crawled", url=url, page_status="robots_denied")
            continue
        elapsed = time.monotonic() - last_request_at
        if last_request_at and elapsed < config.host_delay_seconds:
            sleeper(config.host_delay_seconds - elapsed)

        response: FetchResult | None = None
        final_error = ""
        for attempt_number in range(1, max(1, config.max_attempts) + 1):
            try:
                last_request_at = time.monotonic()
                response = fetcher(url, config.timeout_seconds, config.max_response_bytes)
                if response.status_code >= 500 and attempt_number < config.max_attempts:
                    continue
                break
            except (httpx.HTTPError, OSError, TimeoutError) as exc:
                final_error = f"{type(exc).__name__}: {exc}"[:500]
                if attempt_number < config.max_attempts:
                    continue
        if response is None:
            attempts.append(PageAttempt(url=url, status="failed", error=final_error or "fetch_failed"))
            publish("page_crawled", url=url, page_status="failed")
            continue
        if response.status_code >= 400:
            attempts.append(PageAttempt(url=url, status="http_error", status_code=response.status_code))
            publish("page_crawled", url=url, page_status="http_error", status_code=response.status_code)
            continue
        if "html" not in response.content_type.lower():
            attempts.append(PageAttempt(url=url, status="non_html", status_code=response.status_code))
            publish("page_crawled", url=url, page_status="non_html", status_code=response.status_code)
            continue
        final_url = _normalized_page_url(response.final_url)
        if not final_url or not _same_site(final_url, domain):
            attempts.append(PageAttempt(url=url, status="cross_site_redirect", status_code=response.status_code))
            publish("page_crawled", url=url, page_status="cross_site_redirect", status_code=response.status_code)
            continue

        succeeded += 1
        attempts.append(PageAttempt(url=final_url, status="succeeded", status_code=response.status_code))
        evidence_before = len(evidence)
        for item in extract_public_email_evidence(response.text, final_url):
            current = evidence.get(item.email)
            if current is None or item.rank > current.rank:
                evidence[item.email] = item
        publish("page_crawled", url=final_url, page_status="succeeded", status_code=response.status_code)
        if len(evidence) > evidence_before:
            publish("email_found", url=final_url, page_status="succeeded", status_code=response.status_code)
        for discovered in _candidate_links(response.text, final_url, domain):
            if discovered not in queued and len(queued) < config.max_pages * 5:
                queued.add(discovered)
                queue.append(discovered)

    contacts = sorted(evidence.values(), key=lambda item: (-item.rank, item.email))[:3]
    if contacts:
        status = "email_found"
    elif succeeded:
        status = "no_email"
    else:
        status = "failed"
    return CrawlResult(
        domain=domain,
        status=status,
        pages_attempted=len(attempts),
        pages_succeeded=succeeded,
        contacts=contacts,
        attempts=attempts,
        error="" if succeeded else "no_public_html_pages_succeeded",
    )
