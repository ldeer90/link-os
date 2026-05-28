from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape
from urllib.parse import unquote, urljoin, urlparse

from .utils import clean_domain
from .web import absolutize_same_site_links, parse_page


EMAIL_RE = re.compile(r"(?<![A-Z0-9._%+\-])([A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,})(?![A-Z0-9._%+\-])", re.I)
OBFUSCATED_EMAIL_RE = re.compile(
    r"\b([A-Z0-9._%+\-]+)\s*(?:\[?\s*at\s*\]?|\(\s*at\s*\)|&#64;|\s+at\s+)\s*"
    r"([A-Z0-9.\-]+)\s*(?:\[?\s*dot\s*\]?|\(\s*dot\s*\)|\s+dot\s+)\s*([A-Z]{2,}(?:\.[A-Z]{2,})?)\b",
    re.I,
)

NOISY_PREFIXES = {
    "abuse",
    "bounce",
    "donotreply",
    "do-not-reply",
    "example",
    "mailer-daemon",
    "no-reply",
    "noreply",
    "postmaster",
    "privacy",
    "security",
    "spam",
    "unsubscribe",
    "webmaster",
}

FREE_EMAIL_DOMAINS = {
    "gmail.com",
    "googlemail.com",
    "hotmail.com",
    "outlook.com",
    "live.com",
    "icloud.com",
    "me.com",
    "yahoo.com",
    "yahoo.com.au",
    "proton.me",
    "protonmail.com",
}

BLOCKED_EMAIL_TLDS = {
    "avif",
    "css",
    "gif",
    "jpeg",
    "jpg",
    "js",
    "png",
    "svg",
    "webp",
}

EMAIL_TYPE_PREFIXES: list[tuple[str, set[str]]] = [
    ("editor", {"editor", "editorial"}),
    ("content", {"content", "contribute", "submissions", "articles"}),
    ("advertising", {"advertising", "advertise", "ads", "sales"}),
    ("media", {"media", "press"}),
    ("partnerships", {"partnerships", "partners", "collaborations"}),
    ("sponsored", {"sponsored", "sponsor"}),
    ("generic", {"hello", "hi", "info", "contact", "enquiry", "enquiries", "inquiry", "inquiries", "admin"}),
]

CONTACT_PATHS = [
    "/write-for-us",
    "/guest-post",
    "/contribute",
    "/submit-an-article",
    "/advertise",
    "/advertise-with-us",
    "/media-kit",
    "/sponsored-posts",
    "/contact",
    "/contact-us",
    "/about",
    "/about-us",
]

LINK_HINT_RE = re.compile(
    r"(write[-_\s]?for[-_\s]?us|guest[-_\s]?post|contribut|submit[-_\s]?an[-_\s]?article|advertis|media[-_\s]?kit|sponsor|editorial|contact|about)",
    re.I,
)


@dataclass
class CandidateEmail:
    email: str
    email_type: str
    source_url: str
    source_page_type: str
    confidence: int


def normalize_email(value: str) -> str:
    email = unquote(unescape(value or "")).strip().lower()
    email = email.split("?", 1)[0].strip(".,;:()[]{}<>\"'")
    if not EMAIL_RE.fullmatch(email):
        return ""
    tld = email.rsplit(".", 1)[-1]
    if tld in BLOCKED_EMAIL_TLDS:
        return ""
    return email


def local_prefix(email: str) -> str:
    local = email.split("@", 1)[0].lower()
    return re.split(r"[._+\-]", local, maxsplit=1)[0]


def classify_email_type(email: str) -> str:
    prefix = local_prefix(email)
    for email_type, prefixes in EMAIL_TYPE_PREFIXES:
        if prefix in prefixes:
            return email_type
    return ""


def email_allowed(email: str, root_domain: str) -> bool:
    if not email:
        return False
    if local_prefix(email) in NOISY_PREFIXES:
        return False
    if not classify_email_type(email):
        return False
    domain = email.rsplit("@", 1)[-1].lower()
    if domain in FREE_EMAIL_DOMAINS:
        return False
    root = clean_domain(root_domain)
    return domain == root or domain.endswith(f".{root}")


def emails_from_html(html: str) -> set[str]:
    text = unescape(html or "")
    deobfuscated = re.sub(r"\s*(?:\[|\()?at(?:\]|\))?\s*", "@", text, flags=re.I)
    deobfuscated = re.sub(r"\s*(?:\[|\()?dot(?:\]|\))?\s*", ".", deobfuscated, flags=re.I)
    candidates = {normalize_email(match.group(1)) for match in EMAIL_RE.finditer(text)}
    candidates.update(normalize_email(match.group(1)) for match in EMAIL_RE.finditer(deobfuscated))
    for match in OBFUSCATED_EMAIL_RE.finditer(text):
        candidates.add(normalize_email(f"{match.group(1)}@{match.group(2)}.{match.group(3)}"))
    return {email for email in candidates if email}


def emails_from_mailto(html: str) -> set[str]:
    parser = parse_page(html)
    output: set[str] = set()
    for href, _anchor in parser.links:
        if href.lower().startswith("mailto:"):
            output.add(normalize_email(href.split(":", 1)[1]))
    return {email for email in output if email}


def page_type_for_url(url: str) -> str:
    path = urlparse(url).path.lower()
    if "write-for-us" in path or "guest" in path or "contribute" in path or "submit" in path:
        return "editorial"
    if "advertis" in path or "media-kit" in path or "sponsor" in path:
        return "advertising"
    if "contact" in path:
        return "contact"
    if "about" in path:
        return "about"
    return "page"


def confidence(email: str, source_page_type: str, from_mailto: bool) -> int:
    score = 40
    if classify_email_type(email) in {"editor", "content", "advertising", "media", "partnerships", "sponsored"}:
        score += 30
    else:
        score += 15
    if source_page_type in {"editorial", "advertising"}:
        score += 20
    elif source_page_type == "contact":
        score += 12
    if from_mailto:
        score += 10
    return min(score, 100)


def candidate_page_urls(root_domain: str, seed_url: str, homepage_url: str, homepage_html: str) -> list[str]:
    base = f"{urlparse(homepage_url).scheme}://{urlparse(homepage_url).netloc}"
    candidates = [seed_url, homepage_url]
    candidates.extend(urljoin(base + "/", path.lstrip("/")) for path in CONTACT_PATHS)
    parser = parse_page(homepage_html)
    for link, anchor in absolutize_same_site_links(homepage_url, root_domain, parser.links):
        if link.lower().startswith("mailto:"):
            continue
        if LINK_HINT_RE.search(f"{link} {anchor}"):
            candidates.append(link)
    seen: set[str] = set()
    output: list[str] = []
    for url in candidates:
        if not url or url in seen:
            continue
        seen.add(url)
        output.append(url)
    return output
