from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
PROSPECTING_ROOT = REPO_ROOT / "prospecting"
PROSPECTING_DB = PROSPECTING_ROOT / "data" / "guest_post_prospecting.db"


def _load_prospecting() -> None:
    if not PROSPECTING_ROOT.exists():
        raise RuntimeError(f"Prospecting package is missing: {PROSPECTING_ROOT}")
    path = str(PROSPECTING_ROOT)
    if path not in sys.path:
        sys.path.insert(0, path)


def queue_domains(domains: list[str], *, source_label: str = "link_os_dashboard") -> dict[str, Any]:
    _load_prospecting()
    from guest_post_prospecting.universe import import_seed_domains

    return import_seed_domains(domains, source_label=source_label, allow_non_au=True)


def run_limited_crawl(*, limit: int = 25, workers: int = 4, timeout: int = 8) -> dict[str, Any]:
    _load_prospecting()
    from guest_post_prospecting.universe import crawl_and_score_universe_parallel

    return crawl_and_score_universe_parallel(limit=limit, retry_errors=False, timeout=timeout, workers=workers)


def prospecting_stats() -> dict[str, int]:
    if not PROSPECTING_DB.exists():
        return {"candidate_domains": 0, "pending_urls": 0, "scraped_urls": 0, "emails_found": 0}
    with sqlite3.connect(PROSPECTING_DB) as connection:
        tables = {row[0] for row in connection.execute("select name from sqlite_master where type='table'").fetchall()}

        def count(table: str, where: str = "") -> int:
            if table not in tables:
                return 0
            return int(connection.execute(f"select count(*) from {table} {where}").fetchone()[0])

        return {
            "candidate_domains": count("candidate_domains"),
            "pending_urls": count("crawl_queue", "where status='pending'"),
            "scraped_urls": count("crawl_results"),
            "emails_found": count("guest_post_contact_emails"),
        }
