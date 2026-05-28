from __future__ import annotations

from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from typing import Iterable
from urllib.error import HTTPError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from .utils import clean_domain, normalize_url, root_domain_from_url


USER_AGENT = "GuestPostProspecting/0.1 (+https://ldsearch.com.au)"
MAX_BYTES = 1_500_000


@dataclass
class PageFetch:
    requested_url: str
    final_url: str = ""
    status: str = ""
    html: str = ""
    error_message: str = ""


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self.title = ""
        self.text_parts: list[str] = []
        self._href = ""
        self._anchor_parts: list[str] = []
        self._in_title = False
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
        if tag == "a":
            for key, value in attrs:
                if key.lower() == "href" and value:
                    self._href = value.strip()
                    self._anchor_parts = []
                    break

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = " ".join(unescape(data).split())
        if not text:
            return
        if self._in_title:
            self.title = f"{self.title} {text}".strip()
        if self._href:
            self._anchor_parts.append(text)
        self.text_parts.append(text)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"} and self._skip_depth:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False
        if tag == "a" and self._href:
            self.links.append((self._href, " ".join(self._anchor_parts).strip()))
            self._href = ""
            self._anchor_parts = []

    @property
    def visible_text(self) -> str:
        return " ".join(self.text_parts)


def parse_page(html: str) -> PageParser:
    parser = PageParser()
    try:
        parser.feed(html or "")
    except Exception:
        pass
    return parser


def fetch_html(url: str, timeout: int = 15) -> PageFetch:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    result = PageFetch(requested_url=url)
    try:
        with urlopen(request, timeout=timeout) as response:
            content_type = response.headers.get("Content-Type", "")
            if content_type and not any(token in content_type.lower() for token in ["text/html", "text/plain", "xhtml"]):
                result.final_url = response.geturl()
                result.status = "skipped_non_html"
                return result
            charset = response.headers.get_content_charset() or "utf-8"
            body = response.read(MAX_BYTES + 1)
            result.final_url = response.geturl()
            result.status = f"http_{getattr(response, 'status', 200)}"
            result.html = body[:MAX_BYTES].decode(charset, errors="replace")
            return result
    except HTTPError as exc:
        result.final_url = exc.geturl()
        result.status = f"http_{exc.code}"
        result.error_message = str(exc)
        return result
    except Exception as exc:
        result.status = "fetch_error"
        result.error_message = str(exc)
        return result


def same_site(url: str, root_domain: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    host = clean_domain(parsed.netloc)
    root = clean_domain(root_domain)
    return host == root or host.endswith(f".{root}")


def homepage_candidates(root_domain: str) -> list[str]:
    return [f"https://{root_domain}/", f"https://www.{root_domain}/", f"http://{root_domain}/"]


def resolve_homepage(root_domain: str, timeout: int = 15) -> PageFetch:
    last = PageFetch(requested_url=f"https://{root_domain}/", status="fetch_error", error_message="not tried")
    for url in homepage_candidates(root_domain):
        last = fetch_html(url, timeout=timeout)
        if last.html:
            return last
    return last


def absolutize_same_site_links(base_url: str, root_domain: str, links: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    output: list[tuple[str, str]] = []
    seen: set[str] = set()
    for href, anchor in links:
        if href.lower().startswith("mailto:"):
            output.append((href, anchor))
            continue
        absolute = normalize_url(urljoin(base_url, href))
        if not same_site(absolute, root_domain):
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        output.append((absolute, anchor))
    return output


def site_name_from_title(title: str, root_domain: str) -> str:
    clean = " ".join((title or "").split())
    for sep in [" | ", " - ", " – ", " — "]:
        if sep in clean:
            clean = clean.split(sep, 1)[0].strip()
            break
    if clean and len(clean) <= 80:
        return clean
    stem = root_domain_from_url(root_domain).split(".")[0].replace("-", " ").replace("_", " ")
    return stem.title()
