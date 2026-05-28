from __future__ import annotations

import csv
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse, urlunparse


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def clean_domain(value: str) -> str:
    raw = (value or "").strip().lower()
    raw = re.sub(r"^https?://", "", raw)
    raw = raw.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0].strip(".")
    if raw.startswith("www."):
        raw = raw[4:]
    return raw


def root_domain_from_url(value: str) -> str:
    parsed = urlparse(value if re.match(r"^https?://", value or "", re.I) else f"https://{value}")
    host = (parsed.netloc or "").lower().split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host


def is_au_domain(domain: str) -> bool:
    return clean_domain(domain).endswith((".com.au", ".au"))


AUSTRALIA_CONTEXT_RE = re.compile(
    r"\b("
    r"australia|australian|aussie|melbourne|sydney|brisbane|perth|adelaide|canberra|"
    r"hobart|darwin|gold coast|sunshine coast|geelong|newcastle|wollongong|regional australia|"
    r"nsw|victoria|queensland|western australia|south australia|tasmania"
    r")\b",
    re.I,
)


def is_australian_candidate_domain(domain: str, evidence_text: str = "") -> bool:
    cleaned = clean_domain(domain)
    if cleaned.endswith(".au"):
        return True
    if cleaned.endswith(".com") and AUSTRALIA_CONTEXT_RE.search(evidence_text or ""):
        return True
    return False


def normalize_url(value: str) -> str:
    parsed = urlparse(value)
    if not parsed.scheme or not parsed.netloc:
        return value.strip()
    normalized = parsed._replace(params="", query="", fragment="", path=parsed.path or "/")
    text = urlunparse(normalized)
    return text.rstrip("/") if text.rstrip("/") else text


def safe_slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip().lower()).strip("-")
    return slug[:160] or "item"


def first_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def write_csv(path: Path, rows: Iterable[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: "" if row.get(key) is None else row.get(key) for key in fieldnames})
    tmp.replace(path)
