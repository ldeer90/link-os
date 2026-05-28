from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlparse


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_domain(value: str) -> str:
    raw = (value or "").strip().lower()
    if "@" in raw and not raw.startswith(("http://", "https://")):
        raw = raw.rsplit("@", 1)[-1]
    parsed = urlparse(raw if raw.startswith(("http://", "https://")) else f"https://{raw}")
    host = (parsed.netloc or parsed.path).split("/")[0].split(":")[0].strip(".")
    return host[4:] if host.startswith("www.") else host
